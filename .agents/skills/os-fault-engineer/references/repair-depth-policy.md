# Repair Depth Policy

Recursive repair is allowed, but maximum repair depth is 3.

This policy applies to:
- code implementation
- script fixes
- validator fixes
- dataset pipeline fixes
- hdc / PowerShell command fixes
- NET run processing fixes

---

## 1) Depth definition

Depth 0:
- initial implementation

Depth 1:
- first targeted repair after validation failure or reviewer finding

Depth 2:
- second targeted repair after validation failure or reviewer finding

Depth 3:
- final targeted repair after validation failure or reviewer finding

After Depth 3:
- stop
- do not keep modifying
- report unresolved failure and next manual check

---

## 2) Required format at each repair depth

At each repair depth, output:

  [REPAIR_DEPTH_N]
  - Trigger:
  - Failed command / review finding:
  - Hypothesis:
  - Files to touch:
  - Patch summary:
  - Re-validation:
  - Result:

N must be one of:
- 1
- 2
- 3

---

## 3) Repair rules

Each repair depth must:
1. Address one concrete failure or review finding.
2. State one hypothesis before editing.
3. Modify only files relevant to that hypothesis.
4. Re-run the same validation if possible.
5. Avoid broad rewrites.
6. Avoid changing unrelated behavior.
7. Preserve GT / OBS separation.
8. Preserve recovery gate semantics.
9. Preserve board command compatibility.
10. Preserve naming constraints.

Do not use repair loops to:
- rewrite the solution from scratch
- chase unrelated warnings
- hide validation failures
- add fake evidence
- make validators pass without semantic evidence
- relabel GT to match OBS
- create numbered file variants

---

## 4) Stop condition

If the issue remains unresolved after Depth 3, output:

  [STOP_AFTER_MAX_REPAIR_DEPTH]
  - Max depth: 3
  - Unresolved failure:
  - Last command:
  - Last error:
  - Files touched:
  - What was tried:
  - Likely root cause:
  - Recommended manual check:

Then stop.

Do not continue to Depth 4.

---

## 5) Validation failure examples

Example 1: dataset validator failure

  [REPAIR_DEPTH_1]
  - Trigger: validate_dataset_artifacts.py failed
  - Failed command / review finding: missing recovery_gate_reason
  - Hypothesis: exporter merges recovery_gate_ok but drops recovery_gate_reason
  - Files to touch: export script only
  - Patch summary: preserve recovery_gate_reason during merge
  - Re-validation: rerun same validator
  - Result: PASS

Example 2: hdc command failure

  [REPAIR_DEPTH_1]
  - Trigger: probe command produced /bin/sh: syntax error
  - Failed command / review finding: hdc shell command used fragile quoting
  - Hypothesis: PowerShell expanded a token before sending to board
  - Files to touch: PowerShell collector only
  - Patch summary: split remote command into shorter hdc shell calls
  - Re-validation: rerun probe command
  - Result: PASS

Example 3: reviewer finding

  [REPAIR_DEPTH_1]
  - Trigger: project-reviewer found missed L2 consumer
  - Failed command / review finding: new L1 field not propagated to L2 cause_vs_symptom sample
  - Hypothesis: derive_l2_case_samples.py reads old field list
  - Files to touch: L2 derivation script and validator fixture
  - Patch summary: add compatible read path with fallback
  - Re-validation: rerun L2 derivation and validator
  - Result: PASS

---

## 6) Manual handoff after failed Depth 3

If Depth 3 fails, provide a manual handoff that is immediately actionable.

Include:
- exact command to rerun
- exact file and line area to inspect
- the most likely state or environment dependency
- whether the run should be dropped, rerun, or kept for diagnostics
- whether data should enter training

Do not claim success when validation is still failing.