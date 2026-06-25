#!/bin/sh
# net_fault.sh — hardened network fault injector (toybox/ash)
set -u

LOG_TAG="[net_fault]"
STATE_DIR="/data/local/tmp/net_fault_state"
mkdir -p "$STATE_DIR" 2>/dev/null

# wpa/wlan defaults (overridden by NET_WPA_CTRL / NET_WPA_CONF etc. in params section below)
WPA_CTRL="/data/local/tmp/wpa_ctrl"
WPA_CONF="/data/local/tmp/wpa.conf"
WPA_PID_FILE="/data/local/tmp/wpa.pid"
WPA_BAD_CONF="/data/local/tmp/wpa_bad.conf"
WLAN_SSID="Guest"
WPA_BAD_PSK="WrongPassword999"
WLAN_IP_BAK="$STATE_DIR/wlan_ip.bak"
ROUTE_BAK="$STATE_DIR/route.bak"
WPA_BAD_APPLIED="$STATE_DIR/wpa_bad.applied"

#log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $LOG_TAG $*"; }
NET_DEBUG="${NET_DEBUG:-0}"
log() { [ "$NET_DEBUG" = "1" ] && echo "$(date '+%Y-%m-%d %H:%M:%S') $LOG_TAG $*"; }

safe_reset_wpa_ctrl_dir() {
  case "${WPA_CTRL:-}" in
    /data/local/tmp/wpa_ctrl|/data/local/tmp/wpa_ctrl.*|/data/local/tmp/wpa_ctrl_*)
      rm -rf "$WPA_CTRL" 2>/dev/null || true
      ;;
    *)
      log "safe_reset_wpa_ctrl_dir: skip unsafe WPA_CTRL=${WPA_CTRL:-unset}"
      return 1
      ;;
  esac
  return 0
}

# ====== DNS block (IPv4+IPv6 + auto-probe)
# 目的：让 dns_fail 在不同 DNS 实现（UDP/53、TCP/53、DoT/853、DoH/443、IPv6 DNS）下都尽量生效。
# 约束：避免 -p udp/--dport 53（你板子上会触发 iptables Signal 11），尽量按“目的 IP”丢弃。
IPT_DNS_MARK="$STATE_DIR/iptables_dns_block.applied"
IPT_DNS_LIST4="$STATE_DIR/iptables_dns_block.v4.list"   # 每行一个 IPv4
IPT_DNS_LIST6="$STATE_DIR/iptables_dns_block.v6.list"   # 每行一个 IPv6
IPT_TARGET_MARK="$STATE_DIR/iptables_target_block.applied"  # stores blocked TARGET_IP
# ====== DNS proxy fallback (dnsproxyd) ======
DNS_PROXY_FREEZE="${NET_DNS_PROXY_FREEZE:-1}"               # 1=enable fallback
DNS_PROXY_NAME="${NET_DNS_PROXY_NAME:-dnsproxyd}"           # process name
DNS_SELFTEST_AFTER_APPLY="${NET_DNS_SELFTEST_AFTER_APPLY:-1}"  # 1=selftest then freeze if still resolves
DNS_PROXY_PID_FILE="$STATE_DIR/dnsproxy.pid"
DNS_PROXY_STOPPED_FILE="$STATE_DIR/dnsproxy.stopped"
DNS_ENDPOINT_DROP="${NET_DNS_ENDPOINT_DROP:-0}"                # 0=avoid endpoint DROP-all; rely on resolv/freeze
NET_PING_IP_SAFE="${NET_PING_IP:-${WK_NET_PING_IP:-8.8.8.8}}"

_find_pid_by_name() {
  name="$1"
  # make pattern like [d]nsproxyd to avoid matching grep itself
  first="${name%${name#?}}"
  rest="${name#?}"
  ps -A 2>/dev/null | grep -m 1 "[${first}]${rest}" 2>/dev/null | sed 's/^ *//; s/ .*//'
}

dnsproxy_freeze() {
  [ "$DNS_PROXY_FREEZE" = "1" ] || return 0
  pid="$(_find_pid_by_name "$DNS_PROXY_NAME")"
  if [ -n "${pid:-}" ]; then
    echo "$pid" > "$DNS_PROXY_PID_FILE" 2>/dev/null || true
    kill -STOP "$pid" 2>/dev/null || true
    echo "stopped" > "$DNS_PROXY_STOPPED_FILE" 2>/dev/null || true
    log "dnsproxy_freeze: STOP $DNS_PROXY_NAME pid=$pid"
  else
    log "dnsproxy_freeze: $DNS_PROXY_NAME not found"
  fi
}

dnsproxy_resume() {
  [ -f "$DNS_PROXY_STOPPED_FILE" ] || return 0
  pid=""
  [ -f "$DNS_PROXY_PID_FILE" ] && pid="$(cat "$DNS_PROXY_PID_FILE" 2>/dev/null || true)"
  if [ -n "${pid:-}" ]; then
    kill -CONT "$pid" 2>/dev/null || true
    log "dnsproxy_resume: CONT $DNS_PROXY_NAME pid=$pid"
  fi
  rm -f "$DNS_PROXY_STOPPED_FILE" "$DNS_PROXY_PID_FILE" 2>/dev/null || true
}

_default_gw_v4_from_route() {
  [ -f /proc/net/route ] || return 0
  sed '1d' /proc/net/route 2>/dev/null | while read -r line; do
    [ -z "$line" ] && continue
    set -- $line
    dest="$2"
    gwhex="$3"
    [ "$dest" = "00000000" ] || continue
    echo "$gwhex" | grep -Eq '^[0-9A-Fa-f]{8}$' || continue
    echo "$(_hex2ip4_le "$gwhex")"
    break
  done
}


DNS_AUTO_BLOCK="${NET_DNS_AUTO_BLOCK:-1}"   # 1=探测真实 DNS 端点并加入阻断（默认开）；0=仅按 resolv.conf
DNS_PROBE_HOST="${NET_DNS_PROBE_HOST:-}"    # 留空则用随机 *.invalid 强制触发解析
DNS_PROBE_HOST_FALLBACK="${NET_DNS_CLEANUP_PROBE_HOST:-www.baidu.com}"
# 解析失败“自证”探针（写入 fault_net_*.log 里，方便你判断注入是否真的生效）
DNS_PROOF_ENABLE="${NET_DNS_PROOF_ENABLE:-1}"              # 1=在 dns_fail 模式循环里定期打印解析结果
DNS_PROOF_INTERVAL_SEC="${NET_DNS_PROOF_INTERVAL_SEC:-5}"  # 每隔 N 秒做一次探针
# 探针用的目标 IP（通过 nip.io 把域名解析到这个 IP；用不同 label 规避正缓存）
DNS_PROOF_BASE_IP="${NET_DNS_PROOF_BASE_IP:-$NET_PING_IP_SAFE}"
# [NEW] UDP DNS endpoint discovery helpers
DNS_CT_ENABLE="${NET_DNS_CT_ENABLE:-1}"           # 1=use conntrack to discover UDP DNS dst
DNS_CT_TAIL_LINES="${NET_DNS_CT_TAIL_LINES:-2000}" # tail last N lines from conntrack
DNS_GW_ARP_ENABLE="${NET_DNS_GW_ARP_ENABLE:-0}"   # 1=guess gateway (*.1) from /proc/net/arp
# DoH(443) 自动阻断风险很大（容易误伤正常 https），默认关
DNS_BLOCK_DOH="${NET_DNS_BLOCK_DOH:-0}"


_have_iptables()  { iptables -V  >/dev/null 2>&1; }
_have_ip6tables() { ip6tables -V >/dev/null 2>&1; }

_dns_list_reset() {
  : > "$IPT_DNS_LIST4" 2>/dev/null || true
  : > "$IPT_DNS_LIST6" 2>/dev/null || true
}
_dns_list_add4() {
  ip="$1"
  [ -z "$ip" ] && return 0
  # 避免误伤 loopback（常见 stub resolver），先只记录不阻断
  [ "$ip" = "127.0.0.1" ] && { log "WARN: nameserver=127.0.0.1 detected (loopback stub). Skip DROP by dest IP to avoid collateral."; return 0; }
  grep -Fxq "$ip" "$IPT_DNS_LIST4" 2>/dev/null || echo "$ip" >> "$IPT_DNS_LIST4"
}
_dns_list_add6() {
  ip="$1"
  [ -z "$ip" ] && return 0
  [ "$ip" = "::1" ] && { log "WARN: nameserver=::1 detected (loopback stub). Skip DROP by dest IP to avoid collateral."; return 0; }
  grep -Fxq "$ip" "$IPT_DNS_LIST6" 2>/dev/null || echo "$ip" >> "$IPT_DNS_LIST6"
}

