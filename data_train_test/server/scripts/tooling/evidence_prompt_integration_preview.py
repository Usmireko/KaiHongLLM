#!/usr/bin/env python3
"""Read-only prompt integration preview for evidence chains.

This tool builds or loads evidence chains, renders their recommended prompt
payloads into LLM-ready message previews, and statically validates that prompt
content keeps provenance, labels, and do-not-use evidence outside diagnostic
support. It does not call any LLM and writes nothing by default.
"""

from __future__ import annotations

import argparse
import copy
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

PROMPT_STEERING_VERSION = "p1_2_specific_root_contract"

LINK_FLAP_PROMPT_INDICATORS = {
    "link_oscillation",
    "repeated_transition",
    "wpa_state_cycle",
}

REQUIRED_SAFETY_POLICY = {
    "use_diagnostic_buckets_only": True,
    "do_not_use_provenance_or_labels": True,
    "do_not_use_evidence_is_exclusion_only": True,
    "redacted_sensitive_context": True,
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

REDACTION_MARKER = "[REDACTED_SENSITIVE_CONTEXT]"

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

OUTPUT_REQUIRED_FIELDS = (
    "root_cause",
    "root_object",
    "evidence_used",
    "evidence_roles",
    "alternatives",
    "uncertainty",
)

SYSTEM_MESSAGE = (
    "You are a root cause analysis assistant for KaiHongOS/OpenHarmony network "
    "fault evidence. Use only diagnostic evidence from root_candidates, "
    "supporting_evidence, fault_observations, propagation_evidence, "
    "symptom_evidence, contradicting_evidence, and uncertain_evidence. "
    "Do not use do_not_use_evidence as support. Do not infer root cause from "
    "[REDACTED_SENSITIVE_CONTEXT]/provenance markers, scenario labels, ground truth fields, or "
    "redacted content. Do not treat recovery evidence as root cause. Distinguish "
    "root cause evidence from symptoms. A concrete root_cause must be supported "
    "by at least one evidence_id in evidence_used. If root_candidates are "
    "present, a concrete root_cause should cite at least one root_candidate "
    "evidence_id. If no evidence_id supports the answer, return "
    "insufficient_evidence. Do not output a concrete root_cause with "
    "evidence_used=[]. Every ID in evidence_used must appear exactly once as a "
    "key in evidence_roles, and evidence_roles must not include unused evidence "
    "IDs. Do not cite "
    "supporting or symptom evidence alone as the root cause when a root_candidate "
    "is available. When evidence supports a specific route/IP root candidate, "
    "prefer the specific root over a generic network connectivity issue. "
    "Distinguish wrong default route from missing default route: wrong route "
    "means a route/gateway exists but points incorrectly; missing route means "
    "no default route is present. Distinguish no IPv4 on interface from generic "
    "network issue when interface address evidence is present. Apply "
    "root_candidate_resolution_hints only when they are "
    "present in the user payload. Use link_flap only when repeated/intermittent/"
    "oscillating down-up transitions are present. A single down-up recovery is "
    "link-down recovery context, not link_flap by itself. Output only valid JSON matching the requested "
    "schema. Do not include hidden chain-of-thought; provide only a concise "
    "auditable reasoning summary if requested by the schema."
)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview evidence-chain prompt integration.")
    parser.add_argument("--root", default=".", help="Repository root. Default: current directory.")
    parser.add_argument("--input", default=None, help="Optional evidence_candidates.jsonl file or directory.")
    parser.add_argument("--chains-jsonl", default=None, help="Optional chain JSONL input from evidence_chain_builder.py.")
    parser.add_argument("--stdout-only", action="store_true", help="Force stdout output only.")
    parser.add_argument("--dry-run", action="store_true", help="Explicit safety flag; this tool is read-only by default.")
    parser.add_argument("--format", choices=("json", "markdown", "both", "jsonl", "prompt"), default="both")
    parser.add_argument(
        "--prompt-style",
        choices=("json_contract", "compact_markdown", "both"),
        default="json_contract",
    )
    parser.add_argument(
        "--select",
        choices=("review_ready", "ready", "root_candidate", "all"),
        default="review_ready",
    )
    parser.add_argument("--include-no-root", action="store_true")
    parser.add_argument("--limit-prompts", type=int, default=None)
    parser.add_argument("--max-examples", type=int, default=10)
    parser.add_argument("--prompt-char-budget", type=int, default=12000)
    parser.add_argument("--max-root-candidates", type=int, default=3)
    parser.add_argument("--max-supporting", type=int, default=5)
    parser.add_argument("--max-symptoms", type=int, default=5)
    parser.add_argument("--max-uncertain", type=int, default=5)
    parser.add_argument("--include-do-not-use", dest="include_do_not_use", action="store_true", default=True)
    parser.add_argument("--no-include-do-not-use", dest="include_do_not_use", action="store_false")
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


def numeric(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def percentile(values: Sequence[float], p: float) -> float:
    if not values:
        return 0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(float(ordered[0]), 4)
    idx = (len(ordered) - 1) * (p / 100.0)
    lo = int(idx)
    hi = min(lo + 1, len(ordered) - 1)
    frac = idx - lo
    return round(float(ordered[lo] * (1 - frac) + ordered[hi] * frac), 4)


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
                        "text": compact_text(line),
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
                    "text": compact_text(value),
                }
            )
    return chains, parse_errors


def build_chains_from_root(root: Path, input_path: Optional[str], args: argparse.Namespace) -> Tuple[List[Dict[str, Any]], Dict[str, Any], List[Dict[str, Any]]]:
    builder = safe_import_module(root / "tools" / "evidence_chain_builder.py", "evidence_chain_builder_for_prompt_preview")
    builder_argv = [
        "--root",
        str(root),
        "--dry-run",
        "--stdout-only",
        "--format",
        "json",
        "--max-examples",
        str(args.max_examples),
        "--max-root-candidates",
        str(args.max_root_candidates),
        "--max-supporting",
        str(args.max_supporting),
        "--max-symptoms",
        str(args.max_symptoms),
        "--max-uncertain",
        str(args.max_uncertain),
    ]
    if input_path:
        builder_argv.extend(["--input", input_path])
    builder_args = builder.parse_args(builder_argv)
    summary, chains = builder.run(builder_args)
    return list(chains), dict(summary), list(summary.get("examples", {}).get("parse_errors", []) or [])


