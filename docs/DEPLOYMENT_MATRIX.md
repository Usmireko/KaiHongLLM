# Deployment Matrix

| Component | Business line | Runtime device | Entry | Config | Input | Output | Dependencies | Communicates with |
|---|---|---|---|---|---|---|---|---|
| NET injector | data_train_test | Board | `data_train_test/board/scripts/net_fault.sh` | env `NET_*`, `WK_NET_*` | fault mode/env | board network state and markers | BusyBox/Toybox shell, iptables/tc where available | Host collector over HDC |
| NET collector | data_train_test | Host | `scripts/run_collect.sh` | `configs/examples/data_train_test.env.example` | HDC target, fault env | `inbox/runs/<run_id>` | PowerShell, HDC | Board |
| NET validator | data_train_test | Host | `scripts/run_data_check.sh` | run path/env | run folder | PASS/UNCERTAIN/FAIL JSON/text | PowerShell | Local artifacts |
| Dataset export | data_train_test | Host | `scripts/run_export.sh` | run path/output path | run folder | L1 export files | PowerShell | Local artifacts |
| L2 derivation | data_train_test | Server/Host | `python data_train_test/server/src/derive_l2_case_samples.py` | CLI args | `canonical_case.json`, `evidence_candidates.jsonl` | L2 JSONL tasks | Python stdlib | Local artifacts |
| Dataset validation | data_train_test | Server/Host | `scripts/run_test.sh` | CLI args | L1/L2 directory | validation status | Python stdlib | Local artifacts |
| Demo board bundle | closed_loop_demo | Board | `closed_loop_demo/board/scripts/bundle_real_upload.sh` | `QWEN3_*` env | faultmon window | bundle upload | sh, busybox nc | Server ingest |
| Demo action poller | closed_loop_demo | Board | `closed_loop_demo/board/scripts/actions_poller_nc.sh` | `QWEN3_*` env | server action stream | action file/result upload | sh, busybox nc | Server actions/ingest |
| Demo action executor | closed_loop_demo | Board | `closed_loop_demo/board/scripts/actiond.sh` | allowlist in script | actions file | action result bundle | sh | Server ingest |
| TCP ingest server | closed_loop_demo | Server | `closed_loop_demo/server/src/server_B/tcp/tcp_ingest_server.py` | `QWEN3_INGEST_PORT` | TCP upload | `storage/tcp_inbox` | Python | Board |
| TCP actions server | closed_loop_demo | Server | `closed_loop_demo/server/src/server_B/tcp/tcp_actions_server.py` | `QWEN3_ACTIONS_PORT` | latest actions | TCP action response | Python | Board |
| Watcher/infer | closed_loop_demo | Server | `closed_loop_demo/server/src/server_B/tcp/watch_and_infer.py` | storage/model env | ready bundle marker | latest actions/status | Python, model runtime | Ingest/actions |
| Demo services | closed_loop_demo | Server | `closed_loop_demo/server/src/server_B/tcp/demo_services.sh` | `QWEN3_*` env | service command | pids/logs/status | bash, Python venv | Host/Board |
| Host demo orchestration | closed_loop_demo | Host | `scripts/start_demo.sh` | `configs/examples/closed_loop_demo.env.example` | HDC/SSH target args | demo summary | PowerShell, HDC, SSH | Board and Server |
