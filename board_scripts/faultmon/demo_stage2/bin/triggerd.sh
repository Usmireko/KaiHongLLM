#!/system/bin/sh

BASE_DIR=/data/faultmon/demo_stage2
BIN_DIR="$BASE_DIR/bin"
PID_DIR="$BASE_DIR/pids"
LOG_DIR="$BASE_DIR/logs"
PIDFILE="$PID_DIR/triggerd.pid"
LOGFILE="$LOG_DIR/triggerd.log"
ROOT=/data/faultmon
METRICS_DIR="$ROOT/metrics"
STATE_DIR="$ROOT/state"
LOCK_FILE="$STATE_DIR/trigger.active"
LAST_TRIGGER_JSON="$STATE_DIR/last_trigger.json"
LAST_TRIGGER_EPOCH="$STATE_DIR/last_trigger_epoch"

INTERVAL=2
INTERVAL_SET=0
MODE="multi"
HIT_NEED=3
CPU_HIT_NEED=3
MEM_HIT_NEED=3
CPU_HIT_SET=0
MEM_HIT_SET=0
COOLDOWN=60
TAG=""
PRE_SEC=10
POST_SEC=10
THRESHOLD_LOAD1_INT=3
THRESHOLD_MEM_DROP_KB=200000
CPU_LOAD1_X100_THRESHOLD=300
CPU_X100_SET=0
MEM_AVAIL_KB_THRESHOLD=0
MEM_DROP_KB=200000
DAEMON=0
RUNLOOP=0
RUNLOOP_OWNS_PIDFILE=1

quick_usage() {
  echo "usage: $0 [cpu|mem] [--daemon] [--runloop] [--once] [--interval N] [--mode cpu|mem|multi] [--hit_need N] [--cpu_hit_need N] [--mem_hit_need N] [--cooldown N|--cooldown_sec N] [--tag NAME] [--pre N] [--post N] [--threshold_load1_int N] [--threshold_mem_drop_kb N] [--cpu_load1_x100_threshold N] [--mem_avail_kb_threshold N]" >&2
  echo "example: sh -x $0 --runloop --mode multi --interval 2" >&2
}

# no-arg / help must return immediately (no blocking)
if [ $# -eq 0 ]; then
  quick_usage
  exit 2
fi

if [ "$1" = "--help" ] || [ "$1" = "-h" ] || [ "$1" = "help" ]; then
  quick_usage
  exit 0
fi

mkdir -p "$PID_DIR" "$LOG_DIR" "$STATE_DIR"

log() {
  echo "[triggerd] $*" >> "$LOGFILE"
}

now_epoch_s() {
  /data/local/tmp/busybox date +%s 2>/dev/null || date +%s 2>/dev/null || echo 0
}

write_last_trigger_epoch() {
  epoch="$(now_epoch_s)"
  case "$epoch" in ''|*[!0-9]*) epoch=0 ;; esac
  echo "$epoch" > "$LAST_TRIGGER_EPOCH"
  log "epoch_written epoch=$epoch"
}

is_digits() {
  case "$1" in
    ""|*[!0-9]*) return 1 ;;
    *) return 0 ;;
  esac
}

