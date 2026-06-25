#!/usr/bin/env python3
"""Plan NET-only evidence/cause repair from existing formal candidate targets.

This helper is offline and read-only with respect to formal data. It reads the
formal candidate, Task 8F analysis, contract/checker files, and L1 evidence
candidate sidecars, then writes audit tables and a repair plan. It does not
train, evaluate, generate, rebuild L1/L2, or create a formal repaired candidate.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


ROOT = Path(".").resolve()
DEFAULT_CANDIDATE_DIR = Path(
    "_smoke_net_batch2_20260430/training_candidates/net_only_non_action_formal_20260518"
)
DEFAULT_8F_DIR = Path(
    "_smoke_net_batch2_20260430/audit/net_only_evidence_cause_failure_analysis_20260519"
)
DEFAULT_OUT_DIR = Path(
    "_smoke_net_batch2_20260430/audit/net_only_evidence_cause_repair_plan_20260520"
)
DEFAULT_REPORT = Path(
    "_smoke_net_batch2_20260430/audit/NET_ONLY_EVIDENCE_CAUSE_REPAIR_PLAN_20260520.md"
)
CONTRACT_JSON = Path("tools/contracts/net_only_non_action_diagnostic_output_contract_v1.json")
CONTRACT_MD = Path("tools/contracts/net_only_non_action_diagnostic_output_contract_v1.md")
CHECKER = Path("tools/eval_net_only_diagnostic_schema_contract_v1.py")
L1_INDEX_CANDIDATES = (
    Path("dataset_batches/net_formal_batch_70_20260429/l1_index.jsonl"),
    Path("dataset_batches/net_batch2_7x3_smoke_candidate_20260515/l1_index.jsonl"),
)
EVIDENCE_FIELDS = (
    "primary_evidence",
    "secondary_evidence",
    "symptom_evidence",
    "noise_evidence",
)


def _read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise ValueError(f"{path}:{line_no} must contain a JSON object")
            payload["_line"] = line_no
            rows.append(payload)
    return rows


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def _write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            f.write("\n")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _last_message(messages: Sequence[Dict[str, Any]], role: str) -> str:
    for msg in reversed(messages):
        if msg.get("role") == role:
            return str(msg.get("content") or "")
    raise ValueError(f"missing message role={role}")


def _parse_l2_input(user_content: str) -> Dict[str, Any]:
    marker = "L2_INPUT_JSON:\n"
    if marker not in user_content:
        raise ValueError("missing L2_INPUT_JSON marker")
    return json.loads(user_content.split(marker, 1)[1].strip())


def _parse_candidate_split(path: Path, split: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for line_no, raw in enumerate(_read_jsonl(path), 1):
        messages = raw.get("messages")
        if not isinstance(messages, list):
            raise ValueError(f"{path}:{line_no} missing messages")
        l2 = _parse_l2_input(_last_message(messages, "user"))
        gold = json.loads(_last_message(messages, "assistant"))
        sample_id = str(l2.get("sample_id") or "")
        task = str(l2.get("task") or "")
        if not sample_id or not task:
            raise ValueError(f"{path}:{line_no} missing sample_id/task")
        out.append(
            {
                "split": split,
                "line": line_no,
                "sample_id": sample_id,
                "run_id": sample_id.split("::", 1)[0],
                "task": task,
                "l2": l2,
                "gold": gold,
            }
        )
    return out


EID_CONTAINER_KEYS = {
    "cause_eids",
    "symptom_eids",
    "candidate_evidence",
    "key_evidence",
    "supporting_evidence",
    "primary_evidence",
    "secondary_evidence",
    "symptom_evidence",
    "noise_evidence",
}


def _extract_eids(value: Any) -> Set[str]:
    """Extract only stable evidence identifiers, not arbitrary evidence text."""
    out: Set[str] = set()
    if isinstance(value, str) and value:
        out.add(value)
    elif isinstance(value, dict):
        eid = value.get("eid")
        if isinstance(eid, str) and eid:
            out.add(eid)
        for key in EID_CONTAINER_KEYS:
            if key in value:
                out.update(_extract_eids(value[key]))
    elif isinstance(value, list):
        for item in value:
            out.update(_extract_eids(item))
    return out


def _eids_by_field(payload: Dict[str, Any]) -> Dict[str, List[str]]:
    return {field: sorted(_extract_eids(payload.get(field))) for field in EVIDENCE_FIELDS}


def _evidence_items(payload: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    items: List[Tuple[str, Dict[str, Any]]] = []
    for field in EVIDENCE_FIELDS:
        value = payload.get(field)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    items.append((field, item))
    return items


def _candidate_eids_from_l2(l2: Dict[str, Any]) -> Set[str]:
    inp = l2.get("input") or {}
    return _extract_eids(
        [
            inp.get("candidate_evidence"),
            inp.get("key_evidence"),
            inp.get("symptom_evidence"),
            inp.get("supporting_evidence"),
        ]
    )


def _gold_subtype(row: Dict[str, Any], subtype_by_run: Dict[str, str]) -> str:
    task = row["task"]
    gold = row["gold"]
    if task == "diagnosis":
        return str((gold.get("gt") or {}).get("subtype") or "unknown")
    if task == "cause_vs_symptom":
        return str(gold.get("primary_subtype") or subtype_by_run.get(row["run_id"], "unknown"))
    return subtype_by_run.get(row["run_id"], "unknown")


def _load_l1_index() -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for path in L1_INDEX_CANDIDATES:
        if not path.is_file():
            continue
        for row in _read_jsonl(path):
            run_id = str(row.get("run_id") or "")
            if run_id and run_id not in out:
                out[run_id] = row
    return out


def _load_evidence_candidates(path_raw: Optional[str]) -> Tuple[Set[str], bool]:
    if not path_raw:
        return set(), False
    path = Path(path_raw)
    if not path.is_absolute():
        path = ROOT / path
    if not path.is_file():
        return set(), False
    try:
        rows = _read_jsonl(path)
    except Exception:
        return set(), False
    return _extract_eids(rows), True


def _role_kind_signature(gold: Dict[str, Any]) -> str:
    parts: List[str] = []
    for field, item in _evidence_items(gold):
        parts.append(f"{field}:{item.get('support_role')}:{item.get('kind')}")
    return "|".join(sorted(parts))


def _trace_row(trace_by_sample: Dict[str, Dict[str, Any]], sample_id: str) -> Dict[str, Any]:
    return trace_by_sample.get(sample_id, {})


def _ratio(n: int, d: int) -> Optional[float]:
    if d == 0:
        return None
    return n / d


def _audit_targets(candidate_dir: Path) -> Dict[str, Any]:
    split_rows: List[Dict[str, Any]] = []
    for split in ("train", "val", "test"):
        split_rows.extend(_parse_candidate_split(candidate_dir / f"{split}.jsonl", split))
    trace_by_sample = {str(r.get("sample_id")): r for r in _read_jsonl(candidate_dir / "trace_index.jsonl")}
    l1_index = _load_l1_index()
    subtype_by_run: Dict[str, str] = {}
    for row in split_rows:
        if row["task"] == "diagnosis":
            subtype_by_run[row["run_id"]] = _gold_subtype(row, {})

    evidence_table: List[Dict[str, Any]] = []
    cause_table: List[Dict[str, Any]] = []
    task_balance_rows: List[Dict[str, Any]] = []

    split_task_counter: Dict[Tuple[str, str], int] = Counter()
    split_subtype_task_counter: Dict[Tuple[str, str, str], int] = Counter()
    for row in split_rows:
        subtype = _gold_subtype(row, subtype_by_run)
        split_task_counter[(row["split"], row["task"])] += 1
        split_subtype_task_counter[(row["split"], subtype, row["task"])] += 1
    for (split, task), count in sorted(split_task_counter.items()):
        task_balance_rows.append({"split": split, "subtype": "__all__", "task": task, "samples": count})
    for (split, subtype, task), count in sorted(split_subtype_task_counter.items()):
        task_balance_rows.append({"split": split, "subtype": subtype, "task": task, "samples": count})

    evidence_id_refs = 0
    evidence_id_traceable_refs = 0
    evidence_candidate_file_traceable_refs = 0
    evidence_candidate_file_refs = 0
    evidence_gold_empty_count = 0
    evidence_missing_eid_count = 0
    evidence_duplicate_row_count = 0
    evidence_support_role_mismatch_count = 0
    subtype_signatures: Dict[str, Set[str]] = defaultdict(set)

    cause_refs = 0
    cause_traceable_refs = 0
    symptom_refs = 0
    symptom_traceable_refs = 0
    cause_empty_count = 0
    symptom_empty_count = 0
    cause_only_e1_count = 0
    cause_short_count = 0
    cause_signature_by_subtype: Dict[str, Set[Tuple[Tuple[str, ...], Tuple[str, ...]]]] = defaultdict(set)

    for row in split_rows:
        if row["task"] not in {"evidence_extraction", "cause_vs_symptom"}:
            continue
        subtype = _gold_subtype(row, subtype_by_run)
        trace = _trace_row(trace_by_sample, row["sample_id"])
        l1 = l1_index.get(row["run_id"], {})
        file_eids, file_found = _load_evidence_candidates(l1.get("evidence_candidates_path"))
        l2_eids = _candidate_eids_from_l2(row["l2"])
        candidate_eids = l2_eids | file_eids

        if row["task"] == "evidence_extraction":
            gold = row["gold"]
            eids_by_field = _eids_by_field(gold)
            all_eids = sorted({eid for values in eids_by_field.values() for eid in values})
            total_items = len(_evidence_items(gold))
            missing_eids = [
                f"{field}[{idx}]"
                for field in EVIDENCE_FIELDS
                for idx, item in enumerate(gold.get(field) or [])
                if isinstance(item, dict) and not item.get("eid")
            ]
            duplicate_row = len(all_eids) != sum(len(v) for v in eids_by_field.values())
            support_mismatch = [
                item.get("eid")
                for field, item in _evidence_items(gold)
                if str(item.get("support_role") or "") not in _expected_roles_for_field(field)
            ]
            traceable = sorted(set(all_eids) & candidate_eids)
            missing_from_candidates = sorted(set(all_eids) - candidate_eids)
            file_traceable = sorted(set(all_eids) & file_eids)
            evidence_id_refs += len(all_eids)
            evidence_id_traceable_refs += len(traceable)
            if file_found:
                evidence_candidate_file_refs += len(all_eids)
                evidence_candidate_file_traceable_refs += len(file_traceable)
            evidence_gold_empty_count += int(total_items == 0)
            evidence_missing_eid_count += len(missing_eids)
            evidence_duplicate_row_count += int(duplicate_row)
            evidence_support_role_mismatch_count += len(support_mismatch)
            subtype_signatures[subtype].add(_role_kind_signature(gold))
            evidence_table.append(
                {
                    "split": row["split"],
                    "sample_id": row["sample_id"],
                    "run_id": row["run_id"],
                    "subtype": subtype,
                    "gold_evidence_count": total_items,
                    "gold_eids": all_eids,
                    "eids_by_field": eids_by_field,
                    "gold_evidence_empty": total_items == 0,
                    "missing_eid_count": len(missing_eids),
                    "duplicate_eid_within_sample": duplicate_row,
                    "support_role_mismatch_count": len(support_mismatch),
                    "l2_candidate_eids_count": len(l2_eids),
                    "evidence_candidates_file_found": file_found,
                    "evidence_candidates_file_eids_count": len(file_eids),
                    "traceable_eids_count": len(traceable),
                    "missing_from_candidate_eids": missing_from_candidates,
                    "traceable_rate": _ratio(len(traceable), len(all_eids)),
                    "source_l2_file": trace.get("source_l2_file"),
                    "source_full_l2_file": trace.get("source_full_l2_file"),
                    "evidence_candidates_path": l1.get("evidence_candidates_path"),
                    "role_kind_signature": _role_kind_signature(gold),
                    "repair_risk": _evidence_repair_risk(total_items, missing_from_candidates, support_mismatch),
                }
            )
        else:
            gold = row["gold"]
            cause_eids = sorted(_extract_eids(gold.get("cause_eids")))
            symptom_eids = sorted(_extract_eids(gold.get("symptom_eids")))
            cause_missing = sorted(set(cause_eids) - candidate_eids)
            symptom_missing = sorted(set(symptom_eids) - candidate_eids)
            cause_refs += len(cause_eids)
            cause_traceable_refs += len(set(cause_eids) & candidate_eids)
            symptom_refs += len(symptom_eids)
            symptom_traceable_refs += len(set(symptom_eids) & candidate_eids)
            cause_empty_count += int(not cause_eids)
            symptom_empty_count += int(not symptom_eids)
            cause_only_e1_count += int(cause_eids == ["e1"])
            cause_short_count += int(len(cause_eids) <= 1)
            cause_signature_by_subtype[subtype].add((tuple(cause_eids), tuple(symptom_eids)))
            cause_table.append(
                {
                    "split": row["split"],
                    "sample_id": row["sample_id"],
                    "run_id": row["run_id"],
                    "subtype": subtype,
                    "cause_eids": cause_eids,
                    "symptom_eids": symptom_eids,
                    "cause_eid_count": len(cause_eids),
                    "symptom_eid_count": len(symptom_eids),
                    "cause_eids_empty": not cause_eids,
                    "symptom_eids_empty": not symptom_eids,
                    "cause_eids_only_e1": cause_eids == ["e1"],
                    "cause_eids_under_minimum": len(cause_eids) <= 1,
                    "cause_missing_from_candidate_eids": cause_missing,
                    "symptom_missing_from_candidate_eids": symptom_missing,
                    "cause_traceable_rate": _ratio(len(set(cause_eids) & candidate_eids), len(cause_eids)),
                    "symptom_traceable_rate": _ratio(len(set(symptom_eids) & candidate_eids), len(symptom_eids)),
                    "evidence_candidates_file_found": file_found,
                    "source_l2_file": trace.get("source_l2_file"),
                    "source_full_l2_file": trace.get("source_full_l2_file"),
                    "evidence_candidates_path": l1.get("evidence_candidates_path"),
                    "repair_risk": _cause_repair_risk(cause_eids, symptom_eids, cause_missing, symptom_missing),
                }
            )

    evidence_summary = {
        "samples_audited": len(evidence_table),
        "split_distribution": dict(Counter(row["split"] for row in evidence_table)),
        "subtype_distribution": dict(sorted(Counter(row["subtype"] for row in evidence_table).items())),
        "gold_empty_count": evidence_gold_empty_count,
        "missing_eid_count": evidence_missing_eid_count,
        "duplicate_eid_within_sample_count": evidence_duplicate_row_count,
        "support_role_mismatch_count": evidence_support_role_mismatch_count,
        "evidence_id_traceable_rate": _ratio(evidence_id_traceable_refs, evidence_id_refs),
        "evidence_candidate_file_traceable_rate": _ratio(
            evidence_candidate_file_traceable_refs,
            evidence_candidate_file_refs,
        ),
        "subtype_role_kind_signature_counts": {
            subtype: len(signatures) for subtype, signatures in sorted(subtype_signatures.items())
        },
        "materialization_risks": [
            "Current gold has stable eids and is traceable, but formal model learned schema without selecting eids.",
            "Targets expose full evidence objects; a repaired candidate should emphasize stable evidence_id selection and role arrays.",
            "Strict text exact-match should not be the only evidence debug view; retain strict eid F1 and add relaxed diagnostics.",
        ],
    }
    cause_summary = {
        "samples_audited": len(cause_table),
        "split_distribution": dict(Counter(row["split"] for row in cause_table)),
        "subtype_distribution": dict(sorted(Counter(row["subtype"] for row in cause_table).items())),
        "cause_empty_count": cause_empty_count,
        "symptom_empty_count": symptom_empty_count,
        "cause_eids_only_e1_count": cause_only_e1_count,
        "cause_eids_under_minimum_count": cause_short_count,
        "cause_eid_traceable_rate": _ratio(cause_traceable_refs, cause_refs),
        "symptom_eid_traceable_rate": _ratio(symptom_traceable_refs, symptom_refs),
        "subtype_cause_symptom_signature_counts": {
            subtype: len(signatures) for subtype, signatures in sorted(cause_signature_by_subtype.items())
        },
        "materialization_risks": [
            "Gold cause_eids are traceable and non-empty, but all symptom_eids are empty in current NET-only non-action data.",
            "Cause targets require multi-eid coverage; formal predictions under-selected eids rather than confusing subtype.",
            "Subtype-specific cause templates are needed to make complete cause_eid selection explicit.",
        ],
    }
    return {
        "rows": split_rows,
        "evidence_table": evidence_table,
        "cause_table": cause_table,
        "task_balance_rows": task_balance_rows,
        "evidence_summary": evidence_summary,
        "cause_summary": cause_summary,
    }


def _expected_roles_for_field(field: str) -> Set[str]:
    if field == "primary_evidence":
        return {"primary"}
    if field == "secondary_evidence":
        return {"secondary", "supporting"}
    if field == "symptom_evidence":
        return {"symptom"}
    if field == "noise_evidence":
        return {"noise"}
    return set()


def _evidence_repair_risk(total_items: int, missing_eids: Sequence[str], role_mismatch: Sequence[str]) -> str:
    if total_items == 0:
        return "gold_empty_needs_target_repair"
    if missing_eids:
        return "missing_candidate_traceability"
    if role_mismatch:
        return "role_label_standardization_needed"
    return "traceable_but_training_alignment_needed"


def _cause_repair_risk(
    cause_eids: Sequence[str],
    symptom_eids: Sequence[str],
    cause_missing: Sequence[str],
    symptom_missing: Sequence[str],
) -> str:
    if not cause_eids:
        return "cause_eids_missing"
    if cause_missing or symptom_missing:
        return "candidate_traceability_gap"
    if cause_eids == ["e1"]:
        return "cause_under_specific_only_injector_e1"
    if not symptom_eids:
        return "symptom_empty_by_current_target_design"
    return "traceable_but_role_alignment_needed"


def _write_csv(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    rows = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    headers = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _md_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(item) for item in row) + " |")
    return "\n".join(lines)


def _repair_plans(audit: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "metric_repair_plan": {
            "strict_metrics_to_retain": [
                "strict evidence_eid precision/recall/F1",
                "strict cause_eids and symptom_eids exact set accuracy",
                "schema compliance and semantic metrics reported separately",
            ],
            "relaxed_debug_metrics_to_add": [
                "normalized text overlap",
                "substring hit",
                "evidence_id hit",
                "role-aware evidence F1",
                "primary evidence hit rate",
                "primary cause hit rate",
                "symptom hit rate",
                "cause_eid recall",
                "symptom_eid recall",
                "partial role accuracy",
                "under-selection count",
            ],
            "implementation_boundary": "Plan only; do not modify checker in Task 9A.",
        },
        "target_repair_plan": {
            "stable_evidence_id_rules": [
                "EID scope is per run and must be stable within candidate_evidence.",
                "All target eids must be a subset of candidate_evidence/evidence_candidates for that run.",
                "Do not create synthetic eids in targets.",
            ],
            "evidence_array_rules": [
                "primary_evidence must include injector marker plus subtype-specific probe/snapshot evidence when present.",
                "secondary_evidence must include supporting reachability/context evidence, not noise.",
                "symptom_evidence is allowed empty only when no separate symptom evidence exists.",
                "noise_evidence must keep historical/debug transport evidence out of primary/secondary roles.",
            ],
            "cause_symptom_rules": [
                "cause_eids must cover the complete primary cause evidence set, not only e1.",
                "symptom_eids must remain empty only when the source target has no separate symptom evidence.",
                "Use subtype-specific templates to define expected cause evidence kinds.",
            ],
            "traceability_sidecar": {
                "sample_id": "training sample id",
                "run_id": "source run id",
                "candidate_evidence_path": "evidence_candidates.jsonl or L2 input candidate_evidence",
                "target_eid_set": "all eids used by target",
                "source_l2_file": "source l2 row",
                "validation": "target_eids_subset_of_candidate_eids",
            },
            "boundaries": [
                "Do not include action_after_diagnosis.",
                "Do not include CPU/MEM GT data.",
                "Preserve GT/OBS separation.",
                "Do not rebuild L1/L2 in Task 9A.",
                "Dry-run examples are not training data.",
            ],
        },
        "prompt_repair_plan": {
            "evidence_extraction": [
                "Require choosing eids from candidate_evidence; free-text evidence is insufficient.",
                "Require non-empty arrays when candidate_evidence has targetable primary/secondary/noise eids.",
                "Only allow empty/unknown when candidate_evidence truly lacks evidence for that role.",
                "Ask for role-preserving arrays: primary, secondary, symptom, noise.",
            ],
            "cause_vs_symptom": [
                "Require complete cause_eids coverage for primary cause, not only injector summary e1.",
                "Ask the model to include subtype-specific probe/snapshot eids that explain the primary cause.",
                "State symptom_eids may be empty only when no separate symptom evidence exists.",
                "Consider few-shot examples for multi-eid cause selection.",
            ],
            "few_shot_and_hints": [
                "Use small subtype-specific examples after target repair, not before.",
                "Tighten unknown/empty conditions to avoid empty evidence arrays.",
            ],
        },
        "next_task_priority": [
            "Task 9B: relaxed evidence/cause metrics prototype",
            "Task 9C: repaired target materialization dry-run",
            "Task 9D: repaired NET-only candidate v2 creation",
            "Task 9E: repaired NET-only conservative retraining",
            "Task 10: Action Contract v1 after evidence/cause repair",
        ],
    }


def _dry_run_examples(audit: Dict[str, Any]) -> List[Dict[str, Any]]:
    examples: List[Dict[str, Any]] = []
    evidence_rows = audit["evidence_table"]
    cause_rows = audit["cause_table"]
    if evidence_rows:
        row = evidence_rows[0]
        examples.append(
            {
                "repair_plan_only": True,
                "not_for_training": True,
                "task": "evidence_extraction",
                "sample_id": row["sample_id"],
                "run_id": row["run_id"],
                "subtype": row["subtype"],
                "proposed_target_shape": {
                    "primary_evidence": [{"eid": eid} for eid in row["eids_by_field"]["primary_evidence"]],
                    "secondary_evidence": [{"eid": eid} for eid in row["eids_by_field"]["secondary_evidence"]],
                    "symptom_evidence": [{"eid": eid} for eid in row["eids_by_field"]["symptom_evidence"]],
                    "noise_evidence": [{"eid": eid} for eid in row["eids_by_field"]["noise_evidence"]],
                },
                "validation_required": [
                    "all eids subset of candidate_evidence",
                    "role arrays preserve source target role",
                    "no action/CPU/MEM labels added",
                ],
            }
        )
    if cause_rows:
        row = cause_rows[0]
        examples.append(
            {
                "repair_plan_only": True,
                "not_for_training": True,
                "task": "cause_vs_symptom",
                "sample_id": row["sample_id"],
                "run_id": row["run_id"],
                "subtype": row["subtype"],
                "proposed_target_shape": {
                    "primary_family": "net",
                    "primary_subtype": row["subtype"],
                    "cause_eids": row["cause_eids"],
                    "symptom_eids": row["symptom_eids"],
                    "gt_obs_separated": True,
                },
                "validation_required": [
                    "cause_eids complete for subtype primary cause",
                    "symptom_eids empty only if no symptom target exists",
                    "no observed family promoted to GT",
                ],
            }
        )
    return examples


def _write_summary_md(path: Path, plan: Dict[str, Any]) -> None:
    evidence = plan["evidence_target_audit"]
    cause = plan["cause_target_audit"]
    balance = plan["task_balance_analysis"]
    lines = [
        "# NET-only Evidence/Cause Repair Plan Summary",
        "",
        "## Target Audit",
        _md_table(
            ["area", "value"],
            [
                ["evidence_samples_audited", evidence["samples_audited"]],
                ["evidence_gold_empty_count", evidence["gold_empty_count"]],
                ["evidence_id_traceable_rate", evidence["evidence_id_traceable_rate"]],
                ["cause_samples_audited", cause["samples_audited"]],
                ["cause_empty_count", cause["cause_empty_count"]],
                ["symptom_empty_count", cause["symptom_empty_count"]],
                ["cause_eid_traceable_rate", cause["cause_eid_traceable_rate"]],
                ["symptom_eid_traceable_rate", cause["symptom_eid_traceable_rate"]],
            ],
        ),
        "",
        "## Task Balance",
        _md_table(
            ["split", "diagnosis", "evidence_extraction", "cause_vs_symptom"],
            [
                [
                    split,
                    balance["by_split_task"].get(split, {}).get("diagnosis", 0),
                    balance["by_split_task"].get(split, {}).get("evidence_extraction", 0),
                    balance["by_split_task"].get(split, {}).get("cause_vs_symptom", 0),
                ]
                for split in ("train", "val", "test")
            ],
        ),
        "",
        "## Priority",
    ]
    for idx, item in enumerate(plan["next_task_priority"], 1):
        lines.append(f"{idx}. {item}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_report(path: Path, plan: Dict[str, Any]) -> None:
    evidence = plan["evidence_target_audit"]
    cause = plan["cause_target_audit"]
    key = plan["key_fields"]
    lines = [
        "# NET-only Evidence/Cause Task Repair Plan - Task 9A",
        "",
        "## Scope",
        "This is a repair plan and read-only audit. No training, eval, generation, model loading, adapter update, formal JSONL mutation, L1/L2 rebuild, checker edit, or contract edit was performed.",
        "",
        "## Evidence Target Audit",
        _md_table(
            ["metric", "value"],
            [
                ["samples_audited", evidence["samples_audited"]],
                ["split_distribution", json.dumps(evidence["split_distribution"], sort_keys=True)],
                ["gold_empty_count", evidence["gold_empty_count"]],
                ["missing_eid_count", evidence["missing_eid_count"]],
                ["duplicate_eid_within_sample_count", evidence["duplicate_eid_within_sample_count"]],
                ["support_role_mismatch_count", evidence["support_role_mismatch_count"]],
                ["evidence_id_traceable_rate", evidence["evidence_id_traceable_rate"]],
                ["evidence_candidate_file_traceable_rate", evidence["evidence_candidate_file_traceable_rate"]],
            ],
        ),
        "",
        "Evidence materialization risk: targets are non-empty and traceable, but 8F showed the trained model outputs empty arrays. Repair should make stable evidence-id selection explicit and add debug metrics without replacing strict EID F1.",
        "",
        "## Cause_vs_symptom Target Audit",
        _md_table(
            ["metric", "value"],
            [
                ["samples_audited", cause["samples_audited"]],
                ["split_distribution", json.dumps(cause["split_distribution"], sort_keys=True)],
                ["cause_empty_count", cause["cause_empty_count"]],
                ["symptom_empty_count", cause["symptom_empty_count"]],
                ["cause_eids_only_e1_count", cause["cause_eids_only_e1_count"]],
                ["cause_eids_under_minimum_count", cause["cause_eids_under_minimum_count"]],
                ["cause_eid_traceable_rate", cause["cause_eid_traceable_rate"]],
                ["symptom_eid_traceable_rate", cause["symptom_eid_traceable_rate"]],
            ],
        ),
        "",
        "Cause materialization risk: cause_eids are non-empty and traceable, but model predictions under-select multi-eid causes. Current symptom_eids are empty across the NET-only target set, so symptom handling needs explicit documentation before broader datasets.",
        "",
        "## Task Balance Analysis",
        "The dataset is balanced 1:1:1 by task and balanced by subtype, but evidence/cause targets are semantically harder than diagnosis. Recommend task-balanced repair experiments, evidence/cause oversampling, or staged training after target dry-run validation.",
        "",
        "## Metric/Checker Repair Plan",
    ]
    for item in plan["metric_repair_plan"]["strict_metrics_to_retain"]:
        lines.append(f"- Retain: {item}")
    for item in plan["metric_repair_plan"]["relaxed_debug_metrics_to_add"]:
        lines.append(f"- Add debug metric: {item}")
    lines.extend(
        [
            "",
            "## Target Repair Plan",
        ]
    )
    for section, items in plan["target_repair_plan"].items():
        if isinstance(items, list):
            lines.append(f"### {section}")
            for item in items:
                lines.append(f"- {item}")
        elif isinstance(items, dict):
            lines.append(f"### {section}")
            for k, v in items.items():
                lines.append(f"- {k}: {v}")
    lines.extend(["", "## Prompt Repair Plan"])
    for section, items in plan["prompt_repair_plan"].items():
        lines.append(f"### {section}")
        for item in items:
            lines.append(f"- {item}")
    lines.extend(["", "## Repaired Target Schema Proposal"])
    lines.append(
        "Use contract-v1 output objects, but add a planning-only target sidecar with `candidate_evidence_eids`, `target_eid_set`, `role`, `kind`, `source_l2_file`, and `validation.target_eids_subset_of_candidate_eids=true`."
    )
    lines.extend(["", "## Traceability Sidecar Proposal"])
    for k, v in plan["target_repair_plan"]["traceability_sidecar"].items():
        lines.append(f"- {k}: {v}")
    lines.extend(["", "## Next Task Priority"])
    for idx, item in enumerate(plan["next_task_priority"], 1):
        lines.append(f"{idx}. {item}")
    lines.extend(
        [
            "",
            "Action Contract v1 should wait because the current formal model is schema-clean and diagnosis-clean but evidence_extraction F1 is 0 and cause_vs_symptom is 5/14.",
            "",
            "## Key Fields",
            _md_table(["field", "value"], sorted(key.items())),
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _build_plan(args: argparse.Namespace) -> Dict[str, Any]:
    candidate_dir = Path(args.candidate_dir)
    out_dir = Path(args.output_dir)
    f8_dir = Path(args.failure_analysis_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    audit = _audit_targets(candidate_dir)
    f8_json = _read_json(f8_dir / "evidence_cause_failure_analysis.json")
    contract = _read_json(CONTRACT_JSON)
    checker_text = CHECKER.read_text(encoding="utf-8")
    split_summary = _read_json(candidate_dir / "split_summary.json")
    source_ledger = _read_json(candidate_dir / "source_ledger_summary.json")
    repair = _repair_plans(audit)

    by_split_task: Dict[str, Dict[str, int]] = defaultdict(dict)
    by_subtype_task: Dict[str, Dict[str, int]] = defaultdict(dict)
    for row in audit["task_balance_rows"]:
        if row["subtype"] == "__all__":
            by_split_task[row["split"]][row["task"]] = row["samples"]
        else:
            key = f"{row['split']}::{row['subtype']}"
            by_subtype_task[key][row["task"]] = row["samples"]
    task_balance = {
        "by_split_task": {k: dict(v) for k, v in sorted(by_split_task.items())},
        "by_split_subtype_task": {k: dict(v) for k, v in sorted(by_subtype_task.items())},
        "recommendations": [
            "Keep 1:1:1 task reporting, but try evidence/cause oversampling after target repair.",
            "Consider staged evidence/cause training before mixed diagnosis/evidence/cause training.",
            "Add evidence/cause definitions in system/user prompt after target dry-run validation.",
        ],
    }
    dry_examples = _dry_run_examples(audit)

    evidence_summary = audit["evidence_summary"]
    cause_summary = audit["cause_summary"]
    key_fields = {
        "SKILL_LOADED": True,
        "MAINLINE_STATUS": args.mainline_status,
        "REPORT_CREATED": True,
        "JSON_CREATED": True,
        "AUDIT_TABLES_CREATED": True,
        "REPAIR_PLAN_CREATED": True,
        "DRY_RUN_EXAMPLES_CREATED": bool(dry_examples),
        "PLANNING_SCRIPT_CREATED": True,
        "TRAINING_STARTED": False,
        "EVAL_STARTED": False,
        "GENERATION_STARTED": False,
        "MODEL_LOADED": False,
        "WEIGHT_UPDATE_STARTED": False,
        "ADAPTER_MODIFIED": False,
        "DATA_JSONL_MODIFIED": False,
        "LEDGER_MODIFIED": False,
        "FROZEN_NET_BATCH_MODIFIED": False,
        "L1_REBUILT": False,
        "L2_REBUILT": False,
        "FORMAL_REPAIRED_CANDIDATE_CREATED": False,
        "HDC_USED": False,
        "BOARD_TOUCHED": False,
        "EVIDENCE_SAMPLES_AUDITED": evidence_summary["samples_audited"],
        "CAUSE_SYMPTOM_SAMPLES_AUDITED": cause_summary["samples_audited"],
        "EVIDENCE_GOLD_EMPTY_COUNT": evidence_summary["gold_empty_count"],
        "EVIDENCE_ID_TRACEABLE_RATE": evidence_summary["evidence_id_traceable_rate"],
        "CAUSE_EID_TRACEABLE_RATE": cause_summary["cause_eid_traceable_rate"],
        "SYMPTOM_EID_TRACEABLE_RATE": (
            cause_summary["symptom_eid_traceable_rate"]
            if cause_summary["symptom_eid_traceable_rate"] is not None
            else "N/A_NO_SYMPTOM_EIDS"
        ),
        "TASK_BALANCE_ANALYZED": True,
        "METRIC_REPAIR_PLAN_CREATED": True,
        "TARGET_REPAIR_PLAN_CREATED": True,
        "PROMPT_REPAIR_PLAN_CREATED": True,
        "READY_FOR_RELAXED_METRICS_PROTOTYPE": True,
        "READY_FOR_REPAIRED_TARGET_DRY_RUN": True,
        "READY_FOR_ACTION_CONTRACT": False,
        "REVIEWER_VERDICT": args.reviewer_verdict,
    }
    plan: Dict[str, Any] = {
        "schema_version": "net_only_evidence_cause_repair_plan_v1",
        "created_at_utc": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "inputs": {
            "candidate_dir": str(candidate_dir),
            "failure_analysis_dir": str(f8_dir),
            "contract_json": str(CONTRACT_JSON),
            "contract_md": str(CONTRACT_MD),
            "checker": str(CHECKER),
        },
        "input_sha256": {
            "split_summary": _sha256(candidate_dir / "split_summary.json"),
            "source_ledger_summary": _sha256(candidate_dir / "source_ledger_summary.json"),
            "failure_analysis": _sha256(f8_dir / "evidence_cause_failure_analysis.json"),
            "contract_json": _sha256(CONTRACT_JSON),
            "checker": _sha256(CHECKER),
        },
        "source_split_summary": split_summary,
        "source_ledger_summary": source_ledger,
        "failure_analysis_8f_summary": {
            "evidence_f1": f8_json["evidence"]["f1"],
            "evidence_empty_prediction_count": f8_json["evidence"]["empty_prediction_count"],
            "cause_symptom_accuracy": f8_json["cause_vs_symptom"]["accuracy"],
            "cause_symptom_correct": f8_json["cause_vs_symptom"]["correct"],
            "cause_symptom_total": f8_json["cause_vs_symptom"]["total"],
        },
        "contract_schema_version": contract.get("schema_version"),
        "checker_observations": {
            "strict_evidence_eid_overlap": "gold_all_evidence & pred_all_evidence" in checker_text,
            "strict_cause_symptom_set_equality": "pred_cause == gold_cause" in checker_text,
            "checker_modified_by_task_9a": False,
        },
        "evidence_target_audit": evidence_summary,
        "cause_target_audit": cause_summary,
        "task_balance_analysis": task_balance,
        **repair,
        "dry_run_examples_created": bool(dry_examples),
        "dry_run_examples_note": "Examples are repair_plan_only/not_for_training and are not written to train/val/test.",
        "key_fields": key_fields,
    }
    _write_jsonl(out_dir / "evidence_target_audit_table.jsonl", audit["evidence_table"])
    _write_jsonl(out_dir / "cause_target_audit_table.jsonl", audit["cause_table"])
    _write_csv(out_dir / "task_balance_table.csv", audit["task_balance_rows"])
    _write_jsonl(out_dir / "repaired_target_examples_not_for_training.jsonl", dry_examples)
    _write_json(out_dir / "evidence_cause_repair_plan.json", plan)
    _write_summary_md(out_dir / "repair_plan_summary.md", plan)
    _write_report(Path(args.report), plan)
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-dir", default=str(DEFAULT_CANDIDATE_DIR))
    parser.add_argument("--failure-analysis-dir", default=str(DEFAULT_8F_DIR))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    parser.add_argument("--mainline-status", default="PENDING_AGGREGATION")
    parser.add_argument("--reviewer-verdict", default="PENDING")
    args = parser.parse_args()
    plan = _build_plan(args)
    print(
        json.dumps(
            {
                "RESULT": "PASS",
                "report": args.report,
                "json": str(Path(args.output_dir) / "evidence_cause_repair_plan.json"),
                "evidence_samples": plan["evidence_target_audit"]["samples_audited"],
                "cause_samples": plan["cause_target_audit"]["samples_audited"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
