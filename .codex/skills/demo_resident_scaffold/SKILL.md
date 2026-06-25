---
name: demo_resident_scaffold
description: >
  Create resident-mode demo structure (board_A/server_B/shared). Must reuse existing connection skills:
  - Device ops via skill: hdc-kaihongos
  - Server ops via skill: qwen3_server_runbook (and qwen3_server_env_audit if needed)
---

# Purpose
Scaffold the demo-only resident-mode project layout:
- board_A/bin: faultmon.sh, bundle.sh, actiond.sh
- server_B/ingest + server_B/orchestrator
- shared/protocol/bundle_format.md

# Non-goals
- No auto trigger (faultwatchd)
- No experiment injection mode
- No Windows PowerShell orchestration logic (removed)

# Deliverables
- Directory skeleton
- bundle_format.md with: bundle naming, inner layout, minimal meta fields
