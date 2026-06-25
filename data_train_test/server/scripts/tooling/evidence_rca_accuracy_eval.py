#!/usr/bin/env python3
"""Offline RCA accuracy evaluator preview for evidence diagnosis outputs.

This tool is intentionally offline and read-only with respect to prompts,
responses, canonical cases, datasets, and evidence sources. Ground truth is
used only after model responses already exist, and only to compute evaluation
preview metrics.
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

DEFAULT_PROMPTS = "manual_experiments/evidence_qwen3_direct_diag50_20260527/prompts.jsonl"
DEFAULT_RESPONSES = "manual_experiments/evidence_qwen3_direct_diag50_20260527/responses.jsonl"
DEFAULT_VALIDATOR = "manual_experiments/evidence_qwen3_direct_diag50_20260527/validator_report.json"

HARD_SAFETY_KEYS = (
    "unknown_evidence_id",
    "do_not_use_evidence_cited",
    "forbidden_role_used",
    "sensitive_text_violations",
    "injector_or_label_leakage",
    "coverage_gap_false_root",
)

GT_FIELD_CANDIDATES = {
    "family": (
        ("gt", "family"),
        ("gt", "domain"),
        ("ground_truth", "family"),
        ("ground_truth", "domain"),
        ("label", "family"),
        ("label", "domain"),
        ("family",),
        ("domain",),
    ),
    "subtype": (
        ("gt", "subtype"),
        ("gt", "fault_type"),
        ("gt", "root_cause"),
        ("ground_truth", "subtype"),
        ("ground_truth", "fault_type"),
        ("ground_truth", "root_cause"),
        ("label", "subtype"),
        ("label", "fault_type"),
        ("subtype",),
        ("fault_type",),
    ),
    "object": (
        ("gt", "object"),
        ("gt", "root_object"),
        ("ground_truth", "object"),
        ("ground_truth", "root_object"),
        ("label", "object"),
        ("object",),
        ("root_object",),
    ),
}

PROTECTED_OUTPUT_FILENAMES = {
    "prompts.jsonl",
    "responses.jsonl",
    "responses_raw.jsonl",
    "validator_report.json",
    "validator_report.md",
    "train.jsonl",
    "val.jsonl",
    "test.jsonl",
    "canonical_case.json",
    "accepted_runs.jsonl",
}

SUBTYPE_PATTERNS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (
        "net_link_flap",
        (
            "net_link_flap",
            "link_flap",
            "link flap",
            "flapping",
            "intermittent link",
            "repeated disconnect/reconnect",
            "repeated disconnect reconnect",
            "down/up transition",
            "down up transition",
            "link state oscillation",
            "recovery after disconnect",
            "multiple state transitions",
            "unstable link cycles",
        ),
    ),
    (
        "net_dns_fail",
        (
            "net_dns_fail",
            "dns_failure",
            "dns fail",
            "dns resolution failure",
            "name resolution failure",
            "resolver failure",
        ),
    ),
    (
        "net_wrong_default_route",
        (
            "net_wrong_default_route",
            "wrong default route",
            "wrong default gateway",
            "incorrect gateway",
            "wrong gateway",
            "route misconfiguration",
            "route_blackhole",
            "route_black_hole",
            "blackhole route",
            "black-holed",
            "black holed",
            "unreachable gateway",
        ),
    ),
    (
        "net_no_default_route",
        (
            "net_no_default_route",
            "missing default route",
            "no default route",
            "route_missing",
            "default gateway route missing",
        ),
    ),
    (
        "net_no_ipv4_on_iface",
        (
            "net_no_ipv4_on_iface",
            "ip_config_missing",
            "no_ipv4",
            "no ipv4",
            "missing ip address",
            "no ip on interface",
            "interface has no ipv4",
            "wlan0 no ip",
        ),
    ),
    (
        "net_wifi_auth_fail_wrong_psk",
        (
            "net_wifi_auth_fail_wrong_psk",
            "auth_failure",
            "authentication failure",
            "wrong password",
            "wrong psk",
            "psk failure",
            "handshake failed",
            "wpa authentication failure",
        ),
    ),
    (
        "net_link_down",
        (
            "net_link_down",
            "interface_down",
            "network_interface_down",
            "link down",
            "interface transitioned down",
            "interface eth0 transitioned down",
            "interface eth1 transitioned down",
        ),
    ),
    (
        "net_wifi_disconnect",
        (
            "net_wifi_disconnect",
            "net_wlan_disconnect",
            "wifi disconnect",
            "wi-fi disconnect",
            "wlan disconnected",
            "wifi disconnected",
            "wi-fi disconnected",
        ),
    ),
    (
        "net_public_ip_unreachable",
        (
            "net_public_ip_unreachable",
            "public ip unreachable",
            "reachability_loss",
            "ping failure",
            "packet loss",
            "external connectivity failure",
            "target ip unreachable",
        ),
    ),
)

SUBTYPE_ALIAS_GROUP = {
    "net_dns_fail": "dns",
    "net_wrong_default_route": "wrong_route",
    "net_no_default_route": "missing_route",
    "net_no_ipv4_on_iface": "no_ipv4",
    "net_wifi_auth_fail_wrong_psk": "wifi_auth",
    "net_link_flap": "link_flap",
    "net_wifi_disconnect": "wifi_disconnect",
    "net_link_down": "link_down",
    "net_wlan_disconnect": "wifi_disconnect",
    "net_public_ip_unreachable": "public_reachability",
}

NET_LINK_FLAP_MARKERS = (
    "flap",
    "flapping",
    "intermittent link",
    "repeated disconnect",
    "reconnect",
    "down/up",
    "down up",
    "link state oscillation",
    "recovery after disconnect",
    "multiple state transitions",
    "unstable link cycles",
)

GENERIC_TERMS = {
    "network issue",
    "network failure",
    "connectivity issue",
    "connectivity failure",
    "route issue",
    "wifi issue",
    "unknown",
}


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Offline RCA accuracy evaluator preview.")
    parser.add_argument("--root", default=".", help="Repository root. Default: current directory.")
    parser.add_argument("--prompts-jsonl", default=DEFAULT_PROMPTS)
    parser.add_argument("--responses-jsonl", default=DEFAULT_RESPONSES)
    parser.add_argument("--validator-report", default=DEFAULT_VALIDATOR)
    parser.add_argument("--chains-jsonl", default=None)
    parser.add_argument("--canonical-root", default=None)
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    parser.add_argument("--run", dest="dry_run", action="store_false")
    parser.add_argument("--stdout-only", action="store_true")
    parser.add_argument("--format", choices=("json", "markdown", "both", "jsonl"), default="both")
    parser.add_argument("--max-examples", type=int, default=10)
    parser.add_argument("--min-mapping-coverage", type=float, default=0.95)
    parser.add_argument("--allow-partial-metrics", action="store_true")
    parser.add_argument("--output", default=None)
    parser.add_argument("--redact-sensitive", dest="redact_sensitive", action="store_true", default=True)
    parser.add_argument("--no-redact-sensitive", dest="redact_sensitive", action="store_false")
    return parser.parse_args(argv)


def read_text_lossy(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def safe_json_dump(obj: Any, *, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2)
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compact_text(value: Any, limit: int = 180) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"\s+", " ", text.replace("\r", " ").replace("\n", " ")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def normalize_token(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.strip().lower()
    text = re.sub(r"[\s\-]+", "_", text)
    text = re.sub(r"[^a-z0-9_]+", "", text)
    return text


def load_jsonl(path: Path) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    rows: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    for line_no, line in enumerate(read_text_lossy(path).splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            errors.append({"line": line_no, "error": str(exc), "text": compact_text(line)})
            continue
        if isinstance(value, dict):
            rows.append(value)
        else:
            errors.append({"line": line_no, "error": "JSONL row is not an object", "text": compact_text(value)})
    return rows, errors


def load_json_file(path: Optional[Path]) -> Dict[str, Any]:
    if path is None or not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def resolve_path(root: Path, value: Optional[str]) -> Optional[Path]:
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def get_path_value(obj: Dict[str, Any], path: Sequence[str]) -> Any:
    current: Any = obj
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def first_gt_value(canonical: Dict[str, Any], field: str) -> Tuple[Optional[str], Optional[str]]:
    for path in GT_FIELD_CANDIDATES[field]:
        value = get_path_value(canonical, path)
        if value not in (None, ""):
            return str(value), ".".join(path)
    return None, None


def extract_prompt_payload(prompt: Dict[str, Any]) -> Dict[str, Any]:
    for message in reversed(prompt.get("messages") or []):
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        raw = content.strip()
        if raw.startswith("{"):
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
        match = re.search(r"```json\s*(\{.*?\})\s*```", raw, re.IGNORECASE | re.DOTALL)
        if match:
            try:
                value = json.loads(match.group(1))
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
    return {}


def prompt_root_candidate_ids(prompt: Dict[str, Any]) -> List[str]:
    payload = extract_prompt_payload(prompt)
    chain = payload.get("evidence_chain") if isinstance(payload.get("evidence_chain"), dict) else {}
    return [
        str(item.get("evidence_id"))
        for item in chain.get("root_candidates") or []
        if isinstance(item, dict) and item.get("evidence_id")
    ]


def prompt_root_candidate_summaries(prompt: Dict[str, Any]) -> List[str]:
    payload = extract_prompt_payload(prompt)
    chain = payload.get("evidence_chain") if isinstance(payload.get("evidence_chain"), dict) else {}
    summaries: List[str] = []
    for item in chain.get("root_candidates") or []:
        if not isinstance(item, dict):
            continue
        for key in ("summary", "anomaly_type", "object", "source"):
            value = item.get(key)
            if value not in (None, ""):
                summaries.append(str(value))
    return summaries


def safe_import_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to import module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_regenerated_chain_map(root: Path, args: argparse.Namespace) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any], List[Dict[str, Any]]]:
    """Rebuild prompt preview metadata and map prompt_id to source chain.

    The public prompt pack intentionally redacts source case identifiers. This
    layer rebuilds the same read-only prompt preview order and uses only stable
    prompt IDs to recover case_rel/case_id for offline GT lookup.
    """

    try:
        preview = safe_import_module(root / "tools" / "evidence_prompt_integration_preview.py", "evidence_prompt_preview_for_rca_eval")
        preview_argv = [
            "--root",
            str(root),
            "--dry-run",
            "--stdout-only",
            "--format",
            "jsonl",
            "--prompt-char-budget",
            "12000",
            "--max-examples",
            str(args.max_examples),
        ]
        if args.chains_jsonl:
            preview_argv.extend(["--chains-jsonl", str(args.chains_jsonl)])
        preview_args = preview.parse_args(preview_argv)
        chains, source_summary, parse_errors = preview.load_or_build_chains(preview_args)
        readiness = preview.analyze_chain_readiness(root, chains, preview_args)
        selected = preview.select_chains(chains, readiness, preview_args)
        mapping: Dict[str, Dict[str, Any]] = {}
        for idx, (chain, chain_readiness, blockers, review_warnings) in enumerate(selected, 1):
            prompt_id = f"PROMPT_{idx:06d}"
            mapping[prompt_id] = {
                "case_id": chain.get("case_id"),
                "case_rel": chain.get("case_rel"),
                "chain_id": chain.get("chain_id"),
                "readiness": chain_readiness,
                "blockers": list(blockers or []),
                "review_warnings": list(review_warnings or []),
                "root_candidates": chain.get("root_candidates") or [],
                "method": "regenerated_chain_pool",
            }
        source_summary_compact = {
            "counts": source_summary.get("counts") if isinstance(source_summary, dict) else {},
            "safety": source_summary.get("safety") if isinstance(source_summary, dict) else {},
            "warnings": source_summary.get("warnings") if isinstance(source_summary, dict) else {},
        }
        summary = {
            "mode": "regenerated_chain_pool",
            "chains": len(chains),
            "selected_chains": len(selected),
            "source_summary": source_summary_compact,
            "parse_errors": len(parse_errors),
        }
        return mapping, summary, list(parse_errors or [])
    except Exception as exc:
        return {}, {"mode": "regenerated_chain_pool", "error": str(exc)}, [{"error": str(exc)}]


def direct_prompt_mapping(prompt: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    keys = ("case_path", "case_rel", "canonical_case", "canonical_case_path", "chain_id", "source_case_id", "case_id")
    if not any(prompt.get(key) for key in keys):
        return None
    return {
        "case_id": prompt.get("case_id") or prompt.get("source_case_id"),
        "case_rel": prompt.get("case_rel") or prompt.get("case_path") or prompt.get("canonical_case_path"),
        "chain_id": prompt.get("chain_id"),
        "method": "direct_prompt_metadata",
    }


def candidate_canonical_paths(root: Path, case_rel: Optional[str], canonical_root: Path) -> Iterator[Path]:
    if not case_rel:
        return
    rel = Path(str(case_rel))
    bases: List[Path] = []
    bases.append(rel if rel.is_absolute() else root / rel)
    bases.append(rel if rel.is_absolute() else canonical_root / rel)
    seen = set()
    for base in bases:
        base = base.resolve()
        variants = []
        if base.name == "canonical_case.json":
            variants.append(base)
        if base.suffix:
            variants.append(base.parent / "canonical_case.json")
        else:
            variants.extend(
                [
                    base / "canonical_case.json",
                    base / "dataset_export" / "canonical_case.json",
                    base.parent / "canonical_case.json",
                    base.parent / "dataset_export" / "canonical_case.json",
                ]
            )
        for path in variants:
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            yield path


def build_canonical_index(canonical_root: Path) -> Dict[str, Path]:
    index: Dict[str, Path] = {}
    for path in canonical_root.rglob("canonical_case.json"):
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        for key in (obj.get("case_id"), obj.get("source_case_id")):
            if key:
                index.setdefault(str(key), path)
    return index


def find_canonical_case(
    root: Path,
    canonical_root: Path,
    chain_meta: Optional[Dict[str, Any]],
    canonical_index: Optional[Dict[str, Path]],
) -> Tuple[Optional[Dict[str, Any]], Optional[Path], str]:
    if not chain_meta:
        return None, None, "unmapped"
    case_rel = chain_meta.get("case_rel")
    for path in candidate_canonical_paths(root, str(case_rel) if case_rel else None, canonical_root):
        if path.exists() and path.is_file():
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                return value, path, "case_rel_canonical_case"
    case_id = str(chain_meta.get("case_id") or "").strip()
    if case_id:
        if canonical_index is None:
            canonical_index = build_canonical_index(canonical_root)
        path = canonical_index.get(case_id)
        if path and path.exists():
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                return value, path, "case_id_canonical_index"
    return None, None, "unmapped"


def canonicalize_subtype_from_text(root_cause: Any, root_object: Any) -> Tuple[Optional[str], str]:
    raw = f"{root_cause or ''} {root_object or ''}".strip()
    text = raw.lower().replace("-", "_")
    text_space = re.sub(r"[_]+", " ", text)
    specificity = "specific"
    if normalize_token(root_cause) in {normalize_token(item) for item in GENERIC_TERMS}:
        specificity = "generic"
    for subtype, patterns in SUBTYPE_PATTERNS:
        for pattern in patterns:
            p = pattern.lower().replace("-", "_")
            p_space = re.sub(r"[_]+", " ", p)
            if p in text or p_space in text_space:
                return subtype, specificity
    if any(word in text_space for word in ("issue", "problem", "failure")):
        specificity = "generic"
    return None, specificity


def family_from_subtype(subtype: Optional[str]) -> Optional[str]:
    if not subtype:
        return None
    token = normalize_token(subtype)
    if token.startswith("net_"):
        return "net"
    return token.split("_", 1)[0] if "_" in token else token


def subtype_exact_match(predicted: Optional[str], gt_subtype: Optional[str]) -> Optional[bool]:
    if not predicted or not gt_subtype:
        return None
    return normalize_token(predicted) == normalize_token(gt_subtype)


def has_link_flap_semantics(*values: Any) -> bool:
    text = " ".join("" if value is None else str(value) for value in values).lower()
    text = text.replace("-", " ").replace("_", " ")
    return any(marker in text for marker in NET_LINK_FLAP_MARKERS)


def subtype_alias_match(
    predicted: Optional[str],
    gt_subtype: Optional[str],
    context_texts: Optional[Iterable[Any]] = None,
) -> Optional[bool]:
    if not predicted or not gt_subtype:
        return None
    if subtype_exact_match(predicted, gt_subtype):
        return True
    predicted_token = normalize_token(predicted)
    gt_token = normalize_token(gt_subtype)
    if gt_token == "net_link_flap":
        if predicted_token in {"net_wifi_disconnect", "net_link_down", "net_wlan_disconnect"}:
            return has_link_flap_semantics(*(context_texts or ()))
        return False
    return SUBTYPE_ALIAS_GROUP.get(predicted_token) == SUBTYPE_ALIAS_GROUP.get(gt_token)


def subtype_group_match(predicted: Optional[str], gt_subtype: Optional[str]) -> Optional[bool]:
    if not predicted or not gt_subtype:
        return None
    predicted_group = SUBTYPE_ALIAS_GROUP.get(normalize_token(predicted))
    gt_group = SUBTYPE_ALIAS_GROUP.get(normalize_token(gt_subtype))
    if not predicted_group or not gt_group:
        return None
    if predicted_group in {"link_down", "link_flap", "wifi_disconnect"} and gt_group in {
        "link_down",
        "link_flap",
        "wifi_disconnect",
    }:
        return True
    if predicted_group in {"missing_route", "wrong_route"} and gt_group in {"missing_route", "wrong_route"}:
        return True
    return predicted_group == gt_group


def object_match(predicted: Optional[str], gt_object: Optional[str]) -> Optional[bool]:
    if not predicted or not gt_object:
        return None
    p = normalize_token(predicted)
    g = normalize_token(gt_object)
    if p == g:
        return True
    if {p, g} <= {"wifi", "wlan0", "network", "eth0"}:
        return True
    return False


def metric_value(correct: int, total: int) -> Optional[float]:
    if total <= 0:
        return None
    return round(correct / total, 4)


def load_validator_report(path: Optional[Path]) -> Dict[str, Any]:
    report = load_json_file(path)
    counts = report.get("counts") if isinstance(report.get("counts"), dict) else {}
    evidence_reference = report.get("evidence_reference") if isinstance(report.get("evidence_reference"), dict) else {}
    safety = report.get("safety") if isinstance(report.get("safety"), dict) else {}
    hard_safety = {
        "unknown_evidence_id": int(evidence_reference.get("unknown_evidence_id") or 0),
        "do_not_use_evidence_cited": int(evidence_reference.get("do_not_use_evidence_cited") or 0),
        "forbidden_role_used": int(evidence_reference.get("forbidden_role_used") or 0),
        "sensitive_text_violations": int(safety.get("sensitive_text_violations") or 0),
        "injector_or_label_leakage": int(safety.get("injector_or_label_leakage") or 0),
        "coverage_gap_false_root": int(safety.get("coverage_gap_false_root") or 0),
    }
    return {
        "status": report.get("status") or "UNKNOWN",
        "pass": int(counts.get("pass") or 0),
        "warn": int(counts.get("warn") or 0),
        "fail": int(counts.get("fail") or 0),
        "hard_safety": hard_safety,
        "warnings": report.get("warnings") or {},
        "violations": report.get("violations") or {},
        "schema": report.get("schema") or {},
        "evidence_reference": evidence_reference,
        "counts": counts,
    }


def validate_output_path(path: Path, root: Path) -> None:
    resolved = path.resolve()
    if resolved.exists():
        raise ValueError(f"OUTPUT_ALREADY_EXISTS: {resolved}")
    if resolved.name in PROTECTED_OUTPUT_FILENAMES:
        raise ValueError(f"refusing protected output filename: {resolved}")
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"refusing output path outside repository root: {resolved}") from exc
    if not resolved.parent.exists():
        raise ValueError(f"output parent does not exist: {resolved.parent}")


def evaluate(args: argparse.Namespace) -> Dict[str, Any]:
    root = Path(args.root).resolve()
    prompts_path = resolve_path(root, args.prompts_jsonl)
    responses_path = resolve_path(root, args.responses_jsonl)
    validator_path = resolve_path(root, args.validator_report) if args.validator_report else None
    canonical_root = resolve_path(root, args.canonical_root) if args.canonical_root else root
    if prompts_path is None or responses_path is None or canonical_root is None:
        raise RuntimeError("prompts, responses, and canonical root must be resolvable")
    if not prompts_path.exists():
        raise RuntimeError(f"missing prompts JSONL: {prompts_path}")
    if not responses_path.exists():
        raise RuntimeError(f"missing responses JSONL: {responses_path}")

    prompts, prompt_errors = load_jsonl(prompts_path)
    responses, response_errors = load_jsonl(responses_path)
    prompts_by_id = {str(item.get("prompt_id")): item for item in prompts if item.get("prompt_id")}
    responses_by_id = {str(item.get("prompt_id")): item for item in responses if item.get("prompt_id")}
    matched_ids = [pid for pid in prompts_by_id if pid in responses_by_id]

    validator = load_validator_report(validator_path)
    hard_safety_zero = all(int(value or 0) == 0 for value in validator.get("hard_safety", {}).values())

    regenerated_map, chain_pool_summary, chain_parse_errors = build_regenerated_chain_map(root, args)
    canonical_index: Optional[Dict[str, Path]] = None
    if canonical_root.exists():
        canonical_index = build_canonical_index(canonical_root)

    method_counts: Counter[str] = Counter()
    gt_field_usage: Counter[str] = Counter()
    cases: List[Dict[str, Any]] = []
    quality_flags = Counter()

    family_correct = family_total = 0
    subtype_exact_correct = subtype_alias_correct = subtype_total = 0
    group_correct = group_total = 0
    object_correct = object_total = 0
    root_candidate_cited = root_candidate_applicable = 0
    insufficient_count = 0
    evidence_valid_count = 0

    for pid in matched_ids:
        prompt = prompts_by_id[pid]
        response_row = responses_by_id[pid]
        response = response_row.get("response") if isinstance(response_row.get("response"), dict) else {}
        response_text = response_row.get("response_text")
        root_cause = response.get("root_cause") if response else response_text
        root_object = response.get("root_object") if response else None
        evidence_used = response.get("evidence_used") if isinstance(response.get("evidence_used"), list) else []
        evidence_used = [str(item) for item in evidence_used if item]
        root_ids = prompt_root_candidate_ids(prompt)
        root_candidate_summaries = prompt_root_candidate_summaries(prompt)
        concrete_root = bool(root_cause and normalize_token(root_cause) != "insufficient_evidence")
        if not concrete_root:
            insufficient_count += 1
        if concrete_root:
            root_candidate_applicable += 1
            if set(evidence_used) & set(root_ids):
                root_candidate_cited += 1
            else:
                quality_flags["root_candidate_omitted_count"] += 1
        if concrete_root and not evidence_used:
            quality_flags["concrete_root_without_evidence_count"] += 1

        direct_meta = direct_prompt_mapping(prompt)
        chain_meta = direct_meta or regenerated_map.get(pid)
        chain_method = str((chain_meta or {}).get("method") or "unmapped")
        canonical, canonical_path, canonical_method = find_canonical_case(root, canonical_root, chain_meta, canonical_index)
        mapping_method = canonical_method if canonical else "unmapped"
        if canonical and chain_method != "unmapped":
            mapping_method = f"{chain_method}+{canonical_method}"
        method_counts[mapping_method] += 1

        gt_family = gt_subtype = gt_object = None
        gt_family_field = gt_subtype_field = gt_object_field = None
        if canonical:
            gt_family, gt_family_field = first_gt_value(canonical, "family")
            gt_subtype, gt_subtype_field = first_gt_value(canonical, "subtype")
            gt_object, gt_object_field = first_gt_value(canonical, "object")
            for field in (gt_family_field, gt_subtype_field, gt_object_field):
                if field:
                    gt_field_usage[field] += 1

        predicted_subtype, specificity = canonicalize_subtype_from_text(root_cause, root_object)
        predicted_family = family_from_subtype(predicted_subtype)
        if specificity == "generic":
            quality_flags["generic_prediction_count"] += 1
        if not predicted_subtype:
            quality_flags["unmapped_prediction_count"] += 1

        family_match = None
        if gt_family:
            family_total += 1
            family_match = normalize_token(predicted_family) == normalize_token(gt_family)
            if family_match:
                family_correct += 1
        exact_match = subtype_exact_match(predicted_subtype, gt_subtype)
        alias_match = subtype_alias_match(
            predicted_subtype,
            gt_subtype,
            [root_cause, root_object, *root_candidate_summaries],
        )
        group_ok = subtype_group_match(predicted_subtype, gt_subtype)
        if gt_subtype:
            subtype_total += 1
            if exact_match:
                subtype_exact_correct += 1
            if alias_match:
                subtype_alias_correct += 1
            if group_ok is not None:
                group_total += 1
                if group_ok:
                    group_correct += 1
        obj_match = object_match(str(root_object) if root_object else None, gt_object)
        if gt_object:
            object_total += 1
            if obj_match:
                object_correct += 1
        if evidence_used:
            evidence_valid_count += 1

        cases.append(
            {
                "prompt_id": pid,
                "case_ref": prompt.get("case_ref") or response_row.get("case_ref"),
                "gt_mapping_status": "mapped" if canonical else "unmapped",
                "mapping_method": mapping_method,
                "case_id_redacted": bool(canonical),
                "gt_family": gt_family,
                "gt_subtype": gt_subtype,
                "gt_object": gt_object,
                "gt_fields_used": {
                    "family": gt_family_field,
                    "subtype": gt_subtype_field,
                    "object": gt_object_field,
                },
                "root_cause": root_cause,
                "root_object": root_object,
                "predicted_family_candidate": predicted_family,
                "predicted_subtype_candidate": predicted_subtype,
                "predicted_object": root_object,
                "prediction_specificity": specificity,
                "subtype_exact_match": exact_match,
                "subtype_alias_match": alias_match,
                "subtype_group_match": group_ok,
                "family_match": family_match,
                "object_match": obj_match,
                "evidence_used": evidence_used,
                "root_candidate_cited": bool(set(evidence_used) & set(root_ids)) if concrete_root else None,
            }
        )

    mapped_cases = sum(1 for item in cases if item["gt_mapping_status"] == "mapped")
    unmapped_cases = len(cases) - mapped_cases
    coverage = round(mapped_cases / len(cases), 4) if cases else 0.0
    if int(validator.get("fail") or 0) > 0:
        status = "VALIDATOR_FAIL_BLOCKED"
    elif not hard_safety_zero:
        status = "HARD_SAFETY_BLOCKED"
    elif coverage < float(args.min_mapping_coverage):
        status = "PARTIAL_METRICS_READY" if args.allow_partial_metrics and mapped_cases else "MAPPING_INCOMPLETE"
    else:
        status = "READY_FOR_ACCURACY_EVAL"

    correct_examples = [item for item in cases if item.get("subtype_alias_match") is True][: args.max_examples]
    incorrect_examples = [item for item in cases if item.get("subtype_alias_match") is False][: args.max_examples]
    unmapped_examples = [item for item in cases if item.get("gt_mapping_status") == "unmapped"][: args.max_examples]
    generic_examples = [item for item in cases if item.get("prediction_specificity") == "generic"][: args.max_examples]

    metrics = {
        "mapping_coverage": coverage,
        "validator_pass_rate": metric_value(int(validator.get("pass") or 0), len(cases)),
        "validator_fail_rate": metric_value(int(validator.get("fail") or 0), len(cases)),
        "hard_safety_zero": hard_safety_zero,
        "family_accuracy": metric_value(family_correct, family_total),
        "subtype_exact_accuracy": metric_value(subtype_exact_correct, subtype_total),
        "subtype_alias_accuracy": metric_value(subtype_alias_correct, subtype_total),
        "object_accuracy": metric_value(object_correct, object_total),
        "evidence_citation_validity": metric_value(evidence_valid_count, len(cases)),
        "root_candidate_citation_rate": metric_value(root_candidate_cited, root_candidate_applicable),
        "insufficient_evidence_rate": metric_value(insufficient_count, len(cases)),
        "unmapped_rate": metric_value(unmapped_cases, len(cases)),
        "top_level_anomaly_accuracy": metric_value(subtype_alias_correct, subtype_total),
        "route_vs_dns_vs_wifi_group_accuracy": metric_value(group_correct, group_total),
        "conservative_group_accuracy": metric_value(group_correct, group_total),
        "uncertainty_present_rate": metric_value(
            sum(1 for item in responses if isinstance(item.get("response"), dict) and item["response"].get("uncertainty")),
            len(cases),
        ),
    }
    metric_counts = {
        "family_accuracy": {"correct": family_correct, "total": family_total},
        "subtype_exact_accuracy": {"correct": subtype_exact_correct, "total": subtype_total},
        "subtype_alias_accuracy": {"correct": subtype_alias_correct, "total": subtype_total},
        "conservative_group_accuracy": {"correct": group_correct, "total": group_total},
        "object_accuracy": {"correct": object_correct, "total": object_total},
        "root_candidate_citation_rate": {"correct": root_candidate_cited, "total": root_candidate_applicable},
    }

    report = {
        "status": status,
        "dry_run": bool(args.dry_run),
        "inputs": {
            "prompts_jsonl": str(prompts_path),
            "responses_jsonl": str(responses_path),
            "validator_report": str(validator_path) if validator_path else None,
        },
        "counts": {
            "prompt_records": len(prompts),
            "response_records": len(responses),
            "matched_prompt_response_pairs": len(cases),
            "mapped_gt_cases": mapped_cases,
            "unmapped_cases": unmapped_cases,
        },
        "mapping": {
            "coverage": coverage,
            "method_counts": dict(method_counts.most_common()),
            "gt_field_usage": dict(gt_field_usage.most_common()),
            "chain_pool_summary": chain_pool_summary,
            "chain_parse_error_count": len(chain_parse_errors),
            "unmapped_examples": unmapped_examples,
        },
        "validator": {
            "status": validator.get("status"),
            "pass": validator.get("pass"),
            "warn": validator.get("warn"),
            "fail": validator.get("fail"),
            "hard_safety": validator.get("hard_safety"),
            "warnings": validator.get("warnings"),
        },
        "metrics": metrics,
        "metric_counts": metric_counts,
        "quality_flags": {
            "generic_prediction_count": int(quality_flags.get("generic_prediction_count", 0)),
            "concrete_root_without_evidence_count": int(quality_flags.get("concrete_root_without_evidence_count", 0)),
            "root_candidate_omitted_count": int(quality_flags.get("root_candidate_omitted_count", 0)),
            "unmapped_prediction_count": int(quality_flags.get("unmapped_prediction_count", 0)),
        },
        "examples": {
            "correct_examples": correct_examples,
            "incorrect_examples": incorrect_examples,
            "unmapped_examples": unmapped_examples,
            "generic_prediction_examples": generic_examples,
        },
        "parse_errors": {
            "prompt_errors": prompt_errors[: args.max_examples],
            "response_errors": response_errors[: args.max_examples],
        },
        "notes": [
            "GT is used only for offline evaluation and was not used in prompts or model inference.",
            "Prompts, responses, validator reports, canonical cases, datasets, and evidence sources are not modified.",
            "Metrics are preliminary unless explicitly finalized in a later review.",
            "OBS/root-candidate evidence is never substituted for GT.",
        ],
    }
    return report


def render_markdown(report: Dict[str, Any]) -> str:
    counts = report.get("counts") or {}
    metrics = report.get("metrics") or {}
    validator = report.get("validator") or {}
    mapping = report.get("mapping") or {}
    quality = report.get("quality_flags") or {}
    lines = [
        "# Evidence RCA Accuracy Evaluator Preflight",
        "",
        "## 1. Status",
        "",
        f"- `status`: {report.get('status')}",
        f"- `dry_run`: {str(report.get('dry_run')).lower()}",
        "",
        "## 2. Inputs",
        "",
        f"- `prompts_jsonl`: `{(report.get('inputs') or {}).get('prompts_jsonl')}`",
        f"- `responses_jsonl`: `{(report.get('inputs') or {}).get('responses_jsonl')}`",
        f"- `validator_report`: `{(report.get('inputs') or {}).get('validator_report')}`",
        "",
        "## 3. Mapping Coverage",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Prompt records | {counts.get('prompt_records')} |",
        f"| Response records | {counts.get('response_records')} |",
        f"| Matched pairs | {counts.get('matched_prompt_response_pairs')} |",
        f"| Mapped GT cases | {counts.get('mapped_gt_cases')} |",
        f"| Unmapped cases | {counts.get('unmapped_cases')} |",
        f"| Mapping coverage | {metrics.get('mapping_coverage')} |",
        "",
        "## 4. Validator Recap",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Status | {validator.get('status')} |",
        f"| Pass | {validator.get('pass')} |",
        f"| Warn | {validator.get('warn')} |",
        f"| Fail | {validator.get('fail')} |",
    ]
    for key, value in (validator.get("hard_safety") or {}).items():
        lines.append(f"| `{key}` | {value} |")

    lines.extend(["", "## 5. GT Field Usage", "", "| Field | Count |", "|---|---:|"])
    for key, value in (mapping.get("gt_field_usage") or {}).items():
        lines.append(f"| `{key}` | {value} |")
    if not (mapping.get("gt_field_usage") or {}):
        lines.append("| none | 0 |")

    lines.extend(
        [
            "",
            "## 6. Prediction Canonicalization",
            "",
            "Predictions are canonicalized deterministically from `root_cause` and `root_object` only. The evaluator uses explicit synonym sets for DNS, route, IPv4, Wi-Fi auth, link-down, and public reachability faults. Vague predictions are flagged as generic instead of being over-normalized.",
            "",
            "## 7. Preliminary Metrics, if available",
            "",
            "| Metric | Value |",
            "|---|---:|",
        ]
    )
    for key in (
        "family_accuracy",
        "subtype_exact_accuracy",
        "subtype_alias_accuracy",
        "object_accuracy",
        "root_candidate_citation_rate",
        "insufficient_evidence_rate",
        "evidence_citation_validity",
        "validator_pass_rate",
        "validator_fail_rate",
        "unmapped_rate",
    ):
        lines.append(f"| `{key}` | {metrics.get(key)} |")

    lines.extend(["", "## 8. Quality Flags", "", "| Flag | Count |", "|---|---:|"])
    for key, value in quality.items():
        lines.append(f"| `{key}` | {value} |")

    lines.extend(["", "## 9. Examples"])
    examples = report.get("examples") or {}
    for label in ("correct_examples", "incorrect_examples", "unmapped_examples", "generic_prediction_examples"):
        lines.append("")
        lines.append(f"### {label}")
        subset = examples.get(label) or []
        if not subset:
            lines.append("- none")
            continue
        for item in subset[:5]:
            lines.append(
                "- "
                + safe_json_dump(
                    {
                        "prompt_id": item.get("prompt_id"),
                        "case_ref": item.get("case_ref"),
                        "gt_subtype": item.get("gt_subtype"),
                        "predicted_subtype_candidate": item.get("predicted_subtype_candidate"),
                        "root_cause": item.get("root_cause"),
                        "subtype_alias_match": item.get("subtype_alias_match"),
                    }
                )
            )

    lines.extend(
        [
            "",
            "## 10. Limitations",
            "",
            "- GT was used only offline after model responses already existed.",
            "- Prompts and responses were not modified.",
            "- This is not model inference.",
            "- If mapping is incomplete, metrics must not be treated as final.",
            "- Metrics are preliminary until the user explicitly asks to finalize.",
            "- OBS/root-candidate evidence is not used as GT.",
            "",
            "## 11. Recommended Next Step",
            "",
        ]
    )
    if report.get("status") == "READY_FOR_ACCURACY_EVAL":
        lines.append("Run a reviewed accuracy evaluation pass using this tool with `--run`, then inspect incorrect examples before treating any metric as final.")
    elif report.get("status") == "PARTIAL_METRICS_READY":
        lines.append("Inspect unmapped cases before running a final accuracy evaluation.")
    elif report.get("status") == "MAPPING_INCOMPLETE":
        lines.append("Add or provide a deterministic prompt-to-canonical-case mapping source before computing final accuracy.")
    elif report.get("status") == "VALIDATOR_FAIL_BLOCKED":
        lines.append("Resolve validator failures before RCA accuracy evaluation.")
    elif report.get("status") == "HARD_SAFETY_BLOCKED":
        lines.append("Inspect hard-safety regressions before any accuracy evaluation.")
    else:
        lines.append("Inspect the reported blocker and rerun the preflight.")
    return "\n".join(lines) + "\n"


def render_output(report: Dict[str, Any], args: argparse.Namespace) -> str:
    if args.format == "json":
        return safe_json_dump(report, pretty=True) + "\n"
    if args.format == "markdown":
        return render_markdown(report)
    if args.format == "jsonl":
        return "\n".join(safe_json_dump(item) for item in (report.get("examples") or {}).get("correct_examples", [])) + "\n"
    return "----- JSON -----\n" + safe_json_dump(report, pretty=True) + "\n\n----- MARKDOWN -----\n" + render_markdown(report)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    root = Path(args.root).resolve()
    try:
        report = evaluate(args)
        output = render_output(report, args)
        if args.output and not args.stdout_only:
            output_path = resolve_path(root, args.output)
            if output_path is None:
                raise ValueError("invalid output path")
            validate_output_path(output_path, root)
            output_path.write_text(output, encoding="utf-8")
        else:
            sys.stdout.write(output)
    except Exception as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
