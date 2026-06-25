# NET-Only Non-Action Diagnostic Output Contract v1

This contract freezes the target JSON output shape for NET-only non-action
diagnostic evaluation. It is derived from the existing held-out `test.jsonl`
assistant targets and the Task 7B oracle/rule-based sanity checks. It is not a
new label design and it must remain compatible with the existing gold data.

## Scope

- Family: `net`
- Tasks: `diagnosis`, `evidence_extraction`, `cause_vs_symptom`
- Excluded task: `action_after_diagnosis`
- Excluded families: CPU/MEM as GT labels
- Output mode: JSON object only

The evaluator must report JSON parse success separately from target schema
compliance and semantic correctness.

## Diagnosis

Required fields:

- `diagnosis_summary`: string
- `gt`: object
- `gt.family`: string, exactly `net`
- `gt.subtype`: one of the seven approved NET subtypes
- `gt_obs_separated`: boolean, exactly `true`

Optional observed gold fields inside `gt` include `confidence`,
`is_anomaly`, `run_kind`, and `severity`.

Forbidden top-level fields include `diagnosis`, `obs`, `input`, `case_id`,
`evidence`, `primary_family`, `primary_subtype`, `cause_eids`, and
`symptom_eids`.

Semantic correctness requires `gt.family` and `gt.subtype` to match the gold
target. OBS symptoms may be discussed in `diagnosis_summary`, but OBS must not
replace the GT label.

## Evidence Extraction

Required fields:

- `primary_evidence`: array of evidence items
- `secondary_evidence`: array of evidence items
- `symptom_evidence`: array of evidence items
- `noise_evidence`: array of evidence items

Each evidence item must at least carry `eid`. Existing gold evidence items may
also include `kind`, `score`, `source`, `source_rel`, `span`, `support_role`,
`text`, and `ts`.

Forbidden top-level fields include `diagnosis`, `gt`, `gt_obs_separated`,
`obs`, `input`, `case_id`, `primary_family`, `primary_subtype`, `cause_eids`,
`symptom_eids`, and a generic `evidence` wrapper.

Semantic correctness is measured by evidence EID precision/recall/F1 and by
role-array preservation.

## Cause Vs Symptom

Required fields:

- `primary_family`: string, exactly `net`
- `primary_subtype`: one of the seven approved NET subtypes
- `cause_eids`: array of strings
- `symptom_eids`: array of strings
- `gt_obs_separated`: boolean, exactly `true`

Optional observed gold fields include `judgement_text` and
`primary_processes`.

Forbidden top-level fields include `diagnosis`, `gt`, `obs`, `input`,
`case_id`, `evidence`, `primary_evidence`, `secondary_evidence`,
`symptom_evidence`, and `noise_evidence`.

Semantic correctness requires the primary cause family/subtype and cause/symptom
EID sets to match the gold target. Promoting `input.obs.primary_family` or
`input.obs.state` to the primary cause is a hard GT/OBS leak.

## Reporting Layers

The evaluation framework must report these layers independently:

- JSON parse success: prediction text yields a JSON object.
- Target schema compliance: required fields, types, allowed values, and
  forbidden-key checks pass.
- Semantic metric eligibility: target schema passes and no hard boundary leak is
  present.
- Semantic correctness: task-specific accuracy/F1 metrics.
- Hard GT/OBS leak: OBS fields are promoted into GT/cause fields.
- Soft GT/OBS risk: target shape lacks explicit GT/OBS separation.
- Action/recovery leak: prediction contains action, recovery, remediation, shell
  command, or fix instructions.
- CPU/MEM input contamination: CPU/MEM appears as GT family/task in the eval
  input.
- CPU/MEM prediction echo: prediction repeats CPU/MEM terms that are present
  only in OBS score/text fields.
- CPU/MEM metric false positive: detector hits evaluator metadata rather than
  model prediction/input.

## Approved NET Subtypes

- `net_dns_fail`
- `net_no_default_route`
- `net_no_ipv4_on_iface`
- `net_public_ip_unreachable`
- `net_wifi_auth_fail_wrong_psk`
- `net_wifi_disconnect`
- `net_wrong_default_route`

## Validation Command

The checker uses existing held-out test data, Task 7A predictions, and Task 7B
oracle/rule-based outputs. A representative remote invocation is:

```sh
cd /home/xrh/qwen3_os_fault
PYTHONDONTWRITEBYTECODE=1 python tools/eval_net_only_diagnostic_schema_contract_v1.py \
  --test-file /home/xrh/qwen3_os_fault/data/training_candidates/net_only_non_action_formal_20260518/test.jsonl \
  --predictions-7a /home/xrh/qwen3_os_fault/outputs/diagnostic_eval/net_only_diagnostic_eval_prototype_20260519_20260519_094402/predictions.jsonl \
  --calibration-7b-dir /home/xrh/qwen3_os_fault/outputs/diagnostic_eval/net_only_diagnostic_eval_calibration_20260519_102551 \
  --contract-json /home/xrh/qwen3_os_fault/tools/contracts/net_only_non_action_diagnostic_output_contract_v1.json \
  --output-dir /home/xrh/qwen3_os_fault/outputs/diagnostic_eval/net_only_schema_contract_v1_20260519_<timestamp>
```
