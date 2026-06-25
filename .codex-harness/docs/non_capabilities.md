# Non-Capabilities

These boundaries are part of the accepted harness contract.

## Subagent Launch

- Python does not directly call native `spawn_agent`.
- The parent Codex native runtime launches subagents.
- `codex exec` is not used for subagent spawning.
- Runner `--full` is prepare-and-stop only.

## Backend Authority

- App Server is not the default backend.
- App Server is not authoritative.
- App Server candidate artifacts do not replace JSONL artifacts.
- JSONL replacement is not claimed.
- `dual` mode is comparison only.

## Strict Acceptance

- Whole-parent-session inventory is not accepted as strict PASS.
- Top-level `RESULT: WARN` is not a strict PASS verdict.
- Missing result files, partial capture, unresolved links, or bad gate verdicts
  must fail strict aggregation.

## Role Resolution

`.codex/agents/*.toml` role resolution is not claimed. Observed native agent
roles are captured as runtime metadata only.

## Project Boundaries

Harness work must not modify business files by default, including:

- `board/`
- `board_A/`
- `faults/`
- `server_B/`
- root business scripts
- device and remote-access artifacts
- dataset artifacts outside the requested harness scope

The harness does not connect to boards, device bridges, remote shells, or
production servers unless a future task explicitly asks for that work and the
appropriate project skills and safety rules are used.
