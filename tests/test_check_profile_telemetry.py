"""Behaviour tests for scripts/check_profile_telemetry.py.

The check exists because telemetry config is per-profile and one profile is one
account. Adding an account adds a config nothing verifies, and the resulting
dark lane is invisible to every other alert in the stack: a profile with no
`env` block contributes to neither the OTLP lane nor the transcript witness, so
the capture ratio stays healthy while that account's usage is absent from both
sides of it.

Each test below pins one way the check can quietly stop being a check.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from check_profile_telemetry import (  # noqa: E402
    REQUIRED_KEYS,
    discover_config_dirs,
    main,
)

ENDPOINT = "http://localhost:4317"


def telemetry_env(endpoint: str = ENDPOINT) -> dict:
    return {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
        "OTEL_EXPORTER_OTLP_ENDPOINT": endpoint,
    }


class ProfileTelemetryCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.canonical = self.home / ".claude"
        self.canonical.mkdir()
        self.profiles = self.home / ".claude-profiles"
        self.profiles.mkdir()

    def _settings(self, config_dir: Path, env: dict | None) -> None:
        config_dir.mkdir(parents=True, exist_ok=True)
        payload: dict = {} if env is None else {"env": env}
        (config_dir / "settings.json").write_text(json.dumps(payload))

    def _profile(self, name: str, env: dict | None) -> Path:
        d = self.profiles / name
        self._settings(d, env)
        return d

    def _run(self, claude_home: Path | None = None) -> int:
        return main(["--claude-home", str(claude_home or self.canonical)])

    def test_all_profiles_configured_passes(self) -> None:
        self._settings(self.canonical, telemetry_env())
        self._profile("acct-one", telemetry_env())
        self.assertEqual(self._run(), 0)

    def test_profile_missing_env_block_is_reported(self) -> None:
        """The dark-from-birth case: a new account nobody configured."""
        self._settings(self.canonical, telemetry_env())
        self._profile("acct-new", None)
        self.assertEqual(self._run(), 1)

    def test_each_required_key_is_load_bearing(self) -> None:
        """Drop any one key and the profile stops exporting, so all must fail."""
        for key in REQUIRED_KEYS:
            with self.subTest(key=key):
                env = telemetry_env()
                del env[key]
                self._settings(self.canonical, env)
                self.assertEqual(self._run(), 1, f"{key} missing must fail")

    def test_empty_value_counts_as_missing(self) -> None:
        env = telemetry_env()
        env["OTEL_EXPORTER_OTLP_ENDPOINT"] = "   "
        self._settings(self.canonical, env)
        self.assertEqual(self._run(), 1)

    def test_divergent_endpoints_are_reported(self) -> None:
        """A profile exporting elsewhere still exports, so no liveness alert
        fires, but its tokens never reach this collector."""
        self._settings(self.canonical, telemetry_env())
        self._profile("acct-elsewhere", telemetry_env("http://10.0.0.9:4317"))
        self.assertEqual(self._run(), 1)

    def test_non_profile_directories_are_skipped(self) -> None:
        """`~/.claude-profiles/bin` exists on a real host. Reporting it as dark
        is noise, and noise trains the operator to ignore the check."""
        self._settings(self.canonical, telemetry_env())
        (self.profiles / "bin").mkdir()
        self.assertEqual(self._run(), 0)
        self.assertEqual(len(discover_config_dirs(self.canonical)), 1)

    def test_entry_from_a_profile_still_finds_its_siblings(self) -> None:
        """CLAUDE_CONFIG_DIR usually points at a profile, not the canonical dir.

        Searching for a `.claude-profiles` sibling from there lands a level too
        deep and audits only a subset of the profiles, reporting full coverage
        that was never checked.
        """
        self._settings(self.canonical, telemetry_env())
        entry = self._profile("acct-one", telemetry_env())
        self._profile("acct-dark", None)

        found = discover_config_dirs(entry)
        self.assertEqual(len(found), 3, found)
        self.assertEqual(self._run(claude_home=entry), 1)

    def test_unparseable_settings_is_a_gap_not_a_crash(self) -> None:
        self._settings(self.canonical, telemetry_env())
        broken = self.profiles / "acct-broken"
        broken.mkdir()
        (broken / "settings.json").write_text("{not json")
        self.assertEqual(self._run(), 1)

    def test_explicitly_disabled_profile_is_not_ok(self) -> None:
        """Every key present and non-empty, telemetry switched off.

        Checking only for a non-empty string reports this profile healthy. An
        adversarial audit produced exactly this fixture and got `0 gap(s)`.
        """
        self._settings(self.canonical, telemetry_env())
        env = telemetry_env()
        env["CLAUDE_CODE_ENABLE_TELEMETRY"] = "0"
        env["OTEL_METRICS_EXPORTER"] = "none"
        self._profile("acct-off", env)
        self.assertEqual(self._run(), 1)

    def test_each_disabling_value_is_rejected(self) -> None:
        for key, value in (
            ("CLAUDE_CODE_ENABLE_TELEMETRY", "0"),
            ("CLAUDE_CODE_ENABLE_TELEMETRY", "false"),
            ("OTEL_METRICS_EXPORTER", "none"),
            ("OTEL_METRICS_EXPORTER", "console"),
            ("OTEL_EXPORTER_OTLP_PROTOCOL", "carrier-pigeon"),
        ):
            with self.subTest(key=key, value=value):
                env = telemetry_env()
                env[key] = value
                self._settings(self.canonical, env)
                self.assertEqual(self._run(), 1, f"{key}={value} must fail")

    def test_local_settings_can_disable_a_healthy_looking_profile(self) -> None:
        """settings.local.json overrides settings.json, so reading only the
        latter reports a lane that exports nothing as healthy."""
        self._settings(self.canonical, telemetry_env())
        profile = self._profile("acct-local-off", telemetry_env())
        (profile / "settings.local.json").write_text(
            json.dumps({"env": {"CLAUDE_CODE_ENABLE_TELEMETRY": "false"}})
        )
        self.assertEqual(self._run(), 1)

    def test_local_settings_may_also_supply_the_block(self) -> None:
        """The override cuts both ways; a bare settings.json is not a gap if
        settings.local.json carries the values."""
        self._settings(self.canonical, telemetry_env())
        profile = self._profile("acct-local-on", {})
        (profile / "settings.local.json").write_text(
            json.dumps({"env": telemetry_env()})
        )
        self.assertEqual(self._run(), 0)

    def test_missing_claude_home_cannot_report_success(self) -> None:
        self.assertEqual(self._run(claude_home=self.home / "absent"), 2)


if __name__ == "__main__":
    unittest.main()
