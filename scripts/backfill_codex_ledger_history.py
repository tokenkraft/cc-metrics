#!/usr/bin/env python3
"""Generate OpenMetrics backfill for codex ledger history.

Emits step-aligned cumulative `ai_token_usage_tokens_total` samples and,
with `--raw` or `--raw-out`, `codex_ledger_token_usage_total` samples. Derived
samples mirror the recording rules in prometheus-rules/ai-unified.yml (fresh
input = input - caches clamped at 0; output = output - reasoning clamped at 0). Feed the output to
`promtool tsdb create-blocks-from openmetrics` and copy the blocks into the
Prometheus data dir. Use it at an OTLP->ledger cutover so dashboard history
predating the switch shows true volumes (the native OTLP lane captured 22 %
— see docs/codex-ledger.md); re-run after
an exporter change that alters series identity or totals (docs/codex-ledger.md,
"Effort label and upgrades").

Ledger parsing, discovery, and replay dedup live in the shared
codex_ledger module so this script and the live exporter cannot drift apart.

The generated series must splice under the live counter: pass --end before
the ledger exporter's first Prometheus scrape, with labels exactly matching
the live rule output (env, instance, model, effort and token category). The
input deliberately omits `job`; attach it with promtool's
`--label=job=codex-ledger-guarded`, making that flag the single authority for
the imported series identity.
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


def collect_hourly(
    codex_home: Path, end: dt.datetime, step_seconds: int = 3600
) -> dict[int, Counter]:
    """Bucket start -> deduped tokens; hourly unless a finer step is requested."""
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
        hour = int(ts // step_seconds) * step_seconds
        for fld, value in zip(TOKEN_FIELDS, values):
            if isinstance(value, int) and value > 0:
                hourly[hour][(model, effort, fld)] += value
    if parse_errors[0]:
        raise ValueError(
            f"ledger contains {parse_errors[0]} malformed records; "
            "refusing incomplete backfill"
        )
    print(f"dedup: {records - len(canonical)} replay duplicate records excluded")
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


def backfill_labels(env_label: str, instance: str) -> str:
    """Labels emitted in the OpenMetrics input before promtool adds `job`."""
    return (
        f'env="{escape_label_value(env_label)}",'
        f'instance="{escape_label_value(instance)}"'
    )


def render_samples(
    hourly: dict[int, Counter],
    end: dt.datetime,
    static_labels: str,
    raw: bool,
    step_seconds: int = 3600,
    start: dt.datetime | None = None,
) -> tuple[list[str], Counter, set[tuple[str, str]]]:
    """The OpenMetrics sample lines for the whole hourly grid.

    Separate from main() so the emitted series can be tested directly —
    a test that re-implements this loop would pass against any change to it.
    """
    cum: Counter = Counter()
    models_seen: set[tuple[str, str]] = set()
    series_seen: set[tuple[str, str, str]] = set()
    lines: list[str] = []
    # Continuous grid: idle buckets carry the cumulative forward. Hourly
    # samples support long range functions; use minute samples for short
    # windows and instant lookups with Prometheus's default 5m lookback.
    end_hour = int(end.timestamp())
    for hour in range(min(hourly), end_hour, step_seconds):
        if hour in hourly:
            cum.update(hourly[hour])
            models_seen.update((model, effort) for model, effort, _ in hourly[hour])
        # Values describe events strictly BEFORE the sample timestamp, never
        # events from the following bucket. Keep pre-start events in the
        # cumulative so a limited repair still splices into the live total.
        sample_time = hour + step_seconds
        output_sample = start is None or sample_time >= start.timestamp()
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
                    series = (model, effort, token_type)
                    if series not in series_seen:
                        if start is None or hour >= start.timestamp():
                            lines.append(
                                "codex_ledger_token_usage_total{"
                                f'{static_labels},effort="{effort_label}",'
                                f'model="{model_label}",'
                                f'token_type="{token_type}"}} 0 {hour}'
                            )
                        series_seen.add(series)
                    if not output_sample:
                        continue
                    lines.append(
                        "codex_ledger_token_usage_total{"
                        f'{static_labels},effort="{effort_label}",'
                        f'model="{model_label}",'
                        f'token_type="{token_type}"}} {value} {sample_time}'
                    )
                continue
            for category, value in derive_categories(cum, model, effort).items():
                if value <= 0:
                    continue
                series = (model, effort, category)
                if series not in series_seen:
                    if start is None or hour >= start.timestamp():
                        lines.append(
                            "ai_token_usage_tokens_total{"
                            f'{static_labels},effort="{effort_label}",'
                            f'model="{model_label}",source="codex",'
                            f'type="{category}"}} 0 {hour}'
                        )
                    series_seen.add(series)
                if not output_sample:
                    continue
                lines.append(
                    "ai_token_usage_tokens_total{"
                    f'{static_labels},effort="{effort_label}",'
                    f'model="{model_label}",source="codex",'
                    f'type="{category}"}} {value} {sample_time}'
                )
    return lines, cum, models_seen


def write_openmetrics(path: Path, lines: list[str]) -> None:
    """Write one complete promtool input after collection and rendering pass."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
        fh.write("\n# EOF\n")


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
        help="exclusive UTC event cutoff, step-aligned ISO format (e.g. "
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
    parser.add_argument("--instance", default="host.docker.internal:9314")
    parser.add_argument(
        "--start", help="first sample timestamp; earlier usage stays in totals"
    )
    parser.add_argument("--step-seconds", type=int, choices=(60, 3600), default=3600)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--raw-out",
        type=Path,
        help="write a raw companion file from the same parsed ledger snapshot",
    )
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
    if args.raw and args.raw_out is not None:
        raise SystemExit("--raw and --raw-out are mutually exclusive")
    if args.raw_out is not None and args.raw_out.resolve() == args.out.resolve():
        raise SystemExit("--raw-out must differ from --out")
    end = parse_utc(args.end)
    if end.timestamp() % args.step_seconds != 0:
        raise SystemExit(
            f"--end must be step-aligned (got {args.end}); an unaligned cutoff "
            "would silently drop the final partial bucket"
        )
    start = parse_utc(args.start) if args.start else None
    if start is not None and (start >= end or start.timestamp() % args.step_seconds):
        raise SystemExit("--start must be step-aligned and precede --end")
    try:
        hourly = collect_hourly(args.codex_home, end, args.step_seconds)
    except (OSError, ValueError) as err:
        raise SystemExit(str(err)) from None
    if not hourly:
        raise SystemExit("no ledger events before the cutoff; nothing to backfill")

    static_labels = backfill_labels(args.env_label, args.instance)
    lines, cum, models_seen = render_samples(
        hourly, end, static_labels, args.raw, args.step_seconds, start
    )
    raw_lines: list[str] | None = None
    if args.raw_out is not None:
        raw_lines, _, _ = render_samples(
            hourly, end, static_labels, True, args.step_seconds, start
        )
    grand = sum(
        cum[(model, effort, fld)]
        for model, effort in models_seen
        for fld in ("input_tokens", "output_tokens")
    )
    write_openmetrics(args.out, lines)
    if args.raw_out is not None and raw_lines is not None:
        write_openmetrics(args.raw_out, raw_lines)
    first = dt.datetime.fromtimestamp(min(hourly), dt.timezone.utc)
    last = dt.datetime.fromtimestamp(max(hourly), dt.timezone.utc)
    print(
        f"samples={len(lines)} raw_samples={len(raw_lines or [])} "
        f"buckets={len(hourly)} models={len(models_seen)} "
        f"range={first:%Y-%m-%dT%H:%M}Z..{last:%Y-%m-%dT%H:%M}Z "
        f"final cumulative input+output={grand}"
    )


if __name__ == "__main__":
    main()
