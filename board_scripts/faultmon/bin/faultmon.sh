#!/bin/sh
# faultmon.sh — KaiHongOS / OpenHarmony (toybox/ash) 兼容版
# 单文件守护：事件通道(RDWR单例) + 指标采集 + 规则触发 + 状态查询
#
# 兼容限制：只用 sh printf echo test [ ] date sleep kill ps grep fgrep egrep sed cat cut head tail wc ls mkdir rm rmdir readlink realpath tee usleep
# 禁止：awk/tr/jq/seq/mktemp/复杂 sed 扩展/外部 Python/BusyBox-only 命令
#
# 内置函数清单（名称 — 职责）
# - now_ms()                 — 毫秒时间（统一时间单位）
# - seconds_to_ms()          — 秒→毫秒转换
# - today_json()/metrics_csv() — 生成当日日志/指标文件路径
# - json_escape()            — 基础 JSON 转义（\ " \r\n\t）
# - ensure_directories()     — 初始化 ROOT/STATE/EVENTS/LOGS/METRICS 目录
# - ensure_event_pipe()      — 初始化事件 FIFO
# - write_json_line()        — 将 7 字段写入当日 JSONL
# - queue_event()            — 直接写 FIFO（^A 分隔的 7 段），toybox-safe
# - event_sink_rdwr()        — 单例 RDWR sink，解析 FIFO→JSONL（落盘）
# - start_event_sink_if_needed() — 基于 PID 文件 + 目录锁幂等拉起 sink
# - rule_state_path()        — 规则状态文件路径
# - rule_consider_ts()       — 持续计时 + 冷却判定（统一以 ms）
# - fire_rule_event()        — 触发规则事件（写 FIFO）
# - read_memfree_kb()/read_load1_x100()/read_io_psi_avg10_x100() — 采集原子项
# - to_x100()                — "浮点阈值"→整形x100
# - evaluate_rules()         — 统一规则评估入口（调用 rule_consider_ts）
# - metrics_loop()           — 周期采样→CSV，并驱动 evaluate_rules
# - heartbeat_loop()         — 心跳文件更新
# - watcher_loop()           — 预留监控占位（当前空转）
# - start_daemon()/stop_daemon()/status_daemon() — 守护启动/停止/状态
# - cmd_*()                  — 子命令封装（start/stop/status/poke）

##############################################################################
# 基本常量与路径
##############################################################################
ROOT=/data/faultmon
STATE_DIR="$ROOT/state"
LOGS_DIR="$ROOT/logs"
EVENTS_DIR="${EVENTS_DIR:-$ROOT/events}"   # 禁止写 /events，只能写 $ROOT/events
METRICS_DIR="$ROOT/metrics"

EVENT_PIPE="$STATE_DIR/event.pipe"
RAW_LOG="$LOGS_DIR/event_raw.log"
ERR_LOG="$LOGS_DIR/faultmon.err"
OUT_LOG="$LOGS_DIR/faultmon.out"
SINK_OUT="$LOGS_DIR/event_sink.out"

HEARTBEAT_TS="$STATE_DIR/heartbeat.ts"
SINK_LOCK_DIR="$STATE_DIR/event_sink.lock"
SINK_PID_FILE="$STATE_DIR/event_sink.pid"
CHILDREN_PIDS="$STATE_DIR/children.pids"

# 规则阈值（可通过环境覆盖）
: "${MEM_PRESSURE_KB:=655360}"         # KB，默认 ~640MB
: "${MEM_PRESSURE_SEC:=10}"            # 持续秒
: "${CPU_HOTSPOT_LOAD1:=4.0}"          # load1 阈值（浮点），内部转 x100
: "${CPU_HOTSPOT_SEC:=15}"             # 持续秒
: "${IO_PRESSURE_AVG10:=0.30}"         # PSI avg10 阈值（浮点，若无 PSI 恒 0）
: "${IO_PRESSURE_SEC:=20}"             # 持续秒
: "${RULE_COOLDOWN_MS:=60000}"         # 规则冷却 ms
: "${METRICS_PERIOD_SEC:=2}"           # 采样周期 s
METRICS_PERIOD_MS=$((METRICS_PERIOD_SEC*1000))

# 分隔符（^A）
SEP="$(printf '\001')"

##############################################################################
# 工具函数
##############################################################################
now_ms() { printf '%s000\n' "$(date +%s)"; }  # toybox 无 %N：用秒*1000
seconds_to_ms() { s="$1"; [ -z "$s" ] && s=0; echo $((s*1000)); }

