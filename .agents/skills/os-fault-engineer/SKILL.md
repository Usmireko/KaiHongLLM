---
name: os-fault-engineer
description: Use this skill for implementation tasks in the KaiHongOS/OpenHarmony OS fault diagnosis project, especially code changes involving board collectors, server inference, network faults, L1/L2 dataset export, validators, recovery gates, or closed-loop diagnosis.
---

# OS Fault Engineer Skill

This skill makes Codex behave like an implementation engineer instead of only a reviewer.

Use this skill when the task involves:
- editing project scripts
- implementing a bug fix
- adding or modifying fault collection logic
- changing validators
- changing L1 / L2 dataset derivation
- changing evidence extraction
- changing server-side diagnosis pipeline
- changing board-side collection or injection behavior
- generating a Codex implementation prompt for this project

Always follow:
- repo root AGENTS.md
- local AGENTS.md files when relevant, for example faults/net/AGENTS.md
- task-specific skills when relevant

For detailed task risk classification, read:
- references/workflow-risk-levels.md

For recursive repair rules, read:
- references/repair-depth-policy.md

For subagent prompt templates, read:
- references/subagent-templates.md

---

## 1) Default engineering loop

For non-trivial implementation tasks, use this loop:

  Scope -> Explore -> Plan -> Patch -> Validate -> Self-review -> Final

Do not skip Explore unless the task is clearly a tiny local edit.

---

## 2) Phase 1: Scope

Restate the task in concrete terms.

Identify:
- objective
- expected behavior
- likely affected files or areas
- out-of-scope items
- risk level: low, medium, or high

If unsure about risk level, choose the higher risk level.

---

## 3) Phase 2: Explore

Before editing:
1. Inspect relevant files.
2. Identify call chains.
3. Identify data flow.
4. Identify consumers.
5. Identify validation commands.
6. Identify existing conventions.

Do not patch before understanding the impact surface.

For NET changes, inspect the full chain:
1. injector behavior
2. collection / probe behavior
3. _net_outcome.json
4. _run_meta.json
5. L1 export
6. evidence_candidates.jsonl
7. L2 derivation
8. validators
9. recovery gate handling

For dataset changes, inspect:
1. L0 raw bundle assumptions
2. L1 canonical case format
3. evidence candidate generation
4. L2 task sample generation
5. schema validators
6. existing accepted examples

For board-side changes, inspect:
1. hdc invocation style
2. PowerShell quoting
3. device command availability
4. run window markers
5. probe output markers
6. recovery markers

---

## 4) Phase 3: Plan

Create 3 to 7 subtasks.

The plan should include:
- files likely to change
- validation commands
- compatibility risks
- expected output changes

Prefer:
- localized patch
- compatibility-preserving update
- existing script conventions
- existing schema fields
- explicit marker lines for self-tests
- causal evidence rather than arbitrary counters

Avoid:
- broad rewrite
- changing public interfaces without need
- silently changing GT semantics
- mixing GT and OBS
- relying on unavailable board tools
- creating numbered script variants
- evidence-gaming changes

---

## 5) Phase 4: Patch

Patch rules:
- Make the smallest coherent patch.
- Preserve existing interfaces unless explicitly asked.
- Preserve existing variable names when possible.
- Preserve directory layout.
- Do not introduce new dependencies unless required.
- Do not rewrite working code for style-only reasons.
- Do not broaden the task scope.

Board-side rules:
- Do not use awk.
- Do not use tr.
- Prefer short device commands.
- Prefer explicit markers.
- Avoid silent probes.

PowerShell / hdc rules:
- Follow existing repository style.
- Prefer safe quoting.
- Prefer many short invocations.
- Avoid raw string injection.
- Avoid multi-line remote scripts inside one hdc shell.

Dataset rules:
- Preserve GT / OBS separation.
- Do not replace GT with OBS.
- Do not promote secondary symptoms to primary cause.
- Preserve recovery gate semantics.

Naming rules:
- Do not introduce net_fault1, net_fault2, or numbered variants.
- Do not invent numbered script names unless explicitly asked.

---

## 6) Phase 5: Validate

Run the narrowest useful validation.

Possible validation types:
- syntax check
- script dry run
- dataset validator
- L1 export validation
- L2 derivation validation
- NET-specific validator
- probe epoch review
- _net_outcome.json review
- board/server smoke only if safe and available

If validation cannot be run:
- state why
- provide the exact command the user should run

---

## 7) Phase 6: Self-review

Before final output, check:
1. Did all consumers get updated?
2. Did the patch preserve backward compatibility?
3. Did it preserve GT / OBS separation?
4. Did it affect network subtype boundaries?
5. Did it preserve recovery gate semantics?
6. Did it depend on unavailable board commands?
7. Did it introduce numbered filenames?
8. Did it change behavior outside the task scope?
9. Did validation cover the changed path?
10. Did any self-test align with fault semantics?

For high-risk tasks, use project-reviewer or request adversarial review.

---

## 8) Subagent usage policy

Codex may not automatically spawn subagents unless explicitly requested.

For this project:
- low-risk tasks may use a single agent
- medium-risk tasks should use project-explorer and project-implementer
- high-risk tasks should use project-explorer, project-implementer, project-reviewer, and project-repairer when needed

Read references/subagent-templates.md when constructing prompts for Codex.

---

## 9) Progress format

For long tasks, output:

  [PROGRESS]
  - Phase:
  - Findings:
  - Current decision:
  - Next step:
  - Blockers:

Keep progress updates short.

---

## 10) Final output format

For implementation tasks, final output must include:
1. Files changed
2. Key diff summary
3. Commands run
4. Validation result
5. Remaining risks
6. Recursion depth reached

For review-only tasks, final output must include:
1. RESULT: PASS / UNCERTAIN / FAIL
2. Critical issues
3. High issues
4. Medium issues
5. Low issues
6. Recommended fix order