def load_or_build_chains(args: argparse.Namespace) -> Tuple[List[Dict[str, Any]], Dict[str, Any], List[Dict[str, Any]]]:
    root = Path(args.root).resolve()
    if args.chains_jsonl:
        path = Path(args.chains_jsonl)
        if not path.is_absolute():
            path = root / path
        chains, parse_errors = load_chains_from_jsonl(path, args.max_examples)
        source_summary = {
            "mode": "chains_jsonl",
            "chains": len(chains),
            "parse_errors": len(parse_errors),
            "source": str(path),
        }
        return chains, source_summary, parse_errors
    return build_chains_from_root(root, args.input, args)


def make_checker_args(root: Path, args: argparse.Namespace) -> argparse.Namespace:
    checker = safe_import_module(root / "tools" / "evidence_chain_regression_check.py", "evidence_chain_regression_for_prompt_preview")
    checker_argv = [
        "--root",
        str(root),
        "--dry-run",
        "--stdout-only",
        "--format",
        "json",
        "--max-examples",
        str(args.max_examples),
    ]
    return checker.parse_args(checker_argv)


def analyze_chain_readiness(root: Path, chains: Sequence[Dict[str, Any]], args: argparse.Namespace) -> Dict[str, Dict[str, Any]]:
    checker = safe_import_module(root / "tools" / "evidence_chain_regression_check.py", "evidence_chain_regression_for_prompt_readiness")
    checker_args = checker.parse_args(
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
    )
    safety_by_chain = checker.compute_chain_safety_flags(chains)
    result: Dict[str, Dict[str, Any]] = {}
    for chain in chains:
        chain_id = str(chain.get("chain_id") or "")
        readiness, blockers, review_warnings = checker.analyze_prompt_payload(
            chain,
            checker_args,
            safety_failed=bool(safety_by_chain.get(chain_id, False)),
        )
        result[chain_id] = {
            "readiness": readiness,
            "blockers": blockers,
            "review_warnings": review_warnings,
            "safety_failed": bool(safety_by_chain.get(chain_id, False)),
        }
    return result


def select_chains(chains: Sequence[Dict[str, Any]], readiness: Dict[str, Dict[str, Any]], args: argparse.Namespace) -> List[Tuple[Dict[str, Any], str, List[str], List[str]]]:
    selected: List[Tuple[Dict[str, Any], str, List[str], List[str]]] = []
    for chain in chains:
        info = readiness.get(str(chain.get("chain_id") or ""), {})
        chain_readiness = str(info.get("readiness") or "not_ready")
        blockers = list(info.get("blockers") or [])
        review_warnings = list(info.get("review_warnings") or [])
        has_root = bool(chain.get("root_candidates"))
        safety_failed = bool(info.get("safety_failed"))
        selected_readiness = chain_readiness
        include = False

        if not has_root:
            selected_readiness = "coverage_gap"
            include = args.include_no_root or args.select == "all"
            if "no_root_candidate" not in blockers:
                blockers.append("no_root_candidate")
        elif safety_failed:
            selected_readiness = "not_ready"
            include = args.select == "all"
        elif args.select == "ready":
            include = chain_readiness == "ready"
        elif args.select == "review_ready":
            include = chain_readiness in {"ready", "review_ready"}
        elif args.select == "root_candidate":
            include = True
            if chain_readiness == "not_ready":
                selected_readiness = "not_ready"
        elif args.select == "all":
            include = True

        if include:
            selected.append((chain, selected_readiness, blockers, review_warnings))
    if args.limit_prompts is not None:
        selected = selected[: max(0, args.limit_prompts)]
    return selected


def build_system_message(args: argparse.Namespace) -> str:
    return SYSTEM_MESSAGE


def build_expected_output_schema() -> Dict[str, Any]:
    return {
        "root_cause": "Use a concise string. Use 'insufficient_evidence' if no evidence ID supports a root cause.",
        "root_object": "Use a concise string or 'unknown'.",
        "evidence_used": "List only diagnostic evidence IDs used to support root_cause. Must be non-empty for concrete root_cause.",
        "evidence_roles": (
            "Map every evidence_used ID to exactly one role. Keys must be evidence IDs, "
            "not role names. evidence_roles keys must be a subset of evidence_used, and "
            "no evidence_used ID may be omitted."
        ),
        "alternatives": [
            {
                "root_cause": "string",
                "root_object": "string",
                "evidence_used": ["evidence_id"],
                "reason": "string",
            }
        ],
        "alternatives_rule": "Use [] if no evidence-backed alternative exists.",
        "uncertainty": "Briefly explain evidence limitations without claiming external context.",
    }


def evidence_chain_counts(evidence_chain: Dict[str, Any]) -> Dict[str, int]:
    return {
        "root_candidate_count": len(evidence_chain.get("root_candidates") or []),
        "supporting_count": len(evidence_chain.get("supporting_evidence") or []),
        "symptom_count": len(evidence_chain.get("symptom_evidence") or []),
        "contradicting_count": len(evidence_chain.get("contradicting_evidence") or []),
        "uncertain_count": len(evidence_chain.get("uncertain_evidence") or []),
        "do_not_use_count": len(evidence_chain.get("do_not_use_evidence") or []),
    }


def evidence_item_text(item: Dict[str, Any]) -> str:
    parts = [
        item.get("source"),
        item.get("object"),
        item.get("anomaly_type"),
        item.get("role"),
        item.get("summary"),
        item.get("transition_pattern"),
        item.get("flap_indicators"),
        item.get("state_sequence"),
        item.get("causal_ordering_notes"),
        item.get("strong_flap_evidence"),
    ]
    return " ".join(safe_text(part).lower() for part in parts if part is not None)


