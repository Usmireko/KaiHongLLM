---
name: wk_edit_scripts
description: Edit run_wukong_collect_refactor.ps1, net_fault.sh, and run_wukong_weekend.ps1 to fix data-quality issues (DNS_PROOF, window alignment, hit evidence) with minimal diffs and regression via validators.
metadata:
  short-description: Edit collection/injection scripts with regression checks
---

# wk_edit_scripts

## Purpose / 目的
Make **minimal, targeted code edits** to improve data quality and pass validators (e.g. `wk_validate_run_dns_fail`).
Primary targets:
- `run_wukong_collect_refactor.ps1` (Windows-side orchestrator)
- `net_fault.sh` (device-side injection/proof)
- `run_wukong_weekend.ps1` (batch runner)

目标：解决常见失败点（DNS_PROOF 崩溃、run_window 对不齐、iptables 不命中、证据链不完整），并能通过验证技能回归。

---

## Inputs / 输入（若缺失则询问用户）
Accept any subset (use what is provided):
- `FAULT_TYPE`: e.g. `net_dns_fail`
- `SCENARIO_TAG`: e.g. `net_dns_fail_r01`
- `VALIDATION_REPORT`: pasted output from validator (preferred)
- `RUN_NAME`: failing run folder name (optional, for referencing evidence)
- `TARGET_FILES`: default is the three scripts above

If none are provided, assume the immediate goal is: **fix DNS_FAIL evidence chain**.

---

## Global constraints / 全局约束（必须遵守）
1) Device environment: **NO `awk` and NO `tr`** on board.
2) Naming: treat all as `net_fault` only; do NOT create `net_fault1/2` references.
3) Keep diffs minimal; do not refactor unrelated parts.
4) Avoid huge log spam; prefer focused greps.
5) DNS_PROOF commands:
   - each remote command line should be short (recommend < 200 chars)
   - avoid `if ... then ... else ... fi` blocks in a single remote command
   - avoid deeply nested quoting
6) Always ensure proof commands cannot interpret IP or `.nip.io` as a command due to quoting break.

---

## Workflow / 工作流程（固定步骤）
### Step 1 — Read failing evidence (from validator report)
Parse `VALIDATION_REPORT` and map to fixes:

- `DNS_PROOF_BROKEN`:
  - root causes: quoting, overlong commands, complex if/else, unescaped vars
  - fix: split into multiple short probes, remove if/else, print variables explicitly

- `NO_DNS_FAILURE_PROOF`:
  - root causes: no real resolution attempt, or attempt didn’t run
  - fix: always run at least one resolution attempt on a unique hostname (cache-busting)

- `OUT_OF_WINDOW`:
  - root causes: fault probes collected before run_window start
  - fix: move/define run_window to bracket injection + proof, or start window after injection

- `NO_HIT_EVIDENCE`:
  - root causes: no traffic to bad DNS; counters stay 0
  - fix: add explicit hit-test inside fault stage + capture counters after

### Step 2 — Decide minimal patch set (no unnecessary changes)
Prefer the smallest set that makes validator PASS:
- For DNS fail, usually only:
  - simplify DNS_PROOF probes in `run_wukong_collect_refactor.ps1`
  - ensure hit-test + unique DNS query exists (could be in PS side probes, or in net_fault.sh)
  - fix run_window alignment logic

### Step 3 — Edit files (in order)
1) `run_wukong_collect_refactor.ps1`
   - Make DNS_PROOF probes **short and linear**:
     - no if/else blocks
     - print: `NET_BAD_DNS`, `NET_PING_IP`, marker state
     - run: 1~2 DNS resolution attempts (cache-busting hostname)
     - run: explicit hit-test to `NET_BAD_DNS`
     - capture: iptables before/after with focused grep on bad DNS IP
   - Ensure quoting is safe for `hdc shell`:
     - avoid constructing a single giant shell line
     - prefer multiple append calls rather than one long command

2) `net_fault.sh`
   - Ensure injection action is deterministic:
     - create marker only after rules applied
     - remove marker only after rollback completed
   - Provide a simple, callable proof helper (optional):
     - print resolv.conf nameserver lines from effective path
     - perform one DNS query attempt (cache-busting)
   - Do not add dependencies; avoid awk/tr.

3) `run_wukong_weekend.ps1`
   - Ensure env vars passed correctly for DNS runs:
     - `NET_MODE`, `NET_BAD_DNS`, `NET_IFACE`, `NET_PING_IP` (if used)
   - Ensure scenario_tag naming consistent with validators.

### Step 4 — Regression checks (must run)
After edits:
- Static grep checks:
  - Ensure no references to `net_fault1`/`net_fault2`.
  - Ensure no `awk`/`tr` in device-side commands.
  - Ensure DNS_PROOF section still has BEGIN/END markers.
- If a sample run exists locally, instruct user to rerun one DNS collection then validate:
  - run collection once
  - run `$wk_validate_run_dns_fail RUN_NAME=<new_run>`
- If not possible to run now, clearly state what to run and what PASS looks like.

### Step 5 — Deliverables / 交付
- Provide **unified diffs** for the exact files changed (copy-paste ready).
- Summarize which validator failures each change addresses.

---

## DNS_FAIL specific patch guidelines / DNS_FAIL 专项修复原则
To pass the validator with high confidence:

1) DNS_PROOF must never break:
- avoid complex quoting and long lines
- no `if...else...fi` in one remote command

2) Must have at least one real DNS resolution attempt:
- use unique hostnames to bypass cache (e.g., based on uptime seconds)

3) Must produce hit evidence:
- iptables counters must be >0 OR explicit hit-test output present

4) Must align timestamps to run_window:
- Ensure fault probes are collected after run_window_start
- If current code starts run_window too late, move start earlier OR move probes later.

---

## Safety / 安全
- Do not suggest dangerous commands.
- Do not modify unrelated subsystems.
- Do not remove logging needed for debugging.

---
