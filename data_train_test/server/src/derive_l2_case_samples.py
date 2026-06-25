#!/usr/bin/env python3
"""
Derive controlled L2 samples from L1 canonical exports.

Inputs:
- canonical_case.json
- evidence_candidates.jsonl

Outputs:
- diagnosis.jsonl
- evidence_extraction.jsonl
- cause_vs_symptom.jsonl
- action_after_diagnosis.jsonl
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


TASK_VERSION = "l2_tasks_v1"
VALIDATION_STATUS_PASSED = "passed"
ALLOWED_SUPPORT_ROLES = {"primary", "symptom", "secondary", "noise"}
TASK_OUTPUT_FILES = {
    "diagnosis": "diagnosis.jsonl",
    "evidence_extraction": "evidence_extraction.jsonl",
    "cause_vs_symptom": "cause_vs_symptom.jsonl",
    "action_after_diagnosis": "action_after_diagnosis.jsonl",
}
PATH_LEAK_PATTERNS = ("C:\\Users\\", "Desktop\\work")
NET_SUBTYPE_TARGET_RE = re.compile(r"^net_[a-z0-9_]+$", re.IGNORECASE)
ACTIONABLE_NET_TARGET_KINDS = {
    "dns_resolver",
    "network_component",
    "network_interface",
    "network_path",
    "network_probe",
    "network_service",
    "route",
    "routing_table",
    "wifi_connection",
    "wifi_profile",
}

NET_ACTION_TARGET_MAP = {
    "net_dns_fail": {
        "inspect_action": "investigate_dns_resolution",
        "inspect_kind": "dns_resolver",
        "inspect_target": "dns_resolver_config",
        "recover_action": "recover_dns_resolution_safe",
        "recover_kind": "dns_resolver",
        "recover_target": "dns_resolver_config",
    },
    "net_public_ip_unreachable": {
        "inspect_action": "investigate_public_reachability",
        "inspect_kind": "network_path",
        "inspect_target": "public_ip_reachability_path",
        "recover_action": "recover_public_reachability_safe",
        "recover_kind": "network_path",
        "recover_target": "public_ip_reachability_path",
    },
    "net_no_default_route": {
        "inspect_action": "investigate_default_route",
        "inspect_kind": "routing_table",
        "inspect_target": "default_route",
        "recover_action": "restore_default_route_safe",
        "recover_kind": "routing_table",
        "recover_target": "default_route",
    },
    "net_no_ipv4_on_iface": {
        "inspect_action": "investigate_interface_ipv4",
        "inspect_kind": "network_interface",
        "inspect_target": "wlan0",
        "recover_action": "restore_interface_ipv4_safe",
        "recover_kind": "network_interface",
        "recover_target": "wlan0",
    },
    "net_wrong_default_route": {
        "inspect_action": "investigate_default_route_gateway",
        "inspect_kind": "routing_table",
        "inspect_target": "default_route_gateway",
        "recover_action": "restore_default_route_gateway_safe",
        "recover_kind": "routing_table",
        "recover_target": "default_route_gateway",
    },
    "net_gateway_unreachable": {
        "inspect_action": "investigate_default_gateway_reachability",
        "inspect_kind": "network_path",
        "inspect_target": "default_gateway",
        "recover_action": "recover_default_gateway_reachability_safe",
        "recover_kind": "network_path",
        "recover_target": "default_gateway",
    },
    "net_wifi_disconnect": {
        "inspect_action": "investigate_wifi_link",
        "inspect_kind": "network_interface",
        "inspect_target": "wlan0",
        "recover_action": "reconnect_wifi_safe",
        "recover_kind": "wifi_connection",
        "recover_target": "current_ssid_profile",
    },
    "net_wifi_auth_fail_wrong_psk": {
        "inspect_action": "investigate_wifi_auth_config",
        "inspect_kind": "wifi_profile",
        "inspect_target": "current_ssid_auth_config",
        "recover_action": "restore_wifi_auth_profile_safe",
        "recover_kind": "wifi_profile",
        "recover_target": "current_ssid_auth_config",
    },
}


class ValidationError(Exception):
    """Raised when derived L2 records fail schema validation."""


def read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def read_json_if_exists(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    return read_json(path)


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_jsonl(path: Path, records: Iterable[Dict[str, Any]]) -> None:
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")


def resolve_dataset_export(input_path: Path) -> Path:
    if input_path.is_file() and input_path.name.lower() == "canonical_case.json":
        return input_path.parent
    if input_path.is_dir() and input_path.name == "dataset_export":
        return input_path
    if input_path.is_dir():
        candidate = input_path / "dataset_export"
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"Cannot resolve dataset_export from: {input_path}")


def select_evidence_by_role(evidence_rows: List[Dict[str, Any]], role: str) -> List[Dict[str, Any]]:
    return sorted(
        [row for row in evidence_rows if row.get("support_role") == role],
        key=lambda row: float(row.get("score") or 0.0),
        reverse=True,
    )


def trim_evidence_rows(rows: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    trimmed: List[Dict[str, Any]] = []
    for row in rows[:limit]:
        trimmed.append(
            {
                "eid": row.get("eid"),
                "source": row.get("source"),
                "source_rel": row.get("source_rel"),
                "kind": row.get("kind"),
                "support_role": row.get("support_role"),
                "text": row.get("text"),
                "score": row.get("score"),
                "ts": row.get("ts"),
                "span": row.get("span"),
            }
        )
    return trimmed


def read_text_if_exists(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8-sig", errors="ignore")


def has_wlan_ipv4(text: str) -> bool:
    return bool(re.search(r"(?ms)(### ifconfig wlan0|### ifconfig_wlan|^wlan0\b).*?inet addr:\s*\d+", text))


def get_main_public_ping_section(text: str) -> str:
    match = re.search(r"(?ms)^### ping -c \d+ [^\r\n]+\r?\n(.*?)(?=^### |\Z)", text)
    if match:
        return match.group(1)
    return text


def has_public_probe_fail(text: str) -> bool:
    lowered = get_main_public_ping_section(text).lower()
    return (
        "100% packet loss" in lowered
        or "network unreachable" in lowered
        or "sendto:" in lowered
    )


def has_gateway_probe_fail(text: str) -> bool:
    match = re.search(r"(?ms)^### ping_gateway\r?\n(.*?)(?=^### |\Z)", text)
    if not match:
        return False
    lowered = match.group(1).lower()
    return (
        "100% packet loss" in lowered
        or "0 received" in lowered
        or "network unreachable" in lowered
        or "no_gateway" in lowered
    )


def find_wlan_route_rows(text: str) -> List[Tuple[str, str, str]]:
    rows: List[Tuple[str, str, str]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "wlan0" and re.fullmatch(r"[0-9A-Fa-f]{8}", parts[1]):
            rows.append((parts[0], parts[1].upper(), parts[2].upper()))
    return rows


def synthesize_route_fault_evidence(canonical: Dict[str, Any], dataset_export_dir: Path) -> List[Dict[str, Any]]:
    """Create L2-only OBS evidence for route faults when L1 lacks it.

    The rows are derived from fault-phase raw snapshots and are used only by
    evidence_extraction.  They do not rewrite L1 evidence candidates.
    """
    subtype = (canonical.get("gt") or {}).get("subtype")
    if subtype not in {"net_no_default_route", "net_wrong_default_route", "net_gateway_unreachable"}:
        return []

    run_dir = dataset_export_dir.parent
    net_outcome = read_json_if_exists(run_dir / "_net_outcome.json")
    if net_outcome.get("net_fault_type") != subtype or net_outcome.get("fault_observed") is not True:
        return []

    net_fault = read_text_if_exists(run_dir / "net" / "net_fault.txt")
    probe_fault = read_text_if_exists(run_dir / "net" / "probe_fault.txt")
    fault_text = net_fault + "\n" + probe_fault
    has_ip = has_wlan_ipv4(fault_text)
    public_fail = has_public_probe_fail(probe_fault)
    route_rows = find_wlan_route_rows(fault_text)
    default_route = next((row for row in route_rows if row[1] == "00000000"), None)
    default_route_present = default_route is not None
    wpa_completed = "wpa_state=COMPLETED" in fault_text

    if subtype == "net_no_default_route":
        if has_ip and wpa_completed and (not default_route_present) and public_fail:
            return [
                {
                    "eid": "l2_obs_net_no_default_route",
                    "source": "net_probe",
                    "source_rel": "net",
                    "kind": "net_no_default_route",
                    "target": "primary",
                    "support_role": "primary",
                    "text": (
                        "Fault snapshot shows wlan0 still has IPv4"
                        " while wpa_state=COMPLETED, but no wlan0 default route is present and the main public probe fails."
                    ),
                    "score": 0.985,
                    "ts": None,
                    "span": None,
                }
            ]
        return []

    if subtype == "net_gateway_unreachable":
        if default_route_present and has_ip and public_fail and has_gateway_probe_fail(probe_fault):
            return [
                {
                    "eid": "l2_obs_net_gateway_unreachable",
                    "source": "net_probe",
                    "source_rel": "net",
                    "kind": "net_gateway_unreachable",
                    "target": "primary",
                    "support_role": "primary",
                    "text": (
                        "Fault snapshot shows wlan0 still has IPv4 and a default route, "
                        "but the default gateway probe fails and the main public probe fails downstream."
                    ),
                    "score": 0.985,
                    "ts": None,
                    "span": None,
                }
            ]
        return []

    subnet_route = any(destination != "00000000" and gateway == "00000000" for _, destination, gateway in route_rows)
    wrong_gateway_hex = default_route[2] if default_route else ""
    wrong_default = bool(wrong_gateway_hex and wrong_gateway_hex != "00000000")
    if wrong_default and subnet_route and has_ip and public_fail:
        return [
            {
                "eid": "l2_obs_net_wrong_default_route",
                "source": "net_probe",
                "source_rel": "net",
                "kind": "net_wrong_default_route",
                "target": "primary",
                "support_role": "primary",
                "text": (
                    "Fault snapshot shows a nonzero fake/wrong wlan0 default route gateway with the subnet route still present, "
                    "wlan0 IPv4 still assigned, and the main public probe failing."
                ),
                "score": 0.985,
                "ts": None,
                "span": None,
            }
        ]
    return []


def get_top_processes(canonical: Dict[str, Any], limit: int = 3) -> List[Dict[str, Any]]:
    # NET faults are diagnosed from network evidence, not from incidental
    # process RSS rankings. Keep process leads out unless a future exporter
    # adds explicit causal process evidence for NET.
    if ((canonical.get("gt") or {}).get("family") == "net"):
        return []

    suspects = (((canonical.get("derived") or {}).get("process_suspects") or {}).get("suspects") or [])
    top_rows: List[Dict[str, Any]] = []
    for suspect in suspects[:limit]:
        row = {
            "comm": suspect.get("comm"),
            "max_rss_kb": suspect.get("max_rss_kb"),
            "seen_in_snapshots": suspect.get("seen_in_snapshots"),
            "rss_growth_kb": suspect.get("rss_growth_kb"),
            "mem_pressure_refs": suspect.get("mem_pressure_refs", 0),
            "cpu_hotspot_refs": suspect.get("cpu_hotspot_refs", 0),
            "injector_name_match": suspect.get("injector_name_match", False),
            "ranking_score": suspect.get("ranking_score"),
        }
        top_rows.append(row)
    return top_rows


def build_diagnosis_summary(canonical: Dict[str, Any], evidence_rows: List[Dict[str, Any]]) -> str:
    gt = canonical.get("gt") or {}
    obs = canonical.get("obs") or {}
    family = gt.get("family", "unknown")
    subtype = gt.get("subtype", "unknown")
    top_processes = get_top_processes(canonical, limit=1)
    suspect_text = ""
    if top_processes and top_processes[0].get("comm"):
        suspect_text = f" Top suspect is {top_processes[0]['comm']}."

    symptom_kinds = [row.get("kind") for row in select_evidence_by_role(evidence_rows, "symptom")[:3] if row.get("kind")]
    symptom_text = ""
    if symptom_kinds:
        symptom_text = " Symptoms remain " + ", ".join(symptom_kinds) + "."

    observed = obs.get("primary_family", "unknown")
    return (
        f"Primary diagnosis is {family}/{subtype}. "
        f"Observed family is {observed}, which must stay separated from GT."
        f"{suspect_text}{symptom_text}"
    ).strip()


def build_cause_vs_symptom_text(canonical: Dict[str, Any], evidence_rows: List[Dict[str, Any]]) -> str:
    gt = canonical.get("gt") or {}
    family = gt.get("family", "unknown")
    subtype = gt.get("subtype", "unknown")
    symptom_rows = select_evidence_by_role(evidence_rows, "symptom")[:3]
    symptom_kinds = [row.get("kind") for row in symptom_rows if row.get("kind")]
    top_processes = get_top_processes(canonical, limit=2)
    suspect_names = [row.get("comm") for row in top_processes if row.get("comm")]
    suspect_text = ", ".join(suspect_names) if suspect_names else "no causal process lead"
    if symptom_kinds:
        symptom_text = ", ".join(symptom_kinds)
        if family == "net" and not suspect_names:
            return (
                f"Treat {family}/{subtype} as the primary cause. "
                f"No process lead is promoted because the causal evidence is network-scoped. "
                f"Keep {symptom_text} as symptom-only evidence rather than promoting it to the root cause."
            )
        return (
            f"Treat {family}/{subtype} as the primary cause. "
            f"Use {suspect_text} as the highest-value process leads. "
            f"Keep {symptom_text} as symptom-only evidence rather than promoting it to the root cause."
        )
    if family == "net" and not suspect_names:
        return (
            f"Treat {family}/{subtype} as the primary cause. "
            f"No process lead is promoted because the causal evidence is network-scoped. "
            f"No separate symptom set was extracted from the current evidence."
        )
    return (
        f"Treat {family}/{subtype} as the primary cause. "
        f"Use {suspect_text} as the highest-value process leads. "
        f"No separate symptom set was extracted from the current evidence."
    )


def choose_action_targets(canonical: Dict[str, Any]) -> Tuple[str, str]:
    top_processes = get_top_processes(canonical, limit=2)
    primary_target = "unknown_process"
    secondary_target = "system_behavior"
    if top_processes and top_processes[0].get("comm"):
        primary_target = str(top_processes[0]["comm"])
    if len(top_processes) > 1 and top_processes[1].get("comm"):
        secondary_target = str(top_processes[1]["comm"])
    return primary_target, secondary_target


def _safe_action_target(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if not value or NET_SUBTYPE_TARGET_RE.fullmatch(value):
        return ""
    return value


def choose_net_action_targets(canonical: Dict[str, Any], subtype: str) -> Dict[str, str]:
    derived = canonical.get("derived") or {}
    net_summary = derived.get("net_summary") or {}
    net_outcome = derived.get("net_outcome") or {}
    target_policy = dict(NET_ACTION_TARGET_MAP.get(subtype) or {})

    iface = (
        _safe_action_target(net_summary.get("affected_iface"))
        or _safe_action_target(net_summary.get("iface_used"))
        or _safe_action_target(net_outcome.get("iface_used"))
        or "wlan0"
    )
    active_ping_ip = _safe_action_target(net_summary.get("active_ping_ip")) or _safe_action_target(net_outcome.get("active_ping_ip"))
    active_dns_host = _safe_action_target(net_summary.get("active_dns_host")) or _safe_action_target(net_outcome.get("active_dns_host"))
    target_ip = (
        _safe_action_target(net_summary.get("target_ip"))
        or _safe_action_target(net_summary.get("net_target_ip"))
        or _safe_action_target(net_outcome.get("target_ip"))
        or _safe_action_target(net_outcome.get("net_target_ip"))
    )

    if subtype in {"net_no_ipv4_on_iface", "net_wifi_disconnect"}:
        target_policy["inspect_target"] = iface
        if target_policy.get("recover_kind") == "network_interface":
            target_policy["recover_target"] = iface
    elif subtype == "net_public_ip_unreachable" and target_ip:
        target_policy["inspect_target"] = f"public_ip:{target_ip}"
    elif subtype == "net_dns_fail" and active_dns_host:
        target_policy["inspect_target"] = f"dns_resolver_for:{active_dns_host}"

    if not target_policy:
        target_policy = {
            "inspect_action": "investigate_network_component",
            "inspect_kind": "network_component",
            "inspect_target": "network_stack",
            "recover_action": "recover_network_component_safe",
            "recover_kind": "network_component",
            "recover_target": "network_stack",
        }

    probe_parts = ["dns_resolution", "ping_ip", "link_state"]
    if active_dns_host:
        probe_parts.append(f"dns_host:{active_dns_host}")
    if active_ping_ip:
        probe_parts.append(f"public_ip:{active_ping_ip}")
    if target_ip and target_ip != active_ping_ip:
        probe_parts.append(f"target_public_ip:{target_ip}")
    target_policy["probe_target"] = ",".join(probe_parts)
    return target_policy


def build_action_rows(canonical: Dict[str, Any]) -> List[Dict[str, Any]]:
    gt = canonical.get("gt") or {}
    family = gt.get("family", "unknown")
    subtype = gt.get("subtype", "unknown")
    primary_target, secondary_target = choose_action_targets(canonical)

    if family == "mem":
        return [
            {
                "action_type": "investigate_process",
                "target_kind": "process",
                "target": primary_target,
                "priority": "high",
                "operator_confirmation_required": False,
                "dangerous_command": False,
                "reason": "Highest-ranked leak suspect with aligned memory evidence.",
            },
            {
                "action_type": "collect_followup_evidence",
                "target_kind": "metrics",
                "target": "mem_available_kb,rss_growth",
                "priority": "high",
                "operator_confirmation_required": False,
                "dangerous_command": False,
                "reason": "Confirm the leak trend and verify recovery after containment.",
            },
            {
                "action_type": "restart_service_safe",
                "target_kind": "process",
                "target": primary_target,
                "priority": "medium",
                "operator_confirmation_required": True,
                "dangerous_command": False,
                "reason": "Controlled containment option when the leak suspect keeps growing.",
            },
        ]

    if family == "cpu":
        return [
            {
                "action_type": "investigate_process",
                "target_kind": "process",
                "target": primary_target,
                "priority": "high",
                "operator_confirmation_required": False,
                "dangerous_command": False,
                "reason": "Highest-ranked CPU hotspot suspect should be inspected first.",
            },
            {
                "action_type": "collect_followup_evidence",
                "target_kind": "metrics",
                "target": "cpu_util_total_x100,load1_x100",
                "priority": "high",
                "operator_confirmation_required": False,
                "dangerous_command": False,
                "reason": "Confirm whether the hotspot is sustained or bursty.",
            },
            {
                "action_type": "reduce_load_safe",
                "target_kind": "workload",
                "target": secondary_target if secondary_target != "system_behavior" else subtype,
                "priority": "medium",
                "operator_confirmation_required": True,
                "dangerous_command": False,
                "reason": "Operator-approved load reduction is safer than forceful termination.",
            },
        ]

    if family == "net":
        net_targets = choose_net_action_targets(canonical, subtype)
        return [
            {
                "action_type": net_targets["inspect_action"],
                "target_kind": net_targets["inspect_kind"],
                "target": net_targets["inspect_target"],
                "priority": "high",
                "operator_confirmation_required": False,
                "dangerous_command": False,
                "reason": "Inspect the actionable network object most aligned with the diagnosed fault.",
            },
            {
                "action_type": "collect_followup_evidence",
                "target_kind": "network_probe",
                "target": net_targets["probe_target"],
                "priority": "high",
                "operator_confirmation_required": False,
                "dangerous_command": False,
                "reason": "Confirm whether DNS, reachability, and link state have recovered.",
            },
            {
                "action_type": net_targets["recover_action"],
                "target_kind": net_targets["recover_kind"],
                "target": net_targets["recover_target"],
                "priority": "medium",
                "operator_confirmation_required": True,
                "dangerous_command": False,
                "reason": "Any interface or network-service reset must stay operator-approved and non-destructive by policy.",
            },
        ]

    return [
        {
            "action_type": "collect_followup_evidence",
            "target_kind": "metrics",
            "target": "core_metrics",
            "priority": "medium",
            "operator_confirmation_required": False,
            "dangerous_command": False,
            "reason": "Unknown family requires additional evidence before action.",
        },
        {
            "action_type": "investigate_process",
            "target_kind": "process",
            "target": primary_target,
            "priority": "medium",
            "operator_confirmation_required": False,
            "dangerous_command": False,
            "reason": "Start with the top-ranked process while keeping actions non-destructive.",
        },
        {
            "action_type": "compare_against_baseline",
            "target_kind": "analysis",
            "target": "baseline_run",
            "priority": "medium",
            "operator_confirmation_required": False,
            "dangerous_command": False,
            "reason": "Use baseline comparison before any disruptive action.",
        },
    ]


def build_record_prefix(sample_type: str, canonical: Dict[str, Any]) -> Dict[str, Any]:
    case_id = canonical.get("case_id")
    source_case_id = canonical.get("source_case_id") or case_id
    return {
        "task_version": TASK_VERSION,
        "validation_status": VALIDATION_STATUS_PASSED,
        "schema_version": f"l2_{sample_type}_v1",
        "sample_type": sample_type,
        "sample_id": f"{case_id}::{sample_type}",
        "case_id": case_id,
        "source_case_id": source_case_id,
        "source_type": canonical.get("source_type"),
    }


def build_diagnosis_record(canonical: Dict[str, Any], evidence_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    record = build_record_prefix("diagnosis", canonical)
    record["input"] = {
        "obs": {
            "state": (canonical.get("obs") or {}).get("state"),
            "primary_family": (canonical.get("obs") or {}).get("primary_family"),
            "secondary_families": (canonical.get("obs") or {}).get("secondary_families") or [],
            "fault_families": (canonical.get("obs") or {}).get("fault_families") or [],
        },
        "modality_mask": canonical.get("modality_mask") or canonical.get("modalities") or {},
        "quality_flags": canonical.get("quality_flags") or [],
        "top_processes": get_top_processes(canonical, limit=3),
        "key_evidence": trim_evidence_rows(select_evidence_by_role(evidence_rows, "primary"), limit=6),
        "symptom_evidence": trim_evidence_rows(select_evidence_by_role(evidence_rows, "symptom"), limit=4),
    }
    record["target"] = {
        "gt": canonical.get("gt") or {},
        "diagnosis_summary": build_diagnosis_summary(canonical, evidence_rows),
        "gt_obs_separated": True,
    }
    return record


def build_evidence_extraction_record(
    canonical: Dict[str, Any],
    evidence_rows: List[Dict[str, Any]],
    dataset_export_dir: Path,
) -> Dict[str, Any]:
    route_obs_rows = synthesize_route_fault_evidence(canonical, dataset_export_dir)
    evidence_rows_for_task = route_obs_rows + evidence_rows
    record = build_record_prefix("evidence_extraction", canonical)
    record["input"] = {
        "obs": {
            "primary_family": (canonical.get("obs") or {}).get("primary_family"),
            "fault_families": (canonical.get("obs") or {}).get("fault_families") or [],
        },
        "candidate_evidence": trim_evidence_rows(evidence_rows_for_task, limit=16),
    }
    record["target"] = {
        "primary_evidence": trim_evidence_rows(select_evidence_by_role(evidence_rows_for_task, "primary"), limit=8),
        "symptom_evidence": trim_evidence_rows(select_evidence_by_role(evidence_rows_for_task, "symptom"), limit=6),
        "secondary_evidence": trim_evidence_rows(select_evidence_by_role(evidence_rows_for_task, "secondary"), limit=6),
        "noise_evidence": trim_evidence_rows(select_evidence_by_role(evidence_rows_for_task, "noise"), limit=6),
    }
    return record


def build_cause_vs_symptom_record(canonical: Dict[str, Any], evidence_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    record = build_record_prefix("cause_vs_symptom", canonical)
    primary_rows = select_evidence_by_role(evidence_rows, "primary")
    symptom_rows = select_evidence_by_role(evidence_rows, "symptom")
    record["input"] = {
        "obs": canonical.get("obs") or {},
        "top_processes": get_top_processes(canonical, limit=4),
        "primary_evidence": trim_evidence_rows(primary_rows, limit=8),
        "symptom_evidence": trim_evidence_rows(symptom_rows, limit=6),
    }
    record["target"] = {
        "primary_family": (canonical.get("gt") or {}).get("family"),
        "primary_subtype": (canonical.get("gt") or {}).get("subtype"),
        "primary_processes": [row["comm"] for row in get_top_processes(canonical, limit=3) if row.get("comm")],
        "cause_eids": [row.get("eid") for row in primary_rows[:8] if row.get("eid")],
        "symptom_eids": [row.get("eid") for row in symptom_rows[:6] if row.get("eid")],
        "judgement_text": build_cause_vs_symptom_text(canonical, evidence_rows),
        "gt_obs_separated": True,
    }
    return record


def build_action_after_diagnosis_record(canonical: Dict[str, Any], evidence_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    record = build_record_prefix("action_after_diagnosis", canonical)
    diagnosis_result = {
        "family": (canonical.get("gt") or {}).get("family"),
        "subtype": (canonical.get("gt") or {}).get("subtype"),
        "summary": build_diagnosis_summary(canonical, evidence_rows),
    }
    record["input"] = {
        "task_condition": "diagnosis_conditioned",
        "diagnosis_result": diagnosis_result,
        "gt": {
            "family": diagnosis_result["family"],
            "subtype": diagnosis_result["subtype"],
        },
        "obs": {
            "primary_family": (canonical.get("obs") or {}).get("primary_family"),
            "secondary_families": (canonical.get("obs") or {}).get("secondary_families") or [],
        },
        "top_processes": get_top_processes(canonical, limit=3),
        "primary_evidence": trim_evidence_rows(select_evidence_by_role(evidence_rows, "primary"), limit=6),
        "symptom_evidence": trim_evidence_rows(select_evidence_by_role(evidence_rows, "symptom"), limit=4),
    }
    record["target"] = {
        "policy": {
            "task_condition": "diagnosis_conditioned",
            "direct_commands_allowed": False,
            "destructive_actions_allowed": False,
            "operator_confirmation_required_for_disruption": True,
        },
        "recommended_actions": build_action_rows(canonical),
    }
    return record


def derive_records(dataset_export_dir: Path) -> Dict[str, Dict[str, Any]]:
    canonical = read_json(dataset_export_dir / "canonical_case.json")
    evidence_rows = read_jsonl(dataset_export_dir / "evidence_candidates.jsonl")
    return {
        "diagnosis": build_diagnosis_record(canonical, evidence_rows),
        "evidence_extraction": build_evidence_extraction_record(canonical, evidence_rows, dataset_export_dir),
        "cause_vs_symptom": build_cause_vs_symptom_record(canonical, evidence_rows),
        "action_after_diagnosis": build_action_after_diagnosis_record(canonical, evidence_rows),
    }


def fail_validation(errors: List[str]) -> None:
    message = ["L2 schema validation failed:"]
    for issue in errors:
        message.append(f"- {issue}")
    raise ValidationError("\n".join(message))


def require_fields(record: Dict[str, Any], required_fields: List[str], errors: List[str]) -> None:
    for field_name in required_fields:
        value = record.get(field_name)
        if value is None or value == "":
            errors.append(f"{record.get('sample_id', 'unknown')} missing required field: {field_name}")


def validate_evidence_rows(rows: List[Dict[str, Any]], sample_id: str, field_name: str, errors: List[str]) -> None:
    for index, row in enumerate(rows):
        if row.get("support_role") not in ALLOWED_SUPPORT_ROLES:
            errors.append(
                f"{sample_id} has invalid support_role in {field_name}[{index}]: {row.get('support_role')}"
            )
        for required_key in ("source", "kind", "text"):
            if not row.get(required_key):
                errors.append(f"{sample_id} missing {required_key} in {field_name}[{index}]")


def validate_no_path_or_sn_leak(record: Dict[str, Any], sample_id: str, errors: List[str]) -> None:
    blob = json.dumps(record, ensure_ascii=False)
    for marker in PATH_LEAK_PATTERNS:
        if marker in blob:
            errors.append(f"{sample_id} leaks host path marker: {marker}")
    if '"device_sn"' in blob:
        errors.append(f"{sample_id} leaks raw device_sn field")
    blocked_tokens = ['"kind":"Count"', '"kind":"Keys"', '"kind":"Values"', '"kind":"IsReadOnly"', '"kind":"IsFixedSize"', '"kind":"SyncRoot"', '"kind":"IsSynchronized"']
    for token in blocked_tokens:
        if token in blob:
            errors.append(f"{sample_id} contains blocked evidence pollution token: {token}")


def validate_gt_obs_boundary(record: Dict[str, Any], sample_type: str, errors: List[str]) -> None:
    sample_id = record.get("sample_id", "unknown")
    input_block = record.get("input") or {}
    target_block = record.get("target") or {}

    if sample_type == "diagnosis":
        if "gt" in input_block:
            errors.append(f"{sample_id} diagnosis input must not contain gt")
        if "gt" not in target_block:
            errors.append(f"{sample_id} diagnosis target must contain gt")

    if sample_type == "cause_vs_symptom":
        if "gt" in input_block:
            errors.append(f"{sample_id} cause_vs_symptom input must not contain gt")
        if not target_block.get("gt_obs_separated"):
            errors.append(f"{sample_id} cause_vs_symptom target must set gt_obs_separated=true")

    if sample_type == "action_after_diagnosis":
        if input_block.get("task_condition") != "diagnosis_conditioned":
            errors.append(f"{sample_id} action_after_diagnosis input must declare diagnosis_conditioned task")
        diagnosis_result = input_block.get("diagnosis_result") or {}
        if not diagnosis_result.get("family") or not diagnosis_result.get("subtype"):
            errors.append(f"{sample_id} action_after_diagnosis input missing diagnosis_result family/subtype")
        policy = target_block.get("policy") or {}
        if not policy:
            errors.append(f"{sample_id} action_after_diagnosis target missing policy")
        if policy.get("direct_commands_allowed") is not False:
            errors.append(f"{sample_id} action_after_diagnosis policy must set direct_commands_allowed=false")
        if policy.get("destructive_actions_allowed") is not False:
            errors.append(f"{sample_id} action_after_diagnosis policy must set destructive_actions_allowed=false")


def validate_action_target_semantics(record: Dict[str, Any], errors: List[str]) -> None:
    sample_id = record.get("sample_id", "unknown")
    actions = ((record.get("target") or {}).get("recommended_actions") or [])
    if not isinstance(actions, list):
        errors.append(f"{sample_id} action_after_diagnosis recommended_actions must be a list")
        return
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            errors.append(f"{sample_id} action_after_diagnosis recommended_actions[{index}] must be an object")
            continue
        target_kind = action.get("target_kind")
        target = action.get("target")
        if isinstance(target_kind, str) and isinstance(target, str):
            if target_kind in ACTIONABLE_NET_TARGET_KINDS and NET_SUBTYPE_TARGET_RE.fullmatch(target.strip()):
                errors.append(
                    f"{sample_id} action_after_diagnosis recommended_actions[{index}] "
                    f"uses subtype label {target!r} as actionable {target_kind} target"
                )


def validate_records(grouped_records: Dict[str, List[Dict[str, Any]]]) -> None:
    errors: List[str] = []
    seen_sample_ids = set()

    for sample_type, records in grouped_records.items():
        if not records:
            errors.append(f"{sample_type} has no records")
            continue

        for record in records:
            sample_id = record.get("sample_id", "unknown")
            require_fields(
                record,
                ["task_version", "validation_status", "schema_version", "sample_type", "sample_id", "case_id", "source_case_id", "source_type"],
                errors,
            )
            if record.get("task_version") != TASK_VERSION:
                errors.append(f"{sample_id} has unexpected task_version: {record.get('task_version')}")
            if record.get("validation_status") != VALIDATION_STATUS_PASSED:
                errors.append(f"{sample_id} has unexpected validation_status: {record.get('validation_status')}")
            if record.get("sample_type") != sample_type:
                errors.append(f"{sample_id} sample_type mismatch: expected {sample_type}, got {record.get('sample_type')}")
            if record.get("source_case_id") != record.get("case_id"):
                errors.append(f"{sample_id} source_case_id must match case_id for current L2 derivation")
            if sample_id in seen_sample_ids:
                errors.append(f"duplicate sample_id detected: {sample_id}")
            seen_sample_ids.add(sample_id)

            input_block = record.get("input")
            target_block = record.get("target")
            if not isinstance(input_block, dict):
                errors.append(f"{sample_id} input must be an object")
            if not isinstance(target_block, dict):
                errors.append(f"{sample_id} target must be an object")

            validate_gt_obs_boundary(record, sample_type, errors)
            validate_no_path_or_sn_leak(record, sample_id, errors)

            if sample_type == "evidence_extraction":
                validate_evidence_rows(input_block.get("candidate_evidence") or [], sample_id, "candidate_evidence", errors)
                for field_name in ("primary_evidence", "symptom_evidence", "secondary_evidence", "noise_evidence"):
                    validate_evidence_rows(target_block.get(field_name) or [], sample_id, field_name, errors)
            elif sample_type == "diagnosis":
                validate_evidence_rows(input_block.get("key_evidence") or [], sample_id, "key_evidence", errors)
                validate_evidence_rows(input_block.get("symptom_evidence") or [], sample_id, "symptom_evidence", errors)
            elif sample_type == "cause_vs_symptom":
                validate_evidence_rows(input_block.get("primary_evidence") or [], sample_id, "primary_evidence", errors)
                validate_evidence_rows(input_block.get("symptom_evidence") or [], sample_id, "symptom_evidence", errors)
            elif sample_type == "action_after_diagnosis":
                validate_evidence_rows(input_block.get("primary_evidence") or [], sample_id, "primary_evidence", errors)
                validate_evidence_rows(input_block.get("symptom_evidence") or [], sample_id, "symptom_evidence", errors)
                validate_action_target_semantics(record, errors)

    if errors:
        fail_validation(errors)


def remove_stale_outputs(output_root: Path) -> None:
    stale_path = output_root / "action_recommendation.jsonl"
    if stale_path.exists():
        stale_path.unlink()


def main() -> None:
    parser = argparse.ArgumentParser(description="Derive L2 JSONL samples from L1 canonical exports.")
    parser.add_argument(
        "--input-path",
        action="append",
        required=True,
        help="Run dir, dataset_export dir, or canonical_case.json path. Repeatable.",
    )
    parser.add_argument(
        "--output-root",
        required=True,
        help="Directory where the four JSONL outputs will be written.",
    )
    args = parser.parse_args()

    output_root = Path(args.output_root).resolve()
    ensure_dir(output_root)
    remove_stale_outputs(output_root)

    grouped: Dict[str, List[Dict[str, Any]]] = {
        "diagnosis": [],
        "evidence_extraction": [],
        "cause_vs_symptom": [],
        "action_after_diagnosis": [],
    }

    for raw_path in args.input_path:
        dataset_export_dir = resolve_dataset_export(Path(raw_path).resolve())
        records = derive_records(dataset_export_dir)
        for key, value in records.items():
            grouped[key].append(value)

    try:
        validate_records(grouped)
    except ValidationError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

    write_jsonl(output_root / TASK_OUTPUT_FILES["diagnosis"], grouped["diagnosis"])
    write_jsonl(output_root / TASK_OUTPUT_FILES["evidence_extraction"], grouped["evidence_extraction"])
    write_jsonl(output_root / TASK_OUTPUT_FILES["cause_vs_symptom"], grouped["cause_vs_symptom"])
    write_jsonl(output_root / TASK_OUTPUT_FILES["action_after_diagnosis"], grouped["action_after_diagnosis"])

    print(f"Wrote validated L2 samples to: {output_root}")


if __name__ == "__main__":
    main()
