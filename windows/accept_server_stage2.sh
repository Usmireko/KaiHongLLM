#!/usr/bin/env sh
set -u

ROOT="/home/xrh/qwen3_os_fault"
DEVICE_ID="${DEVICE_ID:-dev1}"
RUN_ID="${1:-}"

fail=0
fails=""

say() { echo "$*"; }
add_fail() {
  key="$1"
  if [ -z "$fails" ]; then
    fails="$key"
  else
    fails="$fails,$key"
  fi
  fail=1
}

wait_for_file() {
  path="$1"
  timeout="$2"
  waited=0
  while [ "$waited" -lt "$timeout" ]; do
    if [ -f "$path" ]; then
      return 0
    fi
    sleep 2
    waited=$((waited + 2))
  done
  return 1
}

is_digits() {
  case "$1" in
    ''|*[!0-9]*) return 1 ;;
    *) return 0 ;;
  esac
}

if [ -z "$RUN_ID" ]; then
  RUN_ID="$(head -n 1 "$ROOT/storage/tcp_out/$DEVICE_ID/latest_run_id.txt" 2>/dev/null || true)"
fi

if [ -z "$RUN_ID" ]; then
  say "RESULT=FAIL"
  say "FAILED=missing_run_id"
  say "NEXT_FIX=check tcp_out/$DEVICE_ID/latest_run_id.txt"
  exit 1
fi

RUN_DIR="$ROOT/storage/runs/$RUN_ID"
INBOX_DIR="$ROOT/storage/tcp_inbox/$DEVICE_ID"

say "RUN_ID=$RUN_ID"

infer_done="$RUN_DIR/_server_out/.infer_done"
infer_ec="$RUN_DIR/_server_out/infer_ec.txt"
action_dir="$RUN_DIR/_action_result"
action_done="$RUN_DIR/_action_result/.unpack_done"
action_json="$RUN_DIR/_action_result/action_result.json"
action_rc="$RUN_DIR/_action_result/actiond_rc.txt"

wait_for_file "$infer_done" 120 || true
wait_for_file "$infer_ec" 120 || true
wait_for_file "$action_done" 120 || true
wait_for_file "$action_json" 120 || true
wait_for_file "$action_rc" 120 || true

if [ ! -f "$infer_done" ]; then
  add_fail C2_infer_done_missing
else
  infer_ok="$(head -n 1 "$infer_done" 2>/dev/null || true)"
  if [ "$infer_ok" != "ok" ]; then
    add_fail C2_infer_done_not_ok
  fi
fi

if [ ! -f "$infer_ec" ]; then
  add_fail C2_infer_ec_missing
else
  infer_ec_val="$(head -n 1 "$infer_ec" 2>/dev/null || true)"
  if [ "$infer_ec_val" != "0" ]; then
    add_fail C2_infer_ec_not_zero
  fi
fi

if [ ! -f "$action_done" ]; then
  add_fail C2_action_unpack_missing
fi

if [ ! -f "$action_json" ]; then
  add_fail C2_action_json_missing
fi

if [ ! -f "$action_rc" ]; then
  add_fail C2_action_rc_missing
else
  action_rc_val="$(head -n 1 "$action_rc" 2>/dev/null || true)"
  if [ "$action_rc_val" != "0" ]; then
    add_fail C2_action_rc_not_zero
  fi
fi

bundle_path="$INBOX_DIR/${RUN_ID}__bundle.tar.gz"
bundle_done="$INBOX_DIR/${RUN_ID}__bundle.tar.gz.done"

cleanup_wait=0
while [ "$cleanup_wait" -lt 60 ]; do
  if [ ! -f "$bundle_path" ] && [ ! -f "$bundle_done" ]; then
    break
  fi
  sleep 2
  cleanup_wait=$((cleanup_wait + 2))
done

if [ -f "$bundle_path" ] || [ -f "$bundle_done" ]; then
  add_fail C3_inbox_not_clean
fi

if [ "$fail" -eq 0 ]; then
  say "RESULT=PASS"
  say "FAILED="
  say "NEXT_FIX=none"
else
  say "RESULT=FAIL"
  say "FAILED=$fails"
  say "NEXT_FIX=inspect $RUN_DIR and $INBOX_DIR"
fi
