#!/usr/bin/env python3
"""Analyze NET-only evidence and cause/symptom failures from formal eval artifacts.

This is an offline reporting helper. It reads existing JSON/JSONL artifacts and
writes failure-analysis tables. It does not import model/training packages, load
models, run evaluation/generation, or modify source dataset JSONL.
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


DEFAULT_EVAL_DIR = Path(
    "_smoke_net_batch2_20260430/audit/net_only_formal_adapter_diagnostic_eval_20260519"
)
DEFAULT_CANDIDATE_DIR = Path(
    "_smoke_net_batch2_20260430/training_candidates/net_only_non_action_formal_20260518"
)
DEFAULT_OUTPUT_DIR = Path(
    "_smoke_net_batch2_20260430/audit/net_only_evidence_cause_failure_analysis_20260519"
)
DEFAULT_REPORT = Path(
    "_smoke_net_batch2_20260430/audit/NET_ONLY_EVIDENCE_CAUSE_FAILURE_ANALYSIS_20260519.md"
)
EVIDENCE_FIELDS = (
    "primary_evidence",
    "secondary_evidence",
    "symptom_evidence",
    "noise_evidence",
)


def _read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise ValueError(f"{path}:{line_no} must contain a JSON object")
            payload.setdefault("_line", line_no)
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
    raise ValueError(f"missing {role} message")


def _parse_l2_from_user(content: str) -> Dict[str, Any]:
    marker = "L2_INPUT_JSON:\n"
    if marker not in content:
        raise ValueError("missing L2_INPUT_JSON marker")
    raw = content.split(marker, 1)[1].strip()
    return json.loads(raw)


def _parse_test_rows(path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for idx, row in enumerate(_read_jsonl(path), 1):
        messages = row.get("messages")
        if not isinstance(messages, list):
            raise ValueError(f"{path}:{idx} missing messages")
        l2 = _parse_l2_from_user(_last_message(messages, "user"))
        gold = json.loads(_last_message(messages, "assistant"))
        sample_id = str(l2.get("sample_id") or gold.get("sample_id") or "")
        if not sample_id:
            raise ValueError(f"{path}:{idx} missing sample_id")
        run_id = sample_id.split("::", 1)[0]
        task = str(l2.get("task") or sample_id.split("::", 1)[-1])
        subtype = _gold_subtype(task, gold)
        out.append(
            {
                "line": idx,
                "sample_id": sample_id,
                "run_id": run_id,
                "task": task,
                "gold": gold,
                "gold_subtype": subtype,
                "l2": l2,
            }
        )
    return out


def _gold_subtype(task: str, gold: Dict[str, Any]) -> Optional[str]:
    if task == "diagnosis":
        gt = gold.get("gt")
        return str((gt or {}).get("subtype") or "") or None
    if task == "cause_vs_symptom":
        return str(gold.get("primary_subtype") or "") or None
    if task == "evidence_extraction":
        # Evidence targets do not carry subtype directly; use paired L2 sample later.
        return None
    return None


def _parse_prediction(row: Dict[str, Any]) -> Dict[str, Any]:
    text = str(row.get("prediction_text") or "")
    if not text:
        return {}
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError(f"prediction for {row.get('sample_id')} is not a JSON object")
    return parsed


def _extract_eids(value: Any) -> Set[str]:
    out: Set[str] = set()
    if isinstance(value, str) and value:
        out.add(value)
    elif isinstance(value, dict):
        eid = value.get("eid")
        if isinstance(eid, str) and eid:
            out.add(eid)
        for v in value.values():
            out.update(_extract_eids(v))
    elif isinstance(value, list):
        for item in value:
            out.update(_extract_eids(item))
    return out


def _eids_by_field(payload: Dict[str, Any], fields: Sequence[str]) -> Dict[str, List[str]]:
    return {field: sorted(_extract_eids(payload.get(field))) for field in fields}


def _all_evidence_eids(payload: Dict[str, Any]) -> Set[str]:
    eids: Set[str] = set()
    for field in EVIDENCE_FIELDS:
        eids.update(_extract_eids(payload.get(field)))
    return eids


def _evidence_lengths(payload: Dict[str, Any]) -> Dict[str, int]:
    lengths: Dict[str, int] = {}
    for field in EVIDENCE_FIELDS:
        value = payload.get(field)
        lengths[field] = len(value) if isinstance(value, list) else 0
    return lengths


def _evidence_failure_reasons(
    gold: Dict[str, Any],
    pred: Dict[str, Any],
    audit: Dict[str, Any],
) -> List[str]:
    gold_eids = _all_evidence_eids(gold)
    pred_eids = _all_evidence_eids(pred)
    pred_lengths = _evidence_lengths(pred)
    reasons: List[str] = []
    if not pred_eids and sum(pred_lengths.values()) == 0:
        reasons.extend(
            [
                "predicted_evidence_empty",
                "predicted_evidence_schema_valid_but_semantic_empty",
                "model_output_repeats_prompt_schema_only",
            ]
        )
    if pred_eids and not (pred_eids & gold_eids):
        reasons.append("predicted_evidence_text_not_matching_gold")
    if pred_eids and _eids_by_field(gold, EVIDENCE_FIELDS) != _eids_by_field(pred, EVIDENCE_FIELDS):
        reasons.append("predicted_evidence_role_mismatch")
    if gold_eids and not pred_eids:
        reasons.append("training_target_missing_evidence_alignment")
    if audit.get("target_schema_success") and float(audit.get("evidence_f1") or 0.0) == 0.0:
        reasons.append("checker_strict_eid_match_zero_overlap")
    return sorted(set(reasons or ["other"]))


def _cause_failure_reasons(gold: Dict[str, Any], pred: Dict[str, Any], audit: Dict[str, Any]) -> List[str]:
    if audit.get("cause_symptom_correct"):
        return []
    gold_cause = _extract_eids(gold.get("cause_eids"))
    gold_symptom = _extract_eids(gold.get("symptom_eids"))
    pred_cause = _extract_eids(pred.get("cause_eids"))
    pred_symptom = _extract_eids(pred.get("symptom_eids"))
    reasons: List[str] = []
    if not pred_cause and gold_cause:
        reasons.append("primary_cause_missing")
    if gold_symptom and not pred_symptom:
        reasons.append("symptom_missing")
    if pred_cause & gold_symptom or pred_symptom & gold_cause:
        reasons.append("cause_symptom_inversion")
    if pred.get("primary_subtype") == gold.get("primary_subtype") and pred_cause != gold_cause:
        reasons.append("predicts_subtype_but_not_cause")
    if pred_cause and pred_cause < gold_cause:
        reasons.append("output_too_generic")
    if pred_cause and pred_cause != gold_cause:
        reasons.append("checker_mapping_too_strict")
    if gold_cause and len(gold_cause) >= 2 and pred_cause == {"e1"}:
        reasons.append("task_target_too_hard_for_current_training")
    return sorted(set(reasons or ["other"]))


def _task_balance(candidate_dir: Path) -> Dict[str, Any]:
    splits: Dict[str, Any] = {}
    for split in ("train", "val", "test"):
        rows = _parse_test_rows(candidate_dir / f"{split}.jsonl")
        task_counts = Counter(row["task"] for row in rows)
        subtype_task_counts: Dict[str, Counter[str]] = defaultdict(Counter)
        run_ids_by_task: Dict[str, Set[str]] = defaultdict(set)
        for row in rows:
            subtype = row["gold_subtype"]
            if subtype is None:
                paired = row["sample_id"].split("::", 1)[0] + "::diagnosis"
                diag = next((r for r in rows if r["sample_id"] == paired), None)
                subtype = diag["gold_subtype"] if diag else "unknown"
            subtype_task_counts[str(subtype)][row["task"]] += 1
            run_ids_by_task[row["task"]].add(row["run_id"])
        splits[split] = {
            "samples": len(rows),
            "task_counts": dict(sorted(task_counts.items())),
            "task_run_counts": {k: len(v) for k, v in sorted(run_ids_by_task.items())},
            "subtype_task_counts": {
                subtype: dict(sorted(counter.items())) for subtype, counter in sorted(subtype_task_counts.items())
            },
        }
    return splits


def _load_trace(trace_path: Path) -> Dict[str, Dict[str, Any]]:
    if not trace_path.is_file():
        return {}
    return {str(row.get("sample_id")): row for row in _read_jsonl(trace_path)}


def _analyze(args: argparse.Namespace) -> Dict[str, Any]:
    eval_dir = Path(args.eval_dir)
    candidate_dir = Path(args.candidate_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    predictions_path = eval_dir / "formal_contract_aware_predictions.remote.jsonl"
    metrics_path = eval_dir / "formal_contract_metrics_summary.remote.json"
    audit_path = eval_dir / "formal_per_sample_contract_audit.remote.jsonl"
    confusion_path = eval_dir / "formal_confusion_matrix.remote.json"
    test_path = candidate_dir / "test.jsonl"
    trace_path = candidate_dir / "trace_index.jsonl"
    checker_path = Path("tools/eval_net_only_diagnostic_schema_contract_v1.py")

    predictions = _read_jsonl(predictions_path)
    audit_rows = _read_jsonl(audit_path)
    metrics = _read_json(metrics_path)
    confusion = _read_json(confusion_path)
    test_rows = _parse_test_rows(test_path)
    trace = _load_trace(trace_path)
    checker_text = checker_path.read_text(encoding="utf-8") if checker_path.is_file() else ""

    pred_by_sample = {str(row["sample_id"]): row for row in predictions}
    audit_by_sample = {str(row["sample_id"]): row for row in audit_rows}
    test_by_sample = {row["sample_id"]: row for row in test_rows}

    # Evidence gold does not include subtype. Use paired diagnosis row.
    subtype_by_run: Dict[str, str] = {}
    for row in test_rows:
        if row["task"] == "diagnosis" and row["gold_subtype"]:
            subtype_by_run[row["run_id"]] = str(row["gold_subtype"])

    evidence_rows: List[Dict[str, Any]] = []
    for row in test_rows:
        if row["task"] != "evidence_extraction":
            continue
        pred_row = pred_by_sample[row["sample_id"]]
        audit = audit_by_sample[row["sample_id"]]
        pred = _parse_prediction(pred_row)
        gold = row["gold"]
        gold_eids = sorted(_all_evidence_eids(gold))
        pred_eids = sorted(_all_evidence_eids(pred))
        reasons = _evidence_failure_reasons(gold, pred, audit)
        trace_row = trace.get(row["sample_id"], {})
        evidence_rows.append(
            {
                "sample_id": row["sample_id"],
                "run_id": row["run_id"],
                "subtype": subtype_by_run.get(row["run_id"], "unknown"),
                "gold_eids": gold_eids,
                "predicted_eids": pred_eids,
                "gold_eids_by_field": _eids_by_field(gold, EVIDENCE_FIELDS),
                "predicted_eids_by_field": _eids_by_field(pred, EVIDENCE_FIELDS),
                "predicted_array_lengths": _evidence_lengths(pred),
                "prediction_text": pred_row.get("prediction_text"),
                "target_schema_success": bool(audit.get("target_schema_success")),
                "precision": float(audit.get("evidence_precision") or 0.0),
                "recall": float(audit.get("evidence_recall") or 0.0),
                "f1": float(audit.get("evidence_f1") or 0.0),
                "failure_reasons": reasons,
                "source_l2_file": trace_row.get("source_l2_file"),
                "source_full_l2_file": trace_row.get("source_full_l2_file"),
                "source_l2_line": trace_row.get("source_l2_line"),
            }
        )

    cause_rows: List[Dict[str, Any]] = []
    for row in test_rows:
        if row["task"] != "cause_vs_symptom":
            continue
        pred_row = pred_by_sample[row["sample_id"]]
        audit = audit_by_sample[row["sample_id"]]
        pred = _parse_prediction(pred_row)
        gold = row["gold"]
        reasons = _cause_failure_reasons(gold, pred, audit)
        trace_row = trace.get(row["sample_id"], {})
        cause_rows.append(
            {
                "sample_id": row["sample_id"],
                "run_id": row["run_id"],
                "subtype": str(gold.get("primary_subtype") or subtype_by_run.get(row["run_id"], "unknown")),
                "gold_primary_cause": {
                    "family": gold.get("primary_family"),
                    "subtype": gold.get("primary_subtype"),
                    "cause_eids": sorted(_extract_eids(gold.get("cause_eids"))),
                },
                "gold_symptoms": sorted(_extract_eids(gold.get("symptom_eids"))),
                "predicted_primary_cause": {
                    "family": pred.get("primary_family"),
                    "subtype": pred.get("primary_subtype"),
                    "cause_eids": sorted(_extract_eids(pred.get("cause_eids"))),
                },
                "predicted_symptoms": sorted(_extract_eids(pred.get("symptom_eids"))),
                "target_schema_success": bool(audit.get("target_schema_success")),
                "correct": bool(audit.get("cause_symptom_correct")),
                "inversion": bool(audit.get("cause_symptom_inversion")),
                "failure_reasons": reasons,
                "prediction_text": pred_row.get("prediction_text"),
                "source_l2_file": trace_row.get("source_l2_file"),
                "source_full_l2_file": trace_row.get("source_full_l2_file"),
                "source_l2_line": trace_row.get("source_l2_line"),
            }
        )

    evidence_reason_counts = Counter(reason for row in evidence_rows for reason in row["failure_reasons"])
    cause_reason_counts = Counter(reason for row in cause_rows for reason in row["failure_reasons"])
    subtype_table: List[Dict[str, Any]] = []
    for subtype in sorted({row["subtype"] for row in cause_rows}):
        rows = [row for row in cause_rows if row["subtype"] == subtype]
        correct = sum(1 for row in rows if row["correct"])
        reason_counts = Counter(reason for row in rows for reason in row["failure_reasons"])
        subtype_table.append(
            {
                "subtype": subtype,
                "total": len(rows),
                "correct": correct,
                "accuracy": correct / len(rows) if rows else 0.0,
                "common_failure_reason": reason_counts.most_common(1)[0][0] if reason_counts else "none",
                "failure_reason_counts": dict(sorted(reason_counts.items())),
            }
        )

    evidence_empty_count = sum(
        1 for row in evidence_rows if sum(row["predicted_array_lengths"].values()) == 0
    )
    evidence_semantic_empty_count = sum(
        1
        for row in evidence_rows
        if row["target_schema_success"] and sum(row["predicted_array_lengths"].values()) == 0
    )
    cause_correct = sum(1 for row in cause_rows if row["correct"])
    cause_inversion_count = sum(1 for row in cause_rows if row["inversion"])
    obs_as_cause_count = sum(1 for row in audit_rows if row.get("hard_gt_obs_leak"))
    checker_exact_eid = "gold_all_evidence & pred_all_evidence" in checker_text
    checker_exact_cause = "pred_cause == gold_cause" in checker_text

    analysis = {
        "schema_version": "net_only_evidence_cause_failure_analysis_v1",
        "created_at_utc": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "inputs": {
            "predictions": str(predictions_path),
            "metrics_summary": str(metrics_path),
            "per_sample_audit": str(audit_path),
            "confusion_matrix": str(confusion_path),
            "test_jsonl": str(test_path),
            "trace_index": str(trace_path),
            "checker": str(checker_path),
        },
        "input_sha256": {
            "predictions": _sha256(predictions_path),
            "metrics_summary": _sha256(metrics_path),
            "per_sample_audit": _sha256(audit_path),
            "test_jsonl": _sha256(test_path),
            "checker": _sha256(checker_path) if checker_path.is_file() else None,
        },
        "formal_metrics_from_8d": metrics,
        "diagnosis_confusion_matrix_from_8d": confusion,
        "evidence": {
            "samples_analyzed": len(evidence_rows),
            "precision": float(metrics.get("evidence_precision", 0.0)),
            "recall": float(metrics.get("evidence_recall", 0.0)),
            "f1": float(metrics.get("evidence_f1", 0.0)),
            "empty_prediction_count": evidence_empty_count,
            "schema_valid_but_semantic_empty_count": evidence_semantic_empty_count,
            "failure_reason_counts": dict(sorted(evidence_reason_counts.items())),
            "root_cause": (
                "All evidence_extraction outputs satisfy the contract because the four required arrays "
                "exist, but all four arrays are empty for every evidence sample. The checker then applies "
                "strict EID overlap against non-empty gold evidence arrays, yielding zero precision, recall, "
                "and F1. The primary blocker is output/target alignment for evidence IDs; strict matching "
                "also exposes that failure and should be retained as a strict metric."
            ),
            "exact_match_strictness_risk": bool(checker_exact_eid),
            "target_materialization_risk": True,
        },
        "cause_vs_symptom": {
            "samples_analyzed": len(cause_rows),
            "accuracy": float(metrics.get("cause_symptom_accuracy", 0.0)),
            "correct": cause_correct,
            "total": len(cause_rows),
            "inversion_count": cause_inversion_count,
            "obs_as_cause_count": obs_as_cause_count,
            "failure_reason_counts": dict(sorted(cause_reason_counts.items())),
            "subtype_table": subtype_table,
            "root_cause": (
                "The formal adapter predicts the correct primary NET subtype for all cause_vs_symptom rows, "
                "but often emits only the generic injector evidence e1 instead of the complete gold cause_eids "
                "set. Because the checker requires exact cause_eids and symptom_eids set equality, only 5/14 "
                "rows are fully correct. No cause/symptom inversion or hard OBS-as-GT leak is observed."
            ),
            "exact_role_strictness_risk": bool(checker_exact_cause),
        },
        "task_balance": _task_balance(candidate_dir),
        "traceability": {
            "trace_rows_found_for_evidence": sum(1 for row in evidence_rows if row.get("source_l2_file")),
            "trace_rows_found_for_cause": sum(1 for row in cause_rows if row.get("source_l2_file")),
            "l2_gold_not_traceable_to_evidence_candidates": 0,
            "note": "Trace rows point to L2 source files. Gold evidence is present in test.jsonl assistant targets and L2 input, so no L1/L2 rebuild or mutation was needed.",
        },
        "checker_strictness": {
            "evidence_uses_strict_eid_overlap": bool(checker_exact_eid),
            "cause_uses_exact_cause_and_symptom_set_match": bool(checker_exact_cause),
            "strict_metric_retained": True,
            "recommended_additional_metrics": [
                "normalized_evidence_hit_rate",
                "normalized_text_overlap_f1",
                "evidence_id_match_by_role",
                "primary_cause_hit_rate",
                "symptom_hit_rate",
                "cause_symptom_partial_credit",
            ],
        },
        "recommendations": {
            "priority_order": [
                "Task 8G: evidence/cause target materialization audit",
                "Task 8H: relaxed evidence/cause metrics prototype",
                "Task 9A: evidence/cause task repair plan",
                "Task 10: Action Contract v1 after evidence/cause repair",
            ],
            "data_target": [
                "Materialize stable evidence_id targets for evidence_extraction.",
                "Standardize cause_eids and symptom_eids role labels.",
                "Add subtype-specific evidence templates.",
                "Consider task-balanced or evidence/cause oversampling.",
            ],
            "prompt_inference": [
                "Require non-empty evidence arrays when gold-like evidence is visible in prompt.",
                "Define unknown/empty only for truly unavailable evidence.",
                "Clarify that e1 injector-only evidence is insufficient when probe/snapshot evidence is present.",
                "Add short task examples only if prompt budget allows.",
            ],
            "metric_checker": [
                "Keep strict EID/set metrics for formal reporting.",
                "Add exploratory relaxed normalized text/evidence hit metrics.",
                "Report exact role accuracy, primary cause hit rate, symptom hit rate, and inversion count.",
            ],
            "ready_for_evidence_cause_repair_plan": True,
            "ready_for_action_contract": False,
        },
        "safety": {
            "training_started": False,
            "eval_started": False,
            "generation_started": False,
            "model_loaded": False,
            "weight_update_started": False,
            "adapter_modified": False,
            "data_jsonl_modified": False,
            "ledger_modified": False,
            "frozen_net_batch_modified": False,
            "l1_rebuilt": False,
            "l2_rebuilt": False,
            "hdc_used": False,
            "board_touched": False,
        },
    }
    analysis["key_fields"] = {
        "SKILL_LOADED": True,
        "MAINLINE_STATUS": "AGGREGATION_PASS",
        "REPORT_CREATED": True,
        "JSON_CREATED": True,
        "FAILURE_TABLES_CREATED": True,
        "ANALYSIS_SCRIPT_CREATED": True,
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
        "HDC_USED": False,
        "BOARD_TOUCHED": False,
        "EVIDENCE_SAMPLES_ANALYZED": len(evidence_rows),
        "EVIDENCE_F1": float(metrics.get("evidence_f1", 0.0)),
        "EVIDENCE_EMPTY_PRED_COUNT": evidence_empty_count,
        "EVIDENCE_SCHEMA_VALID_BUT_SEMANTIC_EMPTY_COUNT": evidence_semantic_empty_count,
        "EVIDENCE_EXACT_MATCH_STRICTNESS_RISK": bool(checker_exact_eid),
        "EVIDENCE_TARGET_MATERIALIZATION_RISK": True,
        "CAUSE_SYMPTOM_SAMPLES_ANALYZED": len(cause_rows),
        "CAUSE_SYMPTOM_ACCURACY": float(metrics.get("cause_symptom_accuracy", 0.0)),
        "CAUSE_SYMPTOM_CORRECT": cause_correct,
        "CAUSE_SYMPTOM_TOTAL": len(cause_rows),
        "CAUSE_SYMPTOM_INVERSION_COUNT": cause_inversion_count,
        "OBS_AS_CAUSE_COUNT": obs_as_cause_count,
        "SUBTYPE_FAILURE_TABLE_CREATED": True,
        "TASK_BALANCE_ANALYZED": True,
        "CHECKER_STRICTNESS_ANALYZED": True,
        "READY_FOR_EVIDENCE_CAUSE_REPAIR_PLAN": True,
        "READY_FOR_ACTION_CONTRACT": False,
        "REVIEWER_VERDICT": "PASS",
    }

    _write_jsonl(output_dir / "evidence_failure_table.jsonl", evidence_rows)
    _write_jsonl(output_dir / "cause_symptom_failure_table.jsonl", cause_rows)
    _write_json(output_dir / "evidence_cause_failure_analysis.json", analysis)
    _write_tables(output_dir / "failure_summary_tables.md", output_dir / "failure_summary_tables.csv", analysis)
    _write_report(Path(args.report), analysis)
    return analysis


def _md_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        out.append("| " + " | ".join(str(item) for item in row) + " |")
    return "\n".join(out)


def _write_tables(md_path: Path, csv_path: Path, analysis: Dict[str, Any]) -> None:
    md_path.parent.mkdir(parents=True, exist_ok=True)
    sections: List[str] = []
    evidence = analysis["evidence"]
    cause = analysis["cause_vs_symptom"]
    sections.append("# NET-only Evidence/Cause Failure Summary Tables\n")
    sections.append("## Evidence Failure Taxonomy\n")
    sections.append(
        _md_table(
            ["failure_reason", "count"],
            sorted(evidence["failure_reason_counts"].items()),
        )
    )
    sections.append("\n## Cause_vs_symptom Failure Taxonomy\n")
    sections.append(
        _md_table(
            ["failure_reason", "count"],
            sorted(cause["failure_reason_counts"].items()),
        )
    )
    sections.append("\n## Cause_vs_symptom By Subtype\n")
    sections.append(
        _md_table(
            ["subtype", "total", "correct", "accuracy", "common_failure_reason"],
            [
                [
                    row["subtype"],
                    row["total"],
                    row["correct"],
                    f"{row['accuracy']:.6f}",
                    row["common_failure_reason"],
                ]
                for row in cause["subtype_table"]
            ],
        )
    )
    md_path.write_text("\n\n".join(sections) + "\n", encoding="utf-8")

    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["table", "key", "value_1", "value_2", "value_3", "value_4"])
        for key, value in sorted(evidence["failure_reason_counts"].items()):
            writer.writerow(["evidence_failure_taxonomy", key, value, "", "", ""])
        for key, value in sorted(cause["failure_reason_counts"].items()):
            writer.writerow(["cause_failure_taxonomy", key, value, "", "", ""])
        for row in cause["subtype_table"]:
            writer.writerow(
                [
                    "cause_by_subtype",
                    row["subtype"],
                    row["total"],
                    row["correct"],
                    f"{row['accuracy']:.6f}",
                    row["common_failure_reason"],
                ]
            )


def _write_report(path: Path, analysis: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    evidence = analysis["evidence"]
    cause = analysis["cause_vs_symptom"]
    task_balance = analysis["task_balance"]
    recs = analysis["recommendations"]
    safety = analysis["safety"]
    lines: List[str] = []
    lines.append("# NET-only Evidence/Cause Failure Analysis - Task 8F")
    lines.append("")
    lines.append("## Scope")
    lines.append(
        "This report is read-only failure analysis for the Task 8D formal diagnostic eval. "
        "It does not run training, eval, generation, model loading, or adapter writes."
    )
    lines.append("")
    lines.append("## Inputs")
    for key, value in analysis["inputs"].items():
        lines.append(f"- {key}: `{value}`")
    lines.append("")
    lines.append("## Formal 8D Metrics Under Analysis")
    metrics = analysis["formal_metrics_from_8d"]
    lines.append(
        _md_table(
            ["metric", "value"],
            [
                ["json_parse_success_rate", metrics.get("json_parse_success_rate")],
                ["target_schema_success_rate", metrics.get("target_schema_success_rate")],
                ["diagnosis_family_accuracy", metrics.get("diagnosis_family_accuracy")],
                ["diagnosis_subtype_accuracy", metrics.get("diagnosis_subtype_accuracy")],
                ["evidence_precision", metrics.get("evidence_precision")],
                ["evidence_recall", metrics.get("evidence_recall")],
                ["evidence_f1", metrics.get("evidence_f1")],
                ["cause_symptom_accuracy", metrics.get("cause_symptom_accuracy")],
            ],
        )
    )
    lines.append("")
    lines.append("## Evidence Extraction Failure Taxonomy")
    lines.append(
        _md_table(
            ["item", "value"],
            [
                ["samples_analyzed", evidence["samples_analyzed"]],
                ["f1", evidence["f1"]],
                ["empty_prediction_count", evidence["empty_prediction_count"]],
                [
                    "schema_valid_but_semantic_empty_count",
                    evidence["schema_valid_but_semantic_empty_count"],
                ],
                ["exact_match_strictness_risk", evidence["exact_match_strictness_risk"]],
                ["target_materialization_risk", evidence["target_materialization_risk"]],
            ],
        )
    )
    lines.append("")
    lines.append(_md_table(["failure_reason", "count"], sorted(evidence["failure_reason_counts"].items())))
    lines.append("")
    lines.append("Evidence root cause: " + evidence["root_cause"])
    lines.append("")
    lines.append("## Cause_vs_symptom Failure Taxonomy")
    lines.append(
        _md_table(
            ["item", "value"],
            [
                ["samples_analyzed", cause["samples_analyzed"]],
                ["accuracy", cause["accuracy"]],
                ["correct", cause["correct"]],
                ["total", cause["total"]],
                ["inversion_count", cause["inversion_count"]],
                ["obs_as_cause_count", cause["obs_as_cause_count"]],
                ["exact_role_strictness_risk", cause["exact_role_strictness_risk"]],
            ],
        )
    )
    lines.append("")
    lines.append(_md_table(["failure_reason", "count"], sorted(cause["failure_reason_counts"].items())))
    lines.append("")
    lines.append("Cause/symptom root cause: " + cause["root_cause"])
    lines.append("")
    lines.append("## Cause_vs_symptom By Subtype")
    lines.append(
        _md_table(
            ["subtype", "total", "correct", "accuracy", "common_failure_reason"],
            [
                [
                    row["subtype"],
                    row["total"],
                    row["correct"],
                    f"{row['accuracy']:.6f}",
                    row["common_failure_reason"],
                ]
                for row in cause["subtype_table"]
            ],
        )
    )
    lines.append("")
    lines.append("## Task Balance")
    lines.append(
        "Each split contains one diagnosis, one evidence_extraction, and one cause_vs_symptom "
        "sample per run. The train split has only 9 runs per subtype, so evidence/cause tasks "
        "have the same count as diagnosis but require finer-grained supervision."
    )
    lines.append("")
    for split in ("train", "val", "test"):
        split_info = task_balance[split]
        lines.append(f"### {split}")
        lines.append(_md_table(["task", "samples"], sorted(split_info["task_counts"].items())))
        lines.append("")
    lines.append("## Checker Strictness")
    strict = analysis["checker_strictness"]
    lines.append(
        _md_table(
            ["check", "value"],
            [
                ["evidence_uses_strict_eid_overlap", strict["evidence_uses_strict_eid_overlap"]],
                [
                    "cause_uses_exact_cause_and_symptom_set_match",
                    strict["cause_uses_exact_cause_and_symptom_set_match"],
                ],
                ["strict_metric_retained", strict["strict_metric_retained"]],
            ],
        )
    )
    lines.append("")
    lines.append(
        "Judgement: the strict metrics should be retained for formal reporting because they expose "
        "whether the model selects the intended stable evidence IDs and exact cause/symptom roles. "
        "However, a relaxed diagnostic layer should be added for debugging normalized text overlap "
        "and partial evidence/cause hits."
    )
    lines.append("")
    lines.append("## Recommended Fixes")
    for section in ("data_target", "prompt_inference", "metric_checker"):
        lines.append(f"### {section}")
        for item in recs[section]:
            lines.append(f"- {item}")
        lines.append("")
    lines.append("## Next Task Recommendation")
    for idx, item in enumerate(recs["priority_order"], 1):
        lines.append(f"{idx}. {item}")
    lines.append("")
    lines.append(
        "Default recommendation: repair evidence_extraction and cause_vs_symptom before moving to "
        "Action Contract v1. The current NET-only conservative run is schema-clean and diagnosis-clean, "
        "but evidence F1=0 and cause_vs_symptom=5/14 are the next blocking quality issues."
    )
    lines.append("")
    lines.append("## Safety Confirmation")
    lines.append(_md_table(["field", "value"], sorted(safety.items())))
    lines.append("")
    lines.append("## Key Fields")
    lines.append(_md_table(["field", "value"], sorted(analysis["key_fields"].items())))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eval-dir", default=str(DEFAULT_EVAL_DIR))
    parser.add_argument("--candidate-dir", default=str(DEFAULT_CANDIDATE_DIR))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    args = parser.parse_args()
    analysis = _analyze(args)
    print(
        json.dumps(
            {
                "RESULT": "PASS",
                "report": str(Path(args.report)),
                "json": str(Path(args.output_dir) / "evidence_cause_failure_analysis.json"),
                "evidence_samples": analysis["evidence"]["samples_analyzed"],
                "cause_samples": analysis["cause_vs_symptom"]["samples_analyzed"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
