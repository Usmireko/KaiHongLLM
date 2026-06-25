---
name: board-net-review-run
description: Use this skill to review an already collected NET run, decide keep/drop/uncertain/not accepted, and optionally continue to L1/L2.
---

# Board NET Review Run Skill

Use this skill when the run already exists and the task is to review whether it is valid, accepted, uncertain, or should be dropped.

This skill does not perform new collection by default.

Always follow:

- global AGENTS.md
- faults/net/AGENTS.md
- dataset-l1-l2-validation skill when reviewing L1/L2

---

## 1) When to use this skill

Use for tasks like:

- review this NET run
- decide whether this run can enter training data
- inspect _net_outcome.json
- check recovery gate
- inspect probe epochs
- check L1/L2 after export
- explain why a run is not accepted
- determine whether rerun is needed

Do not use this skill for starting new board collection.

For new collection, use:

  board-net-collect-and-analyze

---

## 2) Required review loop

Use this loop:

  Locate -> Read metadata -> Read outcome -> Read probes -> Check timing -> Decide -> Optional L1/L2 -> Final

Do not decide from one field alone.

---

## 3) Locate run

Identify:

- run_id
- run path
- fault family
- fault subtype
- available artifacts

Required or expected artifacts:

- _run_meta.json
- _net_outcome.json
- probe_fault.txt
- probe_post.txt
- probe_post2.txt

If any required artifact is missing, report it.

Do not silently ignore missing files.

---

## 4) Read _run_meta.json

Check:

- run_id
- gt.family
- gt.subtype
- fault_type
- run_window
- start time
- end time
- parameters
- collector version if available

Validate:

- run window exists
- run window is plausible
- GT family/subtype are clear
- evidence timestamps can be compared to the run window

If run window is missing or invalid, mark the run FAIL or not accepted for training.

---

## 5) Read _net_outcome.json

Check:

- net_fault_type
- iface_used
- inject_ok
- fault_observed
- recovery_observed
- fault_observation_reason
- recovery_observation_reason
- recovery_gate_ok
- recovery_gate_reason

Important rule:

Do not collapse:

  recovery_observed

and:

  recovery_gate_ok

They mean different things.

A run with:

  fault_observed=true
  recovery_observed=true
  recovery_gate_ok=false

should be treated as:

  fault/recovery chain has evidence,
  but final acceptance gate failed

This usually means NOT_ACCEPTED unless a manual rule says otherwise.

---

## 6) Read probe files

Review probe files by phase.

### Fault phase

Possible files:

- probe_fault.txt

Questions:

- Did the fault effect appear?
- Did the probe execute correctly?
- Is the probe inside the run window?
- Does the effect match GT subtype?

### Post phase

Possible files:

- probe_post.txt

Questions:

- Did recovery begin?
- Did DNS/IP/gateway recover?
- Did only some layers recover?
- Is recovery inside run window, or is it allowed recovery/post-window evidence under the fault-scoped policy?

### Post2 / delayed phase

Possible files:

- probe_post2.txt

Questions:

- Did delayed recovery appear?
- Does it explain recovery gate discrepancy?
- Is it inside or outside the run window?
- Should it be treated as training evidence or diagnostic-only evidence?

---

## 7) Probe execution quality

Check for broken probe indicators:

- /bin/sh:
- syntax error
- unmatched
- inaccessible
- Unknown command
- not found

If present:

- do not treat that probe as valid evidence
- mark validation FAIL if required evidence depends on that probe
- recommend fixing quoting or board command compatibility

---

## 8) Run window alignment

Default rule: fault-effect training evidence must be inside _run_meta.json run window, with small tolerance if appropriate.

Fault-scoped policy may define a recovery/post-window. For NET non-DNS runs, `faults/net/AGENT.md` defines `_run_meta.json.run_window` as the fault/main collection window and allows `recovery_gate`, `probe_post`, and `probe_post2` after `run_window_end` when the documented NET acceptance gates and evidence-consistency checks pass.

If a post-run baseline passes outside the run window and no fault-scoped recovery/post-window policy applies:

- report it
- do not use it as a replacement for in-window recovery gate
- use it as diagnostic context only unless explicitly approved

If the NET non-DNS recovery/post-window policy applies:

- record post/post2 as recovery/post-window evidence
- require `recovery_gate_ok=true`
- require the post baseline and residual cleanup gates to pass
- verify the evidence remains consistent with the GT subtype

