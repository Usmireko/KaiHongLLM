#!/system/bin/sh

BASE_DIR=/data/faultmon/demo_stage2
BIN_DIR="$BASE_DIR/bin"
PID_DIR="$BASE_DIR/pids"
LOG_DIR="$BASE_DIR/logs"
DEVICE_ID_FILE=/data/faultmon/device_id

FAULTMON_GUARD="$BIN_DIR/faultmon_guard.sh"
TRIGGERD="$BIN_DIR/triggerd.sh"
INJECT_CPU="$BIN_DIR/inject_cpu.sh"
INJECT_MEM="$BIN_DIR/inject_mem.sh"

mkdir -p "$PID_DIR" "$LOG_DIR"

ensure_device_id() {
  if [ ! -f "$DEVICE_ID_FILE" ]; then
    echo dev1 > "$DEVICE_ID_FILE"
  fi
  dev_id="$(cat "$DEVICE_ID_FILE" 2>/dev/null | /data/local/tmp/busybox head -n 1)"
  if [ -z "$dev_id" ]; then
    echo dev1 > "$DEVICE_ID_FILE"
  fi
}

start_cpu() {
  ensure_device_id
  "$FAULTMON_GUARD" start
  "$TRIGGERD" --daemon --interval 1 --mode cpu --hit_need 3 --cooldown 60 --tag auto_cpu --pre 10 --post 10 --threshold_load1_int 3
  "$INJECT_CPU" start
}

start_mem() {
  ensure_device_id
  "$FAULTMON_GUARD" start
  "$TRIGGERD" --daemon --interval 1 --mode mem --hit_need 3 --cooldown 60 --tag auto_mem --pre 10 --post 10 --threshold_mem_drop_kb 100000
  "$INJECT_MEM" start
}

stop_all() {
  "$TRIGGERD" stop
  "$INJECT_CPU" stop
  "$INJECT_MEM" stop
}

status_all() {
  echo "faultmon:"; "$FAULTMON_GUARD" status
  echo "triggerd:"; "$TRIGGERD" status
  echo "inject_cpu:"; "$INJECT_CPU" status
  echo "inject_mem:"; "$INJECT_MEM" status
}

logs_all() {
  echo "=== faultmon.log ==="; "$FAULTMON_GUARD" logs
  echo "=== triggerd.log ==="; "$TRIGGERD" logs
  echo "=== inject_cpu.log ==="; "$INJECT_CPU" logs
  echo "=== inject_mem.log ==="; "$INJECT_MEM" logs
}

wait_for_trigger() {
  mode="$1"
  timeout="$2"
  t=0
  run_id=""
  action_bundle=""
  while [ "$t" -lt "$timeout" ]; do
    if [ -f "$BASE_DIR/latest_trigger_run_id.txt" ] && [ -f "$BASE_DIR/latest_action_result_bundle.txt" ]; then
      run_id="$(cat "$BASE_DIR/latest_trigger_run_id.txt" 2>/dev/null)"
      action_bundle="$(cat "$BASE_DIR/latest_action_result_bundle.txt" 2>/dev/null)"
      if [ -n "$run_id" ] && [ -n "$action_bundle" ] && [ -f "$action_bundle" ]; then
        size="$(wc -c < "$action_bundle" 2>/dev/null || echo 0)"
        case "$size" in ''|*[!0-9]*) size=0 ;; esac
        if [ "$size" -gt 0 ]; then
          echo "$run_id" > "$BASE_DIR/latest_trigger_run_id.txt"
          return 0
        fi
      fi
    fi
    /data/local/tmp/busybox sleep 1
    t=$((t + 1))
  done
  return 1
}

clean_markers() {
  rm -f "$BASE_DIR/latest_trigger_run_id.txt" "$BASE_DIR/latest_action_result_bundle.txt" "$BASE_DIR/latest_poller_rc.txt" 2>/dev/null || true
}

demo_once() {
  mode="$1"
  shift
  if [ "$mode" = "cpu" ]; then
    timeout=120
  else
    timeout=180
  fi
  while [ $# -gt 0 ]; do
    case "$1" in
      --timeout) shift; timeout="$1" ;;
    esac
    shift
  done

  clean_markers
  if [ "$mode" = "cpu" ]; then
    start_cpu
  else
    start_mem
  fi

  if wait_for_trigger "$mode" "$timeout"; then
    run_id="$(cat "$BASE_DIR/latest_trigger_run_id.txt" 2>/dev/null)"
    if [ "$mode" = "cpu" ]; then
      "$INJECT_CPU" stop
    else
      "$INJECT_MEM" stop
    fi
    echo "demo_done mode=$mode run_id=$run_id"
    /data/local/tmp/busybox tail -n 20 "$LOG_DIR/triggerd.log" 2>/dev/null || true
    return 0
  fi

  echo "demo_timeout mode=$mode timeout=$timeout" >&2
  if [ "$mode" = "cpu" ]; then
    "$INJECT_CPU" stop
  else
    "$INJECT_MEM" stop
  fi
  /data/local/tmp/busybox tail -n 50 "$LOG_DIR/triggerd.log" 2>/dev/null || true
  /data/local/tmp/busybox tail -n 50 "$LOG_DIR/inject_${mode}.log" 2>/dev/null || true
  return 1
}

case "$1" in
  start_cpu)
    start_cpu
    ;;
  start_mem)
    start_mem
    ;;
  stop)
    stop_all
    ;;
  status)
    status_all
    ;;
  logs)
    logs_all
    ;;
  demo_cpu_once)
    shift
    demo_once cpu "$@"
    ;;
  demo_mem_once)
    shift
    demo_once mem "$@"
    ;;
  *)
    echo "usage: $0 start_cpu|start_mem|stop|status|logs|demo_cpu_once [--timeout N]|demo_mem_once [--timeout N]" >&2
    exit 2
    ;;
 esac