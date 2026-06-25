---
name: demo_board_action_exec
description: >
  Implement board_A/bin/actiond.sh: execute actions_device.txt on device with whitelist,
  log actions_exec.log, and pack action_result bundle into outbox.
  Device ops and transfers MUST reuse skill: hdc-kaihongos.
---

# Input
- run --actions <path>

# Behavior
- Read file line by line:
  - skip empty lines and lines starting with '#'
- Whitelist by command prefix (default):
  - dmesg, cat, ps, top, head, tail, grep
- Execute each cmd with timeout (best-effort on toybox)
- Log per command:
  - start_ts_ms, end_ts_ms, exit_code, cmd, stdout_tail, stderr_tail
- Output:
  - /data/faultmon/logs/actions_exec.log
  - /data/faultmon/outbox/action_result_bundle_<...>.tar.gz

# Constraints
- No awk/tr/jq/seq/mktemp
- Use only basic sh utilities

# Acceptance
- Send actions_device.txt to /data/faultmon/inbox/
- Run:
  - /data/faultmon/demo/bin/actiond.sh run --actions /data/faultmon/inbox/actions_device.txt
- outbox has action_result_bundle_*.tar.gz
