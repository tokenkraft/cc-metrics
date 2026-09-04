from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import urllib.error
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import alert_notify  # noqa: E402


def alert(name: str, state: str = "firing", summary: str = "") -> dict:
    return {
        "labels": {"alertname": name},
        "state": state,
        "annotations": {"summary": summary} if summary else {},
    }


def api_payload(*alerts: dict) -> bytes:
    return json.dumps({"status": "success", "data": {"alerts": list(alerts)}}).encode()


@contextmanager
def harness(payload, state_contents: str | None = None):
    """Run main() against a canned API response and an isolated state file."""
    with tempfile.TemporaryDirectory() as tmp:
        state_path = Path(tmp) / "alert-notify.state"
        if state_contents is not None:
            state_path.write_text(state_contents)
        opener = (
            mock.MagicMock(side_effect=payload)
            if isinstance(payload, Exception)
            else mock.MagicMock(return_value=io.BytesIO(payload))
        )
        with (
            mock.patch.object(alert_notify, "STATE_PATH", state_path),
            mock.patch.object(alert_notify.urllib.request, "urlopen", opener),
            mock.patch.object(alert_notify.subprocess, "run") as run,
            mock.patch.object(alert_notify, "log") as log,
        ):
            yield state_path, run, log


class AlertNotifyTests(unittest.TestCase):
    def test_new_firing_alert_notifies_and_is_recorded(self):
        payload = api_payload(
            alert("ClaudeLaneDarkWhileOthersActive", summary="Claude dark")
        )
        with harness(payload, state_contents=None) as (state_path, run, log):
            self.assertEqual(alert_notify.main(), 0)
            self.assertEqual(run.call_count, 1)
            script = run.call_args.args[0][2]
            self.assertIn("Claude dark", script)
            self.assertIn("ClaudeLaneDarkWhileOthersActive", script)
            self.assertEqual(
                state_path.read_text(), "ClaudeLaneDarkWhileOthersActive\n"
            )
            self.assertIn(
                "FIRING ClaudeLaneDarkWhileOthersActive", log.call_args_list[0].args[0]
            )

    def test_already_notified_alert_stays_silent(self):
        payload = api_payload(
            alert("ClaudeLaneDarkWhileOthersActive", summary="Claude dark")
        )
        with harness(payload, state_contents="ClaudeLaneDarkWhileOthersActive\n") as (
            state_path,
            run,
            _log,
        ):
            self.assertEqual(alert_notify.main(), 0)
            run.assert_not_called()
            self.assertEqual(
                state_path.read_text(), "ClaudeLaneDarkWhileOthersActive\n"
            )

    def test_resolved_alert_clears_state_so_a_recurrence_notifies_again(self):
        with harness(
            api_payload(), state_contents="ClaudeLaneDarkWhileOthersActive\n"
        ) as (
            state_path,
            run,
            log,
        ):
            self.assertEqual(alert_notify.main(), 0)
            run.assert_not_called()
            self.assertEqual(state_path.read_text(), "")
            self.assertIn(
                "RESOLVED ClaudeLaneDarkWhileOthersActive",
                log.call_args_list[0].args[0],
            )

    def test_pending_alert_is_not_delivered(self):
        payload = api_payload(alert("ClaudeLaneDarkWhileOthersActive", state="pending"))
        with harness(payload, state_contents=None) as (state_path, run, _log):
            self.assertEqual(alert_notify.main(), 0)
            run.assert_not_called()
            self.assertEqual(state_path.read_text(), "")

    def test_unreachable_prometheus_fails_loud_and_keeps_state(self):
        error = urllib.error.URLError("connection refused")
        with harness(error, state_contents="ClaudeLaneDarkWhileOthersActive\n") as (
            state_path,
            run,
            _log,
        ):
            self.assertEqual(alert_notify.main(), 1)
            run.assert_not_called()
            self.assertEqual(
                state_path.read_text(), "ClaudeLaneDarkWhileOthersActive\n"
            )


if __name__ == "__main__":
    unittest.main()
