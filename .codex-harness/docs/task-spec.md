# TaskSpec

TaskSpec is the manual intake record for one requested task. It captures what
the operator wants done, which project rules apply, and which evidence is
required before a result can be trusted.

P0 status: documentation and template only. No parser, runner, scheduler, or
agent launcher reads this file.

## Required Intent

A TaskSpec must state:
- Objective in one or two concrete sentences.
- Task type: implementation, validation-only, review-only, collection, dataset,
  documentation, or scaffold.
- Scope boundaries, including paths that may be touched and paths that are
  explicitly out of scope.
- Expected output format.
- Validation command or reason validation is not applicable.

## Project Guardrails

Every TaskSpec should explicitly preserve:
- Windows / PowerShell as the host-side default.
- KaiHongOS / OpenHarmony board constraints when device work is in scope.
- No board-side dependency on `awk` or `tr`.
- Short, separate `hdc shell "..."` commands when board commands are needed.
- L0/L1/L2 dataset boundaries.
- GT/OBS separation.
- NET fault subtype semantics.
- Run-window alignment for evidence.
- Trigger, lock, cooldown, and stage metadata as explicit task state.

## GT / OBS Rules

Ground truth fields describe intended or injected fault identity. Observation
fields describe symptoms, logs, counters, probes, and derived evidence. A
TaskSpec must not ask an agent to silently replace GT with OBS. If GT and OBS
disagree, preserve GT and report the discrepancy.

## Stage Metadata

The `stage` field is descriptive in P0. Suggested values:
- `scope`
- `explore`
- `patch`
- `validate`
- `review`
- `final`

Trigger, lock, and cooldown fields are also descriptive. They are meant to make
manual coordination visible, not to drive an executable state machine.

## Template

Use `templates/task_spec.yaml`.
