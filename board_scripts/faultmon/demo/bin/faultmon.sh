#!/bin/sh
# faultmon.sh — KaiHongOS / OpenHarmony (toybox/ash) 兼容版
# 事件通道(RDWR单例) + 指标采集 + 三规则触发 + 状态查询
#
# 只用：sh printf echo test [ ] date sleep kill ps grep fgrep egrep sed cat cut head tail wc ls mkdir rm rmdir readlink realpath tee usleep
# 以及 toybox 常见子命令（mkfifo 等）；禁止 awk/tr/jq/seq/mktemp/复杂 sed 扩展/外部 Python/BusyBox-only

# ──────────────────────────────────────────────────────────────────────────────
# 内置函数清单（名称 — 职责）
# - now_ms() / seconds_to_ms()                       — 毫秒时间基、秒→毫秒
# - today_json() / metrics_csv()                     — 当日 JSON/CSV 路径
# - json_escape()                                    — 基础 JSON 转义
# - ensure_directories() / ensure_event_pipe()       — 目录/FIFO 初始化
# - write_json_line() / queue_event()                — JSON 落盘 / 向 FIFO 写 7 段
# - event_sink_rdwr() / start_event_sink_if_needed() — RDWR 单例 sink 与幂等启动
# - rule_state_path() / rule_consider_ts()           — 规则状态与持续计时+冷却
# - fire_rule_event()                                — 规则触发事件（经 FIFO）
# - read_memfree_kb()/read_load1_x100()/read_io_psi_avg10_x100() — 指标采集
# - to_x100() / evaluate_rules()                     — 阈值归一化与统一评估
# - metrics_loop()/heartbeat_loop()/watcher_loop()   — 采集/心跳/占位监视
# - start_daemon()/stop_daemon()/status()            — 守护启动/停止/状态
# - cmd_*()                                          — 子命令封装
# ──────────────────────────────────────────────────────────────────────────────

##############################################################################
# 基本常量与路径（按“faultmon脚本恢复”规范修正）
##############################################################################
ROOT=/data/faultmon
STATE_DIR="$ROOT/state"
LAST_TS_MS="$STATE_DIR/last_ts_ms"
EVENTS_DIR="$ROOT/events"     # 绝不写只读 /events
LOG_DIR="$ROOT/logs"
METRICS_DIR="$ROOT/metrics"
PROCS_DIR="$ROOT/procs"       # ★ 新增：保存进程快照

EVENT_PIPE="$STATE_DIR/event.pipe"
RAW_LOG="$LOG_DIR/event_raw.log"
ERR_LOG="$LOG_DIR/faultmon.err"
OUT_LOG="$LOG_DIR/faultmon.out"
SINK_OUT="$LOG_DIR/event_sink.out"

HEARTBEAT_TS="$STATE_DIR/heartbeat.ts"
SINK_LOCK_DIR="$STATE_DIR/event_sink.lock"
SINK_PID_FILE="$STATE_DIR/event_sink.pid"
CHILDREN_PIDS="$STATE_DIR/children.pids"

# 规则阈值（可通过环境覆盖这些数值本身；但不允许覆盖路径）
: "${MEM_PRESSURE_KB:=655360}"         # KB
: "${MEM_PRESSURE_SEC:=10}"
: "${CPU_HOTSPOT_LOAD1:=3.03}"          # load1（浮点），内部转 x100
: "${CPU_HOTSPOT_SEC:=15}"
: "${IO_PRESSURE_AVG10:=0.30}"         # PSI avg10（若内核无 PSI 则采样恒 0）
: "${IO_PRESSURE_SEC:=20}"
: "${RULE_COOLDOWN_MS:=60000}"
: "${METRICS_PERIOD_SEC:=2}"
METRICS_PERIOD_MS=$((METRICS_PERIOD_SEC*1000))
: "${PROCS_SNAPSHOT_EVERY_N:=10}"  # ★ 新增：每多少次 metrics 采样抓一次 ps（0 = 关闭）

# ★ 新增：是否在 start 时清空旧的 metrics/procs（默认 1=清空）
: "${FAULTMON_CLEAN_ON_START:=1}"
# 分隔符（^A）
SEP="$(printf '\001')"

##############################################################################
# 工具函数
##############################################################################
now_ms() { printf '%s000\n' "$(date +%s)"; }
seconds_to_ms() { s="$1"; [ -z "$s" ] && s=0; echo $((s*1000)); }

today_json()  { printf '%s/events_%s.jsonl\n' "$EVENTS_DIR" "$(date +%Y%m%d)"; }
metrics_csv() { printf '%s/sys_%s.csv\n'     "$METRICS_DIR" "$(date +%Y%m%d)"; }

json_escape() { sed 's/\\/\\\\/g; s/"/\\"/g; s/[\r\n\t]/ /g'; }

ensure_directories() {
  mkdir -p "$ROOT" "$STATE_DIR" "$LOG_DIR" "$EVENTS_DIR" "$METRICS_DIR" "$PROCS_DIR" 2>/dev/null
  [ -f "$CHILDREN_PIDS" ] || : >"$CHILDREN_PIDS" 2>/dev/null || true
}

