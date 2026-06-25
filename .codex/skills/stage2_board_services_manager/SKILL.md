---
name: stage2_board_services_manager
description: >
  Create a single board-side launcher to manage resident demo components:
  - faultmon.sh (collector) [if available as a daemon]
  - triggerd.sh (trigger + pack + upload)
  - actions_poller_nc.sh (poll actions, execute, upload action_result)
  Must use BusyBox/Toybox only.
---

# Deliverable (board)
Create /data/faultmon/demo_stage2/bin/board_services.sh
supports: start | stop | status | logs

# start should:
- ensure device_id exists (dev1)
- start faultmon if not running (best effort)
- start triggerd.sh --daemon ...
- start actions_poller loop (if poller has no loop, create a small wrapper that runs --once in a sleep loop)
- write pidfiles under /data/faultmon/demo_stage2/pids/
- write logs under /data/faultmon/demo_stage2/logs/

# Acceptance
- board_services.sh restart
- status shows all pids alive
- inject_cpu_busy.sh start triggers an upload and actions execution within 1-2 minutes.
