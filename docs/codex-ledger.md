# Codex ledger token source

Codex token totals come from the local session ledger, not from Codex's native
OTLP metrics. This page explains why, and how the exporter behaves.

## Why not native OTLP

`codex.turn.token_usage` undercounts. It is emitted only when a turn completes
cleanly; aborted and interrupted turns skip it
(`codex-rs/core/src/tasks/mod.rs`). The exporter is also gated on
`analytics.enabled`
([openai/codex#26271](https://github.com/openai/codex/issues/26271)).
Measured against the session ledger over a week of normal use, the OTLP lane
delivered 22 % of real volume (about a 4.5x undercount), silently.

## The exporter

`scripts/codex_ledger_exporter.py` (stdlib-only) serves the primary Codex
counters. It scans the append-only session ledger under `CODEX_HOME`
(`sessions/` and `archived_sessions/`; the active copy wins on duplicate
basenames) and exposes `codex_ledger_token_usage_total` on `127.0.0.1:9314`.
Prometheus scrapes it as job `codex-ledger` (`host.docker.internal:9314`) and
the `source="codex"` recording rules read it.

Run it as a launchd or systemd service with `HOST_ENV` matching `.env`. The
env label is required, by flag or `HOST_ENV`, and has no default.

```console
python3 scripts/codex_ledger_exporter.py            # daemon, port 9314
python3 scripts/codex_ledger_exporter.py --once     # one scan to stdout
```

### State file

The shrink-guard high-water mark persists to `codex-ledger-state.json` under
`CC_METRICS_RUNTIME_DIR`. When that is unset in the daemon's environment, it
lands in `~/Library/Application Support/cc-metrics/runtime/` on macOS and
`$XDG_STATE_HOME/cc-metrics/runtime/` (default
`~/.local/state/cc-metrics/runtime/`) on Linux. `--state-file` overrides both.

### Bind address

On macOS, Docker Desktop reaches the default `127.0.0.1` bind through
`host.docker.internal`. On Linux Docker Engine that name maps to the Docker
bridge gateway (shipped `extra_hosts` entry in `docker-compose.yml`), which
cannot reach a loopback bind. Run the exporter with `--bind` set to the bridge
address, commonly `172.17.0.1`, never a LAN interface.

### Replay dedup

A session continued in a new rollout file, by subagent fork (`forked_from_id`)
or `codex resume` (same `session_id`), replays earlier records re-stamped with
the continuation's timestamp. Records are therefore identified by session
lineage plus `info.total_token_usage` and `info.last_token_usage`, never by
timestamp; keying on timestamps double-counts every replay. The identity is a
heuristic whose bound is documented in `scripts/codex_ledger.py`.

### Effort label and upgrades

Reasoning effort comes from `turn_context.effort` and is exposed as the
`effort` label, matching what Claude Code emits natively, so the "Tokens by
Effort" panel covers both sources.

Adding it changes series identity, so an existing install shows a one-off
phantom spike until history is regenerated. Run
`scripts/backfill_codex_ledger_history.py` twice: default mode repairs the
`ai_token_usage_tokens_total` history the token panels read, `--raw` repairs
the `codex_ledger_token_usage_total` history the cost panels read. The same
script generates hourly-grid OpenMetrics history predating the first scrape,
for `promtool tsdb create-blocks-from openmetrics`; `--help` documents the
splice constraints.

### Self-telemetry

`codex_ledger_scan_ok`, `codex_ledger_parse_errors`,
`codex_ledger_duplicate_records`, `codex_ledger_corpus_shrunk`, and
`codex_ledger_last_scan_timestamp_seconds`.

The native OTLP lane stays scraped and `ai_codex_otlp_capture_ratio` compares
the two. Near 0 with ledger traffic means the OTLP lane died; sustained above
0.95 means upstream fixed emission and the exporter can be retired. Both lanes
see only this machine's sessions; Codex web and cloud usage is invisible to
both.