ensure_event_pipe() {
  if [ ! -p "$EVENT_PIPE" ]; then
    rm -f "$EVENT_PIPE" 2>/dev/null || true
    mkfifo "$EVENT_PIPE"
    chmod 600 "$EVENT_PIPE" 2>/dev/null || true
  fi
}

write_json_line() {
  ts="$1"; src="$2"; lvl="$3"; comp="$4"; pid="$5"; msg="$6"; tag="$7"
  src_e="$(printf '%s' "$src" | json_escape)"
  lvl_e="$(printf '%s' "$lvl" | json_escape)"
  comp_e="$(printf '%s' "$comp" | json_escape)"
  pid_e="$(printf '%s' "$pid" | json_escape)"
  msg_e="$(printf '%s' "$msg" | json_escape)"
  tag_e="$(printf '%s' "$tag" | json_escape)"
  printf '{"ts":%s,"source":"%s","level":"%s","component":"%s","pid":"%s","msg":"%s","tag":"%s"}\n' \
    "$ts" "$src_e" "$lvl_e" "$comp_e" "$pid_e" "$msg_e" "$tag_e" >>"$(today_json)"
}

queue_event() {
  # 字段：ts source level component pid msg tag
  ts="$1"; src="$2"; lvl="$3"; comp="$4"; pid="$5"; msg="$6"; tag="$7"
  printf '%s%s%s%s%s%s%s%s%s%s%s%s%s\n' \
    "$ts" "$SEP" "$src" "$SEP" "$lvl" "$SEP" "$comp" "$SEP" "$pid" "$SEP" "$msg" "$SEP" "$tag" >"$EVENT_PIPE"
}

capture_ps_snapshot() {
  ts="$1"
  reason="$2"

  [ -n "$ts" ] || ts="$(now_ms)"
  mkdir -p "$PROCS_DIR" 2>/dev/null

  out="$PROCS_DIR/procs_${ts}.txt"

  {
    printf '### ps snapshot at %s ms, reason=%s\n' "$ts" "$reason"

    # 优先使用 toybox/ps 的列输出，不行就退回默认 ps
    if ps -o pid,ppid,stat,rss,vsz,comm >/dev/null 2>&1; then
      # 常用列：pid,ppid,stat,rss,vsz,comm；不依赖 awk/tr
      ps -o pid,ppid,stat,rss,vsz,comm 2>/dev/null
    elif ps -o pid,ppid,stat,rss,comm >/dev/null 2>&1; then
      ps -o pid,ppid,stat,rss,comm 2>/dev/null
    else
      ps 2>/dev/null
    fi
  } >>"$out"
}
# ★ 新增：从刚才的 ps 快照里算出 Top-3 RSS，作为“调用链提示”
build_callchain_hint() {
  ts="$1"
  file="$PROCS_DIR/procs_${ts}.txt"
  [ -r "$file" ] || { echo ""; return; }

  top1_rss=0; top2_rss=0; top3_rss=0
  top1_pid=""; top2_pid=""; top3_pid=""
  top1_ppid=""; top2_ppid=""; top3_ppid=""
  top1_comm=""; top2_comm=""; top3_comm=""

  has_vsz=0
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    case "$line" in
      \#*) continue;;              # 跳过 "### ps snapshot ..." 行
    esac
    case "$line" in
      *PID*RSS*VSZ*|*PID*VSZ*RSS* ) has_vsz=1; continue;;  # 表头含 VSZ
      *PID*RSS* ) has_vsz=0; continue;;       # 跳过 ps 表头
    esac

    set -- $line
    pid="$1"; ppid="$2"; stat="$3"; rss="$4"
    if [ "$has_vsz" -eq 1 ]; then
      comm="$6"
    else
      comm="$5"
    fi
    [ -z "$pid" ] && continue

    # rss 不是纯数字则忽略
    if ! printf '%s\n' "$rss" | grep -q '^[0-9][0-9]*$'; then
      rss=0
    fi

    if [ "$rss" -gt "$top1_rss" ] 2>/dev/null; then
      top3_rss="$top2_rss"; top3_pid="$top2_pid"; top3_ppid="$top2_ppid"; top3_comm="$top2_comm"
      top2_rss="$top1_rss"; top2_pid="$top1_pid"; top2_ppid="$top1_ppid"; top2_comm="$top1_comm"
      top1_rss="$rss";      top1_pid="$pid";      top1_ppid="$ppid";      top1_comm="$comm"
    elif [ "$rss" -gt "$top2_rss" ] 2>/dev/null; then
      top3_rss="$top2_rss"; top3_pid="$top2_pid"; top3_ppid="$top2_ppid"; top3_comm="$top2_comm"
      top2_rss="$rss";      top2_pid="$pid";      top2_ppid="$ppid";      top2_comm="$comm"
    elif [ "$rss" -gt "$top3_rss" ] 2>/dev/null; then
      top3_rss="$rss";      top3_pid="$pid";      top3_ppid="$ppid";      top3_comm="$comm"
    fi
  done < "$file"

  out=""
  if [ "$top1_rss" -gt 0 ] 2>/dev/null; then
    out="P1(pid=$top1_pid,ppid=$top1_ppid,rss=${top1_rss}kB,comm=$top1_comm)"
  fi
  if [ "$top2_rss" -gt 0 ] 2>/dev/null; then
    [ -n "$out" ] && out="$out; "
    out="${out}P2(pid=$top2_pid,ppid=$top2_ppid,rss=${top2_rss}kB,comm=$top2_comm)"
  fi
  if [ "$top3_rss" -gt 0 ] 2>/dev/null; then
    [ -n "$out" ] && out="$out; "
    out="${out}P3(pid=$top3_pid,ppid=$top3_ppid,rss=${top3_rss}kB,comm=$top3_comm)"
  fi

  [ -n "$out" ] && printf '%s\n' "$out" || echo ""
}
##############################################################################
# 事件汇聚器（单例 RDWR）
##############################################################################
event_sink_rdwr() {
  exec 3<>"$EVENT_PIPE"  # RDWR 长持有，消除多读端分流
  write_json_line "$(now_ms)" "faultmon" "INFO" "event_sink" "-" "sink_started" "init"

  oldifs="$IFS"
  while :; do
    IFS= read -r L <&3 || { sleep 1; continue; }
    printf '%s\n' "$L" >>"$RAW_LOG"

    IFS="$SEP"; set -- $L; IFS="$oldifs"
    tsf="$1"; src="$2"; lvl="$3"; comp="$4"; pid="$5"; msg="$6"; tag="$7"
    [ -n "$tsf" ] || tsf="$(now_ms)"
    write_json_line "$tsf" "${src:-}" "${lvl:-}" "${comp:-}" "${pid:-}" "${msg:-}" "${tag:-}"
  done
}

