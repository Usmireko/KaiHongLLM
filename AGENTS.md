# AGENTS.md — Global Project Rules for Codex / Agents

This repository is for KaiHongOS / OpenHarmony OS fault injection, data collection, validation, and LLM-assisted OS fault diagnosis.

This global file defines project-wide non-negotiable rules for all Codex / agent work in this repository.

Use this file for:
- global device constraints
- global naming constraints
- global data-quality principles
- global validation policy
- global edit/output discipline
- skill registry

Do not put long fault-specific semantics or task-specific workflows in this global file.

Fault-specific rules should live in:
- faults/<family>/AGENTS.md
- fault-specific audit / validate skills

Task workflows should live in:
- .codex/skills/<skill-name>/SKILL.md for legacy existing skills
- .agents/skills/<skill-name>/SKILL.md for new repo-specific skills

Do not create duplicate same-name skills across both locations. If a skill already exists in one location, update or reference that existing skill unless the user explicitly requests a migration.

---

## 0) Scope and precedence

Follow these sources in this order:

1. Explicit user instruction in the current task.
2. This global AGENTS.md.
3. More local AGENTS.md files, such as faults/net/AGENTS.md.
4. Matching Codex skills in .codex/skills/*/SKILL.md or .agents/skills/*/SKILL.md.
5. Existing code conventions in the touched files.

If a local rule or skill conflicts with this global file, stop and report the conflict instead of silently choosing one.

For non-trivial implementation tasks, prefer using the os-fault-engineer skill.

For NET collection or NET run review tasks, use the matching NET skill listed in the skill registry below.

---

## 1) Device environment constraints: BusyBox / Toybox

The target board environment does NOT have awk or tr.

Do not generate device-side commands that depend on:
- awk
- tr

Prefer simple and portable constructs:
- sh
- sed
- grep
- head
- tail
- cat
- cut only when already known safe
- simple for loops
- multiple short shell invocations instead of one clever pipeline

When unsure whether a command is available on the board, do not assume GNU userland.

Do not generate Linux desktop/server-style commands for the board unless the command has already been verified on the target image.

---

## 2) Naming constraints

Treat the following as the same conceptual file:
- net_fault
- net_fault1
- net_fault2
- any other numbered variant

The real file name must remain:
- net_fault

Do not output or reference filenames like:
- net_fault1
- net_fault2
- net_fault_v2

The same rule applies to other project scripts.

Do not invent numbered filenames such as:
- *_1
- *_2
- *_v2

unless the user explicitly asks for a new versioned file.

When discussing previous user-provided variants, normalize the name back to the real project file name.

---

## 3) Anti evidence-gaming principle

Do not add actions whose primary purpose is only to make validators pass while being unrelated to fault semantics.

If any self-test, traffic trigger, probe, or synthetic stimulus is necessary:

1. It must be explicitly logged.
2. It must have a clear marker line.
3. It must be protocol / port / semantic-aligned with the fault type.
4. It must not silently hide evidence.

Recommended marker format:

  ### SELFTEST_<TYPE> <short description>

Avoid silent triggers such as:

  some_probe >/dev/null 2>&1

unless the command also writes a meaningful marker or structured result elsewhere.

Validators should prefer causal evidence, such as before/after deltas near the actual probe or injection window, rather than arbitrary counters like any counter > 0.

Fault-specific rules for valid self-test behavior should live in fault-scoped AGENTS.md or fault-specific skills.

---

## 4) HDC / remote shell quoting rules: Windows to device

Remote command construction is fragile.

When generating Windows PowerShell commands that invoke hdc shell:

- Follow existing repository style first.
- Prefer short, separate hdc shell invocations.
- Avoid multi-line remote scripts in a single hdc shell invocation.
- Avoid overly clever one-liners.
- Avoid injecting raw untrusted strings into remote commands.
- Be careful with IPs, hostnames, and .nip.io style strings when quoting may break.

Default quoting rule for this repository:

- Prefer wrapping remote commands in double quotes on the PowerShell side.
- Use single quotes only inside the remote command when needed.

