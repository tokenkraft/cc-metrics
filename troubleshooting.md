# Troubleshooting cc-metrics

Each entry: symptom (heading) → cause → fix. Redact secrets and identifying metric
labels before sharing output.

## Compose validation fails

**Cause** — invalid `.env` value, unreadable secret file, incomplete image
reference, or taken host port.

**Fix**

```console
docker version
docker compose version
docker compose config --quiet
```

Check `.env` exists with every variable set; `GRAFANA_ADMIN_PASSWORD_FILE`
readable; `CC_METRICS_RUNTIME_DIR` existing and absolute; image references carry
tag and digest; chosen host ports free. Keep `--quiet`; resolved output can reveal
local paths in shared logs.

## Container does not become healthy

**Cause** — stopped Docker engine, port collision, missing bind-mounted file,
invalid configuration, unsupported image architecture, or image pull failure.

**Fix** — inspect state, service logs, actual bindings:

```console
docker compose ps --all
docker compose logs --tail=200 otel-collector
docker compose logs --tail=200 prometheus
docker compose logs --tail=200 grafana
docker compose port grafana 3000
docker compose port prometheus 9090
docker compose port otel-collector 4317
docker compose port otel-collector 4318
```

## Claude metrics absent

**Cause** — telemetry keys missing from profile settings file, or set only in shell.

**Fix** — check configuration first, then runtime; runtime check cannot pass if
configuration is wrong. Claude Code must be 2.1.214 or newer.

```console
claude --version
python3 scripts/check_profile_telemetry.py
scripts/telemetry-canary.sh
```

`check_profile_telemetry.py` reads every profile's settings file and names missing
keys. `telemetry-canary.sh` spends one cheap model call and needs two independent
signals: process reporting `First metrics export: SUCCESS`, and matching counter
delta at collector. Log alone reports intent, not arrival.

Shell environment is **not** authoritative; `ps eww` is not a valid check. Durable
carrier is the `env` block of `~/.claude/settings.json`; settings-file variables
never enter the kernel environ, so healthy and dark processes look identical there,
and a shell exporting nothing is normal. Settings-file value wins. `~/.zshrc`
reaches nothing launchd starts — launchd does not source shell rc files, so
telemetry set only there dies silently, exit code 0, on first daemon restart or
launchd adoption.

Temporary console check — launch Claude Code, generate activity, inspect console,
restore `OTEL_METRICS_EXPORTER=otlp`, restart client after exporter changes.

```sh
export OTEL_METRICS_EXPORTER=console
export OTEL_METRIC_EXPORT_INTERVAL=10000
```

