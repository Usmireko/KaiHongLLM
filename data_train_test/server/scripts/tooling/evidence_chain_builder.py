#!/usr/bin/env python3
"""Read-only preview tool for building auditable evidence chains.

The builder consumes legacy L1 evidence rows, normalized Evidence Blocks, or
expanded context blocks and groups them into compact per-case evidence chains.
It writes nothing by default.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, DefaultDict, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple


sys.dont_write_bytecode = True

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
    re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE),
    re.compile(r"\b(?:callerToken|accessTokenId|ssid|bssid|psk)\b", re.IGNORECASE),
    re.compile(r"\bnet_[a-z0-9_]+\b", re.IGNORECASE),
)

NORMAL_OR_RECOVERY_RE = re.compile(
    r"\b(remained\s+reachable|reachable|ok|normal|restored|recovered|after\s+the\s+fault)\b",
    re.IGNORECASE,
)
PRE_FAULT_RE = re.compile(r"\b(baseline|before|pre[-_ ]?fault|normal\s+before)\b", re.IGNORECASE)
RECOVERY_RE = re.compile(r"\b(recovery|recovered|restored|after\s+the\s+fault|post)\b", re.IGNORECASE)
EID_NUM_RE = re.compile(r"(\d+)")
INTERFACE_RE = re.compile(r"\b(?:eth\d+|wlan\d+|wifi\d+|en[a-z0-9]+)\b", re.IGNORECASE)
LINK_FLAP_PATTERNS = {"repeated_down_up", "intermittent", "oscillation"}
STRONG_FLAP_INDICATORS = {"repeated_transition", "link_oscillation", "wpa_state_cycle"}

DIAGNOSTIC_BUCKETS = (
    "baseline_facts",
    "fault_observations",
    "supporting_evidence",
    "propagation_evidence",
    "symptom_evidence",
    "contradicting_evidence",
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

PROMPT_INSTRUCTIONS = (
    "Use only diagnostic evidence from root_candidates, supporting_evidence, "
    "propagation_evidence, symptom_evidence, contradicting_evidence, and uncertain_evidence. "
    "Do not use do_not_use_evidence as support for diagnosis. "
    "Do not infer root cause from injector/provenance markers or labels. "
    "Distinguish root cause evidence from symptoms and recovery evidence. "
    "Return root_cause, root_object, evidence_used, evidence_roles, alternatives, and uncertainty."
)

OUTPUT_REQUIRED_FIELDS = (
    "root_cause",
    "root_object",
    "evidence_used",
    "evidence_roles",
    "alternatives",
    "uncertainty",
)

OUTPUT_FORBIDDEN_BEHAVIOR = (
    "do not cite do_not_use_evidence as diagnostic support",
    "do not infer root cause from injector/provenance markers",
    "do not infer root cause from labels or ground truth strings",
    "do not treat recovery evidence as root cause",
)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview auditable evidence chains.")
    parser.add_argument("--root", default=".", help="Repository root. Default: current directory.")
    parser.add_argument("--input", default=None, help="Optional evidence_candidates.jsonl file or directory.")
    parser.add_argument("--blocks-jsonl", default=None, help="Optional normalized Evidence Block JSONL input.")
    parser.add_argument("--expanded-jsonl", default=None, help="Optional expanded evidence block JSONL input.")
    parser.add_argument("--stdout-only", action="store_true", help="Force stdout output only.")
    parser.add_argument("--dry-run", action="store_true", help="Explicit safety flag; the tool is read-only by default.")
    parser.add_argument("--format", choices=("json", "markdown", "both", "jsonl"), default="both")
    parser.add_argument("--max-examples", type=int, default=10)
    parser.add_argument("--limit-cases", type=int, default=None)
    parser.add_argument("--max-root-candidates", type=int, default=3)
    parser.add_argument("--max-supporting", type=int, default=5)
    parser.add_argument("--max-symptoms", type=int, default=5)
    parser.add_argument("--max-uncertain", type=int, default=5)
    parser.add_argument("--min-utility", type=float, default=0.25)
    parser.add_argument("--before", type=int, default=4, help=argparse.SUPPRESS)
    parser.add_argument("--after", type=int, default=6, help=argparse.SUPPRESS)
    parser.add_argument("--include-symptoms", action="store_true")
    parser.add_argument("--include-uncertain", dest="include_uncertain", action="store_true", default=True)
    parser.add_argument("--no-include-uncertain", dest="include_uncertain", action="store_false")
    parser.add_argument("--include-do-not-use", dest="include_do_not_use", action="store_true", default=True)
    parser.add_argument("--no-include-do-not-use", dest="include_do_not_use", action="store_false")
    parser.add_argument("--chain-char-budget", type=int, default=8000)
    parser.add_argument("--redact-sensitive", dest="redact_sensitive", action="store_true", default=True)
    parser.add_argument("--no-redact-sensitive", dest="redact_sensitive", action="store_false")
    parser.add_argument("--output", default=None, help="Optional explicit output path.")
    return parser.parse_args(argv)


def safe_json_dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return round(max(low, min(high, value)), 4)


def read_text_lossy(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def rel_path(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def path_has_ignored_part(path: Path) -> bool:
    return any(part in IGNORED_DIRS for part in path.parts)


def iter_evidence_files(root: Path, input_path: Optional[str] = None) -> Iterator[Path]:
    if input_path:
        selected = Path(input_path)
        if not selected.is_absolute():
            selected = root / selected
        if selected.is_file():
            if selected.suffix.lower() not in BINARY_SUFFIXES:
                yield selected
            return
        if selected.is_dir():
            base = selected
        else:
            return
    else:
        base = root

    for path in sorted(base.rglob("evidence_candidates.jsonl")):
        if path_has_ignored_part(path):
            continue
        if path.is_file() and path.suffix.lower() not in BINARY_SUFFIXES:
            yield path


def read_jsonl(path: Path, root: Path, max_examples: int) -> Tuple[List[Tuple[int, Dict[str, Any]]], List[Dict[str, Any]]]:
    rows: List[Tuple[int, Dict[str, Any]]] = []
    errors: List[Dict[str, Any]] = []
    try:
        text = read_text_lossy(path)
    except OSError as exc:
        return rows, [{"file": rel_path(path, root), "line": None, "error": str(exc), "snippet": ""}]

    for line_no, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            if len(errors) < max_examples:
                errors.append({"file": rel_path(path, root), "line": line_no, "error": str(exc), "snippet": line[:240]})
            continue
        if isinstance(value, dict):
            rows.append((line_no, value))
        elif len(errors) < max_examples:
            errors.append({"file": rel_path(path, root), "line": line_no, "error": "JSONL row is not an object", "snippet": line[:240]})
    return rows, errors


def load_local_module(file_name: str, module_name: str) -> Any:
    module_path = Path(__file__).resolve().with_name(file_name)
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load {file_name} from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_normalized_blocks(path: Path, root: Path, max_examples: int) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    rows, errors = read_jsonl(path, root, max_examples)
    blocks: List[Dict[str, Any]] = []
    for line_no, row in rows:
        row.setdefault("_source_jsonl", rel_path(path, root))
        row.setdefault("_source_line", line_no)
        blocks.append(row)
    return blocks, errors


def load_expanded_blocks(path: Path, root: Path, max_examples: int) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    return load_normalized_blocks(path, root, max_examples)


def load_legacy_as_blocks(
    root: Path,
    input_path: Optional[str],
    args: argparse.Namespace,
) -> Tuple[List[Path], List[Dict[str, Any]], List[Dict[str, Any]], int, List[Dict[str, Any]]]:
    expander = load_local_module("evidence_context_expander.py", "evidence_context_expander_for_chain")
    files, blocks, parse_errors, input_rows = expander.adapt_legacy_rows(root, input_path, args.max_examples)

    expanded_blocks: List[Dict[str, Any]] = []
    try:
        selected, _selection_counts = expander.select_blocks(blocks, args)
        expanded, _resolution_counts = expander.expand_blocks(selected, root, args)
        merged = expander.merge_overlapping_blocks(expanded)
        scored = sorted(
            merged,
            key=lambda item: (
                str(item.get("case_id") or ""),
                -float(item.get("block_score") or 0.0),
                -float(item.get("density_score") or 0.0),
                len(str(item.get("context_block") or "")),
            ),
        )
        pruned, _pruning_counts, _pruned_examples = expander.prune_blocks_by_case(
            scored,
            max(1, getattr(args, "max_supporting", 5) + getattr(args, "max_root_candidates", 3)),
            max(1, args.chain_char_budget),
        )
        expanded_blocks = expander.assign_expanded_ids(pruned)
    except Exception as exc:
        parse_errors.append(
            {
                "file": "tools/evidence_context_expander.py",
                "line": None,
                "error": f"context expansion preview failed: {exc}",
                "snippet": "",
            }
        )
        expanded_blocks = []

    return files, blocks, parse_errors, input_rows, expanded_blocks


def is_sensitive_text(text: str) -> bool:
    lower = text.lower()
    if any(term in lower for term in SENSITIVE_TERMS):
        return True
    return any(pattern.search(text) for pattern in SENSITIVE_PATTERNS)


def redact_sensitive_text(text: str, enabled: bool = True) -> Tuple[str, bool]:
    if not text or not enabled:
        return text, False
    redacted = text
    changed = False
    for term in SENSITIVE_TERMS:
        pattern = re.compile(re.escape(term), re.IGNORECASE)
        redacted, count = pattern.subn("[REDACTED_SENSITIVE_CONTEXT]", redacted)
        changed = changed or count > 0
    for pattern in SENSITIVE_PATTERNS:
        redacted, count = pattern.subn("[REDACTED_SENSITIVE_CONTEXT]", redacted)
        changed = changed or count > 0
    return redacted, changed


def first_list_value(value: Any, default: str = "unknown") -> str:
    if isinstance(value, list) and value:
        return str(value[0] or default)
    if isinstance(value, str) and value:
        return value
    return default


def as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def numeric(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def joined_text(item: Dict[str, Any]) -> str:
    parts = [
        str(item.get("summary") or ""),
        str(item.get("raw_observation") or ""),
        str(item.get("context_block") or ""),
        str(item.get("key_observation") or ""),
    ]
    return "\n".join(part for part in parts if part)


def is_injector_or_provenance(item: Dict[str, Any]) -> bool:
    flags = {str(flag) for flag in item.get("risk_flags") or []}
    return (
        item.get("source") == "fault_inject"
        or item.get("anomaly_type") == "injector_marker"
        or item.get("causal_role") == "excluded_provenance"
        or "injector_marker" in flags
    )


def is_generic_noise(item: Dict[str, Any]) -> bool:
    flags = {str(flag) for flag in item.get("risk_flags") or []}
    return item.get("anomaly_type") == "noise" or item.get("causal_role") == "noise_candidate" or "generic_noise" in flags


def is_recovery_primary(item: Dict[str, Any]) -> bool:
    flags = {str(flag) for flag in item.get("risk_flags") or []}
    return "recovery_primary" in flags or (item.get("phase") == "recovery" and "legacy_primary" in flags)


def transition_pattern(item: Dict[str, Any]) -> str:
    return str(item.get("transition_pattern") or "unknown")


def flap_indicators(item: Dict[str, Any]) -> List[str]:
    return [str(indicator) for indicator in item.get("flap_indicators") or [] if indicator]


def has_strong_flap_evidence(item: Dict[str, Any]) -> bool:
    if item.get("strong_flap_evidence") is True:
        return True
    pattern = transition_pattern(item)
    if pattern in LINK_FLAP_PATTERNS:
        return True
    indicators = set(flap_indicators(item))
    if indicators & STRONG_FLAP_INDICATORS:
        return True
    text = joined_text(item).lower().replace("_", " ").replace("-", " ")
    return bool(
        re.search(r"\bflapping\b", text)
        or re.search(r"\bintermittent\b.*\blink\b", text)
        or re.search(r"\brepeated\b.*\b(disconnect|reconnect|transition|down|up)\b", text)
        or re.search(r"\bmultiple\b.*\b(disconnect|reconnect|transition|down|up)\b", text)
        or re.search(r"\blink\b.*\boscillat", text)
    )


def has_explicit_flap_signal(item: Dict[str, Any]) -> bool:
    return has_strong_flap_evidence(item)


def has_link_flap_transition_metadata(item: Dict[str, Any]) -> bool:
    return has_strong_flap_evidence(item)


def has_any_transition_metadata(item: Dict[str, Any]) -> bool:
    return (
        transition_pattern(item) != "unknown"
        or bool(flap_indicators(item))
        or bool(item.get("state_sequence"))
        or item.get("transition_count") is not None
        or bool(item.get("recovery_observed"))
        or bool(item.get("causal_ordering_notes"))
        or item.get("strong_flap_evidence") is True
    )


def should_expose_transition_metadata(item: Dict[str, Any]) -> bool:
    if not has_any_transition_metadata(item):
        return False
    anomaly = str(item.get("anomaly_type") or "")
    if anomaly in {"disconnect", "link_flap", "link_down_with_recovery_context"}:
        return True
    return (
        transition_pattern(item) != "unknown"
        or bool(flap_indicators(item))
        or bool(item.get("state_sequence"))
        or item.get("transition_count") is not None
        or bool(item.get("recovery_observed"))
        or bool(item.get("causal_ordering_notes"))
        or item.get("strong_flap_evidence") is True
    )


def transition_subject(item: Dict[str, Any]) -> str:
    text = joined_text(item).lower()
    match = INTERFACE_RE.search(text)
    if match:
        return match.group(0).lower()
    obj = str(item.get("object") or "").lower()
    return obj if obj in {"wifi", "wlan0", "network"} else "unknown"


def merge_transition_metadata(target: Dict[str, Any], metadata: Dict[str, Any]) -> None:
    existing_transition_count = target.get("transition_count")
    if metadata.get("transition_pattern"):
        target["transition_pattern"] = metadata["transition_pattern"]
    indicators = set(flap_indicators(target))
    indicators.update(str(value) for value in metadata.get("flap_indicators") or [] if value)
    if indicators:
        target["flap_indicators"] = sorted(indicators)
    sequence = [str(value) for value in target.get("state_sequence") or [] if value]
    for value in metadata.get("state_sequence") or []:
        if value and value not in sequence:
            sequence.append(str(value))
    if sequence:
        target["state_sequence"] = sequence
    if metadata.get("transition_count") is not None:
        target["transition_count"] = metadata["transition_count"]
    elif existing_transition_count is not None:
        target["transition_count"] = existing_transition_count
    if metadata.get("recovery_observed"):
        target["recovery_observed"] = True
    if metadata.get("strong_flap_evidence") is True or target.get("strong_flap_evidence") is True:
        target["strong_flap_evidence"] = True
    elif "strong_flap_evidence" in metadata and "strong_flap_evidence" not in target:
        target["strong_flap_evidence"] = False
    notes = [str(value) for value in target.get("causal_ordering_notes") or [] if value]
    for value in metadata.get("causal_ordering_notes") or []:
        if value and value not in notes:
            notes.append(str(value))
    if notes:
        target["causal_ordering_notes"] = notes


def annotate_link_transition_context(case_items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    items = [dict(item) for item in case_items]
    down_items: List[Dict[str, Any]] = []
    recovery_items: List[Dict[str, Any]] = []
    strong_flap_items: List[Dict[str, Any]] = []
    for item in items:
        text = joined_text(item).lower()
        if transition_pattern(item) == "single_down" or re.search(r"\b(transitioned\s+down|interface\s+down|link\s+down|disconnected)\b", text):
            down_items.append(item)
        if re.search(r"\b(recovered|restored|reconnect(?:ed)?|up\s+after|after\s+the\s+fault)\b", text):
            recovery_items.append(item)
        if has_strong_flap_evidence(item):
            strong_flap_items.append(item)

    for down in down_items:
        subject = transition_subject(down)
        if subject == "unknown":
            continue
        matching_recovery = [
            item for item in recovery_items
            if transition_subject(item) == subject
        ]
        if not matching_recovery:
            continue
        matching_flap = [
            item for item in strong_flap_items
            if transition_subject(item) in {subject, "network", "unknown"}
        ]
        strong_flap = bool(matching_flap)
        strong_pattern = next(
            (
                transition_pattern(item)
                for item in matching_flap
                if transition_pattern(item) in LINK_FLAP_PATTERNS
            ),
            "repeated_down_up" if strong_flap else "single_down_up",
        )
        sequence = [f"{subject}:down_during_fault", f"{subject}:up_after_fault"]
        notes = [
            f"same-interface {subject} transitioned down during fault and returned up after fault"
        ]
        metadata = {
            "transition_pattern": strong_pattern,
            "flap_indicators": ["recovery_after_disconnect"],
            "state_sequence": sequence,
            "transition_count": 2 if strong_flap else 1,
            "recovery_observed": True,
            "strong_flap_evidence": strong_flap,
            "causal_ordering_notes": notes,
        }
        if strong_flap:
            metadata["flap_indicators"] = sorted(
                set(metadata["flap_indicators"])
                | {
                    indicator
                    for item in matching_flap
                    for indicator in flap_indicators(item)
                    if indicator in STRONG_FLAP_INDICATORS
                }
            )
        merge_transition_metadata(down, metadata)
        down["_transition_support"] = True
        if strong_flap:
            down["_explicit_flap_support"] = True
        for recovery in matching_recovery:
            recovery["_transition_support"] = True
            if strong_flap:
                recovery["_explicit_flap_support"] = True
            merge_transition_metadata(recovery, metadata)
        for flap in matching_flap:
            flap["_transition_support"] = True
            flap["_explicit_flap_support"] = True
            merge_transition_metadata(flap, metadata)
    return items


def is_diagnostic_safe(item: Dict[str, Any]) -> Tuple[bool, str]:
    if is_injector_or_provenance(item):
        return False, "injector/provenance marker is not diagnostic evidence"
    if "gt_label_marker" in {str(flag) for flag in item.get("risk_flags") or []}:
        return False, "GT/label-like marker is not diagnostic evidence"
    if not item.get("usable_for_diagnosis", True):
        return False, str(item.get("exclusion_reason") or "marked non-usable for diagnosis")
    if is_generic_noise(item):
        return False, "generic/noise evidence is not diagnostic evidence"
    text = joined_text(item)
    if is_sensitive_text(text):
        return False, "unredacted GT-like or injector-label text is not diagnostic evidence"
    return True, ""


def extract_case_id(item: Dict[str, Any]) -> Tuple[str, str]:
    case_id = str(item.get("case_id") or "").strip()
    case_rel = str(item.get("case_rel") or item.get("_source_jsonl") or "").strip()
    if not case_id:
        case_id = "UNKNOWN_CASE"
    return case_id, case_rel


def normalize_input_item(item: Dict[str, Any], index: int, args: argparse.Namespace, context_by_eid: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    context_by_eid = context_by_eid or {}
    evidence_id = str(item.get("evidence_id") or item.get("expanded_id") or f"ITEM_{index:06d}")
    legacy_eids = [str(value) for value in as_list(item.get("legacy_eids") or item.get("legacy_eid")) if value]
    if not legacy_eids and item.get("legacy_eid"):
        legacy_eids = [str(item.get("legacy_eid"))]

    if item.get("expanded_id"):
        merged_eids = [str(value) for value in as_list(item.get("merged_evidence_ids")) if value]
        evidence_id = merged_eids[0] if merged_eids else str(item.get("expanded_id"))
        source = first_list_value(item.get("sources"), "unknown")
        obj = first_list_value(item.get("objects"), "unknown")
        anomaly = first_list_value(item.get("anomaly_types"), "unknown")
        role = first_list_value(item.get("causal_roles"), "unknown")
        raw = str(item.get("key_observation") or item.get("context_block") or "")
        utility = numeric(item.get("max_utility_score"), numeric(item.get("block_score"), 0.0))
        context_ref = str(item.get("expanded_id") or "")
        context_found = bool(item.get("context_found"))
        context_block = str(item.get("context_block") or "")
        phase = str(item.get("phase") or "unknown")
    else:
        source = str(item.get("source") or "unknown")
        obj = str(item.get("object") or "unknown")
        anomaly = str(item.get("anomaly_type") or "unknown")
        role = str(item.get("causal_role") or "unknown")
        raw = str(item.get("raw_observation") or item.get("text") or "")
        utility = numeric(item.get("utility_score"), numeric(item.get("score"), 0.0))
        phase = str(item.get("phase") or "unknown")
        context = context_by_eid.get(evidence_id) or {}
        context_ref = str(context.get("expanded_id") or "") or None
        context_found = bool(context.get("context_found"))
        context_block = str(context.get("context_block") or "")
        if context and not raw:
            raw = str(context.get("key_observation") or context.get("context_block") or "")

    anomaly = refine_specific_route_ip_anomaly(anomaly, " ".join([raw, context_block, evidence_id]), source, obj)
    summary_source = raw or context_block or evidence_id
    summary, redacted = redact_sensitive_text(summary_source.strip(), args.redact_sensitive)
    risk_flags = [str(flag) for flag in as_list(item.get("risk_flags")) if flag]
    if redacted and "sensitive_context_redacted" not in risk_flags:
        risk_flags.append("sensitive_context_redacted")

    case_id, case_rel = extract_case_id(item)
    normalized = {
        "evidence_id": evidence_id,
        "legacy_eids": legacy_eids,
        "case_id": case_id,
        "case_rel": case_rel,
        "source": source,
        "object": obj,
        "phase": phase,
        "anomaly_type": anomaly,
        "causal_role": role,
        "summary": summary[:800],
        "utility_score": clamp(utility),
        "context_ref": context_ref,
        "context_found": context_found,
        "risk_flags": risk_flags,
        "raw_refs": item.get("raw_refs") or [],
        "line_start": item.get("line_start") or item.get("key_line_start"),
        "order": item.get("order"),
        "timestamp": item.get("timestamp") or item.get("ts"),
        "usable_for_diagnosis": bool(item.get("usable_for_diagnosis", True)),
        "exclusion_reason": item.get("exclusion_reason"),
        "_read_order": index,
        "_original": item,
    }
    for key in (
        "transition_pattern",
        "flap_indicators",
        "state_sequence",
        "transition_count",
        "recovery_observed",
        "strong_flap_evidence",
        "causal_ordering_notes",
    ):
        if key in item:
            normalized[key] = item.get(key)
    return normalized


def refine_specific_route_ip_anomaly(anomaly: str, text: str, source: str, obj: str) -> str:
    hay = str(text or "").lower().replace("_", " ").replace("-", " ")
    locus = f"{source or ''} {obj or ''}".lower()
    route_relevant = (
        anomaly in {"route_missing", "route_black_hole"}
        or any(marker in locus for marker in ("route", "gateway", "network", "wlan", "wifi", "eth"))
    )
    ip_relevant = (
        anomaly == "ip_config_missing"
        or any(marker in locus for marker in ("iface", "interface", "network", "wlan", "wifi", "eth", "ip"))
    )
    if not route_relevant and not ip_relevant:
        return anomaly
    if route_relevant and any(
        marker in hay
        for marker in (
            "wrong default route",
            "wrong gateway",
            "incorrect default gateway",
            "route points to unreachable gateway",
            "points to unreachable gateway",
            "route points incorrectly",
            "traffic black holed",
            "traffic black-holed",
            "black holed",
            "black-holed",
        )
    ):
        return "route_black_hole"
    if ip_relevant and any(
        marker in hay
        for marker in (
            "no inet addr",
            "inet addr missing",
            "ip cleared",
            "ipv4 missing",
            "no ipv4",
            "lacks ipv4",
        )
    ):
        return "ip_config_missing"
    return anomaly


def context_map_from_expanded(expanded_blocks: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    mapping: Dict[str, Dict[str, Any]] = {}
    for block in expanded_blocks:
        for evidence_id in as_list(block.get("merged_evidence_ids")):
            if evidence_id:
                mapping[str(evidence_id)] = block
    return mapping


def legacy_eid_number(item: Dict[str, Any]) -> int:
    values = [str(item.get("evidence_id") or "")]
    values.extend(str(value) for value in item.get("legacy_eids") or [])
    for value in values:
        match = EID_NUM_RE.search(value)
        if match:
            return int(match.group(1))
    return 10**9


def sort_key_for_evidence(item: Dict[str, Any]) -> Tuple[int, str, int, int, int]:
    timestamp = str(item.get("timestamp") or "")
    has_ts = 0 if timestamp else 1
    line = item.get("line_start")
    try:
        line_no = int(line)
    except (TypeError, ValueError):
        line_no = 10**9
    order = item.get("order")
    try:
        order_no = int(order)
    except (TypeError, ValueError):
        order_no = 10**9
    return (has_ts, timestamp, order_no, line_no, legacy_eid_number(item), int(item.get("_read_order") or 0))


def evidence_ref(item: Dict[str, Any]) -> Dict[str, Any]:
    ref = {
        "evidence_id": item.get("evidence_id"),
        "legacy_eids": item.get("legacy_eids") or [],
        "source": item.get("source"),
        "object": item.get("object"),
        "phase": item.get("phase"),
        "anomaly_type": item.get("anomaly_type"),
        "causal_role": item.get("causal_role"),
        "summary": item.get("summary"),
        "utility_score": item.get("utility_score"),
        "context_ref": item.get("context_ref"),
        "risk_flags": item.get("risk_flags") or [],
    }
    for key in (
        "transition_pattern",
        "flap_indicators",
        "state_sequence",
        "transition_count",
        "recovery_observed",
        "strong_flap_evidence",
        "causal_ordering_notes",
    ):
        if not should_expose_transition_metadata(item):
            continue
        value = item.get(key)
        if key == "transition_pattern" and value == "unknown":
            continue
        if key == "recovery_observed" and value is False:
            continue
        if key == "strong_flap_evidence" and value is False:
            continue
        if value not in (None, "", [], {}):
            ref[key] = value
    return ref


def do_not_use_ref(item: Dict[str, Any], reason: str) -> Dict[str, Any]:
    return {
        "evidence_id": item.get("evidence_id"),
        "legacy_eids": item.get("legacy_eids") or [],
        "reason": reason,
        "risk_flags": item.get("risk_flags") or [],
    }


def is_baseline_like(item: Dict[str, Any]) -> bool:
    return item.get("phase") == "baseline" or bool(PRE_FAULT_RE.search(str(item.get("summary") or "")))


def is_fault_observation(item: Dict[str, Any], args: argparse.Namespace) -> bool:
    anomaly = item.get("anomaly_type")
    return (
        item.get("phase") == "fault"
        or (anomaly not in {"unknown", "noise", "injector_marker"} and numeric(item.get("utility_score")) >= args.min_utility)
    )


def is_contradicting_like(item: Dict[str, Any]) -> bool:
    return item.get("causal_role") == "contradicting" or bool(NORMAL_OR_RECOVERY_RE.search(str(item.get("summary") or "")))


def is_symptom_like(item: Dict[str, Any]) -> bool:
    return item.get("causal_role") == "symptom" or item.get("anomaly_type") in {"reachability_loss", "disconnect"}


def assign_bucket(item: Dict[str, Any], case_context: Dict[str, Any], args: argparse.Namespace) -> str:
    safe, _reason = is_diagnostic_safe(item)
    if not safe:
        return "do_not_use_evidence"
    if is_recovery_primary(item):
        if item.get("_transition_support") or has_link_flap_transition_metadata(item):
            return "supporting_evidence"
        return "do_not_use_evidence"
    if is_baseline_like(item):
        return "baseline_facts"
    if is_contradicting_like(item):
        return "contradicting_evidence"
    role = item.get("causal_role")
    if role == "supporting":
        return "supporting_evidence"
    if role == "propagation":
        return "propagation_evidence"
    if is_symptom_like(item):
        if args.include_symptoms or not case_context.get("has_root_candidate") or item.get("anomaly_type") == "reachability_loss":
            return "symptom_evidence"
        return "uncertain_evidence"
    if is_fault_observation(item, args):
        return "fault_observations"
    return "uncertain_evidence" if args.include_uncertain else "do_not_use_evidence"


def compatible_support(root_item: Dict[str, Any], support_item: Dict[str, Any]) -> bool:
    if root_item.get("evidence_id") == support_item.get("evidence_id"):
        return False
    if support_item.get("object") != "unknown" and support_item.get("object") == root_item.get("object"):
        return True
    if support_item.get("source") != "unknown" and support_item.get("source") == root_item.get("source"):
        return True
    if support_item.get("anomaly_type") != "unknown" and support_item.get("anomaly_type") == root_item.get("anomaly_type"):
        return True
    return False


def score_root_candidate(item: Dict[str, Any], supporting_items: Sequence[Dict[str, Any]], args: argparse.Namespace) -> float:
    score = numeric(item.get("utility_score"), 0.0)
    if item.get("phase") == "fault":
        score += 0.12
    if item.get("anomaly_type") not in {"unknown", "noise", "injector_marker"}:
        score += 0.10
    if item.get("object") not in {"unknown", "", None}:
        score += 0.08
    if any(compatible_support(item, support) for support in supporting_items):
        score += 0.08
    if item.get("context_found"):
        score += 0.05
    if has_link_flap_transition_metadata(item):
        score += 0.12
    if item.get("phase") == "unknown":
        score -= 0.20
    if not item.get("context_found") and len(str(item.get("summary") or "")) < 40:
        score -= 0.25
    if item.get("phase") == "recovery" or RECOVERY_RE.search(str(item.get("summary") or "")):
        score -= 0.40
    if not is_diagnostic_safe(item)[0]:
        score -= 1.00
    return clamp(score)


def build_root_candidates(case_items: Sequence[Dict[str, Any]], args: argparse.Namespace) -> List[Dict[str, Any]]:
    supporting_items = [item for item in case_items if item.get("causal_role") in {"supporting", "propagation"}]
    candidates: List[Tuple[float, Dict[str, Any]]] = []
    for item in case_items:
        if item.get("causal_role") != "root_candidate":
            continue
        if item.get("anomaly_type") in {"unknown", "noise", "injector_marker", "reachability_loss"}:
            continue
        if is_recovery_primary(item) or item.get("phase") == "recovery":
            continue
        if not is_diagnostic_safe(item)[0]:
            continue
        score = score_root_candidate(item, supporting_items, args)
        if score >= args.min_utility:
            candidates.append((score, item))

    candidates.sort(key=lambda pair: (-pair[0], sort_key_for_evidence(pair[1])))
    roots: List[Dict[str, Any]] = []
    for rank, (score, item) in enumerate(candidates[: max(1, args.max_root_candidates)], 1):
        supporting = [support for support in supporting_items if compatible_support(item, support) or support.get("_transition_support")]
        symptoms = [
            symptom
            for symptom in case_items
            if symptom.get("anomaly_type") == "reachability_loss" or symptom.get("causal_role") == "symptom"
        ]
        root_anomaly = item.get("anomaly_type")
        if item.get("anomaly_type") in {"disconnect", "link_flap"}:
            if has_link_flap_transition_metadata(item):
                root_anomaly = "link_flap"
            elif transition_pattern(item) in {"single_down_up", "down_up"} or item.get("recovery_observed") is True:
                root_anomaly = "link_down_with_recovery_context"
        why_root_candidate = "fault-phase or primary diagnostic evidence with known anomaly type and compatible supporting observations"
        if root_anomaly == "link_flap":
            why_root_candidate = "fault-phase repeated/intermittent link-state transition evidence"
        elif root_anomaly == "link_down_with_recovery_context":
            why_root_candidate = "fault-phase link-down evidence with same-interface recovery context; recovery alone is not link_flap"
        root_ref = {
            "rank": rank,
            "evidence_id": item.get("evidence_id"),
            "source": item.get("source"),
            "object": item.get("object"),
            "phase": item.get("phase"),
            "anomaly_type": root_anomaly,
            "causal_role": item.get("causal_role"),
            "summary": item.get("summary"),
            "supporting_evidence_ids": [support.get("evidence_id") for support in supporting[:3]],
            "symptom_evidence_ids": [symptom.get("evidence_id") for symptom in symptoms[:3]],
            "score": score,
            "risk_flags": item.get("risk_flags") or [],
            "why_root_candidate": why_root_candidate,
        }
        for key in (
            "transition_pattern",
            "flap_indicators",
            "state_sequence",
            "transition_count",
            "recovery_observed",
            "strong_flap_evidence",
            "causal_ordering_notes",
        ):
            if not should_expose_transition_metadata(item):
                continue
            value = item.get(key)
            if key == "strong_flap_evidence" and value is False:
                continue
            if value not in (None, "", [], {}):
                root_ref[key] = value
        roots.append(
            root_ref
        )
    return roots


def attach_supporting_evidence(root_candidates: Sequence[Dict[str, Any]], case_items: Sequence[Dict[str, Any]], args: argparse.Namespace) -> List[Dict[str, Any]]:
    root_ids = {candidate.get("evidence_id") for candidate in root_candidates}
    root_objects = {candidate.get("object") for candidate in root_candidates if candidate.get("object") not in {None, "unknown"}}
    root_anomalies = {
        candidate.get("anomaly_type")
        for candidate in root_candidates
        if candidate.get("anomaly_type") not in {None, "unknown", "noise", "injector_marker"}
    }
    selected: List[Dict[str, Any]] = []
    for item in case_items:
        if item.get("evidence_id") in root_ids:
            continue
        if item.get("causal_role") not in {"supporting", "propagation"}:
            continue
        if not is_diagnostic_safe(item)[0]:
            continue
        if root_objects and item.get("object") in root_objects:
            selected.append(item)
        elif root_anomalies and item.get("anomaly_type") in root_anomalies:
            selected.append(item)
        elif not root_candidates:
            selected.append(item)
    selected.sort(key=lambda item: (-numeric(item.get("utility_score")), sort_key_for_evidence(item)))
    return selected[: max(0, args.max_supporting)]


def root_candidate_source_items(root_candidates: Sequence[Dict[str, Any]], case_items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    root_ids = {candidate.get("evidence_id") for candidate in root_candidates}
    return [item for item in case_items if item.get("evidence_id") in root_ids]


def bucket_items(case_items: Sequence[Dict[str, Any]], args: argparse.Namespace) -> Tuple[Dict[str, List[Dict[str, Any]]], List[str]]:
    warnings: List[str] = []
    has_root_candidate = any(
        item.get("causal_role") == "root_candidate"
        and item.get("anomaly_type") not in {"unknown", "noise", "injector_marker"}
        and is_diagnostic_safe(item)[0]
        and not is_recovery_primary(item)
        for item in case_items
    )
    buckets: Dict[str, List[Dict[str, Any]]] = {bucket: [] for bucket in CHAIN_BUCKETS if bucket != "root_candidates"}
    for item in sorted(case_items, key=sort_key_for_evidence):
        bucket = assign_bucket(item, {"has_root_candidate": has_root_candidate}, args)
        if bucket == "do_not_use_evidence":
            safe, reason = is_diagnostic_safe(item)
            if safe and is_recovery_primary(item):
                reason = "recovery-primary evidence is not root-cause evidence"
            elif safe:
                reason = "not selected as diagnostic evidence"
            if args.include_do_not_use:
                buckets[bucket].append(do_not_use_ref(item, reason))
            continue
        buckets[bucket].append(evidence_ref(item))

    for bucket in ("baseline_facts", "fault_observations", "propagation_evidence", "contradicting_evidence"):
        buckets[bucket] = sorted(buckets[bucket], key=lambda ref: str(ref.get("evidence_id") or ""))[: args.max_supporting]
    buckets["supporting_evidence"] = sorted(
        buckets["supporting_evidence"],
        key=lambda ref: (-numeric(ref.get("utility_score")), str(ref.get("evidence_id") or "")),
    )[: args.max_supporting]
    buckets["symptom_evidence"] = sorted(
        buckets["symptom_evidence"],
        key=lambda ref: (-numeric(ref.get("utility_score")), str(ref.get("evidence_id") or "")),
    )[: args.max_symptoms]
    buckets["uncertain_evidence"] = sorted(
        buckets["uncertain_evidence"],
        key=lambda ref: (-numeric(ref.get("utility_score")), str(ref.get("evidence_id") or "")),
    )[: args.max_uncertain]
    buckets["do_not_use_evidence"] = buckets["do_not_use_evidence"][: max(args.max_uncertain, args.max_examples)]
    if not any(item.get("timestamp") or item.get("order") or item.get("line_start") for item in case_items):
        warnings.append("weak_temporal_ordering")
    return buckets, warnings


def score_chain(chain: Dict[str, Any]) -> Tuple[float, str]:
    root_scores = [numeric(candidate.get("score")) for candidate in chain.get("root_candidates") or []]
    score = max(root_scores) if root_scores else 0.0
    score += 0.05 * min(4, len(chain.get("supporting_evidence") or []))
    score += 0.03 * min(5, len(chain.get("fault_observations") or []))
    if not chain.get("has_context"):
        score -= 0.10
    if not chain.get("supporting_evidence"):
        score -= 0.15
    if not chain.get("root_candidates"):
        score -= 0.20
    if len(chain.get("uncertain_evidence") or []) > 4:
        score -= 0.10
    final = clamp(score)
    if final >= 0.75 and chain.get("root_candidates") and chain.get("supporting_evidence"):
        confidence = "high"
    elif final >= 0.5 and chain.get("root_candidates"):
        confidence = "medium"
    else:
        confidence = "low"
    return final, confidence


def compact_ref(ref: Dict[str, Any], summary_limit: int = 220) -> Dict[str, Any]:
    copy = dict(ref)
    summary = str(copy.get("summary") or "")
    if len(summary) > summary_limit:
        copy["summary"] = summary[: summary_limit - 3] + "..."
    return copy


def compact_safe_text(value: Any, limit: int = 220) -> Tuple[str, bool]:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r"\s+", " ", text)
    text, redacted = redact_sensitive_text(text, True)
    if len(text) > limit:
        text = text[: max(0, limit - 3)] + "..."
    return text, redacted


def case_ref_for_chain(chain: Dict[str, Any]) -> str:
    chain_id = str(chain.get("chain_id") or "")
    match = EID_NUM_RE.search(chain_id)
    if match:
        return f"CASE_{int(match.group(1)):06d}"
    return "CASE_000000"


def payload_role_for_bucket(bucket: str) -> str:
    return {
        "root_candidates": "root_candidate",
        "supporting_evidence": "supporting",
        "fault_observations": "fault_observation",
        "propagation_evidence": "propagation",
        "symptom_evidence": "symptom",
        "contradicting_evidence": "contradicting",
        "uncertain_evidence": "uncertain",
    }.get(bucket, "unknown")


def payload_diagnostic_safe(item: Dict[str, Any], bucket: str) -> bool:
    flags = {str(flag) for flag in item.get("risk_flags") or []}
    if item.get("source") == "fault_inject":
        return False
    if item.get("anomaly_type") in {"injector_marker", "noise"}:
        return False
    if item.get("causal_role") in {"excluded_provenance", "noise_candidate"}:
        return False
    if "injector_marker" in flags or "generic_noise" in flags:
        return False
    if "recovery_primary" in flags or item.get("phase") == "recovery":
        if bucket != "root_candidates" and item.get("_transition_support"):
            return True
        return False
    return True


def prompt_payload_item(item: Dict[str, Any], role: str) -> Tuple[Dict[str, Any], bool]:
    summary = item.get("summary") or item.get("why_root_candidate") or ""
    safe_summary, redacted_summary = compact_safe_text(summary, 220)
    source, redacted_source = compact_safe_text(item.get("source") or "unknown", 64)
    obj, redacted_object = compact_safe_text(item.get("object") or "unknown", 64)
    phase, redacted_phase = compact_safe_text(item.get("phase") or "unknown", 64)
    anomaly, redacted_anomaly = compact_safe_text(item.get("anomaly_type") or "unknown", 80)
    utility = numeric(item.get("utility_score"), numeric(item.get("score"), 0.0))
    context_ref = item.get("context_ref")
    if context_ref is not None:
        context_ref, redacted_context = compact_safe_text(context_ref, 80)
    else:
        redacted_context = False
    payload = {
        "evidence_id": item.get("evidence_id"),
        "role": role,
        "source": source or "unknown",
        "object": obj or "unknown",
        "phase": phase or "unknown",
        "anomaly_type": anomaly or "unknown",
        "summary": safe_summary,
        "utility_score": round(clamp(utility), 4),
        "context_ref": context_ref,
    }
    for key in (
        "transition_pattern",
        "flap_indicators",
        "state_sequence",
        "transition_count",
        "recovery_observed",
        "strong_flap_evidence",
        "causal_ordering_notes",
    ):
        if not should_expose_transition_metadata(item):
            continue
        value = item.get(key)
        if key == "transition_pattern" and value == "unknown":
            continue
        if key == "recovery_observed" and value is False:
            continue
        if key == "strong_flap_evidence" and value is False:
            continue
        if value in (None, "", [], {}):
            continue
        if key == "causal_ordering_notes":
            safe_notes: List[str] = []
            for note in value if isinstance(value, list) else [value]:
                safe_note, redacted_note = compact_safe_text(note, 180)
                safe_notes.append(safe_note)
                redacted_summary = redacted_summary or redacted_note
            payload[key] = safe_notes
        else:
            payload[key] = value
    return (
        payload,
        redacted_summary or redacted_source or redacted_object or redacted_phase or redacted_anomaly or redacted_context,
    )


def prompt_do_not_use_item(item: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    reason, redacted_reason = compact_safe_text(
        item.get("reason") or "excluded from diagnostic evidence",
        180,
    )
    safe_flags: List[str] = []
    redacted_flag = False
    for flag in item.get("risk_flags") or []:
        safe_flag, changed = compact_safe_text(flag, 80)
        safe_flags.append(safe_flag)
        redacted_flag = redacted_flag or changed
    return (
        {
            "evidence_id": item.get("evidence_id"),
            "reason": reason,
            "risk_flags": safe_flags,
            "may_not_be_used_as_support": True,
        },
        redacted_reason or redacted_flag,
    )


def prune_chain(chain: Dict[str, Any], char_budget: int) -> Dict[str, Any]:
    payload = safe_json_dump(chain)
    if len(payload) <= char_budget:
        return chain
    chain["warnings"].append("chain_payload_pruned")
    for bucket in ("uncertain_evidence", "symptom_evidence", "fault_observations", "supporting_evidence"):
        while chain.get(bucket) and len(safe_json_dump(chain)) > char_budget:
            chain[bucket].pop()
    for bucket in CHAIN_BUCKETS:
        if bucket in chain and isinstance(chain[bucket], list):
            chain[bucket] = [compact_ref(item, 140) for item in chain[bucket]]
    return chain


def build_prompt_payload(chain: Dict[str, Any]) -> Dict[str, Any]:
    evidence_chain: Dict[str, List[Dict[str, Any]]] = {
        "root_candidates": [],
        "supporting_evidence": [],
        "fault_observations": [],
        "propagation_evidence": [],
        "symptom_evidence": [],
        "contradicting_evidence": [],
        "uncertain_evidence": [],
        "do_not_use_evidence": [],
    }
    has_redactions = "sensitive_context_redacted" in {str(warning) for warning in chain.get("warnings") or []}
    for bucket in (
        "root_candidates",
        "supporting_evidence",
        "fault_observations",
        "propagation_evidence",
        "symptom_evidence",
        "contradicting_evidence",
        "uncertain_evidence",
    ):
        role = payload_role_for_bucket(bucket)
        for item in chain.get(bucket) or []:
            if not isinstance(item, dict) or not payload_diagnostic_safe(item, bucket):
                continue
            payload_item, redacted = prompt_payload_item(item, role)
            evidence_chain[bucket].append(payload_item)
            has_redactions = has_redactions or redacted
    for item in chain.get("do_not_use_evidence") or []:
        if not isinstance(item, dict):
            continue
        payload_item, redacted = prompt_do_not_use_item(item)
        evidence_chain["do_not_use_evidence"].append(payload_item)
        has_redactions = has_redactions or redacted

    diagnostic_item_count = sum(len(evidence_chain[bucket]) for bucket in evidence_chain if bucket != "do_not_use_evidence")
    return {
        "case_ref": case_ref_for_chain(chain),
        "case_id_redacted": True,
        "task": "root_cause_analysis_from_evidence_chain",
        "safety_policy": {
            "use_diagnostic_buckets_only": True,
            "do_not_use_provenance_or_labels": True,
            "do_not_use_evidence_is_exclusion_only": True,
            "redacted_sensitive_context": True,
        },
        "evidence_chain": evidence_chain,
        "output_contract": {
            "required_fields": list(OUTPUT_REQUIRED_FIELDS),
            "forbidden_behavior": list(OUTPUT_FORBIDDEN_BEHAVIOR),
        },
        "readiness_metadata": {
            "has_root_candidate": bool(evidence_chain["root_candidates"]),
            "has_supporting_evidence": bool(evidence_chain["supporting_evidence"]),
            "has_redactions": bool(has_redactions),
            "has_context": bool(chain.get("has_context")),
            "chain_confidence": chain.get("chain_confidence") or "low",
            "chain_score": numeric(chain.get("chain_score"), 0.0),
            "diagnostic_item_count": diagnostic_item_count,
            "do_not_use_item_count": len(evidence_chain["do_not_use_evidence"]),
        },
    }


def build_reasoning_trace(chain: Dict[str, Any]) -> List[str]:
    trace: List[str] = []
    roots = chain.get("root_candidates") or []
    if roots:
        top = roots[0]
        trace.append(
            f"Strongest non-provenance candidate: {top.get('anomaly_type')} on {top.get('object')} from {top.get('evidence_id')}."
        )
    else:
        trace.append("No safe root candidate with known anomaly type met the threshold.")
    if chain.get("supporting_evidence"):
        trace.append("Supporting evidence was kept separately from root candidates.")
    if chain.get("symptom_evidence"):
        trace.append("Symptom evidence is present and should not be treated as root cause by itself.")
    if chain.get("contradicting_evidence"):
        trace.append("Contradicting or recovery-like evidence was kept as a separate check.")
    if chain.get("do_not_use_evidence"):
        trace.append("Injector/provenance, noise, or unsafe evidence was excluded from diagnostic buckets.")
    return trace[:5]


def validate_chain_safety(chain: Dict[str, Any]) -> Dict[str, int]:
    counts = Counter()
    for bucket in DIAGNOSTIC_BUCKETS:
        for item in chain.get(bucket) or []:
            text = str(item.get("summary") or "")
            flags = {str(flag) for flag in item.get("risk_flags") or []}
            if item.get("source") == "fault_inject" or item.get("anomaly_type") == "injector_marker" or item.get("causal_role") == "excluded_provenance" or "injector_marker" in flags:
                counts["injector_in_diagnostic_buckets"] += 1
            if is_sensitive_text(text):
                counts["unredacted_sensitive_context"] += 1
                counts["gt_like_in_diagnostic_buckets"] += 1
            if item.get("anomaly_type") == "noise" or item.get("causal_role") == "noise_candidate" or "generic_noise" in flags:
                counts["generic_noise_as_diagnostic"] += 1
    for candidate in chain.get("root_candidates") or []:
        text = str(candidate.get("summary") or "")
        if RECOVERY_RE.search(text):
            counts["recovery_primary_as_root_candidate"] += 1
        if is_sensitive_text(text):
            counts["unredacted_sensitive_context"] += 1
            counts["gt_like_in_diagnostic_buckets"] += 1
        if candidate.get("anomaly_type") == "injector_marker":
            counts["injector_in_diagnostic_buckets"] += 1
    return dict(counts)


def build_chain_for_case(chain_index: int, case_id: str, case_rel: str, case_items: Sequence[Dict[str, Any]], args: argparse.Namespace) -> Dict[str, Any]:
    case_items = annotate_link_transition_context(case_items)
    roots = build_root_candidates(case_items, args)
    buckets, warnings = bucket_items(case_items, args)
    root_items = root_candidate_source_items(roots, case_items)
    fault_ids = {item.get("evidence_id") for item in root_items}
    for item in root_items:
        if is_fault_observation(item, args):
            ref = evidence_ref(item)
            if not any(existing.get("evidence_id") == ref.get("evidence_id") for existing in buckets["fault_observations"]):
                buckets["fault_observations"].insert(0, ref)
    supporting = attach_supporting_evidence(roots, case_items, args)
    for item in supporting:
        ref = evidence_ref(item)
        if not any(existing.get("evidence_id") == ref.get("evidence_id") for existing in buckets["supporting_evidence"]):
            buckets["supporting_evidence"].append(ref)
    buckets["supporting_evidence"] = buckets["supporting_evidence"][: args.max_supporting]
    has_context = any(item.get("context_found") or item.get("context_ref") for item in case_items)
    if not roots:
        warnings.append("no_root_candidates")
    if any("sensitive_context_redacted" in (item.get("risk_flags") or []) for item in case_items):
        warnings.append("sensitive_context_redacted")
    chain = {
        "chain_id": f"CHAIN_{chain_index:04d}",
        "case_id": case_id,
        "case_rel": case_rel,
        "chain_score": 0.0,
        "chain_confidence": "low",
        "has_context": has_context,
        "warnings": sorted(set(warnings)),
        "baseline_facts": buckets["baseline_facts"],
        "fault_observations": buckets["fault_observations"],
        "root_candidates": roots,
        "supporting_evidence": buckets["supporting_evidence"],
        "propagation_evidence": buckets["propagation_evidence"],
        "symptom_evidence": buckets["symptom_evidence"],
        "contradicting_evidence": buckets["contradicting_evidence"],
        "uncertain_evidence": buckets["uncertain_evidence"],
        "do_not_use_evidence": buckets["do_not_use_evidence"],
        "reasoning_trace": [],
        "recommended_prompt_payload": {},
    }
    score, confidence = score_chain(chain)
    chain["chain_score"] = score
    chain["chain_confidence"] = confidence
    chain = prune_chain(chain, max(1000, args.chain_char_budget))
    chain["reasoning_trace"] = build_reasoning_trace(chain)
    chain["recommended_prompt_payload"] = build_prompt_payload(chain)
    return chain


def group_items_by_case(items: Sequence[Dict[str, Any]]) -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
    groups: DefaultDict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for item in items:
        groups[(str(item.get("case_id") or "UNKNOWN_CASE"), str(item.get("case_rel") or ""))].append(item)
    return dict(groups)


def build_chains(items: Sequence[Dict[str, Any]], args: argparse.Namespace) -> List[Dict[str, Any]]:
    groups = group_items_by_case(items)
    chains: List[Dict[str, Any]] = []
    for idx, ((case_id, case_rel), case_items) in enumerate(sorted(groups.items()), 1):
        chains.append(build_chain_for_case(idx, case_id, case_rel, case_items, args))
    chains.sort(key=lambda chain: (str(chain.get("case_id") or ""), str(chain.get("case_rel") or "")))
    if args.limit_cases is not None:
        return chains[: args.limit_cases]
    return chains


def load_inputs(args: argparse.Namespace) -> Tuple[List[Path], List[Dict[str, Any]], List[Dict[str, Any]], int, int]:
    root = Path(args.root).resolve()
    expanded_count = 0
    evidence_files: List[Path] = []
    input_rows = 0
    parse_errors: List[Dict[str, Any]] = []
    normalized_items: List[Dict[str, Any]] = []

    if args.expanded_jsonl:
        path = Path(args.expanded_jsonl)
        if not path.is_absolute():
            path = root / path
        expanded_blocks, parse_errors = load_expanded_blocks(path, root, args.max_examples)
        expanded_count = len(expanded_blocks)
        input_rows = len(expanded_blocks)
        normalized_items = [
            normalize_input_item(block, index, args)
            for index, block in enumerate(expanded_blocks, 1)
        ]
    elif args.blocks_jsonl:
        path = Path(args.blocks_jsonl)
        if not path.is_absolute():
            path = root / path
        blocks, parse_errors = load_normalized_blocks(path, root, args.max_examples)
        input_rows = len(blocks)
        normalized_items = [
            normalize_input_item(block, index, args)
            for index, block in enumerate(blocks, 1)
        ]
    else:
        evidence_files, blocks, parse_errors, input_rows, expanded_blocks = load_legacy_as_blocks(root, args.input, args)
        expanded_count = len(expanded_blocks)
        context_by_eid = context_map_from_expanded(expanded_blocks)
        normalized_items = [
            normalize_input_item(block, index, args, context_by_eid)
            for index, block in enumerate(blocks, 1)
        ]
    return evidence_files, normalized_items, parse_errors, input_rows, expanded_count


def example_chain(chain: Dict[str, Any]) -> Dict[str, Any]:
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


def build_summary(
    root: Path,
    dry_run: bool,
    evidence_files: Sequence[Path],
    input_rows: int,
    normalized_blocks: int,
    expanded_blocks: int,
    all_chains: Sequence[Dict[str, Any]],
    output_chains: Sequence[Dict[str, Any]],
    parse_errors: Sequence[Dict[str, Any]],
    args: argparse.Namespace,
) -> Dict[str, Any]:
    bucket_counts = Counter()
    safety_counts = Counter(
        {
            "injector_in_diagnostic_buckets": 0,
            "gt_like_in_diagnostic_buckets": 0,
            "unredacted_sensitive_context": 0,
            "recovery_primary_as_root_candidate": 0,
            "generic_noise_as_diagnostic": 0,
        }
    )
    confidence = Counter()
    warnings = Counter()
    for chain in output_chains:
        confidence[str(chain.get("chain_confidence") or "low")] += 1
        for warning in chain.get("warnings") or []:
            warnings[str(warning)] += 1
        for bucket in CHAIN_BUCKETS:
            bucket_counts[bucket] += len(chain.get(bucket) or [])
        safety_counts.update(validate_chain_safety(chain))

    chains_with_root = sum(1 for chain in output_chains if chain.get("root_candidates"))
    chains_without_root = len(output_chains) - chains_with_root
    examples = {
        "chains_high": [example_chain(chain) for chain in output_chains if chain.get("chain_confidence") == "high"][: args.max_examples],
        "chains_low": [example_chain(chain) for chain in output_chains if chain.get("chain_confidence") == "low"][: args.max_examples],
        "chains_without_root_candidates": [example_chain(chain) for chain in output_chains if not chain.get("root_candidates")][: args.max_examples],
        "do_not_use_examples": [
            item
            for chain in output_chains
            for item in (chain.get("do_not_use_evidence") or [])
        ][: args.max_examples],
        "parse_errors": list(parse_errors)[: args.max_examples],
    }
    return {
        "root": str(root),
        "dry_run": bool(dry_run),
        "counts": {
            "evidence_files": len(evidence_files),
            "input_rows": input_rows,
            "normalized_blocks": normalized_blocks,
            "expanded_blocks": expanded_blocks,
            "cases": len(all_chains),
            "chains": len(output_chains),
            "chains_with_root_candidates": chains_with_root,
            "chains_without_root_candidates": chains_without_root,
            "parse_errors": len(parse_errors),
        },
        "bucket_counts": {bucket: bucket_counts.get(bucket, 0) for bucket in CHAIN_BUCKETS},
        "safety": {key: safety_counts.get(key, 0) for key in (
            "injector_in_diagnostic_buckets",
            "gt_like_in_diagnostic_buckets",
            "unredacted_sensitive_context",
            "recovery_primary_as_root_candidate",
            "generic_noise_as_diagnostic",
        )},
        "confidence_distribution": {
            "high": confidence.get("high", 0),
            "medium": confidence.get("medium", 0),
            "low": confidence.get("low", 0),
        },
        "warnings": dict(warnings.most_common()),
        "examples": examples,
    }


def print_json_summary(summary: Dict[str, Any]) -> str:
    return json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True)


def markdown_table(mapping: Dict[str, int], key_title: str) -> List[str]:
    lines = [f"| {key_title} | count |", "|---|---:|"]
    if not mapping:
        lines.append("| none | 0 |")
    else:
        for key, value in mapping.items():
            lines.append(f"| `{key}` | {value} |")
    return lines


def report_warnings(summary: Dict[str, Any]) -> List[str]:
    warnings: List[str] = []
    safety = summary.get("safety") or {}
    if safety.get("injector_in_diagnostic_buckets"):
        warnings.append("INJECTOR_IN_DIAGNOSTIC_BUCKET")
    if safety.get("gt_like_in_diagnostic_buckets"):
        warnings.append("GT_LIKE_IN_DIAGNOSTIC_BUCKET")
    if safety.get("recovery_primary_as_root_candidate"):
        warnings.append("RECOVERY_PRIMARY_AS_ROOT")
    if summary["counts"].get("chains_without_root_candidates"):
        warnings.append("NO_ROOT_CANDIDATES")
    if summary.get("warnings", {}).get("weak_temporal_ordering"):
        warnings.append("WEAK_TEMPORAL_ORDERING")
    if summary.get("warnings", {}).get("chain_payload_pruned"):
        warnings.append("CHAIN_PAYLOAD_PRUNED")
    return warnings


def print_markdown_summary(summary: Dict[str, Any]) -> str:
    lines = [
        "# Evidence Chain Builder Preview Report",
        "",
        "This tool is read-only by default and does not modify existing evidence files.",
        "Injector/provenance markers are excluded from diagnostic buckets by design.",
        "Sensitive GT-like or injector-label context is redacted by default.",
        "",
        "## Counts",
    ]
    for key, value in summary["counts"].items():
        lines.append(f"- `{key}`: {value}")
    lines.extend(["", "## Bucket Summary"])
    lines.extend(markdown_table(summary["bucket_counts"], "bucket"))
    lines.extend(["", "## Safety Summary"])
    lines.extend(markdown_table(summary["safety"], "safety_check"))
    lines.extend(["", "## Confidence Distribution"])
    lines.extend(markdown_table(summary["confidence_distribution"], "confidence"))
    lines.extend(["", "## Warning Summary"])
    lines.extend(markdown_table(summary["warnings"], "warning"))
    lines.extend(["", "## Examples"])
    for title, key in (
        ("High-confidence chains", "chains_high"),
        ("Low-confidence chains", "chains_low"),
        ("Chains without root candidates", "chains_without_root_candidates"),
        ("Do-not-use evidence", "do_not_use_examples"),
        ("Parse errors", "parse_errors"),
    ):
        lines.append(f"### {title}")
        examples = summary["examples"].get(key) or []
        if not examples:
            lines.append("- none")
        else:
            for item in examples:
                lines.append(f"- `{item}`")
        lines.append("")
    warnings = report_warnings(summary)
    lines.append("## Warnings")
    if not warnings:
        lines.append("- none")
    else:
        for warning in warnings:
            lines.append(f"- `{warning}`")
    return "\n".join(lines).rstrip() + "\n"


def print_jsonl_chains(chains: Sequence[Dict[str, Any]]) -> str:
    return "\n".join(safe_json_dump(chain) for chain in chains)


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


def render_output(summary: Dict[str, Any], chains: Sequence[Dict[str, Any]], args: argparse.Namespace) -> str:
    if args.format == "json":
        return print_json_summary(summary) + "\n"
    if args.format == "markdown":
        return print_markdown_summary(summary)
    if args.format == "jsonl":
        return print_jsonl_chains(chains) + ("\n" if chains else "")
    return "----- JSON -----\n" + print_json_summary(summary) + "\n\n----- MARKDOWN -----\n" + print_markdown_summary(summary)


def run(args: argparse.Namespace) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    root = Path(args.root).resolve()
    evidence_files, items, parse_errors, input_rows, expanded_count = load_inputs(args)
    all_chains = build_chains(items, args)
    output_chains = all_chains[: args.limit_cases] if args.limit_cases is not None else all_chains
    summary = build_summary(
        root=root,
        dry_run=args.dry_run,
        evidence_files=evidence_files,
        input_rows=input_rows,
        normalized_blocks=len(items),
        expanded_blocks=expanded_count,
        all_chains=all_chains,
        output_chains=output_chains,
        parse_errors=parse_errors,
        args=args,
    )
    return summary, output_chains


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    root = Path(args.root).resolve()
    try:
        summary, chains = run(args)
    except RuntimeError as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 2
    output = render_output(summary, chains, args)
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