def sanitize_prompt_visible_summary(summary: Any) -> Any:
    if not isinstance(summary, str):
        return summary
    cleaned = summary
    # Keep evidence observations factual. Earlier prompt packs carried analysis
    # language here, which acted like a hidden root-candidate hint.
    cleaned = re.sub(
        r"\s*,?\s*but\s+subtype\s+boundary\s+is\s+dominated\s+by\s+link-state\s+evidence\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\s*,?\s*but\s+subtype\s+boundary\s+is\s+dominated\s+by\s+link\s+state\s+evidence\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\s*,?\s*but\s+.*?\bsubtype\s+boundary\b.*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\s*,?\s*but\s+.*?\bdominated\s+by\s+link[- ]state\s+evidence\b.*$",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", cleaned).strip()


def sanitize_prompt_visible_item(item: Dict[str, Any]) -> Dict[str, Any]:
    sanitized = dict(item)
    if "summary" in sanitized:
        sanitized["summary"] = sanitize_prompt_visible_summary(sanitized.get("summary"))
    return sanitized


def item_is_dns_signal(item: Dict[str, Any]) -> bool:
    text = evidence_item_text(item)
    return any(
        marker in text
        for marker in (
            "dns",
            "resolver",
            "name resolution",
            "resolution failed",
            "dns_failure",
        )
    )


def item_is_link_state_signal(item: Dict[str, Any]) -> bool:
    text = evidence_item_text(item)
    return any(
        marker in text
        for marker in (
            "disconnect",
            "disconnected",
            "link down",
            "link-state",
            "link state",
            "interface down",
            "interface instability",
            "transitioned down",
            "wlan",
            "wifi",
            "wi-fi",
            "carrier",
            "reconnect",
            "recovery",
            "down/up",
            "down-up",
            "oscillation",
            "intermittent",
            "unstable link",
        )
    )


def item_has_explicit_flap_semantics(item: Dict[str, Any]) -> bool:
    if item.get("strong_flap_evidence") is True:
        return True
    pattern = str(item.get("transition_pattern") or "")
    if pattern in {"repeated_down_up", "intermittent", "oscillation"}:
        return True
    indicators = {str(value) for value in item.get("flap_indicators") or [] if value}
    if indicators & LINK_FLAP_PROMPT_INDICATORS:
        return True
    text = evidence_item_text(item)
    explicit_patterns = (
        r"\bflap(?:ping)?\b",
        r"\bintermittent\b.*\blink\b",
        r"\brepeated\b.*\bdisconnect",
        r"\brepeated\b.*\breconnect",
        r"\brepeated\b.*\bdown\b.*\bup\b",
        r"\blink\b.*\boscillat",
        r"\bunstable\b.*\blink\b.*\bcycle",
        r"\bmultiple\b.*\bstate\b.*\btransition",
    )
    return any(re.search(pattern, text) for pattern in explicit_patterns)


def item_has_dns_after_link_transition_semantics(item: Dict[str, Any]) -> bool:
    text = evidence_item_text(item)
    if not item_is_dns_signal(item):
        return False
    ordering_patterns = (
        r"\bdns\b.*\bafter\b.*\blink",
        r"\bdns\b.*\bfollowing\b.*\blink",
        r"\bdns\b.*\bduring\b.*\blink[- ]state\b.*\btransition",
        r"\bdns\b.*\bduring\b.*\binterface instability",
        r"\bdns\b.*\bduring\b.*\bdown[-/ ]up",
        r"\bdns\b.*\bafter\b.*\binterface\b.*\btransition",
        r"\bname resolution\b.*\bafter\b.*\blink",
        r"\bresolver\b.*\bafter\b.*\blink",
        r"\boccurs\b.*\bafter\b.*\blink[- ]state\b.*\btransition",
    )
    return any(re.search(pattern, text) for pattern in ordering_patterns)


def item_has_no_ipv4_specificity(item: Dict[str, Any]) -> bool:
    text = evidence_item_text(item)
    return (
        str(item.get("anomaly_type") or "") == "ip_config_missing"
        or any(
            marker in text
            for marker in (
                "no inet addr",
                "inet addr missing",
                "ipv4 missing",
                "no ipv4",
                "ip cleared",
                "interface address evidence",
                "lacks ipv4",
            )
        )
    )


def item_has_wrong_default_route_specificity(item: Dict[str, Any]) -> bool:
    text = evidence_item_text(item)
    return (
        str(item.get("anomaly_type") or "") in {"route_black_hole", "wrong_default_route"}
        or any(
            marker in text
            for marker in (
                "wrong default route",
                "wrong gateway",
                "incorrect default gateway",
                "points to unreachable gateway",
                "route points",
                "traffic black-holed",
                "black-holed",
                "black holed",
            )
        )
    )


def item_has_missing_default_route_specificity(item: Dict[str, Any]) -> bool:
    text = evidence_item_text(item)
    return (
        str(item.get("anomaly_type") or "") == "route_missing"
        and any(
            marker in text
            for marker in (
                "no default route",
                "default route missing",
                "missing default route",
                "route missing",
            )
        )
        and not item_has_wrong_default_route_specificity(item)
    )


def chain_has_dns_after_link_transition(evidence_chain: Dict[str, Any]) -> bool:
    for bucket in DIAGNOSTIC_BUCKETS:
        for item in evidence_chain.get(bucket) or []:
            if not isinstance(item, dict):
                continue
            notes = " ".join(safe_text(note).lower() for note in item.get("causal_ordering_notes") or [])
            if "same-interface" in notes and ("dns" in notes or item_is_dns_signal(item)):
                return True
    for bucket in DIAGNOSTIC_BUCKETS:
        for item in evidence_chain.get(bucket) or []:
            if isinstance(item, dict) and item_has_dns_after_link_transition_semantics(item):
                return True
    chain_text = " ".join(
        evidence_item_text(item)
        for bucket in DIAGNOSTIC_BUCKETS
        for item in evidence_chain.get(bucket) or []
        if isinstance(item, dict)
    )
    combined_patterns = (
        r"\blink[- ]state\b.*\btransition\b.*\bbefore\b.*\bdns",
        r"\binterface\b.*\btransition\b.*\bbefore\b.*\bdns",
        r"\bdns\b.*\bafter\b.*\blink[- ]state\b.*\btransition",
        r"\bdns\b.*\bfollowing\b.*\binterface\b.*\binstability",
    )
    return any(re.search(pattern, chain_text) for pattern in combined_patterns)


def build_root_candidate_resolution_hints(evidence_chain: Dict[str, Any]) -> List[Dict[str, str]]:
    root_candidate_items = [
        item
        for item in evidence_chain.get("root_candidates") or []
        if isinstance(item, dict)
    ]
    diagnostic_items = [
        item
        for bucket in DIAGNOSTIC_BUCKETS
        for item in evidence_chain.get(bucket) or []
        if isinstance(item, dict)
    ]
    has_dns = any(item_is_dns_signal(item) for item in diagnostic_items)
    has_link_state = any(item_is_link_state_signal(item) for item in diagnostic_items)
    has_root_candidate_strong_flap = any(
        item_has_explicit_flap_semantics(item) for item in root_candidate_items
    )
    has_dns_after_link_transition = (
        has_dns
        and has_link_state
        and has_root_candidate_strong_flap
        and chain_has_dns_after_link_transition(evidence_chain)
    )
    hints: List[Dict[str, str]] = []
    if has_root_candidate_strong_flap:
        hints.append(
            {
                "condition": "explicit repeated/intermittent/oscillating link transition evidence is visible",
                "hint": (
                    "Use link_flap only when repeated/intermittent/oscillating down-up "
                    "transitions are present."
                ),
            }
        )
        hints.append(
            {
                "condition": "single down-up recovery is not enough for link_flap",
                "hint": "A single down-up recovery is link-down recovery context, not link_flap by itself.",
            }
        )
    if has_dns_after_link_transition:
        hints.append(
            {
                "condition": "DNS failure occurs after prompt-visible link-state transition evidence",
                "hint": (
                    "When DNS failure occurs after link-state transition evidence, consider whether DNS "
                    "is a downstream symptom unless DNS-specific root evidence is stronger."
                ),
            }
        )
    has_no_ipv4_candidate = any(item_has_no_ipv4_specificity(item) for item in root_candidate_items)
    has_wrong_route_candidate = any(item_has_wrong_default_route_specificity(item) for item in root_candidate_items)
    has_missing_route_candidate = any(item_has_missing_default_route_specificity(item) for item in root_candidate_items)
    if has_no_ipv4_candidate or has_wrong_route_candidate or has_missing_route_candidate:
        hints.append(
            {
                "condition": "specific route/IP root candidate evidence is visible",
                "hint": (
                    "When evidence supports a specific route/IP root candidate, prefer the "
                    "specific root over a generic network connectivity issue."
                ),
            }
        )
    if has_wrong_route_candidate or has_missing_route_candidate:
        hints.append(
            {
                "condition": "default-route evidence must preserve route specificity",
                "hint": (
                    "Distinguish wrong default route from missing default route: wrong route "
                    "means a route/gateway exists but points incorrectly; missing route means "
                    "no default route is present."
                ),
            }
        )
    if has_no_ipv4_candidate:
        hints.append(
            {
                "condition": "interface address evidence shows no IPv4",
                "hint": (
                    "Distinguish no IPv4 on interface from generic network issue when "
                    "interface address evidence is present."
                ),
            }
        )
    return hints


def base_payload_for_chain(chain: Dict[str, Any], opaque_index: int) -> Dict[str, Any]:
    source_payload = copy.deepcopy(chain.get("recommended_prompt_payload") or {})
    if not isinstance(source_payload, dict):
        source_payload = {}
    # Keep the rendered prompt on an explicit allowlist. Upstream payloads may
    # contain non-diagnostic metadata; only prompt-visible evidence buckets are
    # allowed to pass through before we add safe synthetic prompt metadata below.
    payload: Dict[str, Any] = {}
    evidence_chain = source_payload.get("evidence_chain")
    if not isinstance(evidence_chain, dict):
        evidence_chain = {bucket: [] for bucket in PROMPT_BUCKETS}
    evidence_chain = {bucket: evidence_chain.get(bucket, []) for bucket in PROMPT_BUCKETS}
    for bucket in PROMPT_BUCKETS:
        value = evidence_chain.get(bucket)
        items = list(value) if isinstance(value, list) else []
        evidence_chain[bucket] = [
            sanitize_prompt_visible_item(item) if isinstance(item, dict) else item
            for item in items
        ]
    payload["evidence_chain"] = evidence_chain
    payload["case_ref"] = f"CASE_{max(0, opaque_index):06d}"
    payload["case_id_redacted"] = True
    payload["task"] = "root_cause_analysis_from_evidence_chain"
    payload["safety_policy"] = dict(REQUIRED_SAFETY_POLICY)
    return payload


def cap_items(items: List[Dict[str, Any]], max_items: int) -> List[Dict[str, Any]]:
    if max_items < 0:
        return []
    return items[:max_items]


def build_user_payload(chain: Dict[str, Any], readiness: str, args: argparse.Namespace, opaque_index: int) -> Dict[str, Any]:
    payload = base_payload_for_chain(chain, opaque_index)
    evidence_chain = payload["evidence_chain"]
    evidence_chain["root_candidates"] = cap_items(evidence_chain.get("root_candidates") or [], args.max_root_candidates)
    evidence_chain["supporting_evidence"] = cap_items(evidence_chain.get("supporting_evidence") or [], args.max_supporting)
    evidence_chain["symptom_evidence"] = cap_items(evidence_chain.get("symptom_evidence") or [], args.max_symptoms)
    evidence_chain["uncertain_evidence"] = cap_items(evidence_chain.get("uncertain_evidence") or [], args.max_uncertain)
    if not args.include_do_not_use:
        evidence_chain["do_not_use_evidence"] = []
    payload["evidence_chain"] = evidence_chain
    root_candidate_ids = [
        str(item.get("evidence_id"))
        for item in evidence_chain.get("root_candidates") or []
        if isinstance(item, dict) and item.get("evidence_id")
    ]
    diagnostic_evidence_ids = [
        str(item.get("evidence_id"))
        for bucket in DIAGNOSTIC_BUCKETS
        for item in evidence_chain.get(bucket) or []
        if isinstance(item, dict) and item.get("evidence_id")
    ]
    forbidden_evidence_ids = [
        str(item.get("evidence_id"))
        for item in evidence_chain.get("do_not_use_evidence") or []
        if isinstance(item, dict) and item.get("evidence_id")
    ]
    payload["evidence_id_policy"] = {
        "root_candidate_ids": root_candidate_ids,
        "diagnostic_evidence_ids": diagnostic_evidence_ids,
        "forbidden_evidence_ids": forbidden_evidence_ids,
        "concrete_root_requires_root_candidate": bool(root_candidate_ids),
        "concrete_root_requires_non_empty_evidence_used": True,
        "evidence_roles_must_match_evidence_used": True,
        "evidence_roles_keys_subset_of_evidence_used": True,
        "evidence_roles_required_for_every_evidence_used_id": True,
    }
    resolution_hints = build_root_candidate_resolution_hints(evidence_chain)
    if resolution_hints:
        payload["root_candidate_resolution_hints"] = resolution_hints
    payload["output_contract"] = {
        "format": "json_only",
        "required_fields": list(OUTPUT_REQUIRED_FIELDS),
        "field_rules": build_expected_output_schema(),
        "evidence_backing_rules": [
            "if root_cause is not insufficient_evidence, evidence_used must be non-empty",
            "if root_candidates are available, evidence_used must include at least one root_candidate evidence ID",
            "every ID in evidence_roles must also appear in evidence_used",
            "every ID in evidence_used must have a role in evidence_roles",
            "every ID in evidence_used must appear exactly once as an evidence_roles key",
            "alternatives must either cite evidence IDs or be []",
        ],
        "forbidden_behavior": [
            "do not cite do_not_use_evidence as diagnostic support",
            "do not infer root cause from [REDACTED_SENSITIVE_CONTEXT]/provenance markers",
            "do not infer root cause from labels or ground truth strings",
            "do not treat recovery evidence as root cause",
            "do not output a concrete root_cause with evidence_used=[]",
            "do not include unused evidence IDs in evidence_roles",
            "do not omit evidence_roles for evidence IDs listed in evidence_used",
            "do not cite supporting or symptom evidence alone as the root cause when a root_candidate is available",
        ],
    }
    instructions = [
        "Use evidence IDs when citing support.",
        "Do not cite do_not_use_evidence as diagnostic support.",
        "If root candidates are weak, report uncertainty instead of inventing a cause.",
        "If no root candidate is present, return insufficient_evidence.",
        "Use root_candidate_resolution_hints only when they are present in this payload.",
        "Before returning JSON, silently check that a concrete root_cause has non-empty evidence_used.",
        "If root_candidates are available, evidence_used must include at least one root_candidate evidence ID for a concrete root_cause.",
        "Every ID in evidence_used must appear exactly once as a key in evidence_roles.",
        "evidence_roles keys must be a subset of evidence_used; do not include unused evidence IDs in evidence_roles.",
        "Do not omit roles for used evidence. If uncertain about role, use \"uncertain\" only for prompt-visible diagnostic evidence.",
        "When evidence supports a specific route/IP root candidate, prefer the specific root over a generic network connectivity issue.",
        "Distinguish wrong default route from missing default route: wrong route means a route/gateway exists but points incorrectly; missing route means no default route is present.",
        "Distinguish no IPv4 on interface from generic network issue when interface address evidence is present.",
        "If these evidence ID conditions cannot be met, return insufficient_evidence with root_object unknown, evidence_used [], and evidence_roles {}.",
    ]
    if readiness == "coverage_gap":
        instructions.insert(0, "This chain has no safe root candidate; return insufficient_evidence.")
    payload["instructions"] = instructions
    metadata = payload.get("readiness_metadata") if isinstance(payload.get("readiness_metadata"), dict) else {}
    metadata.update(
        {
            "has_root_candidate": bool(evidence_chain.get("root_candidates")),
            "has_supporting_evidence": bool(evidence_chain.get("supporting_evidence")),
            "has_context": bool(chain.get("has_context")),
            "chain_confidence": chain.get("chain_confidence") or metadata.get("chain_confidence") or "low",
            "chain_score": numeric(chain.get("chain_score"), numeric(metadata.get("chain_score"), 0.0)),
            "diagnostic_item_count": sum(len(evidence_chain.get(bucket) or []) for bucket in DIAGNOSTIC_BUCKETS),
            "do_not_use_item_count": len(evidence_chain.get("do_not_use_evidence") or []),
        }
    )
    payload["readiness_metadata"] = metadata
    return payload


def render_user_message(payload: Dict[str, Any], prompt_style: str) -> str:
    if prompt_style == "compact_markdown":
        return render_compact_markdown_payload(payload)
    if prompt_style == "both":
        return render_compact_markdown_payload(payload) + "\n\n```json\n" + safe_json_dump(payload, pretty=True) + "\n```"
    return safe_json_dump(payload, pretty=True)


def render_compact_markdown_payload(payload: Dict[str, Any]) -> str:
    evidence_chain = payload.get("evidence_chain") if isinstance(payload.get("evidence_chain"), dict) else {}
    lines = [
        f"Task: {payload.get('task')}",
        f"Case: {payload.get('case_ref')}",
        "",
        "Evidence buckets:",
    ]
    for bucket in PROMPT_BUCKETS:
        items = evidence_chain.get(bucket) or []
        lines.append(f"- {bucket}: {len(items)}")
        for item in items[:5]:
            lines.append(
                f"  - {item.get('evidence_id')}: {item.get('role', bucket)} "
                f"{item.get('source')} {item.get('object')} {item.get('anomaly_type')} "
                f"{compact_text(item.get('summary'), 160)}"
            )
    lines.extend(
        [
            "",
            "Output contract:",
            safe_json_dump(payload.get("output_contract") or {}, pretty=True),
            "",
            "Instructions:",
        ]
    )
    hints = payload.get("root_candidate_resolution_hints")
    if hints:
        lines.extend(["", "Root candidate resolution hints:"])
        for hint in hints:
            if not isinstance(hint, dict):
                continue
            lines.append(f"- {hint.get('condition')}: {hint.get('hint')}")
        lines.append("")
    for instruction in payload.get("instructions") or []:
        lines.append(f"- {instruction}")
    return "\n".join(lines)


def prune_prompt_payload(payload: Dict[str, Any], char_budget: int, prompt_style: str) -> Tuple[Dict[str, Any], List[str], List[str]]:
    warnings: List[str] = []
    blockers: List[str] = []
    pruned = copy.deepcopy(payload)
    if len(render_user_message(pruned, prompt_style)) <= char_budget:
        return pruned, warnings, blockers
    evidence_chain = pruned.get("evidence_chain") if isinstance(pruned.get("evidence_chain"), dict) else {}
    for bucket in ("uncertain_evidence", "symptom_evidence", "propagation_evidence", "fault_observations", "supporting_evidence"):
        while evidence_chain.get(bucket) and len(render_user_message(pruned, prompt_style)) > char_budget:
            evidence_chain[bucket].pop()
    if len(render_user_message(pruned, prompt_style)) > char_budget:
        for bucket in PROMPT_BUCKETS:
            compacted = []
            for item in evidence_chain.get(bucket) or []:
                if isinstance(item, dict):
                    item = dict(item)
                    if "summary" in item:
                        item["summary"] = compact_text(item.get("summary"), 120)
                    compacted.append(item)
            evidence_chain[bucket] = compacted
    if len(render_user_message(pruned, prompt_style)) > char_budget:
        blockers.append("prompt_too_long")
    else:
        warnings.append("prompt_pruned_to_budget")
    return pruned, warnings, blockers


def iter_values(value: Any) -> Iterator[Any]:
    if isinstance(value, dict):
        for child in value.values():
            yield from iter_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_values(child)
    else:
        yield value


def contains_unredacted_sensitive_content(text: Any, context: str = "evidence") -> bool:
    raw = safe_text(text)
    if not raw or REDACTION_MARKER in raw:
        return False
    lower = raw.lower()
    if context != "schema" and "root_cause" in lower:
        return True
    if re.search(r"\blabel\b", lower) and context == "evidence":
        return True
    if any(term in lower for term in SENSITIVE_TERMS):
        return True
    return any(pattern.search(raw) for pattern in SENSITIVE_PATTERNS)


def payload_has_unredacted_sensitive_data(payload: Dict[str, Any]) -> bool:
    evidence_chain = payload.get("evidence_chain") if isinstance(payload.get("evidence_chain"), dict) else {}
    for bucket in PROMPT_BUCKETS:
        for item in evidence_chain.get(bucket) or []:
            for value in iter_values(item):
                if contains_unredacted_sensitive_content(value, context="evidence"):
                    return True
    case_ref = payload.get("case_ref")
    if contains_unredacted_sensitive_content(case_ref, context="case_ref"):
        return True
    return False


def diagnostic_item_is_injector(item: Dict[str, Any]) -> bool:
    text = safe_json_dump(item)
    lower = text.lower()
    return (
        item.get("source") == "fault_inject"
        or item.get("anomaly_type") == "injector_marker"
        or item.get("causal_role") == "excluded_provenance"
        or "injector_marker" in {str(flag) for flag in item.get("risk_flags") or []}
        or "fault_inject" in lower
        or "injector" in lower
        or "fault_net_" in lower
    )


def diagnostic_item_is_generic_noise(item: Dict[str, Any]) -> bool:
    lower = safe_json_dump(item).lower()
    return (
        item.get("anomaly_type") == "noise"
        or item.get("causal_role") == "noise_candidate"
        or "generic_noise" in lower
        or "historical_noise" in lower
        or "duplicate_probe" in lower
    )


def payload_diagnostic_ids(payload: Dict[str, Any]) -> set:
    evidence_chain = payload.get("evidence_chain") if isinstance(payload.get("evidence_chain"), dict) else {}
    ids = set()
    for bucket in DIAGNOSTIC_BUCKETS:
        for item in evidence_chain.get(bucket) or []:
            if isinstance(item, dict) and item.get("evidence_id"):
                ids.add(str(item.get("evidence_id")))
    return ids


def payload_do_not_use_ids(payload: Dict[str, Any]) -> set:
    evidence_chain = payload.get("evidence_chain") if isinstance(payload.get("evidence_chain"), dict) else {}
    return {
        str(item.get("evidence_id"))
        for item in evidence_chain.get("do_not_use_evidence") or []
        if isinstance(item, dict) and item.get("evidence_id")
    }


def source_do_not_use_ids(chain: Dict[str, Any]) -> List[str]:
    """Return source-chain exclusion IDs, even if the rendered prompt omits them."""
    ids = set()
    payload = chain.get("recommended_prompt_payload")
    payload_chain = payload.get("evidence_chain") if isinstance(payload, dict) else {}
    sources = []
    if isinstance(payload_chain, dict):
        sources.append(payload_chain.get("do_not_use_evidence") or [])
    sources.append(chain.get("do_not_use_evidence") or [])
    for items in sources:
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and item.get("evidence_id"):
                ids.add(str(item.get("evidence_id")))
    return sorted(ids)


def validate_prompt_record(record: Dict[str, Any], args: argparse.Namespace, check_budget: bool = True) -> Dict[str, Any]:
    violations: List[str] = []
    warnings: List[str] = []
    payload = record.get("_payload") if isinstance(record.get("_payload"), dict) else {}
    evidence_chain = payload.get("evidence_chain") if isinstance(payload.get("evidence_chain"), dict) else {}

    if not payload.get("case_ref"):
        violations.append("missing_case_ref")
    if not payload.get("output_contract"):
        violations.append("missing_output_contract")
    policy = payload.get("safety_policy")
    if not isinstance(policy, dict):
        violations.append("missing_safety_policy")
    elif any(policy.get(key) is not True for key in REQUIRED_SAFETY_POLICY):
        violations.append("invalid_safety_policy")
    if record.get("readiness") in {"ready", "review_ready"} and not evidence_chain.get("root_candidates"):
        violations.append("no_root_candidate")
    if payload_has_unredacted_sensitive_data(payload):
        violations.append("unredacted_sensitive_terms")

    diagnostic_ids = payload_diagnostic_ids(payload)
    source_exclusion_ids = {str(item) for item in record.get("_source_do_not_use_ids") or [] if item}
    do_not_use_ids = payload_do_not_use_ids(payload) | source_exclusion_ids
    if diagnostic_ids & do_not_use_ids:
        violations.append("do_not_use_as_support")

    for bucket in DIAGNOSTIC_BUCKETS:
        for item in evidence_chain.get(bucket) or []:
            if not isinstance(item, dict):
                violations.append("malformed_diagnostic_item")
                continue
            if diagnostic_item_is_injector(item):
                violations.append("injector_in_diagnostic_prompt")
            if diagnostic_item_is_generic_noise(item):
                violations.append("generic_noise_in_diagnostic_prompt")
            if item.get("may_not_be_used_as_support"):
                violations.append("do_not_use_as_support")

    char_count = sum(len(message.get("content") or "") for message in record.get("messages") or [])
    if (
        check_budget
        and
        char_count > args.prompt_char_budget
        and record.get("readiness") != "not_ready"
        and "prompt_pruned_to_budget" not in record.get("review_warnings", [])
    ):
        violations.append("prompt_too_long")

    if record.get("readiness") == "review_ready":
        warnings.append("review_ready")
    if record.get("readiness") == "coverage_gap":
        warnings.append("no_root_coverage_gap")
    for warning in record.get("review_warnings") or []:
        warnings.append(str(warning))
    for blocker in record.get("blockers") or []:
        if blocker == "no_root_candidate" and record.get("readiness") == "coverage_gap":
            continue
        if blocker == "prompt_too_long" and record.get("readiness") == "not_ready":
            warnings.append(str(blocker))
            continue
        warnings.append(str(blocker))

    violations = list(dict.fromkeys(violations))
    warnings = list(dict.fromkeys(warnings))
    return {"passed": not violations, "violations": violations, "warnings": warnings}


def build_prompt_record(
    chain: Dict[str, Any],
    idx: int,
    readiness: str,
    blockers: Sequence[str],
    review_warnings: Sequence[str],
    args: argparse.Namespace,
) -> Dict[str, Any]:
    payload = build_user_payload(chain, readiness, args, idx)
    source_exclusion_ids = source_do_not_use_ids(chain)
    pre_record = {
        "readiness": readiness,
        "review_warnings": list(review_warnings),
        "blockers": list(blockers),
        "messages": [
            {"role": "system", "content": build_system_message(args)},
            {"role": "user", "content": render_user_message(payload, args.prompt_style)},
        ],
        "_payload": payload,
        "_source_do_not_use_ids": source_exclusion_ids,
    }
    pre_validation = validate_prompt_record(pre_record, args, check_budget=False)
    system_content = build_system_message(args)
    user_budget = max(1, args.prompt_char_budget - len(system_content))
    payload, prune_warnings, prune_blockers = prune_prompt_payload(payload, user_budget, args.prompt_style)
    combined_warnings = list(dict.fromkeys([*review_warnings, *prune_warnings]))
    combined_blockers = list(dict.fromkeys([*blockers, *prune_blockers]))
    if "prompt_too_long" in combined_blockers:
        readiness = "not_ready"
    user_content = render_user_message(payload, args.prompt_style)
    evidence_chain = payload.get("evidence_chain") if isinstance(payload.get("evidence_chain"), dict) else {}
    record: Dict[str, Any] = {
        "prompt_id": f"PROMPT_{idx:06d}",
        "case_ref": payload.get("case_ref"),
        "readiness": readiness,
        "chain_confidence": chain.get("chain_confidence") or "low",
        "chain_score": numeric(chain.get("chain_score"), 0.0),
        "prompt_steering_version": PROMPT_STEERING_VERSION,
        "review_warnings": combined_warnings,
        "blockers": combined_blockers,
        "messages": [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content},
        ],
        "expected_output_schema": build_expected_output_schema(),
        "static_validation": {},
        "source_chain_summary": evidence_chain_counts(evidence_chain),
        "_payload": payload,
        "_source_do_not_use_ids": source_exclusion_ids,
    }
    record["static_validation"] = validate_prompt_record(record, args)
    merged_violations = list(
        dict.fromkeys(
            list(pre_validation.get("violations") or [])
            + list((record.get("static_validation") or {}).get("violations") or [])
        )
    )
    merged_warnings = list(
        dict.fromkeys(
            list(pre_validation.get("warnings") or [])
            + list((record.get("static_validation") or {}).get("warnings") or [])
        )
    )
    record["static_validation"] = {
        "passed": not merged_violations,
        "violations": merged_violations,
        "warnings": merged_warnings,
    }
    record.pop("_payload", None)
    record.pop("_source_do_not_use_ids", None)
    return record


