#!/usr/bin/env python3
"""Assert every Claude profile is configured to export telemetry.

Telemetry config lives in the `env` block of a settings file, and each profile
has its own. One profile is one account, so adding an account adds a config that
nothing verifies: the new lane is dark from birth, exit code 0, transcripts
written normally, no warning anywhere.

That gap is invisible to the rest of the stack. `ClaudeTelemetryCaptureLoss`
compares the OTLP lane against the transcript witness, and both move together
when a profile is missing its `env` block - the account contributes to neither -
so the ratio stays healthy while its usage is absent from both sides.

Endpoint divergence is checked for the same reason. A profile pointing somewhere
else still exports, so no liveness alert fires, but its tokens never reach this
collector and the capture ratio cannot see the shortfall.

Static and free: it reads a few small JSON files and spends no model call, so it
runs on every watchdog tick rather than only on a version change. Profiles are
added at arbitrary times, not at upgrades.

Exit 0 when every profile is covered, 1 when any gap is found, 2 when the check
could not run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Enough to make a session export. OTEL_METRICS_INCLUDE_ENTRYPOINT is
# deliberately absent: it enriches a label and its absence loses a dimension,
# not the lane.
#
# Values are checked, not just presence. Testing only for a non-empty string
# passes `CLAUDE_CODE_ENABLE_TELEMETRY: "0"` and `OTEL_METRICS_EXPORTER: "none"`
# - a profile that is switched off explicitly, reported as healthy. A checker
# that cannot fail on a disabled profile is not a checker.
#
# None means "any non-empty value": an endpoint is site-specific and this has no
# way to know which one is right.
REQUIRED_KEYS: dict[str, tuple[str, ...] | None] = {
    "CLAUDE_CODE_ENABLE_TELEMETRY": ("1", "true"),
    "OTEL_METRICS_EXPORTER": ("otlp",),
    "OTEL_EXPORTER_OTLP_PROTOCOL": ("grpc", "http/protobuf", "http/json"),
    "OTEL_EXPORTER_OTLP_ENDPOINT": None,
}


def discover_config_dirs(claude_home: Path) -> list[Path]:
    """The canonical config dir plus every sibling profile.

    Resolved relative to `claude_home` rather than the real home so a caller
    pointed at one tree cannot silently pull in another.

    A directory counts as a profile only if it holds `settings.json` or
    `projects`. `~/.claude-profiles` also accumulates non-profile directories -
    a `bin`, for instance - and reporting those as untelemetered would be noise
    that trains the operator to ignore the check.
    """
    # CLAUDE_CONFIG_DIR often already points at a profile rather than the
    # canonical dir. Looking for a `.claude-profiles` sibling from there lands a
    # level too deep and finds only a subset of the profiles - a coverage check
    # that under-reports coverage is worse than none.
    if claude_home.parent.name == ".claude-profiles":
        profiles_dir = claude_home.parent
        canonical = claude_home.parent.parent / ".claude"
    else:
        profiles_dir = claude_home.parent / ".claude-profiles"
        canonical = claude_home

    dirs = [canonical]
    if profiles_dir.is_dir():
        for entry in sorted(profiles_dir.iterdir()):
            if not entry.is_dir():
                continue
            if (entry / "settings.json").is_file() or (entry / "projects").exists():
                dirs.append(entry)
    seen: set[Path] = set()
    unique: list[Path] = []
    for d in dirs:
        if d.is_dir() and d not in seen:
            seen.add(d)
            unique.append(d)
    return unique


def inspect(config_dir: Path) -> tuple[list[str], str | None]:
    """Missing required keys, and the configured endpoint if present.

    A settings file that is absent or unparseable reports every key missing:
    both mean the profile exports nothing, and collapsing them keeps the caller
    from having to distinguish two states with one remedy.
    """
    # settings.local.json overrides settings.json, so a lane can be switched
    # off there while settings.json still looks correct. Reading only the
    # latter reports a profile that exports nothing as healthy.
    env: dict = {}
    for name in ("settings.json", "settings.local.json"):
        try:
            loaded = json.loads((config_dir / name).read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            return list(REQUIRED_KEYS), None
        layer = loaded.get("env") if isinstance(loaded, dict) else None
        if isinstance(layer, dict):
            env.update(layer)

    bad: list[str] = []
    for key, allowed in REQUIRED_KEYS.items():
        value = str(env.get(key, "")).strip()
        if not value:
            bad.append(key)
        elif allowed is not None and value.lower() not in allowed:
            bad.append(f"{key}={value}")
    return bad, env.get("OTEL_EXPORTER_OTLP_ENDPOINT")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--claude-home",
        type=Path,
        default=Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))),
    )
    args = parser.parse_args(argv)

    if not args.claude_home.is_dir():
        print(f"claude home does not exist: {args.claude_home}", file=sys.stderr)
        return 2

    config_dirs = discover_config_dirs(args.claude_home)
    gaps: list[str] = []
    endpoints: dict[str, list[str]] = {}

    for config_dir in config_dirs:
        missing, endpoint = inspect(config_dir)
        if missing:
            gaps.append(f"{config_dir}: {', '.join(missing)}")
        else:
            print(f"ok   {config_dir}")
        if endpoint:
            endpoints.setdefault(endpoint, []).append(str(config_dir))

    for gap in gaps:
        print(f"DARK {gap}")

    if len(endpoints) > 1:
        gaps.append("profiles disagree on OTEL_EXPORTER_OTLP_ENDPOINT")
        print("SPLIT profiles export to different endpoints:")
        for endpoint, owners in sorted(endpoints.items()):
            print(f"  {endpoint}")
            for owner in owners:
                print(f"    {owner}")

    print(f"checked {len(config_dirs)} profile(s), {len(gaps)} gap(s)")
    return 1 if gaps else 0


if __name__ == "__main__":
    raise SystemExit(main())
