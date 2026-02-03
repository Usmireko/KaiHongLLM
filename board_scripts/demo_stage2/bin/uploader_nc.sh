#!/system/bin/sh
BB=/data/local/tmp/busybox
DB=/data/local/tmp/dbclient

# defaults (can override by env)
SSH_HOST="${ACTIONS_SSH_HOST:-10.70.1.17}"
SSH_PORT="${ACTIONS_SSH_PORT:-22}"
SSH_USER="${ACTIONS_SSH_USER:-xrh}"
SSH_KEY="${ACTIONS_SSH_KEY:-/data/faultmon/ssh/id_dropbear}"

# ingest server listens on server side
INGEST_PORT="${INGEST_PORT:-18080}"

BASE_DIR=/data/faultmon/demo_stage2
LOG_DIR=$BASE_DIR/logs
DB_ERR=$LOG_DIR/dbclient_stderr.log

log(){ printf '[uploader_dbB] %s\n' "$*" >&2; }

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

usage(){
  echo "Usage: $0 --file <path> --type <bundle|action_result> --device <id> --run <run_id>" >&2
  echo "Env: ACTIONS_SSH_HOST ACTIONS_SSH_PORT ACTIONS_SSH_USER ACTIONS_SSH_KEY INGEST_PORT" >&2
}

FILE=""
TYPE=""
DEVICE=""
RUN_ID=""

while [ $# -gt 0 ]; do
  case "$1" in
    --file) shift; FILE="$1" ;;
    --type) shift; TYPE="$1" ;;
    --device) shift; DEVICE="$1" ;;
    --run) shift; RUN_ID="$1" ;;
    -h|--help) usage; exit 0 ;;
    *) log "unknown arg: $1"; usage; exit 2 ;;
  esac
  shift
done

[ -x "$BB" ] || { log "ERROR: missing $BB"; exit 127; }
[ -x "$DB" ] || { log "ERROR: missing $DB"; exit 127; }
[ -r "$SSH_KEY" ] || { log "ERROR: missing key $SSH_KEY"; exit 127; }

[ -n "$FILE" ] || { usage; exit 2; }
[ -r "$FILE" ] || { log "ERROR: file not readable: $FILE"; exit 2; }
[ -n "$TYPE" ] || { usage; exit 2; }
[ -n "$DEVICE" ] || { usage; exit 2; }
[ -n "$RUN_ID" ] || { usage; exit 2; }

case "$TYPE" in
  bundle|action_result) ;;
  *) log "ERROR: bad --type $TYPE"; usage; exit 2 ;;
esac

# bytes
LEN="$("$BB" wc -c <"$FILE" 2>/dev/null)"
set -- $LEN; LEN="$1"
case "$LEN" in ''|*[!0-9]*) LEN=0;; esac
[ "$LEN" -gt 0 ] || { log "ERROR: file empty or len invalid: $FILE"; exit 2; }

export HOME=/data/faultmon
mkdir -p "$LOG_DIR" 2>/dev/null || true

[ "$LEN" -lt 104857600 ] || log "WARN: large upload len=$LEN"

# header + binary payload -> dbclient -B (acts like nc)
{
  "$BB" echo "DEVICE=$DEVICE"
  "$BB" echo "TYPE=$TYPE"
  "$BB" echo "RUN=$RUN_ID"
  "$BB" echo "LEN=$LEN"
  "$BB" echo
  "$BB" cat "$FILE"
} | "$DB" -y -I 20 -p "$SSH_PORT" -i "$SSH_KEY" -B "127.0.0.1:$INGEST_PORT" "$SSH_USER@$SSH_HOST" > /dev/null 2>>"$DB_ERR"

rc=$?
if [ "$rc" -ne 0 ]; then
  log "ERROR: dbclient upload rc=$rc"
  "$BB" tail -n 10 "$DB_ERR" >&2 2>/dev/null || true
  compact_db_err
  exit 1
fi
compact_db_err

log "OK: uploaded type=$TYPE device=$DEVICE run=$RUN_ID len=$LEN"
exit 0
