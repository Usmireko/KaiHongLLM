#!/bin/sh
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec python "$ROOT/data_train_test/server/src/public_dataset_to_canonical_case.py" "$@"
