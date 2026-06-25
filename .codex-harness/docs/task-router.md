# TaskRouter

TaskRouter records a manual routing decision for a TaskSpec. It explains which
skill, repository area, or human role should handle a task and why.

P0 status: documentation and template only. It does not dispatch work, create
subagents, run queues, or manage concurrency.

## Routing Inputs

Useful routing inputs:
- Task type and risk level.
- Affected paths.
- Required skills, such as `os-fault-engineer`, `hdc-kaihongos-windows`,
  `dataset-l1-l2-validation`, `board-net-collect-and-analyze`, or
  `board-net-review-run`.
- Whether board, server, dataset, or NET subtype knowledge is needed.
- Whether the work is read-only, template-only, or implementation.

## P0 Routing Policy

For this scaffold:
- `documentation` and `scaffold` tasks stay local and do not invoke device or
  server workflows.
- `server_B/tcp` tasks must be explicitly marked as future integration before
  touching server code.
- Board tasks require the HDC skill and must honor BusyBox / Toybox limits.
- Dataset tasks require explicit L0/L1/L2 and GT/OBS handling.
- NET tasks require subtype-specific evidence rules before accepting training
  data.

## Non-Goals

TaskRouter is not:
- A queue.
- A scheduler.
- A lock manager.
- An app-server endpoint.
- A multi-agent concurrency system.
- An automatic review gate.

## Template

Use `templates/task_router.yaml`.
