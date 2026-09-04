#!/usr/bin/env bash
# Run the telemetry canary when a tool version changes.
#
# Every known Claude telemetry outage coincided with a tool version change:
# one Paseo upgrade landed shortly before an outage window, and a later one
# landed inside another, where the reparent to launchd dropped the
# shell-carried OTEL env. Claude Code has also updated itself mid-session
# with nothing to say so.
#
# An upgrade is the moment telemetry is most likely to break and least
# likely to be watched, so that is when the canary earns its cost. Steady
# state is free: the canary runs only when a recorded version differs.
#
# Exits 0 even when the canary fails. The watchdog that calls this must
# keep the stack up regardless; the failure is reported by notification,
# matching alert_notify.py's local-only sink.

set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
STATE_DIR="${CC_METRICS_STATE_DIR:-$PROJECT_DIR/runtime}"
STATE_PATH="$STATE_DIR/tool-versions.state"
# Overridable so tests can substitute a stub instead of spending a model call.
CANARY="${CC_METRICS_CANARY:-$PROJECT_DIR/scripts/telemetry-canary.sh}"
OSASCRIPT="/usr/bin/osascript"

# launchd starts with a minimal PATH that omits Homebrew and nvm shims, so a
# bare `command -v` finds nothing. Widen it before resolving any tool.
#
# Pinning one node version here was a portability bug: on a machine with any
# other version the entry does not exist, both tools resolve to `absent`, that
# becomes the recorded baseline, and the gate then reports "no version change"
# forever. A detector that silently never fires is the exact failure this
# script exists to catch. Glob every installed version instead.
# CC_METRICS_PATH replaces the discovered tool directories so the absent-tool
# branch is testable; without a seam it can never be exercised on a machine
# that has the tools installed. It keeps launchd's own minimal PATH beneath it,
# both because that is what this script really runs under and because dropping
# it would take coreutils with it.
if [ -n "${CC_METRICS_PATH:-}" ]; then
  PATH="$CC_METRICS_PATH:/usr/bin:/bin:/usr/sbin:/sbin"
else
  for candidate_dir in "$HOME/.local/bin" "$HOME"/.nvm/versions/node/*/bin \
                       /opt/homebrew/bin /usr/local/bin; do
    [ -d "$candidate_dir" ] && PATH="$candidate_dir:$PATH"
  done
fi
export PATH

timestamp() {
  date '+%Y-%m-%d %H:%M:%S %Z'
}

log() {
  echo "$(timestamp) version-gate: $*"
}

# Absent tool reports `absent` rather than failing: a machine without Paseo is
# a valid state, and a missing binary must not wedge the watchdog.
tool_version() {
  local name="$1" binary
  if ! binary="$(command -v "$name" 2>/dev/null)"; then
    echo "absent"
    return 0
  fi
  "$binary" --version 2>/dev/null | head -1 | tr -d '\n' || echo "unknown"
}

notify() {
  local title="$1" message="$2"
  [ -x "$OSASCRIPT" ] || return 0
  "$OSASCRIPT" -e "display notification \"${message//\"/\\\"}\" with title \"${title//\"/\\\"}\"" \
    >/dev/null 2>&1 || true
}

main() {
  mkdir -p "$STATE_DIR"

  local observed
  observed="claude=$(tool_version claude)
paseo=$(tool_version paseo)"

  # A missing paseo is a valid machine. A missing claude is not, on a host
  # running this stack, and recording it as the baseline would wedge the gate
  # into permanent silence.
  if [ "$(tool_version claude)" = "absent" ]; then
    log "claude not found on PATH; gate cannot verify telemetry"
    notify "cc-metrics" "Telemetry gate cannot find the claude binary."
    return 0
  fi

  if [ ! -f "$STATE_PATH" ]; then
    printf '%s\n' "$observed" >"$STATE_PATH"
    log "baseline recorded, canary not run"
    printf '%s\n' "$observed" | sed 's/^/  /'
    return 0
  fi

  local recorded
  recorded="$(cat "$STATE_PATH")"

  if [ "$observed" = "$recorded" ]; then
    log "no version change"
    return 0
  fi

  log "version change detected"
  diff <(printf '%s\n' "$recorded") <(printf '%s\n' "$observed") | sed 's/^/  /' || true

  # Record before running, so a canary that crashes cannot cause the same
  # change to re-trigger on every subsequent watchdog tick.
  printf '%s\n' "$observed" >"$STATE_PATH"

  if [ ! -x "$CANARY" ]; then
    log "canary missing at $CANARY"
    notify "cc-metrics" "Version changed but telemetry canary is missing."
    return 0
  fi

  local canary_status=0
  "$CANARY" || canary_status=$?

  case "$canary_status" in
    0)
      log "canary PASS after version change"
      ;;
    2)
      log "canary could not run (exit 2)"
      notify "cc-metrics" "Version changed; telemetry canary could not run."
      ;;
    *)
      log "canary FAIL after version change (exit $canary_status)"
      notify "cc-metrics telemetry DARK" \
        "A version change broke Claude telemetry. Run scripts/telemetry-canary.sh."
      ;;
  esac

  return 0
}

main "$@"
