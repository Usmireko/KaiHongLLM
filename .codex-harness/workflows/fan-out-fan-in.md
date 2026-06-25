# fan-out-fan-in

## Role Flow

`fanout-explorer + fanout-implementer -> fanin-reviewer`

Two independent parent-launched subagents gather or produce complementary
evidence. The fan-in reviewer synthesizes both outputs and gates acceptance.

## Expected Result Files

- `01_fanout_explorer_result.md`
- `02_fanout_implementer_result.md`
- `03_fanin_reviewer_result.md`

Manifest finalization may map indexed aliases:

- `producer[0]` -> `fanout-explorer`
- `producer[1]` -> `fanout-implementer`
- `reviewer` -> `fanin-reviewer`

## Strict Pass Criteria

- all fan-out roles and the fan-in reviewer are captured
- every role has an accepted deterministic parent-child link
- every child has matching `parent_thread_id`
- every child has `task_complete.last_agent_message`
- fan-in reviewer top-level verdict is `RESULT: PASS`

## Failure Conditions

- any fan-out role is missing or incomplete
- reviewer cannot synthesize the fan-out evidence
- reviewer verdict is not top-level `RESULT: PASS`
- unresolved link call IDs
- partial capture or capture failure
