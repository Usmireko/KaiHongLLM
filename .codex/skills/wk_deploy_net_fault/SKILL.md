---
name: wk_deploy_net_fault
description: Upload net_fault.sh to device /data/local/tmp/net_fault.sh, chmod +x, and verify it exists and is executable.
metadata:
  short-description: Deploy net_fault.sh to device
---

# wk_deploy_net_fault

## Purpose / 目的
Deploy the device-side injection script to the board:
- Upload to `/data/local/tmp/net_fault.sh`
- `chmod +x`
- Verify with `ls -l`

---

## Inputs / 输入
- `LOCAL_NET_FAULT`: local path to `net_fault.sh` (default: `./net_fault.sh`)
- `REMOTE_PATH`: default `/data/local/tmp/net_fault.sh`
- `HDC_TARGET`: optional device selector (only if you use `hdc -t <target>`)

If `LOCAL_NET_FAULT` does not exist, ask user for the correct path.

---

## Output format / 输出格式（固定）
Print:
- `DEPLOY: PASS|FAIL`
- `REMOTE_PATH: <path>`
- `DETAIL: <one-line reason>`

---

## Steps / 步骤
1) Pre-check:
   - Ensure `LOCAL_NET_FAULT` exists on host.
   - Ensure `hdc list targets -v` shows at least one device Ready.

2) Upload:
   - Run: `hdc file send <LOCAL_NET_FAULT> <REMOTE_PATH>`

3) Permission + verify:
   - Run: `hdc shell "chmod +x <REMOTE_PATH> && ls -l <REMOTE_PATH>"`

4) Light sanity (no heavy output):
   - Run: `hdc shell "head -n 2 <REMOTE_PATH> 2>/dev/null || true"`

5) Print PASS if file exists and is executable; else FAIL with reason.

---

## Notes / 备注
- Do not create or reference net_fault1/2. Always deploy as `net_fault.sh`.
- Avoid using awk/tr on device.