_dns_list_collect_from_resolv() {
  # 从常见 resolv.conf 候选里读 nameserver（只读即可）；不清空 list（允许前后多次 merge）
  for f in /etc/resolv.conf /system/etc/resolv.conf /data/service/el1/public/netmanager/resolv.conf; do
    [ -f "$f" ] || continue
    grep '^nameserver[[:space:]]' "$f" 2>/dev/null | while read -r k v rest; do
      ns="$v"
      [ -z "$ns" ] && continue
      # IPv4
      echo "$ns" | grep -Eq '^[0-9]+(\.[0-9]+){3}$' && { _dns_list_add4 "$ns"; continue; }
      # IPv6（非常宽松：包含冒号即可）
      echo "$ns" | grep -q ':' && { _dns_list_add6 "$ns"; continue; }
    done
  done
}

# --- Auto-probe: 从 /proc/net/{udp,tcp} 观察 53/853/443 的远端 IP（仅 IPv4）---
_hex2ip4_le() {
  h="$1"
  # /proc/net/* 使用 little-endian：0100007F => 127.0.0.1
  echo "$h" | grep -Eq '^[0-9A-Fa-f]{8}$' || { echo ""; return; }
  set -- $(echo "$h" | sed 's/^\(..\)\(..\)\(..\)\(..\)$/\4 \3 \2 \1/')
  b1=$(printf '%d' "0x$1" 2>/dev/null || echo 0)
  b2=$(printf '%d' "0x$2" 2>/dev/null || echo 0)
  b3=$(printf '%d' "0x$3" 2>/dev/null || echo 0)
  b4=$(printf '%d' "0x$4" 2>/dev/null || echo 0)
  echo "$b1.$b2.$b3.$b4"
}

# Convert IPv4 dotted to 8-char little-endian hex (for /proc/net/route comparison)
# e.g. 192.168.3.254 -> FE03A8C0
_ip2hex4_le() {
  _ia="$1"
  _o1="${_ia%%.*}"; _r="${_ia#*.}"
  _o2="${_r%%.*}"; _r="${_r#*.}"
  _o3="${_r%%.*}"; _o4="${_r#*.}"
  printf '%02X%02X%02X%02X' "$_o4" "$_o3" "$_o2" "$_o1"
}

_list_live_dns_endpoints_v4() {
  out="$1"
  : > "$out" 2>/dev/null || true
  for pf in /proc/net/udp /proc/net/tcp; do
    [ -f "$pf" ] || continue
    sed '1d' "$pf" 2>/dev/null | while read -r line; do
      [ -z "$line" ] && continue
      set -- $line
      rem="$3"   # rem_address
      hexip="${rem%:*}"
      hexport="${rem##*:}"
      case "$hexport" in
        0035|0355|01BB) :;;  # 53/853/443
        *) continue;;
      esac
      [ "$hexip" = "00000000" ] && continue
      echo "$hexip" | grep -Eq '^[0-9A-Fa-f]{8}$' || continue
      ip=$(_hex2ip4_le "$hexip")
      [ "$ip" = "127.0.0.1" ] && continue
      grep -Fxq "$ip" "$out" 2>/dev/null || echo "$ip" >> "$out"
    done
  done
}
# ====== DNS unix-socket proxy fallback (dnsproxyd -> netsysnative) ======
DNS_UNIX_FREEZE="${NET_DNS_UNIX_FREEZE:-1}"    # 1=enable STOP/CONT fallback
DNS_UNIX_PATH="${NET_DNS_UNIX_PATH:-/dev/unix/socket/dnsproxyd}"
DNS_UNIX_MARK="$STATE_DIR/dns_unix_frozen.applied"
DNS_UNIX_PID_FILE="$STATE_DIR/dns_unix_proxy.pid"

_dns_unix_get_inode() {
  [ -f /proc/net/unix ] || return 1
  line="$(grep -m 1 "$DNS_UNIX_PATH" /proc/net/unix 2>/dev/null || true)"
  [ -z "$line" ] && line="$(grep -i -m 1 'dnsproxyd' /proc/net/unix 2>/dev/null || true)"
  [ -z "$line" ] && return 1
  set -- $line
  echo "$7"
}

_dns_unix_get_owner_pid() {
  inode="$(_dns_unix_get_inode 2>/dev/null || true)"
  [ -n "$inode" ] || return 1
  for p in /proc/[0-9]*; do
    ls -l "$p/fd" 2>/dev/null | grep -q "socket:\\[$inode\\]" 2>/dev/null || continue
    echo "${p##*/}"
    return 0
  done
  return 1
}

dns_unix_freeze() {
  [ "$DNS_UNIX_FREEZE" = "1" ] || return 0
  [ -f "$DNS_UNIX_MARK" ] && return 0

  pid="$(_dns_unix_get_owner_pid 2>/dev/null || true)"
  if [ -n "${pid:-}" ]; then
    comm="$(cat "/proc/$pid/comm" 2>/dev/null || true)"
    echo "$pid" > "$DNS_UNIX_PID_FILE" 2>/dev/null || true
    kill -STOP "$pid" 2>/dev/null || true
    echo "applied" > "$DNS_UNIX_MARK" 2>/dev/null || true
    log "dns_unix_freeze: STOP pid=$pid comm=$comm (owner of $DNS_UNIX_PATH)"
  else
    log "dns_unix_freeze: cannot find owner pid for $DNS_UNIX_PATH"
  fi
}

dns_unix_resume() {
  [ -f "$DNS_UNIX_MARK" ] || return 0
  pid=""
  [ -f "$DNS_UNIX_PID_FILE" ] && pid="$(cat "$DNS_UNIX_PID_FILE" 2>/dev/null || true)"
  if [ -n "${pid:-}" ]; then
    kill -CONT "$pid" 2>/dev/null || true
    log "dns_unix_resume: CONT pid=$pid"
  fi
  rm -f "$DNS_UNIX_MARK" "$DNS_UNIX_PID_FILE" 2>/dev/null || true
}

_list_dns_endpoints_from_conntrack_v4() {
  out="$1"
  : > "$out" 2>/dev/null || true
  [ "${DNS_CT_ENABLE}" = "1" ] || return 0

  for f in /proc/net/nf_conntrack /proc/net/ip_conntrack; do
    [ -f "$f" ] || continue

    # 只看最近一段，避免文件过大
    tail -n "${DNS_CT_TAIL_LINES}" "$f" 2>/dev/null \
      | grep -E 'dport=53|dport=853' 2>/dev/null \
      | while IFS= read -r line; do
          ip="$(echo "$line" \
            | sed -n 's/.* src=[0-9.]* dst=\([0-9.]*\) sport=[0-9]* dport=\(53\|853\).*/\1/p' 2>/dev/null)"
          [ -z "$ip" ] && continue
          [ "$ip" = "127.0.0.1" ] && continue
          grep -Fxq "$ip" "$out" 2>/dev/null || echo "$ip" >> "$out"
        done

    # DoH(443) 可选（默认关闭）
    if [ "${DNS_BLOCK_DOH}" = "1" ]; then
      tail -n "${DNS_CT_TAIL_LINES}" "$f" 2>/dev/null \
        | grep -E 'dport=443\b' 2>/dev/null \
        | while IFS= read -r line; do
            ip="$(echo "$line" \
              | sed -n 's/.* src=[0-9.]* dst=\([0-9.]*\) sport=[0-9]* dport=443.*/\1/p' 2>/dev/null)"
            [ -z "$ip" ] && continue
            [ "$ip" = "127.0.0.1" ] && continue
            grep -Fxq "$ip" "$out" 2>/dev/null || echo "$ip" >> "$out"
          done
    fi
  done
}

_guess_gateway_from_arp_v4() {
  [ "${DNS_GW_ARP_ENABLE}" = "1" ] || return 0
  [ -f /proc/net/arp ] || return 0

  gw=""
  first=1
  while IFS= read -r ip hwtype flags hwaddr mask dev; do
    if [ "$first" = "1" ]; then first=0; continue; fi
    [ -n "$ip" ] || continue

    case "$flags" in
      0x2|0x6|2|6) :;;
      *) continue;;
    esac

    case "$ip" in
      *.1) gw="$ip"; break;;
    esac
  done < /proc/net/arp

  [ -n "$gw" ] && echo "$gw"
}

