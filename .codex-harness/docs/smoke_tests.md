# Smoke Tests

Run from the repository root.

## Compile Key Tools

```powershell
python -m py_compile `
  .codex-harness/tools/run_subagent_workflow.py `
  .codex-harness/tools/aggregate_subagent_results.py `
  .codex-harness/tools/codex_native_subagent_adapter.py `
  .codex-harness/tools/codex_app_server_shadow_adapter.py `
  .codex-harness/tools/benchmark_parallelism.py
```

## Runner Help

```powershell
python .codex-harness/tools/run_subagent_workflow.py --help
```

## Backend Policy Validation

```powershell
python .codex-harness/tools/run_subagent_workflow.py `
  --mode codex-native-subagent `
  --validate-backend-policy `
  --run-dir .codex-harness/reports/v14_backend_policy_probe
```

## V2.5 Status Regression

```powershell
python .codex-harness/tools/run_subagent_workflow.py `
  --mode codex-native-subagent `
  --status `
  --run-dir .codex-harness/reports/v2_5_positive_source_probe `
  --aggregation-dir .codex-harness/reports/v2_5_strict_aggregation_probe
```

Expected state: `AGGREGATION_PASS`.

## V8 Replay Regression

```powershell
python .codex-harness/tools/codex_app_server_shadow_adapter.py `
  --app-server-events .codex-harness/reports/v7_app_server_live_equivalence_probe/app_server_events.jsonl `
  --jsonl-trace .codex-harness/reports/v7_app_server_live_equivalence_probe/jsonl_parallel_trace.json `
  --expected-workflow .codex-harness/reports/v7_app_server_live_equivalence_probe/expected_workflow.json `
  --out-dir .codex-harness/reports/v16_mainline_readiness_probe/v8_replay_regression
```

Expected classification: `SHADOW_EQUIVALENT`.

## V13 Report Check

```powershell
Select-String -Path .codex-harness/reports/v13_fan_out_fan_in_parity_probe/v13_acceptance_report.md -Pattern "RESULT: V13_PASS"
```

## Optional V15 Benchmark Replay

This is not a correctness gate and may take about a minute because it launches
isolated App Server width probes.

```powershell
python .codex-harness/tools/benchmark_parallelism.py `
  --out-dir .codex-harness/reports/v15_parallelism_benchmark_probe `
  --live-timeout-seconds 300
```
