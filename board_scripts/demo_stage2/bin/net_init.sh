#!/bin/sh

export PATH=/bin:/system/bin:/usr/bin:/data/local/tmp

BB="/data/local/tmp/busybox"
IFACE="${IFACE:-eth1}"
GW="${GW:-192.168.12.1}"
TARGET="${TARGET:-10.70.1.17}"
PING_COUNT="${PING_COUNT:-3}"
PING_TIMEOUT_SEC="${PING_TIMEOUT_SEC:-2}"

fail() {
  echo "[net_init] ERROR: $1" >&2
  exit 1
}

log() {
  echo "[net_init] $1"
}

if [ ! -x "$BB" ]; then
  fail "busybox missing or not executable at $BB"
fi

if ! "$BB" ip link show >/dev/null 2>&1; then
  fail "busybox ip applet not available; rebuild busybox with ip"
fi

if [ ! -d "/sys/class/net/$IFACE" ]; then
  log "interface $IFACE not found in /sys/class/net"
fi

log "interface=$IFACE gw=$GW target=$TARGET"
log "current_routes:"
"$BB" ip route show || true

log "adding default route via $GW dev $IFACE"
"$BB" ip route add default via "$GW" dev "$IFACE" 2>/dev/null || \
  "$BB" ip route replace default via "$GW" dev "$IFACE" 2>/dev/null || true

log "routes_after:"
"$BB" ip route show || true

log "ping $TARGET"
if "$BB" ping -c "$PING_COUNT" -W "$PING_TIMEOUT_SEC" "$TARGET" >/dev/null 2>&1; then
  log "ping_ok"
  exit 0
fi

log "ping_failed; diagnostics:"
if [ -x /system/bin/ifconfig ]; then
  /system/bin/ifconfig "$IFACE" 2>/dev/null || true
elif [ -x /bin/ifconfig ]; then
  /bin/ifconfig "$IFACE" 2>/dev/null || true
else
  "$BB" ifconfig "$IFACE" 2>/dev/null || true
fi
"$BB" ip addr show dev "$IFACE" 2>/dev/null || true
"$BB" ip route show 2>/dev/null || true

fail "unable to reach $TARGET; check cable/vlan/gateway"