_probe_and_extend_dns_list_v4() {
  [ "$DNS_AUTO_BLOCK" = "1" ] || return 0
  _have_iptables || return 0

  before="$STATE_DIR/.dns_ep_before_$$"
  after="$STATE_DIR/.dns_ep_after_$$"
  snap="$STATE_DIR/.dns_ep_snap_$$"
  snapct="$STATE_DIR/.dns_ct_snap_$$"
  new="$STATE_DIR/.dns_ep_new_$$"

  : > "$before" 2>/dev/null || true
  : > "$after" 2>/dev/null || true

  # baseline snapshot
  _list_live_dns_endpoints_v4 "$snap"
  cat "$snap" >> "$before" 2>/dev/null || true
  _list_dns_endpoints_from_conntrack_v4 "$snapct"
  cat "$snapct" >> "$before" 2>/dev/null || true
  rm -f "$snap" "$snapct" 2>/dev/null || true

  # trigger resolution (force query)
  host="$DNS_PROBE_HOST"
  if [ -z "$host" ]; then
    host="t$$.${DNS_PROOF_BASE_IP}.nip.io"
  fi
  log "dns auto-probe: trigger resolution for host=$host (dport 53/853; doh=${DNS_BLOCK_DOH})"
  # Force TCP resolver path if honored, and widen the window by multiple unique lookups.
  for k in 1 2 3 4 5; do
    hk="t$$${k}.${DNS_PROOF_BASE_IP}.nip.io"
    echo "### SELFTEST_DNS_AUTO_PROBE host=$hk"
    (
      if RES_OPTIONS="attempts:1 timeout:1 usevc" ping -c 1 "$hk" >/dev/null 2>&1; then
        echo "SELFTEST_DNS_AUTO_PROBE_RESULT host=$hk result=resolve_ok"
      else
        echo "SELFTEST_DNS_AUTO_PROBE_RESULT host=$hk result=resolve_fail"
      fi
    ) &
    _dns_probe_pids="${_dns_probe_pids:-} $!"
  done


  # capture after (multiple snapshots)
  for i in 1 2 3 4; do
    sleep 1
    _list_live_dns_endpoints_v4 "$snap"
    cat "$snap" >> "$after" 2>/dev/null || true
    _list_dns_endpoints_from_conntrack_v4 "$snapct"
    cat "$snapct" >> "$after" 2>/dev/null || true
    rm -f "$snap" "$snapct" 2>/dev/null || true
  done
  for _dns_probe_pid in ${_dns_probe_pids:-}; do
    kill "$_dns_probe_pid" 2>/dev/null || true
  done

  if [ -s "$after" ]; then
    sort -u "$after" 2>/dev/null | grep -F -x -v -f "$before" 2>/dev/null > "$new" || true
  fi

  if [ -s "$new" ]; then
    log "dns auto-probe: new endpoints discovered:"
    while IFS= read -r ip; do
      [ -z "$ip" ] && continue
      log "  + $ip"
      _dns_list_add4 "$ip"
    done < "$new"
  else
    log "dns auto-probe: no new endpoints discovered (UDP sendto may still be hidden without conntrack)"
  fi

  # ARP gateway fallback (common case: DNS=router)
  gw="$(_guess_gateway_from_arp_v4)"
  if [ -n "$gw" ]; then
    log "dns auto-probe: add ARP gateway candidate: $gw"
    _dns_list_add4 "$gw"
  fi
    gw="$(_default_gw_v4_from_route)"
  if [ -n "${gw:-}" ]; then
    log "dns auto-probe: add default gw candidate: $gw"
    _dns_list_add4 "$gw"
  fi
  rm -f "$before" "$after" "$new" 2>/dev/null || true
}


apply_dns_block() {
  if ! _have_iptables; then
    log "WARN: iptables missing, skip dns block"
    return 0
  fi
  [ -f "$IPT_DNS_MARK" ] && return 0

  if [ "$DNS_ENDPOINT_DROP" != "1" ]; then
    echo "### DNS_BLOCK_MODE endpoint_drop_disabled_dns_only"
    log "dns endpoint DROP-all disabled; rely on resolv disruption and dns proxy freeze"
    : > "$IPT_DNS_MARK" 2>/dev/null || true
    return 0
  fi

  if [ -s "$IPT_DNS_LIST4" ]; then
    while IFS= read -r ns; do
      [ -z "$ns" ] && continue
      if [ "$ns" = "$DNS_PROOF_BASE_IP" ] || [ "$ns" = "$NET_PING_IP_SAFE" ]; then
        echo "### DNS_BLOCK_SKIP protected_ip=$ns"
        log "skip endpoint DROP-all for protected probe IP: $ns"
        continue
      fi
      iptables -I OUTPUT 1 -d "$ns" -j DROP 2>/dev/null || true
    done < "$IPT_DNS_LIST4"
  fi

  if _have_ip6tables && [ -s "$IPT_DNS_LIST6" ]; then
    while IFS= read -r ns; do
      [ -z "$ns" ] && continue
      ip6tables -I OUTPUT 1 -d "$ns" -j DROP 2>/dev/null || true
    done < "$IPT_DNS_LIST6"
  else
    [ -s "$IPT_DNS_LIST6" ] && log "WARN: ip6tables missing, IPv6 endpoints cannot be blocked"
  fi

  : > "$IPT_DNS_MARK" 2>/dev/null || true
  log "iptables DNS block applied"
}

clear_dns_block() {
  if _have_iptables && [ -f "$IPT_DNS_LIST4" ]; then
    while IFS= read -r ns; do
      [ -z "$ns" ] && continue
      while iptables -D OUTPUT -d "$ns" -j DROP 2>/dev/null; do :; done
    done < "$IPT_DNS_LIST4"
  fi

  if _have_ip6tables && [ -f "$IPT_DNS_LIST6" ]; then
    while IFS= read -r ns; do
      [ -z "$ns" ] && continue
      while ip6tables -D OUTPUT -d "$ns" -j DROP 2>/dev/null; do :; done
    done < "$IPT_DNS_LIST6"
  fi

  rm -f "$IPT_DNS_MARK" "$IPT_DNS_LIST4" "$IPT_DNS_LIST6" 2>/dev/null || true
  log "iptables DNS block cleared"
}

clear_target_block() {
  if _have_iptables && [ -f "$IPT_TARGET_MARK" ]; then
    tip="$(cat "$IPT_TARGET_MARK" 2>/dev/null || true)"
    if [ -n "${tip:-}" ]; then
      while iptables -D OUTPUT -d "$tip" -j DROP 2>/dev/null; do :; done
    fi
  fi
  rm -f "$IPT_TARGET_MARK" 2>/dev/null || true
  log "iptables target block cleared"
}


# ---------- resolv targets (write as many as possible) ----------
RESOLV_LIST="$STATE_DIR/resolv.targets"   # each line: path
backup_one_resolv() {
  p="$1"
  base="$(echo "$p" | sed 's#/#_#g')"
  bak="$STATE_DIR/${base}.bak"
  if [ -f "$p" ]; then
    cat "$p" > "$bak" 2>/dev/null || true
  else
    : > "$bak" 2>/dev/null || true
  fi
}

can_write_dir() {
  d="$1"
  t="$d/.net_wtest_$$"
  ( : > "$t" ) 2>/dev/null && rm -f "$t" 2>/dev/null && return 0
  return 1
}

# ====== [MOD] resolv targets：把 /etc 和 /system 也纳入候选 ======
collect_resolv_targets() {
  : > "$RESOLV_LIST" 2>/dev/null || true
  for p in \
    /data/service/el1/public/netmanager/resolv.conf \
    /etc/resolv.conf \
    /system/etc/resolv.conf \
  ; do
    dir="${p%/*}"
    # 只要目录可写，就把这个候选加入（文件不存在也允许创建）
    if [ -d "$dir" ] && can_write_dir "$dir"; then
      echo "$p" >> "$RESOLV_LIST"
      backup_one_resolv "$p"
      continue
    fi
    # 目录不可写但文件存在：也加入（后续写可能失败，但能打印诊断）
    if [ -f "$p" ]; then
      echo "$p" >> "$RESOLV_LIST"
      backup_one_resolv "$p"
    fi
  done
}
write_dns_fail() {
  bad_dns="$1"
  ok=0
  if [ ! -s "$RESOLV_LIST" ]; then
    log "ERROR: no resolv targets"
    return 1
  fi
  while IFS= read -r p; do
    [ -z "$p" ] && continue
    {
      echo "# injected by net_fault.sh mode=dns_fail"
      echo "nameserver $bad_dns"
      echo "options timeout:1 attempts:1"
    } > "$p" 2>/dev/null
    if [ $? -eq 0 ]; then
      log "dns_fail applied -> $p nameserver=$bad_dns"
      ok=1
    else
      log "WARN: write failed -> $p (maybe read-only)"
    fi
  done < "$RESOLV_LIST"
  [ $ok -eq 1 ] && return 0
  return 1
}

# ---------- iface state ----------
get_iface_ip() {
  IF="$1"
  ifconfig "$IF" 2>/dev/null | grep -m 1 'inet addr' | sed -n 's/.*inet addr:\([0-9.]*\).*/\1/p'
}
get_iface_mask() {
  IF="$1"
  ifconfig "$IF" 2>/dev/null | grep -m 1 'Mask:' | sed -n 's/.*Mask:\([0-9.]*\).*/\1/p'
}

# Read default gateway for a wlan interface from /proc/net/route
get_wlan_gw() {
  IF="$1"
  [ -f /proc/net/route ] || return 0
  sed '1d' /proc/net/route 2>/dev/null | while read -r line; do
    [ -z "$line" ] && continue
    set -- $line
    [ "$1" = "$IF" ] || continue
    [ "$2" = "00000000" ] || continue
    echo "$(_hex2ip4_le "$3")"
    break
  done
}

resolv_has_bad_dns() {
  _bad="$1"
  for _p in \
    /data/service/el1/public/netmanager/resolv.conf \
    /etc/resolv.conf \
    /system/etc/resolv.conf \
  ; do
    [ -f "$_p" ] || continue
    grep -F "nameserver $_bad" "$_p" >/dev/null 2>&1 && return 0
  done
  return 1
}

