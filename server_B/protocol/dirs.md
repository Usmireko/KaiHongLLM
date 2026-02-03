# Stage2 Directory Protocol (Board Direct -> Server)

This document defines the stage2 inbox/outbox directory layout on the server.

## Server storage roots

- Inbox (bundles from board):
  - /home/xrh/qwen3_os_fault/storage/inbox_bundles
- Runs (ingested bundles):
  - /home/xrh/qwen3_os_fault/storage/runs/<run_id>
- Actions out (per device/run):
  - /home/xrh/qwen3_os_fault/storage/out/<device_id>/<run_id>/actions_device.txt

## Atomic write rules

- Uploaders must write bundles as *.tmp then rename to *.tar.gz.
- watch_inbox.py writes actions_device.txt via .tmp then os.replace.

## Expected flow

1) Board uploads bundle_*.tar.gz into inbox_bundles/ (atomic .tmp -> mv).
2) watch_inbox.py ingests and runs closed loop.
3) actions_device.txt is published under out/<device_id>/<run_id>/.
4) Board polls and executes actions, then uploads action_result_bundle_*.tar.gz.

## Notes

- device_id is derived from _run_meta.json (device_sn/device_id); fallback is unknown_device.
- action_result bundles can be handled by a future watcher (TODO).
