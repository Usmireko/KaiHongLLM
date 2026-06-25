---
name: net__wk_validate_run_dns_fail
description: Validate a net_dns_fail run folder using minimum evidence chain (marker/iptables/DNS_PROOF/time-window) and output PASS/UNCERTAIN/FAIL. Supports PASS via functional DNS toggle even when iptables delta is zero.
metadata:
  short-description: Validate DNS fail runs
---

# net__wk_validate_run_dns_fail

## Purpose / 目的
Validate whether a collected run labeled as **net_dns_fail** is *usable for training* based on a **minimum evidence chain**:
- Probe executed successfully (no shell syntax break)
- Fault state truly enabled (marker/iptables)
- DNS resolution attempt fails inside the run window
- Evidence aligns with injection (iptables hit / explicit hit-test / or functional DNS toggle)
- All key evidence occurs **within run_window** in `_run_meta.json`

用于判断：这条 **DNS 故障注入**数据是否“合格可入库”（PASS），或证据不足但可能有效（UNCERTAIN），或不合格（FAIL）。

---

## Inputs / 输入
Accept either:
- `RUN_NAME`: the run folder name under `./inbox/runs/` (preferred), e.g. `20260104_155814`
- `RUN_DIR`: full path to the run folder (fallback)

If both are provided, prefer `RUN_DIR`.

Default search roots (in order):
1) `./inbox/runs/`
2) `./inbox/run/`

If neither `RUN_NAME` nor `RUN_DIR` is provided:
- Select the newest folder under `./inbox/runs/` (or `./inbox/run/` if runs/ does not exist).
- If no folders exist, ask user to provide `RUN_NAME` or `RUN_DIR`.

---

## Environment / 环境
- This validation runs on the **host side** (Windows PowerShell preferred).
- Do **NOT** require awk/tr on device. (BusyBox/Toybox device constraint.)

---

## Output format / 输出格式（固定）
Print a concise report:

- `RESULT: PASS|UNCERTAIN|FAIL`
- `RUN_ID: <id>`
- `SCENARIO: <scenario_tag>`
- `FAILED_CRITERIA:` (list, empty if PASS)
- `WARNINGS:` (list)
- `NEXT_FIX:` (actionable one-liners)

---

## Minimum Evidence Checklist / 最小证据链（判卷标准）

### 0) File completeness / 文件齐全（FAIL if missing）
Must exist:
- `_run_meta.json`
- `net/net_pre.txt`
- `net/net_fault.txt`
- `net/net_post.txt`
- `net/probe_fault.txt`

Missing any => `FAIL (INCOMPLETE_FILES)`.

Optional but recommended (used for E3 toggle evidence):
- `net/probe_pre.txt`
- `net/probe_post.txt`

If optional missing:
- Add `WARNING (NO_PRE_POST_PROBES)`
- E3 cannot be satisfied.

---

### A) Probe must have executed cleanly / 探针必须跑通（FAIL if broken）
In `net/probe_fault.txt`:
- Must contain both:
  - `### DNS_PROOF_BEGIN`
  - `### DNS_PROOF_END`
- Must **NOT** contain any of:
  - `/bin/sh:`
  - `syntax error`
  - `unmatched`
  - `inaccessible or not found`

If broken => `FAIL (DNS_PROOF_BROKEN)`.

---

### B) Fault state must be enabled / 必须证明进入 fault 态（FAIL if absent）
Require at least one of:

**B1 Marker evidence**
- `net/probe_fault.txt` shows marker file exists, e.g.
  - `/data/local/tmp/net_fault_state/iptables_dns_block.applied`

**B2 iptables evidence**
- `net/net_fault.txt` shows iptables OUTPUT chain has a DROP rule targeting `NET_BAD_DNS` (e.g. `127.0.0.2`)
- That DROP rule must be absent in `net/net_pre.txt` and absent in `net/net_post.txt` (reversible)

If neither B1 nor B2 => `FAIL (FAULT_NOT_ENABLED)`.

> NOTE: never mention net_fault1/2; treat all as net_fault.

---

### C) Resolver config evidence / DNS 配置证据（missing => WARNING, may affect result)
In `net/probe_fault.txt`, require at least one resolv.conf candidate to show **nameserver lines**.
- Prefer evidence that the effective config points to `NET_BAD_DNS` and/or lists the DNS servers being blocked.

If missing:
- Add `WARNING (NO_NAMESERVER_EVIDENCE)`
- This alone does not FAIL, but reduces confidence.

---

### D) Must show a DNS resolution attempt that fails / 必须有解析尝试 + 明确失败（FAIL if absent）
In `net/probe_fault.txt` DNS_PROOF section:
- Must show at least one **resolution attempt** (getent/nslookup/ping domain) with output indicating failure:
  - timeout, no servers could be reached, temporary failure in name resolution, SERVFAIL, `Name does not resolve`, etc.

If no resolution attempt or no failure output => `FAIL (NO_DNS_FAILURE_PROOF)`.

---

