#!/bin/sh

export PATH=/bin:/system/bin:/usr/bin:/data/local/tmp

DBCLIENT="${DBCLIENT:-/data/local/tmp/dbclient}"
SERVER_HOST="${SERVER_HOST:-10.70.1.17}"
SERVER_USER="${SERVER_USER:-xrh}"
SERVER_PORT="${SERVER_PORT:-22}"
INBOX_DIR="${INBOX_DIR:-/home/xrh/qwen3_os_fault/storage/inbox_bundles}"
OUTBOX_DIR="${OUTBOX_DIR:-/data/faultmon/outbox}"

log() {
  echo "[uploader] $1"
}

if [ ! -x "$DBCLIENT" ]; then
  log "dbclient missing at $DBCLIENT"
  exit 1
fi

if [ ! -d "$OUTBOX_DIR" ]; then
  log "outbox missing: $OUTBOX_DIR"
  exit 1
fi

for f in "$OUTBOX_DIR"/bundle_*.tar.gz "$OUTBOX_DIR"/action_result_bundle_*.tar.gz; do
  [ -f "$f" ] || continue
  marker="$f.sent"
  if [ -f "$marker" ]; then
    continue
  fi

  base="$(basename "$f")"
  tmp="${base}.tmp"

  log "upload $base"
  if "$DBCLIENT" -p "$SERVER_PORT" "$SERVER_USER@$SERVER_HOST" \
    "cat > '$INBOX_DIR/$tmp' && mv '$INBOX_DIR/$tmp' '$INBOX_DIR/$base'" < "$f"; then
    echo "ok" > "$marker"
  else
    log "upload failed: $base"
  fi

done
