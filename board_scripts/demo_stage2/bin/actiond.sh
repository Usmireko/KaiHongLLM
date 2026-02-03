#!/bin/sh

usage() {
  echo "Usage: $0 run --actions <path>" >&2
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

if [ "$1" != "run" ]; then
  usage
fi

if [ "$2" != "--actions" ]; then
  usage
fi

ACTIONS_FILE="$3"
if [ -z "$ACTIONS_FILE" ] || [ ! -f "$ACTIONS_FILE" ]; then
  echo "ERROR: actions file missing: $ACTIONS_FILE" >&2
  exit 1
fi

LOG_DIR="/data/faultmon/logs"
OUTBOX_DIR="/data/faultmon/outbox"
mkdir -p "$LOG_DIR"
mkdir -p "$OUTBOX_DIR"

RUN_ID="$(date +%Y%m%d_%H%M%S 2>/dev/null || echo "actions_$(date +%s)")"
LOG_FILE="$LOG_DIR/actions_exec.log"
TMP_PREFIX="$LOG_DIR/actions_${RUN_ID}"

echo "### ACTIONS_START run_id=$RUN_ID ts_ms=$(now_ms)" >> "$LOG_FILE"

idx=0
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    ""|\#*)
      continue
      ;;
  esac

  cmd="$(printf '%s' "$line" | sed 's/[[:space:]]*$//')"
  [ -z "$cmd" ] && continue
  prefix="${cmd%% *}"

  allowed=0
  for p in dmesg cat ps top head tail grep; do
    if [ "$prefix" = "$p" ]; then
      allowed=1
      break
    fi
  done

  start_ms="$(now_ms)"
  out_path="${TMP_PREFIX}_out_${idx}.txt"
  err_path="${TMP_PREFIX}_err_${idx}.txt"

  if [ "$allowed" -eq 1 ]; then
    if command -v timeout >/dev/null 2>&1; then
      timeout 20 sh -c "$cmd" >"$out_path" 2>"$err_path"
    else
      sh -c "$cmd" >"$out_path" 2>"$err_path"
    fi
    ec=$?
  else
    echo "blocked_prefix=$prefix" >"$err_path"
    ec=126
  fi
  end_ms="$(now_ms)"

  echo "### ACTION idx=$idx start_ts_ms=$start_ms end_ts_ms=$end_ms exit_code=$ec cmd=$cmd" >> "$LOG_FILE"
  echo "--- stdout_tail ---" >> "$LOG_FILE"
  if [ -f "$out_path" ]; then
    tail -n 40 "$out_path" >> "$LOG_FILE"
  fi
  echo "--- stderr_tail ---" >> "$LOG_FILE"
  if [ -f "$err_path" ]; then
    tail -n 40 "$err_path" >> "$LOG_FILE"
  fi
  echo "### ACTION_END idx=$idx" >> "$LOG_FILE"

  idx=$((idx + 1))
done < "$ACTIONS_FILE"

echo "### ACTIONS_END run_id=$RUN_ID ts_ms=$(now_ms)" >> "$LOG_FILE"

BUNDLE_DIR="$OUTBOX_DIR/action_result_${RUN_ID}"
mkdir -p "$BUNDLE_DIR"

cat > "$BUNDLE_DIR/_run_meta.json" <<EOF
{
  "run_id": "${RUN_ID}",
  "scenario_tag": "action_result",
  "fault_type": "action_result",
  "run_window_board_ms_start": $(now_ms),
  "run_window_board_ms_end": $(now_ms),
  "run_window_source": "actiond"
}
EOF

cp "$LOG_FILE" "$BUNDLE_DIR/actions_exec.log" 2>/dev/null || true

for f in ${TMP_PREFIX}_out_*.txt ${TMP_PREFIX}_err_*.txt; do
  [ -f "$f" ] || continue
  cp "$f" "$BUNDLE_DIR/" 2>/dev/null || true
done

BUNDLE_PATH="$OUTBOX_DIR/action_result_bundle_${RUN_ID}.tar.gz"
if tar -czf "$BUNDLE_PATH" -C "$OUTBOX_DIR" "action_result_${RUN_ID}"; then
  echo "bundle_path=$BUNDLE_PATH"
  echo "run_id=$RUN_ID"
  exit 0
fi

echo "ERROR: failed to create bundle at $BUNDLE_PATH" >&2
exit 1
