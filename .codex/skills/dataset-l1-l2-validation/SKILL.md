---
name: dataset-l1-l2-validation
description: Use this skill for L0/L1/L2 dataset export, derivation, validation, GT/OBS separation, evidence extraction, and training-sample preparation.
---

# Dataset L1/L2 Validation Skill

Use this skill for tasks involving:

- L0 raw run bundles
- L1 canonical_case.json
- L1 evidence_candidates.jsonl
- L2 training samples
- diagnosis samples
- evidence extraction samples
- cause-vs-symptom samples
- action-after-diagnosis samples
- schema validation
- GT / OBS separation
- public dataset conversion
- accepted / rejected training cases

Always follow global AGENTS.md.

For NET cases, also follow:

  faults/net/AGENTS.md

---

## 1) Dataset layers

The dataset uses three layers.

### L0: raw archive layer

L0 contains original run bundle data.

Do not rewrite raw evidence.

Examples:

- _run_meta.json
- _net_outcome.json
- probe_fault.txt
- probe_post.txt
- logs
- metrics
- snapshots
- injector markers
- collector outputs

### L1: canonical normalized case layer

L1 contains normalized per-case artifacts.

Important files:

- canonical_case.json
- evidence_candidates.jsonl

L1 should preserve:

- GT
- OBS
- evidence roles
- modality metadata
- timing
- run_id
- source case identity
- validation status

### L2: task sample layer

L2 contains task-specific training examples.

Typical files:

- diagnosis.jsonl
- evidence_extraction.jsonl
- cause_vs_symptom.jsonl
- action_after_diagnosis.jsonl

L2 must be derived from L1, not directly improvised from raw logs.

---

## 2) GT / OBS separation

GT means:

- intended injected fault
- authoritative label
- family
- subtype
- known root cause when available

OBS means:

- probes
- logs
- symptoms
- counters
- derived observations
- recovery results
- noisy side effects

Rules:

- OBS must never replace GT.
- OBS may support, weaken, or contradict GT.
- Contradiction should be represented explicitly.
- Do not relabel a case silently.
- Do not promote secondary symptoms into primary root cause.

For hard cases:

- CPU hotspots may be symptoms.
- Memory pressure may be root cause.
- DNS failure may be secondary to link/gateway failure.
- Recovery gate failure may block acceptance even when some recovery is observed.

---

## 3) L1 canonical case requirements

A valid L1 canonical_case.json should include or preserve:

- case ID
- source run ID
- source paths
- GT family
- GT subtype
- observed symptoms
- evidence summary
- validation status
- run window
- fault window if available
- recovery observations if available
- modality list
- derived fields where appropriate

Do not erase uncertain or conflicting evidence.

Use explicit status rather than silent correction.

Recommended status values:

- PASS
- UNCERTAIN
- FAIL
- not_accepted
- accepted
- needs_review

Use the project’s actual schema when it differs.

---

## 4) Evidence candidates

evidence_candidates.jsonl should distinguish evidence roles.

Recommended roles:

- primary
- secondary
- context
- negative

### Primary evidence

Directly supports GT subtype.

Example:

- DNS host probe failed while IP probe remained OK for net_dns_fail.

### Secondary evidence

Relevant but downstream or concurrent.

Example:

- DNS failed during link_down.

### Context evidence

Environment or baseline.

Example:

- route table before injection
- interface used
- gateway address

### Negative evidence

Evidence against the claimed subtype or acceptance.

Example:

- IP ping succeeded throughout an alleged full link_down.

Do not mark arbitrary log lines as primary evidence merely because they contain suspicious keywords.

---

## 5) L2 task sample policy

L2 samples should preserve the task goal.

### Diagnosis samples

Should teach:

- root cause identification
- subtype classification
- evidence-grounded reasoning
- uncertainty when evidence is weak

### Evidence extraction samples

Should teach:

- extracting relevant evidence
- ignoring noise
- assigning support roles
- locating evidence in artifacts

### Cause-vs-symptom samples

Should teach:

- primary cause vs secondary symptom
- GT vs OBS
- noisy side effects
- difficult cases like memory pressure and network cascades

### Action-after-diagnosis samples

Should teach:

- controlled recovery suggestions
- safe next steps
- non-destructive actions first
- human confirmation when risk is high

Do not let action samples become unsafe automated remediation instructions unless the project explicitly supports that execution path.

---

## 6) Validation order

Use this order:

1. Check raw run bundle exists.
2. Check _run_meta.json.
3. Check run window.
4. Check fault-specific outcome file if available.
5. Check common validation gate.
6. Check fault-specific validator.
7. Export or inspect L1.
8. Validate L1 schema.
9. Derive L2.
10. Validate L2 schema.
11. Review GT / OBS consistency.
12. Decide accepted / uncertain / drop.

Do not skip common gate.

Do not continue to accepted L2 training if the run is invalid for training.

---

## 7) NET-specific dataset rules

For NET cases:

- preserve gt.family = net
- preserve GT subtype
- preserve _net_outcome.json fields where relevant
- include fault_observed
- include recovery_observed
- include recovery_gate_ok
- include recovery_gate_reason
- preserve subtype boundary evidence
- represent secondary DNS symptoms as secondary unless GT supports primary DNS failure

Important distinction:

  fault_observed=true
  recovery_observed=true
  recovery_gate_ok=false

This should not be flattened to recovered.

It means:

- some recovery evidence exists
- final gate did not pass
- acceptance may be blocked
- the reason must be preserved

---

## 8) Validation result policy

Use:

- PASS
- UNCERTAIN
- FAIL

### PASS

Use only when:

- common gate passes
- fault-specific validation passes
- required fault/main evidence is inside `_run_meta.json.run_window`
- recovery/post-window evidence is allowed only when an explicit fault-scoped policy defines that timing split and its acceptance gates
- GT / OBS separation is preserved
- schema validation passes
- L2 samples are consistent with L1

### UNCERTAIN

Use when:

- partial evidence exists
- subtype boundary is ambiguous
- recovery evidence conflicts with gate
- supporting evidence is weak
- case may be useful for debugging but not training

### FAIL

Use when:

- required files missing
- probe execution broken
- required fault/main evidence outside run window, except recovery/post-window evidence allowed by an explicit fault-scoped policy
- schema invalid
- GT / OBS violated
- required fault effect missing
- acceptance would require evidence gaming

---

## 9) Common review output

Use this format:

  RESULT: PASS / UNCERTAIN / FAIL
  Layer reviewed: L0 / L1 / L2
  GT:
  OBS:
  Evidence chain:
  Schema:
  Run window:
  Validation commands:
  Failed criteria:
  Warnings:
  Training acceptance:
  Next fix:

---

## 10) Repair policy

When validation fails:

1. Identify the layer:
   - L0
   - L1
   - L2
   - schema
   - validator
2. Identify exact failure.
3. Form one hypothesis.
4. Make the smallest fix.
5. Re-run the same validation.
6. Stop after repair depth 3.

Do not fix dataset failures by altering GT to match OBS.

Do not add fake evidence to pass validators.
