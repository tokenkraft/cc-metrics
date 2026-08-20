# cc-metrics

Local metrics pipeline for Claude Code, OpenAI Codex CLI, and xAI Grok Build:

```text
Claude Code ─┐
Codex CLI ───┼─ OTLP ─> OpenTelemetry Collector ─> Prometheus ─> Grafana
Grok Build ──┘                                         ^
Codex session ledger ─> ledger exporter (host:9314) ───┘
```

Codex token totals are read from the local session ledger, not from Codex's
native OTLP metrics — the native lane structurally undercounts (~3x). See
"Codex ledger token source" below.

Runs entirely on your machine. Published ports bind to `127.0.0.1` by default.
Client authentication stays in Claude Code, Codex, or Grok Build; this stack
needs no provider API key.

## Dashboard

One Grafana dashboard, "Token & Usage Monitor", provisioned automatically. Five
rows:

| row | what it answers |
| --- | --- |
| Overview | session starts, tokens, estimated cost, Codex threads, active time, cache hit rate |
| Token Usage | tokens over time, split by type, model, and Claude effort level |
| Cost Analysis | estimated cost over time and by model, across supported vendors |
| Agents | tokens and estimated cost for Claude traffic carrying a named agent |
| Sessions & Productivity | commits, pull requests, lines changed, edit decisions |

