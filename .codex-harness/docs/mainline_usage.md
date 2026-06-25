# Mainline Usage

This workflow assumes the parent Codex native runtime will launch subagents.
Python harness tools do not directly call native `spawn_agent`.

## 1. Prepare

```powershell
python .codex-harness/tools/run_subagent_workflow.py `
  --mode codex-native-subagent `
  --prepare-only `
  --workflow-pattern explorer-implementer-reviewer `
  --run-dir .codex-harness/reports/<run_name>
```

Prepare-only writes:

- `prompt_bundle.md`
- `expected_workflow.template.json`
- `next_action_for_parent_codex.md`

The next action is for parent Codex native runtime to launch the requested
subagents.

## 2. Fill Call IDs

After parent Codex completes native subagent calls, finalize the manifest:

```powershell
python .codex-harness/tools/run_subagent_workflow.py `
  --mode codex-native-subagent `
  --finalize-manifest `
  --run-dir .codex-harness/reports/<run_name> `
  --call-id project-explorer=call_xxx `
  --call-id project-implementer=call_yyy `
  --call-id project-reviewer=call_zzz
```

For fan-out/fan-in, use indexed IDs:

```powershell
--call-id producer[0]=call_xxx --call-id producer[1]=call_yyy --call-id reviewer=call_zzz
```

## 3. Capture

JSONL is the default authoritative backend:

```powershell
python .codex-harness/tools/run_subagent_workflow.py `
  --mode codex-native-subagent `
  --capture-only `
  --capture-backend jsonl `
  --run-dir .codex-harness/reports/<run_name>
```

Do not use `--allow-partial` for strict accepted runs.

## 4. Aggregate

```powershell
python .codex-harness/tools/run_subagent_workflow.py `
  --mode codex-native-subagent `
  --aggregate-only `
  --run-dir .codex-harness/reports/<run_name> `
  --aggregation-dir .codex-harness/reports/<run_name>_aggregation
```

Strict aggregation remains JSONL authoritative unless a future accepted change
explicitly changes backend policy.

## 5. Status

```powershell
python .codex-harness/tools/run_subagent_workflow.py `
  --mode codex-native-subagent `
  --status `
  --run-dir .codex-harness/reports/<run_name> `
  --aggregation-dir .codex-harness/reports/<run_name>_aggregation
```

Status reports selected backend, default backend, authoritative backend,
candidate backend state, equivalence state, capture state, and aggregation
state.

## Candidate and Dual Modes

`--capture-backend app-server-candidate` and `--capture-backend dual` are
opt-in and experimental. They must not make App Server authoritative. In dual
mode, JSONL remains authoritative and candidate mismatches must be reported.
