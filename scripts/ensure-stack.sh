#!/bin/sh
set -eu

SCRIPT_DIR=$(
  CDPATH=''
  cd "$(dirname "$0")"
  pwd -P
)
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
STATE_DIR="${CC_METRICS_STATE_DIR:-$PROJECT_DIR/runtime}"
LOCK_DIR="$STATE_DIR/ensure-stack.lock"
RECLAIM_DIR="$STATE_DIR/ensure-stack.reclaim"
LOCK_GRACE_SECONDS=10
LOCK_TOKEN="$$.$(date +%s)"
LOCK_OWNED=false

process_start() {
  ps -p "$1" -o lstart= 2>/dev/null |
    sed 's/^[[:space:]]*//; s/[[:space:]]*$//'
}

process_command() {
  ps -p "$1" -o command= 2>/dev/null |
    sed 's/^[[:space:]]*//; s/[[:space:]]*$//'
}

boot_id() {
  if [ -r /proc/sys/kernel/random/boot_id ]; then
    cat /proc/sys/kernel/random/boot_id
  else
    sysctl -n kern.boottime 2>/dev/null
  fi
}

lock_mtime() {
  if stat -f '%m' "$1" >/dev/null 2>&1; then
    stat -f '%m' "$1"
  else
    stat -c '%Y' "$1"
  fi
}

write_owner() {
  owner_dir=$1
  owner_token=$2
  umask 077
  printf '%s\n' "$$" >"$owner_dir/pid"
  boot_id >"$owner_dir/boot_id"
  process_start "$$" >"$owner_dir/start_time"
  process_command "$$" >"$owner_dir/command"
  printf '%s\n' "$SCRIPT_DIR/$(basename "$0")" >"$owner_dir/script"
  printf '%s\n' "$owner_token" >"$owner_dir/token"
}

metadata_complete() {
  owner_dir=$1
  for name in pid boot_id start_time command script token; do
    if [ ! -s "$owner_dir/$name" ]; then
      return 1
    fi
  done
}

has_only_owner_files() {
  owner_dir=$1
  for entry in "$owner_dir"/* "$owner_dir"/.[!.]* "$owner_dir"/..?*; do
    if [ ! -e "$entry" ] && [ ! -L "$entry" ]; then
      continue
    fi
    case "${entry##*/}" in
      pid | boot_id | start_time | command | script | token)
        [ -f "$entry" ] && [ ! -L "$entry" ] || return 1
        ;;
      *) return 1 ;;
    esac
  done
}

owner_is_active() {
  owner_dir=$1
  metadata_complete "$owner_dir" || return 1

  owner_pid=$(cat "$owner_dir/pid")
  case "$owner_pid" in
    '' | *[!0-9]*) return 1 ;;
  esac

  [ "$(cat "$owner_dir/boot_id")" = "$(boot_id)" ] || return 1
  kill -0 "$owner_pid" 2>/dev/null || return 1
  [ "$(cat "$owner_dir/start_time")" = "$(process_start "$owner_pid")" ] ||
    return 1
  [ "$(cat "$owner_dir/command")" = "$(process_command "$owner_pid")" ] ||
    return 1
  [ "$(cat "$owner_dir/script")" = "$SCRIPT_DIR/$(basename "$0")" ] ||
    return 1
}

lock_is_recent() {
  lock_time=$(lock_mtime "$1") || return 1
  now=$(date +%s)
  [ $((now - lock_time)) -lt "$LOCK_GRACE_SECONDS" ]
}

remove_owner_files() {
  owner_dir=$1
  rm -f \
    "$owner_dir/pid" \
    "$owner_dir/boot_id" \
    "$owner_dir/start_time" \
    "$owner_dir/command" \
    "$owner_dir/script" \
    "$owner_dir/token"
}

release_lock() {
  if [ "$LOCK_OWNED" != true ]; then
    return
  fi
  if [ ! -f "$LOCK_DIR/token" ] ||
    [ "$(cat "$LOCK_DIR/token")" != "$LOCK_TOKEN" ]; then
    return
  fi
  remove_owner_files "$LOCK_DIR"
  rmdir "$LOCK_DIR"
  LOCK_OWNED=false
}

remove_stale_owner_dir() {
  owner_dir=$1
  has_only_owner_files "$owner_dir" || return 1
  remove_owner_files "$owner_dir"
  rmdir "$owner_dir"
}

