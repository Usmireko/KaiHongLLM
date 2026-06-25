# Workflow Risk Levels

Use this reference to decide whether a task is low-risk, medium-risk, or high-risk.

If unsure, choose the higher risk level.

---

## 1) Low-risk tasks

Low-risk tasks are local, easy to review, and unlikely to affect data semantics or runtime behavior.

Examples:
- documentation update
- small typo
- local guard
- one-line display fix
- non-semantic log formatting
- adding a comment
- clarifying an error message without changing logic

Default workflow:
- use os-fault-engineer skill
- single-agent implementation
- Scope -> Explore -> Plan -> Patch -> Validate -> Self-review -> Final
- compact progress after each phase if the task is long
- minimal validation
- maximum repair depth 3

Validation examples:
- syntax check
- grep/readback check
- documentation rendering check if applicable

Do not use subagents unless the task unexpectedly touches multiple files or behavior.

---

## 2) Medium-risk tasks

Medium-risk tasks are localized but can affect runtime behavior, parsing, validation, or one part of the data pipeline.

Examples:
- localized script behavior change
- parser update
- exporter update
- validator update
- one fault subtype handling update
- localized PowerShell / hdc command adjustment
- adding one output field while preserving compatibility
- changing keep/drop review output format
- changing a probe command
- changing a small part of L1/L2 derivation

Default workflow:
- use os-fault-engineer skill
- use project-explorer for read-only exploration
- use project-implementer for minimal patch
- use project-repairer only if validation fails
- maximum repair depth 3

The project-explorer should identify:
- relevant files
- call/data flow
- existing conventions
- validation commands
- likely risks

The project-implementer should:
- make the smallest coherent patch
- preserve interfaces
- run targeted validation when available

The project-repairer should:
- address only the specific failed validation or review finding
- not broaden scope
- stop after repair depth 3

---

## 3) High-risk tasks

High-risk tasks can affect cross-file semantics, state machines, recovery gates, labels, or training data correctness.

Examples:
- network fault subtype boundary change
- trigger / lock / cooldown / state-machine change
- recovery gate semantics
- GT / OBS boundary
- L1 / L2 schema or semantics
- cross-end Windows / board / server coordination
- dataset label policy
- validator acceptance policy
- network collection protocol changes
- run window interpretation changes
- action recommendation safety gate changes

Default workflow:
- use os-fault-engineer skill
- use project-explorer
- use project-implementer
- use project-reviewer
- use project-repairer if needed
- maximum repair depth 3

The project-reviewer must focus on:
- missed consumers
- PowerShell quoting
- BusyBox / Toybox compatibility
- GT / OBS separation
- NET subtype boundaries
- L1 / L2 / validator impact
- recovery gate semantics
- run window alignment
- evidence-gaming risk

High-risk tasks must not be accepted without validation or an explicit statement that validation was unavailable.

---

## 4) Risk escalation triggers

Escalate to a higher risk level if the task touches any of these:
- _net_outcome.json
- _run_meta.json
- canonical_case.json
- evidence_candidates.jsonl
- diagnosis.jsonl
- cause_vs_symptom.jsonl
- action_after_diagnosis.jsonl
- validators
- recovery gate
- GT / OBS fields
- network subtype classification
- trigger / cooldown / lock logic
- cross-device hdc / ssh / PowerShell flow
- board-side shell commands
- server-side inference output contracts

---

## 5) Default final output by risk

Low-risk final output:
1. Files changed
2. Key diff summary
3. Validation result
4. Remaining risks
5. Recursion depth reached

Medium-risk final output:
1. Files changed
2. Explorer findings
3. Key diff summary
4. Commands run
5. Validation result
6. Remaining risks
7. Recursion depth reached

High-risk final output:
1. Files changed
2. Explorer findings
3. Implementer changes
4. Reviewer findings
5. Repairer actions if any
6. Commands run
7. Validation result
8. Remaining risks
9. Recursion depth reached