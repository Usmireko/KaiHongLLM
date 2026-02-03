# Bundle Format (Resident Manual Demo)

This document defines the minimal bundle layout for the resident-mode manual demo.
The layout aligns with run_wukong_collect_refactor.ps1 expectations where possible.

## Bundle filename

- bundle_<run_id>.tar.gz

## Bundle contents

The tarball should contain a single run directory:

<run_id>/
  _run_meta.json
  bundle_manifest.json
  metrics/
    sys_*.csv
  events/
    events_*.jsonl
  procs/
    procs_*.txt

Optional files can be included (e.g., hilog_text_full.log), but are not required
for the manual demo ingest.

## bundle_manifest.json (recommended)

Minimal fields:
- run_id: string
- scenario_tag: string
- fault_type: string
- created_at_ms: integer (epoch ms)
- window_start_ms: integer (epoch ms)
- window_end_ms: integer (epoch ms)

## _run_meta.json (minimal fields)

Minimal fields (aligned with run_wukong_collect_refactor.ps1 naming):
- run_id: string
- scenario_tag: string
- fault_type: string
- run_start: integer (epoch ms)
- run_end: integer (epoch ms)
- run_window_host_epoch_ms_start: integer (epoch ms)
- run_window_host_epoch_ms_end: integer (epoch ms)
- run_window_board_ms_start: integer (epoch ms)
- run_window_board_ms_end: integer (epoch ms)
- run_window_source: string (e.g., manual)

Server ingest will patch missing fields and set run_dir to the server path.

## actions_device.txt

The server generates actions_device.txt from actions.json:
- One command per line
- No JSON parsing on device
- Empty lines and lines starting with '#' are ignored by actiond.sh
