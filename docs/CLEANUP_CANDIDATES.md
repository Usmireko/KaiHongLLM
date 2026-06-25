# Cleanup Candidates

Generated before deletion during workspace cleanup on 2026-06-24.

All uncertain or unique material in this list must be copied to the external
sibling backup before removal from `work`.

## Backup Then Remove

| Path | Reason |
| --- | --- |
| `.claude/` | Non-core agent/editor metadata. |
| `.tmp_qwen3/` | Temporary Qwen/server scratch state. |
| `__pycache__/` | Generated Python cache. |
| `_actions/` | Generated action output. |
| `_backup_local/` | Previous local backups. |
| `_bundles/` | Generated demo bundles. |
| `_smoke_net_batch2_20260430/` | Old smoke output. |
| `_stage2_bridge/` | Generated bridge runtime output. |
| `_stage2_tmp/` | Generated Stage2 temporary output. |
| `_tmp/` | Temporary scratch output. |
| `_tmp_export_validation/` | Temporary export validation output. |
| `_tmp_l2_route_fix_check/` | Temporary L2 repair check output. |
| `_tmp_l2_route_repair_check/` | Temporary L2 repair check output. |
| `_tmp_net_only_5a_blocked_20260518_171109/` | Old blocked experiment scratch. |
| `archive/` | Legacy snapshots replaced by external backup. |
| `artifacts/` | Generated artifacts and logs. |
| `dataset_batches/` | Generated dataset/model batches. |
| `demo_public_dataset/` | Generated dataset export. |
| `docs/legacy/` | Legacy documentation snapshots. |
| `inbox/` | Collected run data. |
| `inbox_net/` | Collected NET run data. |
| `logs/` | Runtime logs. |
| `manual_experiments/` | Generated/manual experiment outputs. |
| `net_tmp_probe/` | Probe scratch output. |
| `storage/` | Runtime state. |
| `tests/` | Empty directory. |
| `tools/` | Root compatibility wrappers and generated `tools/out`; canonical tools live under `closed_loop_demo/host/scripts/tools/`. |
| `ver1/` | Legacy pre-refactor duplicate implementation. |

## Root Compatibility Shims

These wrappers duplicate stable `scripts/*.sh` or canonical subproject paths and
are removed from the root command surface:

- `check_wifi_state.ps1`
- `demo_closed_loop_showcase.ps1`
- `ensure_wifi_connected.ps1`
- `export_run_case_dataset_fixed_v2.ps1`
- `manual_collect_net.ps1`
- `net_fault.sh`
- `qwen3_server_helpers.ps1`
- `run_wukong_collect_refactor.ps1`
- `run_wukong_weekend.ps1`
- `wk_validate_run_net.ps1`

## Harness Generated History

The following harness history is generated output and should be backed up then
removed while preserving tracked harness docs, schemas, templates, workflows, and
tools:

- untracked directories under `.codex-harness/reports/`
- untracked legacy task specs under `.codex-harness/tasks/runs/`

## Numbered Duplicate

- `closed_loop_demo/host/scripts/tools/demo_stage2_1.ps1`

