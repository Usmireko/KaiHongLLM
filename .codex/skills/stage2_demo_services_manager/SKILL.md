---
name: stage2_demo_services_manager
description: >
  Create/maintain a single server-side launcher script that starts/stops/status/restart/logs for:
  - tcp_ingest_server.py (18080)
  - tcp_actions_server.py (28081)
  - watch_and_infer.py
  Must use canonical project root /home/xrh/qwen3_os_fault and venv .venv_qwen3.
  Server connectivity MUST reuse skill: qwen3_server_runbook (ssh alias qwen3-server).
---

# Goal
One command to bring up Stage2 resident demo services, with pidfiles + logs + health checks.

# Canonical paths
- Project root: /home/xrh/qwen3_os_fault
- Venv: source /home/xrh/qwen3_os_fault/.venv_qwen3/bin/activate
- Inbox:  /home/xrh/qwen3_os_fault/storage/tcp_inbox
- Out:    /home/xrh/qwen3_os_fault/storage/tcp_out
- Runs:   /home/xrh/qwen3_os_fault/storage/runs
- Logs:   /home/xrh/qwen3_os_fault/storage/logs
- Pids:   /home/xrh/qwen3_os_fault/storage/pids

# Deliverable
Create: server_B/tcp/demo_services.sh
- supports: start | stop | status | restart | logs
- uses pidfiles under storage/pids/
- writes logs under storage/logs/
- `start` verifies ports 18080/28081 are LISTENing and watcher pid is alive.

# Required behavior (spec)
## start
- mkdir -p storage/logs storage/pids storage/tcp_inbox storage/tcp_out storage/runs
- export PYTHONUNBUFFERED=1
- start processes with nohup (or setsid) and capture PID:
  - python3 server_B/tcp/tcp_ingest_server.py --host 0.0.0.0 --port 18080 --out storage/tcp_inbox
  - python3 server_B/tcp/tcp_actions_server.py --host 0.0.0.0 --port 28081 --out storage/tcp_out
  - python3 server_B/tcp/watch_and_infer.py --inbox storage/tcp_inbox --out storage/tcp_out --runs_root storage/runs --poll_sec 2
- save pidfiles:
  - storage/pids/ingest.pid
  - storage/pids/actions.pid
  - storage/pids/watcher.pid
- health checks:
  - `ss -lntp | grep ':18080'` and `ss -lntp | grep ':28081'`
  - `kill -0 <watcher_pid>` and `ps -p <pid> -o cmd=`
- on failure: print last 50 lines of each log and exit non-zero.

## stop
- for each pidfile: send TERM; wait up to 3s; then KILL if still alive.
- remove pidfiles when stopped.

## status
- show each service: pid alive? listening? log tail 5 lines

## logs
- tail -n 80 storage/logs/{ingest,actions,watcher}.log

# Implementation notes
- Ensure venv activation is inside the script (use bash -lc).
- Do not daemonize via systemd; keep it a self-contained script.

# Acceptance
Run on controller:
ssh qwen3-server "cd /home/xrh/qwen3_os_fault && bash server_B/tcp/demo_services.sh restart && bash server_B/tcp/demo_services.sh status"
Must show:
- LISTEN on :18080 and :28081
- watcher pid alive