is_valid_ts_ms() {
  s="$1"
  is_digits "$s" || return 1
  [ ${#s} -eq 13 ] || return 1
  return 0
}

is_valid_ts_s() {
  s="$1"
  is_digits "$s" || return 1
  [ ${#s} -ge 10 ] || return 1
  return 0
}

write_pidfile() {
  echo "$$" > "$PIDFILE"
}

cleanup_pidfile() {
  if [ "$RUNLOOP_OWNS_PIDFILE" -eq 1 ] 2>/dev/null; then
    rm -f "$PIDFILE" 2>/dev/null || true
  fi
  rm -f "$LOCK_FILE" 2>/dev/null || true
}

latest_metrics_file() {
  /data/local/tmp/busybox ls -t "$METRICS_DIR"/sys_*.csv 2>/dev/null | /data/local/tmp/busybox head -n 1
}

read_faultmon_interval() {
  line="$(/data/local/tmp/busybox grep -m 1 'METRICS_PERIOD_SEC' /data/faultmon/faultmon.sh 2>/dev/null)"
  val="$(printf '%s' "$line" | /data/local/tmp/busybox sed 's/[^0-9]//g')"
  case "$val" in ''|*[!0-9]*) return 1 ;; esac
  [ "$val" -le 0 ] 2>/dev/null && return 1
  echo "$val"
  return 0
}

read_metrics_last() {
  metrics_path="$(latest_metrics_file)"
  [ -z "$metrics_path" ] && return 1
  line="$(/data/local/tmp/busybox tail -n 1 "$metrics_path" 2>/dev/null)"
  [ -z "$line" ] && return 1
  IFS=',' read -r ts_ms mem_free_kb load1_x100 io_psi_avg10_x100 cpu_util_total_x100 cpu_idle_x100 mem_total_kb mem_available_kb swap_total_kb swap_free_kb disk_read_kBps disk_write_kBps net_rx_kBps net_tx_kBps <<EOF
$line
EOF
  case "$ts_ms" in ''|*[!0-9]*) ts_ms="" ;; esac
  case "$load1_x100" in ''|*[!0-9]*) load1_x100="" ;; esac
  case "$mem_available_kb" in ''|*[!0-9]*) mem_available_kb="" ;; esac
  return 0
}

lock_active_other() {
  if [ -f "$LOCK_FILE" ]; then
    lp="$(cat "$LOCK_FILE" 2>/dev/null)"
    if [ -n "$lp" ] && [ "$lp" != "$$" ] && kill -0 "$lp" 2>/dev/null; then
      return 0
    fi
    if [ -n "$lp" ] && ! kill -0 "$lp" 2>/dev/null; then
      rm -f "$LOCK_FILE" 2>/dev/null || true
    fi
  fi
  return 1
}

lock_set() { printf '%s\n' "$$" > "$LOCK_FILE"; }
lock_clear() { rm -f "$LOCK_FILE" 2>/dev/null || true; }

select_tag_for_reasons() {
  reasons="$1"
  if [ -n "$TAG" ]; then
    echo "$TAG"
    return
  fi
  case "$reasons" in
    cpu,mem|mem,cpu) echo "auto_cpu_mem" ;;
    cpu) echo "auto_cpu" ;;
    mem) echo "auto_mem" ;;
    *) echo "auto_unknown" ;;
  esac
}

write_last_trigger() {
  reasons="$1"
  tag="$2"
  l1x="$3"
  makb="$4"
  run_id="$5"
  [ -z "$run_id" ] && run_id="unknown"
  ts_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date)"
  reasons_json="$(reasons_to_json_array "$reasons")"
  {
    echo "{"
    echo "  \"t_trigger_utc\": \"${ts_utc}\","
    echo "  \"reasons\": ${reasons_json},"
    echo "  \"run_id\": \"${run_id}\","
    echo "  \"load1_x100\": ${l1x},"
    echo "  \"mem_available_kb\": ${makb},"
    echo "  \"tag\": \"${tag}\""
    echo "}"
  } > "$LAST_TRIGGER_JSON"
}

write_last_trigger_epoch() {
  epoch="$(/data/local/tmp/busybox date +%s 2>/dev/null || date +%s 2>/dev/null || echo 0)"
  printf '%s\n' "$epoch" > "$LAST_TRIGGER_EPOCH"
  log "epoch_written epoch=$epoch"
}

