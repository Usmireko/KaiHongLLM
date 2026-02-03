#!/bin/sh
set -x
. /data/faultmon/bin/faultmon.sh    # 不要加 "lib"，要真正把函数定义出来
[ -p /data/faultmon/state/event.pipe ] || mkfifo -m 0644 /data/faultmon/state/event.pipe
echo "$(date +%s000) [faultmon][info] event_sink_rdwr bootstrap pid=$$" >>/data/faultmon/logs/event_sink.log
event_sink_rdwr
