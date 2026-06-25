---
name: codex-native-subagent-mainline
description: >
  Use this skill when the user asks Codex to run a harnessed native subagent workflow,
  coordinate explorer/implementer/reviewer-style tasks, use the Codex Native Subagent
  Harness mainline flow, or execute a workflow through .codex-harness. This skill
  prepares the workflow, guides the parent Codex runtime to launch native subagents,
  finalizes real call IDs, captures results through the JSONL authoritative backend,
  and runs strict aggregation. It must not claim that Python can directly spawn native
  subagents.
---

# Codex Native Subagent Mainline Workflow

Use this skill to run the accepted Codex Native Subagent Harness workflow.

This is a **mainline execution skill**. It should be used when the user asks for any of the following:

- run a harnessed Codex native subagent workflow
- use the accepted `.codex-harness` mainline flow
- coordinate `project-explorer`, `project-implementer`, `project-reviewer`, or similar subagents
- run an explorer/implementer/reviewer, producer/reviewer, planner/generator/evaluator, or fan-out/fan-in workflow
- capture native Codex subagent outputs and aggregate them strictly

## Hard boundaries

These are non-negotiable.

- Python does **not** directly call native `spawn_agent`.
- Parent Codex native runtime launches subagents.
- The Python harness prepares, finalizes, captures, aggregates, and reports.
- JSONL is the default authoritative capture backend.
- App Server is candidate/shadow only.
- `dual` mode is experimental comparison only.
- Whole-parent-session inventory is not accepted as strict PASS.
- Do not use `--allow-partial` for acceptance.
- Do not claim `.codex/agents/*.toml` role resolution is confirmed.
- Do not modify business files unless the user explicitly asks and the workflow scope allows it.
- Do not run `git add`, `git commit`, or `git push` unless the user explicitly asks.
- Do not make App Server default or authoritative.
- Do not claim JSONL has been replaced.

## Accepted mainline flow

The accepted flow is:

```text
prepare-only
→ parent Codex native subagent calls
→ collect real spawn_agent call IDs
→ finalize-manifest
→ capture-only --capture-backend jsonl
→ aggregate-only
→ status
```

Short form:

```text
Parent Codex launches native subagents.
Python harness verifies, captures, and aggregates.
```

## Default workflow pattern

Use this pattern unless the user specifies another one:

```text
explorer-implementer-reviewer
```

Supported patterns:

```text
explorer-implementer-reviewer
producer-reviewer
planner-generator-evaluator
fan-out-fan-in
```

## Step 0: Choose a run directory

Use a timestamped run id when possible:

```text
<YYYYMMDD_HHMMSS>_<short_task_name>
```

Example:

```text
.codex-harness/reports/20260511_103000_harness_review
```

Use this placeholder in commands:

```text
<run_id>
```

## Step 1: Prepare the workflow

Run:

```powershell
python .codex-harness/tools/run_subagent_workflow.py `
  --mode codex-native-subagent `
  --workflow-pattern explorer-implementer-reviewer `
  --prepare-only `
  --run-dir .codex-harness/reports/<run_id>
```

If the user requested a different supported pattern, replace `explorer-implementer-reviewer`.

After prepare, read:

```text
.codex-harness/reports/<run_id>/next_action_for_parent_codex.md
.codex-harness/reports/<run_id>/prompt_bundle.md
.codex-harness/reports/<run_id>/expected_workflow.template.json
```

The prepare step must not run capture or aggregation.

## Step 2: Launch native subagents as parent Codex

As the parent Codex session, use native subagent calls according to:

```text
.codex-harness/reports/<run_id>/next_action_for_parent_codex.md
```

Do not use Python to launch native subagents.

For the default pattern, launch:

```text
project-explorer
project-implementer
project-reviewer
```

For `producer-reviewer`, launch:

```text
producer
reviewer
```

For `planner-generator-evaluator`, launch:

```text
planner
generator
evaluator
```

For `fan-out-fan-in`, launch:

```text
producer[0]
producer[1]
reviewer
```

Each subagent result should start with exactly one top-level result line:

```text
RESULT: PASS
```

or one of:

```text
RESULT: NEEDS_FIX
RESULT: FAIL
RESULT: CAPTURE_FAILED
```

Do not use top-level `RESULT: WARN`. Put non-blocking warnings under:

```text
## Non-Blocking Warnings
```

## Step 3: Collect real call IDs

After native subagents complete, collect real parent-side `spawn_agent` call IDs for every required role.

Default pattern example:

```text
project-explorer=call_xxx
project-implementer=call_yyy
project-reviewer=call_zzz
```

Fan-out/fan-in example:

```text
producer[0]=call_xxx
producer[1]=call_yyy
reviewer=call_zzz
```