start_event_sink_if_needed() {
  if [ -f "$SINK_PID_FILE" ]; then
    spid="$(cat "$SINK_PID_FILE" 2>/dev/null)"
    if [ -n "$spid" ] && kill -0 "$spid" 2>/dev/null; then
      return 0
    fi
  fi
  if mkdir "$SINK_LOCK_DIR" 2>/dev/null; then
    ( event_sink_rdwr ) >>"$SINK_OUT" 2>>"$ERR_LOG" &
    spid=$!
    echo "$spid" >"$SINK_PID_FILE"
    echo "$spid" >>"$CHILDREN_PIDS"
    sleep 1
    rmdir "$SINK_LOCK_DIR" 2>/dev/null || true
  else
    sleep 1
  fi
}

##############################################################################
# 规则与触发
##############################################################################
rule_state_path() { printf '%s/rule_%s.state\n' "$STATE_DIR" "$1"; }

rule_consider_ts() {
  # $1=name  $2=cond(0/1)  $3=ts_ms  $4=dur_thresh_ms
  name="$1"; cond="$2"; ts="$3"; need_ms="$4"
  stf="$STATE_DIR/rule_${name}.state"

  on_since=""; last_fire=""
  [ -f "$stf" ] && {
    on_since="$(sed -n 's/^on_since=//p' "$stf" 2>/dev/null)"
    last_fire="$(sed -n 's/^last_fire=//p' "$stf" 2>/dev/null)"
  }
  [ -z "$last_fire" ] && last_fire=0

  # 小工具：安全相减（毫秒时间戳 → (秒,毫秒) 拆分，全部用小整数计算）
  _diff_ms() {
    A="$1"; B="$2"   # A-B，都可能是13位ms，也可能是0
    [ -z "$A" ] && A=0
    [ -z "$B" ] && B=0
    # 特判：B=0 时直接返回一个很大值以通过冷却判定（避免巨大乘法）
    if [ "$B" -eq 0 ] 2>/dev/null; then
      # 这里应该返回 RULE_COOLDOWN_MS（或更大），保证第一次触发时冷却条件天然满足
      printf '%s\n' "$RULE_COOLDOWN_MS"
      return
    fi
    As="$(printf '%s' "$A" | sed 's/...$//')"
    Ams="$(printf '%s' "$A" | sed 's/^.*\(...\)$/\1/')"
    Bs="$(printf '%s' "$B" | sed 's/...$//')"
    Bms="$(printf '%s' "$B" | sed 's/^.*\(...\)$/\1/')"
    ds=$(( As - Bs ))
    dms=$(( Ams - Bms ))
    if [ "$dms" -lt 0 ]; then
      ds=$(( ds - 1 ))
      dms=$(( dms + 1000 ))
    fi
    printf '%s\n' $(( ds*1000 + dms ))
  }

  if [ "$cond" = "1" ]; then
    [ -z "$on_since" ] && on_since="$ts"

    span_ms="$(_diff_ms "$ts" "$on_since")"
    # 冷却：以 last_fire=0 视为已满足（_diff_ms 会返回 need_ms）
    since_fire_ms="$(_diff_ms "$ts" "$last_fire")"

    if [ "$span_ms" -ge "$need_ms" ] && [ "$since_fire_ms" -ge "$RULE_COOLDOWN_MS" ]; then
      { printf 'on_since=%s\n' "$ts"; printf 'last_fire=%s\n' "$ts"; } >"$stf"
      return 0   # 触发
    fi
    { printf 'on_since=%s\n' "$on_since"; printf 'last_fire=%s\n' "$last_fire"; } >"$stf"
    return 1
  else
    { printf 'on_since=\n'; printf 'last_fire=%s\n' "$last_fire"; } >"$stf"
    return 1
  fi
}

