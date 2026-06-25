#!/usr/bin/env python3
"""Read-only triage for evidence chains without root candidates.

The tool builds or loads evidence chains, focuses on chains whose
root_candidates bucket is empty, classifies the coverage gap, and identifies
diagnostic-safe fallback review candidates without promoting them to roots.
It writes nothing by default and uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple


sys.dont_write_bytecode = True

DIAGNOSTIC_BUCKETS = (
    "baseline_facts",
    "fault_observations",
    "supporting_evidence",
    "propagation_evidence",
    "symptom_evidence",
    "contradicting_evidence",
    "uncertain_evidence",
)

FALLBACK_REVIEW_BUCKETS = (
    "fault_observations",
    "supporting_evidence",
    "propagation_evidence",
    "symptom_evidence",
    "uncertain_evidence",
)

CHAIN_BUCKETS = (
    "baseline_facts",
    "fault_observations",
    "root_candidates",
    "supporting_evidence",
    "propagation_evidence",
    "symptom_evidence",
    "contradicting_evidence",
    "uncertain_evidence",
    "do_not_use_evidence",
)

SENSITIVE_TERMS = (
    "ground_truth",
    "root_cause",
    "gt_",
    "gt_family",
    "gt_subtype",
    "primary_subtype",
    "scenario_tag",
    "fault_type",
    "label",
    "fault_inject",
    "injector",
    "provenance",
    "fault_net_",
    "net_dns_fail",
    "net_link_down",
    "net_route_missing",
    "net_no_default_route",
    "net_no_ipv4_on_iface",
    "net_public_ip_unreachable",
    "net_wifi_auth_fail_wrong_psk",
    "net_wifi_disconnect",
    "net_wrong_default_route",
    "net_link_flap",
    "net_gateway_unreachable",
    "net_packet_loss",
    "net_latency_spike",
    "net_auth_fail",
    "net_wlan_disconnect",
)

SENSITIVE_PATTERNS = (
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    re.compile(r"\b[0-9a-f]{2}(?::[0-9a-f]{2}){5}\b", re.IGNORECASE),
    re.compile(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:callerToken|accessTokenId|ssid|bssid|psk)\b", re.IGNORECASE),
    re.compile(r"\bnet_[a-z0-9_]+\b", re.IGNORECASE),
)

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
    "accepted",
    "artifacts",
    "dataset",
    "dataset_batches",
    "datasets",
    "demo_public_dataset",
    "eval",
    "eval_artifacts",
    "frozen",
    "generated",
    "generated_data",
    "inbox",
    "inbox_net",
    "ledger",
    "test",
    "tools",
    "train",
    "training_views",
    "val",
}

DISALLOWED_FALLBACK_FLAGS = {
    "duplicate_probe",
    "excluded_provenance",
    "generic_noise",
    "gt_label_marker",
    "gt_like_text",
    "historical_noise",
    "injector_marker",
    "provenance",
    "recovery_primary",
    "sensitive_context_redacted",
}

PROVENANCE_TEXT_TERMS = (
    "fault_inject",
    "ground_truth",
    "gt_",
    "injector",
    "label",
    "provenance",
    "scenario_tag",
)

STRONG_SAFE_ANOMALY_TERMS = (
    "auth fail",
    "cannot resolve",
    "disconnect",
    "disconnected",
    "dns",
    "down",
    "dropped",
    "error",
    "fail",
    "failed",
    "failure",
    "latency",
    "missing",
    "name resolution",
    "no route",
    "packet loss",
    "refused",
    "reset",
    "service stopped",
    "timeout",
    "timed out",
    "unreachable",
)

KNOWN_UNSAFE_ANOMALIES = {"", "unknown", "noise", "injector_marker"}
UNKNOWN_VALUES = {"", "unknown", "none", "n/a", "null"}

SAFE_ENUM_KEYS = {
    "bucket",
    "category",
    "primary_category",
    "recommendation",
    "record_type",
    "role",
    "secondary_categories",
    "suggested_review",
    "triage_confidence",
}

PRIMARY_CATEGORIES = (
    "only_provenance_or_labels",
    "only_generic_noise",
    "only_recovery_or_normal",
    "symptom_only",
    "supporting_without_root",
    "uncertain_low_utility",
    "missing_anomaly_type",
    "missing_object",
    "context_missing",
    "threshold_too_strict_candidate",
    "potential_adapter_miss",
    "true_coverage_gap",
    "unknown",
)

RECOMMENDATIONS = (
    "accept_gap",
    "review_adapter_rules",
    "review_role_mapping",
    "review_threshold",
    "needs_more_evidence",
    "manual_review",
)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Triage no-root evidence coverage gaps.")
    parser.add_argument("--root", default=".", help="Repository root. Default: current directory.")
    parser.add_argument("--input", default=None, help="Optional evidence_candidates.jsonl file or directory.")
    parser.add_argument("--chains-jsonl", default=None, help="Optional chain JSONL input from evidence_chain_builder.py.")
    parser.add_argument("--stdout-only", action="store_true", help="Force stdout output only.")
    parser.add_argument("--dry-run", action="store_true", help="Explicit safety flag; this tool is read-only by default.")
    parser.add_argument("--format", choices=("json", "markdown", "both", "jsonl"), default="both")
    parser.add_argument("--max-examples", type=int, default=10)
    parser.add_argument("--limit-cases", type=int, default=None)
    parser.add_argument("--min-utility", type=float, default=0.25)
    parser.add_argument("--redact-sensitive", dest="redact_sensitive", action="store_true", default=True)
    parser.add_argument("--no-redact-sensitive", dest="redact_sensitive", action="store_false")
    parser.add_argument("--output", default=None, help="Optional explicit report output path.")
    return parser.parse_args(argv)


def safe_json_dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
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


def load_chain_builder(root: Path) -> Any:
    return safe_import_module(root / "tools" / "evidence_chain_builder.py", "evidence_chain_builder_for_no_root_triage")


def redact_sensitive_text(text: Any, enabled: bool = True) -> Tuple[str, bool]:
    raw = safe_text(text)
    if not enabled:
        return raw, False
    redacted = raw
    for term in SENSITIVE_TERMS:
        redacted = re.sub(re.escape(term), REDACTION_MARKER, redacted, flags=re.IGNORECASE)
    for pattern in SENSITIVE_PATTERNS:
        redacted = pattern.sub(REDACTION_MARKER, redacted)
    return redacted, redacted != raw


def redact_output_value(value: Any, enabled: bool) -> Any:
    if isinstance(value, dict):
        redacted: Dict[str, Any] = {}
        for key, child in value.items():
            key_text = str(key)
            if key_text in SAFE_ENUM_KEYS:
                redacted[key_text] = child
            else:
                redacted[key_text] = redact_output_value(child, enabled)
        return redacted
    if isinstance(value, list):
        return [redact_output_value(child, enabled) for child in value]
    if isinstance(value, tuple):
        return [redact_output_value(child, enabled) for child in value]
    if isinstance(value, str):
        return redact_sensitive_text(value, enabled)[0]
    return value


def iter_lower_values(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for child in value.values():
            yield from iter_lower_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_lower_values(child)
    elif isinstance(value, str):
        yield value.lower()
    elif value is not None:
        yield str(value).lower()


def contains_redaction_marker(value: Any) -> bool:
    marker = REDACTION_MARKER.lower()
    return any(marker in text for text in iter_lower_values(value))


def has_unredacted_sensitive_text(value: Any) -> bool:
    for text in iter_lower_values(value):
        if REDACTION_MARKER.lower() in text:
            text = text.replace(REDACTION_MARKER.lower(), "")
        if any(term in text for term in SENSITIVE_TERMS):
            return True
        if any(pattern.search(text) for pattern in SENSITIVE_PATTERNS):
            return True
    return False


def has_sensitive_or_provenance_text(value: Any) -> bool:
    if contains_redaction_marker(value):
        return True
    if has_unredacted_sensitive_text(value):
        return True
    return any(any(term in text for term in PROVENANCE_TEXT_TERMS) for text in iter_lower_values(value))


def item_text(item: Dict[str, Any]) -> str:
    parts = [
        item.get("summary"),
        item.get("raw_observation"),
        item.get("key_observation"),
        item.get("context_block"),
        item.get("reason"),
        item.get("why_root_candidate"),
    ]
    return "\n".join(safe_text(part) for part in parts if part)


def item_safety_text(item: Dict[str, Any]) -> str:
    parts = [
        item.get("source"),
        item.get("object"),
        item.get("phase"),
        item.get("anomaly_type"),
        item.get("causal_role"),
        item.get("context_ref"),
        item.get("risk_flags"),
        item_text(item),
    ]
    return "\n".join(safe_text(part) for part in parts if part)


def item_flags(item: Dict[str, Any]) -> set:
    return {str(flag) for flag in item.get("risk_flags") or []}


def item_utility(item: Dict[str, Any]) -> float:
    return numeric(item.get("utility_score"), numeric(item.get("score"), 0.0))


def is_unknown_value(value: Any) -> bool:
    return str(value or "").strip().lower() in UNKNOWN_VALUES


def is_injector_or_provenance_item(item: Dict[str, Any]) -> bool:
    flags = item_flags(item)
    text = item_safety_text(item).lower()
    return (
        item.get("source") == "fault_inject"
        or item.get("anomaly_type") == "injector_marker"
        or item.get("causal_role") == "excluded_provenance"
        or bool(flags & {"injector_marker", "excluded_provenance", "provenance"})
        or any(term in text for term in ("fault_inject", "injector", "provenance"))
    )


def is_gt_or_label_like_item(item: Dict[str, Any]) -> bool:
    flags = item_flags(item)
    return bool(flags & {"gt_label_marker", "gt_like_text"}) or has_unredacted_sensitive_text(item_safety_text(item))


def is_generic_noise_item(item: Dict[str, Any]) -> bool:
    flags = item_flags(item)
    text = item_safety_text(item).lower()
    return (
        item.get("anomaly_type") == "noise"
        or item.get("causal_role") == "noise_candidate"
        or bool(flags & {"generic_noise", "historical_noise"})
        or "duplicate_probe" in text
        or "historical_noise" in text
    )


def is_recovery_like_item(item: Dict[str, Any]) -> bool:
    flags = item_flags(item)
    text = item_safety_text(item).lower()
    return (
        item.get("phase") == "recovery"
        or "recovery_primary" in flags
        or "recovery_primary" in text
        or "recovery-primary" in text
        or "after the fault" in text
        or "recovered" in text
        or "restored" in text
    )


def diagnostic_safety_violations(item: Dict[str, Any]) -> List[str]:
    violations: List[str] = []
    if is_injector_or_provenance_item(item):
        violations.append("origin_marker")
    if is_gt_or_label_like_item(item):
        violations.append("truth_marker")
    if is_generic_noise_item(item):
        violations.append("generic_noise")
    if item.get("usable_for_diagnosis") is False:
        violations.append("non_usable")
    return violations


def is_diagnostic_safe_item(item: Dict[str, Any]) -> bool:
    return not diagnostic_safety_violations(item)


def known_anomaly_type(item: Dict[str, Any]) -> bool:
    anomaly = str(item.get("anomaly_type") or "").strip().lower()
    return anomaly not in KNOWN_UNSAFE_ANOMALIES


def looks_strong_safe_anomaly(item: Dict[str, Any]) -> bool:
    text = item_text(item).lower()
    return bool(text) and any(term in text for term in STRONG_SAFE_ANOMALY_TERMS)


def fallback_utility_floor(min_utility: float) -> float:
    return max(0.0, min(float(min_utility), max(float(min_utility) - 0.05, float(min_utility) * 0.8)))


def fallback_reject_reasons(item: Dict[str, Any], bucket: str, args: argparse.Namespace) -> List[str]:
    reasons: List[str] = []
    flags = item_flags(item)
    if bucket == "do_not_use_evidence":
        reasons.append("do_not_use_bucket")
    if bucket not in FALLBACK_REVIEW_BUCKETS:
        reasons.append("not_fault_effect_bucket")
    reasons.extend(diagnostic_safety_violations(item))
    if flags & DISALLOWED_FALLBACK_FLAGS:
        reasons.append("sensitive_or_origin_flag")
    if has_sensitive_or_provenance_text(item_safety_text(item)):
        reasons.append("sensitive_or_origin_text")
    if is_recovery_like_item(item):
        reasons.append("recovery_or_post_fault_evidence")
    if item_utility(item) < fallback_utility_floor(args.min_utility):
        reasons.append("below_review_utility_floor")
    if not known_anomaly_type(item):
        reasons.append("unknown_anomaly_type")
    if not looks_strong_safe_anomaly(item):
        reasons.append("no_strong_safe_anomaly_terms")
    return sorted(set(reasons))


def is_potential_safe_fallback_item(item: Dict[str, Any], bucket: str, args: argparse.Namespace) -> bool:
    return not fallback_reject_reasons(item, bucket, args)


def is_no_root_chain(chain: Dict[str, Any]) -> bool:
    roots = chain.get("root_candidates")
    return not isinstance(roots, list) or len(roots) == 0


def iter_chain_items(chain: Dict[str, Any], buckets: Sequence[str]) -> Iterator[Tuple[str, Dict[str, Any]]]:
    for bucket in buckets:
        for item in chain.get(bucket) or []:
            if isinstance(item, dict):
                yield bucket, item


def iter_diagnostic_items(chain: Dict[str, Any]) -> Iterator[Tuple[str, Dict[str, Any]]]:
    yield from iter_chain_items(chain, DIAGNOSTIC_BUCKETS)


def iter_do_not_use_items(chain: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    for item in chain.get("do_not_use_evidence") or []:
        if isinstance(item, dict):
            yield item


def bucket_counts_for_chain(chain: Dict[str, Any]) -> Dict[str, int]:
    return {bucket: len(chain.get(bucket) or []) for bucket in CHAIN_BUCKETS}


def fallback_candidate_record(chain: Dict[str, Any], bucket: str, item: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    summary, _redacted = redact_sensitive_text(compact_text(item.get("summary") or item_text(item), 260), args.redact_sensitive)
    if bucket == "supporting_evidence":
        suggested_review = "supporting_to_root_candidate_review"
    elif bucket == "fault_observations":
        suggested_review = "fault_observation_to_root_candidate_review"
    elif bucket == "uncertain_evidence":
        suggested_review = "uncertain_to_root_candidate_review"
    elif is_unknown_value(item.get("anomaly_type")):
        suggested_review = "adapter_anomaly_type_review"
    elif is_unknown_value(item.get("object")):
        suggested_review = "object_inference_review"
    else:
        suggested_review = "threshold_review"
    return {
        "chain_id": chain.get("chain_id"),
        "case_ref": case_ref_for_chain(chain),
        "case_id_redacted": True,
        "case_id": None,
        "case_rel": redact_sensitive_text(str(chain.get("case_rel") or ""), args.redact_sensitive)[0],
        "bucket": bucket,
        "evidence_id": item.get("evidence_id"),
        "source": redact_sensitive_text(str(item.get("source") or "unknown"), args.redact_sensitive)[0],
        "object": redact_sensitive_text(str(item.get("object") or "unknown"), args.redact_sensitive)[0],
        "phase": redact_sensitive_text(str(item.get("phase") or "unknown"), args.redact_sensitive)[0],
        "anomaly_type": redact_sensitive_text(str(item.get("anomaly_type") or "unknown"), args.redact_sensitive)[0],
        "causal_role": redact_sensitive_text(str(item.get("causal_role") or "unknown"), args.redact_sensitive)[0],
        "utility_score": round(item_utility(item), 4),
        "summary": summary,
        "reason": "diagnostic-safe item with known anomaly or strong anomaly terms near/above utility threshold",
        "suggested_review": suggested_review,
        "not_promoted": True,
    }


def collect_potential_safe_fallback_candidates(chain: Dict[str, Any], args: argparse.Namespace) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    for bucket, item in iter_chain_items(chain, FALLBACK_REVIEW_BUCKETS):
        if is_potential_safe_fallback_item(item, bucket, args):
            candidates.append(fallback_candidate_record(chain, bucket, item, args))
    candidates.sort(key=lambda item: (-numeric(item.get("utility_score")), str(item.get("evidence_id") or "")))
    return candidates


def classify_no_root_chain(chain: Dict[str, Any], args: argparse.Namespace, fallback_candidates: Sequence[Dict[str, Any]]) -> str:
    diagnostic_items = [item for _bucket, item in iter_diagnostic_items(chain)]
    do_not_use = list(iter_do_not_use_items(chain))
    active_buckets = {bucket for bucket in DIAGNOSTIC_BUCKETS if chain.get(bucket)}
    candidate_like = [
        item
        for bucket, item in iter_diagnostic_items(chain)
        if bucket in FALLBACK_REVIEW_BUCKETS
    ]

    if fallback_candidates:
        buckets = {str(candidate.get("bucket") or "") for candidate in fallback_candidates}
        if "supporting_evidence" in buckets or "propagation_evidence" in buckets or "fault_observations" in buckets:
            return "supporting_without_root"
        if "uncertain_evidence" in buckets:
            return "uncertain_low_utility"
        return "threshold_too_strict_candidate"
    if not diagnostic_items and do_not_use:
        if any(is_injector_or_provenance_item(item) or is_gt_or_label_like_item(item) for item in do_not_use):
            return "only_provenance_or_labels"
        if all(is_generic_noise_item(item) for item in do_not_use):
            return "only_generic_noise"
        return "true_coverage_gap"
    if not diagnostic_items:
        return "true_coverage_gap"
    if active_buckets and active_buckets <= {"baseline_facts", "contradicting_evidence"}:
        return "only_recovery_or_normal"
    if active_buckets == {"symptom_evidence"}:
        return "symptom_only"
    if active_buckets == {"uncertain_evidence"} and all(item_utility(item) < args.min_utility for item in candidate_like):
        return "uncertain_low_utility"
    if candidate_like and all(not known_anomaly_type(item) for item in candidate_like):
        return "missing_anomaly_type"
    if candidate_like and all(is_unknown_value(item.get("object")) for item in candidate_like):
        return "missing_object"
    if candidate_like and all(item_utility(item) < args.min_utility for item in candidate_like):
        if any(looks_strong_safe_anomaly(item) for item in candidate_like):
            return "threshold_too_strict_candidate"
        return "uncertain_low_utility"
    if do_not_use and any(diagnostic_safety_violations(item) for item in do_not_use):
        return "only_provenance_or_labels"
    if "supporting_evidence" in active_buckets or "propagation_evidence" in active_buckets:
        return "supporting_without_root"
    if candidate_like and any(looks_strong_safe_anomaly(item) for item in candidate_like):
        return "potential_adapter_miss"
    return "unknown"


def recommendation_for_category(category: str, fallback_count: int) -> str:
    if fallback_count:
        return "review_role_mapping"
    if category in {"only_provenance_or_labels", "only_generic_noise", "only_recovery_or_normal", "true_coverage_gap"}:
        return "accept_gap"
    if category in {"missing_anomaly_type", "missing_object"}:
        return "review_adapter_rules"
    if category in {"threshold_too_strict_candidate", "uncertain_low_utility"}:
        return "review_threshold"
    if category == "supporting_without_root":
        return "review_role_mapping"
    if category == "symptom_only":
        return "needs_more_evidence"
    if category == "potential_adapter_miss":
        return "review_adapter_rules"
    return "manual_review"


def chain_safety_flags(chain: Dict[str, Any]) -> List[str]:
    flags: List[str] = []
    for bucket, item in iter_diagnostic_items(chain):
        for violation in diagnostic_safety_violations(item):
            flags.append(f"{bucket}:{violation}")
        if has_unredacted_sensitive_text(item_safety_text(item)):
            flags.append(f"{bucket}:unredacted_sensitive_text")
    return sorted(set(flags))


def case_ref_for_chain(chain: Dict[str, Any]) -> str:
    chain_id = str(chain.get("chain_id") or "")
    match = re.search(r"(\d+)$", chain_id)
    if match:
        return f"CASE_{int(match.group(1)):06d}"
    seed_text = f"{chain.get('case_id') or ''}\n{chain.get('case_rel') or ''}"
    seed = int(hashlib.sha1(seed_text.encode("utf-8", errors="replace")).hexdigest()[:10], 16) % 1000000
    return f"CASE_{seed:06d}"


def compact_safe_item(bucket: str, item: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    role = str(item.get("causal_role") or bucket.replace("_evidence", "").replace("_facts", ""))
    summary = redact_sensitive_text(compact_text(item.get("summary") or item_text(item), 220), args.redact_sensitive)[0]
    return {
        "evidence_id": item.get("evidence_id"),
        "role": role,
        "bucket": bucket,
        "source": redact_sensitive_text(str(item.get("source") or "unknown"), args.redact_sensitive)[0],
        "object": redact_sensitive_text(str(item.get("object") or "unknown"), args.redact_sensitive)[0],
        "phase": redact_sensitive_text(str(item.get("phase") or "unknown"), args.redact_sensitive)[0],
        "anomaly_type": redact_sensitive_text(str(item.get("anomaly_type") or "unknown"), args.redact_sensitive)[0],
        "utility_score": round(item_utility(item), 4),
        "summary": summary,
    }


def top_safe_items(chain: Dict[str, Any], args: argparse.Namespace, limit: int = 5) -> List[Dict[str, Any]]:
    items: List[Tuple[str, Dict[str, Any]]] = [
        (bucket, item)
        for bucket, item in iter_diagnostic_items(chain)
        if is_diagnostic_safe_item(item)
    ]
    items.sort(key=lambda pair: (-item_utility(pair[1]), str(pair[1].get("evidence_id") or "")))
    return [compact_safe_item(bucket, item, args) for bucket, item in items[:limit]]


def top_do_not_use_reasons(chain: Dict[str, Any], args: argparse.Namespace, limit: int = 5) -> List[Dict[str, Any]]:
    reasons = Counter()
    for item in iter_do_not_use_items(chain):
        reason = str(item.get("reason") or item.get("exclusion_reason") or "").strip()
        if not reason:
            flags = sorted(item_flags(item))
            reason = ",".join(flags) if flags else "unspecified_exclusion"
        reason = redact_sensitive_text(reason, args.redact_sensitive)[0]
        reasons[reason] += 1
    return [{"reason": reason, "count": count} for reason, count in reasons.most_common(limit)]


def secondary_categories_for_chain(chain: Dict[str, Any], args: argparse.Namespace) -> List[str]:
    categories: List[str] = []
    if chain.get("do_not_use_evidence") and not any(chain.get(bucket) for bucket in DIAGNOSTIC_BUCKETS):
        categories.append("has_do_not_use_only")
    if "sensitive_context_redacted" in {str(w) for w in chain.get("warnings") or []} or contains_redaction_marker(chain):
        categories.append("has_sensitive_redactions")
    if chain.get("fault_observations"):
        categories.append("has_fault_observations")
    if chain.get("supporting_evidence"):
        categories.append("has_supporting_evidence")
    if chain.get("symptom_evidence"):
        categories.append("has_symptom_evidence")
    if chain.get("uncertain_evidence"):
        categories.append("has_uncertain_evidence")
    if chain.get("contradicting_evidence"):
        categories.append("has_contradictions")
    safe_items = [item for _bucket, item in iter_diagnostic_items(chain) if is_diagnostic_safe_item(item)]
    if any(looks_strong_safe_anomaly(item) for item in safe_items):
        categories.append("has_safe_anomaly_terms")
    if not chain.get("has_context"):
        categories.append("has_no_context")
    if any(item_utility(item) < args.min_utility for item in safe_items):
        categories.append("has_low_utility_safe_items")
    if any(is_unknown_value(item.get("object")) for item in safe_items):
        categories.append("has_unknown_object")
    if any(not known_anomaly_type(item) for item in safe_items):
        categories.append("has_unknown_anomaly_type")
    return sorted(set(categories))


def triage_confidence(primary_category: str, secondary_categories: Sequence[str], fallback_count: int) -> str:
    if primary_category in {"unknown", "potential_adapter_miss"}:
        return "low"
    if fallback_count or "has_low_utility_safe_items" in secondary_categories:
        return "medium"
    return "high"


def no_root_triage_record(chain: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    fallback_candidates = collect_potential_safe_fallback_candidates(chain, args)
    category = classify_no_root_chain(chain, args, fallback_candidates)
    secondary = secondary_categories_for_chain(chain, args)
    recommendation = recommendation_for_category(category, len(fallback_candidates))
    warnings = [str(warning) for warning in chain.get("warnings") or []]
    counts = bucket_counts_for_chain(chain)
    diagnostic_items = [item for _bucket, item in iter_diagnostic_items(chain) if is_diagnostic_safe_item(item)]
    record = {
        "record_type": "no_root_gap_triage",
        "chain_id": chain.get("chain_id"),
        "case_ref": case_ref_for_chain(chain),
        "case_id_redacted": True,
        "case_id": None,
        "case_rel": str(chain.get("case_rel") or ""),
        "primary_category": category,
        "secondary_categories": secondary,
        "triage_confidence": triage_confidence(category, secondary, len(fallback_candidates)),
        "safe_diagnostic_item_count": len(diagnostic_items),
        "do_not_use_item_count": counts.get("do_not_use_evidence", 0),
        "symptom_count": counts.get("symptom_evidence", 0),
        "supporting_count": counts.get("supporting_evidence", 0),
        "uncertain_count": counts.get("uncertain_evidence", 0),
        "contradicting_count": counts.get("contradicting_evidence", 0),
        "chain_score": numeric(chain.get("chain_score"), 0.0),
        "chain_confidence": str(chain.get("chain_confidence") or "low"),
        "category": category,
        "recommendation": recommendation,
        "bucket_counts": counts,
        "top_safe_items": top_safe_items(chain, args),
        "top_do_not_use_reasons": top_do_not_use_reasons(chain, args),
        "potential_safe_fallbacks": fallback_candidates,
        "potential_safe_fallback_review_candidates": fallback_candidates,
        "potential_safe_fallback_count": len(fallback_candidates),
        "safety_flags": chain_safety_flags(chain),
        "warnings": warnings,
        "notes": "Potential safe fallbacks are review candidates only and are not promoted to root candidates.",
        "not_promoted": True,
    }
    return redact_output_value(record, args.redact_sensitive)


def build_no_root_triage_records(chains: Sequence[Dict[str, Any]], args: argparse.Namespace) -> Tuple[List[Dict[str, Any]], int]:
    records: List[Dict[str, Any]] = []
    total_no_root = 0
    for chain in chains:
        if not isinstance(chain, dict) or not is_no_root_chain(chain):
            continue
        total_no_root += 1
        if args.limit_cases is not None and len(records) >= args.limit_cases:
            continue
        records.append(no_root_triage_record(chain, args))
    return records, total_no_root


def chain_example(chain: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    value = {
        "chain_id": chain.get("chain_id"),
        "case_ref": case_ref_for_chain(chain),
        "case_id_redacted": True,
        "case_id": None,
        "case_rel": str(chain.get("case_rel") or ""),
        "chain_score": numeric(chain.get("chain_score"), 0.0),
        "chain_confidence": str(chain.get("chain_confidence") or "low"),
        "bucket_counts": bucket_counts_for_chain(chain),
        "warnings": [str(warning) for warning in chain.get("warnings") or []],
    }
    return redact_output_value(value, args.redact_sensitive)


def read_chains_jsonl(path: Path, args: argparse.Namespace) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    chains: List[Dict[str, Any]] = []
    parse_errors: List[Dict[str, Any]] = []
    text = read_text_lossy(path)
    for line_no, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            if len(parse_errors) < args.max_examples:
                parse_errors.append(
                    {
                        "file": str(path),
                        "line": line_no,
                        "error": str(exc),
                        "text": compact_text(line, 220),
                    }
                )
            continue
        if isinstance(value, dict):
            chains.append(value)
        elif len(parse_errors) < args.max_examples:
            parse_errors.append(
                {
                    "file": str(path),
                    "line": line_no,
                    "error": "JSONL row is not an object",
                    "text": compact_text(value, 220),
                }
            )
    return chains, redact_output_value(parse_errors, args.redact_sensitive)


def build_chains_with_builder(args: argparse.Namespace) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
    root = Path(args.root).resolve()
    builder = load_chain_builder(root)
    builder_argv = [
        "--root",
        str(root),
        "--format",
        "json",
        "--max-examples",
        str(max(0, args.max_examples)),
        "--min-utility",
        str(args.min_utility),
    ]
    if args.input:
        builder_argv.extend(["--input", args.input])
    if args.dry_run:
        builder_argv.append("--dry-run")
    if args.redact_sensitive:
        builder_argv.append("--redact-sensitive")
    else:
        builder_argv.append("--no-redact-sensitive")
    builder_args = builder.parse_args(builder_argv)
    source_summary, chains = builder.run(builder_args)
    parse_errors = list((source_summary.get("examples") or {}).get("parse_errors") or [])
    return source_summary, chains, redact_output_value(parse_errors, args.redact_sensitive)


def load_or_build_chains(args: argparse.Namespace) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]], str]:
    root = Path(args.root).resolve()
    if args.chains_jsonl:
        path = Path(args.chains_jsonl)
        if not path.is_absolute():
            path = root / path
        chains, parse_errors = read_chains_jsonl(path, args)
        source_summary = {
            "counts": {
                "chains_jsonl_rows": len(chains),
                "parse_errors": len(parse_errors),
            },
            "source_path": str(path),
        }
        return source_summary, chains, parse_errors, "chains_jsonl"
    source_summary, chains, parse_errors = build_chains_with_builder(args)
    return source_summary, chains, parse_errors, "chain_builder"


def compute_bucket_distribution(records: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    counts = Counter()
    for record in records:
        for bucket, value in (record.get("bucket_counts") or {}).items():
            counts[str(bucket)] += int(value or 0)
    return {bucket: counts.get(bucket, 0) for bucket in CHAIN_BUCKETS}


def output_free_text_for_sensitive_scan(record: Dict[str, Any]) -> Dict[str, Any]:
    """Return only human-output fields where unredacted labels would leak.

    Controlled enums intentionally contain words such as "labels"; those are
    category names, not evidence text. Safety failures here should come from
    examples, notes, or fallback review text that would be shown to a reviewer.
    """
    return {
        "case_rel": record.get("case_rel"),
        "top_safe_items": record.get("top_safe_items"),
        "top_do_not_use_reasons": record.get("top_do_not_use_reasons"),
        "potential_safe_fallbacks": record.get("potential_safe_fallbacks"),
        "potential_safe_fallback_review_candidates": record.get("potential_safe_fallback_review_candidates"),
        "notes": record.get("notes"),
    }


def compute_safety_summary(chains: Sequence[Dict[str, Any]], records: Sequence[Dict[str, Any]], args: argparse.Namespace) -> Dict[str, Any]:
    counts = Counter(
        {
            "injector_or_provenance_in_diagnostic_buckets": 0,
            "gt_or_label_like_in_diagnostic_buckets": 0,
            "generic_noise_in_diagnostic_buckets": 0,
            "unredacted_sensitive_context_in_diagnostic_buckets": 0,
            "fallback_candidates_from_do_not_use": 0,
            "fallback_candidates_sensitive_or_provenance": 0,
            "fallback_candidates_promoted": 0,
            "injector_in_potential_fallbacks": 0,
            "gt_like_in_potential_fallbacks": 0,
            "unredacted_sensitive_examples": 0,
        }
    )
    for chain in chains:
        if not isinstance(chain, dict) or not is_no_root_chain(chain):
            continue
        for _bucket, item in iter_diagnostic_items(chain):
            if is_injector_or_provenance_item(item):
                counts["injector_or_provenance_in_diagnostic_buckets"] += 1
            if is_gt_or_label_like_item(item):
                counts["gt_or_label_like_in_diagnostic_buckets"] += 1
            if is_generic_noise_item(item):
                counts["generic_noise_in_diagnostic_buckets"] += 1
            if has_unredacted_sensitive_text(item_safety_text(item)):
                counts["unredacted_sensitive_context_in_diagnostic_buckets"] += 1
    for record in records:
        if has_unredacted_sensitive_text(output_free_text_for_sensitive_scan(record)):
            counts["unredacted_sensitive_examples"] += 1
        for candidate in record.get("potential_safe_fallbacks") or record.get("potential_safe_fallback_review_candidates") or []:
            if candidate.get("bucket") == "do_not_use_evidence":
                counts["fallback_candidates_from_do_not_use"] += 1
            candidate_text = item_text(candidate) or safe_text(candidate)
            if is_injector_or_provenance_item(candidate) or has_sensitive_or_provenance_text(candidate_text) or has_sensitive_or_provenance_text(candidate.get("risk_flags") or []):
                counts["fallback_candidates_sensitive_or_provenance"] += 1
                counts["injector_in_potential_fallbacks"] += 1
            if is_gt_or_label_like_item(candidate):
                counts["gt_like_in_potential_fallbacks"] += 1
            if candidate.get("promoted") or candidate.get("root_candidate"):
                counts["fallback_candidates_promoted"] += 1
    hard_failures = (
        counts["fallback_candidates_from_do_not_use"]
        + counts["fallback_candidates_sensitive_or_provenance"]
        + counts["fallback_candidates_promoted"]
        + counts["injector_in_potential_fallbacks"]
        + counts["gt_like_in_potential_fallbacks"]
        + counts["unredacted_sensitive_examples"]
    )
    return {
        "output_redacted": bool(args.redact_sensitive),
        "hard_safety_preserved": hard_failures == 0,
        "note": "Fallback review candidates are never promoted and never selected from do_not_use_evidence.",
        **dict(counts),
    }


def build_summary(
    source_summary: Dict[str, Any],
    chains: Sequence[Dict[str, Any]],
    records: Sequence[Dict[str, Any]],
    total_no_root: int,
    parse_errors: Sequence[Dict[str, Any]],
    input_mode: str,
    args: argparse.Namespace,
) -> Dict[str, Any]:
    categories = Counter(str(record.get("category") or "unknown") for record in records)
    recommendations = Counter(str(record.get("recommendation") or "unknown") for record in records)
    fallback_candidates = [
        candidate
        for record in records
        for candidate in (record.get("potential_safe_fallbacks") or record.get("potential_safe_fallback_review_candidates") or [])
    ]
    no_root_chains = [chain for chain in chains if isinstance(chain, dict) and is_no_root_chain(chain)]
    source_counts = source_summary.get("counts") if isinstance(source_summary, dict) else {}
    safe_diagnostic_total = sum(int(record.get("safe_diagnostic_item_count") or 0) for record in records)
    do_not_use_total = sum(int(record.get("do_not_use_item_count") or 0) for record in records)
    safety = compute_safety_summary(chains, records, args)
    status = "PASS"
    if (
        safety.get("injector_in_potential_fallbacks")
        or safety.get("gt_like_in_potential_fallbacks")
        or safety.get("unredacted_sensitive_examples")
        or not safety.get("hard_safety_preserved")
    ):
        status = "FAIL"
    elif fallback_candidates or categories.get("unknown") or recommendations.get("manual_review"):
        status = "WARN"
    summary = {
        "root": str(Path(args.root).resolve()),
        "dry_run": bool(args.dry_run),
        "status": status,
        "input_mode": input_mode,
        "read_only": True,
        "redact_sensitive": bool(args.redact_sensitive),
        "min_utility": args.min_utility,
        "fallback_review_utility_floor": fallback_utility_floor(args.min_utility),
        "counts": {
            "chains": len(chains),
            "no_root_chains": total_no_root,
            "triaged_cases": len(records),
            "safe_diagnostic_items_in_no_root": safe_diagnostic_total,
            "do_not_use_items_in_no_root": do_not_use_total,
            "potential_safe_fallbacks": len(fallback_candidates),
            "chains_loaded": len(chains),
            "chains_with_root_candidates": sum(1 for chain in chains if isinstance(chain, dict) and not is_no_root_chain(chain)),
            "no_root_chains_total": total_no_root,
            "no_root_records_emitted": len(records),
            "potential_safe_fallback_review_candidates": len(fallback_candidates),
            "parse_errors": len(parse_errors),
        },
        "source_counts": dict(source_counts or {}),
        "category_distribution": {key: categories.get(key, 0) for key in PRIMARY_CATEGORIES},
        "recommendation_distribution": {key: recommendations.get(key, 0) for key in RECOMMENDATIONS},
        "bucket_distribution": compute_bucket_distribution(records),
        "safety": safety,
        "examples": {
            "no_root_cases": [chain_example(chain, args) for chain in no_root_chains[: args.max_examples]],
            "potential_safe_fallback_review_candidates": fallback_candidates[: args.max_examples],
            "safety_blocked": safety_blocked_examples(no_root_chains, args),
            "parse_errors": list(parse_errors)[: args.max_examples],
            **{
                recommendation: [
                    record
                    for record in records
                    if record.get("recommendation") == recommendation
                ][: args.max_examples]
                for recommendation in RECOMMENDATIONS
            },
        },
    }
    return redact_output_value(summary, args.redact_sensitive)


def safety_blocked_examples(chains: Sequence[Dict[str, Any]], args: argparse.Namespace) -> List[Dict[str, Any]]:
    examples: List[Dict[str, Any]] = []
    for chain in chains:
        for bucket, item in iter_diagnostic_items(chain):
            reasons = fallback_reject_reasons(item, bucket, args)
            safety_reasons = [
                reason
                for reason in reasons
                if reason
                in {
                    "origin_marker",
                    "truth_marker",
                    "generic_noise",
                    "non_usable",
                    "sensitive_or_origin_flag",
                    "sensitive_or_origin_text",
                    "recovery_or_post_fault_evidence",
                }
            ]
            if not safety_reasons:
                continue
            examples.append(
                redact_output_value(
                    {
                        "chain_id": chain.get("chain_id"),
                        "case_ref": case_ref_for_chain(chain),
                        "case_id_redacted": True,
                        "case_id": None,
                        "bucket": bucket,
                        "evidence_id": item.get("evidence_id"),
                        "blocked_reasons": safety_reasons,
                        "summary": compact_text(item.get("summary") or item_text(item), 220),
                    },
                    args.redact_sensitive,
                )
            )
            if len(examples) >= args.max_examples:
                return examples
    return examples


def markdown_table(mapping: Dict[str, Any], key_title: str = "key") -> List[str]:
    lines = [f"| {key_title} | value |", "|---|---:|"]
    if not mapping:
        lines.append("| none | 0 |")
        return lines
    for key, value in mapping.items():
        lines.append(f"| `{key}` | {value} |")
    return lines


def print_json_summary(summary: Dict[str, Any]) -> str:
    return json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True)


def print_markdown_summary(summary: Dict[str, Any]) -> str:
    lines: List[str] = [
        "# No-root Evidence Coverage Gap Triage Report",
        "",
        "This triage tool is read-only by default and does not modify existing evidence files.",
        "Potential safe fallbacks are review candidates, not automatic root candidates.",
        "Injector/provenance and label-like evidence must remain excluded from diagnostic evidence.",
        "No-root cases are coverage gaps unless safe evidence exists for future rule review.",
        "",
        "## Status",
        f"- `status`: {summary.get('status')}",
        f"- `input_mode`: {summary.get('input_mode')}",
        "",
        "## Counts",
    ]
    lines.extend(markdown_table(summary.get("counts") or {}, "metric"))
    lines.extend(["", "## Category Distribution"])
    lines.extend(markdown_table(summary.get("category_distribution") or {}, "category"))
    lines.extend(["", "## Recommendation Distribution"])
    lines.extend(markdown_table(summary.get("recommendation_distribution") or {}, "recommendation"))
    lines.extend(["", "## Bucket Distribution"])
    lines.extend(markdown_table(summary.get("bucket_distribution") or {}, "bucket"))
    lines.extend(["", "## Safety"])
    lines.extend(markdown_table(summary.get("safety") or {}, "check"))
    lines.extend(["", "## Examples"])
    examples = summary.get("examples") or {}
    for title, key in (
        ("No-root cases", "no_root_cases"),
        ("Potential safe fallback review candidates", "potential_safe_fallback_review_candidates"),
        ("Safety-blocked evidence", "safety_blocked"),
        ("Accept gap examples", "accept_gap"),
        ("Adapter-rule review examples", "review_adapter_rules"),
        ("Role-mapping review examples", "review_role_mapping"),
        ("Threshold review examples", "review_threshold"),
        ("Needs-more-evidence examples", "needs_more_evidence"),
        ("Manual review examples", "manual_review"),
        ("Parse errors", "parse_errors"),
    ):
        lines.append("")
        lines.append(f"### {title}")
        values = examples.get(key) or []
        if not values:
            lines.append("- none")
        else:
            for value in values:
                lines.append(f"- `{compact_text(value, 420)}`")
    return "\n".join(lines).rstrip() + "\n"


def print_jsonl_records(records: Sequence[Dict[str, Any]]) -> str:
    return "\n".join(safe_json_dump(record) for record in records) + ("\n" if records else "")


def render_output(summary: Dict[str, Any], records: Sequence[Dict[str, Any]], args: argparse.Namespace) -> str:
    if args.format == "json":
        return print_json_summary(summary) + "\n"
    if args.format == "markdown":
        return print_markdown_summary(summary)
    if args.format == "jsonl":
        return print_jsonl_records(records)
    return "----- JSON -----\n" + print_json_summary(summary) + "\n\n----- MARKDOWN -----\n" + print_markdown_summary(summary)


def validate_output_path(output_path: Path, root: Path) -> None:
    resolved = output_path.resolve()
    if resolved.exists():
        raise ValueError(f"refusing to overwrite existing output path: {resolved}")
    if not resolved.parent.exists():
        raise ValueError(f"refusing output path with missing parent directory: {resolved.parent}")
    name = resolved.name.lower()
    if name in PROTECTED_OUTPUT_FILENAMES or "ledger" in name:
        raise ValueError(f"refusing protected output filename: {resolved}")
    parts = [part.lower() for part in resolved.parts]
    if any(part in PROTECTED_OUTPUT_DIRS for part in parts):
        raise ValueError(f"refusing output path under protected directory: {resolved}")
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"refusing output path outside repository root: {resolved}") from exc


def run(args: argparse.Namespace) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    source_summary, chains, parse_errors, input_mode = load_or_build_chains(args)
    records, total_no_root = build_no_root_triage_records(chains, args)
    summary = build_summary(source_summary, chains, records, total_no_root, parse_errors, input_mode, args)
    return summary, records


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    root = Path(args.root).resolve()
    try:
        summary, records = run(args)
    except (OSError, RuntimeError, ValueError) as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 2
    output = render_output(summary, records, args)
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
    return 1 if summary.get("status") == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