reasons_to_json_array() {
  r="$1"
  case "$r" in
    cpu,mem|mem,cpu) echo "[\"cpu\",\"mem\"]" ;;
    cpu) echo "[\"cpu\"]" ;;
    mem) echo "[\"mem\"]" ;;
    "") echo "[]" ;;
    *)
      oldifs="$IFS"
      IFS=','; set -- $r
      IFS="$oldifs"
      arr=""
      for part in "$@"; do
        [ -z "$part" ] && continue
        if [ -z "$arr" ]; then arr="\"$part\""; else arr="$arr,\"$part\""; fi
      done
      echo "[${arr}]"
      ;;
  esac
}

parse_bundle_output() {
  out="$1"
  run_id="$(printf '%s\n' "$out" | /data/local/tmp/busybox grep -F 'run_id=' | /data/local/tmp/busybox head -n 1 | /data/local/tmp/busybox cut -d= -f2)"
  bundle_path="$(printf '%s\n' "$out" | /data/local/tmp/busybox grep -F 'bundle_path=' | /data/local/tmp/busybox head -n 1 | /data/local/tmp/busybox cut -d= -f2)"
  upload_rc="$(printf '%s\n' "$out" | /data/local/tmp/busybox grep -F 'upload_rc=' | /data/local/tmp/busybox head -n 1 | /data/local/tmp/busybox cut -d= -f2)"
  case "$upload_rc" in ''|*[!0-9]*) upload_rc="" ;; esac
}

parse_poller_output() {
  out="$1"
  resp_line="$(printf '%s\n' "$out" | /data/local/tmp/busybox grep -F 'resp_header RUN=' | /data/local/tmp/busybox head -n 1)"
  resp_run=""
  resp_len=""
  if [ -n "$resp_line" ]; then
    resp_run="${resp_line#*RUN=}"
    resp_run="${resp_run%% *}"
    resp_len="${resp_line#*LEN=}"
    resp_len="${resp_len%% *}"
  fi
  result_bundle="$(printf '%s\n' "$out" | /data/local/tmp/busybox grep -F 'result_bundle=' | /data/local/tmp/busybox head -n 1 | /data/local/tmp/busybox cut -d= -f2)"
  case "$resp_len" in ''|*[!0-9]*) resp_len="" ;; esac
}

WAIT_MISMATCH=0
WAIT_ELAPSED=0
FIRE_REASONS=""
FIRE_TAG=""

wait_actions_ready() {
  run_id="$1"
  timeout="$2"
  waited=0
  mismatch=0
  last_resp_run=""
  last_resp_len=""
  log "wait_actions_ready start run_id=$run_id timeout=$timeout"
  while [ "$waited" -lt "$timeout" ]; do
    out_h="$($BIN_DIR/actions_poller_nc.sh --once --verbose --header-only 2>&1)"
    rc=$?
    parse_poller_output "$out_h"
    last_resp_run="$resp_run"
    last_resp_len="$resp_len"
    if [ "$resp_run" = "$run_id" ] && [ -n "$resp_len" ] && [ "$resp_len" -gt 0 ]; then
      WAIT_MISMATCH="$mismatch"
      WAIT_ELAPSED="$waited"
      log "wait_actions_ready ok run_id=$run_id resp_len=$resp_len"
      return 0
    fi
    if [ -n "$resp_run" ] && [ "$resp_run" != "$run_id" ]; then
      mismatch=$((mismatch + 1))
    fi
    log "wait_actions_ready sample run_id=$run_id resp_run=$resp_run resp_len=$resp_len rc=$rc mismatch=$mismatch"
    /data/local/tmp/busybox sleep 1
    waited=$((waited + 1))
  done
  WAIT_MISMATCH="$mismatch"
  WAIT_ELAPSED="$waited"
  log "wait_actions_ready timeout run_id=$run_id last_resp_run=$last_resp_run last_resp_len=$last_resp_len mismatch=$mismatch"
  return 1
}

