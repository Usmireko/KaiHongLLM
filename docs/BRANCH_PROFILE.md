# Branch Profile: server

## Branch

server

## Purpose

Data processing, training, testing, evaluation, model service, closed-loop inference, and server-side demo code.

## Kept Directories

- `data_train_test/server/`
- `closed_loop_demo/server/`
- server-related `scripts/`
- `configs/`
- `shared/`
- `faults/`
- server-relevant `docs/`

## Removed Non-Server Content

- `data_train_test/board/`
- `data_train_test/host/`
- `closed_loop_demo/board/`
- `closed_loop_demo/host/`
- board-only and host-only orchestration scripts
- generated datasets, logs, checkpoints, model weights, caches, and cleanup backup material
- generated harness reports and task run history
- `data_train_test/server/scripts/tooling/evidence_diag20_inference_runner.py`, excluded because secret-pattern scan flagged password-like fixture text

## Common Commands

```sh
bash -n closed_loop_demo/server/src/server_B/tcp/demo_services.sh
python data_train_test/server/src/validate_dataset_artifacts.py --help
python closed_loop_demo/server/src/server_B/orchestrator/run_closed_loop.py --help
```

Live SSH, model loading, training, and server restarts are intentionally not run by validation in this publishing task.

## Validation Performed

- shell syntax check for retained `.sh` files
- Python parse check for retained `.py` files
- safe Python CLI `--help` checks
- server entrypoint existence check
- stale/forbidden path scan
- secret scan
- large file scan

