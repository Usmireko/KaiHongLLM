#!/usr/bin/env python3
"""Read-only adapter from legacy L1 evidence rows to Evidence Block objects.

The adapter scans current evidence_candidates.jsonl files, normalizes each row
into a deterministic Evidence Block preview, and reports summary statistics. It
does not import project modules and does not modify existing evidence files by
default.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple


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

ALLOWED_PHASES = {"baseline", "fault", "recovery", "unknown"}
ALLOWED_CAUSAL_ROLES = {
    "root_candidate",
    "supporting",
    "propagation",
    "symptom",
    "contradicting",
    "noise_candidate",
    "excluded_provenance",
    "unknown",
}

INJECTOR_TERMS = (
    "fault_inject",
    "inject",
    "injector",
    "scenario_tag",
    "fault_net_",
    "fault_mem_",
    "fault_cpu_",
)

FREE_TEXT_INJECTOR_TERMS = (
    "fault_inject",
    "inject",
    "injector",
    "scenario_tag",
)

PROVENANCE_TERMS = (
    "provenance",
    "debug_provenance",
    "collection_marker",
    "scenario_marker",
)

SUBTYPE_LIKE_TERMS = (
    "net_dns_fail",
    "net_link_down",
    "net_route_missing",
    "net_no_default_route",
    "net_no_ipv4_on_iface",
    "net_public_ip_unreachable",
    "net_wifi_auth_fail_wrong_psk",
    "net_wifi_disconnect",
    "net_wrong_default_route",
)

GT_LIKE_TERMS = (
    "ground_truth",
    "root_cause",
    "gt_",
    "primary_subtype",
    "scenario_tag",
    "fault_type",
    "label",
)

GT_LABEL_KEY_TERMS = (
    "ground_truth",
    "root_cause",
    "gt",
    "label",
    "metadata",
    "meta",
    "primary_subtype",
    "scenario_tag",
    "fault_type",
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
    "dataset_batches",
    "demo_public_dataset",
    "frozen",
    "accepted",
    "ledger",
    "eval",
    "inbox",
    "inbox_net",
}

RECOVERY_RE = re.compile(r"\b(recovery|recovered|after\s+the\s+fault|post|restored)\b", re.IGNORECASE)
FAULT_RE = re.compile(r"\b(fault|during\s+fault|fault\s+snapshot|failure|abnormal)\b", re.IGNORECASE)
BASELINE_RE = re.compile(r"\b(baseline|before|pre)\b", re.IGNORECASE)
TOO_GENERIC_RE = re.compile(r"^\s*(ok|none|normal|n/a|na|unknown)?\s*$", re.IGNORECASE)
NET_SUBTYPE_TOKEN_RE = re.compile(r"\bnet_[a-z0-9_]+\b", re.IGNORECASE)
INTERFACE_RE = re.compile(r"\b(?:eth\d+|wlan\d+|wifi\d+|en[a-z0-9]+)\b", re.IGNORECASE)
LABEL_ONLY_RE = re.compile(
    r"^\s*(?:ground_truth|gt|gt_family|gt_subtype|root_cause|fault_type|primary_subtype|label|scenario_tag)?"
    r"[:=\s-]*(?:fault_net_[a-z0-9_]+|net_[a-z0-9_]+)\s*$",
    re.IGNORECASE,
)
CANONICAL_CASE_CACHE: Dict[Path, Optional[str]] = {}


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Adapt legacy L1 evidence rows into normalized Evidence Blocks.")
    parser.add_argument("--root", default=".", help="Repository root. Default: current directory.")
    parser.add_argument("--input", default=None, help="Optional evidence_candidates.jsonl file or directory to adapt.")
    parser.add_argument("--stdout-only", action="store_true", help="Force stdout output only.")
    parser.add_argument("--dry-run", action="store_true", help="Explicit safety flag; the tool is read-only by default.")
    parser.add_argument("--format", choices=("json", "markdown", "summary", "both", "jsonl"), default="both")
    parser.add_argument("--max-examples", type=int, default=10)
    parser.add_argument("--limit", type=int, default=None, help="Optional maximum evidence rows to process.")
    parser.add_argument(
        "--include-excluded",
        action="store_true",
        help="Include excluded/provenance/noise blocks in JSONL output.",
    )
    parser.add_argument("--output", default=None, help="Optional explicit output path.")
    return parser.parse_args(argv)


def path_has_ignored_part(path: Path) -> bool:
    return any(part in IGNORED_DIRS for part in path.parts)


def rel_path(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def read_text_lossy(path: Path) -> str:
    last_error: Optional[Exception] = None
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError as exc:
            last_error = exc
    if last_error:
        raise last_error
    return path.read_text(encoding="utf-8")


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
        if not path.is_file():
            continue
        if path.suffix.lower() in BINARY_SUFFIXES:
            continue
        yield path


def read_jsonl(path: Path, root: Path, max_examples: int) -> Tuple[List[Tuple[int, Dict[str, Any]]], List[Dict[str, Any]]]:
    rows: List[Tuple[int, Dict[str, Any]]] = []
    errors: List[Dict[str, Any]] = []
    try:
        text = read_text_lossy(path)
    except OSError as exc:
        return rows, [{"file": rel_path(path, root), "line": None, "error": str(exc), "snippet": ""}]
    except UnicodeDecodeError as exc:
        return rows, [{"file": rel_path(path, root), "line": None, "error": str(exc), "snippet": ""}]

    for line_no, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            if len(errors) < max_examples:
                errors.append(
                    {
                        "file": rel_path(path, root),
                        "line": line_no,
                        "error": str(exc),
                        "snippet": line[:240],
                    }
                )
            continue
        if isinstance(value, dict):
            rows.append((line_no, value))
        elif len(errors) < max_examples:
            errors.append(
                {
                    "file": rel_path(path, root),
                    "line": line_no,
                    "error": "JSONL row is not an object",
                    "snippet": line[:240],
                }
            )
    return rows, errors


def safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list, tuple)):
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except TypeError:
            return str(value)
    return str(value)


def flatten_values(obj: Any) -> Iterator[Tuple[str, str]]:
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield str(key), safe_text(key)
            yield from flatten_values(value)
    elif isinstance(obj, list):
        for value in obj:
            yield "", safe_text(value)
            yield from flatten_values(value)
    else:
        yield "", safe_text(obj)


def all_row_text(row: Dict[str, Any]) -> str:
    return " ".join(value for _, value in flatten_values(row)).lower()


def contains_any(text: str, terms: Sequence[str]) -> bool:
    text_l = text.lower()
    return any(term.lower() in text_l for term in terms)


def get_haystack(row: Dict[str, Any]) -> str:
    return " ".join(
        safe_text(row.get(key))
        for key in ("source", "source_rel", "kind", "text", "target", "support_role")
    ).lower()


def is_legacy_primary(row: Dict[str, Any]) -> bool:
    support_role = safe_text(row.get("support_role")).lower()
    target = safe_text(row.get("target")).lower()
    return support_role == "primary" or target == "primary"


def is_injector_marker(row: Dict[str, Any]) -> bool:
    hay = get_haystack(row)
    marker_hay = " ".join(
        safe_text(row.get(key))
        for key in ("source", "source_rel", "kind", "target", "support_role")
    ).lower()
    text = safe_text(row.get("text")).lower()
    source = safe_text(row.get("source")).lower()
    kind = safe_text(row.get("kind")).lower()
    support_role = safe_text(row.get("support_role")).lower()
    target = safe_text(row.get("target")).lower()
    return (
        "fault_inject" in source
        or "inject" in source
        or "injector" in source
        or source in PROVENANCE_TERMS
        or "inject" in kind
        or "injector" in kind
        or kind in PROVENANCE_TERMS
        or support_role in PROVENANCE_TERMS
        or target in PROVENANCE_TERMS
        or "marker" in kind and "inject" in hay
        or "provenance" in kind
        or "provenance" in source
        or contains_any(marker_hay, INJECTOR_TERMS)
        or contains_any(marker_hay, PROVENANCE_TERMS)
        or contains_any(text, FREE_TEXT_INJECTOR_TERMS)
    )


def is_generic_noise(row: Dict[str, Any]) -> bool:
    hay = get_haystack(row)
    text = safe_text(row.get("text")).strip()
    role = safe_text(row.get("support_role")).lower()
    target = safe_text(row.get("target")).lower()
    return (
        "duplicate_probe" in hay
        or "historical_noise" in hay
        or "not primary training evidence" in hay
        or "do not use" in hay
        or bool(text) and bool(TOO_GENERIC_RE.match(text))
        or role == "noise"
        or target == "noise"
    )


def is_recovery_like(row: Dict[str, Any]) -> bool:
    return bool(RECOVERY_RE.search(get_haystack(row)))


def has_gt_like_text(row: Dict[str, Any]) -> bool:
    for key, value in flatten_values(row):
        key_l = key.lower()
        value_l = value.lower()
        if any(term in key_l or term in value_l for term in GT_LIKE_TERMS):
            return True
        if any(term in value_l for term in SUBTYPE_LIKE_TERMS):
            return True
        if NET_SUBTYPE_TOKEN_RE.search(value_l):
            return True
    return False


def is_gt_label_marker(row: Dict[str, Any]) -> bool:
    text = safe_text(row.get("text")).strip().lower()
    if text and LABEL_ONLY_RE.match(text):
        return True
    if not has_gt_like_text(row):
        return False
    source = safe_text(row.get("source")).strip().lower()
    kind = safe_text(row.get("kind")).strip().lower()
    target = safe_text(row.get("target")).strip().lower()
    support_role = safe_text(row.get("support_role")).strip().lower()
    key_fields = " ".join((source, kind, target, support_role))
    if any(term in key_fields for term in GT_LABEL_KEY_TERMS):
        return True
    return False


def read_canonical_case_id(path: Path) -> Optional[str]:
    resolved = path.resolve()
    if resolved in CANONICAL_CASE_CACHE:
        return CANONICAL_CASE_CACHE[resolved]
    try:
        data = json.loads(read_text_lossy(resolved))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        CANONICAL_CASE_CACHE[resolved] = None
        return None
    case_id = data.get("case_id") or data.get("source_case_id")
    if not case_id and isinstance(data.get("source"), dict):
        case_id = data["source"].get("run_id")
    result = safe_text(case_id) if case_id not in (None, "") else None
    CANONICAL_CASE_CACHE[resolved] = result
    return result


def canonical_case_candidates(path: Path, root: Path) -> List[Path]:
    candidates: List[Path] = []
    for parent in (path.parent, path.parent.parent):
        candidate = parent / "canonical_case.json"
        if candidate not in candidates:
            candidates.append(candidate)
    for parent in path.parents:
        if parent == root.parent:
            break
        if parent.name == "runs":
            break
        candidate = parent / "canonical_case.json"
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates[:5]


def infer_case_id(path: Path, row: Dict[str, Any], root: Path) -> Tuple[str, str, Optional[str]]:
    explicit = row.get("case_id")
    if explicit not in (None, ""):
        case_rel = rel_path(path.parent, root)
        return safe_text(explicit), case_rel, None
    source_case_id = row.get("source_case_id")
    if source_case_id not in (None, ""):
        case_rel = rel_path(path.parent, root)
        return safe_text(source_case_id), case_rel, "case_id inferred from row source_case_id"
    explicit_case = row.get("case")
    if explicit_case not in (None, ""):
        case_rel = rel_path(path.parent, root)
        return safe_text(explicit_case), case_rel, None

    for candidate in canonical_case_candidates(path, root):
        if candidate.is_file():
            canonical_id = read_canonical_case_id(candidate)
            if canonical_id:
                case_rel = rel_path(path.parent, root)
                return canonical_id, case_rel, "case_id inferred from canonical_case.json"

    case_rel = rel_path(path.parent, root)
    parts = list(path.parts)
    if "runs" in parts:
        index = parts.index("runs")
        if index + 1 < len(parts):
            return parts[index + 1], case_rel, "case_id inferred from runs path"
    if path.parent.name == "dataset_export" and path.parent.parent.name:
        return path.parent.parent.name, case_rel, "case_id inferred from dataset_export parent path"
    case_id = case_rel.replace("\\", "/")
    return case_id, case_rel, "case_id inferred from evidence file parent path"


def infer_source(row: Dict[str, Any]) -> str:
    hay = get_haystack(row)
    legacy_source = safe_text(row.get("source")).lower()
    if is_injector_marker(row):
        return "fault_inject"
    if any(term in hay for term in ("dns", "resolv", "nameserver")):
        return "dns"
    if any(term in hay for term in ("route", "gateway", "ip route")):
        return "route"
    if any(term in hay for term in ("wlan", "wifi", "wpa", "ssid", "link", "deauth", "association")):
        return "wifi"
    if any(term in hay for term in ("ping", "icmp", "reachable", "unreachable", "packet loss")):
        return "state"
    if any(term in hay for term in ("process", "pid", "service", "daemon", "procs")):
        return "process"
    if legacy_source == "metrics":
        return "metric"
    if any(term in legacy_source for term in ("snapshot", "probe")):
        return "state"
    if any(term in legacy_source for term in ("faultlog", "log", "events")):
        return "log"
    return "other"


def infer_phase(row: Dict[str, Any]) -> str:
    explicit = safe_text(row.get("phase")).strip().lower()
    if explicit in ALLOWED_PHASES:
        return explicit
    hay = get_haystack(row)
    if RECOVERY_RE.search(hay):
        return "recovery"
    if FAULT_RE.search(hay):
        return "fault"
    if BASELINE_RE.search(hay):
        return "baseline"
    return "unknown"


def infer_object(row: Dict[str, Any]) -> str:
    hay = get_haystack(row)
    if "wlan0" in hay:
        return "wlan0"
    if any(term in hay for term in ("dns", "nameserver", "resolv")):
        return "dns"
    if any(term in hay for term in ("route", "gateway")):
        return "route"
    if any(term in hay for term in ("ping", "icmp", "reachable", "unreachable", "packet loss")):
        return "ping"
    if any(term in hay for term in ("wpa", "ssid", "wifi", "wlan")):
        return "wifi"
    if any(term in hay for term in ("pid", "process", "daemon", "service", "procs")):
        return "process"
    if any(term in hay for term in ("iface", "interface", "eth0", "network", "net_")):
        return "network"
    if any(term in hay for term in ("device", "board")):
        return "device"
    return "unknown"


def infer_anomaly_type(row: Dict[str, Any]) -> str:
    hay = get_haystack(row)
    hay_norm = hay.replace("_", " ").replace("-", " ")
    if is_injector_marker(row):
        return "injector_marker"
    if is_generic_noise(row):
        return "noise"
    if any(term in hay for term in ("net_wifi_auth_fail",)) or any(
        term in hay_norm for term in ("auth fail", "authentication failed", "password failed", "wrong psk", "wrong password", "bad psk")
    ):
        return "auth_failure"
    if any(term in hay_norm for term in ("dns fail", "dns resolution failed", "resolve fail", "nameserver missing", "no such host")):
        return "dns_failure"
    if any(
        term in hay_norm
        for term in (
            "wrong default route",
            "wrong gateway",
            "incorrect default gateway",
            "route points to unreachable gateway",
            "points to unreachable gateway",
            "traffic black holed",
            "traffic black-holed",
            "black holed",
            "black-holed",
        )
    ):
        return "route_black_hole"
    if any(term in hay_norm for term in ("route missing", "default route missing", "no default route", "gateway missing")):
        return "route_missing"
    if any(term in hay for term in ("net_no_ipv4_on_iface",)) or any(
        term in hay_norm for term in ("no inet addr", "inet addr missing", "ip cleared", "ipv4 missing", "no ipv4")
    ):
        return "ip_config_missing"
    if any(term in hay_norm for term in ("timeout", "timed out")):
        return "timeout"
    if any(term in hay_norm for term in ("latency spike", "latency high", "high latency", "delay spike", "rtt spike")):
        return "latency_spike"
    if any(term in hay_norm for term in ("link flap", "flapping", "link unstable", "repeated disconnect")):
        return "link_flap"
    if any(term in hay_norm for term in ("wlan down", "link down", "transitioned down", "disconnect", "disconnected", "deauth", "association failed")):
        return "disconnect"
    if any(term in hay_norm for term in ("ping failed", "unreachable", "packet loss", "no response", "target ip unreachable")):
        return "reachability_loss"
    if any(term in hay_norm for term in ("crash", "died", "killed", "process exit", "service stopped")):
        return "process_crash"
    return "unknown"


def infer_normalized_entity(anomaly_type: str) -> str:
    return {
        "dns_failure": "DNSFailure",
        "route_missing": "RouteMissing",
        "route_black_hole": "RouteBlackHole",
        "disconnect": "WiFiLinkDown",
        "timeout": "NetworkTimeout",
        "latency_spike": "NetworkLatencySpike",
        "link_flap": "NetworkLinkFlap",
        "reachability_loss": "ReachabilityLoss",
        "process_crash": "ProcessFailure",
        "auth_failure": "AuthFailure",
        "ip_config_missing": "IPConfigMissing",
        "injector_marker": "InjectorMarker",
        "noise": "GenericNoise",
        "unknown": "Unknown",
    }.get(anomaly_type, "Unknown")


def infer_transition_subject(row: Dict[str, Any]) -> Optional[str]:
    match = INTERFACE_RE.search(get_haystack(row))
    if match:
        return match.group(0).lower()
    if infer_object(row) in {"wifi", "wlan0", "network"}:
        return infer_object(row)
    return None


def infer_transition_metadata(row: Dict[str, Any], anomaly_type: str) -> Dict[str, Any]:
    hay = get_haystack(row).replace("_", " ").replace("-", " ")
    subject = infer_transition_subject(row)
    has_down = bool(
        re.search(r"\b(transitioned\s+down|interface\s+down|link\s+down|carrier\s+lost|disconnected)\b", hay)
    )
    has_up_or_recovery = bool(
        re.search(r"\b(recovered|restored|reconnect(?:ed)?|carrier\s+restored|up\s+after|after\s+the\s+fault)\b", hay)
    )
    repeated = bool(re.search(r"\b(repeated|multiple)\b.*\b(disconnect|reconnect|transition|down|up)\b", hay))
    intermittent = bool(re.search(r"\b(intermittent|unstable\s+link)\b", hay))
    oscillation = bool(re.search(r"\b(oscillat|flapping)\b", hay))
    explicit_strong_flap = bool(
        re.search(r"\bflapping\b", hay)
        or re.search(r"\blink\s+flap\b", hay)
        and not re.search(r"\blink\s+flap\s+observed\s+through\s+fault\s+and\s+recovery\s+snapshots\b", hay)
    )

    transition_pattern = "unknown"
    if oscillation:
        transition_pattern = "oscillation"
    elif intermittent:
        transition_pattern = "intermittent"
    elif repeated:
        transition_pattern = "repeated_down_up"
    elif has_down and has_up_or_recovery:
        transition_pattern = "single_down_up"
    elif has_down:
        transition_pattern = "single_down"

    indicators: List[str] = []
    if bool(re.search(r"\breconnect", hay)):
        indicators.append("reconnect")
    if bool(re.search(r"\bcarrier\s+restored\b", hay)):
        indicators.append("carrier_restored")
    if anomaly_type == "link_flap" and bool(re.search(r"\bwpa\b.*\b(cycle|cycling)", hay)):
        indicators.append("wpa_state_cycle")
    if repeated:
        indicators.append("repeated_transition")
    if has_up_or_recovery and (has_down or explicit_strong_flap):
        indicators.append("recovery_after_disconnect")
    if oscillation:
        indicators.append("link_oscillation")

    state_sequence: List[str] = []
    if has_down:
        state_sequence.append(f"{subject or 'interface'}:down")
    if has_up_or_recovery and (has_down or explicit_strong_flap):
        state_sequence.append(f"{subject or 'interface'}:up_after_fault")
    if explicit_strong_flap and not state_sequence:
        state_sequence.append(f"{subject or 'link'}:flap_observed")

    recovery_observed = bool(subject and has_down and has_up_or_recovery)
    strong_indicators = {"repeated_transition", "link_oscillation", "wpa_state_cycle"}
    strong_flap_evidence = bool(
        transition_pattern in {"repeated_down_up", "intermittent", "oscillation"}
        or strong_indicators.intersection(indicators)
        or explicit_strong_flap
    )
    if transition_pattern == "single_down_up":
        transition_count: Optional[int] = 1
    elif transition_pattern in {"repeated_down_up", "intermittent", "oscillation"}:
        transition_count = 2 if repeated else None
    else:
        transition_count = None
    return {
        "transition_pattern": transition_pattern,
        "flap_indicators": sorted(set(indicators)),
        "state_sequence": state_sequence,
        "transition_count": transition_count,
        "recovery_observed": recovery_observed,
        "strong_flap_evidence": strong_flap_evidence,
    }


def map_causal_role(row: Dict[str, Any], phase: str, anomaly_type: str, risk_flags: List[str]) -> Tuple[str, bool, Optional[str]]:
    legacy_support = safe_text(row.get("support_role")).lower()
    legacy_target = safe_text(row.get("target")).lower()
    legacy_primary = legacy_support == "primary" or legacy_target == "primary"

    if "injector_marker" in risk_flags:
        return "excluded_provenance", False, "injector/provenance marker is not diagnostic evidence"
    if "gt_label_marker" in risk_flags:
        return "excluded_provenance", False, "GT/label-like marker is not diagnostic evidence"
    if "generic_noise" in risk_flags:
        return "noise_candidate", False, "generic/noise evidence is excluded by default"
    if "empty_text" in risk_flags:
        return "unknown", False, "empty evidence text is not diagnostic evidence"
    if "recovery_primary" in risk_flags:
        return "symptom", True, None
    if "remained reachable" in get_haystack(row):
        return "supporting", True, None
    if legacy_primary and phase in ("fault", "unknown"):
        return "root_candidate", True, None
    if legacy_support == "symptom":
        return "symptom", True, None
    if legacy_target == "symptom":
        return "symptom", True, None
    if any(term in legacy_support for term in ("support", "secondary")) or any(term in legacy_target for term in ("support", "secondary")):
        return "supporting", True, None
    if anomaly_type in ("reachability_loss", "disconnect", "dns_failure", "route_missing", "route_black_hole", "auth_failure"):
        return "supporting", True, None
    return "unknown", True, None


def numeric_score(value: Any) -> Optional[float]:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if 0.0 <= score <= 1.0:
        return score
    return None


def clamp_score(value: float) -> float:
    return round(max(0.0, min(1.0, value)), 4)


def compute_utility_score(row: Dict[str, Any], block: Dict[str, Any]) -> float:
    if (
        "injector_marker" in block["risk_flags"]
        or "gt_label_marker" in block["risk_flags"]
        or "generic_noise" in block["risk_flags"]
    ):
        return 0.0

    base = numeric_score(row.get("score"))
    score = base if base is not None else 0.5
    text_len = len(block["raw_observation"])

    if block["phase"] == "fault":
        score += 0.15
    if block["object"] != "unknown":
        score += 0.10
    if block["anomaly_type"] != "unknown":
        score += 0.10
    if block["causal_role"] == "root_candidate":
        score += 0.10
    if 20 <= text_len <= 500:
        score += 0.05
    if block["source"] in {"dns", "route", "wifi", "process", "state", "network"}:
        score += 0.05

    if "recovery_primary" in block["risk_flags"]:
        score -= 0.40
    if block["phase"] == "recovery":
        score -= 0.20
    if "empty_text" in block["risk_flags"] or text_len < 8 or TOO_GENERIC_RE.match(block["raw_observation"]):
        score -= 0.20
    if block["object"] == "unknown":
        score -= 0.10
    if block["anomaly_type"] == "unknown":
        score -= 0.10

    if "recovery_primary" in block["risk_flags"]:
        score = min(score, 0.30)

    return clamp_score(score)


def normalize_context(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if value == "":
        return []
    return [value]


def initial_risk_flags(row: Dict[str, Any], phase: str, obj: str, anomaly_type: str) -> List[str]:
    flags: List[str] = []
    text = safe_text(row.get("text")).strip()
    if is_injector_marker(row):
        flags.append("injector_marker")
    if is_legacy_primary(row):
        flags.append("legacy_primary")
    if (is_recovery_like(row) or phase == "recovery") and is_legacy_primary(row):
        flags.append("recovery_primary")
    if is_generic_noise(row):
        flags.append("generic_noise")
    if has_gt_like_text(row):
        flags.append("gt_like_text")
    if is_gt_label_marker(row):
        flags.append("gt_label_marker")
    if not text:
        flags.append("empty_text")
    if obj == "unknown":
        flags.append("missing_object")
    if phase == "unknown":
        flags.append("missing_phase")
    legacy_support = safe_text(row.get("support_role")).lower()
    legacy_target = safe_text(row.get("target")).lower()
    if legacy_support == "symptom" or legacy_target == "symptom":
        flags.append("symptom_only")
    return sorted(set(flags))


def adapt_row(row: Dict[str, Any], path: Path, line_no: int, root: Path, case_seq: Dict[str, int]) -> Dict[str, Any]:
    case_id, case_rel, case_note = infer_case_id(path, row, root)
    case_seq[case_id] += 1
    evidence_id = f"E{case_seq[case_id]:03d}"

    phase = infer_phase(row)
    obj = infer_object(row)
    anomaly_type = infer_anomaly_type(row)
    normalized_entity = infer_normalized_entity(anomaly_type)
    transition_metadata = infer_transition_metadata(row, anomaly_type)
    risk_flags = initial_risk_flags(row, phase, obj, anomaly_type)
    causal_role, usable, exclusion_reason = map_causal_role(row, phase, anomaly_type, risk_flags)

    notes: List[str] = []
    if case_note:
        notes.append(case_note)
    if "recovery_primary" in risk_flags:
        notes.append("legacy primary recovery-looking row demoted from root_candidate")
    if "injector_marker" in risk_flags:
        notes.append("injector/provenance row excluded from diagnosis")
    if "gt_label_marker" in risk_flags:
        notes.append("GT/label-like row excluded from diagnosis")
    if "generic_noise" in risk_flags:
        notes.append("generic/noise row excluded from diagnosis")

    block: Dict[str, Any] = {
        "evidence_id": evidence_id,
        "legacy_eid": safe_text(row.get("eid")) or None,
        "case_id": case_id,
        "case_rel": case_rel,
        "source": infer_source(row),
        "legacy_source": safe_text(row.get("source")) or None,
        "legacy_source_rel": safe_text(row.get("source_rel")) or None,
        "legacy_kind": safe_text(row.get("kind")) or None,
        "phase": phase,
        "timestamp": row.get("ts"),
        "order": case_seq[case_id] - 1,
        "object": obj,
        "raw_observation": safe_text(row.get("text")),
        "normalized_entity": normalized_entity,
        "anomaly_type": anomaly_type,
        "causal_role": causal_role,
        "legacy_support_role": safe_text(row.get("support_role")) or None,
        "legacy_target": safe_text(row.get("target")) or None,
        "legacy_score": row.get("score"),
        "legacy": row,
        "confidence": 0.0,
        "utility_score": 0.0,
        "usable_for_diagnosis": usable,
        "exclusion_reason": exclusion_reason,
        "risk_flags": risk_flags,
        "transition_pattern": transition_metadata["transition_pattern"],
        "flap_indicators": transition_metadata["flap_indicators"],
        "state_sequence": transition_metadata["state_sequence"],
        "transition_count": transition_metadata["transition_count"],
        "recovery_observed": transition_metadata["recovery_observed"],
        "strong_flap_evidence": transition_metadata["strong_flap_evidence"],
        "raw_refs": [
            {
                "file": rel_path(path, root),
                "line_start": line_no,
                "line_end": line_no,
                "source_rel": row.get("source_rel"),
                "span": row.get("span"),
            }
        ],
        "context_before": normalize_context(row.get("context_before")),
        "context_after": normalize_context(row.get("context_after")),
        "notes": "; ".join(notes),
    }
    block["utility_score"] = compute_utility_score(row, block)
    if block["utility_score"] < 0.25 and "low_utility" not in block["risk_flags"]:
        block["risk_flags"] = sorted(set(block["risk_flags"] + ["low_utility"]))
    if not block["usable_for_diagnosis"]:
        block["confidence"] = 0.0
    else:
        block["confidence"] = block["utility_score"]
    if block["causal_role"] not in ALLOWED_CAUSAL_ROLES:
        block["causal_role"] = "unknown"
    return block


def percentile(values: Sequence[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 4)
    rank = (len(ordered) - 1) * p
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - lower
    value = ordered[lower] * (1 - fraction) + ordered[upper] * fraction
    return round(value, 4)


def compact_block(block: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "file": block["raw_refs"][0]["file"],
        "line": block["raw_refs"][0]["line_start"],
        "evidence_id": block["evidence_id"],
        "legacy_eid": block["legacy_eid"],
        "source": block["source"],
        "phase": block["phase"],
        "object": block["object"],
        "anomaly_type": block["anomaly_type"],
        "causal_role": block["causal_role"],
        "utility_score": block["utility_score"],
        "usable_for_diagnosis": block["usable_for_diagnosis"],
        "risk_flags": block["risk_flags"],
        "text": block["raw_observation"][:180],
    }


def add_example(examples: List[Dict[str, Any]], block: Dict[str, Any], max_examples: int) -> None:
    if len(examples) < max_examples:
        examples.append(compact_block(block))


def build_summary(
    root: Path,
    dry_run: bool,
    files: List[Path],
    blocks: List[Dict[str, Any]],
    parse_errors: List[Dict[str, Any]],
    max_examples: int,
) -> Dict[str, Any]:
    distributions = {
        "source": Counter(),
        "phase": Counter(),
        "object": Counter(),
        "anomaly_type": Counter(),
        "causal_role": Counter(),
        "risk_flags": Counter(),
    }
    risk_summary = Counter()
    examples: Dict[str, List[Dict[str, Any]]] = {
        "usable_root_candidates": [],
        "excluded_injector_markers": [],
        "recovery_primary_demoted": [],
        "generic_noise_excluded": [],
        "unknown_low_utility": [],
        "parse_errors": parse_errors[:max_examples],
    }
    utility_values: List[float] = []
    context_before = 0
    context_after = 0
    without_context = 0
    usable_injector = 0

    for block in blocks:
        distributions["source"][block["source"]] += 1
        distributions["phase"][block["phase"]] += 1
        distributions["object"][block["object"]] += 1
        distributions["anomaly_type"][block["anomaly_type"]] += 1
        distributions["causal_role"][block["causal_role"]] += 1
        utility_values.append(block["utility_score"])
        for flag in block["risk_flags"]:
            distributions["risk_flags"][flag] += 1

        flags = set(block["risk_flags"])
        if "injector_marker" in flags:
            risk_summary["injector_markers"] += 1
            if block["usable_for_diagnosis"]:
                usable_injector += 1
            if "legacy_primary" in flags:
                risk_summary["injector_markers_legacy_primary"] += 1
            add_example(examples["excluded_injector_markers"], block, max_examples)
        if "recovery_primary" in flags:
            risk_summary["recovery_primary"] += 1
            add_example(examples["recovery_primary_demoted"], block, max_examples)
        if "generic_noise" in flags:
            risk_summary["generic_noise"] += 1
            add_example(examples["generic_noise_excluded"], block, max_examples)
        if "gt_like_text" in flags:
            risk_summary["gt_like_text"] += 1
        if "empty_text" in flags:
            risk_summary["empty_text"] += 1
        if block["usable_for_diagnosis"] and block["causal_role"] == "root_candidate":
            add_example(examples["usable_root_candidates"], block, max_examples)
        if block["causal_role"] == "unknown" or "low_utility" in flags:
            add_example(examples["unknown_low_utility"], block, max_examples)

        if block["context_before"]:
            context_before += 1
        if block["context_after"]:
            context_after += 1
        if not block["context_before"] and not block["context_after"]:
            without_context += 1

    excluded_blocks = sum(1 for block in blocks if not block["usable_for_diagnosis"])
    summary = {
        "root": str(root.resolve()),
        "dry_run": dry_run,
        "counts": {
            "files": len(files),
            "rows": len(blocks) + len(parse_errors),
            "blocks": len(blocks),
            "usable_blocks": len(blocks) - excluded_blocks,
            "excluded_blocks": excluded_blocks,
            "parse_errors": len(parse_errors),
        },
        "distributions": {key: dict(counter.most_common()) for key, counter in distributions.items()},
        "risk_summary": {
            "injector_markers": risk_summary["injector_markers"],
            "injector_markers_legacy_primary": risk_summary["injector_markers_legacy_primary"],
            "usable_injector_markers": usable_injector,
            "recovery_primary": risk_summary["recovery_primary"],
            "generic_noise": risk_summary["generic_noise"],
            "gt_like_text": risk_summary["gt_like_text"],
            "empty_text": risk_summary["empty_text"],
        },
        "utility_score": {
            "min": round(min(utility_values), 4) if utility_values else 0,
            "p50": percentile(utility_values, 0.50),
            "p90": percentile(utility_values, 0.90),
            "max": round(max(utility_values), 4) if utility_values else 0,
            "avg": round(sum(utility_values) / len(utility_values), 4) if utility_values else 0,
        },
        "context_coverage": {
            "with_context_before": context_before,
            "with_context_after": context_after,
            "without_context": without_context,
        },
        "examples": examples,
        "notes": [
            "This adapter is read-only by default and does not modify existing evidence files.",
            "Injector/provenance markers are excluded from diagnostic evidence by design.",
        ],
    }
    return summary


def markdown_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(item).replace("\n", " ") for item in row) + " |")
    return "\n".join(lines)


def top_rows(mapping: Dict[str, int], limit: int) -> List[List[Any]]:
    return [[key, value] for key, value in list(mapping.items())[:limit]]


def print_json_summary(summary: Dict[str, Any]) -> str:
    return json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True)


def print_markdown_summary(summary: Dict[str, Any], max_examples: int) -> str:
    lines: List[str] = []
    counts = summary["counts"]
    risk = summary["risk_summary"]
    distributions = summary["distributions"]
    lines.append("# Evidence Block Schema Adapter Report")
    lines.append("")
    lines.append("This adapter is read-only by default and does not modify existing evidence files.")
    lines.append("")
    lines.append("Injector/provenance markers are excluded from diagnostic evidence by design.")
    lines.append("")
    lines.append("## Counts")
    lines.append(
        markdown_table(
            ["metric", "value"],
            [
                ["root", summary["root"]],
                ["dry_run", summary["dry_run"]],
                ["files", counts["files"]],
                ["rows", counts["rows"]],
                ["blocks", counts["blocks"]],
                ["usable_blocks", counts["usable_blocks"]],
                ["excluded_blocks", counts["excluded_blocks"]],
                ["parse_errors", counts["parse_errors"]],
            ],
        )
    )
    lines.append("")
    lines.append("## Risk Summary")
    lines.append(
        markdown_table(
            ["risk", "count"],
            [
                ["injector_markers", risk["injector_markers"]],
                ["injector_markers_legacy_primary", risk["injector_markers_legacy_primary"]],
                ["usable_injector_markers", risk["usable_injector_markers"]],
                ["recovery_primary_demoted", risk["recovery_primary"]],
                ["generic_noise_excluded", risk["generic_noise"]],
                ["gt_like_text", risk["gt_like_text"]],
                ["empty_text", risk["empty_text"]],
            ],
        )
    )
    lines.append("")
    lines.append("## Utility And Context")
    lines.append(
        markdown_table(
            ["metric", "value"],
            [
                ["utility_score", summary["utility_score"]],
                ["context_coverage", summary["context_coverage"]],
            ],
        )
    )
    lines.append("")
    lines.append("## Distributions")
    for name in ("source", "phase", "object", "anomaly_type", "causal_role", "risk_flags"):
        lines.append(f"### {name}")
        rows = top_rows(distributions.get(name, {}), max_examples)
        lines.append(markdown_table([name, "count"], rows or [["<none>", 0]]))
        lines.append("")
    lines.append("## Examples")
    for title, key in (
        ("Usable root candidates", "usable_root_candidates"),
        ("Excluded injector markers", "excluded_injector_markers"),
        ("Recovery primary demoted", "recovery_primary_demoted"),
        ("Generic noise excluded", "generic_noise_excluded"),
        ("Unknown or low utility", "unknown_low_utility"),
        ("Parse errors", "parse_errors"),
    ):
        lines.append(f"### {title}")
        examples = summary["examples"].get(key) or []
        if not examples:
            lines.append("- none")
        else:
            for item in examples[:max_examples]:
                lines.append(f"- `{item}`")
        lines.append("")
    return "\n".join(lines)


def print_jsonl_blocks(blocks: Sequence[Dict[str, Any]], include_excluded: bool) -> str:
    selected = [block for block in blocks if include_excluded or block["usable_for_diagnosis"]]
    return "\n".join(json.dumps(block, ensure_ascii=False, sort_keys=True) for block in selected)


def render_output(summary: Dict[str, Any], blocks: Sequence[Dict[str, Any]], args: argparse.Namespace) -> str:
    if args.format == "json":
        return print_json_summary(summary)
    if args.format in ("markdown", "summary"):
        return print_markdown_summary(summary, args.max_examples)
    if args.format == "jsonl":
        return print_jsonl_blocks(blocks, args.include_excluded)
    return "\n".join(
        [
            "----- JSON -----",
            print_json_summary(summary),
            "",
            "----- MARKDOWN -----",
            print_markdown_summary(summary, args.max_examples),
        ]
    )


def validate_output_path(output_path: Path, root: Path) -> None:
    resolved = output_path.resolve()
    root_resolved = root.resolve()
    name = resolved.name.lower()

    if resolved.exists():
        raise ValueError(f"refusing to overwrite existing output path: {resolved}")
    if not resolved.parent.exists():
        raise ValueError(f"refusing output path with missing parent directory: {resolved}")
    if name in PROTECTED_OUTPUT_FILENAMES or "ledger" in name:
        raise ValueError(f"refusing protected output filename: {resolved}")

    try:
        rel_parts = [part.lower() for part in resolved.relative_to(root_resolved).parts]
    except ValueError:
        rel_parts = []

    if rel_parts:
        for part in rel_parts[:-1]:
            if part in PROTECTED_OUTPUT_DIRS or "frozen" in part or "accepted" in part or "ledger" in part:
                raise ValueError(f"refusing output under protected repository path: {resolved}")


def collect_blocks(args: argparse.Namespace) -> Tuple[List[Path], List[Dict[str, Any]], List[Dict[str, Any]]]:
    root = Path(args.root).resolve()
    files = list(iter_evidence_files(root, args.input))
    blocks: List[Dict[str, Any]] = []
    parse_errors: List[Dict[str, Any]] = []
    case_seq: Dict[str, int] = defaultdict(int)
    processed_rows = 0

    for path in files:
        rows, errors = read_jsonl(path, root, args.max_examples)
        parse_errors.extend(errors)
        for line_no, row in rows:
            if args.limit is not None and processed_rows >= args.limit:
                return files, blocks, parse_errors
            blocks.append(adapt_row(row, path, line_no, root, case_seq))
            processed_rows += 1
    return files, blocks, parse_errors


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    root = Path(args.root).resolve()
    files, blocks, parse_errors = collect_blocks(args)
    summary = build_summary(root, args.dry_run, files, blocks, parse_errors, args.max_examples)
    output = render_output(summary, blocks, args)

    if args.output and not args.stdout_only:
        output_path = Path(args.output)
        try:
            validate_output_path(output_path, root)
        except ValueError as exc:
            sys.stderr.write(f"ERROR: {exc}\n")
            return 2
        output_path.write_text(output + ("\n" if output else ""), encoding="utf-8")
    else:
        if output:
            sys.stdout.write(output)
            if not output.endswith("\n"):
                sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