first_backup_nameserver() {
  for _bf in "$STATE_DIR"/*.bak; do
    [ -f "$_bf" ] || continue
    _ns="$(sed -n 's/^[[:space:]]*nameserver[[:space:]][[:space:]]*\([^[:space:]]*\).*$/\1/p' "$_bf" 2>/dev/null | head -n 1)"
    if [ -n "${_ns:-}" ] && [ "$_ns" != "$BAD_DNS" ] && [ "$_ns" != "127.0.0.2" ] && [ "$_ns" != "0.0.0.0" ]; then
      echo "$_ns"
      return 0
    fi
  done
  return 1
}

replace_bad_resolv_only() {
  _fallback_dns="$1"
  _replaced=0
  for _p in \
    /data/service/el1/public/netmanager/resolv.conf \
    /etc/resolv.conf \
    /system/etc/resolv.conf \
  ; do
    [ -f "$_p" ] || continue
    grep -F "nameserver $BAD_DNS" "$_p" >/dev/null 2>&1 || continue
    _tmp="$STATE_DIR/.resolv_fix_$$"
    sed "s/^[[:space:]]*nameserver[[:space:]][[:space:]]*$BAD_DNS[[:space:]]*$/nameserver $_fallback_dns/" "$_p" > "$_tmp" 2>/dev/null || {
      rm -f "$_tmp" 2>/dev/null || true
      continue
    }
    cat "$_tmp" > "$_p" 2>/dev/null || true
    rm -f "$_tmp" 2>/dev/null || true
    _replaced=1
  done
  [ "$_replaced" = "1" ]
}

restore_resolv_fallback_if_needed() {
  _fallback_dns="${NET_RESOLV_FALLBACK_DNS:-}"
  [ -n "${_fallback_dns:-}" ] || _fallback_dns="$(first_backup_nameserver || true)"
  [ -n "${_fallback_dns:-}" ] || _fallback_dns="223.5.5.5"
  if [ -z "${_fallback_dns:-}" ] || [ "$_fallback_dns" = "0.0.0.0" ]; then
    log "DNS_CLEANUP_VERIFY_RESULT status=fail reason=no_fallback_dns"
    echo "DNS_CLEANUP_VERIFY_RESULT status=fail reason=no_fallback_dns"
    return 1
  fi

  _needs_fallback=0
  if resolv_has_bad_dns "$BAD_DNS"; then
    _needs_fallback=1
    log "dns cleanup verify: residual bad dns found, fallback_dns=$_fallback_dns"
  else
    if ! RES_OPTIONS="attempts:1 timeout:2" ping -c 1 "$DNS_PROBE_HOST_FALLBACK" >/dev/null 2>&1; then
      _needs_fallback=1
      log "dns cleanup verify: dns probe failed after backup restore, fallback_dns=$_fallback_dns"
    fi
  fi

  if [ "$_needs_fallback" = "1" ]; then
    replace_bad_resolv_only "$_fallback_dns" || true
  fi

  if resolv_has_bad_dns "$BAD_DNS"; then
    log "DNS_CLEANUP_VERIFY_RESULT status=fail reason=bad_dns_residual"
    echo "DNS_CLEANUP_VERIFY_RESULT status=fail reason=bad_dns_residual"
    return 1
  fi
  if RES_OPTIONS="attempts:1 timeout:2" ping -c 1 "$DNS_PROBE_HOST_FALLBACK" >/dev/null 2>&1; then
    log "DNS_CLEANUP_VERIFY_RESULT status=pass fallback_dns=$_fallback_dns"
    echo "DNS_CLEANUP_VERIFY_RESULT status=pass fallback_dns=$_fallback_dns"
    return 0
  fi
  log "DNS_CLEANUP_VERIFY_RESULT status=fail reason=dns_probe_fail fallback_dns=$_fallback_dns"
  echo "DNS_CLEANUP_VERIFY_RESULT status=fail reason=dns_probe_fail fallback_dns=$_fallback_dns"
  return 1
}

# Route command wrapper: prefer /data/busybox, fall back to system route
_route() {
  if [ -x /data/busybox ]; then
    /data/busybox route "$@" 2>/dev/null || true
  else
    route "$@" 2>/dev/null || true
  fi
}

# Pick the first same-subnet address that is unreachable (safe to use as fake GW).
# Args: $1=prefix (e.g. 192.168.3), $2=real_gw, $3=own_ip
_pick_unused_gw() {
  _pgw_prefix="$1"
  _pgw_real="$2"
  _pgw_own="$3"
  for _cand in 254 253 250 249; do
    _ip="${_pgw_prefix}.${_cand}"
    [ "$_ip" = "$_pgw_real" ] && continue
    [ "$_ip" = "$_pgw_own" ] && continue
    if ! ping -c 1 -W 1 "$_ip" >/dev/null 2>&1; then
      echo "$_ip"
      return 0
    fi
    log "_pick_unused_gw: $_ip responds, trying next candidate"
  done
  log "WARN: _pick_unused_gw: all candidates reachable, falling back to ${_pgw_prefix}.254"
  echo "${_pgw_prefix}.254"
}

IFACE_BAK="$STATE_DIR/iface.bak"
WLAN_STATE="$STATE_DIR/wlan.state"

restore_all() {
  log "cleanup begin"
  _restore_rc=0
  dnsproxy_resume
  dns_unix_resume
  # restore resolv
  if [ -f "$RESOLV_LIST" ]; then
    while IFS= read -r p; do
      [ -z "$p" ] && continue
      base="$(echo "$p" | sed 's#/#_#g')"
      bak="$STATE_DIR/${base}.bak"
      if [ -f "$bak" ]; then
        cat "$bak" > "$p" 2>/dev/null || true
        log "restored resolv -> $p"
      fi
    done < "$RESOLV_LIST"
  fi

  # restore iface
  if [ -f "$IFACE_BAK" ]; then
    IFACE="$(sed -n 's/^IFACE=\(.*\)$/\1/p' "$IFACE_BAK" | head -n 1)"
    IP="$(sed -n 's/^IP=\(.*\)$/\1/p' "$IFACE_BAK" | head -n 1)"
    MASK="$(sed -n 's/^MASK=\(.*\)$/\1/p' "$IFACE_BAK" | head -n 1)"
    if [ -n "${IFACE:-}" ]; then
      if [ -n "${IP:-}" ] && [ -n "${MASK:-}" ]; then
        ifconfig "$IFACE" "$IP" netmask "$MASK" up 2>/dev/null || ifconfig "$IFACE" up 2>/dev/null || true
        log "restored iface $IFACE ip=$IP mask=$MASK"
      else
        ifconfig "$IFACE" up 2>/dev/null || true
        log "restored iface $IFACE up (no ip/mask saved)"
      fi
    fi
  fi

  # restore wlan (reconnect via wpa_cli) — skip for auth_fail: bad-PSK supplicant won't reach COMPLETED, the auth_fail block below replaces wpa with good conf
  if [ -f "$WLAN_STATE" ] && [ ! -f "$WPA_BAD_APPLIED" ]; then
    IFW="$(sed -n 's/^WLAN_IFACE=\(.*\)$/\1/p' "$WLAN_STATE" | head -n 1)"
    if [ -n "${IFW:-}" ] && command -v wpa_cli >/dev/null 2>&1; then
      wpa_cli -p "$WPA_CTRL" -i "$IFW" reconnect >/dev/null 2>&1 || true
      log "wlan reconnect on $IFW"
      # wait up to 20s for wpa_state=COMPLETED
      _wrc_t=0
      _wrc_st=""
      while [ $_wrc_t -lt 20 ]; do
        _wrc_st=$(wpa_cli -p "$WPA_CTRL" -i "$IFW" status 2>/dev/null | grep "^wpa_state=" | sed 's/^wpa_state=//')
        [ "$_wrc_st" = "COMPLETED" ] && break
        sleep 2
        _wrc_t=$((_wrc_t + 2))
      done
      if [ "$_wrc_st" = "COMPLETED" ]; then
        log "restore_all: wlan $IFW wpa_state=COMPLETED after ${_wrc_t}s"
      else
        log "restore_all: WARN wlan $IFW wpa_state=${_wrc_st:-unknown} after ${_wrc_t}s (COMPLETED timeout)"
      fi
      if [ "$_wrc_st" != "COMPLETED" ] && [ -f "$WPA_CONF" ]; then
        echo "[net_fault] restore_all: restarting wpa_supplicant from existing conf for $IFW"
        killall wpa_supplicant 2>/dev/null || true
        sleep 1
        safe_reset_wpa_ctrl_dir || true
        mkdir -p "$WPA_CTRL" 2>/dev/null || true
        chmod 777 "$WPA_CTRL" 2>/dev/null || true
        ifconfig "$IFW" up 2>/dev/null || true
        /system/bin/wpa_supplicant -B -D nl80211 -i "$IFW" -c "$WPA_CONF" -P "$WPA_PID_FILE" 2>/dev/null || true
        sleep 2
        wpa_cli -p "$WPA_CTRL" -i "$IFW" reconnect >/dev/null 2>&1 || true
        _wrc_t=0
        _wrc_restart_st=""
        while [ $_wrc_t -lt 45 ]; do
          _wrc_restart_st=$(wpa_cli -p "$WPA_CTRL" -i "$IFW" status 2>/dev/null | grep "^wpa_state=" | sed 's/^wpa_state=//')
          echo "[net_fault] restore_all restart wait t=${_wrc_t}s state=${_wrc_restart_st:-no_response}"
          [ "$_wrc_restart_st" = "COMPLETED" ] && break
          sleep 3
          _wrc_t=$((_wrc_t + 3))
        done
        if [ "$_wrc_restart_st" = "COMPLETED" ]; then
          echo "[net_fault] restore_all: wpa_state=COMPLETED after restart on $IFW"
        else
          echo "[net_fault] restore_all: WARN restart wpa_state=${_wrc_restart_st:-unknown} on $IFW"
        fi
      fi
    fi
  fi

  # restore wlan IP (net_wifi_disconnect / net_no_ipv4_on_iface)
  if [ -f "$WLAN_IP_BAK" ]; then
    IFW="$(sed -n 's/^WLAN_IFACE=\(.*\)$/\1/p' "$WLAN_IP_BAK" | head -n 1)"
    IPW="$(sed -n 's/^IP=\(.*\)$/\1/p' "$WLAN_IP_BAK" | head -n 1)"
    MASKW="$(sed -n 's/^MASK=\(.*\)$/\1/p' "$WLAN_IP_BAK" | head -n 1)"
    if [ -n "${IFW:-}" ] && [ -n "${IPW:-}" ] && [ -n "${MASKW:-}" ]; then
      ifconfig "$IFW" "$IPW" netmask "$MASKW" 2>/dev/null || true
      log "restored wlan IP $IPW mask=$MASKW on $IFW"
    fi
  fi

  # restore default route (net_no_default_route / net_wrong_default_route / net_gateway_unreachable)
  rm -f "$STATE_DIR/wrong_route.ready" 2>/dev/null || true
  rm -f "$STATE_DIR/gateway_unreachable.ready" 2>/dev/null || true
  if [ -f "$ROUTE_BAK" ]; then
    IFW="$(sed -n 's/^WLAN_IFACE=\(.*\)$/\1/p' "$ROUTE_BAK" | head -n 1)"
    GWR="$(sed -n 's/^GW=\(.*\)$/\1/p' "$ROUTE_BAK" | head -n 1)"
    FGWR="$(sed -n 's/^FAKE_GW=\(.*\)$/\1/p' "$ROUTE_BAK" | head -n 1)"
    ROUTE_MODE="$(sed -n 's/^MODE=\(.*\)$/\1/p' "$ROUTE_BAK" | head -n 1)"
    if [ -n "${IFW:-}" ] && [ -n "${GWR:-}" ] && [ "$GWR" != "0.0.0.0" ]; then
      if [ "$ROUTE_MODE" = "link_down" ]; then
        CUR_IP="$(get_iface_ip "$IFW")"
        if [ -n "${CUR_IP:-}" ] && ! grep -q "^${IFW}[[:space:]]*00000000" /proc/net/route 2>/dev/null; then
          _route add default gw "$GWR" dev "$IFW"
          sleep 3
          if ! grep -q "^${IFW}[[:space:]]*00000000" /proc/net/route 2>/dev/null; then
            _route add default gw "$GWR" dev "$IFW"
          fi
          log "restored link_down default route gw=$GWR dev=$IFW"
        else
          log "skip link_down default route restore dev=$IFW ip=${CUR_IP:-none}"
        fi
      else
        [ -n "${FGWR:-}" ] && _route del default gw "$FGWR" dev "$IFW"
        _route add default gw "$GWR" dev "$IFW"
        # retry once after 3s: OS network manager may flush the route immediately after restore
        sleep 3
        if ! grep -q "^${IFW}[[:space:]]*00000000" /proc/net/route 2>/dev/null; then
          _route add default gw "$GWR" dev "$IFW"
        fi
        log "restored default route gw=$GWR dev=$IFW (fake_gw=${FGWR:-none})"
      fi
    fi
  fi

  # restore wpa_supplicant with original conf (net_wifi_auth_fail_wrong_psk)
  if [ -f "$WPA_BAD_APPLIED" ]; then
    # Kill bad-PSK wpa_supplicant: prefer PID file, fallback to name search
    _wpa_bad_pid=""
    [ -f "$WPA_PID_FILE" ] && _wpa_bad_pid="$(cat "$WPA_PID_FILE" 2>/dev/null || true)"
    [ -z "${_wpa_bad_pid:-}" ] && _wpa_bad_pid="$(_find_pid_by_name wpa_supplicant)"
    if [ -n "${_wpa_bad_pid:-}" ]; then
      kill -TERM "$_wpa_bad_pid" 2>/dev/null || true
      sleep 1
      kill -KILL "$_wpa_bad_pid" 2>/dev/null || true
    fi
    sleep 1
    if [ -f "$WPA_CONF" ]; then
      safe_reset_wpa_ctrl_dir || true
      mkdir -p "$WPA_CTRL" 2>/dev/null || true
      chmod 777 "$WPA_CTRL" 2>/dev/null || true
      ifconfig "$WLAN_IFACE" up 2>/dev/null || true
      /system/bin/wpa_supplicant -B -D nl80211 -i "$WLAN_IFACE" -c "$WPA_CONF" -P "$WPA_PID_FILE" 2>/dev/null || true
      sleep 2
      wpa_cli -p "$WPA_CTRL" -i "$WLAN_IFACE" reconnect >/dev/null 2>&1 || true
      _wpaf_t=0
      _wpaf_st=""
      _wpaf_nudged=0
      while [ $_wpaf_t -lt 45 ]; do
        _wpaf_st=$(wpa_cli -p "$WPA_CTRL" -i "$WLAN_IFACE" status 2>/dev/null | grep "^wpa_state=" | sed 's/^wpa_state=//')
        echo "[net_fault] restore_all wpa wait t=${_wpaf_t}s state=${_wpaf_st:-no_response}"
        [ "$_wpaf_st" = "COMPLETED" ] && break
        if [ "$_wpaf_nudged" = "0" ] && [ $_wpaf_t -ge 18 ]; then
          echo "[net_fault] restore_all: nudge wpa (reconfigure+reconnect) at t=${_wpaf_t}s state=${_wpaf_st:-unknown}"
          ifconfig "$WLAN_IFACE" up 2>/dev/null || true
          wpa_cli -p "$WPA_CTRL" -i "$WLAN_IFACE" reconfigure >/dev/null 2>&1 || true
          wpa_cli -p "$WPA_CTRL" -i "$WLAN_IFACE" reconnect >/dev/null 2>&1 || true
          _wpaf_nudged=1
        fi
        sleep 3
        _wpaf_t=$((_wpaf_t + 3))
      done
      if [ "$_wpaf_st" = "COMPLETED" ]; then
        echo "[net_fault] restore_all: wpa_state=COMPLETED after ${_wpaf_t}s on $WLAN_IFACE (nudged=${_wpaf_nudged})"
      else
        echo "[net_fault] restore_all: WARN wpa_state=${_wpaf_st:-unknown} after ${_wpaf_t}s on $WLAN_IFACE (nudged=${_wpaf_nudged})"
      fi
      ifconfig "$WLAN_IFACE" up 2>/dev/null || true
      _auth_gw=""
      [ -f "$STATE_DIR/route.bak" ] && _auth_gw="$(grep '^GW=' "$STATE_DIR/route.bak" 2>/dev/null | sed 's/^GW=//')"
      if [ -n "${_auth_gw:-}" ]; then
        /data/busybox route add default gw "$_auth_gw" dev "$WLAN_IFACE" 2>/dev/null || true
        log "restore_all: re-added default route gw=${_auth_gw} dev=${WLAN_IFACE}"
      fi
      log "restored wpa_supplicant with original conf on $WLAN_IFACE"
    fi
    rm -f "$WPA_BAD_APPLIED" 2>/dev/null || true
    rm -f "$WPA_BAD_CONF" 2>/dev/null || true
  fi

  clear_target_block
  clear_dns_block
  if [ "${MODE:-}" = "dns_fail" ] || [ "${MODE:-}" = "cleanup" ] || resolv_has_bad_dns "$BAD_DNS"; then
    restore_resolv_fallback_if_needed || _restore_rc=1
  fi
  if [ -f "$RESOLV_LIST" ]; then
    while IFS= read -r p; do
      [ -z "$p" ] && continue
      base="$(echo "$p" | sed 's#/#_#g')"
      rm -f "$STATE_DIR/${base}.bak" 2>/dev/null || true
    done < "$RESOLV_LIST"
  fi
  rm -f "$IFACE_BAK" "$RESOLV_LIST" 2>/dev/null || true

  # Unified subtype-residual cleanup: every NET subtype creates backup/state files
  # specific to its restore path. After the restore branch above has consumed
  # them, sweep them so post baseline gate (no_root_bak, no .applied) is clean
  # regardless of which subtype just ran. We only touch files in $STATE_DIR root
  # (no recurse), so quarantine_*/ subdirs and stale_before_*/ archives stay
  # intact for forensics.
  _cleaned=""
  for _bf in \
    "$WLAN_IP_BAK" \
    "$ROUTE_BAK" \
    "$WLAN_STATE" \
    "$WPA_BAD_CONF" \
    "$STATE_DIR/wrong_route.ready" \
    "$STATE_DIR/gateway_unreachable.ready"; do
    [ -e "$_bf" ] || continue
    rm -f "$_bf" 2>/dev/null && _cleaned="${_cleaned} ${_bf##*/}"
  done
  # Safety net: any *.bak still left in STATE_DIR root
  for _bf in "$STATE_DIR"/*.bak; do
    [ -f "$_bf" ] || continue
    rm -f "$_bf" 2>/dev/null && _cleaned="${_cleaned} ${_bf##*/}"
  done
  rm -f "$STATE_DIR/dnsproxy.pid" "$STATE_DIR/dnsproxy.stopped" "$STATE_DIR/dns_unix_proxy.pid" 2>/dev/null || true

  if [ "${MODE:-}" = "dns_fail" ]; then
    echo "DNS_FAIL_CLEANUP_RESULT bak_residual_removed=1"
  elif [ "${MODE:-}" = "net_public_ip_unreachable" ]; then
    echo "PUBLIC_IP_UNREACHABLE_CLEANUP_RESULT bak_residual_removed=1"
  fi
  echo "### NET_CLEANUP_RESULT mode=${MODE:-unknown} bak_residual_removed=1 swept=${_cleaned:- none}"
  rm -f "$STATE_DIR/injector_net_wifi_disconnect.pid" 2>/dev/null || true
  rm -f "$STATE_DIR/injector_net_wifi_auth_fail_wrong_psk.pid" 2>/dev/null || true
  log "cleanup end"
  return "$_restore_rc"
}