today_json()   { printf '%s/events_%s.jsonl\n' "$EVENTS_DIR" "$(date +%Y%m%d)"; }
metrics_csv()  { printf '%s/sys_%s.csv\n'     "$METRICS_DIR" "$(date +%Y%m%d)"; }

json_escape() {
  # 输入 STDIN，输出单行：转义 \ 与 "，并将 \r\n\t 规整为空格
  sed 's/\\/\\\\/g; s/"/\\"/g; s/[\r\n\t]/ /g'
}

ensure_directories() {
  mkdir -p "$ROOT" "$STATE_DIR" "$LOGS_DIR" "$EVENTS_DIR" "$METRICS_DIR" 2>/dev/null
  : >"$CHILDREN_PIDS" 2>/dev/null || true
}

ensure_event_pipe() {
  if [ ! -p "$EVENT_PIPE" ]; then
    rm -f "$EVENT_PIPE" 2>/dev/null || true
    mkfifo "$EVENT_PIPE"
    chmod 600 "$EVENT_PIPE" 2>/dev/null || true
  fi
}

write_json_line() {
  # $1 ts $2 source $3 level $4 component $5 pid $6 msg $7 tag
  ts="$1"; src="$2"; lvl="$3"; comp="$4"; pid="$5"; msg="$6"; tag="$7"
  ts_e="$ts"
  src_e="$(printf '%s' "$src" | json_escape)"
  lvl_e="$(printf '%s' "$lvl" | json_escape)"
  comp_e="$(printf '%s' "$comp" | json_escape)"
  pid_e="$(printf '%s' "$pid" | json_escape)"
  msg_e="$(printf '%s' "$msg" | json_escape)"
  tag_e="$(printf '%s' "$tag" | json_escape)"
  printf '{"ts":%s,"source":"%s","level":"%s","component":"%s","pid":"%s","msg":"%s","tag":"%s"}\n' \
    "$ts_e" "$src_e" "$lvl_e" "$comp_e" "$pid_e" "$msg_e" "$tag_e" >>"$(today_json)"
}

queue_event() {
  # 直接向 FIFO 写入一行（^A 分隔），toybox-safe，不依赖临时文件
  # 字段：ts source level component pid msg tag
  ts="$1"; src="$2"; lvl="$3"; comp="$4"; pid="$5"; msg="$6"; tag="$7"
  # 若 sink 未启动，写 FIFO 会阻塞，因此默认由 daemon 保证 sink 先行
  printf '%s%s%s%s%s%s%s%s%s%s%s%s%s\n' \
    "$ts" "$SEP" "$src" "$SEP" "$lvl" "$SEP" "$comp" "$SEP" "$pid" "$SEP" "$msg" "$SEP" "$tag" >"$EVENT_PIPE"
}

##############################################################################
# 事件汇聚器（单例 RDWR 持有 FIFO）
##############################################################################
event_sink_rdwr() {
  # 关键：用 RDWR（3<>）长期持有 FIFO，消除“无读端阻塞/多持有者抢读”
  exec 3<>"$EVENT_PIPE"

  # 启动记录
  ts="$(now_ms)"
  write_json_line "$ts" "faultmon" "INFO" "event_sink" "-" "sink_started" "init"

  oldifs="$IFS"
  while :; do
    IFS= read -r L <&3 || { sleep 1; continue; }  # 避免 EOF 忙等
    printf '%s\n' "$L" >>"$RAW_LOG"

    IFS="$SEP"; set -- $L; IFS="$oldifs"
    tsf="$1"; src="$2"; lvl="$3"; comp="$4"; pid="$5"; msg="$6"; tag="$7"

    [ -z "$tsf" ] && tsf="$(now_ms)"
    : "${src:=}"; : "${lvl:=}"; : "${comp:=}"; : "${pid:=}"; : "${msg:=}"; : "${tag:=}"

    write_json_line "$tsf" "$src" "$lvl" "$comp" "$pid" "$msg" "$tag"
  done
}

