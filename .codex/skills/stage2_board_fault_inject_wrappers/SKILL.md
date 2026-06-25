---
name: stage2_board_fault_inject_wrappers
description: >
  Provide board-side manual fault injection wrappers used to demonstrate the chain.
  Must be safe, stoppable, and not require awk/tr/jq/seq/mktemp.
---

# Goal
Make demo repeatable:
- one command starts an injection
- one command stops it
- triggerd detects it and packages/uploads

# Deliverables (board)
Create under /data/faultmon/demo_stage2/bin:
- inject_cpu_busy.sh (start|stop|status)
- inject_mem_leak.sh (start|stop|status) [optional if available]
- inject_stop_all.sh

# Implementation strategy
1) Discover existing injectors:
   - wukong, cpu_busy_loop, memory_leak_demo, deadlock_demo etc.
   - search: find /data -maxdepth 4 -type f -name '*wukong*' -o -name '*cpu*busy*' -o -name '*leak*'
2) Use pidfiles under /data/faultmon/demo_stage2/pids/:
   - cpu_busy.pid, mem_leak.pid
3) start:
   - spawn injector in background with nohup (or &), redirect logs to /data/faultmon/demo_stage2/logs/
   - record PID
4) stop:
   - kill PID; wait; kill -9 if needed; remove pidfile
5) status:
   - kill -0 PID checks

# Acceptance
- ./inject_cpu_busy.sh start
- wait 5-10s, ensure load1 rises
- ./inject_cpu_busy.sh status shows running
- ./inject_cpu_busy.sh stop
