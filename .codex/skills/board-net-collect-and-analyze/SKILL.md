---
name: board-net-collect-and-analyze
description: Use this skill to run board-side NET collection through run_wukong_collect_refactor.ps1, then review the collected run for keep/drop/uncertain and optional L1/L2 follow-up.
---

# Board NET Collect and Analyze Skill

Use this skill when the task is to collect a new NET fault run from the board and analyze whether the run is usable.

This skill covers:

- pre-run baseline
- NET fault collection
- probe artifact review
- _net_outcome.json review
- keep / drop / uncertain decision
- optional L1 / L2 follow-up

Always follow:

- global AGENTS.md
- faults/net/AGENTS.md
- hdc-kaihongos-windows skill for hdc / board commands
- dataset-l1-l2-validation skill if continuing to L1 / L2

---

## 1) When to use this skill

Use for tasks like:

- collect one NET run
- collect several NET runs
- rerun net_dns_fail
- rerun net_link_down
- rerun net_link_flap
- collect net_auth_fail
- collect new NET subtype data
- collect then decide whether to keep or drop
- collect then export L1/L2 if accepted

Do not use this skill for reviewing an already collected run only.

For existing runs, use:

  board-net-review-run

---

## 2) Required working loop

Use this loop:

  Scope -> Pre-check -> Collect -> Inspect -> Decide -> Optional L1/L2 -> Final

For implementation changes to the collection scripts, also use:

  os-fault-engineer

---

## 3) Phase 1: Scope

Identify:

- target fault subtype
- board connection method
- expected interface
- expected probe targets
- whether this is a smoke run or accepted-data run
- whether L1/L2 export is required
- whether recovery gate must pass

Record relevant parameters, such as:

- WK_NET_DNS_HOST
- WK_NET_PING_IP
- WK_NET_LINK_DOWN_HOLD_SEC
- WK_NET_FLAP_COUNT
- WK_NET_FLAP_DOWN_SEC
- WK_NET_FLAP_UP_SEC

Use actual project-supported parameters only.

Do not invent new parameters without implementation work.

---

## 4) Phase 2: Pre-check

Before collection, check baseline if available.

Recommended baseline layers:

1. interface state
2. route state
3. gateway ping
4. public IP ping
5. DNS host probe

A baseline should answer:

  Is the board network healthy before injection?

If baseline fails:

- do not collect accepted training data
- either fix environment or mark run as diagnostic-only

Example output:

  Baseline:
  - gateway: PASS
  - IP: PASS
  - DNS: PASS
  Decision: proceed

---

## 5) Phase 3: Collect

Use existing project collection script:

  run_wukong_collect_refactor.ps1

Do not rename it.

Do not create numbered variants.

During collection:

- preserve run ID
- preserve logs
- preserve probe output
- preserve injector markers
- preserve _run_meta.json
- preserve _net_outcome.json

Do not add hidden traffic triggers.

Any self-test must be:

- explicitly logged
- semantically aligned
- inside the fault/main run window when used as fault-effect evidence, or inside the documented recovery/post-window when a fault-scoped policy allows that split
- visible to validators

---

## 6) Phase 4: Inspect artifacts

After collection, inspect:

- _run_meta.json
- _net_outcome.json
- probe_fault.txt
- probe_post.txt
- probe_post2.txt

When files are absent, report absence explicitly.

Do not infer success from missing files.

Review:

- run_id
- gt.family
- gt.subtype
- run_window
- net_fault_type
- iface_used
- inject_ok
- fault_observed
- recovery_observed
- fault_observation_reason
- recovery_observation_reason
- recovery_gate_ok
- recovery_gate_reason

Also inspect probe execution quality.

Broken probe indicators:

- /bin/sh:
- syntax error
- unmatched
- inaccessible
- Unknown command
- not found

If probe execution is broken, mark run as FAIL or not accepted.

---

## 7) Phase 5: NET subtype evidence review

Follow faults/net/AGENTS.md.

Minimum review questions:

1. Does GT subtype match the intended injection?
2. Does _net_outcome.json match GT subtype?
3. Was injection successful?
4. Was fault effect observed?
5. Was recovery observed?
6. Did recovery gate pass?
7. Are fault/main probe timestamps inside run window, and are recovery/post probes allowed by the fault-scoped recovery/post-window policy when applicable?
8. Is subtype boundary clean?
9. Are secondary symptoms properly separated?
10. Is there evidence-gaming risk?

---

## 8) Phase 6: Decision

Use one of:

- KEEP
- DROP
- UNCERTAIN
- NOT_ACCEPTED

### KEEP

Use if:

- fault effect observed
- recovery observed if required
- recovery gate passes or accepted by explicit rule
- required fault/main evidence is inside `_run_meta.json.run_window`
- recovery gate, `probe_post`, and `probe_post2` may be recovery/post-window evidence when allowed by the NET run_window / recovery timing policy
- subtype boundary clean
- probes executed correctly
- GT / OBS preserved

### DROP

Use if:

- injector failed
- fault effect not observed
- probes broken
- required files missing
- required fault/main evidence outside run window, except recovery/post-window evidence allowed by the fault-scoped policy
- subtype boundary invalid
- data cannot be trusted

### UNCERTAIN

Use if:

- evidence exists but is weak
- supporting evidence missing
- final baseline conflicts with in-window gate
- recovery partially observed
- subtype boundary ambiguous

### NOT_ACCEPTED

Use when the run may be diagnostically useful but should not enter training data.

Example:

  fault_observed=true
  recovery_observed=true
  recovery_gate_ok=false

This may be NOT_ACCEPTED even if post-run baseline later passes.

---

## 9) Optional L1 / L2 follow-up

Only continue to L1 / L2 if:

- run is KEEP or explicitly approved for export
- GT / OBS separation is preserved
- run window evidence is valid
- fault-specific validation passes or acceptable status is documented

If continuing, use:

  dataset-l1-l2-validation

Check:

- canonical_case.json
- evidence_candidates.jsonl
- diagnosis.jsonl
- evidence_extraction.jsonl
- cause_vs_symptom.jsonl
- action_after_diagnosis.jsonl

Run validators where available.

---

## 10) Output format

Use this final format:

  RESULT: KEEP / DROP / UNCERTAIN / NOT_ACCEPTED

  Run:
  - run_id:
  - subtype:
  - path:

  Baseline:
  - gateway:
  - IP:
  - DNS:

  Outcome:
  - inject_ok:
  - fault_observed:
  - recovery_observed:
  - recovery_gate_ok:
  - recovery_gate_reason:

  Evidence:
  - primary:
  - secondary:
  - negative:
  - probe execution:

  Decision reason:
  - ...

  L1/L2:
  - export:
  - validation:
  - recommendation:

  Next fix:
  - ...

Keep the output concise.

---

## 11) Repair / rerun policy

If collection fails:

1. Identify whether the failure is environment, injector, probe, script, or validation.
2. Do not modify code unless the failure is clearly script-related.
3. Prefer rerun only after a concrete fix.
4. Do not collect repeated runs blindly.
5. Stop after repair depth 3.

Common next fixes:

- fix board network baseline
- adjust probe target
- fix PowerShell quoting
- fix missing marker
- fix run window alignment
- rerun with explicit WK_NET_PING_IP
- rerun post2 probe
