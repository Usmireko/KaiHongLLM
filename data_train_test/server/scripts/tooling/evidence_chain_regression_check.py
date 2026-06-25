#!/usr/bin/env python3
"""Read-only regression and coverage checker for evidence chains.

The checker builds or loads evidence chains, validates hard safety invariants,
and reports case coverage, no-root reasons, do-not-use distribution, and prompt
payload readiness. It writes nothing by default and uses only the Python
standard library.
"""

from __future__ import annotations

import argparse
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
    "root_candidates",
    "supporting_evidence",
    "propagation_evidence",
    "symptom_evidence",
    "contradicting_evidence",
    "uncertain_evidence",
)

CHAIN_BUCKETS = DIAGNOSTIC_BUCKETS + ("do_not_use_evidence",)

HARD_SAFETY_KEYS = (
    "injector_in_diagnostic_buckets",
    "gt_like_in_diagnostic_buckets",
    "unredacted_sensitive_context",
    "recovery_primary_as_root_candidate",
    "generic_noise_as_diagnostic",
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
PROMPT_PAYLOAD_CHAR_BUDGET = 8000
PROMPT_REQUIRED_FIELDS = {
    "root_cause",
    "root_object",
    "evidence_used",
    "evidence_roles",
    "alternatives",
    "uncertainty",
}

ANOMALY_TERMS = (
    "fail",
    "failed",
    "error",
    "timeout",
    "unreachable",
    "disconnect",
    "down",
    "missing",
    "no route",
    "dns",
    "packet loss",
    "service stopped",
)

VAGUE_EXCLUSION_REASONS = {
    "",
    "marked non-usable for diagnosis",
    "not selected as diagnostic evidence",
    "non-usable",
    "unknown",
}

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
    parser = argparse.ArgumentParser(description="Validate evidence-chain regression and coverage.")
    parser.add_argument("--root", default=".", help="Repository root. Default: current directory.")
    parser.add_argument("--input", default=None, help="Optional evidence_candidates.jsonl file or directory.")
    parser.add_argument("--chains-jsonl", default=None, help="Optional chain JSONL input from evidence_chain_builder.py.")
    parser.add_argument("--blocks-jsonl", default=None, help="Optional normalized Evidence Block JSONL input.")
    parser.add_argument("--expanded-jsonl", default=None, help="Optional expanded evidence block JSONL input.")
    parser.add_argument("--stdout-only", action="store_true", help="Force stdout output only.")
    parser.add_argument("--dry-run", action="store_true", help="Explicit safety flag; this checker is read-only.")
    parser.add_argument("--format", choices=("json", "markdown", "both"), default="both")
    parser.add_argument("--max-examples", type=int, default=10)
    parser.add_argument("--min-chain-score", type=float, default=0.5)
    parser.add_argument("--min-root-candidate-score", type=float, default=0.25)
    parser.add_argument("--max-do-not-use-ratio", type=float, default=0.6)
    parser.add_argument("--max-no-root-ratio", type=float, default=0.3)
    parser.add_argument("--include-case-examples", action="store_true")
    parser.add_argument(
        "--strict-prompt-readiness",
        action="store_true",
        help="Treat redaction, missing support, and low confidence as prompt-readiness blockers.",
    )
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


def compact_text(value: Any, limit: int = 180) -> str:
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


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return round(max(low, min(high, value)), 4)


def safe_import_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to import module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_chain_builder(root: Path) -> Any:
    return safe_import_module(root / "tools" / "evidence_chain_builder.py", "evidence_chain_builder_for_regression_check")


def read_text_lossy(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def load_chains_from_jsonl(path: Path, max_examples: int = 10) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    chains: List[Dict[str, Any]] = []
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
                        "text": compact_text(line, 220),
                    }
                )
            continue
        if isinstance(value, dict):
            chains.append(value)
        elif len(parse_errors) < max_examples:
            parse_errors.append(
                {
                    "file": str(path),
                    "line": line_no,
                    "error": "JSONL row is not an object",
                    "text": compact_text(value, 220),
                }
            )
    return chains, parse_errors


def build_chains_from_root(root: Path, input_path: Optional[str], args: argparse.Namespace) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    builder = load_chain_builder(root)
    builder_argv = [
        "--root",
        str(root),
        "--dry-run",
        "--stdout-only",
        "--format",
        "json",
        "--max-examples",
        str(args.max_examples),
        "--min-utility",
        str(args.min_root_candidate_score),
    ]
    if input_path:
        builder_argv.extend(["--input", input_path])
    if args.blocks_jsonl:
        builder_argv.extend(["--blocks-jsonl", args.blocks_jsonl])
    if args.expanded_jsonl:
        builder_argv.extend(["--expanded-jsonl", args.expanded_jsonl])
    if args.redact_sensitive:
        builder_argv.append("--redact-sensitive")
    else:
        builder_argv.append("--no-redact-sensitive")
    builder_args = builder.parse_args(builder_argv)
    summary, chains = builder.run(builder_args)
    return chains, summary