fire_rule_event() {
  ts="$(now_ms)"; name="$1"; msg="$2"

  # ★ 规则触发时抓一份 ps 快照，方便之后做进程级根因分析
  capture_ps_snapshot "$ts" "$name"
 # ★ 新增：基于刚才的 ps 快照，给出一个 “Top-3 RSS 嫌疑进程” 提示
  hint="$(build_callchain_hint "$ts")"
  if [ -n "$hint" ]; then
    msg="${msg} | top_rss: ${hint}"
  fi

  queue_event "$ts" "metrics" "WARN" "$name" "-" "$msg" "$name"
}

##############################################################################
# 指标采集
##############################################################################
read_memfree_kb() {
  grep '^MemFree:' /proc/meminfo 2>/dev/null | sed 's/[^0-9]//g'
}

read_load1_x100() {
  la="$(cut -d' ' -f1 /proc/loadavg 2>/dev/null)"
  if printf '%s' "$la" | grep -q '\.'; then
    int="$(printf '%s' "$la" | sed 's/\..*//; s/[^0-9]//g')"
    dec="$(printf '%s' "$la" | sed 's/.*\.//; s/[^0-9]//g; s/^\(.\{0,2\}\).*/\1/; s/^$/0/')"
    [ ${#dec} -eq 1 ] && dec="${dec}0"
    printf '%s%s\n' "$int" "$dec"
  else
    v="$(printf '%s' "$la" | sed 's/[^0-9]//g')"
    printf '%s00\n' "$v"
  fi
}

read_io_psi_avg10_x100() { printf '0\n'; }  # 大多无 PSI，恒 0
# -------[ Extra metrics for PyRCA: CPU / Mem / Disk / Net ]-------
# 注意：不使用 awk / tr，仅用内建 shell + cut/sed/grep。
# 这些函数只在 metrics_loop 中使用，对原有事件逻辑无影响。

read_cpu_jiffies_total_idle() {
  # 输出: "<total> <idle>"，读 /proc/stat 第一行
  if [ ! -r /proc/stat ]; then
    echo "0 0"
    return
  fi
  line="$(grep '^cpu ' /proc/stat 2>/dev/null | head -n 1)"
  [ -z "$line" ] && { echo "0 0"; return; }
  set -- $line
  # $1=cpu $2.. = jiffies
  u=$2; n=$3; s=$4; i=$5; w=$6; q=$7; sq=$8; st=$9
  [ -z "$u" ] && u=0; [ -z "$n" ] && n=0; [ -z "$s" ] && s=0
  [ -z "$i" ] && i=0; [ -z "$w" ] && w=0; [ -z "$q" ] && q=0
  [ -z "$sq" ] && sq=0; [ -z "$st" ] && st=0
  idle=$(( i + w ))
  total=$(( u + n + s + idle + q + sq + st ))
  echo "$total $idle"
}

read_meminfo_5fields_kb() {
  # 输出: "MemTotal MemFree MemAvailable SwapTotal SwapFree"（单位 KB）
  mt=0; mf=0; ma=0; st=0; sf=0
  if [ -r /proc/meminfo ]; then
    while IFS= read -r line; do
      case "$line" in
        MemTotal:*)
          v="$(printf '%s' "$line" | sed 's/[^0-9]//g')"
          [ -n "$v" ] && mt="$v"
          ;;
        MemFree:*)
          v="$(printf '%s' "$line" | sed 's/[^0-9]//g')"
          [ -n "$v" ] && mf="$v"
          ;;
        MemAvailable:*)
          v="$(printf '%s' "$line" | sed 's/[^0-9]//g')"
          [ -n "$v" ] && ma="$v"
          ;;
        SwapTotal:*)
          v="$(printf '%s' "$line" | sed 's/[^0-9]//g')"
          [ -n "$v" ] && st="$v"
          ;;
        SwapFree:*)
          v="$(printf '%s' "$line" | sed 's/[^0-9]//g')"
          [ -n "$v" ] && sf="$v"
          ;;
      esac
    done < /proc/meminfo
  fi
  echo "$mt $mf $ma $st $sf"
}

read_disk_sectors_rd_wr() {
  # 输出: "<rd_sectors> <wr_sectors>"，优先选 mmcblk0 / sda / nvme0n1
  if [ ! -r /proc/diskstats ]; then
    echo "0 0"
    return
  fi
  best_dev=""
  best_rest=""
  while read -r a b dev rest; do
    [ -z "$dev" ] && continue
    case "$dev" in
      mmcblk0) best_dev="$dev"; best_rest="$rest"; break;;
    esac
  done < /proc/diskstats
  if [ -z "$best_dev" ]; then
    while read -r a b dev rest; do
      [ -z "$dev" ] && continue
      case "$dev" in
        sd[a-z]|nvme0n1)
          best_dev="$dev"; best_rest="$rest"
          break
          ;;
      esac
    done < /proc/diskstats
  fi
  if [ -z "$best_dev" ]; then
    echo "0 0"
    return
  fi
  set -- $best_rest
  rd="$3"; wr="$7"
  [ -z "$rd" ] && rd=0
  [ -z "$wr" ] && wr=0
  echo "$rd $wr"
}

