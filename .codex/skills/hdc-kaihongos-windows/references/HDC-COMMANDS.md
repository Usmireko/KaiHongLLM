Project note:
For this OS fault diagnosis project, prefer Windows PowerShell examples.
Board-side commands must avoid awk/tr.
For network/data collection tasks, prefer short hdc shell invocations with explicit marker lines.
Do not use destructive commands unless explicitly requested.

# HDC Command Reference

Complete command reference for HDC (HarmonyOS Device Connector).

## Table of Contents

1. [Global Options](#global-options)
2. [Session Commands](#session-commands)
3. [Service Commands](#service-commands)
4. [File Commands](#file-commands)
5. [Forward Commands](#forward-commands)
6. [Application Commands](#application-commands)
7. [Debug Commands](#debug-commands)
8. [Security Commands](#security-commands)

## Global Options

| Option | Description |
|--------|-------------|
| `-h/help [verbose]` | Print help, 'verbose' for extended commands |
| `-v/version` | Print HDC version |
| `-t <connectkey>` | Specify target device by connect key |

## Session Commands

Commands for server management.

### list targets

```powershell
hdc list targets [-v]
```

List connected devices. `-v` shows detailed information including:
- Device serial number
- Connection type (USB/TCP)
- Device state

### start

```powershell
hdc start [-r]
```

Start HDC server. `-r` restarts if already running.

### kill

```powershell
hdc kill [-r]
```

Stop HDC server. `-r` restarts after kill.

## Service Commands

Commands executed on device daemon.

### target mount

```powershell
hdc target mount
```

Remount `/system` and `/vendor` partitions as read-write.

### target boot

```powershell
hdc target boot [-bootloader|-recovery]
hdc target boot [MODE]
```

Reboot device. Options:
- No option: normal reboot
- `-bootloader`: boot to bootloader
- `-recovery`: boot to recovery
- `[MODE]`: custom boot mode

### smode

```powershell
hdc smode [-r]
```

Restart daemon with root permissions. `-r` cancels root.

### tmode

```powershell
hdc tmode usb
hdc tmode port [port]
```

Set device connection mode:
- `usb`: listen on USB
- `port [port]`: listen on TCP port (default: 5555)

## File Commands

### file send

```powershell
hdc file send [option] <local> <remote>
```

Options:
| Option | Description |
|--------|-------------|
| `-a` | Preserve file timestamp |
| `-sync` | Only update if local is newer |
| `-z` | Compress during transfer |
| `-m` | Mode sync |

Examples:
```powershell
# Basic send
hdc file send ./app /data/local/tmp/app

# Compressed send with timestamp
hdc file send -a -z ./build/ /data/local/tmp/build/
```

### file recv

```powershell
hdc file recv [option] <remote> <local>
```

Same options as `file send`.

Examples:
```powershell
# Pull log file
hdc file recv /data/log/app.log ./logs/

# Pull with compression
hdc file recv -z /data/core_dump ./crash/
```

## Forward Commands

### fport (forward)

```powershell
hdc fport <local_node> <remote_node>
```

Forward local traffic to device.

Node format: `schema:content`

| Schema | Content | Example |
|--------|---------|---------|
| `tcp` | port | `tcp:8080` |
| `localfilesystem` | unix socket name | `localfilesystem:/tmp/sock` |
| `localreserved` | unix socket name | `localreserved:mysock` |
| `localabstract` | unix socket name | `localabstract:@mysock` |
| `dev` | device name | `dev:/dev/ttyUSB0` |
| `jdwp` | pid (remote only) | `jdwp:1234` |

Examples:
```powershell
# TCP port forward
hdc fport tcp:8080 tcp:8080

# JDWP debugging
hdc fport tcp:5005 jdwp:$(hdc shell pidof my_app)
```

### rport (reverse)

```powershell
hdc rport <remote_node> <local_node>
```

Reverse forward: device traffic to host.

### fport ls

```powershell
hdc fport ls
```

List all forward/reverse tasks.

### fport rm

```powershell
hdc fport rm <taskstr>
```

Remove forward task by task string (from `fport ls`).

## Application Commands

### install

```powershell
hdc install [-r|-s] <src>
```

Install OpenHarmony package(s).

| Option | Description |
|--------|-------------|
| `-r` | Replace existing application |
| `-s` | Install as shared bundle |

`src` accepts:
- Single `.hap` or `.hsp` file
- Multiple packages
- Directories containing packages

Examples:
```powershell
# Install single package
hdc install ./MyApp.hap

# Replace existing
hdc install -r ./MyApp.hap

# Install from directory
hdc install ./packages/
```

### uninstall

```powershell
hdc uninstall [-k] [-s] <package>
```

| Option | Description |
|--------|-------------|
| `-k` | Keep data and cache directories |
| `-s` | Remove shared bundle |

## Debug Commands

### hilog

```powershell
hdc hilog [-h]
```

Stream device logs. `-h` shows filter options.

Common filters:
```powershell
# Filter by tag
hdc hilog | findstr "MyApp"

# Filter by level (PowerShell)
hdc hilog | Select-String "E/"
```

### shell

```powershell
hdc shell [COMMAND...]
```

No command: interactive shell.
With command: execute and return.

### bugreport

```powershell
hdc bugreport [FILE]
```

Collect full device diagnostic info. Optionally save to FILE.

### jpid

```powershell
hdc jpid
```

List PIDs of processes with JDWP transport (debuggable).

## Security Commands

### keygen

```powershell
hdc keygen <FILE>
```

Generate RSA key pair for device authentication.
- Private key: `FILE`
- Public key: `FILE.pub`