on_term() {
  # 防止 exit 再触发 EXIT trap 导致重复恢复也没关系，但更干净
  trap - EXIT
  restore_all
  exit $?
}

trap on_term INT TERM
trap restore_all EXIT


# ---------- params ----------
MODE="${NET_MODE:-${1:-dns_fail}}"
IFACE="${NET_IFACE:-${2:-eth1}}"
FLAP_SEC="${NET_FLAP_SEC:-${3:-3}}"
BAD_DNS="${NET_BAD_DNS:-${4:-127.0.0.2}}"
WLAN_IFACE="${NET_WLAN_IFACE:-wlan0}"
HOLD_SEC="${NET_HOLD_SEC:-15}"
FLAP_COUNT="${NET_FLAP_COUNT:-2}"
FLAP_DOWN_SEC="${NET_FLAP_DOWN_SEC:-$FLAP_SEC}"
FLAP_UP_SEC="${NET_FLAP_UP_SEC:-$FLAP_SEC}"
WPA_CTRL="${NET_WPA_CTRL:-$WPA_CTRL}"
WPA_CONF="${NET_WPA_CONF:-$WPA_CONF}"
WPA_PID_FILE="${NET_WPA_PID_FILE:-$WPA_PID_FILE}"
WPA_BAD_CONF="${NET_WPA_BAD_CONF:-$WPA_BAD_CONF}"
WLAN_SSID="${NET_WLAN_SSID:-$WLAN_SSID}"
WPA_BAD_PSK="${NET_WPA_BAD_PSK:-$WPA_BAD_PSK}"
TARGET_IP="${NET_TARGET_IP:-8.8.4.4}"

