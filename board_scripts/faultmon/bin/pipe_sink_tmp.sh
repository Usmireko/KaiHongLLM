#!/bin/sh
PIPE=/data/faultmon/state/event.pipe
LOG=/data/faultmon/logs/pipe_sink_tmp.out
mkdir -p /data/faultmon/events /data/faultmon/logs
[ -p "$PIPE" ] || { echo "E: $PIPE not fifo" >>"$LOG"; exit 1; }
exec 3<> "$PIPE"
esc() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }

DELIM=$(printf '\001')

while :
do
  if read -r line <&3; then
    [ -z "$line" ] && continue
rest=$line

    case "$rest" in *"$DELIM"*) ts=${rest%%"$DELIM"*}; rest=${rest#*"$DELIM"};; *) continue;; esac
    case "$rest" in *"$DELIM"*) src=${rest%%"$DELIM"*}; rest=${rest#*"$DELIM"};; *) continue;; esac
    case "$rest" in *"$DELIM"*) lvl=${rest%%"$DELIM"*}; rest=${rest#*"$DELIM"};; *) continue;; esac
    case "$rest" in *"$DELIM"*) tag=${rest%%"$DELIM"*}; rest=${rest#*"$DELIM"};; *) continue;; esac
    case "$rest" in *"$DELIM"*) pid=${rest%%"$DELIM"*}; rest=${rest#*"$DELIM"};; *) continue;; esac
    case "$rest" in *"$DELIM"*) msg=${rest%%"$DELIM"*}; rule=${rest#*"$DELIM"};; *) continue;; esac

    ymd=$(date +%Y%m%d)
    out="/data/faultmon/events/events_${ymd}.jsonl"

    printf '{"ts":"%s","source":"%s","level":"%s","tag":"%s","pid":"%s","message":"%s","rule":"%s"}\n' \
      "$ts" "$(esc "$src")" "$(esc "$lvl")" "$(esc "$tag")" "$(esc "$pid")" "$(esc "$msg")" "$(esc "$rule")" >> "$out"
  else
sleep 1
fi
done
