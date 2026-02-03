#!/system/bin/sh
BB=/data/local/tmp/busybox
DB=/data/local/tmp/dbclient

HOST="${ACTIONS_SSH_HOST:-10.70.1.17}"
SSH_PORT="${ACTIONS_SSH_PORT:-22}"
SSH_USER="${ACTIONS_SSH_USER:-xrh}"
KEY="${ACTIONS_SSH_KEY:-/data/faultmon/ssh/id_dropbear}"
ACTIONS_PORT="${ACTIONS_DAEMON_PORT:-28081}"

BASE_DIR=/data/faultmon/demo_stage2
BIN_DIR=$BASE_DIR/bin
LOG_DIR=$BASE_DIR/logs
DEVICE_ID_FILE=/data/faultmon/device_id

ACTIONS_FILE=$BASE_DIR/actions_device_latest.txt
META_RUN=$BASE_DIR/latest_run_id.txt
META_DEV=$BASE_DIR/latest_device_id.txt
DB_ERR=$LOG_DIR/dbclient_stderr.log

UPLOADER="$BIN_DIR/uploader_nc.sh"
ACTIOND="$BIN_DIR/actiond.sh"

ONCE=0
VERBOSE=0
HEADER_ONLY=0
EXPECT_RUN=""
INTERVAL_SEC="${ACTIONS_INTERVAL_SEC:-2}"

log(){ printf '[actions_poller] %s\n' "$*" >&2; }

compact_db_err() {
  [ -f "$DB_ERR" ] || return 0
  TMP=/data/local/tmp/db_err_tail.$$
  TMP2=/data/local/tmp/db_err_compact.$$
  "$BB" tail -n 200 "$DB_ERR" > "$TMP" 2>/dev/null || return 0
  : > "$TMP2"

  pending_block=""
  pending_repeat=0

  emit_pending() {
    if [ "$pending_repeat" -gt 0 ]; then
      printf '%s\n' "$pending_block" >> "$TMP2"
      if [ "$pending_repeat" -gt 1 ]; then
        printf '[dbclient] hostkey_noise_repeated=%s\n' "$pending_repeat" >> "$TMP2"
      fi
    fi
    pending_block=""
    pending_repeat=0
  }

  emit_block() {
    block="$1"
    [ -n "$block" ] || return 0
    if [ "$pending_repeat" -eq 0 ]; then
      pending_block="$block"
      pending_repeat=1
      return 0
    fi
    if [ "$pending_block" = "$block" ]; then
      pending_repeat=$((pending_repeat + 1))
      return 0
    fi
    emit_pending
    pending_block="$block"
    pending_repeat=1
  }

  in_block=0
  cur_block=""
  while IFS= read -r line || [ -n "$line" ]; do
    is_host=0
    case "$line" in
      "/data/local/tmp/dbclient:"|Host\ \'*\'\ key\ accepted\ unconditionally.|"(ssh-ed25519 fingerprint "*) is_host=1 ;;
      *"key accepted unconditionally."*) is_host=1 ;;
    esac

    if [ "$is_host" -eq 1 ]; then
      if [ "$line" = "/data/local/tmp/dbclient:" ] && [ "$in_block" -eq 1 ] && [ -n "$cur_block" ]; then
        emit_block "$cur_block"
        cur_block="$line"
        in_block=1
        continue
      fi
      if [ -z "$cur_block" ]; then
        cur_block="$line"
      else
        cur_block="$cur_block
$line"
      fi
      in_block=1
      continue
    fi

    if [ "$in_block" -eq 1 ]; then
      emit_block "$cur_block"
      cur_block=""
      in_block=0
      emit_pending
    fi
    printf '%s\n' "$line" >> "$TMP2"
  done < "$TMP"

  if [ "$in_block" -eq 1 ]; then
    emit_block "$cur_block"
  fi
  emit_pending

  mv "$TMP2" "$DB_ERR" 2>/dev/null || cat "$TMP2" > "$DB_ERR"
  rm -f "$TMP" "$TMP2" 2>/dev/null || true
}

