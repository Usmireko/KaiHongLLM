# Branch Profile: board

## Branch

board

## Purpose

RK3588 / KaiHongOS / OpenHarmony board-side code for fault injection helpers, board demo scripts, bundle upload helpers, and board-facing shared documentation.

## Kept Directories

- `data_train_test/board/`
- `closed_loop_demo/board/`
- `configs/`
- `shared/`
- `faults/`
- board-relevant `docs/`

## Removed Non-Board Content

- `data_train_test/server/`
- `data_train_test/host/`
- `closed_loop_demo/server/`
- `closed_loop_demo/host/`
- generated datasets, logs, checkpoints, model weights, caches, and cleanup backup material
- generated harness reports and task run history

## Common Commands

```sh
bash -n data_train_test/board/scripts/net_fault.sh
bash -n closed_loop_demo/board/scripts/board_services.sh
```

Board deployment and live HDC actions are intentionally not run by validation in this publishing task.

## Validation Performed

- shell syntax check for retained `.sh` files
- Python parse check when Python files exist
- board entrypoint existence check
- stale/forbidden path scan
- secret scan
- large file scan

