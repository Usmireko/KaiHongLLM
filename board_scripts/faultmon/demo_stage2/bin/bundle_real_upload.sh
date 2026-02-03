#!/system/bin/sh
set -e

BB=/data/local/tmp/busybox
BASE_DIR=/data/faultmon/demo_stage2
TMP_ROOT="$BASE_DIR/tmp"
BIN_DIR="$BASE_DIR/bin"
UPLOADER="$BIN_DIR/uploader_nc.sh"
DEVICE_ID_FILE=/data/faultmon/device_id

log(){ echo "[bundle_real] $*" >&2; }

now_ms() {
  val="$(date +%s%3N 2>/dev/null || true)"
  case "$val" in *N*|*%*|"") val="" ;; esac
  if [ -z "$val" ]; then
    s="$(date +%s 2>/dev/null || echo 0)"
    val=$((s * 1000))
  fi
  echo "$val"
}

collect_pidstat() {
  in_file="$1"
  out_file="$2"
  max_pids="$3"
  case "$max_pids" in ''|*[!0-9]*) max_pids=120 ;; esac
  t_ms="$(now_ms)"
  echo "# t_ms=$t_ms" > "$out_file"
  echo "# interval_ms=2000" >> "$out_file"
echo "#" >> "$out_file"
  ps_dump() {
    if [ -x "$BB" ]; then
      "$BB" ps -A 2>/dev/null || "$BB" ps 2>/dev/null || true
    else
      ps -A 2>/dev/null || ps 2>/dev/null || true
    fi
  }
   tmp_dir="$TMP_ROOT/.pidstat_tmp"
  mkdir -p "$tmp_dir" 2>/dev/null || true
  ps_tmp="$tmp_dir/ps.$$"
  ps_dump > "$ps_tmp" 2>/dev/null || true
  count=0
  seen=" "
  stop=0 
  emit_pid() {
   [ "$stop" -eq 1 ] && return 0
   pid="$1"
   case "$pid" in ''|*[!0-9]*) return 0 ;; esac
   case "$seen" in *" $pid "*) return 0 ;; esac
   [ -r "/proc/$pid/stat" ] || return 0

stat_line="$(cat "/proc/$pid/stat" 2>/dev/null || true)"
    comm="NA"
    if [ -r "/proc/$pid/comm" ]; then
      IFS= read -r comm < "/proc/$pid/comm" 2>/dev/null || comm="NA"
      [ -n "$comm" ] || comm="NA"
    fi

    rss_kb="NA"
    if [ -r "/proc/$pid/status" ]; then
      rss_line="$( ( [ -x "$BB" ] && "$BB" grep -F "VmRSS:" "/proc/$pid/status" 2>/dev/null ) || grep -F "VmRSS:" "/proc/$pid/status" 2>/dev/null || true )"
      set -- $rss_line
      [ -n "$2" ] && rss_kb="$2"
    fi

    [ -n "$stat_line" ] || stat_line="NA"

    echo "pid=$pid comm=$comm rss_kb=$rss_kb stat=\"$stat_line\" cmd=\"$comm\"" >> "$out_file"
    seen="$seen$pid "
    count=$((count + 1))
    if [ "$count" -ge "$max_pids" ]; then
      stop=1
    fi
    return 0
  }

  # 1) inject_mem pidfile（如果存在）
  inject_file="/data/faultmon/demo_stage2/pids/inject_mem.pid"
  if [ -f "$inject_file" ]; then
    while IFS= read -r pid; do
      emit_pid "$pid"
    done < "$inject_file"
  fi

  # 2) 从 in_file 中抽取 PID：取每行第一个纯数字 token
  if [ -f "$in_file" ]; then
    while IFS= read -r line; do
      [ "$stop" -eq 1 ] && break
      pid=""
      for tok in $line; do
        case "$tok" in ''|*[!0-9]*) ;; *) pid="$tok"; break ;; esac
      done
      [ -n "$pid" ] && emit_pid "$pid"
    done < "$in_file"
  fi

  # 3) ps 输出中优先抓 render_service/appspawn
  if [ -f "$ps_tmp" ]; then
    while IFS= read -r line; do
      [ "$stop" -eq 1 ] && break
      case "$line" in
        *render_service*|*appspawn*)
          pid=""
          for tok in $line; do
            case "$tok" in ''|*[!0-9]*) ;; *) pid="$tok"; break ;; esac
          done
          [ -n "$pid" ] && emit_pid "$pid"
        ;;
      esac
    done < "$ps_tmp"
  fi

  # 4) 用 ps 输出补齐
  if [ -f "$ps_tmp" ]; then
    while IFS= read -r line; do
      [ "$stop" -eq 1 ] && break
      pid=""
      for tok in $line; do
        case "$tok" in ''|*[!0-9]*) ;; *) pid="$tok"; break ;; esac
      done
      [ -n "$pid" ] && emit_pid "$pid"
    done < "$ps_tmp"
  fi

  # 5) 兜底：遍历 /proc 补齐（避免 ps 格式差异导致 0 行）
  if [ "$stop" -ne 1 ]; then
    for d in /proc/[0-9]*; do
      [ "$stop" -eq 1 ] && break
      pid="${d##*/}"
      emit_pid "$pid"
    done
  fi

rm -f "$ps_tmp" 2>/dev/null || true
  echo "pidstat collected: $count lines"
}

gen_run_id() {
  ts="$(date +%Y%m%d_%H%M%S 2>/dev/null || true)"
  if [ -z "$ts" ]; then
    ts="$(date +%s 2>/dev/null || echo 0)"
  fi
  echo "real_${TAG}_${ts}"
}

