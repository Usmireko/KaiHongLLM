# producer-reviewer

## Role Flow

`producer -> reviewer`

The producer creates the scoped artifact or a no-op confirmation. The reviewer
checks the producer output and gates acceptance.

## Expected Result Files

- `01_producer_result.md`
- `02_reviewer_result.md`

## Strict Pass Criteria

- producer and reviewer are captured
- every role has an accepted deterministic parent-child link
- every child has matching `parent_thread_id`
- every child has `task_complete.last_agent_message`
- reviewer top-level verdict is `RESULT: PASS`

## Failure Conditions

- producer missing or incomplete
- reviewer missing or not `RESULT: PASS`
- unresolved link call IDs
- partial capture or capture failure
- blocking issue reported by either role
