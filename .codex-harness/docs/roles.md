# Harness Role Registry

This document defines the non-executable role registry for the future Codex
harness.

P1-1 status:
- This file is not an executable agent configuration.
- P1-1 does not enable subagents.
- P1-1 does not start a scheduler, multi-agent runtime, automatic review gate,
  board workflow, server workflow, HDC command, SSH command, app-server, or real
  collection.
- `templates/roles.yaml` is a contract source for a future TaskRouter or
  subagent scheduler. It is not consumed by any runner in P1-1.
- Existing `.codex/agents/*` files are observed inputs only. P1-1 does not
  modify them.
- Existing `.claude/*`, `.agents/*`, `AGENTS.md`, and business scripts remain
  out of scope.

## Naming Model

The registry uses two layers:

| Layer | Meaning |
|---|---|
| `role_id` | Stable harness role name used by TaskSpec, TaskRouter, review notes, and result bundles. |
| `backend_agent` | Optional mapping to an existing backend Codex agent. A null value means the harness role has no runnable backend agent yet. |

Current mapping:

| Harness role | Backend agent | Notes |
|---|---|---|
| `explorer` | `project-explorer` | Read-only repository exploration. |
| `implementer` | `project-implementer` | Scoped patch role for files allowed by TaskSpec and PathLease. |
| `reviewer` | `project-reviewer` | Read-only ordinary review. |
| `adversarial_reviewer` | `project-reviewer` | Review mode only; no new runnable subagent is created in P1-1. |
| `validator` | null | Harness role only. Runs only TaskSpec-approved validation commands. |
| `repairer` | `project-repairer` | Targeted small repair role for explicit review or validation findings. |

## Role Boundaries

### explorer

The explorer is read-only. It locates files, call chains, data flow, consumers,
validation commands, and risk points. It must not edit files. Board and server
access are forbidden by default and require explicit TaskSpec authorization plus
human gate before any future runtime may attempt them.

### implementer

The implementer makes the smallest coherent patch inside TaskSpec
`write_allowed` and `allowed_paths`. It must not broaden scope, run `git add`,
commit, or edit outside the active lease. If a requested patch would modify an
untracked business script, the implementer must stop and ask for a baseline
commit before editing.

### reviewer

The reviewer is read-only. It checks missed consumers, interface compatibility,
PowerShell quoting, BusyBox/Toybox compatibility, board command restrictions,
GT/OBS separation, L0/L1/L2 boundaries, NET subtype boundaries, validator
impact, recovery gate semantics, and validation adequacy.

### adversarial_reviewer

The adversarial reviewer is a harness role and review mode mapped to
`project-reviewer`. P1-1 does not create a runnable
`adversarial_reviewer` subagent.

Use adversarial review for changes involving:
- state machines
- protocols
- trigger / lock / cooldown / stage flow
- recovery logic or recovery gates
- GT/OBS boundaries
- L0/L1/L2 schema, derivation, or validation
- accepted semantics
- NET subtype boundaries
- action recommendation safety gates

### validator

The validator is a harness role with no backend agent in P1-1. It may run only
validation commands explicitly allowed by TaskSpec. It must not automatically
repair failures, connect to board or server, invoke HDC or SSH, write generated
collection data, or write into forbidden data directories.

### repairer

The repairer is used only after review or validation provides a concrete,
bounded finding. It may modify only files already allowed by the original
TaskSpec and PathLease. Default repair depth is 1 and absolute maximum is 2 for
P1 harness routing. If a repair requires changing scope, touching additional
files, or changing semantics outside the original task, it must stop.

## Project-Wide Guardrails

Codex Pro is the primary execution tool. Claude is an auxiliary tool for long
context advice, plan review, second reading of Codex output, writing support,
and chat-history compression. Claude and Codex must not be wired into automatic
infinite loops.

Default policy:
- No automatic review gate by default.
- No multi-agent concurrency by default.
- Real board, HDC, SSH, server, app-server, or collection actions require a
  human gate and explicit TaskSpec authorization.
- Windows and PowerShell are the control-plane default.
- PowerShell, HDC, and SSH commands require careful quoting and variable
  expansion review.
- KaiHongOS/OpenHarmony board commands must not assume GNU userland.
- Board-side commands must avoid `awk` and `tr`.
- `route` must not be assumed available on the board.
- `storage/runs/**`, `dataset/raw/**`, `dataset/l0/**`, `dataset/l1/**`,
  `dataset/l2/**`, `models/**`, and `checkpoints/**` are forbidden by default.
- L0/L1/L2 boundaries, GT/OBS separation, accepted semantics, and subtype
  boundaries must remain strictly separated.

## Future Enablement Gate

This registry is intentionally inert in P1-1. Real subagent enablement should
wait until at least P1-7 and should require:
- a human-approved role registry
- TaskSpec integration
- PathLease / WriteGate enforcement
- BudgetCircuitBreaker integration
- human gate fields for board/HDC/SSH/server/collection
- validation of role output contracts
- explicit decision on Claude compatibility