Cost split by model across supported vendors is the reason this exists rather
than any one vendor's own view. Claude figures are the provider's own estimates; Codex
and Grok figures are list-price estimates computed here — see
[Cost meaning](#cost-meaning) for what that difference implies.

Overview headline tiles:

![Overview tiles: tokens, estimated Codex cost, Codex thread starts, active time](docs/images/overview.webp)

Token usage over the selected range, split by token type:

![Token Usage over 30 days, split by token type](docs/images/token-usage.webp)

The Codex side of the cost split, priced from the list-price rules:

![Estimated Cost by Model: Codex list-price estimates](docs/images/cost-by-model.webp)

Named-agent attribution — the share of Claude tokens carrying an agent name,
and its split by agent type:

![Named agent tokens, share, and the split by agent type](docs/images/agents.webp)

Every panel is empty until a client sends data. Setup steps 3 to 5 do that.

## Features

- Claude Code, Codex, and Grok Build token views under one `ai_*` metric
  namespace.
- Provider-emitted Claude cost estimates.
- Standard OpenAI API list-price estimates for selected Codex models, and xAI
  API list-price estimates for `grok-4.6`.
- Configurable host ports, environment label, retention, and image references.
- Optional, forward-looking Codex commit counter.
- Prometheus rule, dashboard contract, hook, pricing, and hygiene tests.

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

On macOS, install and start
[Docker Desktop](https://docs.docker.com/desktop/setup/install/mac-install/).
On Ubuntu, follow Docker's
[Engine installation](https://docs.docker.com/engine/install/ubuntu/) steps and
install `docker-compose-plugin`; note that membership in the `docker` group
grants root-level privileges. Install Git, Python 3.10 or newer, and OpenSSL
from trusted sources on either host.

Install and authenticate the clients through their official instructions:
[Claude Code](https://code.claude.com/docs/en/installation) and
[Codex CLI](https://developers.openai.com/codex/cli/). Do not add provider
credentials to this repository.

The version floors are behavioral, not arbitrary: Anthropic documents token and
cost inflation in Claude Code releases before 2.1.214, and Codex cache-write
telemetry is version-bound to 0.145.0 — see
[Claude monitoring](https://code.claude.com/docs/en/monitoring-usage) and
[Codex 0.145.0 source](https://github.com/openai/codex/blob/rust-v0.145.0/codex-rs/core/src/tasks/mod.rs).

Check local tools:

```console
docker version
docker compose version
claude --version
codex --version
python3 --version
```

## Setup

### 1. Get the code and fill deployment values

Every command below is run from the repository root:

```console
git clone https://github.com/tokenkraft/cc-metrics.git
cd cc-metrics
cp .env.example .env
```

Create the Grafana administrator password (the `umask` keeps the directory and
file private to your account):

```sh
(umask 077 && mkdir -p .secrets \
  && openssl rand -base64 24 > .secrets/grafana_admin_password.txt)
```

Create the ignored repository-local runtime directory and copy the printed
absolute path into `.env`:

```sh
runtime_directory="$(pwd -P)/runtime"
mkdir -p "$runtime_directory"
printf 'CC_METRICS_RUNTIME_DIR=%s\n' "$runtime_directory"
```

Review every `.env` entry. Blank `HOST_ENV` and `CC_METRICS_RUNTIME_DIR` are
required user choices; other entries provide local defaults that still require
review:

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

Do not put passwords or provider keys in `.env`. Verify ignored local state:

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
**AI tools → Token & Usage Monitor**. Open Prometheus `/targets` at its
binding.

Grafana reads the password secret only when initializing an empty data volume;
replacing the file later does not rotate a stored password. Rotate through the
Grafana UI, then update the local secret file to match. See
[troubleshooting.md](troubleshooting.md) for lost-password recovery.

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
only after successful ingestion, and restart existing Claude Code processes
after changes.

Disabling the optional session/account fields does not remove every possible
identity field at the client. The collector uses a fail-closed label allowlist
and replaces deployment `env` with `HOST_ENV`.

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
The snippet enables the metrics exporter only; Codex analytics is a separate
`analytics.enabled` setting. Official contracts:
[configuration reference](https://developers.openai.com/codex/config-reference)
and
[telemetry configuration](https://developers.openai.com/codex/config-advanced#observability-and-telemetry).

### 5. Send Grok Build metrics

Grok Build's external OpenTelemetry stream is off by default and needs a
double opt-in: the master switch plus an explicit exporter selection. Add to
`~/.grok/config.toml` (environment variables `GROK_EXTERNAL_OTEL` and `OTEL_*`
override these keys when set):

```toml
[telemetry]
otel_enabled = true
otel_metrics_exporter = "otlp"
otel_endpoint = "http://<OTLP_HOST>:<OTLP_HTTP_PORT>"
otel_protocol = "http/protobuf"
```

Replace both placeholders, then restart Grok Build. Leave `otel_logs_exporter`
unset: this stack ingests metrics only. Grok Build exports delta temporality by
default; the collector admits only `grok_code.token.usage` from it and strips
the client's session identity. The stream is alpha (schema v1) — additive
changes can land without notice. Reference: the CLI's own user guide,
*Monitoring Usage (External OpenTelemetry)*, installed under
`~/.grok/docs/user-guide/`.

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

Select matching `source` and `env` values in Grafana. See
[troubleshooting.md](troubleshooting.md) when series remain absent.

### 7. Keep the stack running (optional)

`scripts/ensure-stack.sh` runs an idempotent `docker compose up -d`, safe to
run repeatedly — suits launchd, cron, or a systemd timer to bring the stack
back after reboot. A lock directory prevents two invocations racing; see
[troubleshooting.md](troubleshooting.md) if forced termination leaves
`runtime/ensure-stack.lock` behind.

```console
scripts/ensure-stack.sh
```

## Metrics contract

The collector accepts OTLP, injects the configured `env`, and removes
unapproved resource and datapoint attributes. Privacy filtering strips session,
thread, producer, and instrumentation-scope identities before anything reaches
Prometheus; only aggregate Claude session-start and Codex thread-start counters
remain, never per-session identity.

All three clients emit delta temporality (Claude via the explicit export
above, Codex natively as of 0.145.0, Grok Build by default). The collector
rejects unknown metric families and non-delta input, compacts equal safe-label streams, then converts deltas to
cumulative state for Prometheus. That state lives in collector memory: a
collector restart, a stream inactive past `max_stale: 24h`, or the
`max_streams: 10000` cap can start a stream over, which Prometheus may read as
a counter reset — inspect restart boundaries when reconciling totals.

The runtime contract is best-effort observability, not financial exactly-once
delivery: abrupt failure can lose acknowledged points, and retries without
stable event IDs can duplicate them. Provider billing remains the source of
truth.

Codex display categories are disjoint:

- fresh input = emitted input minus cache-read and cache-write input;
- cache read and cache creation remain separate;
- non-reasoning output = emitted output minus reasoning output;
- reasoning output remains separate.

Grok Build schema v1 reports `input` inclusive of `cache_read` and `output`
inclusive of `reasoning`, with no cache-write type; the same disjoint
categories are derived from it.

Cost expressions price raw Codex and Grok output, which already includes
reasoning tokens. GPT-5.6 cache-write decomposition is source-backed for Codex
0.145.0 but has not been proven here against a captured live export fixture; treat it
as version-bound until one exists.

## Cost meaning

Cost panels are operational estimates, never invoices.

- Claude values come from `claude_code.cost.usage`; Anthropic calls them
  approximations and directs users to provider billing.
- Codex values apply rates in `pricing/openai-model-pricing.json` to telemetry.
  They represent standard OpenAI API list-price equivalents.
- Grok values apply rates in `pricing/xai-model-pricing.json` to telemetry.
  They represent xAI API list-price equivalents at the short-context tier; xAI
  publishes no separate reasoning rate, so gross output is priced at the output
  rate.
- Unknown models and unmatched token types are omitted; omitted volume appears
  in the **Unpriced Codex/Grok Tokens** diagnostic.
- Telemetry does not identify every billing modifier. Estimates exclude
  subscription or credit charges, Batch/Flex/Priority selection, regional
  uplift, long-context multipliers, and separately billed tools or containers.

Use Claude Console, Amazon Bedrock, Google provider billing, Microsoft provider
billing, OpenAI billing, or xAI billing records as applicable.

## Optional Codex commit metric

Codex exports no repository commit counter. The installer adds `PreToolUse` and
`PostToolUse` Bash hooks:

```console
python3 scripts/install_codex_commit_hook.py \
  --runtime-dir "$(pwd -P)/runtime"
```

If `.env` uses another runtime directory, pass that exact absolute path.
Confirm the printed `runtime_dir` matches `.env`, restart Codex and the
collector, then open `/hooks` in Codex and review the exact commands before
trusting them.

The hook counts only commits it detects around a Bash command naming one of:
`am`, `cherry-pick`, `commit`, `merge`, `pull`, `rebase`, `revert`. Commits
made any other way — an editor, a GUI client, an unseen script — are not
counted. The public event file contains an event name and an HMAC
deduplication ID, never a repository path or commit SHA. The metric is
forward-looking and best-effort: existing history is not attributed,
stopped-time events are not backfilled, the event file caps at 10,000 records,
and hook errors fail open into a private local error log.

Update or remove the installed copy (repeat any custom install arguments):

```console
python3 scripts/install_codex_commit_hook.py --update
python3 scripts/install_codex_commit_hook.py --uninstall
```

Uninstall preserves the installed script and runtime data; review the printed
paths before deleting either.

## Codex ledger token source

Codex's native OTLP token histogram (`codex.turn.token_usage`) undercounts:
it is emitted only when a turn completes cleanly (aborted or interrupted turns
skip emission, `codex-rs/core/src/tasks/mod.rs`), and the exporter is gated on
`analytics.enabled` ([openai/codex#26271](https://github.com/openai/codex/issues/26271)).
Measured against the session ledger on 2026-08-20 the OTLP lane delivered 31 %
of real volume, silently.

`scripts/codex_ledger_exporter.py` (stdlib-only) therefore serves the
authoritative codex counters by scanning the append-only session ledger under
`CODEX_HOME` (`sessions/` and `archived_sessions/`, active copy wins on
duplicate basenames; fork/replay copies of the same `token_count` record
across rollout files are deduplicated — parsing shared with the backfill via
`scripts/codex_ledger.py`) and exposing `codex_ledger_token_usage_total` on
`127.0.0.1:9314`. Prometheus scrapes it as job `codex-ledger`
(`host.docker.internal:9314`); the `source="codex"` recording rules read it.
Run it as a service (launchd/systemd) with `HOST_ENV` matching `.env`
(the env label is required — flag or `HOST_ENV`, no default):

```console
python3 scripts/codex_ledger_exporter.py            # daemon, port 9314
python3 scripts/codex_ledger_exporter.py --once     # one scan to stdout
```

Bind address: on macOS, Docker Desktop reaches the default `127.0.0.1` bind
through `host.docker.internal`. On Linux Docker Engine that name maps to the
Docker bridge gateway (shipped `extra_hosts` entry in `docker-compose.yml`),
which cannot reach a loopback bind — run the exporter with `--bind` set to
the bridge address (commonly `172.17.0.1`), never a LAN interface.

For dashboard history predating the exporter's first scrape,
`scripts/backfill_codex_ledger_history.py` generates hourly-grid OpenMetrics
history from the same ledger for
`promtool tsdb create-blocks-from openmetrics`; its `--help` documents the
splice constraints (`--end` must precede the first live scrape, labels must
match the live series).

Self-telemetry: `codex_ledger_scan_ok`, `codex_ledger_parse_errors`,
`codex_ledger_duplicate_records`, `codex_ledger_corpus_shrunk`,
`codex_ledger_last_scan_timestamp_seconds`. The native OTLP lane stays
scraped; `ai_codex_otlp_capture_ratio` compares the two — near-0 with ledger
traffic means the OTLP lane died, sustained >0.95 means upstream fixed
emission and the exporter can be retired. Both lanes see only this machine's
sessions; Codex web/cloud usage is invisible to both.

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

## Security and privacy

- Keep published ports on loopback. The stack has no Prometheus or collector
  application authentication.
- The Grafana password protects Grafana only, and its secret file seeds first
  database initialization only.
- Never commit `.env`, `.secrets/`, runtime state, backups, metric exports, or
  logs.
- Metrics can carry model, tool, user, account, repository, session, and
  environment metadata before collector filtering. Exported labels retain
  exact model names and bounded Claude `agent.name` attribution — do not place
  people, emails, account IDs, session IDs, secrets, or customer data in
  either field.
- Client logs, traces, prompts, and tool content are outside this metrics-only
  setup.
- Do not expose the stack to another network without authentication, TLS,
  firewall, retention, and privacy design.

See [SECURITY.md](SECURITY.md) for reporting and the supported-security
boundary.

## Validation

`scripts/check.sh` runs every check below plus the Docker-based collector and
Prometheus validations when Docker is available; exit `3` means everything
passed but a tool was missing and its check was skipped (printed). The
individual commands, runnable from a checkout without a `.env` (`HOST_ENV` and
`CC_METRICS_RUNTIME_DIR` ship blank in `.env.example`, so both are supplied
inline — the same form CI uses):

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

## Maintenance

### Pricing updates

`pricing/openai-model-pricing.json` owns Codex rates and
`pricing/xai-model-pricing.json` owns Grok rates, each with source URLs. Verify
rates against the official OpenAI or xAI pages, update the verification and
effective-date fields, then run the commands below (`--provider openai` or
`--provider xai` limits a run to one provider; the default covers both):

```console
python3 scripts/generate_pricing_rules.py --write
python3 scripts/generate_pricing_rules.py --check
python3 -m unittest tests/test_pricing_rules.py tests/test_dashboard_contract.py
```

Do not edit the generated pricing block in the recording rules.

### Stack updates

Image references are explicit — nothing changes until `.env` references change.
Review release notes and image digests, then:

```console
docker compose config --quiet
docker compose pull
docker compose up -d
docker compose ps
```

### Stop and remove

Preserve named volumes:

```console
docker compose down
```

> **Warning:** The next command permanently deletes local Prometheus and
> Grafana named-volume data.

```console
docker compose down --volumes
```

Hook data is separate: uninstall the hook first, then delete only the exact
paths printed by the installer after deciding retention needs.

## Contributing and license

See [CONTRIBUTING.md](CONTRIBUTING.md). Licensed under the MIT
[LICENSE](LICENSE).

Grafana, Prometheus, and the OpenTelemetry Collector are pulled as upstream
container images, pinned by digest in `.env.example`, and remain under their
own licenses.

## Acknowledgements

This project started from Anthropic's
[Claude Code monitoring guide](https://github.com/anthropics/claude-code-monitoring-guide),
written by Kashyap Coimbatore Murali. Thanks for the starting point.
