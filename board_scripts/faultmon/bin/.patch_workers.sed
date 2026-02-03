/^start_background_workers() {/,/^}/c\
start_background_workers() {\
  spawn event_sink_loop "$STDOUT_LOG" "$ERR_LOG";\
  EVENT_SINK_PID=$(cat "$STATE_DIR/pid.event_sink_loop" 2>/dev/null);\
  spawn hilog_watcher "$STDOUT_LOG" "$ERR_LOG";\
  HILOG_PID=$(cat "$STATE_DIR/pid.hilog_watcher" 2>/dev/null);\
  spawn dmesg_watcher "$STDOUT_LOG" "$ERR_LOG";\
  DMESG_PID=$(cat "$STATE_DIR/pid.dmesg_watcher" 2>/dev/null);\
  spawn metrics_loop "$STDOUT_LOG" "$ERR_LOG";\
  METRIC_PID=$(cat "$STATE_DIR/pid.metrics_loop" 2>/dev/null);\
  spawn stats_loop "$STDOUT_LOG" "$ERR_LOG";\
  STATS_PID=$(cat "$STATE_DIR/pid.stats_loop" 2>/dev/null);\
  spawn heartbeat_loop "$STDOUT_LOG" "$ERR_LOG";\
  HEART_PID=$(cat "$STATE_DIR/pid.heartbeat_loop" 2>/dev/null);\
  spawn faultlogger_monitor "$STDOUT_LOG" "$ERR_LOG";\
  FAULT_PID=$(cat "$STATE_DIR/pid.faultlogger_monitor" 2>/dev/null);\
  spawn retention_loop "$STDOUT_LOG" "$ERR_LOG";\
  RET_PID=$(cat "$STATE_DIR/pid.retention_loop" 2>/dev/null);\
}
