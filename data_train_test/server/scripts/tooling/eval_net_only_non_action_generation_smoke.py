#!/usr/bin/env python3
"""NET-only non-action held-out generation smoke evaluator.

This prototype intentionally performs generation only. It does not import the
Trainer stack, does not update weights, and uses trace_index.jsonl only for
metadata mapping and validation.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple


PROJECT_ROOT = Path("/home/xrh/qwen3_os_fault")
CANDIDATE_ROOT = PROJECT_ROOT / "data/training_candidates/net_only_non_action_formal_20260518"
DIAGNOSTIC_OUTPUT_ROOT = PROJECT_ROOT / "outputs/diagnostic_eval"
DEFAULT_MODEL = Path("/home/xrh/models/Qwen/Qwen3-8B")
DEFAULT_ADAPTER = (
    PROJECT_ROOT
    / "outputs/training_smoke/net_only_non_action_train_val_smoke_rerun_20260518_20260519_084005/adapter"
)

EXPECTED_TOTAL_TEST_ROWS = 42
EXPECTED_TASK_COUNTS = {
    "diagnosis": 14,
    "evidence_extraction": 14,
    "cause_vs_symptom": 14,
}
EXPECTED_SUBTYPE_COUNTS = {
    "net_dns_fail": 6,
    "net_no_default_route": 6,
    "net_no_ipv4_on_iface": 6,
    "net_public_ip_unreachable": 6,
    "net_wifi_auth_fail_wrong_psk": 6,
    "net_wifi_disconnect": 6,
    "net_wrong_default_route": 6,
}
REQUIRED_FIELDS = {
    "diagnosis": [
        "diagnosis_summary",
        "gt",
        "gt.family",
        "gt.subtype",
        "gt_obs_separated",
    ],
    "evidence_extraction": [
        "primary_evidence",
        "secondary_evidence",
        "symptom_evidence",
        "noise_evidence",
    ],
    "cause_vs_symptom": [
        "primary_family",
        "primary_subtype",
        "cause_eids",
        "symptom_eids",
        "gt_obs_separated",
    ],
}
ACTION_LEAK_PATTERNS = (
    "action_after_diagnosis",
    "actions_device",
    "recovery_action",
    "recover the",
    "rollback",
    "restart service",
    "reboot",
    "route add",
    "ifconfig",
    "ip route",
)
CPU_MEM_PATTERN = re.compile(r"(^|[^a-z0-9])(cpu|mem|memory|oom)([^a-z0-9]|$)")
NET_FAMILY = "net"
SMOKE_NOTE = "Smoke-only prototype: adapter came from max_steps=3 training smoke; metrics are not formal performance."


class UserError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RunLogger:
    def __init__(self, output_dir: Path) -> None:
        self.stdout_path = output_dir / "stdout.log"
        self.stderr_path = output_dir / "stderr.log"
        self._stdout = self.stdout_path.open("a", encoding="utf-8")
        self._stderr = self.stderr_path.open("a", encoding="utf-8")

    def close(self) -> None:
        self._stdout.close()
        self._stderr.close()

    def info(self, message: str) -> None:
        line = f"{_now_iso()} {message}"
        print(line, flush=True)
        print(line, file=self._stdout, flush=True)

    def error(self, message: str) -> None:
        line = f"{_now_iso()} {message}"
        print(line, file=sys.stderr, flush=True)
        print(line, file=self._stderr, flush=True)


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


def _resolve_existing_file(raw: str, label: str) -> Path:
    if _has_glob_chars(raw):
        raise UserError("UNSAFE_GLOB_PREVENTED", f"{label} must be an explicit path: {raw}")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise UserError("ABSOLUTE_PATH_REQUIRED", f"{label} must be absolute: {raw}")
    if path.is_dir():
        raise UserError("DIRECTORY_INPUT_REJECTED", f"{label} must be a file, not a directory: {path}")
    if not path.is_file():
        raise UserError("FILE_NOT_FOUND", f"{label} not found: {path}")
    return path.resolve()


def _resolve_test_file(raw: str) -> Path:
    path = _resolve_existing_file(raw, "test_file")
    if path.name in {"train.jsonl", "val.jsonl", "all.jsonl"}:
        raise UserError("HELD_OUT_TEST_FILE_REQUIRED", f"--test-file cannot be {path.name}")
    if path.name != "test.jsonl":
        raise UserError("CANONICAL_TEST_FILE_REQUIRED", f"--test-file must be canonical test.jsonl: {path}")
    if path.resolve() != (CANDIDATE_ROOT / "test.jsonl").resolve():
        raise UserError("APPROVED_TEST_ROOT_REQUIRED", f"--test-file must be under {CANDIDATE_ROOT}: {path}")
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


def _resolve_existing_dir(raw: str, label: str) -> Path:
    if _has_glob_chars(raw):
        raise UserError("UNSAFE_GLOB_PREVENTED", f"{label} must be explicit: {raw}")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise UserError("ABSOLUTE_PATH_REQUIRED", f"{label} must be absolute: {raw}")
    if not path.is_dir():
        raise UserError("DIRECTORY_NOT_FOUND", f"{label} directory not found: {path}")
    return path.resolve()


def _load_jsonl(path: Path) -> Tuple[List[Dict[str, Any]], List[str]]:
    rows: List[Dict[str, Any]] = []
    raw_lines: List[str] = []
    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            stripped = line.rstrip("\n\r")
            if not stripped:
                raise UserError("EMPTY_JSONL_LINE", f"{path} has an empty line at {line_no}")
            try:
                rows.append(json.loads(stripped))
            except json.JSONDecodeError as exc:
                raise UserError("JSONL_PARSE_FAILED", f"{path} line {line_no}: {exc}") from exc
            raw_lines.append(stripped)
    return rows, raw_lines


def _safe_json_object(text: str) -> Optional[Dict[str, Any]]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL | re.IGNORECASE).strip()
    for candidate in _json_candidates(cleaned):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
            return parsed[0]
    return None


def _json_candidates(text: str) -> Iterator[str]:
    yield text
    start_positions = [idx for idx, ch in enumerate(text) if ch == "{"]
    for start in start_positions:
        in_string = False
        escape = False
        depth = 0
        for idx in range(start, len(text)):
            ch = text[idx]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    yield text[start : idx + 1]
                    break


def _get_path(obj: Dict[str, Any], dotted: str) -> Any:
    cur: Any = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _has_path(obj: Optional[Dict[str, Any]], dotted: str) -> bool:
    if obj is None:
        return False
    return _get_path(obj, dotted) is not None


def _recursive_keys(obj: Any) -> Set[str]:
    found: Set[str] = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            found.add(str(key))
            found.update(_recursive_keys(value))
    elif isinstance(obj, list):
        for item in obj:
            found.update(_recursive_keys(item))
    return found


def _extract_eids(value: Any) -> Set[str]:
    eids: Set[str] = set()
    if isinstance(value, str):
        eids.add(value)
    elif isinstance(value, dict):
        raw = value.get("eid")
        if isinstance(raw, str):
            eids.add(raw)
    elif isinstance(value, list):
        for item in value:
            eids.update(_extract_eids(item))
    return eids


def _evidence_eids(gold: Dict[str, Any], fields: Sequence[str]) -> Set[str]:
    eids: Set[str] = set()
    for field in fields:
        eids.update(_extract_eids(gold.get(field)))
    return eids


def _prediction_eids(pred: Optional[Dict[str, Any]], fields: Sequence[str]) -> Set[str]:
    if pred is None:
        return set()
    eids: Set[str] = set()
    for field in fields:
        eids.update(_extract_eids(pred.get(field)))
    return eids


def _ratio(numerator: int, denominator: int) -> float:
    return float(numerator) / float(denominator) if denominator else 0.0


def _f1(precision: float, recall: float) -> float:
    return 2.0 * precision * recall / (precision + recall) if precision + recall else 0.0


def _normalize_string(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value.strip()
    return None


def _prediction_family_subtype(pred: Optional[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
    if pred is None:
        return None, None
    family = _normalize_string(_get_path(pred, "gt.family")) or _normalize_string(pred.get("primary_family"))
    subtype = _normalize_string(_get_path(pred, "gt.subtype")) or _normalize_string(pred.get("primary_subtype"))
    return family, subtype


def _task_routing_error(task: str, pred: Optional[Dict[str, Any]]) -> bool:
    if pred is None:
        return False
    keys = _recursive_keys(pred)
    if task == "diagnosis":
        return bool(keys.intersection({"primary_evidence", "cause_eids", "symptom_eids", "primary_subtype"}))
    if task == "evidence_extraction":
        return bool(keys.intersection({"diagnosis_summary", "gt", "primary_subtype", "cause_eids"}))
    if task == "cause_vs_symptom":
        return bool(keys.intersection({"diagnosis_summary", "gt", "primary_evidence", "secondary_evidence"}))
    return True


def _gt_field_leak(task: str, pred: Optional[Dict[str, Any]]) -> bool:
    if pred is None or task == "diagnosis":
        return False
    keys = _recursive_keys(pred)
    return bool(keys.intersection({"gt", "ground_truth", "gold", "gold_family", "gold_subtype"}))


def _action_leak(text: str, pred: Optional[Dict[str, Any]]) -> bool:
    lowered = text.lower()
    if any(marker in lowered for marker in ACTION_LEAK_PATTERNS):
        return True
    if pred is None:
        return False
    keys = {key.lower() for key in _recursive_keys(pred)}
    return bool(keys.intersection({"action", "actions", "recovery", "recovery_steps", "remediation"}))


def _cpu_mem_contamination(text: str, pred: Optional[Dict[str, Any]]) -> bool:
    if CPU_MEM_PATTERN.search(text.lower()):
        return True
    if pred is None:
        return False
    return any(CPU_MEM_PATTERN.search(key.lower()) for key in _recursive_keys(pred))


def _validate_trace_and_test(test_file: Path, trace_file: Path) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    rows, raw_lines = _load_jsonl(test_file)
    trace_rows, _ = _load_jsonl(trace_file)
    if len(rows) != EXPECTED_TOTAL_TEST_ROWS:
        raise UserError("TEST_ROW_COUNT_FAILED", f"expected 42 test rows, got {len(rows)}")

    trace_by_line: Dict[int, Dict[str, Any]] = {}
    split_run_ids: Dict[str, Set[str]] = defaultdict(set)
    all_sample_ids: List[str] = []
    action_after_count = 0
    cpu_mem_trace_hits: List[str] = []

    for trace in trace_rows:
        split = trace.get("split")
        run_id = trace.get("run_id")
        sample_id = trace.get("sample_id") or trace.get("materialized_sample_id")
        if split and run_id:
            split_run_ids[str(split)].add(str(run_id))
        if sample_id:
            all_sample_ids.append(str(sample_id))
        if trace.get("action_after_diagnosis_included") not in (False, 0, None):
            action_after_count += 1
        trace_text = " ".join(
            str(trace.get(k, ""))
            for k in ("sample_id", "sample_type", "task", "subtype", "source_l2_file", "source_full_l2_file")
        )
        if CPU_MEM_PATTERN.search(trace_text.lower()):
            cpu_mem_trace_hits.append(str(sample_id or trace.get("materialized_line")))
        if split == "test" and trace.get("materialized_file") == "test.jsonl":
            materialized_line = trace.get("materialized_line")
            if not isinstance(materialized_line, int):
                raise UserError("TRACE_LINE_MAPPING_FAILED", f"invalid materialized_line in trace row: {trace}")
            trace_by_line[materialized_line] = trace

    if len(trace_by_line) != EXPECTED_TOTAL_TEST_ROWS:
        raise UserError("TRACE_TEST_MAPPING_FAILED", f"expected 42 test trace rows, got {len(trace_by_line)}")

    duplicate_sample_ids = sorted(sample_id for sample_id, count in Counter(all_sample_ids).items() if count > 1)
    if duplicate_sample_ids:
        raise UserError("DUPLICATE_SAMPLE_ID_FOUND", f"duplicate sample_id values: {duplicate_sample_ids[:5]}")
    if action_after_count:
        raise UserError("ACTION_AFTER_DIAGNOSIS_FOUND", f"trace action_after_diagnosis count: {action_after_count}")
    if cpu_mem_trace_hits:
        raise UserError("CPU_MEM_TRACE_CONTAMINATION", f"CPU/MEM trace hits: {cpu_mem_trace_hits[:5]}")

    leakage: Dict[str, List[str]] = {}
    for left, right in (("train", "val"), ("train", "test"), ("val", "test")):
        overlap = sorted(split_run_ids.get(left, set()).intersection(split_run_ids.get(right, set())))
        if overlap:
            leakage[f"{left}_vs_{right}"] = overlap
    if leakage:
        raise UserError("RUN_ID_LEAKAGE_FOUND", f"train/val/test run_id leakage: {leakage}")

    mapped: List[Dict[str, Any]] = []
    for idx, row in enumerate(rows, 1):
        if sorted(row.keys()) != ["messages"]:
            raise UserError("MESSAGES_ONLY_REQUIRED", f"test line {idx} must be messages-only")
        messages = row.get("messages")
        if not isinstance(messages, list) or len(messages) < 3:
            raise UserError("MESSAGES_SCHEMA_REQUIRED", f"test line {idx} has invalid messages")
        if messages[-1].get("role") != "assistant":
            raise UserError("ASSISTANT_GOLD_REQUIRED", f"test line {idx} must end with assistant gold")
        trace = trace_by_line.get(idx)
        if trace is None:
            raise UserError("TRACE_LINE_MAPPING_FAILED", f"no trace row for test line {idx}")
        raw_hash = _sha256_text(raw_lines[idx - 1])
        expected_hash = trace.get("materialized_row_sha256")
        hash_ok = expected_hash in (None, raw_hash)
        if not hash_ok:
            raise UserError("TRACE_ROW_HASH_MISMATCH", f"test line {idx} hash mismatch")
        gold = _safe_json_object(str(messages[-1].get("content", "")))
        if gold is None:
            raise UserError("GOLD_JSON_PARSE_FAILED", f"assistant gold is not JSON at test line {idx}")
        task = trace.get("task")
        subtype = trace.get("subtype")
        if task not in EXPECTED_TASK_COUNTS:
            raise UserError("TASK_DISTRIBUTION_FAILED", f"unexpected task {task!r} at test line {idx}")
        if subtype not in EXPECTED_SUBTYPE_COUNTS:
            raise UserError("SUBTYPE_DISTRIBUTION_FAILED", f"unexpected subtype {subtype!r} at test line {idx}")
        mapped.append(
            {
                "line": idx,
                "row": row,
                "messages": messages,
                "prompt_messages": messages[:-1],
                "gold": gold,
                "trace": trace,
                "sample_id": trace.get("sample_id") or trace.get("materialized_sample_id"),
                "run_id": trace.get("run_id"),
                "task": task,
                "gold_family": NET_FAMILY,
                "gold_subtype": subtype,
                "row_sha256": raw_hash,
            }
        )

    task_counts = Counter(item["task"] for item in mapped)
    subtype_counts = Counter(item["gold_subtype"] for item in mapped)
    if dict(task_counts) != EXPECTED_TASK_COUNTS:
        raise UserError("TASK_DISTRIBUTION_FAILED", f"expected {EXPECTED_TASK_COUNTS}, got {dict(task_counts)}")
    if dict(subtype_counts) != EXPECTED_SUBTYPE_COUNTS:
        raise UserError("SUBTYPE_DISTRIBUTION_FAILED", f"expected {EXPECTED_SUBTYPE_COUNTS}, got {dict(subtype_counts)}")

    validation = {
        "test_file": str(test_file),
        "trace_index": str(trace_file),
        "total_samples": len(mapped),
        "messages_only_rows": True,
        "metadata_source": "trace_index.jsonl by split/materialized_file/materialized_line",
        "samples_by_task": dict(sorted(task_counts.items())),
        "samples_by_subtype": dict(sorted(subtype_counts.items())),
        "action_after_diagnosis_count": 0,
        "cpu_mem_trace_count": 0,
        "duplicate_sample_id_found": False,
        "run_id_leakage_found": False,
        "trace_used_as_model_input": False,
        "trace_used_for_metadata_mapping": True,
        "train_val_all_used_as_eval_input": False,
        "row_hash_validated": True,
    }
    return mapped, validation


def _apply_chat_template(tokenizer: Any, messages: List[Dict[str, Any]]) -> str:
    try:
        return tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=False,
        )
    except TypeError:
        return tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)


def _load_model_and_tokenizer(args: argparse.Namespace, logger: RunLogger) -> Tuple[Any, Any, Any]:
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

    logger.info("loading tokenizer")
    tokenizer = AutoTokenizer.from_pretrained(str(args.model_name_or_path), trust_remote_code=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "left"

    quant_config = None
    if not args.no_4bit:
        quant_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16 if torch.cuda.is_available() else torch.float32,
            bnb_4bit_use_double_quant=True,
        )
    logger.info("loading base model")
    model_kwargs: Dict[str, Any] = {"device_map": "auto", "trust_remote_code": True}
    if quant_config is not None:
        model_kwargs["quantization_config"] = quant_config
    else:
        model_kwargs["torch_dtype"] = torch.bfloat16 if torch.cuda.is_available() else torch.float32
    base_model = AutoModelForCausalLM.from_pretrained(str(args.model_name_or_path), **model_kwargs)
    logger.info("loading adapter")
    model = PeftModel.from_pretrained(base_model, str(args.adapter_path), is_trainable=False)
    for param in model.parameters():
        param.requires_grad_(False)
    model.eval()
    if hasattr(model, "config"):
        model.config.use_cache = True
    return torch, tokenizer, model


def _model_device(model: Any) -> Any:
    with contextlib.suppress(Exception):
        return next(model.parameters()).device
    return "cuda"


def _generate_predictions(
    args: argparse.Namespace,
    mapped_rows: List[Dict[str, Any]],
    output_dir: Path,
    logger: RunLogger,
) -> List[Dict[str, Any]]:
    torch, tokenizer, model = _load_model_and_tokenizer(args, logger)
    device = _model_device(model)
    predictions: List[Dict[str, Any]] = []
    generation_config = {
        "do_sample": False,
        "max_new_tokens": args.max_new_tokens,
        "temperature": None,
        "top_p": None,
    }
    total = len(mapped_rows)
    with torch.inference_mode():
        for idx, item in enumerate(mapped_rows, 1):
            prompt_text = _apply_chat_template(tokenizer, item["prompt_messages"])
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
            row = {
                "sample_id": item["sample_id"],
                "run_id": item["run_id"],
                "task": item["task"],
                "gold_family": item["gold_family"],
                "gold_subtype": item["gold_subtype"],
                "prompt_hash": _sha256_text(prompt_text),
                "prediction_text": prediction_text,
                "generation_config": generation_config,
                "adapter_path": str(args.adapter_path),
                "source_test_jsonl": str(args.test_file),
            }
            predictions.append(row)
            _jsonl_write(output_dir / "predictions.jsonl", predictions)
            logger.info(f"generated {idx}/{total} sample_id={item['sample_id']} task={item['task']}")
    return predictions


def _per_sample_metric(item: Dict[str, Any], pred_row: Dict[str, Any]) -> Dict[str, Any]:
    text = pred_row.get("prediction_text") or ""
    pred = _safe_json_object(text)
    task = item["task"]
    gold = item["gold"]
    required = REQUIRED_FIELDS[task]
    present = [field for field in required if _has_path(pred, field)]
    missing = [field for field in required if field not in present]
    required_rate = _ratio(len(present), len(required))
    family, subtype = _prediction_family_subtype(pred)

    gold_primary = _evidence_eids(gold, ["primary_evidence"])
    gold_secondary = _evidence_eids(gold, ["secondary_evidence"])
    gold_symptom = _evidence_eids(gold, ["symptom_evidence"])
    gold_noise = _evidence_eids(gold, ["noise_evidence"])
    gold_cause = _extract_eids(gold.get("cause_eids"))
    gold_cause_symptom = _extract_eids(gold.get("symptom_eids"))
    available_eids = gold_primary | gold_secondary | gold_symptom | gold_noise | gold_cause | gold_cause_symptom

    pred_primary = _prediction_eids(pred, ["primary_evidence"])
    pred_all_evidence = _prediction_eids(
        pred,
        ["primary_evidence", "secondary_evidence", "symptom_evidence", "noise_evidence", "cause_eids", "symptom_eids"],
    )
    evidence_overlap = sorted(pred_all_evidence.intersection(available_eids))
    evidence_hallucinated = sorted(pred_all_evidence - available_eids)
    evidence_precision = _ratio(len(pred_primary.intersection(gold_primary)), len(pred_primary))
    evidence_recall = _ratio(len(pred_primary.intersection(gold_primary)), len(gold_primary))
    cause_pred = _prediction_eids(pred, ["cause_eids"])
    symptom_pred = _prediction_eids(pred, ["symptom_eids"])

    gt_obs_value = _get_path(pred or {}, "gt_obs_separated") if pred is not None else None
    gt_obs_risk = False
    if task in {"diagnosis", "cause_vs_symptom"} and gt_obs_value is not True:
        gt_obs_risk = True
    if _gt_field_leak(task, pred):
        gt_obs_risk = True

    schema_violations = []
    if pred is None:
        schema_violations.append("prediction_not_parseable_json_object")
    if missing:
        schema_violations.append("required_fields_missing")
    if _task_routing_error(task, pred):
        schema_violations.append("task_routing_error")
    if _gt_field_leak(task, pred):
        schema_violations.append("gt_field_leak")

    diagnosis_family_correct = task == "diagnosis" and family == NET_FAMILY
    diagnosis_subtype_correct = task == "diagnosis" and subtype == item["gold_subtype"]
    cause_family_correct = task == "cause_vs_symptom" and family == NET_FAMILY
    cause_subtype_correct = task == "cause_vs_symptom" and subtype == item["gold_subtype"]
    cause_primary_hit = bool(cause_pred.intersection(gold_cause)) if task == "cause_vs_symptom" else False
    cause_symptom_hit = symptom_pred == gold_cause_symptom if task == "cause_vs_symptom" else False
    cause_inversion = bool(cause_pred.intersection(gold_cause_symptom) or symptom_pred.intersection(gold_cause))

    return {
        "sample_id": item["sample_id"],
        "run_id": item["run_id"],
        "line": item["line"],
        "task": task,
        "gold_family": item["gold_family"],
        "gold_subtype": item["gold_subtype"],
        "prediction_empty": not bool(text.strip()),
        "schema_parse_success": pred is not None,
        "required_fields_present": present,
        "required_fields_missing": missing,
        "required_field_presence_rate": required_rate,
        "task_routing_error": _task_routing_error(task, pred),
        "diagnosis_family_pred": family if task == "diagnosis" else None,
        "diagnosis_subtype_pred": subtype if task == "diagnosis" else None,
        "diagnosis_family_correct": diagnosis_family_correct,
        "diagnosis_subtype_correct": diagnosis_subtype_correct,
        "evidence_precision": evidence_precision if task == "evidence_extraction" else None,
        "evidence_recall": evidence_recall if task == "evidence_extraction" else None,
        "evidence_f1": _f1(evidence_precision, evidence_recall) if task == "evidence_extraction" else None,
        "evidence_overlap_eids": evidence_overlap,
        "evidence_primary_hit": bool(pred_primary.intersection(gold_primary)) if task == "evidence_extraction" else False,
        "evidence_hallucinated_eids": evidence_hallucinated,
        "cause_family_pred": family if task == "cause_vs_symptom" else None,
        "cause_subtype_pred": subtype if task == "cause_vs_symptom" else None,
        "cause_family_correct": cause_family_correct,
        "cause_subtype_correct": cause_subtype_correct,
        "cause_primary_eid_hit": cause_primary_hit,
        "cause_symptom_eids_correct": cause_symptom_hit,
        "cause_symptom_inversion": cause_inversion,
        "gt_obs_risk": gt_obs_risk,
        "action_or_recovery_leak": _action_leak(text, pred),
        "cpu_mem_contamination": _cpu_mem_contamination(text, pred),
        "gt_field_leak": _gt_field_leak(task, pred),
        "schema_violations": schema_violations,
    }


def _aggregate_metrics(per_sample: List[Dict[str, Any]], mapped_rows: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    total = len(per_sample)
    task_counts = Counter(row["task"] for row in per_sample)
    subtype_counts = Counter(row["gold_subtype"] for row in per_sample)
    parse_success = sum(1 for row in per_sample if row["schema_parse_success"])
    empty = sum(1 for row in per_sample if row["prediction_empty"])
    required_rates = [row["required_field_presence_rate"] for row in per_sample]
    routing_errors = sum(1 for row in per_sample if row["task_routing_error"])
    action_leaks = sum(1 for row in per_sample if row["action_or_recovery_leak"])
    cpu_mem = sum(1 for row in per_sample if row["cpu_mem_contamination"])
    gt_leaks = sum(1 for row in per_sample if row["gt_field_leak"])
    gt_obs_risk = sum(1 for row in per_sample if row["gt_obs_risk"])
    schema_violation_count = sum(1 for row in per_sample if row["schema_violations"])

    diagnosis_rows = [row for row in per_sample if row["task"] == "diagnosis"]
    evidence_rows = [row for row in per_sample if row["task"] == "evidence_extraction"]
    cause_rows = [row for row in per_sample if row["task"] == "cause_vs_symptom"]

    family_confusion: Dict[str, Counter[str]] = defaultdict(Counter)
    subtype_confusion: Dict[str, Counter[str]] = defaultdict(Counter)
    for row in diagnosis_rows:
        family_confusion[row["gold_family"]][row.get("diagnosis_family_pred") or "__missing__"] += 1
        subtype_confusion[row["gold_subtype"]][row.get("diagnosis_subtype_pred") or "__missing__"] += 1

    evidence_precision = [row["evidence_precision"] for row in evidence_rows if row["evidence_precision"] is not None]
    evidence_recall = [row["evidence_recall"] for row in evidence_rows if row["evidence_recall"] is not None]
    evidence_f1 = [row["evidence_f1"] for row in evidence_rows if row["evidence_f1"] is not None]
    evidence_hallucination = sum(1 for row in evidence_rows if row["evidence_hallucinated_eids"])
    evidence_primary_hit = sum(1 for row in evidence_rows if row["evidence_primary_hit"])

    confusion = {
        "diagnosis_family_confusion": {
            gold: dict(sorted(preds.items())) for gold, preds in sorted(family_confusion.items())
        },
        "diagnosis_subtype_confusion": {
            gold: dict(sorted(preds.items())) for gold, preds in sorted(subtype_confusion.items())
        },
    }

    metrics = {
        "smoke_only_note": SMOKE_NOTE,
        "total_samples": total,
        "samples_by_task": dict(sorted(task_counts.items())),
        "samples_by_subtype": dict(sorted(subtype_counts.items())),
        "prediction_count": total,
        "missing_prediction_count": 0,
        "empty_prediction_count": empty,
        "schema_parse_success_count": parse_success,
        "schema_parse_success_rate": _ratio(parse_success, total),
        "required_field_presence_rate": sum(required_rates) / len(required_rates) if required_rates else 0.0,
        "task_routing_error_count": routing_errors,
        "diagnosis": {
            "sample_count": len(diagnosis_rows),
            "family_accuracy": _ratio(sum(1 for row in diagnosis_rows if row["diagnosis_family_correct"]), len(diagnosis_rows)),
            "subtype_accuracy": _ratio(
                sum(1 for row in diagnosis_rows if row["diagnosis_subtype_correct"]), len(diagnosis_rows)
            ),
            "confusion_matrix": confusion["diagnosis_subtype_confusion"],
        },
        "evidence": {
            "sample_count": len(evidence_rows),
            "precision": sum(evidence_precision) / len(evidence_precision) if evidence_precision else 0.0,
            "recall": sum(evidence_recall) / len(evidence_recall) if evidence_recall else 0.0,
            "f1": sum(evidence_f1) / len(evidence_f1) if evidence_f1 else 0.0,
            "overlap_sample_count": sum(1 for row in evidence_rows if row["evidence_overlap_eids"]),
            "primary_hit_rate": _ratio(evidence_primary_hit, len(evidence_rows)),
            "hallucination_sample_count": evidence_hallucination,
        },
        "cause_vs_symptom": {
            "sample_count": len(cause_rows),
            "primary_family_accuracy": _ratio(sum(1 for row in cause_rows if row["cause_family_correct"]), len(cause_rows)),
            "primary_subtype_accuracy": _ratio(sum(1 for row in cause_rows if row["cause_subtype_correct"]), len(cause_rows)),
            "primary_eid_hit_rate": _ratio(sum(1 for row in cause_rows if row["cause_primary_eid_hit"]), len(cause_rows)),
            "symptom_eids_accuracy": _ratio(sum(1 for row in cause_rows if row["cause_symptom_eids_correct"]), len(cause_rows)),
            "inversion_count": sum(1 for row in cause_rows if row["cause_symptom_inversion"]),
            "gt_obs_risk_count": sum(1 for row in cause_rows if row["gt_obs_risk"]),
        },
        "leakage_and_boundary": {
            "action_recovery_leak_count": action_leaks,
            "cpu_mem_contamination_count": cpu_mem,
            "gt_field_leak_count": gt_leaks,
            "obs_as_gt_risk_count": gt_obs_risk,
            "schema_violation_count": schema_violation_count,
            "non_action_boundary_passed": action_leaks == 0,
            "cpu_mem_boundary_passed": cpu_mem == 0,
        },
        "validation_inputs": {
            "action_after_diagnosis_count": 0,
            "cpu_mem_trace_count": 0,
            "duplicate_sample_id_found": False,
            "run_id_leakage_found": False,
            "train_val_all_used_as_eval_input": False,
            "trace_used_as_model_input": False,
            "trace_used_for_metadata_mapping": True,
        },
    }
    return metrics, confusion


def _read_adapter_config(adapter_path: Path) -> Dict[str, Any]:
    config_path = adapter_path / "adapter_config.json"
    if not config_path.is_file():
        raise UserError("ADAPTER_CONFIG_NOT_FOUND", f"missing adapter_config.json under {adapter_path}")
    with config_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _read_adapter_parent_manifest(adapter_path: Path) -> Optional[Dict[str, Any]]:
    manifest_path = adapter_path.parent / "wrapper_run_manifest.json"
    if not manifest_path.is_file():
        return None
    with manifest_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _build_manifest(
    args: argparse.Namespace,
    output_dir: Path,
    data_validation: Dict[str, Any],
    adapter_config: Dict[str, Any],
    metrics: Optional[Dict[str, Any]],
    started_at: str,
    completed_at: Optional[str],
    status: str,
) -> Dict[str, Any]:
    adapter_parent_manifest = _read_adapter_parent_manifest(args.adapter_path)
    script_path = Path(__file__).resolve()
    return {
        "schema_version": "net_only_diagnostic_eval_prototype_v1",
        "status": status,
        "started_at_utc": started_at,
        "completed_at_utc": completed_at,
        "smoke_only_note": SMOKE_NOTE,
        "project_root": str(PROJECT_ROOT),
        "script_path": str(script_path),
        "script_sha256": _sha256_file(script_path) if script_path.is_file() else None,
        "model_name_or_path": str(args.model_name_or_path),
        "adapter_path": str(args.adapter_path),
        "adapter_config": {
            "peft_type": adapter_config.get("peft_type"),
            "r": adapter_config.get("r"),
            "lora_alpha": adapter_config.get("lora_alpha"),
            "lora_dropout": adapter_config.get("lora_dropout"),
            "target_modules": adapter_config.get("target_modules"),
            "base_model_name_or_path": adapter_config.get("base_model_name_or_path"),
        },
        "adapter_parent_manifest": {
            "path": str(args.adapter_path.parent / "wrapper_run_manifest.json"),
            "phase": (adapter_parent_manifest or {}).get("phase"),
            "max_steps": ((adapter_parent_manifest or {}).get("validation") or {}).get("max_steps"),
            "training_started": (adapter_parent_manifest or {}).get("training_started"),
            "training_completed": (adapter_parent_manifest or {}).get("training_completed"),
        },
        "test_file": str(args.test_file),
        "trace_index": str(args.trace_index),
        "output_dir": str(output_dir),
        "generation_config": {
            "do_sample": False,
            "max_new_tokens": args.max_new_tokens,
            "load_in_4bit": not args.no_4bit,
        },
        "read_only_inputs": True,
        "training_started": False,
        "weight_update_started": False,
        "trainer_used": False,
        "model_eval_mode": True,
        "torch_inference_mode": True,
        "data_validation": data_validation,
        "metrics_summary": metrics,
        "outputs": {
            "predictions_jsonl": str(output_dir / "predictions.jsonl"),
            "metrics_summary_json": str(output_dir / "metrics_summary.json"),
            "per_sample_metrics_jsonl": str(output_dir / "per_sample_metrics.jsonl"),
            "confusion_matrix_json": str(output_dir / "confusion_matrix.json"),
            "diagnostic_eval_manifest_json": str(output_dir / "diagnostic_eval_manifest.json"),
            "diagnostic_eval_summary_json": str(output_dir / "diagnostic_eval_summary.json"),
            "stdout_log": str(output_dir / "stdout.log"),
            "stderr_log": str(output_dir / "stderr.log"),
        },
    }


def validate_args(args: argparse.Namespace) -> argparse.Namespace:
    args.test_file = _resolve_test_file(args.test_file)
    args.trace_index = _resolve_existing_file(args.trace_index, "trace_index")
    if args.trace_index.resolve() != (CANDIDATE_ROOT / "trace_index.jsonl").resolve():
        raise UserError("APPROVED_TRACE_INDEX_REQUIRED", f"--trace-index must be canonical trace_index.jsonl: {args.trace_index}")
    args.model_name_or_path = _resolve_existing_dir(args.model_name_or_path, "model_name_or_path")
    args.adapter_path = _resolve_existing_dir(args.adapter_path, "adapter_path")
    args.output_dir = _resolve_output_dir(args.output_dir)
    if args.max_new_tokens <= 0 or args.max_new_tokens > 512:
        raise UserError("MAX_NEW_TOKENS_INVALID", "--max-new-tokens must be between 1 and 512")
    return args


def run(args: argparse.Namespace) -> Dict[str, Any]:
    started_at = _now_iso()
    mapped_rows, data_validation = _validate_trace_and_test(args.test_file, args.trace_index)
    adapter_config = _read_adapter_config(args.adapter_path)
    if args.validate_only:
        return {
            "result": "VALIDATION_ONLY_PASS",
            "created_output_dir": False,
            "training_started": False,
            "model_loaded": False,
            "data_validation": data_validation,
            "adapter_config": {
                "r": adapter_config.get("r"),
                "lora_alpha": adapter_config.get("lora_alpha"),
                "lora_dropout": adapter_config.get("lora_dropout"),
                "target_modules": adapter_config.get("target_modules"),
            },
        }

    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=False)
    logger = RunLogger(output_dir)
    try:
        logger.info("diagnostic generation smoke started")
        manifest = _build_manifest(args, output_dir, data_validation, adapter_config, None, started_at, None, "running")
        _json_dump(output_dir / "diagnostic_eval_manifest.json", manifest)
        predictions = _generate_predictions(args, mapped_rows, output_dir, logger)
        per_sample = [_per_sample_metric(item, pred) for item, pred in zip(mapped_rows, predictions)]
        metrics, confusion = _aggregate_metrics(per_sample, mapped_rows)
        completed_at = _now_iso()
        _jsonl_write(output_dir / "per_sample_metrics.jsonl", per_sample)
        _json_dump(output_dir / "metrics_summary.json", metrics)
        _json_dump(output_dir / "confusion_matrix.json", confusion)
        manifest = _build_manifest(args, output_dir, data_validation, adapter_config, metrics, started_at, completed_at, "completed")
        _json_dump(output_dir / "diagnostic_eval_manifest.json", manifest)
        summary = {
            "result": "PASS",
            "status": "completed",
            "smoke_only_note": SMOKE_NOTE,
            "output_dir": str(output_dir),
            "total_samples": metrics["total_samples"],
            "schema_parse_success_rate": metrics["schema_parse_success_rate"],
            "required_field_presence_rate": metrics["required_field_presence_rate"],
            "diagnosis_subtype_accuracy": metrics["diagnosis"]["subtype_accuracy"],
            "evidence_f1": metrics["evidence"]["f1"],
            "cause_primary_subtype_accuracy": metrics["cause_vs_symptom"]["primary_subtype_accuracy"],
            "action_recovery_leak_count": metrics["leakage_and_boundary"]["action_recovery_leak_count"],
            "cpu_mem_contamination_count": metrics["leakage_and_boundary"]["cpu_mem_contamination_count"],
            "gt_field_leak_count": metrics["leakage_and_boundary"]["gt_field_leak_count"],
            "schema_violation_count": metrics["leakage_and_boundary"]["schema_violation_count"],
        }
        _json_dump(output_dir / "diagnostic_eval_summary.json", summary)
        logger.info("diagnostic generation smoke completed")
        return summary
    except Exception as exc:
        logger.error(f"diagnostic generation smoke failed: {type(exc).__name__}: {exc}")
        manifest = _build_manifest(args, output_dir, data_validation, adapter_config, None, started_at, _now_iso(), "failed")
        manifest["error"] = {"type": type(exc).__name__, "message": str(exc)}
        _json_dump(output_dir / "diagnostic_eval_manifest.json", manifest)
        raise
    finally:
        logger.close()


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="NET-only non-action diagnostic generation smoke prototype.")
    parser.add_argument("--model-name-or-path", default=str(DEFAULT_MODEL))
    parser.add_argument("--adapter-path", default=str(DEFAULT_ADAPTER))
    parser.add_argument("--test-file", default=str(CANDIDATE_ROOT / "test.jsonl"))
    parser.add_argument("--trace-index", default=str(CANDIDATE_ROOT / "trace_index.jsonl"))
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--max-new-tokens", type=int, default=256)
    parser.add_argument("--no-4bit", action="store_true", help="Load the base model without 4-bit quantization.")
    parser.add_argument("--validate-only", action="store_true", help="Validate inputs and guards without creating output.")
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    try:
        args = validate_args(parse_args(argv))
        result = run(args)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except UserError as exc:
        print(json.dumps({"result": exc.code, "error": str(exc), "training_started": False}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
