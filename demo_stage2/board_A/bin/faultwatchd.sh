#!/bin/sh

export PATH=/bin:/system/bin:/usr/bin:/data/local/tmp

EVENTS_DIR="/data/faultmon/events"
STATE_FILE="/data/faultmon/logs/faultwatchd.state"
BUNDLE_SH="${BUNDLE_SH:-/data/faultmon/demo/bin/bundle.sh}"
TAG="${TAG:-auto_event}"
PRE_SEC="${PRE_SEC:-10}"
POST_SEC="${POST_SEC:-10}"
SLEEP_SEC="${SLEEP_SEC:-3}"

log() {
  echo "[faultwatchd] $1"
}

mkdir -p /data/faultmon/logs

if [ ! -x "$BUNDLE_SH" ]; then
  log "bundle.sh not found at $BUNDLE_SH"
  exit 1
fi

last_lines=0
if [ -f "$STATE_FILE" ]; then
  last_lines="$(head -n 1 "$STATE_FILE" 2>/dev/null)"
fi

while true; do
  latest="$(ls -1t "$EVENTS_DIR"/events_*.jsonl 2>/dev/null | head -n 1)"
  if [ -z "$latest" ]; then
    log "no events file found"
    sleep "$SLEEP_SEC"
    continue
  fi

  set -- $(wc -l < "$latest")
  lines="$1"
  if [ -z "$lines" ]; then
    sleep "$SLEEP_SEC"
    continue
  fi

  if [ "$lines" -gt "$last_lines" ]; then
    echo "$lines" > "$STATE_FILE"
    last_lines="$lines"

    # TODO: filter only faultmon trigger events before bundling.
    log "new events detected; trigger bundle"
    "$BUNDLE_SH" manual "$TAG" --pre "$PRE_SEC" --post "$POST_SEC" || true
  fi

  sleep "$SLEEP_SEC"
done
