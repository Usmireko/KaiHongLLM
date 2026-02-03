#!/bin/sh

export PATH=/bin:/system/bin:/usr/bin:/data/local/tmp

DBCLIENT="${DBCLIENT:-/data/local/tmp/dbclient}"
SERVER_HOST="${SERVER_HOST:-10.70.1.17}"
SERVER_USER="${SERVER_USER:-xrh}"
SERVER_PORT="${SERVER_PORT:-22}"
OUT_DIR="${OUT_DIR:-/home/xrh/qwen3_os_fault/storage/out}"
INBOX_DIR="${INBOX_DIR:-/data/faultmon/inbox}"
ACTIOND="${ACTIOND:-/data/faultmon/demo/bin/actiond.sh}"
UPLOADER="${UPLOADER:-/data/faultmon/demo_stage2/bin/uploader.sh}"
SLEEP_SEC="${SLEEP_SEC:-5}"

log() {
  echo "[actions_poller] $1"
}

if [ ! -x "$DBCLIENT" ]; then
  log "dbclient missing at $DBCLIENT"
  exit 1
fi

if [ ! -x "$ACTIOND" ]; then
  log "actiond.sh not found at $ACTIOND"
  exit 1
fi

mkdir -p "$INBOX_DIR"

DEVICE_ID="${DEVICE_ID:-}"
if [ -z "$DEVICE_ID" ] && [ -f /data/faultmon/device_id ]; then
  DEVICE_ID="$(head -n 1 /data/faultmon/device_id 2>/dev/null)"
fi
if [ -z "$DEVICE_ID" ]; then
  DEVICE_ID="board_unknown"
fi

log "device_id=$DEVICE_ID"

while true; do
  run_list="$("$DBCLIENT" -p "$SERVER_PORT" "$SERVER_USER@$SERVER_HOST" \
    "ls -1 '$OUT_DIR/$DEVICE_ID' 2>/dev/null" 2>/dev/null)"

  if [ -n "$run_list" ]; then
    printf '%s\n' "$run_list" | while IFS= read -r run_id; do
      [ -z "$run_id" ] && continue

      local_actions="$INBOX_DIR/actions_device_${run_id}.txt"
      done_mark="$INBOX_DIR/actions_done_${run_id}"

      if [ -f "$done_mark" ]; then
        continue
      fi

      if "$DBCLIENT" -p "$SERVER_PORT" "$SERVER_USER@$SERVER_HOST" \
        "cat '$OUT_DIR/$DEVICE_ID/$run_id/actions_device.txt'" > "$local_actions" 2>/dev/null; then
        log "got actions_device for run_id=$run_id"
        "$ACTIOND" run --actions "$local_actions" || true

        if [ -x "$UPLOADER" ]; then
          "$UPLOADER" || true
        else
          log "uploader missing at $UPLOADER"
        fi

        echo "ok" > "$done_mark"
      fi
    done
  fi

  sleep "$SLEEP_SEC"
done
