#!/bin/sh
PIPE=/data/faultmon/state/event.pipe
[ -p "$PIPE" ] || { echo "ERR: $PIPE not fifo"; exit 1; }
ts="$(date +%s)000"
(
 printf "%s\001%s\001%s\001%s\001%s\001%s\001%s\n" \
 "$ts" "watcher" "WARN" "threshold" "-" "SELFTEST" "SELFTEST_RULE" \
 > "$PIPE"
) &
echo "SENT ts=$ts"
