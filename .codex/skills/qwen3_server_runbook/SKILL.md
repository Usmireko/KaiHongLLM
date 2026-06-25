---
name: qwen3_server_runbook
description: Provide Qwen3 server runbook guidance and operational checklists for controlled workflows.
---

# SKILL: qwen3_server_runbook

## Purpose
Provide canonical, token-efficient commands to run Qwen3 SFT training and batch inference on server.
This skill assumes SSH alias `qwen3-server` is configured on the controller machine.

## Canonical paths (single source of truth)
- Server workdir: /home/xrh
- Project root: /home/xrh/qwen3_os_fault
- Venv activate: source /home/xrh/qwen3_os_fault/.venv_qwen3/bin/activate
- Base model: /home/xrh/models/Qwen/Qwen3-8B
- LoRA output: /home/xrh/qwen3_os_fault/qwen3_8b_fault_qlora
- Test set: /home/xrh/qwen3_os_fault/data/llm_sft_test.jsonl

## Guardrails
- Default is read-only checks + short commands.
- Do NOT print secrets.
- Do NOT modify system configs.
- Training can be long; capture logs to files under project root.

## Procedure

### 0) Quick health check (must run first)
Run on controller:
ssh qwen3-server "cd /home/xrh/qwen3_os_fault && \
  test -f .venv_qwen3/bin/activate && echo venv_ok=1 || echo venv_ok=0; \
  bash -lc 'source .venv_qwen3/bin/activate && python -V && pip -V'; \
  test -f train_qwen3_fault_qlora.py && echo train_py_ok=1 || echo train_py_ok=0; \
  test -f infer_qwen3_fault_2stage.py && echo infer_py_ok=1 || echo infer_py_ok=0; \
  test -f data/llm_sft_test.jsonl && echo testset_ok=1 || echo testset_ok=0; \
  test -d /home/xrh/models/Qwen/Qwen3-8B && echo base_model_ok=1 || echo base_model_ok=0; \
  test -d qwen3_8b_fault_qlora && echo lora_dir_ok=1 || echo lora_dir_ok=0"

Return a compact summary + risk_flags if any *_ok=0.

### 1) Run SFT/QLoRA training (explicit action)
Run:
ssh qwen3-server "cd /home/xrh/qwen3_os_fault && \
  bash -lc 'source .venv_qwen3/bin/activate && \
  python train_qwen3_fault_qlora.py 2>&1 | tee train_qlora_$(date +%Y%m%d_%H%M%S).log'"

Output requirements:
- exit code
- last 50 lines of log (tail)
- updated checkpoints under qwen3_8b_fault_qlora/ (list top 20 entries)

### 2) Run batch inference on llm_sft_test.jsonl (explicit action)
Run:
ssh qwen3-server "cd /home/xrh/qwen3_os_fault && \
  bash -lc 'source .venv_qwen3/bin/activate && \
  python infer_qwen3_fault_2stage.py 2>&1 | tee infer_2stage_$(date +%Y%m%d_%H%M%S).log'"

Output requirements:
- identify any produced output file(s) (new json/jsonl/csv/txt)
- last 80 lines of infer log
- if script prints metrics, capture them into JSON

### 3) Minimal artifact report (after train or infer)
Run:
ssh qwen3-server "cd /home/xrh/qwen3_os_fault && \
  echo '--- lora dir ---'; ls -la qwen3_8b_fault_qlora | head -n 120; \
  echo '--- checkpoints ---'; find qwen3_8b_fault_qlora -maxdepth 2 -type d -name 'checkpoint-*' | sort | tail -n 50"

## Output format (must return JSON)
{
  "server": {"alias":"qwen3-server","project":"/home/xrh/qwen3_os_fault"},
  "venv": {"activate":"/home/xrh/qwen3_os_fault/.venv_qwen3/bin/activate","python":"...","pip":"..."},
  "actions": {"train_ran":true/false,"infer_ran":true/false},
  "artifacts": {"base_model_dir":"/home/xrh/models/Qwen/Qwen3-8B","lora_dir":"...","checkpoints":[...],"outputs":[...]},
  "logs": {"train_log":"...","infer_log":"...","train_tail":[...],"infer_tail":[...]},
  "risk_flags":[...]
}
