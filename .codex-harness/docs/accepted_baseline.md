# Accepted Baseline

This document summarizes the accepted Codex Native Subagent Harness baseline
through V15. It is a mainline-readiness summary, not a replacement for the raw
probe artifacts.

## Status Chain

| version | status | accepted meaning |
|---|---|---|
| V0.1 | `V0_PASS` | Scoped parent-line-range capture proved native subagent metadata could be parsed without Python spawning agents. Whole-parent inventory was not accepted as strict PASS. |
| V1 | rejected for strict pass | Scoped result capture found a reviewer `RESULT: WARN`; strict aggregation correctly refused to treat WARN as PASS. |
| V2.1 | `V2_NOT_PASS` | Positive source attempt failed because capture was partial and reviewer returned `RESULT: NEEDS_FIX`. |
| V2.2 | repair ready | Strict aggregation and verdict parsing were hardened, including adapter metadata gates and top-level verdict parsing. |
| V2.3-A | diagnostic only | Fresh preflight showed missing `collab_agent_spawn_end` in the current runtime. |
| V2.4 | `V2_4_LINK_MODE_READY` | Deterministic JSONL link mode `function_call_output_agent_id` was accepted when `output.agent_id` exactly matched the child thread ID. |
| V2.5 | `V2_PASS` | Full positive JSONL source workflow and strict aggregation passed with expected-call-id scope and reviewer `RESULT: PASS`. |
| V3 | `V3_PASS` | Runner integrated prepare, capture, aggregate, status, and full prepare-and-stop modes without spawning subagents. |
| V4 | `V4_PASS` | Workflow pattern abstraction accepted for four patterns. |
| V5 | `V5_PASS` | Manifest finalization and call-ID mapping accepted. |
| V6 | `V6_RESEARCH_COMPLETE` | App Server protocol/schema research completed. JSONL remained baseline. |
| V7 | `V7_RESEARCH_COMPLETE` | Isolated live App Server equivalence research captured deterministic link and result evidence. |
| V8 | `V8_SHADOW_ADAPTER_READY` | Offline App Server shadow adapter produced equivalence classification against JSONL artifacts. |
| V9 | `V9_LIVE_SHADOW_READY` | Isolated live App Server shadow mode produced a valid comparison. |
| V10 | `V10_CANDIDATE_BACKEND_READY` | App Server candidate artifacts could be generated and compared for a controlled one-role run. No replacement claim. |
| V11 | `V11_PASS` | App Server candidate parity passed across multiple controlled patterns A-D. JSONL remained authoritative. |
| V12 | `V12_PASS` | Runner-level backend selection accepted with JSONL default, App Server candidate opt-in, and dual experimental mode. |
| V13 | `V13_PASS` | Fan-out/fan-in parity and concurrency handling passed for JSONL authoritative and App Server candidate comparison. |
| V14 | `V14_PASS` | Backend policy hardening and negative policy checks accepted. |
| V15 | `V15_BENCHMARK_COMPLETE` | Runtime scheduling, width probes, backend latency, and overhead benchmark reports were produced. This is benchmark evidence, not a correctness gate. |

## Current Accepted Capabilities

- Prepare prompt bundles and workflow templates for parent Codex native
  subagent calls.
- Finalize manifests with concrete expected call IDs.
- Capture completed native subagent calls from JSONL session logs.
- Run strict aggregation over captured result files.
- Report workflow status and backend-policy state.
- Generate App Server shadow/candidate artifacts for comparison only.
- Validate four accepted workflow patterns.
- Record fan-out/fan-in timing and runtime-width benchmark evidence.

## Current Authority

JSONL is the default and authoritative capture backend. App Server is
candidate/shadow only. `dual` mode compares App Server candidate evidence
against JSONL authoritative evidence.

## Strict Acceptance Requirements

Strict source acceptance requires:

- `adapter_exit_code = 0`
- `allow_partial = false`
- `capture_status = complete`
- `workflow_validation_scope = expected_call_ids` for V2+ positive acceptance
- every expected role has a deterministic parent-child link
- child `session_meta.parent_thread_id` matches the parent thread
- child `task_complete.last_agent_message` exists
- all expected result files exist
- gate role verdict is top-level `RESULT: PASS`
- no top-level `RESULT: WARN`, `RESULT: NEEDS_FIX`, `RESULT: FAIL`, or
  `RESULT: CAPTURE_FAILED`

Whole-parent-session inventory is useful diagnostic evidence, but it is not a
strict PASS source by itself.

## Non-Claims

- Python does not directly call native `spawn_agent`.
- `codex exec` is not used for subagent spawning.
- `.codex/agents/*.toml` role resolution is not claimed.
- App Server is not the default backend.
- App Server is not authoritative.
- JSONL replacement is not claimed.
