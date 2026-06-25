---
name: stage2_board_pid_evidence_capture
description: >
  Ensure board-side bundle contains per-pid evidence for suspect scoring:
  - candidate_pids.txt (inject pids + top RSS pids)
  - pidstat_0.txt / pidstat_1.txt OR status_0/status_1 snapshots
  Must be BusyBox/Toybox compatible (no awk/tr/jq/seq/mktemp).
---

# Candidate PID set
1) Injection pidfiles:
   - /data/faultmon/demo_stage2/pids/inject_cpu.pid
   - /data/faultmon/demo_stage2/pids/inject_mem.pid (may contain multiple pids, one per line)
2) Top RSS:
   - scan /proc/[0-9]*/status, parse VmRSS
   - sort -nr, take top N (N=20)

# Output in run_dir/procs
- candidate_pids.txt
- pidstat_0.txt
- pidstat_1.txt
(or status snapshots if pidstat unavailable)

# Constraints
- Use sh + grep/cut/sed/sort/head/printf/while read
- Avoid long one-liners; keep robust quoting.

# Acceptance
- candidate_pids.txt non-empty
- pidstat_0/1 exist and include at least injection pids
- Server-side suspects cpu/score no longer all NA for those pids
