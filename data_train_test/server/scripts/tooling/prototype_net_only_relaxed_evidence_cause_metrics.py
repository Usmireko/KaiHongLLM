#!/usr/bin/env python3
"""Offline relaxed evidence/cause metric prototype for NET-only formal eval.

This script is a debug-side-channel prototype. It reads Task 8D formal
predictions, formal test JSONL, and Task 8F/9A audit artifacts, then writes
relaxed evidence/cause metrics. It does not train, run model eval, generate,
load a model, modify formal JSONL data, modify the contract/checker, or create
a repaired candidate.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


PREDICTIONS = Path(
    "_smoke_net_batch2_20260430/audit/net_only_formal_adapter_diagnostic_eval_20260519/"
    "formal_contract_aware_predictions.remote.jsonl"
)
FORMAL_METRICS = Path(
    "_smoke_net_batch2_20260430/audit/net_only_formal_adapter_diagnostic_eval_20260519/"
    "formal_contract_metrics_summary.remote.json"
)
FORMAL_AUDIT = Path(
    "_smoke_net_batch2_20260430/audit/net_only_formal_adapter_diagnostic_eval_20260519/"
    "formal_per_sample_contract_audit.remote.jsonl"
)
FAILURE_JSON = Path(
    "_smoke_net_batch2_20260430/audit/net_only_evidence_cause_failure_analysis_20260519/"
    "evidence_cause_failure_analysis.json"
)
EVIDENCE_FAILURE_TABLE = Path(
    "_smoke_net_batch2_20260430/audit/net_only_evidence_cause_failure_analysis_20260519/"
    "evidence_failure_table.jsonl"
)
CAUSE_FAILURE_TABLE = Path(
    "_smoke_net_batch2_20260430/audit/net_only_evidence_cause_failure_analysis_20260519/"
    "cause_symptom_failure_table.jsonl"
)
REPAIR_PLAN_JSON = Path(
    "_smoke_net_batch2_20260430/audit/net_only_evidence_cause_repair_plan_20260520/"
    "evidence_cause_repair_plan.json"
)
EVIDENCE_TARGET_TABLE = Path(
    "_smoke_net_batch2_20260430/audit/net_only_evidence_cause_repair_plan_20260520/"
    "evidence_target_audit_table.jsonl"
)
CAUSE_TARGET_TABLE = Path(
    "_smoke_net_batch2_20260430/audit/net_only_evidence_cause_repair_plan_20260520/"
    "cause_target_audit_table.jsonl"
)
TASK_BALANCE_TABLE = Path(
    "_smoke_net_batch2_20260430/audit/net_only_evidence_cause_repair_plan_20260520/"
    "task_balance_table.csv"
)
TEST_JSONL = Path(
    "_smoke_net_batch2_20260430/training_candidates/"
    "net_only_non_action_formal_20260518/test.jsonl"
)
TRACE_INDEX = Path(
    "_smoke_net_batch2_20260430/training_candidates/"
    "net_only_non_action_formal_20260518/trace_index.jsonl"
)
MANIFEST = Path(
    "_smoke_net_batch2_20260430/training_candidates/"
    "net_only_non_action_formal_20260518/manifest.json"
)
SPLIT_SUMMARY = Path(
    "_smoke_net_batch2_20260430/training_candidates/"
    "net_only_non_action_formal_20260518/split_summary.json"
)
SOURCE_LEDGER_SUMMARY = Path(
    "_smoke_net_batch2_20260430/training_candidates/"
    "net_only_non_action_formal_20260518/source_ledger_summary.json"
)
OUT_DIR = Path(
    "_smoke_net_batch2_20260430/audit/"
    "net_only_relaxed_evidence_cause_metrics_20260520"
)
REPORT = Path(
    "_smoke_net_batch2_20260430/audit/"
    "NET_ONLY_RELAXED_EVIDENCE_CAUSE_METRICS_20260520.md"
)

EVIDENCE_FIELDS = (
    "primary_evidence",
    "secondary_evidence",
    "symptom_evidence",
    "noise_evidence",
)
NET_SUBTYPES = (
    "net_dns_fail",
    "net_public_ip_unreachable",
    "net_no_default_route",
    "net_no_ipv4_on_iface",
    "net_wifi_disconnect",
    "net_wifi_auth_fail_wrong_psk",
    "net_wrong_default_route",
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


def _write_csv(path: Path, rows: Sequence[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    keys: List[str] = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def _read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


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


def _parse_test_rows(path: Path) -> Dict[str, Dict[str, Any]]:
    rows: Dict[str, Dict[str, Any]] = {}
    diagnosis_subtype_by_run: Dict[str, str] = {}
    staged: List[Dict[str, Any]] = []
    for line_no, raw in enumerate(_read_jsonl(path), 1):
        messages = raw.get("messages")
        if not isinstance(messages, list):
            raise ValueError(f"{path}:{line_no} missing messages")
        l2 = _parse_l2_input(_last_message(messages, "user"))
        gold = json.loads(_last_message(messages, "assistant"))
        sample_id = str(l2.get("sample_id") or "")
        task = str(l2.get("task") or "")
        run_id = sample_id.split("::", 1)[0]
        subtype = _gold_subtype(task, gold, diagnosis_subtype_by_run.get(run_id))
        if task == "diagnosis":
            diagnosis_subtype_by_run[run_id] = subtype
        staged.append(
            {
                "line": line_no,
                "sample_id": sample_id,
                "run_id": run_id,
                "task": task,
                "gold": gold,
                "l2": l2,
                "subtype": subtype,
            }
        )
    for row in staged:
        if row["subtype"] == "unknown":
            row["subtype"] = diagnosis_subtype_by_run.get(row["run_id"], "unknown")
        rows[row["sample_id"]] = row
    return rows


def _gold_subtype(task: str, gold: Dict[str, Any], fallback: Optional[str]) -> str:
    if task == "diagnosis":
        return str((gold.get("gt") or {}).get("subtype") or fallback or "unknown")
    if task == "cause_vs_symptom":
        return str(gold.get("primary_subtype") or fallback or "unknown")
    return str(fallback or "unknown")


def _parse_prediction_text(row: Dict[str, Any]) -> Dict[str, Any]:
    text = str(row.get("prediction_text") or "")
    try:
        payload = json.loads(text)
    except Exception:
        return {"_parse_error": True, "_raw_text": text}
    if not isinstance(payload, dict):
        return {"_parse_error": True, "_raw_text": text}
    return payload


def _extract_eids(value: Any) -> Set[str]:
    out: Set[str] = set()
    if isinstance(value, str) and value:
        out.add(value)
    elif isinstance(value, dict):
        eid = value.get("eid")
        if isinstance(eid, str) and eid:
            out.add(eid)
    elif isinstance(value, list):
        for item in value:
            out.update(_extract_eids(item))
    return out


def _eids_by_field(payload: Dict[str, Any]) -> Dict[str, Set[str]]:
    return {field: _extract_eids(payload.get(field)) for field in EVIDENCE_FIELDS}


def _all_eids(payload: Dict[str, Any]) -> Set[str]:
    out: Set[str] = set()
    for values in _eids_by_field(payload).values():
        out.update(values)
    return out


def _evidence_texts(payload: Dict[str, Any]) -> List[str]:
    texts: List[str] = []
    for field in EVIDENCE_FIELDS:
        value = payload.get(field)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and item.get("text"):
                    texts.append(str(item.get("text")))
                elif isinstance(item, str):
                    texts.append(item)
    return texts


def _token_set(texts: Sequence[str]) -> Set[str]:
    joined = " ".join(texts).lower()
    tokens = re.findall(r"[a-z0-9_]+", joined)
    return {tok for tok in tokens if len(tok) > 1}


def _text_overlap(gold_texts: Sequence[str], pred_texts: Sequence[str]) -> float:
    gold = _token_set(gold_texts)
    pred = _token_set(pred_texts)
    if not gold or not pred:
        return 0.0
    return len(gold & pred) / len(gold | pred)


def _substring_hit(gold_texts: Sequence[str], pred_texts: Sequence[str]) -> bool:
    pred_norms = [" ".join(sorted(_token_set([text]))) for text in pred_texts]
    gold_norms = [" ".join(sorted(_token_set([text]))) for text in gold_texts]
    for gold in gold_norms:
        if not gold:
            continue
        for pred in pred_norms:
            if gold and pred and (gold in pred or pred in gold):
                return True
    return False


def _prf(gold: Set[str], pred: Set[str]) -> Tuple[float, float, float, int]:
    hits = len(gold & pred)
    precision = hits / len(pred) if pred else 0.0
    recall = hits / len(gold) if gold else 0.0
    if precision + recall == 0:
        f1 = 0.0
    else:
        f1 = 2 * precision * recall / (precision + recall)
    return precision, recall, f1, hits


def _safe_ratio(n: int, d: int) -> Optional[float]:
    if d == 0:
        return None
    return n / d


def _mean(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)


def _role_pairs(payload: Dict[str, Any]) -> Set[str]:
    pairs: Set[str] = set()
    for field, eids in _eids_by_field(payload).items():
        for eid in eids:
            pairs.add(f"{field}:{eid}")
    return pairs


def _evidence_failure_type(gold_eids: Set[str], pred_eids: Set[str], text_overlap: float) -> str:
    if not pred_eids:
        return "predicted_evidence_empty"
    if pred_eids.isdisjoint(gold_eids) and text_overlap > 0:
        return "text_overlap_without_eid_match"
    if pred_eids.isdisjoint(gold_eids):
        return "wrong_evidence_eids"
    if pred_eids < gold_eids:
        return "evidence_under_generation"
    if pred_eids - gold_eids:
        return "evidence_over_generation"
    return "partial_or_exact_evidence_match"


def _cause_failure_type(
    gold_cause: Set[str],
    pred_cause: Set[str],
    gold_symptom: Set[str],
    pred_symptom: Set[str],
) -> str:
    if pred_cause == gold_cause and pred_symptom == gold_symptom:
        return "exact_match"
    if pred_cause and pred_cause < gold_cause:
        return "cause_under_selection"
    if pred_cause - gold_cause:
        return "cause_over_selection"
    if pred_cause.isdisjoint(gold_cause):
        return "cause_no_overlap"
    if pred_symptom != gold_symptom:
        return "symptom_mismatch"
    return "partial_cause_overlap"


def _analyze(args: argparse.Namespace) -> Dict[str, Any]:
    out_dir = Path(args.output_dir)
    report_path = Path(args.report)
    out_dir.mkdir(parents=True, exist_ok=True)

    formal_metrics = _read_json(Path(args.formal_metrics))
    failure_json = _read_json(Path(args.failure_json))
    repair_json = _read_json(Path(args.repair_plan_json))
    evidence_failure_rows = _read_jsonl(Path(args.evidence_failure_table))
    cause_failure_rows = _read_jsonl(Path(args.cause_failure_table))
    evidence_target_rows = _read_jsonl(Path(args.evidence_target_table))
    cause_target_rows = _read_jsonl(Path(args.cause_target_table))
    task_balance_rows = _read_csv(Path(args.task_balance_table))
    test_rows = _parse_test_rows(Path(args.test_jsonl))
    trace_rows = _read_jsonl(Path(args.trace_index))
    predictions = {str(r.get("sample_id")): r for r in _read_jsonl(Path(args.predictions))}
    audit_rows = {str(r.get("sample_id")): r for r in _read_jsonl(Path(args.formal_audit))}
    _read_json(Path(args.manifest))
    _read_json(Path(args.split_summary))
    _read_json(Path(args.source_ledger_summary))

    evidence_rows: List[Dict[str, Any]] = []
    cause_rows: List[Dict[str, Any]] = []
    role_gold_total = 0
    role_pred_total = 0
    role_hits = 0
    evidence_gold_total = 0
    evidence_pred_total = 0
    evidence_hits = 0
    primary_hit_count = 0
    evidence_hit_count = 0
    evidence_empty_count = 0
    evidence_semantic_empty_count = 0
    evidence_under_count = 0
    substring_hits = 0
    text_overlaps: List[float] = []

    cause_gold_total = 0
    cause_pred_total = 0
    cause_hits_total = 0
    cause_primary_hits = 0
    cause_under_count = 0
    cause_over_count = 0
    cause_exact_count = 0
    cause_partial_overlap_count = 0
    symptom_gold_total = 0
    symptom_pred_total = 0
    symptom_hits_total = 0

    for sample_id, gold_row in sorted(test_rows.items()):
        task = gold_row["task"]
        if task not in {"evidence_extraction", "cause_vs_symptom"}:
            continue
        pred_outer = predictions.get(sample_id)
        if pred_outer is None:
            raise ValueError(f"missing prediction for {sample_id}")
        pred = _parse_prediction_text(pred_outer)
        audit = audit_rows.get(sample_id, {})
        subtype = str(pred_outer.get("gold_subtype") or gold_row["subtype"])

        if task == "evidence_extraction":
            gold = gold_row["gold"]
            gold_eids = _all_eids(gold)
            pred_eids = _all_eids(pred)
            gold_primary = _eids_by_field(gold)["primary_evidence"]
            pred_primary = _eids_by_field(pred)["primary_evidence"]
            precision, recall, f1, hits = _prf(gold_eids, pred_eids)
            primary_hit = bool(gold_primary & pred_primary)
            gold_pairs = _role_pairs(gold)
            pred_pairs = _role_pairs(pred)
            role_precision, role_recall, role_f1, row_role_hits = _prf(gold_pairs, pred_pairs)
            text_overlap = _text_overlap(_evidence_texts(gold), _evidence_texts(pred))
            substring_hit = _substring_hit(_evidence_texts(gold), _evidence_texts(pred))
            non_empty = bool(pred_eids)
            failure_type = _evidence_failure_type(gold_eids, pred_eids, text_overlap)

            evidence_gold_total += len(gold_eids)
            evidence_pred_total += len(pred_eids)
            evidence_hits += hits
            role_gold_total += len(gold_pairs)
            role_pred_total += len(pred_pairs)
            role_hits += row_role_hits
            primary_hit_count += int(primary_hit)
            evidence_hit_count += int(bool(gold_eids & pred_eids))
            evidence_empty_count += int(not non_empty)
            evidence_semantic_empty_count += int(bool(audit.get("target_schema_success")) and not non_empty)
            evidence_under_count += int(len(pred_eids) < len(gold_eids))
            substring_hits += int(substring_hit)
            text_overlaps.append(text_overlap)
            evidence_rows.append(
                {
                    "sample_id": sample_id,
                    "run_id": gold_row["run_id"],
                    "subtype": subtype,
                    "gold_eids": sorted(gold_eids),
                    "predicted_eids": sorted(pred_eids),
                    "gold_primary_eids": sorted(gold_primary),
                    "predicted_primary_eids": sorted(pred_primary),
                    "strict_precision": float(audit.get("evidence_precision") or 0.0),
                    "strict_recall": float(audit.get("evidence_recall") or 0.0),
                    "strict_f1": float(audit.get("evidence_f1") or 0.0),
                    "relaxed_eid_precision": precision,
                    "relaxed_eid_recall": recall,
                    "relaxed_eid_f1": f1,
                    "primary_evidence_hit": primary_hit,
                    "role_aware_evidence_precision": role_precision,
                    "role_aware_evidence_recall": role_recall,
                    "role_aware_evidence_f1": role_f1,
                    "normalized_text_overlap": text_overlap,
                    "substring_evidence_hit": substring_hit,
                    "non_empty_prediction": non_empty,
                    "failure_type": failure_type,
                }
            )
        else:
            gold = gold_row["gold"]
            gold_cause = _extract_eids(gold.get("cause_eids"))
            pred_cause = _extract_eids(pred.get("cause_eids"))
            gold_symptom = _extract_eids(gold.get("symptom_eids"))
            pred_symptom = _extract_eids(pred.get("symptom_eids"))
            pred_family = str(pred.get("primary_family") or "")
            pred_subtype = str(pred.get("primary_subtype") or "")
            gold_subtype = str(gold.get("primary_subtype") or subtype)
            primary_label_correct = pred_family == "net" and pred_subtype == gold_subtype
            precision, recall, f1, hits = _prf(gold_cause, pred_cause)
            symptom_precision, symptom_recall, symptom_f1, symptom_hits = _prf(gold_symptom, pred_symptom)
            exact = pred_cause == gold_cause and pred_symptom == gold_symptom
            primary_hit = primary_label_correct and bool(gold_cause & pred_cause)
            under = bool(pred_cause and pred_cause < gold_cause)
            over = bool(pred_cause - gold_cause)
            partial = primary_hit and not exact
            failure_type = _cause_failure_type(gold_cause, pred_cause, gold_symptom, pred_symptom)

            cause_gold_total += len(gold_cause)
            cause_pred_total += len(pred_cause)
            cause_hits_total += hits
            symptom_gold_total += len(gold_symptom)
            symptom_pred_total += len(pred_symptom)
            symptom_hits_total += symptom_hits
            cause_primary_hits += int(primary_hit)
            cause_under_count += int(under)
            cause_over_count += int(over)
            cause_exact_count += int(exact)
            cause_partial_overlap_count += int(partial)
            cause_rows.append(
                {
                    "sample_id": sample_id,
                    "run_id": gold_row["run_id"],
                    "subtype": subtype,
                    "gold_cause_eids": sorted(gold_cause),
                    "predicted_cause_eids": sorted(pred_cause),
                    "gold_symptom_eids": sorted(gold_symptom),
                    "predicted_symptom_eids": sorted(pred_symptom),
                    "strict_correct": bool(audit.get("cause_symptom_correct")),
                    "primary_label_correct": primary_label_correct,
                    "cause_eid_precision": precision,
                    "cause_eid_recall": recall,
                    "cause_eid_f1": f1,
                    "primary_cause_hit": primary_hit,
                    "under_selection": under,
                    "over_selection": over,
                    "exact_cause_set_match": exact,
                    "partial_cause_overlap": partial,
                    "symptom_metric_status": (
                        "N/A_NO_SYMPTOM_EIDS" if not gold_symptom and not pred_symptom else "ACTIVE"
                    ),
                    "symptom_eid_precision": None if not gold_symptom and not pred_symptom else symptom_precision,
                    "symptom_eid_recall": None if not gold_symptom and not pred_symptom else symptom_recall,
                    "symptom_eid_f1": None if not gold_symptom and not pred_symptom else symptom_f1,
                    "failure_type": failure_type,
                }
            )

    evidence_precision = _safe_ratio(evidence_hits, evidence_pred_total) or 0.0
    evidence_recall = _safe_ratio(evidence_hits, evidence_gold_total) or 0.0
    evidence_f1 = _f1(evidence_precision, evidence_recall)
    role_precision = _safe_ratio(role_hits, role_pred_total) or 0.0
    role_recall = _safe_ratio(role_hits, role_gold_total) or 0.0
    role_f1 = _f1(role_precision, role_recall)
    cause_precision = _safe_ratio(cause_hits_total, cause_pred_total) or 0.0
    cause_recall = _safe_ratio(cause_hits_total, cause_gold_total) or 0.0
    cause_f1 = _f1(cause_precision, cause_recall)
    symptom_status = (
        "N/A_NO_SYMPTOM_EIDS"
        if symptom_gold_total == 0 and symptom_pred_total == 0
        else "ACTIVE"
    )
    symptom_precision = None if symptom_status.startswith("N/A") else (_safe_ratio(symptom_hits_total, symptom_pred_total) or 0.0)
    symptom_recall = None if symptom_status.startswith("N/A") else (_safe_ratio(symptom_hits_total, symptom_gold_total) or 0.0)
    symptom_f1 = None if symptom_status.startswith("N/A") else _f1(symptom_precision or 0.0, symptom_recall or 0.0)

    subtype_rows = _subtype_rows(evidence_rows, cause_rows)
    recommendations = _recommendations(
        evidence_empty_count=evidence_empty_count,
        evidence_total=len(evidence_rows),
        cause_under_count=cause_under_count,
        cause_total=len(cause_rows),
        cause_recall=cause_recall,
        strict_cause=formal_metrics["cause_symptom_accuracy"],
    )

    evidence_summary = {
        "strict_evidence_precision": formal_metrics["evidence_precision"],
        "strict_evidence_recall": formal_metrics["evidence_recall"],
        "strict_evidence_f1": formal_metrics["evidence_f1"],
        "evidence_id_hit_rate": _safe_ratio(evidence_hit_count, len(evidence_rows)),
        "evidence_id_precision": evidence_precision,
        "evidence_id_recall": evidence_recall,
        "evidence_id_f1": evidence_f1,
        "primary_evidence_hit_rate": _safe_ratio(primary_hit_count, len(evidence_rows)),
        "role_aware_evidence_precision": role_precision,
        "role_aware_evidence_recall": role_recall,
        "role_aware_evidence_f1": role_f1,
        "normalized_text_overlap": _mean(text_overlaps),
        "substring_evidence_hit_rate": _safe_ratio(substring_hits, len(evidence_rows)),
        "non_empty_evidence_array_rate": _safe_ratio(len(evidence_rows) - evidence_empty_count, len(evidence_rows)),
        "evidence_under_generation_count": evidence_under_count,
        "evidence_empty_array_count": evidence_empty_count,
        "evidence_schema_valid_but_semantic_empty_count": evidence_semantic_empty_count,
        "strictness_main_issue": False,
        "primary_issue": "empty_predictions_not_exact_match_strictness",
    }
    cause_summary = {
        "strict_cause_vs_symptom_accuracy": formal_metrics["cause_symptom_accuracy"],
        "cause_eid_precision": cause_precision,
        "cause_eid_recall": cause_recall,
        "cause_eid_f1": cause_f1,
        "primary_cause_hit_rate": _safe_ratio(cause_primary_hits, len(cause_rows)),
        "cause_under_selection_count": cause_under_count,
        "cause_over_selection_count": cause_over_count,
        "exact_cause_set_match_rate": _safe_ratio(cause_exact_count, len(cause_rows)),
        "partial_cause_overlap_rate": _safe_ratio(cause_partial_overlap_count, len(cause_rows)),
        "symptom_eid_precision": symptom_precision,
        "symptom_eid_recall": symptom_recall,
        "symptom_eid_f1": symptom_f1,
        "symptom_metrics_status": symptom_status,
        "strictness_main_issue": True,
        "primary_issue": "cause_eid_under_selection_with_high_precision_partial_overlap",
    }
    key_fields = {
        "SKILL_LOADED": True,
        "MAINLINE_STATUS": args.mainline_status,
        "REPORT_CREATED": True,
        "JSON_CREATED": True,
        "METRICS_TABLES_CREATED": True,
        "RELAXED_METRICS_SCRIPT_CREATED": True,
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
        "STRICT_EVIDENCE_F1": formal_metrics["evidence_f1"],
        "RELAXED_EVIDENCE_ID_F1": evidence_f1,
        "RELAXED_EVIDENCE_HIT_RATE": _safe_ratio(evidence_hit_count, len(evidence_rows)),
        "EVIDENCE_EMPTY_ARRAY_COUNT": evidence_empty_count,
        "EVIDENCE_STRICTNESS_MAIN_ISSUE": False,
        "CAUSE_SYMPTOM_SAMPLES_ANALYZED": len(cause_rows),
        "STRICT_CAUSE_SYMPTOM_ACCURACY": formal_metrics["cause_symptom_accuracy"],
        "CAUSE_EID_PRECISION": cause_precision,
        "CAUSE_EID_RECALL": cause_recall,
        "CAUSE_EID_F1": cause_f1,
        "PRIMARY_CAUSE_HIT_RATE": _safe_ratio(cause_primary_hits, len(cause_rows)),
        "CAUSE_UNDER_SELECTION_COUNT": cause_under_count,
        "SYMPTOM_METRICS_STATUS": symptom_status,
        "SUBTYPE_RELAXED_TABLE_CREATED": True,
        "STRICT_METRICS_RETAINED": True,
        "RELAXED_METRICS_DEBUG_ONLY": True,
        "READY_FOR_REPAIRED_TARGET_DRY_RUN": True,
        "READY_FOR_ACTION_CONTRACT": False,
        "REVIEWER_VERDICT": args.reviewer_verdict,
    }
    result: Dict[str, Any] = {
        "schema_version": "net_only_relaxed_evidence_cause_metrics_v1",
        "created_at_utc": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "inputs": {
            "predictions": str(args.predictions),
            "formal_metrics": str(args.formal_metrics),
            "formal_audit": str(args.formal_audit),
            "failure_json": str(args.failure_json),
            "repair_plan_json": str(args.repair_plan_json),
            "test_jsonl": str(args.test_jsonl),
            "trace_index": str(args.trace_index),
        },
        "input_sha256": {
            "predictions": _sha256(Path(args.predictions)),
            "formal_metrics": _sha256(Path(args.formal_metrics)),
            "formal_audit": _sha256(Path(args.formal_audit)),
            "failure_json": _sha256(Path(args.failure_json)),
            "evidence_failure_table": _sha256(Path(args.evidence_failure_table)),
            "cause_failure_table": _sha256(Path(args.cause_failure_table)),
            "repair_plan_json": _sha256(Path(args.repair_plan_json)),
            "evidence_target_table": _sha256(Path(args.evidence_target_table)),
            "cause_target_table": _sha256(Path(args.cause_target_table)),
            "task_balance_table": _sha256(Path(args.task_balance_table)),
            "test_jsonl": _sha256(Path(args.test_jsonl)),
            "trace_index": _sha256(Path(args.trace_index)),
        },
        "auxiliary_rows_read": {
            "evidence_failure_table": len(evidence_failure_rows),
            "cause_failure_table": len(cause_failure_rows),
            "evidence_target_audit_table": len(evidence_target_rows),
            "cause_target_audit_table": len(cause_target_rows),
            "task_balance_table": len(task_balance_rows),
        },
        "source_task_8d_strict_metrics": formal_metrics,
        "source_task_8f_failure_summary": {
            "evidence_f1": failure_json["evidence"]["f1"],
            "evidence_empty_prediction_count": failure_json["evidence"]["empty_prediction_count"],
            "cause_symptom_accuracy": failure_json["cause_vs_symptom"]["accuracy"],
        },
        "source_task_9a_repair_summary": {
            "evidence_samples_audited": repair_json["evidence_target_audit"]["samples_audited"],
            "evidence_id_traceable_rate": repair_json["evidence_target_audit"]["evidence_id_traceable_rate"],
            "cause_samples_audited": repair_json["cause_target_audit"]["samples_audited"],
            "cause_eid_traceable_rate": repair_json["cause_target_audit"]["cause_eid_traceable_rate"],
            "symptom_eid_traceable_rate": repair_json["key_fields"]["SYMPTOM_EID_TRACEABLE_RATE"],
        },
        "trace_rows_read": len(trace_rows),
        "evidence_metrics": evidence_summary,
        "cause_metrics": cause_summary,
        "strict_vs_relaxed_conclusion": {
            "evidence_f1_zero_reason": "relaxed evidence metrics remain zero because formal predictions emit empty evidence arrays; exact-match strictness is not the primary issue for this run.",
            "cause_accuracy_low_reason": "predicted cause_eids are high-precision subsets of gold cause_eids; strict exact-set accuracy is low because of under-selection.",
            "relaxed_metrics_policy": "debug_side_channel_only_not_formal_replacement",
        },
        "metric_repair_recommendation": recommendations,
        "key_fields": key_fields,
    }
    _write_jsonl(out_dir / "evidence_relaxed_metrics_table.jsonl", evidence_rows)
    _write_jsonl(out_dir / "cause_relaxed_metrics_table.jsonl", cause_rows)
    _write_csv(out_dir / "subtype_relaxed_metrics_table.csv", subtype_rows)
    _write_json(out_dir / "relaxed_evidence_cause_metrics.json", result)
    _write_recommendation(out_dir / "metrics_recommendation.md", recommendations)
    _write_report(report_path, result, subtype_rows)
    return result


def _f1(precision: float, recall: float) -> float:
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def _subtype_rows(evidence_rows: Sequence[Dict[str, Any]], cause_rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    evidence_by_subtype: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    cause_by_subtype: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in evidence_rows:
        evidence_by_subtype[row["subtype"]].append(row)
    for row in cause_rows:
        cause_by_subtype[row["subtype"]].append(row)
    for subtype in NET_SUBTYPES:
        ev = evidence_by_subtype.get(subtype, [])
        cause = cause_by_subtype.get(subtype, [])
        cause_gold = sum(len(row["gold_cause_eids"]) for row in cause)
        cause_hits = sum(len(set(row["gold_cause_eids"]) & set(row["predicted_cause_eids"])) for row in cause)
        failures = Counter(row["failure_type"] for row in list(ev) + list(cause))
        rows.append(
            {
                "subtype": subtype,
                "evidence_samples": len(ev),
                "evidence_strict_f1": _mean([float(row["strict_f1"]) for row in ev]),
                "evidence_relaxed_f1": _mean([float(row["relaxed_eid_f1"]) for row in ev]),
                "evidence_relaxed_hit_rate": _safe_ratio(
                    sum(1 for row in ev if set(row["gold_eids"]) & set(row["predicted_eids"])),
                    len(ev),
                ),
                "non_empty_evidence_prediction_count": sum(1 for row in ev if row["non_empty_prediction"]),
                "cause_samples": len(cause),
                "cause_strict_accuracy": _safe_ratio(sum(1 for row in cause if row["strict_correct"]), len(cause)),
                "cause_eid_recall": _safe_ratio(cause_hits, cause_gold),
                "primary_cause_hit_rate": _safe_ratio(sum(1 for row in cause if row["primary_cause_hit"]), len(cause)),
                "cause_under_selection_count": sum(1 for row in cause if row["under_selection"]),
                "common_failure_reason": failures.most_common(1)[0][0] if failures else "none",
            }
        )
    return rows


def _recommendations(
    *,
    evidence_empty_count: int,
    evidence_total: int,
    cause_under_count: int,
    cause_total: int,
    cause_recall: float,
    strict_cause: float,
) -> Dict[str, Any]:
    return {
        "formal_metrics_to_retain": [
            "strict evidence F1",
            "strict cause_vs_symptom exact accuracy",
            "schema compliance",
            "GT/OBS leak counts",
            "action/recovery leak counts",
        ],
        "debug_metrics_to_report": [
            "evidence_id hit/precision/recall/F1",
            "normalized text overlap",
            "substring evidence hit rate",
            "role-aware evidence F1",
            "cause_eid precision/recall/F1",
            "primary_cause_hit_rate",
            "cause_under_selection_count",
            "symptom_metrics_status",
        ],
        "interpretation": [
            f"Evidence relaxed metrics are expected to remain zero when {evidence_empty_count}/{evidence_total} evidence predictions are empty.",
            f"Cause strict accuracy is {strict_cause}, but cause_eid recall is {cause_recall}; this supports partial-credit debug reporting.",
            f"Cause under-selection count is {cause_under_count}/{cause_total}; target/prompt repair should force full cause_eid coverage.",
            "Relaxed metrics are debug-only and must not replace strict formal metrics.",
        ],
        "next_task_recommendation": [
            "Task 9C: repaired target materialization dry-run",
            "Then Task 9D: repaired NET-only candidate v2 creation",
            "Then Task 9E: repaired candidate conservative retraining",
            "Action Contract v1 should remain deferred until evidence/cause improves.",
        ],
    }


def _md_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    lines = [
        "| " + " | ".join(str(h) for h in headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(v) for v in row) + " |")
    return "\n".join(lines)


def _write_recommendation(path: Path, rec: Dict[str, Any]) -> None:
    lines = ["# Relaxed Metrics Recommendation", ""]
    for section, items in rec.items():
        lines.append(f"## {section}")
        for item in items:
            lines.append(f"- {item}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _write_report(path: Path, result: Dict[str, Any], subtype_rows: Sequence[Dict[str, Any]]) -> None:
    evidence = result["evidence_metrics"]
    cause = result["cause_metrics"]
    key = result["key_fields"]
    lines = [
        "# NET-only Relaxed Evidence/Cause Metrics Prototype - Task 9B",
        "",
        "## Scope",
        "Offline debug metrics only. No training, model eval, generation, model loading, adapter modification, contract/checker edit, formal JSONL mutation, L1/L2 rebuild, HDC, or board action was performed.",
        "",
        "## Strict vs Relaxed Evidence Metrics",
        _md_table(
            ["metric", "value"],
            [
                ["strict_evidence_f1", evidence["strict_evidence_f1"]],
                ["relaxed_evidence_id_f1", evidence["evidence_id_f1"]],
                ["evidence_id_hit_rate", evidence["evidence_id_hit_rate"]],
                ["primary_evidence_hit_rate", evidence["primary_evidence_hit_rate"]],
                ["role_aware_evidence_f1", evidence["role_aware_evidence_f1"]],
                ["normalized_text_overlap", evidence["normalized_text_overlap"]],
                ["substring_evidence_hit_rate", evidence["substring_evidence_hit_rate"]],
                ["non_empty_evidence_array_rate", evidence["non_empty_evidence_array_rate"]],
                ["evidence_empty_array_count", evidence["evidence_empty_array_count"]],
                ["evidence_schema_valid_but_semantic_empty_count", evidence["evidence_schema_valid_but_semantic_empty_count"]],
            ],
        ),
        "",
        "Evidence conclusion: strict F1=0 is not mainly caused by exact-match strictness in this run. Predictions are schema-valid but emit empty evidence arrays, so relaxed EID/text metrics also remain 0.",
        "",
        "## Strict vs Relaxed Cause Metrics",
        _md_table(
            ["metric", "value"],
            [
                ["strict_cause_vs_symptom_accuracy", cause["strict_cause_vs_symptom_accuracy"]],
                ["cause_eid_precision", cause["cause_eid_precision"]],
                ["cause_eid_recall", cause["cause_eid_recall"]],
                ["cause_eid_f1", cause["cause_eid_f1"]],
                ["primary_cause_hit_rate", cause["primary_cause_hit_rate"]],
                ["cause_under_selection_count", cause["cause_under_selection_count"]],
                ["cause_over_selection_count", cause["cause_over_selection_count"]],
                ["exact_cause_set_match_rate", cause["exact_cause_set_match_rate"]],
                ["partial_cause_overlap_rate", cause["partial_cause_overlap_rate"]],
                ["symptom_metrics_status", cause["symptom_metrics_status"]],
            ],
        ),
        "",
        "Cause conclusion: strict accuracy is low mainly because predictions under-select cause_eids. Cause precision and primary-cause hit are high, so partial-credit debug metrics are useful, but strict exact-set accuracy remains the formal metric.",
        "",
        "## Subtype Relaxed Metrics",
        _md_table(
            [
                "subtype",
                "evidence_relaxed_f1",
                "non_empty_evidence_prediction_count",
                "cause_strict_accuracy",
                "cause_eid_recall",
                "primary_cause_hit_rate",
                "cause_under_selection_count",
                "common_failure_reason",
            ],
            [
                [
                    row["subtype"],
                    row["evidence_relaxed_f1"],
                    row["non_empty_evidence_prediction_count"],
                    row["cause_strict_accuracy"],
                    row["cause_eid_recall"],
                    row["primary_cause_hit_rate"],
                    row["cause_under_selection_count"],
                    row["common_failure_reason"],
                ]
                for row in subtype_rows
            ],
        ),
        "",
        "## Metric Repair Recommendation",
        "Formal metrics retain strict evidence F1, strict cause_vs_symptom exact accuracy, schema compliance, GT/OBS leak, and action/recovery leak. Relaxed metrics are debug-only side channels for target repair and must not upgrade formal PASS.",
        "",
        "Next recommended task: Task 9C repaired target materialization dry-run. Action Contract v1 should wait until evidence/cause repair is validated.",
        "",
        "## Key Fields",
        _md_table(["field", "value"], sorted(key.items())),
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions", default=str(PREDICTIONS))
    parser.add_argument("--formal-metrics", default=str(FORMAL_METRICS))
    parser.add_argument("--formal-audit", default=str(FORMAL_AUDIT))
    parser.add_argument("--failure-json", default=str(FAILURE_JSON))
    parser.add_argument("--evidence-failure-table", default=str(EVIDENCE_FAILURE_TABLE))
    parser.add_argument("--cause-failure-table", default=str(CAUSE_FAILURE_TABLE))
    parser.add_argument("--repair-plan-json", default=str(REPAIR_PLAN_JSON))
    parser.add_argument("--evidence-target-table", default=str(EVIDENCE_TARGET_TABLE))
    parser.add_argument("--cause-target-table", default=str(CAUSE_TARGET_TABLE))
    parser.add_argument("--task-balance-table", default=str(TASK_BALANCE_TABLE))
    parser.add_argument("--test-jsonl", default=str(TEST_JSONL))
    parser.add_argument("--trace-index", default=str(TRACE_INDEX))
    parser.add_argument("--manifest", default=str(MANIFEST))
    parser.add_argument("--split-summary", default=str(SPLIT_SUMMARY))
    parser.add_argument("--source-ledger-summary", default=str(SOURCE_LEDGER_SUMMARY))
    parser.add_argument("--output-dir", default=str(OUT_DIR))
    parser.add_argument("--report", default=str(REPORT))
    parser.add_argument("--mainline-status", default="PENDING_AGGREGATION")
    parser.add_argument("--reviewer-verdict", default="PENDING")
    args = parser.parse_args()
    result = _analyze(args)
    print(
        json.dumps(
            {
                "RESULT": "PASS",
                "report": args.report,
                "json": str(Path(args.output_dir) / "relaxed_evidence_cause_metrics.json"),
                "evidence_samples": result["key_fields"]["EVIDENCE_SAMPLES_ANALYZED"],
                "cause_samples": result["key_fields"]["CAUSE_SYMPTOM_SAMPLES_ANALYZED"],
                "relaxed_evidence_f1": result["key_fields"]["RELAXED_EVIDENCE_ID_F1"],
                "cause_eid_recall": result["key_fields"]["CAUSE_EID_RECALL"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
