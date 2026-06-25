#!/usr/bin/env python3
"""Prepare a small, safe evidence-based diagnosis experiment pack.

This preview tool selects a deterministic subset of prompt preview records for a
controlled diagnosis experiment. It does not call any LLM, does not generate
diagnosis answers, and writes nothing unless an explicit guarded output path is
provided.
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import os
import random
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


sys.dont_write_bytecode = True

EXPERIMENT_ID = "EXP_LINKFLAP_P1_2_20260530"
PROMPT_STEERING_VERSION = "p1_2_specific_root_contract"

DIAGNOSTIC_BUCKETS = (
    "root_candidates",
    "supporting_evidence",
    "fault_observations",
    "propagation_evidence",
    "symptom_evidence",
    "contradicting_evidence",
    "uncertain_evidence",
)

PROMPT_BUCKETS = DIAGNOSTIC_BUCKETS + ("do_not_use_evidence",)

PROTECTED_OUTPUT_FILENAMES = {
    "evidence_candidates.jsonl",
    "diagnosis.jsonl",
    "evidence_extraction.jsonl",
    "cause_vs_symptom.jsonl",
    "action_after_diagnosis.jsonl",
    "train.jsonl",
    "val.jsonl",
    "test.jsonl",
}

PROTECTED_OUTPUT_DIRS = {
    "accepted",
    "dataset",
    "dataset_batches",
    "datasets",
    "demo_public_dataset",
    "eval",
    "frozen",
    "generated",
    "inbox",
    "inbox_net",
    "ledger",
    "test",
    "train",
    "training_views",
    "val",
}

SENSITIVE_TERMS = (
    "ground_truth",
    "gt_",
    "gt_family",
    "gt_subtype",
    "primary_subtype",
    "scenario_tag",
    "fault_type",
    "fault_inject",
    "injector",
    "fault_net_",
    "net_dns_fail",
    "net_link_down",
    "net_route_missing",
)

SENSITIVE_PATTERNS = (
    re.compile(r"\bnet_[a-z0-9_]+\b", re.IGNORECASE),
)

REDACTION_MARKER = "[REDACTED_SENSITIVE_CONTEXT]"


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare a small evidence diagnosis experiment pack.")
    parser.add_argument("--root", default=".", help="Repository root. Default: current directory.")
    parser.add_argument("--input", default=None, help="Optional evidence_candidates.jsonl file or directory.")
    parser.add_argument("--prompts-jsonl", default=None, help="Optional prompt preview JSONL input.")
    parser.add_argument("--responses-jsonl", default=None, help="Optional model response JSONL to validate.")
    parser.add_argument("--stdout-only", action="store_true", help="Force stdout output only.")
    parser.add_argument("--dry-run", action="store_true", help="Explicit safety flag; this tool is read-only.")
    parser.add_argument("--format", choices=("json", "markdown", "both", "jsonl"), default="both")
    parser.add_argument("--sample-size", type=int, default=20)
    parser.add_argument("--include-no-root", action="store_true")
    parser.add_argument("--no-root-size", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20260523)
    parser.add_argument("--selection", choices=("stratified", "top_score", "random"), default="stratified")
    parser.add_argument("--max-per-anomaly-type", type=int, default=5)
    parser.add_argument("--max-per-object", type=int, default=5)
    parser.add_argument("--prefer-review-diversity", dest="prefer_review_diversity", action="store_true", default=True)
    parser.add_argument("--no-prefer-review-diversity", dest="prefer_review_diversity", action="store_false")
    parser.add_argument("--prompt-char-budget", type=int, default=12000)
    parser.add_argument("--max-examples", type=int, default=10)
    parser.add_argument("--output", default=None, help="Optional selected prompt JSONL output path.")
    parser.add_argument("--response-template-output", default=None, help="Optional response template JSONL output path.")
    parser.add_argument("--validate-responses", action="store_true")
    parser.add_argument("--strict", action="store_true", help="Treat WARN-level issues as failures.")
    parser.add_argument("--redact-sensitive", dest="redact_sensitive", action="store_true", default=True)
    parser.add_argument("--no-redact-sensitive", dest="redact_sensitive", action="store_false")
    return parser.parse_args(argv)


def safe_json_dump(obj: Any, *, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2)
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return safe_json_dump(value)
    except TypeError:
        return str(value)


def compact_text(value: Any, limit: int = 220) -> str:
    text = safe_text(value).replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def numeric(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_import_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to import module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_text_lossy(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def load_prompts_from_jsonl(path: Path) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    records: List[Dict[str, Any]] = []
    parse_errors: List[Dict[str, Any]] = []
    text = read_text_lossy(path)
    for line_no, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            parse_errors.append(
                {
                    "file": str(path),
                    "line": line_no,
                    "error": str(exc),
                    "text": compact_text(line),
                }
            )
            continue
        if isinstance(value, dict):
            value.setdefault("_source_file", str(path))
            value.setdefault("_source_line", line_no)
            records.append(value)
        else:
            parse_errors.append(
                {
                    "file": str(path),
                    "line": line_no,
                    "error": "JSONL row is not an object",
                    "text": compact_text(value),
                }
            )
    return records, parse_errors


def build_prompts_from_root(root: Path, input_path: Optional[str], args: argparse.Namespace) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    command = [
        sys.executable,
        "-B",
        "tools/evidence_prompt_integration_preview.py",
        "--root",
        ".",
        "--dry-run",
        "--stdout-only",
        "--format",
        "jsonl",
        "--prompt-char-budget",
        str(args.prompt_char_budget),
    ]
    if input_path:
        command.extend(["--input", input_path])
    if args.include_no_root:
        command.extend(["--select", "all", "--include-no-root"])
    proc = subprocess.run(
        command,
        cwd=str(root),
        capture_output=True,
        text=True,
        shell=False,
        timeout=240,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "prompt preview failed: "
            + compact_text(proc.stderr or proc.stdout or f"returncode={proc.returncode}", 1000)
        )
    records: List[Dict[str, Any]] = []
    parse_errors: List[Dict[str, Any]] = []
    for line_no, line in enumerate(proc.stdout.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            parse_errors.append({"line": line_no, "error": str(exc), "text": compact_text(line)})
            continue
        if isinstance(value, dict):
            records.append(value)
        else:
            parse_errors.append({"line": line_no, "error": "JSONL row is not an object", "text": compact_text(value)})
    return records, {
        "mode": "root",
        "command": command,
        "parse_errors": parse_errors,
        "stderr": compact_text(proc.stderr, 1000),
    }


def get_message_content(record: Dict[str, Any], role: str) -> str:
    for item in record.get("messages") or []:
        if isinstance(item, dict) and item.get("role") == role:
            return safe_text(item.get("content"))
    return ""


def parse_user_payload(record: Dict[str, Any]) -> Dict[str, Any]:
    content = get_message_content(record, "user").strip()
    if not content:
        return {}
    if content.startswith("```"):
        match = re.search(r"```(?:json)?\s*(.*?)```", content, re.IGNORECASE | re.DOTALL)
        if match:
            content = match.group(1).strip()
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def iter_values(value: Any) -> Iterable[Any]:
    if isinstance(value, dict):
        for child in value.values():
            yield from iter_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_values(child)
    else:
        yield value


def contains_sensitive_text(text: str) -> bool:
    lowered = text.lower()
    if REDACTION_MARKER.lower() in lowered:
        lowered = lowered.replace(REDACTION_MARKER.lower(), "")
    if any(term in lowered for term in SENSITIVE_TERMS):
        return True
    return any(pattern.search(lowered) for pattern in SENSITIVE_PATTERNS)


def redact_sensitive_text(text: str) -> str:
    redacted = text
    for term in SENSITIVE_TERMS:
        redacted = re.sub(re.escape(term), REDACTION_MARKER, redacted, flags=re.IGNORECASE)
    for pattern in SENSITIVE_PATTERNS:
        redacted = pattern.sub(REDACTION_MARKER, redacted)
    return redacted


def evidence_chain_from_record(record: Dict[str, Any]) -> Dict[str, Any]:
    payload = parse_user_payload(record)
    chain = payload.get("evidence_chain") if isinstance(payload.get("evidence_chain"), dict) else {}
    return chain if isinstance(chain, dict) else {}


def diagnostic_items(record: Dict[str, Any]) -> List[Dict[str, Any]]:
    chain = evidence_chain_from_record(record)
    items: List[Dict[str, Any]] = []
    for bucket in DIAGNOSTIC_BUCKETS:
        for item in chain.get(bucket) or []:
            if isinstance(item, dict):
                tagged = dict(item)
                tagged.setdefault("_bucket", bucket)
                items.append(tagged)
    return items


def do_not_use_items(record: Dict[str, Any]) -> List[Dict[str, Any]]:
    chain = evidence_chain_from_record(record)
    return [item for item in chain.get("do_not_use_evidence") or [] if isinstance(item, dict)]


def prompt_size(record: Dict[str, Any]) -> int:
    return sum(len(safe_text(item.get("content"))) for item in record.get("messages") or [] if isinstance(item, dict))


def validate_prompt_record(record: Dict[str, Any]) -> Dict[str, Any]:
    violations: List[str] = []
    warnings: List[str] = []
    counters = Counter()

    static_validation = record.get("static_validation") if isinstance(record.get("static_validation"), dict) else {}
    static_violations = list(static_validation.get("violations") or [])
    static_warnings = list(static_validation.get("warnings") or [])
    violations.extend(str(item) for item in static_violations)
    warnings.extend(str(item) for item in static_warnings)

    if not record.get("prompt_id"):
        violations.append("missing_prompt_id")
    if not record.get("case_ref"):
        violations.append("missing_case_ref")
        counters["missing_case_ref"] += 1
    if not get_message_content(record, "system") or not get_message_content(record, "user"):
        violations.append("missing_messages")
    if not record.get("expected_output_schema"):
        violations.append("missing_output_contract")
        counters["missing_output_contract"] += 1

    payload = parse_user_payload(record)
    if not payload.get("output_contract"):
        violations.append("missing_output_contract")
        counters["missing_output_contract"] += 1

    chain = evidence_chain_from_record(record)
    do_not_use_ids = {str(item.get("evidence_id")) for item in do_not_use_items(record) if item.get("evidence_id")}
    diagnostic_ids = set()
    for item in diagnostic_items(record):
        evidence_id = item.get("evidence_id")
        if evidence_id:
            diagnostic_ids.add(str(evidence_id))
        risk_flags = [str(flag).lower() for flag in item.get("risk_flags") or []]
        source = str(item.get("source") or "").lower()
        anomaly = str(item.get("anomaly_type") or "").lower()
        causal_role = str(item.get("causal_role") or item.get("role") or "").lower()
        if (
            source == "fault_inject"
            or anomaly == "injector_marker"
            or causal_role == "excluded_provenance"
            or "injector_marker" in risk_flags
        ):
            violations.append("injector_in_diagnostic_prompt")
            counters["injector_in_diagnostic_prompt"] += 1
        for value in iter_values(item):
            if isinstance(value, str) and contains_sensitive_text(value):
                violations.append("unredacted_sensitive_terms")
                counters["unredacted_sensitive_terms"] += 1
                break

    if do_not_use_ids.intersection(diagnostic_ids):
        violations.append("do_not_use_as_support")
        counters["do_not_use_as_support"] += 1

    if record.get("readiness") == "coverage_gap":
        instructions = safe_text(payload.get("instructions") or "").lower()
        if "insufficient_evidence" not in instructions:
            warnings.append("coverage_gap_missing_insufficient_instruction")
    if not chain and record.get("readiness") != "not_ready":
        warnings.append("prompt_payload_unparsed")

    unique_violations = list(dict.fromkeys(violations))
    unique_warnings = list(dict.fromkeys(warnings))
    if unique_violations:
        counters["hard_prompt_violations"] += 1
    return {
        "passed": not unique_violations,
        "violations": unique_violations,
        "warnings": unique_warnings,
        "counters": counters,
    }


def extract_prompt_features(record: Dict[str, Any]) -> Dict[str, Any]:
    items = diagnostic_items(record)
    root_items = [item for item in items if item.get("_bucket") == "root_candidates"]
    first = root_items[0] if root_items else (items[0] if items else {})
    summary = record.get("source_chain_summary") if isinstance(record.get("source_chain_summary"), dict) else {}
    warnings = [str(item) for item in record.get("review_warnings") or []]
    size = prompt_size(record)
    if size < 5000:
        size_bucket = "small"
    elif size < 7000:
        size_bucket = "medium"
    else:
        size_bucket = "large"
    return {
        "prompt_id": str(record.get("prompt_id") or ""),
        "case_ref": str(record.get("case_ref") or ""),
        "readiness": str(record.get("readiness") or "not_ready"),
        "chain_confidence": str(record.get("chain_confidence") or "low"),
        "chain_score": numeric(record.get("chain_score"), 0.0),
        "anomaly_type": str(first.get("anomaly_type") or "unknown"),
        "object": str(first.get("object") or "unknown"),
        "source": str(first.get("source") or "unknown"),
        "review_warnings": warnings,
        "prompt_size": size,
        "prompt_size_bucket": size_bucket,
        "root_candidate_count": int(summary.get("root_candidate_count") or len(root_items)),
        "supporting_count": int(summary.get("supporting_count") or 0),
        "symptom_count": int(summary.get("symptom_count") or 0),
    }


def is_prompt_eligible(record: Dict[str, Any], args: argparse.Namespace) -> Tuple[bool, List[str]]:
    reasons: List[str] = []
    features = extract_prompt_features(record)
    validation = validate_prompt_record(record)
    if validation["violations"]:
        return False, ["hard_prompt_safety"]
    if features["readiness"] not in {"ready", "review_ready"}:
        return False, ["not_diagnosis_ready"]
    if features["root_candidate_count"] <= 0:
        return False, ["missing_root_candidate"]
    if features["prompt_size"] > args.prompt_char_budget:
        return False, ["prompt_too_long"]
    reasons.extend(["has_root_candidate", features["readiness"]])
    if features["supporting_count"] <= 0:
        reasons.append("no_supporting_evidence_review")
    if features["symptom_count"] <= 0:
        reasons.append("no_symptom_evidence_review")
    return True, reasons


def is_coverage_gap_eligible(record: Dict[str, Any], args: argparse.Namespace) -> Tuple[bool, List[str]]:
    features = extract_prompt_features(record)
    validation = validate_prompt_record(record)
    if validation["violations"]:
        return False, ["hard_prompt_safety"]
    if features["readiness"] != "coverage_gap" and features["root_candidate_count"] > 0:
        return False, ["not_coverage_gap"]
    if features["prompt_size"] > args.prompt_char_budget:
        return False, ["prompt_too_long"]
    return True, ["coverage_gap", "insufficient_evidence_behavior"]


def deterministic_key(record: Dict[str, Any]) -> Tuple[str, str]:
    return (str(record.get("prompt_id") or ""), str(record.get("case_ref") or ""))


def quota_ok(record: Dict[str, Any], anomaly_counts: Counter, object_counts: Counter, args: argparse.Namespace) -> bool:
    features = extract_prompt_features(record)
    anomaly = features["anomaly_type"]
    obj = features["object"]
    if args.max_per_anomaly_type > 0 and anomaly_counts[anomaly] >= args.max_per_anomaly_type:
        return False
    if args.max_per_object > 0 and object_counts[obj] >= args.max_per_object:
        return False
    return True


def stratified_select(records: Sequence[Dict[str, Any]], args: argparse.Namespace) -> List[Tuple[Dict[str, Any], List[str]]]:
    target = max(0, args.sample_size)
    eligible = sorted(records, key=deterministic_key)
    selected: List[Tuple[Dict[str, Any], List[str]]] = []
    selected_ids: set[int] = set()
    anomaly_counts: Counter = Counter()
    object_counts: Counter = Counter()
    seen_anomalies: set[str] = set()
    seen_objects: set[str] = set()
    seen_sources: set[str] = set()
    seen_conf: set[str] = set()
    seen_warnings: set[str] = set()
    seen_size: set[str] = set()

    def add(record: Dict[str, Any], base_reason: str, relax_quota: bool = False) -> bool:
        if len(selected) >= target or id(record) in selected_ids:
            return False
        if not relax_quota and not quota_ok(record, anomaly_counts, object_counts, args):
            return False
        features = extract_prompt_features(record)
        selected_ids.add(id(record))
        anomaly_counts[features["anomaly_type"]] += 1
        object_counts[features["object"]] += 1
        reasons = [base_reason, features["readiness"], "has_root_candidate"]
        for key, seen, label in (
            ("anomaly_type", seen_anomalies, "stratified_anomaly_type"),
            ("object", seen_objects, "stratified_object"),
            ("source", seen_sources, "stratified_source"),
            ("chain_confidence", seen_conf, "stratified_confidence"),
            ("prompt_size_bucket", seen_size, "stratified_prompt_size"),
        ):
            value = features[key]
            if value not in seen:
                seen.add(value)
                reasons.append(label)
        for warning in features["review_warnings"]:
            if warning not in seen_warnings:
                seen_warnings.add(warning)
                reasons.append(f"review_warning:{warning}")
        selected.append((record, list(dict.fromkeys(reasons))))
        return True

    for dimension in ("anomaly_type", "object", "source", "chain_confidence", "prompt_size_bucket"):
        seen_values: set[str] = set()
        for record in eligible:
            value = extract_prompt_features(record)[dimension]
            if value in seen_values:
                continue
            if add(record, f"stratified_{dimension}"):
                seen_values.add(value)
            if len(selected) >= target:
                return selected

    if args.prefer_review_diversity:
        warning_values = sorted({warning for record in eligible for warning in extract_prompt_features(record)["review_warnings"]})
        for warning in warning_values:
            for record in eligible:
                if warning in extract_prompt_features(record)["review_warnings"] and add(record, f"review_warning:{warning}"):
                    break
            if len(selected) >= target:
                return selected

    for record in eligible:
        add(record, "stratified_fill")
        if len(selected) >= target:
            return selected

    for record in eligible:
        add(record, "stratified_relaxed_fill", relax_quota=True)
        if len(selected) >= target:
            break
    return selected


def random_select(records: Sequence[Dict[str, Any]], args: argparse.Namespace) -> List[Tuple[Dict[str, Any], List[str]]]:
    shuffled = list(records)
    random.Random(args.seed).shuffle(shuffled)
    return [(record, ["deterministic_random", "has_root_candidate"]) for record in shuffled[: max(0, args.sample_size)]]


def top_score_select(records: Sequence[Dict[str, Any]], args: argparse.Namespace) -> List[Tuple[Dict[str, Any], List[str]]]:
    ordered = sorted(records, key=lambda record: (-extract_prompt_features(record)["chain_score"],) + deterministic_key(record))
    return [(record, ["top_score", "has_root_candidate"]) for record in ordered[: max(0, args.sample_size)]]


def add_experiment_metadata(
    record: Dict[str, Any],
    rank: int,
    reason: Sequence[str],
    group: str,
    expected_behavior: str,
    args: argparse.Namespace,
) -> Dict[str, Any]:
    updated = copy.deepcopy(record)
    updated["experiment_id"] = EXPERIMENT_ID
    updated["prompt_steering_version"] = record.get("prompt_steering_version") or PROMPT_STEERING_VERSION
    updated["sample_rank"] = rank
    updated["selection_reason"] = list(dict.fromkeys(str(item) for item in reason))
    updated["sample_group"] = group
    updated["expected_behavior"] = expected_behavior
    updated["selection_config"] = {
        "seed": args.seed,
        "selection": args.selection,
        "prompt_char_budget": args.prompt_char_budget,
    }
    if args.redact_sensitive:
        updated = redact_record(updated)
    return updated


def redact_record(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: redact_record(child) for key, child in value.items()}
    if isinstance(value, list):
        return [redact_record(child) for child in value]
    if isinstance(value, str) and contains_sensitive_text(value):
        return redact_sensitive_text(value)
    return value


def select_experiment_records(prompt_pool: Sequence[Dict[str, Any]], args: argparse.Namespace) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    eligible: List[Tuple[Dict[str, Any], List[str]]] = []
    ineligible = Counter()
    for record in prompt_pool:
        ok, reasons = is_prompt_eligible(record, args)
        if ok:
            eligible.append((record, reasons))
        else:
            ineligible.update(reasons)

    eligible_records = [record for record, _ in eligible]
    if args.selection == "random":
        diagnosis_pairs = random_select(eligible_records, args)
    elif args.selection == "top_score":
        diagnosis_pairs = top_score_select(eligible_records, args)
    else:
        diagnosis_pairs = stratified_select(eligible_records, args)
    reason_by_id = {id(record): reasons for record, reasons in eligible}

    selected: List[Dict[str, Any]] = []
    rank = 1
    for record, reasons in diagnosis_pairs:
        merged = list(reason_by_id.get(id(record), [])) + list(reasons)
        selected.append(add_experiment_metadata(record, rank, merged, "diagnosis_ready", "diagnose_from_evidence", args))
        rank += 1

    coverage_selected = 0
    if args.include_no_root and args.no_root_size > 0:
        coverage_candidates: List[Tuple[Dict[str, Any], List[str]]] = []
        for record in sorted(prompt_pool, key=deterministic_key):
            ok, reasons = is_coverage_gap_eligible(record, args)
            if ok:
                coverage_candidates.append((record, reasons))
        for record, reasons in coverage_candidates[: max(0, args.no_root_size)]:
            selected.append(
                add_experiment_metadata(
                    record,
                    rank,
                    reasons,
                    "coverage_gap",
                    "return_insufficient_evidence",
                    args,
                )
            )
            rank += 1
            coverage_selected += 1

    diagnostics_selected = sum(1 for record in selected if record.get("sample_group") == "diagnosis_ready")
    return selected, {
        "eligible_diagnosis": len(eligible_records),
        "ineligible": dict(ineligible),
        "diagnosis_selected": diagnostics_selected,
        "coverage_selected": coverage_selected,
    }


def build_response_template(records: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    template: List[Dict[str, Any]] = []
    for record in records:
        template.append(
            {
                "prompt_id": record.get("prompt_id"),
                "case_ref": record.get("case_ref"),
                "response": {
                    "root_cause": "",
                    "root_object": "",
                    "evidence_used": [],
                    "evidence_roles": {},
                    "alternatives": [],
                    "uncertainty": "",
                },
            }
        )
    return template


def validate_responses(selected_records: Sequence[Dict[str, Any]], responses_path: Path, args: argparse.Namespace) -> Dict[str, Any]:
    root = Path(args.root).resolve()
    validator = safe_import_module(root / "tools" / "evidence_response_validator.py", "evidence_response_validator_for_experiment")
    validator_args = validator.parse_args(
        [
            "--root",
            str(root),
            "--dry-run",
            "--stdout-only",
            "--format",
            "json",
            "--max-examples",
            str(args.max_examples),
        ]
        + (["--strict"] if args.strict else [])
    )
    responses, parse_errors = validator.load_responses_from_jsonl(responses_path, args.max_examples)
    results, aux = validator.build_validation_results(list(selected_records), responses, validator_args)
    summary = validator.build_summary(results, list(selected_records), responses, parse_errors, aux, validator_args)
    return {
        "enabled": True,
        "summary": summary,
        "results": results,
        "matched_pairs": summary.get("counts", {}).get("matched_pairs", 0),
        "pass": summary.get("counts", {}).get("pass", 0),
        "warn": summary.get("counts", {}).get("warn", 0),
        "fail": summary.get("counts", {}).get("fail", 0),
        "unknown_evidence_id": summary.get("evidence_reference", {}).get("unknown_evidence_id", 0),
        "do_not_use_evidence_cited": summary.get("evidence_reference", {}).get("do_not_use_evidence_cited", 0),
        "sensitive_text_violations": summary.get("safety", {}).get("sensitive_text_violations", 0),
        "coverage_gap_false_root": summary.get("safety", {}).get("coverage_gap_false_root", 0),
    }


def safe_output_path(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    if resolved.exists():
        raise ValueError(f"refusing to overwrite existing output path: {resolved}")
    if not resolved.parent.exists():
        raise ValueError(f"output parent does not exist: {resolved.parent}")
    lowered_name = resolved.name.lower()
    if lowered_name in PROTECTED_OUTPUT_FILENAMES or "ledger" in lowered_name:
        raise ValueError(f"refusing protected output filename: {resolved}")
    parts = [part.lower() for part in resolved.parts]
    for part in parts[:-1]:
        if part in PROTECTED_OUTPUT_DIRS or "frozen" in part or "accepted" in part or "ledger" in part:
            raise ValueError(f"refusing output under protected path: {resolved}")
    try:
        rel_parts = [part.lower() for part in resolved.relative_to(root.resolve()).parts]
    except ValueError:
        rel_parts = []
    for part in rel_parts[:-1]:
        if part in PROTECTED_OUTPUT_DIRS or "frozen" in part or "accepted" in part or "ledger" in part:
            raise ValueError(f"refusing output under protected repository path: {resolved}")
    return resolved


def compute_composition(records: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    by_anomaly: Counter = Counter()
    by_object: Counter = Counter()
    by_source: Counter = Counter()
    by_warning: Counter = Counter()
    for record in records:
        if record.get("sample_group") == "coverage_gap":
            continue
        features = extract_prompt_features(record)
        by_anomaly[features["anomaly_type"]] += 1
        by_object[features["object"]] += 1
        by_source[features["source"]] += 1
        by_warning.update(features["review_warnings"])
    return {
        "by_anomaly_type": dict(sorted(by_anomaly.items())),
        "by_object": dict(sorted(by_object.items())),
        "by_source": dict(sorted(by_source.items())),
        "by_review_warning": dict(sorted(by_warning.items())),
    }


def prompt_pool_counts(records: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    readiness = Counter(str(record.get("readiness") or "not_ready") for record in records)
    return {
        "prompt_records": len(records),
        "diagnosis_ready_prompts": sum(
            1
            for record in records
            if str(record.get("readiness") or "") in {"ready", "review_ready"}
            and extract_prompt_features(record)["root_candidate_count"] > 0
        ),
        "review_ready_prompts": readiness.get("review_ready", 0),
        "coverage_gap_prompts": readiness.get("coverage_gap", 0),
        "not_ready_prompts": readiness.get("not_ready", 0),
    }


def selected_counts(records: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    confidence = Counter(str(record.get("chain_confidence") or "low") for record in records)
    return {
        "diagnosis_ready": sum(1 for record in records if record.get("sample_group") == "diagnosis_ready"),
        "review_ready": sum(1 for record in records if record.get("readiness") == "review_ready"),
        "coverage_gap": sum(1 for record in records if record.get("sample_group") == "coverage_gap"),
        "high_confidence": confidence.get("high", 0),
        "medium_confidence": confidence.get("medium", 0),
        "low_confidence": confidence.get("low", 0),
    }


def aggregate_prompt_safety(records: Sequence[Dict[str, Any]]) -> Tuple[Dict[str, int], List[Dict[str, Any]], Counter]:
    totals = Counter()
    failures: List[Dict[str, Any]] = []
    warnings = Counter()
    for record in records:
        validation = validate_prompt_record(record)
        totals.update(validation["counters"])
        warnings.update(validation["warnings"])
        if validation["violations"]:
            failures.append(
                {
                    "prompt_id": record.get("prompt_id"),
                    "case_ref": record.get("case_ref"),
                    "violations": validation["violations"],
                }
            )
    return {
        "hard_prompt_violations": totals.get("hard_prompt_violations", 0),
        "unredacted_sensitive_terms": totals.get("unredacted_sensitive_terms", 0),
        "injector_in_diagnostic_prompt": totals.get("injector_in_diagnostic_prompt", 0),
        "do_not_use_as_support": totals.get("do_not_use_as_support", 0),
        "missing_output_contract": totals.get("missing_output_contract", 0),
        "missing_case_ref": totals.get("missing_case_ref", 0),
    }, failures, warnings


def summarize_record(record: Dict[str, Any]) -> Dict[str, Any]:
    features = extract_prompt_features(record)
    return {
        "prompt_id": record.get("prompt_id"),
        "case_ref": record.get("case_ref"),
        "sample_rank": record.get("sample_rank"),
        "sample_group": record.get("sample_group"),
        "readiness": record.get("readiness"),
        "chain_confidence": record.get("chain_confidence"),
        "chain_score": record.get("chain_score"),
        "anomaly_type": features["anomaly_type"],
        "object": features["object"],
        "source": features["source"],
        "review_warnings": record.get("review_warnings") or [],
        "selection_reason": record.get("selection_reason") or [],
    }


def build_summary(
    records: Sequence[Dict[str, Any]],
    prompt_pool: Sequence[Dict[str, Any]],
    response_validation: Dict[str, Any],
    args: argparse.Namespace,
    selection_meta: Dict[str, Any],
    parse_errors: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    safety, failures, prompt_warnings = aggregate_prompt_safety(records)
    source_counts = prompt_pool_counts(prompt_pool)
    sample_counts = selected_counts(records)
    response_enabled = bool(response_validation.get("enabled"))
    response_fail = int(response_validation.get("fail", 0))
    status = "PASS"
    if any(safety.values()) or failures or response_fail:
        status = "FAIL"
    elif (
        sample_counts["diagnosis_ready"] < max(0, args.sample_size)
        or parse_errors
        or prompt_warnings
        or response_validation.get("warn", 0)
    ):
        status = "WARN"
    if args.strict and status == "WARN":
        status = "FAIL"

    warnings = Counter(prompt_warnings)
    if sample_counts["diagnosis_ready"] < max(0, args.sample_size):
        warnings["sample_size_not_satisfied"] += 1
    if parse_errors:
        warnings["prompt_parse_errors"] += len(parse_errors)
    if response_validation.get("warn", 0):
        warnings["response_validation_warn"] += int(response_validation.get("warn", 0))
    if args.responses_jsonl and not args.validate_responses:
        warnings["responses_provided_not_validated"] += 1
        if status == "PASS":
            status = "WARN"

    examples = {
        "selected_prompts": [summarize_record(record) for record in records if record.get("sample_group") != "coverage_gap"][
            : args.max_examples
        ],
        "coverage_gap_prompts": [
            summarize_record(record) for record in records if record.get("sample_group") == "coverage_gap"
        ][: args.max_examples],
        "warnings": [
            {"warning": key, "count": value}
            for key, value in warnings.most_common(args.max_examples)
        ],
        "failures": failures[: args.max_examples],
    }

    return {
        "root": str(Path(args.root).resolve()),
        "dry_run": bool(args.dry_run),
        "status": status,
        "mode": "validate_responses" if response_enabled else "pack",
        "experiment": {
            "experiment_id": EXPERIMENT_ID,
            "seed": args.seed,
            "selection": args.selection,
            "sample_size_requested": max(0, args.sample_size),
            "sample_size_selected": len(records),
            "include_no_root": bool(args.include_no_root),
            "no_root_selected": sample_counts["coverage_gap"],
        },
        "source_prompt_pool": source_counts,
        "selected_sample": sample_counts,
        "safety": safety,
        "composition": compute_composition(records),
        "selection_meta": selection_meta,
        "response_validation": {
            "enabled": response_enabled,
            "matched_pairs": int(response_validation.get("matched_pairs", 0)),
            "pass": int(response_validation.get("pass", 0)),
            "warn": int(response_validation.get("warn", 0)),
            "fail": int(response_validation.get("fail", 0)),
            "unknown_evidence_id": int(response_validation.get("unknown_evidence_id", 0)),
            "do_not_use_evidence_cited": int(response_validation.get("do_not_use_evidence_cited", 0)),
            "sensitive_text_violations": int(response_validation.get("sensitive_text_violations", 0)),
            "coverage_gap_false_root": int(response_validation.get("coverage_gap_false_root", 0)),
        },
        "warnings": dict(warnings.most_common()),
        "examples": examples,
    }


def print_json_summary(summary: Dict[str, Any]) -> str:
    return safe_json_dump(summary, pretty=True)


def print_markdown_summary(summary: Dict[str, Any]) -> str:
    experiment = summary.get("experiment") or {}
    pool = summary.get("source_prompt_pool") or {}
    selected = summary.get("selected_sample") or {}
    safety = summary.get("safety") or {}
    response = summary.get("response_validation") or {}
    lines = [
        "# Small-scale Evidence Diagnosis Experiment Pack",
        "",
        "## 1. Status",
        "",
        f"- Status: `{summary.get('status')}`",
        f"- Mode: `{summary.get('mode')}`",
        f"- Dry run: `{summary.get('dry_run')}`",
        "",
        "## 2. Experiment Configuration",
        "",
        f"- Experiment ID: `{experiment.get('experiment_id')}`",
        f"- Seed: `{experiment.get('seed')}`",
        f"- Selection: `{experiment.get('selection')}`",
        f"- Requested diagnosis sample size: `{experiment.get('sample_size_requested')}`",
        f"- Selected total records: `{experiment.get('sample_size_selected')}`",
        f"- Include no-root: `{experiment.get('include_no_root')}`",
        f"- No-root selected: `{experiment.get('no_root_selected')}`",
        "",
        "## 3. Prompt Pool Summary",
        "",
        f"- Prompt records: `{pool.get('prompt_records', 0)}`",
        f"- Diagnosis-ready prompts: `{pool.get('diagnosis_ready_prompts', 0)}`",
        f"- Review-ready prompts: `{pool.get('review_ready_prompts', 0)}`",
        f"- Coverage-gap prompts: `{pool.get('coverage_gap_prompts', 0)}`",
        f"- Not-ready prompts: `{pool.get('not_ready_prompts', 0)}`",
        "",
        "## 4. Selected Sample Summary",
        "",
        f"- Diagnosis-ready selected: `{selected.get('diagnosis_ready', 0)}`",
        f"- Review-ready selected: `{selected.get('review_ready', 0)}`",
        f"- Coverage-gap selected: `{selected.get('coverage_gap', 0)}`",
        f"- High / medium / low confidence: `{selected.get('high_confidence', 0)} / {selected.get('medium_confidence', 0)} / {selected.get('low_confidence', 0)}`",
        "",
        "## 5. Safety Checks",
        "",
    ]
    for key in (
        "hard_prompt_violations",
        "unredacted_sensitive_terms",
        "injector_in_diagnostic_prompt",
        "do_not_use_as_support",
        "missing_output_contract",
        "missing_case_ref",
    ):
        lines.append(f"- {key}: `{safety.get(key, 0)}`")
    lines.extend(["", "## 6. Composition", ""])
    for name, values in (summary.get("composition") or {}).items():
        rendered = ", ".join(f"{key}={value}" for key, value in sorted((values or {}).items()))
        lines.append(f"- {name}: {rendered or '(none)'}")
    lines.extend(["", "## 7. Response Validation"])
    if response.get("enabled"):
        lines.extend(
            [
                "",
                f"- Matched pairs: `{response.get('matched_pairs', 0)}`",
                f"- Pass / warn / fail: `{response.get('pass', 0)} / {response.get('warn', 0)} / {response.get('fail', 0)}`",
                f"- Unknown evidence IDs: `{response.get('unknown_evidence_id', 0)}`",
                f"- Do-not-use cited: `{response.get('do_not_use_evidence_cited', 0)}`",
                f"- Sensitive text violations: `{response.get('sensitive_text_violations', 0)}`",
                f"- Coverage-gap false roots: `{response.get('coverage_gap_false_root', 0)}`",
            ]
        )
    else:
        lines.extend(["", "- Response validation was not enabled."])
    lines.extend(
        [
            "",
            "## 8. How to Run the Manual LLM Step",
            "",
            "1. Export prompt JSONL with explicit `--output` to a safe non-dataset path.",
            "2. Run the model outside this tool.",
            "3. Save responses as JSONL with `prompt_id`, `case_ref`, and `response`.",
            "4. Validate with:",
            "",
            "```powershell",
            "$env:PYTHONDONTWRITEBYTECODE=1",
            "python -B tools/evidence_response_validator.py --prompts-jsonl <PROMPTS_JSONL> --responses-jsonl <RESPONSES_JSONL> --dry-run --stdout-only --format both --max-examples 10",
            "```",
            "",
            "## 9. How to Validate Responses",
            "",
            "Use `--responses-jsonl <RESPONSES_JSONL> --validate-responses` with this experiment tool for a selected-sample view, or use `evidence_response_validator.py` directly for the full validator report.",
            "",
            "## 10. Notes",
            "",
            "- This tool is read-only by default and does not modify existing evidence files.",
            "- This tool does not call any LLM and does not generate diagnosis answers.",
            "- Selected prompts are for controlled small-scale experiments only.",
            "- All model responses must be validated with evidence_response_validator.py before use.",
            "- No-root prompts, if included, are expected to return insufficient_evidence.",
        ]
    )
    return "\n".join(lines)


def print_jsonl_records(records: Sequence[Dict[str, Any]]) -> str:
    return "\n".join(safe_json_dump(record) for record in records)


def write_jsonl(path: Path, records: Sequence[Dict[str, Any]], root: Path) -> None:
    resolved = safe_output_path(path, root)
    resolved.write_text(print_jsonl_records(records) + ("\n" if records else ""), encoding="utf-8")


def write_json(path: Path, payload: Dict[str, Any], root: Path) -> None:
    resolved = safe_output_path(path, root)
    resolved.write_text(print_json_summary(payload) + "\n", encoding="utf-8")


def load_prompt_pool(args: argparse.Namespace) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    root = Path(args.root).resolve()
    if args.prompts_jsonl:
        path = Path(args.prompts_jsonl)
        if not path.is_absolute():
            path = root / path
        records, parse_errors = load_prompts_from_jsonl(path)
        return records, parse_errors, {"mode": "prompts_jsonl", "source": str(path)}
    records, source = build_prompts_from_root(root, args.input, args)
    return records, source.get("parse_errors", []), source


def run(args: argparse.Namespace) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    root = Path(args.root).resolve()
    prompt_pool, parse_errors, source = load_prompt_pool(args)
    selected_records, selection_meta = select_experiment_records(prompt_pool, args)
    selection_meta["source"] = source

    response_validation: Dict[str, Any] = {"enabled": False}
    if args.responses_jsonl and args.validate_responses:
        response_path = Path(args.responses_jsonl)
        if not response_path.is_absolute():
            response_path = root / response_path
        response_validation = validate_responses(selected_records, response_path, args)

    summary = build_summary(selected_records, prompt_pool, response_validation, args, selection_meta, parse_errors)
    return summary, selected_records


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    root = Path(args.root).resolve()
    try:
        summary, records = run(args)
        output = ""
        if args.format == "json":
            output = print_json_summary(summary)
        elif args.format == "markdown":
            output = print_markdown_summary(summary)
        elif args.format == "jsonl":
            output = print_jsonl_records(records)
        else:
            output = print_json_summary(summary) + "\n\n" + print_markdown_summary(summary)
        if output:
            print(output)

        if args.output and not args.stdout_only:
            output_path = Path(args.output)
            if args.format == "json" and output_path.suffix.lower() == ".json":
                write_json(output_path, summary, root)
            else:
                write_jsonl(output_path, records, root)
        if args.response_template_output and not args.stdout_only:
            write_jsonl(Path(args.response_template_output), build_response_template(records), root)
    except Exception as exc:  # pragma: no cover - CLI safety net
        error_summary = {
            "root": str(root),
            "dry_run": bool(args.dry_run),
            "status": "FAIL",
            "error": str(exc),
        }
        print(safe_json_dump(error_summary, pretty=(args.format != "jsonl")))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
