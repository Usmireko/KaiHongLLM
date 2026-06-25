#!/system/bin/sh

BASE_DIR=/data/faultmon/demo_stage2
BIN_DIR=$BASE_DIR/bin
LOG_DIR=$BASE_DIR/logs
DEVICE_ID_FILE=/data/faultmon/device_id

INBOX_DIR=$BASE_DIR/inbox
INBOX_ACTIONS=$INBOX_DIR/actions_device.txt
INBOX_RUN=$INBOX_DIR/latest_run_id.txt
INBOX_CONCLUSION=$INBOX_DIR/latest_conclusion.txt
READY_MARK=$INBOX_DIR/actions.ready
DONE_MARK=$INBOX_DIR/actions.done
FAIL_MARK=$INBOX_DIR/actions.fail

ACTIONS_FILE=$BASE_DIR/actions_device_latest.txt
META_RUN=$BASE_DIR/latest_run_id.txt
META_DEV=$BASE_DIR/latest_device_id.txt

UPLOADER="$BIN_DIR/uploader_nc.sh"
ACTIOND="$BIN_DIR/actiond.sh"

ONCE=0
VERBOSE=0
HEADER_ONLY=0
EXPECT_RUN=""
INTERVAL_SEC="${ACTIONS_INTERVAL_SEC:-2}"

log(){ printf '[actions_poller] %s\n' "$*" >&2; }

read_device_id() {
  DEV="dev"
  if [ -r "$DEVICE_ID_FILE" ]; then
    DEV="$(cat "$DEVICE_ID_FILE" 2>/dev/null | head -n 1)"
  fi
  [ -n "$DEV" ] || DEV="dev"
  printf '%s' "$DEV"
}

usage(){ echo "actions_poller_nc.sh [--once] [--verbose] [--interval SEC] [--header-only] [--expect-run RUN]" >&2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --once) ONCE=1 ;;
    --verbose) VERBOSE=1 ;;
    --header-only) HEADER_ONLY=1 ;;
    --expect-run) shift; EXPECT_RUN="$1" ;;
    --interval) shift; INTERVAL_SEC="$1" ;;
    -h|--help) usage; exit 0 ;;
    *) log "unknown arg: $1"; usage; exit 2 ;;
  esac
  shift
 done

mkdir -p "$BASE_DIR" "$BIN_DIR" "$LOG_DIR" "$INBOX_DIR" 2>/dev/null || true

fetch_actions_once() {
  DEV_ID="$(read_device_id)"
  echo "$DEV_ID" > "$META_DEV" 2>/dev/null || true

  RUN=""
  [ -f "$INBOX_RUN" ] && RUN="$(cat "$INBOX_RUN" 2>/dev/null | head -n 1)"
  CONCLUSION_TEXT=""
  [ -f "$INBOX_CONCLUSION" ] && CONCLUSION_TEXT="$(cat "$INBOX_CONCLUSION" 2>/dev/null | head -n 1)"
  LEN=0
  if [ -f "$READY_MARK" ] && [ -f "$INBOX_ACTIONS" ]; then
    LEN="$(wc -c <"$INBOX_ACTIONS" 2>/dev/null)"
    set -- $LEN
    LEN="$1"
  fi
  case "$LEN" in ''|*[!0-9]*) LEN=0;; esac

  [ "$VERBOSE" -eq 1 ] && log "resp_header RUN=$RUN LEN=$LEN source=board_inbox"
  [ "$VERBOSE" -eq 1 ] && [ -n "$CONCLUSION_TEXT" ] && log "resp_conclusion=$CONCLUSION_TEXT"

  if [ -z "$EXPECT_RUN" ] || [ "$RUN" = "$EXPECT_RUN" ]; then
    echo "$RUN" > "$META_RUN" 2>/dev/null || true
  fi

  if [ "$HEADER_ONLY" -eq 1 ]; then
    return 20
  fi

  if [ -n "$EXPECT_RUN" ] && [ "$RUN" != "$EXPECT_RUN" ]; then
    log "expect_run_mismatch expect=$EXPECT_RUN got=$RUN"
    return 21
  fi

  if [ "$LEN" -le 0 ]; then
    return 0
  fi

  cat "$INBOX_ACTIONS" > "$ACTIONS_FILE"
  BYTES="$(wc -c <"$ACTIONS_FILE" 2>/dev/null)"; set -- $BYTES; BYTES="$1"
  log "saved: $ACTIONS_FILE bytes=$BYTES source=board_inbox"

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

