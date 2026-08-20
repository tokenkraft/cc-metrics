"""Shared Codex session-ledger parsing for the exporter and the backfill.

Single home for the ledger schema knowledge so the long-running exporter
(codex_ledger_exporter.py) and the one-shot history backfill
(backfill_codex_ledger_history.py) cannot drift apart: discovery + dedup
rules, the token-field mapping, record iteration, and label escaping.

Fork/replay dedup: forked or resumed Codex sessions replay the parent
session's token_count records verbatim into new rollout files (verified on
disk 2026-08-20: one record identical across 10+ files carrying
forked_from_id; 3.9 % of all ledger tokens were such duplicates). Callers
must therefore deduplicate across files with `record_key`, keeping the first
occurrence in `discover_session_files` order (deterministic: sorted active
paths, then sorted archived).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

# last_token_usage field -> token_type label value. Mirrors the codex OTLP
# histogram's token_type set so recording rules and the OTLP-vs-ledger
# capture-ratio cross-check compare like with like. `input` includes cached;
# `output` includes reasoning; `total` = input + output.
TOKEN_FIELDS = {
    "input_tokens": "input",
    "cached_input_tokens": "cached_input",
    "cache_write_input_tokens": "cache_write_input",
    "output_tokens": "output",
    "reasoning_output_tokens": "reasoning_output",
    "total_tokens": "total",
}

# A line without any of these markers cannot contribute a usage record.
LINE_MARKERS = ('"token_count"', '"turn_context"')


class UsageRecord(NamedTuple):
    timestamp: str  # raw ledger timestamp string (UTC ISO)
    model: str
    usage: dict  # last_token_usage payload
    key: tuple  # cross-file dedup identity


def record_key(timestamp: str, usage: dict) -> tuple:
    """Identity of one token_count record for fork/replay dedup.

    Timestamp plus the full usage tuple: replayed records are byte-identical,
    while two genuinely distinct API calls in the same second with identical
    six-way token counts are vanishingly unlikely (and cost one small record
    if ever hit).
    """
    return (
        timestamp,
        usage.get("input_tokens"),
        usage.get("cached_input_tokens"),
        usage.get("cache_write_input_tokens"),
        usage.get("output_tokens"),
        usage.get("reasoning_output_tokens"),
        usage.get("total_tokens"),
    )


def discover_session_files(codex_home: Path) -> list[Path]:
    """All session ledger files, active `sessions/` copy winning over an
    `archived_sessions/` copy with the same basename (archives are copies —
    ccusage's published dedup rule). Deterministic order."""
    active_dir = codex_home / "sessions"
    archived_dir = codex_home / "archived_sessions"
    active = sorted(active_dir.rglob("*.jsonl")) if active_dir.is_dir() else []
    seen = {path.name for path in active}
    archived = []
    if archived_dir.is_dir():
        archived = [
            path
            for path in sorted(archived_dir.rglob("*.jsonl"))
            if path.name not in seen
        ]
    return active + archived


def iter_usage_records(path: Path, parse_errors: list[int]) -> Iterator[UsageRecord]:
    """Yield usage records from one session file.

    Malformed lines (e.g. a mid-write truncated tail) increment
    parse_errors[0] and are skipped; the next scan self-heals. The current
    model is tracked from preceding turn_context records ("unknown" before
    the first one — old ledger files predate turn_context.model).
    """
    model = "unknown"
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not any(marker in line for marker in LINE_MARKERS):
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                parse_errors[0] += 1
                continue
            if not isinstance(record, dict):
                parse_errors[0] += 1
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            if record.get("type") == "turn_context":
                turn_model = payload.get("model")
                if isinstance(turn_model, str) and turn_model:
                    model = turn_model
                continue
            if payload.get("type") != "token_count":
                continue
            info = payload.get("info")
            if not isinstance(info, dict):
                continue
            usage = info.get("last_token_usage")
            if not isinstance(usage, dict):
                continue
            timestamp = record.get("timestamp")
            if not isinstance(timestamp, str):
                parse_errors[0] += 1
                continue
            yield UsageRecord(timestamp, model, usage, record_key(timestamp, usage))


def escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
