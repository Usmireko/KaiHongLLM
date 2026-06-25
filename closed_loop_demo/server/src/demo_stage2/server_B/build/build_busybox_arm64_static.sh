#!/bin/sh
# Run on server (qwen3-server) from repo root: /home/xrh/qwen3_os_fault

set -e

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
OUT_BIN="$ROOT_DIR/demo_stage2/board_A/third_party/busybox_arm64_static"
WORK_DIR="$ROOT_DIR/demo_stage2/server_B/build/_busybox_build"
BUSYBOX_VER="${BUSYBOX_VER:-1.36.1}"
TARBALL="busybox-${BUSYBOX_VER}.tar.bz2"
URL="https://busybox.net/downloads/${TARBALL}"

CROSS_COMPILE="${CROSS_COMPILE:-aarch64-linux-gnu-}"
ARCH="${ARCH:-arm64}"

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "ERROR: missing command: $1" >&2
    exit 1
  }
}

need_cmd curl
need_cmd make
need_cmd "${CROSS_COMPILE}gcc"

mkdir -p "$WORK_DIR"
cd "$WORK_DIR"

if [ ! -f "$TARBALL" ]; then
  echo "[busybox] downloading $URL"
  curl -L -o "$TARBALL" "$URL"
fi

SRC_DIR="$WORK_DIR/busybox-${BUSYBOX_VER}"
if [ ! -d "$SRC_DIR" ]; then
  tar -xjf "$TARBALL"
fi

cd "$SRC_DIR"

make distclean
make defconfig

# Force static build and enable ip/route/ping applets.
# Using sed keeps dependencies minimal.
sed -i 's/^# CONFIG_STATIC is not set/CONFIG_STATIC=y/' .config || true
sed -i 's/^# CONFIG_IP is not set/CONFIG_IP=y/' .config || true
sed -i 's/^# CONFIG_ROUTE is not set/CONFIG_ROUTE=y/' .config || true
sed -i 's/^# CONFIG_PING is not set/CONFIG_PING=y/' .config || true
sed -i 's/^# CONFIG_PING6 is not set/CONFIG_PING6=y/' .config || true

make -j"$(nproc)" ARCH="$ARCH" CROSS_COMPILE="$CROSS_COMPILE"

mkdir -p "$(dirname "$OUT_BIN")"
cp -f busybox "$OUT_BIN"

file "$OUT_BIN" || true
ls -la "$OUT_BIN"

echo "OK: $OUT_BIN"
