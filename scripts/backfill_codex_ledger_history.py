#!/usr/bin/env python3
"""Generate OpenMetrics backfill for codex ledger history.

Emits hourly cumulative samples of
`ai_token_usage_tokens_total{source="codex", job="codex-ledger", ...}`
derived from the Codex session ledger, mirroring the recording rules in
prometheus-rules/ai-unified.yml (fresh input = input - caches clamped at 0;
output = output - reasoning clamped at 0). Feed the output to
`promtool tsdb create-blocks-from openmetrics` and copy the blocks into the
Prometheus data dir. Use it at an OTLP->ledger cutover so dashboard history
predating the switch shows true volumes (the native OTLP lane captured 22 %
— see docs/codex-ledger.md); re-run after
an exporter change that alters series identity or totals (docs/codex-ledger.md,
"Effort label and upgrades").

Ledger parsing, discovery, and replay dedup live in the shared
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
    fork_roots,
    iter_usage_records,
    parse_utc,
)

# The five disjoint-derivation inputs; `total_tokens` is emitted only in --raw.
CATEGORY_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)


def collect_hourly(codex_home: Path, end: dt.datetime) -> dict[int, Counter]:
    """hour_epoch -> Counter[(model, effort, raw_field)] of deduped tokens."""
    end_ts = end.timestamp()
    hourly: dict[int, Counter] = defaultdict(Counter)
    parse_errors = [0]

    # Resolve every key to its EARLIEST copy before bucketing anything.
    # Discovery order is active-then-archived, which is not chronological, so
    # the first copy encountered can be a replay carrying the continuation's
    # restamped time. Bucketing that copy would file the tokens in the wrong
    # hour, and — when the replay falls outside the cutoff — drop the original
    # entirely because the key was already consumed.
    canonical: dict[tuple, tuple[float, str, str, tuple]] = {}
    records = 0
    paths = discover_session_files(codex_home)
    roots = fork_roots(paths)
    for path in paths:
        for record in iter_usage_records(path, parse_errors, roots):
            try:
                ts = parse_utc(record.timestamp).timestamp()
            except ValueError:
                parse_errors[0] += 1
                continue
            records += 1
            previous = canonical.get(record.key)
            if previous is not None and previous[0] <= ts:
                continue
            canonical[record.key] = (
                ts,
                record.model,
                record.effort,
                tuple(record.usage.get(fld) for fld in TOKEN_FIELDS),
            )

    for ts, model, effort, values in canonical.values():
        if ts >= end_ts:
            continue
        hour = int(ts // 3600) * 3600
        for fld, value in zip(TOKEN_FIELDS, values):
            if isinstance(value, int) and value > 0:
                hourly[hour][(model, effort, fld)] += value
    print(
        f"dedup: {records - len(canonical)} replay duplicate records excluded, "
        f"{parse_errors[0]} parse errors skipped"
    )
    return hourly


def derive_categories(cum: Counter, model: str, effort: str) -> dict[str, int]:
    """Mirror the ai-unified.yml codex category derivation on cumulatives."""
    inp = cum[(model, effort, "input_tokens")]
    cached = cum[(model, effort, "cached_input_tokens")]
    cache_write = cum[(model, effort, "cache_write_input_tokens")]
    out = cum[(model, effort, "output_tokens")]
    reasoning = cum[(model, effort, "reasoning_output_tokens")]
    return {
        "input": max(0, inp - cached - cache_write),
        "cacheRead": cached,
        "cacheCreation": cache_write,
        "output": max(0, out - reasoning),
        "reasoning_output": reasoning,
    }


def render_samples(
    hourly: dict[int, Counter],
    end: dt.datetime,
    static_labels: str,
    raw: bool,
) -> tuple[list[str], Counter, set[tuple[str, str]]]:
    """The OpenMetrics sample lines for the whole hourly grid.

    Separate from main() so the emitted series can be tested directly —
    a test that re-implements this loop would pass against any change to it.
    """
    cum: Counter = Counter()
    models_seen: set[tuple[str, str]] = set()
    lines: list[str] = []
    # Continuous hourly grid: idle hours carry the cumulative forward so
    # instant queries (5m lookback) and increase() windows never hit gaps.
    end_hour = int(end.timestamp())
    for hour in range(min(hourly), end_hour, 3600):
        if hour in hourly:
            cum.update(hourly[hour])
            models_seen.update((model, effort) for model, effort, _ in hourly[hour])
        for model, effort in sorted(models_seen):
            model_label = escape_label_value(model)
            effort_label = escape_label_value(effort)
            if raw:
                for fld, token_type in TOKEN_FIELDS.items():
                    value = cum[(model, effort, fld)]
                    # The exporter omits a series while its value is 0
                    # (codex_ledger_exporter.py, `if value > 0`); emitting one
                    # here would splice zero-valued series into history that
                    # the live lane never produces.
                    if value <= 0:
                        continue
                    lines.append(
                        "codex_ledger_token_usage_total{"
                        f'{static_labels},effort="{effort_label}",'
                        f'model="{model_label}",'
                        f'token_type="{token_type}"}} {value} {hour}'
                    )
                continue
            for category, value in derive_categories(cum, model, effort).items():
                if value <= 0:
                    continue
                lines.append(
                    "ai_token_usage_tokens_total{"
                    f'{static_labels},effort="{effort_label}",'
                    f'model="{model_label}",source="codex",'
                    f'type="{category}"}} {value} {hour}'
                )
    return lines, cum, models_seen


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
        "YYYY-MM-DDTHH:00:00) — must precede the ledger exporter's first "
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
        help="emit raw codex_ledger_token_usage_total{model,effort,token_type} "
        "series "
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
    lines, cum, models_seen = render_samples(hourly, end, static_labels, args.raw)
    grand = sum(
        cum[(model, effort, fld)]
        for model, effort in models_seen
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