---

## 9) Subtype boundary review

Follow faults/net/AGENTS.md.

Review whether primary evidence supports GT subtype.

Examples:

### DNS

Primary only if:

  DNS host probe fails while IP probe remains OK

### Link down

Primary if:

  interface/link/path is unavailable

Do not misclassify as flap without transition.

### Link flap

Requires:

  down-to-up transition

### Gateway unreachable

Primary if:

  gateway fails while link/interface may remain up

### Auth fail

Review authentication/access-control symptoms and recovery gate separately.

Do not relabel to DNS merely because DNS also failed.

---

## 10) Decision policy

Use one validation result:

- PASS
- UNCERTAIN
- FAIL

And one operational decision:

- KEEP
- DROP
- NOT_ACCEPTED
- NEEDS_RERUN

### PASS + KEEP

Use only if:

- GT clear
- injection succeeded
- fault observed
- recovery observed if required
- recovery gate passes or explicit exception exists
- required fault/main evidence is inside `_run_meta.json.run_window`
- recovery gate, `probe_post`, and `probe_post2` may be recovery/post-window evidence when allowed by the NET run_window / recovery timing policy
- subtype boundary clean
- probe execution valid

### UNCERTAIN

Use if:

- evidence exists but is incomplete
- supporting evidence weak
- recovery gate conflicts with baseline
- subtype boundary not fully clean
- useful for debugging but not training

### FAIL + DROP

Use if:

- required files missing
- probes broken
- fault not observed
- required fault/main evidence outside run window, except recovery/post-window evidence allowed by the fault-scoped policy
- subtype invalid
- GT / OBS would be conflated

### NOT_ACCEPTED

Use if:

- the run is diagnostically meaningful
- but blocked from training acceptance

Common case:

  fault_observed=true
  recovery_observed=true
  recovery_gate_ok=false

---

## 11) L1 / L2 review

If L1/L2 artifacts exist, review:

- canonical_case.json
- evidence_candidates.jsonl
- diagnosis.jsonl
- evidence_extraction.jsonl
- cause_vs_symptom.jsonl
- action_after_diagnosis.jsonl

Use dataset-l1-l2-validation.

Check:

- GT preserved
- OBS preserved
- recovery gate preserved
- evidence roles correct
- primary evidence supports GT
- secondary symptoms not promoted
- validators pass

Do not mark L1/L2 valid if underlying run is invalid for training unless explicitly marked diagnostic-only.

---

## 12) Output format

Use this format:

  RESULT: PASS / UNCERTAIN / FAIL
  Decision: KEEP / DROP / NOT_ACCEPTED / NEEDS_RERUN

  Run:
  - run_id:
  - path:
  - GT family:
  - GT subtype:

  Outcome:
  - net_fault_type:
  - inject_ok:
  - fault_observed:
  - recovery_observed:
  - recovery_gate_ok:
  - recovery_gate_reason:

  Timing:
  - run_window:
  - fault probe:
  - post probe:
  - post2 probe:

  Evidence:
  - primary:
  - secondary:
  - negative:
  - probe execution:

  Subtype boundary:
  - clean / ambiguous / invalid
  - reason:

  L1/L2:
  - present:
  - validation:
  - recommendation:

  Failed criteria:
  - ...

  Warnings:
  - ...

  Next fix:
  - ...

---

## 13) Example interpretation: recovery gate failure

Case:

  fault_observed=true
  recovery_observed=true
  recovery_gate_ok=false
  recovery_gate_reason=ping_ip_fail
  post-run baseline=PASS

Interpretation:

  The injection and recovery chain produced evidence, but the in-window recovery gate failed.
  The later baseline suggests the environment eventually recovered.
  This does not automatically make the run accepted for training.

Recommended decision:

  RESULT: UNCERTAIN or FAIL depending on required gate
  Decision: NOT_ACCEPTED
  Next fix: rerun with explicit ping target and inspect post2 probe epoch

Do not say the injector failed unless inject_ok=false or injector markers support that conclusion.

---

## 14) Repair policy

This skill is review-first.

Do not patch code unless explicitly asked.

If asked to fix a run-processing issue:

1. Use os-fault-engineer.
2. Identify exact failure.
3. Patch minimally.
4. Validate same run.
5. Stop after repair depth 3.
