# Cleanup Report

Date: 2026-06-24

## Summary

The workspace root was cleaned to keep only the refactored core project,
project metadata, docs, configs, shared protocol notes, and active agent/harness
tooling.

Final root entries:

```text
.agents/
.codex/
.codex-harness/
.git/
.gitignore
AGENTS.md
README.md
closed_loop_demo/
configs/
data_train_test/
docs/
faults/
scripts/
shared/
```

## Backup

Cleanup candidates were copied before removal to:

```text
C:\Users\kaihong-xuerx\Desktop\work_cleanup_backup_20260624_101022
```

Backup manifest:

```text
C:\Users\kaihong-xuerx\Desktop\work_cleanup_backup_20260624_101022\BACKUP_MANIFEST.md
```

Backup summary:

- Removed candidate entries: 570
- Backed up files: 188,598
- Backed up bytes: 9,870,351,635

## Removed From Workspace

Major removed groups:

- root compatibility wrappers: `net_fault.sh`, `run_wukong_collect_refactor.ps1`,
  `run_wukong_weekend.ps1`, `wk_validate_run_net.ps1`, Wi-Fi helpers, export/manual
  helpers, `demo_closed_loop_showcase.ps1`, and `qwen3_server_helpers.ps1`
- root `tools/` wrappers and generated `tools/out/`
- `ver1/` legacy implementation
- `archive/`, `artifacts/`, `docs/legacy/`
- collected/generated data: `inbox/`, `inbox_net/`, `dataset_batches/`,
  `demo_public_dataset/`
- runtime/log/scratch outputs: `logs/`, `storage/`, `_tmp*/`, `_stage2_*`,
  `_smoke_*`, `net_tmp_probe/`, `manual_experiments/`
- generated harness history under `.codex-harness/reports/` and untracked legacy
  task specs under `.codex-harness/tasks/runs/`
- numbered duplicate `closed_loop_demo/host/scripts/tools/demo_stage2_1.ps1`

Tracked harness baseline reports and tracked harness task specs were preserved.
`.codex-harness/tools/run_subagent_workflow.py` was preserved and not edited by
this cleanup.

## Code And Doc Changes

- Removed stale `ver1/` fallback paths from
  `closed_loop_demo/host/scripts/tools/demo_stage2.ps1`.
- Added `docs/CORE_KEEP_MANIFEST.md`.
- Added `docs/CLEANUP_CANDIDATES.md`.
- Updated `README.md`, `docs/MIGRATION_MAP.md`,
  `docs/REFACTOR_REPORT.md`, and `docs/DATA_TRAIN_TEST_RUNBOOK.md` so they no
  longer advertise root compatibility shims.
- Updated `.gitignore` to ignore recreated `archive/` and `artifacts/` runtime
  roots.

## Validation

Passed:

- PowerShell parse: `PS_PARSE_OK files=27`
- Python AST parse with BOM-aware decoding: `PY_PARSE_OK files=67`
- Demo preflight: `bash scripts/demo_preflight.sh`
- Demo dry-run: `bash scripts/start_demo.sh --dry-run`
- Stale path scan outside migration/candidate docs: no matches for old root
  shims, `tools/out`, `docs/legacy`, `archive/legacy_20260624`, `ver1`,
  `demo_stage2_1`, or absolute local workspace paths.

Failed:

- Shell syntax validation:

```text
bash -n closed_loop_demo/board/src/demo_stage2/board_A/bin/faultwatchd.sh
closed_loop_demo/board/src/demo_stage2/board_A/bin/faultwatchd.sh: line 55: syntax error: unexpected end of file
```

The file currently has 54 lines and ends after `done`. This needs a targeted
follow-up inspection before the shell validation can be marked PASS.

Not completed after the shell validation stop:

- Individual Python CLI `--help` checks.

## Git Status Notes

