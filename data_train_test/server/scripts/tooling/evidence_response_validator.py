#!/usr/bin/env python3
"""Read-only validator for RCA responses generated from evidence prompts.

The validator consumes prompt preview records and optional LLM response JSONL
records, then checks response schema, evidence references, coverage-gap
behavior, and safety invariants. It does not call any LLM and writes nothing by
default.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple


sys.dont_write_bytecode = True

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

BUCKET_TO_ROLE = {
    "root_candidates": "root_candidate",
    "supporting_evidence": "supporting",
    "fault_observations": "fault_observation",
    "propagation_evidence": "propagation",
    "symptom_evidence": "symptom",
    "contradicting_evidence": "contradicting",
    "uncertain_evidence": "uncertain",
}

REQUIRED_RESPONSE_FIELDS = (
    "root_cause",
    "root_object",
    "evidence_used",
    "evidence_roles",
    "alternatives",
    "uncertainty",
)

ALLOWED_ROLES = {
    "root_candidate",
    "supporting",
    "fault_observation",
    "propagation",
    "symptom",
    "contradicting",
    "uncertain",
}

FORBIDDEN_ROLES = {
    "do_not_use",
    "excluded_provenance",
    "injector_marker",
    "label",
    "ground_truth",
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

EXTERNAL_CONTEXT_PATTERNS = (
    re.compile(r"\blogs?\s+(?:are\s+|were\s+|is\s+|was\s+)?not\s+shown\b", re.IGNORECASE),
    re.compile(r"\bfrom\s+(?:the\s+)?logs?\b", re.IGNORECASE),
    re.compile(r"\bfrom\s+system\s+logs?\b", re.IGNORECASE),
    re.compile(r"\bbased\s+on\s+unseen\s+logs?\b", re.IGNORECASE),
    re.compile(r"\bI\s+assume\s+from\s+logs?\b", re.IGNORECASE),
    re.compile(r"\bexternal\s+knowledge\b", re.IGNORECASE),
    re.compile(r"\bexternal\s+context\b", re.IGNORECASE),
    re.compile(r"\badditional\s+context\b", re.IGNORECASE),
    re.compile(r"\boutside\s+context\b", re.IGNORECASE),
    re.compile(r"\bnot\s+present\s+in\s+the\s+prompt\b", re.IGNORECASE),
    re.compile(r"\bnot\s+provided\s+in\s+the\s+evidence\b", re.IGNORECASE),
    re.compile(r"\bnot\s+provided\b", re.IGNORECASE),
    re.compile(r"\baccording\s+to\s+(?:the\s+)?dataset(?:\s+label)?\b", re.IGNORECASE),
    re.compile(r"\bdataset\s+label\s+(?:indicates?|shows?|says|states?)\b", re.IGNORECASE),
    re.compile(r"\b(?:from|using|based\s+on)\s+(?:the\s+)?dataset\s+label\b", re.IGNORECASE),
    re.compile(r"\bfrom\s+the\s+original\s+case\b", re.IGNORECASE),
    re.compile(r"\b(?:the|this|that)\s+file\s+(?:shows?|indicates?|says|states?)\b", re.IGNORECASE),
    re.compile(r"\bfile\s*[:=]\s*[^,\s]+", re.IGNORECASE),
    re.compile(r"\bline\s+\d+\b", re.IGNORECASE),
    re.compile(r"[A-Za-z]:[\\/][^\s]+", re.IGNORECASE),
)

ABSOLUTE_PATH_PATTERN = re.compile(r"(?<!\S)/[A-Za-z0-9_.:-]+(?:/[A-Za-z0-9_.:-]+)+")
RELATIVE_FILE_PATH_PATTERN = re.compile(
    r"\b[A-Za-z0-9_.:-]+(?:/[A-Za-z0-9_.:-]+)+\."
    r"(?:jsonl|json|md|txt|log|yaml|yml|py|csv|tsv|tar|gz|zip)\b",
    re.IGNORECASE,
)
STRUCTURED_SLASH_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]+(?:/[A-Za-z0-9_.:-]+)+$")
SAFE_TECHNICAL_SLASH_TERMS = {
    "wifi",
    "wlan0",
    "wpa_state",
    "route",
    "default",
    "dns",
    "resolve",
    "ping",
    "gateway",
    "reachable",
    "unreachable",
    "missing",
    "failure",
    "disconnect",
    "disconnected",
    "completed",
    "link",
    "down",
    "state",
    "network",
    "process",
    "service",
    "root_candidate",
    "supporting",
    "reachability_loss",
    "dns_failure",
    "link_down",
    "service_stopped",
}

GENERIC_ROOT_CAUSES = {
    "network issue",
    "network problem",
    "connectivity issue",
    "connectivity problem",
    "unknown network issue",
}

REDACTION_MARKER = "[REDACTED_SENSITIVE_CONTEXT]"

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
    "dataset",
    "dataset_batches",
    "datasets",
    "demo_public_dataset",
    "frozen",
    "accepted",
    "ledger",
    "eval",
    "train",
    "val",
    "test",
    "training_views",
    "inbox",
    "inbox_net",
}


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate evidence RCA responses.")
    parser.add_argument("--root", default=".", help="Repository root. Default: current directory.")
    parser.add_argument("--input", default=None, help="Optional evidence_candidates.jsonl file or directory.")
    parser.add_argument("--prompts-jsonl", default=None, help="Optional prompt preview JSONL input.")
    parser.add_argument("--responses-jsonl", default=None, help="Optional response JSONL input.")
    parser.add_argument(
        "--response-only",
        action="store_true",
        help="Validate responses without prompt context; evidence references are reported as unchecked.",
    )
    parser.add_argument("--stdout-only", action="store_true", help="Force stdout output only.")
    parser.add_argument("--dry-run", action="store_true", help="Explicit safety flag; this validator is read-only.")
    parser.add_argument("--format", choices=("json", "markdown", "both", "jsonl"), default="both")
    parser.add_argument("--max-examples", type=int, default=10)
    parser.add_argument("--limit-records", type=int, default=None)
    parser.add_argument("--include-no-root", action="store_true")
    parser.add_argument("--strict", action="store_true", help="Treat warnings as failures.")
    parser.add_argument(
        "--allow-insufficient-evidence",
        dest="allow_insufficient_evidence",
        action="store_true",
        default=True,
    )
    parser.add_argument(
        "--no-allow-insufficient-evidence",
        dest="allow_insufficient_evidence",
        action="store_false",
    )
    parser.add_argument("--redact-sensitive", dest="redact_sensitive", action="store_true", default=True)
    parser.add_argument("--no-redact-sensitive", dest="redact_sensitive", action="store_false")
    parser.add_argument("--output", default=None, help="Optional explicit output path.")
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


def read_text_lossy(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def safe_import_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to import module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def iter_values(value: Any) -> Iterator[Any]:
    if isinstance(value, dict):
        for child in value.values():
            yield from iter_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_values(child)
    else:
        yield value


def looks_sensitive_text(value: Any) -> bool:
    text = safe_text(value)
    if not text:
        return False
    text = text.replace(REDACTION_MARKER, "")
    if not text.strip():
        return False
    lower = text.lower()
    if any(term in lower for term in SENSITIVE_TERMS):
        return True
    return any(pattern.search(text) for pattern in SENSITIVE_PATTERNS)


def redact_sensitive_text(text: Any) -> str:
    raw = safe_text(text)
    if not raw:
        return ""
    redacted = raw
    for term in SENSITIVE_TERMS:
        redacted = re.sub(re.escape(term), REDACTION_MARKER, redacted, flags=re.IGNORECASE)
    for pattern in SENSITIVE_PATTERNS:
        redacted = pattern.sub(REDACTION_MARKER, redacted)
    return redacted


def strip_markdown_json_fence(text: str) -> str:
    stripped = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", stripped, re.IGNORECASE | re.DOTALL)
    if fence:
        return fence.group(1).strip()
    if "```" in stripped:
        inner = re.search(r"```(?:json)?\s*(.*?)\s*```", stripped, re.IGNORECASE | re.DOTALL)
        if inner:
            return inner.group(1).strip()
    return stripped


def load_jsonl_objects(path: Path, max_examples: int) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    records: List[Dict[str, Any]] = []
    parse_errors: List[Dict[str, Any]] = []
    text = read_text_lossy(path)
    for line_no, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            if len(parse_errors) < max_examples:
                parse_errors.append(
                    {
                        "file": str(path),
                        "line": line_no,
                        "error": str(exc),
                        "text": redact_sensitive_text(compact_text(line)),
                    }
                )
            continue
        if isinstance(value, dict):
            value.setdefault("_source_file", str(path))
            value.setdefault("_source_line", line_no)
            records.append(value)
        elif len(parse_errors) < max_examples:
            parse_errors.append(
                {
                    "file": str(path),
                    "line": line_no,
                    "error": "JSONL row is not an object",
                    "text": redact_sensitive_text(compact_text(value)),
                }
            )
    return records, parse_errors


def load_prompts_from_jsonl(path: Path, max_examples: int = 10) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    return load_jsonl_objects(path, max_examples)


def load_responses_from_jsonl(path: Path, max_examples: int = 10) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    return load_jsonl_objects(path, max_examples)


def build_prompts_from_root(root: Path, input_path: Optional[str], args: argparse.Namespace) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    preview = safe_import_module(root / "tools" / "evidence_prompt_integration_preview.py", "evidence_prompt_preview_for_response_validator")
    preview_argv = [
        "--root",
        str(root),
        "--dry-run",
        "--stdout-only",
        "--format",
        "json",
        "--max-examples",
        str(args.max_examples),
    ]
    if input_path:
        preview_argv.extend(["--input", input_path])
    if args.include_no_root:
        preview_argv.append("--include-no-root")
    preview_args = preview.parse_args(preview_argv)
    summary, records = preview.run(preview_args)
    return list(records), dict(summary)


def load_or_build_prompts(args: argparse.Namespace) -> Tuple[List[Dict[str, Any]], Dict[str, Any], List[Dict[str, Any]]]:
    root = Path(args.root).resolve()
    if args.prompts_jsonl:
        path = Path(args.prompts_jsonl)
        if not path.is_absolute():
            path = root / path
        records, parse_errors = load_prompts_from_jsonl(path, args.max_examples)
        summary = {
            "mode": "prompts_jsonl",
            "prompt_records": len(records),
            "parse_errors": len(parse_errors),
            "source": str(path),
        }
        return records, summary, parse_errors
    if args.responses_jsonl and not args.root and not args.input:
        return [], {"mode": "response_only"}, []
    records, summary = build_prompts_from_root(root, args.input, args)
    return records, summary, []


def prompt_user_payload(prompt_record: Dict[str, Any]) -> Dict[str, Any]:
    for message in prompt_record.get("messages") or []:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = safe_text(message.get("content"))
        candidate = strip_markdown_json_fence(content)
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            embedded = re.search(r"```json\s*(.*?)\s*```", content, re.IGNORECASE | re.DOTALL)
            if not embedded:
                continue
            try:
                parsed = json.loads(embedded.group(1))
            except json.JSONDecodeError:
                continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def extract_prompt_context(prompt_record: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not prompt_record:
        return {
            "context_missing": True,
            "allowed_ids": set(),
            "do_not_use_ids": set(),
            "role_map": {},
            "root_candidate_ids": set(),
            "root_candidate_objects": set(),
            "root_candidate_anomalies": set(),
            "readiness": "context_missing",
            "case_ref": None,
            "do_not_use_text": [],
            "prompt_warnings": [],
            "prompt_blockers": [],
        }

    payload = prompt_user_payload(prompt_record)
    evidence_chain = payload.get("evidence_chain") if isinstance(payload.get("evidence_chain"), dict) else {}
    allowed_ids: set[str] = set()
    do_not_use_ids: set[str] = set()
    role_map: Dict[str, str] = {}
    root_candidate_ids: set[str] = set()
    root_candidate_objects: set[str] = set()
    root_candidate_anomalies: set[str] = set()
    do_not_use_text: List[str] = []

    for bucket in DIAGNOSTIC_BUCKETS:
        expected_role = BUCKET_TO_ROLE[bucket]
        for item in evidence_chain.get(bucket) or []:
            if not isinstance(item, dict) or not item.get("evidence_id"):
                continue
            eid = str(item.get("evidence_id"))
            allowed_ids.add(eid)
            role_map.setdefault(eid, str(item.get("role") or expected_role))
            if bucket == "root_candidates":
                root_candidate_ids.add(eid)
                if item.get("object"):
                    root_candidate_objects.add(str(item.get("object")).lower())
                if item.get("anomaly_type"):
                    root_candidate_anomalies.add(str(item.get("anomaly_type")).lower())

    for item in evidence_chain.get("do_not_use_evidence") or []:
        if not isinstance(item, dict):
            continue
        if item.get("evidence_id"):
            do_not_use_ids.add(str(item.get("evidence_id")))
        do_not_use_text.append(compact_text(item))
        for key in ("reason", "summary", "text", "raw_observation", "exclusion_reason", "notes"):
            if item.get(key):
                do_not_use_text.append(compact_text(item.get(key), 180))
        risk_flags = item.get("risk_flags")
        if isinstance(risk_flags, list):
            do_not_use_text.extend(compact_text(flag, 80) for flag in risk_flags if flag)

    return {
        "context_missing": False,
        "allowed_ids": allowed_ids,
        "do_not_use_ids": do_not_use_ids,
        "role_map": role_map,
        "root_candidate_ids": root_candidate_ids,
        "root_candidate_objects": root_candidate_objects,
        "root_candidate_anomalies": root_candidate_anomalies,
        "readiness": prompt_record.get("readiness") or "unknown",
        "case_ref": prompt_record.get("case_ref") or payload.get("case_ref"),
        "do_not_use_text": do_not_use_text,
        "prompt_warnings": list(prompt_record.get("review_warnings") or []),
        "prompt_blockers": list(prompt_record.get("blockers") or []),
        "static_validation": prompt_record.get("static_validation") or {},
        "expected_output_schema": prompt_record.get("expected_output_schema") or {},
    }


def extract_allowed_evidence_ids(prompt_context: Dict[str, Any]) -> set[str]:
    return set(prompt_context.get("allowed_ids") or set())


def extract_do_not_use_ids(prompt_context: Dict[str, Any]) -> set[str]:
    return set(prompt_context.get("do_not_use_ids") or set())


def response_ids_from_alternatives(response: Dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for alt in response.get("alternatives") or []:
        if isinstance(alt, dict):
            evidence_used = alt.get("evidence_used")
            if isinstance(evidence_used, list):
                for eid in evidence_used:
                    ids.add(str(eid))
    return ids


def strip_loader_metadata(record: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in record.items() if not str(key).startswith("_source_")}


def parse_response_record(record: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if isinstance(record.get("response"), dict):
        return record["response"], None
    if "response_text" in record:
        text = strip_markdown_json_fence(safe_text(record.get("response_text")))
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            return None, f"response_text is not valid JSON: {exc}"
        if not isinstance(parsed, dict):
            return None, "response_text JSON is not an object"
        return parsed, None
    if any(field in record for field in REQUIRED_RESPONSE_FIELDS):
        return strip_loader_metadata(record), None
    return None, "response object not found"


def validate_response_schema(response: Optional[Dict[str, Any]]) -> Tuple[bool, List[str], Counter, Counter]:
    violations: List[str] = []
    missing = Counter()
    wrong_types = Counter()
    if response is None:
        violations.append("response_parse_error")
        return False, violations, missing, wrong_types
    if not isinstance(response, dict):
        violations.append("response_not_object")
        return False, violations, missing, wrong_types
    for field in REQUIRED_RESPONSE_FIELDS:
        if field not in response:
            missing[field] += 1
            violations.append(f"missing_required_field:{field}")
    type_checks = {
        "root_cause": str,
        "root_object": str,
        "evidence_used": list,
        "evidence_roles": dict,
        "alternatives": list,
        "uncertainty": str,
    }
    for field, expected_type in type_checks.items():
        if field in response and not isinstance(response.get(field), expected_type):
            wrong_types[field] += 1
            violations.append(f"wrong_type_field:{field}")
    if isinstance(response.get("evidence_used"), list):
        if not all(isinstance(item, str) for item in response.get("evidence_used") or []):
            wrong_types["evidence_used_item"] += 1
            violations.append("wrong_type_field:evidence_used_item")
    if isinstance(response.get("evidence_roles"), dict):
        roles = response.get("evidence_roles", {})
        if not all(isinstance(key, str) and isinstance(value, str) for key, value in roles.items()):
            wrong_types["evidence_roles_item"] += 1
            violations.append("wrong_type_field:evidence_roles_item")
        for role in roles.values():
            if isinstance(role, str) and role in FORBIDDEN_ROLES:
                wrong_types["evidence_roles_forbidden_role"] += 1
                violations.append("forbidden_evidence_role")
            elif isinstance(role, str) and role not in ALLOWED_ROLES:
                wrong_types["evidence_roles_unknown_role"] += 1
                violations.append("unknown_evidence_role")
    if isinstance(response.get("alternatives"), list):
        for alt in response.get("alternatives") or []:
            if not isinstance(alt, dict):
                wrong_types["alternatives_item"] += 1
                violations.append("wrong_type_field:alternatives_item")
                continue
            for field in ("root_cause", "root_object", "reason"):
                if field in alt and not isinstance(alt.get(field), str):
                    wrong_types[f"alternatives.{field}"] += 1
                    violations.append(f"wrong_type_field:alternatives.{field}")
            if "evidence_used" in alt:
                if not isinstance(alt.get("evidence_used"), list):
                    wrong_types["alternatives.evidence_used"] += 1
                    violations.append("wrong_type_field:alternatives.evidence_used")
                elif not all(isinstance(item, str) for item in alt.get("evidence_used") or []):
                    wrong_types["alternatives.evidence_used_item"] += 1
                    violations.append("wrong_type_field:alternatives.evidence_used_item")
    return not violations, list(dict.fromkeys(violations)), missing, wrong_types


def validate_evidence_references(response: Dict[str, Any], prompt_context: Dict[str, Any]) -> Tuple[List[str], List[str], Counter]:
    violations: List[str] = []
    warnings: List[str] = []
    counters = Counter()
    if prompt_context.get("context_missing"):
        warnings.append("context_missing")
        return violations, warnings, counters

    allowed_ids = extract_allowed_evidence_ids(prompt_context)
    do_not_use_ids = extract_do_not_use_ids(prompt_context)
    known_ids = allowed_ids | do_not_use_ids
    evidence_used_value = response.get("evidence_used")
    used_ids = {str(item) for item in evidence_used_value} if isinstance(evidence_used_value, list) else set()
    role_map = response.get("evidence_roles") if isinstance(response.get("evidence_roles"), dict) else {}
    role_ids = {str(item) for item in role_map.keys()}
    alt_ids = response_ids_from_alternatives(response)

    for eid in sorted(used_ids | role_ids | alt_ids):
        if eid not in known_ids:
            violations.append("unknown_evidence_id")
            counters["unknown_evidence_id"] += 1
        if eid in do_not_use_ids:
            violations.append("do_not_use_evidence_cited")
            counters["do_not_use_evidence_cited"] += 1

    for eid, role in role_map.items():
        role_text = str(role)
        if role_text in FORBIDDEN_ROLES:
            violations.append("forbidden_role_used")
            counters["forbidden_role_used"] += 1
        elif role_text not in ALLOWED_ROLES:
            warnings.append("unknown_role_used")
        expected_role = (prompt_context.get("role_map") or {}).get(str(eid))
        if expected_role and role_text in ALLOWED_ROLES and role_text != expected_role:
            warnings.append("role_mismatch")
            counters["role_mismatch"] += 1

    if role_ids - used_ids - alt_ids:
        warnings.append("evidence_roles_contains_unused_ids")
    if used_ids and not set(role_map).intersection(used_ids):
        warnings.append("evidence_roles_missing_used_ids")

    root_cause = str(response.get("root_cause") or "").strip().lower()
    insufficient = root_cause == "insufficient_evidence"
    if not insufficient and not used_ids:
        warnings.append("concrete_root_without_evidence")

    root_candidate_ids = set(prompt_context.get("root_candidate_ids") or set())
    if root_candidate_ids and not insufficient and not used_ids.intersection(root_candidate_ids):
        warnings.append("root_candidate_omitted")

    for alt in response.get("alternatives") or []:
        if isinstance(alt, dict) and not alt.get("evidence_used"):
            warnings.append("alternative_without_evidence")

    return list(dict.fromkeys(violations)), list(dict.fromkeys(warnings)), counters


def validate_coverage_gap_behavior(response: Dict[str, Any], prompt_context: Dict[str, Any], args: argparse.Namespace) -> Tuple[List[str], List[str], Counter]:
    violations: List[str] = []
    warnings: List[str] = []
    counters = Counter()
    if prompt_context.get("context_missing"):
        return violations, warnings, counters
    readiness = str(prompt_context.get("readiness") or "")
    no_root = not prompt_context.get("root_candidate_ids")
    if readiness != "coverage_gap" and not no_root:
        return violations, warnings, counters

    root_cause = str(response.get("root_cause") or "").strip().lower()
    root_object = str(response.get("root_object") or "").strip().lower()
    evidence_used = response.get("evidence_used") if isinstance(response.get("evidence_used"), list) else []
    insufficient = root_cause == "insufficient_evidence"
    unknown_object = root_object in {"", "unknown", "insufficient_evidence", "n/a", "none"}
    if insufficient and not args.allow_insufficient_evidence:
        violations.append("insufficient_evidence_not_allowed")
    if not insufficient and (root_cause or evidence_used):
        violations.append("coverage_gap_false_root")
        counters["coverage_gap_false_root"] += 1
    if insufficient and not unknown_object:
        warnings.append("coverage_gap_root_object_not_unknown")
    if insufficient and evidence_used:
        warnings.append("coverage_gap_uses_evidence")
    uncertainty = str(response.get("uncertainty") or "").lower()
    if not insufficient and "uncertain" in uncertainty:
        warnings.append("coverage_gap_diagnostic_language_with_uncertainty")
    return violations, warnings, counters


def validate_sensitive_text(response: Dict[str, Any]) -> Tuple[List[str], Counter]:
    violations: List[str] = []
    counters = Counter()
    for value in iter_values(response):
        if looks_sensitive_text(value):
            violations.append("sensitive_text_violation")
            counters["sensitive_text_violations"] += 1
            counters["injector_or_label_leakage"] += 1
            break
    return violations, counters


def is_structured_slash_token(text: str) -> bool:
    token = safe_text(text).strip(" ,.;:()[]{}\"'")
    if not STRUCTURED_SLASH_TOKEN.fullmatch(token):
        return False
    parts = [part.lower() for part in token.split("/") if part]
    if len(parts) < 2:
        return False
    # Permit compact evidence-internal state/anomaly paths, for example
    # wifi/wlan0/disconnected or SCANNING/ASSOCIATING/4WAY_HANDSHAKE.
    if all(part in SAFE_TECHNICAL_SLASH_TERMS for part in parts):
        return True
    return all(re.fullmatch(r"[A-Za-z0-9_.:-]+", part) for part in parts) and any(
        "_" in part or part.isupper() for part in token.split("/")
    )


def is_structured_slash_token_list(text: str) -> bool:
    tokens = [token for token in re.split(r"[\s,;]+", safe_text(text).strip()) if token]
    if not tokens:
        return False
    return all(is_structured_slash_token(token) for token in tokens)


def has_external_context_claim(text: str) -> bool:
    span = safe_text(text)
    if not span:
        return False
    for pattern in EXTERNAL_CONTEXT_PATTERNS:
        if pattern.search(span):
            return True
    # Absolute filesystem-like paths are still external-context claims. Compact
    # non-leading slash state tokens inside ordinary response text are not.
    if ABSOLUTE_PATH_PATTERN.search(span) or RELATIVE_FILE_PATH_PATTERN.search(span):
        return True
    if is_structured_slash_token_list(span):
        return False
    return False


def validate_hallucination_claims(response: Dict[str, Any], prompt_context: Dict[str, Any]) -> Tuple[List[str], Counter]:
    violations: List[str] = []
    counters = Counter()
    text_values = " ".join(str(value) for value in iter_values(response) if isinstance(value, str))
    for value in iter_values(response):
        if isinstance(value, str) and has_external_context_claim(value):
            violations.append("external_context_claim")
            counters["external_context_claims"] += 1
            break
    case_ref = prompt_context.get("case_ref")
    if case_ref and re.search(r"\bCASE_\d{6}\b", text_values) and str(case_ref) not in text_values:
        violations.append("unknown_case_reference")
    return violations, counters


def validate_do_not_use_semantics(response: Dict[str, Any], prompt_context: Dict[str, Any]) -> Tuple[List[str], Counter]:
    violations: List[str] = []
    counters = Counter()
    diagnostic_text = " ".join(str(value) for value in iter_values(response) if isinstance(value, str)).lower()
    if looks_sensitive_text(diagnostic_text):
        violations.append("do_not_use_semantic_leakage")
        counters["do_not_use_semantic_leakage"] += 1
    for do_not_use_text in prompt_context.get("do_not_use_text") or []:
        if not do_not_use_text:
            continue
        needle = compact_text(do_not_use_text.replace(REDACTION_MARKER, " "), 120).lower()
        needle = re.sub(r"\s+", " ", needle).strip()
        if len(needle) >= 12 and needle in diagnostic_text:
            violations.append("do_not_use_semantic_leakage")
            counters["do_not_use_semantic_leakage"] += 1
            break
    return list(dict.fromkeys(violations)), counters


def validate_consistency(response: Dict[str, Any], prompt_context: Dict[str, Any]) -> List[str]:
    warnings: List[str] = []
    root_object = str(response.get("root_object") or "").strip().lower()
    root_cause = str(response.get("root_cause") or "").strip().lower()
    if root_cause == "insufficient_evidence":
        return warnings
    candidate_objects = set(prompt_context.get("root_candidate_objects") or set())
    candidate_anomalies = set(prompt_context.get("root_candidate_anomalies") or set())
    if candidate_objects and root_object and root_object not in candidate_objects and root_object != "unknown":
        warnings.append("root_object_not_aligned_with_top_candidate")
    if candidate_anomalies and root_cause:
        normalized_cause = root_cause.replace(" ", "_")
        if not any(anomaly in normalized_cause or anomaly.replace("_", " ") in root_cause for anomaly in candidate_anomalies):
            warnings.append("root_cause_not_aligned_with_candidate_anomaly")
    if root_cause in GENERIC_ROOT_CAUSES:
        warnings.append("generic_root_cause")
    alternatives = response.get("alternatives") if isinstance(response.get("alternatives"), list) else []
    for alt in alternatives:
        if isinstance(alt, dict) and str(alt.get("root_cause") or "").strip().lower() == root_cause:
            warnings.append("alternative_duplicates_main_root_cause")
            break
    rendered = safe_json_dump(response)
    if len(rendered) > 4000:
        warnings.append("response_too_verbose")
    return warnings


def validate_prompt_only(prompt_record: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    static_validation = prompt_record.get("static_validation") if isinstance(prompt_record.get("static_validation"), dict) else {}
    hard_violations = list(static_validation.get("violations") or [])
    warnings = list(static_validation.get("warnings") or [])
    if not prompt_record.get("expected_output_schema"):
        hard_violations.append("missing_expected_output_schema")
    if not prompt_record.get("case_ref"):
        hard_violations.append("missing_case_ref")
    status = "FAIL" if hard_violations else ("WARN" if warnings else "PASS")
    if args.strict and warnings and status == "WARN":
        status = "FAIL"
        hard_violations.append("strict_warning_failure")
    return {
        "prompt_id": prompt_record.get("prompt_id"),
        "case_ref": prompt_record.get("case_ref"),
        "status": status,
        "mode": "prompt_only",
        "hard_violations": list(dict.fromkeys(hard_violations)),
        "warnings": list(dict.fromkeys(warnings)),
        "schema_valid": True,
        "evidence_reference_valid": True,
        "do_not_use_violation": False,
        "sensitive_text_violation": "unredacted_sensitive_terms" in hard_violations,
        "coverage_gap_behavior_valid": True,
        "response_summary": {
            "root_cause": None,
            "root_object": None,
            "evidence_used_count": 0,
            "alternative_count": 0,
        },
    }


def validate_response_pair(
    prompt_record: Optional[Dict[str, Any]],
    response_record: Dict[str, Any],
    args: argparse.Namespace,
) -> Tuple[Dict[str, Any], Counter, Counter, Counter]:
    prompt_context = extract_prompt_context(prompt_record)
    response, parse_error = parse_response_record(response_record)
    hard_violations: List[str] = []
    warnings: List[str] = []
    schema_counters = Counter()
    evidence_counters = Counter()
    safety_counters = Counter()

    if parse_error:
        hard_violations.append("response_parse_error")
        schema_counters["parse_errors"] += 1
    schema_valid, schema_violations, missing, wrong_types = validate_response_schema(response)
    schema_counters["valid" if schema_valid else "invalid"] += 1
    schema_counters.update({f"missing_required_fields.{key}": value for key, value in missing.items()})
    schema_counters.update({f"wrong_type_fields.{key}": value for key, value in wrong_types.items()})
    hard_violations.extend(schema_violations)

    if response is None:
        response = {}

    ref_violations, ref_warnings, ref_counters = validate_evidence_references(response, prompt_context)
    coverage_violations, coverage_warnings, coverage_counters = validate_coverage_gap_behavior(response, prompt_context, args)
    sensitive_violations, sensitive_counters = validate_sensitive_text(response)
    hallucination_violations, hallucination_counters = validate_hallucination_claims(response, prompt_context)
    dnu_violations, dnu_counters = validate_do_not_use_semantics(response, prompt_context)

    hard_violations.extend(ref_violations)
    hard_violations.extend(coverage_violations)
    hard_violations.extend(sensitive_violations)
    hard_violations.extend(hallucination_violations)
    hard_violations.extend(dnu_violations)
    warnings.extend(ref_warnings)
    warnings.extend(coverage_warnings)
    warnings.extend(validate_consistency(response, prompt_context))
    if prompt_context.get("context_missing"):
        warnings.append("context_missing")

    evidence_counters.update(ref_counters)
    safety_counters.update(coverage_counters)
    safety_counters.update(sensitive_counters)
    safety_counters.update(hallucination_counters)
    safety_counters.update(dnu_counters)

    if args.strict and warnings:
        hard_violations.append("strict_warning_failure")

    hard_violations = list(dict.fromkeys(hard_violations))
    warnings = list(dict.fromkeys(warnings))
    status = "FAIL" if hard_violations else ("WARN" if warnings else "PASS")
    used = response.get("evidence_used") if isinstance(response.get("evidence_used"), list) else []
    alternatives = response.get("alternatives") if isinstance(response.get("alternatives"), list) else []
    result = {
        "prompt_id": response_record.get("prompt_id") or (prompt_record or {}).get("prompt_id"),
        "case_ref": response_record.get("case_ref") or prompt_context.get("case_ref"),
        "status": status,
        "mode": "prompt_response" if prompt_record else "response_only",
        "hard_violations": hard_violations,
        "warnings": warnings,
        "schema_valid": schema_valid and not parse_error,
        "evidence_reference_valid": (not prompt_context.get("context_missing")) and not any(
            item in hard_violations
            for item in ("unknown_evidence_id", "do_not_use_evidence_cited", "forbidden_role_used")
        ),
        "do_not_use_violation": any(
            item in hard_violations
            for item in ("do_not_use_evidence_cited", "do_not_use_semantic_leakage")
        ),
        "sensitive_text_violation": "sensitive_text_violation" in hard_violations,
        "coverage_gap_behavior_valid": "coverage_gap_false_root" not in hard_violations,
        "response_summary": {
            "root_cause": redact_sensitive_text(response.get("root_cause")) if args.redact_sensitive else response.get("root_cause"),
            "root_object": redact_sensitive_text(response.get("root_object")) if args.redact_sensitive else response.get("root_object"),
            "evidence_used_count": len(used),
            "alternative_count": len(alternatives),
        },
    }
    return result, schema_counters, evidence_counters, safety_counters


def match_prompts_and_responses(
    prompts: Sequence[Dict[str, Any]],
    responses: Sequence[Dict[str, Any]],
) -> Tuple[List[Tuple[Optional[Dict[str, Any]], Dict[str, Any]]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    prompt_by_id = {str(prompt.get("prompt_id")): prompt for prompt in prompts if prompt.get("prompt_id")}
    prompt_by_case = {str(prompt.get("case_ref")): prompt for prompt in prompts if prompt.get("case_ref")}
    used_prompt_ids: set[int] = set()
    used_response_ids: set[int] = set()
    pairs: List[Tuple[Optional[Dict[str, Any]], Dict[str, Any]]] = []

    for idx, response in enumerate(responses):
        prompt = None
        prompt_id = response.get("prompt_id")
        if prompt_id is not None:
            prompt = prompt_by_id.get(str(prompt_id))
        if prompt is None and response.get("case_ref") is not None:
            prompt = prompt_by_case.get(str(response.get("case_ref")))
        if prompt is not None:
            pairs.append((prompt, response))
            used_prompt_ids.add(id(prompt))
            used_response_ids.add(idx)

    remaining_prompts = [prompt for prompt in prompts if id(prompt) not in used_prompt_ids]
    remaining_responses = [response for idx, response in enumerate(responses) if idx not in used_response_ids]
    for prompt, response in zip(remaining_prompts, remaining_responses):
        pairs.append((prompt, response))
        used_prompt_ids.add(id(prompt))
        used_response_ids.add(responses.index(response))

    unmatched_prompts = [prompt for prompt in prompts if id(prompt) not in used_prompt_ids]
    unmatched_responses = [response for idx, response in enumerate(responses) if idx not in used_response_ids]
    return pairs, unmatched_prompts, unmatched_responses


def build_validation_results(
    prompts: Sequence[Dict[str, Any]],
    responses: Sequence[Dict[str, Any]],
    args: argparse.Namespace,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    schema_totals = Counter()
    evidence_totals = Counter()
    safety_totals = Counter()
    if args.limit_records is not None:
        prompts = list(prompts)[: max(0, args.limit_records)]
        responses = list(responses)[: max(0, args.limit_records)]

    results: List[Dict[str, Any]] = []
    unmatched_prompts: List[Dict[str, Any]] = []
    unmatched_responses: List[Dict[str, Any]] = []

    if not responses:
        results = [validate_prompt_only(prompt, args) for prompt in prompts]
        return results, {
            "schema": schema_totals,
            "evidence": evidence_totals,
            "safety": safety_totals,
            "unmatched_prompts": [],
            "unmatched_responses": [],
        }

    if prompts:
        pairs, unmatched_prompts, unmatched_responses = match_prompts_and_responses(prompts, responses)
    else:
        pairs = [(None, response) for response in responses]

    for prompt, response in pairs:
        result, schema_counts, evidence_counts, safety_counts = validate_response_pair(prompt, response, args)
        results.append(result)
        schema_totals.update(schema_counts)
        evidence_totals.update(evidence_counts)
        safety_totals.update(safety_counts)

    return results, {
        "schema": schema_totals,
        "evidence": evidence_totals,
        "safety": safety_totals,
        "unmatched_prompts": unmatched_prompts,
        "unmatched_responses": unmatched_responses,
    }


def validation_mode(prompts: Sequence[Dict[str, Any]], responses: Sequence[Dict[str, Any]]) -> str:
    if prompts and responses:
        return "prompt_response"
    if responses and not prompts:
        return "response_only"
    return "prompt_only"


def build_summary(
    results: Sequence[Dict[str, Any]],
    prompts: Sequence[Dict[str, Any]],
    responses: Sequence[Dict[str, Any]],
    parse_errors: Sequence[Dict[str, Any]],
    aux: Dict[str, Any],
    args: argparse.Namespace,
) -> Dict[str, Any]:
    status_counts = Counter(str(result.get("status") or "FAIL").lower() for result in results)
    warning_counts = Counter()
    violation_counts = Counter()
    for result in results:
        warning_counts.update(str(item) for item in result.get("warnings") or [])
        violation_counts.update(str(item) for item in result.get("hard_violations") or [])

    mode = validation_mode(prompts, responses)
    status = "PASS"
    if status_counts.get("fail", 0):
        status = "FAIL"
    elif status_counts.get("warn", 0) or parse_errors or (mode == "prompt_only" and not responses):
        status = "WARN"

    schema_aux = Counter(aux.get("schema") or {})
    evidence_aux = Counter(aux.get("evidence") or {})
    safety_aux = Counter(aux.get("safety") or {})
    if results and not schema_aux.get("valid") and not schema_aux.get("invalid"):
        schema_aux["valid"] = sum(1 for result in results if result.get("schema_valid"))
        schema_aux["invalid"] = sum(1 for result in results if not result.get("schema_valid"))
    unmatched_prompts = list(aux.get("unmatched_prompts") or [])
    unmatched_responses = list(aux.get("unmatched_responses") or [])
    if parse_errors:
        schema_aux["parse_errors"] += len(parse_errors)

    failures = [result for result in results if result.get("status") == "FAIL"][: args.max_examples]
    warnings = [result for result in results if result.get("status") == "WARN"][: args.max_examples]
    examples = {
        "failures": failures,
        "warnings": warnings,
        "parse_errors": list(parse_errors)[: args.max_examples],
        "unmatched_responses": [
            {
                "prompt_id": item.get("prompt_id"),
                "case_ref": item.get("case_ref"),
                "file": item.get("_source_file"),
                "line": item.get("_source_line"),
            }
            for item in unmatched_responses[: args.max_examples]
        ],
    }

    return {
        "root": str(Path(args.root).resolve()),
        "dry_run": bool(args.dry_run),
        "status": status,
        "mode": mode,
        "counts": {
            "prompt_records": len(prompts),
            "response_records": len(responses),
            "matched_pairs": len(results) if prompts and responses else 0,
            "unmatched_prompts": len(unmatched_prompts),
            "unmatched_responses": len(unmatched_responses),
            "validation_results": len(results),
            "pass": status_counts.get("pass", 0),
            "warn": status_counts.get("warn", 0),
            "fail": status_counts.get("fail", 0),
        },
        "schema": {
            "valid": schema_aux.get("valid", 0),
            "invalid": schema_aux.get("invalid", 0),
            "parse_errors": schema_aux.get("parse_errors", 0),
            "missing_required_fields": {
                key.split(".", 1)[1]: value
                for key, value in schema_aux.items()
                if key.startswith("missing_required_fields.")
            },
            "wrong_type_fields": {
                key.split(".", 1)[1]: value
                for key, value in schema_aux.items()
                if key.startswith("wrong_type_fields.")
            },
        },
        "evidence_reference": {
            "valid": sum(1 for result in results if result.get("evidence_reference_valid")),
            "unknown_evidence_id": evidence_aux.get("unknown_evidence_id", 0),
            "do_not_use_evidence_cited": evidence_aux.get("do_not_use_evidence_cited", 0),
            "forbidden_role_used": evidence_aux.get("forbidden_role_used", 0),
            "role_mismatch": evidence_aux.get("role_mismatch", 0),
        },
        "safety": {
            "sensitive_text_violations": safety_aux.get("sensitive_text_violations", 0),
            "injector_or_label_leakage": safety_aux.get("injector_or_label_leakage", 0),
            "do_not_use_semantic_leakage": safety_aux.get("do_not_use_semantic_leakage", 0),
            "external_context_claims": safety_aux.get("external_context_claims", 0),
            "coverage_gap_false_root": safety_aux.get("coverage_gap_false_root", 0),
        },
        "warnings": dict(warning_counts.most_common()),
        "violations": dict(violation_counts.most_common()),
        "examples": examples,
    }


def print_json_summary(summary: Dict[str, Any]) -> str:
    return safe_json_dump(summary, pretty=True)


def markdown_table(title: str, values: Dict[str, Any]) -> List[str]:
    lines = [f"## {title}", "| metric | value |", "|---|---:|"]
    for key, value in values.items():
        lines.append(f"| `{key}` | {safe_json_dump(value) if isinstance(value, dict) else safe_text(value)} |")
    return lines


def print_markdown_summary(summary: Dict[str, Any]) -> str:
    lines = [
        "# Evidence Response Validator Preview Report",
        "",
        "This validator is read-only by default and does not modify existing evidence files.",
        "This validator does not call any LLM and does not generate diagnosis answers.",
        "do_not_use_evidence is exclusion-only and must never be cited as diagnostic support.",
        "No-root coverage-gap prompts should normally produce insufficient_evidence.",
        "",
        "## Status",
        f"- `status`: {summary.get('status')}",
        f"- `mode`: {summary.get('mode')}",
        "",
    ]
    lines.extend(markdown_table("Counts", summary.get("counts") or {}))
    lines.append("")
    lines.extend(markdown_table("Schema Validation", summary.get("schema") or {}))
    lines.append("")
    lines.extend(markdown_table("Evidence Reference Validation", summary.get("evidence_reference") or {}))
    lines.append("")
    lines.extend(markdown_table("Safety Validation", summary.get("safety") or {}))
    lines.append("")
    lines.append("## Coverage-gap Behavior")
    lines.append(f"- `coverage_gap_false_root`: {(summary.get('safety') or {}).get('coverage_gap_false_root', 0)}")
    if summary.get("warnings"):
        lines.append("")
        lines.extend(markdown_table("Warning Summary", summary.get("warnings") or {}))
    if summary.get("violations"):
        lines.append("")
        lines.extend(markdown_table("Violation Summary", summary.get("violations") or {}))
    examples = summary.get("examples") or {}
    lines.append("")
    lines.append("## Examples")
    for label in ("failures", "warnings", "parse_errors", "unmatched_responses"):
        items = examples.get(label) or []
        if not items:
            continue
        lines.append(f"### {label}")
        for item in items:
            lines.append(f"- `{safe_json_dump(item)}`")
    lines.extend(
        [
            "",
            "## Notes",
            "- Prompt-only mode validates prompt records and reports that no responses were provided.",
            "- Response-only mode validates schema but cannot prove evidence-reference correctness.",
            "- JSONL prompt or response files are treated as authoritative input for this preview run.",
            "- App Server candidates are not used by this validator.",
        ]
    )
    return "\n".join(lines)


def print_jsonl_results(results: Sequence[Dict[str, Any]]) -> str:
    return "\n".join(safe_json_dump(result) for result in results)


def validate_output_path(path: Path, root: Path) -> None:
    resolved = path.resolve()
    if resolved.exists():
        raise ValueError(f"refusing to overwrite existing output path: {resolved}")
    if resolved.name.lower() in PROTECTED_OUTPUT_FILENAMES:
        raise ValueError(f"refusing protected output filename: {resolved}")
    parts = [part.lower() for part in resolved.parts]
    if any(part in PROTECTED_OUTPUT_DIRS for part in parts):
        raise ValueError(f"refusing output path under protected directory: {resolved}")
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"refusing output path outside repository root: {resolved}") from exc
    if not resolved.parent.exists():
        raise ValueError(f"output parent does not exist: {resolved.parent}")


def render_output(summary: Dict[str, Any], results: Sequence[Dict[str, Any]], args: argparse.Namespace) -> str:
    if args.format == "json":
        return print_json_summary(summary) + "\n"
    if args.format == "markdown":
        return print_markdown_summary(summary)
    if args.format == "jsonl":
        return print_jsonl_results(results) + ("\n" if results else "")
    return "----- JSON -----\n" + print_json_summary(summary) + "\n\n----- MARKDOWN -----\n" + print_markdown_summary(summary)


def run(args: argparse.Namespace) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    root = Path(args.root).resolve()
    prompts: List[Dict[str, Any]] = []
    prompt_parse_errors: List[Dict[str, Any]] = []
    if args.prompts_jsonl:
        prompt_path = Path(args.prompts_jsonl)
        if not prompt_path.is_absolute():
            prompt_path = root / prompt_path
        prompts, prompt_parse_errors = load_prompts_from_jsonl(prompt_path, args.max_examples)
    elif args.response_only:
        prompts = []
    else:
        prompts, _prompt_summary, prompt_parse_errors = load_or_build_prompts(args)

    responses: List[Dict[str, Any]] = []
    response_parse_errors: List[Dict[str, Any]] = []
    if args.responses_jsonl:
        response_path = Path(args.responses_jsonl)
        if not response_path.is_absolute():
            response_path = root / response_path
        responses, response_parse_errors = load_responses_from_jsonl(response_path, args.max_examples)

    results, aux = build_validation_results(prompts, responses, args)
    parse_errors = list(prompt_parse_errors) + list(response_parse_errors)
    summary = build_summary(results, prompts, responses, parse_errors, aux, args)
    return summary, results


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    root = Path(args.root).resolve()
    try:
        summary, results = run(args)
    except RuntimeError as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 2
    output = render_output(summary, results, args)
    if args.output and not args.stdout_only:
        output_path = Path(args.output)
        if not output_path.is_absolute():
            output_path = root / output_path
        try:
            validate_output_path(output_path, root)
        except ValueError as exc:
            sys.stderr.write(f"ERROR: {exc}\n")
            return 2
        output_path.write_text(output, encoding="utf-8", newline="\n")
    else:
        sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
