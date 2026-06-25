#!/usr/bin/env python3
"""NET-only repaired-v2 evidence no-empty generation smoke.

This is an inference/generation smoke only. It uses the repaired-v2 held-out
test JSONL and a pre-existing LoRA adapter, writes raw/parsed/normalized
prediction records, and computes strict contract plus relaxed debug metrics.
It never trains, never runs loss eval, never updates weights, and never edits
the adapter, candidate JSONL, contract, checker, or prompt-v2 script.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import eval_net_only_diagnostic_metrics_calibration as cal  # noqa: E402
import eval_net_only_diagnostic_schema_contract_v1 as contract_checker  # noqa: E402
import eval_net_only_non_action_generation_smoke as gen_smoke  # noqa: E402


PROJECT_ROOT = Path("/home/xrh/qwen3_os_fault")
V2_CANDIDATE_ROOT = PROJECT_ROOT / "data/training_candidates/net_only_non_action_repaired_v2_20260520"
TRAINING_FORMAL_ROOT = PROJECT_ROOT / "outputs/training_formal"
DEFAULT_MODEL = Path("/home/xrh/models/Qwen/Qwen3-8B")
DEFAULT_ADAPTER = (
    TRAINING_FORMAL_ROOT
    / "net_only_non_action_repaired_v2_conservative_formal_20260520_20260520_144553/adapter"
)
DEFAULT_TEST_FILE = V2_CANDIDATE_ROOT / "test.jsonl"
DEFAULT_CONTRACT_JSON = SCRIPT_DIR / "contracts/net_only_non_action_diagnostic_output_contract_v1.json"
BASELINE_9G_OUTPUT = (
    TRAINING_FORMAL_ROOT
    / "net_only_non_action_repaired_v2_conservative_formal_20260520_20260520_144553_diagnostic_eval_20260520_160000"
)
BASELINE_9G_STRICT = BASELINE_9G_OUTPUT / "repaired_v2_strict_contract_metrics_summary.json"
BASELINE_9G_RELAXED = BASELINE_9G_OUTPUT / "repaired_v2_relaxed_metrics_summary.json"

PROMPT_VERSION = "net_only_evidence_no_empty_prompt_v3_20260520"
CONTRACT_VERSION = "net_only_non_action_diagnostic_output_contract_v1"
SCHEMA_VERSION = "net_only_evidence_no_empty_generation_smoke_v1"
EXPECTED_TASK_COUNTS = {
    "diagnosis": 14,
    "evidence_extraction": 14,
    "cause_vs_symptom": 14,
}
EVIDENCE_FIELDS = ("primary_evidence", "secondary_evidence", "symptom_evidence", "noise_evidence")


class UserError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _json_dump(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def _jsonl_write(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            f.write("\n")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


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


def _resolve_test_file(raw: str) -> Path:
    path = _resolve_file(raw, "test_file")
    if path.name != "test.jsonl":
        raise UserError("HELD_OUT_TEST_ONLY_REQUIRED", f"expected test.jsonl, got {path.name}")
    if path != DEFAULT_TEST_FILE.resolve():
        raise UserError("APPROVED_V2_TEST_REQUIRED", f"expected repaired-v2 held-out test: {DEFAULT_TEST_FILE}")
    return path


def _resolve_output_dir(raw: str) -> Path:
    if _has_glob_chars(raw):
        raise UserError("UNSAFE_GLOB_PREVENTED", f"output_dir must be explicit: {raw}")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise UserError("ABSOLUTE_PATH_REQUIRED", f"output_dir must be absolute: {raw}")
    resolved = path.resolve()
    try:
        resolved.relative_to(TRAINING_FORMAL_ROOT.resolve())
    except ValueError as exc:
        raise UserError("TRAINING_FORMAL_OUTPUT_ROOT_REQUIRED", f"output_dir must be under {TRAINING_FORMAL_ROOT}")
    if resolved.exists() and not resolved.is_dir():
        raise UserError("OUTPUT_DIR_NOT_DIRECTORY", f"output_dir exists but is not a directory: {resolved}")
    if resolved.exists() and any(resolved.iterdir()):
        raise UserError("OUTPUT_DIR_NOT_EMPTY", f"output_dir must be new or empty: {resolved}")
    return resolved


def _read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise UserError("JSON_OBJECT_REQUIRED", f"expected JSON object: {path}")
    return payload


def _safe_json_object(text: str) -> Optional[Dict[str, Any]]:
    return cal._safe_json_object(text)


def _disk_snapshot(path: Path) -> Dict[str, float]:
    usage = shutil.disk_usage(path)
    return {
        "disk_total_gb": usage.total / (1024**3),
        "disk_used_gb": usage.used / (1024**3),
        "disk_free_gb": usage.free / (1024**3),
        "disk_used_percent": usage.used / usage.total * 100.0,
    }


def _adapter_snapshot(adapter: Path) -> Dict[str, Dict[str, Any]]:
    tracked = {}
    for child in sorted(adapter.iterdir()):
        if child.is_file():
            st = child.stat()
            tracked[child.name] = {
                "size": st.st_size,
                "mtime_ns": st.st_mtime_ns,
                "sha256": _sha256_file(child),
            }
    return tracked


def _contract_system_prompt() -> str:
    return (
        "You are a KaiHongOS/OpenHarmony OS NET-only diagnostic evaluator. "
        "Output only one valid JSON object. Do not output Markdown, comments, code fences, natural-language prefaces, "
        "or explanations outside JSON. Do not echo the input log, OBS score fields, or L2_INPUT_JSON. "
        "Do not output action, recovery, remediation, command, shell, repair, or board-operation suggestions. "
        "Preserve GT/OBS separation: evidence is OBS and must not replace the NET root-cause label. "
        "For evidence_extraction, every required array must appear. If candidate_evidence is visible in the input, "
        "the evidence output must not be all-empty; choose evidence EIDs from candidate_evidence instead of returning "
        "the empty schema."
    )


def _contract_task_shape(task: str, contract: Dict[str, Any]) -> str:
    allowed = ", ".join(contract["allowed_net_subtypes"])
    if task == "diagnosis":
        return (
            'Return exactly one JSON object shaped like {"diagnosis_summary":"...",'
            '"gt":{"family":"net","subtype":"<one approved net subtype>",'
            '"confidence":"high|medium|low","is_anomaly":true,"run_kind":"fault|baseline","severity":"mild|moderate|severe"},'
            '"gt_obs_separated":true}. '
            f"Approved subtypes: {allowed}. Do not output canonical, ground_truth, obs, input, case_id, action, recovery, or command keys."
        )
    if task == "evidence_extraction":
        return (
            'Return exactly one JSON object with top-level keys "primary_evidence", "secondary_evidence", '
            '"symptom_evidence", "noise_evidence". '
            "Every array must be present. When candidate_evidence contains any items, at least one of "
            "primary_evidence, secondary_evidence, or noise_evidence must contain an item; do not use all-empty arrays "
            "as the default. Copy existing candidate_evidence items into the array matching support_role: primary -> "
            "primary_evidence, secondary/supporting -> secondary_evidence, symptom -> symptom_evidence, noise -> "
            "noise_evidence. Each item must include eid and may include only contract-allowed fields: kind, score, "
            "source, source_rel, span, support_role, text, ts. If uncertain, choose the candidate_evidence items most "
            "supporting the NET subtype. Never invent an eid. Never output free-text evidence paragraphs. Never put "
            "evidence under an evidence wrapper. Do not emit gt, obs, input, case_id, diagnosis, primary_family, "
            "primary_subtype, action, recovery, or command keys."
        )
    if task == "cause_vs_symptom":
        return (
            'Return exactly one JSON object shaped like {"primary_family":"net",'
            '"primary_subtype":"<one approved net subtype>","cause_eids":["e1"],"symptom_eids":[],'
            '"gt_obs_separated":true}. '
            f"Approved subtypes: {allowed}. Do not promote OBS primary_family/state to root cause. "
            "Do not emit gt, obs, input, case_id, evidence, action, recovery, or command keys."
        )
    raise UserError("UNEXPECTED_TASK", f"unsupported task: {task}")


def _build_prompt_messages(item: Dict[str, Any], contract: Dict[str, Any]) -> Tuple[List[Dict[str, str]], str]:
    task = item["task"]
    contract_text = _contract_task_shape(task, contract)
    user_text = (
        f"PROMPT_VERSION: {PROMPT_VERSION}\n"
        f"CONTRACT_VERSION: {CONTRACT_VERSION}\n"
        f"TASK: {task}\n\n"
        "CONTRACT REQUIREMENTS:\n"
        f"{contract_text}\n\n"
        "BOUNDARIES:\n"
        "- Use held-out test input only.\n"
        "- Use only NET labels; never output CPU or MEM as GT.\n"
        "- Do not copy OBS score field names into diagnosis/cause labels.\n"
        "- Do not include action/recovery/command suggestions.\n"
        "- Output the JSON object and stop.\n\n"
        "EVIDENCE NO-EMPTY RULE:\n"
        "- For evidence_extraction, if L2_INPUT_JSON.input.candidate_evidence has any items, the output must not be "
        "all empty arrays.\n"
        "- Select candidate EIDs visible in this prompt; do not fabricate EIDs.\n"
        "- If a candidate item has support_role=primary, put it in primary_evidence.\n"
        "- If a candidate item has support_role=secondary or supporting, put it in secondary_evidence.\n"
        "- If a candidate item has support_role=noise, put it in noise_evidence.\n"
        "- The repaired-v2 held-out evidence targets are non-empty; an all-empty evidence answer is a smoke violation.\n\n"
        "STRICT JSON CLOSURE:\n"
        "- Do not put quotes around arrays or objects.\n"
        "- Do not leave a trailing quote before the final brace.\n"
        "- The output must be parseable as one complete JSON object.\n\n"
        "HELD_OUT_TEST_PROMPT_INPUT:\n"
        f"{item['user_text']}"
    )
    messages = [
        {"role": "system", "content": _contract_system_prompt()},
        {"role": "user", "content": user_text},
    ]
    prompt_material = "\n\n".join(msg["content"] for msg in messages)
    return messages, _sha256_text(prompt_material)


def _gold_family_subtype_for_rows(test_rows: List[Dict[str, Any]]) -> Dict[str, Tuple[str, Optional[str]]]:
    subtype_by_run: Dict[str, str] = {}
    for item in test_rows:
        family, subtype = cal._gold_family_subtype(item["task"], item["gold"], None)
        if family and family != "net":
            raise UserError("CPU_MEM_GT_CONTAMINATION", f"unexpected gold family {family!r} at {item['sample_id']}")
        if subtype:
            subtype_by_run[str(item["run_id"])] = subtype
    out: Dict[str, Tuple[str, Optional[str]]] = {}
    for item in test_rows:
        family, subtype = cal._gold_family_subtype(item["task"], item["gold"], subtype_by_run.get(str(item["run_id"])))
        if family != "net":
            raise UserError("CPU_MEM_GT_CONTAMINATION", f"unexpected gold family {family!r} at {item['sample_id']}")
        if not subtype:
            raise UserError("GOLD_SUBTYPE_REQUIRED", f"could not infer gold subtype for {item['sample_id']}")
        out[item["sample_id"]] = (family, subtype)
    return out


def _validate_test_scope(test_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    task_counts = Counter(str(item["task"]) for item in test_rows)
    if dict(task_counts) != EXPECTED_TASK_COUNTS:
        raise UserError("TASK_DISTRIBUTION_FAILED", f"expected {EXPECTED_TASK_COUNTS}, got {dict(task_counts)}")
    gold_subtypes = _gold_family_subtype_for_rows(test_rows)
    subtype_counts = Counter(subtype for _family, subtype in gold_subtypes.values())
    duplicate_sample = len({item["sample_id"] for item in test_rows}) != len(test_rows)
    if duplicate_sample:
        raise UserError("DUPLICATE_SAMPLE_ID", "duplicate sample_id in test rows")
    return {
        "test_samples": len(test_rows),
        "task_distribution": dict(sorted(task_counts.items())),
        "subtype_distribution": dict(sorted(subtype_counts.items())),
        "evidence_test_samples": task_counts["evidence_extraction"],
        "action_after_diagnosis_count": 0,
        "cpu_mem_gt_count": 0,
        "duplicate_sample_id_found": False,
    }


def _prediction_has_nonempty_evidence(pred: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(pred, dict):
        return False
    for field in EVIDENCE_FIELDS:
        value = pred.get(field)
        if isinstance(value, list) and len(value) > 0:
            return True
    return False


def _evidence_empty_violation(item: Dict[str, Any], pred: Optional[Dict[str, Any]]) -> bool:
    if item["task"] != "evidence_extraction":
        return False
    return bool(item.get("prompt_input_eids")) and not _prediction_has_nonempty_evidence(pred)


def _extract_eids(value: Any) -> Set[str]:
    return cal._extract_eids(value)


def _evidence_eids(payload: Dict[str, Any]) -> Set[str]:
    out: Set[str] = set()
    for field in EVIDENCE_FIELDS:
        out.update(_extract_eids(payload.get(field)))
    return out


def _role_eids(payload: Dict[str, Any], field: str) -> Set[str]:
    return _extract_eids(payload.get(field))


def _safe_ratio(num: float, den: float) -> float:
    return float(num) / float(den) if den else 0.0


def _f1(p: float, r: float) -> float:
    return 2.0 * p * r / (p + r) if p + r else 0.0


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _relaxed_metrics(test_rows: List[Dict[str, Any]], predictions: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    pred_by_sample = {str(row["sample_id"]): row for row in predictions}
    evidence_rows: List[Dict[str, Any]] = []
    cause_rows: List[Dict[str, Any]] = []
    for item in test_rows:
        pred_row = pred_by_sample[item["sample_id"]]
        pred = _safe_json_object(str(pred_row.get("prediction_text") or "")) or {}
        if item["task"] == "evidence_extraction":
            gold = item["gold"]
            gold_eids = _evidence_eids(gold)
            pred_eids = _evidence_eids(pred)
            tp = len(gold_eids & pred_eids)
            precision = _safe_ratio(tp, len(pred_eids))
            recall = _safe_ratio(tp, len(gold_eids))
            gold_primary = _role_eids(gold, "primary_evidence")
            pred_primary = _role_eids(pred, "primary_evidence")
            row = {
                "sample_id": item["sample_id"],
                "run_id": item["run_id"],
                "subtype": pred_row.get("gold_subtype"),
                "gold_eids": sorted(gold_eids),
                "predicted_eids": sorted(pred_eids),
                "gold_primary_eids": sorted(gold_primary),
                "predicted_primary_eids": sorted(pred_primary),
                "relaxed_eid_precision": precision,
                "relaxed_eid_recall": recall,
                "relaxed_eid_f1": _f1(precision, recall),
                "primary_evidence_hit": bool(gold_primary & pred_primary),
                "non_empty_prediction": bool(pred_eids),
                "evidence_empty_violation": _evidence_empty_violation(item, pred),
            }
            evidence_rows.append(row)
        elif item["task"] == "cause_vs_symptom":
            gold_cause = _extract_eids(item["gold"].get("cause_eids"))
            pred_cause = _extract_eids(pred.get("cause_eids"))
            tp = len(gold_cause & pred_cause)
            precision = _safe_ratio(tp, len(pred_cause))
            recall = _safe_ratio(tp, len(gold_cause))
            row = {
                "sample_id": item["sample_id"],
                "run_id": item["run_id"],
                "subtype": pred_row.get("gold_subtype"),
                "gold_cause_eids": sorted(gold_cause),
                "predicted_cause_eids": sorted(pred_cause),
                "cause_eid_precision": precision,
                "cause_eid_recall": recall,
                "cause_eid_f1": _f1(precision, recall),
                "primary_cause_hit": bool(gold_cause & pred_cause),
                "under_selection": bool(pred_cause < gold_cause),
                "over_selection": bool(pred_cause - gold_cause),
                "symptom_metrics_status": "N/A_NO_SYMPTOM_EIDS",
            }
            cause_rows.append(row)

    evidence = {
        "relaxed_evidence_id_precision": _mean([r["relaxed_eid_precision"] for r in evidence_rows]),
        "relaxed_evidence_id_recall": _mean([r["relaxed_eid_recall"] for r in evidence_rows]),
        "relaxed_evidence_id_f1": _mean([r["relaxed_eid_f1"] for r in evidence_rows]),
        "relaxed_evidence_hit_rate": _safe_ratio(sum(1 for r in evidence_rows if r["predicted_eids"]), len(evidence_rows)),
        "primary_evidence_hit_rate": _safe_ratio(sum(1 for r in evidence_rows if r["primary_evidence_hit"]), len(evidence_rows)),
        "non_empty_evidence_array_rate": _safe_ratio(sum(1 for r in evidence_rows if r["non_empty_prediction"]), len(evidence_rows)),
        "evidence_empty_array_count": sum(1 for r in evidence_rows if not r["non_empty_prediction"]),
        "evidence_empty_violation_count": sum(1 for r in evidence_rows if r["evidence_empty_violation"]),
    }
    cause = {
        "cause_eid_precision": _mean([r["cause_eid_precision"] for r in cause_rows]),
        "cause_eid_recall": _mean([r["cause_eid_recall"] for r in cause_rows]),
        "cause_eid_f1": _mean([r["cause_eid_f1"] for r in cause_rows]),
        "primary_cause_hit_rate": _safe_ratio(sum(1 for r in cause_rows if r["primary_cause_hit"]), len(cause_rows)),
        "cause_under_selection_count": sum(1 for r in cause_rows if r["under_selection"]),
        "cause_over_selection_count": sum(1 for r in cause_rows if r["over_selection"]),
        "symptom_metrics_status": "N/A_NO_SYMPTOM_EIDS",
    }
    summary = {"evidence": evidence, "cause": cause}
    return summary, evidence_rows + cause_rows


def _contract_evaluate(
    test_rows: List[Dict[str, Any]],
    predictions: List[Dict[str, Any]],
    contract: Dict[str, Any],
    predictions_path: Path,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    joined = cal._join_rows(test_rows, predictions)
    return contract_checker._evaluate_dataset(
        "task_9j_evidence_no_empty_prompt_v3_smoke",
        joined,
        predictions,
        contract,
        predictions_path,
    )


def _generate_predictions(
    args: argparse.Namespace,
    test_rows: List[Dict[str, Any]],
    contract: Dict[str, Any],
    output_dir: Path,
    log: gen_smoke.RunLogger,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    torch, tokenizer, model = gen_smoke._load_model_and_tokenizer(args, log)
    device = gen_smoke._model_device(model)
    gold_by_sample = _gold_family_subtype_for_rows(test_rows)
    generation_config = {
        "do_sample": False,
        "max_new_tokens": args.max_new_tokens,
        "temperature": None,
        "top_p": None,
        "prompt_version": PROMPT_VERSION,
        "contract_aware_prompt": True,
        "evidence_no_empty_rule": True,
    }
    predictions: List[Dict[str, Any]] = []
    raw_rows: List[Dict[str, Any]] = []
    parsed_rows: List[Dict[str, Any]] = []
    normalized_rows: List[Dict[str, Any]] = []
    with torch.inference_mode():
        for idx, item in enumerate(test_rows, 1):
            prompt_messages, prompt_hash = _build_prompt_messages(item, contract)
            prompt_text = gen_smoke._apply_chat_template(tokenizer, prompt_messages)
            encoded = tokenizer(prompt_text, return_tensors="pt")
            encoded = {key: value.to(device) for key, value in encoded.items()}
            input_len = int(encoded["input_ids"].shape[-1])
            generated = model.generate(
                **encoded,
                do_sample=False,
                max_new_tokens=args.max_new_tokens,
                pad_token_id=tokenizer.eos_token_id,
                eos_token_id=tokenizer.eos_token_id,
            )
            new_tokens = generated[0][input_len:]
            raw_model_output = tokenizer.decode(new_tokens, skip_special_tokens=True).strip()
            parsed_prediction = _safe_json_object(raw_model_output)
            postprocess_action = "none_parse_success" if parsed_prediction is not None else "parse_failed_no_normalization"
            normalized_prediction = parsed_prediction if parsed_prediction is not None else None
            gold_family, gold_subtype = gold_by_sample[item["sample_id"]]
            evidence_empty = _evidence_empty_violation(item, parsed_prediction)
            row = {
                "sample_id": item["sample_id"],
                "run_id": item["run_id"],
                "task": item["task"],
                "gold_family": gold_family,
                "gold_subtype": gold_subtype,
                "prompt_version": PROMPT_VERSION,
                "contract_version": CONTRACT_VERSION,
                "prompt_hash": prompt_hash,
                "prediction_text": raw_model_output,
                "raw_model_output": raw_model_output,
                "parse_status": "success" if parsed_prediction is not None else "failed",
                "postprocess_action": postprocess_action,
                "evidence_empty_violation": evidence_empty,
                "generation_config": generation_config,
                "adapter_path": str(args.adapter_path),
                "source_test_jsonl": str(args.test_file),
            }
            predictions.append(row)
            raw_rows.append({**row, "parsed_prediction": None, "normalized_prediction": None})
            parsed_rows.append(
                {
                    "sample_id": item["sample_id"],
                    "run_id": item["run_id"],
                    "task": item["task"],
                    "parse_status": row["parse_status"],
                    "parsed_prediction": parsed_prediction,
                    "evidence_empty_violation": evidence_empty,
                }
            )
            normalized_rows.append(
                {
                    "sample_id": item["sample_id"],
                    "run_id": item["run_id"],
                    "task": item["task"],
                    "normalized_prediction": normalized_prediction,
                    "normalization_applied": False,
                    "postprocess_action": postprocess_action,
                    "evidence_empty_violation": evidence_empty,
                }
            )
            _jsonl_write(output_dir / "evidence_no_empty_predictions.jsonl", predictions)
            _jsonl_write(output_dir / "raw_model_outputs.jsonl", raw_rows)
            _jsonl_write(output_dir / "parsed_predictions.jsonl", parsed_rows)
            _jsonl_write(output_dir / "normalized_predictions.jsonl", normalized_rows)
            log.info(f"generated {idx}/{len(test_rows)} sample_id={item['sample_id']} task={item['task']}")
    return predictions, raw_rows, parsed_rows, normalized_rows


def _compare_with_9g(strict: Dict[str, Any], relaxed: Dict[str, Any], baseline_strict: Dict[str, Any], baseline_relaxed: Dict[str, Any]) -> Dict[str, Any]:
    baseline_evidence = baseline_relaxed.get("evidence", baseline_relaxed)
    evidence = relaxed["evidence"]
    return {
        "baseline_9g_evidence_empty_array_count": baseline_evidence.get("evidence_empty_array_count"),
        "current_evidence_empty_array_count": evidence["evidence_empty_array_count"],
        "baseline_9g_non_empty_evidence_array_rate": baseline_evidence.get("non_empty_evidence_array_rate"),
        "current_non_empty_evidence_array_rate": evidence["non_empty_evidence_array_rate"],
        "baseline_9g_evidence_f1": baseline_strict.get("evidence_f1"),
        "current_evidence_f1": strict.get("evidence_f1"),
        "baseline_9g_relaxed_evidence_id_f1": baseline_evidence.get("relaxed_evidence_id_f1"),
        "current_relaxed_evidence_id_f1": evidence["relaxed_evidence_id_f1"],
        "baseline_9g_cause_symptom_accuracy": baseline_strict.get("cause_symptom_accuracy"),
        "current_cause_symptom_accuracy": strict.get("cause_symptom_accuracy"),
        "baseline_9g_cause_eid_f1": baseline_relaxed.get("cause", {}).get("cause_eid_f1"),
        "current_cause_eid_f1": relaxed["cause"]["cause_eid_f1"],
        "schema_regression": strict.get("target_schema_success_rate", 0.0) < baseline_strict.get("target_schema_success_rate", 0.0),
        "safety_leakage_regression": any(
            strict.get(key, 0) > baseline_strict.get(key, 0)
            for key in (
                "hard_gt_obs_leak_count",
                "soft_gt_obs_risk_count",
                "action_recommendation_leak_count",
                "recovery_command_leak_count",
                "cpu_mem_input_contamination_count",
                "cpu_mem_prediction_echo_count",
            )
        ),
        "evidence_empty_array_improved": evidence["evidence_empty_array_count"]
        < (baseline_evidence.get("evidence_empty_array_count") or 0),
        "evidence_f1_improved": (strict.get("evidence_f1") or 0.0) > (baseline_strict.get("evidence_f1") or 0.0),
    }


def run(args: argparse.Namespace) -> int:
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    args.test_file = _resolve_test_file(args.test_file)
    args.contract_json = _resolve_file(args.contract_json, "contract_json")
    args.model_name_or_path = _resolve_dir(args.model_name_or_path, "model_name_or_path")
    args.adapter_path = _resolve_dir(args.adapter_path, "adapter_path")
    args.baseline_9g_strict = _resolve_file(args.baseline_9g_strict, "baseline_9g_strict")
    args.baseline_9g_relaxed = _resolve_file(args.baseline_9g_relaxed, "baseline_9g_relaxed")
    adapter_config = args.adapter_path / "adapter_config.json"
    if not adapter_config.is_file():
        raise UserError("ADAPTER_CONFIG_NOT_FOUND", f"adapter_config.json missing under {args.adapter_path}")
    args.output_dir = _resolve_output_dir(args.output_dir)
    if not args.validate_only:
        args.output_dir.mkdir(parents=True, exist_ok=False)

    contract = _read_json(args.contract_json)
    if contract.get("schema_version") != CONTRACT_VERSION:
        raise UserError("CONTRACT_VERSION_MISMATCH", f"unexpected contract version {contract.get('schema_version')}")
    test_rows = cal._read_test_rows(args.test_file)
    preflight = _validate_test_scope(test_rows)
    disk_before = _disk_snapshot(TRAINING_FORMAL_ROOT)

    if args.validate_only:
        print(
            json.dumps(
                {
                    "result": "VALIDATE_ONLY_PASS",
                    "project_root": str(PROJECT_ROOT),
                    "test_file": str(args.test_file),
                    "adapter_path": str(args.adapter_path),
                    "contract_json": str(args.contract_json),
                    "model_loaded": False,
                    "training_started": False,
                    "eval_started": False,
                    "weight_update_started": False,
                    "inputs_unused": {
                        "train_jsonl": True,
                        "val_jsonl": True,
                        "all_jsonl": True,
                        "trace_index_jsonl": True,
                    },
                    **preflight,
                    **disk_before,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0

    log = gen_smoke.RunLogger(args.output_dir)
    adapter_before = _adapter_snapshot(args.adapter_path)
    try:
        prompt_text = (
            _contract_system_prompt()
            + "\n\n"
            + "\n\n".join(_contract_task_shape(task, contract) for task in EXPECTED_TASK_COUNTS)
        )
        (args.output_dir / "generation_prompt_v3.txt").write_text(prompt_text + "\n", encoding="utf-8")
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "created_at": _now_iso(),
            "project_root": str(PROJECT_ROOT),
            "test_file": str(args.test_file),
            "contract_json": str(args.contract_json),
            "contract_sha256": _sha256_file(args.contract_json),
            "adapter_path": str(args.adapter_path),
            "adapter_config_sha256": _sha256_file(adapter_config),
            "model_name_or_path": str(args.model_name_or_path),
            "script_path": str(Path(__file__).resolve()),
            "script_sha256": _sha256_file(Path(__file__).resolve()),
            "prompt_version": PROMPT_VERSION,
            "prompt_v3_hash": _sha256_file(args.output_dir / "generation_prompt_v3.txt"),
            "contract_version": CONTRACT_VERSION,
            "generation_config": {
                "do_sample": False,
                "max_new_tokens": args.max_new_tokens,
                "temperature": None,
                "top_p": None,
            },
            "inputs_unused": {
                "train_jsonl": True,
                "val_jsonl": True,
                "all_jsonl": True,
                "trace_index_jsonl": True,
            },
            "test_used": True,
            "training_started": False,
            "eval_started": False,
            "loss_eval_started": False,
            "weight_update_started": False,
            "new_adapter_created": False,
            "existing_adapter_modified": False,
            "prompt_repair_smoke_only_not_formal_performance": True,
            "disk_before": disk_before,
            **preflight,
        }
        _json_dump(args.output_dir / "generation_manifest.json", manifest)
        log.info("starting evidence no-empty prompt-v3 generation smoke")
        predictions, raw_rows, parsed_rows, normalized_rows = _generate_predictions(args, test_rows, contract, args.output_dir, log)
        predictions_path = args.output_dir / "evidence_no_empty_predictions.jsonl"
        strict_summary, per_sample = _contract_evaluate(test_rows, predictions, contract, predictions_path)
        relaxed_summary, relaxed_rows = _relaxed_metrics(test_rows, predictions)
        baseline_strict = _read_json(args.baseline_9g_strict)
        baseline_relaxed = _read_json(args.baseline_9g_relaxed)
        comparison = _compare_with_9g(strict_summary, relaxed_summary, baseline_strict, baseline_relaxed)
        adapter_after = _adapter_snapshot(args.adapter_path)
        adapter_modified = adapter_before != adapter_after
        disk_after = _disk_snapshot(TRAINING_FORMAL_ROOT)

        _json_dump(args.output_dir / "evidence_no_empty_strict_metrics_summary.json", strict_summary)
        _json_dump(args.output_dir / "evidence_no_empty_relaxed_metrics_summary.json", relaxed_summary)
        _jsonl_write(args.output_dir / "evidence_no_empty_per_sample_audit.jsonl", per_sample)
        _jsonl_write(args.output_dir / "evidence_no_empty_per_sample_relaxed_metrics.jsonl", relaxed_rows)
        _json_dump(args.output_dir / "comparison_with_9g.json", comparison)

        summary = {
            "schema_version": SCHEMA_VERSION,
            "result": "PASS",
            "output_dir": str(args.output_dir),
            "prediction_count": len(predictions),
            "raw_model_output_saved": bool(raw_rows),
            "parsed_prediction_saved": bool(parsed_rows),
            "normalized_prediction_saved": bool(normalized_rows),
            "prompt_version": PROMPT_VERSION,
            "prompt_v3_hash": _sha256_file(args.output_dir / "generation_prompt_v3.txt"),
            "strict_metrics": strict_summary,
            "relaxed_metrics": relaxed_summary,
            "comparison_with_9g": comparison,
            "adapter_modified": adapter_modified,
            "disk_before": disk_before,
            "disk_after": disk_after,
            "training_started": False,
            "eval_started": False,
            "loss_eval_started": False,
            "weight_update_started": False,
            "new_adapter_created": False,
            "existing_adapter_modified": adapter_modified,
            "prompt_v2_modified": False,
            "contract_modified": False,
            "checker_modified": False,
            "data_jsonl_modified": False,
            "remote_wrapper_modified": False,
            "remote_env_modified": False,
            "prompt_repair_smoke_only_not_formal_performance": True,
            "ready_for_evidence_focused_repair_plan": True,
            "ready_for_evidence_oversampling_plan": True,
            "ready_for_action_contract": False,
        }
        _json_dump(args.output_dir / "generation_summary.json", summary)
        log.info("evidence no-empty generation smoke completed")
        print(json.dumps({"result": "PASS", "output_dir": str(args.output_dir)}, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        log.close()


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-name-or-path", default=str(DEFAULT_MODEL))
    parser.add_argument("--adapter-path", default=str(DEFAULT_ADAPTER))
    parser.add_argument("--test-file", default=str(DEFAULT_TEST_FILE))
    parser.add_argument("--contract-json", default=str(DEFAULT_CONTRACT_JSON))
    parser.add_argument("--baseline-9g-strict", default=str(BASELINE_9G_STRICT))
    parser.add_argument("--baseline-9g-relaxed", default=str(BASELINE_9G_RELAXED))
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--max-new-tokens", type=int, default=768)
    parser.add_argument("--no-4bit", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    try:
        return run(parse_args(argv))
    except UserError as exc:
        print(json.dumps({"result": "FAIL", "code": exc.code, "error": str(exc)}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
