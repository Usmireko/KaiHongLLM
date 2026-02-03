#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/xrh/qwen3_os_fault"
VENV="$ROOT/.venv_qwen3"
PY="$VENV/bin/python"
LOG_DIR="$ROOT/storage/logs"
PID_DIR="$ROOT/storage/pids"
INBOX="$ROOT/storage/tcp_inbox"
OUT="$ROOT/storage/tcp_out"
RUNS="$ROOT/storage/runs"
WIN_RUNS="${WIN_RUNS:-}"
TRIGGER_POLL_SEC="${TRIGGER_POLL_SEC:-2}"

# Legacy run-root trigger loop can compete with watcher (double inference / GPU OOM).
# Default: disabled. Enable by exporting ENABLE_TRIGGERD=1 before running this script.
ENABLE_TRIGGERD="${ENABLE_TRIGGERD:-0}"

ensure_dirs() {
  mkdir -p "$LOG_DIR" "$PID_DIR" "$INBOX" "$OUT" "$RUNS"
}

activate_venv() {
  if [ ! -f "$VENV/bin/activate" ]; then
    echo "venv missing: $VENV" >&2
    exit 1
  fi
  # shellcheck disable=SC1090
  source "$VENV/bin/activate"
  export PYTHONUNBUFFERED=1
}

pid_alive() {
  local pidfile="$1"
  local pid
  [ -f "$pidfile" ] || return 1
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null
}

kill_pid() {
  local pid="$1"
  [ -n "$pid" ] || return 0
  kill "$pid" 2>/dev/null || true
  sleep 1
  if kill -0 "$pid" 2>/dev/null; then
    kill -9 "$pid" 2>/dev/null || true
  fi
}

check_port() {
  local port="$1"
  ss -lntp | grep -q ":$port" 2>/dev/null
}

kill_by_port() {
  local port="$1"
  ss -lntp 2>/dev/null | grep ":$port" | while IFS= read -r line; do
    pid="$(printf '%s\n' "$line" | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p')"
    [ -n "$pid" ] && kill_pid "$pid"
  done
}

pre_kill_ports() {
  # WHY: avoid EADDRINUSE log pollution
  local port pidfile pid
  for port in 18080 28081; do
    if [ "$port" = "18080" ]; then
      pidfile="$PID_DIR/ingest.pid"
    else
      pidfile="$PID_DIR/actions.pid"
    fi

    if pid_alive "$pidfile"; then
      pid="$(cat "$pidfile" 2>/dev/null || true)"
      [ -n "$pid" ] && kill_pid "$pid"
      rm -f "$pidfile"
    fi

    if check_port "$port"; then
      kill_by_port "$port"
    fi

    if check_port "$port"; then
      echo "ERROR: port $port still in use" >&2
      ss -lntp | grep ":$port" || true
      exit 1
    fi
  done
}

start_one() {
  local name="$1"
  local pidfile="$2"
  local logfile="$3"
  shift 3
  if pid_alive "$pidfile"; then
    echo "$name already running pid=$(cat "$pidfile")"
    return 0
  fi
  : > "$logfile"
  nohup "$@" >>"$logfile" 2>&1 &
  echo "$!" > "$pidfile"
  echo "$name started pid=$!"
}

