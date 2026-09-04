#!/usr/bin/env python3
"""Prometheus exporter for Claude Code token usage read from local transcripts.

The Claude OTLP lane can go silently dark, and observed losses have been
PARTIAL: capture collapses but never reaches zero. Every `claude_code_*` metric
rides the same OTLP pipe, so a ratio between two of them measures which
emitters survived, not whether the pipe works. This exporter is an INDEPENDENT
witness: Claude Code writes a JSONL transcript to disk under
`~/.claude/projects/**` for every turn regardless of OTEL state, so its token
total can be compared against the OTLP counter to detect capture loss instead
of merely inferring it.

This daemon scans transcripts, sums usage from `message.usage` by
(model, effort, type) — the `type` label values (input/output/cacheRead/
cacheCreation) match `claude_code_token_usage_tokens_total` exactly so a
capture-ratio query needs no label translation — and serves Prometheus text
exposition on 127.0.0.1:9315. Counters are recomputed deterministically from
the corpus each scan; per-file parses are cached by (size, mtime_ns) so a
scrape never re-reads an unchanged file (see TranscriptScanner).

The corpus is READ-ONLY and retention-sensitive: Claude Code prunes old
session files on its own schedule, so — unlike an append-only ledger — the
scanned total can legitimately shrink over time as old sessions age out. This
exporter never writes, moves, or deletes anything under `~/.claude/`, and it
exposes `claude_transcript_corpus_shrunk` so a shrink (retention OR data loss)
is visible rather than silently read by Prometheus as a counter reset.

Stdlib only. Intended to run the same way as scripts/codex_ledger_exporter.py
(the model this file mirrors), e.g. under launchd.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
import tempfile
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LOG = logging.getLogger("claude_transcript_exporter")

STATE_VERSION = 1

# transcript usage field -> `type` label value, matching the values observed
# on claude_code_token_usage_tokens_total{type=...} (input, output, cacheRead,
# cacheCreation) so a capture-ratio query compares like with like without
# relabelling either side.
TOKEN_FIELDS = {
    "input_tokens": "input",
    "output_tokens": "output",
    "cache_read_input_tokens": "cacheRead",
    "cache_creation_input_tokens": "cacheCreation",
}
FIELD_ORDER = tuple(TOKEN_FIELDS)
OUTPUT_INDEX = FIELD_ORDER.index("output_tokens")

# All-zero placeholder Claude Code writes for a failed/errored turn (e.g. a
# server-side 529). Real usage is never labelled with this model string.
SYNTHETIC_MODEL = "<synthetic>"

# Cheap pre-filter so most lines (user turns, tool results, attachments) are
# skipped without a json.loads call. Matches the exact minified-JSON byte
# sequence Claude Code writes (verified against the live corpus: no spaces
# around `:`).
ASSISTANT_MARKER = '"type":"assistant"'


def default_runtime_dir() -> Path:
    """User-owned state directory, matching scripts/codex_ledger_exporter.py."""
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/cc-metrics/runtime"
    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    base = (
        Path(xdg_state_home).expanduser()
        if xdg_state_home
        else Path.home() / ".local/state"
    )
    return base / "cc-metrics/runtime"


def default_state_path() -> Path:
    configured = os.environ.get("CC_METRICS_RUNTIME_DIR")
    runtime = Path(configured).expanduser() if configured else default_runtime_dir()
    return runtime / "claude-transcript-state.json"


def _series_id(model: str, effort: str, token_type: str) -> str:
    # \t cannot occur in a label value the transcript schema produces, so it
    # is a safe separator for the flat JSON keys.
    return "\t".join((model, effort, token_type))


def load_state(path: Path) -> tuple[Counter, int, int]:
    """(high-water token totals, high-water session count, corpus_shrunk)
    from disk; empty/zero when unusable.

    A missing or corrupt state file must not stop the exporter — it only
    resets the guard's memory, which is the pre-existing behaviour
    (codex_ledger_exporter.load_state).
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Counter(), 0, 0
    except (OSError, ValueError) as err:
        LOG.warning(
            "state file %s unusable (%s) — shrink guard restarts empty", path, err
        )
        return Counter(), 0, 0
    if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
        LOG.warning(
            "state file %s has version %r, want %s — shrink guard restarts empty",
            path,
            raw.get("version") if isinstance(raw, dict) else None,
            STATE_VERSION,
        )
        return Counter(), 0, 0
    totals = raw.get("high_water")
    counter: Counter = Counter()
    if isinstance(totals, dict):
        for key, value in totals.items():
            parts = key.split("\t")
            if len(parts) == 3 and isinstance(value, int):
                counter[tuple(parts)] = value
    sessions = raw.get("high_water_sessions")
    high_water_sessions = sessions if isinstance(sessions, int) else 0
    return counter, high_water_sessions, 1 if raw.get("corpus_shrunk") else 0