def load_or_build_chains(args: argparse.Namespace) -> Tuple[List[Dict[str, Any]], Dict[str, Any], List[Dict[str, Any]]]:
    root = Path(args.root).resolve()
    if args.chains_jsonl:
        path = Path(args.chains_jsonl)
        if not path.is_absolute():
            path = root / path
        chains, parse_errors = load_chains_from_jsonl(path, args.max_examples)
        source_summary = {
            "root": str(root),
            "counts": {
                "evidence_files": 0,
                "input_rows": len(chains),
                "normalized_blocks": 0,
                "expanded_blocks": 0,
                "parse_errors": len(parse_errors),
            },
            "examples": {"parse_errors": parse_errors},
        }
        return chains, source_summary, parse_errors
    chains, source_summary = build_chains_from_root(root, args.input, args)
    parse_errors = list((source_summary.get("examples") or {}).get("parse_errors") or [])
    return chains, source_summary, parse_errors


def iter_chain_items(chain: Dict[str, Any], diagnostic_only: bool = False) -> Iterator[Tuple[str, Dict[str, Any]]]:
    buckets = DIAGNOSTIC_BUCKETS if diagnostic_only else CHAIN_BUCKETS
    for bucket in buckets:
        for item in chain.get(bucket) or []:
            if isinstance(item, dict):
                yield bucket, item


def iter_diagnostic_items(chain: Dict[str, Any]) -> Iterator[Tuple[str, Dict[str, Any]]]:
    yield from iter_chain_items(chain, diagnostic_only=True)


