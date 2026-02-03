#!/system/bin/sh
set -u

BB=/data/local/tmp/busybox
BASE=/data/faultmon/demo_stage2
BIN="$BASE/bin"
LOG="$BASE/logs/triggerd.log"
STATE_DIR=/data/faultmon/state
FAULTMON=/data/faultmon/faultmon.sh

fail=0
fails=""
run_id=""

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

is_digits() {
  case "$1" in
    ''|*[!0-9]*) return 1 ;;
    *) return 0 ;;
  esac
}

now_s() {
  "$BB" date +%s 2>/dev/null || date +%s 2>/dev/null || echo 0
}

need_bin() {
  if [ ! -x "$1" ]; then
    say "RESULT=FAIL"
    say "FAILED=missing_$2"
    say "NEXT_FIX=install $1"
    exit 1
  fi
}

cleanup() {
  if [ -x "$BIN/inject_cpu.sh" ]; then
    "$BIN/inject_cpu.sh" stop >/dev/null 2>&1 || true
  fi
}
trap 'cleanup' EXIT

need_bin "$BB" busybox
need_bin "$BIN/triggerd.sh" triggerd
need_bin "$BIN/inject_cpu.sh" inject_cpu
need_bin "$FAULTMON" faultmon

say "A1: faultmon start/stop x5"
a1_ok=1
i=1
while [ "$i" -le 5 ]; do
  out="$(sh "$FAULTMON" stop 2>&1)"
  rc=$?
  echo "$out" | "$BB" grep -F -e "Text file busy" >/dev/null 2>&1 && a1_ok=0
  [ "$rc" -eq 0 ] || a1_ok=0
  out="$(sh "$FAULTMON" start 2>&1)"
  rc=$?
  echo "$out" | "$BB" grep -F -e "Text file busy" >/dev/null 2>&1 && a1_ok=0
  [ "$rc" -eq 0 ] || a1_ok=0
  i=$((i + 1))
done
if [ "$a1_ok" -eq 1 ]; then
  say "A1_PASS"
else
  say "A1_FAIL"
  add_fail A1
fi

say "A2: metrics update"
sys_file=""
waited=0
while [ "$waited" -lt 10 ]; do
  sys_file="$("$BB" ls -t /data/faultmon/metrics/sys_*.csv 2>/dev/null | "$BB" head -n 1)"
  [ -n "$sys_file" ] && break
  "$BB" sleep 1
  waited=$((waited + 1))
done
if [ -z "$sys_file" ]; then
  say "A2_FAIL missing_sys_csv"
  add_fail A2
else
  l1="$("$BB" tail -n 1 "$sys_file" 2>/dev/null)"
  "$BB" sleep 6
  l2="$("$BB" tail -n 1 "$sys_file" 2>/dev/null)"
  if [ -n "$l1" ] && [ -n "$l2" ] && [ "$l1" != "$l2" ]; then
    say "A2_PASS"
  else
    say "A2_FAIL no_change"
    add_fail A2
  fi
fi

say "B1: help/no-arg rc + latency"
help_t0="$(now_s)"
"$BIN/triggerd.sh" --help >/dev/null 2>&1
help_rc=$?
help_t1="$(now_s)"
help_dur_ok=1
if is_digits "$help_t0" && is_digits "$help_t1"; then
  help_dur=$((help_t1 - help_t0))
  if [ "$help_dur" -gt 1 ]; then help_dur_ok=0; fi
fi
if [ "$help_rc" -eq 0 ] && [ "$help_dur_ok" -eq 1 ]; then
  say "B1_HELP_PASS"
else
  say "B1_HELP_FAIL rc=$help_rc"
  add_fail B1
fi

noarg_t0="$(now_s)"
"$BIN/triggerd.sh" >/dev/null 2>&1
noarg_rc=$?
noarg_t1="$(now_s)"
noarg_dur_ok=1
if is_digits "$noarg_t0" && is_digits "$noarg_t1"; then
  noarg_dur=$((noarg_t1 - noarg_t0))
  if [ "$noarg_dur" -gt 1 ]; then noarg_dur_ok=0; fi
fi
if [ "$noarg_rc" -ne 0 ] && [ "$noarg_dur_ok" -eq 1 ]; then
  say "B1_NOARG_PASS"
else
  say "B1_NOARG_FAIL rc=$noarg_rc"
  add_fail B1
fi

