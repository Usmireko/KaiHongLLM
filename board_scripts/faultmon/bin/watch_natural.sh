#!/bin/sh
#
# watch_natural.sh - Natural threshold watcher sidecar for faultmon

set -eu

PATH=/bin:/system/bin:/usr/bin:/usr/local/bin
export PATH
umask 027

BASE_DIR=/data/faultmon
LOG_DIR=$BASE_DIR/logs
STATE_DIR=$BASE_DIR/state
RATE_LIMIT_DIR=$STATE_DIR/ratelimit
PIPE=$STATE_DIR/event.pipe
OUT_LOG=$LOG_DIR/watch_natural.out
MAIN_STDOUT_LOG=$LOG_DIR/faultmon.out
DEDUP_FILE=$STATE_DIR/watch_natural_dedup.tsv

EVENT_DELIM="$(printf '\001')"
RATE_LIMIT_PER_MINUTE=60
DEDUP_WINDOW_MS=3000
POLL_INTERVAL=5
RATE_LIMIT_LAST_COUNT=0

DATE_MS_SUPPORTED=0
if date +%s%3N >/dev/null 2>&1; then
    DATE_MS_SUPPORTED=1
fi

now_ms() {
    if [ "$DATE_MS_SUPPORTED" -eq 1 ]; then
        date +%s%3N
    else
        printf '%s000\n' "$(date +%s)"
    fi
}

mkdir_p() {
    if [ ! -d "$1" ]; then
        mkdir -p "$1"
    fi
    chmod "$2" "$1" 2>/dev/null || true
}

ensure_environment() {
    mkdir_p "$BASE_DIR" 750
    mkdir_p "$LOG_DIR" 700
    mkdir_p "$STATE_DIR" 700
    mkdir_p "$RATE_LIMIT_DIR" 700
    if [ ! -f "$OUT_LOG" ]; then : >"$OUT_LOG"; fi
    if [ ! -f "$MAIN_STDOUT_LOG" ]; then : >"$MAIN_STDOUT_LOG"; fi
    if [ ! -f "$DEDUP_FILE" ]; then : >"$DEDUP_FILE"; fi
}

sanitize_field() {
    printf '%s' "$1" | sed "s/$EVENT_DELIM/ /g;s/\r/ /g;s/\n/ /g"
}

sanitize_rule_token() {
    local token
    token=$(printf '%s\n' "$1" | sed 's/[^A-Za-z0-9._-]/_/g')
    if [ -z "$token" ]; then
        token="rule"
    fi
    printf '%s\n' "$token"
}

rate_limit_allow() {
    local rule="$1" minute="$2" do_increment="${3:-1}" safe file count new_count tmp
    safe=$(sanitize_rule_token "$rule")
    file="$RATE_LIMIT_DIR/${safe}_${minute}.cnt"
    count=0
    if [ -f "$file" ]; then
        IFS= read -r count <"$file" 2>/dev/null || count=0
    fi
    case "$count" in
        ''|*[!0-9]*)
            count=0
            ;;
    esac
    RATE_LIMIT_LAST_COUNT="$count"
    if [ "$count" -ge "$RATE_LIMIT_PER_MINUTE" ]; then
        return 1
    fi
    if [ "$do_increment" -eq 1 ]; then
        new_count=$((count + 1))
        tmp="${file}.tmp.$$"
        printf '%s\n' "$new_count" >"$tmp"
        mv "$tmp" "$file" 2>/dev/null || {
            cat "$tmp" >"$file"
            rm -f "$tmp"
        }
        RATE_LIMIT_LAST_COUNT="$new_count"
    fi
    return 0
}

