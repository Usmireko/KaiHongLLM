#!/usr/bin/env python3
"""NET-only contract-aware held-out generation smoke.

This script is intentionally evaluation-only. It reads the held-out test JSONL,
loads the smoke adapter for generation, and evaluates the generated text with
the contract-v1 checker helpers. It never trains, never updates weights, never
modifies the adapter, and never reads train/val/all/trace as evaluation input.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import eval_net_only_diagnostic_metrics_calibration as cal  # noqa: E402
import eval_net_only_diagnostic_schema_contract_v1 as contract_checker  # noqa: E402
import eval_net_only_non_action_generation_smoke as gen_smoke  # noqa: E402


PROJECT_ROOT = Path("/home/xrh/qwen3_os_fault")
CANDIDATE_ROOT = PROJECT_ROOT / "data/training_candidates/net_only_non_action_formal_20260518"
DIAGNOSTIC_OUTPUT_ROOT = PROJECT_ROOT / "outputs/diagnostic_eval"
DEFAULT_MODEL = Path("/home/xrh/models/Qwen/Qwen3-8B")
DEFAULT_ADAPTER = (
    PROJECT_ROOT
    / "outputs/training_smoke/net_only_non_action_train_val_smoke_rerun_20260518_20260519_084005/adapter"
)
DEFAULT_TEST_FILE = CANDIDATE_ROOT / "test.jsonl"
DEFAULT_CONTRACT_JSON = SCRIPT_DIR / "contracts/net_only_non_action_diagnostic_output_contract_v1.json"
DEFAULT_BASELINE_7C_SUMMARY = (
    DIAGNOSTIC_OUTPUT_ROOT
    / "net_only_schema_contract_v1_20260519_20260519_112500/schema_contract_eval_summary.json"
)
PROMPT_VERSION = "net_only_contract_aware_prompt_v1_20260519"
CONTRACT_VERSION = "net_only_non_action_diagnostic_output_contract_v1"
SCHEMA_VERSION = "net_only_contract_aware_generation_smoke_v1"
SMOKE_NOTE = (
    "Prompt-alignment smoke only: adapter came from max_steps=3 training smoke; "
    "metrics are not formal model performance."
)
EXPECTED_TASK_COUNTS = {
    "diagnosis": 14,
    "evidence_extraction": 14,
    "cause_vs_symptom": 14,
}


class UserError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _json_dump(path: Path, payload: Any) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def _jsonl_write(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
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


def _resolve_test_file(raw: str) -> Path:
    path = _resolve_file(raw, "test_file")
    if path.name != "test.jsonl":
        raise UserError("CANONICAL_TEST_FILE_REQUIRED", f"--test-file must be test.jsonl: {path}")
    if path.resolve() != DEFAULT_TEST_FILE.resolve():
        raise UserError("APPROVED_TEST_ROOT_REQUIRED", f"--test-file must be the approved held-out test: {path}")
    return path


def _resolve_output_dir(raw: str) -> Path:
    if _has_glob_chars(raw):
        raise UserError("UNSAFE_GLOB_PREVENTED", f"output_dir must be explicit: {raw}")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise UserError("ABSOLUTE_PATH_REQUIRED", f"output_dir must be absolute: {raw}")
    resolved = path.resolve()
    try:
        resolved.relative_to(DIAGNOSTIC_OUTPUT_ROOT.resolve())
    except ValueError as exc:
        raise UserError(
            "DIAGNOSTIC_OUTPUT_ROOT_REQUIRED",
            f"output_dir must be under {DIAGNOSTIC_OUTPUT_ROOT}: {resolved}",
        ) from exc
    if resolved.exists() and not resolved.is_dir():
        raise UserError("OUTPUT_DIR_NOT_DIRECTORY", f"output_dir exists but is not a directory: {resolved}")
    if resolved.exists() and any(resolved.iterdir()):
        raise UserError("OUTPUT_DIR_NOT_EMPTY", f"output_dir must be new or empty: {resolved}")
    return resolved


def _load_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise UserError("JSON_OBJECT_REQUIRED", f"{path} must be a JSON object")
    return data


def _contract_task_shape(task: str, contract: Dict[str, Any]) -> str:
    allowed = ", ".join(contract["allowed_net_subtypes"])
    if task == "diagnosis":
        return (
            'Return exactly one JSON object shaped like: '
            '{"diagnosis_summary":"...",'
            '"gt":{"family":"net","subtype":"<one approved net subtype>",'
            '"confidence":"high|medium|low","is_anomaly":true,'
            '"run_kind":"fault|baseline","severity":"mild|moderate|severe"},'
            '"gt_obs_separated":true}. '
            f"Approved subtypes: {allowed}. Use lowercase key gt only because contract v1 requires it; "
            "do not output canonical, ground_truth, obs, input, case_id, action, recovery, or command keys."
        )
    if task == "evidence_extraction":
        return (
            'Return exactly one JSON object shaped like: '
            '{"primary_evidence":[{"eid":"e1"}],'
            '"secondary_evidence":[{"eid":"e2"}],'
            '"symptom_evidence":[],'
            '"noise_evidence":[{"eid":"e3"}]}. '
            "Use only evidence EIDs present in L2_INPUT_JSON. Evidence is OBS only; do not emit gt, obs, input, "
            "case_id, diagnosis, primary_family, primary_subtype, action, recovery, or command keys."
        )
    if task == "cause_vs_symptom":
        return (
            'Return exactly one JSON object shaped like: '
            '{"primary_family":"net","primary_subtype":"<one approved net subtype>",'
            '"cause_eids":["e1"],"symptom_eids":[],"gt_obs_separated":true}. '
            f"Approved subtypes: {allowed}. Do not promote OBS primary_family/state to root cause. "
            "Do not emit gt, obs, input, case_id, evidence, action, recovery, or command keys."
        )
    raise UserError("UNEXPECTED_TASK", f"unsupported task: {task}")


def _contract_system_prompt() -> str:
    return (
        "You are a KaiHongOS/OpenHarmony OS NET-only diagnostic evaluator. "
        "Output only one valid JSON object. Do not output Markdown, comments, code fences, natural-language prefaces, "
        "or explanations outside JSON. Do not echo the input log, OBS score fields, or L2_INPUT_JSON. "
        "Do not output action, recovery, remediation, command, shell, repair, or board-operation suggestions. "
        "Preserve GT/OBS separation: evidence is OBS and must not replace the NET root-cause label. "
        "For uncertainty, leave optional text short and choose only values allowed by the task contract."
    )


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
        "- Do not copy OBS score field names into the output.\n"
        "- Do not include action/recovery/command suggestions.\n"
        "- Output the JSON object and stop.\n\n"
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
    action_rows = [item["sample_id"] for item in test_rows if item["task"] == "action_after_diagnosis"]
    if action_rows:
        raise UserError("ACTION_AFTER_DIAGNOSIS_INCLUDED", f"action rows found: {action_rows[:3]}")
    gold_subtypes = _gold_family_subtype_for_rows(test_rows)
    subtype_counts = Counter(subtype for _family, subtype in gold_subtypes.values())
    return {
        "test_samples": len(test_rows),
        "task_distribution": dict(sorted(task_counts.items())),
        "subtype_distribution": dict(sorted(subtype_counts.items())),
        "action_after_diagnosis_count": 0,
        "cpu_mem_gt_count": 0,
    }


def _generate_predictions(
    args: argparse.Namespace,
    test_rows: List[Dict[str, Any]],
    contract: Dict[str, Any],
    output_dir: Path,
    log: gen_smoke.RunLogger,
) -> List[Dict[str, Any]]:
    torch, tokenizer, model = gen_smoke._load_model_and_tokenizer(args, log)
    device = gen_smoke._model_device(model)
    gold_by_sample = _gold_family_subtype_for_rows(test_rows)
    generation_config = {
        "do_sample": False,
        "max_new_tokens": args.max_new_tokens,
        "temperature": None,
        "top_p": None,
        "contract_aware_prompt": True,
        "prompt_version": PROMPT_VERSION,
    }
    predictions: List[Dict[str, Any]] = []
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
            prediction_text = tokenizer.decode(new_tokens, skip_special_tokens=True).strip()
            gold_family, gold_subtype = gold_by_sample[item["sample_id"]]
            row = {
                "sample_id": item["sample_id"],
                "run_id": item["run_id"],
                "task": item["task"],
                "gold_family": gold_family,
                "gold_subtype": gold_subtype,
                "prompt_version": PROMPT_VERSION,
                "contract_version": CONTRACT_VERSION,
                "prompt_hash": prompt_hash,
                "prediction_text": prediction_text,
                "generation_config": generation_config,
                "adapter_path": str(args.adapter_path),
                "source_test_jsonl": str(args.test_file),
            }
            predictions.append(row)
            _jsonl_write(output_dir / "contract_aware_predictions.jsonl", predictions)
            log.info(f"generated {idx}/{len(test_rows)} sample_id={item['sample_id']} task={item['task']}")
    return predictions


def _contract_evaluate(
    test_rows: List[Dict[str, Any]],
    predictions: List[Dict[str, Any]],
    contract: Dict[str, Any],
    predictions_path: Path,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    joined = cal._join_rows(test_rows, predictions)
    return contract_checker._evaluate_dataset(
        "task_7d_contract_aware_predictions",
        joined,
        predictions,
        contract,
        predictions_path,
    )


def _load_baseline_7a(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        raise UserError("BASELINE_7A_SUMMARY_NOT_FOUND", f"missing 7A baseline summary: {path}")
    return _load_json(path)


def _comparison_with_7a(summary_7d: Dict[str, Any], baseline_7a: Dict[str, Any]) -> Dict[str, Any]:
    keys = [
        "json_parse_success_rate",
        "target_schema_success_rate",
        "semantic_metric_eligible_rate",
        "cpu_mem_prediction_echo_count",
        "hard_gt_obs_leak_count",
        "soft_gt_obs_risk_count",
        "action_recommendation_leak_count",
        "recovery_command_leak_count",
    ]
    comparison = {
        key: {
            "task_7a": baseline_7a.get(key),
            "task_7d_contract_aware": summary_7d.get(key),
        }
        for key in keys
    }
    old_schema = float(baseline_7a.get("target_schema_success_rate") or 0.0)
    new_schema = float(summary_7d.get("target_schema_success_rate") or 0.0)
    comparison["target_schema_improved_over_7a"] = new_schema > old_schema
    comparison["prompt_alignment_only_not_model_performance"] = True
    return comparison


def run(args: argparse.Namespace) -> int:
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    args.test_file = _resolve_test_file(args.test_file)
    args.contract_json = _resolve_file(args.contract_json, "contract_json")
    args.baseline_7a_summary = _resolve_file(args.baseline_7a_summary, "baseline_7a_summary")
    args.model_name_or_path = _resolve_dir(args.model_name_or_path, "model_name_or_path")
    args.adapter_path = _resolve_dir(args.adapter_path, "adapter_path")
    adapter_config = args.adapter_path / "adapter_config.json"
    if not adapter_config.is_file():
        raise UserError("ADAPTER_CONFIG_NOT_FOUND", f"adapter_config.json missing under {args.adapter_path}")
    args.output_dir = _resolve_output_dir(args.output_dir)
    if not args.validate_only:
        args.output_dir.mkdir(parents=True, exist_ok=False)
    contract = _load_json(args.contract_json)
    if contract.get("schema_version") != CONTRACT_VERSION:
        raise UserError("CONTRACT_VERSION_MISMATCH", f"unexpected contract version: {contract.get('schema_version')}")
    test_rows = cal._read_test_rows(args.test_file)
    preflight = _validate_test_scope(test_rows)

    if args.validate_only:
        print(
            json.dumps(
                {
                    "result": "VALIDATE_ONLY_PASS",
                    "test_file": str(args.test_file),
                    "contract_json": str(args.contract_json),
                    "adapter_path": str(args.adapter_path),
                    **preflight,
                    "training_started": False,
                    "weight_update_started": False,
                    "model_loaded": False,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0

    log = gen_smoke.RunLogger(args.output_dir)
    try:
        _json_dump(
            args.output_dir / "generation_manifest.json",
            {
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
                "training_started": False,
                "weight_update_started": False,
                "new_adapter_created": False,
                "existing_adapter_modified": False,
                "prompt_alignment_only_not_model_performance": True,
                **preflight,
            },
        )
        prompt_text = (
            _contract_system_prompt()
            + "\n\n"
            + "\n\n".join(_contract_task_shape(task, contract) for task in EXPECTED_TASK_COUNTS)
        )
        (args.output_dir / "generation_prompt_v1.txt").write_text(prompt_text + "\n", encoding="utf-8")
        log.info("starting contract-aware generation")
        predictions = _generate_predictions(args, test_rows, contract, args.output_dir, log)
        predictions_path = args.output_dir / "contract_aware_predictions.jsonl"
        summary_7d, per_sample = _contract_evaluate(test_rows, predictions, contract, predictions_path)
        baseline_7a = _load_baseline_7a(args.baseline_7a_summary)
        comparison = _comparison_with_7a(summary_7d, baseline_7a)
        _json_dump(args.output_dir / "contract_aware_metrics_summary.json", summary_7d)
        _jsonl_write(args.output_dir / "per_sample_contract_audit.jsonl", per_sample)
        _json_dump(args.output_dir / "comparison_with_7a.json", comparison)
        summary = {
            "schema_version": SCHEMA_VERSION,
            "result": "PASS",
            "smoke_only_note": SMOKE_NOTE,
            "output_dir": str(args.output_dir),
            "predictions": str(predictions_path),
            "prediction_count": len(predictions),
            "prompt_version": PROMPT_VERSION,
            "prompt_hash": _sha256_file(args.output_dir / "generation_prompt_v1.txt"),
            "contract_aware_metrics": summary_7d,
            "comparison_with_7a": comparison,
            "conclusions": {
                "target_schema_improved_over_7a": comparison["target_schema_improved_over_7a"],
                "ready_for_formal_training_plan": True,
                "ready_for_formal_performance_reporting": summary_7d["target_schema_success_rate"] == 1.0,
                "prompt_alignment_only_not_model_performance": True,
                "contract_v1_modified": False,
                "data_jsonl_modified": False,
                "adapter_modified": False,
            },
        }
        _json_dump(args.output_dir / "generation_summary.json", summary)
        log.info("contract-aware generation smoke completed")
        return 0
    finally:
        log.close()


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-name-or-path", default=str(DEFAULT_MODEL))
    parser.add_argument("--adapter-path", default=str(DEFAULT_ADAPTER))
    parser.add_argument("--test-file", default=str(DEFAULT_TEST_FILE))
    parser.add_argument("--contract-json", default=str(DEFAULT_CONTRACT_JSON))
    parser.add_argument("--baseline-7a-summary", default=str(DEFAULT_BASELINE_7C_SUMMARY))
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--max-new-tokens", type=int, default=512)
    parser.add_argument("--no-4bit", action="store_true", help="Load the base model without 4-bit quantization.")
    parser.add_argument("--validate-only", action="store_true")
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    try:
        return run(parse_args(argv))
    except UserError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