write_markers() {
  run_id="$1"
  poller_rc="$2"
  action_bundle="$3"
  echo "$run_id" > "$BASE_DIR/latest_trigger_run_id.txt"
  echo "$poller_rc" > "$BASE_DIR/latest_poller_rc.txt"
  if [ -n "$action_bundle" ]; then
    echo "$action_bundle" > "$BASE_DIR/latest_action_result_bundle.txt"
  fi
}

write_summary() {
  run_id="$1"
  resp_run="$2"
  resp_len="$3"
  poller_rc="$4"
  action_bundle="$5"
  upload_rc="$6"
  wait_mismatch="$7"
  wait_elapsed="$8"
  summary_file="$BASE_DIR/latest_trigger_summary.txt"
  {
    echo "SUMMARY_BEGIN"
    echo "run_id=$run_id"
    echo "resp_run=$resp_run"
    echo "resp_len=$resp_len"
    echo "poller_rc=$poller_rc"
    echo "action_result_bundle=$action_bundle"
    echo "upload_rc=$upload_rc"
    echo "wait_mismatch=$wait_mismatch"
    echo "wait_elapsed=$wait_elapsed"
    echo "SUMMARY_END"
  } > "$summary_file"

  log "SUMMARY_BEGIN"
  log "run_id=$run_id"
  log "resp_run=$resp_run"
  log "resp_len=$resp_len"
  log "poller_rc=$poller_rc"
  log "action_result_bundle=$action_bundle"
  log "upload_rc=$upload_rc"
  log "wait_mismatch=$wait_mismatch"
  log "wait_elapsed=$wait_elapsed"
  log "SUMMARY_END"
}

fire_trigger() {
  write_last_trigger_epoch
  log "trigger_start mode=$MODE tag=$FIRE_TAG reasons=$FIRE_REASONS pre=$PRE_SEC post=$POST_SEC"
  out="$($BIN_DIR/bundle_real_upload.sh --tag "$FIRE_TAG" --pre "$PRE_SEC" --post "$POST_SEC" 2>&1)"
  bundle_rc=$?
  printf '%s\n' "$out" >> "$LOGFILE"
  parse_bundle_output "$out"
  if [ -z "$run_id" ]; then
    run_id="unknown_$(date +%s 2>/dev/null || echo 0)"
  fi
  if [ -z "$upload_rc" ]; then
    upload_rc="$bundle_rc"
  fi
  write_last_trigger "$FIRE_REASONS" "$FIRE_TAG" "$SAMPLE_L1X" "$SAMPLE_MAKB" "$run_id"
  log "trigger_bundle run_id=$run_id bundle_path=$bundle_path upload_rc=$upload_rc tag=$FIRE_TAG reasons=$FIRE_REASONS"

  if [ "$upload_rc" -ne 0 ]; then
    log "trigger_skip_poller run_id=$run_id upload_rc=$upload_rc"
    return 1
  fi

  if ! wait_actions_ready "$run_id" 60; then
    log "trigger_wait_actions_ready_failed run_id=$run_id"
    return 2
  fi

  poller_rc=255
  action_bundle=""
  attempt=1
  while [ "$attempt" -le 3 ]; do
    out_p="$($BIN_DIR/actions_poller_nc.sh --once --verbose --expect-run "$run_id" 2>&1)"
    poller_rc=$?
    parse_poller_output "$out_p"
    log "trigger_poller_resp run_id=$run_id resp_run=$resp_run resp_len=$resp_len poller_rc=$poller_rc attempt=$attempt"
    if [ "$resp_run" = "$run_id" ] && [ -n "$resp_len" ] && [ "$resp_len" -gt 0 ] && [ "$poller_rc" -eq 0 ]; then
      action_bundle="$result_bundle"
      if [ -z "$action_bundle" ]; then
        action_bundle="$(ls -t "$BASE_DIR"/action_result_bundle_${run_id}__*.tar.gz 2>/dev/null | /data/local/tmp/busybox head -n 1)"
      fi
      write_markers "$resp_run" "$poller_rc" "$action_bundle"
      write_summary "$run_id" "$resp_run" "$resp_len" "$poller_rc" "$action_bundle" "$upload_rc" "$WAIT_MISMATCH" "$WAIT_ELAPSED"
      return 0
    fi
    /data/local/tmp/busybox sleep 1
    attempt=$((attempt + 1))
  done

  log "trigger_poller_failed run_id=$run_id"
  return 3
}