def save_state(
    path: Path, high_water: Counter, high_water_sessions: int, corpus_shrunk: int
) -> None:
    """Atomically persist the guard's memory. Best-effort by design: failing
    to write must not take the exporter down."""
    payload = {
        "version": STATE_VERSION,
        "corpus_shrunk": int(corpus_shrunk),
        "high_water_sessions": int(high_water_sessions),
        "high_water": {_series_id(*k): v for k, v in high_water.items()},
    }
    try:
        # Same mode as codex_ledger_exporter.save_state: the shared runtime
        # dir stays traversable but not listable.
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o711)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}."
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
    except OSError as err:
        LOG.warning("could not persist transcript state to %s: %s", path, err)


def parse_utc(value: str) -> dt.datetime:
    """ISO timestamp -> aware UTC datetime (naive input is taken as UTC).

    Transcript timestamps carry a Z suffix, which fromisoformat only accepts
    from Python 3.11 — rewrite it so a 3.10 floor would still hold.
    """
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def discover_project_roots(
    claude_home: Path, extra: tuple[Path, ...] = ()
) -> list[Path]:
    """Every `projects` directory whose transcripts belong to this witness.

    The OTLP collector receives from every profile at once - they all export to
    the same endpoint - so a witness that reads one profile is not measuring the
    same population as the numerator it is compared against. It would undercount
    the denominator, which inflates the capture ratio and hides real loss rather
    than causing a false alarm.

    On a host whose profiles symlink `projects` back to the canonical directory
    this changes nothing; `discover_transcript_files` dedupes by real path. It
    matters where profiles hold their own directories, which is the default when
    a profile is created.

    Profiles are resolved as a sibling of `claude_home`, not from `Path.home()`.
    Reaching for the real home would mean a scanner pointed at one directory
    silently pulling in another - wrong for callers, and it makes every test
    scan the live corpus.

    `.claude-profiles` is a convention, not a guaranteed path, so a missing one
    is silently skipped and `extra` exists for layouts that differ.
    """
    roots = [claude_home / "projects"]
    profiles_dir = claude_home.parent / ".claude-profiles"
    if profiles_dir.is_dir():
        roots.extend(
            sorted(p / "projects" for p in profiles_dir.iterdir() if p.is_dir())
        )
    roots.extend(extra)
    return [r for r in roots if r.is_dir()]


def discover_transcript_files(*roots: Path) -> list[Path]:
    """All *.jsonl transcript files under each root, recursive, deduped by
    resolved real path.

    Recursion is mandatory: a depth-2 glob (`projects/*/*.jsonl`) misses the
    `subagents/` subtree entirely, which on a real corpus is a large share of
    transcript volume. `Path.rglob` correctly follows a root that is itself a
    symlink
    (verified: `~/.claude-profiles/*/projects -> ~/.claude/projects` returns
    the same file count as the canonical dir; plain `find` without `-L`
    returns zero on the same path), so no special-casing is needed there.

    The realpath dedup is what makes it safe to point more than one root at
    the same underlying directory — e.g. the canonical `~/.claude/projects`
    plus a profile's `projects` symlink to it — without double-counting.
    """
    seen: set[Path] = set()
    files: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.jsonl")):
            try:
                real = path.resolve()
            except OSError:
                real = path
            if real in seen:
                continue
            seen.add(real)
            files.append(path)
    return files


@dataclass
class FileTally:
    size: int
    mtime_ns: int
    session_id: str | None
    # (message_id, unix ts, model, effort, (value per FIELD_ORDER order)).
    records: list = field(default_factory=list)
    parse_errors: int = 0


def _extract_session_id(record: dict) -> str | None:
    session_id = record.get("sessionId")
    return session_id if isinstance(session_id, str) and session_id else None