def iter_do_not_use_items(chain: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    for item in chain.get("do_not_use_evidence") or []:
        if isinstance(item, dict):
            yield item


def lower_values(value: Any, skip_keys: Sequence[str] = ()) -> Iterator[str]:
    skip = {key.lower() for key in skip_keys}
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in skip:
                continue
            yield from lower_values(child, skip_keys=skip_keys)
    elif isinstance(value, list):
        for child in value:
            yield from lower_values(child, skip_keys=skip_keys)
    elif isinstance(value, str):
        if value == REDACTION_MARKER:
            return
        yield value.lower()
    elif value is not None:
        yield str(value).lower()


def is_sensitive_text(text: str) -> bool:
    lower = text.lower()
    if REDACTION_MARKER.lower() in lower:
        lower = lower.replace(REDACTION_MARKER.lower(), "")
    if any(term in lower for term in SENSITIVE_TERMS):
        return True
    return any(pattern.search(text) for pattern in SENSITIVE_PATTERNS)


def has_unredacted_sensitive_text(value: Any, skip_keys: Sequence[str] = ()) -> bool:
    for text in lower_values(value, skip_keys=skip_keys):
        if is_sensitive_text(text):
            return True
    return False


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


def item_flags(item: Dict[str, Any]) -> set:
    return {str(flag) for flag in (item.get("risk_flags") or [])}


def is_injector_item(item: Dict[str, Any]) -> bool:
    flags = item_flags(item)
    text = item_text(item).lower()
    return (
        item.get("source") == "fault_inject"
        or item.get("anomaly_type") == "injector_marker"
        or item.get("causal_role") == "excluded_provenance"
        or "injector_marker" in flags
        or "fault_inject" in text
        or "injector" in text
    )


def is_gt_like_item(item: Dict[str, Any]) -> bool:
    flags = item_flags(item)
    return "gt_label_marker" in flags or has_unredacted_sensitive_text(item_text(item))


def is_generic_noise_item(item: Dict[str, Any]) -> bool:
    flags = item_flags(item)
    text = item_text(item).lower()
    return (
        item.get("anomaly_type") == "noise"
        or item.get("causal_role") == "noise_candidate"
        or "generic_noise" in flags
        or "historical_noise" in text
        or "duplicate_probe" in text
    )


def is_recovery_primary_item(item: Dict[str, Any]) -> bool:
    flags = item_flags(item)
    text = item_text(item).lower()
    return (
        "recovery_primary" in flags
        or (item.get("phase") == "recovery" and "legacy_primary" in flags)
        or "recovery_primary" in text
        or "recovery-primary" in text
    )


def is_recovery_primary_root(item: Dict[str, Any]) -> bool:
    text = item_text(item).lower()
    return is_recovery_primary_item(item) or item.get("phase") == "recovery" or any(
        term in text for term in ("recovery", "recovered", "restored", "after the fault", "post")
    )


def validate_hard_safety(chains: Sequence[Dict[str, Any]], max_examples: int = 10) -> Tuple[Dict[str, int], List[Dict[str, Any]]]:
    counts = Counter({key: 0 for key in HARD_SAFETY_KEYS})
    examples: List[Dict[str, Any]] = []
    for chain in chains:
        for bucket, item in iter_diagnostic_items(chain):
            item_violations: List[str] = []
            if is_injector_item(item):
                counts["injector_in_diagnostic_buckets"] += 1
                item_violations.append("injector_in_diagnostic_buckets")
            if is_gt_like_item(item):
                counts["gt_like_in_diagnostic_buckets"] += 1
                item_violations.append("gt_like_in_diagnostic_buckets")
            if has_unredacted_sensitive_text(item_text(item)):
                counts["unredacted_sensitive_context"] += 1
                item_violations.append("unredacted_sensitive_context")
            if (bucket == "root_candidates" and is_recovery_primary_root(item)) or (
                bucket != "root_candidates" and is_recovery_primary_item(item)
            ):
                counts["recovery_primary_as_root_candidate"] += 1
                item_violations.append("recovery_primary_as_root_candidate")
            if is_generic_noise_item(item):
                counts["generic_noise_as_diagnostic"] += 1
                item_violations.append("generic_noise_as_diagnostic")
            if item_violations and len(examples) < max_examples:
                examples.append(
                    {
                        "chain_id": chain.get("chain_id"),
                        "case_id": chain.get("case_id"),
                        "bucket": bucket,
                        "evidence_id": item.get("evidence_id"),
                        "violations": item_violations,
                        "summary": compact_text(item.get("summary") or item_text(item), 220),
                    }
                )
    return {key: counts.get(key, 0) for key in HARD_SAFETY_KEYS}, examples


def compute_bucket_counts(chains: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    counts = Counter()
    for chain in chains:
        for bucket in CHAIN_BUCKETS:
            counts[bucket] += len(chain.get(bucket) or [])
    return {bucket: counts.get(bucket, 0) for bucket in CHAIN_BUCKETS}


def compute_confidence_distribution(chains: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    counts = Counter(str(chain.get("chain_confidence") or "low") for chain in chains)
    return {"high": counts.get("high", 0), "medium": counts.get("medium", 0), "low": counts.get("low", 0)}


def percentile(values: Sequence[float], p: float) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    if len(sorted_values) == 1:
        return round(sorted_values[0], 4)
    rank = (len(sorted_values) - 1) * p
    lower = int(rank)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = rank - lower
    return round(sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight, 4)


def compute_score_stats(values: Sequence[float]) -> Dict[str, float]:
    clean = [float(value) for value in values if value is not None]
    if not clean:
        return {"min": 0.0, "p50": 0.0, "p90": 0.0, "max": 0.0, "average": 0.0}
    return {
        "min": round(min(clean), 4),
        "p50": percentile(clean, 0.5),
        "p90": percentile(clean, 0.9),
        "max": round(max(clean), 4),
        "average": round(sum(clean) / len(clean), 4),
    }


def root_candidate_score(item: Dict[str, Any]) -> float:
    return numeric(item.get("score"), numeric(item.get("utility_score"), 0.0))


def evidence_id_map(chain: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    mapped: Dict[str, Dict[str, Any]] = {}
    for bucket, item in iter_chain_items(chain):
        evidence_id = str(item.get("evidence_id") or "")
        if evidence_id and evidence_id not in mapped:
            mapped[evidence_id] = item
    return mapped


def root_candidate_distributions(chains: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    source = Counter()
    obj = Counter()
    anomaly = Counter()
    for chain in chains:
        by_id = evidence_id_map(chain)
        for candidate in chain.get("root_candidates") or []:
            backing = by_id.get(str(candidate.get("evidence_id") or ""), {})
            source[str(candidate.get("source") or backing.get("source") or "unknown")] += 1
            obj[str(candidate.get("object") or backing.get("object") or "unknown")] += 1
            anomaly[str(candidate.get("anomaly_type") or backing.get("anomaly_type") or "unknown")] += 1
    return {
        "source": dict(source.most_common()),
        "object": dict(obj.most_common()),
        "anomaly_type": dict(anomaly.most_common()),
    }


def chain_example(chain: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "chain_id": chain.get("chain_id"),
        "case_id": chain.get("case_id"),
        "case_rel": chain.get("case_rel"),
        "chain_score": chain.get("chain_score"),
        "chain_confidence": chain.get("chain_confidence"),
        "root_candidates": len(chain.get("root_candidates") or []),
        "supporting_evidence": len(chain.get("supporting_evidence") or []),
        "do_not_use_evidence": len(chain.get("do_not_use_evidence") or []),
        "warnings": chain.get("warnings") or [],
    }


def all_utilities(items: Sequence[Dict[str, Any]]) -> List[float]:
    return [numeric(item.get("utility_score"), numeric(item.get("score"), 0.0)) for item in items]


def analyze_no_root_chain(chain: Dict[str, Any], args: argparse.Namespace) -> str:
    diagnostic_items = [item for _bucket, item in iter_diagnostic_items(chain)]
    do_not_use = list(iter_do_not_use_items(chain))
    symptoms = chain.get("symptom_evidence") or []
    contradictions = chain.get("contradicting_evidence") or []
    uncertain = chain.get("uncertain_evidence") or []
    faults = chain.get("fault_observations") or []

    if not diagnostic_items and do_not_use:
        return "only_do_not_use"
    if symptoms and not any(chain.get(bucket) for bucket in ("fault_observations", "supporting_evidence", "uncertain_evidence")):
        return "only_symptoms"
    if contradictions and not any(chain.get(bucket) for bucket in ("fault_observations", "supporting_evidence", "uncertain_evidence", "symptom_evidence")):
        return "only_contradictions"
    if uncertain and all(value < args.min_root_candidate_score for value in all_utilities(uncertain)):
        return "only_uncertain_low_utility"
    if faults and any(str(item.get("anomaly_type") or "unknown") in ("", "unknown") for item in faults):
        return "missing_anomaly_type"
    if faults and any(str(item.get("object") or "unknown") in ("", "unknown") for item in faults):
        return "missing_object"
    candidate_like = faults + list(chain.get("supporting_evidence") or []) + uncertain
    if candidate_like and all(value < args.min_root_candidate_score for value in all_utilities(candidate_like)):
        return "below_min_utility"
    if any(is_injector_item(item) or is_generic_noise_item(item) or is_gt_like_item(item) for item in do_not_use):
        return "safety_filtered"
    if do_not_use and not diagnostic_items:
        return "all_evidence_excluded"
    return "unknown"


def analyze_no_root(chains: Sequence[Dict[str, Any]], args: argparse.Namespace) -> Dict[str, Any]:
    reasons = Counter()
    examples: List[Dict[str, Any]] = []
    for chain in chains:
        if chain.get("root_candidates"):
            continue
        reason = analyze_no_root_chain(chain, args)
        reasons[reason] += 1
        if len(examples) < args.max_examples:
            example = chain_example(chain)
            example["reason"] = reason
            examples.append(example)
    return {"by_reason": dict(reasons.most_common()), "examples": examples}


def do_not_use_reason(item: Dict[str, Any]) -> str:
    reason = str(item.get("reason") or item.get("exclusion_reason") or "").strip()
    if reason:
        return reason
    flags = item_flags(item)
    if "injector_marker" in flags:
        return "injector/provenance marker is not diagnostic evidence"
    if "generic_noise" in flags:
        return "generic/noise evidence is excluded by default"
    if "gt_label_marker" in flags:
        return "GT/label-like marker is not diagnostic evidence"
    if "recovery_primary" in flags:
        return "recovery-primary evidence was demoted"
    return "unknown"


def looks_anomaly_like(text: str) -> bool:
    lower = text.lower()
    return any(term in lower for term in ANOMALY_TERMS)


def reason_is_vague(reason: str) -> bool:
    lower = reason.lower().strip()
    return lower in VAGUE_EXCLUSION_REASONS or len(lower) < 8


def analyze_do_not_use(chains: Sequence[Dict[str, Any]], args: argparse.Namespace) -> Dict[str, Any]:
    reasons = Counter()
    flags = Counter()
    injector_count = 0
    generic_count = 0
    gt_like_count = 0
    recovery_primary_count = 0
    unknown_reason_count = 0
    overfiltered: List[Dict[str, Any]] = []
    for chain in chains:
        for item in iter_do_not_use_items(chain):
            reason = do_not_use_reason(item)
            reasons[reason] += 1
            if reason == "unknown":
                unknown_reason_count += 1
            for flag in item_flags(item):
                flags[flag] += 1
            if is_injector_item(item):
                injector_count += 1
            if is_generic_noise_item(item):
                generic_count += 1
            if is_gt_like_item(item) or "gt_like_text" in item_flags(item) or "gt_label_marker" in item_flags(item):
                gt_like_count += 1
            if "recovery_primary" in item_flags(item):
                recovery_primary_count += 1
            text = item_text(item)
            if (
                not is_injector_item(item)
                and not is_gt_like_item(item)
                and not is_generic_noise_item(item)
                and looks_anomaly_like(text)
                and reason_is_vague(reason)
                and len(overfiltered) < args.max_examples
            ):
                overfiltered.append(
                    {
                        "chain_id": chain.get("chain_id"),
                        "case_id": chain.get("case_id"),
                        "evidence_id": item.get("evidence_id"),
                        "reason": reason,
                        "risk_flags": sorted(item_flags(item)),
                        "summary": compact_text(item.get("summary") or text, 220),
                    }
                )
    return {
        "by_reason": dict(reasons.most_common()),
        "by_risk_flag": dict(flags.most_common()),
        "injector_provenance_count": injector_count,
        "generic_noise_count": generic_count,
        "gt_like_count": gt_like_count,
        "recovery_primary_demoted_count": recovery_primary_count,
        "unknown_exclusion_reason_count": unknown_reason_count,
        "potentially_overfiltered_count": len(overfiltered),
        "examples": overfiltered,
    }


def payload_bucket_items(payload: Dict[str, Any], bucket: str) -> List[Dict[str, Any]]:
    evidence_chain = payload.get("evidence_chain") if isinstance(payload, dict) else {}
    value = evidence_chain.get(bucket) if isinstance(evidence_chain, dict) else []
    if value is None:
        return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def prompt_payload_has_sensitive_diagnostic_text(payload: Dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    for bucket in DIAGNOSTIC_BUCKETS:
        for item in payload_bucket_items(payload, bucket):
            text = "\n".join(
                safe_text(item.get(key))
                for key in ("summary", "raw_observation", "key_observation", "context_block", "why_root_candidate")
                if item.get(key)
            )
            if has_unredacted_sensitive_text(text):
                return True
    return False


def prompt_payload_has_usable_injector(payload: Dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    for bucket in DIAGNOSTIC_BUCKETS:
        for item in payload_bucket_items(payload, bucket):
            if is_injector_item(item):
                return True
    return False


def prompt_payload_has_recovery_primary_diagnostic(payload: Dict[str, Any]) -> bool:
    if not isinstance(payload, dict):
        return False
    for bucket in DIAGNOSTIC_BUCKETS:
        for item in payload_bucket_items(payload, bucket):
            if is_recovery_primary_item(item):
                return True
    return False


def payload_has_required_output_contract(payload: Dict[str, Any]) -> bool:
    contract = payload.get("output_contract") if isinstance(payload, dict) else None
    if not isinstance(contract, dict):
        return False
    required = contract.get("required_fields")
    forbidden = contract.get("forbidden_behavior")
    if not isinstance(required, list) or not PROMPT_REQUIRED_FIELDS.issubset({str(value) for value in required}):
        return False
    return isinstance(forbidden, list) and bool(forbidden)


def payload_has_required_safety_policy(payload: Dict[str, Any]) -> bool:
    policy = payload.get("safety_policy") if isinstance(payload, dict) else None
    if not isinstance(policy, dict):
        return False
    required_true = (
        "use_diagnostic_buckets_only",
        "do_not_use_provenance_or_labels",
        "do_not_use_evidence_is_exclusion_only",
        "redacted_sensitive_context",
    )
    return all(policy.get(key) is True for key in required_true)


def payload_has_readiness_metadata(payload: Dict[str, Any], has_chain_root: bool) -> bool:
    metadata = payload.get("readiness_metadata") if isinstance(payload, dict) else None
    if not isinstance(metadata, dict):
        return False
    required_keys = (
        "has_root_candidate",
        "has_supporting_evidence",
        "has_redactions",
        "has_context",
        "chain_confidence",
        "chain_score",
        "diagnostic_item_count",
        "do_not_use_item_count",
    )
    if any(key not in metadata for key in required_keys):
        return False
    if bool(metadata.get("has_root_candidate")) != bool(has_chain_root):
        return False
    for key in ("diagnostic_item_count", "do_not_use_item_count"):
        try:
            if int(metadata.get(key)) < 0:
                return False
        except (TypeError, ValueError):
            return False
    try:
        float(metadata.get("chain_score"))
    except (TypeError, ValueError):
        return False
    return bool(str(metadata.get("chain_confidence") or "").strip())


def payload_has_case_ref(payload: Dict[str, Any]) -> bool:
    case_ref = payload.get("case_ref") if isinstance(payload, dict) else None
    return isinstance(case_ref, str) and bool(case_ref.strip()) and not has_unredacted_sensitive_text(case_ref)


def chain_confidence_is_low(chain: Dict[str, Any]) -> bool:
    return str(chain.get("chain_confidence") or "").lower() == "low" or numeric(chain.get("chain_score"), 0.0) < 0.5


def analyze_prompt_payload(chain: Dict[str, Any], args: argparse.Namespace, safety_failed: bool = False) -> Tuple[str, List[str], List[str]]:
    payload = chain.get("recommended_prompt_payload")
    blockers: List[str] = []
    review_warnings: List[str] = []
    has_chain_root = bool(chain.get("root_candidates"))
    if not isinstance(payload, dict) or not payload:
        blockers.append("missing_prompt_payload")
        return "not_ready", blockers, review_warnings

    evidence_chain = payload.get("evidence_chain")
    if not isinstance(evidence_chain, dict):
        blockers.append("malformed_payload")
    if not payload_has_case_ref(payload):
        blockers.append("missing_case_ref")
    if payload.get("case_id") and has_unredacted_sensitive_text(payload.get("case_id")):
        blockers.append("unredacted_sensitive_text")
    if not has_chain_root:
        blockers.append("no_root_candidate")
    elif not payload_bucket_items(payload, "root_candidates"):
        blockers.append("missing_root_candidate")
    if not payload_has_required_output_contract(payload):
        blockers.append("missing_output_contract")
    if not payload_has_required_safety_policy(payload):
        blockers.append("missing_safety_policy")
    if not payload_has_readiness_metadata(payload, has_chain_root):
        blockers.append("missing_readiness_metadata")
    if safety_failed:
        blockers.append("safety_violation")
    if prompt_payload_has_usable_injector(payload):
        blockers.append("injector_in_payload_diagnostic")
    if prompt_payload_has_recovery_primary_diagnostic(payload):
        blockers.append("recovery_primary_in_payload_diagnostic")
    if prompt_payload_has_sensitive_diagnostic_text(payload):
        blockers.append("unredacted_sensitive_text")
    if len(safe_json_dump(payload)) > PROMPT_PAYLOAD_CHAR_BUDGET:
        blockers.append("payload_too_long")

    if has_chain_root and not chain.get("supporting_evidence"):
        review_warnings.append("no_supporting_evidence")
    if has_chain_root and chain_confidence_is_low(chain):
        review_warnings.append("low_confidence")
    if has_chain_root and not chain.get("symptom_evidence"):
        review_warnings.append("no_symptom_evidence")
    metadata = payload.get("readiness_metadata") if isinstance(payload, dict) else {}
    if isinstance(metadata, dict) and metadata.get("has_context") is False:
        review_warnings.append("inline_only_context")

    warnings = {str(warning) for warning in chain.get("warnings") or []}
    if "sensitive_context_redacted" in warnings:
        review_warnings.append("sensitive_context_redacted")
    if "weak_temporal_ordering" in warnings:
        review_warnings.append("weak_temporal_ordering")
    if "chain_payload_pruned" in warnings:
        review_warnings.append("chain_payload_pruned")

    if args.strict_prompt_readiness:
        for warning in ("sensitive_context_redacted", "no_supporting_evidence", "low_confidence"):
            if warning in review_warnings:
                blockers.append(warning)
        review_warnings = [warning for warning in review_warnings if warning not in set(blockers)]

    blockers = list(dict.fromkeys(blockers))
    review_warnings = list(dict.fromkeys(review_warnings))
    if blockers:
        return "not_ready", blockers, review_warnings
    if review_warnings:
        return "review_ready", blockers, review_warnings
    return "ready", blockers, review_warnings


def analyze_prompt_payloads(chains: Sequence[Dict[str, Any]], safety_by_chain: Dict[str, bool], args: argparse.Namespace) -> Dict[str, Any]:
    ready = 0
    review_ready = 0
    not_ready = 0
    root_candidate_chains = 0
    no_root_chains = 0
    root_candidate_not_ready = 0
    blockers = Counter()
    review_warning_counts = Counter()
    examples: List[Dict[str, Any]] = []
    for chain in chains:
        has_root = bool(chain.get("root_candidates"))
        if has_root:
            root_candidate_chains += 1
        else:
            no_root_chains += 1
        readiness, chain_blockers, chain_review_warnings = analyze_prompt_payload(
            chain,
            args,
            safety_failed=safety_by_chain.get(str(chain.get("chain_id") or ""), False),
        )
        if readiness == "ready":
            ready += 1
        elif readiness == "review_ready":
            review_ready += 1
            review_warning_counts.update(chain_review_warnings)
        else:
            not_ready += 1
            blockers.update(chain_blockers)
            review_warning_counts.update(chain_review_warnings)
            if has_root:
                root_candidate_not_ready += 1
        if len(examples) < args.max_examples and (readiness != "ready" or chain_review_warnings):
            example = chain_example(chain)
            example["readiness"] = readiness
            example["blockers"] = chain_blockers
            example["review_warnings"] = chain_review_warnings
            examples.append(example)
    return {
        "ready": ready,
        "review_ready": review_ready,
        "not_ready": not_ready,
        "root_candidate_chains": root_candidate_chains,
        "no_root_chains": no_root_chains,
        "root_candidate_not_ready": root_candidate_not_ready,
        "blockers": dict(blockers.most_common()),
        "review_warnings": dict(review_warning_counts.most_common()),
        "examples": examples,
    }


def compute_chain_safety_flags(chains: Sequence[Dict[str, Any]]) -> Dict[str, bool]:
    flagged: Dict[str, bool] = {}
    for chain in chains:
        chain_id = str(chain.get("chain_id") or "")
        has_violation = False
        for _bucket, item in iter_diagnostic_items(chain):
            if is_injector_item(item) or is_gt_like_item(item) or is_generic_noise_item(item) or has_unredacted_sensitive_text(item_text(item)):
                has_violation = True
                break
        flagged[chain_id] = has_violation
    return flagged


def compute_warning_counts(chains: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    warnings = Counter()
    for chain in chains:
        for warning in chain.get("warnings") or []:
            warnings[str(warning)] += 1
    return dict(warnings.most_common())


def compute_gates(report: Dict[str, Any], args: argparse.Namespace) -> Dict[str, str]:
    safety = report["safety"]
    hard_safety_pass = all(safety.get(key, 0) == 0 for key in HARD_SAFETY_KEYS)
    gates = {
        "hard_safety_gate": "PASS" if hard_safety_pass else "FAIL",
        "coverage_gate": "PASS"
        if report["coverage"].get("no_root_ratio", 0.0) <= args.max_no_root_ratio
        else "WARN",
        "do_not_use_gate": "PASS"
        if report["coverage"].get("do_not_use_ratio", 0.0) <= args.max_do_not_use_ratio
        else "WARN",
        "prompt_payload_gate": "PASS",
        "context_gate": "PASS",
    }
    if not hard_safety_pass:
        gates["prompt_payload_gate"] = "FAIL"
    else:
        readiness = report["prompt_payload_readiness"]
        ready_like = readiness.get("ready", 0) + readiness.get("review_ready", 0)
        root_candidate_chains = readiness.get(
            "root_candidate_chains",
            report["counts"].get("chains_with_root_candidates", 0),
        )
        root_candidate_not_ready = readiness.get("root_candidate_not_ready", 0)
        if ready_like < root_candidate_chains or root_candidate_not_ready:
            gates["prompt_payload_gate"] = "WARN"
        else:
            gates["prompt_payload_gate"] = "PASS"
    chains = max(1, int(report["counts"].get("chains", 0)))
    no_context_ratio = report["coverage"].get("chains_without_context", 0) / chains
    if no_context_ratio > 0.5:
        gates["context_gate"] = "WARN"
    return gates


def final_status(gates: Dict[str, str]) -> str:
    if gates.get("hard_safety_gate") == "FAIL":
        return "FAIL"
    if any(value != "PASS" for value in gates.values()):
        return "WARN"
    return "PASS"


def build_report(chains: Sequence[Dict[str, Any]], source_summary: Dict[str, Any], parse_errors: Sequence[Dict[str, Any]], args: argparse.Namespace) -> Dict[str, Any]:
    root = Path(args.root).resolve()
    safety_counts, safety_examples = validate_hard_safety(chains, args.max_examples)
    safety_by_chain = compute_chain_safety_flags(chains)
    bucket_counts = compute_bucket_counts(chains)
    diagnostic_items = sum(bucket_counts.get(bucket, 0) for bucket in DIAGNOSTIC_BUCKETS)
    do_not_use_items = bucket_counts.get("do_not_use_evidence", 0)
    total_items = diagnostic_items + do_not_use_items
    chains_with_root = sum(1 for chain in chains if chain.get("root_candidates"))
    chains_without_root = len(chains) - chains_with_root
    chains_with_context = sum(1 for chain in chains if chain.get("has_context"))
    chain_scores = [numeric(chain.get("chain_score"), 0.0) for chain in chains]
    root_scores = [
        root_candidate_score(candidate)
        for chain in chains
        for candidate in (chain.get("root_candidates") or [])
        if isinstance(candidate, dict)
    ]
    prompt_readiness = analyze_prompt_payloads(chains, safety_by_chain, args)
    no_root = analyze_no_root(chains, args)
    do_not_use = analyze_do_not_use(chains, args)
    confidence = compute_confidence_distribution(chains)
    warnings = compute_warning_counts(chains)
    root_distributions = root_candidate_distributions(chains)
    root_without_support = sum(1 for chain in chains if chain.get("root_candidates") and not chain.get("supporting_evidence"))
    only_do_not_use = sum(
        1
        for chain in chains
        if not any(chain.get(bucket) for bucket in DIAGNOSTIC_BUCKETS)
        and bool(chain.get("do_not_use_evidence"))
    )
    only_uncertain = sum(
        1
        for chain in chains
        if bool(chain.get("uncertain_evidence"))
        and not any(chain.get(bucket) for bucket in DIAGNOSTIC_BUCKETS if bucket != "uncertain_evidence")
    )
    report = {
        "root": str(root),
        "dry_run": bool(args.dry_run),
        "status": "PASS",
        "gates": {},
        "source_counts": dict((source_summary.get("counts") or {})),
        "counts": {
            "chains": len(chains),
            "chains_with_root_candidates": chains_with_root,
            "chains_without_root_candidates": chains_without_root,
            "diagnostic_items": diagnostic_items,
            "do_not_use_items": do_not_use_items,
            "parse_errors": len(parse_errors),
        },
        "safety": safety_counts,
        "coverage": {
            "no_root_ratio": round(chains_without_root / len(chains), 4) if chains else 0.0,
            "do_not_use_ratio": round(do_not_use_items / total_items, 4) if total_items else 0.0,
            "chains_with_context": chains_with_context,
            "chains_without_context": len(chains) - chains_with_context,
            "chains_with_supporting_evidence": sum(1 for chain in chains if chain.get("supporting_evidence")),
            "chains_with_symptoms": sum(1 for chain in chains if chain.get("symptom_evidence")),
            "chains_with_only_uncertain_evidence": only_uncertain,
            "chains_with_only_do_not_use": only_do_not_use,
            "chains_with_root_candidate_but_no_supporting_evidence": root_without_support,
            "chains_with_multiple_root_candidates": sum(1 for chain in chains if len(chain.get("root_candidates") or []) > 1),
        },
        "bucket_counts": bucket_counts,
        "confidence_distribution": confidence,
        "score_stats": {
            "chain_score": compute_score_stats(chain_scores),
            "root_candidate_score": compute_score_stats(root_scores),
        },
        "root_candidate_distributions": root_distributions,
        "root_candidate_stats": {
            "root_candidate_count": len(root_scores),
            "chains_with_multiple_root_candidates": sum(1 for chain in chains if len(chain.get("root_candidates") or []) > 1),
            "chains_with_root_candidate_but_no_supporting_evidence": root_without_support,
        },
        "no_root_analysis": no_root,
        "do_not_use_analysis": do_not_use,
        "prompt_payload_readiness": prompt_readiness,
        "warnings": warnings,
        "examples": {
            "safety_violations": safety_examples,
            "high_quality_chains": [
                chain_example(chain)
                for chain in chains
                if chain.get("chain_confidence") == "high" and numeric(chain.get("chain_score"), 0.0) >= args.min_chain_score
            ][: args.max_examples],
            "low_quality_chains": [
                chain_example(chain)
                for chain in chains
                if chain.get("chain_confidence") == "low" or numeric(chain.get("chain_score"), 0.0) < args.min_chain_score
            ][: args.max_examples],
            "chains_without_root_candidates": [chain_example(chain) for chain in chains if not chain.get("root_candidates")][
                : args.max_examples
            ],
            "potentially_overfiltered_do_not_use": do_not_use.get("examples", [])[: args.max_examples],
            "parse_errors": list(parse_errors)[: args.max_examples],
        },
    }
    report["gates"] = compute_gates(report, args)
    report["status"] = final_status(report["gates"])
    return report


def markdown_table(mapping: Dict[str, Any], key_title: str = "key") -> List[str]:
    lines = [f"| {key_title} | value |", "|---|---:|"]
    if not mapping:
        lines.append("| none | 0 |")
    else:
        for key, value in mapping.items():
            lines.append(f"| `{key}` | {value} |")
    return lines


def print_json_report(report: Dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)


def print_markdown_report(report: Dict[str, Any]) -> str:
    lines: List[str] = [
        "# Evidence Chain Regression and Coverage Report",
        "",
        "This checker is read-only by default and does not modify existing evidence files.",
        "Injector/provenance markers must remain outside diagnostic buckets.",
        "Potential over-filtering examples are review candidates, not definite bugs.",
        "A WARN status does not mean the pipeline is unsafe; it means coverage or review thresholds need attention.",
        "",
        "## Status and Gates",
        f"- `status`: {report.get('status')}",
    ]
    lines.extend(markdown_table(report.get("gates") or {}, "gate"))
    lines.extend(
        [
            "",
            "## Hard Safety Invariants",
        ]
    )
    lines.extend(markdown_table(report.get("safety") or {}, "check"))
    lines.extend(
        [
            "",
            "## Case Coverage",
        ]
    )
    lines.extend(markdown_table(report.get("coverage") or {}, "metric"))
    lines.extend(
        [
            "",
            "## Bucket Distribution",
        ]
    )
    lines.extend(markdown_table(report.get("bucket_counts") or {}, "bucket"))
    lines.extend(
        [
            "",
            "## Chain Confidence and Score Stats",
        ]
    )
    lines.extend(markdown_table(report.get("confidence_distribution") or {}, "confidence"))
    score_stats = report.get("score_stats") or {}
    for name, stats in score_stats.items():
        lines.append("")
        lines.append(f"### {name}")
        lines.extend(markdown_table(stats, "stat"))
    lines.extend(
        [
            "",
            "## Root Candidate Distribution",
        ]
    )
    distributions = report.get("root_candidate_distributions") or {}
    for name, distribution in distributions.items():
        lines.append("")
        lines.append(f"### {name}")
        lines.extend(markdown_table(distribution, name))
    lines.extend(
        [
            "",
            "## No-root Case Analysis",
        ]
    )
    lines.extend(markdown_table((report.get("no_root_analysis") or {}).get("by_reason") or {}, "reason"))
    lines.extend(
        [
            "",
            "## Do-not-use / Over-filtering Review",
        ]
    )
    dnu = report.get("do_not_use_analysis") or {}
    lines.extend(markdown_table(dnu.get("by_reason") or {}, "reason"))
    lines.append(f"- `potentially_overfiltered_count`: {dnu.get('potentially_overfiltered_count', 0)}")
    lines.append("")
    lines.append("## Prompt Payload Readiness")
    readiness = report.get("prompt_payload_readiness") or {}
    readiness_counts = {
        "ready": readiness.get("ready", 0),
        "review_ready": readiness.get("review_ready", 0),
        "not_ready": readiness.get("not_ready", 0),
        "root_candidate_chains": readiness.get("root_candidate_chains", 0),
        "no_root_chains": readiness.get("no_root_chains", 0),
        "root_candidate_not_ready": readiness.get("root_candidate_not_ready", 0),
    }
    lines.extend(markdown_table(readiness_counts, "metric"))
    lines.append("")
    lines.append("### Blockers")
    lines.extend(markdown_table(readiness.get("blockers") or {}, "blocker"))
    lines.append("")
    lines.append("### Review Warnings")
    lines.extend(markdown_table(readiness.get("review_warnings") or {}, "warning"))
    lines.append("")
    lines.append("- `review_ready` chains are safe to test in prompt integration but should be monitored.")
    lines.append("- No-root chains are coverage gaps, not prompt payload failures.")
    lines.append("- Successful redaction is safe by default and is reported as a review warning.")
    lines.append("")
    lines.append("## Warning Summary")
    lines.extend(markdown_table(report.get("warnings") or {}, "warning"))
    lines.append("")
    lines.append("## Examples")
    examples = report.get("examples") or {}
    for title, key in (
        ("Safety violations", "safety_violations"),
        ("High quality chains", "high_quality_chains"),
        ("Low quality chains", "low_quality_chains"),
        ("Chains without root candidates", "chains_without_root_candidates"),
        ("Potentially over-filtered do-not-use", "potentially_overfiltered_do_not_use"),
    ):
        lines.append("")
        lines.append(f"### {title}")
        values = examples.get(key) or []
        if not values:
            lines.append("- none")
        else:
            for value in values:
                lines.append(f"- `{compact_text(value, 360)}`")
    warnings_to_print: List[str] = []
    safety = report.get("safety") or {}
    if safety.get("injector_in_diagnostic_buckets"):
        warnings_to_print.append("INJECTOR_IN_DIAGNOSTIC_BUCKET")
    if safety.get("gt_like_in_diagnostic_buckets"):
        warnings_to_print.append("GT_LIKE_IN_DIAGNOSTIC_BUCKET")
    if safety.get("recovery_primary_as_root_candidate"):
        warnings_to_print.append("RECOVERY_PRIMARY_AS_ROOT")
    if report.get("counts", {}).get("chains_without_root_candidates"):
        warnings_to_print.append("NO_ROOT_CANDIDATES")
    if "weak_temporal_ordering" in (report.get("warnings") or {}):
        warnings_to_print.append("WEAK_TEMPORAL_ORDERING")
    if "chain_payload_pruned" in (report.get("warnings") or {}):
        warnings_to_print.append("CHAIN_PAYLOAD_PRUNED")
    if warnings_to_print:
        lines.append("")
        lines.append("## Warning Labels")
        for warning in warnings_to_print:
            lines.append(f"- `{warning}`")
    lines.append("")
    lines.append("## Notes")
    lines.append("- Chain JSONL, if supplied, is treated as the authoritative input for this checker run.")
    lines.append("- App Server candidates are not used by this checker.")
    return "\n".join(lines).rstrip() + "\n"


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


def render_output(report: Dict[str, Any], args: argparse.Namespace) -> str:
    if args.format == "json":
        return print_json_report(report) + "\n"
    if args.format == "markdown":
        return print_markdown_report(report)
    return "----- JSON -----\n" + print_json_report(report) + "\n\n----- MARKDOWN -----\n" + print_markdown_report(report)


def run(args: argparse.Namespace) -> Dict[str, Any]:
    chains, source_summary, parse_errors = load_or_build_chains(args)
    return build_report(chains, source_summary, parse_errors, args)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    root = Path(args.root).resolve()
    try:
        report = run(args)
    except RuntimeError as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 2
    output = render_output(report, args)
    if args.output and not args.stdout_only:
        output_path = Path(args.output)
        if not output_path.is_absolute():
            output_path = root / output_path
        try:
            validate_output_path(output_path, root)
        except ValueError as exc:
            sys.stderr.write(f"ERROR: {exc}\n")
            return 2
        output_path.write_text(output, encoding="utf-8")
    else:
        sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
