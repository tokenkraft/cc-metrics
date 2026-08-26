#!/usr/bin/env python3
"""Prometheus exporter for Codex token usage read from the local session ledger.

Codex's own OTLP metrics lane structurally undercounts (22 % capture measured
over the 7 days to 2026-08-25): the `codex.turn.token_usage` histogram is emitted only on clean
turn completion (aborted/interrupted turns skip emission entirely,
codex-rs/core/src/tasks/mod.rs early return) and the whole exporter is gated
on `analytics.enabled` (openai/codex#26271). The session JSONL ledger under
CODEX_HOME is written synchronously per API call and is the accurate source —
the same source every community tracker (ccusage etc.) reads.

This daemon scans the ledger via the shared codex_ledger module (discovery,
replay dedup, field mapping), sums usage by (model, effort, token_type), and
serves Prometheus text exposition on 127.0.0.1:9314. Counters are recomputed
deterministically from the append-only files, so restarts never reset or skew
totals. The ledger directories must stay append-only: deleting session files
shrinks the counters, which Prometheus reads as a reset (phantom usage spike);
`codex_ledger_corpus_shrunk` flags any observed decrease. The high-water mark
it compares against is persisted under the runtime dir, so the flag also
catches a shrink across a restart or an exporter-version change — the case an
in-memory-only baseline silently missed.

Stdlib only. Runs under launchd (com.tokenkraft.cc-metrics.codex-ledger).
"""

from __future__ import annotations

import argparse
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

from codex_ledger import (
    TOKEN_FIELDS,
    discover_session_files,
    escape_label_value,
    fork_links,
    iter_usage_records,
    parse_utc,
    resolve_fork_roots,
)

LOG = logging.getLogger("codex_ledger_exporter")

STATE_VERSION = 1


def default_runtime_dir() -> Path:
    """User-owned state directory, matching scripts/codex_commit_hook.py."""
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
    return runtime / "codex-ledger-state.json"


def _series_id(model: str, effort: str, token_type: str) -> str:
    # \t cannot occur in a label value the ledger produces, so it is a safe
    # separator for the flat JSON keys.
    return "\t".join((model, effort, token_type))


