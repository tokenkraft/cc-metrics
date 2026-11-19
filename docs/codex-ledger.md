---
updated: "2026-11-19"
rev: 15
---

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

### Analytics gate

Set `analytics.enabled = true` explicitly for app-server metrics. App-server
metrics default off when launcher omits `--analytics-default-enabled`. Restart
the app-server process after changing the setting; a new thread does not
rebuild its metrics provider.

In Codex 0.153.4, `false` disables OTLP metrics and its external analytics
queue, while unset still enables the queue. A loopback OTLP endpoint does not
localize that queue. The ledger exporter works independently; disabling
analytics removes the native capture-ratio cross-check, not ledger token
panels. Evidence:
[metrics gate](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/core/src/otel_init.rs#L68-L94),
[analytics queue](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/analytics/src/client.rs#L239-L249),
[startup initialization](https://github.com/openai/codex/blob/rust-v0.153.4/codex-rs/app-server/src/lib.rs#L586-L597).

## The exporter

`scripts/codex_ledger_exporter.py` (stdlib-only) serves the primary Codex
counters. It scans the normally append-only session ledger under `CODEX_HOME`
(`sessions/` and `archived_sessions/`; the active copy wins on duplicate
basenames) and exposes `codex_ledger_token_usage_total` on `127.0.0.1:9314`.

Codex 0.167 and later compresses rollouts older than seven days in place, from
`<name>.jsonl` to `<name>.jsonl.zst`
([compression.rs](https://github.com/openai/codex/blob/rust-v0.167.2/codex-rs/rollout/src/compression.rs)).
The exporter reads both forms as one file, and the plain copy wins when both
exist, as in Codex. Reading `.jsonl.zst` needs Python 3.14 or newer
(`compression.zstd`). On an older interpreter, each compressed file counts as a
parse error, so the scan fails closed and the log names Python 3.14.

Prometheus scrapes the exporter as job `codex-ledger-guarded`
(`host.docker.internal:9314`); `source="codex"` recording rules read it.

### Exporter interpreter

Python 3.10+ can scan plain ledgers. For compressed rollouts, run these
commands from the checkout with Python 3.14+ and the same Codex home as the
service (set `CODEX_HOME` if needed). Replace the label with the deployment's
`HOST_ENV` value from `.env`.

```sh
ledger_python="$(python3.14 -c 'from compression import zstd; import sys; print(sys.executable)')" &&
  "$ledger_python" scripts/codex_ledger_exporter.py --env-label '<HOST_ENV>' --once
```

Require `codex_ledger_scan_ok 1` and `codex_ledger_parse_errors 0` in the output.
The one-time scan leaves the service's saved counters untouched. To start a
new exporter in the foreground, pass the same environment label. The exporter
requires either `--env-label` or the `HOST_ENV` environment variable.

```sh
"$ledger_python" scripts/codex_ledger_exporter.py --env-label '<HOST_ENV>'
```

Use the absolute path printed by `printf '%s\n' "$ledger_python"` as the
first launchd `ProgramArguments` entry or the executable in systemd's
`ExecStart`. Follow it with the absolute exporter script path and keep the
service's existing arguments, environment, and state file. If the plist sets
`Program` explicitly, update that executable too. If the service
starts a wrapper, change the Python command inside it. Omit `--once` for the
service.

For an edited, loaded macOS LaunchAgent, replace the plist path below.

```sh
plist='<absolute-path-to-installed-plist>'
launchctl bootout "gui/$(id -u)" "$plist" && launchctl bootstrap "gui/$(id -u)" "$plist"
```

For a systemd unit, run `systemctl daemon-reload` and `systemctl restart <unit>`
(use `systemctl --user` for a user unit). Check the same two gauges on the
running exporter's `/metrics` endpoint after its first scan.

### Profile homes

Every direct child under a sibling `.codex-profiles/` (for example
`~/.codex-profiles/<account>` next to `~/.codex`) is scanned when that child
contains `config.toml`. This marker distinguishes profile homes from backups
that happen to contain `sessions/`. Keep backups outside `.codex-profiles/` or
nested beneath a profile. A symlinked `sessions/` is read once.

Removing a profile can lower token totals and trip the counter-regression guard.
Adding a profile with an earlier copy of an already counted replay can relabel
series because the earliest copy's model and effort win. That scan is rejected
until history is repaired and state is conditionally rebased.

Adding a profile with new historical usage increases live counters at its first
accepted scan. Prometheus records that increase at scan time, not at original
session times; use the repair runbook when historical timing matters.

### Verify the deployed code

Editing source does not reload a running Python exporter. Restart the installed
service after an update, preserving its state file. Keep both source files
unchanged during startup. For a macOS LaunchAgent, replace the placeholder
with its installed label:

```console
launchctl kickstart -k "gui/$(id -u)/<launch-agent-label>"
```

After the initial scan finishes, run this from the deployed checkout:

```python
import hashlib
from pathlib import Path
from urllib.request import urlopen

sources = ("scripts/codex_ledger_exporter.py", "scripts/codex_ledger.py")
expected = hashlib.sha256(b"\0".join(Path(p).read_bytes() for p in sources)).hexdigest()
with urlopen("http://127.0.0.1:9314/metrics", timeout=10) as response:
    metrics = response.read().decode()
assert f'codex_ledger_build_info{{source_sha256="{expected}"}} 1' in metrics
print("Running exporter startup fingerprint matches this checkout")
```

The fingerprint is captured once at import, so later edits cannot make an old
process report the new source identity. Missing or mismatched identity fails
this check. It identifies startup disk bytes, not executable attestation; a
matching fingerprint alone does not prove token correctness. Also check fresh
successful scans, parse errors, counter resets, and independent session totals.

### State file

The shrink-guard high-water mark persists to `codex-ledger-state.json` under
`CC_METRICS_RUNTIME_DIR`. When that is unset in the daemon's environment, it
lands in `~/Library/Application Support/cc-metrics/runtime/` on macOS and
`$XDG_STATE_HOME/cc-metrics/runtime/` (default
`~/.local/state/cc-metrics/runtime/`) on Linux. `--state-file` overrides both.

### Counter regressions and repair

Rollout rewrites, missing files, and changed replay attribution can lower a
recomputed total. Prometheus interprets a counter decrease as a restart and
counts the remaining lifetime balance as new usage. The exporter rejects
such scans, keeps publishing last-good token totals, sets `scan_ok=0`, and
latches `corpus_shrunk=1`. Any malformed or unreadable ledger file also rejects
the whole scan, even when newer usage offsets the missing tokens. It also checks
derived fresh-input and non-reasoning-output counters.

`CodexLedgerStale` alerts after two minutes of failed scans, or after a scan
timestamp is stale for ten minutes plus the two-minute `for` duration. Frozen
values mean incomplete telemetry, not zero usage.

A regression in one series rejects the whole scan, so token totals stay at
their last good values. The state file does not persist the turn-record
counter; if the first scan after restart is rejected, that diagnostic remains
absent. A later corpus that reaches every persisted high-water mark resumes
publication (`scan_ok=1`), while `corpus_shrunk=1` remains as a historical
latch. That recovered case does not need a TSDB rewrite. Use the
[repair runbook](codex-ledger-repair.md) when aggregate usage is preserved but
series identity or history changed. When a ledger file was deleted or lost,
restore it from backup first; without the original records, no safe history
repair or state rebase can prove the missing usage.

The cache checks change time as well as size and modification time, so a
same-size rewrite preserving modification time still triggers a rescan.
Regression tests cover rewrites, restart persistence, resumed growth, and
cache reclassification in `tests/test_codex_ledger.py`.

### Bind address

On macOS, Docker Desktop reaches the default `127.0.0.1` bind through
`host.docker.internal`. On Linux Docker Engine that name maps to the Docker
bridge gateway (shipped `extra_hosts` entry in `docker-compose.yml`), which
cannot reach a loopback bind. Run the exporter with `--bind` set to the bridge
address, commonly `172.17.0.1`, never a LAN interface.

### Replay dedup

A session continued in a new rollout file, by subagent fork (`forked_from_id`)
or `codex resume` (same `session_id`), replays earlier records re-stamped with
the continuation's timestamp.

Records are therefore identified by session lineage plus
`info.total_token_usage` and `info.last_token_usage`, never by timestamp;
keying on timestamps double-counts every replay. The identity is a heuristic
whose bound is documented in `scripts/codex_ledger.py`.

### Effort label and upgrades

Reasoning effort comes from `turn_context.effort` and is exposed as the
`effort` label, matching what Claude Code emits natively, so the "Tokens by
Effort" panel covers both sources.

Adding it changes series identity, so an existing install needs the ordered
[history-repair runbook](codex-ledger-repair.md). It covers derived and raw
history, conditional state rebase, splice validation, and rollback.

### Self-telemetry

`codex_ledger_build_info`, `codex_ledger_scan_ok`, `codex_ledger_parse_errors`,
`codex_ledger_duplicate_records`, `codex_ledger_corpus_shrunk`,
`codex_ledger_last_scan_timestamp_seconds`, and
`codex_ledger_candidate_token_usage`. Candidate totals describe the latest
attempted scan and exist only for repair validation; production counters still
serve last-good values after a rejected scan.

The native OTLP lane stays scraped and `ai_codex_otlp_capture_ratio` compares
the two over one hour. Zero with ledger traffic establishes no captured native
increase in that window; disabled export, incomplete turns and interrupted
turns require separate checks. A high ratio does not prove complete coverage
or justify retiring the ledger. Both lanes see only this machine's sessions;
Codex web and cloud usage is invisible to both.

For exact reconciliation, compare input plus output tokens with the sum of
the dashboard's disjoint categories. Cached input and reasoning output are
already included in those base fields. Independently compare cumulative
session transitions with per-call usage, and identify replay copies before
summing. Pin full label sets and UTC window boundaries after scans, scrapes
and rule evaluations have caught up. Otherwise a preceding session's delayed
final sample can enter the next window. These checks verify recorded client
usage, not provider billing or subscription allowance accounting.