read_device_id() {
  DEV="dev"
  if [ -r "$DEVICE_ID_FILE" ]; then
    DEV="$("$BB" cat "$DEVICE_ID_FILE" 2>/dev/null | "$BB" head -n 1)"
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

[ -x "$DB" ] || { log "ERROR: missing $DB"; exit 127; }
[ -r "$KEY" ] || { log "ERROR: missing key $KEY"; exit 127; }
[ -x "$BB" ] || { log "ERROR: missing $BB"; exit 127; }

mkdir -p "$BASE_DIR" "$BIN_DIR" "$LOG_DIR" 2>/dev/null || true
export HOME=/data/faultmon

fetch_actions_once() {
  DEV_ID="$(read_device_id)"
  echo "$DEV_ID" > "$META_DEV" 2>/dev/null || true

  [ "$VERBOSE" -eq 1 ] && log "fetch device_id=$DEV_ID via ssh $SSH_USER@$HOST:$SSH_PORT -B 127.0.0.1:$ACTIONS_PORT"

  TMP_RAW=/data/local/tmp/actions_raw.$$
  TMP_PAY=/data/local/tmp/actions_payload.$$
  rm -f "$TMP_RAW" "$TMP_PAY" 2>/dev/null || true

  ( "$BB" echo "DEVICE=$DEV_ID"; "$BB" echo ) | \
  "$DB" -y -I 6 -p "$SSH_PORT" -i "$KEY" -B "127.0.0.1:$ACTIONS_PORT" "$SSH_USER@$HOST" >"$TMP_RAW" 2>>"$DB_ERR"

  DBRC=$?
  if [ "$DBRC" -ne 0 ]; then
    log "ERROR: dbclient rc=$DBRC"
    "$BB" tail -n 10 "$DB_ERR" >&2 2>/dev/null || true
    compact_db_err
    rm -f "$TMP_RAW" "$TMP_PAY" 2>/dev/null || true
    return 3
  fi
  compact_db_err

  RUN=""; LEN=0; HEADER_BYTES=0
  while IFS= read -r line; do
    line_len="$(printf '%s' "$line" | "$BB" wc -c 2>/dev/null)"
    set -- $line_len; line_len="$1"
    HEADER_BYTES=$((HEADER_BYTES + line_len + 1))
    [ -z "$line" ] && break
    case "$line" in
      RUN=*) RUN="${line#RUN=}" ;;
      LEN=*) LEN="${line#LEN=}" ;;
    esac
  done < "$TMP_RAW"
  case "$LEN" in ''|*[!0-9]*) LEN=0;; esac

  [ "$VERBOSE" -eq 1 ] && log "resp_header RUN=$RUN LEN=$LEN"

  if [ -z "$EXPECT_RUN" ] || [ "$RUN" = "$EXPECT_RUN" ]; then
    echo "$RUN" > "$META_RUN" 2>/dev/null || true
  fi

  if [ "$HEADER_ONLY" -eq 1 ]; then
    rm -f "$TMP_RAW" "$TMP_PAY" 2>/dev/null || true
    return 20
  fi

  if [ -n "$EXPECT_RUN" ] && [ "$RUN" != "$EXPECT_RUN" ]; then
    log "expect_run_mismatch expect=$EXPECT_RUN got=$RUN"
    rm -f "$TMP_RAW" "$TMP_PAY" 2>/dev/null || true
    return 21
  fi

  if [ "$LEN" -le 0 ]; then
    rm -f "$TMP_RAW" "$TMP_PAY" 2>/dev/null || true
    exit 0
  fi

  "$BB" dd if="$TMP_RAW" of="$TMP_PAY" bs=1 skip="$HEADER_BYTES" count="$LEN" 2>/dev/null
  cat "$TMP_PAY" > "$ACTIONS_FILE"
  BYTES="$("$BB" wc -c <"$ACTIONS_FILE" 2>/dev/null)"; set -- $BYTES; BYTES="$1"
  log "saved: $ACTIONS_FILE bytes=$BYTES"

  rm -f "$TMP_RAW" "$TMP_PAY" 2>/dev/null || true
  return 10
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

  TS="$("$BB" date +%Y%m%d_%H%M%S 2>/dev/null || echo 00000000_000000)"
  STDOUT_F=$BASE_DIR/actiond_stdout_$TS.txt
  STDERR_F=$BASE_DIR/actiond_stderr_$TS.txt

  DEV_ID="$("$BB" cat "$META_DEV" 2>/dev/null | "$BB" head -n 1)"
  RUN_ID="$("$BB" cat "$META_RUN" 2>/dev/null | "$BB" head -n 1)"
  [ -n "$DEV_ID" ] || DEV_ID="dev"
  [ -n "$RUN_ID" ] || RUN_ID="run_unknown"

  [ "$VERBOSE" -eq 1 ] && log "exec: $ACTIOND run --actions $ACTIONS_FILE"
  cd "$BASE_DIR" 2>/dev/null || true
  "$ACTIOND" run --actions "$ACTIONS_FILE" >"$STDOUT_F" 2>"$STDERR_F"
  ARC=$?

  [ "$VERBOSE" -eq 1 ] && log "actiond_rc=$ARC (stdout=$STDOUT_F stderr=$STDERR_F)"

  RES="$(make_fresh_bundle "$TS" "$RUN_ID" "$DEV_ID" "$ARC" "$STDOUT_F" "$STDERR_F")"
  BRC=$?
  [ "$BRC" -eq 0 ] || return $BRC

  log "result_bundle=$RES"

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