Example style:

  hdc shell "ifconfig"
  hdc shell "route"
  hdc shell "ping -c 3 8.8.8.8"

If an existing script already uses a different quoting pattern and it is known to work, preserve the local convention instead of doing style-only rewrites.

Prefer many short lines over one clever line for probes.

---

## 5) Run window alignment

Default rule: fault-effect evidence used for training acceptance must be collected within _run_meta.json run_window.

Examples of fault-effect evidence include:
- fault-phase probes
- fault-phase snapshots
- fault-phase counters
- injected marker lines
- run_begin / run_end marker lines

A small tolerance is allowed when appropriate, for example:
- +/- 2 seconds

If fault-effect evidence is outside the run window, the run is considered invalid for training, unless the task explicitly asks for a non-training diagnostic review.

Fault-scoped policy may define additional timing windows when the collector intentionally separates:
- fault / main collection window
- recovery gate
- post / post2 recovery snapshots
- final baseline

When a fault-scoped policy explicitly documents this split, recovery_gate, probe_post, and probe_post2 after _run_meta.json run_window are reviewed as recovery/post-window evidence rather than automatic run_window violations. The policy must define the acceptance gates and evidence consistency checks that make the recovery/post-window evidence valid.

Fault-specific definitions of minimum valid evidence should live in fault-scoped AGENTS.md or fault-specific skills.

---

## 6) Data validation policy: PASS / UNCERTAIN / FAIL

Validators should use three statuses:
- PASS
- UNCERTAIN
- FAIL

### FAIL

Use FAIL if any of the following is true:
- required files are missing
- probe execution is broken
- shell execution produced errors such as:
  - /bin/sh:
  - syntax error
  - unmatched
  - inaccessible
- required fault-specific effect evidence is missing
- required evidence is outside the applicable run/recovery window policy
- GT / OBS separation is violated
- data is structurally invalid

### UNCERTAIN

Use UNCERTAIN if:
- injection appears enabled
- some effect evidence exists
- but supporting evidence is weak, incomplete, noisy, or partially outside expected timing
- the case may be useful for debugging but is not clearly accepted for training

Fault-specific validators may define additional UNCERTAIN cases.

### PASS

Use PASS only if:
- the minimum evidence chain is complete
- the evidence is internally consistent
- the evidence is inside the run window
- common validation gate passes
- fault-specific validation passes

Always run the common gate first, such as:

  wk_validate_run_common

Then run the fault-specific validator.

Do not mark a run as accepted for training only because one validator counter passed.

Acceptance must follow the fault semantics.

---

## 7) GT / OBS separation

The dataset uses a strict separation between:

- GT: ground truth, intended or injected fault identity
- OBS: observed symptoms, probes, logs, counters, and derived signals

OBS must never replace GT.

Do not silently convert observed symptoms into ground truth labels.

When GT and OBS disagree:

1. Preserve GT.
2. Record the OBS discrepancy.
3. Mark the case as UNCERTAIN or not accepted if evidence is insufficient.
4. Do not relabel the case unless the user explicitly asks for a relabeling workflow.

For difficult cases such as memory pressure / OOM-safe behavior, separate:

- primary root cause
- secondary symptoms
- process-level suspects
- noisy incidental observations

---

## 8) Keep outputs concise and actionable

When reporting validation or review results, output:

- RESULT: PASS / UNCERTAIN / FAIL
- failed criteria
- warnings
- one-line next fix

Do not dump huge logs unless explicitly requested.

Prefer short structured summaries.

Example:

  RESULT: UNCERTAIN
  Failed criteria:
  - recovery_gate_ok=false
  Warnings:
  - post probe recovered DNS but IP probe failed
  Next fix:
  - Re-run post2 probe with explicit WK_NET_PING_IP and compare probe epoch.

---

## 9) Scope of edits

When asked to modify code:

- prefer minimal diffs
- edit only necessary sections
- preserve existing variable names
- preserve directory layout
- preserve public interfaces unless explicitly asked
- do not introduce new dependencies unless required
- do not rewrite working scripts for style-only reasons
- do not broaden task scope without evidence

