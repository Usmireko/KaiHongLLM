# Codex Native Subagent Harness

The Codex Native Subagent Harness records the accepted prepare, capture,
aggregation, backend policy, and workflow-pattern contracts for parent-launched
Codex native subagent work.

V16 is a documentation and commit-boundary milestone. It does not add runner
behavior, does not relax strict aggregation, and does not change the accepted
backend authority.

## Current Baseline

- JSONL session-log capture is the default and authoritative backend.
- Python does not directly call native `spawn_agent`.
- The parent Codex native runtime launches subagents.
- Python harness tools prepare manifests, capture completed native calls,
  aggregate strict result bundles, report status, and run diagnostics.
- App Server support is candidate/shadow only.
- `dual` backend mode is experimental comparison only; JSONL remains
  authoritative.
- Whole-parent-session inventory is not accepted as strict PASS. Strict
  acceptance requires expected-call-id scope, or a deliberately scoped source
  run where accepted by that probe.
- `.codex/agents/*.toml` role resolution is not claimed.

## Accepted Link Modes

Strict deterministic parent-child link evidence may use:

- `collab_agent_spawn_end`
- `function_call_output_agent_id`
- `app_server_collab_agent_tool_call`
- `app_server_function_call_output_agent_id`

App Server link modes remain candidate/shadow evidence unless explicitly
compared against the JSONL authoritative run.

## Main Runner

`run_subagent_workflow.py` supports `--mode codex-native-subagent` for:

- `--prepare-only`: write prompt bundle and expected workflow template, then
  stop for parent Codex native subagent calls.
- `--finalize-manifest`: map concrete call IDs into
  `expected_workflow.json`.
- `--capture-only`: capture completed native subagent calls using the selected
  capture backend. The default is `--capture-backend jsonl`.
- `--aggregate-only`: run strict aggregation. JSONL remains authoritative by
  default.
- `--status`: report preparation, capture, aggregation, backend, and policy
  state.
- `--full`: prepare-and-stop only. It does not spawn subagents.

The runner must not imply that Python can spawn native subagents.

## Workflow Patterns

Accepted patterns are defined under `.codex-harness/workflows/`:

- `explorer-implementer-reviewer`
- `producer-reviewer`
- `planner-generator-evaluator`
- `fan-out-fan-in`

Each pattern defines role order or dependencies, required roles, result-file
mapping, gate role, prompt defaults, and strict pass rules.

## Backend Policy

The backend policy lives in `.codex-harness/backend_policy.json`.

Key policy values:

- `default_capture_backend = jsonl`
- `authoritative_backend = jsonl`
- `candidate_backends = ["app-server-candidate"]`
- `experimental_backends = ["dual"]`
- `jsonl_replacement_claimed = false`
- `app_server_default_allowed = false`
- `app_server_authoritative_allowed = false`

## Documentation

- `docs/accepted_baseline.md`: V0 through V15 accepted status chain.
- `docs/backend_policy.md`: backend authority and negative policy.
- `docs/workflow_patterns.md`: pattern usage and strict pass rules.
- `docs/mainline_usage.md`: prepare, finalize, capture, aggregate, and status
  workflow.
- `docs/non_capabilities.md`: explicit non-capabilities and safety boundaries.
- `docs/smoke_tests.md`: minimal regression command list.

## Reports

The compact V16 baseline is:

```text
.codex-harness/reports/V16_MAINLINE_READINESS_BASELINE.md
```

The V16 probe directory contains:

```text
.codex-harness/reports/v16_mainline_readiness_probe/
```

Bulky generated probe runs from V0 through V15 are evidence artifacts, not
default commit candidates. See the V16 commit-boundary report before staging.
