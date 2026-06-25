---
name: demo_board_bundle_manual
description: >
  Implement board_A/bin/bundle.sh for manual trigger packing into /data/faultmon/outbox.
  IMPORTANT: board side forbids awk/tr/jq/seq/mktemp.
  Connection actions (deploy/run) must use skill: hdc-kaihongos.
---

# Inputs
- manual <tag> [--pre N] [--post N]

# Behavior
- t0_ms = now (epoch ms)
- window = [t0-pre, t0+post]
- Slice:
  - /data/faultmon/metrics/sys_*.csv (keep header, filter rows by ts_ms)
  - /data/faultmon/events/events_*.jsonl (filter by ts)
- procs snapshot: ps -o pid,ppid,stat,rss,comm
- Generate:
  - bundle_manifest.json (recommended)
  - _run_meta.json (minimal fields)
- Output bundle:
  - /data/faultmon/outbox/bundle_<...>.tar.gz

# Constraints
- No awk/tr/jq/seq/mktemp
- Use sh + grep/sed/cut/head/tail/wc/ps/date/tar/gzip (if exists)

# Acceptance (manual)
- Run on device:
  - /data/faultmon/demo/bin/bundle.sh manual demo_manual --pre 10 --post 10
- outbox has new bundle_*.tar.gz
- tar contains run_dir layout + _run_meta.json
