#!/bin/sh

export PATH=/bin:/system/bin:/usr/bin:/data/local/tmp

BASE_DIR="${BASE_DIR:-/data/faultmon/demo_stage2}"
BIN_DIR="${BIN_DIR:-$BASE_DIR/bin}"
INBOX_DIR="${INBOX_DIR:-$BASE_DIR/inbox}"
INBOX_ACTIONS="${INBOX_ACTIONS:-$INBOX_DIR/actions_device.txt}"
INBOX_RUN="${INBOX_RUN:-$INBOX_DIR/latest_run_id.txt}"
INBOX_CONCLUSION="${INBOX_CONCLUSION:-$INBOX_DIR/latest_conclusion.txt}"
READY_MARK="${READY_MARK:-$INBOX_DIR/actions.ready}"
DONE_MARK="${DONE_MARK:-$INBOX_DIR/actions.done}"
FAIL_MARK="${FAIL_MARK:-$INBOX_DIR/actions.fail}"
OUTBOX_DIR="${OUTBOX_DIR:-/data/faultmon/outbox}"
ACTIOND="${ACTIOND:-$BIN_DIR/actiond.sh}"
UPLOADER="${UPLOADER:-/data/faultmon/demo_stage2/bin/uploader_nc.sh}"
DEVICE_ID="${DEVICE_ID:-}"
SLEEP_SEC="${SLEEP_SEC:-5}"
VERBOSE=0
HEADER_ONLY=0
EXPECT_RUN=""
ONCE=0

log() {
  echo "[actions_poller_nc] $1"
}

if [ ! -x "$ACTIOND" ]; then
  log "actiond.sh not found at $ACTIOND"
  exit 1
fi

mkdir -p "$INBOX_DIR"
mkdir -p "$OUTBOX_DIR"

if [ -z "$DEVICE_ID" ] && [ -f /data/faultmon/device_id ]; then
  DEVICE_ID="$(head -n 1 /data/faultmon/device_id 2>/dev/null)"
fi
if [ -z "$DEVICE_ID" ]; then
  DEVICE_ID="board_unknown"
fi

log "device_id=$DEVICE_ID"

ACTIONS_FILE="$BASE_DIR/actions_device_latest.txt"
META_RUN="$BASE_DIR/latest_run_id.txt"
META_DEV="$BASE_DIR/latest_device_id.txt"

usage() {
  echo "actions_poller_nc.sh [--once] [--verbose] [--interval SEC] [--header-only] [--expect-run RUN]" >&2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --once) ONCE=1 ;;
    --verbose) VERBOSE=1 ;;
    --header-only) HEADER_ONLY=1 ;;
    --expect-run) shift; EXPECT_RUN="$1" ;;
    --interval) shift; SLEEP_SEC="$1" ;;
    -h|--help) usage; exit 0 ;;
    *) log "unknown arg: $1"; usage; exit 2 ;;
  esac
  shift
done

fetch_actions_once() {
  echo "$DEVICE_ID" > "$META_DEV" 2>/dev/null || true
  RUN_ID=""
  [ -f "$INBOX_RUN" ] && RUN_ID="$(cat "$INBOX_RUN" 2>/dev/null | head -n 1)"
  CONCLUSION_TEXT=""
  [ -f "$INBOX_CONCLUSION" ] && CONCLUSION_TEXT="$(cat "$INBOX_CONCLUSION" 2>/dev/null | head -n 1)"
  LEN=0
  if [ -f "$READY_MARK" ] && [ -f "$INBOX_ACTIONS" ]; then
    LEN="$(wc -c <"$INBOX_ACTIONS" 2>/dev/null)"
    set -- $LEN
    LEN="$1"
  fi
  case "$LEN" in ''|*[!0-9]*) LEN=0 ;; esac
  [ "$VERBOSE" -eq 1 ] && log "resp_header RUN=$RUN_ID LEN=$LEN source=board_inbox"
  [ "$VERBOSE" -eq 1 ] && [ -n "$CONCLUSION_TEXT" ] && log "resp_conclusion=$CONCLUSION_TEXT"

  if [ -z "$EXPECT_RUN" ] || [ "$RUN_ID" = "$EXPECT_RUN" ]; then
    echo "$RUN_ID" > "$META_RUN" 2>/dev/null || true
  fi
  if [ "$HEADER_ONLY" -eq 1 ]; then
    return 20
  fi
  if [ -n "$EXPECT_RUN" ] && [ "$RUN_ID" != "$EXPECT_RUN" ]; then
    log "expect_run_mismatch expect=$EXPECT_RUN got=$RUN_ID"
    return 21
  fi
  if [ "$LEN" -le 0 ]; then
    return 0
  fi
  cat "$INBOX_ACTIONS" > "$ACTIONS_FILE"
  return 10
}

mark_consumed() {
  run_id="$1"
  echo "$run_id" > "$DONE_MARK" 2>/dev/null || true
  rm -f "$READY_MARK" "$FAIL_MARK" 2>/dev/null || true
}

mark_failed() {
  run_id="$1"
  echo "$run_id" > "$FAIL_MARK" 2>/dev/null || true
}

exec_once() {
  fetch_actions_once
  frc=$?
  if [ "$frc" -eq 20 ]; then
    return 0
  fi
  if [ "$frc" -eq 21 ]; then
    return 21
  fi
  if [ "$frc" -ne 10 ]; then
    return 0
  fi

  RUN_ID="$(cat "$META_RUN" 2>/dev/null | head -n 1)"
  [ -n "$RUN_ID" ] || RUN_ID="run_unknown"
  out="$($ACTIOND run --actions "$ACTIONS_FILE")"
  bundle_path="$(printf '%s\n' "$out" | sed -n 's/^bundle_path=//p' | head -n 1)"

  if [ -z "$bundle_path" ]; then
    bundle_path="$(ls -1t "$OUTBOX_DIR"/action_result_bundle_*.tar.gz 2>/dev/null | head -n 1)"
  fi

  if [ -n "$bundle_path" ] && [ -x "$UPLOADER" ]; then
    "$UPLOADER" --file "$bundle_path" --type action_result --device "$DEVICE_ID" --run "$RUN_ID" || true
    mark_consumed "$RUN_ID"
    return 0
  fi

  mark_failed "$RUN_ID"
  log "uploader missing or bundle not found"
  return 2
}

if [ "$ONCE" -eq 1 ]; then
  exec_once
  exit $?
fi

while :; do
  exec_once
  sleep "$SLEEP_SEC"
done
