#!/usr/bin/env python3
"""Build metadata for non-frozen NET smoke candidate batches."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List


TASK_FILES = [
    "diagnosis.jsonl",
    "evidence_extraction.jsonl",
    "cause_vs_symptom.jsonl",
    "action_after_diagnosis.jsonl",
]

AUDIT_NOTES = [
    "_smoke_net_batch2_20260430/audit/NET_RUN_WINDOW_RECOVERY_POLICY_20260512.md",
    "_smoke_net_batch2_20260430/audit/net_wifi_disconnect_20260512_115341_reconciliation.md",
    "_smoke_net_batch2_20260430/audit/net_wifi_auth_fail_wrong_psk_20260513_081120_provenance_correction.md",
    "_smoke_net_batch2_20260430/audit/NET_BATCH2_7X1_SMOKE_FINAL_MINI_REVIEW_20260513.md",
    "_smoke_net_batch2_20260430/audit/FROZEN_BATCH_QUALITY_AUDIT_ANOMALY_20260513.md",
    "_smoke_net_batch2_20260430/audit/NET_BATCH2_7X2_MANUAL_TOPUP_AUTHORIZATION_20260513.md",
    "_smoke_net_batch2_20260430/audit/NET_BATCH2_7X2_MANUAL_TOPUP_PARTIAL_20260513.md",
    "_smoke_net_batch2_20260430/audit/NET_BATCH2_7X2_SMOKE_FINAL_MINI_REVIEW_20260513.md",
    "_smoke_net_batch2_20260430/audit/NET_BATCH2_7X3_MANUAL_TOPUP_AUTHORIZATION_20260514.md",
    "_smoke_net_batch2_20260430/audit/NET_BATCH2_7X3_REMAINING5_TOPUP_AUTHORIZATION_20260514.md",
    "_smoke_net_batch2_20260430/audit/DNS_REPAIR_20260514.md",
    "_smoke_net_batch2_20260430/audit/NET_BATCH2_7X3_SMOKE_FINAL_MINI_REVIEW_20260514.md",
    "dataset_batches/net_batch2_7x1_smoke_candidate_20260513/CANDIDATE_MANIFEST_REVIEW_20260513.md",
    "dataset_batches/net_batch2_7x1_smoke_candidate_20260513/sft_index_dryrun_20260513/SFT_INDEX_DRYRUN_REPORT_20260513.md",
    "dataset_batches/net_batch2_7x2_smoke_candidate_20260514_r1/ACTION_TARGET_SEMANTIC_REPAIR_REPORT_20260514.md",
    "dataset_batches/net_batch2_7x2_smoke_candidate_20260514_r1/CANDIDATE_MANIFEST_REVIEW_20260514_R1_METADATA_REPAIR.md",
    "dataset_batches/net_batch2_7x2_smoke_candidate_20260514_r1/sft_index_dryrun_20260514_r1/SFT_INDEX_DRYRUN_REPORT_20260514_R1.md",
]

EXPECTED_SUBTYPES = [
    "net_dns_fail",
    "net_no_default_route",
    "net_no_ipv4_on_iface",
    "net_public_ip_unreachable",
    "net_wifi_auth_fail_wrong_psk",
    "net_wifi_disconnect",
    "net_wrong_default_route",
]

SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(psk|password|passphrase|sae_password|wep_key|private_key)\s*[:=]"
)
PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
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


def read_json(path: Path) -> Dict[str, Any]:
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            with path.open("r", encoding=encoding) as handle:
                return json.load(handle)
        except UnicodeError:
            continue
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            with path.open("r", encoding=encoding) as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if line:
                        rows.append(json.loads(line))
            return rows
        except UnicodeError:
            rows = []
            continue
    return rows


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def rel(path: Path) -> str:
    return str(path).replace("\\", "/")


def normalize_note_refs(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def audit_refs_for(run_id: str, subtype: str) -> List[str]:
    refs = [
        "_smoke_net_batch2_20260430/audit/NET_BATCH2_7X2_SMOKE_FINAL_MINI_REVIEW_20260513.md",
        "_smoke_net_batch2_20260430/audit/NET_BATCH2_7X3_SMOKE_FINAL_MINI_REVIEW_20260514.md",
    ]
    if subtype in {
        "net_no_ipv4_on_iface",
        "net_wrong_default_route",
        "net_wifi_disconnect",
        "net_wifi_auth_fail_wrong_psk",
        "net_no_default_route",
    }:
        refs.append("_smoke_net_batch2_20260430/audit/NET_RUN_WINDOW_RECOVERY_POLICY_20260512.md")
    if subtype == "net_public_ip_unreachable":
        refs.append("legacy_public_ip_missing_recovery_gate_fields_caveat")
    if subtype == "net_dns_fail":
        refs.append("legacy_dns_validator_warning_NO_HIT_EVIDENCE")
    if run_id == "20260512_115341":
        refs.append("_smoke_net_batch2_20260430/audit/net_wifi_disconnect_20260512_115341_reconciliation.md")
    if run_id == "20260513_081120":
        refs.append("_smoke_net_batch2_20260430/audit/net_wifi_auth_fail_wrong_psk_20260513_081120_provenance_correction.md")
    if run_id.startswith("20260513_14") or run_id == "20260513_150317":
        refs.append("_smoke_net_batch2_20260430/audit/NET_BATCH2_7X2_MANUAL_TOPUP_AUTHORIZATION_20260513.md")
    if run_id == "20260513_150317":
        refs.append("_smoke_net_batch2_20260430/audit/NET_BATCH2_7X2_MANUAL_TOPUP_PARTIAL_20260513.md")
    if run_id.startswith("20260514_"):
        refs.append("_smoke_net_batch2_20260430/audit/NET_BATCH2_7X3_MANUAL_TOPUP_AUTHORIZATION_20260514.md")
        refs.append("_smoke_net_batch2_20260430/audit/DNS_REPAIR_20260514.md")
    if run_id in {"20260514_164448", "20260514_164846", "20260514_170814", "20260514_171259", "20260514_183828"}:
        refs.append("_smoke_net_batch2_20260430/audit/NET_BATCH2_7X3_REMAINING5_TOPUP_AUTHORIZATION_20260514.md")
    refs.append("dataset_batches/net_batch2_7x2_smoke_candidate_20260514_r1/ACTION_TARGET_SEMANTIC_REPAIR_REPORT_20260514.md")
    refs.append("dataset_batches/net_batch2_7x2_smoke_candidate_20260514_r1/CANDIDATE_MANIFEST_REVIEW_20260514_R1_METADATA_REPAIR.md")
    return list(dict.fromkeys(refs))


def first_non_empty(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def net_profile_metadata(canonical: Dict[str, Any]) -> Dict[str, Any]:
    derived = canonical.get("derived") or {}
    net_summary = derived.get("net_summary") or {}
    net_outcome = derived.get("net_outcome") or {}
    profile_id = first_non_empty(net_summary.get("probe_profile_id"), net_outcome.get("probe_profile_id"))
    return {
        "probe_profile_id": profile_id,
        "active_ping_ip": first_non_empty(net_summary.get("active_ping_ip"), net_outcome.get("active_ping_ip")),
        "active_dns_host": first_non_empty(net_summary.get("active_dns_host"), net_outcome.get("active_dns_host")),
        "dns_proof_base_ip": first_non_empty(net_summary.get("dns_proof_base_ip"), net_outcome.get("dns_proof_base_ip")),
        "ap_or_network_profile": first_non_empty(
            net_summary.get("ap_or_network_profile"), net_outcome.get("ap_or_network_profile")
        ),
        "profile_effective_from": first_non_empty(
            net_summary.get("profile_effective_from"), net_outcome.get("profile_effective_from")
        ),
    }


def classify_profile_epoch(canonical: Dict[str, Any]) -> str:
    profile_id = net_profile_metadata(canonical).get("probe_profile_id")
    if profile_id == "batch2_cn_public_ip_stable_v1":
        return "hotspot_usamirenko_profile_batch2_cn_public_ip_stable_v1"
    return "legacy_guest_epoch"


def load_validation_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"missing": True}
    return read_json(path)


def scan_l2_action_targets(l2_root: Path) -> Dict[str, Any]:
    path = l2_root / "action_after_diagnosis.jsonl"
    bad: List[Dict[str, str]] = []
    if not path.exists():
        return {"status": "failed", "bad_action_count": 0, "bad_actions": [{"path": str(path), "reason": "missing"}]}
    for row in read_jsonl(path):
        case_id = str(row.get("case_id") or row.get("sample_id") or "unknown")
        actions = ((row.get("target") or {}).get("recommended_actions") or [])
        for index, action in enumerate(actions):
            if not isinstance(action, dict):
                continue
            target_kind = action.get("target_kind")
            target = action.get("target")
            if (
                isinstance(target_kind, str)
                and isinstance(target, str)
                and target_kind in ACTIONABLE_NET_TARGET_KINDS
                and NET_SUBTYPE_TARGET_RE.fullmatch(target.strip())
            ):
                bad.append(
                    {
                        "case_id": case_id,
                        "index": str(index),
                        "target_kind": target_kind,
                        "target": target,
                    }
                )
    return {"status": "passed" if not bad else "failed", "bad_action_count": len(bad), "bad_actions": bad}


def scan_sensitive(candidate_root: Path) -> Dict[str, Any]:
    paths: List[str] = []
    suffixes = {".json", ".jsonl", ".md", ".txt", ".csv"}
    for path in candidate_root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in suffixes:
            continue
        text = path.read_text(encoding="utf-8-sig", errors="ignore")
        if SENSITIVE_ASSIGNMENT_RE.search(text) or PRIVATE_KEY_RE.search(text):
            paths.append(rel(path))
    return {
        "status": "passed" if not paths else "failed",
        "sensitive_match_path_count": len(paths),
        "assignment_match_paths": paths,
        "secret_token_match_paths": [],
        "allowed_terms": ["wrong_psk subtype name"],
    }


def frozen_anomaly_check(workspace: Path) -> Dict[str, Any]:
    frozen_qa = workspace / "dataset_batches" / "net_formal_batch_70_20260429" / "quality_audit"
    anomalous_names = ["l2_sample_audit.json", "l2_sample_audit.md", "sampled_cases.jsonl"]
    present = [rel(frozen_qa / name) for name in anomalous_names if (frozen_qa / name).exists()]
    guard_script = workspace / "tools" / "audit_net_l2_samples.py"
    guard_text = guard_script.read_text(encoding="utf-8-sig", errors="ignore") if guard_script.exists() else ""
    return {
        "status": "passed" if not present and "net_formal_batch_70_20260429" in guard_text else "failed",
        "anomalous_quality_audit_files_present": bool(present),
        "anomalous_files": present,
        "quarantine_exists": (workspace / "_smoke_net_batch2_20260430" / "audit" / "frozen_batch_quality_audit_anomaly_20260513").exists(),
        "guard_script": "tools/audit_net_l2_samples.py",
        "guard_present": "net_formal_batch_70_20260429" in guard_text,
    }


def write_csv(path: Path, rows: List[Dict[str, Any]], fields: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(row.get(key), ensure_ascii=False) if isinstance(row.get(key), (list, dict)) else row.get(key) for key in fields})


def main() -> None:
    parser = argparse.ArgumentParser(description="Build non-frozen NET smoke candidate manifests.")
    parser.add_argument("--candidate-root", required=True)
    parser.add_argument("--accepted-ledger", required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--expected-ledger-sha256", required=True)
    parser.add_argument("--expected-ledger-lines", required=True, type=int)
    args = parser.parse_args()

    workspace = Path.cwd()
    candidate_root = Path(args.candidate_root)
    ledger_path = Path(args.accepted_ledger)
    ledger_hash = sha256_file(ledger_path)
    ledger_rows = read_jsonl(ledger_path)
    if ledger_hash != args.expected_ledger_sha256.upper() or len(ledger_rows) != args.expected_ledger_lines:
        raise SystemExit("ACCEPTED_LEDGER_MISMATCH")

    generated_at = datetime.now().replace(microsecond=0).isoformat()
    accepted_rows: List[Dict[str, Any]] = []
    l1_rows: List[Dict[str, Any]] = []
    audit_rows: List[Dict[str, Any]] = []
    exports: List[Dict[str, str]] = []
    net_results: List[Dict[str, Any]] = []
    l1_l2_results: List[Dict[str, Any]] = []
    subtype_counts: Counter[str] = Counter()
    profile_epoch_counts: Counter[str] = Counter()
    task_counts: Counter[str] = Counter()
    sample_ids: List[str] = []

    for ledger_row in ledger_rows:
        run_id = str(ledger_row["run_id"])
        subtype = str(ledger_row["subtype"])
        subtype_counts[subtype] += 1
        export_dir = candidate_root / "runs" / run_id / "dataset_export"
        canonical = read_json(export_dir / "canonical_case.json")
        profile_meta = net_profile_metadata(canonical)
        profile_epoch = classify_profile_epoch(canonical)
        profile_epoch_counts[profile_epoch] += 1
        audit_refs = audit_refs_for(run_id, subtype)
        validation_status = "passed"
        val_log = load_validation_json(candidate_root / "validation_logs" / f"l1_l2_{run_id}.json")
        if val_log.get("validation_status") != "passed":
            validation_status = "failed"
        net_log = load_validation_json(candidate_root / "validation_logs" / f"net_{run_id}.json")
        net_results.append(net_log)
        l1_l2_results.append(
            {
                "run_id": run_id,
                "input_dir": rel(export_dir),
                "checked_objects": val_log.get("checked_objects"),
                "validation_status": val_log.get("validation_status"),
            }
        )

        row = {
            "run_id": run_id,
            "subtype": subtype,
            "source_run_dir": str(ledger_row.get("path") or f"inbox/runs/{run_id}"),
            "candidate_dataset_export": rel(export_dir),
            "validator_result": (ledger_row.get("validator") or {}).get("RESULT"),
            "inject_ok": (ledger_row.get("outcome") or {}).get("inject_ok"),
            "fault_observed": (ledger_row.get("outcome") or {}).get("fault_observed"),
            "recovery_observed": (ledger_row.get("outcome") or {}).get("recovery_observed"),
            "recovery_gate_ok": (ledger_row.get("outcome") or {}).get("recovery_gate_ok"),
            "recovery_gate_reason": (ledger_row.get("outcome") or {}).get("recovery_gate_reason"),
            "profile_epoch": profile_epoch,
            "probe_profile_id": profile_meta.get("probe_profile_id"),
            "active_ping_ip": profile_meta.get("active_ping_ip"),
            "active_dns_host": profile_meta.get("active_dns_host"),
            "dns_proof_base_ip": profile_meta.get("dns_proof_base_ip"),
            "validation_status": validation_status,
            "audit_note_refs": audit_refs,
        }
        accepted_rows.append(row)
        l1_rows.append(
            {
                "run_id": run_id,
                "subtype": subtype,
                "dataset_export_dir": rel(export_dir),
                "canonical_case_path": rel(export_dir / "canonical_case.json"),
                "evidence_candidates_path": rel(export_dir / "evidence_candidates.jsonl"),
                "quality_flags_path": rel(export_dir / "quality_flags.json"),
                "source_manifest_path": rel(export_dir / "source_manifest.json"),
                "diagnosis_input_path": rel(export_dir / "training_views" / "diagnosis_input.txt"),
                "profile_epoch": profile_epoch,
                "probe_profile_id": profile_meta.get("probe_profile_id"),
                "active_ping_ip": profile_meta.get("active_ping_ip"),
                "active_dns_host": profile_meta.get("active_dns_host"),
                "dns_proof_base_ip": profile_meta.get("dns_proof_base_ip"),
                "audit_note_refs": audit_refs,
            }
        )
        audit_rows.append(
            {
                "run_id": run_id,
                "subtype": subtype,
                "audit_note_refs": audit_refs,
                "profile_epoch": profile_epoch,
                "probe_profile_id": profile_meta.get("probe_profile_id"),
                "active_ping_ip": profile_meta.get("active_ping_ip"),
                "active_dns_host": profile_meta.get("active_dns_host"),
                "dns_proof_base_ip": profile_meta.get("dns_proof_base_ip"),
                "ap_or_network_profile": profile_meta.get("ap_or_network_profile"),
                "profile_effective_from": profile_meta.get("profile_effective_from"),
                "caveat_summary": "; ".join(audit_refs),
            }
        )
        exports.append({"run_id": run_id, "dataset_export": rel(export_dir), "l2_dir": rel(export_dir / "l2")})

    for task_file in TASK_FILES:
        rows = read_jsonl(candidate_root / "l2" / task_file)
        sample_type = task_file.removesuffix(".jsonl")
        task_counts[sample_type] = len(rows)
        sample_ids.extend(str(row.get("sample_id")) for row in rows)

    merged_l2 = load_validation_json(candidate_root / "validation_logs" / "merged_l2.json")
    semantic_scan = scan_l2_action_targets(candidate_root / "l2")
    password_scan = scan_sensitive(candidate_root)
    frozen_check = frozen_anomaly_check(workspace)
    duplicate_sample_ids = sorted([sample_id for sample_id, count in Counter(sample_ids).items() if count > 1])

    validation_summary = {
        "net_validator": net_results,
        "l1_l2_per_run": l1_l2_results,
        "merged_l2": {
            "input_dir": rel(candidate_root / "l2"),
            "checked_objects": merged_l2.get("checked_objects"),
            "validation_status": merged_l2.get("validation_status"),
        },
        "repair_checks": {
            "merged_l2_count": sum(task_counts.values()),
            "action_target_semantic_status": semantic_scan["status"],
            "action_target_bad_count": semantic_scan["bad_action_count"],
            "sample_id_unique": not duplicate_sample_ids,
        },
        "action_target_semantic_scan": semantic_scan,
        "password_psk_scan": password_scan,
        "frozen_anomaly_check": frozen_check,
    }

    manifest = {
        "batch_id": args.batch_id,
        "status": "candidate_not_frozen",
        "candidate": True,
        "frozen": False,
        "created_at_local": generated_at,
        "source_accepted_ledger": {
            "path": rel(ledger_path),
            "sha256": ledger_hash,
            "line_count": len(ledger_rows),
            "authoritative": True,
        },
        "source_policy": "JSONL accepted ledger authoritative; App Server candidate/shadow only",
        "output_root": rel(candidate_root),
        "do_not_overwrite": [
            "dataset_batches/net_batch2_7x1_smoke_candidate_20260513",
            "dataset_batches/net_batch2_7x2_smoke_candidate_20260514",
            "dataset_batches/net_batch2_7x2_smoke_candidate_20260514_r1",
            "dataset_batches/net_formal_batch_70_20260429",
        ],
        "accepted_count": len(accepted_rows),
        "expected_subtypes": EXPECTED_SUBTYPES,
        "subtype_counts": dict(sorted(subtype_counts.items())),
        "profile_epoch_counts": dict(sorted(profile_epoch_counts.items())),
        "audit_notes_consumed": AUDIT_NOTES,
        "audit_note_aware_export_rules": [
            "non_dns_run_window_is_fault_main_window; recovery_gate/post/post2 may occur after run_window",
            "20260512_115341 valid under run_window/recovery policy closure",
            "20260513_081120 stale copied approval text superseded by provenance correction sidecar",
            "legacy public-IP rows may lack recovery_gate_ok/reason and are evaluated by validator PASS, recovery_observed, post baseline, and probe evidence",
            "Guest-era accepted runs are distinct from Usamirenko/profile runs; profile runs use batch2_cn_public_ip_stable_v1 / 223.5.5.5 / www.baidu.com",
            "wrong_psk subtype token is allowed; actual PSK/password assignments are not allowed",
            "action_after_diagnosis targets must be actionable objects, not NET subtype labels",
        ],
        "caveats": [
            "Legacy DNS rows may carry non-blocking NO_HIT_EVIDENCE warnings while validator PASS is authoritative for accepted scope.",
            "Legacy public-IP accepted rows may lack newer recovery_gate_ok/recovery_gate_reason fields by legacy runner semantics.",
            "20260512_115341 relies on documented post-window recovery evidence policy.",
            "20260513_081120 provenance correction sidecar supersedes stale profile approval text; accepted ledger was not rewritten.",
            "7x2 top-up authorization and partial retry context are preserved for provenance.",
            "7x3 top-up authorization, DNS repair dependency, and final mini review are preserved for provenance.",
            "Frozen batch quality_audit anomaly repair note remains required context.",
        ],
        "validation_results": {
            "net_validator_pass": sum(1 for row in net_results if row.get("RESULT") == "PASS"),
            "net_validator_total": len(net_results),
            "per_run_l1_l2_pass": sum(1 for row in l1_l2_results if row.get("validation_status") == "passed"),
            "per_run_l1_l2_total": len(l1_l2_results),
            "merged_l2_status": merged_l2.get("validation_status"),
            "merged_l2_count": sum(task_counts.values()),
            "action_target_semantic_status": semantic_scan["status"],
            "password_scan_status": password_scan["status"],
            "frozen_anomaly_status": frozen_check["status"],
        },
        "accepted_runs": accepted_rows,
        "ready_for_downstream_split_sft_index_dry_run": (
            sum(1 for row in net_results if row.get("RESULT") == "PASS") == len(net_results)
            and sum(1 for row in l1_l2_results if row.get("validation_status") == "passed") == len(l1_l2_results)
            and semantic_scan["status"] == "passed"
            and password_scan["status"] == "passed"
            and frozen_check["status"] == "passed"
            and merged_l2.get("validation_status") == "passed"
            and not duplicate_sample_ids
        ),
        "not_frozen_statement": "This is a non-frozen smoke candidate and must not be treated as a production frozen batch.",
    }

    stats = {
        "batch_id": args.batch_id,
        "source_accepted_count": len(accepted_rows),
        "subtype_counts": dict(sorted(subtype_counts.items())),
        "profile_epoch_counts": dict(sorted(profile_epoch_counts.items())),
        "merged_l2_count": sum(task_counts.values()),
        "task_counts": dict(task_counts),
        "sample_id_unique": not duplicate_sample_ids,
        "duplicate_sample_ids": duplicate_sample_ids,
        "action_target_semantic_status": semantic_scan["status"],
        "action_target_bad_count": semantic_scan["bad_action_count"],
    }

    build_exports = {
        "batch_id": args.batch_id,
        "generated_at_local": generated_at,
        "l1_export_script": "export_run_case_dataset_fixed_v2.ps1",
        "per_run_l2_script": "derive_l2_case_samples.py",
        "merged_l2_script": "batch_derive_l2_samples.py",
        "manifest_script": "tools/build_net_smoke_candidate_manifest.py",
        "source_accepted_ledger": rel(ledger_path),
        "source_accepted_ledger_sha256": ledger_hash,
        "source_accepted_ledger_line_count": len(ledger_rows),
        "candidate_output_root": rel(candidate_root),
        "per_run_exports": exports,
    }

    write_jsonl(candidate_root / "accepted_runs.jsonl", accepted_rows)
    write_csv(candidate_root / "accepted_runs.csv", accepted_rows, list(accepted_rows[0].keys()))
    write_jsonl(candidate_root / "l1_index.jsonl", l1_rows)
    write_json(candidate_root / "audit_note_mapping.json", audit_rows)
    write_json(candidate_root / "batch_manifest.json", manifest)
    write_json(candidate_root / "build_exports.json", build_exports)
    write_json(candidate_root / "stats.json", stats)
    write_json(candidate_root / "validation_summary.json", validation_summary)

    subtype_lines = "\n".join(f"- {key}: {value}" for key, value in sorted(subtype_counts.items()))
    task_lines = "\n".join(f"- {key}: {value}" for key, value in task_counts.items())
    profile_lines = "\n".join(f"- {key}: {value}" for key, value in sorted(profile_epoch_counts.items()))
    audit_lines = "\n".join(f"- {note}" for note in AUDIT_NOTES)

    stats_md = f"""# {args.batch_id} Stats

