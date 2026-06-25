#!/usr/bin/env bash
# 3CM-R1 compatibility wrapper, candidate/shadow only.
# Starts the Stage2 demo TCP services used by the old two-window demo.
# It does not execute Action R1, recovery actions, HDC, board commands, or adapter mutation.

set -eu

ROOT="${QWEN3_OS_FAULT_ROOT:-/home/xrh/qwen3_os_fault}"
VENV="${QWEN3_VENV:-$ROOT/.venv_qwen3}"
PY="${PYTHON:-$VENV/bin/python}"
HOST="${QWEN3_LISTEN_HOST:-0.0.0.0}"
INGEST_PORT="${QWEN3_INGEST_PORT:-18080}"
ACTIONS_PORT="${QWEN3_ACTIONS_PORT:-28081}"
LOG_DIR="${QWEN3_LOG_DIR:-$ROOT/storage/logs}"
PID_DIR="${QWEN3_PID_DIR:-$ROOT/storage/pids}"
INBOX="${QWEN3_TCP_INBOX:-$ROOT/storage/tcp_inbox}"
OUT="${QWEN3_TCP_OUT:-$ROOT/storage/tcp_out}"
RUNS="${QWEN3_RUNS_ROOT:-$ROOT/storage/runs}"
POLL_SEC="${QWEN3_WATCH_POLL_SEC:-2}"

SERVER_SRC="$ROOT/closed_loop_demo/server/src"
if [ ! -d "$SERVER_SRC/server_B" ]; then
  SERVER_SRC="$ROOT"
fi

INGEST_SCRIPT="$SERVER_SRC/server_B/tcp/tcp_ingest_server.py"
ACTIONS_SCRIPT="$SERVER_SRC/server_B/tcp/tcp_actions_server.py"
WATCHER_SCRIPT="$SERVER_SRC/server_B/tcp/watch_and_infer.py"

ensure_dirs() {
  mkdir -p "$LOG_DIR" "$PID_DIR" "$INBOX" "$OUT" "$RUNS"
}

require_files() {
  if [ ! -x "$PY" ]; then
    echo "ERROR: python runtime missing or not executable: $PY" >&2
    return 1
  fi
  for f in "$INGEST_SCRIPT" "$ACTIONS_SCRIPT" "$WATCHER_SCRIPT" "$SERVER_SRC/server_B/orchestrator/run_closed_loop.py" "$SERVER_SRC/server_B/orchestrator/action_r1_strategy_library.py" "$SERVER_SRC/closed_loop_infer_run.py" "$SERVER_SRC/infer_qwen3_fault_2stage.py"; do
    if [ ! -f "$f" ]; then
      echo "ERROR: required runtime file missing: $f" >&2
      return 1
    fi
  done
}

candidate_shadow_mode() {
  [ "${WK_3CM_CANDIDATE_MODE:-}" = "1" ] || [ "${WK_3CM_ACTION_SUGGESTION_ONLY:-}" = "1" ]
}

clear_candidate_action_downlinks() {
  candidate_shadow_mode || return 0
  for f in "$OUT"/*/latest_actions_device.txt; do
    [ -f "$f" ] || continue
    rm -f "$f"
  done
  for f in "$OUT"/*/latest_action_run_id.txt; do
    [ -f "$f" ] || continue
    rm -f "$f"
  done
}

pid_alive() {
  pidfile="$1"
  [ -f "$pidfile" ] || return 1
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null
}

pid_matches() {
  pidfile="$1"
  expected="$2"
  [ -f "$pidfile" ] || return 1
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  [ -n "$pid" ] || return 1
  [ -r "/proc/$pid/cmdline" ] || return 1
  cmdline="$(tr '\000' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
  case "$cmdline" in
    *"$expected"*) return 0 ;;
    *) return 1 ;;
  esac
}

check_port() {
  port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -lnt 2>/dev/null | grep -q ":$port"
  elif command -v netstat >/dev/null 2>&1; then
    netstat -lnt 2>/dev/null | grep -q ":$port"
  else
    return 1
  fi
}

start_one() {
  name="$1"
  pidfile="$2"
  logfile="$3"
  expected="$4"
  shift 4
  if pid_alive "$pidfile" && pid_matches "$pidfile" "$expected"; then
    echo "$name already running pid=$(cat "$pidfile")"
    return 0
  fi
  if pid_alive "$pidfile"; then
    echo "$name stale pidfile points at unrelated process pid=$(cat "$pidfile"); removing pidfile"
    rm -f "$pidfile"
  fi
  : > "$logfile"
  nohup "$@" >>"$logfile" 2>&1 &
  echo "$!" > "$pidfile"
  echo "$name started pid=$!"
}

