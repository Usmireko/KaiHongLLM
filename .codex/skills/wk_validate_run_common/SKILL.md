---
name: wk_validate_run_common
description: "Common validation for any run folder: completeness, probe/log sanity (no shell crash markers), and run_window time alignment."
metadata:
  short-description: Common run validation gate (all fault types)
---

# wk_validate_run_common

## Purpose / 目的
A fault-type-agnostic gate to catch bad runs early:
- required files exist
- probe outputs are not obviously broken
- timestamps align with `_run_meta.json` run window

This should be run **before** any fault-specific validator.

---

## Inputs / 输入
Accept:
- `RUN_NAME`: folder name under `./inbox/runs/` (preferred)
- `RUN_DIR`: full path (fallback)

Search roots (in order) if using RUN_NAME:
1) `./inbox/runs/`
2) `./inbox/run/`

If neither provided: select newest folder under the first existing root.

---

## Output format / 输出格式（固定）
Print:
- `RESULT: PASS|FAIL`
- `RUN_NAME: <name>`
- `RUN_DIR: <path>`
- `FAILED:` list
- `WARNINGS:` list
- `NEXT_FIX:` short bullets

---

## Checks / 检查项

### 0) Minimum file set (FAIL if missing)
Must exist:
- `_run_meta.json`
- at least one of `net/`, `cpu/`, `mem/` subfolders OR a known artifact folder used by this project
- at least one probe/log file (e.g., `net/probe_fault.txt` or equivalent)

If missing => `FAIL (INCOMPLETE_FILES)` and list missing.

### 1) Probe sanity (FAIL if obvious crash markers)
Scan all `probe_*.txt` under run dir (or known probe files):
- FAIL if any contains:
  - `/bin/sh:`
  - `syntax error`
  - `unmatched`
  - `inaccessible or not found`
  - `Needs 1 argument` (common symptom of broken variable interpolation)

=> `FAIL (PROBE_BROKEN)`.

### 2) Run window alignment (FAIL if outside)
From `_run_meta.json`, extract host window fields used in this project:
- `run_window_host_epoch_ms_start`
- `run_window_host_epoch_ms_end`
(or whichever run_window fields exist)

Then check that fault artifacts (e.g., `*fault*` files) have timestamps falling within that window
(allow small tolerance ±2 seconds).

If outside => `FAIL (OUT_OF_WINDOW)`.

### 3) Warnings (non-fatal)
- WARNING if key artifacts are empty files (size 0) where non-empty is expected.
- WARNING if scenario_tag missing/empty.

---

## Decision
- PASS if no FAIL checks.
- FAIL otherwise.

---

## Next
If PASS, run a fault-specific validator:
- net DNS: `wk_validate_run_dns_fail`
- cpu/mem/... (as you implement them)
