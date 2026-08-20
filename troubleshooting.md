# Troubleshooting cc-metrics

Follow sections in order. Redact secrets and identifying metric labels before
sharing output.

## Compose validation fails

Run:

```console
docker version
docker compose version
docker compose config --quiet
```

Check:

- `.env` exists and every variable has value;
- `GRAFANA_ADMIN_PASSWORD_FILE` points to readable file;
- `CC_METRICS_RUNTIME_DIR` exists and is absolute;
- image references include usable tag and digest;
- chosen host ports are unused.

Do not use `docker compose config` without `--quiet` in shared logs; resolved
output can reveal local paths.

## Container does not become healthy

Inspect state and service logs:

```console
docker compose ps --all
docker compose logs --tail=200 otel-collector
docker compose logs --tail=200 prometheus
docker compose logs --tail=200 grafana
```

Find actual bindings:

```console
docker compose port grafana 3000
docker compose port prometheus 9090
docker compose port otel-collector 4317
docker compose port otel-collector 4318
```

Common causes: stopped Docker engine, port collision, missing bind-mounted file,
invalid configuration, unsupported image architecture, or image pull failure.

## Claude metrics absent

Confirm Claude Code is 2.1.214 or newer:

```console
claude --version
```

Print non-secret telemetry selection from same shell that launches Claude:

```sh
printf '%s\n' \
  "$CLAUDE_CODE_ENABLE_TELEMETRY" \
  "$OTEL_METRICS_EXPORTER" \
  "$OTEL_EXPORTER_OTLP_PROTOCOL" \
  "$OTEL_EXPORTER_OTLP_ENDPOINT"
```

Expected values: `1`, `otlp`, `grpc`, and reachable collector endpoint.

Temporary console check:

```sh
export OTEL_METRICS_EXPORTER=console
export OTEL_METRIC_EXPORT_INTERVAL=10000
```

Launch Claude Code, generate activity, inspect console, then restore
`OTEL_METRICS_EXPORTER=otlp`. Restart client after exporter changes.