read_net_bytes_rx_tx() {
  # 输出: "<rx_bytes_total> <tx_bytes_total>"，汇总所有非 lo 网卡
  if [ ! -r /proc/net/dev ]; then
    echo "0 0"
    return
  fi
  rx_total=0
  tx_total=0
  while IFS= read -r line; do
    case "$line" in
      *:*)
        name="${line%%:*}"
        name="$(printf '%s' "$name" | sed 's/[^A-Za-z0-9_.]//g')"
        [ "$name" = "lo" ] && continue
        rest="${line#*:}"
        set -- $rest
        rx="$1"; tx="$9"
        [ -z "$rx" ] && rx=0
        [ -z "$tx" ] && tx=0
        rx_total=$(( rx_total + rx ))
        tx_total=$(( tx_total + tx ))
        ;;
    esac
  done < /proc/net/dev
  echo "$rx_total $tx_total"
}


to_x100() {
  s="$(printf '%s' "$1" | sed 's/[^0-9.]//g')"
  if printf '%s' "$s" | grep -q '\.'; then
    int="$(printf '%s' "$s" | sed 's/\..*//; s/[^0-9]//g')"
    dec="$(printf '%s' "$s" | sed 's/.*\.//; s/[^0-9]//g; s/^\(.\{0,2\}\).*/\1/; s/^$/0/')"
    [ ${#dec} -eq 1 ] && dec="${dec}0"
    printf '%s%s\n' "$int" "$dec"
  else
    printf '%s00\n' "$(printf '%s' "$s" | sed 's/[^0-9]//g')"
  fi
}

evaluate_rules() {
  ts="$1"; mf="$2"; l1x="$3"; iox="$4"

  mem_thr_kb="$MEM_PRESSURE_KB"
  cpu_thr_x100="$(to_x100 "$CPU_HOTSPOT_LOAD1")"
  io_thr_x100="$(to_x100 "$IO_PRESSURE_AVG10")"

  mem_need_ms="$(seconds_to_ms "$MEM_PRESSURE_SEC")"
  cpu_need_ms="$(seconds_to_ms "$CPU_HOTSPOT_SEC")"
  io_need_ms="$(seconds_to_ms "$IO_PRESSURE_SEC")"

  cond_mem=0; [ "$mf" -lt "$mem_thr_kb" ] && cond_mem=1
  if rule_consider_ts "mem_pressure" "$cond_mem" "$ts" "$mem_need_ms"; then
    fire_rule_event "mem_pressure" "MemFree=${mf}KB < ${mem_thr_kb}KB for ${MEM_PRESSURE_SEC}s"
  fi

  cond_cpu=0; [ "$l1x" -gt "$cpu_thr_x100" ] && cond_cpu=1
  if rule_consider_ts "cpu_hotspot" "$cond_cpu" "$ts" "$cpu_need_ms"; then
    fire_rule_event "cpu_hotspot" "load1_x100=${l1x} > ${cpu_thr_x100} for ${CPU_HOTSPOT_SEC}s"
  fi

  cond_io=0; [ "$iox" -gt "$io_thr_x100" ] && cond_io=1
  if rule_consider_ts "io_pressure" "$cond_io" "$ts" "$io_need_ms"; then
    fire_rule_event "io_pressure" "psi_io_avg10_x100=${iox} > ${io_thr_x100} for ${IO_PRESSURE_SEC}s"
  fi
}

metrics_loop() {
  csv="$(metrics_csv)"
  if [ ! -f "$csv" ]; then
    printf 'ts_ms,mem_free_kb,load1_x100,io_psi_avg10_x100,cpu_util_total_x100,cpu_idle_x100,mem_total_kb,mem_available_kb,swap_total_kb,swap_free_kb,disk_read_kBps,disk_write_kBps,net_rx_kBps,net_tx_kBps\n' >"$csv"
  fi

  # 上一轮采样的快照（仅在本函数内使用）
  cpu_prev_total=""
  cpu_prev_idle=""
  disk_prev_rd=""
  disk_prev_wr=""
  net_prev_rx=""
  net_prev_tx=""

  period="$METRICS_PERIOD_SEC"
  [ -z "$period" ] && period=2
  snap_count=0   # ★ 进程快照计数器
  if [ "$period" -le 0 ] 2>/dev/null; then
    period=2
  fi

  while :; do
    # 直接使用简单的 now_ms()，不再做 CLOCK_GUARD 校正
    ts="$(now_ms)"
    snap_count=$(( snap_count + 1 ))
    # 内存相关：一次性读 /proc/meminfo，减少开销
    set -- $(read_meminfo_5fields_kb)
    mem_total_kb="$1"
    mem_free_kb="$2"
    mem_avail_kb="$3"
    swap_total_kb="$4"
    swap_free_kb="$5"
    [ -z "$mem_free_kb" ] && mem_free_kb=0

    # loadavg + IO PSI
    load1_x100="$(read_load1_x100)"; [ -z "$load1_x100" ] && load1_x100=0
    io_psi_x100="$(read_io_psi_avg10_x100)"; [ -z "$io_psi_x100" ] && io_psi_x100=0

    # CPU 使用率（总 / 空闲），基于 /proc/stat 差分
    cpu_util_x100=-1
    cpu_idle_x100=-1
    set -- $(read_cpu_jiffies_total_idle)
    cpu_total="$1"
    cpu_idle="$2"
    if [ -n "$cpu_prev_total" ] && [ -n "$cpu_prev_idle" ]; then
      dt_total=$(( cpu_total - cpu_prev_total ))
      dt_idle=$(( cpu_idle - cpu_prev_idle ))
      if [ "$dt_total" -gt 0 ] 2>/dev/null; then
        busy=$(( dt_total - dt_idle ))
        if [ "$busy" -lt 0 ]; then busy=0; fi
        cpu_util_x100=$(( busy * 10000 / dt_total ))
        cpu_idle_x100=$(( dt_idle * 10000 / dt_total ))
      fi
    fi
    cpu_prev_total="$cpu_total"
    cpu_prev_idle="$cpu_idle"

    # 磁盘读写速率（kB/s），基于 sectors 差分
    disk_r_kBps=-1
    disk_w_kBps=-1
    set -- $(read_disk_sectors_rd_wr)
    rd_sectors="$1"
    wr_sectors="$2"
    if [ -n "$disk_prev_rd" ] && [ -n "$disk_prev_wr" ]; then
      d_rd=$(( rd_sectors - disk_prev_rd ))
      d_wr=$(( wr_sectors - disk_prev_wr ))
      if [ "$d_rd" -lt 0 ]; then d_rd=0; fi
      if [ "$d_wr" -lt 0 ]; then d_wr=0; fi
      denom=$(( 2 * period ))  # 512B * sectors -> 0.5kB，所以除以 (2*period)
      if [ "$denom" -le 0 ]; then denom=1; fi
      disk_r_kBps=$(( d_rd / denom ))
      disk_w_kBps=$(( d_wr / denom ))
    fi
    disk_prev_rd="$rd_sectors"
    disk_prev_wr="$wr_sectors"

    # 网络吞吐（kB/s），汇总所有非 lo 网卡
    net_rx_kBps=-1
    net_tx_kBps=-1
    set -- $(read_net_bytes_rx_tx)
    rx_bytes="$1"
    tx_bytes="$2"
    if [ -n "$net_prev_rx" ] && [ -n "$net_prev_tx" ]; then
      d_rx=$(( rx_bytes - net_prev_rx ))
      d_tx=$(( tx_bytes - net_prev_tx ))
      if [ "$d_rx" -lt 0 ]; then d_rx=0; fi
      if [ "$d_tx" -lt 0 ]; then d_tx=0; fi
      kB_rx=$(( d_rx / 1024 ))
      kB_tx=$(( d_tx / 1024 ))
      local_period="$period"
      if [ "$local_period" -le 0 ]; then local_period=1; fi
      net_rx_kBps=$(( kB_rx / local_period ))
      net_tx_kBps=$(( kB_tx / local_period ))
    fi
    net_prev_rx="$rx_bytes"
    net_prev_tx="$tx_bytes"

    printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' \
      "$ts" "$mem_free_kb" "$load1_x100" "$io_psi_x100" \
      "$cpu_util_x100" "$cpu_idle_x100" \
      "$mem_total_kb" "$mem_avail_kb" "$swap_total_kb" "$swap_free_kb" \
      "$disk_r_kBps" "$disk_w_kBps" "$net_rx_kBps" "$net_tx_kBps" >>"$csv"

        printf '%s\n' "$ts" >"$LAST_TS_MS"
    evaluate_rules "$ts" "$mem_free_kb" "$load1_x100" "$io_psi_x100"

    # ★ 周期性抓 ps 快照，防止长期无事件时 procs 目录为空
    if [ "$PROCS_SNAPSHOT_EVERY_N" -gt 0 ] 2>/dev/null; then
      mod=$(( snap_count % PROCS_SNAPSHOT_EVERY_N ))
      if [ "$mod" -eq 0 ] 2>/dev/null; then
        capture_ps_snapshot "$ts" "periodic"
      fi
    fi

    printf '%s\n' "$ts" >"$HEARTBEAT_TS"
    sleep "$METRICS_PERIOD_SEC"
  done
}



heartbeat_loop() {
  while :; do
    printf '%s\n' "$(now_ms)" >"$HEARTBEAT_TS"
    sleep 5
  done
}

watcher_loop() { while :; do sleep 60; done; }

# 判断 daemon 是否在跑：用 event_sink.pid + kill -0
is_daemon_running() {
  if [ -f "$SINK_PID_FILE" ]; then
    spid="$(cat "$SINK_PID_FILE" 2>/dev/null)"
    if [ -n "$spid" ] && kill -0 "$spid" 2>/dev/null; then
      return 0
    fi
  fi
  return 1
}

# 清理可能遗留的子进程（比如上一次 stop 没跑完）
cleanup_orphan_children() {
  if [ -f "$CHILDREN_PIDS" ]; then
    for c in $(cat "$CHILDREN_PIDS" 2>/dev/null); do
      [ -n "$c" ] || continue
      [ -d "/proc/$c" ] || continue
      kill_pid_silent "$c"
      sleep 1
      kill9_pid_silent "$c"
    done
    : >"$CHILDREN_PIDS" 2>/dev/null || true
  fi
}

# ★ 新增：在开启新一轮采样前清理旧的 metrics/procs
cleanup_metrics_and_procs() {
  # 只清理结构化采样数据，不动事件/日志
  rm -f "$METRICS_DIR"/sys_*.csv 2>/dev/null || true
  rm -f "$PROCS_DIR"/procs_*.txt 2>/dev/null || true
  rm -f "$LAST_TS_MS" 2>/dev/null || true
}

##############################################################################
# 守护管理
##############################################################################
start_daemon() {
  ensure_directories
  cleanup_orphan_children

 # ★ 新增：按需清理旧的 metrics/procs，避免跨 run 堆积
  if [ "${FAULTMON_CLEAN_ON_START:-1}" = "1" ]; then
    cleanup_metrics_and_procs
  fi

  ensure_event_pipe
  start_event_sink_if_needed
  [ "${HILOG_ENABLE:-0}" = "1" ] && "$0" hilog-start
  ( metrics_loop )   >>"$OUT_LOG" 2>>"$ERR_LOG" & echo $! >>"$CHILDREN_PIDS"
  ( heartbeat_loop ) >>"$OUT_LOG" 2>>"$ERR_LOG" & echo $! >>"$CHILDREN_PIDS"
  ( watcher_loop )   >>"$OUT_LOG" 2>>"$ERR_LOG" & echo $! >>"$CHILDREN_PIDS"
  echo "daemon started (pid $$)"
}


kill_pid_silent() { pid="$1"; [ -n "$pid" ] && kill "$pid" 2>/dev/null || true; }
kill9_pid_silent(){ pid="$1"; [ -n "$pid" ] && kill -9 "$pid" 2>/dev/null || true; }

stop_daemon() {
  if [ -f "$CHILDREN_PIDS" ]; then
    for c in $(cat "$CHILDREN_PIDS" 2>/dev/null); do [ -n "$c" ] && kill_pid_silent "$c"; done
    sleep 1
    for c in $(cat "$CHILDREN_PIDS" 2>/dev/null); do [ -n "$c" ] && kill9_pid_silent "$c"; done
    : >"$CHILDREN_PIDS" 2>/dev/null || true
  fi
  if [ -f "$SINK_PID_FILE" ]; then
    spid="$(cat "$SINK_PID_FILE" 2>/dev/null)"
    kill_pid_silent "$spid"; sleep 1; kill9_pid_silent "$spid"
    rm -f "$SINK_PID_FILE" 2>/dev/null || true
  fi
  rmdir "$SINK_LOCK_DIR" 2>/dev/null || true
  "$0" hilog-stop
  echo "daemon stopped"
}

status() {
  dstat="stopped"
  [ -f "$SINK_PID_FILE" ] && spid="$(cat "$SINK_PID_FILE" 2>/dev/null)" || spid=""
  [ -n "$spid" ] && kill -0 "$spid" 2>/dev/null && dstat="running"
  echo "daemon: $dstat"

  now="$(now_ms)"; hb="$(cat "$HEARTBEAT_TS" 2>/dev/null)"
  if [ -n "$hb" ]; then delay=$(( now - hb )); [ "$delay" -lt 0 ] && delay=0; else delay=0; fi
  echo "heartbeat_delay_ms: $delay"

  evf="$(today_json)"
  if [ -f "$evf" ]; then
    evc="$(wc -l < "$evf" 2>/dev/null)"; echo "events_today: $evc"
  else
    latest="$(ls -1 "$EVENTS_DIR"/events_*.jsonl 2>/dev/null | tail -n 1)"
    if [ -n "$latest" ] && [ -f "$latest" ]; then
      evc="$(wc -l < "$latest" 2>/dev/null)"; echo "events_today: $evc (latest)"
    else
      echo "events_today: 0"
    fi
  fi

  ac=$(ls -1 "$EVENTS_DIR"/events_*.jsonl 2>/dev/null | wc -l); [ -z "$ac" ] && ac=0
  echo "archives: $ac"

  held=0
  for p in /proc/[0-9]*; do
    ls -l "$p/fd" 2>/dev/null | grep -F "$EVENT_PIPE" >/dev/null && held=$((held+1))
  done
  echo "pipe_holders: $held"
}

##############################################################################
# 子命令
##############################################################################
cmd_start() {
  if is_daemon_running; then
    echo "daemon already running"
    return 0
  fi

  # 关键改动：
  # 把整个 start_daemon 丢到一个子 shell 里后台跑，
  # 这样 `faultmon.sh start` 本身会很快返回，不再把 hdc 卡住。
  ( start_daemon ) >>"$OUT_LOG" 2>>"$ERR_LOG" &
  echo "daemon started (pid $!)"
}

cmd_stop()   { stop_daemon; }
cmd_status() { status; }
cmd_poke() {
  tag="$1"
  ts="$(now_ms)"
  # 确保有 sink 在读 FIFO，避免在无人读时 open event.pipe 卡住
  start_event_sink_if_needed
  queue_event "$ts" "cli" "INFO" "poke" "-" "$tag" "poke"
  echo "poked:$tag"
}

cmd_pssnap() {
  ensure_directories
  ts="$(now_ms)"
  capture_ps_snapshot "$ts" "cli_pssnap"
  echo "ps snapshot written to $PROCS_DIR/procs_${ts}.txt"
}

##### ===== BEGIN HILOG SUPPORT ===== #####
BASE=${BASE:-$ROOT}
STATE=${STATE:-$STATE_DIR}
PIPE=${PIPE:-$EVENT_PIPE}
HILOG_PIDFILE="$STATE/hilog_ingest.pid"
HILOG_RATE=${HILOG_RATE:-50}
HILOG_BUFFERS=${HILOG_BUFFERS:-main,system}
HILOG_ENABLE=${HILOG_ENABLE:-0}

hilog_detect() {
  if command -v hilogcat >/dev/null 2>&1; then
    HILOG_CMD="hilogcat"
    HILOG_OPTS="-b $HILOG_BUFFERS -v threadtime"
  elif command -v logcat >/dev/null 2>&1; then
    HILOG_CMD="logcat"
    HILOG_OPTS="-v threadtime"
  else
    echo "hilog: neither hilogcat nor logcat found" >&2
    return 1
  fi
}

hilog_wait_sink() {
  mkdir -p "$STATE"
  [ -p "$PIPE" ] || { rm -f "$PIPE"; mkfifo "$PIPE"; chmod 666 "$PIPE"; }
  i=0
  while :; do
    if [ -f "$STATE/event_sink.pid" ]; then
      spid=$(cat "$STATE/event_sink.pid" 2>/dev/null)
      [ -n "$spid" ] && [ -d "/proc/$spid" ] && break
    fi
    i=$((i+1)); [ "$i" -gt 30 ] && break
    sleep 1
  done
}

hilog_level_from_line() {
  L=$(echo "$1" | cut -d' ' -f6 2>/dev/null | head -c1)
  case "$L" in
    V|v) echo VERBOSE;;
    D|d) echo DEBUG;;
    I|i) echo INFO;;
    W|w) echo WARN;;
    E|e) echo ERROR;;
    F|f) echo FATAL;;
    *)   echo INFO;;
  esac
}

