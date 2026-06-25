# planner-generator-evaluator

## Role Flow

`planner -> generator -> evaluator`

The planner defines constraints and acceptance gates. The generator produces
the scoped artifact or no-op confirmation. The evaluator checks the output
against the plan.

## Expected Result Files

- `01_planner_result.md`
- `02_generator_result.md`
- `03_evaluator_result.md`

## Strict Pass Criteria

- planner, generator, and evaluator are captured
- every role has an accepted deterministic parent-child link
- every child has matching `parent_thread_id`
- every child has `task_complete.last_agent_message`
- evaluator top-level verdict is `RESULT: PASS`

## Failure Conditions

- missing planner constraints
- generator output violates planner constraints
- evaluator verdict is not top-level `RESULT: PASS`
- unresolved link call IDs
- partial capture or capture failure
