# cc-metrics

Local metrics pipeline for Claude Code, OpenAI Codex CLI, and xAI Grok Build:

```text
Claude Code ─┐
Codex CLI ───┼─ OTLP ─> OpenTelemetry Collector ─> Prometheus ─> Grafana
Grok Build ──┘                                         ^
Codex session ledger ─> ledger exporter (host:9314) ───┘
```

Runs entirely on your machine. Published ports bind to `127.0.0.1` by default,
and client authentication stays in Claude Code, Codex, or Grok Build — this
stack needs no provider API key.

Codex token totals are read from the local session ledger, not from Codex's
native OTLP metrics, which structurally undercount (~4.5x). See
[Codex ledger token source](#codex-ledger-token-source).

## Dashboard

One Grafana dashboard, "Token & Usage Monitor", provisioned automatically. Five
rows:

| row | what it answers |
| --- | --- |
| Overview | session starts, tokens, estimated cost, Codex threads, active time, cache hit rate |
| Token Usage | tokens over time, split by type, model, and effort level (both sources) |
| Cost Analysis | estimated cost over time and by model, across supported vendors |
| Agents | tokens and estimated cost for Claude traffic carrying a named agent |
| Sessions & Productivity | commits, pull requests, lines changed, edit decisions |

Cost split by model across supported vendors is the reason this exists rather
than any one vendor's own view — see [Cost meaning](#cost-meaning) for what the
figures do and do not represent.

Overview headline tiles:

![Overview tiles: tokens, estimated Codex cost, Codex thread starts, active time](docs/images/overview.webp)

Token usage over the selected range, split by token type:

![Token Usage over 30 days, split by token type](docs/images/token-usage.webp)

The Codex side of the cost split, priced from the list-price rules:

![Estimated Cost by Model: Codex list-price estimates](docs/images/cost-by-model.webp)

Named-agent attribution — the share of Claude tokens carrying an agent name,
and its split by agent type:

![Named agent tokens, share, cost estimate, and the split by agent type](docs/images/agents.webp)

Every panel is empty until a client sends data. Setup steps 3 to 5 do that.

## Requirements

| Component | Supported requirement |
| --- | --- |
| Host | macOS or Ubuntu only |
| Containers | Docker with `docker compose` |
| Claude Code | 2.1.214 or newer for corrected streaming token/cost accounting |
| Codex CLI | 0.145.0 or newer for GPT-5.6 cache-write telemetry |
| Grok Build | 1.0.5 verified; its external OpenTelemetry export is alpha (schema v1) |
| Python | 3.10 or newer for hook installation, pricing maintenance, and tests |
| Git | Needed for cloning and the optional commit metric |
| Password generator | OpenSSL command below, or a password manager |

Both version floors are behavioral, not arbitrary
([Claude monitoring](https://code.claude.com/docs/en/monitoring-usage),
[Codex 0.145.0 source](https://github.com/openai/codex/blob/rust-v0.145.0/codex-rs/core/src/tasks/mod.rs)).

Install [Docker Desktop](https://docs.docker.com/desktop/setup/install/mac-install/)
on macOS, or Docker's
[Engine installation](https://docs.docker.com/engine/install/ubuntu/) plus
`docker-compose-plugin` on Ubuntu, where membership in the `docker` group grants
root-level privileges. Install Git, Python 3.10 or newer, and OpenSSL from
trusted sources. Install and authenticate the clients through their own
instructions — [Claude Code](https://code.claude.com/docs/en/installation),
[Codex CLI](https://developers.openai.com/codex/cli/) — and never add provider
credentials to this repository.

```console
docker version
docker compose version
claude --version
codex --version
python3 --version
```

## Setup

### 1. Get the code and fill deployment values

Run every command below from the repository root.

```console
git clone https://github.com/tokenkraft/cc-metrics.git
cd cc-metrics
cp .env.example .env
```

Create the Grafana administrator password; the `umask` keeps the directory and
file private to your account.

```sh
(umask 077 && mkdir -p .secrets \
  && openssl rand -base64 24 > .secrets/grafana_admin_password.txt)
```

Create the ignored repository-local runtime directory and copy the printed
absolute path into `.env`.

```sh
runtime_directory="$(pwd -P)/runtime"
mkdir -p "$runtime_directory"
printf 'CC_METRICS_RUNTIME_DIR=%s\n' "$runtime_directory"
```

Review every `.env` entry. `HOST_ENV` and `CC_METRICS_RUNTIME_DIR` ship blank
and are required choices; the rest have local defaults that still need review.

| Variable | User input |
| --- | --- |
| `GRAFANA_ADMIN_PASSWORD_FILE` | Host path to password file |
| `CC_METRICS_RUNTIME_DIR` | Absolute local runtime directory |
| `HOST_ENV` | Non-sensitive deployment label |
| `HOST_BIND_ADDRESS` | Listener interface; keep `127.0.0.1` for same-host use |
| `GRAFANA_PORT` | Unused host port for Grafana |
| `PROMETHEUS_PORT` | Unused host port for Prometheus |
| `OTLP_GRPC_PORT` | Unused host port for OTLP/gRPC |
| `OTLP_HTTP_PORT` | Unused host port for OTLP/HTTP |
| `OTEL_METRICS_PORT` | Unused collector metrics port |
| `PROMETHEUS_RETENTION` | Prometheus duration such as `30d` |
| Image variables | Tag plus manifest digest |

Never put passwords or provider keys in `.env`. Verify ignored local state:

```console
git check-ignore .env
git check-ignore .secrets/grafana_admin_password.txt
```

### 2. Validate and start

```console
docker compose config --quiet
docker compose pull
docker compose up -d
docker compose ps
```

Find effective addresses:

```console
docker compose port grafana 3000
docker compose port prometheus 9090
docker compose port otel-collector 4317
docker compose port otel-collector 4318
```

Open Grafana at the returned binding, sign in as `admin`, then open
**AI tools → Token & Usage Monitor**. Open Prometheus `/targets` at its binding.

Grafana reads the password secret only when initializing an empty data volume;
replacing the file later does not rotate a stored password. Rotate in the
Grafana UI, then update the local secret file to match — see
[troubleshooting.md](troubleshooting.md) for lost-password recovery.

Editing a dashboard JSON under `grafana/dashboards/` can silently do nothing: at
`updateIntervalSeconds: 10` Grafana watches the directory instead of polling it,
and an in-place write to an existing file fires no watcher event on a bind
mount, so the edit is never picked up and nothing is logged. Write so the inode
changes — write a temporary file, then rename it over the original — or restart
Grafana, which re-provisions unconditionally. Confirm what Grafana serves by
reading the stored `version` from `/api/dashboards/uid/<uid>`; if it did not
increment, the edit did not land.

### 3. Send Claude Code metrics

For same-host default, `<OTLP_HOST>` is `127.0.0.1`. A bind address such as
`0.0.0.0` is not a client destination; VM or container clients need an address
reachable from their network.

```sh
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_METRICS_EXPORTER=otlp
export OTEL_LOGS_EXPORTER=none
export OTEL_TRACES_EXPORTER=none
export OTEL_EXPORTER_OTLP_PROTOCOL=grpc
export OTEL_EXPORTER_OTLP_ENDPOINT="http://<OTLP_HOST>:<OTLP_GRPC_PORT>"
export OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE=delta
export OTEL_METRICS_INCLUDE_SESSION_ID=false
export OTEL_METRICS_INCLUDE_ACCOUNT_UUID=false
```

Launch Claude Code from this shell. Add the exports to your shell startup file
only after successful ingestion, and restart running Claude Code processes after
any change.

Shell exports reach only processes that inherit that shell. A Claude Code
started by a supervisor — launchd, systemd, or an agent daemon those start —
inherits none of them and exports nothing, silently, while its session logs keep
recording normally — a real outage of this shape held 2 % capture.
Where that applies, put the same variables in the `env` block of
`~/.claude/settings.json`: the CLI reads it regardless of parent process, and
settings `env` takes precedence over process env.

Disabling the optional session and account fields does not remove every possible
identity field at the client. The collector applies a fail-closed label
allowlist and replaces deployment `env` with `HOST_ENV`.

### 4. Send Codex metrics

Telemetry routing is user-level configuration; a project `.codex/config.toml`
cannot override it.

```toml
[otel]
environment = "<codex-otel-environment>"

[otel.metrics_exporter."otlp-grpc"]
endpoint = "http://<OTLP_HOST>:<OTLP_GRPC_PORT>"
```

Replace both placeholders, save in `~/.codex/config.toml`, then restart Codex.
This enables the metrics exporter only; Codex analytics is a separate
`analytics.enabled` setting, and the OTLP metrics exporter is wholly gated on
it — with analytics disabled, Codex's native OTLP lane emits nothing at all.
Contracts:
[configuration](https://developers.openai.com/codex/config-reference),
[telemetry](https://developers.openai.com/codex/config-advanced#observability-and-telemetry).

### 5. Send Grok Build metrics

Grok Build's external OpenTelemetry stream is off by default and needs a double
opt-in — master switch plus explicit exporter. Add to `~/.grok/config.toml`;
environment variables `GROK_EXTERNAL_OTEL` and `OTEL_*` override these keys.

```toml
[telemetry]
otel_enabled = true
otel_metrics_exporter = "otlp"
otel_endpoint = "http://<OTLP_HOST>:<OTLP_HTTP_PORT>"
otel_protocol = "http/protobuf"
```

Replace both placeholders, then restart Grok Build. Leave `otel_logs_exporter`
unset: this stack ingests metrics only. The collector admits only
`grok_code.token.usage` from this client and strips its session identity. The
stream is alpha (schema v1), so additive changes can land without notice; the
contract is the CLI's own user guide, *Monitoring Usage (External
OpenTelemetry)*, installed under `~/.grok/docs/user-guide/`.

### 6. Verify ingestion

Generate normal tool activity, wait at least one exporter interval, then:

```console
docker compose ps
docker compose logs --tail=100 otel-collector
docker compose logs --tail=100 prometheus
```

Prometheus target `cc-metrics-collector` must be `UP`. Query:

```promql
{__name__=~"claude_code_.*|codex_.*|grok_code_.*"}
```

Select matching `source` and `env` values in Grafana, and see
[troubleshooting.md](troubleshooting.md) when series remain absent.

## Operations

Everything in this section is optional.

### Keep the stack running

`scripts/ensure-stack.sh` runs an idempotent `docker compose up -d`, safe to
repeat — suits launchd, cron, or a systemd timer for bringing the stack back
after reboot. A lock directory prevents two invocations racing; see
[troubleshooting.md](troubleshooting.md) if forced termination leaves
`runtime/ensure-stack.lock` behind.

### Get told when a lane goes dark

A stopped emitter is invisible: scraping continues, the dashboard keeps drawing,
one lane just reads low. `ClaudeLaneDarkWhileOthersActive` in
`prometheus-rules/ai-unified.yml` fires when Claude takes under 5 % of
trailing-3h tokens while the Codex and grok lanes move more than 100M, held
`for: 2h` so overnight idle cannot trip it.

The stack runs no Alertmanager. `python3 scripts/alert_notify.py` polls
`localhost:9090/api/v1/alerts` and raises a macOS notification once per firing
alert, staying quiet until it resolves; nothing leaves the machine. Run it from
a `StartInterval` LaunchAgent — an agent, not a daemon, because
`display notification` needs the GUI session.

Prometheus runs without `--web.enable-lifecycle`, so `POST /-/reload` returns
403, and restarting leaves a scrape gap that cannot be backfilled. Validate rule
edits with `promtool`, SIGHUP to reload config and rules in place, then confirm
the container's `StartedAt` is unchanged and
`prometheus_config_last_reload_successful` reads 1.

```console
docker kill -s HUP "$(docker compose ps -q prometheus)"
```

### Catch telemetry breaking on a tool upgrade

Cross-lane alerting catches a fully dark lane, not partial loss — and partial is
the shape real outages take: two here held 2.0 % and 2.2 % capture without ever
reaching zero. Three scripts cover that.

- `scripts/telemetry-canary.sh` proves Claude can still export. It runs
  `claude -p` under `env -i` and requires two signals — the process reporting
  `First metrics export: SUCCESS` and a matching counter delta at the collector
  — because the log reports intent, not arrival, and `ps eww` proves nothing,
  since settings-injected variables never reach the kernel environ. With
  profiles rather than `~/.claude`, set `CLAUDE_CONFIG_DIR` or it exits 2 with
  `Not logged in`: `CLAUDE_CONFIG_DIR="$HOME/.claude" scripts/telemetry-canary.sh`.
- `scripts/version-gate.sh` runs the canary only when `claude --version` or
  `paseo --version` differs from `runtime/tool-versions.state`, and always exits
  0 so a telemetry fault cannot wedge the watchdog. `scripts/ensure-stack.sh`
  calls it, so no extra scheduling is needed.
- `scripts/claude_transcript_exporter.py` measures *how much* was lost, from the
  per-turn JSONL transcript Claude Code writes regardless of OTEL state. Serve
  it on `:9315`, scrape it as job `claude-transcript`, and
  `ClaudeTelemetryCaptureLoss` compares the lanes. `WitnessExporterDown`,
  `WitnessExporterStale`, and `TranscriptCorpusShrunk` guard the witness itself
  — a dead exporter, frozen counters behind a healthy `up`, and a retention
  prune read as a counter reset each stop the comparison from firing.

### Optional Codex commit metric

Codex exports no repository commit counter. The installer adds `PreToolUse` and
`PostToolUse` Bash hooks:

```console
python3 scripts/install_codex_commit_hook.py \
  --runtime-dir "$(pwd -P)/runtime"
```

Pass the exact absolute path if `.env` uses another runtime directory. Confirm
the printed `runtime_dir` matches `.env`, restart Codex and the collector, then
open `/hooks` in Codex and review the exact commands before trusting them.

Only commits around a Bash command naming `am`, `cherry-pick`, `commit`,
`merge`, `pull`, `rebase`, or `revert` are counted; an editor, GUI client, or
unseen script is not. The public event file holds an event name and an HMAC
deduplication ID, never a repository path or commit SHA. The metric is
forward-looking and best-effort: no attribution of existing history, no backfill
of stopped-time events, a 10,000-record cap on the event file, and hook errors
failing open into a private local error log.

Maintain or remove the installed copy with the same installer, repeating any
custom install arguments:

```console
python3 scripts/install_codex_commit_hook.py --update
python3 scripts/install_codex_commit_hook.py --uninstall
```

Uninstall preserves the installed script and runtime data, so review the printed
paths before deleting either.

## Codex ledger token source

Codex's native OTLP token histogram (`codex.turn.token_usage`) undercounts. It
is emitted only when a turn completes cleanly — aborted and interrupted turns
skip it (`codex-rs/core/src/tasks/mod.rs`) — and the exporter is gated on
`analytics.enabled`
([openai/codex#26271](https://github.com/openai/codex/issues/26271)). Measured
against the session ledger over a week of normal use, the OTLP lane delivered
22 % of real volume, silently.

`scripts/codex_ledger_exporter.py` (stdlib-only) therefore serves the primary
codex counters. It scans the append-only session ledger under `CODEX_HOME`
(`sessions/` and `archived_sessions/`; the active copy wins on duplicate
basenames) and exposes `codex_ledger_token_usage_total` on `127.0.0.1:9314`.
Prometheus scrapes it as job `codex-ledger` (`host.docker.internal:9314`) and
the `source="codex"` recording rules read it. Run it as a launchd or systemd
service with `HOST_ENV` matching `.env`; the env label is required, by flag or
`HOST_ENV`, and has no default.

```console
python3 scripts/codex_ledger_exporter.py            # daemon, port 9314
python3 scripts/codex_ledger_exporter.py --once     # one scan to stdout
```

Its shrink-guard high-water mark persists to `codex-ledger-state.json` under
`CC_METRICS_RUNTIME_DIR`, or — when that is unset in the daemon's environment —
`~/Library/Application Support/cc-metrics/runtime/` on macOS and
`$XDG_STATE_HOME/cc-metrics/runtime/` (default
`~/.local/state/cc-metrics/runtime/`) on Linux. `--state-file` overrides both.

**Bind address.** On macOS, Docker Desktop reaches the default `127.0.0.1` bind
through `host.docker.internal`. On Linux Docker Engine that name maps to the
Docker bridge gateway (shipped `extra_hosts` entry in `docker-compose.yml`),
which cannot reach a loopback bind — run the exporter with `--bind` set to the
bridge address, commonly `172.17.0.1`, never a LAN interface.

**Replay dedup.** A session continued in a new rollout file — subagent fork
(`forked_from_id`) or `codex resume` (same `session_id`) — replays earlier
records re-stamped with the continuation's timestamp, so records are identified
by session lineage plus `info.total_token_usage` and `info.last_token_usage`,
never by timestamp; keying on timestamps double-counts every replay. The
identity is a heuristic whose bound is documented in `scripts/codex_ledger.py`.

**Effort label and upgrades.** Reasoning effort comes from `turn_context.effort`
and is exposed as the `effort` label, matching what Claude Code emits natively,
so the "Tokens by Effort" panel covers both sources. Adding it changes series
identity, so an existing install shows a one-off phantom spike until history is
regenerated: run `scripts/backfill_codex_ledger_history.py` twice — default mode
repairs the `ai_token_usage_tokens_total` history the token panels read, `--raw`
repairs the `codex_ledger_token_usage_total` history the cost panels read. The
same script generates hourly-grid OpenMetrics history predating the first
scrape, for `promtool tsdb create-blocks-from openmetrics`; `--help` documents
the splice constraints.

**Self-telemetry.** `codex_ledger_scan_ok`, `codex_ledger_parse_errors`,
`codex_ledger_duplicate_records`, `codex_ledger_corpus_shrunk`, and
`codex_ledger_last_scan_timestamp_seconds`. The native OTLP lane stays scraped
and `ai_codex_otlp_capture_ratio` compares the two: near-0 with ledger traffic
means the OTLP lane died; sustained >0.95 means upstream fixed emission and the
exporter can be retired. Both lanes see only this machine's sessions — Codex
web and cloud usage is invisible to both.

## Metrics contract

Client metrics land under one `ai_*` namespace. The collector accepts OTLP,
injects the configured `env`, and drops unapproved resource and datapoint
attributes; session, thread, producer, and instrumentation-scope identity is
stripped before anything reaches Prometheus, leaving only aggregate Claude
session-start and Codex thread-start counters.

All three clients emit delta temporality (Claude via the export above, Codex
natively as of 0.145.0, Grok Build by default). The collector rejects unknown
metric families and non-delta input, compacts equal safe-label streams, then
converts deltas to cumulative state held in collector memory. A collector
restart, or a stream idle past `max_stale: 24h`, starts that stream's cumulative
state over, which Prometheus may read as a counter reset; the
`max_streams: 10000` cap does something different — once the tracking limit is
reached, new streams are dropped. Inspect restart boundaries when reconciling
totals.

The runtime contract is best-effort observability, not financial exactly-once
delivery: abrupt failure can lose acknowledged points, and retries without
stable event IDs can duplicate them. Provider billing remains the source of
truth.

Codex display categories are disjoint — fresh input is emitted input minus
cache-read and cache-write input, non-reasoning output is emitted output minus
reasoning output, and cache read, cache creation, and reasoning output each stay
separate. Grok Build schema v1 reports `input` inclusive of `cache_read` and
`output` inclusive of `reasoning` with no cache-write type; the same disjoint
categories are derived from it. Cost expressions price raw Codex and Grok
output, which already includes reasoning tokens. GPT-5.6 cache-write
decomposition is source-backed for Codex 0.145.0 but unproven here against a
captured live export fixture; treat it as version-bound until one exists.

## Cost meaning

Cost panels are operational estimates, never invoices.

- Claude values come from `claude_code.cost.usage`; Anthropic calls them
  approximations and directs users to provider billing.
- Codex values apply rates in `pricing/openai-model-pricing.json` to telemetry.
  They represent standard OpenAI API list-price equivalents.
- Grok values apply rates in `pricing/xai-model-pricing.json` to telemetry, as
  xAI API list-price equivalents at the short-context tier; `grok-4.6` is the
  sole priced model there. xAI publishes no separate reasoning rate, so gross
  output is priced at the output rate.
- Unknown models and unmatched token types are omitted; omitted volume appears
  in the **Unpriced Codex/Grok Tokens** diagnostic.
- Estimates exclude billing modifiers telemetry cannot see: subscription or
  credit charges, Batch/Flex/Priority selection, regional uplift, long-context
  multipliers, and separately billed tools or containers.

Use Claude Console, Amazon Bedrock, Google provider billing, Microsoft provider
billing, OpenAI billing, or xAI billing records as applicable.

## Concurrency correctness boundary

- The pipeline removes producer identity and compacts concurrent producers'
  delta streams safely; compaction adds at most 200 ms before downstream
  processing under normal load.
- The best-effort contract does not promise exactly-once delivery, replay
  deduplication, crash-safe acknowledged batches, strict wall-clock ordering,
  or continuity through collector restart.
- Hook event appends and installer runs are serialized with local lock files
  and atomic renames, validated only on local macOS/Ubuntu file systems —
  network file systems are outside the supported contract.
- `scripts/ensure-stack.sh` lets one invocation run; a concurrent invocation
  exits successfully.

## Maintenance

### Validation

`scripts/check.sh` runs every check below, plus Docker-based collector and
Prometheus validation when Docker is available; exit `3` means everything passed
but a tool was missing and its check was skipped (printed). The individual
commands run from a checkout without a `.env`, supplying the two blank variables
inline as CI does:

```console
python3 -m unittest discover -s tests -v
python3 scripts/generate_pricing_rules.py --check
ruff check scripts tests
ruff format --check scripts tests
shellcheck scripts/*.sh
jq empty grafana/dashboards/*.json
HOST_ENV=ci CC_METRICS_RUNTIME_DIR=./runtime \
  docker compose --env-file .env.example config --quiet
```

CI also runs `promtool check config`, `promtool check rules`, Prometheus rule
tests, collector config validation, secret scanning, and filesystem checks.

### Pricing updates

`pricing/openai-model-pricing.json` owns Codex rates and
`pricing/xai-model-pricing.json` owns Grok rates, each with source URLs. Verify
rates against the official OpenAI or xAI pages, update the verification and
effective-date fields, then run the commands below; `--provider openai` or
`--provider xai` limits a run to one provider, and the default covers both.
Never edit the generated pricing block in the recording rules.

```console
python3 scripts/generate_pricing_rules.py --write
python3 scripts/generate_pricing_rules.py --check
python3 -m unittest tests/test_pricing_rules.py tests/test_dashboard_contract.py
```

### Stack updates

Image references are explicit — nothing changes until `.env` references change.
Review release notes and image digests, update `.env`, then repeat the
validate-and-start commands from setup step 2.

### Stop and remove

`docker compose down` stops the stack and preserves named volumes.

> **Warning:** The next command permanently deletes local Prometheus and
> Grafana named-volume data.

```console
docker compose down --volumes
```

Hook data is separate: uninstall the hook first, then delete only the exact
paths printed by the installer, after deciding retention needs.

## Troubleshooting

[troubleshooting.md](troubleshooting.md) covers absent series per client,
collector and target failures, empty Grafana, password recovery, counter changes
around collector restart, cost reconciliation, hook and lock problems, and how
to report an issue safely.

## Security and privacy

- Keep published ports on loopback. The stack has no Prometheus or collector
  application authentication.
- The Grafana password protects Grafana only, and its secret file seeds first
  database initialization only.
- Never commit `.env`, `.secrets/`, runtime state, backups, metric exports, or
  logs.
- Metrics can carry model, tool, user, account, repository, session, and
  environment metadata before collector filtering. Exported labels retain exact
  model names and bounded Claude `agent.name` attribution — never put people,
  emails, account IDs, session IDs, secrets, or customer data in either field.
- Client logs, traces, prompts, and tool content are outside this metrics-only
  setup.
- Do not expose the stack to another network without authentication, TLS,
  firewall, retention, and privacy design.

See [SECURITY.md](SECURITY.md) for reporting and the supported-security
boundary.

## Contributing, license, and provenance

See [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request. This
project is licensed under the MIT [LICENSE](LICENSE).

Grafana, Prometheus, and the OpenTelemetry Collector are pulled as upstream
container images, pinned by digest in `.env.example`, and remain under their own
licenses.

This project started from Anthropic's
[Claude Code monitoring guide](https://github.com/anthropics/claude-code-monitoring-guide),
written by Kashyap Coimbatore Murali. Thanks for the starting point.
