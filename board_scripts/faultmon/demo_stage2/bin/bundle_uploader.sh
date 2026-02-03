#!/system/bin/sh
set -e

BASE_DIR=/data/faultmon/demo_stage2
BIN_DIR="$BASE_DIR/bin"
UPLOADER="$BIN_DIR/uploader_nc.sh"
DEVICE_ID_FILE=/data/faultmon/device_id

TAG=""
ONCE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --once) ONCE=1 ;;
    --tag) shift; TAG="$1" ;;
  esac
  shift
 done

if [ ! -f "$DEVICE_ID_FILE" ]; then
  echo dev1 > "$DEVICE_ID_FILE"
fi
DEVICE_ID="$(cat "$DEVICE_ID_FILE" 2>/dev/null | head -n 1)"
[ -n "$DEVICE_ID" ] || DEVICE_ID="dev1"

FOUND="$(find /data/faultmon -type f -name '*bundle*.tar.gz' 2>/dev/null | tail -n 1)"
run_id=""

if [ -n "$FOUND" ]; then
  name="$(basename "$FOUND")"
  case "$name" in
    bundle_*.tar.gz)
      run_id="${name#bundle_}"
      run_id="${run_id%.tar.gz}"
      ;;
    *__bundle.tar.gz)
      run_id="${name%__bundle.tar.gz}"
      ;;
  esac
fi

if [ -z "$run_id" ]; then
  OUT="$($BIN_DIR/bundle_manual.sh manual "$TAG" --pre 1 --post 1 2>/dev/null || true)"
  run_id="$(printf '%s\n' "$OUT" | grep -F 'run_id=' | head -n 1 | cut -d= -f2)"
  bundle_path="$(printf '%s\n' "$OUT" | grep -F 'bundle_path=' | head -n 1 | cut -d= -f2)"
  if [ -z "$run_id" ]; then
    run_id="manual_$(date +%Y%m%d_%H%M%S 2>/dev/null || date +%s)"
  fi
  if [ -z "$bundle_path" ]; then
    bundle_path="$BASE_DIR/${run_id}__bundle.tar.gz"
  fi
else
  bundle_path="$BASE_DIR/${run_id}__bundle.tar.gz"
  if [ "$FOUND" != "$bundle_path" ]; then
    cp "$FOUND" "$bundle_path"
  fi
fi

size="$(wc -c < "$bundle_path" 2>/dev/null || echo 0)"
echo "[bundle_uploader] run_id=$run_id file=$bundle_path size=$size"

"$UPLOADER" --file "$bundle_path" --type bundle --device "$DEVICE_ID" --run "$run_id"