#!/bin/sh
STATE=/data/faultmon/state

# 读毫秒时间戳并转成秒（去掉最后3位），全部用32位安全的秒做运算
now_s=$(date +%s)
hb_ms=$(cat $STATE/heartbeat.ts 2>/dev/null || echo 0)
lt_ms=$(cat $STATE/last_ts_ms   2>/dev/null || echo 0)

hb_s=${hb_ms%???}; [ -z "$hb_s" ] && hb_s=0
lt_s=${lt_ms%???}; [ -z "$lt_s" ] && lt_s=0

delay_s=$(( now_s - hb_s ))
since_s=$(( now_s - lt_s ))

echo "heartbeat_delay_ms=$(( delay_s * 1000 ))"
echo "since_last_ts_ms=$(( since_s * 1000 ))"
echo "clock_offset=$(cat $STATE/clock.offset 2>/dev/null || echo 0)"
sinkpid=$(cat $STATE/event_sink.pid 2>/dev/null || echo 0)
echo "event_sink_pid=$sinkpid"
[ -n "$sinkpid" ] && [ -d "/proc/$sinkpid" ] && echo "event_sink=RUNNING" || echo "event_sink=NOT_RUNNING"

for r in cpu_hotspot io_pressure mem_pressure; do
  f="$STATE/rule_${r}.state"
  printf "%s=" "rule_${r}_state"
  if [ -s "$f" ]; then cat "$f"; else echo "N/A"; fi
done
