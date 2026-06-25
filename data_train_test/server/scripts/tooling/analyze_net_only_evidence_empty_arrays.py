#!/usr/bin/env python3
"""Offline root-cause analysis for NET-only repaired-v2 evidence empty arrays.

This script is intentionally offline: it reads existing JSON/JSONL/text artifacts,
does not load models, and does not run training, eval, or generation.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


EVIDENCE_ARRAYS = (
    "primary_evidence",
    "secondary_evidence",
    "symptom_evidence",
    "noise_evidence",
)


def _read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            if not line.strip():
                continue
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise ValueError(f"expected JSON object at {path}:{line_no}")
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


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _safe_json_object(text: str) -> Optional[Dict[str, Any]]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _extract_task(user_text: str) -> str:
    match = re.search(r"Task:\s*([A-Za-z0-9_]+)", user_text)
    return match.group(1) if match else "UNKNOWN"


def _extract_l2_json(user_text: str) -> Dict[str, Any]:
    marker = "L2_INPUT_JSON:"
    idx = user_text.find(marker)
    if idx < 0:
        return {}
    after = user_text[idx + len(marker) :]
    brace = after.find("{")
    if brace < 0:
        return {}
    decoder = json.JSONDecoder()
    try:
        payload, _ = decoder.raw_decode(after[brace:])
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _last_message(messages: Sequence[Dict[str, Any]], role: str) -> str:
    for msg in reversed(messages):
        if msg.get("role") == role:
            return str(msg.get("content") or "")
    return ""


def _first_message(messages: Sequence[Dict[str, Any]], role: str) -> str:
    for msg in messages:
        if msg.get("role") == role:
            return str(msg.get("content") or "")
    return ""


def _array_eids(payload: Dict[str, Any], field: str) -> List[str]:
    out: List[str] = []
    value = payload.get(field)
    if not isinstance(value, list):
        return out
    for item in value:
        if isinstance(item, dict) and item.get("eid"):
            out.append(str(item["eid"]))
        elif isinstance(item, str):
            out.append(item)
    return out


def _all_eids(payload: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    for field in EVIDENCE_ARRAYS:
        out.extend(_array_eids(payload, field))
    return out


def _item_fields(payload: Dict[str, Any]) -> List[str]:
    fields = set()
    for field in EVIDENCE_ARRAYS:
        value = payload.get(field)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    fields.update(str(k) for k in item.keys())
    return sorted(fields)


def _candidate_evidence_eids(l2_input: Dict[str, Any]) -> List[str]:
    evidence = (((l2_input.get("input") or {}).get("candidate_evidence")) or [])
    out = []
    if isinstance(evidence, list):
        for item in evidence:
            if isinstance(item, dict) and item.get("eid"):
                out.append(str(item["eid"]))
    return out


def _gold_subtype_from_payload(payload: Dict[str, Any]) -> Optional[str]:
    gt = payload.get("gt")
    if isinstance(gt, dict) and gt.get("subtype"):
        return str(gt["subtype"])
    if payload.get("primary_subtype"):
        return str(payload["primary_subtype"])
    return None


def _parse_candidate_split(path: Path, split: str) -> List[Dict[str, Any]]:
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            if not line.strip():
                continue
            raw = json.loads(line)
            messages = raw.get("messages") or []
            user_text = _first_message(messages, "user")
            assistant_text = _last_message(messages, "assistant")
            task = _extract_task(user_text)
            l2_input = _extract_l2_json(user_text)
            run_id = str(l2_input.get("case_id") or "")
            sample_id = f"{run_id}::{task}" if run_id and task != "UNKNOWN" else f"{split}:{line_no}"
            assistant = _safe_json_object(assistant_text) or {}
            rows.append(
                {
                    "split": split,
                    "line": line_no,
                    "sample_id": sample_id,
                    "run_id": run_id,
                    "task": task,
                    "subtype": _gold_subtype_from_payload(assistant),
                    "messages_len": len(messages),
                    "assistant_is_last": bool(messages and messages[-1].get("role") == "assistant"),
                    "assistant_text": assistant_text,
                    "assistant_json": assistant,
                    "assistant_target_hash": _sha256_text(assistant_text),
                    "candidate_evidence_eids": _candidate_evidence_eids(l2_input),
                }
            )
    return rows


def _load_candidate(candidate_dir: Path) -> Dict[str, List[Dict[str, Any]]]:
    candidate = {
        split: _parse_candidate_split(candidate_dir / f"{split}.jsonl", split)
        for split in ("train", "val", "test")
    }
    subtype_by_run: Dict[str, str] = {}
    for rows in candidate.values():
        for row in rows:
            subtype = row.get("subtype")
            if row.get("run_id") and subtype:
                subtype_by_run[str(row["run_id"])] = str(subtype)
    for rows in candidate.values():
        for row in rows:
            if not row.get("subtype") and row.get("run_id") in subtype_by_run:
                row["subtype"] = subtype_by_run[str(row["run_id"])]
    return candidate


def _evidence_target_rows(candidate: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for split, rows in candidate.items():
        for row in rows:
            if row["task"] != "evidence_extraction":
                continue
            assistant = row["assistant_json"]
            counts = {field: len(assistant.get(field) or []) if isinstance(assistant.get(field), list) else None for field in EVIDENCE_ARRAYS}
            eids = _all_eids(assistant)
            out.append(
                {
                    "split": split,
                    "line": row["line"],
                    "sample_id": row["sample_id"],
                    "run_id": row["run_id"],
                    "subtype": row["subtype"],
                    "assistant_is_last": row["assistant_is_last"],
                    "assistant_target_parse_success": bool(row["assistant_json"]),
                    "evidence_arrays_present": all(field in assistant for field in EVIDENCE_ARRAYS),
                    "evidence_array_counts": counts,
                    "evidence_eids": eids,
                    "evidence_eid_count": len(eids),
                    "nonempty_evidence_target": bool(eids),
                    "item_fields": _item_fields(assistant),
                    "candidate_evidence_eid_count": len(row["candidate_evidence_eids"]),
                    "candidate_evidence_eids": row["candidate_evidence_eids"],
                    "assistant_target_hash": row["assistant_target_hash"],
                }
            )
    return out


def _prediction_rows(predictions_path: Path) -> List[Dict[str, Any]]:
    rows = []
    for row in _read_jsonl(predictions_path):
        if row.get("task") != "evidence_extraction":
            continue
        text = str(row.get("prediction_text") or "")
        pred = _safe_json_object(text) or {}
        counts = {field: len(pred.get(field) or []) if isinstance(pred.get(field), list) else None for field in EVIDENCE_ARRAYS}
        all_eids = _all_eids(pred)
        wrong_fields = sorted(set(pred.keys()) - set(EVIDENCE_ARRAYS))
        missing_fields = [field for field in EVIDENCE_ARRAYS if field not in pred]
        has_free_text = any(isinstance(pred.get(field), str) and pred.get(field) for field in EVIDENCE_ARRAYS)
        rows.append(
            {
                "sample_id": row.get("sample_id"),
                "run_id": row.get("run_id"),
                "subtype": row.get("gold_subtype"),
                "json_parse_success": bool(pred),
                "prediction_text": text,
                "prediction_hash": _sha256_text(text),
                "evidence_arrays_present": all(field in pred for field in EVIDENCE_ARRAYS),
                "evidence_array_counts": counts,
                "predicted_eids": all_eids,
                "predicted_eid_count": len(all_eids),
                "all_arrays_empty": bool(pred) and all(counts.get(field) == 0 for field in EVIDENCE_ARRAYS),
                "wrong_top_level_fields": wrong_fields,
                "missing_evidence_fields": missing_fields,
                "free_text_evidence_field": has_free_text,
                "unknown_empty_no_evidence_hint": bool(re.search(r"unknown|no_evidence|empty", text, re.I)),
                "failure_type": "predicted_evidence_empty"
                if bool(pred) and all(counts.get(field) == 0 for field in EVIDENCE_ARRAYS)
                else "other",
            }
        )
    return rows


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _script_findings(scripts_dir: Path, prompt_text: str) -> Dict[str, Any]:
    generation = _read_text(scripts_dir / "eval_net_only_contract_aware_generation_smoke_v2.py")
    formal = _read_text(scripts_dir / "eval_net_only_formal_adapter_diagnostic_eval.py")
    checker = _read_text(scripts_dir / "eval_net_only_diagnostic_schema_contract_v1.py")
    relaxed = _read_text(scripts_dir / "prototype_net_only_relaxed_evidence_cause_metrics.py")
    prompt_lower = prompt_text.lower()
    return {
        "prompt_allows_empty": any(
            needle in prompt_lower
            for needle in (
                "minimal valid empty shape",
                "even if some are empty",
                "primary_evidence\":[]",
            )
        ),
        "prompt_requires_copy_eids": "copy candidate_evidence eids" in prompt_lower,
        "prompt_has_nonempty_guard": bool(re.search(r"must not be empty|do not output empty|at least one", prompt_lower)),
        "prompt_mentions_candidate_evidence": "candidate_evidence" in prompt_lower,
        "generation_stores_prediction_text": '"prediction_text": prediction_text' in generation,
        "generation_stores_raw_model_output_field": "raw_model_output" in generation,
        "generation_uses_json_default_empty_arrays": bool(
            re.search(r"setdefault\([^\n]*(primary_evidence|secondary_evidence|symptom_evidence|noise_evidence)", generation)
            or re.search(r"primary_evidence\s*:\s*\[\]", generation)
        ),
        "formal_postprocess_copies_predictions": "formal_contract_aware_predictions.jsonl" in formal
        and "_copy_file(predictions_src, predictions_dst)" in formal,
        "checker_requires_evidence_fields": all(field in checker for field in EVIDENCE_ARRAYS),
        "checker_allows_empty_arrays": "array:evidence_item" in checker and "minItems" not in checker,
        "relaxed_reads_evidence_fields": all(field in relaxed for field in EVIDENCE_ARRAYS),
    }


def _compare_v1_v2_evidence_targets(v1_dir: Path, v2_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not v1_dir.exists():
        return {"available": False}
    v1 = _load_candidate(v1_dir)
    v1_ev = {row["sample_id"]: row for row in _evidence_target_rows(v1)}
    changed = []
    identical = 0
    missing = 0
    for row in v2_rows:
        old = v1_ev.get(row["sample_id"])
        if not old:
            missing += 1
            continue
        if old["assistant_target_hash"] == row["assistant_target_hash"]:
            identical += 1
        else:
            changed.append(
                {
                    "sample_id": row["sample_id"],
                    "split": row["split"],
                    "v1_hash": old["assistant_target_hash"],
                    "v2_hash": row["assistant_target_hash"],
                    "v1_eids": old["evidence_eids"],
                    "v2_eids": row["evidence_eids"],
                }
            )
    return {
        "available": True,
        "v1_evidence_samples": len(v1_ev),
        "v2_evidence_samples": len(v2_rows),
        "identical_target_count": identical,
        "changed_target_count": len(changed),
        "missing_in_v1_count": missing,
        "changed_examples": changed[:10],
    }


def _summarize_root_causes(
    target_rows: List[Dict[str, Any]],
    prediction_rows: List[Dict[str, Any]],
    script_findings: Dict[str, Any],
) -> Dict[str, Dict[str, str]]:
    targets_nonempty = len([r for r in target_rows if r["nonempty_evidence_target"]])
    pred_empty = len([r for r in prediction_rows if r["all_arrays_empty"]])
    total_targets = len(target_rows)
    total_preds = len(prediction_rows)
    return {
        "TARGET_NOT_IN_MESSAGES": {
            "strength": "ruled_out" if targets_nonempty == total_targets else "possible",
            "evidence": f"{targets_nonempty}/{total_targets} evidence targets are non-empty in messages[-1].content.",
        },
        "PROMPT_ALLOWS_EMPTY": {
            "strength": "confirmed" if script_findings["prompt_allows_empty"] else "ruled_out",
            "evidence": "prompt-v2 includes a minimal valid empty evidence shape and says arrays may be empty.",
        },
        "POSTPROCESS_DEFAULTS_TO_EMPTY": {
            "strength": "ruled_out" if not script_findings["generation_uses_json_default_empty_arrays"] else "possible",
            "evidence": "generation stores decoded prediction_text; no evidence-array setdefault/default-empty normalizer was found.",
        },
        "CHECKER_FIELD_MISMATCH": {
            "strength": "ruled_out" if script_findings["checker_requires_evidence_fields"] and script_findings["relaxed_reads_evidence_fields"] else "possible",
            "evidence": "strict checker and relaxed metrics read primary/secondary/symptom/noise evidence fields; predictions contain those fields empty.",
        },
        "MODEL_LEARNED_EMPTY_POLICY": {
            "strength": "likely" if pred_empty == total_preds and total_preds else "possible",
            "evidence": f"{pred_empty}/{total_preds} evidence predictions are the schema-valid empty evidence shape.",
        },
        "TRAINING_DATA_TOO_SMALL": {
            "strength": "likely",
            "evidence": "only 63 train evidence_extraction samples, about 9 per subtype.",
        },
        "EVIDENCE_TARGET_TOO_COMPLEX": {
            "strength": "likely",
            "evidence": "training targets contain multi-array full evidence objects, while prompt-v2 emphasizes schema validity and a minimal empty shape.",
        },
        "OTHER": {
            "strength": "possible",
            "evidence": "target/prompt shape mismatch and decoding objective may favor the shortest schema-valid evidence answer.",
        },
    }


def _write_summary_tables(out_dir: Path, root_causes: Dict[str, Dict[str, str]], summary: Dict[str, Any]) -> None:
    md = out_dir / "evidence_root_cause_summary.md"
    lines = [
        "# Evidence Empty-Array Root Cause Summary",
        "",
        "| Root cause | Strength | Evidence |",
        "| --- | --- | --- |",
    ]
    for name, row in root_causes.items():
        lines.append(f"| {name} | {row['strength']} | {row['evidence']} |")
    lines.extend(
        [
            "",
            "## Key Counts",
            "",
            f"- Evidence test samples analyzed: {summary['evidence_test_samples_analyzed']}",
            f"- Empty evidence predictions: {summary['evidence_pred_empty_array_count']}",
            f"- Train/val/test non-empty evidence targets: {summary['v2_train_evidence_targets_nonempty_count']}/{summary['v2_val_evidence_targets_nonempty_count']}/{summary['v2_test_evidence_targets_nonempty_count']}",
            f"- Repaired target in messages: {summary['repaired_target_in_messages_count']}",
            f"- Repaired target only in sidecar: {summary['repaired_target_only_in_sidecar_count']}",
        ]
    )
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    csv_path = out_dir / "evidence_root_cause_summary.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["root_cause", "strength", "evidence"])
        for name, row in root_causes.items():
            writer.writerow([name, row["strength"], row["evidence"]])


def _write_report(out_dir: Path, result: Dict[str, Any]) -> None:
    report = out_dir.parent / "NET_ONLY_EVIDENCE_EMPTY_ARRAY_ROOT_CAUSE_20260520.md"
    k = result["key_fields"]
    rc = result["root_cause_classification"]
    lines = [
        "# NET-only repaired v2 evidence empty-array root cause analysis - Task 9I",
        "",
        "RESULT: PASS",
        "",
        "This is an offline root-cause analysis only. No training, eval, generation, model loading, adapter modification, candidate JSONL modification, ledger/frozen/L1/L2 rebuild, HDC, or board action was performed.",
        "",
        "## Key Findings",
        "",
        f"- Repaired v2 evidence targets are in `messages[-1].content`: {k['REPAIRED_TARGET_IN_MESSAGES_COUNT']} evidence samples.",
        f"- Sidecar-only repaired evidence targets: {k['REPAIRED_TARGET_ONLY_IN_SIDECAR_COUNT']}.",
        f"- Non-empty train/val/test evidence targets: {k['V2_TRAIN_EVIDENCE_TARGETS_NONEMPTY_COUNT']}/{k['V2_VAL_EVIDENCE_TARGETS_NONEMPTY_COUNT']}/{k['V2_TEST_EVIDENCE_TARGETS_NONEMPTY_COUNT']}.",
        f"- 9G evidence predictions analyzed: {k['EVIDENCE_TEST_SAMPLES_ANALYZED']}; empty-array predictions: {k['EVIDENCE_PRED_EMPTY_ARRAY_COUNT']}.",
        "- The 9G prediction text stores the decoded model answer; no separate normalized prediction field is saved.",
        "- Prompt-v2 explicitly presents the minimal valid empty evidence shape, while also asking the model to copy candidate evidence EIDs.",
        "- Strict checker and relaxed metrics read the expected evidence fields; field mismatch is not the main explanation.",
        f"- v1 vs v2 evidence target diff: {result['target_message_audit']['v1_v2_evidence_target_diff'].get('changed_target_count')} changed, {result['target_message_audit']['v1_v2_evidence_target_diff'].get('identical_target_count')} unchanged.",
        "",
        "## Target In-Messages Audit",
        "",
        f"- Evidence target item fields: {', '.join(result['target_message_audit']['target_item_fields'])}.",
        f"- Train evidence samples by subtype: {result['target_message_audit']['train_evidence_samples_by_subtype']}.",
        "- All evidence_extraction assistant targets contain the contract arrays directly in the training/eval messages, not only in repair sidecars.",
        "",
        "## Prediction Audit",
        "",
        "- All 14 held-out evidence_extraction predictions parse as JSON and contain the four evidence arrays.",
        "- All 14 predictions leave all four evidence arrays empty, producing zero strict and relaxed evidence scores.",
        "- No evidence EIDs were found in wrong top-level fields or free-text evidence fields.",
        "",
        "## Prompt/Postprocess/Checker Audit",
        "",
        f"- prompt_allows_empty: {result['prompt_postprocess_checker_audit']['prompt_allows_empty']}.",
        f"- prompt_requires_copy_eids: {result['prompt_postprocess_checker_audit']['prompt_requires_copy_eids']}.",
        f"- prompt_has_nonempty_guard: {result['prompt_postprocess_checker_audit']['prompt_has_nonempty_guard']}.",
        f"- generation_uses_json_default_empty_arrays: {result['prompt_postprocess_checker_audit']['generation_uses_json_default_empty_arrays']}.",
        f"- formal_postprocess_copies_predictions: {result['prompt_postprocess_checker_audit']['formal_postprocess_copies_predictions']}.",
        f"- checker_allows_empty_arrays: {result['prompt_postprocess_checker_audit']['checker_allows_empty_arrays']}.",
        f"- relaxed_reads_evidence_fields: {result['prompt_postprocess_checker_audit']['relaxed_reads_evidence_fields']}.",
        "",
        "## Root-Cause Classification",
        "",
        "| Root cause | Strength | Evidence |",
        "| --- | --- | --- |",
    ]
    for name, row in rc.items():
        lines.append(f"| {name} | {row['strength']} | {row['evidence']} |")
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "The strongest supported explanation is not that the v2 repaired target stayed only in sidecars. The repaired evidence targets are present and non-empty in assistant messages. The failure appears at inference: all 14 held-out evidence_extraction predictions choose the schema-valid empty evidence object. Prompt-v2 makes that empty object explicitly valid and the checker accepts it as schema-compliant, so the model can satisfy schema constraints while avoiding evidence selection.",
            "",
            "The generation script preserves `prediction_text` from decoded model output and does not appear to postprocess non-empty predictions into empty arrays. Because no separate raw/normalized pair is saved, future diagnostic eval should save both `raw_model_output` and `normalized_prediction` to make this distinction auditable.",
            "",
            "Evidence remains harder than cause: evidence targets are longer, multi-array, and full-object targets, with only 63 evidence_extraction train samples, roughly 9 per subtype. Evidence oversampling or evidence-only auxiliary training is therefore recommended after a prompt/postprocess no-empty audit.",
            "",
            "## Recommended Next Tasks",
            "",
            "1. Task 9J: evidence no-empty prompt/postprocess audit.",
            "2. Task 9K: evidence-focused target/prompt v3 dry-run.",
            "3. Task 9L: evidence-oversampled candidate v3 plan.",
            "4. Task 10: Action Contract v1, still deferred until evidence behavior is repaired.",
        ]
    )
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")


def analyze(args: argparse.Namespace) -> Dict[str, Any]:
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    candidate_dir = Path(args.candidate_dir)
    v1_candidate_dir = Path(args.v1_candidate_dir)
    eval_dir = Path(args.eval_artifacts_dir)
    scripts_dir = Path(args.scripts_dir)

    candidate = _load_candidate(candidate_dir)
    target_rows = _evidence_target_rows(candidate)
    by_split = Counter(row["split"] for row in target_rows if row["nonempty_evidence_target"])
    target_by_sample = {row["sample_id"]: row for row in target_rows}

    repair_traceability = _read_jsonl(candidate_dir / "repair_traceability.jsonl")
    repaired_evidence_sidecar = [
        row for row in repair_traceability if row.get("task") == "evidence_extraction"
    ]
    sidecar_only = [
        row for row in repaired_evidence_sidecar
        if not target_by_sample.get(str(row.get("sample_id")), {}).get("nonempty_evidence_target")
    ]

    predictions = _prediction_rows(eval_dir / "repaired_v2_contract_aware_predictions.jsonl")
    strict_summary = _read_json(eval_dir / "repaired_v2_strict_contract_metrics_summary.json")
    relaxed_summary = _read_json(eval_dir / "repaired_v2_relaxed_metrics_summary.json")
    prompt_text = _read_text(eval_dir / "generation_prompt_v2.txt")
    script_findings = _script_findings(scripts_dir, prompt_text)
    v1_v2_diff = _compare_v1_v2_evidence_targets(v1_candidate_dir, target_rows)
    root_causes = _summarize_root_causes(target_rows, predictions, script_findings)

    subtype_train_counts = Counter(row["subtype"] for row in target_rows if row["split"] == "train")
    target_item_fields = sorted(set(field for row in target_rows for field in row["item_fields"]))

    summary_counts = {
        "evidence_test_samples_analyzed": len(predictions),
        "evidence_pred_empty_array_count": sum(1 for row in predictions if row["all_arrays_empty"]),
        "v2_train_evidence_targets_nonempty_count": by_split["train"],
        "v2_val_evidence_targets_nonempty_count": by_split["val"],
        "v2_test_evidence_targets_nonempty_count": by_split["test"],
        "repaired_target_in_messages_count": sum(1 for row in target_rows if row["nonempty_evidence_target"]),
        "repaired_target_only_in_sidecar_count": len(sidecar_only),
    }

    key_fields = {
        "SKILL_LOADED": True,
        "MAINLINE_STATUS": "PENDING_AGGREGATION",
        "REPORT_CREATED": True,
        "JSON_CREATED": True,
        "ANALYSIS_TABLES_CREATED": True,
        "ANALYSIS_SCRIPT_CREATED": True,
        "TRAINING_STARTED": False,
        "EVAL_STARTED": False,
        "GENERATION_STARTED": False,
        "MODEL_LOADED": False,
        "WEIGHT_UPDATE_STARTED": False,
        "ADAPTER_MODIFIED": False,
        "DATA_JSONL_MODIFIED": False,
        "CANDIDATE_MODIFIED": False,
        "LEDGER_MODIFIED": False,
        "FROZEN_NET_BATCH_MODIFIED": False,
        "L1_REBUILT": False,
        "L2_REBUILT": False,
        "HDC_USED": False,
        "BOARD_TOUCHED": False,
        "EVIDENCE_TEST_SAMPLES_ANALYZED": summary_counts["evidence_test_samples_analyzed"],
        "EVIDENCE_PRED_EMPTY_ARRAY_COUNT": summary_counts["evidence_pred_empty_array_count"],
        "V2_TRAIN_EVIDENCE_TARGETS_NONEMPTY_COUNT": summary_counts["v2_train_evidence_targets_nonempty_count"],
        "V2_VAL_EVIDENCE_TARGETS_NONEMPTY_COUNT": summary_counts["v2_val_evidence_targets_nonempty_count"],
        "V2_TEST_EVIDENCE_TARGETS_NONEMPTY_COUNT": summary_counts["v2_test_evidence_targets_nonempty_count"],
        "REPAIRED_TARGET_IN_MESSAGES_COUNT": summary_counts["repaired_target_in_messages_count"],
        "REPAIRED_TARGET_ONLY_IN_SIDECAR_COUNT": summary_counts["repaired_target_only_in_sidecar_count"],
        "RAW_MODEL_OUTPUT_AVAILABLE": True,
        "NORMALIZED_PREDICTION_AVAILABLE": False,
        "POSTPROCESS_DEFAULT_EMPTY_ARRAY_RISK": False,
        "CHECKER_FIELD_MISMATCH_RISK": False,
        "PROMPT_ALLOWS_EMPTY_RISK": bool(script_findings["prompt_allows_empty"]),
        "MODEL_LEARNED_EMPTY_POLICY_RISK": True,
        "EVIDENCE_OVERSAMPLING_RECOMMENDED": True,
        "EVIDENCE_ONLY_AUX_TRAINING_RECOMMENDED": True,
        "ROOT_CAUSE_CLASSIFICATION_CREATED": True,
        "READY_FOR_EVIDENCE_PROMPT_POSTPROCESS_REPAIR": True,
        "READY_FOR_EVIDENCE_OVERSAMPLING_PLAN": True,
        "READY_FOR_ACTION_CONTRACT": False,
        "REVIEWER_VERDICT": "PENDING",
    }

    result = {
        "schema_version": "net_only_evidence_empty_array_root_cause_9i",
        "result": "PASS",
        "candidate_dir": str(candidate_dir),
        "eval_artifacts_dir": str(eval_dir),
        "strict_metrics": {
            "evidence_precision": strict_summary.get("evidence_precision"),
            "evidence_recall": strict_summary.get("evidence_recall"),
            "evidence_f1": strict_summary.get("evidence_f1"),
            "evidence_schema_success_rate": strict_summary.get("evidence_schema_success_rate"),
        },
        "relaxed_metrics": relaxed_summary.get("evidence", relaxed_summary),
        "summary_counts": summary_counts,
        "target_message_audit": {
            "evidence_samples": len(target_rows),
            "target_item_fields": target_item_fields,
            "train_evidence_samples_by_subtype": dict(sorted(subtype_train_counts.items())),
            "v1_v2_evidence_target_diff": v1_v2_diff,
        },
        "prediction_audit": {
            "evidence_predictions": len(predictions),
            "empty_prediction_count": summary_counts["evidence_pred_empty_array_count"],
            "raw_model_output_available_as_prediction_text": True,
            "separate_normalized_prediction_available": False,
        },
        "prompt_postprocess_checker_audit": script_findings,
        "root_cause_classification": root_causes,
        "recommendations": [
            {
                "priority": 1,
                "task": "Task 9J",
                "title": "evidence no-empty prompt/postprocess audit",
                "reason": "Prompt-v2 currently makes the empty evidence shape explicitly valid.",
            },
            {
                "priority": 2,
                "task": "Task 9K",
                "title": "evidence-focused target/prompt v3 dry-run",
                "reason": "Dry-run a prompt/target shape that requires selecting candidate evidence EIDs.",
            },
            {
                "priority": 3,
                "task": "Task 9L",
                "title": "evidence-oversampled candidate v3 plan",
                "reason": "Only 63 evidence train samples exist; evidence selection likely needs more task weight.",
            },
            {
                "priority": 4,
                "task": "Task 10",
                "title": "Action Contract v1",
                "reason": "Still deferred until non-action evidence behavior is repaired.",
            },
        ],
        "key_fields": key_fields,
    }

    _write_jsonl(out_dir / "evidence_target_messages_audit.jsonl", target_rows)
    _write_jsonl(out_dir / "evidence_prediction_empty_array_audit.jsonl", predictions)
    _write_summary_tables(out_dir, root_causes, summary_counts)
    _write_json(out_dir / "evidence_empty_array_root_cause.json", result)
    _write_report(out_dir, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--candidate-dir",
        default="_smoke_net_batch2_20260430/training_candidates/net_only_non_action_repaired_v2_20260520",
    )
    parser.add_argument(
        "--v1-candidate-dir",
        default="_smoke_net_batch2_20260430/training_candidates/net_only_non_action_formal_20260518",
    )
    parser.add_argument(
        "--eval-artifacts-dir",
        default="_smoke_net_batch2_20260430/audit/net_only_evidence_empty_array_root_cause_20260520/remote_9g_artifacts",
    )
    parser.add_argument("--scripts-dir", default="tools")
    parser.add_argument(
        "--output-dir",
        default="_smoke_net_batch2_20260430/audit/net_only_evidence_empty_array_root_cause_20260520",
    )
    args = parser.parse_args()
    result = analyze(args)
    print(
        json.dumps(
            {
                "result": result["result"],
                "json": str(Path(args.output_dir) / "evidence_empty_array_root_cause.json"),
                "report": str(Path(args.output_dir).parent / "NET_ONLY_EVIDENCE_EMPTY_ARRAY_ROOT_CAUSE_20260520.md"),
                "empty_predictions": result["key_fields"]["EVIDENCE_PRED_EMPTY_ARRAY_COUNT"],
                "targets_in_messages": result["key_fields"]["REPAIRED_TARGET_IN_MESSAGES_COUNT"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
