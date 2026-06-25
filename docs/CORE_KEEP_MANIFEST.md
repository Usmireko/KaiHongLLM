# Core Keep Manifest

Generated during workspace cleanup on 2026-06-24.

## Keep Policy

The cleaned workspace keeps source, configuration, validation scripts, runbooks,
contracts, and project automation required to operate the refactored core:

- `data_train_test/` for board injection, host collection, dataset validation,
  preprocessing, export, L1/L2 derivation, and testing.
- `closed_loop_demo/` for Stage2 board resident scripts, server ingest/infer/action
  services, and host orchestration.
- `scripts/` for stable top-level entrypoints.
- `configs/` and `shared/` for non-secret examples and protocol contracts.
- `docs/` for current architecture, runbooks, migration map, cleanup report, and
  schema notes.
- `faults/` for fault-scoped policy such as NET evidence rules.
- `.agents/`, `.codex/`, and `.codex-harness/` for active project skills and
  subagent tooling. Generated reports and task-output history under the harness
  are cleanup candidates, but `.codex-harness/tools/run_subagent_workflow.py` is
  explicitly protected.
- `.git/`, `.gitignore`, `AGENTS.md`, and `README.md` as repository metadata and
  root navigation.

## Kept Paths

| Path | Reason |
| --- | --- |
| `AGENTS.md` | Global project rules and skill registry. |
| `README.md` | Clean workspace entrypoint map. |
| `.gitignore` | Runtime output and backup ignore policy. |
| `.agents/skills/os-fault-engineer/` | Repo-specific implementation workflow required by `AGENTS.md`. |
| `.codex/skills/` | Local operational skills referenced by `AGENTS.md`. |
| `.codex-harness/README.md` | Harness usage documentation. |
| `.codex-harness/docs/` | Harness documentation. |
| `.codex-harness/schemas/` | Harness task schema. |
| `.codex-harness/templates/` | Harness task templates. |
| `.codex-harness/tools/` | Harness tooling; `run_subagent_workflow.py` is protected. |
| `.codex-harness/workflows/` | Harness workflow definitions. |
| `configs/examples/` | Non-secret deployment/config examples. |
| `data_train_test/` | Canonical data/train/test project line. |
| `closed_loop_demo/` | Canonical closed-loop demo project line. |
| `docs/ARCHITECTURE.md` | Current architecture map. |
| `docs/CLOSED_LOOP_DEMO_RUNBOOK.md` | Current closed-loop demo runbook. |
| `docs/DATA_TRAIN_TEST_RUNBOOK.md` | Current data/train/test runbook. |
| `docs/DEPLOYMENT_MATRIX.md` | Board/host/server deployment matrix. |
| `docs/MIGRATION_MAP.md` | Refactor migration map. |
| `docs/REFACTOR_REPORT.md` | Refactor validation summary. |
| `docs/dataset_schema_spec.md` | Dataset schema reference. |
| `faults/` | Fault-scoped rules. |
| `scripts/` | Stable root command surface. |
| `shared/` | Shared protocol documents. |

