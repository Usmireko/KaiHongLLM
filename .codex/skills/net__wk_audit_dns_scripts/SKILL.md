---
name: net__wk_audit_dns_scripts
description: Audit DNS/net scripts (run_wukong_collect_refactor.ps1, net_fault.sh, run_wukong_weekend.ps1) for robustness, window alignment, and anti-evidence-gaming rules.
metadata:
  short-description: Audit DNS/net scripts for stable PASS-grade evidence
---

# net__wk_audit_dns_scripts

## Purpose / 目的
Static audit of scripts that affect **net / DNS fail** data quality.
This is NOT a runtime validator for a run folder — it checks code patterns that commonly produce:
- DNS_PROOF_BROKEN
- NO_DNS_FAILURE_PROOF
- OUT_OF_WINDOW
- NO_HIT_EVIDENCE
and prevents “evidence gaming”.

---

## Inputs / 输入
Optional file paths (defaults assume you run Codex in repo root):
- `SCRIPT_PS1`: default `./run_wukong_collect_refactor.ps1`
- `SCRIPT_SH`:  default `./net_fault.sh`
- `SCRIPT_WEEKEND`: default `./run_wukong_weekend.ps1`

If a file is missing, ask user for the correct path.

---

## Output format / 输出格式（固定）
Print:
- `AUDIT: PASS|FAIL`
- `FILES: <resolved paths>`
- `FAILED_CHECKS:` list
- `WARNINGS:` list
- `NEXT_FIX:` short actionable bullets

---

## Checks / 审查项（DNS/net 专用）

### A) Device constraints (FAIL if violated)
- In `net_fault.sh` and any device-side probes embedded in PS1:
  - Must NOT contain `awk` or `tr`.
If found => `FAIL (DEVICE_TOOL_VIOLATION)`.

### B) DNS_PROOF robustness (FAIL if risky patterns exist)
In `run_wukong_collect_refactor.ps1` DNS_PROOF section:
- Prefer “many short remote commands”, avoid a single giant command string.
- FAIL if these appear in DNS_PROOF remote commands:
  - `if ... then ... else ... fi` (single-line control block)
  - extremely long quoted one-liners (heuristic)
  - nested quoting likely to break (e.g., repeated `\"` chains; unescaped `'` inside `'...'`)
- Require BEGIN/END markers printed literally:
  - `### DNS_PROOF_BEGIN`
  - `### DNS_PROOF_END`
If risky => `FAIL (DNS_PROOF_RISKY)`.

### C) Required evidence hooks (FAIL if missing)
PS1 DNS_PROOF must include capability to collect minimum evidence:
1) Print NET vars (`NET_BAD_DNS`, `NET_PING_IP`).
2) Capture effective resolv.conf candidates and show nameserver lines.
3) At least one cache-busting DNS attempt (e.g., nip.io with time/uptime).
4) Capture iptables before/after (or equivalent) near the DNS attempt, and the output must cover **all blocked DNS rules** in this run.
   - Accept examples: `iptables -L OUTPUT -n -v | head -n 40`
   - Do NOT accept: only printing `grep -F $NET_BAD_DNS` (may miss hits to other nameservers like 114/8.8).
Missing or too-narrow output => `FAIL (MISSING_EVIDENCE_HOOKS)`.

### D) Run window alignment hook (FAIL if missing)
In `run_wukong_collect_refactor.ps1`:
- run_window must cover fault probes/snapshots.
- FAIL if run_window_start is set after net_fault/probe_fault collection.
=> `FAIL (WINDOW_ALIGNMENT_RISK)`.

### E) Weekend runner NET_* passthrough (FAIL if missing)
In `run_wukong_weekend.ps1`:
- For net runs, NET_* variables must be passed through explicitly or set per-iteration.
- FAIL if NET_* are never set/forwarded,
  or can leak across runs without explicit reset/override.
=> `FAIL (NET_ENV_PASSTHROUGH_RISK)`.

### F) Anti evidence-gaming (FAIL for silent/irrelevant triggers; WARNING for weak alignment)
- FAIL if there is any **silent traffic trigger** used only to bump counters without marker output:
  - e.g., `ping ... >/dev/null 2>&1` without printing `### SELFTEST_*`.
  => `FAIL (EVIDENCE_GAMING_SUSPECT)`
- WARNING if hit evidence relies on ICMP ping to BAD_DNS:
  - ICMP is not DNS traffic; do not rely on it as primary PASS evidence.
  => `WARNING (EVIDENCE_ALIGNMENT_WEAK)`

---

## Notes / 备注
- Keep output short: list check IDs and the file/pattern that triggered it.
- Use this audit together with `wk_validate_run_common` + `net__wk_validate_run_dns_fail`.
