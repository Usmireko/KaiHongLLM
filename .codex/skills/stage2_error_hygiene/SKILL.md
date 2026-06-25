---
name: stage2_error_hygiene
description: >
  Ensure watcher writes a clear infer status for demo and does not leave stale latest_error.txt.
  On success: write latest_infer_status.txt=llm_ok and remove/clear latest_error.txt.
  On fallback: write latest_infer_status.txt=fallback and write latest_error.txt.
---

# Goal
Avoid demo confusion where latest_run_id is successful but latest_error.txt is from an older run.

# Target files (server)
- server_B/tcp/watch_and_infer.py

# Requirements
1) When bundle processing succeeds and watcher writes:
   - storage/tcp_out/<device>/latest_actions_device.txt
   - storage/tcp_out/<device>/latest_run_id.txt
   Then also:
   - storage/tcp_out/<device>/latest_infer_status.txt (content: "llm_ok\n")
   - If storage/tcp_out/<device>/latest_error.txt exists, delete it OR overwrite to empty.

2) When bundle processing fails and watcher writes fallback actions:
   - storage/tcp_out/<device>/latest_infer_status.txt (content: "fallback\n")
   - storage/tcp_out/<device>/latest_error.txt should contain the error reason (<=2KB)

# Acceptance
After a successful run_id:
- latest_infer_status.txt == llm_ok
- latest_error.txt absent or empty
After a forced-failure run (bad bundle):
- latest_infer_status.txt == fallback
- latest_error.txt exists and describes the failure.
