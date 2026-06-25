---
name: demo_transport_windows_bridge
description: >
  Windows-side bridge workflow for demo: Board <-> Windows via hdc, Windows <-> Server via ssh/scp.
  MUST reuse existing skills:
  - Device side: hdc-kaihongos
  - Server side: qwen3_server_runbook (qwen3-server alias)
---

# Purpose
Provide canonical PowerShell commands to:
1) recv bundle from board outbox
2) scp to server inbox
3) scp actions_device back
4) send actions_device to board inbox
5) recv action_result bundle and scp back to server

# Commands (PowerShell templates)
- Board outbox -> Windows:
  hdc file recv /data/faultmon/outbox/<bundle>.tar.gz .\_bundles\<bundle>.tar.gz

- Windows -> Server:
  ssh qwen3-server "mkdir -p /home/xrh/qwen3_os_fault/storage/inbox_bundles"
  scp .\_bundles\<bundle>.tar.gz qwen3-server:/home/xrh/qwen3_os_fault/storage/inbox_bundles/

- Server -> Windows:
  scp qwen3-server:/home/xrh/qwen3_os_fault/storage/runs/<run_id>/_server_out/actions_device.txt .\_actions\actions_device.txt

- Windows -> Board:
  hdc file send .\_actions\actions_device.txt /data/faultmon/inbox/actions_device.txt