usage() { quick_usage; }

if [ "$1" = "stop" ]; then
  if [ -f "$PIDFILE" ]; then
    pid="$(cat "$PIDFILE" 2>/dev/null)"
    if [ -n "$pid" ]; then
      kill "$pid" 2>/dev/null || true
      /data/local/tmp/busybox sleep 1
      if kill -0 "$pid" 2>/dev/null; then
        kill -9 "$pid" 2>/dev/null || true
      fi
    fi
    rm -f "$PIDFILE" 2>/dev/null || true
  fi
  if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
    ps_out="$(/data/local/tmp/busybox ps w 2>/dev/null || /data/local/tmp/busybox ps 2>/dev/null || ps 2>/dev/null)"
    echo "$ps_out" | /data/local/tmp/busybox grep -F -e "triggerd.sh" | /data/local/tmp/busybox grep -F -e "--runloop" | /data/local/tmp/busybox grep -F -v -e "grep" | while read -r line; do
      set -- $line
      cand="$1"
      case "$cand" in ''|*[!0-9]*) continue ;; esac
      kill "$cand" 2>/dev/null || true
      /data/local/tmp/busybox sleep 1
      if kill -0 "$cand" 2>/dev/null; then
        kill -9 "$cand" 2>/dev/null || true
      fi
    done
  fi
  rm -f "$PIDFILE" "$LOCK_FILE" 2>/dev/null || true
  echo "stopped"
  exit 0
fi

if [ "$1" = "status" ]; then
  if [ -f "$PIDFILE" ]; then
    pid="$(cat "$PIDFILE" 2>/dev/null)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      echo "alive pid=$pid"
      exit 0
    fi
  fi
  echo "not_running"
  exit 1
fi

if [ "$1" = "logs" ]; then
  /data/local/tmp/busybox tail -n 50 "$LOGFILE" 2>/dev/null || true
  exit 0
fi

while [ $# -gt 0 ]; do
  case "$1" in
    --daemon) DAEMON=1 ;;
    --runloop) RUNLOOP=1 ;;
    --interval) shift; INTERVAL="$1"; INTERVAL_SET=1 ;;
    --mode) shift; MODE="$1" ;;
    --hit_need) shift; HIT_NEED="$1" ;;
    --cpu_hit_need) shift; CPU_HIT_NEED="$1"; CPU_HIT_SET=1 ;;
    --mem_hit_need) shift; MEM_HIT_NEED="$1"; MEM_HIT_SET=1 ;;
    --cooldown) shift; COOLDOWN="$1" ;;
    --cooldown_sec) shift; COOLDOWN="$1" ;; # alias
    --tag) shift; TAG="$1" ;;
    --pre) shift; PRE_SEC="$1" ;;
    --post) shift; POST_SEC="$1" ;;
    --threshold_load1_int) shift; THRESHOLD_LOAD1_INT="$1" ;;
    --threshold_mem_drop_kb) shift; THRESHOLD_MEM_DROP_KB="$1" ;;
    --cpu_load1_x100_threshold) shift; CPU_LOAD1_X100_THRESHOLD="$1"; CPU_X100_SET=1 ;;
    --mem_avail_kb_threshold) shift; MEM_AVAIL_KB_THRESHOLD="$1" ;;
    cpu|mem) MODE="$1" ;;
    --once) ONCE=1; RUNLOOP=1 ;;
    *)
      usage
      exit 2
      ;;
  esac
  shift
 done

