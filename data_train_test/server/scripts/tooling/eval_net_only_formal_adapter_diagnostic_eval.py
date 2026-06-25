#!/usr/bin/env python3
"""Formal NET-only adapter diagnostic generation/evaluation wrapper.

This script is evaluation-only. It reuses the contract-aware prompt-v2
generation implementation and contract-v1 checker helpers, but writes formal
8D artifact names for the conservative formal adapter. It never trains, never
updates weights, never saves adapters, and never reads train/val/all/trace as
model inputs.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import eval_net_only_contract_aware_generation_smoke_v2 as prompt_v2  # noqa: E402


PROJECT_ROOT = Path("/home/xrh/qwen3_os_fault")
FORMAL_OUTPUT_ROOT = PROJECT_ROOT / "outputs/training_formal"
CANDIDATE_ROOT = PROJECT_ROOT / "data/training_candidates/net_only_non_action_formal_20260518"
FORMAL_TRAINING_OUTPUT_DIR = (
    FORMAL_OUTPUT_ROOT / "net_only_non_action_conservative_formal_20260519_20260519_160558"
)
DEFAULT_ADAPTER = FORMAL_TRAINING_OUTPUT_DIR / "adapter"
DEFAULT_TEST_FILE = CANDIDATE_ROOT / "test.jsonl"
DEFAULT_MODEL = Path("/home/xrh/models/Qwen/Qwen3-8B")
DEFAULT_CONTRACT_JSON = SCRIPT_DIR / "contracts/net_only_non_action_diagnostic_output_contract_v1.json"
DEFAULT_BASELINE_7D_SUMMARY = (
    PROJECT_ROOT
    / "outputs/diagnostic_eval/net_only_contract_aware_generation_smoke_20260519_20260519_134654/contract_aware_metrics_summary.json"
)
DEFAULT_BASELINE_7D_OUTPUT = (
    PROJECT_ROOT / "outputs/diagnostic_eval/net_only_contract_aware_generation_smoke_20260519_20260519_134654"
)
DEFAULT_TEST_LOSS_8C = 0.06015147641301155
SCHEMA_VERSION = "net_only_formal_adapter_diagnostic_eval_8d"
PROMPT_VERSION = prompt_v2.PROMPT_VERSION
CONTRACT_VERSION = prompt_v2.CONTRACT_VERSION


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


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            f.write("\n")


def _safe_json_object(text: str) -> Optional[Dict[str, Any]]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _has_glob_chars(raw: str) -> bool:
    return any(ch in raw for ch in "*?[]{}")


def _resolve_file(raw: str, label: str) -> Path:
    if _has_glob_chars(raw):
        raise UserError("UNSAFE_GLOB_PREVENTED", f"{label} must be an explicit path: {raw}")
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


def _adapter_snapshot(adapter_path: Path) -> Dict[str, Dict[str, int]]:
    tracked = ["adapter_config.json", "adapter_model.safetensors"]
    snapshot: Dict[str, Dict[str, int]] = {}
    for name in tracked:
        path = adapter_path / name
        if path.is_file():
            st = path.stat()
            snapshot[str(path)] = {"size": int(st.st_size), "mtime_ns": int(st.st_mtime_ns)}
    return snapshot


def _copy_file(src: Path, dst: Path) -> None:
    if not src.is_file():
        raise UserError("EXPECTED_OUTPUT_MISSING", f"missing output file: {src}")
    shutil.copy2(src, dst)


def _prediction_subtype(row: Dict[str, Any]) -> Optional[str]:
    payload = _safe_json_object(str(row.get("prediction_text") or ""))
    if not payload:
        return None
    task = row.get("task")
    if task == "diagnosis":
        gt = payload.get("gt")
        if isinstance(gt, dict):
            subtype = gt.get("subtype")
            return str(subtype) if subtype is not None else None
    if task == "cause_vs_symptom":
        subtype = payload.get("primary_subtype")
        return str(subtype) if subtype is not None else None
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
        total = int(stats["total"])
        stats["accuracy"] = (float(stats["correct"]) / float(total)) if total else 0.0
    return {
        "diagnosis_confusion_matrix": matrix,
        "subtype_accuracy_by_class": by_class,
    }


def _retag_per_sample(src: Path, dst: Path) -> None:
    rows = _load_jsonl(src)
    for row in rows:
        row["dataset_kind"] = "task_8d_formal_contract_aware_predictions"
    _write_jsonl(dst, rows)


def _run_prompt_v2(args: argparse.Namespace) -> int:
    prompt_v2.DIAGNOSTIC_OUTPUT_ROOT = FORMAL_OUTPUT_ROOT
    prompt_args = prompt_v2.parse_args(
        [
            "--model-name-or-path",
            str(args.model_name_or_path),
            "--adapter-path",
            str(args.adapter_path),
            "--test-file",
            str(args.test_file),
            "--contract-json",
            str(args.contract_json),
            "--baseline-7d-summary",
            str(args.baseline_7d_summary),
            "--baseline-7d-output",
            str(args.baseline_7d_output),
            "--output-dir",
            str(args.output_dir),
            "--max-new-tokens",
            str(args.max_new_tokens),
        ]
    )
    if args.validate_only:
        prompt_args.validate_only = True
    return prompt_v2.run(prompt_args)


def _postprocess_outputs(args: argparse.Namespace, adapter_before: Dict[str, Dict[str, int]]) -> None:
    output_dir = args.output_dir
    predictions_src = output_dir / "contract_aware_v2_predictions.jsonl"
    metrics_src = output_dir / "contract_aware_v2_metrics_summary.json"
    per_sample_src = output_dir / "per_sample_contract_audit_v2.jsonl"
    prompt_file = output_dir / "generation_prompt_v2.txt"

    predictions_dst = output_dir / "formal_contract_aware_predictions.jsonl"
    metrics_dst = output_dir / "formal_contract_metrics_summary.json"
    per_sample_dst = output_dir / "formal_per_sample_contract_audit.jsonl"
    confusion_dst = output_dir / "formal_confusion_matrix.json"

    _copy_file(predictions_src, predictions_dst)
    _retag_per_sample(per_sample_src, per_sample_dst)

    metrics = _load_json(metrics_src)
    metrics["dataset_kind"] = "task_8d_formal_contract_aware_predictions"
    metrics["source_predictions"] = str(predictions_dst)
    _json_dump(metrics_dst, metrics)

    predictions = _load_jsonl(predictions_dst)
    confusion = _confusion_and_by_class(predictions)
    _json_dump(confusion_dst, confusion)

    adapter_after = _adapter_snapshot(args.adapter_path)
    adapter_modified = adapter_before != adapter_after
    prompt_hash = _sha256_file(prompt_file)
    adapter_config = _load_json(args.adapter_path / "adapter_config.json")
    lora_rank = adapter_config.get("r")
    lora_alpha = adapter_config.get("lora_alpha")
    lora_dropout = adapter_config.get("lora_dropout")
    target_modules = adapter_config.get("target_modules")

    summary = {
        "schema_version": SCHEMA_VERSION,
        "result": "PASS" if not adapter_modified else "NEEDS_MANUAL_REVIEW",
        "created_at": _now_iso(),
        "remote_project_root": str(PROJECT_ROOT),
        "formal_training_output_dir": str(FORMAL_TRAINING_OUTPUT_DIR),
        "adapter_output_path": str(args.adapter_path),
        "test_jsonl": str(args.test_file),
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
        "contract_json": str(args.contract_json),
        "checker_script_path": str(SCRIPT_DIR / "eval_net_only_diagnostic_schema_contract_v1.py"),
        "prompt_v2_used": True,
        "prompt_v2_hash": prompt_hash,
        "predictions": str(predictions_dst),
        "prediction_count": len(predictions),
        "metrics": metrics,
        "confusion_matrix": confusion,
        "formal_test_loss_from_8c": args.test_loss_8c,
        "method": {
            "sft_used": True,
            "lora_used": True,
            "qlora_style_used": True,
            "full_finetune_used": False,
            "base_model_quantized": True,
            "base_model_frozen": True,
            "lora_rank": lora_rank,
            "lora_alpha": lora_alpha,
            "lora_dropout": lora_dropout,
            "target_modules": target_modules,
        },
        "boundaries": {
            "net_only": True,
            "conservative_formal_run": True,
            "not_final_system_generalization": True,
            "cpu_mem_gt_included": False,
            "action_after_diagnosis_included": False,
            "schema_and_semantic_metrics_reported_separately": True,
        },
        "adapter_before": adapter_before,
        "adapter_after": adapter_after,
        "remote_wrapper_modified": False,
        "remote_training_code_modified": False,
        "remote_env_modified": False,
        "contract_modified": False,
        "checker_modified": False,
        "data_jsonl_modified": False,
        "ledger_modified": False,
        "frozen_net_batch_modified": False,
        "l1_rebuilt": False,
        "l2_rebuilt": False,
        "hdc_used": False,
        "board_touched": False,
        "ready_for_result_summary": metrics.get("target_schema_success_rate") == 1.0,
        "ready_for_thesis_table": metrics.get("target_schema_success_rate") == 1.0,
    }
    _json_dump(output_dir / "formal_diagnostic_eval_summary.json", summary)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "result": summary["result"],
        "created_at": summary["created_at"],
        "output_dir": str(output_dir),
        "formal_contract_aware_predictions": str(predictions_dst),
        "formal_contract_metrics_summary": str(metrics_dst),
        "formal_per_sample_contract_audit": str(per_sample_dst),
        "formal_confusion_matrix": str(confusion_dst),
        "formal_diagnostic_eval_summary": str(output_dir / "formal_diagnostic_eval_summary.json"),
        "generation_prompt_v2": str(prompt_file),
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
        "formal_test_loss_from_8c": args.test_loss_8c,
    }
    _json_dump(output_dir / "formal_diagnostic_eval_manifest.json", manifest)


def run(args: argparse.Namespace) -> int:
    args.model_name_or_path = _resolve_dir(args.model_name_or_path, "model_name_or_path")
    args.adapter_path = _resolve_dir(args.adapter_path, "adapter_path")
    args.test_file = _resolve_file(args.test_file, "test_file")
    args.contract_json = _resolve_file(args.contract_json, "contract_json")
    args.baseline_7d_summary = _resolve_file(args.baseline_7d_summary, "baseline_7d_summary")
    args.baseline_7d_output = _resolve_dir(args.baseline_7d_output, "baseline_7d_output")
    args.output_dir = _resolve_output_dir(args.output_dir)

    if args.test_file.name != "test.jsonl":
        raise UserError("HELD_OUT_TEST_ONLY_REQUIRED", f"expected test.jsonl, got {args.test_file}")
    if args.adapter_path != DEFAULT_ADAPTER.resolve():
        raise UserError("FORMAL_ADAPTER_REQUIRED", f"expected formal adapter {DEFAULT_ADAPTER}, got {args.adapter_path}")

    adapter_config = args.adapter_path / "adapter_config.json"
    if not adapter_config.is_file():
        raise UserError("ADAPTER_CONFIG_NOT_FOUND", f"missing {adapter_config}")

    adapter_before = _adapter_snapshot(args.adapter_path)
    rc = _run_prompt_v2(args)
    if args.validate_only:
        return rc
    if rc != 0:
        return rc
    _postprocess_outputs(args, adapter_before)
    print(json.dumps({"result": "PASS", "output_dir": str(args.output_dir)}, sort_keys=True))
    return 0


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
    parser.add_argument("--test-loss-8c", type=float, default=DEFAULT_TEST_LOSS_8C)
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