def tally_file(path: Path, stat: os.stat_result) -> FileTally:
    """Parse one transcript file into a FileTally.

    Session id is read from the first line that carries one — cheap (no full
    parse needed) and correct even for `subagents/*.jsonl` files, which carry
    the PARENT session's id (verified on disk), not a distinct one, so no
    extra bookkeeping is needed to avoid inventing spurious sessions.

    Usage records are read from every line matching ASSISTANT_MARKER. The
    `<synthetic>` model (an all-zero placeholder Claude Code writes for a
    failed turn, e.g. a server 529) is dropped, and each record is bound by
    its own `timestamp` field — file mtime is never used as a usage or
    freshness value, only (elsewhere) as a cache-invalidation key.
    """
    tally = FileTally(size=stat.st_size, mtime_ns=stat.st_mtime_ns, session_id=None)
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line_no, line in enumerate(fh):
                if tally.session_id is None and line_no == 0:
                    try:
                        first = json.loads(line)
                    except json.JSONDecodeError:
                        first = None
                    if isinstance(first, dict):
                        tally.session_id = _extract_session_id(first)
                if ASSISTANT_MARKER not in line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    tally.parse_errors += 1
                    continue
                if not isinstance(record, dict) or record.get("type") != "assistant":
                    continue
                if tally.session_id is None:
                    tally.session_id = _extract_session_id(record)
                message = record.get("message")
                if not isinstance(message, dict):
                    tally.parse_errors += 1
                    continue
                model = message.get("model")
                if model == SYNTHETIC_MODEL:
                    continue
                if not isinstance(model, str) or not model:
                    model = "unknown"
                message_id = message.get("id")
                if not isinstance(message_id, str) or not message_id:
                    tally.parse_errors += 1
                    continue
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    tally.parse_errors += 1
                    continue
                timestamp = record.get("timestamp")
                if not isinstance(timestamp, str):
                    tally.parse_errors += 1
                    continue
                try:
                    ts = parse_utc(timestamp).timestamp()
                except ValueError:
                    tally.parse_errors += 1
                    continue
                effort = record.get("effort")
                effort = effort if isinstance(effort, str) else ""
                values = tuple(
                    v if isinstance(v := usage.get(f), int) else 0 for f in FIELD_ORDER
                )
                tally.records.append((message_id, ts, model, effort, values))
    except OSError as err:
        LOG.warning("unreadable transcript file %s: %s", path, err)
        tally.parse_errors += 1
    return tally


