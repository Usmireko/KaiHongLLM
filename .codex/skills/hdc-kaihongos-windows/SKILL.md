---
name: hdc-kaihongos-windows
Project-specific constraints:
- This project primarily uses Windows PowerShell to operate hdc.
- Board-side commands must not depend on awk or tr.
- Prefer short hdc shell invocations with explicit marker lines.
- Do not use this skill to perform destructive deployment or cleanup unless explicitly requested.
- For NET collection/review, also read faults/net/AGENTS.md and the board-net-* skills.
description: "MANDATORY for any OpenHarmony/KaihongOS device operations. ALWAYS read this skill FIRST before executing ANY hdc command or device interaction. Covers: RK3588S/RK3568 boards, hdc tool usage, device shell, file transfer, hap install, hilog, dsoftbus debugging. TRIGGER CONDITIONS (must consult this skill): (1) Any mention of hdc/hdc, (2) OpenHarmony/KaihongOS/OHOS device, (3) RK3588/RK3568 board, (4) Device file transfer or deployment, (5) hilog or device logs, (6) dsoftbus/softbus debugging, (7) HAP package operations, (8) Device connection issues. Contains install script, deployment workflows, and troubleshooting guides. DO NOT attempt device operations without reading this skill first."
---

# HDC KaihongOS Controller

Control RK3588S/RK3588/RK3568 KaihongOS devices via `hdc`.

## Prerequisites

### Install HDC

If `hdc` is not installed, run:

```bash
./scripts/install_hdc.sh
```

Options:
- `-v VERSION`: SDK version (4.0/4.1/5.0.0/5.0.3/5.1.0/6.0, default: 5.0.0)
- `-d DIR`: Install directory (default: /opt/ohos-sdk)
- `-f`: Force reinstall

The script auto-detects platform (Linux/macOS/WSL) and downloads from GitHub mirror.

## Environment

- Host: Windows PowerShell primarily; WSL/Linux references are secondary.
- Tool: `hdc` (already in PATH)
- Device: KaiHongOS/OpenHarmony board, including RK3568/RK3588-class boards.

## Quick Reference

### Connection Management

```bash
# List connected devices
hdc list targets -v

# TCP connection (manual)
hdc tconn <ip>:10178

# Discover devices on LAN
hdc discover

# Specify target device for command
hdc -t <connectkey> <command>
```

### Shell Commands

```bash
# Interactive shell
hdc shell

# Execute single command
hdc shell <command>

# Example: check system info
hdc shell cat /proc/version
```

### File Transfer

```bash
# Send file to device
hdc file send [-a|-s|-z] <local_path> <remote_path>

# Receive file from device
hdc file recv [-a|-s|-z] <remote_path> <local_path>

# Options:
#   -a: preserve timestamp
#   -s: sync (update newer only)
#   -z: compress transfer
```

### App Management

```bash
# Install hap package
hdc install [-r] [-d] [-g] <path_to_hap>
#   -r: replace existing
#   -d: allow downgrade
#   -g: grant all permissions

# Uninstall package
hdc uninstall [-k] <package_name>
#   -k: keep data and cache
```

### Debug

```bash
# View device log
hdc hilog [-v]

# Bug report
hdc bugreport [save_path]
```

### System Operations

```bash
# Mount /system /vendor as read-write
hdc target mount

# Reboot device
hdc target boot

# Boot to bootloader/recovery
hdc target boot -bootloader
hdc target boot -recovery

# Enable root daemon
hdc smode

# Disable root daemon
hdc smode -r
```

### Port Forwarding

```bash
# Forward local to remote
hdc fport tcp:<local_port> tcp:<remote_port>

# Reverse forward
hdc rport tcp:<remote_port> tcp:<local_port>

# List forward tasks
hdc fport ls

# Remove forward task
hdc fport rm <taskstr>
```

## Common Workflows

### Deploy and Test ROS2 Node

1. Send compiled binary:
   ```bash
   hdc file send -z ./build/my_node /data/ros2/my_node
   ```

2. Set permissions:
   ```bash
   hdc shell chmod +x /data/ros2/my_node
   ```

3. Execute:
   ```bash
   hdc shell /data/ros2/my_node
   ```

### Debug dsoftbus

1. Check dsoftbus service:
   ```bash
   hdc shell ps -ef | grep softbus
   ```

2. View dsoftbus logs:
   ```bash
   hdc hilog | grep -i softbus
   ```

### Batch File Sync

Use script: `scripts/sync_files.sh`

## Troubleshooting

| Issue | Solution |
|-------|----------|
| No device found | Check USB connection, run `hdc kill -r` to restart server |
| Permission denied | Run `hdc smode` to enable root |
| File transfer slow | Use `-z` option for compression |
| Connection timeout (TCP) | Verify IP, ensure port 10178 is open |

## Scripts

- `scripts/install_hdc.sh`: Auto-install HDC from OpenHarmony SDK
- `scripts/sync_files.sh`: Batch sync files with device
- `scripts/deploy_ros2.sh`: Deploy ROS2 workspace to device
- `scripts/collect_logs.sh`: Collect system and hilog

See references/commands.md for complete command reference.