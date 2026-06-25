---
name: stage2_server_diagnosis_narrative_fix
description: >
  Patch server-side diagnosis generation to be human-readable and evidence-aware:
  - Produce narrative with Observation/Hypothesis/Evidence/NextChecks
  - suspects cpu/score must be NA when evidence missing (never 0 by default)
  - Keep structured suspects for downstream automation
  Must reuse qwen3_server_runbook for canonical paths and venv.
---

# Deliverables
- Identify where PRIMARY_SUSPECT/SECONDARY/DIAGNOSIS are generated.
- Modify diagnosis.json schema:
  - narrative (cn)
  - observations[], hypothesis, evidence[], next_checks[]
  - suspects[] with evidence_missing[] and cpu_pct/score nullable
- Update console/report renderer to prefer narrative.

# NA policy
- If pidstat/top evidence does NOT include pid:
  - cpu_pct = null
  - score = null
  - evidence_ok=false
  - evidence_missing includes "pidstat"
- Display must show "NA", never "0.0%" or "0".

# Acceptance
- On an existing run_dir, regenerate diagnosis:
  - narrative contains 4 sections
  - suspects show NA when missing
  - no misleading 0/0.0 defaults