class TranscriptScanner:
    """Incremental scanner over `~/.claude/projects/**/*.jsonl`.

    Finished session files rarely change, so parsed records are cached keyed
    by (size, mtime_ns) and only new/appended files are re-parsed — the same
    design as codex_ledger_exporter.LedgerScanner. Aggregation dedups replay
    copies of one `message.id` across (and within) files by keeping the copy
    with the highest `output_tokens`: intermediate streaming copies of the
    same message hold a placeholder output_tokens (median 3 tokens observed)
    while only the final copy carries the true value — keeping the first
    copy instead understates output tokens by double digits of percent.

    The corpus-shrunk guard compares each scan against a high-water mark
    persisted at state_path, so it spans restarts and exporter-version
    changes. Unlike the codex ledger, this corpus is retention-pruned by
    Claude Code itself, so a shrink is not necessarily corruption — flagging
    it is still correct: a consumer treating this counter's `increase()` at
    face value needs to know either way. state_path=None keeps the guard in
    memory only (tests).
    """

    def __init__(
        self,
        claude_home: Path,
        state_path: Path | None = None,
        extra_roots: tuple[Path, ...] = (),
    ) -> None:
        self.claude_home = claude_home
        self.extra_roots = extra_roots
        self.state_path = state_path
        self._cache: dict[Path, FileTally] = {}
        self._lock = threading.Lock()
        self._high_water: Counter = Counter()
        self._high_water_sessions = 0
        # Exposition state, replaced atomically after each scan.
        self.tokens: Counter = Counter()
        self.session_count = 0
        self.last_write_unix = 0.0
        self.duplicate_records = 0
        self.parse_errors = 0
        self.files_scanned = 0
        self.last_scan_unix = 0.0
        self.scan_duration = 0.0
        self.scan_ok = 0
        self.corpus_shrunk = 0
        if state_path is not None:
            (
                self._high_water,
                self._high_water_sessions,
                self.corpus_shrunk,
            ) = load_state(state_path)

    def scan(self) -> None:
        started = time.monotonic()
        try:
            fresh_cache: dict[Path, FileTally] = {}
            canonical: dict[
                str, tuple
            ] = {}  # message_id -> (ts, model, effort, values)
            session_ids: set[str] = set()
            duplicates = 0
            parse_errors = 0
            last_write = 0.0
            roots = discover_project_roots(self.claude_home, self.extra_roots)
            for path in discover_transcript_files(*roots):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                cached = self._cache.get(path)
                if (
                    cached is not None
                    and cached.size == stat.st_size
                    and cached.mtime_ns == stat.st_mtime_ns
                ):
                    tally = cached
                else:
                    tally = tally_file(path, stat)
                fresh_cache[path] = tally
                parse_errors += tally.parse_errors
                if tally.session_id:
                    session_ids.add(tally.session_id)
                for message_id, ts, model, effort, values in tally.records:
                    if ts > last_write:
                        last_write = ts
                    previous = canonical.get(message_id)
                    if previous is None:
                        canonical[message_id] = (ts, model, effort, values)
                        continue
                    duplicates += 1
                    if values[OUTPUT_INDEX] > previous[3][OUTPUT_INDEX]:
                        canonical[message_id] = (ts, model, effort, values)
            tokens: Counter = Counter()
            for _, model, effort, values in canonical.values():
                for fld, value in zip(FIELD_ORDER, values):
                    if value > 0:
                        tokens[(model, effort, TOKEN_FIELDS[fld])] += value
            with self._lock:
                # A shrink means either normal retention pruning old session
                # files or genuine data loss — this exporter cannot tell
                # those apart, so it flags either rather than guessing.
                token_shrunk = any(
                    tokens[k] < self._high_water[k] for k in self._high_water
                )
                session_shrunk = len(session_ids) < self._high_water_sessions
                shrunk = int(token_shrunk or session_shrunk)
                self._high_water |= tokens
                self._high_water_sessions = max(
                    self._high_water_sessions, len(session_ids)
                )
                self._cache = fresh_cache
                self.tokens = tokens
                self.session_count = len(session_ids)
                self.last_write_unix = last_write
                self.duplicate_records = duplicates
                self.parse_errors = parse_errors
                self.files_scanned = len(fresh_cache)
                self.last_scan_unix = time.time()
                self.scan_duration = time.monotonic() - started
                self.scan_ok = 1
                self.corpus_shrunk = max(self.corpus_shrunk, shrunk)
                high_water = Counter(self._high_water)
                high_water_sessions = self._high_water_sessions
                corpus_shrunk = self.corpus_shrunk
            if self.state_path is not None:
                save_state(
                    self.state_path, high_water, high_water_sessions, corpus_shrunk
                )
        except Exception:
            # Serve last good values, but flag the failure loudly.
            LOG.exception("transcript scan failed")
            with self._lock:
                self.scan_ok = 0

    def exposition(self, env_label: str) -> str:
        env = f'env="{escape_label_value(env_label)}",'
        with self._lock:
            lines = [
                "# HELP claude_transcript_token_usage_total Claude Code tokens"
                " summed from local transcripts (~/.claude/projects), replay"
                " deduped by message id, by model, effort and type. `type`"
                " values match claude_code_token_usage_tokens_total so a"
                " capture ratio can be computed directly between the two.",
                "# TYPE claude_transcript_token_usage_total counter",
            ]
            for (model, effort, token_type), value in sorted(self.tokens.items()):
                lines.append(
                    "claude_transcript_token_usage_total"
                    f'{{{env}effort="{escape_label_value(effort)}",'
                    f'model="{escape_label_value(model)}",'
                    f'type="{token_type}"}} {value}'
                )
            lines += [
                "# HELP claude_transcript_session_count_total distinct"
                " session ids observed across the transcript corpus.",
                "# TYPE claude_transcript_session_count_total counter",
                f"claude_transcript_session_count_total{{{env[:-1]}}} {self.session_count}",
                "# HELP claude_transcript_last_write_timestamp_seconds unix"
                " timestamp of the most recent transcript record, from the"
                " record's own `timestamp` field (never file mtime, which"
                " measurably inflates freshness).",
                "# TYPE claude_transcript_last_write_timestamp_seconds gauge",
                f"claude_transcript_last_write_timestamp_seconds{{{env[:-1]}}} "
                f"{self.last_write_unix:.3f}",
                "# HELP claude_transcript_scan_ok 1 if the most recent"
                " transcript scan succeeded, 0 if it failed (stale values are"
                " being served).",
                "# TYPE claude_transcript_scan_ok gauge",
                f"claude_transcript_scan_ok {self.scan_ok}",
                "# HELP claude_transcript_parse_errors current malformed or"
                " incomplete record count across the corpus (self-heals when"
                " a mid-write tail completes, hence a gauge).",
                "# TYPE claude_transcript_parse_errors gauge",
                f"claude_transcript_parse_errors {self.parse_errors}",
                "# HELP claude_transcript_duplicate_records streamed copies"
                " of a message id excluded by dedup in the current corpus.",
                "# TYPE claude_transcript_duplicate_records gauge",
                f"claude_transcript_duplicate_records {self.duplicate_records}",
                "# HELP claude_transcript_corpus_shrunk 1 if the token total"
                " or session count ever fell below its persisted high-water"
                " mark. This corpus is retention-pruned by Claude Code, so a"
                " shrink is not necessarily data loss — but it does mean"
                " Prometheus increase() over this counter can read a phantom"
                " spike at that point, same as a genuine reset. Survives"
                " restarts.",
                "# TYPE claude_transcript_corpus_shrunk gauge",
                f"claude_transcript_corpus_shrunk {self.corpus_shrunk}",
                "# TYPE claude_transcript_files_scanned gauge",
                f"claude_transcript_files_scanned {self.files_scanned}",
                "# TYPE claude_transcript_last_scan_timestamp_seconds gauge",
                f"claude_transcript_last_scan_timestamp_seconds {self.last_scan_unix:.3f}",
                "# TYPE claude_transcript_scan_duration_seconds gauge",
                f"claude_transcript_scan_duration_seconds {self.scan_duration:.3f}",
            ]
            return "\n".join(lines) + "\n"


def escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class MetricsHandler(BaseHTTPRequestHandler):
    scanner: TranscriptScanner  # assigned in main() before the server starts
    env_label: str

    def do_GET(self) -> None:  # noqa: N802 (http.server contract)
        if self.path.split("?", 1)[0] != "/metrics":
            self.send_error(404)
            return
        body = self.scanner.exposition(self.env_label).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        # Scrape-per-15s access noise does not belong in the launchd log.
        del format, args


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prometheus exporter for Claude Code token usage from "
        "local transcripts (~/.claude/projects) — read-only, independent of "
        "the OTLP lane"
    )
    parser.add_argument(
        "--claude-home",
        type=Path,
        default=Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))),
    )
    parser.add_argument(
        "--extra-root",
        type=Path,
        action="append",
        default=[],
        metavar="DIR",
        help="additional projects directory to scan; repeatable. Profiles under "
        "~/.claude-profiles are found automatically",
    )
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9315)
    parser.add_argument("--scan-interval", type=float, default=30.0)
    parser.add_argument(
        "--state-file",
        type=Path,
        default=default_state_path(),
        help="where the corpus-shrunk high-water mark is persisted so the "
        "guard survives restarts (default: under CC_METRICS_RUNTIME_DIR)",
    )
    parser.add_argument(
        "--env-label",
        default=os.environ.get("HOST_ENV"),
        help="value for the env label; must match the stack's .env HOST_ENV "
        "(required via flag or HOST_ENV — no silent default, a mismatch "
        "splits the transcript lane across env values)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="scan once, print the exposition to stdout, and exit "
        "(verification; leaves the persisted shrink guard untouched)",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )

    if not args.env_label:
        raise SystemExit("set --env-label or HOST_ENV (must match .env HOST_ENV)")
    if not args.claude_home.is_dir():
        raise SystemExit(f"claude home does not exist: {args.claude_home}")

    # --once is a verification dump and may be pointed at another corpus, so
    # it neither reads nor writes the shared high-water mark.
    scanner = TranscriptScanner(
        args.claude_home,
        state_path=None if args.once else args.state_file,
        extra_roots=tuple(args.extra_root),
    )
    scanner.scan()
    if args.once:
        print(scanner.exposition(args.env_label), end="")
        return

    def rescan_loop() -> None:
        while True:
            time.sleep(args.scan_interval)
            scanner.scan()

    threading.Thread(target=rescan_loop, daemon=True).start()

    MetricsHandler.scanner = scanner
    MetricsHandler.env_label = args.env_label
    server = ThreadingHTTPServer((args.bind, args.port), MetricsHandler)
    LOG.info(
        "serving transcript metrics on %s:%d from %s (%d files)",
        args.bind,
        args.port,
        args.claude_home,
        scanner.files_scanned,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