### E) Must show alignment with injection / 必须证明“与注入一致”（hit evidence OR functional toggle）
> 背景：某些环境下 DNS 失败可能在本地解析层就返回错误，或者探针的网络栈路径不经过你抓的计数点，导致 iptables counters 不增长。
> 因此：允许用“功能闭环证据（pre OK → fault FAIL → post OK）”作为 PASS 依据。

Define `BLOCKED_DNS_IPS` as:
1) All IPs found in `nameserver <IP>` lines printed in the DNS_PROOF resolv.conf section, AND/OR
2) All destination IPs of `DROP` rules shown in the DNS_PROOF iptables OUTPUT before/after blocks.

Alignment evidence satisfies **any one** of:

**E1 iptables delta increased for any blocked DNS (strong hit evidence)**
- Compare the iptables OUTPUT “before” vs “after” blocks collected inside DNS_PROOF.
- For any IP in `BLOCKED_DNS_IPS`, if there exists a `DROP ... <destIP>` rule whose pkts or bytes increases delta > 0,
  then hit evidence is satisfied.

**OR**

**E2 DNS-aligned trigger evidence (explicit hit-test)**
- Evidence of a DNS-aligned trigger that targets a blocked DNS server (UDP/TCP 53 semantics) and is explicitly logged
  (e.g., `### SELFTEST_*`).
- Do NOT accept ICMP-only ping as primary hit evidence.

**OR**

**E3 Functional DNS toggle evidence (允许 PASS，即使 E1/E2 缺失)**
Require all:
- Pre success: in `net/probe_pre.txt` (preferred) or `net/net_pre.txt`, show at least one DNS-based success
  (e.g., `### ping_nip_io` reports `1 received`, or `getent hosts <domain>` returns an IP).
- Fault failure: already satisfied by (D) inside `net/probe_fault.txt`.
- Post success: in `net/probe_post.txt` (preferred) or `net/net_post.txt`, show DNS-based success again.
- Control: during fault, IP reachability still OK (e.g., `ping -c 3 1.1.1.1` shows `0% packet loss`)
  to exclude link-down/route-down masquerading as DNS failure.

If E3 is satisfied but E1/E2 is missing:
- Still allow `PASS`
- Add `WARNING (NO_HIT_EVIDENCE)`
- `NEXT_FIX`: recommend adding UDP/53 hit-test to make counters reliably increment (optional enhancement)

If none of E1/E2/E3:
- Result cannot be PASS
- Mark as `UNCERTAIN (NO_ALIGNMENT_EVIDENCE)` if A+B+D+F satisfied

---

### F) Time-window alignment / 时间窗对齐（FAIL if outside）
All fault evidence (probe_fault / net_fault) must fall within:
- `_run_meta.json` `run_window_start_ms` ~ `run_window_end_ms`
- Allow small tolerance (±2 seconds).

If probe_fault/net_fault timestamps are outside => `FAIL (OUT_OF_WINDOW)`.

---

## Decision rules / 判定规则（固定）
- **PASS**: A + B + D + F satisfied, and E satisfied (E1 or E2 or E3)
  - If PASS via E3 only (no E1/E2), keep `WARNING (NO_HIT_EVIDENCE)` but do not downgrade.
- **UNCERTAIN**: A + B + D + F satisfied, but E missing (no E1/E2/E3) OR evidence weak (e.g., C missing + no toggle)
- **FAIL**: any of A, B, D, F fails OR files incomplete

---

## Path resolution / 路径解析（必须做）
1) If `RUN_DIR` is provided and exists, use it.
2) Else if `RUN_NAME` is provided:
   - Try `./inbox/runs/<RUN_NAME>`
   - Then `./inbox/run/<RUN_NAME>`
   - If still not found, fail with:
     - `FAIL (RUN_NOT_FOUND)`
     - Print available run folders (top 10) under existing roots.
3) Else (no inputs):
   - Pick newest folder under `./inbox/runs/` if exists; otherwise under `./inbox/run/`.
   - If none, ask user for `RUN_NAME`.

## Recommended implementation steps (host-side) / 建议实现步骤（主机侧）
1) Resolve `RUN_DIR` using the Path resolution rules above.
2) Load `_run_meta.json` and extract:
   - `run_id`
   - `scenario_tag`
   - `run_window_start_ms`, `run_window_end_ms`
3) Read required text files; check completeness.
4) Validate A: DNS_PROOF markers + banned error patterns.
5) Validate B:
   - marker existence in probe_fault
   - iptables DROP rule present in net_fault, absent in pre/post
6) Validate D: find a resolution attempt and failure indicators in DNS_PROOF section.
7) Validate E:
   - E1: parse pkts/bytes delta for DROP rules in DNS_PROOF before/after
   - E2: look for explicit UDP/TCP 53 hit-test log markers
   - E3: if probe_pre/probe_post available, verify pre OK → fault FAIL → post OK + fault IP ping OK
8) Validate F:
   - parse timestamps from file content (e.g., lines near `### phase=...; date`)
   - ensure they map into run_window (±2s tolerance)
9) Print report in the fixed output format.

---

## Notes / 备注
- Prefer PowerShell JSON parsing (`ConvertFrom-Json`).
- Do not rely on device-side awk/tr; any parsing happens on host.
- Keep the report short and actionable.