def summarize_prompt_record(record: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "prompt_id": record.get("prompt_id"),
        "case_ref": record.get("case_ref"),
        "readiness": record.get("readiness"),
        "chain_confidence": record.get("chain_confidence"),
        "chain_score": record.get("chain_score"),
        "review_warnings": record.get("review_warnings") or [],
        "blockers": record.get("blockers") or [],
        "static_validation": record.get("static_validation") or {},
        "source_chain_summary": record.get("source_chain_summary") or {},
    }


def compute_prompt_size_stats(records: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    sizes = [sum(len(message.get("content") or "") for message in record.get("messages") or []) for record in records]
    if not sizes:
        return {"min_chars": 0, "p50_chars": 0, "p90_chars": 0, "max_chars": 0, "avg_chars": 0}
    return {
        "min_chars": min(sizes),
        "p50_chars": percentile(sizes, 50),
        "p90_chars": percentile(sizes, 90),
        "max_chars": max(sizes),
        "avg_chars": round(sum(sizes) / len(sizes), 4),
    }


def build_summary(records: Sequence[Dict[str, Any]], chains: Sequence[Dict[str, Any]], args: argparse.Namespace) -> Dict[str, Any]:
    readiness_counts = Counter(str(record.get("readiness") or "not_ready") for record in records)
    blockers = Counter()
    review_warnings = Counter()
    validation_violations = Counter()
    validation_warnings = Counter()
    for record in records:
        blockers.update(str(item) for item in record.get("blockers") or [])
        review_warnings.update(str(item) for item in record.get("review_warnings") or [])
        validation = record.get("static_validation") or {}
        validation_violations.update(str(item) for item in validation.get("violations") or [])
        validation_warnings.update(str(item) for item in validation.get("warnings") or [])

    hard_violations = sum(validation_violations.values())
    diagnosis_ready_prompts = sum(
        1
        for record in records
        if record.get("readiness") in {"ready", "review_ready"}
        and (record.get("static_validation") or {}).get("passed")
    )
    review_ready_prompts = sum(
        1
        for record in records
        if record.get("readiness") == "review_ready"
        and (record.get("static_validation") or {}).get("passed")
    )
    status = "PASS"
    if hard_violations:
        status = "FAIL"
    elif review_warnings or validation_warnings or readiness_counts.get("coverage_gap", 0):
        status = "WARN"

    examples = {
        "prompt_records": [summarize_prompt_record(record) for record in records[: args.max_examples]],
        "coverage_gaps": [
            summarize_prompt_record(record)
            for record in records
            if record.get("readiness") == "coverage_gap"
        ][: args.max_examples],
        "violations": [
            summarize_prompt_record(record)
            for record in records
            if (record.get("static_validation") or {}).get("violations")
        ][: args.max_examples],
        "warnings": [
            summarize_prompt_record(record)
            for record in records
            if (record.get("static_validation") or {}).get("warnings")
        ][: args.max_examples],
    }

    return {
        "root": str(Path(args.root).resolve()),
        "dry_run": bool(args.dry_run),
        "status": status,
        "counts": {
            "chains": len(chains),
            "selected_chains": len(records),
            "prompt_records": len(records),
            "diagnosis_ready_prompts": diagnosis_ready_prompts,
            "review_ready_prompts": review_ready_prompts,
            "coverage_gap_prompts": readiness_counts.get("coverage_gap", 0),
            "not_ready_prompts": readiness_counts.get("not_ready", 0),
        },
        "safety": {
            "prompt_hard_violations": hard_violations,
            "unredacted_sensitive_terms": validation_violations.get("unredacted_sensitive_terms", 0),
            "injector_in_diagnostic_prompt": validation_violations.get("injector_in_diagnostic_prompt", 0),
            "do_not_use_as_support": validation_violations.get("do_not_use_as_support", 0),
            "missing_output_contract": validation_violations.get("missing_output_contract", 0),
            "missing_case_ref": validation_violations.get("missing_case_ref", 0),
        },
        "warnings": dict((review_warnings + validation_warnings).most_common()),
        "readiness": {
            "ready": readiness_counts.get("ready", 0),
            "review_ready": readiness_counts.get("review_ready", 0),
            "coverage_gap": readiness_counts.get("coverage_gap", 0),
            "not_ready": readiness_counts.get("not_ready", 0),
            "blockers": dict(blockers.most_common()),
            "review_warnings": dict(review_warnings.most_common()),
        },
        "prompt_size": compute_prompt_size_stats(records),
        "examples": examples,
    }


def print_json_summary(summary: Dict[str, Any]) -> str:
    return safe_json_dump(summary, pretty=True)


def markdown_table(title: str, values: Dict[str, Any]) -> List[str]:
    lines = [f"## {title}", "| metric | value |", "|---|---:|"]
    for key, value in values.items():
        lines.append(f"| `{key}` | {safe_text(value)} |")
    return lines


def print_markdown_summary(summary: Dict[str, Any]) -> str:
    lines = [
        "# Evidence Prompt Integration Preview Report",
        "",
        "This preview tool is read-only by default and does not modify existing evidence files.",
        "This tool does not call any LLM and does not generate diagnosis answers.",
        "Injector/provenance and label-like evidence remain excluded from diagnostic support.",
        "No-root chains are coverage gaps, not diagnosis-ready prompts.",
        "",
        "## Status",
        f"- `status`: {summary.get('status')}",
        "",
        "## Prompt Selection",
    ]
    lines.extend(markdown_table("Counts", summary.get("counts") or [])[1:])
    lines.append("")
    lines.extend(markdown_table("Safety Validation", summary.get("safety") or {}))
    lines.append("")
    lines.extend(markdown_table("Readiness Breakdown", {k: v for k, v in (summary.get("readiness") or {}).items() if not isinstance(v, dict)}))
    readiness = summary.get("readiness") or {}
    if readiness.get("blockers"):
        lines.append("")
        lines.extend(markdown_table("Readiness Blockers", readiness.get("blockers") or {}))
    if readiness.get("review_warnings"):
        lines.append("")
        lines.extend(markdown_table("Review Warnings", readiness.get("review_warnings") or {}))
    lines.append("")
    lines.extend(markdown_table("Prompt Size Stats", summary.get("prompt_size") or {}))
    if summary.get("warnings"):
        lines.append("")
        lines.extend(markdown_table("Warning Summary", summary.get("warnings") or {}))
    coverage = (summary.get("readiness") or {}).get("coverage_gap", 0)
    lines.extend(["", "## Coverage Gaps", f"- `coverage_gap_prompts`: {coverage}"])
    examples = summary.get("examples") or {}
    lines.append("")
    lines.append("## Example Prompt Records")
    for example in examples.get("prompt_records") or []:
        lines.append(f"- `{safe_json_dump(example)}`")
    if examples.get("violations"):
        lines.append("")
        lines.append("## Violations")
        for example in examples.get("violations") or []:
            lines.append(f"- `{safe_json_dump(example)}`")
    lines.extend(
        [
            "",
            "## Notes",
            "- Chain JSONL, if supplied, is treated as authoritative for this preview run.",
            "- App Server candidates are not used by this preview tool.",
            "- Review-ready prompts are safe for prompt integration smoke tests but should be monitored.",
        ]
    )
    return "\n".join(lines)


def print_jsonl_records(records: Sequence[Dict[str, Any]]) -> str:
    return "\n".join(safe_json_dump(record) for record in records)


def print_prompt_examples(records: Sequence[Dict[str, Any]], max_examples: int) -> str:
    chunks: List[str] = []
    for record in records[:max_examples]:
        chunks.append(f"===== {record.get('prompt_id')} {record.get('case_ref')} {record.get('readiness')} =====")
        for message in record.get("messages") or []:
            chunks.append(f"[{message.get('role')}]\n{message.get('content')}")
        chunks.append("")
    return "\n".join(chunks)


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


def render_output(summary: Dict[str, Any], records: Sequence[Dict[str, Any]], args: argparse.Namespace) -> str:
    if args.format == "json":
        return print_json_summary(summary) + "\n"
    if args.format == "markdown":
        return print_markdown_summary(summary)
    if args.format == "jsonl":
        return print_jsonl_records(records) + ("\n" if records else "")
    if args.format == "prompt":
        return print_prompt_examples(records, args.limit_prompts or args.max_examples)
    return "----- JSON -----\n" + print_json_summary(summary) + "\n\n----- MARKDOWN -----\n" + print_markdown_summary(summary)


def run(args: argparse.Namespace) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    root = Path(args.root).resolve()
    chains, _source_summary, _parse_errors = load_or_build_chains(args)
    readiness = analyze_chain_readiness(root, chains, args)
    selected = select_chains(chains, readiness, args)
    records = [
        build_prompt_record(chain, idx, chain_readiness, blockers, review_warnings, args)
        for idx, (chain, chain_readiness, blockers, review_warnings) in enumerate(selected, 1)
    ]
    summary = build_summary(records, chains, args)
    return summary, records


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    root = Path(args.root).resolve()
    try:
        summary, records = run(args)
    except RuntimeError as exc:
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
