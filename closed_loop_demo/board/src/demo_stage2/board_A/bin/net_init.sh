#!/bin/sh

export PATH=/bin:/system/bin:/usr/bin:/data/local/tmp

IFACE="${IFACE:-eth1}"
TARGET="${QWEN3_SERVER_HOST:-${TARGET:-183.56.183.131}}"
PING_COUNT="${PING_COUNT:-3}"
PING_TIMEOUT_SEC="${PING_TIMEOUT_SEC:-2}"

fail() {
  echo "[net_init] ERROR: $1" >&2
  exit 1
}

log() {
  echo "[net_init] $1"
}

show_ifconfig() {
  log "current_ifconfig:"
  if [ -x /system/bin/ifconfig ]; then
    /system/bin/ifconfig "$IFACE" 2>/dev/null || /system/bin/ifconfig 2>/dev/null || true
    return 0
  fi
  if [ -x /bin/ifconfig ]; then
    /bin/ifconfig "$IFACE" 2>/dev/null || /bin/ifconfig 2>/dev/null || true
    return 0
  fi
  if command -v ifconfig >/dev/null 2>&1; then
    ifconfig "$IFACE" 2>/dev/null || ifconfig 2>/dev/null || true
    return 0
  fi
  fail "ifconfig not available"
}

show_routes() {
  log "current_routes:"
  if [ -r /proc/net/route ]; then
    cat /proc/net/route
    return 0
  fi
  fail "/proc/net/route not readable"
}

if [ ! -d "/sys/class/net/$IFACE" ]; then
  log "interface=$IFACE missing_in_sysfs"
else
  log "interface=$IFACE"
fi

log "target=$TARGET"
show_ifconfig
show_routes

log "ping_target=$TARGET count=$PING_COUNT timeout_sec=$PING_TIMEOUT_SEC"
if ping -c "$PING_COUNT" -W "$PING_TIMEOUT_SEC" "$TARGET"; then
  log "ping_ok"
  exit 0
fi

log "ping_failed"
show_ifconfig
show_routes

fail "unable to reach $TARGET; inspect interface and routes above"
