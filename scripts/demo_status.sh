#!/bin/sh
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
if [ -n "${QWEN3_OS_FAULT_ROOT:-}" ]; then
  exec bash "$ROOT/closed_loop_demo/server/src/server_B/tcp/demo_services.sh" status
fi
echo "Set QWEN3_OS_FAULT_ROOT on the server, then run:"
echo "  bash closed_loop_demo/server/src/server_B/tcp/demo_services.sh status"
