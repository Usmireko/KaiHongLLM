# Subagent Enablement Gate

## 1. Purpose

This document defines the manual gate that must be satisfied before any real
subagent call is allowed in this project.

It is not an executor, scheduler, router, agent configuration, or permission
enforcement mechanism. It does not spawn agents, call Codex or Claude agents,
connect to board/server/HDC/SSH/app-server, run collection, mutate files, or
enable automatic review gates.

## 2. Current Allowed State

The current P1 harness state allows only documentation and manual rehearsal:

- Non-executable role registry in `.codex-harness/templates/roles.yaml`.
- Non-executable TaskSpec examples and run notes.
- Non-executable routing notes that map TaskSpec intent to a planned role set.
- Validator structure and consistency checks over `.codex-harness`.
- Manual selection of a single role for planning.
- Human copying of a prompt to Codex or Claude for review or advice.

Codex Pro remains the primary execution tool. Claude may be used only as an
auxiliary long-context advisor, plan reviewer, result second reader, writing
aid, or context compressor. Claude and Codex must not be wired into automatic
loops.

## 3. Still Forbidden

The following remain forbidden unless a later TaskSpec and human gate explicitly
authorize a narrower action:

- Automatic scheduler.
- Multi-agent concurrency.
- Auto review gate.
- Automatic real board or HDC operations.
- Automatic real collection.
- Automatic `git add` or `git commit`.
- Automatic modification of untracked business files.
- Automatic repair loop beyond the TaskSpec repair-depth limit.
- Automatic Claude/Codex mutual loops.

These prohibitions apply even when `roles.yaml`, a TaskSpec, and a routing note
all exist. The P1 harness documents describe contracts; they do not grant
runtime authority.

## 4. Gate Before Any Real Subagent Call

Before calling any real subagent, a human operator must confirm all of the
following:

- A TaskSpec exists and names the exact task boundary.
- A routing note exists and maps the TaskSpec to the planned role set.
- The selected role exists in `.codex-harness/templates/roles.yaml`.
- Write scope is explicit.
- Forbidden paths are explicit.
- Git baseline has been confirmed.
- Untracked business scripts will not be modified.
- Board, server, or HDC access has an explicit human gate if it is needed.
- Repair depth is explicit.
- Final output must use the AgentResultBundle shape.

If any item is missing, stop before subagent invocation.

## 5. First Allowed Real Subagent Scenario

P1-8 may allow only this first real subagent rehearsal scenario, after the
entry criteria below are satisfied and a human confirms the scope:

- Call one existing Codex agent, such as `project-explorer`.
- Use a read-only role only.
- Analyze only files under `.codex-harness/`.
- Do not modify files.
- Do not connect to board, server, or HDC.
- Do not execute collection.
- Do not run `git add` or `git commit`.
- Return an AgentResultBundle.

This first scenario is intended to test prompt handoff and result format only.
It is not permission to enable scheduler behavior, concurrent agents, repair
loops, write roles, or external-system access.

## 6. Escalation Rules

Stop and escalate to a human if any planned or observed step requires:

- Writing files.
- Connecting to board or server.
- Using HDC, SSH, SCP, or app-server access.
- Modifying business scripts.
- Calling multiple agents.
- Performing repair.
- Resolving a mismatch between `roles.yaml`, TaskSpec, or routing note.
- Touching GT/OBS separation, L0/L1/L2 boundaries, accepted semantics, or
  subtype boundary behavior.

Escalation means the current subagent rehearsal must stop. A new TaskSpec and
routing note are required before continuing.

## 7. P1-8 Entry Criteria

Before P1-8 begins, all of the following must be true:

- `python .codex-harness/tools/validate_harness.py` returns
  PASS_WITH_WARNINGS or PASS with no errors.
- `roles.yaml` validation passes.
- Routing note validation passes.
- Consistency check passes.
- The working tree has no uncommitted P1-5 or P1-6 `.codex-harness` changes.
- A human explicitly confirms the read-only subagent rehearsal scope.

If the working tree still contains P1-5 or P1-6 harness changes, stop and ask
for a baseline decision before P1-8.