def load_state(path: Path) -> tuple[Counter, int]:
    """(high-water totals, corpus_shrunk) from disk; empty when unusable.

    A missing or corrupt state file must not stop the exporter — it only
    resets the guard's memory, which is the pre-existing behaviour.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Counter(), 0
    except (OSError, ValueError) as err:
        LOG.warning(
            "state file %s unusable (%s) — shrink guard restarts empty", path, err
        )
        return Counter(), 0
    if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
        LOG.warning(
            "state file %s has version %r, want %s — shrink guard restarts empty",
            path,
            raw.get("version") if isinstance(raw, dict) else None,
            STATE_VERSION,
        )
        return Counter(), 0
    totals = raw.get("high_water")
    counter: Counter = Counter()
    if isinstance(totals, dict):
        for key, value in totals.items():
            parts = key.split("\t")
            if len(parts) == 3 and isinstance(value, int):
                counter[tuple(parts)] = value
    return counter, 1 if raw.get("corpus_shrunk") else 0


def save_state(path: Path, high_water: Counter, corpus_shrunk: int) -> None:
    """Atomically persist the guard's memory. Best-effort by design: failing
    to write must not take the exporter down."""
    payload = {
        "version": STATE_VERSION,
        "corpus_shrunk": int(corpus_shrunk),
        "high_water": {_series_id(*k): v for k, v in high_water.items()},
    }
    try:
        # Same mode as codex_commit_hook._ensure_runtime_directory: the shared
        # runtime dir stays traversable but not listable.
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
        LOG.warning("could not persist ledger state to %s: %s", path, err)


@dataclass
class FileTally:
    size: int
    mtime_ns: int
    # (dedup key, unix ts, model, effort, (value per TOKEN_FIELDS order)).
    records: list = field(default_factory=list)
    parse_errors: int = 0
    # old-schema (fork id, parent id) pairs; feeds the corpus-wide root map.
    links: list = field(default_factory=list)


FIELD_ORDER = tuple(TOKEN_FIELDS)


def tally_file(
    path: Path, stat: os.stat_result, roots: dict[str, str], links: list
) -> FileTally:
    tally = FileTally(size=stat.st_size, mtime_ns=stat.st_mtime_ns, links=links)
    errors = [0]
    try:
        for record in iter_usage_records(path, errors, roots):
            try:
                ts = parse_utc(record.timestamp).timestamp()
            except ValueError:
                errors[0] += 1
                continue
            values = tuple(
                v if isinstance(v := record.usage.get(f), int) else 0
                for f in FIELD_ORDER
            )
            tally.records.append((record.key, ts, record.model, record.effort, values))
    except OSError as err:
        LOG.warning("unreadable session file %s: %s", path, err)
        errors[0] += 1
    tally.parse_errors = errors[0]
    return tally


class LedgerScanner:
    """Incremental scanner over CODEX_HOME session ledgers.

    Finished session files never change, so parsed records are cached keyed by
    (size, mtime_ns) and only new/appended files are re-parsed. Aggregation
    dedups replay copies across files (see codex_ledger.record_key), the
    EARLIEST copy winning — never the first in discovery order, which is
    active-then-archived: a resume replay in sessions/ precedes its archived
    original and carries no model until its own turn_context, so first-wins
    labelled real usage model="unknown" and flipped the label (a per-series
    decrease that trips the shrink guard) once the continuation was archived.

    The corpus-shrunk guard compares each scan against a high-water mark
    persisted at state_path, so it spans restarts and exporter-version
    changes. state_path=None keeps the guard in memory only (tests).
    """

    def __init__(self, codex_home: Path, state_path: Path | None = None) -> None:
        self.codex_home = codex_home
        self.state_path = state_path
        self._cache: dict[Path, FileTally] = {}
        self._lock = threading.Lock()
        self._high_water: Counter = Counter()
        # Exposition state, replaced atomically after each scan.
        self._roots: dict[str, str] = {}
        self.tokens: Counter = Counter()
        self.turn_records: Counter = Counter()
        self.duplicate_records = 0
        self.parse_errors = 0
        self.files_scanned = 0
        self.last_scan_unix = 0.0
        self.scan_duration = 0.0
        self.scan_ok = 0
        self.corpus_shrunk = 0
        if state_path is not None:
            self._high_water, self.corpus_shrunk = load_state(state_path)

    def scan(self) -> None:
        started = time.monotonic()
        try:
            tokens: Counter = Counter()
            turn_records: Counter = Counter()
            canonical: dict[tuple, tuple] = {}  # key -> (ts, model, effort, values)
            duplicates = 0
            parse_errors = 0
            fresh_cache: dict[Path, FileTally] = {}
            # Pass 1 — fork links. Cached per (size, mtime) with the records;
            # only new or appended files are read. Record keys depend on the
            # corpus-wide root map, so a changed map (a new old-schema fork
            # chain — none produced by current Codex) re-tallies everything.
            stats: dict[Path, os.stat_result] = {}
            stale: set[Path] = set()
            links_by_path: dict[Path, list] = {}
            for path in discover_session_files(self.codex_home):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                stats[path] = stat
                cached = self._cache.get(path)
                if (
                    cached is None
                    or cached.size != stat.st_size
                    or cached.mtime_ns != stat.st_mtime_ns
                ):
                    stale.add(path)
                    try:
                        links_by_path[path] = fork_links(path)
                    except OSError as err:
                        LOG.warning("unreadable session file %s: %s", path, err)
                        links_by_path[path] = []
                else:
                    links_by_path[path] = cached.links
            roots = resolve_fork_roots(
                [link for links in links_by_path.values() for link in links]
            )
            if roots != self._roots:
                self._roots = roots
                stale = set(stats)
            # Pass 2 — usage records, re-parsed only where stale.
            for path, stat in stats.items():
                if path in stale:
                    cached = tally_file(path, stat, roots, links_by_path[path])
                else:
                    cached = self._cache[path]
                fresh_cache[path] = cached
                parse_errors += cached.parse_errors
                for key, ts, model, effort, values in cached.records:
                    previous = canonical.get(key)
                    if previous is not None:
                        duplicates += 1
                        if previous[0] <= ts:
                            continue
                    canonical[key] = (ts, model, effort, values)
            for _, model, effort, values in canonical.values():
                turn_records[(model, effort)] += 1
                for fld, value in zip(FIELD_ORDER, values):
                    if value > 0:
                        tokens[(model, effort, TOKEN_FIELDS[fld])] += value
            with self._lock:
                # Append-only corpus should never shrink; a decrease means
                # session files were deleted and increase() will misread the
                # counter reset — flag it loudly. Compared against a persisted
                # high-water mark, not the previous scan, so a shrink across a
                # restart or a parser change is caught too.
                shrunk = int(
                    any(tokens[k] < self._high_water[k] for k in self._high_water)
                )
                self._high_water |= tokens
                self._cache = fresh_cache
                self.tokens = tokens
                self.turn_records = turn_records
                self.duplicate_records = duplicates
                self.parse_errors = parse_errors
                self.files_scanned = len(fresh_cache)
                self.last_scan_unix = time.time()
                self.scan_duration = time.monotonic() - started
                self.scan_ok = 1
                self.corpus_shrunk = max(self.corpus_shrunk, shrunk)
                high_water = Counter(self._high_water)
                corpus_shrunk = self.corpus_shrunk
            if self.state_path is not None:
                save_state(self.state_path, high_water, corpus_shrunk)
        except Exception:
            # Serve last good values, but flag the failure loudly.
            LOG.exception("ledger scan failed")
            with self._lock:
                self.scan_ok = 0

    def exposition(self, env_label: str) -> str:
        env = f'env="{escape_label_value(env_label)}",'
        with self._lock:
            lines = [
                "# HELP codex_ledger_token_usage_total Codex tokens summed from"
                " the local session ledger (CODEX_HOME), replay deduped,"
                " by model, effort and token_type.",
                "# TYPE codex_ledger_token_usage_total counter",
            ]
            for (model, effort, token_type), value in sorted(self.tokens.items()):
                lines.append(
                    "codex_ledger_token_usage_total"
                    f'{{{env}effort="{escape_label_value(effort)}",'
                    f'model="{escape_label_value(model)}",'
                    f'token_type="{token_type}"}} {value}'
                )
            lines += [
                "# HELP codex_ledger_turn_records_total deduplicated"
                " token_count records parsed from the ledger, by model and"
                " effort. One record is one API call, not one turn.",
                "# TYPE codex_ledger_turn_records_total counter",
            ]
            for (model, effort), value in sorted(self.turn_records.items()):
                lines.append(
                    "codex_ledger_turn_records_total"
                    f'{{{env}effort="{escape_label_value(effort)}",'
                    f'model="{escape_label_value(model)}"}} {value}'
                )
            lines += [
                "# HELP codex_ledger_scan_ok 1 if the most recent ledger scan"
                " succeeded, 0 if it failed (stale values are being served).",
                "# TYPE codex_ledger_scan_ok gauge",
                f"codex_ledger_scan_ok {self.scan_ok}",
                "# HELP codex_ledger_parse_errors current malformed-line count"
                " across the corpus (self-heals when a mid-write tail"
                " completes, hence a gauge).",
                "# TYPE codex_ledger_parse_errors gauge",
                f"codex_ledger_parse_errors {self.parse_errors}",
                "# HELP codex_ledger_duplicate_records replay copies"
                " excluded by dedup in the current corpus.",
                "# TYPE codex_ledger_duplicate_records gauge",
                f"codex_ledger_duplicate_records {self.duplicate_records}",
                "# HELP codex_ledger_corpus_shrunk 1 if any series total ever"
                " fell below its persisted high-water mark (session files"
                " deleted, or a parser change lowered a total — counter"
                " semantics broken until Prometheus history is repaired)."
                " Survives restarts.",
                "# TYPE codex_ledger_corpus_shrunk gauge",
                f"codex_ledger_corpus_shrunk {self.corpus_shrunk}",
                "# TYPE codex_ledger_files_scanned gauge",
                f"codex_ledger_files_scanned {self.files_scanned}",
                "# TYPE codex_ledger_last_scan_timestamp_seconds gauge",
                f"codex_ledger_last_scan_timestamp_seconds {self.last_scan_unix:.3f}",
                "# TYPE codex_ledger_scan_duration_seconds gauge",
                f"codex_ledger_scan_duration_seconds {self.scan_duration:.3f}",
            ]
            return "\n".join(lines) + "\n"


class MetricsHandler(BaseHTTPRequestHandler):
    scanner: LedgerScanner  # assigned in main() before the server starts
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
        description="Prometheus exporter for Codex token usage from the local session ledger"
    )
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))),
    )
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9314)
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
        "splits codex series across env values)",
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
    if not args.codex_home.is_dir():
        raise SystemExit(f"CODEX_HOME does not exist: {args.codex_home}")

    # --once is a verification dump and may be pointed at another corpus, so
    # it neither reads nor writes the shared high-water mark.
    scanner = LedgerScanner(
        args.codex_home, state_path=None if args.once else args.state_file
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
        "serving ledger metrics on %s:%d from %s (%d files)",
        args.bind,
        args.port,
        args.codex_home,
        scanner.files_scanned,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
