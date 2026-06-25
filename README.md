# KaiHongOS / OpenHarmony OS Fault Diagnosis Workspace

This workspace is organized around two runtime lines:

- `data_train_test/`: board-side fault injection, host collection, dataset
  validation, preprocessing, L1/L2 derivation, training data build, testing, and
  export.
- `closed_loop_demo/`: board resident demo scripts, server ingest/inference/action
  services, and host orchestration for the closed-loop demo.

Shared protocol notes live in `shared/`. Runtime output, datasets, logs, model
weights, and old snapshots are ignored runtime material and should not live next
to source code in the cleaned workspace.

## Main Entrypoints

```sh
scripts/run_collect.sh
scripts/run_data_check.sh
scripts/run_preprocess.sh
scripts/run_train.sh
scripts/run_test.sh
scripts/run_export.sh

scripts/demo_preflight.sh
scripts/start_demo.sh
scripts/start_demo.sh --dry-run
scripts/demo_status.sh
scripts/stop_demo.sh
```

Use the stable `scripts/*.sh` entrypoints above or the canonical subproject
paths directly. Legacy root compatibility wrappers were removed during cleanup.

## Directory Guide

```text
data_train_test/
  board/scripts/       board injectors used by training collection
  host/scripts/        Windows/HDC collection and validation entrypoints
  server/src/          dataset conversion, validation, and L2 derivation
  server/scripts/      analysis and experiment tooling

closed_loop_demo/
  board/scripts/       deployable Stage2 board bin bundle
  board/src/           resident board collectors/actions source
  host/scripts/        Windows orchestration helpers
  server/src/          TCP ingest/actions/watcher and inference orchestration

shared/
  protocol/            stable shared protocol descriptions

configs/
  examples/            non-secret example env files
```

See `docs/MIGRATION_MAP.md`, `docs/ARCHITECTURE.md`,
`docs/DEPLOYMENT_MATRIX.md`, `docs/DATA_TRAIN_TEST_RUNBOOK.md`, and
`docs/CLOSED_LOOP_DEMO_RUNBOOK.md` for the detailed map.
