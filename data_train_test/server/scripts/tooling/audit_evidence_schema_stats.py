#!/usr/bin/env python3
"""Read-only evidence schema/statistics audit for this repository.

The tool scans existing JSONL/text artifacts and reports evidence schema
coverage, role distribution, duplication, empty evidence targets, and common
leakage-risk patterns. It is intentionally standalone and uses only the Python
standard library.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple


IGNORED_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".venv",
    "venv",
    "node_modules",
}

BINARY_SUFFIXES = {
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".ico",
    ".zip",
    ".rar",
    ".7z",
    ".tar",
    ".gz",
    ".bin",
    ".exe",
    ".dll",
    ".so",
    ".pyc",
}

L1_FIELDS = (
    "eid",
    "source",
    "source_rel",
    "kind",
    "target",
    "support_role",
    "ts",
    "span",
    "text",
    "score",
    "phase",
    "object",
    "causal_role",
    "raw_refs",
    "context_before",
    "context_after",
    "normalized_entity",
    "anomaly_type",
    "utility_score",
)

PROPOSED_FIELDS = (
    "phase",
    "object",
    "causal_role",
    "raw_refs",
    "context_before",
    "context_after",
    "normalized_entity",
    "anomaly_type",
    "utility_score",
)

TASK_JSONL_NAMES = {
    "diagnosis.jsonl",
    "evidence_extraction.jsonl",
    "cause_vs_symptom.jsonl",
    "action_after_diagnosis.jsonl",
    "train.jsonl",
    "val.jsonl",
    "test.jsonl",
}

EVIDENCE_ARRAY_KEYS = (
    "evidence",
    "primary_evidence",
    "supporting_evidence",
    "secondary_evidence",
    "symptom_evidence",
    "contradicting_evidence",
    "noise_evidence",
    "do_not_use_evidence",
    "cause_eids",
    "evidence_used",
)

GT_LIKE_KEYS = {
    "gt",
    "ground_truth",
    "label",
    "answer",
    "root_cause",
    "root_cause_text",
    "fault_type",
    "primary_subtype",
    "subtype",
    "scenario_tag",
}

TARGET_PATH_HINTS = {
    "target",
    "expected",
    "answer",
    "assistant",
    "gold",
    "label",
}

HIGH_RISK_PATH_HINTS = {
    "input",
    "prompt",
    "context",
    "message",
    "messages",
    "user",
    "training_view",
    "diagnosis_input",
}

GT_TEXT_RE = re.compile(
    r"\b(gt|gt_family|gt_subtype|ground_truth|root_cause|root_cause_text|"
    r"fault_type|primary_subtype|scenario_tag)\b",
    re.IGNORECASE,
)

KNOWN_NET_SUBTYPE_RE = re.compile(
    r"\b(?:fault_)?net_(?:dns_fail|link_down|link_flap|gateway_unreachable|"
    r"packet_loss|latency_spike|auth_fail|public_ip_unreachable|"
    r"no_default_route|no_ipv4_on_iface|wrong_default_route|wifi_disconnect|"
    r"wifi_auth_fail_wrong_psk|wlan_disconnect)\b",
    re.IGNORECASE,
)

EID_RE = re.compile(r"\b(?:e\d+|E\d{3,})\b")

RECOVERY_RE = re.compile(r"\b(recovery|recovered|restored|post)\b|after the fault", re.IGNORECASE)
FAULT_RE = re.compile(r"\b(fault|during fault|fault snapshot)\b", re.IGNORECASE)
BASELINE_RE = re.compile(r"\b(baseline|before|pre)\b", re.IGNORECASE)
SYMPTOM_RE = re.compile(r"\b(symptom|downstream|secondary symptom)\b", re.IGNORECASE)


def is_empty_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) == 0
    return False


def add_example(bucket: List[Dict[str, Any]], example: Dict[str, Any], max_examples: int) -> None:
    if len(bucket) < max_examples:
        bucket.append(example)


def rel_path(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def iter_files(root: Path) -> Iterator[Path]:
    for path in root.rglob("*"):
        if any(part in IGNORED_DIRS for part in path.parts):
            continue
        if not path.is_file():
            continue
        if path.suffix.lower() in BINARY_SUFFIXES:
            continue
        yield path


def read_text_lossy(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def read_jsonl(path: Path, max_examples: int) -> Tuple[List[Tuple[int, Dict[str, Any]]], List[Dict[str, Any]]]:
    rows: List[Tuple[int, Dict[str, Any]]] = []
    errors: List[Dict[str, Any]] = []
    try:
        text = read_text_lossy(path)
    except OSError as exc:
        return rows, [{"file": str(path), "line": None, "error": str(exc), "snippet": ""}]
    for line_no, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            add_example(
                errors,
                {
                    "file": str(path),
                    "line": line_no,
                    "error": str(exc),
                    "snippet": line[:240],
                },
                max_examples,
            )
            continue
        if isinstance(payload, dict):
            rows.append((line_no, payload))
        else:
            add_example(
                errors,
                {
                    "file": str(path),
                    "line": line_no,
                    "error": "JSONL row is not an object",
                    "snippet": line[:240],
                },
                max_examples,
            )
    return rows, errors


def flatten_json_paths(obj: Any, prefix: str = "") -> Iterator[Tuple[str, str, Any]]:
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            yield path, str(key), value
            yield from flatten_json_paths(value, path)
    elif isinstance(obj, list):
        for index, item in enumerate(obj):
            path = f"{prefix}[{index}]"
            yield from flatten_json_paths(item, path)


def recursive_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from recursive_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from recursive_strings(item)


def all_values_text(obj: Any) -> str:
    return " ".join(recursive_strings(obj))


def normalize_text(text: str) -> str:
    return " ".join(text.lower().split())


def percentile(values: Sequence[float], p: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    index = int(round((len(ordered) - 1) * p))
    index = max(0, min(index, len(ordered) - 1))
    return ordered[index]


def infer_phase(row: Dict[str, Any]) -> str:
    hay = " ".join(
        str(row.get(key) or "")
        for key in ("phase", "source", "source_rel", "kind", "text")
    )
    if RECOVERY_RE.search(hay):
        return "recovery"
    if FAULT_RE.search(hay):
        return "fault"
    if BASELINE_RE.search(hay):
        return "baseline"
    return "unknown"


def detect_injector_marker(row: Dict[str, Any]) -> bool:
    source = str(row.get("source") or "").lower()
    kind = str(row.get("kind") or "").lower()
    text = str(row.get("text") or "")
    all_text = all_values_text(row)
    return (
        "fault_inject" in source
        or "inject" in source
        or "injector" in source
        or "inject" in kind
        or "injector" in kind
        or "marker" in kind
        or "fault_net_" in text.lower()
        or "inject" in text.lower()
        or "injector" in text.lower()
        or "scenario_tag" in text.lower()
        or bool(KNOWN_NET_SUBTYPE_RE.search(all_text))
    )


def detect_recovery_primary(row: Dict[str, Any]) -> bool:
    hay = " ".join(str(row.get(key) or "") for key in ("source", "source_rel", "kind", "text"))
    is_primary = str(row.get("support_role") or "").lower() == "primary" or str(row.get("target") or "").lower() == "primary"
    return is_primary and bool(RECOVERY_RE.search(hay))


def detect_generic_noise(row: Dict[str, Any]) -> bool:
    text = str(row.get("text") or "").lower()
    kind = str(row.get("kind") or "").lower()
    role = str(row.get("support_role") or "").lower()
    return (
        "duplicate_probe" in text
        or "duplicate_probe" in kind
        or "historical_noise" in text
        or "historical_noise" in kind
        or "remained reachable" in text
        or role == "noise"
    )


def is_symptom_like(row: Dict[str, Any]) -> bool:
    if str(row.get("support_role") or "").lower() == "symptom":
        return True
    if str(row.get("causal_role") or "").lower() == "symptom":
        return True
    return bool(SYMPTOM_RE.search(str(row.get("kind") or "") + " " + str(row.get("text") or "")))


def example_from_l1(path: Path, root: Path, line_no: int, row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "file": rel_path(path, root),
        "line": line_no,
        "eid": row.get("eid"),
        "source": row.get("source"),
        "kind": row.get("kind"),
        "support_role": row.get("support_role"),
        "target": row.get("target"),
        "text": str(row.get("text") or "")[:300],
    }


def find_evidence_like_arrays(obj: Any, prefix: str = "") -> List[Tuple[str, List[Any]]]:
    found: List[Tuple[str, List[Any]]] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            key_l = str(key).lower()
            if isinstance(value, list) and any(term in key_l for term in EVIDENCE_ARRAY_KEYS):
                found.append((path, value))
            found.extend(find_evidence_like_arrays(value, path))
    elif isinstance(obj, list):
        for index, item in enumerate(obj):
            found.extend(find_evidence_like_arrays(item, f"{prefix}[{index}]"))
    return found


def extract_eids(value: Any) -> List[str]:
    out: List[str] = []
    if isinstance(value, str):
        out.extend(EID_RE.findall(value))
    elif isinstance(value, dict):
        for item in value.values():
            out.extend(extract_eids(item))
    elif isinstance(value, list):
        for item in value:
            out.extend(extract_eids(item))
    return out


def parse_json_object_text(text: str) -> Optional[Dict[str, Any]]:
    text = text.strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def extract_l2_input_from_user_text(text: str) -> Optional[Dict[str, Any]]:
    marker = "L2_INPUT_JSON:"
    if marker not in text:
        return None
    after = text.split(marker, 1)[1]
    brace = after.find("{")
    if brace < 0:
        return None
    decoder = json.JSONDecoder()
    try:
        payload, _ = decoder.raw_decode(after[brace:])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def message_payloads(row: Dict[str, Any]) -> Tuple[Optional[str], Dict[str, Any]]:
    payload: Dict[str, Any] = {}
    task: Optional[str] = None
    messages = row.get("messages")
    if not isinstance(messages, list):
        return task, payload
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        content = str(message.get("content") or "")
        if role == "user":
            l2_input = extract_l2_input_from_user_text(content)
            if l2_input:
                payload["input_envelope"] = l2_input
                payload["input"] = l2_input.get("input") or {}
                task = str(l2_input.get("task") or task or "")
        elif role == "assistant":
            assistant_obj = parse_json_object_text(content)
            if assistant_obj:
                payload["target"] = assistant_obj
    return task or None, payload


def infer_task_type(path: Path, row: Dict[str, Any], normalized: Dict[str, Any], message_task: Optional[str]) -> str:
    for key in ("sample_type", "task"):
        if row.get(key):
            return str(row[key])
    if message_task:
        return message_task
    name = path.name.lower()
    if name in {"diagnosis.jsonl", "evidence_extraction.jsonl", "cause_vs_symptom.jsonl", "action_after_diagnosis.jsonl"}:
        return name[:-6]
    if name in {"train.jsonl", "val.jsonl", "test.jsonl"}:
        return "train_val_test_unknown"
    if find_evidence_like_arrays(normalized or row):
        return "evidence_like_unknown"
    return "unknown"


def normalized_l2_payload(row: Dict[str, Any]) -> Tuple[Optional[str], Dict[str, Any]]:
    task, payload = message_payloads(row)
    if payload:
        return task, payload
    out: Dict[str, Any] = {}
    if isinstance(row.get("input"), dict):
        out["input"] = row["input"]
    if isinstance(row.get("target"), dict):
        out["target"] = row["target"]
    if not out:
        out = row
    return task, out


def has_l2_like_structure(path: Path, row: Dict[str, Any], normalized: Dict[str, Any]) -> bool:
    if path.name.lower() in TASK_JSONL_NAMES:
        return True
    if any(key in row for key in ("input", "target", "messages", "sample_type", "task")):
        return True
    return bool(find_evidence_like_arrays(normalized))


def classify_gt_path(path: str) -> str:
    path_l = path.lower()
    if any(f".{hint}" in path_l or path_l.startswith(hint) for hint in TARGET_PATH_HINTS):
        return "target_or_expected"
    if any(f".{hint}" in path_l or path_l.startswith(hint) for hint in HIGH_RISK_PATH_HINTS):
        return "input_prompt_context_high_risk"
    return "other"


def detect_gt_like_paths(
    obj: Any,
    file_path: Path,
    root: Path,
    line_no: int,
    max_examples: int,
    counts: Dict[str, Counter],
    examples: Dict[str, List[Dict[str, Any]]],
) -> None:
    for path, key, value in flatten_json_paths(obj):
        key_l = key.lower()
        if key_l in GT_LIKE_KEYS:
            bucket = classify_gt_path(path)
            counts[bucket][path] += 1
            add_example(
                examples[bucket],
                {
                    "file": rel_path(file_path, root),
                    "line": line_no,
                    "path": path,
                    "key": key,
                    "value_preview": str(value)[:180],
                },
                max_examples,
            )
        if isinstance(value, str) and classify_gt_path(path) == "input_prompt_context_high_risk" and GT_TEXT_RE.search(value):
            bucket = "input_prompt_context_high_risk_text"
            counts[bucket][path] += 1
            add_example(
                examples[bucket],
                {
                    "file": rel_path(file_path, root),
                    "line": line_no,
                    "path": path,
                    "value_preview": value[:220],
                },
                max_examples,
            )

    messages = obj.get("messages") if isinstance(obj, dict) else None
    if isinstance(messages, list):
        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "unknown")
            content = str(message.get("content") or "")
            if GT_TEXT_RE.search(content):
                bucket = "target_or_expected" if role == "assistant" else "input_prompt_context_high_risk_text"
                path = f"messages[{index}].content(role={role})"
                counts[bucket][path] += 1
                add_example(
                    examples[bucket],
                    {
                        "file": rel_path(file_path, root),
                        "line": line_no,
                        "path": path,
                        "value_preview": content[:220],
                    },
                    max_examples,
                )


def count_l1_evidence(
    root: Path,
    evidence_files: Sequence[Path],
    max_examples: int,
    parse_errors: List[Dict[str, Any]],
) -> Tuple[Dict[str, Any], Dict[str, Any], set[str]]:
    field_present = Counter()
    field_non_empty = Counter()
    distributions: Dict[str, Counter] = {
        "source": Counter(),
        "source_rel": Counter(),
        "kind": Counter(),
        "target": Counter(),
        "support_role": Counter(),
        "causal_role": Counter(),
    }
    explicit_phase = Counter()
    inferred_phase = Counter()
    duplicate_texts: Dict[str, Dict[str, Any]] = {}
    text_counter = Counter()
    text_lengths: List[int] = []
    l1_eids: set[str] = set()

    risks = {
        "injector_marker": Counter(),
        "recovery_primary": Counter(),
        "generic_noise": Counter(),
    }
    examples = {
        "injector_primary": [],
        "injector_like": [],
        "recovery_primary": [],
        "generic_noise": [],
    }
    symptom_like = 0
    total_rows = 0

    for path in evidence_files:
        rows, errors = read_jsonl(path, max_examples)
        parse_errors.extend(errors)
        for line_no, row in rows:
            total_rows += 1
            eid = row.get("eid")
            if isinstance(eid, str) and eid:
                l1_eids.add(eid)
            for field in L1_FIELDS:
                if field in row:
                    field_present[field] += 1
                    if not is_empty_value(row.get(field)):
                        field_non_empty[field] += 1
            for field in distributions:
                if field in row:
                    value = row.get(field)
                    distributions[field][str(value) if value is not None else "<null>"] += 1
            if not is_empty_value(row.get("phase")):
                explicit_phase[str(row.get("phase"))] += 1
            inferred_phase[infer_phase(row)] += 1
            text = str(row.get("text") or "")
            text_lengths.append(len(text))
            norm = normalize_text(text)
            if norm:
                text_counter[norm] += 1
                duplicate_texts.setdefault(
                    norm,
                    {
                        "text": text[:240],
                        "examples": [],
                    },
                )
                add_example(
                    duplicate_texts[norm]["examples"],
                    {"file": rel_path(path, root), "line": line_no, "eid": row.get("eid")},
                    3,
                )
            if detect_injector_marker(row):
                risks["injector_marker"]["total"] += 1
                add_example(examples["injector_like"], example_from_l1(path, root, line_no, row), max_examples)
                if str(row.get("support_role") or "").lower() == "primary":
                    risks["injector_marker"]["primary_support_role"] += 1
                    add_example(examples["injector_primary"], example_from_l1(path, root, line_no, row), max_examples)
                if str(row.get("target") or "").lower() == "primary":
                    risks["injector_marker"]["primary_target"] += 1
            if detect_recovery_primary(row):
                risks["recovery_primary"]["total"] += 1
                add_example(examples["recovery_primary"], example_from_l1(path, root, line_no, row), max_examples)
            if detect_generic_noise(row):
                risks["generic_noise"]["total"] += 1
                add_example(examples["generic_noise"], example_from_l1(path, root, line_no, row), max_examples)
            if is_symptom_like(row):
                symptom_like += 1

    coverage = {}
    for field in L1_FIELDS:
        present = field_present[field]
        non_empty = field_non_empty[field]
        coverage[field] = {
            "present": present,
            "non_empty": non_empty,
            "missing": total_rows - present,
            "empty_or_null": present - non_empty,
        }

    duplicates = []
    for norm, count in text_counter.most_common():
        if count <= 1:
            continue
        item = duplicate_texts[norm]
        duplicates.append(
            {
                "count": count,
                "text": item["text"],
                "examples": item["examples"],
            }
        )

    text_length = {
        "min": min(text_lengths) if text_lengths else None,
        "p50": percentile(text_lengths, 0.50),
        "p90": percentile(text_lengths, 0.90),
        "max": max(text_lengths) if text_lengths else None,
        "average": round(sum(text_lengths) / len(text_lengths), 3) if text_lengths else None,
    }

    mostly_missing = {}
    for field in PROPOSED_FIELDS:
        non_empty = coverage[field]["non_empty"]
        ratio = (non_empty / total_rows) if total_rows else 0.0
        if ratio < 0.20:
            mostly_missing[field] = {
                "non_empty": non_empty,
                "total_rows": total_rows,
                "non_empty_ratio": round(ratio, 6),
            }

    l1_report = {
        "field_coverage": coverage,
        "distributions": {name: dict(counter.most_common(30)) for name, counter in distributions.items()},
        "text_length": text_length,
        "duplicates": {
            "duplicate_text_count": len(duplicates),
            "top_duplicate_texts": duplicates[:max_examples],
        },
        "phase": {
            "explicit": dict(explicit_phase.most_common()),
            "inferred_preview": dict(inferred_phase.most_common()),
            "inference_note": "Inferred phase is heuristic and not authoritative.",
        },
        "missing_proposed_schema_fields": mostly_missing,
        "symptom_sparsity": {
            "symptom_like_rows": symptom_like,
            "total_rows": total_rows,
            "ratio": round(symptom_like / total_rows, 6) if total_rows else 0.0,
        },
    }
    risks_report = {
        "injector_marker": {
            "total_injector_like_rows": risks["injector_marker"]["total"],
            "primary_support_role_rows": risks["injector_marker"]["primary_support_role"],
            "primary_target_rows": risks["injector_marker"]["primary_target"],
        },
        "recovery_primary": {"total_rows": risks["recovery_primary"]["total"]},
        "generic_noise": {"total_rows": risks["generic_noise"]["total"]},
    }
    return l1_report, {"risks": risks_report, "examples": examples, "total_rows": total_rows}, l1_eids


def count_l2_jsonl(
    root: Path,
    jsonl_files: Sequence[Path],
    max_examples: int,
    parse_errors: List[Dict[str, Any]],
    l1_eids: set[str],
    gt_counts: Dict[str, Counter],
    gt_examples: Dict[str, List[Dict[str, Any]]],
) -> Tuple[Dict[str, Any], set[str], List[Dict[str, Any]]]:
    by_file: Dict[str, Any] = {}
    task_distribution = Counter()
    l2_files = 0
    l2_samples = 0
    samples_with_arrays = 0
    all_empty_count = 0
    all_empty_examples: List[Dict[str, Any]] = []
    referenced_eids: set[str] = set()

    for path in jsonl_files:
        if path.name == "evidence_candidates.jsonl":
            continue
        rows, errors = read_jsonl(path, max_examples)
        parse_errors.extend(errors)
        file_sample_count = 0
        file_array_samples = 0
        file_all_empty = 0
        file_task_counter = Counter()
        file_referenced_eids: set[str] = set()
        for line_no, row in rows:
            message_task, normalized = normalized_l2_payload(row)
            if not has_l2_like_structure(path, row, normalized):
                continue
            detect_gt_like_paths(row, path, root, line_no, max_examples, gt_counts, gt_examples)
            detect_gt_like_paths(normalized, path, root, line_no, max_examples, gt_counts, gt_examples)
            task = infer_task_type(path, row, normalized, message_task)
            arrays = find_evidence_like_arrays(normalized)
            eids = set(extract_eids(normalized))
            referenced_eids.update(eids)
            file_referenced_eids.update(eids)
            file_sample_count += 1
            l2_samples += 1
            task_distribution[task] += 1
            file_task_counter[task] += 1
            if arrays:
                samples_with_arrays += 1
                file_array_samples += 1
                empty_arrays = [(name, values) for name, values in arrays if len(values) == 0]
                if len(empty_arrays) == len(arrays):
                    all_empty_count += 1
                    file_all_empty += 1
                    add_example(
                        all_empty_examples,
                        {
                            "file": rel_path(path, root),
                            "line": line_no,
                            "case_id": row.get("case_id") or (normalized.get("input_envelope") or {}).get("case_id"),
                            "sample_id": row.get("sample_id") or (normalized.get("input_envelope") or {}).get("sample_id"),
                            "task": task,
                            "array_paths": [name for name, _values in arrays],
                        },
                        max_examples,
                    )
        if file_sample_count:
            l2_files += 1
            by_file[rel_path(path, root)] = {
                "samples": file_sample_count,
                "samples_with_evidence_arrays": file_array_samples,
                "all_empty_evidence_targets": file_all_empty,
                "task_distribution": dict(file_task_counter.most_common()),
                "referenced_eid_count": len(file_referenced_eids),
            }

    unknown_eids = referenced_eids - l1_eids if l1_eids else set()
    l2_report = {
        "by_file": by_file,
        "task_distribution": dict(task_distribution.most_common()),
        "empty_evidence_targets": {
            "samples_with_evidence_arrays": samples_with_arrays,
            "samples_with_all_empty_evidence_arrays": all_empty_count,
            "note": "All-empty detection is structural and includes any evidence-like arrays found in a sample.",
        },
        "referenced_eids": {
            "referenced_eid_count": len(referenced_eids),
            "known_l1_eid_count": len(l1_eids),
            "unknown_referenced_eid_count": len(unknown_eids),
            "unknown_referenced_eids_sample": sorted(unknown_eids)[:max_examples],
            "note": "Global approximate matching only; EIDs are not matched case-by-case.",
        },
    }
    return l2_report, referenced_eids, all_empty_examples


def scan_training_views(root: Path, max_examples: int) -> Dict[str, Any]:
    file_count = 0
    match_count = 0
    examples: List[Dict[str, Any]] = []
    for path in iter_files(root):
        if path.name != "diagnosis_input.txt" or path.parent.name != "training_views":
            continue
        file_count += 1
        try:
            text = read_text_lossy(path)
        except OSError:
            continue
        for line_no, line in enumerate(text.splitlines(), 1):
            if GT_TEXT_RE.search(line):
                match_count += 1
                add_example(
                    examples,
                    {"file": rel_path(path, root), "line": line_no, "text": line[:240]},
                    max_examples,
                )
    return {
        "files_scanned": file_count,
        "matching_lines": match_count,
        "examples": examples,
        "note": "Potential leakage only if these files are used as model input.",
    }


def discover_files(root: Path) -> Tuple[List[Path], List[Path], List[Path]]:
    evidence_files: List[Path] = []
    jsonl_files: List[Path] = []
    training_views: List[Path] = []
    for path in iter_files(root):
        name = path.name.lower()
        if name == "evidence_candidates.jsonl":
            evidence_files.append(path)
        if path.suffix.lower() == ".jsonl":
            jsonl_files.append(path)
        if name == "diagnosis_input.txt" and path.parent.name == "training_views":
            training_views.append(path)
    return sorted(evidence_files), sorted(jsonl_files), sorted(training_views)


def top_counter(counter: Counter, limit: int) -> Dict[str, int]:
    return dict(counter.most_common(limit))


def build_report(args: argparse.Namespace) -> Dict[str, Any]:
    root = Path(args.root).resolve()
    parse_errors: List[Dict[str, Any]] = []
    gt_counts: Dict[str, Counter] = defaultdict(Counter)
    gt_examples: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    evidence_files, jsonl_files, _training_view_files = discover_files(root)
    l1_report, l1_aux, l1_eids = count_l1_evidence(root, evidence_files, args.max_examples, parse_errors)
    l2_report, _referenced_eids, all_empty_examples = count_l2_jsonl(
        root,
        jsonl_files,
        args.max_examples,
        parse_errors,
        l1_eids,
        gt_counts,
        gt_examples,
    )

    training_view_report = {
        "files_scanned": 0,
        "matching_lines": 0,
        "examples": [],
        "note": "Not scanned. Use --include-training-views to enable plain-text scan.",
    }
    if args.include_training_views:
        training_view_report = scan_training_views(root, args.max_examples)

    l2_file_count = len(l2_report["by_file"])
    l2_sample_count = sum(item["samples"] for item in l2_report["by_file"].values())
    risk_examples = l1_aux["examples"]
    risk_examples["all_empty_evidence_targets"] = all_empty_examples
    risk_examples["parse_errors"] = parse_errors[: args.max_examples]

    gt_like_paths = {
        "target_or_expected": {
            "total": sum(gt_counts["target_or_expected"].values()),
            "top_paths": top_counter(gt_counts["target_or_expected"], args.max_examples),
            "examples": gt_examples["target_or_expected"][: args.max_examples],
        },
        "input_prompt_context_high_risk": {
            "total": sum(gt_counts["input_prompt_context_high_risk"].values()),
            "top_paths": top_counter(gt_counts["input_prompt_context_high_risk"], args.max_examples),
            "examples": gt_examples["input_prompt_context_high_risk"][: args.max_examples],
        },
        "input_prompt_context_high_risk_text": {
            "total": sum(gt_counts["input_prompt_context_high_risk_text"].values()),
            "top_paths": top_counter(gt_counts["input_prompt_context_high_risk_text"], args.max_examples),
            "examples": gt_examples["input_prompt_context_high_risk_text"][: args.max_examples],
        },
        "other": {
            "total": sum(gt_counts["other"].values()),
            "top_paths": top_counter(gt_counts["other"], args.max_examples),
            "examples": gt_examples["other"][: args.max_examples],
        },
        "note": "These are potential GT-like paths. Review context before calling them definite leakage.",
    }

    return {
        "root": str(root),
        "dry_run": bool(args.dry_run),
        "stdout_only": bool(args.stdout_only),
        "counts": {
            "l1_evidence_files": len(evidence_files),
            "l1_evidence_rows": l1_aux["total_rows"],
            "l2_jsonl_files": l2_file_count,
            "l2_samples": l2_sample_count,
            "parse_errors": len(parse_errors),
        },
        "l1": l1_report,
        "l2": l2_report,
        "risks": {
            **l1_aux["risks"],
            "gt_like_paths": gt_like_paths,
            "training_view_gt_like_text": training_view_report,
        },
        "examples": risk_examples,
        "safety_note": "This script is read-only and does not modify evidence files.",
    }


def json_report(report: Dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)


def markdown_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(lines)


def format_percent(numerator: int, denominator: int) -> str:
    if not denominator:
        return "n/a"
    return f"{(numerator / denominator) * 100:.2f}%"


def markdown_report(report: Dict[str, Any], max_examples: int) -> str:
    counts = report["counts"]
    l1 = report["l1"]
    l2 = report["l2"]
    risks = report["risks"]
    lines: List[str] = []
    lines.append("# Evidence Schema Audit Report")
    lines.append("")
    lines.append("This script is read-only and does not modify evidence files.")
    lines.append("")
    lines.append("## Counts Summary")
    lines.append(
        markdown_table(
            ["metric", "value"],
            [
                ["root", report["root"]],
                ["dry_run", report["dry_run"]],
                ["l1_evidence_files", counts["l1_evidence_files"]],
                ["l1_evidence_rows", counts["l1_evidence_rows"]],
                ["l2_jsonl_files", counts["l2_jsonl_files"]],
                ["l2_samples", counts["l2_samples"]],
                ["parse_errors", counts["parse_errors"]],
            ],
        )
    )
    lines.append("")
    lines.append("## Risk Summary")
    risk_rows: List[List[Any]] = []
    injector_primary_support = risks["injector_marker"]["primary_support_role_rows"]
    injector_primary_target = risks["injector_marker"]["primary_target_rows"]
    all_empty = l2["empty_evidence_targets"]["samples_with_all_empty_evidence_arrays"]
    recovery_primary = risks["recovery_primary"]["total_rows"]
    symptom_ratio = l1["symptom_sparsity"]["ratio"]
    missing_fields = l1["missing_proposed_schema_fields"]
    if injector_primary_support or injector_primary_target:
        risk_rows.append(
            [
                "P0",
                "injector-like evidence is primary",
                f"support_role=primary: {injector_primary_support}; target=primary: {injector_primary_target}",
            ]
        )
    if all_empty:
        risk_rows.append(["P0", "all-empty evidence target arrays", all_empty])
    if missing_fields:
        risk_rows.append(["P1", "proposed schema fields mostly missing", ", ".join(sorted(missing_fields))])
    if recovery_primary:
        risk_rows.append(["P1", "recovery-looking evidence marked primary", recovery_primary])
    if counts["l1_evidence_rows"] and symptom_ratio < 0.05:
        risk_rows.append(["P1", "symptom evidence ratio very low", f"{symptom_ratio:.4f}"])
    if not risk_rows:
        risk_rows.append(["OK", "no P0/P1 risk trigger fired", ""])
    lines.append(markdown_table(["severity", "risk", "value"], risk_rows))
    lines.append("")
    lines.append("## L1 Field Coverage")
    field_rows = []
    for field, data in l1["field_coverage"].items():
        field_rows.append(
            [
                field,
                data["present"],
                data["non_empty"],
                data["missing"],
                data["empty_or_null"],
                format_percent(data["non_empty"], counts["l1_evidence_rows"]),
            ]
        )
    lines.append(markdown_table(["field", "present", "non_empty", "missing", "empty/null", "non_empty_pct"], field_rows))
    lines.append("")
    lines.append("## L1 Distributions")
    for name in ("support_role", "target", "source", "kind"):
        rows = [[key, value] for key, value in list(l1["distributions"].get(name, {}).items())[:max_examples]]
        lines.append(f"### {name}")
        lines.append(markdown_table([name, "count"], rows or [["<none>", 0]]))
        lines.append("")
    lines.append("## L1 Text And Phase")
    lines.append(
        markdown_table(
            ["metric", "value"],
            [
                ["text_length", l1["text_length"]],
                ["duplicate_text_count", l1["duplicates"]["duplicate_text_count"]],
                ["explicit_phase", l1["phase"]["explicit"]],
                ["inferred_phase_preview", l1["phase"]["inferred_preview"]],
            ],
        )
    )
    lines.append("")
    lines.append("## L2 Empty Evidence Target Summary")
    lines.append(
        markdown_table(
            ["metric", "value"],
            [
                ["samples_with_evidence_arrays", l2["empty_evidence_targets"]["samples_with_evidence_arrays"]],
                ["samples_with_all_empty_evidence_arrays", l2["empty_evidence_targets"]["samples_with_all_empty_evidence_arrays"]],
                ["referenced_eid_count", l2["referenced_eids"]["referenced_eid_count"]],
                ["unknown_referenced_eid_count", l2["referenced_eids"]["unknown_referenced_eid_count"]],
            ],
        )
    )
    per_file_rows = []
    for file_name, data in list(l2["by_file"].items())[:max_examples]:
        per_file_rows.append([file_name, data["samples"], data["samples_with_evidence_arrays"], data["all_empty_evidence_targets"]])
    lines.append(markdown_table(["file", "samples", "with evidence arrays", "all-empty"], per_file_rows or [["<none>", 0, 0, 0]]))
    lines.append("")
    lines.append("## Risk Examples")
    for title, key in (
        ("Injector primary", "injector_primary"),
        ("Recovery primary", "recovery_primary"),
        ("All-empty evidence targets", "all_empty_evidence_targets"),
        ("Parse errors", "parse_errors"),
    ):
        lines.append(f"### {title}")
        examples = report["examples"].get(key) or []
        if not examples:
            lines.append("- none")
        else:
            for item in examples[:max_examples]:
                lines.append(f"- `{item}`")
        lines.append("")
    lines.append("## GT-like Paths")
    lines.append("Potential GT-like paths only; review context before treating them as definite leakage.")
    gt_rows = []
    for key in ("target_or_expected", "input_prompt_context_high_risk", "input_prompt_context_high_risk_text", "other"):
        gt_rows.append([key, risks["gt_like_paths"][key]["total"], risks["gt_like_paths"][key]["top_paths"]])
    lines.append(markdown_table(["bucket", "count", "top_paths"], gt_rows))
    if risks["training_view_gt_like_text"]["files_scanned"]:
        lines.append("")
        lines.append("## Training View GT-like Text")
        training_view_examples = risks["training_view_gt_like_text"].get("examples") or []
        lines.append(
            markdown_table(
                ["metric", "value"],
                [
                    ["files_scanned", risks["training_view_gt_like_text"]["files_scanned"]],
                    ["matching_lines", risks["training_view_gt_like_text"]["matching_lines"]],
                ],
            )
        )
        if training_view_examples:
            lines.append("")
            lines.append("Examples:")
            for item in training_view_examples[:max_examples]:
                lines.append(f"- `{item}`")
    return "\n".join(lines)


def render_report(report: Dict[str, Any], output_format: str, max_examples: int) -> str:
    if output_format == "json":
        return json_report(report)
    if output_format == "markdown":
        return markdown_report(report, max_examples)
    return (
        "----- JSON -----\n"
        + json_report(report)
        + "\n\n----- MARKDOWN -----\n"
        + markdown_report(report, max_examples)
    )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only evidence schema/statistics audit.")
    parser.add_argument("--root", default=".", help="Repository root to scan. Default: .")
    parser.add_argument("--stdout-only", action="store_true", help="Force stdout output only.")
    parser.add_argument("--dry-run", action="store_true", help="Explicit safety flag; script is read-only either way.")
    parser.add_argument("--format", choices=("json", "markdown", "both"), default="both", help="Output format.")
    parser.add_argument("--max-examples", type=int, default=10, help="Maximum examples per section.")
    parser.add_argument("--include-training-views", action="store_true", help="Scan training_views/diagnosis_input.txt for GT-like text.")
    parser.add_argument("--output", default="", help="Optional output path. Not used when --stdout-only is set.")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.max_examples < 0:
        raise SystemExit("--max-examples must be >= 0")
    report = build_report(args)
    rendered = render_report(report, args.format, args.max_examples)
    if args.output and not args.stdout_only:
        output_path = Path(args.output)
        output_path.write_text(rendered + "\n", encoding="utf-8")
    else:
        sys.stdout.write(rendered)
        if not rendered.endswith("\n"):
            sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