start_triggerd() {
  local pidfile="$PID_DIR/triggerd.pid"
  local logfile="$LOG_DIR/triggerd.log"
  if pid_alive "$pidfile"; then
    echo "triggerd already running pid=$(cat "$pidfile")"
    return 0
  fi
  local runroot="${WIN_RUNS:-$RUNS}"
  mkdir -p "$runroot"
  : > "$logfile"
  local cmd
  cmd=$(cat <<EOF
set -euo pipefail
ROOT="$ROOT"
PY="$PY"
RUNROOT="$runroot"
INFER="/home/xrh/qwen3_os_fault/closed_loop_infer_run.py"
if [ ! -f "\$INFER" ]; then
  INFER="/home/xrh/qwen3_os_fault/closed_loop_infer_run.py"
fi
POLL="$TRIGGER_POLL_SEC"
timeline_update() {
  local file="\$1"
  local run_dir="\$2"
  local run_id="\$3"
  local t_trigger="\$4"
  local t_infer_start="\$5"
  local t_infer_end="\$6"
  local t_actions_end="\$7"
  local status="\$8"
  local err="\$9"
  TL_FILE="\$file" TL_RUN_DIR="\$run_dir" TL_RUN_ID="\$run_id" TL_TRIGGER="\$t_trigger" TL_INFER_START="\$t_infer_start" TL_INFER_END="\$t_infer_end" TL_ACTIONS_END="\$t_actions_end" TL_STATUS="\$status" TL_ERROR="\$err" "\$PY" - <<'PY'
import json
import os
from datetime import datetime, timezone

def now_utc():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

def parse_ts(s):
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1]
        return datetime.fromisoformat(s)
    except Exception:
        return None

def set_if_empty(d, k, v):
    if v and not d.get(k):
        d[k] = v

def set_overwrite(d, k, v):
    if v:
        d[k] = v

path = os.environ.get("TL_FILE")
if not path:
    raise SystemExit(0)

data = {}
if os.path.exists(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}

set_if_empty(data, "run_dir", os.environ.get("TL_RUN_DIR"))
set_if_empty(data, "run_id", os.environ.get("TL_RUN_ID"))
set_if_empty(data, "t_trigger_seen_utc", os.environ.get("TL_TRIGGER"))
set_if_empty(data, "t_infer_start_utc", os.environ.get("TL_INFER_START"))
set_if_empty(data, "t_infer_end_utc", os.environ.get("TL_INFER_END"))
set_if_empty(data, "t_actions_end_utc", os.environ.get("TL_ACTIONS_END"))
set_overwrite(data, "status", os.environ.get("TL_STATUS"))
set_overwrite(data, "error", os.environ.get("TL_ERROR"))

t_inject = parse_ts(data.get("t_inject_start_utc"))
t_trigger = parse_ts(data.get("t_trigger_seen_utc"))
t_infer_start = parse_ts(data.get("t_infer_start_utc"))
t_infer_end = parse_ts(data.get("t_infer_end_utc"))
t_actions_start = parse_ts(data.get("t_actions_start_utc"))
t_actions_end = parse_ts(data.get("t_actions_end_utc"))

if t_inject and t_trigger:
    data["dur_trigger_s"] = int((t_trigger - t_inject).total_seconds())
if t_infer_start and t_infer_end:
    data["dur_infer_s"] = int((t_infer_end - t_infer_start).total_seconds())
if t_actions_end and not t_actions_start and t_infer_end:
    data["t_actions_start_utc"] = data.get("t_infer_end_utc")
    t_actions_start = t_infer_end
if t_actions_start and t_actions_end:
    data["dur_actions_s"] = int((t_actions_end - t_actions_start).total_seconds())
if t_trigger and t_actions_end:
    data["dur_total_s"] = int((t_actions_end - t_trigger).total_seconds())

tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
os.replace(tmp, path)
PY
}
while true; do
  if [ -d "\$RUNROOT" ]; then
    for d in "\$RUNROOT"/*; do
      [ -d "\$d" ] || continue
      out="\$d/_server_out"
      mark="\$out/.infer_done"
      run_id="\$(basename "\$d")"
      timeline="\$out/timeline.json"
      if [ -f "\$mark" ]; then
        if [ -f "\$out/actions_exec_result.json" ]; then
          timeline_update "\$timeline" "\$d" "\$run_id" "" "" "" "\$(date -u +%Y-%m-%dT%H:%M:%SZ)" "ok" ""
        fi
        continue
      fi
      if [ -f "\$mark" ]; then
        continue
      fi
      mkdir -p "\$out"
      echo "[triggerd] run_dir=\$d infer=\$INFER start"
      timeline_update "\$timeline" "\$d" "\$run_id" "\$(date -u +%Y-%m-%dT%H:%M:%SZ)" "" "" "" "" ""
      if [ ! -f "\$INFER" ]; then
        echo "error" > "\$mark"
        timeline_update "\$timeline" "\$d" "\$run_id" "" "" "\$(date -u +%Y-%m-%dT%H:%M:%SZ)" "" "error" "infer_script_missing"
        continue
      fi
      timeline_update "\$timeline" "\$d" "\$run_id" "" "\$(date -u +%Y-%m-%dT%H:%M:%SZ)" "" "" "" ""
      if "\$PY" "\$INFER" --run_dir "\$d" --out_dir "\$out"; then
        echo "ok" > "\$mark"
        timeline_update "\$timeline" "\$d" "\$run_id" "" "" "\$(date -u +%Y-%m-%dT%H:%M:%SZ)" "" "ok" ""
      else
        echo "error" > "\$mark"
        timeline_update "\$timeline" "\$d" "\$run_id" "" "" "\$(date -u +%Y-%m-%dT%H:%M:%SZ)" "" "error" "infer_failed"
      fi
      if [ -f "\$out/actions_exec_result.json" ]; then
        timeline_update "\$timeline" "\$d" "\$run_id" "" "" "" "\$(date -u +%Y-%m-%dT%H:%M:%SZ)" "" ""
      fi
    done
  fi
  sleep "\$POLL"
done
EOF
)
  nohup bash -lc "$cmd" >>"$logfile" 2>&1 &
  echo "$!" > "$pidfile"
  echo "triggerd started pid=$!"
}

stop_one() {
  local name="$1"
  local pidfile="$2"
  if ! pid_alive "$pidfile"; then
    echo "$name not running"
    rm -f "$pidfile"
    return 0
  fi
  local pid
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  echo "stopping $name pid=$pid"
  kill_pid "$pid"
  rm -f "$pidfile"
}

status_one() {
  local name="$1"
  local pidfile="$2"
  local port="$3"
  local logfile="$4"
  local status="down"
  if pid_alive "$pidfile"; then
    status="up"
  fi
  local listen="no"
  if [ -n "$port" ] && check_port "$port"; then
    listen="yes"
  fi
  echo "$name: pid=$(cat "$pidfile" 2>/dev/null || echo '-') alive=$status listen=$listen"
  tail -n 5 "$logfile" 2>/dev/null || true
}

start_all() {
  ensure_dirs
  activate_venv
  pre_kill_ports
  start_one "ingest" "$PID_DIR/ingest.pid" "$LOG_DIR/ingest.log" "$PY" "$ROOT/server_B/tcp/tcp_ingest_server.py" --host 0.0.0.0 --port 18080 --inbox "$INBOX"
  start_one "actions" "$PID_DIR/actions.pid" "$LOG_DIR/actions.log" "$PY" "$ROOT/server_B/tcp/tcp_actions_server.py" --host 0.0.0.0 --port 28081 --out "$OUT"
  start_one "watcher" "$PID_DIR/watcher.pid" "$LOG_DIR/watcher.log" "$PY" "$ROOT/server_B/tcp/watch_and_infer.py" --inbox "$INBOX" --out "$OUT" --runs_root "$RUNS" --poll_sec 2
  if [ "${ENABLE_TRIGGERD}" = "1" ]; then
    start_triggerd
  else
    echo "triggerd disabled (ENABLE_TRIGGERD=${ENABLE_TRIGGERD})"
  fi

  sleep 1
  local ok=1
  if ! pid_alive "$PID_DIR/ingest.pid"; then ok=0; fi
  if ! pid_alive "$PID_DIR/actions.pid"; then ok=0; fi
  if ! pid_alive "$PID_DIR/watcher.pid"; then ok=0; fi
  if ! check_port 18080 || ! check_port 28081; then ok=0; fi
  if [ "$ok" -ne 1 ]; then
    echo "start failed, logs tail:"
    tail -n 50 "$LOG_DIR/ingest.log" 2>/dev/null || true
    tail -n 50 "$LOG_DIR/actions.log" 2>/dev/null || true
    tail -n 50 "$LOG_DIR/watcher.log" 2>/dev/null || true
    exit 1
  fi

  ss -lntp | grep ":18080" || true
  ss -lntp | grep ":28081" || true

  if ! pid_alive "$PID_DIR/ingest.pid" || ! pid_alive "$PID_DIR/actions.pid"; then
    echo "start failed: pid not alive" >&2
    exit 1
  fi
  if ! check_port 18080 || ! check_port 28081; then
    echo "start failed: port not listening" >&2
    exit 1
  fi

  echo "ports ok: 18080/28081, pids alive"
}

stop_all() {
  stop_one "triggerd" "$PID_DIR/triggerd.pid"
  stop_one "watcher" "$PID_DIR/watcher.pid"
  stop_one "actions" "$PID_DIR/actions.pid"
  stop_one "ingest" "$PID_DIR/ingest.pid"
}

status_all() {
  status_one "ingest" "$PID_DIR/ingest.pid" "18080" "$LOG_DIR/ingest.log"
  status_one "actions" "$PID_DIR/actions.pid" "28081" "$LOG_DIR/actions.log"
  status_one "watcher" "$PID_DIR/watcher.pid" "" "$LOG_DIR/watcher.log"
  if [ "${ENABLE_TRIGGERD}" = "1" ]; then
    status_one "triggerd" "$PID_DIR/triggerd.pid" "" "$LOG_DIR/triggerd.log"
  else
    echo "triggerd:"
    echo "disabled (ENABLE_TRIGGERD=${ENABLE_TRIGGERD})"
  fi
}

logs_all() {
  tail -n 80 "$LOG_DIR/ingest.log" 2>/dev/null || true
  tail -n 80 "$LOG_DIR/actions.log" 2>/dev/null || true
  tail -n 80 "$LOG_DIR/watcher.log" 2>/dev/null || true
  tail -n 80 "$LOG_DIR/triggerd.log" 2>/dev/null || true
}

case "${1:-}" in
  start) start_all ;;
  stop) stop_all ;;
  restart) stop_all; start_all ;;
  status) status_all ;;
  logs) logs_all ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|logs}" >&2
    exit 2
    ;;
 esac
