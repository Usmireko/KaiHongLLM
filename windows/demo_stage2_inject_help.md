# Stage2 Demo Injection Help

This demo uses two PowerShell windows.

## Window A: Start the demo script
```powershell
powershell -ExecutionPolicy Bypass -File .\tools\demo_stage2.ps1
```

When you see:
```
READY: run injection in another window.
```

## Window B: Manually inject CPU load
Start injection:
```powershell
hdc shell "sh /data/faultmon/demo_stage2/bin/inject_cpu.sh start"
```

Stop injection:
```powershell
hdc shell "sh /data/faultmon/demo_stage2/bin/inject_cpu.sh stop"
```

## If demo times out
Collect logs:
```powershell
hdc shell "/data/local/tmp/busybox tail -n 120 /data/faultmon/demo_stage2/logs/triggerd.log"
ssh qwen3-server "cd /home/xrh/qwen3_os_fault && tail -n 120 storage/logs/watcher.log"
```