say "B2: cooldown_sec alias"
"$BIN/triggerd.sh" --once --mode cpu --interval 1 --hit_need 1 --cpu_hit_need 1 --threshold_load1_int 999 --cooldown_sec 5 >/dev/null 2>&1
b2_rc=$?
if [ "$b2_rc" -eq 0 ]; then
  say "B2_PASS"
else
  say "B2_FAIL rc=$b2_rc"
  add_fail B2
fi

say "B3: once writes last_trigger_epoch"
old_epoch="$("$BB" head -n 1 "$STATE_DIR/last_trigger_epoch" 2>/dev/null)"
"$BB" sleep 2
"$BIN/triggerd.sh" --once --mode cpu --interval 1 --hit_need 1 --cpu_hit_need 1 --threshold_load1_int 999 >/dev/null 2>&1
new_epoch="$("$BB" head -n 1 "$STATE_DIR/last_trigger_epoch" 2>/dev/null)"
log_hit="$("$BB" tail -n 20 "$LOG" 2>/dev/null | "$BB" grep -F -e "once_no_trigger" -e "once_trigger_done" | "$BB" tail -n 1)"
if is_digits "$new_epoch" && [ "$new_epoch" != "${old_epoch:-}" ] && [ -n "$log_hit" ]; then
  say "B3_PASS"
else
  say "B3_FAIL"
  add_fail B3
fi

say "B4: daemon/status/stop"
"$BIN/triggerd.sh" --daemon --mode cpu --interval 2 --hit_need 3 >/dev/null 2>&1
"$BB" sleep 1
status_out="$("$BIN/triggerd.sh" status 2>/dev/null)"
"$BIN/triggerd.sh" stop >/dev/null 2>&1
status_out2="$("$BIN/triggerd.sh" status 2>/dev/null)"
case "$status_out" in
  *"alive pid="*) status_ok=1 ;;
  *) status_ok=0 ;;
 esac
case "$status_out2" in
  *"not_running"*) stop_ok=1 ;;
  *) stop_ok=0 ;;
 esac
if [ "$status_ok" -eq 1 ] && [ "$stop_ok" -eq 1 ]; then
  say "B4_PASS"
else
  say "B4_FAIL"
  add_fail B4
fi

say "### SELFTEST_CPU_TRIGGER start"
"$BIN/inject_cpu.sh" start >/dev/null 2>&1 || true
proc_file=""
waited=0
while [ "$waited" -lt 20 ]; do
  proc_file="$("$BB" ls -t /data/faultmon/procs/procs_*.txt 2>/dev/null | "$BB" head -n 1)"
  [ -n "$proc_file" ] && break
  "$BB" sleep 2
  waited=$((waited + 2))
done
if [ -z "$proc_file" ]; then
  say "### SELFTEST_PROCS_SNAPSHOT create"
  ts="$(now_s)"
  mkdir -p /data/faultmon/procs 2>/dev/null || true
  ps > "/data/faultmon/procs/procs_${ts}000.txt" 2>/dev/null || true
fi
"$BIN/triggerd.sh" --once --mode cpu --interval 1 --hit_need 1 --cpu_hit_need 1 --threshold_load1_int 1 --cooldown_sec 1 >/dev/null 2>&1
line="$("$BB" tail -n 200 "$LOG" 2>/dev/null | "$BB" grep -F -e "trigger_bundle run_id=" | "$BB" tail -n 1)"
if [ -n "$line" ]; then
  run_id="${line#*run_id=}"
  run_id="${run_id%% *}"
fi
ok_line="$("$BB" tail -n 200 "$LOG" 2>/dev/null | "$BB" grep -F -e "OK: uploaded type=bundle" | "$BB" grep -F -e "${run_id}" | "$BB" tail -n 1)"
"$BIN/inject_cpu.sh" stop >/dev/null 2>&1 || true
say "### SELFTEST_CPU_TRIGGER end"

if [ -n "$run_id" ] && [ -n "$ok_line" ]; then
  say "C1_PASS"
else
  say "C1_FAIL"
  add_fail C1
fi

if [ -n "$run_id" ]; then
  say "RUN_ID=$run_id"
fi

if [ "$fail" -eq 0 ]; then
  say "RESULT=PASS"
  say "FAILED="
  say "NEXT_FIX=none"
else
  say "RESULT=FAIL"
  say "FAILED=$fails"
  say "NEXT_FIX=check $LOG and rerun"
fi