If a requested change has a large impact surface, first explore and report likely affected files before editing.

---

## 10) Recursion and repair policy

Recursive repair is allowed, but the maximum repair depth is 3.

Definition:

- Depth 0: initial implementation
- Depth 1: first targeted repair
- Depth 2: second targeted repair
- Depth 3: final targeted repair

After Depth 3, stop.

Do not continue looping.

At each repair depth:

1. State the failed command, review finding, or broken behavior.
2. Form one concrete hypothesis.
3. Make the smallest targeted fix.
4. Re-run the same validation if possible.
5. Do not expand scope unless required by evidence.

If Depth 3 still fails, stop and report:

- exact failure
- files touched
- what was tried
- likely cause
- recommended next manual check

---

## 11) Progress visibility

For long or multi-step tasks, provide compact progress after each major phase.

Use this format:

  [PROGRESS]
  - Phase:
  - Findings:
  - Current decision:
  - Next step:
  - Blockers:

Do not remain silent through a long multi-step task.

---

## 12) Skill registry

The following local skills are available in this workspace and should be used when the task matches them.

### os-fault-engineer

Path:

  .agents/skills/os-fault-engineer/SKILL.md

Purpose:

Use for general implementation tasks in this project.

This skill defines the standard engineering loop:

  Scope -> Explore -> Plan -> Patch -> Validate -> Self-review -> Final

Use it for:
- code changes
- script changes
- validator changes
- data pipeline changes
- cross-file implementation tasks
- task risk classification
- repair loops with maximum depth 3

---

### hdc-kaihongos-windows

Path:

  .codex/skills/hdc-kaihongos-windows/SKILL.md

Policy:

This is an existing legacy skill under .codex/skills with established references/ and scripts/. Do not overwrite or recreate it under another skill path. Project-specific HDC rules live in:

  .codex/skills/hdc-kaihongos-windows/references/PROJECT-HDC-RULES.md

Purpose:

Use when generating or modifying Windows PowerShell commands that interact with KaiHongOS / OpenHarmony boards through hdc.

Use it for:
- hdc shell
- hdc file send
- hdc file recv
- board-side probes
- Windows-to-board quoting
- avoiding unavailable board tools such as awk and tr

---

### dataset-l1-l2-validation

Path:

  .codex/skills/dataset-l1-l2-validation/SKILL.md

Policy:

If the .codex version already exists, use that existing path. When creating a new repo-specific skill, prefer .agents/skills/<skill-name>/SKILL.md and do not create a duplicate same-name skill.

Purpose:

Use for L0 / L1 / L2 dataset export, derivation, schema validation, GT / OBS separation, evidence extraction, and training-sample preparation.

---

### board-net-collect-and-analyze

Path:

  .codex/skills/board-net-collect-and-analyze/SKILL.md

Policy:

If the .codex version already exists, use that existing path. When creating a new repo-specific skill, prefer .agents/skills/<skill-name>/SKILL.md and do not create a duplicate same-name skill.

Purpose:

Run board-side NET collection through run_wukong_collect_refactor.ps1, then perform keep / drop review and optional L1 / L2 follow-up.

Use this skill for new NET data collection tasks.

---

### board-net-review-run

Path:

  .codex/skills/board-net-review-run/SKILL.md

Policy:

If the .codex version already exists, use that existing path. When creating a new repo-specific skill, prefer .agents/skills/<skill-name>/SKILL.md and do not create a duplicate same-name skill.

Purpose:

Review an already collected NET run, decide keep / drop / uncertain, and optionally continue to L1 / L2.

Use this skill for existing NET run audit tasks.

---

## 13) Final response format

For implementation tasks, final output must include:

1. Files changed
2. Key diff summary
3. Commands run
4. Validation result
5. Remaining risks
6. Recursion depth reached

For validation-only tasks, final output must include:

1. RESULT: PASS / UNCERTAIN / FAIL
2. Failed criteria
3. Warnings
4. Evidence summary
5. Next fix
