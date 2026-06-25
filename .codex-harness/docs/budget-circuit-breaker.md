# BudgetCircuitBreaker

BudgetCircuitBreaker defines manual stop conditions. It prevents a task from
quietly expanding into risky automation, repeated repair loops, or unintended
board/server activity.

P0 status: documentation and template only. It does not measure time, kill
processes, block commands, or enforce limits.

## Suggested Budgets

Track these limits manually:
- Maximum repair depth, aligned with project policy: `3`.
- Maximum touched path count.
- Maximum new file count.
- Maximum validation attempts.
- Whether network, HDC, board, or server access is allowed.
- Whether generated data may be written.
- Whether automatic review gates are allowed. For P0 scaffold, this must be
  `false`.

## Break Conditions

Stop and report if:
- A task would require changing existing business scripts outside the approved
  path lease.
- A command would interact with the board without the HDC skill and explicit
  task scope.
- A validation result depends on GT/OBS mixing.
- Evidence is outside `_run_meta.json` run_window for training acceptance.
- NET subtype evidence is missing or weak.
- A repair attempt reaches depth 3 and still fails.
- The task starts to require an app-server, queue, concurrent agents, or
  automatic review gate.

## Trigger / Lock / Cooldown / Stage

These fields are manual safety notes:
- `trigger`: why work begins.
- `lock`: what must not be touched concurrently.
- `cooldown`: what pause or validation is needed before the next stage.
- `stage`: current task phase.

No P0 component enforces these fields.

## Template

Use `templates/budget_circuit_breaker.yaml`.
