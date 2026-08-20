#!/usr/bin/env python3
"""Prometheus exporter for Codex token usage read from the local session ledger.

Codex's own OTLP metrics lane structurally undercounts (~1/3 capture measured
2026-08-20): the `codex.turn.token_usage` histogram is emitted only on clean
turn completion (aborted/interrupted turns skip emission entirely,
codex-rs/core/src/tasks/mod.rs early return) and the whole exporter is gated
on `analytics.enabled` (openai/codex#26271). The session JSONL ledger under
CODEX_HOME is written synchronously per API call and is the accurate source —
the same source every community tracker (ccusage etc.) reads.

This daemon scans the ledger via the shared codex_ledger module (discovery,
fork/replay dedup, field mapping), sums usage by (model, token_type), and
serves Prometheus text exposition on 127.0.0.1:9314. Counters are recomputed
deterministically from the append-only files, so restarts never reset or skew
totals. The ledger directories must stay append-only: deleting session files
shrinks the counters, which Prometheus reads as a reset (phantom usage spike);
`codex_ledger_corpus_shrunk` flags any observed decrease.

Stdlib only. Runs under launchd (com.tokenkraft.cc-metrics.codex-ledger).
"""

from __future__ import annotations

import argparse
import logging
import os
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
    iter_usage_records,
)

LOG = logging.getLogger("codex_ledger_exporter")


@dataclass
class FileTally:
    size: int
    mtime_ns: int
    # (dedup key, model, (value per TOKEN_FIELDS order)) per usage record.
    records: list = field(default_factory=list)
    parse_errors: int = 0


FIELD_ORDER = tuple(TOKEN_FIELDS)


def tally_file(path: Path, stat: os.stat_result) -> FileTally:
    tally = FileTally(size=stat.st_size, mtime_ns=stat.st_mtime_ns)
    errors = [0]
    try:
        for record in iter_usage_records(path, errors):
            values = tuple(
                v if isinstance(v := record.usage.get(f), int) else 0
                for f in FIELD_ORDER
            )
            tally.records.append((record.key, record.model, values))
    except OSError as err:
        LOG.warning("unreadable session file %s: %s", path, err)
        errors[0] += 1
    tally.parse_errors = errors[0]
    return tally


class LedgerScanner:
    """Incremental scanner over CODEX_HOME session ledgers.

    Finished session files never change, so parsed records are cached keyed by
    (size, mtime_ns) and only new/appended files are re-parsed. Aggregation
    dedups fork/replay copies across files (see codex_ledger.record_key),
    first occurrence in discovery order winning.
    """

    def __init__(self, codex_home: Path) -> None:
        self.codex_home = codex_home
        self._cache: dict[Path, FileTally] = {}
        self._lock = threading.Lock()
        # Exposition state, replaced atomically after each scan.
        self.tokens: Counter = Counter()
        self.turn_records: Counter = Counter()
        self.duplicate_records = 0
        self.parse_errors = 0
        self.files_scanned = 0
        self.last_scan_unix = 0.0
        self.scan_duration = 0.0
        self.scan_ok = 0
        self.corpus_shrunk = 0

    def scan(self) -> None:
        started = time.monotonic()
        try:
            tokens: Counter = Counter()
            turn_records: Counter = Counter()
            seen: set = set()
            duplicates = 0
            parse_errors = 0
            fresh_cache: dict[Path, FileTally] = {}
            for path in discover_session_files(self.codex_home):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                cached = self._cache.get(path)
                if (
                    cached is None
                    or cached.size != stat.st_size
                    or cached.mtime_ns != stat.st_mtime_ns
                ):
                    cached = tally_file(path, stat)
                fresh_cache[path] = cached
                parse_errors += cached.parse_errors
                for key, model, values in cached.records:
                    if key in seen:
                        duplicates += 1
                        continue
                    seen.add(key)
                    turn_records[model] += 1
                    for fld, value in zip(FIELD_ORDER, values):
                        if value > 0:
                            tokens[(model, TOKEN_FIELDS[fld])] += value
            with self._lock:
                # Append-only corpus should never shrink; a decrease means
                # session files were deleted and increase() will misread the
                # counter reset — flag it loudly.
                shrunk = int(any(tokens[k] < self.tokens[k] for k in self.tokens))
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
                " the local session ledger (CODEX_HOME), fork/replay deduped,"
                " by model and token_type.",
                "# TYPE codex_ledger_token_usage_total counter",
            ]
            for (model, token_type), value in sorted(self.tokens.items()):
                lines.append(
                    "codex_ledger_token_usage_total"
                    f'{{{env}model="{escape_label_value(model)}",'
                    f'token_type="{token_type}"}} {value}'
                )
            lines += [
                "# HELP codex_ledger_turn_records_total deduplicated"
                " token_count records parsed from the ledger, by model.",
                "# TYPE codex_ledger_turn_records_total counter",
            ]
            for model, value in sorted(self.turn_records.items()):
                lines.append(
                    "codex_ledger_turn_records_total"
                    f'{{{env}model="{escape_label_value(model)}"}} {value}'
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
                "# HELP codex_ledger_duplicate_records fork/replay copies"
                " excluded by dedup in the current corpus.",
                "# TYPE codex_ledger_duplicate_records gauge",
                f"codex_ledger_duplicate_records {self.duplicate_records}",
                "# HELP codex_ledger_corpus_shrunk 1 if any series total ever"
                " decreased between scans (session files deleted — counter"
                " semantics broken until Prometheus history is repaired).",
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
        "--env-label",
        default=os.environ.get("HOST_ENV"),
        help="value for the env label; must match the stack's .env HOST_ENV "
        "(required via flag or HOST_ENV — no silent default, a mismatch "
        "splits codex series across env values)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="scan once, print the exposition to stdout, and exit (verification)",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )

    if not args.env_label:
        raise SystemExit("set --env-label or HOST_ENV (must match .env HOST_ENV)")
    if not args.codex_home.is_dir():
        raise SystemExit(f"CODEX_HOME does not exist: {args.codex_home}")

    scanner = LedgerScanner(args.codex_home)
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
