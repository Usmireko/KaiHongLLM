# Dataset Schema Spec

This document defines the current three-layer dataset stack in this workspace and matches the implementation in:

- `data_train_test/host/scripts/export_run_case_dataset_fixed_v2.ps1`
- `data_train_test/server/src/derive_l2_case_samples.py`
- `data_train_test/server/src/batch_derive_l2_samples.py`

## Layer Overview

### L0: Raw Run Bundle
- Purpose: Preserve original collection artifacts exactly as archived input.
- Typical contents: `_run_meta.json`, `metrics/`, `events/`, `procs/`, `fault_inject/`, `dmesg_*.log`, `hilog_text_full.log`, `faultlog/`.
- Rule: L0 is archival and should not be rewritten by downstream training preparation.

### L1: Canonical Case Export
- Purpose: Normalize one run into a stable case representation for downstream training.
- Producer: `export_run_case_dataset_fixed_v2.ps1`
- Main outputs:
  - `canonical_case.json`
  - `evidence_candidates.jsonl`
  - `quality_flags.json`
  - `source_manifest.json`
  - `training_views/diagnosis_input.txt`

### L2: Task-Specific Training Samples
- Purpose: Derive multiple supervised tasks from one L1 case without re-reading raw logs directly.
- Producer: `derive_l2_case_samples.py`
- Current tasks:
  - `diagnosis.jsonl`
  - `evidence_extraction.jsonl`
  - `cause_vs_symptom.jsonl`
  - `action_after_diagnosis.jsonl`

## L1 Canonical Case

### `canonical_case.json`

Core top-level fields:
- `schema_version`
- `source_type`
- `source_case_id`
- `case_id`
- `source`
- `gt`
- `obs`
- `timing`
- `modalities`
- `modality_mask`
- `quality_flags`
- `derived`
- `paths_rel`

### `canonical_case.source`

Stable provenance fields:
- `run_id`
- `collector_version`
- `scenario_tag`
- `fault_type`
- `device_sn_hash`

Rules:
- Host absolute paths must not appear here.
- Raw device serial number must not appear here.

### `canonical_case.gt`

Ground truth fields:
- `run_kind`
- `family`
- `subtype`
- `severity`
- `is_anomaly`
- `confidence`

Meaning:
- `gt` is the authoritative label derived from the controlled injection setup or trusted case annotation.
- `gt.family` and `gt.subtype` are the main training labels for causal tasks.

### `canonical_case.obs`

Observed interpretation fields:
- `state`
- `primary_family`
- `secondary_families`
- `fault_families`
- `warn_families`
- `confounders`
- `scores`

Meaning:
- `obs` is what the observed evidence suggests.
- `obs.primary_family` can differ from `gt.family`.
- This difference is intentional and should remain visible in diagnosis-oriented tasks.

### `canonical_case.derived`

Important derived subtrees:
- `metrics_summary`
- `event_summary`
- `injector_summary`
- `process_suspects`
- `dmesg_before_markers`
- `dmesg_after_markers`
- `hilog_markers`

#### `derived.process_suspects`

Typical fields:
- `exists`
- `snapshot_count`
- `ranking_basis`
- `suspects`

Each `suspects[]` row may contain:
- `comm`
- `max_rss_kb`
- `min_rss_kb`
- `rss_growth_kb`
- `seen_in_snapshots`
- `occurrence_count`
- `mem_pressure_refs`
- `cpu_hotspot_refs`
- `event_hit_count`
- `max_event_rss_kb`
- `first_event_ts`
- `last_event_ts`
- `injector_name_match`
- `ranking_score`
- `reasons`
- `pids`

## L1 Evidence Candidates

### `evidence_candidates.jsonl`

Each line is one candidate evidence item.

Required fields:
- `eid`
- `source`
- `kind`
- `text`

Common optional fields:
- `source_rel`
- `target`
- `support_role`
- `score`
- `ts`
- `span`

### `support_role` Enum

Allowed values:
- `primary`
- `symptom`
- `secondary`
- `noise`

Meaning:
- `primary`: direct cause-aligned evidence
- `symptom`: downstream effect evidence that should not be promoted to root cause by default
- `secondary`: weaker supporting evidence
- `noise`: historical, duplicated, or non-training-safe evidence