The cleanup intentionally removed root compatibility wrappers. Git reports the
original tracked root scripts as moved into `data_train_test/...` from the
previous refactor. The pre-existing modified
`.codex-harness/tools/run_subagent_workflow.py` remains modified and was not
changed by this cleanup.

## Remaining Risks

- Shell syntax validation is not fully green because of
  `closed_loop_demo/board/src/demo_stage2/board_A/bin/faultwatchd.sh`.
- Hardware/HDC, server SSH, model loading, and live demo execution were not run
  in this cleanup pass.
- The backup is external to the repo and should be retained until the cleaned
  workspace has been accepted.

## Recursion Depth

Reached repair depth 3 during validation command repair and shell syntax
validation. Stopped per project policy after the remaining shell syntax failure.

## Follow-Up Validation Update

Date: 2026-06-24

### faultwatchd.sh Root Cause

`closed_loop_demo/board/src/demo_stage2/board_A/bin/faultwatchd.sh` visually
ended with the expected `done`, but byte inspection showed the final line used
CRLF while the rest of the file used LF. Bash parsed the loop terminator as
`done\r`, so the `while true; do` block remained open and the parser reported:

```text
line 55: syntax error: unexpected end of file
```

### Fix

Normalized only `faultwatchd.sh` from mixed line endings to LF. The file changed
by one byte:

- before: `LF=54`, `CRLF=1`, `bytes=1194`
- after: `LF=54`, `CRLF=0`, `bytes=1193`

No script logic, device paths, arguments, protocol fields, or control flow were
changed.

### Validation Results

Passed:

- Target shell parse:
  `bash -n closed_loop_demo/board/src/demo_stage2/board_A/bin/faultwatchd.sh`
- Same-directory shell parse: all `*.sh` files under
  `closed_loop_demo/board/src/demo_stage2/board_A/bin/`
- Core shell parse: `SH_PARSE_OK files=37`
- PowerShell parse: `PS_PARSE_OK files=27`
- Python AST parse with BOM-aware decoding: `PY_PARSE_OK files=67`
- Python CLI `--help` checks:
  - `python data_train_test/server/src/validate_dataset_artifacts.py --help`
  - `python data_train_test/server/src/derive_l2_case_samples.py --help`
  - `python closed_loop_demo/server/src/server_B/tcp/tcp_ingest_server.py --help`
  - `python closed_loop_demo/server/src/server_B/tcp/tcp_actions_server.py --help`
  - `python closed_loop_demo/server/src/server_B/tcp/watch_and_infer.py --help`
  - `python closed_loop_demo/server/src/server_B/orchestrator/run_closed_loop.py --help`
- Entrypoint checks:
  - all `scripts/*.sh` entrypoints exist and have `#!/bin/sh`
  - referenced canonical target paths exist
  - safe help/status checks passed for `run_preprocess.sh`, `run_train.sh`,
    `run_test.sh`, `demo_status.sh`, and `stop_demo.sh`
- Demo preflight: `bash scripts/demo_preflight.sh`, exit `0`
- Demo dry-run: `bash scripts/start_demo.sh --dry-run`, exit `0`

Reference scan:

- No root tree restoration occurred; root entries remain limited to the cleaned
  core workspace.
- No matches for `ver1`, `tools/out`, `docs/legacy`,
  `archive/legacy_20260624`, or `demo_stage2_1` outside allowed historical docs.
- Remaining `board_A`, `server_B`, `demo_stage2`, `inbox`, `dataset_batches`,
  `tools`, and `artifacts` references are nested canonical paths, skill/template
  text, dataset tooling defaults, or generated-output configuration examples;
  no deleted root directory was restored.

Not run:

- Real board/HDC operations.
- SSH server operations.
- Live server service start/stop.
- Model loading or end-to-end hardware demo.

Git risk:

- `.codex-harness/tools/run_subagent_workflow.py` remains modified from before
  cleanup and was not touched by this follow-up.
- The workspace still contains the previous refactor/cleanup changes and
  untracked core files; no commit was created.

