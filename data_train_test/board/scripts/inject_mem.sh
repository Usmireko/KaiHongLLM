#!/system/bin/sh

BASE_DIR=/data/faultmon/demo_stage2
PID_DIR="$BASE_DIR/pids"
LOG_DIR="$BASE_DIR/logs"
PIDFILE="$PID_DIR/inject_mem.pid"
LOGFILE="$LOG_DIR/inject_mem.log"
MEM_BIN=/data/local/tmp/out_static_arm64/memory_leak_demo

mkdir -p "$PID_DIR" "$LOG_DIR"

is_running() {
  if [ -f "$PIDFILE" ]; then
    pid="$(cat "$PIDFILE" 2>/dev/null)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
  fi
  return 1
}

case "$1" in
  start)
    if is_running; then
      echo "already_running pid=$(cat "$PIDFILE" 2>/dev/null)"
      exit 0
    fi
    if [ ! -x "$MEM_BIN" ]; then
      echo "error:mem_bin_missing $MEM_BIN" >&2
      exit 2
    fi
    "$MEM_BIN" >> "$LOGFILE" 2>&1 &
    echo $! > "$PIDFILE"
    echo "started pid=$(cat "$PIDFILE")"
    ;;
  stop)
    if is_running; then
      pid="$(cat "$PIDFILE" 2>/dev/null)"
      kill "$pid" 2>/dev/null || true
      /data/local/tmp/busybox sleep 1
      if kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid" 2>/dev/null || true
      fi
    fi
    rm -f "$PIDFILE" 2>/dev/null || true
    echo "stopped"
    ;;
  status)
    if is_running; then
      echo "alive pid=$(cat "$PIDFILE" 2>/dev/null)"
    else
      echo "not_running"
    fi
    ;;
  logs)
    /data/local/tmp/busybox tail -n 50 "$LOGFILE" 2>/dev/null || true
    ;;
  *)
    echo "usage: $0 start|stop|status|logs" >&2
    exit 2
    ;;
 esac