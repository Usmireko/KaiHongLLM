---
name: demo_server_ingest_and_infer
description: >
  Implement server_B/ingest/ingest_bundle.py and server_B/orchestrator/run_closed_loop.py.
  Server connectivity MUST reuse skill: qwen3_server_runbook (ssh alias qwen3-server).
---

# ingest_bundle.py
Input: --bundle <path> --out_root server_B/storage/runs
- Safe extract (block .. and absolute paths)
- Determine run_id from manifest.json or filename
- Ensure run_dir contains:
  - _run_meta.json
  - metrics/sys_*.csv
  - events/events_*.jsonl
  - procs/procs_*.txt
- Patch _run_meta.json minimal fields if missing

# run_closed_loop.py
Input: --bundle <path> OR --run_dir <path>
- If bundle: call ingest_bundle.py -> get run_dir
- Run inference:
  - source /home/xrh/qwen3_os_fault/.venv_qwen3/bin/activate
  - python closed_loop_infer_run.py --run_dir ... --out_dir .../_server_out
- Default ensure env WK_QWEN3_ENABLE_STAGE2=0
- Generate actions_device.txt from actions.json (one cmd per line)

# Acceptance
- ssh qwen3-server "cd /home/xrh/qwen3_os_fault && source .venv_qwen3/bin/activate && python server_B/orchestrator/run_closed_loop.py --bundle storage/inbox_bundles/<bundle>"
- run_dir/_server_out contains diagnosis.json + actions.json + actions_device.txt