- source accepted runs: {len(accepted_rows)}
- merged L2 samples: {sum(task_counts.values())}
- sample_id_unique: {str(not duplicate_sample_ids)}
- action_target_semantic_status: {semantic_scan["status"]}

## Subtype Counts
{subtype_lines}

## Task Counts
{task_lines}

## Profile Epoch Counts
{profile_lines}
"""
    (candidate_root / "stats.md").write_text(stats_md, encoding="utf-8", newline="\n")

    readme = f"""# {args.batch_id}

Non-frozen audit-note-aware L1/L2 candidate for NET batch2 smoke.

## Status
- candidate: true
- frozen: false
- source accepted count: {len(accepted_rows)}
- merged L2 count: {sum(task_counts.values())}
- action target semantic validation: {semantic_scan["status"]}
- no split/SFT index generated
- no training generated

## Audit Notes Consumed
{audit_lines}

## Caveats
- Non-DNS NET post/recovery evidence may occur after run_window under policy.
- 20260513_081120 stale approval text is superseded by sidecar provenance correction.
- Legacy public-IP nullable recovery_gate fields are preserved as caveat.
- Action targets in `action_after_diagnosis` are actionable objects, not NET subtype labels.
- Candidate is not frozen.

## Files
- `batch_manifest.json`: audit-note-aware manifest.
- `validation_summary.json`: validator and safety gate summary.
- `l1_index.jsonl`: L1 artifact index.
- `l2/*.jsonl`: merged L2 task samples.
"""
    (candidate_root / "README.md").write_text(readme, encoding="utf-8", newline="\n")
    (candidate_root / "batch_notes.md").write_text(readme, encoding="utf-8", newline="\n")

    build_report = f"""# NET Batch2 Smoke Candidate Build Report - {generated_at}

