#!/bin/sh

export PATH=/bin:/system/bin:/usr/bin:/data/local/tmp

BASE_DIR="${BASE_DIR:-/data/faultmon/demo_stage2}"
SPOOL_ROOT="${SPOOL_ROOT:-$BASE_DIR/spool}"
LOG_DIR="${LOG_DIR:-$BASE_DIR/logs}"
FILE=""
TYPE=""
DEVICE_ID=""
RUN_ID=""

log() {
  echo "[uploader_nc] $1" >&2
}

fail() {
  msg="$1"
  [ -n "$TMP_FILE" ] && rm -f "$TMP_FILE" 2>/dev/null || true
  [ -n "$TMP_META" ] && rm -f "$TMP_META" 2>/dev/null || true
  [ -n "$TMP_MANIFEST" ] && rm -f "$TMP_MANIFEST" 2>/dev/null || true
  log "ERROR: $msg"
  exit 1
}

usage() {
  echo "Usage: $0 --file <path> --type <bundle|action_result> --device <id> --run <run_id>" >&2
  exit 1
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --file)
      FILE="$2"
      shift 2
      ;;
    --type)
      TYPE="$2"
      shift 2
      ;;
    --device)
      DEVICE_ID="$2"
      shift 2
      ;;
    --run)
      RUN_ID="$2"
      shift 2
      ;;
    --host)
      HOST="$2"
      shift 2
      ;;
    --port)
      PORT="$2"
      shift 2
      ;;
    *)
      usage
      ;;
  esac
done

if [ -z "$FILE" ] || [ -z "$TYPE" ] || [ -z "$DEVICE_ID" ] || [ -z "$RUN_ID" ]; then
  usage
fi

if [ ! -f "$FILE" ]; then
  fail "file missing $FILE"
fi

set -- $(wc -c < "$FILE")
LEN="$1"

if [ -z "$LEN" ]; then
  fail "unable to get length"
fi

case "$TYPE" in
  bundle|action_result) ;;
  *) fail "bad type $TYPE" ;;
esac

mkdir -p "$LOG_DIR" "$SPOOL_ROOT/$DEVICE_ID" 2>/dev/null || true

QUEUE_DIR="$SPOOL_ROOT/$DEVICE_ID"
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
  echo "DEVICE=$DEVICE_ID"
  echo "RUN=$RUN_ID"
  echo "LEN=$LEN"
  echo "NAME=$NAME"
  echo "SRC=$FILE"
} > "$TMP_META" 2>/dev/null || fail "write meta failed"
mv "$TMP_META" "$META_PATH" 2>/dev/null || fail "move meta failed"

cp "$META_PATH" "$TMP_MANIFEST" 2>/dev/null || fail "write manifest failed"
mv "$TMP_MANIFEST" "$MANIFEST_PATH" 2>/dev/null || fail "move manifest failed"

printf 'ready\n' > "$READY_PATH" 2>/dev/null || fail "write ready marker failed"

log "queued type=$TYPE device=$DEVICE_ID run=$RUN_ID len=$LEN path=$QUEUE_PATH"
echo "queued_path=$QUEUE_PATH"
echo "meta_path=$META_PATH"
echo "manifest_path=$MANIFEST_PATH"
echo "ready_marker=$READY_PATH"
exit 0