release_reclaim() {
  if [ ! -f "$RECLAIM_DIR/token" ] ||
    [ "$(cat "$RECLAIM_DIR/token")" != "reclaim.$LOCK_TOKEN" ]; then
    return
  fi
  remove_owner_files "$RECLAIM_DIR"
  rmdir "$RECLAIM_DIR"
}

acquire_lock() {
  if [ -d "$RECLAIM_DIR" ]; then
    if owner_is_active "$RECLAIM_DIR"; then
      return 1
    fi
    if ! metadata_complete "$RECLAIM_DIR" &&
      lock_is_recent "$RECLAIM_DIR"; then
      return 1
    fi
    if ! remove_stale_owner_dir "$RECLAIM_DIR"; then
      echo "stale ensure-stack reclaim contains unexpected entries: $RECLAIM_DIR" >&2
      return 2
    fi
  fi

  if mkdir "$LOCK_DIR" 2>/dev/null; then
    write_owner "$LOCK_DIR" "$LOCK_TOKEN"
    LOCK_OWNED=true
    return 0
  fi

  if owner_is_active "$LOCK_DIR"; then
    return 1
  fi
  if ! metadata_complete "$LOCK_DIR" && lock_is_recent "$LOCK_DIR"; then
    return 1
  fi

  mkdir "$RECLAIM_DIR" 2>/dev/null || return 1
  write_owner "$RECLAIM_DIR" "reclaim.$LOCK_TOKEN"

  if owner_is_active "$LOCK_DIR"; then
    release_reclaim
    return 1
  fi

  stale_dir="$STATE_DIR/.ensure-stack.stale.$LOCK_TOKEN"
  if [ -d "$LOCK_DIR" ]; then
    if ! has_only_owner_files "$LOCK_DIR"; then
      echo "stale ensure-stack lock contains unexpected entries: $LOCK_DIR" >&2
      release_reclaim
      return 2
    fi
    if ! mv "$LOCK_DIR" "$stale_dir"; then
      if [ -e "$LOCK_DIR" ]; then
        release_reclaim
        return 2
      fi
    else
      if ! remove_stale_owner_dir "$stale_dir"; then
        echo "could not remove quarantined ensure-stack lock: $stale_dir" >&2
        release_reclaim
        return 2
      fi
    fi
  fi

  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    release_reclaim
    return 1
  fi
  write_owner "$LOCK_DIR" "$LOCK_TOKEN"
  LOCK_OWNED=true
  release_reclaim
}

if ! command -v docker >/dev/null 2>&1; then
  echo "docker command not found" >&2
  exit 1
fi

mkdir -p "$STATE_DIR"
if acquire_lock; then
  :
else
  lock_status=$?
  if [ "$lock_status" -eq 1 ]; then
    exit 0
  fi
  exit "$lock_status"
fi
trap 'release_lock' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

timestamp() {
  date '+%Y-%m-%d %H:%M:%S %Z'
}

if ! docker info >/dev/null 2>&1; then
  echo "$(timestamp) Docker engine unavailable" >&2
  exit 1
fi

cd "$PROJECT_DIR"

if ! docker compose version >/dev/null 2>&1; then
  echo "$(timestamp) Docker Compose plugin unavailable" >&2
  exit 1
fi

docker compose --progress quiet up -d
echo "$(timestamp) cc-metrics stack ensured"

# Containers being up is not evidence that Claude is emitting. Both known
# telemetry outages coincided with a tool upgrade and ran 29h and 19h
# unnoticed while this script reported success. The gate runs the canary
# only when a version actually changed, and never fails the stack.
"$PROJECT_DIR/scripts/version-gate.sh" || true

# Profiles are added at arbitrary times, not at upgrades, so this cannot ride
# the version gate. It is static and spends no model call, which is why it can
# run every tick. A profile missing its env block is invisible to the capture
# ratio - the account contributes to neither side of it - so nothing downstream
# would notice. Reports only; never fails the stack.
if ! python3 "$PROJECT_DIR/scripts/check_profile_telemetry.py" >/tmp/cc-metrics-profile-check.$$ 2>&1; then
  echo "$(timestamp) profile telemetry gaps found:"
  sed 's/^/  /' /tmp/cc-metrics-profile-check.$$
  [ -x /usr/bin/osascript ] && /usr/bin/osascript -e \
    'display notification "A Claude profile is not configured to export telemetry." with title "cc-metrics"' \
    >/dev/null 2>&1
fi
mv /tmp/cc-metrics-profile-check.$$ /tmp/cc-metrics-profile-check.last 2>/dev/null || true