Reference: [Claude Code monitoring](https://code.claude.com/docs/en/monitoring-usage).

## Claude metrics absent for one account only

**Cause** — telemetry is per account: each profile is a separate config directory
with its own settings file, so a profile added later has a dark OTLP lane from
birth — exit code 0, transcripts normal, no warning. The transcript witness still
sees that account: it discovers `~/.claude-profiles/*/projects` automatically and
reads from disk regardless of OTEL state, so the dark profile lands in the
capture-ratio denominator only and `ai_claude_otlp_capture_ratio` degrades.
`ClaudeTelemetryCaptureLoss` fires once the shortfall holds the whole-host ratio
under 0.25 for two hours; a small account's gap can stay above that threshold, and
the ratio names loss without naming the profile.

**Fix**

```console
python3 scripts/check_profile_telemetry.py
```

Direct per-profile diagnosis: reports every profile found, names missing keys, flags
profiles pointing at a different endpoint — one exporting elsewhere still exports,
so nothing looks dead, but its tokens never arrive here. `ensure-stack.sh` runs it
every tick. Copy the `env` block into the reported profile's `settings.json`;
configuration is read once at startup, so running sessions stay dark until they
exit.

## Codex metrics absent

**Cause** — Codex token and cost panels read the ledger exporter, not this lane (see
"Codex ledger series absent"); native OTLP lane feeds only the
`ai_codex_otlp_capture_ratio` cross-check. A thin or partly empty OTLP lane is
normal even with a correct `[otel]` block: Codex skips emission for aborted or
interrupted turns (README "Codex ledger token source"). A fully dark lane points at
missing or misrouted `[otel]` user config, or `analytics.enabled` disabled — the
whole exporter is gated on it.

**Fix** — Codex CLI must be 0.145.0 or newer for GPT-5.6 cache writes:

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

Replace placeholders, restart Codex. Project `.codex/config.toml` cannot override
telemetry routing. References:
[Codex configuration](https://developers.openai.com/codex/config-reference),
[Codex telemetry](https://developers.openai.com/codex/config-advanced#observability-and-telemetry).

## Codex ledger series absent

**Cause** — `codex_ledger_token_usage_total` comes from the host-side ledger exporter
(job `codex-ledger`, port 9314; README "Codex ledger token source"), which is down,
unreachable from the container, or serving stale values.

**Fix**

```console
curl -s http://127.0.0.1:9314/metrics | head
```

- No response — exporter not running. Start it with `HOST_ENV` matching `.env`.
- Response present, Prometheus `codex-ledger` target `DOWN` — container cannot reach
  the host. On Linux Docker Engine, `host.docker.internal` maps to the Docker bridge
  gateway (shipped `extra_hosts` entry), which cannot reach the exporter's default
  loopback bind; run the exporter with `--bind` on the bridge address (README "Codex
  ledger token source"). On macOS, Docker Desktop reaches the loopback bind directly.
- `codex_ledger_scan_ok 0` — last scan failed, stale values served. Check exporter log.
- `codex_ledger_corpus_shrunk 1` — a per-series total fell below its persisted
  high-water mark: session files deleted, or a parser change lowered a total. Counter
  semantics are broken until Prometheus history is repaired. Flag survives restarts;
  after repair, stop the exporter and delete the state file — the `--state-file` path
  if given, else `codex-ledger-state.json` in `CC_METRICS_RUNTIME_DIR`, or, if that
  variable is unset for the daemon, in
  `~/Library/Application Support/cc-metrics/runtime/` (macOS) or
  `$XDG_STATE_HOME/cc-metrics/runtime/` (Linux, default
  `~/.local/state/cc-metrics/runtime/`).

## Grok Build metrics absent

**Cause** — incomplete `[telemetry]` config (`otel_enabled` and
`otel_metrics_exporter` both required, one alone enables nothing), environment
override, or checking before startup gate and export interval elapse.

**Fix** — 1.0.5 is verified with this stack; set config, restart client:

```console
grok --version
```

```toml
[telemetry]
otel_enabled = true
otel_metrics_exporter = "otlp"
otel_endpoint = "http://<OTLP_HOST>:<OTLP_HTTP_PORT>"
otel_protocol = "http/protobuf"
```

`GROK_EXTERNAL_OTEL` and `OTEL_*` override the config file, so a stale
`OTEL_EXPORTER_OTLP_ENDPOINT` or `OTEL_EXPORTER_OTLP_PROTOCOL` in the launching
shell redirects Grok. Print them from the same shell:

```sh
printf '%s\n' \
  "$GROK_EXTERNAL_OTEL" \
  "$OTEL_METRICS_EXPORTER" \
  "$OTEL_EXPORTER_OTLP_PROTOCOL" \
  "$OTEL_EXPORTER_OTLP_ENDPOINT"
```

Emission is held closed at startup until the client resolves fleet policy — bounded
at 30 seconds — and default export interval is 60 seconds; wait past both before
concluding failure. Stream is alpha (schema v1); reference the CLI's own user guide,
*Monitoring Usage (External OpenTelemetry)*, under `~/.grok/docs/user-guide/`.

## Collector unreachable

**Cause** — client pointed at wrong destination, or no TCP path to it. Default
same-host destination is `127.0.0.1`, not wildcard bind address. OTLP/gRPC is not
HTTP; `GET /v1/metrics` does not prove ingestion.

**Fix** — test TCP reachability, values set to chosen `.env` destination and host
port:

```sh
otlp_host=127.0.0.1
otlp_grpc_port=4317
docker run --rm --network host busybox \
  nc -z -w2 "$otlp_host" "$otlp_grpc_port" && echo reachable
```

Uses the Docker this stack already requires, so no Netcat install is needed. With
`netcat-openbsd` present, `nc -zv "$otlp_host" "$otlp_grpc_port"` is equivalent. On
macOS, `--network host` is unavailable — use the plain `nc` form, which ships with
the OS. Success proves only TCP path; VM and container clients need a host address
reachable from their network. Do not broaden listener binding without security
design.

## Prometheus target down

**Cause** — collector not scrapeable; or, once `UP`, no raw client series arriving.

**Fix** — open `/targets` at the binding returned by:

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

**Cause** — series absent upstream, or dashboard variables and time range not
matching what arrived.

**Fix** — verify in order:

1. Prometheus collector target is `UP`.
2. Raw query above returns series.
3. Dashboard `env` matches `.env` `HOST_ENV`.
4. Dashboard `source` includes active client.
5. Time range exceeds exporter interval.
6. Host clock is correct.

## Grafana password file change has no effect

**Cause** — expected after first initialization. Administrator secret seeds empty
Grafana database only; replacing file and restarting does not rotate stored
credential.

**Fix** — if current password works, change it in the signed-in Grafana
password-change UI, then set the local secret file to the same value so future
empty-volume initialization stays consistent.

If the password is lost, use the official
[Grafana CLI reset procedure](https://grafana.com/docs/grafana/latest/administration/cli/#reset-admin-password).
Its new-password argument can be visible to local process inspection: run only on a
trusted host, keep the literal password out of shell history, then update the secret
file. Deleting the Grafana volume resets more than the password and is not a
rotation procedure.

## Totals differ during concurrent clients

**Cause** — supported Claude/Codex/Grok delta sums and Claude/Codex histograms are
privacy-filtered, batched, compacted by safe labels, then converted to cumulative.
Concurrent producer identities must not appear in Prometheus output.

**Fix** — check the Claude shell pins delta temporality; expected value `delta`,
restart Claude after changing it:

```console
printf '%s\n' "$OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE"
```

Codex 0.145.0 hard-codes delta metrics; Grok Build defaults to delta. Collector
rejects unknown metric families, and supported Claude/Codex/Grok metrics whose
temporality is not delta. If metrics disappear, fix client environment; do not
bypass this guard.

Collector is best-effort observability: OTLP replay without stable event IDs can
duplicate values; OTLP success can precede in-memory batch flush, so abrupt failure
can lose acknowledged values; restart loses cumulative state; wall-clock rollback or
equal arrival timestamps can make cumulative conversion drop one compacted batch.
Correlate anomalies with collector logs, clock events, restarts. Do not add user,
account, email, session, or producer labels as workaround.

## Counter changes around collector restart

**Cause** — delta-to-cumulative processor stores state in memory. Restart loses it;
stream inactive for `max_stale: 24h` is removed and its next delta starts new
cumulative state, which Prometheus may treat as counter reset.

**Fix** — correlate collector restarts with the query window:

```console
docker compose ps
docker compose logs --since=24h otel-collector
```

`max_streams: 10000` also drops new streams once tracking limit is reached.

## Codex cache-write series absent

**Cause** — `cache_write_input` requires compatible Codex telemetry and
request/model behavior. Current public Codex catalog omits this token type even
though Codex 0.145.0 source emits it; absence does not prove collector failure.

**Fix** — check Codex is at least 0.145.0; raw `codex_ledger_token_usage_total`
series exist; activity used GPT-5.6 path capable of cache creation; collector was
running during activity. Do not synthesize cache-write values from other buckets.

## Cost differs from bill

**Cause** — expected; dashboard is not a billing system. Claude cost is a
provider-described approximation, Codex cost the standard OpenAI API list-price
equivalent, Grok cost the xAI API list-price equivalent, each from current manifest
rates. Differences include:

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

**Fix** — use provider billing records for financial decisions.

## Commit count absent

**Cause** — commit counter is optional and forward-only. After restart the collector
starts at the end; events written while it was stopped stay on disk and are
intentionally not converted later.

**Fix**

```console
docker compose ps otel-collector
docker compose logs --tail=100 otel-collector
```

Confirm installer printed the same runtime directory `.env` uses; Codex `/hooks`
shows trusted PreToolUse and PostToolUse entries; collector ran before the commit;
Bash command named one of `am`, `cherry-pick`, `commit`, `merge`, `pull`, `rebase`,
`revert` — a commit made any other way is not seen; event file has not reached the
configured record limit.

Hook errors are recorded in private `codex-commit-hook-errors.log` under selected
runtime directory. Redact paths and error content before sharing.

## Hook installer reports no removal

**Cause** — uninstaller removes exact owned signatures only, so original custom
values must be repeated.

**Fix**

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

**Cause** — stale local lock file from forced termination that bypassed cleanup.
Cooperating hook operations use local lock files; do not use hook runtime on a
network file system.

**Fix** — confirm no supported process still holds the lock. For
`runtime/ensure-stack.lock`, check first for a scheduler entry running
`scripts/ensure-stack.sh`:

```console
ps aux | grep '[e]nsure-stack.sh'
```

Then remove only the exact stale lock directory, once no process owns it.

## Claude login or startup failure

**Cause** — broken install, stale authentication state, provider-side credential
expiry, or a blocked network path.

**Fix**

```console
claude doctor
```

To reset authentication, run `/logout` inside Claude Code, exit, restart,
authenticate. Do not delete the full `~/.claude`; it can hold settings, plugins,
skills, MCP configuration, and session history. Reference:
[Claude Code installation troubleshooting](https://code.claude.com/docs/en/troubleshoot-install).

## Report issue safely

Include: macOS or Ubuntu version and architecture; `docker version` and
`docker compose version`; redacted `docker compose ps`; relevant redacted logs;
metric names, not raw identity-bearing label values; Claude Code, Codex, or Grok
Build version.

Remove passwords, tokens, emails, account IDs, session IDs, repository paths,
private hostnames, and raw telemetry payloads.
