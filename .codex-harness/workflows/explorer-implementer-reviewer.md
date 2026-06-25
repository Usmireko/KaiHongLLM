# explorer-implementer-reviewer

## Role Flow

`project-explorer -> project-implementer -> project-reviewer`

The explorer gathers context, the implementer applies a scoped change or no-op
confirmation, and the reviewer gates acceptance.

## Expected Result Files

- `01_project_explorer_result.md`
- `02_project_implementer_result.md`
- `03_project_reviewer_result.md`

## Strict Pass Criteria

- all required roles are captured
- every role has an accepted deterministic parent-child link
- every child has matching `parent_thread_id`
- every child has `task_complete.last_agent_message`
- reviewer top-level verdict is `RESULT: PASS`

## Failure Conditions

- unresolved link call IDs
- partial capture or capture failure
- missing expected result file
- reviewer verdict is not top-level `RESULT: PASS`
- any result has top-level `RESULT: NEEDS_FIX`, `RESULT: FAIL`, or `RESULT: CAPTURE_FAILED`
