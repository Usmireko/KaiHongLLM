#!/bin/sh
EV="$(ls -1 /data/faultmon/events/events_*.jsonl 2>/dev/null | tail -n 1)"
c_mem(){ grep -c '"tag":"mem_pressure"' "$EV" 2>/dev/null || echo 0; }
c_cpu(){ grep -c '"tag":"cpu_hotspot"'  "$EV" 2>/dev/null || echo 0; }
num(){ printf '%s\n' "$1" | sed 's/[^0-9]//g; s/^$/0/'; }

b_mem="$(c_mem)"; b_cpu="$(c_cpu)"
sleep 70
a_mem="$(c_mem)"; a_cpu="$(c_cpu)"

am="$(num "$a_mem")"; bm="$(num "$b_mem")"
ac="$(num "$a_cpu")"; bc="$(num "$b_cpu")"

inc_mem=0; [ "$am" -gt "$bm" ] && inc_mem=1
inc_cpu=0; [ "$ac" -gt "$bc" ] && inc_cpu=1
echo "mem_increased=$inc_mem (before=$bm, after=$am)"
echo "cpu_increased=$inc_cpu (before=$bc, after=$ac)"
