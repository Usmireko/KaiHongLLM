# AgentResultBundle

AgentResultBundle is the manual handoff record produced at the end of a task.
It captures what changed, how it was validated, and which risks remain.

P0 status: Markdown template only. It is not submitted to an automatic review
gate, uploaded to a server, or consumed by an app-server.

## Required Sections

For implementation tasks, include:
- Files changed.
- Key diff summary.
- Commands run.
- Validation result.
- Remaining risks.
- Recursion depth reached.

For validation-only tasks, include:
- RESULT: PASS, UNCERTAIN, or FAIL.
- Failed criteria.
- Warnings.
- Evidence summary.
- Next fix.

## Project-Specific Evidence

When relevant, the bundle should mention:
- Windows / PowerShell command surface.
- Whether any HDC or board operation occurred.
- Whether `server_B/tcp` was touched or only referenced.
- L0/L1/L2 level affected.
- GT/OBS separation status.
- NET fault subtype and evidence chain.
- Trigger, lock, cooldown, and stage state at completion.

## Template

Use `templates/agent_result_bundle.md`.
