#!/bin/sh
EV_DIR=/data/faultmon/events
EV="$(ls -1 "$EV_DIR"/events_*.jsonl 2>/dev/null | tail -n 1)"

# 取计数（缺失时给 0）
c_mem() { grep -c '"tag":"mem_pressure"' "$EV" 2>/dev/null || echo 0; }
c_cpu() { grep -c '"tag":"cpu_hotspot"'  "$EV" 2>/dev/null || echo 0; }

# 只保留数字，规避粘贴混入的奇怪字符
num() { printf '%s\n' "$1" | sed 's/[^0-9]//g; s/^$/0/'; }

# 前后各取一次
b_mem="$(c_mem)"; b_cpu="$(c_cpu)"
sleep 70
a_mem="$(c_mem)"; a_cpu="$(c_cpu)"

# 净化为纯数字再做算术
am="$(num "$a_mem")"; bm="$(num "$b_mem")"
ac="$(num "$a_cpu")"; bc="$(num "$b_cpu")"

delta_mem=$((am-bm))
delta_cpu=$((ac-bc))

printf 'delta_mem=%s  delta_cpu=%s\n' "$delta_mem" "$delta_cpu"
