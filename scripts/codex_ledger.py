"""Shared Codex session-ledger parsing for the exporter and the backfill.

Single home for the ledger schema knowledge so the long-running exporter
(codex_ledger_exporter.py) and the one-shot history backfill
(backfill_codex_ledger_history.py) cannot drift apart: discovery + dedup
rules, the token-field mapping, record iteration, and label escaping.

Replay dedup: a Codex session continued in a new rollout file replays the
earlier records into it, and the replayed copies are NOT byte-identical —
they are re-stamped with the continuation's own time. Measured on disk for
one subagent fork against its parent: every replayed record carried the
parent's usage verbatim, none shared the parent's timestamp. Replay has two
forms, and only one leaves a `forked_from_id`:
  * fork      — `session_meta.payload.forked_from_id` set (subagent spawn),
                replay written before the file's first `turn_context`;
  * resume    — no `forked_from_id`, same `session_meta.payload.session_id`
                as the original; replay position varies — observed
                interleaved after a `turn_context`, and (parent archived)
                ahead of the continuation's first `turn_context`, where the
                copies carry no model.
Timestamp is therefore useless as an identity. `record_key` instead uses the
session lineage id plus `info.total_token_usage`, the session's cumulative
running total, which a replay reproduces exactly. Cumulative alone collides
across diverging branches of one lineage (measured: thousands of collisions
carrying different `last_token_usage`), so the per-call `last_token_usage` is
part of the key too.

This is a heuristic, not a proof of identity: the ledger carries no
per-API-call id. What bounds it is that a billed call always advances
`info.total_token_usage`, so two genuinely distinct calls cannot share a
cumulative — a false merge needs two diverging branches of one lineage to
bill byte-identical usage from the same cumulative point. No such case has
been demonstrated on a measured corpus. Two candidate pairs raised in review
were examined record by record and both proved to be replays: identical
`last_token_usage` AND identical `total_token_usage`, seconds apart, across a
fork boundary.

Callers deduplicate across files with `record_key`. `discover_session_files`
order is deterministic (sorted active paths, then sorted archived) but NOT
chronological — active sorts before archived — so a replay in `sessions/` is
seen before its original in `archived_sessions/`. Every caller must therefore
resolve each key to its EARLIEST copy (`parse_utc` on the record timestamp),
never its first: the replay block precedes the continuation's first
`turn_context`, so keeping it labels real usage model="unknown", and the
label would flip once the continuation file is archived.

Every count in this module's docstrings is a point measurement against a LIVE
corpus under CODEX_HOME, which grows as Codex runs. Ratios and relationships
hold; exact figures drift and will not reproduce. Treat them as evidence of
what was observed at the time, not as invariants.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple, TextIO

try:
    # Stdlib from Python 3.14. Without it a `.jsonl.zst` ledger file is a read
    # error (see `_open_ledger`), so the scan fails closed instead of dropping it.
    from compression import zstd as _zstd
except ImportError:
    _zstd = None

# Codex 0.167 compresses rollouts older than seven days in place:
# `<name>.jsonl` becomes `<name>.jsonl.zst` (codex-rs/rollout/src/compression.rs,
# MIN_ROLLOUT_AGE). Both forms are the same ledger file.
COMPRESSED_SUFFIX = ".zst"

# last_token_usage field -> token_type label value. Mirrors the codex OTLP
# histogram's token_type set so recording rules and the OTLP-vs-ledger
# capture-ratio cross-check compare like with like. `input` includes cached;
# `output` includes reasoning. `total` is Codex's own figure, not a derived
# sum — it does not equal input + output (measured over the deduped corpus:
# `total` runs a small fraction of a percent above input + output), so never
# reconstruct one from the others.
TOKEN_FIELDS = {
    "input_tokens": "input",
    "cached_input_tokens": "cached_input",
    "cache_write_input_tokens": "cache_write_input",
    "output_tokens": "output",
    "reasoning_output_tokens": "reasoning_output",
    "total_tokens": "total",
}

# A line without any of these markers cannot contribute a usage record or the
# lineage/context state one is labelled with. `session_meta` carries the
# lineage id that `record_key` needs, so it must stay in this fast path.
LINE_MARKERS = ('"token_count"', '"turn_context"', '"session_meta"')

USAGE_FIELDS = tuple(TOKEN_FIELDS)


class UsageRecord(NamedTuple):
    timestamp: str  # raw ledger timestamp string (UTC ISO)
    model: str
    effort: str  # reasoning effort for the turn; "" when the turn omits it
    usage: dict  # last_token_usage payload
    key: tuple  # cross-file dedup identity


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


def _usage_tuple(usage: dict) -> tuple:
    return tuple(usage.get(field) for field in USAGE_FIELDS)


def record_key(session_id: str, cumulative: dict, usage: dict) -> tuple:
    """Identity of one token_count record for replay dedup.

    Session lineage id + the cumulative running total at that point + the
    per-call usage. A replay reproduces all three; the timestamp it does not
    (see module docstring).
    """
    return (session_id,) + _usage_tuple(cumulative) + _usage_tuple(usage)


def discover_session_files(codex_home: Path) -> list[Path]:
    """All session ledger files of `codex_home` and of every profile home under
    a sibling `.codex-profiles/`. Deterministic order, main home first.

    Each extra Codex account runs from its own CODEX_HOME there. A direct child
    is a profile only when it carries `config.toml`; this keeps backup trees
    from becoming token sources merely because they contain `sessions/`.
    Profiles resolve as a sibling of `codex_home`, not from `Path.home()`, as
    in `claude_transcript_exporter.discover_project_roots`. Files are deduped
    by real path, so a profile symlinking `sessions/` back is listed once.
    """
    homes = [codex_home]
    profiles_dir = codex_home.parent / ".codex-profiles"
    if profiles_dir.is_dir():
        homes.extend(
            sorted(
                p
                for p in profiles_dir.iterdir()
                if p.is_dir() and (p / "config.toml").is_file()
            )
        )
    seen: set[Path] = set()
    files: list[Path] = []
    for home in homes:
        for path in _home_session_files(home):
            real = path.resolve()
            if real not in seen:
                seen.add(real)
                files.append(path)
    return files


def _home_session_files(codex_home: Path) -> list[Path]:
    """One home's ledger files, active `sessions/` copy winning over an
    `archived_sessions/` copy with the same plain basename (archives are
    copies — ccusage's published dedup rule). Deterministic order."""
    active = _ledger_files(codex_home / "sessions")
    seen = {_plain_name(path) for path in active}
    archived = [
        path
        for path in _ledger_files(codex_home / "archived_sessions")
        if _plain_name(path) not in seen
    ]
    return active + archived


def _plain_name(path: Path) -> str:
    """`x.jsonl` for both `x.jsonl` and `x.jsonl.zst`."""
    return path.name.removesuffix(COMPRESSED_SUFFIX)


def _ledger_files(directory: Path) -> list[Path]:
    """Plain and compressed rollouts under `directory`, sorted by plain path so
    compressing a file never reorders discovery. A `.jsonl.zst` is skipped when
    its plain sibling exists, Codex's own precedence
    (compression.rs `should_skip_compressed_sibling`)."""
    if not directory.is_dir():
        return []
    plain = set(directory.rglob("*.jsonl"))
    compressed = [
        path
        for path in directory.rglob("*.jsonl" + COMPRESSED_SUFFIX)
        if path.with_name(_plain_name(path)) not in plain
    ]
    return sorted([*plain, *compressed], key=lambda p: p.with_name(_plain_name(p)))


@contextmanager
def _open_ledger(path: Path) -> Iterator[TextIO]:
    """Text reader for a plain or zstd-compressed ledger file."""
    if not path.name.endswith(COMPRESSED_SUFFIX):
        with open(path, encoding="utf-8", errors="replace") as fh:
            yield fh
        return
    if _zstd is None:
        raise OSError("reading .jsonl.zst needs Python 3.14+ (compression.zstd)")
    try:
        with _zstd.open(path, "rt", encoding="utf-8", errors="replace") as fh:
            yield fh
    except (_zstd.ZstdError, EOFError) as err:
        raise OSError(f"cannot decompress {path}: {err}") from err


def fork_links(path: Path) -> list[tuple[str, str]]:
    """(fork id, parent id) pairs from one file's old-schema session_meta
    records — those without `session_id` that carry `forked_from_id`. Only
    lines containing both markers are parsed. Cheap per file, and cacheable
    by (size, mtime, ctime) exactly like the usage records."""
    links: list[tuple[str, str]] = []
    with _open_ledger(path) as fh:
        for line in fh:
            if '"forked_from_id"' not in line or '"session_meta"' not in line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = record.get("payload") if isinstance(record, dict) else None
            if not isinstance(payload, dict) or payload.get("session_id"):
                continue
            child, fork_parent = payload.get("id"), payload.get("forked_from_id")
            if isinstance(child, str) and isinstance(fork_parent, str):
                links.append((child, fork_parent))
    return links


def resolve_fork_roots(links: list[tuple[str, str]]) -> dict[str, str]:
    """Old-schema fork id -> root lineage id.

    A chain A -> B -> C must key every member on A, or C's replay of A's
    records escapes the dedup (no nested chain was observed among the
    old-schema forks measured; kept correct by construction). First link per
    child wins; an unknown parent is the root. A cycle (malformed ledger) resolves
    every member to the smallest id in it, so the members still share one
    lineage and their replays still dedup.
    """
    parent: dict[str, str] = {}
    for child, fork_parent in links:
        parent.setdefault(child, fork_parent)
    roots: dict[str, str] = {}
    for child in parent:
        node, seen = child, []
        while node in parent and node not in seen:
            seen.append(node)
            node = parent[node]
        if node in seen:  # cycle: node is where the walk re-entered it
            node = min(seen[seen.index(node) :])
        roots[child] = node
    return roots


def fork_roots(paths: list[Path]) -> dict[str, str]:
    """Corpus-wide fork-root map in one pass (backfill and tests; the
    exporter caches `fork_links` per file instead of re-reading)."""
    links: list[tuple[str, str]] = []
    for path in paths:
        links.extend(fork_links(path))
    return resolve_fork_roots(links)


def iter_usage_records(
    path: Path, parse_errors: list[int], roots: dict[str, str] | None = None
) -> Iterator[UsageRecord]:
    """Yield usage records from one session file.

    Malformed lines (e.g. a mid-write truncated tail) increment
    parse_errors[0] and are skipped; the next scan self-heals.

    Three pieces of state are tracked from the records that precede a
    token_count:
      * lineage id, from `session_meta` — `session_id` names the lineage a
        fork or resume continues; some files only carry `id` (measured: many
        session_meta records lack `session_id`, none lack `id`), so for those
        the fork's parent `forked_from_id` is resolved to its root through
        `roots` (see `fork_roots`; pass the corpus map, else only one fork
        level resolves) and `id` is the last fallback. A file may hold more
        than one session_meta (many do); a CHANGE of lineage starts a fresh
        context, while a session_meta repeating the lineage already in scope
        is a re-emission for the same session and leaves model/effort alone.
      * model, from `turn_context.model`, carried forward while unset (a
        turn_context writing an empty model is a degenerate write — a
        handful of records in a single file corpus-wide).
      * effort, from `turn_context.effort`, reset per turn_context: effort
        is a per-turn setting, so carrying a previous turn's value into a
        turn that omits it would mislabel it (some turn_context records omit
        effort).
    Records before the first turn_context have no model or effort; on the
    measured corpus every such record is a fork replay that dedups away
    against the original, leaving no `unknown` series.
    """
    session_id = ""
    model = "unknown"
    effort = ""
    with _open_ledger(path) as fh:
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
            if record.get("type") == "session_meta":
                # Lineage: `session_id` (current schema) — else, for the older
                # schema that lacks it, the fork's parent `forked_from_id`
                # (a small fraction of such files) resolved to the chain's
                # root, else the file's own `id`. Keying an
                # old-schema fork on its own id would count its replayed
                # parent records a second time.
                lineage = payload.get("session_id") or payload.get("forked_from_id")
                lineage = lineage or payload.get("id")
                lineage = lineage if isinstance(lineage, str) else ""
                if roots and not payload.get("session_id"):
                    lineage = roots.get(lineage, lineage)
                # Only a CHANGE of lineage starts a new context. A session_meta
                # repeating the lineage already in scope is a re-emission for
                # the same session (a meaningful share corpus-wide) and the
                # turns around it carry the same model — resetting there would
                # strand
                # the token_count records written before the next turn_context.
                if lineage != session_id:
                    session_id = lineage
                    model = "unknown"
                    effort = ""
                continue
            if record.get("type") == "turn_context":
                turn_model = payload.get("model")
                if isinstance(turn_model, str) and turn_model:
                    model = turn_model
                turn_effort = payload.get("effort")
                effort = turn_effort if isinstance(turn_effort, str) else ""
                continue
            if payload.get("type") != "token_count":
                continue
            info = payload.get("info")
            if not isinstance(info, dict):
                continue
            usage = info.get("last_token_usage")
            if not isinstance(usage, dict):
                continue
            cumulative = info.get("total_token_usage")
            if not isinstance(cumulative, dict):
                # No cumulative means no lineage-stable identity; skip rather
                # than fall back to the timestamp, which replays rewrite.
                parse_errors[0] += 1
                continue
            timestamp = record.get("timestamp")
            if not isinstance(timestamp, str):
                parse_errors[0] += 1
                continue
            if not session_id:
                # No session_meta before this record means no lineage; an
                # empty lineage would dedup against every other lineage-less
                # record corpus-wide. Skip and count it, like a missing
                # cumulative (none observed on the measured corpus).
                parse_errors[0] += 1
                continue
            yield UsageRecord(
                timestamp,
                model,
                effort,
                usage,
                record_key(session_id, cumulative, usage),
            )


def escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
