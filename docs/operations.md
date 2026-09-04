# Operations

Everything here is optional. Setup lives in the [README](../README.md);
failures are diagnosed in [troubleshooting.md](../troubleshooting.md).

## Keep the stack running

`scripts/ensure-stack.sh` runs an idempotent `docker compose up -d`, safe to
repeat from launchd, cron, or a systemd timer, so the stack returns after a
reboot. A lock directory prevents two invocations racing; a concurrent
invocation exits successfully. If forced termination leaves
`runtime/ensure-stack.lock` behind, see
[Hook or installer lock timeout](../troubleshooting.md#hook-or-installer-lock-timeout).

Each tick also runs `scripts/version-gate.sh` (below) and
`scripts/check_profile_telemetry.py`, so no extra scheduling is needed.

## Get told when a lane goes dark

A stopped emitter is invisible: scraping continues, the dashboard keeps
drawing, one lane just reads low. `ClaudeLaneDarkWhileOthersActive` in
`prometheus-rules/ai-unified.yml` fires when Claude takes under 5 % of
trailing-3h tokens while the Codex and Grok lanes move more than 100M, held
`for: 2h` so overnight idle cannot trip it.

The stack runs no Alertmanager. `python3 scripts/alert_notify.py` polls
`localhost:9090/api/v1/alerts` and raises a macOS notification once per firing
alert, staying quiet until it resolves; nothing leaves the machine. Run it from
a `StartInterval` LaunchAgent, not a daemon: `display notification` needs the
GUI session.

### Reload rules without a scrape gap

Prometheus runs without `--web.enable-lifecycle`, so `POST /-/reload` returns
403, and a restart leaves a scrape gap that cannot be backfilled. Validate
rule edits with `promtool`, then SIGHUP:

```console
docker kill -s HUP "$(docker compose ps -q prometheus)"
```

Confirm the container's `StartedAt` is unchanged and
`prometheus_config_last_reload_successful` reads 1.

## Catch telemetry breaking on a tool upgrade

Cross-lane alerting catches a fully dark lane, not partial loss, and partial is
the shape real outages take: two held 2.0 % and 2.2 % capture without ever
reaching zero. Three scripts cover that.

### `scripts/telemetry-canary.sh`

Proves Claude can still export. It runs `claude -p` under `env -i` and
requires two signals: the process reporting `First metrics export: SUCCESS`,
and a matching counter delta at the collector. The log reports intent, not
arrival, and `ps eww` proves nothing, since settings-injected variables never
reach the kernel environ.

With profiles rather than `~/.claude`, set `CLAUDE_CONFIG_DIR` or it exits 2
with `Not logged in`:

```console
CLAUDE_CONFIG_DIR="$HOME/.claude" scripts/telemetry-canary.sh
```

### `scripts/version-gate.sh`

Runs the canary only when `claude --version` or `paseo --version` differs from
`runtime/tool-versions.state`, and always exits 0 so a telemetry fault cannot
wedge the watchdog. `scripts/ensure-stack.sh` calls it.

### `scripts/claude_transcript_exporter.py`

Measures *how much* was lost, from the per-turn JSONL transcript Claude Code
writes regardless of OTEL state. It discovers `~/.claude-profiles/*/projects`
automatically. Serve it on `:9315`, scrape it as job `claude-transcript`, and
`ClaudeTelemetryCaptureLoss` compares the lanes: it fires once
`ai_claude_otlp_capture_ratio` holds under 0.25 for two hours.

Three rules guard the witness itself: `WitnessExporterDown` (dead exporter),
`WitnessExporterStale` (frozen counters behind a healthy `up`), and
`TranscriptCorpusShrunk` (a retention prune read as a counter reset). Each
would otherwise stop the comparison from firing.

## Optional Codex commit metric

Codex exports no repository commit counter. The installer adds `PreToolUse`
and `PostToolUse` Bash hooks:

```console
python3 scripts/install_codex_commit_hook.py \
  --runtime-dir "$(pwd -P)/runtime"
```

Pass the exact absolute path if `.env` uses another runtime directory. Confirm
the printed `runtime_dir` matches `.env`, restart Codex and the collector, then
open `/hooks` in Codex and review the exact commands before trusting them.

What is counted:

- Only commits around a Bash command naming `am`, `cherry-pick`, `commit`,
  `merge`, `pull`, `rebase`, or `revert`. An editor, GUI client, or unseen
  script is not.
- The public event file holds an event name and an HMAC deduplication ID,
  never a repository path or commit SHA.
- Forward-looking and best-effort: no attribution of existing history, no
  backfill of stopped-time events, a 10,000-record cap on the event file, and
  hook errors failing open into a private local error log.

Maintain or remove the installed copy with the same installer, repeating any
custom install arguments:

```console
python3 scripts/install_codex_commit_hook.py --update
python3 scripts/install_codex_commit_hook.py --uninstall
```

Uninstall preserves the installed script and runtime data, so review the
printed paths before deleting either.
