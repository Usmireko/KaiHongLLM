#!/system/bin/sh
# demo_stage2 injection: memory leak
# BusyBox/Toybox compatible: no awk/tr required

BASE_DIR=/data/faultmon/demo_stage2
PID_DIR="$BASE_DIR/pids"
LOG_DIR="$BASE_DIR/logs"
PIDFILE="$PID_DIR/inject_mem.pid"
LOGFILE="$LOG_DIR/inject_mem.log"
MEM_BIN=/data/local/tmp/out_static_arm64/memory_leak_demo
BB=/data/local/tmp/busybox

mkdir -p "$PID_DIR" "$LOG_DIR"

list_pids() {
  if [ -f "$PIDFILE" ]; then
    $BB cat "$PIDFILE" 2>/dev/null || true
  fi
}

alive_pids() {
  if [ -f "$PIDFILE" ]; then
    while IFS= read -r pid; do
      [ -n "$pid" ] || continue
      if kill -0 "$pid" 2>/dev/null; then
        echo "$pid"
      fi
    done < "$PIDFILE"
  fi
}

any_alive() {
  pids="$(alive_pids)"
  [ -n "$pids" ]
}

kill_all() {
  if [ -f "$PIDFILE" ]; then
    while IFS= read -r pid; do
      [ -n "$pid" ] || continue
      kill "$pid" 2>/dev/null || true
    done < "$PIDFILE"
    $BB sleep 1
    while IFS= read -r pid; do
      [ -n "$pid" ] || continue
      if kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid" 2>/dev/null || true
      fi
    done < "$PIDFILE"
  fi
}

parse_n() {
  # default 1
  N=1
  # accept: start 4 | start --n 4 | start --n=4
  while [ $# -gt 0 ]; do
    case "$1" in
      --n)
        shift
        N="$1"
        ;;
      --n=*)
        N="${1#*=}"
        ;;
      [0-9]*)
        N="$1"
        ;;
      *)
        ;;
    esac
    shift
  done
  case "$N" in
    ""|*[!0-9]*)
      N=1
      ;;
  esac
  if [ "$N" -lt 1 ]; then N=1; fi
}

case "$1" in
  start)
    shift
    parse_n "$@"

    if any_alive; then
      echo "already_running pids:"
      list_pids
      exit 0
    fi

    if [ ! -x "$MEM_BIN" ]; then
      echo "error:mem_bin_missing $MEM_BIN" >&2
      exit 2
    fi

    # reset pidfile
    : > "$PIDFILE" 2>/dev/null || true

    i=1
    while [ "$i" -le "$N" ]; do
      "$MEM_BIN" >> "$LOGFILE" 2>&1 &
      pid=$!
      echo "$pid" >> "$PIDFILE"
      echo "started[$i/$N] pid=$pid"
      i=$((i+1))
    done
    ;;

  stop)
    if any_alive; then
      kill_all
    fi
    $BB rm -f "$PIDFILE" 2>/dev/null || true
    echo "stopped"
    ;;

  status)
    if any_alive; then
      echo "alive pids:"
      alive_pids
    else
      echo "not_running"
    fi
    ;;

  logs)
    $BB tail -n 80 "$LOGFILE" 2>/dev/null || true
    ;;

  *)
    echo "usage: $0 start [--n N]|stop|status|logs" >&2
    echo "  example: $0 start --n 4" >&2
    exit 2
    ;;
esac
