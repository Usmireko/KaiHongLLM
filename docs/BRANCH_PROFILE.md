# Branch Profile: host

## Branch

host

## Purpose

Windows host orchestration, collection control, demo control, status checking, and bridge scripts used to coordinate board and server workflows.

## Kept Directories

- `data_train_test/host/`
- `closed_loop_demo/host/`
- minimal board deployment resources under `data_train_test/board/scripts/`
- minimal board demo resources under `closed_loop_demo/board/scripts/`
- minimal server demo resources under `closed_loop_demo/server/src/server_B/tcp/` required by `scripts/start_demo.sh --dry-run`
- host-related `scripts/`
- `configs/`
- `shared/`
- `faults/`
- host-relevant `docs/`

## Removed Non-Host Content

- full `data_train_test/server/`
- full `closed_loop_demo/server/`, except the minimal TCP demo files required by retained host dry-run checks
- full board implementation trees not needed as host-side deployment resources
- generated datasets, logs, checkpoints, model weights, caches, and cleanup backup material
- generated harness reports and task run history

## Common Commands

```powershell
powershell -NoProfile -Command "$null = [System.Management.Automation.Language.Parser]::ParseFile('data_train_test/host/scripts/run_wukong_collect_refactor.ps1',[ref]$null,[ref]$null)"
bash scripts/start_demo.sh --dry-run
```

Live HDC, SSH deployment, data collection, and closed-loop demo execution are intentionally not run by validation in this publishing task.

## Validation Performed

- PowerShell parse check for retained `.ps1` files
- shell syntax check for retained `.sh` files
- Python parse check for retained `.py` files
- host entrypoint existence check
- safe demo dry-run when retained
- stale/forbidden path scan
- secret scan
- large file scan