case "$INTERVAL" in ''|*[!0-9]*) INTERVAL=2; INTERVAL_SET=0 ;; esac
if [ "$INTERVAL_SET" -eq 0 ] 2>/dev/null; then
  fm_interval="$(read_faultmon_interval)"
  if [ -n "$fm_interval" ] && [ "$fm_interval" -gt 0 ] 2>/dev/null; then
    INTERVAL="$fm_interval"
    log "interval_default_from_faultmon=$INTERVAL"
  else
    INTERVAL=2
    log "interval_default_fallback=2"
  fi
fi
case "$HIT_NEED" in ''|*[!0-9]*) HIT_NEED=3 ;; esac
if [ "$CPU_HIT_SET" -eq 0 ] 2>/dev/null; then
  CPU_HIT_NEED="$HIT_NEED"
fi
if [ "$MEM_HIT_SET" -eq 0 ] 2>/dev/null; then
  MEM_HIT_NEED="$HIT_NEED"
fi
case "$CPU_HIT_NEED" in ''|*[!0-9]*) CPU_HIT_NEED="$HIT_NEED" ;; esac
case "$MEM_HIT_NEED" in ''|*[!0-9]*) MEM_HIT_NEED="$HIT_NEED" ;; esac
case "$COOLDOWN" in ''|*[!0-9]*) COOLDOWN=60 ;; esac
case "$PRE_SEC" in ''|*[!0-9]*) PRE_SEC=10 ;; esac
case "$POST_SEC" in ''|*[!0-9]*) POST_SEC=10 ;; esac
case "$THRESHOLD_LOAD1_INT" in ''|*[!0-9]*) THRESHOLD_LOAD1_INT=3 ;; esac
case "$THRESHOLD_MEM_DROP_KB" in ''|*[!0-9]*) THRESHOLD_MEM_DROP_KB=200000 ;; esac
case "$CPU_LOAD1_X100_THRESHOLD" in ''|*[!0-9]*) CPU_LOAD1_X100_THRESHOLD=300 ;; esac
case "$MEM_AVAIL_KB_THRESHOLD" in ''|*[!0-9]*) MEM_AVAIL_KB_THRESHOLD=0 ;; esac

MEM_DROP_KB="$THRESHOLD_MEM_DROP_KB"
if [ "${CPU_X100_SET:-0}" -eq 0 ] 2>/dev/null; then
  CPU_LOAD1_X100_THRESHOLD=$(( THRESHOLD_LOAD1_INT * 100 ))
fi

if [ "$DAEMON" -eq 1 ]; then
  if [ -f "$PIDFILE" ]; then
    pid="$(cat "$PIDFILE" 2>/dev/null)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      echo "already_running pid=$pid"
      exit 0
    fi
  fi
  RUNLOOP_OWNS_PIDFILE=0 "$0" --runloop --interval "$INTERVAL" --mode "$MODE" --hit_need "$HIT_NEED" --cpu_hit_need "$CPU_HIT_NEED" --mem_hit_need "$MEM_HIT_NEED" --cooldown "$COOLDOWN" --tag "$TAG" --pre "$PRE_SEC" --post "$POST_SEC" --threshold_load1_int "$THRESHOLD_LOAD1_INT" --threshold_mem_drop_kb "$THRESHOLD_MEM_DROP_KB" --cpu_load1_x100_threshold "$CPU_LOAD1_X100_THRESHOLD" --mem_avail_kb_threshold "$MEM_AVAIL_KB_THRESHOLD" >> "$LOGFILE" 2>&1 &
  echo $! > "$PIDFILE"
  echo "started pid=$(cat "$PIDFILE")"
  exit 0
fi

if [ "$ONCE" -eq 1 ]; then
  RUNLOOP=1
fi

if [ "$RUNLOOP" -ne 1 ]; then
  usage
  exit 2
fi

