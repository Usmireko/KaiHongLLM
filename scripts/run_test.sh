#!/bin/sh
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec python "$ROOT/data_train_test/server/src/validate_dataset_artifacts.py" "$@"