Rule:
- L2 reuses these labels. It does not invent a replacement evidence taxonomy.

## `quality_flags`

`quality_flags` marks data quality caveats rather than task labels.

Examples in current exports:
- `faultlog_all_is_historical_noise`
- `probe_dmesg_duplicates_present`
- `prefer_faultlog_new_over_faultlog_all`
- `injector_log_no_explicit_limit_reached_marker`

Usage:
- Keep them in L1 and pass them into diagnosis-oriented L2 tasks as context.
- Do not treat them as direct fault labels.

## GT vs OBS

### Ground Truth (`gt`)
- Controlled label or trusted case truth.
- Used in L2 targets for diagnosis and causal judgement.

### Observation (`obs`)
- What the telemetry appears to indicate before or during reasoning.
- Kept in L2 input blocks.

Rule:
- `gt` and `obs` must remain separated.
- A case may have `gt.family=mem` while `obs.primary_family=cpu`; this is valid and useful.

## L2 Task Boundaries

All L2 records currently include:
- `task_version`
- `validation_status`
- `schema_version`
- `sample_type`
- `sample_id`
- `case_id`
- `source_case_id`
- `source_type`
- `input`
- `target`

### 1. `diagnosis.jsonl`

Goal:
- Predict diagnosis result from observed evidence.

Input should contain:
- `obs`
- `modality_mask`
- `quality_flags`
- `top_processes`
- `key_evidence`
- `symptom_evidence`

Target should contain:
- `gt`
- `diagnosis_summary`
- `gt_obs_separated`

Boundary:
- `gt` must not be in `input`.

### 2. `evidence_extraction.jsonl`

Goal:
- Reorganize candidate evidence into `primary/symptom/secondary/noise`.

Input should contain:
- `obs`
- `candidate_evidence`

Target should contain:
- `primary_evidence`
- `symptom_evidence`
- `secondary_evidence`
- `noise_evidence`

Boundary:
- Reuse L1 evidence labels.
- Do not invent new `support_role` enums here.

### 3. `cause_vs_symptom.jsonl`

Goal:
- Teach the model to keep root cause and symptoms separate.

Input should contain:
- `obs`
- `top_processes`
- `primary_evidence`
- `symptom_evidence`

Target should contain:
- `primary_family`
- `primary_subtype`
- `primary_processes`
- `cause_eids`
- `symptom_eids`
- `judgement_text`
- `gt_obs_separated`

Semantic rule:
- For MEM cases, keep `cpu_hotspot` as symptom when the main cause is memory pressure or leak behavior.

### 4. `action_after_diagnosis.jsonl`

Goal:
- Generate safe actions after a diagnosis result already exists.

Why it is diagnosis-conditioned:
- It is not predicting actions directly from raw observations.
- Its input may include:
  - `diagnosis_result`
  - `gt.family`
  - `gt.subtype`
  - ranked suspects
  - primary and symptom evidence
- This task assumes a prior diagnostic stage has already summarized the case.

Input should contain:
- `task_condition=diagnosis_conditioned`
- `diagnosis_result`
- `gt`
- `obs`
- `top_processes`
- `primary_evidence`
- `symptom_evidence`

Target should contain:
- `policy`
- `recommended_actions`

Safety rule:
- `policy.direct_commands_allowed` must be `false`
- `policy.destructive_actions_allowed` must be `false`
- Output should recommend safe action categories, not shell commands

## Public Dataset Adaptation

Target for public dataset adapters:
- `canonical_case.json`
- `evidence_candidates.jsonl`

Extra provenance fields expected for public data:
- `source.source_dataset`
- `source_case_id`
- `source_type`
- `gt.confidence`

Missing-modality strategy:
- Set corresponding `modalities` and `modality_mask` fields to `false`
- Keep missing subtrees empty but structurally valid
- Prefer explicit downgrade over fabricating evidence

## Validation Principles

Current L2 validation checks:
- required fields exist
- `sample_id` is unique
- `support_role` is one of `primary/symptom/secondary/noise`
- action task policy is safe
- GT/OBS boundaries stay separated
- no host path leakage
- no raw SN leakage
- no PowerShell dictionary pollution fields in evidence

If validation fails:
- the batch or single-case derivation should print a clear error
- the process should exit non-zero
