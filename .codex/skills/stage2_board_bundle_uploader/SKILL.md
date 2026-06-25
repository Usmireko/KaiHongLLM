---
name: stage2_board_bundle_uploader
description: >
  Implement board-side scripts to produce and upload a run bundle to Stage2 ingest server (18080)
  using SSH port-forward (dbclient -B). Must not rely on awk/tr/jq/seq/mktemp (BusyBox/Toybox constraints).
  Must integrate with existing stage2 scripts:
  - /data/faultmon/demo_stage2/bin/uploader_nc.sh
  - /data/faultmon/device_id
---

# Goal
One command on board to create/find a bundle and upload it:
- Produces a file named: <run_id>__bundle.tar.gz
- Uploads via uploader_nc.sh:
  uploader_nc.sh --file <path> --type bundle --device <device_id> --run <run_id>

# Deliverables
Create under /data/faultmon/demo_stage2/bin/:
1) bundle_manual.sh
   - Creates minimal demo bundle when no real faultmon bundle exists.
   - Must create a temp run_dir under /data/faultmon/demo_stage2/tmp/<run_id>/
     with minimal structure:
       _run_meta.json
       metrics/sys_metrics.csv (can be 1 header line + 1 row)
       events/events.jsonl (can be empty)
       procs/procs.txt (can be empty)
   - Pack to /data/faultmon/demo_stage2/<run_id>__bundle.tar.gz
   - No awk/tr/jq; use cat/printf/echo/date/mkdir/find/tar

2) bundle_uploader.sh
   - Finds an existing bundle if available (preferred):
     find /data/faultmon -type f -name '*bundle*.tar.gz' | tail -n 1
     If found, copies/links to stage2 name format: <run_id>__bundle.tar.gz
   - Otherwise calls bundle_manual.sh to generate a minimal bundle.
   - Uploads using uploader_nc.sh with --type bundle.

# Notes
- run_id generation: use UTC-like timestamp without spaces:
  manual_$(date +%Y%m%d_%H%M%S)
  If date formatting is limited, fallback to seconds since epoch.
- Device id must read from /data/faultmon/device_id (create if missing).
- Keep logs short but explicit: print run_id and file size.

# Acceptance
On board:
- cd /data/faultmon/demo_stage2/bin
- ./bundle_uploader.sh --once --tag chain_smoke
Expect:
- uploader prints OK and upload_rc=0
On server:
- storage/tcp_inbox/dev1/<run_id>__bundle.tar.gz exists (and .done if ingest writes it)
