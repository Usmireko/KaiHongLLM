---
name: qwen3_server_env_audit
description: Audit the Qwen3 server environment and related harness state using read-only checks.
---

# SKILL: qwen3_server_env_audit

## 1) Purpose
Audit the Qwen3 inference/training server environment via SSH, focusing on:
- OS + hardware (CPU/RAM/disk)
- GPU + driver/CUDA
- Python + venv (.venv_qwen3) health
- Qwen3 project workspace under /home/xrh
- inference entrypoints & model artifacts presence
Return a structured JSON report for downstream closed-loop automation.

## 2) Scope / Non-goals
- No code changes on the server (read-only inspection by default).
- No long-running training jobs.
- No exposing secrets in logs/output.

## 3) Preconditions
- Controller machine (where Codex runs) can reach the server network.
- SSH client available.
- Authentication is non-interactive:
  - Preferred: SSH key (recommended)
  - Alternative: secret-injected password + sshpass (only if already installed)

Server target:
- user: xrh
- host: 10.70.1.17
- workdir: /home/xrh
- venv activate: `source qwen3_os_fault/.venv_qwen3/bin/activate`

## 4) Secrets Handling (MANDATORY)
- NEVER hardcode passwords in this skill file.
- Use one of:
  A) SSH Key (recommended)
     - IdentityFile: ~/.ssh/qwen3_server_ed25519 (example)
  B) Environment secret injection
     - e.g. export SERVER_PASS=... (do not print it)
- Any output MUST redact secrets.

## 5) Suggested one-time SSH key setup (human step)
(If key auth not ready, do this once outside Codex)
- On controller:
  - `ssh-keygen -t ed25519 -f ~/.ssh/qwen3_server_ed25519 -N ""`
  - `ssh-copy-id -i ~/.ssh/qwen3_server_ed25519.pub xrh@10.70.1.17`
- Add ~/.ssh/config:
  Host qwen3-server
    HostName 10.70.1.17
    User xrh
    IdentityFile ~/.ssh/qwen3_server_ed25519
    ServerAliveInterval 30
    ServerAliveCountMax 3

Then Codex uses:
- `ssh qwen3-server "..."`

## 6) Procedure (Codex must execute in order)

### Step 0 — Connectivity & identity
Run:
- `ssh qwen3-server "whoami && hostname && pwd"`

### Step 1 — System summary
Run:
- `ssh qwen3-server "uname -a; cat /etc/os-release 2>/dev/null || true"`
- `ssh qwen3-server "uptime; free -h || true"`
- `ssh qwen3-server "df -h / /home 2>/dev/null || df -h || true"`

### Step 2 — GPU / Driver / CUDA
Run (best-effort):
- `ssh qwen3-server "command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi || echo 'nvidia-smi not found'"`
- `ssh qwen3-server "command -v nvcc >/dev/null 2>&1 && nvcc --version || echo 'nvcc not found'"`

### Step 3 — Workspace discovery
Run:
- `ssh qwen3-server "cd /home/xrh && ls -la | head -n 200"`
- Locate likely repos/scripts (avoid awk/tr):
  - `ssh qwen3-server "cd /home/xrh && find . -maxdepth 3 -type f \\( -iname '*infer*.py' -o -iname '*train*.py' -o -iname '*serve*.py' -o -iname '*fault*.py' \\) | head -n 200"`

### Step 4 — Venv health check (.venv_qwen3)
Run:
- `ssh qwen3-server "cd /home/xrh && test -f .venv_qwen3/bin/activate && echo 'venv_ok=1' || echo 'venv_ok=0'"`
- `ssh qwen3-server "cd /home/xrh && bash -lc 'source .venv_qwen3/bin/activate && python -V && pip -V'"`

### Step 5 — Core Python deps sanity (torch/transformers/etc.)
Run:
- `ssh qwen3-server "cd /home/xrh && bash -lc 'source .venv_qwen3/bin/activate && python - <<\"PY\"\nimport sys\nprint(\"python\", sys.version)\ntry:\n  import torch\n  print(\"torch\", torch.__version__)\n  print(\"cuda_available\", torch.cuda.is_available())\n  if torch.cuda.is_available():\n    print(\"gpu0\", torch.cuda.get_device_name(0))\nexcept Exception as e:\n  print(\"torch_import_error\", repr(e))\nfor m in [\"transformers\",\"accelerate\",\"peft\",\"bitsandbytes\"]:\n  try:\n    __import__(m)\n    print(m, \"ok\")\n  except Exception as e:\n    print(m, \"err\", repr(e))\nPY'"`
- Optional (short) package snapshot:
  - `ssh qwen3-server "cd /home/xrh && bash -lc 'source .venv_qwen3/bin/activate && pip list | head -n 200'"`

### Step 6 — Qwen3 entrypoints & artifacts
Run:
- `ssh qwen3-server "cd /home/xrh && find . -maxdepth 5 -type d -iname '*lora*' -o -iname '*checkpoint*' -o -iname '*output*' | head -n 200"`
- For each discovered infer script (top 1–3), run help-only (no heavy run):
  - `ssh qwen3-server "cd /home/xrh && bash -lc 'source .venv_qwen3/bin/activate && python <PATH_TO_INFER_PY> --help'"`
  - If `--help` unsupported, do `python <file>.py -h` or `python -c "import runpy; runpy.run_path(...)"` (best-effort, but avoid executing full inference).

## 7) Output format (Codex must return JSON)
Return a single JSON object:

{
  "server": {
    "host": "10.70.1.17",
    "user": "xrh",
    "workdir": "/home/xrh"
  },
  "system": {
    "uname": "...",
    "os_release": "...",
    "uptime": "...",
    "disk": "...",
    "memory": "..."
  },
  "gpu": {
    "nvidia_smi_present": true/false,
    "nvidia_smi": "...",
    "nvcc": "..."
  },
  "venv": {
    "activate_path": "/home/xrh/.venv_qwen3/bin/activate",
    "python_version": "...",
    "pip_version": "...",
    "imports": {
      "torch": {"ok": true/false, "version": "...", "cuda_available": true/false, "gpu0": "...", "error": "..."},
      "transformers": {"ok": true/false, "error": "..."},
      "accelerate": {"ok": true/false, "error": "..."},
      "peft": {"ok": true/false, "error": "..."},
      "bitsandbytes": {"ok": true/false, "error": "..."}
    }
  },
  "workspace": {
    "top_entries": ["..."],
    "candidate_scripts": ["..."],
    "candidate_artifacts": ["..."],
    "notes": ["..."]
  },
  "risk_flags": [
    "venv_missing",
    "torch_import_error",
    "cuda_unavailable",
    "no_infer_entrypoint_found"
  ]
}

## 8) Safety / Guardrails
- Do not print or echo secrets.
- Avoid launching long processes (no training; no full inference).
- Prefer help/metadata checks.
- If any command fails, capture stderr snippet and continue.

