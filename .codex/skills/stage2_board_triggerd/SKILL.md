---
name: stage2_board_triggerd
description: >
  Create a board-side trigger daemon that detects an anomaly and then calls "real bundle pack" and uploader.
  Must not depend on awk/tr/jq/seq/mktemp. Must integrate with stage2 uploader and device_id.
---

# Goal
Replace bundle_manual with a real faultmon-driven bundle flow, without yet implementing a full trigger engine.
Provide:
- manual trigger: triggerd.sh --once
- daemon mode:   triggerd.sh --daemon --interval 1 --cooldown 60

# Deliverables (board)
Create under /data/faultmon/demo_stage2/bin:
- triggerd.sh

# How to detect anomaly (minimal, demo-friendly)
Default rule (configurable):
- If integer part of load1 >= 3  (from /proc/loadavg), then trigger.

Implementation without awk:
- la="$(cat /proc/loadavg)"; load1="${la%% *}"; int="${load1%%.*}"
- compare [ "$int" -ge 3 ]

# What to do on trigger
1) Determine run_id:
   - run_id="auto_$(date +%Y%m%d_%H%M%S 2>/dev/null || date +%s)"
2) Create real bundle:
   - Discover faultmon pack entrypoint by searching for scripts that produce "*__bundle.tar.gz" or "bundle_*.tar.gz"
   - Prefer calling the official pack command (whatever exists in your repo/device), and wait until file is finalized.
   - Add a stability check: size stable for 1s before uploading.
3) Upload via existing uploader:
   - uploader_nc.sh --file <bundle_path> --type bundle --device <device_id> --run <run_id>
4) Write a local marker:
   - /data/faultmon/demo_stage2/trigger_last_run_id.txt
   - /data/faultmon/demo_stage2/trigger_last_ts.txt

# Cooldown
After trigger fires, sleep cooldown seconds before checking again.

# Acceptance
- Start faultmon.sh in background (existing)
- Start triggerd.sh --daemon
- Manually inject CPU load to raise load1 >= 3
Expect: a new <run_id>__bundle.tar.gz uploaded to server tcp_inbox/dev1, and watcher produces latest_actions.
