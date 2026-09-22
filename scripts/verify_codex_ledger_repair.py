#!/usr/bin/env python3
"""Fail-closed checks for a Codex ledger history repair.

The pre-upgrade persisted high-water state is the independent baseline. The
new exporter's attempted-scan gauges must preserve every model/effort/token
series, except operator-declared identity remaps, before state can be rebased.
After Prometheus starts, raw post-cutoff deltas must exactly equal attempted
totals minus imported cutoff totals while Codex ledger writers remain paused.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

TOKEN_TYPES = {
    "input",
    "cached_input",
    "cache_write_input",
    "output",
    "reasoning_output",
    "total",
}
LABEL_RE = re.compile(
    r'(?:^|,)(?P<name>[a-zA-Z_][a-zA-Z0-9_]*)="(?P<value>(?:\\.|[^"])*)"'
)
CANDIDATE_RE = re.compile(
    r"^codex_ledger_candidate_token_usage\{(?P<labels>.*)\} (?P<value>\S+)$"
)
RAW_RE = re.compile(
    r"^codex_ledger_token_usage_total\{(?P<labels>.*)\} "
    r"(?P<value>\S+) (?P<timestamp>\d+)$"
)


class VerificationError(ValueError):
    """Repair evidence is malformed or violates a safety invariant."""


def _integer(value: object, context: str) -> int:
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as err:
        raise VerificationError(f"{context}: invalid numeric value {value!r}") from err
    if not parsed.is_finite() or parsed != parsed.to_integral_value() or parsed < 0:
        raise VerificationError(
            f"{context}: expected nonnegative integer, got {value!r}"
        )
    return int(parsed)


def _series_id(model: str, effort: str, token_type: str) -> str:
    if "\t" in model or "\t" in effort:
        raise VerificationError("model and effort cannot contain tabs")
    if token_type not in TOKEN_TYPES:
        raise VerificationError(f"unknown token type {token_type!r}")
    return "\t".join((model, effort, token_type))


def _parse_series_id(series: object, context: str) -> tuple[str, str, str]:
    parts = series.split("\t") if isinstance(series, str) else []
    if len(parts) != 3 or parts[2] not in TOKEN_TYPES:
        raise VerificationError(f"{context}: invalid series key {series!r}")
    return parts[0], parts[1], parts[2]


def _labels(value: str, context: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    for match in LABEL_RE.finditer(value):
        try:
            label_value = json.loads(f'"{match.group("value")}"')
        except json.JSONDecodeError as err:
            raise VerificationError(f"{context}: invalid label escaping") from err
        labels[match.group("name")] = label_value
    return labels


def _series_from_labels(labels: dict[str, Any], context: str) -> str:
    required = ("model", "effort", "token_type")
    missing = set(required) - labels.keys()
    if missing:
        raise VerificationError(f"{context}: missing labels {sorted(missing)}")
    if any(not isinstance(labels[key], str) for key in required):
        raise VerificationError(f"{context}: series labels must be strings")
    return _series_id(labels["model"], labels["effort"], labels["token_type"])


def load_prior_series(path: Path) -> dict[str, int]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    high_water = payload.get("high_water") if isinstance(payload, dict) else None
    if not isinstance(high_water, dict):
        raise VerificationError("prior state has no high_water object")
    series_totals: dict[str, int] = {}
    for series, value in high_water.items():
        model, effort, token_type = _parse_series_id(series, "prior state")
        key = _series_id(model, effort, token_type)
        series_totals[key] = _integer(value, f"prior state {series!r}")
    if not series_totals:
        raise VerificationError("prior state high_water is empty")
    return series_totals


def load_candidate_series(path: Path) -> dict[str, int]:
    series_totals: defaultdict[str, int] = defaultdict(int)
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        match = CANDIDATE_RE.match(line)
        if match is None:
            continue
        labels = _labels(match.group("labels"), f"candidate line {line_number}")
        key = _series_from_labels(labels, f"candidate line {line_number}")
        series_totals[key] += _integer(
            match.group("value"), f"candidate line {line_number}"
        )
    if not series_totals:
        raise VerificationError("candidate metrics contain no attempted token totals")
    return dict(series_totals)


def load_remap(path: Path | None, prior: dict[str, int]) -> dict[str, str]:
    if path is None:
        return {}
    payload: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise VerificationError("series remap must be a JSON object")
    remap: dict[str, str] = {}
    for source, target in payload.items():
        source_parts = _parse_series_id(source, "series remap source")
        target_parts = _parse_series_id(target, "series remap target")
        if source not in prior:
            raise VerificationError(
                f"series remap source is absent from prior state: {source!r}"
            )
        if source_parts[2] != target_parts[2]:
            raise VerificationError(
                f"series remap changes token type: {source!r} -> {target!r}"
            )
        remap[source] = target
    return remap


def load_cutoff_series(path: Path) -> dict[str, int]:
    last_by_series: dict[str, tuple[int, int, str]] = {}
    saw_eof = False
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1
    ):
        if line == "# EOF":
            saw_eof = True
            continue
        if not line or line.startswith("#"):
            continue
        match = RAW_RE.match(line)
        if match is None:
            raise VerificationError(f"raw OpenMetrics line {line_number} is invalid")
        labels = match.group("labels")
        series = _series_from_labels(
            _labels(labels, f"raw OpenMetrics line {line_number}"),
            f"raw OpenMetrics line {line_number}",
        )
        value = _integer(match.group("value"), f"raw OpenMetrics line {line_number}")
        timestamp = int(match.group("timestamp"))
        previous = last_by_series.get(labels)
        if previous is not None:
            if timestamp <= previous[0] or value < previous[1]:
                raise VerificationError(
                    f"raw series is non-monotone at line {line_number}"
                )
        last_by_series[labels] = (timestamp, value, series)
    if not saw_eof or not last_by_series:
        raise VerificationError("raw OpenMetrics input is empty or lacks # EOF")
    totals: defaultdict[str, int] = defaultdict(int)
    for _, value, series in last_by_series.values():
        totals[series] += value
    return dict(totals)


def load_prom_delta_series(path: Path) -> dict[str, int]:
    payload: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise VerificationError("Prometheus delta report must be an array")
    totals: defaultdict[str, int] = defaultdict(int)
    for index, item in enumerate(payload):
        labels = item.get("labels") if isinstance(item, dict) else None
        if not isinstance(labels, dict):
            raise VerificationError(f"Prometheus delta item {index} lacks labels")
        labels = dict(labels)
        # Prometheus omits empty-valued labels from query results. The ledger
        # parser uses an empty effort when a turn_context has none, so restore
        # that stable identity before comparing it with exporter/backfill data.
        labels.setdefault("effort", "")
        series = _series_from_labels(labels, f"Prometheus delta item {index}")
        totals[series] += _integer(item.get("delta"), f"Prometheus delta item {index}")
    if not totals:
        raise VerificationError("Prometheus delta report is empty")
    return dict(totals)


def verify(
    prior_state: Path,
    candidate_metrics: Path,
    raw_openmetrics: Path | None = None,
    prom_deltas: Path | None = None,
    series_remap: Path | None = None,
) -> dict[str, Any]:
    if (raw_openmetrics is None) != (prom_deltas is None):
        raise VerificationError(
            "raw OpenMetrics and Prometheus deltas must be supplied together"
        )
    prior = load_prior_series(prior_state)
    candidate = load_candidate_series(candidate_metrics)
    remap = load_remap(series_remap, prior)
    required: defaultdict[str, int] = defaultdict(int)
    for series, value in prior.items():
        required[remap.get(series, series)] += value
    missing = {
        series: {"required": value, "candidate": candidate.get(series, 0)}
        for series, value in required.items()
        if candidate.get(series, 0) < value
    }
    if missing:
        raise VerificationError(
            "candidate series fell below prior high-water requirements: "
            + json.dumps(missing, sort_keys=True)
        )
    report: dict[str, Any] = {
        "label_mapping": "state key fields model/effort/token_type -> matching candidate labels",
        "series_remap": remap,
        "tolerance_tokens": 0,
        "prior": prior,
        "candidate": candidate,
        "baseline_check": "pass",
    }
    if raw_openmetrics is not None and prom_deltas is not None:
        cutoff = load_cutoff_series(raw_openmetrics)
        observed = load_prom_delta_series(prom_deltas)
        series_keys = sorted(set(candidate) | set(cutoff) | set(observed))
        expected = {
            series: candidate.get(series, 0) - cutoff.get(series, 0)
            for series in series_keys
        }
        negative = {key: value for key, value in expected.items() if value < 0}
        if negative:
            raise VerificationError(
                "cutoff totals exceed candidate totals: "
                + json.dumps(negative, sort_keys=True)
            )
        mismatch = {
            series: {
                "expected": expected[series],
                "observed": observed.get(series, 0),
            }
            for series in series_keys
            if observed.get(series, 0) != expected[series]
        }
        if mismatch:
            raise VerificationError(
                "Prometheus post-cutoff deltas mismatch candidate totals: "
                + json.dumps(mismatch, sort_keys=True)
            )
        report.update(
            {
                "cutoff": cutoff,
                "expected_post_cutoff": expected,
                "observed_post_cutoff": observed,
                "delta_check": "pass",
            }
        )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prior-state", type=Path, required=True)
    parser.add_argument("--candidate-metrics", type=Path, required=True)
    parser.add_argument("--raw-openmetrics", type=Path)
    parser.add_argument("--prom-deltas", type=Path)
    parser.add_argument("--series-remap", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = verify(
            args.prior_state,
            args.candidate_metrics,
            args.raw_openmetrics,
            args.prom_deltas,
            args.series_remap,
        )
    except (OSError, ValueError, json.JSONDecodeError) as err:
        raise SystemExit(str(err)) from None
    args.report.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"repair verification PASS: {args.report}")


if __name__ == "__main__":
    main()