Do not invent call IDs. If call IDs cannot be identified, stop and report `NEEDS_FIX`.

## Step 4: Finalize the manifest

Default pattern:

```powershell
python .codex-harness/tools/run_subagent_workflow.py `
  --mode codex-native-subagent `
  --finalize-manifest `
  --run-dir .codex-harness/reports/<run_id> `
  --call-id project-explorer=call_xxx `
  --call-id project-implementer=call_yyy `
  --call-id project-reviewer=call_zzz
```

Fan-out/fan-in example:

```powershell
python .codex-harness/tools/run_subagent_workflow.py `
  --mode codex-native-subagent `
  --finalize-manifest `
  --run-dir .codex-harness/reports/<run_id> `
  --call-id "producer[0]=call_xxx" `
  --call-id "producer[1]=call_yyy" `
  --call-id reviewer=call_zzz
```

The finalized manifest must exist:

```text
.codex-harness/reports/<run_id>/expected_workflow.json
```

If finalize-manifest fails, do not continue to capture.

## Step 5: Capture using JSONL authoritative backend

Run:

```powershell
python .codex-harness/tools/run_subagent_workflow.py `
  --mode codex-native-subagent `
  --capture-only `
  --capture-backend jsonl `
  --run-dir .codex-harness/reports/<run_id>
```

Acceptance requirements:

```text
adapter_exit_code = 0
allow_partial = false
capture_status = complete
workflow_validation_scope = expected_call_ids
unresolved_link_call_ids = []
result files written for all required roles
no required role missing
```

If any requirement fails, do not claim PASS.

## Step 6: Strict aggregate

Run:

```powershell
python .codex-harness/tools/run_subagent_workflow.py `
  --mode codex-native-subagent `
  --aggregate-only `
  --run-dir .codex-harness/reports/<run_id>
```

Strict aggregation must fail if:

- any expected result file is missing
- any result has top-level `RESULT: FAIL`
- any result has top-level `RESULT: NEEDS_FIX`
- any result has top-level `RESULT: CAPTURE_FAILED`
- reviewer/evaluator/gate role top-level verdict is not `RESULT: PASS`
- capture status is not complete
- unresolved links exist
- `allow_partial` is true or missing
- `workflow_validation_scope` is not `expected_call_ids`

## Step 7: Status

Run:

```powershell
python .codex-harness/tools/run_subagent_workflow.py `
  --mode codex-native-subagent `
  --status `
  --run-dir .codex-harness/reports/<run_id>
```

Only claim success if status reports:

```text
AGGREGATION_PASS
```

If status reports `CAPTURE_FAILED`, `AGGREGATION_FAIL`, `NEEDS_FIX`, or any partial/incomplete state, report the failure and stop.

## Optional dual backend

Only use dual backend when the user explicitly asks for App Server candidate comparison.

```powershell
python .codex-harness/tools/run_subagent_workflow.py `
  --mode codex-native-subagent `
  --capture-only `
  --capture-backend dual `
  --run-dir .codex-harness/reports/<run_id>
```

In dual mode:

- JSONL remains authoritative.
- App Server remains candidate/shadow only.
- Do not claim JSONL replacement.
- Do not make App Server default.
- Candidate mismatch must be reported.

## Backend policy validation

When changing harness behavior or before committing, run:

```powershell
python .codex-harness/tools/run_subagent_workflow.py `
  --mode codex-native-subagent `
  --validate-backend-policy `
  --run-dir .codex-harness/reports/v14_backend_policy_probe
```

Do not proceed if backend policy validation fails.

## Output format to the user

When done, summarize:

```text
RESULT: <AGGREGATION_PASS | CAPTURE_FAILED | AGGREGATION_FAIL | NEEDS_FIX>

Run dir:
.codex-harness/reports/<run_id>

Workflow pattern:
<pattern>

Backend:
jsonl authoritative

Result files:
- ...

Aggregation:
...

Known warnings:
...
```

Never claim that Python directly spawned native subagents.

## Stop conditions

Stop and report `NEEDS_FIX` if:

- concrete call IDs cannot be collected
- `expected_workflow.json` cannot be finalized
- capture uses or requires `--allow-partial`
- `capture_status != complete`
- `workflow_validation_scope != expected_call_ids`
- `unresolved_link_call_ids` is non-empty
- any required result file is missing
- reviewer/evaluator/gate role does not return top-level `RESULT: PASS`
- App Server is required as authoritative
- business files would be modified outside the requested scope

## Commit behavior

Do not commit unless the user explicitly asks.

If the user asks to commit harness changes, follow the repo's commit boundary report and do not stage unrelated business paths. Never use `git add .` unless the user explicitly instructs it and the commit boundary has been reviewed.
