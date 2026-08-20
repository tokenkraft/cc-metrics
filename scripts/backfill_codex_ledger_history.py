#!/usr/bin/env python3
"""Generate OpenMetrics backfill for codex ledger history.

Emits hourly cumulative samples of
`ai_token_usage_tokens_total{source="codex", job="codex-ledger", ...}`
derived from the Codex session ledger, mirroring the recording rules in
prometheus-rules/ai-unified.yml (fresh input = input - caches clamped at 0;
output = output - reasoning clamped at 0). Feed the output to
`promtool tsdb create-blocks-from openmetrics` and copy the blocks into the
Prometheus data dir. Used once at the 2026-08-20 OTLP->ledger cutover so
dashboard history predating the switch shows true volumes (the native OTLP
lane captured ~31 % — see README "Codex ledger token source").

Ledger parsing, discovery, and fork/replay dedup live in the shared
codex_ledger module so this script and the live exporter cannot drift apart.

The generated series must splice under the live counter: pass --end as the
hour before the ledger exporter's first Prometheus scrape, and labels matching
the live rule output (env, job, instance).
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
from collections import Counter, defaultdict
from pathlib import Path

from codex_ledger import (
    TOKEN_FIELDS,
    discover_session_files,
    escape_label_value,
    iter_usage_records,
)

# The five disjoint-derivation inputs; `total_tokens` is emitted only in --raw.
CATEGORY_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)


def parse_utc(value: str) -> dt.datetime:
    """ISO timestamp -> aware UTC datetime (naive input is taken as UTC).

    Ledger timestamps carry a Z suffix, which fromisoformat only accepts
    from Python 3.11 — rewrite it so the documented 3.10 floor holds.
    """
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def collect_hourly(codex_home: Path, end: dt.datetime) -> dict[int, Counter]:
    """hour_epoch -> Counter[(model, raw_field)] of deduped tokens."""
    end_ts = end.timestamp()
    hourly: dict[int, Counter] = defaultdict(Counter)
    seen: set = set()
    duplicates = 0
    parse_errors = [0]
    for path in discover_session_files(codex_home):
        for record in iter_usage_records(path, parse_errors):
            if record.key in seen:
                duplicates += 1
                continue
            seen.add(record.key)
            try:
                ts = parse_utc(record.timestamp).timestamp()
            except ValueError:
                parse_errors[0] += 1
                continue
            if ts >= end_ts:
                continue
            hour = int(ts // 3600) * 3600
            for fld in TOKEN_FIELDS:
                value = record.usage.get(fld)
                if isinstance(value, int) and value > 0:
                    hourly[hour][(record.model, fld)] += value
    print(
        f"dedup: {duplicates} fork/replay duplicate records excluded, "
        f"{parse_errors[0]} parse errors skipped"
    )
    return hourly


def derive_categories(cum: Counter, model: str) -> dict[str, int]:
    """Mirror the ai-unified.yml codex category derivation on cumulatives."""
    inp = cum[(model, "input_tokens")]
    cached = cum[(model, "cached_input_tokens")]
    cache_write = cum[(model, "cache_write_input_tokens")]
    out = cum[(model, "output_tokens")]
    reasoning = cum[(model, "reasoning_output_tokens")]
    return {
        "input": max(0, inp - cached - cache_write),
        "cacheRead": cached,
        "cacheCreation": cache_write,
        "output": max(0, out - reasoning),
        "reasoning_output": reasoning,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate OpenMetrics backfill for codex ledger history"
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))),
    )
    parser.add_argument(
        "--end",
        required=True,
        help="exclusive UTC cutoff, hour-aligned ISO format (e.g. "
        "2026-08-20T01:00:00) — must precede the ledger exporter's first "
        "live scrape",
    )
    parser.add_argument(
        "--env-label",
        default=os.environ.get("HOST_ENV"),
        help="value for the env label; must match the live series' env label "
        "(required via flag or HOST_ENV — a mismatch splits the backfill "
        "from the live counter)",
    )
    parser.add_argument("--job", default="codex-ledger")
    parser.add_argument("--instance", default="host.docker.internal:9314")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--raw",
        action="store_true",
        help="emit raw codex_ledger_token_usage_total{model,token_type} series "
        "(the exporter's own metric, used directly by dashboard cost panels) "
        "instead of the derived ai_token_usage_tokens_total categories",
    )
    args = parser.parse_args()

    if not args.env_label:
        raise SystemExit("set --env-label or HOST_ENV (must match .env HOST_ENV)")
    end = parse_utc(args.end)
    if end.timestamp() % 3600 != 0:
        raise SystemExit(
            f"--end must be hour-aligned (got {args.end}); an unaligned cutoff "
            "would silently drop the final partial hour"
        )
    hourly = collect_hourly(args.codex_home, end)
    if not hourly:
        raise SystemExit("no ledger events before the cutoff; nothing to backfill")

    static_labels = (
        f'env="{escape_label_value(args.env_label)}",'
        f'instance="{escape_label_value(args.instance)}",'
        f'job="{escape_label_value(args.job)}"'
    )
    cum: Counter = Counter()
    models_seen: set[str] = set()
    lines: list[str] = []
    # Continuous hourly grid: idle hours carry the cumulative forward so
    # instant queries (5m lookback) and increase() windows never hit gaps.
    end_hour = int(end.timestamp())
    for hour in range(min(hourly), end_hour, 3600):
        if hour in hourly:
            cum.update(hourly[hour])
            models_seen.update(model for model, _ in hourly[hour])
        for model in sorted(models_seen):
            model_label = escape_label_value(model)
            if args.raw:
                for fld, token_type in TOKEN_FIELDS.items():
                    lines.append(
                        "codex_ledger_token_usage_total{"
                        f'{static_labels},model="{model_label}",'
                        f'token_type="{token_type}"}} {cum[(model, fld)]} {hour}'
                    )
                continue
            for category, value in derive_categories(cum, model).items():
                lines.append(
                    "ai_token_usage_tokens_total{"
                    f'{static_labels},model="{model_label}",source="codex",'
                    f'type="{category}"}} {value} {hour}'
                )
    grand = sum(
        cum[(model, fld)]
        for model in models_seen
        for fld in ("input_tokens", "output_tokens")
    )
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
        fh.write("\n# EOF\n")
    first = dt.datetime.fromtimestamp(min(hourly), dt.timezone.utc)
    last = dt.datetime.fromtimestamp(max(hourly), dt.timezone.utc)
    print(
        f"samples={len(lines)} hours={len(hourly)} models={len(models_seen)} "
        f"range={first:%Y-%m-%dT%H:%M}Z..{last:%Y-%m-%dT%H:%M}Z "
        f"final cumulative input+output={grand}"
    )


if __name__ == "__main__":
    main()
