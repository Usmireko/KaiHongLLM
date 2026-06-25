#!/usr/bin/env python3
"""Read-only adapter rule gap review for no-root evidence chains.

This tool focuses on no-root cases already triaged as possible adapter rule
gaps. It simulates deterministic anomaly/object/source inference proposals in
memory only. It does not modify adapter rules, evidence data, chains, or any
pipeline artifact.
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
try:
    sys.stdout.reconfigure(encoding="utf-8")
except AttributeError:
    pass

REDACTION_MARKER = "[REDACTED_SENSITIVE_CONTEXT]"

DIAGNOSTIC_REVIEW_BUCKETS = (
    "fault_observations",
    "supporting_evidence",
    "symptom_evidence",
    "uncertain_evidence",
    "contradicting_evidence",
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

RULE_FAMILY_ORDER = (
    "dns_failure",
    "route_missing",
    "disconnect",
    "timeout",
    "reachability_loss",
    "process_crash",
    "auth_failure",
)

CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}

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

KNOWN_UNSAFE_ANOMALIES = {"", "unknown", "noise", "injector_marker"}
UNKNOWN_VALUES = {"", "unknown", "none", "n/a", "null"}
UNSAFE_FLAGS = {
    "duplicate_probe",
    "excluded_provenance",
    "generic_noise",
    "gt_label_marker",
    "gt_like_text",
    "historical_noise",
    "injector_marker",
    "provenance",
    "recovery_primary",
}

RULE_FAMILIES: Dict[str, Dict[str, Any]] = {
    "dns_failure": {
        "terms": (
            "dns",
            "resolve",
            "resolver",
            "resolv.conf",
            "nameserver",
            "no such host",
            "temporary failure in name resolution",
            "getaddrinfo",
            "nxdomain",
            "servfail",
        ),
        "strong_terms": (
            "no such host",
            "temporary failure in name resolution",
            "getaddrinfo",
            "nxdomain",
            "servfail",
            "nameserver",
            "resolv.conf",
            "resolve",
        ),
        "anomaly_type": "dns_failure",
        "normalized_entity": "DNSFailure",
        "object": "dns",
        "source": "dns",
    },
    "route_missing": {
        "terms": (
            "no route",
            "network is unreachable",
            "default route",
            "gateway",
            "routing table",
            "ip route",
            "rtnetlink",
            "route missing",
        ),
        "strong_terms": (
            "no route",
            "network is unreachable",
            "route missing",
            "rtnetlink",
            "default route",
        ),
        "anomaly_type": "route_missing",
        "normalized_entity": "RouteMissing",
        "object": "route",
        "source": "route",
    },
    "disconnect": {
        "terms": (
            "wlan0 down",
            "link down",
            "disconnected",
            "deauth",
            "deauthenticated",
            "disassoc",
            "not associated",
            "association failed",
            "ctrl-event-disconnected",
            "carrier lost",
            "wpa_supplicant",
        ),
        "strong_terms": (
            "wlan0 down",
            "link down",
            "disconnected",
            "deauthenticated",
            "not associated",
            "association failed",
            "ctrl-event-disconnected",
            "carrier lost",
        ),
        "anomaly_type": "disconnect",
        "normalized_entity": "WiFiLinkDown",
        "object": "wlan0",
        "source": "wifi",
    },
    "timeout": {
        "terms": (
            "timeout",
            "timed out",
            "request timeout",
            "operation timed out",
        ),
        "strong_terms": (
            "timeout",
            "timed out",
            "request timeout",
            "operation timed out",
        ),
        "anomaly_type": "timeout",
        "normalized_entity": "NetworkTimeout",
        "object": "network",
        "source": "network",
    },
    "reachability_loss": {
        "terms": (
            "unreachable",
            "packet loss",
            "100% packet loss",
            "destination host unreachable",
            "no response",
            "ping failed",
            "icmp",
        ),
        "strong_terms": (
            "unreachable",
            "packet loss",
            "100% packet loss",
            "destination host unreachable",
            "ping failed",
        ),
        "anomaly_type": "reachability_loss",
        "normalized_entity": "ReachabilityLoss",
        "object": "ping",
        "source": "network",
    },
    "process_crash": {
        "terms": (
            "service stopped",
            "process exit",
            "process exited",
            "daemon stopped",
            "crash",
            "killed",
            "pid",
            "segfault",
        ),
        "strong_terms": (
            "service stopped",
            "process exit",
            "process exited",
            "daemon stopped",
            "crash",
            "killed",
            "segfault",
        ),
        "anomaly_type": "process_crash",
        "normalized_entity": "ProcessFailure",
        "object": "process",
        "source": "process",
    },
    "auth_failure": {
        "terms": (
            "authentication failed",
            "auth failed",
            "password failed",
            "handshake failed",
            "association rejected",
        ),
        "strong_terms": (
            "authentication failed",
            "auth failed",
            "password failed",
            "handshake failed",
            "association rejected",
        ),
        "anomaly_type": "auth_failure",
        "normalized_entity": "AuthFailure",
        "object": "wifi",
        "source": "wifi",
    },
}


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Review adapter rule gaps in no-root evidence cases.")
    parser.add_argument("--root", default=".", help="Repository root. Default: current directory.")
    parser.add_argument("--input", default=None, help="Optional evidence_candidates.jsonl file or directory.")
    parser.add_argument("--triage-jsonl", default=None, help="Optional JSONL from evidence_no_root_gap_triage.py.")
    parser.add_argument("--chains-jsonl", default=None, help="Optional chain JSONL from evidence_chain_builder.py.")
    parser.add_argument("--stdout-only", action="store_true", help="Force stdout output only.")
    parser.add_argument("--dry-run", action="store_true", help="Explicit safety flag; this tool is read-only by default.")
    parser.add_argument("--format", choices=("json", "markdown", "both", "jsonl"), default="both")
    parser.add_argument("--max-examples", type=int, default=10)
    parser.add_argument("--limit-cases", type=int, default=None)
    parser.add_argument("--min-term-frequency", type=int, default=1)
    parser.add_argument("--min-confidence", choices=("low", "medium", "high"), default="medium")
    parser.add_argument("--include-low-confidence", action="store_true")
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


def compact_text(value: Any, limit: int = 260) -> str:
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
        raise ImportError(f"cannot import {name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_triage_module(root: Path) -> Any:
    return safe_import_module(root / "tools" / "evidence_no_root_gap_triage.py", "_evidence_no_root_gap_triage_for_rule_review")


def contains_sensitive_text(text: Any) -> bool:
    value = safe_text(text)
    if not value:
        return False
    lower = value.lower()
    if REDACTION_MARKER.lower() in lower:
        lower = lower.replace(REDACTION_MARKER.lower(), "")
    if any(term in lower for term in SENSITIVE_TERMS):
        return True
    return any(pattern.search(value) for pattern in SENSITIVE_PATTERNS)


def contains_forbidden_positive_terms(text: Any) -> bool:
    return contains_sensitive_text(text)


def redact_sensitive_text(text: Any, enabled: bool = True) -> Tuple[str, bool]:
    value = safe_text(text)
    if not enabled or not value:
        return value, False
    redacted = value
    changed = False
    for term in sorted(SENSITIVE_TERMS, key=len, reverse=True):
        pattern = re.compile(re.escape(term), re.IGNORECASE)
        redacted, count = pattern.subn(REDACTION_MARKER, redacted)
        changed = changed or count > 0
    for pattern in SENSITIVE_PATTERNS:
        redacted, count = pattern.subn(REDACTION_MARKER, redacted)
        changed = changed or count > 0
    return redacted, changed


def redact_value(value: Any, enabled: bool) -> Any:
    if isinstance(value, dict):
        return {str(k): redact_value(v, enabled) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(item, enabled) for item in value]
    if isinstance(value, tuple):
        return [redact_value(item, enabled) for item in value]
    if isinstance(value, str):
        return redact_sensitive_text(value, enabled)[0]
    return value


def read_text_lossy(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def load_chains_from_jsonl(path: Path, args: argparse.Namespace) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    chains: List[Dict[str, Any]] = []
    parse_errors: List[Dict[str, Any]] = []
    for line_no, line in enumerate(read_text_lossy(path).splitlines(), 1):
        line = line.lstrip("\ufeff")
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            if len(parse_errors) < args.max_examples:
                parse_errors.append({"file": str(path), "line": line_no, "error": str(exc), "text": compact_text(line)})
            continue
        if isinstance(value, dict):
            chains.append(value)
        elif len(parse_errors) < args.max_examples:
            parse_errors.append({"file": str(path), "line": line_no, "error": "JSONL row is not an object"})
    return chains, redact_value(parse_errors, args.redact_sensitive)


def build_chains_from_root(root: Path, input_path: Optional[str], args: argparse.Namespace) -> Tuple[List[Dict[str, Any]], Dict[str, Any], List[Dict[str, Any]]]:
    triage = load_triage_module(root)
    triage_argv = [
        "--root",
        str(root),
        "--format",
        "json",
        "--max-examples",
        str(max(0, args.max_examples)),
        "--min-utility",
        str(args.min_utility),
    ]
    if input_path:
        triage_argv.extend(["--input", input_path])
    if args.dry_run:
        triage_argv.append("--dry-run")
    if args.redact_sensitive:
        triage_argv.append("--redact-sensitive")
    else:
        triage_argv.append("--no-redact-sensitive")
    triage_args = triage.parse_args(triage_argv)
    source_summary, chains, parse_errors, input_mode = triage.load_or_build_chains(triage_args)
    source_summary = dict(source_summary)
    source_summary["input_mode"] = input_mode
    return chains, source_summary, parse_errors


def load_triage_records(path: Path, args: argparse.Namespace) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    records: List[Dict[str, Any]] = []
    parse_errors: List[Dict[str, Any]] = []
    for line_no, line in enumerate(read_text_lossy(path).splitlines(), 1):
        line = line.lstrip("\ufeff")
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            if len(parse_errors) < args.max_examples:
                parse_errors.append({"file": str(path), "line": line_no, "error": str(exc), "text": compact_text(line)})
            continue
        if isinstance(value, dict):
            records.append(redact_value(value, args.redact_sensitive))
        elif len(parse_errors) < args.max_examples:
            parse_errors.append({"file": str(path), "line": line_no, "error": "JSONL row is not an object"})
    return records, redact_value(parse_errors, args.redact_sensitive)


def build_triage_records(root: Path, input_path: Optional[str], args: argparse.Namespace) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any], List[Dict[str, Any]], int]:
    triage = load_triage_module(root)
    if args.chains_jsonl:
        chains_path = Path(args.chains_jsonl)
        if not chains_path.is_absolute():
            chains_path = root / chains_path
        chains, parse_errors = load_chains_from_jsonl(chains_path, args)
        source_summary = {"counts": {"chains_jsonl_rows": len(chains), "parse_errors": len(parse_errors)}, "source_path": str(chains_path)}
    else:
        chains, source_summary, parse_errors = build_chains_from_root(root, input_path, args)
    triage_args = argparse.Namespace(
        root=str(root),
        input=input_path,
        chains_jsonl=args.chains_jsonl,
        stdout_only=True,
        dry_run=args.dry_run,
        format="json",
        max_examples=args.max_examples,
        limit_cases=None,
        min_utility=args.min_utility,
        redact_sensitive=args.redact_sensitive,
        output=None,
    )
    triage_records, total_no_root = triage.build_no_root_triage_records(chains, triage_args)
    return chains, list(triage_records), source_summary, list(parse_errors), int(total_no_root)


def case_ref_from_chain(chain: Optional[Dict[str, Any]], triage_record: Optional[Dict[str, Any]]) -> str:
    if triage_record and triage_record.get("case_ref"):
        return str(triage_record.get("case_ref"))
    if chain:
        payload = chain.get("recommended_prompt_payload") or {}
        if isinstance(payload, dict) and payload.get("case_ref"):
            return str(payload.get("case_ref"))
        chain_id = str(chain.get("chain_id") or "").strip()
        if chain_id.startswith("CHAIN_"):
            return "CASE_" + chain_id.split("_", 1)[1]
    return "CASE_UNKNOWN"


def chain_key(chain: Dict[str, Any]) -> Tuple[str, str, str]:
    return (
        str(chain.get("chain_id") or ""),
        str((chain.get("recommended_prompt_payload") or {}).get("case_ref") or ""),
        str(chain.get("case_rel") or ""),
    )


def select_target_cases(
    chains: Sequence[Dict[str, Any]],
    triage_records: Sequence[Dict[str, Any]],
    args: argparse.Namespace,
) -> List[Tuple[Optional[Dict[str, Any]], Dict[str, Any]]]:
    by_chain_id: Dict[str, Dict[str, Any]] = {}
    by_case_ref: Dict[str, Dict[str, Any]] = {}
    by_case_rel: Dict[str, Dict[str, Any]] = {}
    for chain in chains:
        chain_id, case_ref, case_rel = chain_key(chain)
        if chain_id:
            by_chain_id[chain_id] = chain
        if case_ref:
            by_case_ref[case_ref] = chain
        if case_rel:
            by_case_rel[case_rel] = chain

    selected: List[Tuple[Optional[Dict[str, Any]], Dict[str, Any]]] = []
    excluded_categories = {"only_provenance_or_labels", "only_generic_noise", "true_coverage_gap"}
    for record in triage_records:
        category = str(record.get("primary_category") or record.get("category") or "")
        recommendation = str(record.get("recommendation") or "")
        if category in excluded_categories:
            continue
        if category != "missing_anomaly_type" and recommendation != "review_adapter_rules":
            continue
        chain = by_chain_id.get(str(record.get("chain_id") or ""))
        if chain is None:
            chain = by_case_ref.get(str(record.get("case_ref") or ""))
        if chain is None:
            chain = by_case_rel.get(str(record.get("case_rel") or ""))
        if chain is not None and chain.get("root_candidates"):
            continue
        selected.append((chain, record))
    return selected


def iter_flags(item: Dict[str, Any]) -> Iterator[str]:
    for key in ("risk_flags", "flags"):
        value = item.get(key)
        if isinstance(value, (list, tuple, set)):
            for flag in value:
                yield str(flag).lower()
        elif value:
            yield str(value).lower()


def item_summary(item: Dict[str, Any]) -> str:
    return compact_text(item.get("summary") or item.get("raw_observation") or item.get("key_observation") or item.get("text") or "")


def observation_text(item: Dict[str, Any]) -> str:
    fields = [
        item.get("summary"),
        item.get("raw_observation"),
        item.get("key_observation"),
        item.get("context_block"),
        item.get("source"),
        item.get("object"),
        item.get("anomaly_type"),
        item.get("causal_role") or item.get("role"),
    ]
    return " ".join(compact_text(field, 500) for field in fields if field is not None)


def is_unknown(value: Any) -> bool:
    return safe_text(value).strip().lower() in UNKNOWN_VALUES


def is_generic_noise_item(item: Dict[str, Any]) -> bool:
    flags = set(iter_flags(item))
    if flags & {"generic_noise", "duplicate_probe", "historical_noise"}:
        return True
    anomaly = safe_text(item.get("anomaly_type")).lower()
    text = observation_text(item).lower()
    return anomaly == "noise" or "duplicate_probe" in text or "historical_noise" in text


def is_recovery_primary_like(item: Dict[str, Any]) -> bool:
    flags = set(iter_flags(item))
    if "recovery_primary" in flags:
        return True
    role = safe_text(item.get("causal_role") or item.get("role")).lower()
    phase = safe_text(item.get("phase")).lower()
    text = observation_text(item).lower()
    return role == "recovery_primary" or phase == "recovery" or "recovered" in text or "restored" in text


def is_safe_observation(item: Dict[str, Any]) -> bool:
    text = observation_text(item)
    flags = set(iter_flags(item))
    if item.get("usable_for_diagnosis") is False:
        return False
    if safe_text(item.get("source")).lower() == "fault_inject":
        return False
    if safe_text(item.get("anomaly_type")).lower() == "injector_marker":
        return False
    if safe_text(item.get("causal_role") or item.get("role")).lower() == "excluded_provenance":
        return False
    if flags & UNSAFE_FLAGS:
        return False
    if contains_forbidden_positive_terms(text):
        return False
    if is_generic_noise_item(item):
        return False
    if is_recovery_primary_like(item):
        return False
    return True


def normalize_observation(bucket: str, item: Dict[str, Any], args: argparse.Namespace) -> Dict[str, Any]:
    summary, _changed = redact_sensitive_text(item_summary(item), args.redact_sensitive)
    return {
        "bucket": bucket,
        "evidence_id": item.get("evidence_id"),
        "source": redact_sensitive_text(safe_text(item.get("source") or "unknown"), args.redact_sensitive)[0],
        "object": redact_sensitive_text(safe_text(item.get("object") or "unknown"), args.redact_sensitive)[0],
        "phase": redact_sensitive_text(safe_text(item.get("phase") or "unknown"), args.redact_sensitive)[0],
        "anomaly_type": redact_sensitive_text(safe_text(item.get("anomaly_type") or "unknown"), args.redact_sensitive)[0],
        "causal_role": redact_sensitive_text(safe_text(item.get("causal_role") or item.get("role") or "unknown"), args.redact_sensitive)[0],
        "utility_score": round(numeric(item.get("utility_score"), numeric(item.get("score"), 0.0)), 4),
        "summary": summary,
        "context_ref": item.get("context_ref"),
        "risk_flags": sorted(set(iter_flags(item))),
    }


def extract_safe_observations(chain: Optional[Dict[str, Any]], triage_record: Optional[Dict[str, Any]], args: argparse.Namespace) -> List[Dict[str, Any]]:
    observations: List[Dict[str, Any]] = []
    if chain:
        for bucket in DIAGNOSTIC_REVIEW_BUCKETS:
            for item in chain.get(bucket) or []:
                if isinstance(item, dict) and is_safe_observation(item):
                    observations.append(normalize_observation(bucket, item, args))
    elif triage_record:
        for item in triage_record.get("top_safe_items") or []:
            if not isinstance(item, dict):
                continue
            bucket = safe_text(item.get("bucket") or item.get("role") or "uncertain_evidence")
            if bucket == "do_not_use_evidence" or bucket not in DIAGNOSTIC_REVIEW_BUCKETS:
                continue
            if is_safe_observation(item):
                observations.append(normalize_observation(bucket, item, args))
    observations.sort(key=lambda item: (-numeric(item.get("utility_score")), safe_text(item.get("evidence_id"))))
    return observations


def term_matches(text: str, term: str) -> bool:
    if " " in term or "." in term or "-" in term or "%" in term:
        return term in text
    return re.search(r"(?<![a-z0-9_])" + re.escape(term) + r"(?![a-z0-9_])", text) is not None


def match_candidate_rule_family(observation: Dict[str, Any]) -> List[Dict[str, Any]]:
    text = observation_text(observation).lower()
    if contains_forbidden_positive_terms(text):
        return []
    matches: List[Dict[str, Any]] = []
    for family in RULE_FAMILY_ORDER:
        spec = RULE_FAMILIES[family]
        matched_terms = [term for term in spec["terms"] if term_matches(text, term.lower())]
        if len(matched_terms) >= 1:
            matches.append({"family": family, "matched_terms": matched_terms})
    return matches


def compatible_source_or_object(observation: Dict[str, Any], family: str) -> bool:
    spec = RULE_FAMILIES[family]
    source = safe_text(observation.get("source")).lower()
    obj = safe_text(observation.get("object")).lower()
    target_source = safe_text(spec["source"]).lower()
    target_object = safe_text(spec["object"]).lower()
    if source and source not in UNKNOWN_VALUES and (target_source in source or source in target_source):
        return True
    if obj and obj not in UNKNOWN_VALUES and (target_object in obj or obj in target_object):
        return True
    if family in {"timeout", "reachability_loss"} and source in {"network", "state", "metric", "ping"}:
        return True
    if family == "process_crash" and source == "process":
        return True
    return False


def score_rule_confidence(observation: Dict[str, Any], matched_family: str, matched_terms: Sequence[str]) -> str:
    spec = RULE_FAMILIES[matched_family]
    strong = {term.lower() for term in spec["strong_terms"]}
    matched = {term.lower() for term in matched_terms}
    strong_hits = matched & strong
    utility = numeric(observation.get("utility_score"))
    phase = safe_text(observation.get("phase")).lower()
    compatible = compatible_source_or_object(observation, matched_family)
    if len(matched) >= 2 and compatible and utility >= 0.25 and phase in {"fault", "unknown", ""} and strong_hits:
        return "high"
    if strong_hits and (compatible or utility >= 0.25):
        return "medium"
    return "low"


def proposed_object_for_family(observation: Dict[str, Any], family: str) -> str:
    spec = RULE_FAMILIES[family]
    current = safe_text(observation.get("object") or "").strip().lower()
    if family == "timeout" and current and current not in UNKNOWN_VALUES:
        return current
    if family == "route_missing":
        text = observation_text(observation).lower()
        if "gateway" in text:
            return "gateway"
    if family in {"process_crash", "auth_failure"}:
        current_source = safe_text(observation.get("source") or "").strip().lower()
        if current and current not in UNKNOWN_VALUES:
            return current
        if family == "auth_failure" and current_source == "wifi":
            return "wifi"
    return safe_text(spec["object"])


def simulate_rule_impact(observation: Dict[str, Any], proposal: Dict[str, Any], args: argparse.Namespace) -> bool:
    if not is_safe_observation(observation):
        return False
    if safe_text(proposal.get("proposed_anomaly_type")).lower() in KNOWN_UNSAFE_ANOMALIES:
        return False
    proposed_object = safe_text(proposal.get("proposed_object")).lower()
    proposed_source = safe_text(proposal.get("proposed_source")).lower()
    if proposed_object in UNKNOWN_VALUES and proposed_source in UNKNOWN_VALUES:
        return False
    role = safe_text(observation.get("causal_role") or "").lower()
    bucket = safe_text(observation.get("bucket")).lower()
    if role == "symptom" or bucket == "symptom_evidence":
        return False
    return numeric(observation.get("utility_score")) >= max(0.0, args.min_utility - 0.05)


def proposal_from_match(observation: Dict[str, Any], match: Dict[str, Any], args: argparse.Namespace) -> Optional[Dict[str, Any]]:
    family = str(match.get("family") or "")
    if family not in RULE_FAMILIES:
        return None
    matched_terms = sorted(set(str(term) for term in match.get("matched_terms") or []))
    if len(matched_terms) < max(1, args.min_term_frequency):
        return None
    confidence = score_rule_confidence(observation, family, matched_terms)
    if not args.include_low_confidence and confidence == "low":
        return None
    min_confidence = "low" if args.include_low_confidence else args.min_confidence
    if CONFIDENCE_ORDER[confidence] < CONFIDENCE_ORDER[min_confidence]:
        return None
    spec = RULE_FAMILIES[family]
    proposal = {
        "evidence_id": observation.get("evidence_id"),
        "matched_family": family,
        "matched_terms": matched_terms,
        "proposed_anomaly_type": spec["anomaly_type"],
        "proposed_normalized_entity": spec["normalized_entity"],
        "proposed_object": proposed_object_for_family(observation, family),
        "proposed_source": spec["source"] if family != "timeout" else safe_text(observation.get("source") or spec["source"]),
        "confidence": confidence,
        "would_enable_review_candidate": False,
        "reason": f"safe {family.replace('_', ' ')} terms appear in diagnostic-safe observation",
    }
    proposal["would_enable_review_candidate"] = simulate_rule_impact(observation, proposal, args)
    return proposal


def build_case_review_record(
    chain: Optional[Dict[str, Any]],
    triage_record: Dict[str, Any],
    args: argparse.Namespace,
) -> Dict[str, Any]:
    observations = extract_safe_observations(chain, triage_record, args)
    matches: List[Dict[str, Any]] = []
    for observation in observations:
        for match in match_candidate_rule_family(observation):
            proposal = proposal_from_match(observation, match, args)
            if proposal is not None:
                matches.append(proposal)
    matches.sort(
        key=lambda item: (
            -CONFIDENCE_ORDER.get(str(item.get("confidence")), 0),
            not bool(item.get("would_enable_review_candidate")),
            str(item.get("matched_family") or ""),
            str(item.get("evidence_id") or ""),
        )
    )
    enabling = [match for match in matches if match.get("would_enable_review_candidate")]
    if enabling:
        recommendation = "adapter_patch_candidate"
    elif observations:
        recommendation = "manual_review"
    else:
        recommendation = "accept_gap"
    case_rel = safe_text((triage_record or {}).get("case_rel") or (chain or {}).get("case_rel") or "")
    case_rel = redact_sensitive_text(case_rel, args.redact_sensitive)[0]
    notes = []
    if chain is None:
        notes.append("limited analysis from triage JSONL; chain context was not available")
    if not matches and observations:
        notes.append("safe observations did not match the configured network/process rule families at the requested confidence")
    record = {
        "case_ref": case_ref_from_chain(chain, triage_record),
        "case_id_redacted": True,
        "case_rel": case_rel,
        "current_triage_category": str(triage_record.get("primary_category") or triage_record.get("category") or "unknown"),
        "safe_observation_count": len(observations),
        "candidate_rule_matches": redact_value(matches, args.redact_sensitive),
        "case_recommendation": recommendation,
        "notes": "; ".join(notes),
    }
    return redact_value(record, args.redact_sensitive)


def validate_rule_review_safety(records: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    counts = Counter(
        {
            "injector_in_rule_matches": 0,
            "gt_like_in_rule_matches": 0,
            "unredacted_sensitive_examples": 0,
            "do_not_use_used_for_rule": 0,
        }
    )
    for record in records:
        free_text = {
            "case_rel": record.get("case_rel"),
            "candidate_rule_matches": record.get("candidate_rule_matches"),
            "notes": record.get("notes"),
        }
        if contains_sensitive_text(free_text):
            counts["unredacted_sensitive_examples"] += 1
        for match in record.get("candidate_rule_matches") or []:
            text = safe_json_dump(match).lower()
            if "fault_inject" in text or "injector" in text or "provenance" in text:
                counts["injector_in_rule_matches"] += 1
            if contains_forbidden_positive_terms(text):
                counts["gt_like_in_rule_matches"] += 1
            if str(match.get("bucket") or "").lower() == "do_not_use_evidence" or match.get("from_do_not_use"):
                counts["do_not_use_used_for_rule"] += 1
    return {key: counts.get(key, 0) for key in (
        "injector_in_rule_matches",
        "gt_like_in_rule_matches",
        "unredacted_sensitive_examples",
        "do_not_use_used_for_rule",
    )}


def count_no_root_chains(chains: Sequence[Dict[str, Any]], triage_records: Sequence[Dict[str, Any]]) -> int:
    if chains:
        return sum(1 for chain in chains if not chain.get("root_candidates"))
    return len(triage_records)


def build_summary(
    records: Sequence[Dict[str, Any]],
    chains: Sequence[Dict[str, Any]],
    triage_records: Sequence[Dict[str, Any]],
    parse_errors: Sequence[Dict[str, Any]],
    root: Path,
    args: argparse.Namespace,
) -> Dict[str, Any]:
    rule_counts = Counter()
    confidence_counts = Counter({"high": 0, "medium": 0, "low": 0})
    recommendations = Counter(record.get("case_recommendation") for record in records)
    matches_total = 0
    cases_with_review = 0
    for record in records:
        case_has_review = False
        for match in record.get("candidate_rule_matches") or []:
            family = str(match.get("matched_family") or "")
            confidence = str(match.get("confidence") or "low")
            rule_counts[family] += 1
            confidence_counts[confidence] += 1
            matches_total += 1
            case_has_review = case_has_review or bool(match.get("would_enable_review_candidate"))
        if case_has_review:
            cases_with_review += 1
    no_root = count_no_root_chains(chains, triage_records)
    safety = validate_rule_review_safety(records)
    status = "PASS"
    if any(safety.values()):
        status = "FAIL"
    elif recommendations.get("adapter_patch_candidate", 0) or recommendations.get("manual_review", 0) or confidence_counts.get("low", 0):
        status = "WARN"
    examples = {
        "adapter_patch_candidates": [
            record for record in records if record.get("case_recommendation") == "adapter_patch_candidate"
        ][: args.max_examples],
        "manual_review": [
            record for record in records if record.get("case_recommendation") == "manual_review"
        ][: args.max_examples],
        "accept_gap": [
            record for record in records if record.get("case_recommendation") == "accept_gap"
        ][: args.max_examples],
        "parse_errors": list(parse_errors)[: args.max_examples],
    }
    return {
        "root": str(root),
        "dry_run": bool(args.dry_run),
        "status": status,
        "counts": {
            "chains": len(chains) if chains else 0,
            "no_root_chains": no_root,
            "target_cases": len(records),
            "reviewed_cases": len(records),
            "safe_observations_reviewed": sum(int(record.get("safe_observation_count") or 0) for record in records),
            "candidate_rule_matches": matches_total,
            "adapter_patch_candidate_cases": recommendations.get("adapter_patch_candidate", 0),
            "manual_review_cases": recommendations.get("manual_review", 0),
            "accept_gap_cases": recommendations.get("accept_gap", 0),
        },
        "rule_family_distribution": {family: rule_counts.get(family, 0) for family in RULE_FAMILY_ORDER},
        "confidence_distribution": {
            "high": confidence_counts.get("high", 0),
            "medium": confidence_counts.get("medium", 0),
            "low": confidence_counts.get("low", 0),
        },
        "coverage_simulation": {
            "cases_with_review_candidates": cases_with_review,
            "would_reduce_no_root_by": cases_with_review,
            "remaining_no_root_estimate": max(0, no_root - cases_with_review),
        },
        "safety": safety,
        "examples": examples,
    }


def print_json_summary(summary: Dict[str, Any]) -> str:
    return json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True)


def markdown_table(mapping: Dict[str, Any], key_title: str = "key") -> List[str]:
    lines = [f"| {key_title} | count |", "|---|---:|"]
    if not mapping:
        lines.append("| none | 0 |")
        return lines
    for key, value in mapping.items():
        lines.append(f"| `{key}` | {value} |")
    return lines


def print_markdown_summary(summary: Dict[str, Any]) -> str:
    lines = [
        "# Evidence Adapter Rule Gap Review Report",
        "",
        "This review tool is read-only by default and does not modify adapter rules.",
        "Rule proposals are review candidates, not automatic changes.",
        "Injector/provenance and label-like terms are forbidden as positive diagnostic rules.",
        "Any adapter patch must be implemented in a separate scoped task and re-run the full smoke runner.",
        "",
        "## Status",
        f"- `status`: {summary.get('status')}",
        "",
        "## Target Case Summary",
    ]
    for key, value in (summary.get("counts") or {}).items():
        lines.append(f"- `{key}`: {value}")
    lines.extend(["", "## Candidate Rule Family Distribution"])
    lines.extend(markdown_table(summary.get("rule_family_distribution") or {}, "rule_family"))
    lines.extend(["", "## Confidence Distribution"])
    lines.extend(markdown_table(summary.get("confidence_distribution") or {}, "confidence"))
    lines.extend(["", "## Coverage Impact Simulation"])
    for key, value in (summary.get("coverage_simulation") or {}).items():
        lines.append(f"- `{key}`: {value}")
    lines.extend(["", "## Safety Checks"])
    lines.extend(markdown_table(summary.get("safety") or {}, "safety_check"))
    lines.extend(["", "## Adapter Patch Candidate Examples"])
    for item in (summary.get("examples") or {}).get("adapter_patch_candidates") or []:
        lines.append(f"- `{safe_json_dump(item)}`")
    if not (summary.get("examples") or {}).get("adapter_patch_candidates"):
        lines.append("- none")
    lines.extend(["", "## Manual Review Examples"])
    for item in (summary.get("examples") or {}).get("manual_review") or []:
        lines.append(f"- `{safe_json_dump(item)}`")
    if not (summary.get("examples") or {}).get("manual_review"):
        lines.append("- none")
    lines.extend(["", "## Notes"])
    lines.append("- This task is simulation-only; no root candidates are created or promoted.")
    lines.append("- Low-confidence proposals are omitted unless `--include-low-confidence` is passed.")
    lines.append("- `WARN` is expected when review candidates or manual review cases remain.")
    return "\n".join(lines).rstrip() + "\n"


def print_jsonl_records(records: Sequence[Dict[str, Any]]) -> str:
    return "\n".join(safe_json_dump(record) for record in records)


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
    if any(part in PROTECTED_OUTPUT_DIRS or "frozen" in part or "accepted" in part or "ledger" in part for part in parts):
        raise ValueError(f"refusing output path under protected directory: {resolved}")
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"refusing output path outside repository root: {resolved}") from exc


def render_output(summary: Dict[str, Any], records: Sequence[Dict[str, Any]], args: argparse.Namespace) -> str:
    if args.format == "json":
        return print_json_summary(summary) + "\n"
    if args.format == "markdown":
        return print_markdown_summary(summary)
    if args.format == "jsonl":
        return print_jsonl_records(records) + ("\n" if records else "")
    return "----- JSON -----\n" + print_json_summary(summary) + "\n\n----- MARKDOWN -----\n" + print_markdown_summary(summary)


def run(args: argparse.Namespace) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    root = Path(args.root).resolve()
    chains: List[Dict[str, Any]] = []
    parse_errors: List[Dict[str, Any]] = []
    total_no_root = 0
    if args.triage_jsonl:
        triage_path = Path(args.triage_jsonl)
        if not triage_path.is_absolute():
            triage_path = root / triage_path
        triage_records, parse_errors = load_triage_records(triage_path, args)
        if args.chains_jsonl:
            chains_path = Path(args.chains_jsonl)
            if not chains_path.is_absolute():
                chains_path = root / chains_path
            chains, chain_errors = load_chains_from_jsonl(chains_path, args)
            parse_errors.extend(chain_errors)
        total_no_root = len(triage_records)
    else:
        chains, triage_records, _source_summary, parse_errors, total_no_root = build_triage_records(root, args.input, args)

    selected = select_target_cases(chains, triage_records, args)
    records = [build_case_review_record(chain, triage_record, args) for chain, triage_record in selected]
    if args.limit_cases is not None:
        records = records[: args.limit_cases]
    summary = build_summary(records, chains, triage_records, parse_errors, root, args)
    if not chains and total_no_root:
        summary["counts"]["no_root_chains"] = total_no_root
        summary["coverage_simulation"]["remaining_no_root_estimate"] = max(
            0,
            total_no_root - summary["coverage_simulation"].get("would_reduce_no_root_by", 0),
        )
    return summary, records


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    summary, records = run(args)
    output = render_output(summary, records, args)
    if args.output and not args.stdout_only:
        root = Path(args.root).resolve()
        output_path = Path(args.output)
        if not output_path.is_absolute():
            output_path = root / output_path
        validate_output_path(output_path, root)
        output_path.write_text(output, encoding="utf-8")
    else:
        sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ImportError, OSError, ValueError) as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        raise SystemExit(2)