Official reference:
[Claude Code monitoring](https://code.claude.com/docs/en/monitoring-usage).

## Codex metrics absent

Codex token and cost panels read the ledger exporter, not this lane — see
"Codex ledger series absent" below. The native OTLP lane feeds only the
`ai_codex_otlp_capture_ratio` cross-check.

Confirm Codex CLI 0.145.0 or newer when expecting GPT-5.6 cache writes:

```console
codex --version
```

Check user config:

```toml
[otel]
environment = "<codex-otel-environment>"

[otel.metrics_exporter."otlp-grpc"]
endpoint = "http://<OTLP_HOST>:<OTLP_GRPC_PORT>"
```

Replace placeholders and restart Codex. Project `.codex/config.toml` cannot
override telemetry routing.

Official references:
[Codex configuration](https://developers.openai.com/codex/config-reference) and
[Codex telemetry](https://developers.openai.com/codex/config-advanced#observability-and-telemetry).

## Codex ledger series absent

`codex_ledger_token_usage_total` comes from the host-side ledger exporter
(job `codex-ledger`, port 9314) — see README "Codex ledger token source".
If codex token and cost panels are empty:

```console
curl -s http://127.0.0.1:9314/metrics | head
```

No response: the exporter is not running — start it with `HOST_ENV`
matching `.env`. Response present but the Prometheus `codex-ledger` target
is `DOWN`: the container cannot reach the host. On Linux Docker Engine,
`host.docker.internal` maps to the Docker bridge gateway (shipped
`extra_hosts` entry), which cannot reach the exporter's default loopback
bind — run the exporter with `--bind` on the bridge address (README "Codex
ledger token source"). On macOS, Docker Desktop reaches the loopback bind
directly.

`codex_ledger_scan_ok 0` means the last scan failed and stale values are
served — check the exporter log. `codex_ledger_corpus_shrunk 1` means
session files were deleted; counter semantics are broken until the
Prometheus history is repaired.

## Grok Build metrics absent

Confirm Grok Build version (1.0.5 verified with this stack):

```console
grok --version
```

Check user config — `otel_enabled` and `otel_metrics_exporter` are both
required, one alone enables nothing; endpoint and protocol route metrics to
this stack:

```toml
[telemetry]
otel_enabled = true
otel_metrics_exporter = "otlp"
otel_endpoint = "http://<OTLP_HOST>:<OTLP_HTTP_PORT>"
otel_protocol = "http/protobuf"
```

Environment variables `GROK_EXTERNAL_OTEL` and `OTEL_*` override the config
file, so a stale `OTEL_EXPORTER_OTLP_ENDPOINT` or `OTEL_EXPORTER_OTLP_PROTOCOL`
in the launching shell redirects Grok. Print them from the same shell:

```sh
printf '%s\n' \
  "$GROK_EXTERNAL_OTEL" \
  "$OTEL_METRICS_EXPORTER" \
  "$OTEL_EXPORTER_OTLP_PROTOCOL" \
  "$OTEL_EXPORTER_OTLP_ENDPOINT"
```

Emission is held closed at startup until the client resolves fleet policy —
bounded at 30 seconds — and the default metric export interval is 60 seconds,
so wait past both before concluding failure. Restart the client after config
changes. The stream is alpha (schema v1); reference the CLI's own user guide,
*Monitoring Usage (External OpenTelemetry)*, under `~/.grok/docs/user-guide/`.

## Collector unreachable

Default same-host destination is `127.0.0.1`, not wildcard bind address.
OTLP/gRPC is not HTTP; `GET /v1/metrics` does not prove ingestion.

Check TCP reachability:

```sh
otlp_host=127.0.0.1
otlp_grpc_port=4317
docker run --rm --network host busybox \
  nc -z -w2 "$otlp_host" "$otlp_grpc_port" && echo reachable
```

Uses the Docker already required by this stack, so it works without installing
Netcat. With `netcat-openbsd` present, `nc -zv "$otlp_host" "$otlp_grpc_port"`
is equivalent. On macOS, `--network host` is unavailable — run the plain `nc`
form there, which ships with the OS.

Set values to chosen `.env` destination and host port. Success proves only TCP
path. VM/container clients need host address reachable from their network. Do
not broaden listener binding without security design.

## Prometheus target down

Open `/targets` at binding returned by:

```console
docker compose port prometheus 9090
```

If `cc-metrics-collector` is not `UP`:

```console
docker compose logs --tail=200 otel-collector prometheus
docker compose restart otel-collector prometheus
```

Then query raw client metrics:

```promql
{__name__=~"claude_code_.*|codex_.*|grok_code_.*"}
```

## Grafana empty

Verify:

1. Prometheus collector target is `UP`.
2. Raw query above returns series.
3. Dashboard `env` matches `.env` `HOST_ENV`.
4. Dashboard `source` includes active client.
5. Time range exceeds exporter interval.
6. Host clock is correct.

## Grafana password file change has no effect

Expected after first initialization. Grafana administrator secret seeds empty
Grafana database only. Replacing file and restarting container does not rotate
stored credential.

If current password works, use signed-in Grafana password-change UI. Update
local secret file to same new value so future empty-volume initialization stays
consistent.

If password is lost, use official
[Grafana CLI reset procedure](https://grafana.com/docs/grafana/latest/administration/cli/#reset-admin-password).
CLI requires new password argument, which can be visible to local process
inspection. Run only on trusted host, avoid literal password in shell history,
then update secret file. Deleting Grafana volume resets more than password and
is not password-rotation procedure.

## Totals differ during concurrent clients

Supported Claude/Codex/Grok delta sums and Claude/Codex histograms are
privacy-filtered, batched, compacted by safe labels, then converted to
cumulative values. Concurrent
producer identities must not appear in Prometheus output.

Check Claude shell pins delta temporality:

```console
printf '%s\n' "$OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE"
```

Expected value is `delta`. Restart Claude after changing it. Codex 0.145.0
hard-codes delta metrics; Grok Build defaults to delta.

Collector rejects unknown metric families and supported Claude/Codex/Grok
metrics when temporality is not delta. If metrics disappear, fix client environment;
do not bypass this guard.

Collector is best-effort observability. OTLP replay without stable event IDs can
duplicate values. OTLP success can precede in-memory batch flush, so abrupt
failure can lose acknowledged values. Collector restart loses cumulative state.
Wall-clock rollback or equal arrival timestamps can cause cumulative conversion
to drop one compacted batch. Correlate anomalies with collector logs, clock
events, and restarts. Do not add user, account, email, session, or producer
labels as workaround.

## Counter changes around collector restart

Delta-to-cumulative processor stores state in memory. Collector restart loses
accumulated state. Stream inactive for `max_stale: 24h` is removed; next delta
starts new cumulative state. Prometheus may treat lower replacement value as
counter reset.

Check:

```console
docker compose ps
docker compose logs --since=24h otel-collector
```

Correlate collector restarts with query window. `max_streams: 10000` also drops
new streams after tracking limit is reached.

## Codex cache-write series absent

`cache_write_input` requires compatible Codex telemetry and request/model
behavior. Current public Codex catalog omits this token type even though Codex
0.145.0 source emits it. Absence does not prove collector failure.

Check:

- Codex is at least 0.145.0;
- raw `codex_ledger_token_usage_total` series exist;
- activity used GPT-5.6 path capable of cache creation;
- collector was running during activity.

Do not synthesize cache-write values from other buckets.

## Cost differs from bill

Expected. Dashboard is not billing system.

Claude cost metric is provider-described approximation. Codex cost is standard
OpenAI API list-price equivalent and Grok cost is xAI API list-price
equivalent, each calculated from current manifest rates.
Differences include:

- range timeseries use price gauge available at each evaluation step;
- instant and range-total panels join current evaluation-time price gauge;
- unknown models omitted;
- unmatched token types omitted even when another type for same model is priced;
- subscription or workspace credits;
- Batch, Flex, or Priority service tiers;
- regional processing uplift;
- qualifying long-context multipliers;
- separately billed tools or containers;
- provider-specific pricing and delayed telemetry.

Use provider billing records for financial decisions.

## Commit count absent

Commit counter is optional and forward-only.

```console
docker compose ps otel-collector
docker compose logs --tail=100 otel-collector
```

Confirm:

- installer printed same runtime directory used by `.env`;
- Codex `/hooks` shows trusted PreToolUse and PostToolUse entries;
- collector was running before commit;
- Bash command named one of `am`, `cherry-pick`, `commit`, `merge`, `pull`,
  `rebase`, `revert` — a commit made any other way is not seen;
- event file has not reached configured record limit.

Collector starts at end after restart. Events written while stopped remain on
disk but are intentionally not converted later.

Hook errors are recorded in private
`codex-commit-hook-errors.log` under selected runtime directory. Redact paths
and error content before sharing.

## Hook installer reports no removal

Uninstaller removes exact owned signatures only. Repeat original custom values:

```console
python3 scripts/install_codex_commit_hook.py \
  --uninstall \
  --hooks-file <hooks-file> \
  --install-dir <install-directory> \
  --runtime-dir <runtime-directory> \
  --state-max-age-seconds <seconds> \
  --max-event-records <count>
```

Repeat each `--legacy-hook-script` used during migration. Uninstall preserves
unrelated hooks, installed script, and runtime data.

## Hook or installer lock timeout

Cooperating hook operations use local lock files. Check no supported process is
still running before removing a stale lock. Do not use hook runtime on network
file system.

For `runtime/ensure-stack.lock`, check for a scheduler entry running
`scripts/ensure-stack.sh` first. Forced termination can bypass cleanup:

```console
ps aux | grep '[e]nsure-stack.sh'
```

Remove only exact stale lock directory after confirming no process owns it.

## Claude login or startup failure

Run:

```console
claude doctor
```

For authentication reset, run `/logout` inside Claude Code, exit, restart, and
authenticate. Do not delete full `~/.claude`; it can contain settings, plugins,
skills, MCP configuration, and session history.

Official reference:
[Claude Code installation troubleshooting](https://code.claude.com/docs/en/troubleshoot-install).

## Report issue safely

Include:

- macOS or Ubuntu version and architecture;
- `docker version` and `docker compose version`;
- redacted `docker compose ps`;
- relevant redacted logs;
- metric names, not raw identity-bearing label values;
- Claude Code, Codex, or Grok Build version.

Remove passwords, tokens, emails, account IDs, session IDs, repository paths,
private hostnames, and raw telemetry payloads.