start_event_sink_if_needed() {
  # 先看 PID 是否在、且存活（幂等）
  if [ -f "$SINK_PID_FILE" ]; then
    spid="$(cat "$SINK_PID_FILE" 2>/dev/null)"
    if [ -n "$spid" ] && kill -0 "$spid" 2>/dev/null; then
      return 0
    fi
  fi
  # 目录锁防并发启动
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

# 持续计时 + 冷却（统一以毫秒）
rule_consider_ts() {
  # $1=name  $2=cond(0/1)  $3=ts_ms  $4=dur_thresh_ms
  name="$1"; cond="$2"; ts="$3"; need_ms="$4"
  stf="$(rule_state_path "$name")"

  on_since=""; last_fire=""
  [ -f "$stf" ] && {
    on_since="$(sed -n 's/^on_since=//p' "$stf" 2>/dev/null)"
    last_fire="$(sed -n 's/^last_fire=//p' "$stf" 2>/dev/null)"
  }
  [ -z "$last_fire" ] && last_fire=0

  if [ "$cond" = "1" ]; then
    [ -z "$on_since" ] && on_since="$ts"

    # 仅用整数算术（toybox 友好）
    span_ms=$(( ts - on_since ))
    if [ "$span_ms" -ge "$need_ms" ]; then
      since_fire_ms=$(( ts - last_fire ))
      if [ "$since_fire_ms" -ge "$RULE_COOLDOWN_MS" ]; then
        { printf 'on_since=%s\n' "$ts"; printf 'last_fire=%s\n' "$ts"; } >"$stf"
        return 0
      fi
    fi
    { printf 'on_since=%s\n' "$on_since"; printf 'last_fire=%s\n' "$last_fire"; } >"$stf"
    return 1
  else
    { printf 'on_since=\n'; printf 'last_fire=%s\n' "$last_fire"; } >"$stf"
    return 1
  fi
}

fire_rule_event() {
  # $1 name  $2 message
  ts="$(now_ms)"
  name="$1"; msg="$2"
  queue_event "$ts" "metrics" "WARN" "$name" "-" "$msg" "$name"
}

##############################################################################
# 指标采集
##############################################################################
read_memfree_kb() {
  grep '^MemFree:' /proc/meminfo 2>/dev/null | sed 's/[^0-9]//g'
}

read_load1_x100() {
  # 取 /proc/loadavg 第一段，转为 x100 整数（例如 0.35->35, 4->400）
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

read_io_psi_avg10_x100() {
  # 多数 OpenHarmony 内核无 PSI：恒 0
  printf '0\n'
}

to_x100() {
  # "0.30" -> 30；"4.0" -> 400
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
  # $1 ts_ms  $2 mem_free_kb  $3 load1_x100  $4 io_psi_x100
  ts="$1"; mf="$2"; l1x="$3"; iox="$4"

  mem_thr_kb="$MEM_PRESSURE_KB"
  cpu_thr_x100="$(to_x100 "$CPU_HOTSPOT_LOAD1")"
  io_thr_x100="$(to_x100 "$IO_PRESSURE_AVG10")"

  mem_need_ms="$(seconds_to_ms "$MEM_PRESSURE_SEC")"
  cpu_need_ms="$(seconds_to_ms "$CPU_HOTSPOT_SEC")"
  io_need_ms="$(seconds_to_ms "$IO_PRESSURE_SEC")"

  # mem_pressure
  cond_mem=0; [ "$mf" -lt "$mem_thr_kb" ] && cond_mem=1
  if rule_consider_ts "mem_pressure" "$cond_mem" "$ts" "$mem_need_ms"; then
    fire_rule_event "mem_pressure" "MemFree=${mf}KB < ${mem_thr_kb}KB for ${MEM_PRESSURE_SEC}s"
  fi

  # cpu_hotspot
  cond_cpu=0; [ "$l1x" -gt "$cpu_thr_x100" ] && cond_cpu=1
  if rule_consider_ts "cpu_hotspot" "$cond_cpu" "$ts" "$cpu_need_ms"; then
    fire_rule_event "cpu_hotspot" "load1_x100=${l1x} > ${cpu_thr_x100} for ${CPU_HOTSPOT_SEC}s"
  fi

  # io_pressure
  cond_io=0; [ "$iox" -gt "$io_thr_x100" ] && cond_io=1
  if rule_consider_ts "io_pressure" "$cond_io" "$ts" "$io_need_ms"; then
    fire_rule_event "io_pressure" "psi_io_avg10_x100=${iox} > ${io_thr_x100} for ${IO_PRESSURE_SEC}s"
  fi
}

metrics_loop() {
  csv="$(metrics_csv)"
  if [ ! -f "$csv" ]; then
    printf 'ts_ms,mem_free_kb,load1_x100,io_psi_avg10_x100\n' >"$csv"
  fi

  while :; do
    ts="$(now_ms)"
    mf="$(read_memfree_kb)"; [ -z "$mf" ] && mf=0
    l1x="$(read_load1_x100)"; [ -z "$l1x" ] && l1x=0
    iox="$(read_io_psi_avg10_x100)"; [ -z "$iox" ] && iox=0

    printf '%s,%s,%s,%s\n' "$ts" "$mf" "$l1x" "$iox" >>"$csv"

    evaluate_rules "$ts" "$mf" "$l1x" "$iox"

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

watcher_loop() {
  # 预留：后续可加入进程/服务守护逻辑；当前空转，保证结构完备
  while :; do
    sleep 60
  done
}

##############################################################################
# 守护进程管理
##############################################################################
start_daemon() {
  ensure_directories
  ensure_event_pipe
  start_event_sink_if_needed

  # 后台启动 metrics/heartbeat/watch（记录子进程 PID）
  ( metrics_loop )   >>"$OUT_LOG" 2>>"$ERR_LOG" & echo $! >>"$CHILDREN_PIDS"
  ( heartbeat_loop ) >>"$OUT_LOG" 2>>"$ERR_LOG" & echo $! >>"$CHILDREN_PIDS"
  ( watcher_loop )   >>"$OUT_LOG" 2>>"$ERR_LOG" & echo $! >>"$CHILDREN_PIDS"

  echo "daemon started (pid $$)"
}

kill_pid_silent() { pid="$1"; [ -n "$pid" ] && kill "$pid" 2>/dev/null || true; }
kill9_pid_silent(){ pid="$1"; [ -n "$pid" ] && kill -9 "$pid" 2>/dev/null || true; }

stop_daemon() {
  # 优先杀子进程与 sink
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

  # 清理可能误持有 FIFO 的其它进程（避免 holders>1）
  for p in /proc/[0-9]*; do
    ls -l "$p/fd" 2>/dev/null | grep -F "$EVENT_PIPE" >/dev/null || continue
    pid="${p##*/}"
    [ -f "$SINK_PID_FILE" ] && sp="$(cat "$SINK_PID_FILE" 2>/dev/null)" || sp=""
    [ "$pid" = "$sp" ] && continue
    kill_pid_silent "$pid"
  done

  echo "daemon stopped"
}

status_daemon() {
  # daemon: running|stopped
  dstat="stopped"
  [ -f "$SINK_PID_FILE" ] && spid="$(cat "$SINK_PID_FILE" 2>/dev/null)" || spid=""
  [ -n "$spid" ] && kill -0 "$spid" 2>/dev/null && dstat="running"
  echo "daemon: $dstat"

  # heartbeat 延迟
  now="$(now_ms)"; hb="$(cat "$HEARTBEAT_TS" 2>/dev/null)"
  if [ -n "$hb" ]; then delay=$(( now - hb )); [ "$delay" -lt 0 ] && delay=0; else delay=0; fi
  echo "heartbeat_delay_ms: $delay"

  # events_today：若今日文件缺失，则回退到最新 events_*.jsonl，并标注 (latest)
  evf="$(today_json)"
  if [ -f "$evf" ]; then
    evc="$(wc -l < "$evf" 2>/dev/null)"; echo "events_today: $evc"
  else
    latest="$(ls -1 "$EVENTS_DIR"/events_*.jsonl 2>/dev/null | tail -n 1)"
    if [ -n "$latest" ] && [ -f "$latest" ]; then
      evc="$(wc -l < "$latest" 2>/dev/null)"; echo "events_today(latest): $evc"
    else
      echo "events_today: 0"
    fi
  fi

  # archives（events_*.jsonl 总数）
  ac=$(ls -1 "$EVENTS_DIR"/events_*.jsonl 2>/dev/null | wc -l)
  [ -z "$ac" ] && ac=0
  echo "archives: $ac"

  # pipe_holders：持有 FIFO 的进程个数（期望=1）
  held=0
  for p in /proc/[0-9]*; do
    ls -l "$p/fd" 2>/dev/null | grep -F "$EVENT_PIPE" >/dev/null && held=$((held+1))
  done
  echo "pipe_holders: $held"
}

##############################################################################
# 子命令
##############################################################################
cmd_start()  { start_daemon; }
cmd_stop()   { stop_daemon; }
cmd_status() { status_daemon; }
cmd_poke() {
  tag="$1"
  ts="$(now_ms)"
  queue_event "$ts" "cli" "INFO" "poke" "-" "$tag" "poke"
  echo "poked:$tag"
}

##############################################################################
# 入口
##############################################################################
case "$1" in
  start)   shift; cmd_start "$@";;
  stop)    shift; cmd_stop  "$@";;
  status)  shift; cmd_status;;
  poke)    shift; cmd_poke "$@";;
  *) echo "usage: $0 {start|stop|status|poke <TAG>}"; exit 1;;
esac
