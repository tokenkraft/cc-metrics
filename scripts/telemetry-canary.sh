#!/usr/bin/env bash
# Telemetry canary - proves Claude Code telemetry actually reaches the collector.
#
# Runs one throwaway `claude -p` under `env -i`, which is the whole point: that
# is the launchd/daemon environment, so only the settings.json carrier can turn
# telemetry on: the CLI reads its OTEL configuration from process env at
# startup, so a daemon launched or restarted by a service manager that does not
# source shell rc files starts with a minimal environment, and every session it
# spawns inherits that gap and exports nothing without erroring.
#
# Two independent gates, both required:
#   1. the run's debug log records a successful first metrics export, and
#      names a non-empty OTLP reader list;
#   2. the collector's own counter for the canary model gains input tokens.
# `isTelemetryEnabled=true` is deliberately NOT a gate - sessions have been
# measured enabled with `getOtlpReaders: types=[], endpoint=undefined`, i.e.
# enabled and exporting nowhere. `ps eww` is not a gate either: it shows the
# kernel's initial environ, where settings-injected variables never appear.
#
# Exit codes: 0 = PASS; 1 = FAIL (a gate did not hold); 2 = the gate could not
# be run (collector unreachable, claude missing, bad arguments).
set -euo pipefail

METRICS_URL="${CANARY_METRICS_URL:-http://127.0.0.1:8889/metrics}"
# Assumes nothing else concurrently uses the canary model alias, so the counter
# delta stays attributable to this run; concurrent same-model traffic can mask a
# failure. Session counters are not attributable either way: they are shared.
MODEL="claude-haiku-4-5"
PROMPT='Reply with exactly the word: CANARY'
DELTA_TIMEOUT_SECONDS=90
DELTA_POLL_SECONDS=3
EXPORT_LINE='[3P telemetry] First metrics export: SUCCESS'
READERS_PATTERN='\[3P telemetry\] getOtlpReaders: types=\["[^]]'

usage() {
  cat <<'USAGE'
Usage:
  telemetry-canary.sh                             run the live gate
  telemetry-canary.sh --check-log FILE            gate 1 only, against a debug log
  telemetry-canary.sh --check-delta BEFORE AFTER  gate 2 only, against two scrapes
USAGE
}

die() {
  printf 'telemetry-canary: %s\n' "$1" >&2
  exit 2
}

scrape() {
  curl --fail --silent --show-error --max-time 10 --output "$1" "$METRICS_URL"
}

# Sum every canary-model input-token series in a scrape dump. The model label
# appears under several app_entrypoint values, so the series are summed rather
# than matched one by one; the closing quote keeps dated model labels out.
sum_input_tokens() {
  awk -v model="$MODEL" '
    index($0, "claude_code_token_usage_tokens_total{") == 1 &&
    index($0, "model=\"" model "\"") &&
    index($0, "type=\"input\"") {
      value = $0
      sub(/^[^}]*\}[[:space:]]*/, "", value)
      split(value, fields, /[[:space:]]+/)
      total += fields[1]
    }
    END { printf "%.0f\n", total + 0 }
  ' "$1"
}

# Gate 1. Returns 0 only when the log proves an export happened.
check_debug_log() {
  local file=$1 failed=0 readers
  [ -r "$file" ] || die "debug log not readable: $file"

  if grep -qF "$EXPORT_LINE" "$file"; then
    printf 'gate 1 export:  PASS  %s\n' "$EXPORT_LINE"
  else
    printf 'gate 1 export:  FAIL  no "%s" in %s\n' "$EXPORT_LINE" "$file"
    failed=1
  fi

  if grep -qE "$READERS_PATTERN" "$file"; then
    readers=$(
      awk 'match($0, /getOtlpReaders: types=\[[^]]*\]/) {
             print substr($0, RSTART, RLENGTH); exit
           }' "$file"
    )
    printf 'gate 1 readers: PASS  %s\n' "$readers"
  else
    printf 'gate 1 readers: FAIL  no non-empty getOtlpReaders types in %s\n' "$file"
    failed=1
  fi

  return "$failed"
}

# Gate 2. Prints the delta and returns 0 only when the counter moved up.
check_delta() {
  local before after delta verdict=PASS
  [ -r "$1" ] || die "scrape not readable: $1"
  [ -r "$2" ] || die "scrape not readable: $2"
  before=$(sum_input_tokens "$1")
  after=$(sum_input_tokens "$2")
  delta=$((after - before))
  [ "$delta" -gt 0 ] || verdict=FAIL
  printf 'gate 2 delta:   %s  input tokens %s -> %s (delta %+d, model=%s)\n' \
    "$verdict" "$before" "$after" "$delta" "$MODEL"
  [ "$verdict" = PASS ]
}