log "start mode=$MODE iface=$IFACE flap_sec=$FLAP_SEC hold_sec=$HOLD_SEC flap_count=$FLAP_COUNT flap_down=$FLAP_DOWN_SEC flap_up=$FLAP_UP_SEC bad_dns=$BAD_DNS wlan_if=$WLAN_IFACE"
log "ifconfig -a snapshot:"
ifconfig -a 2>/dev/null || true

# save iface state
IP0="$(get_iface_ip "$IFACE")"
MASK0="$(get_iface_mask "$IFACE")"
{
  echo "IFACE=$IFACE"
  echo "IP=$IP0"
  echo "MASK=$MASK0"
} > "$IFACE_BAK" 2>/dev/null || true

# save resolv targets
collect_resolv_targets

# ====== [MOD] dns_fail：进入前先清一次，避免历史残留影响本次 ======
case "$MODE" in
  dns_fail)
    clear_dns_block

    # 1) 收集“当前系统正在用”的 DNS 端点（resolv + 活体探测），并先行阻断
    _dns_list_reset
    _dns_list_collect_from_resolv
    _probe_and_extend_dns_list_v4

    # 2) 写坏 resolv.conf（若写失败，也至少通过阻断真实端点让解析失败）
    write_dns_fail "$BAD_DNS" || log "WARN: dns_fail write to resolv targets failed (read-only?)"
    _dns_list_add4 "$BAD_DNS"
    _dns_list_collect_from_resolv

    # 3) 应用阻断（marker 保证幂等）
    apply_dns_block
    echo "### SELFTEST_DNS_RESOLVE_BEFORE_FREEZE"
        # 4) 如果仍然能解析，说明存在 fallback（你现在就是这个情况）=> 冻结 dnsproxyd socket owner（netsysnative）
    th="t$$.${DNS_PROOF_BASE_IP}.nip.io"
    if RES_OPTIONS="attempts:1 timeout:1" ping -c 1 "$th" >/dev/null 2>&1; then
      echo "SELFTEST_DNS_RESOLVE_BEFORE_FREEZE_RESULT host=$th result=resolve_ok"
      log "dns_fail selftest: still resolves; fallback freeze unix-dns proxy"
      dns_unix_freeze

      # 兜底验证：冻结后 DNS 应该失败，但直接 ping IP 应该仍通（避免变成断网注入）
      if RES_OPTIONS="attempts:1 timeout:1" ping -c 1 "$th" >/dev/null 2>&1; then
        echo "SELFTEST_DNS_AFTER_FREEZE_RESULT host=$th result=resolve_ok"
        log "WARN: dns_fail selftest: still resolves even after freeze (unexpected)"
      else
        echo "SELFTEST_DNS_AFTER_FREEZE_RESULT host=$th result=resolve_fail"
        log "dns_fail selftest: resolve blocked after freeze (ok)"
      fi
      echo "### SELFTEST_IP_STILL_REACHABLE"
      if ping -c 1 "$DNS_PROOF_BASE_IP" >/dev/null 2>&1; then
        echo "SELFTEST_IP_STILL_REACHABLE_RESULT ip=$DNS_PROOF_BASE_IP result=ok"
      else
        echo "SELFTEST_IP_STILL_REACHABLE_RESULT ip=$DNS_PROOF_BASE_IP result=fail"
      fi
    else
      echo "SELFTEST_DNS_RESOLVE_BEFORE_FREEZE_RESULT host=$th result=resolve_fail"
      log "dns_fail selftest: already blocked (ok)"
    fi

    if [ "$DNS_SELFTEST_AFTER_APPLY" = "1" ] && [ "$DNS_PROXY_FREEZE" = "1" ]; then
      echo "### SELFTEST_DNS_PROXY_FREEZE"
      th="t$$2.${DNS_PROOF_BASE_IP}.nip.io"
      if RES_OPTIONS="attempts:1 timeout:1" ping -c 1 "$th" >/dev/null 2>&1; then
        echo "SELFTEST_DNS_PROXY_FREEZE_RESULT host=$th result=resolve_ok"
        log "dns_fail: still resolves after apply; fallback to freeze $DNS_PROXY_NAME"
        dnsproxy_freeze
      else
        echo "SELFTEST_DNS_PROXY_FREEZE_RESULT host=$th result=resolve_fail"
        log "dns_fail: resolution blocked (ok)"
      fi
    fi

    # 4) 持续保持配置，并定期打印“解析失败自证”（避免仅凭 ping 既往缓存误判）
    sec=0
    case "$HOLD_SEC" in
      ""|*[!0-9]*) _dns_hold_sec=15 ;;
      *) _dns_hold_sec="$HOLD_SEC" ;;
    esac
    echo "### NET_DNS_FAIL_APPLIED bad_dns=$BAD_DNS hold_sec=$_dns_hold_sec"
    echo "### NET_DNS_FAIL_HOLD_START hold_sec=$_dns_hold_sec"
    sec=0
    while [ "$sec" -lt "$_dns_hold_sec" ]; do
      write_dns_fail "$BAD_DNS" >/dev/null 2>&1 || true
      sec=$((sec+1))

      if [ "$DNS_PROOF_ENABLE" = "1" ]; then
        # 每 DNS_PROOF_INTERVAL_SEC 秒触发一次：
        # 用不同前缀的 nip.io 域名强制触发解析（即便有缓存，也很难命中）
        if [ "$DNS_PROOF_INTERVAL_SEC" -gt 0 ] && [ $((sec % DNS_PROOF_INTERVAL_SEC)) -eq 0 ]; then
          h="t$$${sec}.${DNS_PROOF_BASE_IP}.nip.io"
          log "dns_proof: try resolve+ping $h"
          RES_OPTIONS="attempts:1 timeout:1" ping -c 1 "$h" 2>&1 | head -n 2 | while IFS= read -r line; do
            [ -n "$line" ] && log "dns_proof: $line"
          done
        fi
      fi

      sleep 1
    done
    echo "### NET_DNS_FAIL_HOLD_DONE elapsed_sec=$sec"
    echo "### NET_DNS_FAIL_CLEANUP_START"
    trap - EXIT
    restore_all
    _dns_cleanup_rc=$?
    echo "### NET_DNS_FAIL_CLEANUP_DONE status=$_dns_cleanup_rc"
    if [ "$_dns_cleanup_rc" -ne 0 ]; then
      echo "[net_fault] dns_fail cleanup verification failed; next manual step: NET_MODE=cleanup NET_WLAN_IFACE=$WLAN_IFACE $0"
    fi
    echo "### NET_DNS_FAIL_EXIT status=$_dns_cleanup_rc"
    exit "$_dns_cleanup_rc"
    ;;



  link_down)
    if [ "$IFACE" = "$WLAN_IFACE" ]; then
      GW0="$(get_wlan_gw "$WLAN_IFACE")"
      if [ -n "${GW0:-}" ] && [ "$GW0" != "0.0.0.0" ]; then
        { echo "MODE=link_down"; echo "WLAN_IFACE=$WLAN_IFACE"; echo "GW=${GW0:-}"; } > "$ROUTE_BAK" 2>/dev/null || true
        log "link_down: saved default route gw=${GW0:-unknown} dev=$WLAN_IFACE"
      else
        log "link_down: no pre default route to save for $WLAN_IFACE"
      fi
    fi
    log "link_down hold: keep $IFACE down (target_hold=${HOLD_SEC}s)"
    while :; do
      ifconfig "$IFACE" down 2>/dev/null || true
      sleep 1
    done
    ;;

  link_flap)
    log "link_flap loop: count=${FLAP_COUNT} down=${FLAP_DOWN_SEC}s up=${FLAP_UP_SEC}s"
    n=0
    while :; do
      log "flap: DOWN $IFACE"
      ifconfig "$IFACE" down 2>/dev/null || true
      sleep "$FLAP_DOWN_SEC"
      log "flap: UP   $IFACE"
      if [ -n "$IP0" ] && [ -n "$MASK0" ]; then
        ifconfig "$IFACE" "$IP0" netmask "$MASK0" up 2>/dev/null || ifconfig "$IFACE" up 2>/dev/null || true
      else
        ifconfig "$IFACE" up 2>/dev/null || true
      fi
      sleep "$FLAP_UP_SEC"
      n=$((n+1))
      if [ "$FLAP_COUNT" -gt 0 ] && [ "$n" -ge "$FLAP_COUNT" ]; then
        log "link_flap loop: reached configured count=${FLAP_COUNT}, keep iface in current state"
        while :; do sleep 1; done
      fi
    done
    ;;

  wlan_disconnect)
    if command -v wpa_cli >/dev/null 2>&1; then
      echo "WLAN_IFACE=$WLAN_IFACE" > "$WLAN_STATE" 2>/dev/null || true
      log "wlan_disconnect hold: keep $WLAN_IFACE disconnected"
      while :; do
        wpa_cli -p "$WPA_CTRL" -i "$WLAN_IFACE" disconnect >/dev/null 2>&1 || true
        sleep 2
      done
    else
      log "WARN: wpa_cli missing, wlan_disconnect skipped"
      while :; do sleep 1; done
    fi
    ;;

  net_wifi_disconnect)
    if command -v wpa_cli >/dev/null 2>&1; then
      echo "WLAN_IFACE=$WLAN_IFACE" > "$WLAN_STATE" 2>/dev/null || true
      WLAN_IP0="$(get_iface_ip "$WLAN_IFACE")"
      WLAN_MASK0="$(get_iface_mask "$WLAN_IFACE")"
      { echo "WLAN_IFACE=$WLAN_IFACE"; echo "IP=$WLAN_IP0"; echo "MASK=$WLAN_MASK0"; } > "$WLAN_IP_BAK" 2>/dev/null || true
      # save default route so restore_all() can re-add it (problem 4 fix)
      GW0="$(get_wlan_gw "$WLAN_IFACE")"
      { echo "WLAN_IFACE=$WLAN_IFACE"; echo "GW=${GW0:-}"; } > "$ROUTE_BAK" 2>/dev/null || true
      log "net_wifi_disconnect: disconnect $WLAN_IFACE and hold L3 down (saved gw=${GW0:-unknown})"
      # write PID file for reliable PS-side termination
      rm -f "$STATE_DIR/injector_net_wifi_disconnect.pid" 2>/dev/null || true
      echo $$ > "$STATE_DIR/injector_net_wifi_disconnect.pid" 2>/dev/null || true
      log "net_wifi_disconnect: injector pid=$$ saved"
      while :; do
        wpa_cli -p "$WPA_CTRL" -i "$WLAN_IFACE" disconnect >/dev/null 2>&1 || true
        ifconfig "$WLAN_IFACE" 0.0.0.0 2>/dev/null || true
        _route del default dev "$WLAN_IFACE"
        sleep 2
      done
    else
      log "WARN: wpa_cli missing, net_wifi_disconnect skipped"
      while :; do sleep 1; done
    fi
    ;;

  net_wifi_auth_fail_wrong_psk)
    if command -v wpa_cli >/dev/null 2>&1 && [ -f "$WPA_CONF" ]; then
      echo "WLAN_IFACE=$WLAN_IFACE" > "$WLAN_STATE" 2>/dev/null || true
      WLAN_IP0="$(get_iface_ip "$WLAN_IFACE")"
      WLAN_MASK0="$(get_iface_mask "$WLAN_IFACE")"
      { echo "WLAN_IFACE=$WLAN_IFACE"; echo "IP=$WLAN_IP0"; echo "MASK=$WLAN_MASK0"; } > "$WLAN_IP_BAK" 2>/dev/null || true
      GW0="$(get_wlan_gw "$WLAN_IFACE")"
      { echo "WLAN_IFACE=$WLAN_IFACE"; echo "GW=${GW0:-}"; } > "$ROUTE_BAK" 2>/dev/null || true
      log "net_wifi_auth_fail_wrong_psk: replace supplicant conf with bad PSK for ssid=$WLAN_SSID"
      printf 'ctrl_interface=%s\nupdate_config=1\n\nnetwork={\n    ssid="%s"\n    psk="%s"\n    key_mgmt=WPA-PSK\n}\n' \
        "$WPA_CTRL" "$WLAN_SSID" "$WPA_BAD_PSK" > "$WPA_BAD_CONF" 2>/dev/null || true
      killall wpa_supplicant 2>/dev/null || true
      sleep 1
      safe_reset_wpa_ctrl_dir || true
      mkdir -p "$WPA_CTRL" 2>/dev/null || true
      chmod 777 "$WPA_CTRL" 2>/dev/null || true
      /system/bin/wpa_supplicant -B -D nl80211 -i "$WLAN_IFACE" -c "$WPA_BAD_CONF" -P "$WPA_PID_FILE" 2>/dev/null || true
      echo "applied" > "$WPA_BAD_APPLIED" 2>/dev/null || true
      # write PID file for reliable PS-side termination
      rm -f "$STATE_DIR/injector_net_wifi_auth_fail_wrong_psk.pid" 2>/dev/null || true
      echo $$ > "$STATE_DIR/injector_net_wifi_auth_fail_wrong_psk.pid" 2>/dev/null || true
      log "net_wifi_auth_fail_wrong_psk: injector pid=$$ saved"
      log "bad-PSK wpa_supplicant started"
      while :; do
        pid="$(_find_pid_by_name wpa_supplicant)"
        if [ -z "${pid:-}" ]; then
          log "wpa_supplicant exited; restart with bad conf"
          safe_reset_wpa_ctrl_dir || true
          mkdir -p "$WPA_CTRL" 2>/dev/null || true
          chmod 777 "$WPA_CTRL" 2>/dev/null || true
          /system/bin/wpa_supplicant -B -D nl80211 -i "$WLAN_IFACE" -c "$WPA_BAD_CONF" -P "$WPA_PID_FILE" 2>/dev/null || true
        fi
        sleep 5
      done
    else
      log "WARN: wpa_cli or $WPA_CONF missing, net_wifi_auth_fail_wrong_psk skipped"
      while :; do sleep 1; done
    fi
    ;;

  net_no_default_route)
    GW0="$(get_wlan_gw "$WLAN_IFACE")"
    { echo "WLAN_IFACE=$WLAN_IFACE"; echo "GW=${GW0:-}"; } > "$ROUTE_BAK" 2>/dev/null || true
    log "net_no_default_route: keep default route deleted from $WLAN_IFACE (saved gw=${GW0:-unknown})"
    while :; do
      ip route del default dev "$WLAN_IFACE" 2>/dev/null || _route del default dev "$WLAN_IFACE"
      sleep 2
    done
    ;;

  net_no_ipv4_on_iface)
    WLAN_IP0="$(get_iface_ip "$WLAN_IFACE")"
    WLAN_MASK0="$(get_iface_mask "$WLAN_IFACE")"
    { echo "WLAN_IFACE=$WLAN_IFACE"; echo "IP=$WLAN_IP0"; echo "MASK=$WLAN_MASK0"; } > "$WLAN_IP_BAK" 2>/dev/null || true
    GW0="$(get_wlan_gw "$WLAN_IFACE")"
    { echo "WLAN_IFACE=$WLAN_IFACE"; echo "GW=${GW0:-}"; } > "$ROUTE_BAK" 2>/dev/null || true
    log "net_no_ipv4_on_iface: clear IP from $WLAN_IFACE (saved ip=${WLAN_IP0:-unknown} gw=${GW0:-unknown})"
    while :; do
      ifconfig "$WLAN_IFACE" 0.0.0.0 2>/dev/null || true
      sleep 2
    done
    ;;

  net_wrong_default_route|net_gateway_unreachable)
    # Clear stale ready marker before starting
    if [ "$MODE" = "net_gateway_unreachable" ]; then
      _route_ready="$STATE_DIR/gateway_unreachable.ready"
      _route_label="net_gateway_unreachable"
      _route_summary="gateway unreachable"
    else
      _route_ready="$STATE_DIR/wrong_route.ready"
      _route_label="net_wrong_default_route"
      _route_summary="fake default route"
    fi
    rm -f "$STATE_DIR/wrong_route.ready" "$STATE_DIR/gateway_unreachable.ready" 2>/dev/null || true

    GW0="$(get_wlan_gw "$WLAN_IFACE")"
    _wlan_ip="$(get_iface_ip "$WLAN_IFACE")"
    _prefix="${_wlan_ip%.*}"
    if [ -n "${NET_WRONG_GW:-}" ]; then
      FAKE_GW="$NET_WRONG_GW"
    else
      FAKE_GW="$(_pick_unused_gw "$_prefix" "${GW0:-}" "$_wlan_ip")"
    fi
    { echo "WLAN_IFACE=$WLAN_IFACE"; echo "GW=${GW0:-}"; echo "FAKE_GW=$FAKE_GW"; echo "MODE=$MODE"; } > "$ROUTE_BAK" 2>/dev/null || true

    FAKE_GW_HEX="$(_ip2hex4_le "$FAKE_GW")"
    REAL_GW_HEX="$(_ip2hex4_le "${GW0:-0.0.0.0}")"

    echo "[net_fault] ${_route_label} start: real_gw=${GW0:-unknown} real_gw_hex=${REAL_GW_HEX} fake_gw=${FAKE_GW} fake_gw_hex=${FAKE_GW_HEX} wlan_ip=${_wlan_ip} prefix=${_prefix} iface=${WLAN_IFACE}"
    echo "[net_fault] /proc/net/route BEFORE injection:"
    cat /proc/net/route 2>/dev/null || true

    _wdr_injected=0
    _wdr_ready_written=0
    _wdr_loop=0
    _wdr_max_no_ready=15

    while :; do
      _wdr_loop=$((_wdr_loop + 1))

      # Delete real default route (only when not yet injected)
      if [ "$_wdr_injected" = "0" ] && [ -n "${GW0:-}" ]; then
        echo "[net_fault] loop=${_wdr_loop}: route del default gw ${GW0} dev ${WLAN_IFACE}"
        if [ -x /data/busybox ]; then
          /data/busybox route del default gw "$GW0" dev "$WLAN_IFACE" 2>&1 || true
        else
          route del default gw "$GW0" dev "$WLAN_IFACE" 2>&1 || true
        fi
        echo "[net_fault] route del done"
      fi

      # Add fake default route
      echo "[net_fault] loop=${_wdr_loop}: route add default gw ${FAKE_GW} dev ${WLAN_IFACE}"
      if [ -x /data/busybox ]; then
        /data/busybox route add default gw "$FAKE_GW" dev "$WLAN_IFACE" 2>&1 || true
      else
        route add default gw "$FAKE_GW" dev "$WLAN_IFACE" 2>&1 || true
      fi
      echo "[net_fault] route add done"

      # Snapshot /proc/net/route
      echo "[net_fault] /proc/net/route AFTER loop=${_wdr_loop}:"
      cat /proc/net/route 2>/dev/null || true

      # Verify exact match of fake GW hex and presence of subnet route
      _wdr_def=0
      _wdr_sub=0
      _wdr_fake_exact=0
      _wdr_tmp="$STATE_DIR/.wdr_rt_$$"
      sed '1d' /proc/net/route 2>/dev/null > "$_wdr_tmp" 2>/dev/null || true
      while IFS= read -r _wdr_line; do
        [ -z "$_wdr_line" ] && continue
        set -- $_wdr_line
        [ "$1" = "$WLAN_IFACE" ] || continue
        if [ "$2" = "00000000" ]; then
          _wdr_def=1
          _cur_gwhex="$3"
          if [ "$_cur_gwhex" = "$FAKE_GW_HEX" ]; then
            _wdr_fake_exact=1
          fi
          echo "[net_fault] default route: dest=$2 gw_hex=$3 fake_gw_hex=${FAKE_GW_HEX} exact=${_wdr_fake_exact}"
        else
          _wdr_sub=1
          echo "[net_fault] subnet route: dest=$2 gw_hex=$3"
        fi
      done < "$_wdr_tmp"
      rm -f "$_wdr_tmp" 2>/dev/null || true

      echo "[net_fault] verify loop=${_wdr_loop}: def=${_wdr_def} fake_exact=${_wdr_fake_exact} subnet=${_wdr_sub}"

      # Handle missing subnet route (network manager may have flushed routes)
      if [ "$_wdr_sub" = "0" ]; then
        echo "[net_fault] WARN: subnet route gone; re-adding ${_prefix}.0/255.255.255.0 dev ${WLAN_IFACE}"
        if [ -x /data/busybox ]; then
          /data/busybox route add -net "${_prefix}.0" netmask "255.255.255.0" dev "$WLAN_IFACE" 2>&1 || true
        else
          route add -net "${_prefix}.0" netmask "255.255.255.0" dev "$WLAN_IFACE" 2>&1 || true
        fi
        _wdr_injected=0
        sleep 1
        continue
      fi

      # Write ready marker only when fake/default blackhole route (exact hex match) + subnet route both present
      if [ "$_wdr_fake_exact" = "1" ] && [ "$_wdr_sub" = "1" ]; then
        _wdr_injected=1
        if [ "$_wdr_ready_written" = "0" ]; then
          echo "[net_fault] SUCCESS: ${_route_summary} verified (loop=${_wdr_loop}), writing ready marker"
          {
            echo "mode=${MODE}"
            echo "real_gw=${GW0:-unknown}"
            echo "fake_gw=${FAKE_GW}"
            echo "fake_gw_hex=${FAKE_GW_HEX}"
            echo "loop=${_wdr_loop}"
            echo "uptime=$(cat /proc/uptime 2>/dev/null | head -c 20 || true)"
            echo "### proc_net_route_snapshot"
            cat /proc/net/route 2>/dev/null || true
          } > "$_route_ready" 2>/dev/null || true
          _wdr_ready_written=1
        fi
      else
        _wdr_injected=0
        if [ "$_wdr_ready_written" = "0" ] && [ "$_wdr_loop" -ge "$_wdr_max_no_ready" ]; then
          echo "[net_fault] ERROR: fake route not stable after ${_wdr_max_no_ready} retries (fake_exact=${_wdr_fake_exact} subnet=${_wdr_sub}). Ready marker will NOT be written."
        fi
      fi

      sleep 2
    done
    ;;

  net_public_ip_unreachable)
    if ! _have_iptables; then
      log "ERROR: iptables missing, cannot inject net_public_ip_unreachable"
      exit 2
    fi
    log "net_public_ip_unreachable: block target_ip=$TARGET_IP via iptables OUTPUT DROP"
    iptables -I OUTPUT 1 -d "$TARGET_IP" -j DROP 2>/dev/null || true
    echo "$TARGET_IP" > "$IPT_TARGET_MARK" 2>/dev/null || true
    while :; do
      iptables -C OUTPUT -d "$TARGET_IP" -j DROP 2>/dev/null || iptables -I OUTPUT 1 -d "$TARGET_IP" -j DROP 2>/dev/null || true
      sleep 2
    done
    ;;

  cleanup)
    echo "### NET_FAULT_CLEANUP_MODE"
    trap - EXIT
    restore_all
    _cleanup_rc=$?
    echo "### NET_FAULT_CLEANUP_EXIT status=$_cleanup_rc"
    exit "$_cleanup_rc"
    ;;

  *)
    log "ERROR: unknown mode=$MODE"
    exit 2
    ;;
esac