make_fresh_bundle() {
  # always create a brand-new bundle for THIS run+timestamp (never reuse old file)
  TS="$1"
  RUN_ID="$2"
  DEV_ID="$3"
  ACTIOND_RC="$4"
  STDOUT_F="$5"
  STDERR_F="$6"

  WORK=$BASE_DIR/_bundle_work_$TS
  mkdir -p "$WORK" 2>/dev/null || true

  echo "$DEV_ID" > "$WORK/device_id.txt" 2>/dev/null || true
  echo "$RUN_ID" > "$WORK/run_id.txt" 2>/dev/null || true
  echo "$ACTIOND_RC" > "$WORK/actiond_rc.txt" 2>/dev/null || true

  cp "$ACTIONS_FILE" "$WORK/actions_device.txt" 2>/dev/null || true
  cp "$STDOUT_F" "$WORK/actiond_stdout.txt" 2>/dev/null || true
  cp "$STDERR_F" "$WORK/actiond_stderr.txt" 2>/dev/null || true

  if [ "$ACTIOND_RC" -eq 0 ]; then
    echo "{\"ok\":true,\"note\":\"actiond rc=0\",\"ts\":\"$TS\",\"run\":\"$RUN_ID\",\"device\":\"$DEV_ID\"}" > "$WORK/action_result.json"
  else
    echo "{\"ok\":false,\"note\":\"actiond rc!=0\",\"ts\":\"$TS\",\"run\":\"$RUN_ID\",\"device\":\"$DEV_ID\"}" > "$WORK/action_result.json"
  fi

  RES=$BASE_DIR/action_result_bundle_${RUN_ID}__${TS}.tar.gz
  tar -czf "$RES" -C "$WORK" . 2>/dev/null
  TRC=$?
  rm -rf "$WORK" 2>/dev/null || true

  [ "$TRC" -eq 0 ] || { log "ERROR: tar failed rc=$TRC"; return 5; }
  echo "$RES"
  return 0
}

exec_and_upload_once() {
  if [ ! -x "$ACTIOND" ]; then
    log "ERROR: missing $ACTIOND (cannot execute actions)"
    return 2
  fi

  TS="$(date +%Y%m%d_%H%M%S 2>/dev/null || echo 00000000_000000)"
  STDOUT_F=$BASE_DIR/actiond_stdout_$TS.txt
  STDERR_F=$BASE_DIR/actiond_stderr_$TS.txt

  DEV_ID="$(cat "$META_DEV" 2>/dev/null | head -n 1)"
  RUN_ID="$(cat "$META_RUN" 2>/dev/null | head -n 1)"
  [ -n "$DEV_ID" ] || DEV_ID="dev"
  [ -n "$RUN_ID" ] || RUN_ID="run_unknown"

  [ "$VERBOSE" -eq 1 ] && log "exec: $ACTIOND run --actions $ACTIONS_FILE"
  cd "$BASE_DIR" 2>/dev/null || true
  "$ACTIOND" run --actions "$ACTIONS_FILE" >"$STDOUT_F" 2>"$STDERR_F"
  ARC=$?

  [ "$VERBOSE" -eq 1 ] && log "actiond_rc=$ARC (stdout=$STDOUT_F stderr=$STDERR_F)"

  RES="$(make_fresh_bundle "$TS" "$RUN_ID" "$DEV_ID" "$ARC" "$STDOUT_F" "$STDERR_F")"
  BRC=$?
  if [ "$BRC" -ne 0 ]; then
    mark_failed "$RUN_ID"
    return $BRC
  fi

  log "result_bundle=$RES"
  mark_consumed "$RUN_ID"

  if [ ! -x "$UPLOADER" ]; then
    log "WARN: missing $UPLOADER (skip upload)"
    return 0
  fi

  [ "$VERBOSE" -eq 1 ] && log "upload: $UPLOADER --file $RES --type action_result --device $DEV_ID --run $RUN_ID"
  "$UPLOADER" --file "$RES" --type action_result --device "$DEV_ID" --run "$RUN_ID"
  URC=$?
  log "upload_rc=$URC"
  return 0
}

main_once() {
  fetch_actions_once
  FRC=$?
  if [ "$FRC" -eq 10 ]; then
    exec_and_upload_once
    return $?
  fi
  if [ "$FRC" -eq 20 ]; then
    return 0
  fi
  if [ "$FRC" -eq 21 ]; then
    return 21
  fi
  [ "$VERBOSE" -eq 1 ] && log "empty response (no action)"
  return 0
}

if [ "$ONCE" -eq 1 ]; then
  main_once
  exit $?
fi

while :; do
  main_once
  sleep "$INTERVAL_SEC" 2>/dev/null || sleep 2
 done
