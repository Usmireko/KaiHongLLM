#!/bin/sh
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec python "$ROOT/data_train_test/server/src/build_llm_sft_dataset.py" "$@"
