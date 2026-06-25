# Backend Policy

Backend policy is defined in `.codex-harness/backend_policy.json` and enforced
by runner policy validation.

## Accepted Policy

- `default_capture_backend`: `jsonl`
- `authoritative_backend`: `jsonl`
- `candidate_backends`: `app-server-candidate`
- `experimental_backends`: `dual`
- `jsonl_replacement_claimed`: `false`
- `app_server_default_allowed`: `false`
- `app_server_authoritative_allowed`: `false`

## Backend Modes

### `jsonl`

Default and authoritative. Uses `codex_native_subagent_adapter.py` to scan
local JSONL session logs for completed parent-launched native subagent calls.

### `app-server-candidate`

Explicit opt-in candidate mode. Uses an isolated App Server process when live
capture is requested. It may emit JSONL-compatible candidate artifacts, but it
must mark:

- `authority: app-server-candidate`
- `jsonl_replacement_claimed: false`
- `backend_default: false`

### `dual`

Experimental comparison mode. JSONL remains authoritative. App Server candidate
artifacts are compared against the JSONL authoritative run, and mismatches must
be reported rather than silently ignored.

## Accepted Deterministic Link Modes

- `collab_agent_spawn_end`
- `function_call_output_agent_id`
- `app_server_collab_agent_tool_call`
- `app_server_function_call_output_agent_id`

App Server link modes are accepted only as candidate/shadow evidence unless
the run also has a JSONL authoritative comparison.

## Negative Policy

Policy validation must fail if:

- default backend is not `jsonl`
- authoritative backend is not `jsonl`
- App Server candidate is default or authoritative
- `jsonl_replacement_claimed = true`
- `dual` mode lacks `authoritative_backend = jsonl`
- candidate/equivalence metadata is missing for candidate runs

## Boundary

The backend policy does not authorize Python to call native `spawn_agent`.
Parent Codex native runtime remains responsible for subagent launch.
