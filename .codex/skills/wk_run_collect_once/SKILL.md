---
name: wk_run_collect_once
description: Run one collection using run_wukong_collect_refactor.ps1 with specified env vars (supports 4 net faults + bg_net baseline), capture console log, and report the created run folder.
metadata:
  short-description: Run one collection and report run folder
---

# wk_run_collect_once

## Purpose / 目的
在 Windows 主机侧执行一次采集编排脚本 `run_wukong_collect_refactor.ps1`，通过环境变量选择并驱动 **net 四类故障**或 **网络正常背景 bg_net**，
将控制台输出保存到 `./logs/`，并回报新创建的 run 目录（位于 `./inbox/runs/` 或 `./inbox/run/`）。

---

## Inputs / 输入

### Required / 必填
- `SCENARIO_TAG`：例如
  - `net_dns_fail_r01`
  - `net_link_down_r01`
  - `net_link_flap_r01`
  - `net_wlan_disconnect_r01`
  - `bg_net_pure_r01`（网络正常背景）

### Optional / 可选
- `SCRIPT_PATH`：默认 `./run_wukong_collect_refactor.ps1`

### Net mode selector / Net 场景选择
- `NET_MODE`：五选一：
  - `dns_fail`
  - `link_down`
  - `link_flap`
  - `wlan_disconnect`
  - `bg_net`（网络正常，不注入，仅采集 net 快照/探针）

---

## Per-mode env vars / 各场景参数（缺失时使用脚本默认）
> 默认值（来自脚本）：`NET_IFACE=eth1`、`NET_WLAN_IFACE=wlan0`、`NET_PING_IP=1.1.1.1`、`NET_BAD_DNS=127.0.0.2`

### 1) NET_MODE=dns_fail
- 推荐：
  - `NET_IFACE`（例如 `eth1`）
  - `NET_BAD_DNS`（例如 `127.0.0.2`）
- 可选：
  - `NET_PING_IP`

### 2) NET_MODE=link_down
- 推荐：`NET_IFACE`
- 可选：`NET_PING_IP`

### 3) NET_MODE=link_flap
- 推荐：`NET_IFACE`
- 可选：`NET_PING_IP`
- 备注：脚本里 flap 周期可能是固定值（如需可配置需改脚本）

### 4) NET_MODE=wlan_disconnect
- 推荐：`NET_WLAN_IFACE`（例如 `wlan0`）
- 可选：`NET_PING_IP`

### 5) NET_MODE=bg_net（网络正常背景）
- 推荐（只用于快照展示更准确）：
  - `NET_IFACE`（有线背景）或 `NET_WLAN_IFACE`（无线背景）二选一
- 可选：
  - `NET_PING_IP`
- 重要：bg_net **不应执行 net_fault.sh 注入**，只做 pre/fault/post 的 net 快照与 probe（让数据结构与故障 run 对齐）。

---

## Output format / 输出格式（固定）
打印：
- `RUN_START: <timestamp>`
- `SCRIPT: <SCRIPT_PATH>`
- `RUN_DIR: <newest run dir>`
- `CONSOLE_LOG: <log path>`
- `STATUS: OK|FAIL`

---

## Steps / 步骤

### 1) Pre-check
- Verify `SCRIPT_PATH` exists.
- Ensure `./logs/` exists (create if needed).

### 2) Capture "before" newest run
- Identify newest directory under (in order):
  - `./inbox/runs/`
  - `./inbox/run/`
- Record as `BEFORE_RUN_NAME` (may be empty).

### 3) Set env vars (PowerShell)

#### 3.1 Clear stale env vars (avoid carry-over)
Clear if exist:
- `SCENARIO_TAG`
- `NET_MODE`
- `NET_IFACE`
- `NET_WLAN_IFACE`
- `NET_BAD_DNS`
- `NET_PING_IP`
- `WK_FAULT_TYPE`  (防止上一次残留直接覆盖本次 NET_MODE)

#### 3.2 Set required
- Always set: `SCENARIO_TAG`
- If provided: set `NET_MODE`

#### 3.3 Auto-sanitize iface vars (避免混用)
- If `NET_MODE` is `wlan_disconnect`:
  - Clear `NET_IFACE` (optional but recommended)
- Else (dns_fail/link_down/link_flap/bg_net):
  - Clear `NET_WLAN_IFACE` (optional but recommended)

#### 3.4 Per-mode set
- dns_fail: set `NET_IFACE`, `NET_BAD_DNS`, optional `NET_PING_IP`
- link_down/link_flap: set `NET_IFACE`, optional `NET_PING_IP`
- wlan_disconnect: set `NET_WLAN_IFACE`, optional `NET_PING_IP`
- bg_net:
  - (推荐) 只设置 `NET_IFACE` 或 `NET_WLAN_IFACE` 其一 + 可选 `NET_PING_IP`
  - 并强制：`WK_FAULT_TYPE=bg_net`（避免脚本按 net_* 注入）

> 注：bg_net 是否真正采集到 `net/` 目录，取决于脚本是否把 bg_net 也纳入 net 快照采集条件；若尚未支持，请应用下方“ps1 最小补丁”。

### 4) Run script and capture console
- Execute the script in the same PowerShell session and tee output to:
  - `./logs/collect_<yyyyMMdd_HHmmss>.log`

### 5) Determine "after" newest run
- Re-scan roots and find newest folder again.
- If newest == BEFORE and no newer folder appeared:
  - `STATUS: FAIL`
  - likely causes: script error, output path changed, run creation failed.

### 6) Print report

---

## Examples / 示例

### bg_net（有线网络正常背景）
- `SCENARIO_TAG=bg_net_pure_r01`
- `NET_MODE=bg_net NET_IFACE=eth1 NET_PING_IP=1.1.1.1`

### bg_net（无线网络正常背景）
- `SCENARIO_TAG=bg_net_pure_r01`
- `NET_MODE=bg_net NET_WLAN_IFACE=wlan0 NET_PING_IP=1.1.1.1`

### DNS fail
- `SCENARIO_TAG=net_dns_fail_r01`
- `NET_MODE=dns_fail NET_IFACE=eth1 NET_BAD_DNS=127.0.0.2 NET_PING_IP=1.1.1.1`

### Link down / flap / WLAN disconnect
- `SCENARIO_TAG=net_link_down_r01` + `NET_MODE=link_down NET_IFACE=eth1`
- `SCENARIO_TAG=net_link_flap_r01` + `NET_MODE=link_flap NET_IFACE=eth1`
- `SCENARIO_TAG=net_wlan_disconnect_r01` + `NET_MODE=wlan_disconnect NET_WLAN_IFACE=wlan0`

---

## Notes / 备注
- Do not assume any device-side tools (awk/tr) exist; parsing happens on host.
- Keep console log path stable (always under `./logs/`).
- If multiple devices exist, rely on existing `hdc` config inside the script unless user provides a target.
