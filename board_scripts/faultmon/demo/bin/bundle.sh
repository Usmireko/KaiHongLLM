#!/bin/sh

usage() {
  echo "Usage: $0 manual <tag> [--pre N] [--post N]" >&2
  exit 1
}

now_ms() {
  val="$(date +%s%3N 2>/dev/null || true)"
  case "$val" in
    *N*|*%*|"")
      val=""
      ;;
  esac
  if [ -z "$val" ]; then
    s="$(date +%s 2>/dev/null || echo 0)"
    val=$((s * 1000))
  fi
  echo "$val"
}

filter_csv_by_ts() {
  in_file="$1"
  out_file="$2"
  start_ms="$3"
  end_ms="$4"
  first=1

  [ -f "$in_file" ] || return 0

  : > "$out_file"
  while IFS= read -r line || [ -n "$line" ]; do
    if [ "$first" -eq 1 ]; then
      printf '%s\n' "$line" >> "$out_file"
      first=0
      continue
    fi
    ts="$(printf '%s' "$line" | cut -d, -f1)"
    case "$ts" in
      ''|*[!0-9]*)
        continue
        ;;
    esac
    if [ "$ts" -ge "$start_ms" ] && [ "$ts" -le "$end_ms" ]; then
      printf '%s\n' "$line" >> "$out_file"
    fi
  done < "$in_file"
}

filter_jsonl_by_ts() {
  in_file="$1"
  out_file="$2"
  start_ms="$3"
  end_ms="$4"

  [ -f "$in_file" ] || return 0

  : > "$out_file"
  while IFS= read -r line || [ -n "$line" ]; do
    ts="$(printf '%s' "$line" | sed -n 's/.*"ts":[ ]*\([0-9][0-9]*\).*/\1/p')"
    case "$ts" in
      ''|*[!0-9]*)
        continue
        ;;
    esac
    if [ "$ts" -ge "$start_ms" ] && [ "$ts" -le "$end_ms" ]; then
      printf '%s\n' "$line" >> "$out_file"
    fi
  done < "$in_file"
}

if [ "$1" != "manual" ]; then
  usage
fi

shift

if [ -z "$1" ]; then
  usage
fi

TAG="$1"
shift

PRE_SEC=10
POST_SEC=10
while [ "$#" -gt 0 ]; do
  case "$1" in
    --pre)
      PRE_SEC="$2"
      shift 2
      ;;
    --post)
      POST_SEC="$2"
      shift 2
      ;;
    *)
      echo "Unknown arg: $1" >&2
      exit 1
      ;;
  esac
done

T0_MS="$(now_ms)"
PRE_MS=$((PRE_SEC * 1000))
POST_MS=$((POST_SEC * 1000))
WIN_START_MS=$((T0_MS - PRE_MS))
WIN_END_MS=$((T0_MS + POST_MS))

RUN_ID="$(date +%Y%m%d_%H%M%S 2>/dev/null || echo "run_${T0_MS}")"
RUN_DIR="/data/faultmon/outbox/${RUN_ID}"
if [ -e "$RUN_DIR" ]; then
  RUN_ID="${RUN_ID}_${T0_MS}"
  RUN_DIR="/data/faultmon/outbox/${RUN_ID}"
fi

MET_DIR="$RUN_DIR/metrics"
EV_DIR="$RUN_DIR/events"
PROC_DIR="$RUN_DIR/procs"

mkdir -p "$MET_DIR"
mkdir -p "$EV_DIR"
mkdir -p "$PROC_DIR"

for f in /data/faultmon/metrics/sys_*.csv; do
  [ -f "$f" ] || continue
  out="$MET_DIR/$(basename "$f")"
  filter_csv_by_ts "$f" "$out" "$WIN_START_MS" "$WIN_END_MS"
done

for f in /data/faultmon/events/events_*.jsonl; do
  [ -f "$f" ] || continue
  out="$EV_DIR/$(basename "$f")"
  filter_jsonl_by_ts "$f" "$out" "$WIN_START_MS" "$WIN_END_MS"
done

PROC_OUT="$PROC_DIR/procs_${T0_MS}.txt"
if ps -o pid,ppid,stat,rss,comm > "$PROC_OUT" 2>/dev/null; then
  :
else
  ps > "$PROC_OUT" 2>/dev/null || true
fi

cat > "$RUN_DIR/_run_meta.json" <<EOF
{
  "run_id": "${RUN_ID}",
  "scenario_tag": "${TAG}",
  "fault_type": "manual",
  "run_start": ${T0_MS},
  "run_end": ${T0_MS},
  "run_window_host_epoch_ms_start": ${WIN_START_MS},
  "run_window_host_epoch_ms_end": ${WIN_END_MS},
  "run_window_board_ms_start": ${WIN_START_MS},
  "run_window_board_ms_end": ${WIN_END_MS},
  "run_window_source": "manual"
}
EOF

cat > "$RUN_DIR/bundle_manifest.json" <<EOF
{
  "run_id": "${RUN_ID}",
  "scenario_tag": "${TAG}",
  "fault_type": "manual",
  "created_at_ms": ${T0_MS},
  "window_start_ms": ${WIN_START_MS},
  "window_end_ms": ${WIN_END_MS}
}
EOF

BUNDLE_PATH="/data/faultmon/outbox/bundle_${RUN_ID}.tar.gz"
if tar -czf "$BUNDLE_PATH" -C "/data/faultmon/outbox" "$RUN_ID"; then
  echo "bundle_path=$BUNDLE_PATH"
  echo "run_id=$RUN_ID"
  exit 0
fi

echo "ERROR: failed to create bundle at $BUNDLE_PATH" >&2
exit 1
