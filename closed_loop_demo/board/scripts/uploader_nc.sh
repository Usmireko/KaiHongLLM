#!/system/bin/sh

BASE_DIR=/data/faultmon/demo_stage2
SPOOL_ROOT="${SPOOL_ROOT:-$BASE_DIR/spool}"
LOG_DIR="$BASE_DIR/logs"

log(){ printf '[uploader_nc] %s\n' "$*" >&2; }

fail() {
  msg="$1"
  [ -n "$TMP_FILE" ] && rm -f "$TMP_FILE" 2>/dev/null || true
  [ -n "$TMP_META" ] && rm -f "$TMP_META" 2>/dev/null || true
  [ -n "$TMP_MANIFEST" ] && rm -f "$TMP_MANIFEST" 2>/dev/null || true
  log "ERROR: $msg"
  exit 1
}

usage(){
  echo "Usage: $0 --file <path> --type <bundle|action_result> --device <id> --run <run_id>" >&2
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

[ -n "$FILE" ] || { usage; exit 2; }
[ -r "$FILE" ] || fail "file not readable: $FILE"

[ -n "$TYPE" ] || { usage; exit 2; }
[ -n "$DEVICE" ] || { usage; exit 2; }
[ -n "$RUN_ID" ] || { usage; exit 2; }

case "$TYPE" in
  bundle|action_result) ;;
  *) log "ERROR: bad --type $TYPE"; usage; exit 2 ;;
esac

LEN="$(wc -c <"$FILE" 2>/dev/null)"
set -- $LEN; LEN="$1"
case "$LEN" in ''|*[!0-9]*) LEN=0;; esac
[ "$LEN" -gt 0 ] || fail "file empty or len invalid: $FILE"

mkdir -p "$LOG_DIR" "$SPOOL_ROOT/$DEVICE" 2>/dev/null || true

QUEUE_DIR="$SPOOL_ROOT/$DEVICE"
NAME="${RUN_ID}__${TYPE}.tar.gz"
QUEUE_PATH="$QUEUE_DIR/$NAME"
META_PATH="$QUEUE_PATH.meta"
MANIFEST_PATH="$QUEUE_PATH.manifest"
READY_PATH="$QUEUE_PATH.ready"
DONE_PATH="$QUEUE_PATH.done"
FAIL_PATH="$QUEUE_PATH.fail"
TMP_FILE="$QUEUE_PATH.tmp.$$"
TMP_META="$META_PATH.tmp.$$"
TMP_MANIFEST="$MANIFEST_PATH.tmp.$$"

rm -f "$READY_PATH" "$DONE_PATH" "$FAIL_PATH" 2>/dev/null || true

cp "$FILE" "$TMP_FILE" 2>/dev/null || fail "copy failed to $TMP_FILE"
mv "$TMP_FILE" "$QUEUE_PATH" 2>/dev/null || fail "move failed to $QUEUE_PATH"

{
  echo "TYPE=$TYPE"
  echo "DEVICE=$DEVICE"
  echo "RUN=$RUN_ID"
  echo "LEN=$LEN"
  echo "NAME=$NAME"
  echo "SRC=$FILE"
} > "$TMP_META" 2>/dev/null || fail "write meta failed"
mv "$TMP_META" "$META_PATH" 2>/dev/null || fail "move meta failed"

cp "$META_PATH" "$TMP_MANIFEST" 2>/dev/null || fail "write manifest failed"
mv "$TMP_MANIFEST" "$MANIFEST_PATH" 2>/dev/null || fail "move manifest failed"

printf 'ready\n' > "$READY_PATH" 2>/dev/null || fail "write ready marker failed"

log "queued type=$TYPE device=$DEVICE run=$RUN_ID len=$LEN path=$QUEUE_PATH"
echo "queued_path=$QUEUE_PATH"
echo "meta_path=$META_PATH"
echo "manifest_path=$MANIFEST_PATH"
echo "ready_marker=$READY_PATH"
exit 0