log "start mode=$MODE interval=$INTERVAL cpu_hit_need=$CPU_HIT_NEED mem_hit_need=$MEM_HIT_NEED cooldown=$COOLDOWN tag_override=${TAG:-none} cpu_thr_x100=$CPU_LOAD1_X100_THRESHOLD mem_avail_thr_kb=$MEM_AVAIL_KB_THRESHOLD mem_drop_kb=$MEM_DROP_KB metrics_dir=$METRICS_DIR"
if [ "$RUNLOOP_OWNS_PIDFILE" -eq 1 ] 2>/dev/null; then
  write_pidfile
fi
trap 'cleanup_pidfile' EXIT

mode_has_cpu=0
mode_has_mem=0
case "$MODE" in
  cpu) mode_has_cpu=1 ;;
  mem) mode_has_mem=1 ;;
  multi) mode_has_cpu=1; mode_has_mem=1 ;;
  *) mode_has_cpu=1; mode_has_mem=1 ;;
esac

# TODO(net): read metrics CSV fields (e.g., dns_fail_cnt/rx_drop/ping_loss) and add net rule.

cpu_hit=0
mem_hit=0
baseline_mem_kb=""
last_ts_ms_seen=""
ONCE="${ONCE:-0}"
while true; do
  if [ "$ONCE" -ne 1 ]; then
    if lock_active_other; then
      log "lock_active skip pid=$(cat "$LOCK_FILE" 2>/dev/null)"
      /data/local/tmp/busybox sleep "$INTERVAL"
      continue
    fi
  fi

  if ! read_metrics_last; then
    log "sample metrics_unavailable"
    if [ "$ONCE" -eq 1 ]; then
      write_last_trigger_epoch
      log "once_no_trigger cpu_hit=$cpu_hit mem_hit=$mem_hit"
      exit 0
    fi
    /data/local/tmp/busybox sleep "$INTERVAL"
    continue
  fi
  ts_ms="$(printf '%s' "$ts_ms" | /data/local/tmp/busybox sed 's/[^0-9]//g')"
  load1_x100="$(printf '%s' "$load1_x100" | /data/local/tmp/busybox sed 's/[^0-9]//g')"
  mem_available_kb="$(printf '%s' "$mem_available_kb" | /data/local/tmp/busybox sed 's/[^0-9]//g')"
  [ -z "$load1_x100" ] && load1_x100=0
  [ -z "$mem_available_kb" ] && mem_available_kb=0
  if ! is_valid_ts_ms "$ts_ms"; then
    log "sample invalid_ts skip ts_ms=$ts_ms file=$metrics_path"
    if [ "$ONCE" -eq 1 ]; then
      write_last_trigger_epoch
      log "once_no_trigger cpu_hit=$cpu_hit mem_hit=$mem_hit"
      exit 0
    fi
    /data/local/tmp/busybox sleep "$INTERVAL"
    continue
  fi
  if [ -n "$last_ts_ms_seen" ] && [ "x$ts_ms" = "x$last_ts_ms_seen" ]; then
    log "same_sample skip ts_ms=$ts_ms last_ts_ms_seen=$last_ts_ms_seen file=$metrics_path"
    if [ "$ONCE" -eq 1 ]; then
      write_last_trigger_epoch
      log "once_no_trigger cpu_hit=$cpu_hit mem_hit=$mem_hit"
      exit 0
    fi
    /data/local/tmp/busybox sleep "$INTERVAL"
    continue
  fi
  ts_s="${ts_ms%???}"
  delta_s="na"
  if [ -n "$last_ts_ms_seen" ]; then
    last_ts_s="${last_ts_ms_seen%???}"
    if is_valid_ts_s "$ts_s" && is_valid_ts_s "$last_ts_s"; then
      delta_s=$(( ts_s - last_ts_s ))
      if [ "$delta_s" -lt 0 ] 2>/dev/null; then delta_s=0; fi
    fi
  fi
  last_ts_ms_seen="$ts_ms"

  cond_cpu=0
  cond_mem=0
  drop_kb="na"

  if [ "$mode_has_cpu" -eq 1 ]; then
    if [ "$load1_x100" -ge "$CPU_LOAD1_X100_THRESHOLD" ] 2>/dev/null; then
      cond_cpu=1
      cpu_hit=$((cpu_hit + 1))
    else
      cpu_hit=0
    fi
  fi

  if [ "$mode_has_mem" -eq 1 ]; then
    if [ "$MEM_AVAIL_KB_THRESHOLD" -gt 0 ] 2>/dev/null; then
      if [ "$mem_available_kb" -le "$MEM_AVAIL_KB_THRESHOLD" ] 2>/dev/null; then
        cond_mem=1
        mem_hit=$((mem_hit + 1))
      else
        mem_hit=0
      fi
    else
      [ -z "$baseline_mem_kb" ] && baseline_mem_kb="$mem_available_kb"
      drop_kb=$(( baseline_mem_kb - mem_available_kb ))
      if [ "$drop_kb" -lt 0 ] 2>/dev/null; then drop_kb=0; fi
      if [ "$mem_available_kb" -le $(( baseline_mem_kb - MEM_DROP_KB )) ] 2>/dev/null; then
        cond_mem=1
        mem_hit=$((mem_hit + 1))
      else
        mem_hit=0
      fi
    fi
  fi

  SAMPLE_L1X="$load1_x100"
  SAMPLE_MAKB="$mem_available_kb"
  log "sample metrics file=$metrics_path ts_ms=$ts_ms ts_s=$ts_s last_ts_ms_seen=$last_ts_ms_seen delta_s=$delta_s load1_x100=$load1_x100 mem_avail_kb=$mem_available_kb cpu_hit=$cpu_hit mem_hit=$mem_hit drop_kb=$drop_kb"

  reasons=""
  if [ "$mode_has_cpu" -eq 1 ] && [ "$cpu_hit" -ge "$CPU_HIT_NEED" ] 2>/dev/null; then
    reasons="cpu"
  fi
  if [ "$mode_has_mem" -eq 1 ] && [ "$mem_hit" -ge "$MEM_HIT_NEED" ] 2>/dev/null; then
    if [ -n "$reasons" ]; then
      reasons="${reasons},mem"
    else
      reasons="mem"
    fi
  fi

  if [ "$ONCE" -eq 1 ]; then
    write_last_trigger_epoch
    if [ -z "$reasons" ]; then
      log "once_no_trigger cpu_hit=$cpu_hit mem_hit=$mem_hit"
      exit 0
    fi
    FIRE_REASONS="$reasons"
    FIRE_TAG="$(select_tag_for_reasons "$reasons")"
    fire_trigger
    log "once_trigger_done rc=$?"
    exit 0
  fi

  if [ -n "$reasons" ]; then
    FIRE_REASONS="$reasons"
    FIRE_TAG="$(select_tag_for_reasons "$reasons")"
    lock_set
    write_last_trigger "$FIRE_REASONS" "$FIRE_TAG" "$load1_x100" "$mem_available_kb"
    if fire_trigger; then
      log "trigger_done run_id=$run_id reasons=$FIRE_REASONS tag=$FIRE_TAG"
    else
      log "trigger_failed run_id=$run_id reasons=$FIRE_REASONS tag=$FIRE_TAG"
    fi
    log "cooldown_start sec=$COOLDOWN"
    /data/local/tmp/busybox sleep "$COOLDOWN"
    lock_clear
    cpu_hit=0
    mem_hit=0
    if [ "$mode_has_mem" -eq 1 ]; then
      baseline_mem_kb="$mem_available_kb"
    fi
    log "cooldown_end baseline_mem_kb=${baseline_mem_kb:-na}"
  fi
  /data/local/tmp/busybox sleep "$INTERVAL"
done