stop_one() {
  name="$1"
  pidfile="$2"
  expected="$3"
  if ! pid_alive "$pidfile"; then
    echo "$name not running"
    rm -f "$pidfile"
    return 0
  fi
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  if ! pid_matches "$pidfile" "$expected"; then
    echo "$name stale pidfile points at unrelated process pid=$pid; removing pidfile"
    rm -f "$pidfile"
    return 0
  fi
  echo "stopping $name pid=$pid"
  kill "$pid" 2>/dev/null || true
  i=0
  while kill -0 "$pid" 2>/dev/null; do
    i=$((i + 1))
    if [ "$i" -gt 5 ]; then
      kill -9 "$pid" 2>/dev/null || true
      break
    fi
    sleep 1
  done
  rm -f "$pidfile"
}

status_one() {
  name="$1"
  pidfile="$2"
  port="$3"
  logfile="$4"
  expected="$5"
  alive="down"
  listen="no"
  if pid_alive "$pidfile" && pid_matches "$pidfile" "$expected"; then
    alive="up"
  fi
  if [ -n "$port" ] && check_port "$port"; then
    listen="yes"
  fi
  echo "$name: pid=$(cat "$pidfile" 2>/dev/null || echo '-') alive=$alive listen=$listen port=:$port"
  tail -n 3 "$logfile" 2>/dev/null || true
}

start_all() {
  ensure_dirs
  require_files
  clear_candidate_action_downlinks
  export PYTHONUNBUFFERED=1
  export WK_3CM_ACTION_SUGGESTION_ONLY="${WK_3CM_ACTION_SUGGESTION_ONLY:-1}"
  start_one "ingest" "$PID_DIR/ingest.pid" "$LOG_DIR/ingest.log" "$INGEST_SCRIPT" "$PY" "$INGEST_SCRIPT" --host "$HOST" --port "$INGEST_PORT" --inbox "$INBOX"
  start_one "actions" "$PID_DIR/actions.pid" "$LOG_DIR/actions.log" "$ACTIONS_SCRIPT" "$PY" "$ACTIONS_SCRIPT" --host "$HOST" --port "$ACTIONS_PORT" --out "$OUT"
  start_one "watcher" "$PID_DIR/watcher.pid" "$LOG_DIR/watcher.log" "$WATCHER_SCRIPT" "$PY" "$WATCHER_SCRIPT" --inbox "$INBOX" --out "$OUT" --runs_root "$RUNS" --poll_sec "$POLL_SEC"
  sleep 1
  status_all
  if ! pid_matches "$PID_DIR/ingest.pid" "$INGEST_SCRIPT" || ! pid_matches "$PID_DIR/actions.pid" "$ACTIONS_SCRIPT" || ! pid_matches "$PID_DIR/watcher.pid" "$WATCHER_SCRIPT"; then
    echo "ERROR: at least one service failed to start" >&2
    exit 1
  fi
}

stop_all() {
  stop_one "watcher" "$PID_DIR/watcher.pid" "$WATCHER_SCRIPT"
  stop_one "actions" "$PID_DIR/actions.pid" "$ACTIONS_SCRIPT"
  stop_one "ingest" "$PID_DIR/ingest.pid" "$INGEST_SCRIPT"
}

status_all() {
  ensure_dirs
  status_one "ingest" "$PID_DIR/ingest.pid" "$INGEST_PORT" "$LOG_DIR/ingest.log" "$INGEST_SCRIPT"
  status_one "actions" "$PID_DIR/actions.pid" "$ACTIONS_PORT" "$LOG_DIR/actions.log" "$ACTIONS_SCRIPT"
  status_one "watcher" "$PID_DIR/watcher.pid" "" "$LOG_DIR/watcher.log" "$WATCHER_SCRIPT"
}

logs_all() {
  tail -n 80 "$LOG_DIR/ingest.log" 2>/dev/null || true
  tail -n 80 "$LOG_DIR/actions.log" 2>/dev/null || true
  tail -n 80 "$LOG_DIR/watcher.log" 2>/dev/null || true
}

case "${1:-}" in
  start) start_all ;;
  stop) stop_all ;;
  restart) stop_all; start_all ;;
  status) status_all ;;
  logs) logs_all ;;
  dry-run|self-test) ensure_dirs; require_files; clear_candidate_action_downlinks; echo "DRY_RUN_OK root=$ROOT ingest=:$INGEST_PORT actions=:$ACTIONS_PORT watcher=$WATCHER_SCRIPT" ;;
  *) echo "Usage: $0 {start|stop|restart|status|logs|dry-run|self-test}" >&2; exit 2 ;;
esac