run_live_gate() {
  local config_dir work_dir debug_log before_file after_file
  local canary_user before after delta waited=0 rc=0 log_ok=0 delta_ok=0

  command -v curl >/dev/null 2>&1 || die "curl command not found on PATH"

  work_dir="$(mktemp -d "${TMPDIR:-/tmp}/telemetry-canary.XXXXXX")"
  debug_log="$work_dir/canary.debug"
  before_file="$work_dir/scrape-before.txt"
  after_file="$work_dir/scrape-after.txt"

  # Read the counter first: an unreachable collector makes the run pointless,
  # and this way that failure is reported without spending a model call.
  scrape "$before_file" ||
    die "collector metrics endpoint unreachable: $METRICS_URL"
  before=$(sum_input_tokens "$before_file")

  command -v claude >/dev/null 2>&1 || die "claude command not found on PATH"
  # Fall back to the CLI's own default rather than any named profile: a
  # hardcoded profile name does not exist on anyone else's machine.
  config_dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
  [ -d "$config_dir" ] ||
    die "claude config dir not found: $config_dir (set CLAUDE_CONFIG_DIR)"

  canary_user="${USER:-$(id -un)}"
  printf 'canary: %s under env -i, config dir %s\n' "$MODEL" "$config_dir"
  env -i \
    HOME="$HOME" \
    PATH="$PATH" \
    USER="$canary_user" \
    LOGNAME="$canary_user" \
    SHELL=/bin/zsh \
    TMPDIR="${TMPDIR:-/tmp}" \
    CLAUDE_CONFIG_DIR="$config_dir" \
    claude -p "$PROMPT" --model "$MODEL" --debug-file "$debug_log" \
    </dev/null >"$work_dir/reply.txt" 2>"$work_dir/claude.err" || rc=$?
  if [ "$rc" -ne 0 ]; then
    # `Not logged in - Please run /login` goes to stdout, not stderr, and it is
    # the most likely reason for a non-zero exit here: credentials are keyed to
    # the config dir, so an unauthenticated dir fails while a sibling profile
    # works. Showing only stderr hid that behind a bare exit code.
    tail -n 5 "$work_dir/claude.err" >&2 || true
    tail -n 5 "$work_dir/reply.txt" >&2 || true
    die "claude exited $rc under CLAUDE_CONFIG_DIR=$config_dir - the gate did not get to run (logs in $work_dir)"
  fi
  printf 'canary: reply %s\n' "$(tr -d '\n' <"$work_dir/reply.txt")"

  # The exporter flushes on exit, but the collector's own scrape endpoint is
  # updated asynchronously, so poll rather than sample once.
  while true; do
    scrape "$after_file" ||
      die "collector metrics endpoint unreachable: $METRICS_URL"
    after=$(sum_input_tokens "$after_file")
    if [ "$after" -gt "$before" ] || [ "$waited" -ge "$DELTA_TIMEOUT_SECONDS" ]; then
      break
    fi
    sleep "$DELTA_POLL_SECONDS"
    waited=$((waited + DELTA_POLL_SECONDS))
  done

  check_debug_log "$debug_log" || log_ok=1
  check_delta "$before_file" "$after_file" || delta_ok=1
  delta=$((after - before))

  printf 'canary: debug log %s\n' "$debug_log"
  if [ "$log_ok" -eq 0 ] && [ "$delta_ok" -eq 0 ]; then
    printf 'telemetry-canary: PASS  export SUCCESS, %s input tokens %+d in %ss\n' \
      "$MODEL" "$delta" "$waited"
    return 0
  fi
  printf 'telemetry-canary: FAIL  %s input tokens %+d after %ss (see gates above)\n' \
    "$MODEL" "$delta" "$waited"
  return 1
}

case "${1:-}" in
  '')
    if run_live_gate; then exit 0; else exit 1; fi
    ;;
  --check-log)
    [ "$#" -eq 2 ] || die "usage: --check-log FILE"
    if check_debug_log "$2"; then
      printf 'telemetry-canary: PASS  export evidence found in %s\n' "$2"
      exit 0
    fi
    printf 'telemetry-canary: FAIL  no export evidence in %s\n' "$2"
    exit 1
    ;;
  --check-delta)
    [ "$#" -eq 3 ] || die "usage: --check-delta BEFORE AFTER"
    if check_delta "$2" "$3"; then
      printf 'telemetry-canary: PASS  counter moved\n'
      exit 0
    fi
    printf 'telemetry-canary: FAIL  counter did not move\n'
    exit 1
    ;;
  -h | --help)
    usage
    exit 0
    ;;
  *)
    usage >&2
    die "unknown argument: $1"
    ;;
esac
