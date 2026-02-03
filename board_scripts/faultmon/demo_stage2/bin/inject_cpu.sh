#!/system/bin/sh
set -u

BB=/data/local/tmp/busybox
BASE=/data/faultmon/demo_stage2
PID_DIR="$BASE/pids"
PIDFILE="$PID_DIR/inject_cpu.pids"

mkdir -p "$PID_DIR" 2>/dev/null || true

usage() {
  echo "usage: $0 start [--workers N] | stop | status" >&2
  echo "default workers = online_cpu * 2 (min 4)" >&2
}

is_digits() { case "${1:-}" in ''|*[!0-9]*) return 1;; *) return 0;; esac; }

get_ncpu() {
  # count /sys/devices/system/cpu/cpu0..N
  n="$("$BB" ls -d /sys/devices/system/cpu/cpu[0-9]* 2>/dev/null | "$BB" wc -l 2>/dev/null)"
  n="$("$BB" echo "$n" | "$BB" sed 's/[^0-9]//g')"
  [ -z "$n" ] && n=1
  echo "$n"
}

alive_pids() {
  [ -f "$PIDFILE" ] || return 1
  ok=1
  while read -r p; do
    [ -z "$p" ] && continue
    case "$p" in *[!0-9]*) continue;; esac
    if "$BB" kill -0 "$p" 2>/dev/null; then
      echo "$p"
      ok=0
    fi
  done < "$PIDFILE"
  return "$ok"
}

start_workers() {
  workers="$1"

  # already running?
  if alive_pids >/dev/null 2>&1; then
    echo "already_running pids=$(alive_pids | "$BB" tr '\n' ' ' 2>/dev/null || alive_pids)"
    return 0
  fi

  : > "$PIDFILE" 2>/dev/null || true

  i=0
  while [ "$i" -lt "$workers" ]; do
    # compiled busybox applet; very CPU-hungry
    "$BB" yes >/dev/null 2>&1 &
    echo "$!" >> "$PIDFILE"
    i=$((i + 1))
  done

  echo "started workers=$workers pids_count=$("$BB" wc -l < "$PIDFILE" 2>/dev/null | "$BB" sed 's/[^0-9]//g')"
  return 0
}

stop_workers() {
  if [ ! -f "$PIDFILE" ]; then
    echo "not_running"
    return 0
  fi

  # try TERM then KILL
  while read -r p; do
    [ -z "$p" ] && continue
    case "$p" in *[!0-9]*) continue;; esac
    "$BB" kill "$p" 2>/dev/null || true
  done < "$PIDFILE"

  "$BB" sleep 1

  while read -r p; do
    [ -z "$p" ] && continue
    case "$p" in *[!0-9]*) continue;; esac
    "$BB" kill -9 "$p" 2>/dev/null || true
  done < "$PIDFILE"

  "$BB" rm -f "$PIDFILE" 2>/dev/null || true
  echo "stopped"
  return 0
}

cmd="${1:-}"
shift 2>/dev/null || true

case "$cmd" in
  start)
    workers=""
    while [ "${1:-}" != "" ]; do
      case "$1" in
        --workers)
          workers="${2:-}"
          shift 2>/dev/null || true
          ;;
        --workers=*)
          workers="${1#*=}"
          ;;
        -h|--help|help)
          usage
          exit 0
          ;;
        *)
          # ignore unknown for compatibility
          ;;
      esac
      shift 2>/dev/null || true
    done

    if [ -z "$workers" ]; then
      ncpu="$(get_ncpu)"
      workers=$((ncpu * 2))
      [ "$workers" -lt 4 ] && workers=4
    fi

    if ! is_digits "$workers"; then
      echo "invalid --workers '$workers'" >&2
      usage
      exit 2
    fi

    start_workers "$workers"
    ;;
  stop)
    stop_workers
    ;;
  status)
    if alive_pids >/dev/null 2>&1; then
      cnt="$(alive_pids | "$BB" wc -l 2>/dev/null | "$BB" sed 's/[^0-9]//g')"
      echo "alive workers=$cnt"
      exit 0
    fi
    echo "not_running"
    exit 0
    ;;
  ""|-h|--help|help)
    usage
    exit 2
    ;;
  *)
    usage
    exit 2
    ;;
esac
