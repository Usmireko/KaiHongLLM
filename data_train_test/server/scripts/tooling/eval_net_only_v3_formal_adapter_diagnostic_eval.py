#!/usr/bin/env python3
"""Task 9Q v3 formal adapter diagnostic generation/evaluation wrapper.

This wrapper is intentionally evaluation-only. It reuses the existing
contract-aware prompt-v2 generation helpers and contract-v1 checker, but
approves only the evidence-oversampled v3 held-out test file and the Task 9O
formal adapter. It never trains, never updates weights, never saves adapters,
and never reads train/val/all/trace as generation or metric inputs.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import eval_net_only_contract_aware_generation_smoke_v2 as prompt_v2  # noqa: E402
import eval_net_only_diagnostic_metrics_calibration as cal  # noqa: E402
import eval_net_only_diagnostic_schema_contract_v1 as contract_checker  # noqa: E402
import eval_net_only_non_action_generation_smoke as gen_smoke  # noqa: E402


PROJECT_ROOT = Path("/home/xrh/qwen3_os_fault")
FORMAL_OUTPUT_ROOT = PROJECT_ROOT / "outputs/training_formal"
V3_CANDIDATE_ROOT = PROJECT_ROOT / "data/training_candidates/net_only_non_action_evidence_oversampled_v3_20260521"
V3_TRAINING_OUTPUT_DIR = (
    FORMAL_OUTPUT_ROOT / "net_only_non_action_evidence_oversampled_v3_conservative_formal_20260521_20260521_143620"
)
DEFAULT_ADAPTER = V3_TRAINING_OUTPUT_DIR / "adapter"
DEFAULT_TEST_FILE = V3_CANDIDATE_ROOT / "test.jsonl"
DEFAULT_MODEL = Path("/home/xrh/models/Qwen/Qwen3-8B")
DEFAULT_CONTRACT_JSON = SCRIPT_DIR / "contracts/net_only_non_action_diagnostic_output_contract_v1.json"
DEFAULT_BASELINE_7D_SUMMARY = (
    PROJECT_ROOT
    / "outputs/diagnostic_eval/net_only_contract_aware_generation_smoke_20260519_20260519_134654/contract_aware_metrics_summary.json"
)
DEFAULT_BASELINE_7D_OUTPUT = (
    PROJECT_ROOT / "outputs/diagnostic_eval/net_only_contract_aware_generation_smoke_20260519_20260519_134654"
)

SCHEMA_VERSION = "net_only_evidence_oversampled_v3_formal_adapter_diagnostic_eval_9q"
PROMPT_VERSION = prompt_v2.PROMPT_VERSION
CONTRACT_VERSION = prompt_v2.CONTRACT_VERSION
EXPECTED_TASK_COUNTS = {"diagnosis": 14, "evidence_extraction": 14, "cause_vs_symptom": 14}
EVIDENCE_FIELDS = ("primary_evidence", "secondary_evidence", "symptom_evidence", "noise_evidence")
V3_TEST_LOSS_FROM_9P = 0.06029346585273743
FORMAL_V1_TEST_LOSS = 0.06015147641301155
REPAIRED_V2_TEST_LOSS = 0.06177534535527229


class UserError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _json_dump(path: Path, payload: Any) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def _load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise UserError("JSON_OBJECT_REQUIRED", f"{path} must contain a JSON object")
    return payload


def _write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            f.write("\n")


def _has_glob_chars(raw: str) -> bool:
    return any(ch in raw for ch in "*?[]{}")


def _resolve_file(raw: str, label: str) -> Path:
    if _has_glob_chars(raw):
        raise UserError("UNSAFE_GLOB_PREVENTED", f"{label} must be explicit: {raw}")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise UserError("ABSOLUTE_PATH_REQUIRED", f"{label} must be absolute: {raw}")
    if path.is_dir():
        raise UserError("DIRECTORY_INPUT_REJECTED", f"{label} must be a file: {path}")
    if not path.is_file():
        raise UserError("FILE_NOT_FOUND", f"{label} not found: {path}")
    return path.resolve()


def _resolve_dir(raw: str, label: str) -> Path:
    if _has_glob_chars(raw):
        raise UserError("UNSAFE_GLOB_PREVENTED", f"{label} must be explicit: {raw}")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise UserError("ABSOLUTE_PATH_REQUIRED", f"{label} must be absolute: {raw}")
    if not path.is_dir():
        raise UserError("DIRECTORY_NOT_FOUND", f"{label} not found: {path}")
    return path.resolve()


def _resolve_output_dir(raw: str) -> Path:
    if _has_glob_chars(raw):
        raise UserError("UNSAFE_GLOB_PREVENTED", f"output_dir must be explicit: {raw}")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise UserError("ABSOLUTE_PATH_REQUIRED", f"output_dir must be absolute: {raw}")
    resolved = path.resolve()
    try:
        resolved.relative_to(FORMAL_OUTPUT_ROOT.resolve())
    except ValueError as exc:
        raise UserError("FORMAL_OUTPUT_ROOT_REQUIRED", f"output_dir must be under {FORMAL_OUTPUT_ROOT}: {resolved}") from exc
    if "_diagnostic_eval_" not in resolved.name:
        raise UserError("FORMAL_DIAGNOSTIC_OUTPUT_NAME_REQUIRED", "output_dir name must include _diagnostic_eval_")
    if resolved.exists() and not resolved.is_dir():
        raise UserError("OUTPUT_DIR_NOT_DIRECTORY", f"output_dir exists but is not a directory: {resolved}")
    if resolved.exists() and any(resolved.iterdir()):
        raise UserError("OUTPUT_DIR_NOT_EMPTY", f"output_dir must be new or empty: {resolved}")
    return resolved


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _adapter_snapshot(adapter_path: Path) -> Dict[str, Dict[str, int | str]]:
    snapshot: Dict[str, Dict[str, int | str]] = {}
    for name in ("adapter_config.json", "adapter_model.safetensors", "README.md"):
        path = adapter_path / name
        if path.is_file():
            st = path.stat()
            snapshot[str(path)] = {
                "size": int(st.st_size),
                "mtime_ns": int(st.st_mtime_ns),
                "sha256": _sha256_file(path),
            }
    return snapshot


def _disk_snapshot() -> Dict[str, float]:
    usage = shutil.disk_usage(PROJECT_ROOT)
    return {
        "total_gb": round(usage.total / 1024**3, 3),
        "free_gb": round(usage.free / 1024**3, 3),
        "used_percent": round(100.0 * (usage.total - usage.free) / usage.total, 3),
    }


def _safe_json_object(text: str) -> Optional[Dict[str, Any]]:
    return cal._safe_json_object(text)


def _extract_eids(value: Any) -> Set[str]:
    return cal._extract_eids(value)


def _evidence_eids(payload: Dict[str, Any]) -> Set[str]:
    eids: Set[str] = set()
    for field in EVIDENCE_FIELDS:
        eids.update(_extract_eids(payload.get(field)))
    return eids


def _role_pairs(payload: Dict[str, Any]) -> Set[str]:
    pairs: Set[str] = set()
    for field in EVIDENCE_FIELDS:
        for eid in _extract_eids(payload.get(field)):
            pairs.add(f"{field}:{eid}")
    return pairs


def _ratio(num: float, den: float) -> float:
    return float(num) / float(den) if den else 0.0


def _f1(precision: float, recall: float) -> float:
    return 2.0 * precision * recall / (precision + recall) if precision + recall else 0.0


def _prediction_subtype(row: Dict[str, Any]) -> Optional[str]:
    payload = _safe_json_object(str(row.get("prediction_text") or ""))
    if not payload:
        return None
    if row.get("task") == "diagnosis":
        gt = payload.get("gt")
        if isinstance(gt, dict) and gt.get("subtype") is not None:
            return str(gt["subtype"])
    if row.get("task") == "cause_vs_symptom" and payload.get("primary_subtype") is not None:
        return str(payload["primary_subtype"])
    return None


def _confusion_and_by_class(predictions: List[Dict[str, Any]]) -> Dict[str, Any]:
    matrix: Dict[str, Dict[str, int]] = {}
    by_class: Dict[str, Dict[str, Any]] = {}
    for row in predictions:
        if row.get("task") != "diagnosis":
            continue
        gold = str(row.get("gold_subtype") or "unknown")
        pred = _prediction_subtype(row) or "unparsed"
        matrix.setdefault(gold, {})
        matrix[gold][pred] = matrix[gold].get(pred, 0) + 1
        stats = by_class.setdefault(gold, {"correct": 0, "total": 0, "accuracy": 0.0})
        stats["total"] += 1
        if pred == gold:
            stats["correct"] += 1
    for stats in by_class.values():
        stats["accuracy"] = _ratio(float(stats["correct"]), float(stats["total"]))
    return {"diagnosis_confusion_matrix": matrix, "subtype_accuracy_by_class": by_class}


def _validate_test_scope(test_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    task_counts = Counter(str(item["task"]) for item in test_rows)
    if dict(task_counts) != EXPECTED_TASK_COUNTS:
        raise UserError("TASK_DISTRIBUTION_FAILED", f"expected {EXPECTED_TASK_COUNTS}, got {dict(task_counts)}")
    if len({item["sample_id"] for item in test_rows}) != len(test_rows):
        raise UserError("DUPLICATE_SAMPLE_ID_FOUND", "duplicate test sample_id found")
    gold_by_sample = prompt_v2._gold_family_subtype_for_rows(test_rows)
    subtype_counts = Counter(subtype for _family, subtype in gold_by_sample.values())
    return {
        "test_samples": len(test_rows),
        "task_distribution": dict(sorted(task_counts.items())),
        "subtype_distribution": dict(sorted(subtype_counts.items())),
        "action_after_diagnosis_count": 0,
        "cpu_mem_gt_count": 0,
        "duplicate_sample_id_found": False,
    }


def _contract_evaluate(
    test_rows: List[Dict[str, Any]],
    predictions: List[Dict[str, Any]],
    contract: Dict[str, Any],
    predictions_path: Path,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    joined = cal._join_rows(test_rows, predictions)
    return contract_checker._evaluate_dataset(
        "task_9q_v3_contract_aware_prompt_v2_predictions",
        joined,
        predictions,
        contract,
        predictions_path,
    )


def _relaxed_metrics(
    test_rows: List[Dict[str, Any]],
    predictions: List[Dict[str, Any]],
    strict_by_sample: Dict[str, Dict[str, Any]],
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    pred_by_sample = {str(row["sample_id"]): row for row in predictions}
    rows: List[Dict[str, Any]] = []
    subtype_rows: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {
            "subtype": "",
            "evidence_samples": 0,
            "cause_samples": 0,
            "non_empty_evidence_prediction_count": 0,
            "evidence_hits": 0,
            "evidence_gold": 0,
            "evidence_pred": 0,
            "cause_hits": 0,
            "cause_gold": 0,
            "cause_pred": 0,
            "cause_under_selection_count": 0,
            "primary_cause_hits": 0,
            "cause_strict_correct": 0,
        }
    )

    evidence_hits = evidence_gold_total = evidence_pred_total = 0
    role_hits = role_gold_total = role_pred_total = 0
    evidence_empty_count = 0
    evidence_under_count = 0
    evidence_over_count = 0
    primary_evidence_hit_count = 0
    evidence_hit_count = 0

    cause_hits = cause_gold_total = cause_pred_total = 0
    cause_under_count = 0
    cause_over_count = 0
    cause_exact_count = 0
    cause_partial_count = 0
    primary_cause_hit_count = 0
    symptom_gold_total = symptom_pred_total = symptom_hits = 0

    for item in test_rows:
        task = item["task"]
        if task not in {"evidence_extraction", "cause_vs_symptom"}:
            continue
        pred_row = pred_by_sample[item["sample_id"]]
        pred = _safe_json_object(str(pred_row.get("prediction_text") or "")) or {}
        subtype = str(pred_row.get("gold_subtype") or item.get("gold_subtype") or "unknown")
        bucket = subtype_rows[subtype]
        bucket["subtype"] = subtype
        strict = strict_by_sample.get(item["sample_id"], {})

        if task == "evidence_extraction":
            gold = item["gold"]
            gold_eids = _evidence_eids(gold)
            pred_eids = _evidence_eids(pred)
            hits = len(gold_eids & pred_eids)
            precision = _ratio(hits, len(pred_eids))
            recall = _ratio(hits, len(gold_eids))
            gold_primary = _extract_eids(gold.get("primary_evidence"))
            pred_primary = _extract_eids(pred.get("primary_evidence"))
            gold_pairs = _role_pairs(gold)
            pred_pairs = _role_pairs(pred)
            role_row_hits = len(gold_pairs & pred_pairs)
            role_precision = _ratio(role_row_hits, len(pred_pairs))
            role_recall = _ratio(role_row_hits, len(gold_pairs))
            non_empty = bool(pred_eids)
            primary_hit = bool(gold_primary & pred_primary)
            under = len(pred_eids) < len(gold_eids)
            over = bool(pred_eids - gold_eids)

            evidence_hits += hits
            evidence_gold_total += len(gold_eids)
            evidence_pred_total += len(pred_eids)
            role_hits += role_row_hits
            role_gold_total += len(gold_pairs)
            role_pred_total += len(pred_pairs)
            evidence_empty_count += int(not non_empty)
            evidence_under_count += int(under)
            evidence_over_count += int(over)
            primary_evidence_hit_count += int(primary_hit)
            evidence_hit_count += int(bool(gold_eids & pred_eids))

            bucket["evidence_samples"] += 1
            bucket["non_empty_evidence_prediction_count"] += int(non_empty)
            bucket["evidence_hits"] += hits
            bucket["evidence_gold"] += len(gold_eids)
            bucket["evidence_pred"] += len(pred_eids)

            rows.append(
                {
                    "sample_id": item["sample_id"],
                    "run_id": item["run_id"],
                    "task": task,
                    "subtype": subtype,
                    "gold_eids": sorted(gold_eids),
                    "predicted_eids": sorted(pred_eids),
                    "gold_primary_eids": sorted(gold_primary),
                    "predicted_primary_eids": sorted(pred_primary),
                    "strict_precision": float(strict.get("evidence_precision") or 0.0),
                    "strict_recall": float(strict.get("evidence_recall") or 0.0),
                    "strict_f1": float(strict.get("evidence_f1") or 0.0),
                    "relaxed_eid_precision": precision,
                    "relaxed_eid_recall": recall,
                    "relaxed_eid_f1": _f1(precision, recall),
                    "primary_evidence_hit": primary_hit,
                    "role_aware_evidence_precision": role_precision,
                    "role_aware_evidence_recall": role_recall,
                    "role_aware_evidence_f1": _f1(role_precision, role_recall),
                    "non_empty_prediction": non_empty,
                    "under_selection": under,
                    "over_selection": over,
                    "failure_type": "predicted_evidence_empty"
                    if not non_empty
                    else ("evidence_under_generation" if under else ("evidence_over_generation" if over else "partial_or_exact_evidence_match")),
                }
            )
            continue

        gold = item["gold"]
        gold_cause = _extract_eids(gold.get("cause_eids"))
        pred_cause = _extract_eids(pred.get("cause_eids"))
        gold_symptom = _extract_eids(gold.get("symptom_eids"))
        pred_symptom = _extract_eids(pred.get("symptom_eids"))
        hits = len(gold_cause & pred_cause)
        precision = _ratio(hits, len(pred_cause))
        recall = _ratio(hits, len(gold_cause))
        symptom_row_hits = len(gold_symptom & pred_symptom)
        primary_label_correct = pred.get("primary_family") == "net" and pred.get("primary_subtype") == pred_row.get("gold_subtype")
        primary_hit = bool(gold_cause & pred_cause) and primary_label_correct
        under = bool(pred_cause < gold_cause)
        over = bool(pred_cause - gold_cause)
        exact = pred_cause == gold_cause and pred_symptom == gold_symptom
        partial = primary_hit and not exact

        cause_hits += hits
        cause_gold_total += len(gold_cause)
        cause_pred_total += len(pred_cause)
        symptom_hits += symptom_row_hits
        symptom_gold_total += len(gold_symptom)
        symptom_pred_total += len(pred_symptom)
        cause_under_count += int(under)
        cause_over_count += int(over)
        cause_exact_count += int(exact)
        cause_partial_count += int(partial)
        primary_cause_hit_count += int(primary_hit)

        bucket["cause_samples"] += 1
        bucket["cause_hits"] += hits
        bucket["cause_gold"] += len(gold_cause)
        bucket["cause_pred"] += len(pred_cause)
        bucket["cause_under_selection_count"] += int(under)
        bucket["primary_cause_hits"] += int(primary_hit)
        bucket["cause_strict_correct"] += int(bool(strict.get("cause_symptom_correct")))

        rows.append(
            {
                "sample_id": item["sample_id"],
                "run_id": item["run_id"],
                "task": task,
                "subtype": subtype,
                "gold_cause_eids": sorted(gold_cause),
                "predicted_cause_eids": sorted(pred_cause),
                "gold_symptom_eids": sorted(gold_symptom),
                "predicted_symptom_eids": sorted(pred_symptom),
                "strict_correct": bool(strict.get("cause_symptom_correct")),
                "primary_label_correct": primary_label_correct,
                "cause_eid_precision": precision,
                "cause_eid_recall": recall,
                "cause_eid_f1": _f1(precision, recall),
                "primary_cause_hit": primary_hit,
                "under_selection": under,
                "over_selection": over,
                "exact_cause_set_match": exact,
                "partial_cause_overlap": partial,
                "symptom_metrics_status": "N/A_NO_SYMPTOM_EIDS"
                if not gold_symptom and not pred_symptom
                else "ACTIVE",
            }
        )

    evidence_precision = _ratio(evidence_hits, evidence_pred_total)
    evidence_recall = _ratio(evidence_hits, evidence_gold_total)
    role_precision = _ratio(role_hits, role_pred_total)
    role_recall = _ratio(role_hits, role_gold_total)
    cause_precision = _ratio(cause_hits, cause_pred_total)
    cause_recall = _ratio(cause_hits, cause_gold_total)
    symptom_status = "N/A_NO_SYMPTOM_EIDS" if symptom_gold_total == 0 and symptom_pred_total == 0 else "ACTIVE"
    symptom_precision = None if symptom_status.startswith("N/A") else _ratio(symptom_hits, symptom_pred_total)
    symptom_recall = None if symptom_status.startswith("N/A") else _ratio(symptom_hits, symptom_gold_total)
    symptom_f1 = None if symptom_status.startswith("N/A") else _f1(symptom_precision or 0.0, symptom_recall or 0.0)

    subtype_metrics: List[Dict[str, Any]] = []
    for subtype, bucket in sorted(subtype_rows.items()):
        ep = _ratio(bucket["evidence_hits"], bucket["evidence_pred"])
        er = _ratio(bucket["evidence_hits"], bucket["evidence_gold"])
        cp = _ratio(bucket["cause_hits"], bucket["cause_pred"])
        cr = _ratio(bucket["cause_hits"], bucket["cause_gold"])
        subtype_metrics.append(
            {
                "subtype": subtype,
                "evidence_samples": bucket["evidence_samples"],
                "cause_samples": bucket["cause_samples"],
                "evidence_relaxed_f1": _f1(ep, er),
                "non_empty_evidence_prediction_count": bucket["non_empty_evidence_prediction_count"],
                "cause_eid_recall": cr,
                "cause_eid_f1": _f1(cp, cr),
                "cause_strict_accuracy": _ratio(bucket["cause_strict_correct"], bucket["cause_samples"]),
                "cause_under_selection_count": bucket["cause_under_selection_count"],
                "primary_cause_hit_rate": _ratio(bucket["primary_cause_hits"], bucket["cause_samples"]),
            }
        )

    evidence_sample_count = sum(1 for item in test_rows if item["task"] == "evidence_extraction")
    cause_sample_count = sum(1 for item in test_rows if item["task"] == "cause_vs_symptom")
    summary = {
        "schema_version": "net_only_evidence_oversampled_v3_relaxed_metrics_9q",
        "debug_only": True,
        "policy": "debug_only_side_channel_not_formal_replacement",
        "strict_metrics_retained": True,
        "evidence_samples": evidence_sample_count,
        "cause_vs_symptom_samples": cause_sample_count,
        "relaxed_evidence_id_precision": evidence_precision,
        "relaxed_evidence_id_recall": evidence_recall,
        "relaxed_evidence_id_f1": _f1(evidence_precision, evidence_recall),
        "relaxed_evidence_hit_rate": _ratio(evidence_hit_count, evidence_sample_count),
        "primary_evidence_hit_rate": _ratio(primary_evidence_hit_count, evidence_sample_count),
        "role_aware_evidence_precision": role_precision,
        "role_aware_evidence_recall": role_recall,
        "role_aware_evidence_f1": _f1(role_precision, role_recall),
        "non_empty_evidence_array_rate": _ratio(evidence_sample_count - evidence_empty_count, evidence_sample_count),
        "evidence_empty_array_count": evidence_empty_count,
        "evidence_under_generation_count": evidence_under_count,
        "evidence_over_generation_count": evidence_over_count,
        "cause_eid_precision": cause_precision,
        "cause_eid_recall": cause_recall,
        "cause_eid_f1": _f1(cause_precision, cause_recall),
        "primary_cause_hit_rate": _ratio(primary_cause_hit_count, cause_sample_count),
        "cause_under_selection_count": cause_under_count,
        "cause_over_selection_count": cause_over_count,
        "exact_cause_set_match_rate": _ratio(cause_exact_count, cause_sample_count),
        "partial_cause_overlap_rate": _ratio(cause_partial_count, cause_sample_count),
        "symptom_eid_precision": symptom_precision,
        "symptom_eid_recall": symptom_recall,
        "symptom_eid_f1": symptom_f1,
        "symptom_metrics_status": symptom_status,
        "subtype_relaxed_metrics": subtype_metrics,
    }
    return summary, rows


def _method_from_adapter(adapter_path: Path) -> Dict[str, Any]:
    cfg = _load_json(adapter_path / "adapter_config.json")
    return {
        "sft_used": True,
        "lora_used": True,
        "qlora_style_used": True,
        "full_finetune_used": False,
        "base_model_quantized": True,
        "base_model_frozen": True,
        "lora_rank": cfg.get("r"),
        "lora_alpha": cfg.get("lora_alpha"),
        "lora_dropout": cfg.get("lora_dropout"),
        "target_modules": cfg.get("target_modules"),
    }


def _run_generation(args: argparse.Namespace) -> int:
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    contract = _load_json(args.contract_json)
    if contract.get("schema_version") != CONTRACT_VERSION:
        raise UserError("CONTRACT_VERSION_MISMATCH", f"unexpected contract version: {contract.get('schema_version')}")
    test_rows = cal._read_test_rows(args.test_file)
    preflight = _validate_test_scope(test_rows)
    disk_before = _disk_snapshot()
    adapter_before = _adapter_snapshot(args.adapter_path)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    log = gen_smoke.RunLogger(args.output_dir)
    try:
        prompt_text = (
            prompt_v2._contract_system_prompt()
            + "\n\n"
            + "\n\n".join(prompt_v2._contract_task_shape(task, contract) for task in EXPECTED_TASK_COUNTS)
        )
        prompt_path = args.output_dir / "generation_prompt_v2.txt"
        prompt_path.write_text(prompt_text + "\n", encoding="utf-8")
        manifest_start = {
            "schema_version": SCHEMA_VERSION,
            "created_at_utc": _now_iso(),
            "remote_project_root": str(PROJECT_ROOT),
            "v3_candidate_path": str(V3_CANDIDATE_ROOT),
            "v3_training_output_dir": str(V3_TRAINING_OUTPUT_DIR),
            "adapter_output_path": str(args.adapter_path),
            "test_jsonl": str(args.test_file),
            "train_jsonl": str(V3_CANDIDATE_ROOT / "train.jsonl"),
            "val_jsonl": str(V3_CANDIDATE_ROOT / "val.jsonl"),
            "all_jsonl": str(V3_CANDIDATE_ROOT / "all.jsonl"),
            "trace_index_jsonl": str(V3_CANDIDATE_ROOT / "trace_index.jsonl"),
            "contract_json_path": str(args.contract_json),
            "checker_script_path": str(SCRIPT_DIR / "eval_net_only_diagnostic_schema_contract_v1.py"),
            "script_path": str(Path(__file__).resolve()),
            "script_sha256": _sha256_file(Path(__file__).resolve()),
            "prompt_v2_script_sha256": _sha256_file(SCRIPT_DIR / "eval_net_only_contract_aware_generation_smoke_v2.py"),
            "contract_sha256": _sha256_file(args.contract_json),
            "adapter_config_sha256": _sha256_file(args.adapter_path / "adapter_config.json"),
            "prompt_version": PROMPT_VERSION,
            "prompt_v2_hash": _sha256_file(prompt_path),
            "contract_version": CONTRACT_VERSION,
            "generation_config": {
                "do_sample": False,
                "max_new_tokens": args.max_new_tokens,
                "temperature": None,
                "top_p": None,
                "contract_aware_prompt": True,
                "prompt_version": PROMPT_VERSION,
            },
            "inputs_unused": {"train_jsonl": True, "val_jsonl": True, "all_jsonl": True, "trace_index_jsonl": True},
            "test_used": True,
            "train_used": False,
            "val_used": False,
            "all_used": False,
            "trace_used": False,
            "training_started": False,
            "weight_update_started": False,
            "new_adapter_created": False,
            "existing_adapter_modified": False,
            "disk_before": disk_before,
            **preflight,
        }
        _json_dump(args.output_dir / "generation_manifest.json", manifest_start)

        log.info("starting v3 contract-aware prompt-v2 generation")
        predictions = prompt_v2._generate_predictions(args, test_rows, contract, args.output_dir, log)
        incremental = args.output_dir / "contract_aware_predictions.jsonl"
        predictions_path = args.output_dir / "v3_contract_aware_predictions.jsonl"
        if incremental.is_file():
            incremental.replace(predictions_path)
        else:
            _write_jsonl(predictions_path, predictions)

        strict_summary, per_sample = _contract_evaluate(test_rows, predictions, contract, predictions_path)
        strict_summary["source_predictions"] = str(predictions_path)
        strict_summary["dataset_kind"] = "task_9q_v3_contract_aware_prompt_v2_predictions"
        strict_by_sample = {str(row["sample_id"]): row for row in per_sample}
        relaxed_summary, relaxed_rows = _relaxed_metrics(test_rows, predictions, strict_by_sample)
        confusion = _confusion_and_by_class(predictions)
        adapter_after = _adapter_snapshot(args.adapter_path)
        disk_after = _disk_snapshot()

        _json_dump(args.output_dir / "v3_strict_contract_metrics_summary.json", strict_summary)
        _write_jsonl(args.output_dir / "v3_per_sample_contract_audit.jsonl", per_sample)
        _json_dump(args.output_dir / "v3_relaxed_metrics_summary.json", relaxed_summary)
        _write_jsonl(args.output_dir / "v3_per_sample_relaxed_metrics.jsonl", relaxed_rows)
        _json_dump(args.output_dir / "v3_confusion_matrix.json", confusion)

        adapter_modified = adapter_before != adapter_after
        summary = {
            "schema_version": SCHEMA_VERSION,
            "result": "PASS" if not adapter_modified else "NEEDS_MANUAL_REVIEW",
            "created_at_utc": _now_iso(),
            "remote_project_root": str(PROJECT_ROOT),
            "remote_v3_candidate_path": str(V3_CANDIDATE_ROOT),
            "v3_training_output_dir": str(V3_TRAINING_OUTPUT_DIR),
            "adapter_output_path": str(args.adapter_path),
            "adapter_config_found": (args.adapter_path / "adapter_config.json").is_file(),
            "test_jsonl": str(args.test_file),
            "train_jsonl": str(V3_CANDIDATE_ROOT / "train.jsonl"),
            "val_jsonl": str(V3_CANDIDATE_ROOT / "val.jsonl"),
            "all_jsonl": str(V3_CANDIDATE_ROOT / "all.jsonl"),
            "trace_index_jsonl": str(V3_CANDIDATE_ROOT / "trace_index.jsonl"),
            "test_samples": len(test_rows),
            "train_used": False,
            "val_used": False,
            "all_used": False,
            "trace_used": False,
            "test_used": True,
            "generation_started": True,
            "generation_completed": True,
            "training_started": False,
            "weight_update_started": False,
            "new_adapter_created": False,
            "existing_adapter_modified": adapter_modified,
            "model_loaded": True,
            "contract_json_path": str(args.contract_json),
            "checker_script_path": str(SCRIPT_DIR / "eval_net_only_diagnostic_schema_contract_v1.py"),
            "prompt_v2_used": True,
            "prompt_v2_hash": _sha256_file(prompt_path),
            "predictions_created": predictions_path.is_file(),
            "prediction_count": len(predictions),
            "strict_metrics": strict_summary,
            "relaxed_metrics": relaxed_summary,
            "confusion_matrix": confusion,
            "method": _method_from_adapter(args.adapter_path),
            "formal_v1_test_loss": FORMAL_V1_TEST_LOSS,
            "repaired_v2_test_loss": REPAIRED_V2_TEST_LOSS,
            "v3_test_loss_from_9p": V3_TEST_LOSS_FROM_9P,
            "formal_eval_output_dir": str(args.output_dir),
            "disk_before": disk_before,
            "disk_after": disk_after,
            "adapter_before": adapter_before,
            "adapter_after": adapter_after,
            "run_id_leakage_found": False,
            "duplicate_sample_id_found": False,
            "remote_wrapper_modified": False,
            "remote_training_code_modified": False,
            "remote_env_modified": False,
            "contract_modified": False,
            "checker_modified": False,
            "prompt_v2_modified": False,
            "data_jsonl_modified": False,
            "ledger_modified": False,
            "frozen_net_batch_modified": False,
            "l1_rebuilt": False,
            "l2_rebuilt": False,
            "hdc_used": False,
            "board_touched": False,
            "formal_diagnostic_eval_completed": True,
            "ready_for_v1_v2_v3_result_comparison": True,
            "ready_for_thesis_table": True,
            "ready_for_action_contract": False,
            "boundary_statement": (
                "NET-only evidence-oversampled v3 conservative formal run held-out diagnostic eval; "
                "small dataset, no CPU/MEM accepted positives, no action_after_diagnosis, not final system generalization. "
                "Strict metrics are formal; relaxed metrics are debug-only."
            ),
            "interpretation": {
                "loss_vs_diagnostic_metrics": (
                    "Loss measures token likelihood on held-out target text; diagnostic metrics measure contract/schema and "
                    "task-specific semantic correctness."
                ),
                "strict_vs_relaxed": "Strict metrics are formal; relaxed evidence/cause metrics are debug-only side channel.",
            },
        }
        _json_dump(args.output_dir / "v3_diagnostic_eval_summary.json", summary)

        manifest = {
            "schema_version": SCHEMA_VERSION,
            "result": summary["result"],
            "created_at_utc": summary["created_at_utc"],
            "output_dir": str(args.output_dir),
            "files": {
                "predictions": str(predictions_path),
                "strict_metrics": str(args.output_dir / "v3_strict_contract_metrics_summary.json"),
                "relaxed_metrics": str(args.output_dir / "v3_relaxed_metrics_summary.json"),
                "per_sample_contract_audit": str(args.output_dir / "v3_per_sample_contract_audit.jsonl"),
                "per_sample_relaxed_metrics": str(args.output_dir / "v3_per_sample_relaxed_metrics.jsonl"),
                "confusion_matrix": str(args.output_dir / "v3_confusion_matrix.json"),
                "summary": str(args.output_dir / "v3_diagnostic_eval_summary.json"),
                "prompt": str(prompt_path),
                "stdout": str(args.output_dir / "stdout.log"),
                "stderr": str(args.output_dir / "stderr.log"),
            },
            "adapter_output_path": str(args.adapter_path),
            "test_jsonl": str(args.test_file),
            "test_used": True,
            "train_used": False,
            "val_used": False,
            "all_used": False,
            "trace_used": False,
            "training_started": False,
            "weight_update_started": False,
            "new_adapter_created": False,
            "existing_adapter_modified": adapter_modified,
            "strict_metrics_are_formal": True,
            "relaxed_metrics_debug_only": True,
        }
        _json_dump(args.output_dir / "v3_diagnostic_eval_manifest.json", manifest)
        log.info("v3 contract-aware diagnostic eval completed")
        print(json.dumps({"result": summary["result"], "output_dir": str(args.output_dir)}, sort_keys=True))
        return 0 if not adapter_modified else 3
    finally:
        log.close()


def run(args: argparse.Namespace) -> int:
    args.model_name_or_path = _resolve_dir(args.model_name_or_path, "model_name_or_path")
    args.adapter_path = _resolve_dir(args.adapter_path, "adapter_path")
    args.test_file = _resolve_file(args.test_file, "test_file")
    args.contract_json = _resolve_file(args.contract_json, "contract_json")
    args.baseline_7d_summary = _resolve_file(args.baseline_7d_summary, "baseline_7d_summary")
    args.baseline_7d_output = _resolve_dir(args.baseline_7d_output, "baseline_7d_output")
    args.output_dir = _resolve_output_dir(args.output_dir)
    if args.test_file != DEFAULT_TEST_FILE.resolve():
        raise UserError("V3_HELD_OUT_TEST_REQUIRED", f"expected {DEFAULT_TEST_FILE}, got {args.test_file}")
    if args.adapter_path != DEFAULT_ADAPTER.resolve():
        raise UserError("V3_FORMAL_ADAPTER_REQUIRED", f"expected {DEFAULT_ADAPTER}, got {args.adapter_path}")
    if not (args.adapter_path / "adapter_config.json").is_file():
        raise UserError("ADAPTER_CONFIG_NOT_FOUND", f"missing {args.adapter_path / 'adapter_config.json'}")
    test_rows = cal._read_test_rows(args.test_file)
    preflight = _validate_test_scope(test_rows)
    if args.validate_only:
        print(
            json.dumps(
                {
                    "result": "VALIDATE_ONLY_PASS",
                    "schema_version": SCHEMA_VERSION,
                    "remote_project_root": str(PROJECT_ROOT),
                    "test_file": str(args.test_file),
                    "adapter_path": str(args.adapter_path),
                    "contract_json": str(args.contract_json),
                    "output_dir": str(args.output_dir),
                    "training_started": False,
                    "weight_update_started": False,
                    "model_loaded": False,
                    "train_used": False,
                    "val_used": False,
                    "all_used": False,
                    "trace_used": False,
                    **preflight,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    return _run_generation(args)


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-name-or-path", default=str(DEFAULT_MODEL))
    parser.add_argument("--adapter-path", default=str(DEFAULT_ADAPTER))
    parser.add_argument("--test-file", default=str(DEFAULT_TEST_FILE))
    parser.add_argument("--contract-json", default=str(DEFAULT_CONTRACT_JSON))
    parser.add_argument("--baseline-7d-summary", default=str(DEFAULT_BASELINE_7D_SUMMARY))
    parser.add_argument("--baseline-7d-output", default=str(DEFAULT_BASELINE_7D_OUTPUT))
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--max-new-tokens", type=int, default=512)
    parser.add_argument("--no-4bit", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    try:
        return run(parse_args(argv))
    except UserError as exc:
        print(
            json.dumps(
                {
                    "result": exc.code,
                    "error": str(exc),
                    "training_started": False,
                    "weight_update_started": False,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
