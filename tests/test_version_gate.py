"""Behaviour tests for scripts/version-gate.sh.

The gate exists because both known Claude telemetry outages coincide with a
tool version change, and neither was noticed. Its contract has three parts,
each of which fails differently if broken:

- it must run the canary when a version changes, or it detects nothing;
- it must NOT run the canary otherwise, or every watchdog tick spends a
  model call;
- it must exit 0 even when the canary fails, or a telemetry fault wedges the
  watchdog that keeps the stack up.

A stub canary stands in for the real one so these tests spend no tokens.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE = REPO_ROOT / "scripts" / "version-gate.sh"

STUB_TEMPLATE = """#!/bin/sh
echo ran >>"{marker}"
exit {code}
"""


class VersionGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.state_dir = self.tmp / "state"
        self.marker = self.tmp / "canary-ran"

    def _stub(self, code: int) -> Path:
        stub = self.tmp / f"stub-{code}.sh"
        stub.write_text(STUB_TEMPLATE.format(marker=self.marker, code=code))
        stub.chmod(0o755)
        return stub

    def _run(
        self, canary: Path | str, search_path: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["CC_METRICS_STATE_DIR"] = str(self.state_dir)
        env["CC_METRICS_CANARY"] = str(canary)
        if search_path is not None:
            env["CC_METRICS_PATH"] = search_path
        return subprocess.run(
            [str(GATE)], capture_output=True, text=True, env=env, check=False
        )

    def _canary_runs(self) -> int:
        if not self.marker.exists():
            return 0
        return len(self.marker.read_text().split())

    def _write_state(self, text: str) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "tool-versions.state").write_text(text)

    def test_baseline_records_state_without_running_canary(self) -> None:
        """First run has nothing to compare against, so spending a call is waste."""
        result = self._run(self._stub(0))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._canary_runs(), 0)
        self.assertTrue((self.state_dir / "tool-versions.state").exists())

    def test_unchanged_versions_do_not_run_canary(self) -> None:
        """Steady state must be free; the watchdog ticks constantly."""
        stub = self._stub(0)
        self._run(stub)
        result = self._run(stub)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._canary_runs(), 0)
        self.assertIn("no version change", result.stdout)

    def test_version_change_runs_canary(self) -> None:
        self._write_state("claude=0.0.0-stale\npaseo=0.0.0-stale\n")
        result = self._run(self._stub(0))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._canary_runs(), 1)
        self.assertIn("version change detected", result.stdout)

    def test_canary_failure_still_exits_zero(self) -> None:
        """A telemetry fault must not stop the watchdog keeping the stack up."""
        self._write_state("claude=0.0.0-stale\npaseo=0.0.0-stale\n")
        result = self._run(self._stub(1))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._canary_runs(), 1)
        self.assertIn("canary FAIL", result.stdout)

    def test_could_not_run_is_distinct_from_failure(self) -> None:
        """Exit 2 means the check never ran; reporting it as DARK cries wolf."""
        self._write_state("claude=0.0.0-stale\npaseo=0.0.0-stale\n")
        result = self._run(self._stub(2))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("could not run", result.stdout)
        self.assertNotIn("canary FAIL", result.stdout)

    def test_state_is_updated_even_when_canary_fails(self) -> None:
        """Otherwise one bad upgrade re-runs the canary on every later tick."""
        self._write_state("claude=0.0.0-stale\npaseo=0.0.0-stale\n")
        self._run(self._stub(1))
        recorded = (self.state_dir / "tool-versions.state").read_text()
        self.assertNotIn("0.0.0-stale", recorded)

        second = self._run(self._stub(1))
        self.assertIn("no version change", second.stdout)
        self.assertEqual(self._canary_runs(), 1)

    def test_missing_canary_is_reported_not_crashed(self) -> None:
        self._write_state("claude=0.0.0-stale\npaseo=0.0.0-stale\n")
        result = self._run(self.tmp / "does-not-exist.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("canary missing", result.stdout)

    def test_absent_claude_is_loud_not_a_silent_baseline(self) -> None:
        """The gate pinned one node version and resolved nothing without it.

        On any machine with a different version both tools read `absent`, that
        became the recorded baseline, and every later run reported "no version
        change" — a detector permanently silent while logging success. Recording
        an absent claude as a baseline is the step that makes it permanent.
        """
        empty = self.tmp / "empty-bin"
        empty.mkdir()
        result = self._run(self._stub(0), search_path=str(empty))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("claude not found", result.stdout)
        self.assertEqual(self._canary_runs(), 0)
        self.assertFalse(
            (self.state_dir / "tool-versions.state").exists(),
            "absent claude must not be recorded as the baseline",
        )

    def test_node_bin_is_discovered_without_pinning_a_version(self) -> None:
        """Any pinned interpreter version is a portability bug for teammates."""
        gate_source = GATE.read_text()
        self.assertNotRegex(
            gate_source,
            r"\.nvm/versions/node/v[0-9]+\.[0-9]+\.[0-9]+",
            "version-gate must glob node versions, not pin one",
        )


if __name__ == "__main__":
    unittest.main()
