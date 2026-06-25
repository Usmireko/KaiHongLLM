# Data Train Test Runbook

## Preflight

```sh
scripts/run_data_check.sh -AsJson
```

For real board collection, configure a local ignored env file from
`configs/examples/data_train_test.env.example` and make sure `hdc` can reach the
target board.

## Collect

```sh
scripts/run_collect.sh -SN 192.168.3.28:8711
```

The collector pushes `data_train_test/board/scripts/net_fault.sh` to
`/data/local/tmp/net_fault.sh` when a NET fault is requested.

## Validate a Run

```sh
scripts/run_data_check.sh -RUN_DIR artifacts/datasets/runs/<run_id> -AsJson
```

Use PASS/UNCERTAIN/FAIL. Preserve GT/OBS separation and do not relabel observed
symptoms as ground truth.

## Preprocess and Derive Samples

```sh
python data_train_test/server/src/public_dataset_to_canonical_case.py --help
python data_train_test/server/src/derive_l2_case_samples.py --help
python data_train_test/server/src/batch_derive_l2_samples.py --help
```

## Test and Export

```sh
scripts/run_test.sh --help
scripts/run_export.sh -Help
```

Write datasets, metrics, checkpoints, and model outputs under ignored runtime
paths such as `artifacts/` when running experiments. Do not commit generated
run data.