TAG="e2e_fix"
PRE_SEC=10
POST_SEC=10
while [ $# -gt 0 ]; do
  case "$1" in
    --tag) shift; TAG="$1" ;;
    --pre) shift; PRE_SEC="$1" ;;
    --post) shift; POST_SEC="$1" ;;
  esac
  shift
 done

case "$PRE_SEC" in ''|*[!0-9]*) PRE_SEC=10 ;; esac
case "$POST_SEC" in ''|*[!0-9]*) POST_SEC=10 ;; esac

if [ ! -f "$DEVICE_ID_FILE" ]; then
  echo dev1 > "$DEVICE_ID_FILE"
fi
DEVICE_ID="$(cat "$DEVICE_ID_FILE" 2>/dev/null | head -n 1)"
[ -n "$DEVICE_ID" ] || DEVICE_ID="dev1"

SYS_FILE="$(ls -t /data/faultmon/metrics/sys_*.csv 2>/dev/null | /data/local/tmp/busybox head -n 1)"
EV_FILE="$(ls -t /data/faultmon/events/events_*.jsonl 2>/dev/null | /data/local/tmp/busybox head -n 1)"

if [ -z "$SYS_FILE" ] || [ -z "$EV_FILE" ]; then
  echo "error:missing_real_metrics_or_events" >&2
  echo "sys_file=$SYS_FILE" >&2
  echo "events_file=$EV_FILE" >&2
  exit 2
fi

line="$(/data/local/tmp/busybox tail -n 1 "$SYS_FILE" 2>/dev/null || true)"
ts="${line%%,*}"
case "$ts" in ''|*[!0-9]*) ts="" ;; esac

if [ -z "$ts" ]; then
  TS="$(now_ms)"
else
  if [ ${#ts} -le 10 ]; then
    TS=$((ts * 1000))
  else
    TS="$ts"
  fi
fi

RUN_ID="$(gen_run_id)"
RUN_DIR="$TMP_ROOT/$RUN_ID"
MET_DIR="$RUN_DIR/metrics"
EV_DIR="$RUN_DIR/events"
PROC_DIR="$RUN_DIR/procs"
SNAP_DIR="$RUN_DIR/snapshots"

mkdir -p "$MET_DIR" "$EV_DIR" "$PROC_DIR" "$SNAP_DIR"

WIN_START=$((TS - PRE_SEC * 1000))
WIN_END=$((TS + POST_SEC * 1000))

cat > "$RUN_DIR/_run_meta.json" <<EOF
{
  "run_id": "${RUN_ID}",
  "scenario_tag": "${TAG}",
  "fault_type": "real",
  "run_start": ${TS},
  "run_end": ${TS},
  "run_window_host_epoch_ms_start": ${WIN_START},
  "run_window_host_epoch_ms_end": ${WIN_END},
  "run_window_board_ms_start": ${WIN_START},
  "run_window_board_ms_end": ${WIN_END},
  "run_window_source": "sys_tail_ts"
}
EOF

cp "$SYS_FILE" "$MET_DIR/" 2>/dev/null || true
cp "$EV_FILE" "$EV_DIR/" 2>/dev/null || true

PROC_FILE="$(ls -t /data/faultmon/procs/procs_*.txt 2>/dev/null | /data/local/tmp/busybox head -n 1)"
if [ -n "$PROC_FILE" ]; then
  cp "$PROC_FILE" "$PROC_DIR/" 2>/dev/null || true
fi

PROC_SNAPSHOT="$(ls -t "$PROC_DIR"/procs_*.txt 2>/dev/null | /data/local/tmp/busybox head -n 1)"
if [ -n "$PROC_SNAPSHOT" ]; then
  PIDSTAT0="$PROC_DIR/pidstat_0.txt"
  PIDSTAT1="$PROC_DIR/pidstat_1.txt"
  collect_pidstat "$PROC_SNAPSHOT" "$PIDSTAT0" 120
  sleep 2
  collect_pidstat "$PROC_SNAPSHOT" "$PIDSTAT1" 120
fi

cat /proc/loadavg > "$SNAP_DIR/loadavg.txt" 2>/dev/null || true
cat /proc/meminfo > "$SNAP_DIR/meminfo.txt" 2>/dev/null || true
ps > "$SNAP_DIR/ps.txt" 2>/dev/null || true
(dmesg | /data/local/tmp/busybox tail -n 200) > "$SNAP_DIR/dmesg_tail.txt" 2>/dev/null || true

bundle_path="$BASE_DIR/${RUN_ID}__bundle.tar.gz"
tar_err="$BASE_DIR/tar_err_${RUN_ID}.txt"
rm -f "$bundle_path" "$tar_err" 2>/dev/null || true
cd "$TMP_ROOT" || exit 2

tar -czf "$bundle_path" "$RUN_ID" 2>"$tar_err"
tar_rc=$?
if [ "$tar_rc" -ne 0 ]; then
  echo "error:tar_failed rc=$tar_rc err=$tar_err" >&2
  exit "$tar_rc"
fi

size="$(wc -c < "$bundle_path" 2>/dev/null || echo 0)"
case "$size" in ''|*[!0-9]*) size=0 ;; esac
if [ "$size" -le 0 ]; then
  echo "error:bundle_size_zero" >&2
  exit 2
fi

echo "run_id=$RUN_ID"
echo "bundle_path=$bundle_path"
echo "bundle_size=$size"

"$UPLOADER" --file "$bundle_path" --type bundle --device "$DEVICE_ID" --run "$RUN_ID"
rc=$?
echo "upload_rc=$rc"
if [ "$rc" -eq 0 ]; then
  echo "$RUN_ID" > "$BASE_DIR/latest_bundle_run_id.txt"
  echo "$bundle_path" > "$BASE_DIR/latest_bundle_path.txt"
fi
exit $rc