dedup_allow() {
    local rule="$1" message="$2" ts="$3" key tmp allowed line entry_ts entry_key keep_window
    key="${rule}|${message}"
    tmp="${DEDUP_FILE}.tmp.$$"
    : >"$tmp"
    allowed=1
    if [ -f "$DEDUP_FILE" ]; then
        while IFS= read -r line || [ -n "$line" ]; do
            [ -z "$line" ] && continue
            entry_ts=${line%%	*}
            entry_key=${line#*	}
            keep_window=$((ts - entry_ts))
            if [ "$keep_window" -lt "$DEDUP_WINDOW_MS" ] && [ "$keep_window" -ge 0 ]; then
                printf '%s\n' "$line" >>"$tmp"
                if [ "$entry_key" = "$key" ]; then
                    allowed=0
                fi
            fi
        done <"$DEDUP_FILE"
    fi
    if [ "$allowed" -eq 1 ]; then
        printf '%s\t%s\n' "$ts" "$key" >>"$tmp"
    fi
    if mv "$tmp" "$DEDUP_FILE" 2>/dev/null; then
        :
    else
        cat "$tmp" >"$DEDUP_FILE"
        rm -f "$tmp"
    fi
    [ "$allowed" -eq 1 ]
}

log_main_drop() {
    local rule="$1" minute="$2" count="$3"
    printf '%s [faultmon][info] dropped(%s, %s, %s)\n' "$(now_ms)" "$rule" "$minute" "$count" >>"$MAIN_STDOUT_LOG"
}

ensure_pipe() {
    if [ -p "$PIPE" ]; then
        return
    fi
    if [ -e "$PIPE" ] && [ ! -p "$PIPE" ]; then
        rm -f "$PIPE"
    fi
    if ! mkfifo "$PIPE" 2>/dev/null; then
        : >"$PIPE"
    fi
}

emit_event() {
    local rule="$1" message="$2" ts minute sanitized payload
    ts=$(now_ms)
    minute=$(date +%Y%m%d%H%M)
    if ! rate_limit_allow "$rule" "$minute"; then
        log_main_drop "$rule" "$minute" "$RATE_LIMIT_LAST_COUNT"
        return
    fi
    sanitized=$(sanitize_field "$message")
    if ! dedup_allow "$rule" "$sanitized" "$ts"; then
        return
    fi
    ensure_pipe
    payload="${ts}${EVENT_DELIM}watcher${EVENT_DELIM}WARN${EVENT_DELIM}threshold${EVENT_DELIM}-${EVENT_DELIM}${sanitized}${EVENT_DELIM}${rule}"
    printf '%s\n' "$payload" >>"$PIPE"
}

read_memfree_kb() {
    local value
    value=$(grep -i '^MemFree:' /proc/meminfo 2>/dev/null | head -n1 | sed 's/[^0-9]//g')
    [ -n "$value" ] || value=0
    printf '%s\n' "$value"
}

read_psi_avg10_hundred() {
    local file="$1" line value int frac frac_value int_value
    if [ ! -r "$file" ]; then
        printf '0\n'
        return
    fi
    IFS= read -r line <"$file" || line=""
    case "$line" in
        some*)
            value=${line#*avg10=}
            value=${value%% *}
            value=${value%%,*}
            int=${value%%.*}
            int=$(printf '%s' "$int" | sed 's/[^0-9]//g')
            [ -n "$int" ] || int=0
            frac=${value#*.}
            if [ "$value" = "$int" ]; then
                frac=""
            fi
            frac=$(printf '%s' "$frac" | sed 's/[^0-9].*//')
            frac=$(printf '%s' "$frac" | cut -c1-2)
            while [ "${#frac}" -lt 2 ]; do
                frac="${frac}0"
            done
            if [ -n "$frac" ]; then
                frac_value=$((10#$frac))
            else
                frac_value=0
            fi
            int_value=$((10#$int))
            printf '%s\n' $((int_value * 100 + frac_value))
            ;;
        *)
            printf '0\n'
            ;;
    esac
}

read_soc_temp() {
    local temp
    if [ -f /sys/class/thermal/thermal_zone0/temp ]; then
        IFS= read -r temp </sys/class/thermal/thermal_zone0/temp || temp=0
    else
        temp=0
    fi
    printf '%s\n' "$temp"
}

read_cpu0_freq() {
    local path="/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq" freq
    if [ -f "$path" ]; then
        IFS= read -r freq <"$path" || freq=0
    else
        freq=0
    fi
    printf '%s\n' "$freq"
}

ensure_environment
exec >>"$OUT_LOG" 2>&1
printf '%s watcher started (pid %s)\n' "$(now_ms)" "$$"

memfree_hits=0
psi_cpu_hits=0
psi_io_hits=0
cpu_low_duration=0
soc_hot_sent=0

while :; do
    memfree_kb=$(read_memfree_kb)
    memfree_kb=$((memfree_kb + 0))
    if [ "$memfree_kb" -lt 204800 ]; then
        memfree_hits=$((memfree_hits + 1))
    else
        memfree_hits=0
    fi
    if [ "$memfree_hits" -ge 2 ]; then
        emit_event "THRESH_MEMFREE_LT_200MB" "MemFree=${memfree_kb}KB"
        memfree_hits=0
    fi

    psi_cpu=$(read_psi_avg10_hundred /proc/pressure/cpu)
    psi_cpu=$((psi_cpu + 0))
    if [ "$psi_cpu" -gt 400 ]; then
        psi_cpu_hits=$((psi_cpu_hits + 1))
    else
        psi_cpu_hits=0
    fi
    if [ "$psi_cpu_hits" -ge 2 ]; then
        emit_event "THRESH_PSI_CPU_AVG10_GT_4" "psi_cpu_avg10_hundred=${psi_cpu}"
        psi_cpu_hits=0
    fi

    psi_io=$(read_psi_avg10_hundred /proc/pressure/io)
    psi_io=$((psi_io + 0))
    if [ "$psi_io" -gt 100 ]; then
        psi_io_hits=$((psi_io_hits + 1))
    else
        psi_io_hits=0
    fi
    if [ "$psi_io_hits" -ge 2 ]; then
        emit_event "THRESH_PSI_IO_AVG10_GT_1" "psi_io_avg10_hundred=${psi_io}"
        psi_io_hits=0
    fi

    soc_temp=$(read_soc_temp)
    soc_temp=$((soc_temp + 0))
    if [ "$soc_temp" -gt 70000 ]; then
        if [ "$soc_hot_sent" -eq 0 ]; then
            emit_event "THRESH_SOC_TEMP_GT_70C" "temp_mC=${soc_temp}"
            soc_hot_sent=1
        fi
    else
        soc_hot_sent=0
    fi

    cpu_freq=$(read_cpu0_freq)
    cpu_freq=$((cpu_freq + 0))
    if [ "$cpu_freq" -gt 0 ] && [ "$cpu_freq" -lt 600000 ]; then
        cpu_low_duration=$((cpu_low_duration + POLL_INTERVAL))
    else
        cpu_low_duration=0
    fi
    if [ "$cpu_low_duration" -ge 30 ]; then
        emit_event "THRESH_CPU0_FREQ_LT_600MHZ_30S" "freq_khz=${cpu_freq}"
        cpu_low_duration=0
    fi

    sleep "$POLL_INTERVAL"
done

# 验收提示：
# tail -n 20 /data/faultmon/logs/watch_natural.out
# tail -n 20 /data/faultmon/logs/faultmon.out
# 模拟低内存、PSI、温度或 CPU 限频场景验证对应 THRESH_* 事件
