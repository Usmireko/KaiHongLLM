#!/bin/sh
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PS=$("$ROOT/scripts/bootstrap/resolve_powershell.sh") || exit $?
exec "$PS" -ExecutionPolicy Bypass -File "$ROOT/data_train_test/host/scripts/export_run_case_dataset_fixed_v2.ps1" "$@"
