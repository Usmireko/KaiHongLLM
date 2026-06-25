# Routing Note

Schema: `codex-harness.routing_note.p1`
Status: non-executable manual planning note

This routing note does not spawn agents. It does not dispatch work, start a
scheduler, run multi-agent concurrency, enable an automatic review gate, connect
to board/server/HDC/SSH/app-server, or execute real collection.

## TaskSpec Source

- TaskSpec path:
- Task ID:
- Task title:
- Requested by:
- Created at:

## Task Summary

- `risk_level`:
- `task_type`:
- Stage:
- Trigger:
- Lock:
- Cooldown:

## Scope

In scope:
- 

Out of scope:
- creating runnable subagents
- modifying `.codex/agents/*`
- modifying `.claude/*`
- modifying `.agents/*`
- modifying `AGENTS.md`
- editing business scripts outside TaskSpec write scope
- board/server/HDC/SSH/app-server access without human gate
- real collection
- scheduler, multi-agent concurrency, or automatic review gate

## Path Policy

`write_allowed`:
- 

`read_only`:
- 

`forbidden_paths`:
- `.git/**`
- `storage/runs/**`
- `dataset/raw/**`
- `dataset/l0/**`
- `dataset/l1/**`
- `dataset/l2/**`
- `models/**`
- `checkpoints/**`
- 

Untracked business script policy:
- If a planned edit touches an untracked business script, stop and require a
  baseline commit before modification.

## Role Selection Rules

Source role registry:
- `.codex-harness/templates/roles.yaml`

Routing rules:

| Task condition | Planned roles | Review mode | Notes |
|---|---|---|---|
| low risk | `implementer`; optional `reviewer` | `self-review` or `standard` | Use only with explicit write scope. Do not spawn automatically. |
| medium risk | `explorer`, `implementer`, `reviewer`; optional `repairer` if validation fails | `standard` | Repairer is bounded to original write scope. |
| high risk | `explorer`, `implementer`, `adversarial_reviewer`, `reviewer`; `repairer` only with bounded depth | `adversarial` | Required for state machine, protocol, recovery, GT/OBS, L1/L2, accepted semantics, or subtype boundary risks. |
| validation-only | `validator` only | `none` | Run only TaskSpec-approved validation commands. No automatic repair. |
| review-only | `reviewer` or `adversarial_reviewer` | `standard` or `adversarial` | Read-only review. No file modification. |
| repair | `repairer` only | `targeted-repair` | Must be bounded by previous TaskSpec write scope and concrete finding. |

## Selected Roles

Planned role set:
- 

## Role Sequence

1. 

## Backend Agent Mapping

| Harness role | backend_agent | write_permission | access policy |
|---|---|---|---|
| `explorer` | `project-explorer` | `none` | board/server forbidden by default |
| `implementer` | `project-implementer` | `scoped` | board/server require human gate |
| `reviewer` | `project-reviewer` | `none` | board/server forbidden by default |
| `adversarial_reviewer` | `project-reviewer` with `review_mode=adversarial` | `none` | board/server forbidden by default |
| `validator` | null | `forbidden` | board/server require TaskSpec authorization and human gate |
| `repairer` | `project-repairer` | `scoped` | board/server require human gate |

## Review Mode

- Selected `review_mode`:
- Reason:
- Adversarial review required: yes/no

High-risk surfaces requiring adversarial review:
- state machine
- protocol
- trigger / lock / cooldown / stage flow
- recovery logic or recovery gate
- GT/OBS boundary
- L0/L1/L2 derivation, schema, or validation
- accepted semantics
- subtype boundary
- action recommendation safety gate

## Validation Plan

Validation commands allowed by TaskSpec:
- 

Validation role:
- `validator` may run only TaskSpec-approved validation commands.
- `validator` must not repair automatically.
- `validator` must not write generated collection data.
- `validator` must not access board/server/HDC/SSH/app-server unless TaskSpec
  explicitly authorizes it and a human gate is passed.

## Repair Policy

- Repair role:
- Repair trigger:
- `max_repair_depth`:
- Absolute max repair depth:
- Repair scope source:

Rules:
- `repairer` may run only for a concrete validation failure or reviewer finding.
- `repairer` may modify only the original TaskSpec `write_allowed` paths.
- If repair requires scope expansion, stop.
- If repair touches GT/OBS, L0/L1/L2, accepted semantics, subtype boundary,
  protocol, state machine, or recovery gate, escalate to adversarial review and
  human decision.

## Human Gate

`human_gate_required`: yes/no

Human gate is required for:
- real board actions
- HDC commands
- SSH/SCP commands
- server mutation or app-server access
- real collection
- generated run/data writes
- modification of untracked business scripts before baseline commit
- GT relabeling or accepted semantics change
- L0/L1/L2 boundary change
- subtype boundary change

## Board Access Policy

- `board_access_policy`:
- Board commands must avoid `awk` and `tr`.
- Do not assume GNU userland on KaiHongOS/OpenHarmony.
- Do not assume `route` exists.
- Prefer short, explicit commands and careful Windows/PowerShell quoting.

## Server Access Policy

- `server_access_policy`:
- SSH/SCP/app-server access is forbidden unless TaskSpec explicitly authorizes it
  and human gate passes.
- Server-side generated data writes are forbidden unless TaskSpec scopes them.

## Claude Assist Policy

- Codex Pro is the primary execution tool.
- Claude may be used only as auxiliary long-context advisor, plan reviewer,
  result second reader, writing aid, or context compressor.
- Do not wire Claude and Codex into automatic loops.
- Claude assistance must not execute commands, spawn agents, or bypass human
  gates.

## Stop Conditions

Stop and report if:
- TaskSpec is missing or ambiguous.
- Required write scope is missing.
- A planned edit touches a forbidden path.
- A planned edit touches an untracked business script before baseline commit.
- The task requires board/server/HDC/SSH/app-server/collection without explicit
  TaskSpec authorization and human gate.
- The task would enable scheduler, multi-agent concurrency, or automatic review
  gate.
- Repair depth would exceed the TaskSpec or role registry limit.
- Evidence or validation would write into `storage/runs/**`, `dataset/raw/**`,
  `models/**`, `checkpoints/**`, or `.git/**`.
- GT/OBS, L0/L1/L2, accepted semantics, or subtype boundary decisions require
  human judgment.

## Escalation Rules

Escalate to human if:
- TaskRouter cannot choose a role set from TaskSpec risk and task type.
- The task crosses from documentation/scaffold into implementation.
- Board/server/HDC/SSH/app-server or real collection is needed.
- GT/OBS relabeling, accepted semantics, L0/L1/L2, or subtype boundary changes
  are proposed.
- More repair depth is requested than allowed.

Escalate to adversarial review if:
- state machine, protocol, recovery, trigger/lock/cooldown/stage, GT/OBS,
  L0/L1/L2, accepted semantics, subtype boundary, or action safety is touched.

## Final AgentResultBundle Expectation

Final output should include:
1. Summary
2. Files changed
3. Diff summary
4. Validation commands
5. Validation result
6. Planned roles and backend mapping
7. Human gate status
8. Remaining risks
9. Rollback plan
10. Whether this enabled real subagents
11. Next recommended task

Expected statement:
- This routing note did not spawn agents, enable subagents, run a scheduler,
  start multi-agent concurrency, enable an automatic review gate, connect to
  board/server/HDC/SSH/app-server, or execute real collection.
