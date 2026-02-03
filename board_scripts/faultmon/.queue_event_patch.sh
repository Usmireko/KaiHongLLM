queue_event() {
  # be safe under `set -u`
  set +u
  local ts="$1" source="$2" level="$3" component="$4" pid="$5" msg="$6" tag="$7"

  # fallback paths
  [ -n "$STATE_DIR" ] || STATE_DIR="/data/faultmon/state"
  [ -n "$EVENT_PIPE" ] || EVENT_PIPE="$STATE_DIR/event.pipe"

  # ensure fifo exists (daemon should be reading; otherwise this blocks)
  [ -p "$EVENT_PIPE" ] || mkfifo -m 0644 "$EVENT_PIPE" 2>/dev/null || true

  # write one record to fifo; event_sink_* will JSONify and persist
  printf '%s\001%s\001%s\001%s\001%s\001%s\001%s\n' \
    "$ts" "$source" "$level" "$component" "$pid" "$msg" "$tag" > "$EVENT_PIPE"
}
