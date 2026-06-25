#!/bin/sh
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
case "${1:-}" in
  --dry-run)
    shift
    "$ROOT/scripts/demo_preflight.sh" "$@"
    ;;
  *)
    PS=$("$ROOT/scripts/bootstrap/resolve_powershell.sh") || exit $?
    exec "$PS" -ExecutionPolicy Bypass -File "$ROOT/closed_loop_demo/host/scripts/tools/demo_stage2.ps1" "$@"
    ;;
esac
