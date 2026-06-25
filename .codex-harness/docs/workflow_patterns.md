# Workflow Patterns

Workflow pattern definitions live under `.codex-harness/workflows/`. Runner
prepare-only mode can generate pattern-specific prompt bundles and manifest
templates, but it does not spawn subagents.

## Accepted Patterns

| pattern | role flow | gate role |
|---|---|---|
| `explorer-implementer-reviewer` | explorer -> implementer -> reviewer | reviewer |
| `producer-reviewer` | producer -> reviewer | reviewer |
| `planner-generator-evaluator` | planner -> generator -> evaluator | evaluator |
| `fan-out-fan-in` | producer[0] + producer[1] -> reviewer | reviewer |

## Manifest Requirements

Every finalized manifest must preserve:

- pattern name
- role order or dependency graph
- required/optional role flags
- result-file mapping
- gate role
- strict privacy settings
- concrete `expected_roles[].call_id` for required roles

For fan-out/fan-in, indexed role IDs such as `producer[0]` and `producer[1]`
must map to distinct call IDs and distinct result files.

## Strict Pass Rules

All patterns require:

- expected-call-id scoped capture
- complete capture
- no partial acceptance
- accepted deterministic link mode for every expected role
- matching child `parent_thread_id`
- child `task_complete.last_agent_message`
- all required result files
- gate role top-level `RESULT: PASS`

Non-blocking warnings belong under a warnings section in the role result. They
must not be expressed as a top-level `RESULT: WARN` in strict accepted runs.

## Failure Conditions

Strict aggregation must fail on:

- unresolved link call IDs
- missing expected call IDs
- missing result files
- partial or failed capture
- top-level `RESULT: WARN`
- top-level `RESULT: NEEDS_FIX`
- top-level `RESULT: FAIL`
- top-level `RESULT: CAPTURE_FAILED`
- reviewer/evaluator verdict other than top-level `RESULT: PASS`

Whole-parent-session inventory is not a strict pass substitute for a scoped
expected workflow.
