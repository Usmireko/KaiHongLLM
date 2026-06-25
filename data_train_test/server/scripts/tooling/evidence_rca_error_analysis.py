#!/usr/bin/env python3
"""Offline RCA error analysis for evidence diagnosis outputs.

This tool materializes an explicit prompt-to-GT mapping manifest and classifies
RCA prediction outcomes after model responses already exist. It never calls a
model and never modifies prompts, responses, evidence, canonical cases, or
dataset artifacts.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


sys.dont_write_bytecode = True

DEFAULT_PROMPTS = "manual_experiments/evidence_qwen3_direct_diag50_20260527/prompts.jsonl"
DEFAULT_RESPONSES = "manual_experiments/evidence_qwen3_direct_diag50_20260527/responses.jsonl"
DEFAULT_VALIDATOR = "manual_experiments/evidence_qwen3_direct_diag50_20260527/validator_report.json"
DEFAULT_ACCURACY = "manual_experiments/evidence_qwen3_direct_diag50_20260527/rca_accuracy_eval_preflight.json"

ALLOWED_OUTPUTS = {
    "manual_experiments/evidence_qwen3_direct_diag50_20260527/prompt_gt_mapping_manifest.jsonl",
    "manual_experiments/evidence_qwen3_direct_diag50_20260527/prompt_gt_mapping_manifest_summary.json",
    "manual_experiments/evidence_qwen3_direct_diag50_20260527/rca_accuracy_error_analysis.json",
    "manual_experiments/evidence_qwen3_direct_diag50_20260527/rca_accuracy_error_analysis.md",
    "manual_experiments/evidence_qwen3_direct_diag50_20260527/rca_accuracy_eval_refined.json",
    "manual_experiments/evidence_qwen3_direct_diag50_20260527/rca_accuracy_eval_refined.md",
    "manual_experiments/evidence_qwen3_direct_diag50_20260527/link_flap_taxonomy_review.json",
    "manual_experiments/evidence_qwen3_direct_diag50_20260527/link_flap_taxonomy_review.md",
}

CLASSIFICATIONS = (
    "exact_correct",
    "alias_correct",
    "group_correct_subtype_wrong",
    "generic_prediction",
    "true_mismatch",
    "canonicalization_gap",
    "link_flap_taxonomy_issue",
    "insufficient_evidence_output",
    "mapping_uncertain",
    "manual_review",
)

GROUP_BY_SUBTYPE = {
    "net_dns_fail": "dns",
    "net_no_default_route": "route",
    "net_wrong_default_route": "route",
    "net_no_ipv4_on_iface": "ipv4",
    "net_wifi_disconnect": "link_state",
    "net_link_down": "link_state",
    "net_link_flap": "link_state",
    "net_wlan_disconnect": "link_state",
    "net_wifi_auth_fail_wrong_psk": "wifi_auth",
    "net_public_ip_unreachable": "public_reachability",
}


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Offline RCA error analysis for evidence diagnosis outputs.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--prompts-jsonl", default=DEFAULT_PROMPTS)
    parser.add_argument("--responses-jsonl", default=DEFAULT_RESPONSES)
    parser.add_argument("--validator-report", default=DEFAULT_VALIDATOR)
    parser.add_argument("--accuracy-report", default=DEFAULT_ACCURACY)
    parser.add_argument("--chains-jsonl", default=None)
    parser.add_argument("--canonical-root", default=None)
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    parser.add_argument("--run", dest="dry_run", action="store_false")
    parser.add_argument("--stdout-only", action="store_true")
    parser.add_argument("--format", choices=("json", "markdown", "both", "jsonl"), default="both")
    parser.add_argument("--max-examples", type=int, default=10)
    parser.add_argument("--write-manifest", default=None)
    parser.add_argument("--write-manifest-summary", default=None)
    parser.add_argument("--output-json", default=None)
    parser.add_argument("--output-md", default=None)
    parser.add_argument("--output-refined-json", default=None)
    parser.add_argument("--output-refined-md", default=None)
    parser.add_argument("--output-link-flap-json", default=None)
    parser.add_argument("--output-link-flap-md", default=None)
    parser.add_argument("--redact-sensitive", dest="redact_sensitive", action="store_true", default=True)
    parser.add_argument("--no-redact-sensitive", dest="redact_sensitive", action="store_false")
    return parser.parse_args(argv)


def import_accuracy_module(root: Path) -> Any:
    path = root / "tools" / "evidence_rca_accuracy_eval.py"
    spec = importlib.util.spec_from_file_location("evidence_rca_accuracy_eval_for_error_analysis", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to import accuracy evaluator from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def safe_json_dump(obj: Any, *, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2)
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_token(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.strip().lower()
    text = re.sub(r"[\s\-]+", "_", text)
    text = re.sub(r"[^a-z0-9_]+", "", text)
    return text


def compact_text(value: Any, limit: int = 180) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def resolve_path(root: Path, value: Optional[str]) -> Optional[Path]:
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def relative_posix(root: Path, path: Optional[Path]) -> Optional[str]:
    if path is None:
        return None
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def validate_output_path(root: Path, path: Path) -> None:
    resolved = path.resolve()
    rel = relative_posix(root, resolved)
    if rel not in ALLOWED_OUTPUTS:
        raise ValueError(f"refusing output outside allowed task files: {resolved}")
    if resolved.exists():
        raise ValueError(f"OUTPUT_ALREADY_EXISTS: {resolved}")
    if not resolved.parent.exists():
        raise ValueError(f"output parent does not exist: {resolved.parent}")


def get_path_value(obj: Dict[str, Any], path: Sequence[str]) -> Any:
    current: Any = obj
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def first_value(obj: Dict[str, Any], paths: Iterable[Sequence[str]]) -> Tuple[Optional[str], Optional[str]]:
    for path in paths:
        value = get_path_value(obj, path)
        if value not in (None, ""):
            return str(value), ".".join(path)
    return None, None


def extract_prompt_payload(acc: Any, prompt: Dict[str, Any]) -> Dict[str, Any]:
    value = acc.extract_prompt_payload(prompt)
    return value if isinstance(value, dict) else {}


def prompt_policy_and_candidates(acc: Any, prompt: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[str], List[str]]:
    payload = extract_prompt_payload(acc, prompt)
    chain = payload.get("evidence_chain") if isinstance(payload.get("evidence_chain"), dict) else {}
    policy = payload.get("evidence_id_policy") if isinstance(payload.get("evidence_id_policy"), dict) else {}
    root_candidates = [item for item in chain.get("root_candidates") or [] if isinstance(item, dict)]
    root_ids = [str(item.get("evidence_id")) for item in root_candidates if item.get("evidence_id")]
    diagnostic_ids = [str(item) for item in policy.get("diagnostic_evidence_ids") or [] if item]
    forbidden_ids = [str(item) for item in policy.get("forbidden_evidence_ids") or [] if item]
    if not diagnostic_ids:
        seen = set()
        for items in chain.values():
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict) and item.get("evidence_id"):
                    eid = str(item["evidence_id"])
                    if eid not in seen:
                        seen.add(eid)
                        diagnostic_ids.append(eid)
    return policy, root_candidates, diagnostic_ids, forbidden_ids


def group_for_subtype(subtype: Optional[str]) -> Optional[str]:
    if not subtype:
        return None
    token = normalize_token(subtype)
    return GROUP_BY_SUBTYPE.get(token)


def group_match(predicted: Optional[str], gt_subtype: Optional[str]) -> Optional[bool]:
    if not predicted or not gt_subtype:
        return None
    pg = group_for_subtype(predicted)
    gg = group_for_subtype(gt_subtype)
    if not pg or not gg:
        return None
    return pg == gg


def is_link_state_canonicalization_gap(predicted: Optional[str], gt_subtype: Optional[str], root_cause: Any) -> bool:
    if normalize_token(gt_subtype) != "net_link_flap":
        return False
    if normalize_token(predicted) not in {"net_wifi_disconnect", "net_link_down", "net_wlan_disconnect"}:
        return False
    text = f"{root_cause or ''}".lower()
    return any(term in text for term in ("interface", "down", "disconnect", "link"))


def is_link_state_boundary_case(predicted: Optional[str], gt_subtype: Optional[str]) -> bool:
    if normalize_token(gt_subtype) != "net_link_flap":
        return False
    return normalize_token(predicted) in {"net_wifi_disconnect", "net_link_down", "net_wlan_disconnect"}


def gt_root_cause_field(canonical: Optional[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
    if not canonical:
        return None, None
    return first_value(
        canonical,
        (
            ("gt", "root_cause"),
            ("ground_truth", "root_cause"),
            ("label", "root_cause"),
            ("root_cause",),
        ),
    )


def gt_domain_field(canonical: Optional[Dict[str, Any]]) -> Tuple[Optional[str], Optional[str]]:
    if not canonical:
        return None, None
    return first_value(
        canonical,
        (
            ("gt", "domain"),
            ("ground_truth", "domain"),
            ("label", "domain"),
            ("domain",),
        ),
    )


def summarize_root_candidates(root_candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    summary: List[Dict[str, Any]] = []
    for item in root_candidates:
        summary.append(
            {
                "evidence_id": item.get("evidence_id"),
                "object": item.get("object"),
                "anomaly_type": item.get("anomaly_type"),
                "source": item.get("source"),
                "summary": compact_text(item.get("summary")),
            }
        )
    return summary


def classify_case(
    *,
    mapped: bool,
    root_cause: Any,
    predicted_subtype: Optional[str],
    gt_subtype: Optional[str],
    exact_match: Optional[bool],
    alias_match: Optional[bool],
    group_ok: Optional[bool],
    specificity: str,
    root_candidate_cited: Optional[bool],
    link_flap_semantics: bool = False,
) -> Tuple[str, Optional[str], str]:
    if not mapped:
        return "mapping_uncertain", "case could not be confidently mapped to GT", "manual_review"
    if normalize_token(root_cause) == "insufficient_evidence":
        return "insufficient_evidence_output", "model returned insufficient_evidence", "manual_review"
    if exact_match is True:
        return "exact_correct", None, "accept"
    if alias_match is True:
        return "alias_correct", "deterministic alias maps prediction to GT subtype", "accept"
    if specificity == "generic" or not predicted_subtype:
        return "generic_prediction", "prediction is too generic to map to a subtype", "improve_prompt"
    if is_link_state_boundary_case(predicted_subtype, gt_subtype) and root_candidate_cited:
        if link_flap_semantics:
            return "canonicalization_gap", "explicit flap semantics should have been alias-correct", "extend_alias_map"
        return (
            "link_flap_taxonomy_issue",
            "GT is net_link_flap but prompt/response only expose generic link-down or disconnect wording",
            "adjust_evidence_anomaly_taxonomy",
        )
    if group_ok is True:
        return "group_correct_subtype_wrong", "coarse fault group matches but subtype differs", "manual_review"
    return "true_mismatch", "prediction maps to a different fault group or subtype than GT", "manual_review"


def load_accuracy_report(path: Optional[Path]) -> Dict[str, Any]:
    if not path or not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def build_analysis(args: argparse.Namespace) -> Tuple[Dict[str, Any], List[Dict[str, Any]], Dict[str, Any]]:
    root = Path(args.root).resolve()
    acc = import_accuracy_module(root)
    prompts_path = resolve_path(root, args.prompts_jsonl)
    responses_path = resolve_path(root, args.responses_jsonl)
    validator_path = resolve_path(root, args.validator_report) if args.validator_report else None
    accuracy_path = resolve_path(root, args.accuracy_report) if args.accuracy_report else None
    canonical_root = resolve_path(root, args.canonical_root) if args.canonical_root else root
    if prompts_path is None or responses_path is None or canonical_root is None:
        raise RuntimeError("prompts, responses, and canonical root must be resolvable")
    if not prompts_path.exists():
        raise RuntimeError(f"missing prompts JSONL: {prompts_path}")
    if not responses_path.exists():
        raise RuntimeError(f"missing responses JSONL: {responses_path}")

    prompts, prompt_errors = acc.load_jsonl(prompts_path)
    responses, response_errors = acc.load_jsonl(responses_path)
    prompts_by_id = {str(item.get("prompt_id")): item for item in prompts if item.get("prompt_id")}
    responses_by_id = {str(item.get("prompt_id")): item for item in responses if item.get("prompt_id")}
    matched_ids = [pid for pid in prompts_by_id if pid in responses_by_id]

    regen_args = argparse.Namespace(
        root=str(root),
        prompts_jsonl=str(prompts_path),
        responses_jsonl=str(responses_path),
        validator_report=str(validator_path) if validator_path else None,
        chains_jsonl=args.chains_jsonl,
        canonical_root=str(canonical_root),
        dry_run=True,
        stdout_only=True,
        format="json",
        max_examples=args.max_examples,
        min_mapping_coverage=0.95,
        allow_partial_metrics=False,
        output=None,
        redact_sensitive=args.redact_sensitive,
    )
    regenerated_map, chain_pool_summary, chain_parse_errors = acc.build_regenerated_chain_map(root, regen_args)
    canonical_index = acc.build_canonical_index(canonical_root) if canonical_root.exists() else {}
    validator = acc.load_validator_report(validator_path)
    accuracy_report = load_accuracy_report(accuracy_path)

    manifest_rows: List[Dict[str, Any]] = []
    cases: List[Dict[str, Any]] = []
    method_counts: Counter[str] = Counter()
    confidence_counts: Counter[str] = Counter()
    gt_field_usage: Counter[str] = Counter()
    classification_counts: Counter[str] = Counter()
    by_gt_subtype: Dict[str, Counter[str]] = defaultdict(Counter)
    hard_safety_zero = all(int(value or 0) == 0 for value in (validator.get("hard_safety") or {}).values())

    exact_correct = 0
    alias_correct_total = 0
    root_candidate_cited_count = 0
    root_candidate_applicable = 0
    evidence_valid_count = 0
    family_correct = family_total = 0
    group_correct = group_total = 0

    for pid in matched_ids:
        prompt = prompts_by_id[pid]
        response_row = responses_by_id[pid]
        response = response_row.get("response") if isinstance(response_row.get("response"), dict) else {}
        root_cause = response.get("root_cause") if response else response_row.get("response_text")
        root_object = response.get("root_object") if response else None
        evidence_used = response.get("evidence_used") if isinstance(response.get("evidence_used"), list) else []
        evidence_used = [str(item) for item in evidence_used if item]
        _policy, root_candidates, diagnostic_ids, forbidden_ids = prompt_policy_and_candidates(acc, prompt)
        root_ids = [str(item.get("evidence_id")) for item in root_candidates if item.get("evidence_id")]
        concrete_root = bool(root_cause and normalize_token(root_cause) != "insufficient_evidence")
        root_candidate_cited = bool(set(evidence_used) & set(root_ids)) if concrete_root else None
        if concrete_root:
            root_candidate_applicable += 1
            if root_candidate_cited:
                root_candidate_cited_count += 1
        evidence_citation_valid = bool(evidence_used) and all(eid in diagnostic_ids for eid in evidence_used) and not (
            set(evidence_used) & set(forbidden_ids)
        )
        if evidence_citation_valid:
            evidence_valid_count += 1

        direct_meta = acc.direct_prompt_mapping(prompt)
        chain_meta = direct_meta or regenerated_map.get(pid)
        chain_method = str((chain_meta or {}).get("method") or "unmapped")
        canonical, canonical_path, canonical_method = acc.find_canonical_case(root, canonical_root, chain_meta, canonical_index)
        mapped = bool(canonical and canonical_path)
        if direct_meta and mapped:
            mapping_method = "direct_prompt_metadata"
            mapping_confidence = "high"
        elif chain_meta and mapped:
            mapping_method = "regenerated_chain_metadata"
            mapping_confidence = "high" if canonical_method == "case_rel_canonical_case" else "medium"
        else:
            mapping_method = "unmapped"
            mapping_confidence = "low"
        method_counts[mapping_method] += 1
        confidence_counts[mapping_confidence] += 1

        gt_family = gt_subtype = gt_object = None
        gt_family_field = gt_subtype_field = gt_object_field = None
        gt_domain, gt_domain_used = gt_domain_field(canonical)
        gt_root_cause, gt_root_cause_used = gt_root_cause_field(canonical)
        if canonical:
            gt_family, gt_family_field = acc.first_gt_value(canonical, "family")
            gt_subtype, gt_subtype_field = acc.first_gt_value(canonical, "subtype")
            gt_object, gt_object_field = acc.first_gt_value(canonical, "object")
        for field in (gt_family_field, gt_subtype_field, gt_object_field, gt_domain_used, gt_root_cause_used):
            if field:
                gt_field_usage[field] += 1

        root_candidate_summary = summarize_root_candidates(root_candidates)
        alias_context: List[Any] = [root_cause, root_object]
        for item in root_candidate_summary:
            alias_context.extend([item.get("summary"), item.get("anomaly_type"), item.get("object"), item.get("source")])

        predicted_subtype, specificity = acc.canonicalize_subtype_from_text(root_cause, root_object)
        predicted_family = acc.family_from_subtype(predicted_subtype)
        exact_match = acc.subtype_exact_match(predicted_subtype, gt_subtype)
        alias_match = acc.subtype_alias_match(predicted_subtype, gt_subtype, alias_context)
        group_ok = acc.subtype_group_match(predicted_subtype, gt_subtype)
        link_flap_semantics = acc.has_link_flap_semantics(*alias_context)
        family_match = None
        if gt_family:
            family_total += 1
            family_match = normalize_token(predicted_family) == normalize_token(gt_family)
            if family_match:
                family_correct += 1
        if group_ok is not None:
            group_total += 1
            if group_ok:
                group_correct += 1
        if exact_match:
            exact_correct += 1
        if alias_match:
            alias_correct_total += 1

        classification, error_reason, recommendation = classify_case(
            mapped=mapped,
            root_cause=root_cause,
            predicted_subtype=predicted_subtype,
            gt_subtype=gt_subtype,
            exact_match=exact_match,
            alias_match=alias_match,
            group_ok=group_ok,
            specificity=specificity,
            root_candidate_cited=root_candidate_cited,
            link_flap_semantics=link_flap_semantics,
        )
        classification_counts[classification] += 1
        gt_key = normalize_token(gt_subtype) or "unmapped"
        by_gt_subtype[gt_key]["total"] += 1
        if classification not in {"exact_correct", "alias_correct"}:
            by_gt_subtype[gt_key][classification] += 1
        if exact_match:
            by_gt_subtype[gt_key]["exact_correct"] += 1
        if alias_match:
            by_gt_subtype[gt_key]["alias_correct"] += 1
        if classification == "alias_correct":
            by_gt_subtype[gt_key]["alias_only_correct"] += 1
        if classification not in {"exact_correct", "alias_correct"}:
            by_gt_subtype[gt_key]["errors"] += 1

        case_common = {
            "prompt_id": pid,
            "case_ref": prompt.get("case_ref") or response_row.get("case_ref"),
            "gt_subtype": gt_subtype,
            "gt_family": gt_family,
            "pred_root_cause": root_cause,
            "pred_root_object": root_object,
            "predicted_subtype_exact": predicted_subtype if exact_match else None,
            "predicted_subtype_alias": predicted_subtype if alias_match else None,
            "predicted_subtype_candidate": predicted_subtype,
            "predicted_family": predicted_family,
            "evidence_used": evidence_used,
            "root_candidate_cited": root_candidate_cited,
            "classification": classification,
            "error_reason": error_reason,
            "recommendation": recommendation,
            "link_flap_semantics": link_flap_semantics,
            "root_candidate_evidence": root_candidate_summary,
        }
        cases.append(case_common)

        manifest_rows.append(
            {
                "prompt_id": pid,
                "case_ref": case_common["case_ref"],
                "response_present": True,
                "canonical_case_path": relative_posix(root, canonical_path),
                "case_rel": (chain_meta or {}).get("case_rel"),
                "mapping_method": mapping_method,
                "mapping_confidence": mapping_confidence,
                "gt_fields": {
                    "family": gt_family,
                    "subtype": gt_subtype,
                    "domain": gt_domain,
                    "root_cause": gt_root_cause,
                    "object": gt_object,
                },
                "prediction": {
                    "root_cause": root_cause,
                    "root_object": root_object,
                    "evidence_used": evidence_used,
                    "predicted_family": predicted_family,
                    "predicted_subtype_exact": case_common["predicted_subtype_exact"],
                    "predicted_subtype_alias": case_common["predicted_subtype_alias"],
                    "predicted_subtype_candidate": predicted_subtype,
                },
                "evaluation": {
                    "family_match": family_match,
                    "subtype_exact_match": exact_match,
                    "subtype_alias_match": alias_match,
                    "group_match": group_ok,
                    "root_candidate_cited": root_candidate_cited,
                    "evidence_citation_valid": evidence_citation_valid,
                },
                "root_candidate_evidence": root_candidate_summary,
                "classification": classification,
                "notes": error_reason or "",
            }
        )

    total = len(cases)
    mapped_records = sum(1 for item in manifest_rows if item.get("canonical_case_path"))
    mapping_coverage = round(mapped_records / total, 4) if total else 0.0
    alias_only = alias_correct_total - exact_correct
    mismatch_unresolved = total - alias_correct_total
    input_metrics = (accuracy_report.get("metrics") if isinstance(accuracy_report.get("metrics"), dict) else {}) or {}
    validator_summary = {
        "status": validator.get("status"),
        "pass": int(validator.get("pass") or 0),
        "warn": int(validator.get("warn") or 0),
        "fail": int(validator.get("fail") or 0),
        "hard_safety": validator.get("hard_safety"),
    }

    if not hard_safety_zero or validator_summary["fail"] > 0 or mapped_records < total:
        status = "FAIL"
    elif classification_counts.get("true_mismatch", 0) or classification_counts.get("manual_review", 0):
        status = "WARN"
    elif mismatch_unresolved:
        status = "WARN"
    else:
        status = "PASS"

    if classification_counts.get("true_mismatch", 0) or classification_counts.get("manual_review", 0):
        next_action = "manual_review_errors"
        reason = "Some alias-uncovered cases map to different GT groups/subtypes and need manual review before final accuracy."
    elif classification_counts.get("canonicalization_gap", 0):
        next_action = "extend_alias_map"
        reason = "Remaining unresolved cases appear to be canonicalization gaps."
    elif classification_counts.get("link_flap_taxonomy_issue", 0):
        next_action = "adjust_evidence_anomaly_taxonomy"
        reason = "Remaining link-flap cases need explicit flap/recovery evidence before alias credit is safe."
    elif classification_counts.get("generic_prediction", 0):
        next_action = "improve_prompt"
        reason = "Some predictions are too generic for subtype scoring."
    else:
        next_action = "proceed_to_196"
        reason = "All cases are exact or alias-correct under the current manifest."

    counts = {
        "total_cases": total,
        "exact_correct": exact_correct,
        "alias_correct_total": alias_correct_total,
        "alias_only_correct": alias_only,
        "mismatch_unresolved": mismatch_unresolved,
        "group_correct_subtype_wrong": int(classification_counts.get("group_correct_subtype_wrong", 0)),
        "generic_prediction": int(classification_counts.get("generic_prediction", 0)),
        "true_mismatch": int(classification_counts.get("true_mismatch", 0)),
        "canonicalization_gap": int(classification_counts.get("canonicalization_gap", 0)),
        "link_flap_taxonomy_issue": int(classification_counts.get("link_flap_taxonomy_issue", 0)),
        "insufficient_evidence_output": int(classification_counts.get("insufficient_evidence_output", 0)),
        "mapping_uncertain": int(classification_counts.get("mapping_uncertain", 0)),
        "manual_review": int(classification_counts.get("manual_review", 0)),
    }
    for name in CLASSIFICATIONS:
        counts.setdefault(name, int(classification_counts.get(name, 0)))

    analysis = {
        "status": status,
        "dry_run": bool(args.dry_run),
        "inputs": {
            "prompts_jsonl": str(prompts_path),
            "responses_jsonl": str(responses_path),
            "accuracy_report": str(accuracy_path) if accuracy_path else None,
            "validator_report": str(validator_path) if validator_path else None,
        },
        "input_metrics": {
            "family_accuracy": input_metrics.get("family_accuracy"),
            "subtype_exact_accuracy": input_metrics.get("subtype_exact_accuracy"),
            "subtype_alias_accuracy": input_metrics.get("subtype_alias_accuracy"),
            "group_accuracy": input_metrics.get("route_vs_dns_vs_wifi_group_accuracy") or input_metrics.get("group_accuracy"),
            "root_candidate_citation_rate": input_metrics.get("root_candidate_citation_rate"),
            "evidence_citation_validity": input_metrics.get("evidence_citation_validity"),
        },
        "computed_metrics": {
            "mapping_coverage": mapping_coverage,
            "family_accuracy": round(family_correct / family_total, 4) if family_total else None,
            "subtype_exact_accuracy": round(exact_correct / total, 4) if total else None,
            "subtype_alias_accuracy": round(alias_correct_total / total, 4) if total else None,
            "group_accuracy": round(group_correct / group_total, 4) if group_total else None,
            "root_candidate_citation_rate": round(root_candidate_cited_count / root_candidate_applicable, 4)
            if root_candidate_applicable
            else None,
            "evidence_citation_validity": round(evidence_valid_count / total, 4) if total else None,
        },
        "validator": validator_summary,
        "hard_safety_zero": hard_safety_zero,
        "counts": counts,
        "classification_counts": dict(classification_counts.most_common()),
        "by_gt_subtype": {
            subtype: {key: int(value) for key, value in counts_for_subtype.items()}
            for subtype, counts_for_subtype in sorted(by_gt_subtype.items())
        },
        "cases": cases,
        "recommendation": {
            "next_action": next_action,
            "reason": reason,
        },
        "notes": [
            "GT was used only offline after model responses already existed.",
            "Prompts, responses, validator reports, canonical cases, evidence, and datasets were not modified.",
            "No model was called and no prompts were sent.",
            "Accuracy remains preliminary until the prompt-to-GT manifest and mismatch classifications are reviewed.",
        ],
        "parse_errors": {
            "prompt_errors": prompt_errors[: args.max_examples],
            "response_errors": response_errors[: args.max_examples],
            "chain_parse_errors": chain_parse_errors[: args.max_examples],
        },
        "chain_pool_summary": chain_pool_summary,
    }

    manifest_summary = {
        "total_records": total,
        "mapped_records": mapped_records,
        "mapping_coverage": mapping_coverage,
        "mapping_method_counts": dict(method_counts.most_common()),
        "mapping_confidence_counts": dict(confidence_counts.most_common()),
        "gt_field_usage": dict(gt_field_usage.most_common()),
        "unmapped_records": [
            {
                "prompt_id": item.get("prompt_id"),
                "case_ref": item.get("case_ref"),
                "mapping_method": item.get("mapping_method"),
                "mapping_confidence": item.get("mapping_confidence"),
            }
            for item in manifest_rows
            if not item.get("canonical_case_path")
        ],
        "manifest_status": "PASS" if mapped_records == total else ("WARN" if mapped_records else "FAIL"),
    }
    return analysis, manifest_rows, manifest_summary


def render_markdown(analysis: Dict[str, Any], max_examples: int) -> str:
    counts = analysis.get("counts") or {}
    computed = analysis.get("computed_metrics") or {}
    validator = analysis.get("validator") or {}
    by_subtype = analysis.get("by_gt_subtype") or {}
    cases = analysis.get("cases") or []
    exact_cases = [item for item in cases if item.get("classification") == "exact_correct"]
    alias_cases = [item for item in cases if item.get("classification") == "alias_correct"]
    unresolved_cases = [item for item in cases if item.get("classification") not in {"exact_correct", "alias_correct"}]

    lines = [
        "# Evidence RCA Accuracy Error Analysis",
        "",
        "## 1. Status",
        "",
        f"- `status`: {analysis.get('status')}",
        f"- `dry_run`: {str(analysis.get('dry_run')).lower()}",
        f"- Recommendation: `{(analysis.get('recommendation') or {}).get('next_action')}`",
        "",
        "## 2. Inputs and Mapping",
        "",
        f"- `prompts_jsonl`: `{(analysis.get('inputs') or {}).get('prompts_jsonl')}`",
        f"- `responses_jsonl`: `{(analysis.get('inputs') or {}).get('responses_jsonl')}`",
        f"- `accuracy_report`: `{(analysis.get('inputs') or {}).get('accuracy_report')}`",
        f"- Total cases: {counts.get('total_cases')}",
        f"- Mapping coverage: {computed.get('mapping_coverage')}",
        "",
        "## 3. Overall Metrics",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Validator status | {validator.get('status')} |",
        f"| Validator pass / warn / fail | {validator.get('pass')} / {validator.get('warn')} / {validator.get('fail')} |",
        f"| Hard safety zero | {str(analysis.get('hard_safety_zero')).lower()} |",
        f"| Family accuracy | {computed.get('family_accuracy')} |",
        f"| Subtype exact accuracy | {computed.get('subtype_exact_accuracy')} |",
        f"| Subtype alias accuracy | {computed.get('subtype_alias_accuracy')} |",
        f"| Group accuracy | {computed.get('group_accuracy')} |",
        f"| Root candidate citation rate | {computed.get('root_candidate_citation_rate')} |",
        f"| Evidence citation validity | {computed.get('evidence_citation_validity')} |",
        "",
        "## 4. Error Category Summary",
        "",
        "| Category | Count |",
        "|---|---:|",
    ]
    for key in (
        "exact_correct",
        "alias_correct_total",
        "alias_only_correct",
        "mismatch_unresolved",
        "canonicalization_gap",
        "link_flap_taxonomy_issue",
        "group_correct_subtype_wrong",
        "generic_prediction",
        "true_mismatch",
        "insufficient_evidence_output",
        "mapping_uncertain",
        "manual_review",
    ):
        lines.append(f"| `{key}` | {counts.get(key, 0)} |")

    lines.extend(["", "## 5. Breakdown by GT Subtype", "", "| GT subtype | Total | Exact | Alias | Errors |", "|---|---:|---:|---:|---:|"])
    for subtype, item in by_subtype.items():
        lines.append(
            f"| `{subtype}` | {item.get('total', 0)} | {item.get('exact_correct', 0)} | {item.get('alias_correct', 0)} | {item.get('errors', 0)} |"
        )

    def case_line(item: Dict[str, Any]) -> str:
        return (
            f"- `{item.get('prompt_id')}` `{item.get('case_ref')}`: "
            f"GT `{item.get('gt_subtype')}`, predicted `{item.get('predicted_subtype_candidate')}` "
            f"from `{compact_text(item.get('pred_root_cause'), 80)}`"
        )

    lines.extend(["", "## 6. Exact-correct Cases", ""])
    for item in exact_cases[:max_examples]:
        lines.append(case_line(item))
    if len(exact_cases) > max_examples:
        lines.append(f"- ... {len(exact_cases) - max_examples} more exact-correct cases omitted from Markdown.")
    if not exact_cases:
        lines.append("- none")

    lines.extend(["", "## 7. Alias-only Correct Cases", ""])
    for item in alias_cases[:max_examples]:
        lines.append(case_line(item))
    if not alias_cases:
        lines.append("- none")

    lines.extend(["", "## 8. Mismatch / Unresolved Cases", ""])
    for item in unresolved_cases:
        lines.append(
            case_line(item)
            + f"; classification `{item.get('classification')}`; reason: {item.get('error_reason') or 'n/a'}; recommendation `{item.get('recommendation')}`"
        )
    if not unresolved_cases:
        lines.append("- none")

    lines.extend(
        [
            "",
            "## 9. Recommended Fixes",
            "",
            f"- Next action: `{(analysis.get('recommendation') or {}).get('next_action')}`",
            f"- Reason: {(analysis.get('recommendation') or {}).get('reason')}",
            "- Review true mismatches before treating RCA accuracy as final.",
            "- Keep link-flap alias handling guarded by explicit flap/recovery evidence.",
            "",
            "## 10. Limitations",
            "",
            "- GT was used only offline.",
            "- Prompts/responses were not modified.",
            "- No model was called.",
            "- This is not a training step.",
            "- Accuracy is preliminary until the prompt-to-GT manifest is reviewed.",
            "",
            "## 11. Recommended Next Step",
            "",
        ]
    )
    next_action = (analysis.get("recommendation") or {}).get("next_action")
    if next_action == "manual_review_errors":
        lines.append("Manually review the mismatch/unresolved cases, then decide whether to extend aliases, improve prompts, or proceed to the next experiment.")
    elif next_action == "extend_alias_map":
        lines.append("Extend the evaluator alias map in a scoped task, then rerun this analysis.")
    elif next_action == "improve_prompt":
        lines.append("Improve prompt specificity before using these metrics as final.")
    else:
        lines.append("Proceed only after accepting this manifest as the official prompt-to-GT mapping.")
    return "\n".join(lines) + "\n"


def load_previous_error_analysis(root: Path) -> Dict[str, Any]:
    path = root / "manual_experiments" / "evidence_qwen3_direct_diag50_20260527" / "rca_accuracy_error_analysis.json"
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def build_refined_report(root: Path, analysis: Dict[str, Any]) -> Dict[str, Any]:
    previous = load_previous_error_analysis(root)
    previous_cases = {
        str(item.get("prompt_id")): item
        for item in (previous.get("cases") or [])
        if isinstance(item, dict) and item.get("prompt_id")
    }
    changed_cases: List[Dict[str, Any]] = []
    for item in analysis.get("cases") or []:
        old = previous_cases.get(str(item.get("prompt_id"))) or {}
        old_class = old.get("classification")
        new_class = item.get("classification")
        if old_class != new_class:
            changed_cases.append(
                {
                    "prompt_id": item.get("prompt_id"),
                    "case_ref": item.get("case_ref"),
                    "gt_subtype": item.get("gt_subtype"),
                    "old_classification": old_class,
                    "new_classification": new_class,
                    "reason": item.get("error_reason"),
                }
            )
    unresolved = [
        {
            "prompt_id": item.get("prompt_id"),
            "case_ref": item.get("case_ref"),
            "gt_subtype": item.get("gt_subtype"),
            "predicted_subtype": item.get("predicted_subtype_candidate"),
            "classification": item.get("classification"),
            "reason": item.get("error_reason"),
        }
        for item in analysis.get("cases") or []
        if item.get("classification") not in {"exact_correct", "alias_correct"}
    ]
    input_metrics = analysis.get("input_metrics") or {}
    computed = analysis.get("computed_metrics") or {}
    return {
        "status": analysis.get("status"),
        "baseline_metrics": {
            "subtype_exact_accuracy": input_metrics.get("subtype_exact_accuracy"),
            "subtype_alias_accuracy": input_metrics.get("subtype_alias_accuracy"),
            "group_accuracy": input_metrics.get("group_accuracy"),
        },
        "refined_metrics": {
            "subtype_exact_accuracy": computed.get("subtype_exact_accuracy"),
            "subtype_alias_accuracy": computed.get("subtype_alias_accuracy"),
            "group_accuracy": computed.get("group_accuracy"),
            "family_accuracy": computed.get("family_accuracy"),
            "root_candidate_citation_rate": computed.get("root_candidate_citation_rate"),
            "evidence_citation_validity": computed.get("evidence_citation_validity"),
        },
        "counts": analysis.get("counts"),
        "changed_cases": changed_cases,
        "remaining_unresolved": unresolved,
        "recommendation": {
            "next_action": "review_taxonomy_before_final_accuracy",
            "reason": "Conservative canonicalization avoids broad link-down to link-flap credit; remaining cases need taxonomy or prompt-rule review.",
        },
        "notes": [
            "Metrics remain preliminary until reviewed.",
            "Generic disconnect/link-down wording is not credited as net_link_flap without explicit flap/recovery semantics.",
            "Wrong-default-route remains distinct from no-default-route.",
            "No model was called and no prompts/responses/GT files were modified.",
        ],
    }


def render_refined_markdown(report: Dict[str, Any]) -> str:
    counts = report.get("counts") or {}
    baseline = report.get("baseline_metrics") or {}
    refined = report.get("refined_metrics") or {}
    lines = [
        "# Refined RCA Accuracy Evaluation Preview",
        "",
        "## 1. Status",
        "",
        f"- Status: `{report.get('status')}`",
        "- Scope: offline evaluator canonicalization preview",
        "- Final-paper accuracy: not claimed",
        "",
        "## 2. Baseline vs Refined Metrics",
        "",
        "| Metric | Baseline | Refined |",
        "|---|---:|---:|",
        f"| Subtype exact accuracy | {baseline.get('subtype_exact_accuracy')} | {refined.get('subtype_exact_accuracy')} |",
        f"| Subtype alias accuracy | {baseline.get('subtype_alias_accuracy')} | {refined.get('subtype_alias_accuracy')} |",
        f"| Group accuracy | {baseline.get('group_accuracy')} | {refined.get('group_accuracy')} |",
        f"| Family accuracy | n/a | {refined.get('family_accuracy')} |",
        f"| Root-candidate citation rate | n/a | {refined.get('root_candidate_citation_rate')} |",
        f"| Evidence citation validity | n/a | {refined.get('evidence_citation_validity')} |",
        "",
        "## 3. Refined Counts",
        "",
        "| Category | Count |",
        "|---|---:|",
    ]
    for key in (
        "total_cases",
        "exact_correct",
        "alias_correct_total",
        "alias_only_correct",
        "mismatch_unresolved",
        "link_flap_taxonomy_issue",
        "group_correct_subtype_wrong",
        "true_mismatch",
    ):
        lines.append(f"| `{key}` | {counts.get(key, 0)} |")
    lines.extend(["", "## 4. Changed Cases", ""])
    for item in report.get("changed_cases") or []:
        lines.append(
            f"- `{item.get('prompt_id')}` `{item.get('case_ref')}`: "
            f"`{item.get('old_classification')}` -> `{item.get('new_classification')}`; {item.get('reason')}"
        )
    if not report.get("changed_cases"):
        lines.append("- none")
    lines.extend(["", "## 5. Remaining Unresolved", ""])
    for item in report.get("remaining_unresolved") or []:
        lines.append(
            f"- `{item.get('prompt_id')}` `{item.get('case_ref')}`: GT `{item.get('gt_subtype')}`, "
            f"predicted `{item.get('predicted_subtype')}`, classification `{item.get('classification')}`."
        )
    lines.extend(
        [
            "",
            "## 6. Notes",
            "",
            "- GT was used only offline.",
            "- Prompts, responses, validator reports, evidence, and GT files were not modified.",
            "- No model was called.",
            "- Metrics remain preliminary until reviewed.",
        ]
    )
    return "\n".join(lines) + "\n"


def build_link_flap_review(analysis: Dict[str, Any]) -> Dict[str, Any]:
    link_cases = [item for item in analysis.get("cases") or [] if normalize_token(item.get("gt_subtype")) == "net_link_flap"]
    case_reviews: List[Dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for item in link_cases:
        predicted = normalize_token(item.get("predicted_subtype_candidate"))
        if item.get("classification") == "alias_correct":
            boundary = "credit_as_link_flap"
            recommendation = "accept_guarded_alias"
        elif predicted in {"net_wifi_disconnect", "net_link_down", "net_wlan_disconnect"}:
            boundary = "link_state_wording_without_flap_semantics"
            recommendation = "adjust_evidence_anomaly_taxonomy"
        elif predicted == "net_dns_fail":
            boundary = "dns_selected_over_link_state_candidate"
            recommendation = "adjust_prompt_wording"
        else:
            boundary = "manual_review_needed"
            recommendation = "manual_review_needed"
        counts[boundary] += 1
        case_reviews.append(
            {
                "prompt_id": item.get("prompt_id"),
                "case_ref": item.get("case_ref"),
                "gt_subtype": item.get("gt_subtype"),
                "predicted_root_cause": item.get("pred_root_cause"),
                "predicted_subtype": item.get("predicted_subtype_candidate"),
                "evidence_used": item.get("evidence_used"),
                "root_candidate_evidence": item.get("root_candidate_evidence"),
                "classification": item.get("classification"),
                "boundary": boundary,
                "recommendation": recommendation,
            }
        )
    return {
        "status": "WARN" if any(item.get("classification") != "alias_correct" for item in link_cases) else "PASS",
        "gt_subtype": "net_link_flap",
        "case_count": len(link_cases),
        "alias_correct_count": sum(1 for item in link_cases if item.get("classification") == "alias_correct"),
        "boundary_counts": dict(counts.most_common()),
        "safe_credit_rule": {
            "credit_only_if": [
                "response contains explicit flap/flapping/intermittent/reconnect/down-up/oscillation semantics",
                "or prompt-visible root-candidate evidence contains explicit flap/recovery/multiple-transition semantics",
            ],
            "do_not_credit_by_default": [
                "link down",
                "interface down",
                "Wi-Fi disconnected",
                "wlan disconnected",
                "disconnect",
            ],
        },
        "cases": case_reviews,
        "recommendation": {
            "next_action": "adjust_taxonomy_and_prompt_rules",
            "reason": "Current prompt-visible evidence often presents link-flap GT as generic disconnect/interface-down evidence, and DNS may appear as a competing root candidate.",
        },
        "notes": [
            "This review does not relabel GT or responses.",
            "Broad disconnect-to-link-flap aliases are intentionally rejected.",
            "Final RCA accuracy should wait for taxonomy/prompt-rule review.",
        ],
    }


def render_link_flap_markdown(report: Dict[str, Any]) -> str:
    lines = [
        "# net_link_flap Taxonomy Boundary Review",
        "",
        "## 1. Status",
        "",
        f"- Status: `{report.get('status')}`",
        f"- Cases reviewed: {report.get('case_count')}",
        f"- Alias-correct under guarded rule: {report.get('alias_correct_count')}",
        "",
        "## 2. Safe Credit Boundary",
        "",
        "Credit `net_link_flap` only when the response or prompt-visible root-candidate evidence contains explicit flap semantics: flap, flapping, intermittent link, repeated disconnect/reconnect, down/up transition, link-state oscillation, recovery after disconnect, multiple state transitions, or unstable link cycles.",
        "",
        "Do not credit generic link down, interface down, Wi-Fi disconnected, wlan disconnected, or disconnect wording as `net_link_flap` by default.",
        "",
        "## 3. Boundary Counts",
        "",
        "| Boundary | Count |",
        "|---|---:|",
    ]
    for key, value in (report.get("boundary_counts") or {}).items():
        lines.append(f"| `{key}` | {value} |")
    lines.extend(["", "## 4. Case Findings", "", "| Case | Prediction | Boundary | Recommendation |", "|---|---|---|---|"])
    for item in report.get("cases") or []:
        lines.append(
            f"| `{item.get('prompt_id')}` / `{item.get('case_ref')}` | `{item.get('predicted_subtype')}` from `{compact_text(item.get('predicted_root_cause'), 60)}` | `{item.get('boundary')}` | `{item.get('recommendation')}` |"
        )
    lines.extend(
        [
            "",
            "## 5. Recommendation",
            "",
            f"- Next action: `{(report.get('recommendation') or {}).get('next_action')}`",
            f"- Reason: {(report.get('recommendation') or {}).get('reason')}",
            "",
            "## 6. Notes",
            "",
            "- No model was called.",
            "- Prompts, responses, validator reports, evidence, and GT files were not modified.",
            "- GT was used only offline to review already-generated responses.",
            "- This is not final paper-level RCA accuracy.",
        ]
    )
    return "\n".join(lines) + "\n"


def render_stdout(analysis: Dict[str, Any], manifest_rows: List[Dict[str, Any]], summary: Dict[str, Any], args: argparse.Namespace) -> str:
    if args.format == "json":
        return safe_json_dump(analysis, pretty=True) + "\n"
    if args.format == "markdown":
        return render_markdown(analysis, args.max_examples)
    if args.format == "jsonl":
        return "\n".join(safe_json_dump(row) for row in manifest_rows) + "\n"
    return (
        "----- MANIFEST SUMMARY -----\n"
        + safe_json_dump(summary, pretty=True)
        + "\n\n----- ERROR ANALYSIS JSON -----\n"
        + safe_json_dump(analysis, pretty=True)
        + "\n\n----- MARKDOWN -----\n"
        + render_markdown(analysis, args.max_examples)
    )


def write_outputs(root: Path, args: argparse.Namespace, analysis: Dict[str, Any], manifest_rows: List[Dict[str, Any]], summary: Dict[str, Any]) -> None:
    refined_report = build_refined_report(root, analysis)
    link_flap_report = build_link_flap_review(analysis)
    output_specs = [
        (args.write_manifest, "\n".join(safe_json_dump(row) for row in manifest_rows) + "\n"),
        (args.write_manifest_summary, safe_json_dump(summary, pretty=True) + "\n"),
        (args.output_json, safe_json_dump(analysis, pretty=True) + "\n"),
        (args.output_md, render_markdown(analysis, args.max_examples)),
        (args.output_refined_json, safe_json_dump(refined_report, pretty=True) + "\n"),
        (args.output_refined_md, render_refined_markdown(refined_report)),
        (args.output_link_flap_json, safe_json_dump(link_flap_report, pretty=True) + "\n"),
        (args.output_link_flap_md, render_link_flap_markdown(link_flap_report)),
    ]
    for value, content in output_specs:
        if not value:
            continue
        path = resolve_path(root, value)
        if path is None:
            raise ValueError(f"invalid output path: {value}")
        validate_output_path(root, path)
        path.write_text(content, encoding="utf-8")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    root = Path(args.root).resolve()
    try:
        analysis, manifest_rows, manifest_summary = build_analysis(args)
        if args.stdout_only or not any(
            (
                args.write_manifest,
                args.write_manifest_summary,
                args.output_json,
                args.output_md,
                args.output_refined_json,
                args.output_refined_md,
                args.output_link_flap_json,
                args.output_link_flap_md,
            )
        ):
            sys.stdout.write(render_stdout(analysis, manifest_rows, manifest_summary, args))
        else:
            write_outputs(root, args, analysis, manifest_rows, manifest_summary)
    except Exception as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