hilog_ingest_run() {
  hilog_detect || exit 1
  hilog_wait_sink
  lim_sec=0; lim_cnt=0
  $HILOG_CMD $HILOG_OPTS 2>/dev/null | while IFS= read -r line; do
    [ -n "$line" ] || continue
    now=$(date +%s)
    if [ "$now" != "$lim_sec" ]; then lim_sec="$now"; lim_cnt=0; fi
    lim_cnt=$((lim_cnt + 1))
    [ "$lim_cnt" -le "$HILOG_RATE" ] || continue
    TS="${now}000"
    LVL=$(hilog_level_from_line "$line")
    printf '%s\001%s\001%s\001%s\001%s\001%s\001%s\n' \
      "$TS" "hilog" "$LVL" "hilog" "-" "$line" "hilog" \
      > "$PIPE" 2>/dev/null || true
  done
}

hilog_start() {
  [ -p "$PIPE" ] || { mkdir -p "$STATE"; rm -f "$PIPE"; mkfifo "$PIPE"; chmod 666 "$PIPE"; }
  if [ -f "$HILOG_PIDFILE" ]; then
    pid=$(cat "$HILOG_PIDFILE" 2>/dev/null)
    if [ -n "$pid" ] && [ -d "/proc/$pid" ]; then
      echo "hilog: already running (pid=$pid)"
      return 0
    fi
  fi
  if ! hilog_detect >/dev/null 2>&1; then
    echo "hilog: hilogcat/logcat not available"
    return 0
  fi
  nohup "$0" _hilog-run >/dev/null 2>&1 &
  echo $! > "$HILOG_PIDFILE"
  echo "hilog: started (pid=$(cat "$HILOG_PIDFILE" 2>/dev/null))"
}

