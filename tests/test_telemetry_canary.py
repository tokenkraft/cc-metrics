"""Gate tests for scripts/telemetry-canary.sh.

The canary's value is that it fails when telemetry is dark, so these tests feed
its two parsing seams - ``--check-log`` and ``--check-delta`` - the shapes that
a dark session actually produced, and assert the script rejects them. No live
model call is needed for any of this.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANARY = ROOT / "scripts" / "telemetry-canary.sh"

PREFIX = "2026-09-03T12:55:25.424Z [DEBUG] [3P telemetry] "
ENABLED = f"{PREFIX}isTelemetryEnabled=true (CLAUDE_CODE_ENABLE_TELEMETRY=1)"
READERS_OTLP = (
    f'{PREFIX}getOtlpReaders: types=["otlp"], interval=60000, '
    "protocol=grpc, endpoint=http://localhost:4317"
)
# The measured silent failure: telemetry enabled, exporting nowhere.
READERS_NONE = (
    f"{PREFIX}getOtlpReaders: types=[], interval=60000, "
    "protocol=grpc, endpoint=undefined"
)
EXPORT_SUCCESS = f"{PREFIX}First metrics export: SUCCESS"
EXPORT_FAILED = f"{PREFIX}First metrics export: FAILED"

METRIC = "claude_code_token_usage_tokens_total"
# One canary-model series per app_entrypoint, plus series that must not count:
# output tokens, the dated model label, and another model entirely.
SCRAPE = "\n".join(
    (
        f"# TYPE {METRIC} counter",
        f'{METRIC}{{env="test-env",model="claude-haiku-4-5",type="input"}} 5',
        f'{METRIC}{{app_entrypoint="sdk-cli",env="test-env",'
        f'model="claude-haiku-4-5",type="input"}} 60',
        f'{METRIC}{{app_entrypoint="sdk-cli",env="test-env",'
        f'model="claude-haiku-4-5",type="output"}} 4543',
        f'{METRIC}{{app_entrypoint="sdk-cli",env="test-env",'
        f'model="claude-haiku-4-5-20251001",type="input"}} 326996',
        f'{METRIC}{{env="test-env",model="claude-sonnet-4-5",type="input"}} 999',
        "",
    )
)
CANARY_INPUT_TOTAL = 65  # 5 + 60, the two series the gate is allowed to count


class TelemetryCanaryTests(unittest.TestCase):
    def setUp(self) -> None:
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.workspace = Path(workspace.name)

    def fixture(self, name: str, *lines: str) -> str:
        target = self.workspace / name
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(target)

    def scrape(self, name: str, replacements: dict[str, str] | None = None) -> str:
        content = SCRAPE
        for old, new in (replacements or {}).items():
            self.assertIn(old, content)
            content = content.replace(old, new)
        target = self.workspace / name
        target.write_text(content, encoding="utf-8")
        return str(target)

    def run_canary(
        self, *args: str, overrides: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(CANARY), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
            env={**os.environ, **(overrides or {})},
        )

    # --- gate 1: the debug log has to prove an export happened ---------------

    def test_export_success_line_passes(self) -> None:
        log = self.fixture("good.log", ENABLED, READERS_OTLP, EXPORT_SUCCESS)
        result = self.run_canary("--check-log", log)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("gate 1 export:  PASS", result.stdout)

    def test_enabled_flag_without_export_fails(self) -> None:
        """isTelemetryEnabled=true is not evidence: three real sessions had it
        while exporting nothing."""
        log = self.fixture("enabled_only.log", ENABLED, READERS_OTLP)
        result = self.run_canary("--check-log", log)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("gate 1 export:  FAIL", result.stdout)
        self.assertIn("telemetry-canary: FAIL", result.stdout)

    def test_failed_export_is_not_mistaken_for_success(self) -> None:
        log = self.fixture("failed.log", ENABLED, READERS_OTLP, EXPORT_FAILED)
        result = self.run_canary("--check-log", log)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("gate 1 export:  FAIL", result.stdout)

    def test_empty_otlp_reader_list_fails(self) -> None:
        log = self.fixture("no_readers.log", ENABLED, READERS_NONE, EXPORT_SUCCESS)
        result = self.run_canary("--check-log", log)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("gate 1 readers: FAIL", result.stdout)

    def test_missing_debug_log_is_a_setup_failure(self) -> None:
        result = self.run_canary("--check-log", str(self.workspace / "absent.log"))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("not readable", result.stderr)

    # --- gate 2: the collector's own counter has to move ---------------------

    def test_counter_growth_across_entrypoints_passes(self) -> None:
        before = self.scrape("before.txt")
        after = self.scrape(
            "after.txt",
            {
                'model="claude-haiku-4-5",type="input"} 60': (
                    'model="claude-haiku-4-5",type="input"} 70'
                )
            },
        )
        result = self.run_canary("--check-delta", before, after)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            f"input tokens {CANARY_INPUT_TOTAL} -> {CANARY_INPUT_TOTAL + 10} "
            "(delta +10",
            result.stdout,
        )

    def test_unchanged_counter_fails(self) -> None:
        before = self.scrape("before.txt")
        after = self.scrape("after.txt")
        result = self.run_canary("--check-delta", before, after)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("gate 2 delta:   FAIL", result.stdout)

    def test_output_tokens_do_not_satisfy_the_input_gate(self) -> None:
        before = self.scrape("before.txt")
        after = self.scrape(
            "after.txt", {'type="output"} 4543': 'type="output"} 99999'}
        )
        result = self.run_canary("--check-delta", before, after)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"input tokens {CANARY_INPUT_TOTAL} -> ", result.stdout)

    def test_other_model_growth_does_not_satisfy_the_gate(self) -> None:
        """The canary model is the point: a busy neighbouring series must not
        be able to carry the gate."""
        before = self.scrape("before.txt")
        after = self.scrape(
            "after.txt",
            {
                'model="claude-haiku-4-5-20251001",type="input"} 326996': (
                    'model="claude-haiku-4-5-20251001",type="input"} 999999'
                ),
                'model="claude-sonnet-4-5",type="input"} 999': (
                    'model="claude-sonnet-4-5",type="input"} 888888'
                ),
            },
        )
        result = self.run_canary("--check-delta", before, after)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("(delta +0", result.stdout)

    # --- setup failures are reported, not crashed on ------------------------

    def test_unknown_argument_is_a_setup_failure(self) -> None:
        result = self.run_canary("--not-a-flag")
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("unknown argument", result.stderr)

    @unittest.skipUnless(
        shutil.which("curl"), "curl is required to reach the collector"
    )
    def test_unreachable_collector_fails_before_spending_a_model_call(self) -> None:
        # Port 1 is privileged and unused, so the connection is refused at once.
        result = self.run_canary(
            overrides={"CANARY_METRICS_URL": "http://127.0.0.1:1/metrics"}
        )
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("collector metrics endpoint unreachable", result.stderr)
        self.assertNotIn("canary: reply", result.stdout)


if __name__ == "__main__":
    unittest.main()