- candidate: {args.batch_id}
- result: {"PASS" if manifest["ready_for_downstream_split_sft_index_dry_run"] else "NEEDS_FIX"}
- not_frozen: true
- source_ledger_sha256: {ledger_hash}
- source_ledger_line_count: {len(ledger_rows)}
- source runs: {len(accepted_rows)} accepted rows / {len(subtype_counts)} subtypes
- validation: NET validator {manifest["validation_results"]["net_validator_pass"]}/{manifest["validation_results"]["net_validator_total"]} PASS; L1/per-run L2 {manifest["validation_results"]["per_run_l1_l2_pass"]}/{manifest["validation_results"]["per_run_l1_l2_total"]} PASS; merged L2 {merged_l2.get("validation_status")} with {sum(task_counts.values())} records.
- action target semantic validation: {semantic_scan["status"]}; bad actions: {semantic_scan["bad_action_count"]}
- password/PSK scan: {password_scan["status"]}
- frozen anomaly check: {frozen_check["status"]}

## Audit Note Handling
{audit_lines}

## Next Action
Review this candidate manifest, then decide whether to run split/SFT index dry-run for the candidate. Do not freeze this candidate without separate approval.
"""
    (candidate_root / "build_report.md").write_text(build_report, encoding="utf-8", newline="\n")

    print(json.dumps({"candidate_root": rel(candidate_root), "ready": manifest["ready_for_downstream_split_sft_index_dry_run"], "validation_results": manifest["validation_results"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
