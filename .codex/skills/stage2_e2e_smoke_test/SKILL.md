---
name: stage2_e2e_smoke_test
description: >
  Run a deterministic end-to-end Stage2 smoke test until pass:
  - Server: restart demo services (ingest/actions/watcher)
  - Board: upload one bundle (manual ok)
  - Server: verify tcp_out latest_actions updated (LEN>0)
  - Board: run actions_poller once, execute actiond, upload action_result
  - Server: verify action_result landed in tcp_inbox
  Must avoid awk/tr in board commands. Must reuse: hdc-kaihongos + qwen3_server_runbook.
---

# Inputs
- device_id: dev1
- server alias: qwen3-server
- ports: ingest 18080, actions 28081

# Procedure (canonical)
## 1) Server: restart and status
ssh qwen3-server "cd /home/xrh/qwen3_os_fault && bash server_B/tcp/demo_services.sh restart && bash server_B/tcp/demo_services.sh status"

## 2) Board: ensure device_id and generate+upload bundle
hdc shell "mkdir -p /data/faultmon; echo dev1 > /data/faultmon/device_id"
hdc shell "cd /data/faultmon/demo_stage2/bin; ./bundle_uploader.sh --once --tag chain_smoke"

## 3) Server: wait for watcher to update tcp_out
ssh qwen3-server "cd /home/xrh/qwen3_os_fault &&   ls -la storage/tcp_inbox/dev1 | tail -n 20;   cat storage/tcp_out/dev1/latest_run_id.txt 2>/dev/null || true;   wc -c storage/tcp_out/dev1/latest_actions_device.txt 2>/dev/null || true;   head -n 5 storage/tcp_out/dev1/latest_actions_device.txt 2>/dev/null || true;   tail -n 30 storage/logs/watcher.log 2>/dev/null || true"

If latest_actions is empty, treat as FAIL and inspect watcher/actions logs.

## 4) Board: fetch/execute/upload action_result
hdc shell "cd /data/faultmon/demo_stage2/bin; ./actions_poller_nc.sh --once --verbose; echo rc=\$?;   /data/local/tmp/busybox ls -la /data/faultmon/demo_stage2/action_result_bundle_* 2>/dev/null | /data/local/tmp/busybox tail -n 3"

## 5) Server: verify action_result landed
ssh qwen3-server "cd /home/xrh/qwen3_os_fault &&   ls -la storage/tcp_inbox/dev1 | grep action_result | tail -n 10 || true"

# Output format (must be JSON)
{
  "run_id":"...",
  "server":{"inbox_bundle":true,"out_actions_nonempty":true,"inbox_action_result":true},
  "board":{"poller_len_gt0":true,"actiond_rc":0,"upload_rc":0},
  "logs_tail":{"watcher":[...],"actions":[...],"ingest":[...]},
  "pass":true/false,
  "next_fix_hint":"..."
}

# Loop policy
If pass=false, run at most 10 iterations:
- Restart services
- Re-upload a new run_id bundle
- Re-run poller
Stop early once pass=true.
