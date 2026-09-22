---
updated: "2026-09-23"
rev: 9
---

# Repair Codex ledger history

Keep `codex-ledger-guarded` unscraped until repaired blocks are imported and the
exporter has completed a healthy scan. This order prevents an old high-water
value from appearing after a lower repaired sample, then resetting when state
is rebased.

Use this cutover when a scrape-job upgrade, ledger labels, replay attribution,
or profile discovery changes existing series. First apply the repair decision in
[Counter regressions and repair](codex-ledger.md#counter-regressions-and-repair).

For an in-place upgrade, read this procedure before changing the checkout. Run
step 1 against the current stack, then disable automatic restarts before
updating files. Keep the prior release separately for rollback. Only step 7 may
activate the new Prometheus configuration. Activating it earlier hides old
history; deleting exporter state instead creates false counter increases.

## Preconditions

- Run from the repository root with the deployment's `.env` loaded by Compose.
- Identify how to disable and restore the `ensure-stack` scheduler and the host
  exporter's service-manager restart policy, such as launchd `KeepAlive`.
- Keep a complete prior release tree available for rollback.
- Keep the current Prometheus configuration and exporter state available for
  rollback.
- Require zero existing `job="codex-ledger-guarded"` samples in the planned
  backfill interval. Existing samples require a separate, disjoint-range plan.
- Keep the oldest generated sample inside `PROMETHEUS_RETENTION`; Prometheus
  removes older backfilled data during compaction.
- Require `PROMETHEUS_IMAGE` in `.env` to use an immutable digest. Recheck its
  runtime UID before using the import command.

## Ordered cutover

1. Choose an exclusive UTC cutoff aligned to `--step-seconds` and create a
   unique run directory. Never reuse it: promtool preserves existing output
   blocks. The cutoff must already be more than three hours old; Prometheus
   warns that backfilling its mutable last three hours is unsafe. Prove the new
   job has no samples through the cutoff before stopping the old exporter.

   ```sh
   set -eu
   cutoff='<RFC3339-exclusive-cutoff>'
   prometheus_url='http://127.0.0.1:9090'
   mkdir -p runtime/codex-ledger-repair
   run_id="$(date -u +%Y%m%dT%H%M%SZ)"
   repair_root="runtime/codex-ledger-repair/$run_id"
   test ! -e "$repair_root"
   mkdir "$repair_root"
   printf '%s\n' "$repair_root" > runtime/codex-ledger-repair/current-run
   printf '%s\n' "$cutoff" > "$repair_root/cutoff.txt"
   cutoff_epoch="$(python3 -c 'from datetime import datetime; import sys; print(int(datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00")).timestamp()))' "$cutoff")"
   now_epoch="$(date -u +%s)"
   test "$((now_epoch - cutoff_epoch))" -gt 10800
   guarded_series="$(
     curl -fsS --get "$prometheus_url/api/v1/series" \
       --data-urlencode 'match[]={job="codex-ledger-guarded"}' \
       --data-urlencode 'start=1970-01-01T00:00:00Z' \
       --data-urlencode "end=$cutoff" \
       | jq -er 'if .status == "success" then (.data | length) else error(.error) end'
   )"
   test "$guarded_series" -eq 0
   ```

   Disable the `ensure-stack` scheduler and unload or disable the exporter
   service's restart policy. Then stop the exporter. Keep both restart paths
   disabled until step 7 or rollback completes, and verify the endpoint is
   unavailable before continuing:

   ```sh
   set -eu
   if curl -fsS http://127.0.0.1:9314/metrics >/dev/null 2>&1; then
     echo 'exporter is still running' >&2
     exit 1
   fi
   ```

2. Resolve the exact Compose volume, then back up exporter state, Prometheus
   configuration and rules, and the Grafana dashboard. Record the volume now
   because `compose ps -q` returns running containers by default.

   ```sh
   set -eu
   prometheus_container="$(docker compose ps -q prometheus)"
   prometheus_volume="$(docker inspect "$prometheus_container" --format '{{range .Mounts}}{{if eq .Destination "/prometheus"}}{{.Name}}{{end}}{{end}}')"
   test -n "$prometheus_volume"
   state_file='<absolute-state-file>'
   prior_release='<absolute-path-to-prior-release>'
   deployed_exporter='<absolute-service-path>/codex_ledger_exporter.py'
   deployed_parser='<absolute-service-path>/codex_ledger.py'
   test -f "$state_file"
   test -f "$prior_release/prometheus.yml"
   test -f "$prior_release/prometheus-rules/ai-unified.yml"
   test -f "$prior_release/grafana/dashboards/ai-monitor.json"
   test -f "$prior_release/scripts/codex_ledger_exporter.py"
   test -f "$prior_release/scripts/codex_ledger.py"
   test -f "$deployed_exporter"
   test -f "$deployed_parser"
   repair_root="$(cat runtime/codex-ledger-repair/current-run)"
   test -d "$repair_root"
   printf '%s\n' "$prometheus_volume" > "$repair_root/prometheus-volume.txt"
   printf '%s\n' "$state_file" > "$repair_root/state-file-path.txt"
   printf '{}\n' > "$repair_root/series-remap.json"
   cp -p "$prior_release/prometheus.yml" "$repair_root/prometheus.yml.before"
   cp -p "$prior_release/prometheus-rules/ai-unified.yml" "$repair_root/ai-unified.yml.before"
   cp -p "$prior_release/grafana/dashboards/ai-monitor.json" "$repair_root/ai-monitor.json.before"
   cp "$state_file" "$repair_root/codex-ledger-state.before.json"
   cp -p "$prior_release/scripts/codex_ledger_exporter.py" "$repair_root/codex_ledger_exporter.py.before"
   cp -p "$prior_release/scripts/codex_ledger.py" "$repair_root/codex_ledger.py.before"
   printf '%s\n' "$deployed_exporter" > "$repair_root/deployed-exporter-path.txt"
   printf '%s\n' "$deployed_parser" > "$repair_root/deployed-parser-path.txt"
   ```

   `series-remap.json` defaults to exact identity. For each intentional model or
   effort relabel, add one reviewed mapping from the old state key to the new
   key, for example `{"old-model\thigh\tinput":"new-model\thigh\tinput"}`.
   Never change token type. The verifier rejects unknown sources, token-type
   changes, and any target whose candidate total cannot preserve mapped prior
   totals.

3. Stop or pause every Codex client that can write under the main or profile
   homes. Generate derived and raw OpenMetrics files ending at the same cutoff
   from one parsed in-memory snapshot:

   ```sh
   set -eu
   repair_root="$(cat runtime/codex-ledger-repair/current-run)"
   test -d "$repair_root"
   cutoff="$(cat "$repair_root/cutoff.txt")"
   env_label='<HOST_ENV>'
   python3 scripts/backfill_codex_ledger_history.py --end "$cutoff" --step-seconds 60 --env-label "$env_label" --out "$repair_root/derived.openmetrics" --raw-out "$repair_root/raw.openmetrics"
   test -s "$repair_root/derived.openmetrics"
   test -s "$repair_root/raw.openmetrics"
   ```

   The generator scans the whole corpus for integrity, including records after
   the cutoff, and exits before writing either file when any record is malformed
   or unreadable. A post-cutoff partial tail therefore also aborts generation;
   wait for the writer to finish or repair the file, then retry. Keep every
   Codex client paused through step 7 so the candidate snapshot cannot change.
   Minute samples preserve short-window queries. `--start` limits emitted
   timestamps while retaining earlier events in cumulative totals. Use
   disjoint time ranges when combining resolutions. A cutoff older than three
   hours necessarily leaves a live-data gap; usage in that gap appears as one
   increase at the first live scrape, so short-window history inside the gap
   cannot be reconstructed.

4. Convert each file and attach the live job label through promtool:

   ```sh
   set -eu
   repair_root="$(cat runtime/codex-ledger-repair/current-run)"
   test -d "$repair_root"
   prometheus_image="$(sed -n 's/^PROMETHEUS_IMAGE=//p' .env | tail -1)"
   case "$prometheus_image" in *@sha256:*) ;; *) exit 1;; esac
   prometheus_uid="$(docker run --rm --entrypoint id "$prometheus_image" -u)"
   prometheus_gid="$(docker run --rm --entrypoint id "$prometheus_image" -g)"
   test -n "$prometheus_uid"
   test -n "$prometheus_gid"
   case "$prometheus_uid:$prometheus_gid" in *[!0-9:]*) exit 1;; esac
   printf '%s\n' "$prometheus_uid" > "$repair_root/prometheus-uid.txt"
   printf '%s\n' "$prometheus_gid" > "$repair_root/prometheus-gid.txt"
   docker run --rm --user "$(id -u):$(id -g)" --entrypoint promtool \
     --volume "$(pwd -P):/workspace" --workdir /workspace \
     "$prometheus_image" tsdb create-blocks-from openmetrics \
     --label=job=codex-ledger-guarded \
     "$repair_root/derived.openmetrics" \
     "$repair_root/derived-blocks"
   docker run --rm --user "$(id -u):$(id -g)" --entrypoint promtool \
     --volume "$(pwd -P):/workspace" --workdir /workspace \
     "$prometheus_image" tsdb create-blocks-from openmetrics \
     --label=job=codex-ledger-guarded \
     "$repair_root/raw.openmetrics" \
     "$repair_root/raw-blocks"
   find "$repair_root/derived-blocks" "$repair_root/raw-blocks" \
     -mindepth 1 -maxdepth 1 -type d -print \
     | sed "s#^$repair_root/##" | sort > "$repair_root/block-paths.txt"
   test -s "$repair_root/block-paths.txt"
   ```

   Input samples omit `job`, so `--label` is the only job authority. Prometheus
   3.13.1's block importer sets or overwrites that label and does not create
   `exported_job`. Current queries also reject imported series carrying a
   relabelled `exported_job="codex-ledger-guarded"` identity. See
   [Prometheus backfill guidance](https://prometheus.io/docs/prometheus/3.13/storage/#backfilling-from-openmetrics-format).

5. Stop Prometheus and take a fresh volume snapshot immediately before import.
   Then copy every generated ULID directory directly into `/prometheus`. The
   first loop proves every destination absent before the second loop copies
   anything. The copy runs as the pinned image's UID/GID and verifies each
   block's metadata and ownership. Stopping Prometheus creates a scrape gap for
   every telemetry lane; only Codex ledger history is backfilled, so keep this
   outage short.

   ```sh
   set -eu
   repair_root="$(cat runtime/codex-ledger-repair/current-run)"
   test -d "$repair_root"
   prometheus_volume="$(cat "$repair_root/prometheus-volume.txt")"
   prometheus_uid="$(cat "$repair_root/prometheus-uid.txt")"
   prometheus_gid="$(cat "$repair_root/prometheus-gid.txt")"
   prometheus_image="$(sed -n 's/^PROMETHEUS_IMAGE=//p' .env | tail -1)"
   case "$prometheus_image" in *@sha256:*) ;; *) exit 1;; esac
   docker compose stop prometheus
   docker run --rm --volume "$prometheus_volume:/prometheus:ro" \
     --entrypoint tar "$prometheus_image" -C /prometheus -czf - . \
     > "$repair_root/prometheus-data.tgz"
   test -s "$repair_root/prometheus-data.tgz"
   docker run --rm \
     --volume "$(pwd -P)/$repair_root:/repair:ro" \
     --entrypoint tar "$prometheus_image" \
     -tzf /repair/prometheus-data.tgz >/dev/null
   docker compose run --rm --no-deps \
     --user "$prometheus_uid:$prometheus_gid" --entrypoint sh \
     --env PROMETHEUS_UID="$prometheus_uid" \
     --env PROMETHEUS_GID="$prometheus_gid" \
     --volume "$(pwd -P)/$repair_root:/repair:ro" \
     prometheus -c '
       set -eu
       while IFS= read -r rel; do
         block=${rel##*/}
         case "$block" in ""|*[!0-9A-HJKMNP-TV-Z]*) exit 1;; esac
         test ! -e "/prometheus/$block"
       done < /repair/block-paths.txt
       while IFS= read -r rel; do
         block=${rel##*/}
         cp -R "/repair/$rel" "/prometheus/$block"
         test -f "/prometheus/$block/meta.json"
         test "$(stat -c "%u:%g" "/prometheus/$block/meta.json")" = \
           "$PROMETHEUS_UID:$PROMETHEUS_GID"
       done < /repair/block-paths.txt
     '
   ```

6. Keep Prometheus stopped, which keeps the guarded scrape disabled. Install
   the new exporter and parser at the service paths recorded in step 2:

   ```sh
   set -eu
   repair_root="$(cat runtime/codex-ledger-repair/current-run)"
   deployed_exporter="$(cat "$repair_root/deployed-exporter-path.txt")"
   deployed_parser="$(cat "$repair_root/deployed-parser-path.txt")"
   cmp -s scripts/codex_ledger_exporter.py "$deployed_exporter" || cp -p scripts/codex_ledger_exporter.py "$deployed_exporter"
   cmp -s scripts/codex_ledger.py "$deployed_parser" || cp -p scripts/codex_ledger.py "$deployed_parser"
   ```

   Start the new exporter with its existing state, verify its startup
   fingerprint as described in [Verify the deployed code](codex-ledger.md#verify-the-deployed-code), then save and inspect its direct
   metrics. Require zero parse errors and `scan_ok` equal to 0 or 1:

   ```sh
   set -eu
   repair_root="$(cat runtime/codex-ledger-repair/current-run)"
   test -d "$repair_root"
   state_file="$(cat "$repair_root/state-file-path.txt")"
   test -f "$state_file"
   ledger_metrics="$(curl -fsS http://127.0.0.1:9314/metrics)"
   scan_ok="$(printf '%s\n' "$ledger_metrics" | awk '$1 == "codex_ledger_scan_ok" {print $2}')"
   parse_errors="$(printf '%s\n' "$ledger_metrics" | awk '$1 == "codex_ledger_parse_errors" {print $2}')"
   corpus_shrunk="$(printf '%s\n' "$ledger_metrics" | awk '$1 == "codex_ledger_corpus_shrunk" {print $2}')"
   case "$scan_ok" in 0|1) ;; *) exit 1;; esac
   test "$parse_errors" -eq 0
   printf '%s\n' "$ledger_metrics" > "$repair_root/pre-rebase.metrics"
   python3 scripts/verify_codex_ledger_repair.py \
     --prior-state "$repair_root/codex-ledger-state.before.json" \
     --candidate-metrics "$repair_root/pre-rebase.metrics" \
     --series-remap "$repair_root/series-remap.json" \
     --report "$repair_root/pre-rebase-verification.json"
   case "$scan_ok:$corpus_shrunk" in
     1:*) : > "$repair_root/rebase-not-required" ;;
     0:1) : > "$repair_root/rebase-required" ;;
     *) exit 1 ;;
   esac
   printf 'scan_ok=%s parse_errors=%s corpus_shrunk=%s\n' \
     "$scan_ok" "$parse_errors" "$corpus_shrunk"
   ```

   A healthy scan creates `rebase-not-required`; the next blocks skip state
   mutation. A rejected regression creates `rebase-required` only when
   `pre-rebase-verification.json` passes. The verifier compares each
   model/effort/token series with zero-token tolerance after applying only the
   explicit mappings in `series-remap.json`.

   Only for `rebase-required`, run the deployment-specific exporter stop
   command, then run this block. It proves the endpoint unavailable before
   moving state. On the healthy path it exits successfully without mutation:

   ```sh
   set -eu
   repair_root="$(cat runtime/codex-ledger-repair/current-run)"
   if test -f "$repair_root/rebase-not-required"; then
     echo 'healthy scan: preserving exporter state'
   else
     test -f "$repair_root/rebase-required"
     state_file="$(cat "$repair_root/state-file-path.txt")"
     ledger_metrics="$(cat "$repair_root/pre-rebase.metrics")"
     test "$(printf '%s\n' "$ledger_metrics" | awk '$1 == "codex_ledger_scan_ok" {print $2}')" -eq 0
     test "$(printf '%s\n' "$ledger_metrics" | awk '$1 == "codex_ledger_parse_errors" {print $2}')" -eq 0
     test "$(printf '%s\n' "$ledger_metrics" | awk '$1 == "codex_ledger_corpus_shrunk" {print $2}')" -eq 1
     if curl -fsS http://127.0.0.1:9314/metrics >/dev/null 2>&1; then
       echo 'exporter is still running' >&2
       exit 1
     fi
     mv "$state_file" "$repair_root/codex-ledger-state.rebased-away.json"
   fi
   ```

   Only after a rebase, restart the exporter. This block enforces the
   post-rebase gate and skips cleanly on the healthy path:

   ```sh
   set -eu
   repair_root="$(cat runtime/codex-ledger-repair/current-run)"
   if test -f "$repair_root/rebase-required"; then
     ledger_metrics="$(curl -fsS http://127.0.0.1:9314/metrics)"
     scan_ok="$(printf '%s\n' "$ledger_metrics" | awk '$1 == "codex_ledger_scan_ok" {print $2}')"
     parse_errors="$(printf '%s\n' "$ledger_metrics" | awk '$1 == "codex_ledger_parse_errors" {print $2}')"
     corpus_shrunk="$(printf '%s\n' "$ledger_metrics" | awk '$1 == "codex_ledger_corpus_shrunk" {print $2}')"
     test "$scan_ok" -eq 1
     test "$parse_errors" -eq 0
     test "$corpus_shrunk" -eq 0
   fi
   ```

   Do not rebase for parse, I/O, or configuration failures. Any failed check
   stops this procedure; stop the exporter before running rollback.

7. Start Prometheus with the shipped configuration; this activates
   `codex-ledger-guarded`. Poll until scrapes are queryable, then capture cutoff
   and live vectors. This gate requires every cutoff series to exist live at an
   equal or greater value. It also writes per-series deltas for exact comparison
   with the frozen candidate snapshot:

   ```sh
   set -eu
   repair_root="$(cat runtime/codex-ledger-repair/current-run)"
   cutoff="$(cat "$repair_root/cutoff.txt")"
   prometheus_url='http://127.0.0.1:9090'
   docker compose start prometheus
   attempt=0
   until curl -fsS "$prometheus_url/-/ready" >/dev/null; do
     attempt=$((attempt + 1))
     test "$attempt" -lt 24
     sleep 5
   done
   raw_query='codex_ledger_token_usage_total{job="codex-ledger-guarded",exported_job!="codex-ledger-guarded"}'
   derived_query='ai_token_usage_tokens_total{source="codex",job="codex-ledger-guarded",exported_job!="codex-ledger-guarded"}'
   for series_kind in raw derived; do
     case "$series_kind" in
       raw) query=$raw_query ;;
       derived) query=$derived_query ;;
     esac
     cutoff_response="$repair_root/$series_kind-cutoff-response.json"
     live_response="$repair_root/$series_kind-live-response.json"
     curl -fsS --get "$prometheus_url/api/v1/query" \
       --data-urlencode "query=$query" --data-urlencode "time=$cutoff" \
       > "$cutoff_response"
     jq -e '.status == "success" and (.data.result | length > 0)' \
       "$cutoff_response" >/dev/null
     attempt=0
     until curl -fsS --get "$prometheus_url/api/v1/query" \
         --data-urlencode "query=$query" > "$live_response" \
         && jq -e '.status == "success" and (.data.result | length > 0)' \
           "$live_response" >/dev/null; do
       attempt=$((attempt + 1))
       test "$attempt" -lt 24
       sleep 5
     done
     jq '.data.result' "$cutoff_response" \
       > "$repair_root/$series_kind-cutoff-vector.json"
     jq '.data.result' "$live_response" \
       > "$repair_root/$series_kind-live-vector.json"
     jq -e -s '
       def keyed:
         reduce .[] as $sample ({};
           .[($sample.metric | to_entries | sort_by(.key) | tojson)] =
             ($sample.value[1] | tonumber));
       (.[0] | keyed) as $cutoff
       | (.[1] | keyed) as $live
       | ($cutoff | length) > 0
         and all($cutoff | keys[];
           $live[.] != null and $live[.] >= $cutoff[.])
     ' "$repair_root/$series_kind-cutoff-vector.json" \
       "$repair_root/$series_kind-live-vector.json" >/dev/null
     jq -S -s '
       def keyed:
         reduce .[] as $sample ({};
           .[($sample.metric | to_entries | sort_by(.key) | tojson)] =
             ($sample.value[1] | tonumber));
       (.[0] | keyed) as $cutoff
       | (.[1] | keyed) as $live
       | (($cutoff | keys) + ($live | keys) | unique) as $keys
       | [$keys[] as $key |
           {labels: ($key | fromjson | from_entries),
            cutoff: ($cutoff[$key] // 0),
            live: ($live[$key] // 0),
            delta: (($live[$key] // 0) - ($cutoff[$key] // 0))}]
     ' "$repair_root/$series_kind-cutoff-vector.json" \
       "$repair_root/$series_kind-live-vector.json" \
       > "$repair_root/$series_kind-post-cutoff-deltas.json"
   done
   ```

   Reject an imported label collision and prove retention kept the oldest raw
   sample:

   ```sh
   set -eu
   repair_root="$(cat runtime/codex-ledger-repair/current-run)"
   cutoff="$(cat "$repair_root/cutoff.txt")"
   prometheus_url='http://127.0.0.1:9090'
   bad_identity_query='{__name__=~"codex_ledger_token_usage_total|ai_token_usage_tokens_total",job="codex-ledger-guarded",exported_job="codex-ledger-guarded"}'
   curl -fsS --get "$prometheus_url/api/v1/query" \
     --data-urlencode "query=$bad_identity_query" \
     --data-urlencode "time=$cutoff" \
     | jq -e '.status == "success" and (.data.result | length == 0)' >/dev/null
   oldest_sample="$(awk '!/^#/ && NF {print $NF; exit}' "$repair_root/raw.openmetrics")"
   test -n "$oldest_sample"
   raw_query='codex_ledger_token_usage_total{job="codex-ledger-guarded",exported_job!="codex-ledger-guarded"}'
   curl -fsS --get "$prometheus_url/api/v1/query" \
     --data-urlencode "query=$raw_query" \
     --data-urlencode "time=$oldest_sample" \
     | jq -e '.status == "success" and (.data.result | length > 0)' >/dev/null
   python3 scripts/verify_codex_ledger_repair.py \
     --prior-state "$repair_root/codex-ledger-state.before.json" \
     --candidate-metrics "$repair_root/pre-rebase.metrics" \
     --raw-openmetrics "$repair_root/raw.openmetrics" \
     --prom-deltas "$repair_root/raw-post-cutoff-deltas.json" \
     --series-remap "$repair_root/series-remap.json" \
     --report "$repair_root/final-verification.json"
   ```

   `final-verification.json` proves each model/effort/token raw post-cutoff
   Prometheus delta equals the frozen candidate snapshot minus its imported
   cutoff total, with zero-token tolerance. Check derived deltas and dashboard token and cost panels across
   the boundary. Restart Codex clients, restore the exporter restart policy,
   and re-enable the `ensure-stack` scheduler only after those checks pass.
   Roll back on any command failure, missing or negative series, unmatched
   delta, absent oldest sample, or panel discontinuity.

## Rollback

On any failed check, stop Prometheus and the exporter. Restore the full volume
snapshot taken immediately before import. This removes original imported ULIDs
and any later compacted blocks that contain their samples. The command first
moves current volume contents into a recoverable, non-ULID directory on the
same volume, then extracts the verified snapshot.

```sh
set -eu
repair_root="$(cat runtime/codex-ledger-repair/current-run)"
test -d "$repair_root"
run_id=${repair_root##*/}
state_file="$(cat "$repair_root/state-file-path.txt")"
prometheus_uid="$(cat "$repair_root/prometheus-uid.txt")"
prometheus_gid="$(cat "$repair_root/prometheus-gid.txt")"
deployed_exporter="$(cat "$repair_root/deployed-exporter-path.txt")"
deployed_parser="$(cat "$repair_root/deployed-parser-path.txt")"
docker compose stop prometheus
if python3 -c 'from urllib.request import urlopen; urlopen("http://127.0.0.1:9314/metrics", timeout=2)' >/dev/null 2>&1; then
  echo 'exporter is still running' >&2
  exit 1
fi
docker compose run --rm --no-deps \
  --user "$prometheus_uid:$prometheus_gid" --entrypoint sh \
  --env REPAIR_RUN_ID="$run_id" \
  --volume "$(pwd -P)/$repair_root:/repair:ro" \
  prometheus -c '
    set -eu
    tar -tzf /repair/prometheus-data.tgz >/dev/null
    quarantine="/prometheus/.rollback-$REPAIR_RUN_ID"
    test ! -e "$quarantine"
    mkdir "$quarantine"
    find /prometheus -mindepth 1 -maxdepth 1 ! -path "$quarantine" -print \
      | while IFS= read -r entry; do mv "$entry" "$quarantine/"; done
    tar -C /prometheus -xzf /repair/prometheus-data.tgz
    while IFS= read -r rel; do
      block=${rel##*/}
      test ! -e "/prometheus/$block"
    done < /repair/block-paths.txt
  '
cp "$repair_root/prometheus.yml.before" prometheus.yml
cp "$repair_root/ai-unified.yml.before" prometheus-rules/ai-unified.yml
cp "$repair_root/ai-monitor.json.before" grafana/dashboards/ai-monitor.json
cp "$repair_root/codex-ledger-state.before.json" "$state_file"
cp -p "$repair_root/codex_ledger_exporter.py.before" "$deployed_exporter"
cp -p "$repair_root/codex_ledger.py.before" "$deployed_parser"
```

Restart the restored prior exporter before `docker compose start prometheus`,
then restart Grafana so it reprovisions the restored dashboard. Restore the
exporter restart policy and `ensure-stack` scheduler only after checks pass. Require
`curl -fsS http://127.0.0.1:9090/-/ready` to succeed, prior job samples and
dashboard to resume, and repaired-job samples to remain absent. Keep the
quarantine, volume snapshot, failed exports, and block list until diagnosis is
complete.
