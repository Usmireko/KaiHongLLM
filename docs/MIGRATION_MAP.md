# Migration Map

Generated: 2026-06-24

This map records the workspace refactor from a mixed root layout into two
runtime lines:

- `data_train_test/`: collection, dataset build, validation, training/test/export.
- `closed_loop_demo/`: online inference, control, feedback, and demo orchestration.

Uncertain generated output, old backups, logs, datasets, and binary artifacts
were backed up outside this workspace during the cleanup pass recorded in
`docs/CLEANUP_REPORT.md`.

| Original path | New path | Business line | Runtime area | Action | Basis |
|---|---|---|---|---|---|
| `net_fault.sh` | `data_train_test/board/scripts/net_fault.sh` | Data/train/test | Board | Move; root shim removed | Board-side NET injector deployed by collector; filename must stay `net_fault.sh`. |
| `run_wukong_collect_refactor.ps1` | `data_train_test/host/scripts/run_wukong_collect_refactor.ps1` | Data/train/test | Host | Move; root shim removed | Windows/HDC collection entrypoint that uploads board injector and writes `inbox/runs`. |
| `run_wukong_weekend.ps1` | `data_train_test/host/scripts/run_wukong_weekend.ps1` | Data/train/test | Host | Move; root shim removed | Batch NET collection runner that calls collector and validator. |
| `wk_validate_run_net.ps1` | `data_train_test/host/scripts/wk_validate_run_net.ps1` | Data/train/test | Host | Move; root shim removed | NET run validator for training acceptance review. |
| `check_wifi_state.ps1` | `data_train_test/host/scripts/check_wifi_state.ps1` | Data/train/test | Host | Move; root shim removed | Host-side HDC Wi-Fi state check used around NET collection. |
| `ensure_wifi_connected.ps1` | `data_train_test/host/scripts/ensure_wifi_connected.ps1` | Data/train/test | Host | Move; root shim removed | Host-side HDC Wi-Fi recovery/preflight helper for collection. |
| `tools/hdc_target.ps1` | `data_train_test/host/scripts/tools/hdc_target.ps1` and `closed_loop_demo/host/scripts/tools/hdc_target.ps1` | Shared host helper | Host | Copy; root helper removed | PowerShell HDC target resolver used by collection and demo host scripts. |
| `build_fault_samples.py` | `data_train_test/server/src/build_fault_samples.py` | Data/train/test | Server | Move | Converts collected run folders into structured JSONL samples. |
| `build_llm_sft_dataset.py` | `data_train_test/server/src/build_llm_sft_dataset.py` | Data/train/test | Server | Move | Builds LLM SFT dataset from structured samples. |
| `batch_derive_l2_samples.py` | `data_train_test/server/src/batch_derive_l2_samples.py` | Data/train/test | Server | Move | Batch L2 derivation over dataset exports. |
| `derive_l2_case_samples.py` | `data_train_test/server/src/derive_l2_case_samples.py` | Data/train/test | Server | Move | L2 sample derivation entrypoint. |
| `dataset_models.py` | `data_train_test/server/src/dataset_models.py` | Data/train/test | Server | Move | Shared L1/L2 validation model code. |
| `validate_dataset_artifacts.py` | `data_train_test/server/src/validate_dataset_artifacts.py` | Data/train/test | Server | Move | Dataset artifact validator; imports `dataset_models`. |
| `public_dataset_to_canonical_case.py` | `data_train_test/server/src/public_dataset_to_canonical_case.py` | Data/train/test | Server | Move | Public dataset to canonical L1 adapter. |
| `export_run_case_dataset_fixed_v2.ps1` | `data_train_test/host/scripts/export_run_case_dataset_fixed_v2.ps1` | Data/train/test | Host | Move; root shim removed | Host-side dataset export from collected runs. |
| `manual_collect_net.ps1` | `data_train_test/host/scripts/manual_collect_net.ps1` | Data/train/test | Host | Move; root shim removed | Manual NET collection helper that invokes board `net_fault.sh`. |
| `closed_loop_infer_run.py` | `closed_loop_demo/server/src/closed_loop_infer_run.py` | Closed-loop demo | Server | Move | Main offline/online inference-to-actions script. |
| `infer_qwen3_fault_2stage.py` | `closed_loop_demo/server/src/infer_qwen3_fault_2stage.py` | Closed-loop demo | Server | Move | Compatibility inference adapter used by closed-loop inference. |
| `server_B/` | `closed_loop_demo/server/src/server_B/` | Closed-loop demo | Server | Move | TCP ingest/actions/watch services and orchestrator. |
| `board_A/` | `closed_loop_demo/board/src/board_A/` | Closed-loop demo | Board | Move | Board resident collector, bundler, and action executor. |
| `demo_stage2/` | `closed_loop_demo/board/src/demo_stage2/` and `closed_loop_demo/server/src/demo_stage2/` | Closed-loop demo | Board/Server | Move by subtree | Stage2 board poller/uploader and server build assets. |
| `demo_closed_loop_showcase.ps1` | `closed_loop_demo/host/scripts/demo_closed_loop_showcase.ps1` | Closed-loop demo | Host | Move; root shim removed | Host-side showcase orchestration. |
| `tools/demo_stage2.ps1` | `closed_loop_demo/host/scripts/tools/demo_stage2.ps1` | Closed-loop demo | Host | Move; root wrapper removed | Primary Stage2 host orchestration entrypoint. |
| `tools/stage2_*.ps1`, `tools/accept_stage2.ps1` | `closed_loop_demo/host/scripts/tools/` | Closed-loop demo | Host | Move; root wrappers removed | Host bridge, downlink, and acceptance helpers. |
| `tools/server_config.*`, `tools/config.local.*` | `closed_loop_demo/host/configs/` | Closed-loop demo | Host | Copy examples, ignore local secrets | Demo host/server configuration. |
| `shared/protocol/` | `shared/protocol/` | Shared | Shared | Keep | Stable protocol docs consumed by both business lines. |
| `dataset_batches/`, `demo_public_dataset/`, `inbox/`, `inbox_net/` | external cleanup backup | Data/train/test | Artifact | Backed up and removed | Dataset/run material should not be treated as source. |
| `logs/`, `storage/`, `_tmp*/`, `_stage2_tmp/`, `_stage2_bridge/`, `tools/out/` | external cleanup backup | Mixed | Artifact | Backed up and removed | Runtime state, logs, temporary outputs, and generated reports. |
| `*.bak*`, `*_tmp*`, old duplicate scripts | external cleanup backup | Mixed | Archive | Backed up and removed | Existing backups or uncertain versions were preserved outside `work`. |
