# KaiHongLLM

This repository contains the closed-loop fault diagnosis pipeline, server components, and demo scripts.

## What is included
- Closed-loop inference code (server-side)
- Server TCP action service (server_B)
- Demo scripts (Windows) for stage2 workflow
- Board-side scripts (snapshot) under oard_scripts/

## What is NOT included (kept as empty folders)
Large models and run data are excluded from GitHub. The following folders are placeholders only:
- data/ (SFT datasets)
- storage/runs/ (run evidence)
- storage/logs/ / storage/tcp_inbox/ / storage/tcp_out/ / storage/tcp_runs/ / storage/in_action_results/ / storage/pids/
- qwen3_8b_fault_qlora/ (LoRA adapter checkpoints)

## Data and model locations (local)
Server:
- Datasets: /home/xrh/qwen3_os_fault/data/ (e.g., llm_sft*.jsonl)
- Runs: /home/xrh/qwen3_os_fault/storage/runs/
- LoRA adapters: /home/xrh/qwen3_os_fault/qwen3_8b_fault_qlora/
- Base models: /home/xrh/models/

Board (device):
- Scripts: /data/faultmon/demo_stage2/bin/ and /data/faultmon/
- Binaries: /data/local/tmp/out_static_arm64/

Windows:
- Demo scripts: C:\Users\Administrator\Desktop\work\tools\demo_stage2.ps1 and related scripts

## Notes
- oard_scripts/ contains a snapshot of board-side scripts for reference. The live board scripts are on the device paths above.
- windows/ contains Windows demo scripts copied from /work.
