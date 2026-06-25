#!/bin/sh
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
missing=0
for p in \
  "$ROOT/closed_loop_demo/host/scripts/tools/demo_stage2.ps1" \
  "$ROOT/closed_loop_demo/server/src/server_B/tcp/demo_services.sh" \
  "$ROOT/closed_loop_demo/server/src/server_B/tcp/tcp_ingest_server.py" \
  "$ROOT/closed_loop_demo/server/src/server_B/tcp/tcp_actions_server.py" \
  "$ROOT/closed_loop_demo/server/src/server_B/tcp/watch_and_infer.py" \
  "$ROOT/closed_loop_demo/board/scripts/bundle_real_upload.sh" \
  "$ROOT/closed_loop_demo/board/scripts/actions_poller_nc.sh" \
  "$ROOT/closed_loop_demo/board/scripts/actiond.sh"
do
  if [ -f "$p" ]; then
    echo "OK $p"
  else
    echo "MISSING $p" >&2
    missing=1
  fi
done
exit "$missing"
