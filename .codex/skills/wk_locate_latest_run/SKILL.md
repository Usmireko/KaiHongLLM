---
name: wk_locate_latest_run
description: Locate the newest run folder under ./inbox/runs (or ./inbox/run), optionally by RUN_NAME, and perform a basic completeness check.
metadata:
  short-description: Find latest run and check key files
---

# wk_locate_latest_run

## Purpose / 目的
Locate a run folder produced by the collection pipeline:
- Preferred root: `./inbox/runs/`
- Fallback root: `./inbox/run/`

Then check whether the run has the minimum required files.

---

## Inputs / 输入
Accept any of:
- `RUN_NAME`: a folder name like `20260104_155814` (optional)
- `ROOT`: override search root (optional). If not set, use defaults.
- `MODE`: `latest` (default) or `by_name`

Rules:
- If `RUN_NAME` provided => `MODE=by_name`
- Else => `MODE=latest`

---

## Output format / 输出格式（固定）
Print:
- `FOUND: yes|no`
- `RUN_DIR: <path or empty>`
- `RUN_NAME: <name or empty>`
- `STATUS: OK|INCOMPLETE`
- `MISSING_FILES:` list (empty if OK)

---

## Steps / 步骤
1) Determine search roots:
   - If `ROOT` provided: use it only.
   - Else try in order:
     1) `./inbox/runs/`
     2) `./inbox/run/`

2) Select run folder:
   - If `MODE=by_name`: try `<root>/<RUN_NAME>` under each root.
   - If `MODE=latest`:
     - List directories under the first existing root.
     - Prefer newest by folder name (timestamp-like names sort lexicographically).
     - If names are not sortable, fallback to last modified time.

3) Basic completeness check (minimum set):
   Must exist:
   - `_run_meta.json`
   - `net/net_pre.txt`
   - `net/net_fault.txt`
   - `net/net_post.txt`
   - `net/probe_fault.txt`

4) Print report.

---

## Notes / 备注
- Keep output concise (no huge directory listings).
- If `FOUND=no`, print up to 10 available folder names under the first existing root to help user pick `RUN_NAME`.
