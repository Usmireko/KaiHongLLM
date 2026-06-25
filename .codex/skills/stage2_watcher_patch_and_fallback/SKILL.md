---
name: stage2_watcher_patch_and_fallback
description: >
  Patch server_B/tcp/watch_and_infer.py so Stage2 auto-chain works:
  - Treat *.done as READY (not processed)
  - Use a separate processed marker (*.infer_done) to avoid reprocessing
  - Call server_B/orchestrator/run_closed_loop.py with explicit --out_root matching runs_root
  - Always update tcp_out/<device>/latest_run_id.txt and latest_actions_device.txt
    even if inference fails/OOM (fallback actions must be non-empty).
  Must reuse qwen3_server_runbook (ssh alias qwen3-server).
---

# Context
In Stage2 resident demo:
- ingest server writes: tcp_inbox/<device>/<run_id>__bundle.tar.gz and optionally a sibling .done marker.
- watcher should process only AFTER upload complete, so `.done` should be treated as "ready".
- watcher must write tcp_out/<device>/latest_actions_device.txt for actions_server to return LEN>0.

# Deliverables
Modify: server_B/tcp/watch_and_infer.py
Optionally add: small helper functions inside that file (no new modules required).

# Required watcher behavior
For each device dir under tcp_inbox:
- Consider bundle file: <run_id>__bundle.tar.gz
- Only process when: bundle exists AND bundle+'.done' exists (ready)
- Skip if: bundle+'.infer_done' exists (already processed, OK or ERROR)
- When processing:
  1) Run orchestrator:
     python3 server_B/orchestrator/run_closed_loop.py --bundle <bundle_path> --out_root <runs_root>
     NOTE: must pass --out_root to align outputs with runs_root (do not rely on run_closed_loop default).
  2) Determine expected actions path from returned run_dir OR from runs_root/<run_id>:
     - <run_dir>/_server_out/actions_device.txt
     - if missing, treat as failure.
  3) Success path:
     - write tcp_out/<device>/latest_actions_device.txt = actions_device.txt (non-empty)
     - write tcp_out/<device>/latest_run_id.txt = <run_id>
     - write bundle+'.infer_done' with "ok\n"
  4) Failure path (including LLM OOM / exceptions / missing outputs):
     - write tcp_out/<device>/latest_actions_device.txt with fallback non-empty script, e.g.
       `echo INFER_FAILED device=<device> run=<run_id>`
     - write tcp_out/<device>/latest_run_id.txt = <run_id>
     - write tcp_out/<device>/latest_error.txt with a short reason (first 2KB)
     - write bundle+'.infer_done' with "error:<reason>\n"
- Never block the loop forever; each item must transition to infer_done.

# Guardrails
- Do not delete inbox files.
- Avoid heavy parsing; no external deps.
- Keep logs: print one-line progress per bundle.

# Acceptance checklist
On server:
1) Start services (or just watcher) and place a test bundle with .done marker:
   - storage/tcp_inbox/dev1/test__bundle.tar.gz
   - storage/tcp_inbox/dev1/test__bundle.tar.gz.done
2) Observe:
   - storage/tcp_out/dev1/latest_run_id.txt == test
   - storage/tcp_out/dev1/latest_actions_device.txt exists and size > 0
   - storage/tcp_inbox/dev1/test__bundle.tar.gz.infer_done exists (ok or error)
3) Board poller must then receive LEN>0 via actions_server.
