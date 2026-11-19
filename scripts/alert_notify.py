#!/usr/bin/env python3
"""Local-only delivery for Prometheus alerts.

The stack runs no Alertmanager, so a firing rule is visible only to whoever
happens to open Prometheus or Grafana. That is how a telemetry outage runs
unnoticed for as long as nobody looks: the data is missing in plain sight with
nothing to say so.

This polls the local Prometheus alerts API and raises a macOS notification the
first time each alert enters `firing`, then again every REMIND_AFTER_SECONDS
while it keeps firing.
Nothing leaves the machine: the only network call is to localhost, and the only
sink is `osascript`. State (alertname and last notification time) lives under
the runtime dir so a restart of the launchd job does not re-notify early.

Stdlib only. Runs under launchd (com.tokenkraft.cc-metrics.alert-notify).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ALERTS_URL = "http://localhost:9090/api/v1/alerts"
STATE_PATH = Path(__file__).resolve().parent.parent / "runtime" / "alert-notify.state"
OSASCRIPT = "/usr/bin/osascript"
TIMEOUT_SECONDS = 10
REMIND_AFTER_SECONDS = 6 * 60 * 60


def firing_alerts() -> dict[str, dict]:
    """Return firing alerts keyed by alertname (one notification per rule)."""
    with urllib.request.urlopen(ALERTS_URL, timeout=TIMEOUT_SECONDS) as response:
        payload = json.load(response)
    if payload.get("status") != "success":
        raise RuntimeError(f"Prometheus alerts API returned {payload.get('status')!r}")
    return {
        alert["labels"]["alertname"]: alert
        for alert in payload["data"]["alerts"]
        if alert.get("state") == "firing"
    }


def notify(name: str, alert: dict, still_firing: bool = False) -> None:
    body = alert.get("annotations", {}).get("summary") or name
    subtitle = f"{name} still firing" if still_firing else name
    script = (
        f"display notification {json.dumps(body)} "
        f'with title "cc-metrics alert" subtitle {json.dumps(subtitle)}'
    )
    subprocess.run([OSASCRIPT, "-e", script], check=False)


def read_state(now: int) -> dict[str, int]:
    """alertname -> unix time of its last notification. A bare name is the
    format from before reminders; it counts as notified now, so an upgrade
    does not re-notify every known alert at once."""
    if not STATE_PATH.exists():
        return {}
    state: dict[str, int] = {}
    for line in STATE_PATH.read_text().splitlines():
        name, _, stamp = line.strip().partition("\t")
        if name:
            state[name] = int(stamp) if stamp.isdigit() else now
    return state


def write_state(state: dict[str, int]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text("".join(f"{name}\t{state[name]}\n" for name in sorted(state)))


def log(message: str) -> None:
    print(
        f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {message}",
        flush=True,
    )


def main() -> int:
    try:
        current = firing_alerts()
    except (urllib.error.URLError, TimeoutError, RuntimeError, KeyError) as exc:
        # Prometheus down is its own signal, and the watchdog owns that job.
        # Exit non-zero so launchd records it rather than reporting all-clear.
        log(f"could not read alerts: {exc}")
        return 1

    now = int(time.time())
    known = read_state(now)
    state: dict[str, int] = {}
    for name in sorted(current):
        last = known.get(name)
        if last is None:
            notify(name, current[name])
            log(f"FIRING {name}")
            state[name] = now
        elif now - last >= REMIND_AFTER_SECONDS:
            notify(name, current[name], still_firing=True)
            log(f"STILL FIRING {name}")
            state[name] = now
        else:
            state[name] = last
    for name in sorted(set(known) - set(current)):
        log(f"RESOLVED {name}")
    write_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