hilog_stop() {
  if [ -f "$HILOG_PIDFILE" ]; then
    pid=$(cat "$HILOG_PIDFILE" 2>/dev/null)
    [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
    rm -f "$HILOG_PIDFILE"
    echo "hilog: stopped"
  else
    echo "hilog: not running"
  fi
}

hilog_status() {
  if [ -f "$HILOG_PIDFILE" ]; then
    pid=$(cat "$HILOG_PIDFILE" 2>/dev/null)
    if [ -n "$pid" ] && [ -d "/proc/$pid" ]; then
      echo "hilog: running (pid=$pid) rate=${HILOG_RATE}/s buffers=${HILOG_BUFFERS}"
      return 0
    fi
  fi
  echo "hilog: stopped"
}

##### ===== END HILOG SUPPORT ===== #####

##############################################################################
# 入口
##############################################################################
case "$1" in
  start)  shift; cmd_start "$@";;
  stop)   shift; cmd_stop;;
  status) shift; cmd_status;;
  poke)   shift; cmd_poke "$@";;
  pssnap) shift; cmd_pssnap;;   # ★ 新增：手动抓一份 ps 快照
  hilog-start)  hilog_start; exit 0;;
  hilog-stop)   hilog_stop; exit 0;;
  hilog-status) hilog_status; exit 0;;
  _hilog-run)   hilog_ingest_run; exit 0;;
  *) printf '%s\n' "usage: $0 {start|stop|status|poke <TAG>|hilog-start|hilog-stop|hilog-status}" \
       "  hilog-start|hilog-stop|hilog-status   Manage HiLog ingestion" \
       "  env HILOG_ENABLE=1                    Auto-start HiLog when running 'start'" \
       "  env HILOG_RATE=50 HILOG_BUFFERS=main,system   Tuning options"; exit 1;;
esac
