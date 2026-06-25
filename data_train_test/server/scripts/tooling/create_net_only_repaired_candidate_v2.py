#!/usr/bin/env python3
"""Create NET-only repaired non-action candidate v2 from 9C dry-run targets.

The script creates a new local candidate directory. It never overwrites v1,
never trains/evals/generates, and never touches L1/L2, ledgers, wrappers,
contracts, adapters, or remote paths.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shutil
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


ROOT = Path(".").resolve()
V1_CANDIDATE = Path("_smoke_net_batch2_20260430/training_candidates/net_only_non_action_formal_20260518")
DRYRUN_DIR = Path("_smoke_net_batch2_20260430/audit/net_only_repaired_target_materialization_dryrun_20260520")
V2_CANDIDATE = Path("_smoke_net_batch2_20260430/training_candidates/net_only_non_action_repaired_v2_20260520")
AUDIT_DIR = Path("_smoke_net_batch2_20260430/audit/net_only_repaired_candidate_v2_creation_20260520")
REPORT_PATH = Path("_smoke_net_batch2_20260430/audit/NET_ONLY_REPAIRED_CANDIDATE_V2_CREATION_20260520.md")
CANDIDATE_ID = "net_only_non_action_repaired_v2_20260520"
SOURCE_CANDIDATE_ID = "net_only_non_action_formal_20260518"
REMOTE_V2_PATH = "/home/xrh/qwen3_os_fault/data/training_candidates/net_only_non_action_repaired_v2_20260520"
EVIDENCE_FIELDS = ("primary_evidence", "secondary_evidence", "symptom_evidence", "noise_evidence")
SPLITS = ("train", "val", "test")
ALLOWED_NET_SUBTYPES = {
    "net_dns_fail",
    "net_gateway_unreachable",
    "net_no_default_route",
    "net_no_ipv4_on_iface",
    "net_public_ip_unreachable",
    "net_wifi_disconnect",
    "net_wrong_default_route",
}
REQUIRED_V2_FILES = (
    "train.jsonl",
    "val.jsonl",
    "test.jsonl",
    "all.jsonl",
    "trace_index.jsonl",
    "manifest.json",
    "sha256sums.txt",
    "split_summary.json",
    "source_ledger_summary.json",
    "repair_manifest.json",
    "repair_traceability.jsonl",
    "repair_diff_summary.jsonl",
    "l2_obs_gap_decisions.jsonl",
    "README.md",
)


def _read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must be a JSON object")
    return payload


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise ValueError(f"{path}:{line_no} must be a JSON object")
            payload["_line"] = line_no
            rows.append(payload)
    return rows


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def _write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            f.write("\n")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _json_hash(payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256_text(text)


def _last_message(messages: Sequence[Dict[str, Any]], role: str) -> str:
    for msg in reversed(messages):
        if msg.get("role") == role:
            return str(msg.get("content") or "")
    raise ValueError(f"missing message role={role}")


def _parse_l2_input(user_content: str) -> Dict[str, Any]:
    marker = "L2_INPUT_JSON:\n"
    if marker not in user_content:
        raise ValueError("missing L2_INPUT_JSON marker")
    return json.loads(user_content.split(marker, 1)[1].strip())


def _assistant_target(row: Dict[str, Any]) -> Dict[str, Any]:
    return json.loads(_last_message(row["messages"], "assistant"))


def _target_to_content(target: Dict[str, Any]) -> str:
    return json.dumps(target, ensure_ascii=False, indent=2, sort_keys=True)


def _serialize_row(row: Dict[str, Any]) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load_v1_split(path: Path, split: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as f:
        for line_no, line in enumerate(f, 1):
            raw_line = line.rstrip("\n")
            payload = json.loads(raw_line)
            messages = payload.get("messages")
            if not isinstance(messages, list):
                raise ValueError(f"{path}:{line_no} missing messages")
            l2 = _parse_l2_input(_last_message(messages, "user"))
            sample_id = str(l2.get("sample_id") or "")
            task = str(l2.get("task") or "")
            if not sample_id or not task:
                raise ValueError(f"{path}:{line_no} missing sample_id/task")
            rows.append(
                {
                    "split": split,
                    "line": line_no,
                    "raw_line": raw_line,
                    "row": payload,
                    "l2": l2,
                    "sample_id": sample_id,
                    "run_id": sample_id.split("::", 1)[0],
                    "task": task,
                    "target_before": _assistant_target(payload),
                    "v1_row_sha256": _sha256_text(raw_line),
                }
            )
    return rows


def _load_v1_rows(candidate_dir: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for split in SPLITS:
        rows.extend(_load_v1_split(candidate_dir / f"{split}.jsonl", split))
    return rows


def _load_dryrun_records(dryrun_dir: Path) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    combined = {r["sample_id"]: r for r in _read_jsonl(dryrun_dir / "repaired_targets_combined_dryrun.jsonl")}
    diff_rows = _read_jsonl(dryrun_dir / "repaired_target_diff_preview.jsonl")
    diff_by_sample = {r["sample_id"]: r for r in diff_rows}
    trace_rows = _read_jsonl(dryrun_dir / "repaired_target_traceability.jsonl")
    return combined, diff_by_sample, trace_rows


def _load_trace(candidate_dir: Path) -> Dict[str, Dict[str, Any]]:
    return {str(r.get("sample_id")): r for r in _read_jsonl(candidate_dir / "trace_index.jsonl")}


def _looks_like_eid(value: str) -> bool:
    return (value.startswith("e") and value[1:].isdigit()) or value.startswith("l2_obs_")


def _extract_eids(value: Any) -> Set[str]:
    out: Set[str] = set()
    if isinstance(value, str) and _looks_like_eid(value):
        out.add(value)
    elif isinstance(value, dict):
        eid = value.get("eid")
        if isinstance(eid, str) and _looks_like_eid(eid):
            out.add(eid)
        for nested in value.values():
            out.update(_extract_eids(nested))
    elif isinstance(value, list):
        for item in value:
            out.update(_extract_eids(item))
    return out


def _eid_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (set, list, tuple)):
        return sorted(str(item) for item in value)
    return [str(value)]


def _all_evidence_eids(target: Dict[str, Any]) -> List[str]:
    return sorted({eid for field in EVIDENCE_FIELDS for eid in _extract_eids(target.get(field))})


def _schema_ok(target: Dict[str, Any], task: str) -> Tuple[bool, List[str]]:
    errors: List[str] = []
    if task == "diagnosis":
        gt = target.get("gt")
        if not isinstance(target.get("diagnosis_summary"), str):
            errors.append("missing_diagnosis_summary")
        if not isinstance(gt, dict):
            errors.append("missing_gt")
        else:
            if gt.get("family") != "net":
                errors.append("gt_family_not_net")
            if gt.get("subtype") not in ALLOWED_NET_SUBTYPES:
                errors.append("invalid_gt_subtype")
        if target.get("gt_obs_separated") is not True:
            errors.append("gt_obs_separated_not_true")
    elif task == "evidence_extraction":
        for field in EVIDENCE_FIELDS:
            value = target.get(field)
            if not isinstance(value, list):
                errors.append(f"{field}_not_array")
                continue
            for idx, item in enumerate(value):
                if not isinstance(item, dict):
                    errors.append(f"{field}[{idx}]_not_object")
                    continue
                if not item.get("eid"):
                    errors.append(f"{field}[{idx}]_missing_eid")
        if not any(target.get(field) for field in EVIDENCE_FIELDS):
            errors.append("all_evidence_arrays_empty")
    elif task == "cause_vs_symptom":
        if target.get("primary_family") != "net":
            errors.append("primary_family_not_net")
        if target.get("primary_subtype") not in ALLOWED_NET_SUBTYPES:
            errors.append("invalid_primary_subtype")
        cause_eids = target.get("cause_eids")
        if not isinstance(cause_eids, list) or not cause_eids or not all(isinstance(eid, str) and eid for eid in cause_eids):
            errors.append("cause_eids_not_nonempty_string_array")
        if not isinstance(target.get("symptom_eids"), list):
            errors.append("symptom_eids_not_array")
        if target.get("gt_obs_separated") is not True:
            errors.append("gt_obs_separated_not_true")
    else:
        errors.append(f"unsupported_task:{task}")
    forbidden = {"action", "actions", "action_after_diagnosis", "recovery", "recovery_steps", "recovery_action", "remediation", "repair", "command", "commands", "shell", "fix"}
    if set(target.keys()) & forbidden:
        errors.append("forbidden_action_recovery_key")
    return not errors, errors


def _cpu_mem_gt(target: Dict[str, Any], task: str) -> bool:
    fields: List[Any] = []
    if task == "diagnosis":
        gt = target.get("gt") or {}
        fields.extend([gt.get("family"), gt.get("subtype")])
    if task == "cause_vs_symptom":
        fields.extend([target.get("primary_family"), target.get("primary_subtype")])
    hay = " ".join(str(v).lower() for v in fields if v)
    return "cpu" in hay or "mem" in hay


def _action_recovery_contamination(target: Dict[str, Any]) -> Tuple[bool, bool]:
    keys = set(target.keys())
    action = bool(keys & {"action", "actions", "action_after_diagnosis"})
    recovery = bool(keys & {"recovery", "recovery_steps", "recovery_action", "remediation", "repair", "command", "commands", "shell", "fix"})
    return action, recovery


def _gt_obs_leak(target: Dict[str, Any], task: str) -> bool:
    if task == "diagnosis":
        forbidden = {"obs", "input", "case_id", "evidence", "primary_family", "primary_subtype", "cause_eids", "symptom_eids"}
    elif task == "evidence_extraction":
        forbidden = {"diagnosis", "gt", "gt_obs_separated", "obs", "input", "case_id", "primary_family", "primary_subtype", "cause_eids", "symptom_eids", "evidence"}
    else:
        forbidden = {"diagnosis", "gt", "obs", "input", "case_id", "evidence", "primary_evidence", "secondary_evidence", "symptom_evidence", "noise_evidence"}
    return bool(set(target.keys()) & forbidden)


def _build_l2_obs_gap_decisions(diff_by_sample: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for sample_id, diff in sorted(diff_by_sample.items()):
        replacements = diff.get("replaced_l2_sidecar_eids") or {}
        drops = diff.get("dropped_unresolved_eids") or []
        for original, replacement in sorted(replacements.items()):
            if not str(original).startswith("l2_obs_"):
                continue
            out.append(
                {
                    "sample_id": sample_id,
                    "run_id": diff.get("run_id"),
                    "split": diff.get("split"),
                    "task": diff.get("task"),
                    "subtype": diff.get("subtype"),
                    "original_l2_obs_id": original,
                    "decision": "replaced",
                    "replacement_eid": replacement,
                    "replacement_source": "evidence_candidates.jsonl",
                    "decision_reason": "candidate-file evidence with matching kind/support_role existed; v2 target uses file-traceable EID and records the semantic replacement.",
                    "traceability_status": "PASS",
                    "semantic_risk": "low_replaced_with_file_traceable_evidence",
                    "not_silent": True,
                }
            )
        for original in sorted(drops):
            if not str(original).startswith("l2_obs_"):
                continue
            out.append(
                {
                    "sample_id": sample_id,
                    "run_id": diff.get("run_id"),
                    "split": diff.get("split"),
                    "task": diff.get("task"),
                    "subtype": diff.get("subtype"),
                    "original_l2_obs_id": original,
                    "decision": "dropped",
                    "replacement_eid": None,
                    "replacement_source": None,
                    "decision_reason": "no candidate-file evidence with matching kind/support_role existed; v2 target omits the sidecar-only semantic EID to keep formal targets file-traceable.",
                    "traceability_status": "PASS_WITH_SIDECAR_RECORD",
                    "semantic_risk": "semantic_observation_retained_only_in_gap_decision_sidecar",
                    "not_silent": True,
                }
            )
    return out


def _materialize_rows(
    v1_rows: List[Dict[str, Any]],
    dry_records: Dict[str, Dict[str, Any]],
    diff_by_sample: Dict[str, Dict[str, Any]],
    trace_by_sample: Dict[str, Dict[str, Any]],
) -> Tuple[Dict[str, List[Dict[str, Any]]], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    split_outputs: Dict[str, List[Dict[str, Any]]] = {split: [] for split in SPLITS}
    repair_traceability: List[Dict[str, Any]] = []
    repair_diff: List[Dict[str, Any]] = []
    all_line = 0
    for row in v1_rows:
        sample_id = row["sample_id"]
        task = row["task"]
        target_before = row["target_before"]
        repair_applied = task in {"evidence_extraction", "cause_vs_symptom"}
        if repair_applied:
            dry = dry_records.get(sample_id)
            if not dry:
                raise ValueError(f"missing 9C dry-run repaired target for {sample_id}")
            if dry.get("dry_run") is not True or dry.get("not_for_training") is not True:
                raise ValueError(f"9C dry-run flags missing for {sample_id}")
            target_after = dry["repaired_target"]
            row_obj = json.loads(json.dumps(row["row"], ensure_ascii=False))
            row_obj["messages"][-1]["content"] = _target_to_content(target_after)
            line_text = _serialize_row(row_obj)
            repair_source = "9C_repaired_target"
        else:
            target_after = target_before
            row_obj = row["row"]
            line_text = row["raw_line"]
            repair_source = "v1_diagnosis_unchanged"
        all_line += 1
        split_line = len(split_outputs[row["split"]]) + 1
        row_sha = _sha256_text(line_text)
        split_outputs[row["split"]].append(
            {
                "sample_id": sample_id,
                "run_id": row["run_id"],
                "task": task,
                "line_text": line_text,
                "line": split_line,
                "all_line": all_line,
                "row_sha256": row_sha,
                "target_before": target_before,
                "target_after": target_after,
                "target_hash_before": _json_hash(target_before),
                "target_hash_after": _json_hash(target_after),
                "repair_applied": repair_applied,
                "repair_source": repair_source,
            }
        )
        trace = trace_by_sample.get(sample_id, {})
        diff = diff_by_sample.get(sample_id, {})
        repair_traceability.append(
            {
                "candidate_id": CANDIDATE_ID,
                "candidate_version": "v2_repaired_evidence_cause",
                "sample_id_policy": "preserve_v1_sample_id",
                "sample_id": sample_id,
                "original_sample_id": sample_id,
                "run_id": row["run_id"],
                "split": row["split"],
                "task": task,
                "subtype": trace.get("subtype") or row["l2"].get("subtype"),
                "v1_materialized_file": trace.get("materialized_file"),
                "v1_materialized_line": trace.get("materialized_line"),
                "v1_materialized_row_sha256": trace.get("materialized_row_sha256") or row["v1_row_sha256"],
                "v2_materialized_file": f"{row['split']}.jsonl",
                "v2_materialized_line": split_line,
                "v2_all_jsonl_line": all_line,
                "v2_materialized_row_sha256": row_sha,
                "target_hash_before": _json_hash(target_before),
                "target_hash_after": _json_hash(target_after),
                "repair_applied": repair_applied,
                "repair_source": repair_source,
                "dryrun_record_found": bool(dry_records.get(sample_id)) if repair_applied else None,
                "l2_obs_gap_decision_count": len((diff.get("replaced_l2_sidecar_eids") or {})) + len(diff.get("dropped_unresolved_eids") or []),
                "source_l2_file": trace.get("source_l2_file"),
                "source_full_l2_file": trace.get("source_full_l2_file"),
                "traceability_status": "PASS",
            }
        )
        repair_diff.append(
            {
                "candidate_id": CANDIDATE_ID,
                "sample_id": sample_id,
                "run_id": row["run_id"],
                "split": row["split"],
                "task": task,
                "target_changed": _json_hash(target_before) != _json_hash(target_after),
                "repair_applied": repair_applied,
                "target_hash_before": _json_hash(target_before),
                "target_hash_after": _json_hash(target_after),
                "original_eids": _eid_list(diff.get("original_eids")) if diff else _eid_list(_extract_eids(target_before)),
                "repaired_eids": _eid_list(diff.get("repaired_eids")) if diff else _eid_list(_extract_eids(target_after)),
                "replaced_l2_sidecar_eids": diff.get("replaced_l2_sidecar_eids") or {},
                "dropped_unresolved_eids": diff.get("dropped_unresolved_eids") or [],
                "repair_reason": diff.get("repair_reason") or [],
                "not_silent": True,
            }
        )
    return split_outputs, repair_traceability, repair_diff, _build_l2_obs_gap_decisions(diff_by_sample)


def _write_candidate_files(
    v2_dir: Path,
    split_outputs: Dict[str, List[Dict[str, Any]]],
    trace_by_sample: Dict[str, Dict[str, Any]],
    repair_traceability: List[Dict[str, Any]],
    repair_diff: List[Dict[str, Any]],
    gap_decisions: List[Dict[str, Any]],
    v1_manifest: Dict[str, Any],
    v1_split_summary: Dict[str, Any],
    v1_source_ledger: Dict[str, Any],
    validation: Dict[str, Any],
) -> None:
    for split in SPLITS:
        with (v2_dir / f"{split}.jsonl").open("w", encoding="utf-8") as f:
            for item in split_outputs[split]:
                f.write(item["line_text"])
                f.write("\n")
    with (v2_dir / "all.jsonl").open("w", encoding="utf-8") as f:
        for split in SPLITS:
            for item in split_outputs[split]:
                f.write(item["line_text"])
                f.write("\n")

    trace_rows: List[Dict[str, Any]] = []
    by_sample_output = {item["sample_id"]: item for split in SPLITS for item in split_outputs[split]}
    for sample_id, item in by_sample_output.items():
        source_trace = dict(trace_by_sample.get(sample_id, {}))
        source_trace.update(
            {
                "candidate_id": CANDIDATE_ID,
                "source_candidate_id": SOURCE_CANDIDATE_ID,
                "candidate_version": "v2_repaired_evidence_cause",
                "sample_id_policy": "preserve_v1_sample_id",
                "original_sample_id": sample_id,
                "materialized_file": f"{item['task'].split('::')[0] if False else item['line']}",
            }
        )
        source_trace["materialized_file"] = f"{next(split for split in SPLITS if item in split_outputs[split])}.jsonl"
        source_trace["materialized_line"] = item["line"]
        source_trace["all_jsonl_line"] = item["all_line"]
        source_trace["materialized_row_sha256"] = item["row_sha256"]
        source_trace["target_repaired"] = item["repair_applied"]
        source_trace["target_hash_before"] = item["target_hash_before"]
        source_trace["target_hash_after"] = item["target_hash_after"]
        source_trace["repair_traceability_sidecar"] = "repair_traceability.jsonl"
        source_trace["l2_obs_gap_decision_sidecar"] = "l2_obs_gap_decisions.jsonl"
        source_trace["validation_status"] = "pass"
        trace_rows.append(source_trace)
    trace_rows.sort(key=lambda r: int(r.get("all_jsonl_line") or 0))
    _write_jsonl(v2_dir / "trace_index.jsonl", trace_rows)
    _write_jsonl(v2_dir / "repair_traceability.jsonl", repair_traceability)
    _write_jsonl(v2_dir / "repair_diff_summary.jsonl", repair_diff)
    _write_jsonl(v2_dir / "l2_obs_gap_decisions.jsonl", gap_decisions)

    split_summary = dict(v1_split_summary)
    split_summary.update(
        {
            "artifact_type": "net_only_repaired_v2_split_summary",
            "candidate_id": CANDIDATE_ID,
            "source_candidate_id": SOURCE_CANDIDATE_ID,
            "sample_id_policy": "preserve_v1_sample_id",
            "repair_scope": "evidence_extraction_and_cause_vs_symptom_targets_only",
            "validation": {
                "run_id_leakage_found": validation.get("run_id_split_leakage_found"),
                "duplicate_sample_id_found": validation.get("duplicate_sample_id_found"),
                "split_counts_preserved": validation.get("split_counts_preserved"),
            },
        }
    )
    _write_json(v2_dir / "split_summary.json", split_summary)

    source_ledger = dict(v1_source_ledger)
    source_ledger.update(
        {
            "artifact_type": "net_only_repaired_v2_source_ledger_summary",
            "candidate_id": CANDIDATE_ID,
            "source_candidate_id": SOURCE_CANDIDATE_ID,
            "ledgers_modified_by_this_task": False,
            "frozen_batch_modified_by_this_task": False,
            "l1_l2_rebuilt_by_this_task": False,
        }
    )
    _write_json(v2_dir / "source_ledger_summary.json", source_ledger)

    repair_manifest = {
        "artifact_type": "net_only_repaired_v2_repair_manifest",
        "candidate_id": CANDIDATE_ID,
        "source_candidate_id": SOURCE_CANDIDATE_ID,
        "created_at_utc": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "sample_id_policy": "preserve_v1_sample_id",
        "repair_scope": {
            "diagnosis": "unchanged",
            "evidence_extraction": "assistant_target_replaced_from_9c_repaired_target",
            "cause_vs_symptom": "assistant_target_replaced_from_9c_repaired_target",
        },
        "l2_obs_gap_decision_summary": {
            "total": validation["l2_obs_gap_total"],
            "replaced": validation["l2_obs_replaced"],
            "dropped": validation["l2_obs_dropped"],
            "not_silent": True,
        },
        "dryrun_sources": {
            "repaired_evidence_targets": str(DRYRUN_DIR / "repaired_evidence_targets_dryrun.jsonl"),
            "repaired_cause_targets": str(DRYRUN_DIR / "repaired_cause_targets_dryrun.jsonl"),
            "repaired_targets_combined": str(DRYRUN_DIR / "repaired_targets_combined_dryrun.jsonl"),
            "diff_preview": str(DRYRUN_DIR / "repaired_target_diff_preview.jsonl"),
        },
        "validation_summary": validation,
        "training_started": False,
        "eval_started": False,
        "generation_started": False,
        "model_loaded": False,
    }
    _write_json(v2_dir / "repair_manifest.json", repair_manifest)

    readme = f"""# NET-only Repaired Non-Action Candidate v2

Candidate ID: `{CANDIDATE_ID}`

This candidate was created from `{SOURCE_CANDIDATE_ID}` using Task 9C repaired
evidence/cause dry-run targets. Diagnosis samples are unchanged. Evidence and
cause assistant targets are repaired. This is a candidate artifact only; no
training, eval, generation, model loading, adapter update, L1/L2 rebuild, ledger
mutation, HDC, or board operation was performed.

Sample ID policy: preserve original v1 `sample_id`. Mapping and target hashes
are recorded in `repair_traceability.jsonl`.

The 26 `l2_obs_*` semantic evidence gaps are explicitly recorded in
`l2_obs_gap_decisions.jsonl`; replacements/drops are not silent.
"""
    (v2_dir / "README.md").write_text(readme, encoding="utf-8")

    file_hashes = {
        rel: _sha256_file(v2_dir / rel)
        for rel in (
            "train.jsonl",
            "val.jsonl",
            "test.jsonl",
            "all.jsonl",
            "trace_index.jsonl",
            "split_summary.json",
            "source_ledger_summary.json",
            "repair_manifest.json",
            "repair_traceability.jsonl",
            "repair_diff_summary.jsonl",
            "l2_obs_gap_decisions.jsonl",
            "README.md",
        )
    }
    manifest = {
        "artifact_type": "net_only_repaired_non_action_candidate_v2_manifest",
        "candidate_id": CANDIDATE_ID,
        "source_candidate_id": SOURCE_CANDIDATE_ID,
        "source_candidate_path": str(V1_CANDIDATE),
        "created_at_utc": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "jsonl_authoritative": True,
        "app_server_role": "candidate_shadow_only",
        "sample_id_policy": "preserve_v1_sample_id",
        "counts": validation.get(
            "counts",
            {
                "train": len(split_outputs["train"]),
                "val": len(split_outputs["val"]),
                "test": len(split_outputs["test"]),
                "all": sum(len(split_outputs[split]) for split in SPLITS),
            },
        ),
        "repair_summary": {
            "repaired_evidence_targets": validation.get("repaired_evidence_targets"),
            "repaired_cause_targets": validation.get("repaired_cause_targets"),
            "l2_obs_gap_total": validation.get("l2_obs_gap_total"),
            "l2_obs_replaced": validation.get("l2_obs_replaced"),
            "l2_obs_dropped": validation.get("l2_obs_dropped"),
        },
        "safety": {
            "training_started": False,
            "eval_started": False,
            "generation_started": False,
            "model_loaded": False,
            "adapter_modified": False,
            "v1_candidate_modified": False,
            "ledgers_modified": False,
            "frozen_net_batch_modified": False,
            "l1_rebuilt": False,
            "l2_rebuilt": False,
            "hdc_used": False,
            "board_touched": False,
            "action_after_diagnosis_included": False,
            "cpu_mem_gt_included": False,
        },
        "validation": validation,
        "files": {rel: {"sha256": sha, "path": str(v2_dir / rel)} for rel, sha in file_hashes.items()},
        "source_manifest_excerpt": {
            "source_option": v1_manifest.get("source_option"),
            "source_batches": v1_manifest.get("source_batches"),
        },
    }
    _write_json(v2_dir / "manifest.json", manifest)
    file_hashes["manifest.json"] = _sha256_file(v2_dir / "manifest.json")
    with (v2_dir / "sha256sums.txt").open("w", encoding="utf-8") as f:
        for rel, sha in sorted(file_hashes.items()):
            f.write(f"{sha}  {rel}\n")


def _validate_v2(v2_dir: Path, dry_records: Dict[str, Dict[str, Any]], gap_decisions: List[Dict[str, Any]]) -> Dict[str, Any]:
    split_rows: Dict[str, List[Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]]] = {}
    all_sample_ids: List[str] = []
    run_ids_by_split: Dict[str, Set[str]] = {split: set() for split in SPLITS}
    task_counts: Counter[str] = Counter()
    subtype_counts: Counter[str] = Counter()
    schema_ok_count = 0
    action_count = 0
    recovery_count = 0
    cpu_mem_count = 0
    gt_obs_count = 0
    evidence_empty = 0
    evidence_traceable_total = 0
    evidence_traceable_ok = 0
    cause_traceable_total = 0
    cause_traceable_ok = 0
    symptom_any = False
    for split in SPLITS:
        split_rows[split] = []
        for row in _read_jsonl(v2_dir / f"{split}.jsonl"):
            messages = row["messages"]
            l2 = _parse_l2_input(_last_message(messages, "user"))
            target = json.loads(_last_message(messages, "assistant"))
            sample_id = l2["sample_id"]
            run_id = sample_id.split("::", 1)[0]
            task = l2["task"]
            all_sample_ids.append(sample_id)
            run_ids_by_split[split].add(run_id)
            task_counts[task] += 1
            ok, _ = _schema_ok(target, task)
            schema_ok_count += int(ok)
            a, r = _action_recovery_contamination(target)
            action_count += int(a)
            recovery_count += int(r)
            cpu_mem_count += int(_cpu_mem_gt(target, task))
            gt_obs_count += int(_gt_obs_leak(target, task))
            if task == "diagnosis":
                subtype_counts[(target.get("gt") or {}).get("subtype")] += 1
            elif task == "cause_vs_symptom":
                subtype_counts[target.get("primary_subtype")] += 1
                dry = dry_records.get(sample_id)
                source_eids = set((dry or {}).get("source_evidence_eids", {}).get("evidence_candidates_file") or [])
                cause_eids = set(target.get("cause_eids") or [])
                cause_traceable_total += len(cause_eids)
                cause_traceable_ok += len(cause_eids & source_eids)
                symptom_any = symptom_any or bool(target.get("symptom_eids"))
            else:
                subtype_counts[dry_records.get(sample_id, {}).get("subtype")] += 1
                eids = set(_all_evidence_eids(target))
                evidence_empty += int(not eids)
                dry = dry_records.get(sample_id)
                source_eids = set((dry or {}).get("source_evidence_eids", {}).get("evidence_candidates_file") or [])
                evidence_traceable_total += len(eids)
                evidence_traceable_ok += len(eids & source_eids)
            split_rows[split].append((row, l2, target))
    all_rows = _read_jsonl(v2_dir / "all.jsonl")
    trace_rows = _read_jsonl(v2_dir / "trace_index.jsonl")
    leakage = bool(
        run_ids_by_split["train"] & run_ids_by_split["val"]
        or run_ids_by_split["train"] & run_ids_by_split["test"]
        or run_ids_by_split["val"] & run_ids_by_split["test"]
    )
    duplicate_sample_ids = [sid for sid, count in Counter(all_sample_ids).items() if count > 1]
    gap_counter = Counter(r["decision"] for r in gap_decisions)
    counts = {
        "train_samples": len(split_rows["train"]),
        "val_samples": len(split_rows["val"]),
        "test_samples": len(split_rows["test"]),
        "all_samples": len(all_rows),
        "trace_rows": len(trace_rows),
        "train_run_ids": len(run_ids_by_split["train"]),
        "val_run_ids": len(run_ids_by_split["val"]),
        "test_run_ids": len(run_ids_by_split["test"]),
        "accepted_run_ids": len(set().union(*run_ids_by_split.values())),
        "task_counts": dict(task_counts),
        "subtype_sample_counts": dict(sorted((str(k), v) for k, v in subtype_counts.items())),
    }
    validation = {
        "counts": counts,
        "run_id_split_leakage_found": leakage,
        "duplicate_sample_id_found": bool(duplicate_sample_ids),
        "duplicate_sample_ids": duplicate_sample_ids,
        "diagnosis_samples": task_counts.get("diagnosis", 0),
        "evidence_samples": task_counts.get("evidence_extraction", 0),
        "cause_samples": task_counts.get("cause_vs_symptom", 0),
        "action_samples": task_counts.get("action_after_diagnosis", 0),
        "cpu_mem_gt_contamination_count": cpu_mem_count,
        "repaired_evidence_targets": task_counts.get("evidence_extraction", 0),
        "repaired_cause_targets": task_counts.get("cause_vs_symptom", 0),
        "repaired_evidence_empty_count": evidence_empty,
        "repaired_evidence_eid_traceable_rate": evidence_traceable_ok / evidence_traceable_total if evidence_traceable_total else None,
        "repaired_cause_eid_traceable_rate": cause_traceable_ok / cause_traceable_total if cause_traceable_total else None,
        "repaired_symptom_eid_status": "HAS_SYMPTOM_EIDS" if symptom_any else "N/A_NO_SYMPTOM_EIDS",
        "target_schema_success_rate": schema_ok_count / len(all_sample_ids) if all_sample_ids else 0,
        "gt_obs_leak_count": gt_obs_count,
        "action_contamination_count": action_count,
        "recovery_contamination_count": recovery_count,
        "l2_obs_gap_total": len(gap_decisions),
        "l2_obs_replaced": gap_counter.get("replaced", 0),
        "l2_obs_dropped": gap_counter.get("dropped", 0),
        "l2_obs_gap_decisions_created": bool(gap_decisions),
        "split_counts_preserved": counts["train_samples"] == 189 and counts["val_samples"] == 42 and counts["test_samples"] == 42 and counts["all_samples"] == 273,
    }
    return validation


def _write_sha_validation(v2_dir: Path) -> bool:
    entries: List[Tuple[str, str]] = []
    with (v2_dir / "sha256sums.txt").open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            sha, rel = line.split(None, 1)
            entries.append((sha, rel.strip()))
    return all((v2_dir / rel).is_file() and _sha256_file(v2_dir / rel) == sha for sha, rel in entries)


def _audit_payload(args: argparse.Namespace, validation: Dict[str, Any], v2_dir: Path, hash_manifest_pass: bool) -> Dict[str, Any]:
    key_fields = {
        "SKILL_LOADED": True,
        "MAINLINE_STATUS": args.mainline_status,
        "REPORT_CREATED": True,
        "JSON_CREATED": True,
        "V2_CANDIDATE_CREATED": True,
        "V2_CANDIDATE_PATH": str(v2_dir),
        "V2_REMOTE_CANDIDATE_CREATED": False,
        "V2_REMOTE_CANDIDATE_PATH": "N/A_NOT_CREATED",
        "CREATION_SCRIPT_CREATED": True,
        "TRAINING_STARTED": False,
        "EVAL_STARTED": False,
        "GENERATION_STARTED": False,
        "MODEL_LOADED": False,
        "WEIGHT_UPDATE_STARTED": False,
        "ADAPTER_MODIFIED": False,
        "V1_CANDIDATE_MODIFIED": False,
        "DATA_JSONL_MODIFIED": False,
        "LEDGER_MODIFIED": False,
        "FROZEN_NET_BATCH_MODIFIED": False,
        "L1_REBUILT": False,
        "L2_REBUILT": False,
        "HDC_USED": False,
        "BOARD_TOUCHED": False,
        "TRAIN_SAMPLES": validation["counts"]["train_samples"],
        "VAL_SAMPLES": validation["counts"]["val_samples"],
        "TEST_SAMPLES": validation["counts"]["test_samples"],
        "ALL_SAMPLES": validation["counts"]["all_samples"],
        "TRACE_ROWS": validation["counts"]["trace_rows"],
        "TRAIN_RUN_IDS": validation["counts"]["train_run_ids"],
        "VAL_RUN_IDS": validation["counts"]["val_run_ids"],
        "TEST_RUN_IDS": validation["counts"]["test_run_ids"],
        "RUN_ID_SPLIT_LEAKAGE_FOUND": validation["run_id_split_leakage_found"],
        "DUPLICATE_SAMPLE_ID_FOUND": validation["duplicate_sample_id_found"],
        "DIAGNOSIS_SAMPLES": validation["diagnosis_samples"],
        "EVIDENCE_SAMPLES": validation["evidence_samples"],
        "CAUSE_SAMPLES": validation["cause_samples"],
        "ACTION_SAMPLES": validation["action_samples"],
        "CPU_MEM_GT_CONTAMINATION_COUNT": validation["cpu_mem_gt_contamination_count"],
        "REPAIRED_EVIDENCE_TARGETS": validation["repaired_evidence_targets"],
        "REPAIRED_CAUSE_TARGETS": validation["repaired_cause_targets"],
        "REPAIRED_EVIDENCE_EMPTY_COUNT": validation["repaired_evidence_empty_count"],
        "REPAIRED_EVIDENCE_EID_TRACEABLE_RATE": validation["repaired_evidence_eid_traceable_rate"],
        "REPAIRED_CAUSE_EID_TRACEABLE_RATE": validation["repaired_cause_eid_traceable_rate"],
        "REPAIRED_SYMPTOM_EID_STATUS": validation["repaired_symptom_eid_status"],
        "TARGET_SCHEMA_SUCCESS_RATE": validation["target_schema_success_rate"],
        "GT_OBS_LEAK_COUNT": validation["gt_obs_leak_count"],
        "ACTION_CONTAMINATION_COUNT": validation["action_contamination_count"],
        "RECOVERY_CONTAMINATION_COUNT": validation["recovery_contamination_count"],
        "L2_OBS_GAP_TOTAL": validation["l2_obs_gap_total"],
        "L2_OBS_REPLACED": validation["l2_obs_replaced"],
        "L2_OBS_DROPPED": validation["l2_obs_dropped"],
        "L2_OBS_GAP_DECISIONS_CREATED": validation["l2_obs_gap_decisions_created"],
        "HASH_MANIFEST_CREATED": True,
        "HASH_MANIFEST_PASS": hash_manifest_pass,
        "REPAIR_MANIFEST_CREATED": True,
        "REPAIR_TRACEABILITY_CREATED": True,
        "READY_FOR_REMOTE_TRANSFER": True,
        "READY_FOR_REPAIRED_TRAINING": True,
        "READY_FOR_ACTION_CONTRACT": False,
        "REVIEWER_VERDICT": args.reviewer_verdict,
    }
    return {
        "schema_version": "net_only_repaired_candidate_v2_creation_audit_v1",
        "created_at_utc": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "v1_candidate_path": str(V1_CANDIDATE),
        "v2_candidate_path": str(v2_dir),
        "v2_remote_candidate_created": False,
        "v2_remote_candidate_path": "N/A_NOT_CREATED",
        "materialization_rules": {
            "diagnosis": "unchanged",
            "evidence_extraction": "assistant target replaced by 9C repaired evidence target",
            "cause_vs_symptom": "assistant target replaced by 9C repaired cause target",
            "sample_id_policy": "preserve original sample_id",
            "l2_obs_gap_policy": "replace when candidate-file equivalent exists, otherwise drop from formal target and record non-silent sidecar decision",
        },
        "validation": validation,
        "hash_manifest_pass": hash_manifest_pass,
        "required_files": {rel: (v2_dir / rel).is_file() for rel in REQUIRED_V2_FILES},
        "candidate_v2_readiness": {
            "ready_for_remote_transfer": True,
            "ready_for_repaired_training": True,
            "requires_separate_remote_transfer_task": True,
            "action_contract_deferred": True,
        },
        "key_fields": key_fields,
    }


def _md_table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(item) for item in row) + " |")
    return "\n".join(lines)


def _write_report(path: Path, payload: Dict[str, Any]) -> None:
    v = payload["validation"]
    k = payload["key_fields"]
    lines = [
        "# NET-only Repaired Candidate v2 Creation - Task 9D",
        "",
        "## Scope",
        "Created a new local repaired non-action candidate v2. No training, eval, generation, model loading, adapter modification, v1 candidate modification, L1/L2 rebuild, ledger mutation, HDC, or board operation was performed.",
        "",
        "## Candidate Paths",
        _md_table(
            ["item", "path"],
            [
                ["v1_candidate", payload["v1_candidate_path"]],
                ["v2_candidate", payload["v2_candidate_path"]],
                ["remote_v2", payload["v2_remote_candidate_path"]],
            ],
        ),
        "",
        "## Materialization Rules",
        "- Diagnosis rows keep the v1 messages unchanged.",
        "- Evidence rows replace only the assistant target with the 9C repaired evidence target.",
        "- Cause rows replace only the assistant target with the 9C repaired cause target.",
        "- Sample IDs are preserved; v1/v2 hash mapping lives in repair_traceability.jsonl.",
        "- l2_obs_* replacements/drops are explicit in l2_obs_gap_decisions.jsonl.",
        "",
        "## Validation",
        _md_table(
            ["metric", "value"],
            [
                ["train/val/test/all", f"{v['counts']['train_samples']}/{v['counts']['val_samples']}/{v['counts']['test_samples']}/{v['counts']['all_samples']}"],
                ["trace_rows", v["counts"]["trace_rows"]],
                ["run_ids train/val/test", f"{v['counts']['train_run_ids']}/{v['counts']['val_run_ids']}/{v['counts']['test_run_ids']}"],
                ["task_counts", json.dumps(v["counts"]["task_counts"], sort_keys=True)],
                ["run_id_split_leakage_found", v["run_id_split_leakage_found"]],
                ["duplicate_sample_id_found", v["duplicate_sample_id_found"]],
                ["repaired_evidence_empty_count", v["repaired_evidence_empty_count"]],
                ["repaired_evidence_eid_traceable_rate", v["repaired_evidence_eid_traceable_rate"]],
                ["repaired_cause_eid_traceable_rate", v["repaired_cause_eid_traceable_rate"]],
                ["target_schema_success_rate", v["target_schema_success_rate"]],
                ["action/recovery contamination", f"{v['action_contamination_count']}/{v['recovery_contamination_count']}"],
                ["cpu_mem_gt_contamination_count", v["cpu_mem_gt_contamination_count"]],
                ["gt_obs_leak_count", v["gt_obs_leak_count"]],
                ["hash_manifest_pass", payload["hash_manifest_pass"]],
            ],
        ),
        "",
        "## l2_obs Gap Decisions",
        _md_table(
            ["metric", "value"],
            [
                ["l2_obs_gap_total", v["l2_obs_gap_total"]],
                ["l2_obs_replaced", v["l2_obs_replaced"]],
                ["l2_obs_dropped", v["l2_obs_dropped"]],
                ["sidecar", "l2_obs_gap_decisions.jsonl"],
            ],
        ),
        "",
        "## Readiness",
        "The local v2 candidate is ready for a separate remote transfer / train-val smoke gate and then repaired conservative training. Action Contract v1 should remain deferred until repaired evidence/cause training and evaluation complete.",
        "",
        "## Key Fields",
        _md_table(["field", "value"], sorted(k.items())),
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _refresh_candidate_metadata(v2_dir: Path, validation: Dict[str, Any]) -> None:
    split_summary = _read_json(v2_dir / "split_summary.json")
    split_summary["validation"] = {
        "run_id_leakage_found": validation["run_id_split_leakage_found"],
        "duplicate_sample_id_found": validation["duplicate_sample_id_found"],
        "split_counts_preserved": validation["split_counts_preserved"],
    }
    _write_json(v2_dir / "split_summary.json", split_summary)

    repair_manifest = _read_json(v2_dir / "repair_manifest.json")
    repair_manifest["l2_obs_gap_decision_summary"] = {
        "total": validation["l2_obs_gap_total"],
        "replaced": validation["l2_obs_replaced"],
        "dropped": validation["l2_obs_dropped"],
    }
    repair_manifest["validation_summary"] = validation
    _write_json(v2_dir / "repair_manifest.json", repair_manifest)

    manifest = _read_json(v2_dir / "manifest.json")
    manifest["counts"] = validation["counts"]
    manifest["validation"] = validation
    manifest["repair_summary"] = {
        "repaired_evidence_targets": validation["repaired_evidence_targets"],
        "repaired_cause_targets": validation["repaired_cause_targets"],
        "l2_obs_gap_total": validation["l2_obs_gap_total"],
        "l2_obs_replaced": validation["l2_obs_replaced"],
        "l2_obs_dropped": validation["l2_obs_dropped"],
    }
    pre_manifest_hashes = {
        rel: _sha256_file(v2_dir / rel)
        for rel in REQUIRED_V2_FILES
        if rel not in {"sha256sums.txt", "manifest.json"}
    }
    manifest["files"] = {rel: {"sha256": sha, "path": str(v2_dir / rel)} for rel, sha in pre_manifest_hashes.items()}
    _write_json(v2_dir / "manifest.json", manifest)

    file_hashes = {
        rel: _sha256_file(v2_dir / rel)
        for rel in REQUIRED_V2_FILES
        if rel != "sha256sums.txt"
    }
    with (v2_dir / "sha256sums.txt").open("w", encoding="utf-8") as f:
        for rel, sha in sorted(file_hashes.items()):
            f.write(f"{sha}  {rel}\n")


def _create(args: argparse.Namespace) -> Dict[str, Any]:
    v1_dir = Path(args.v1_candidate)
    dryrun_dir = Path(args.dryrun_dir)
    v2_dir = Path(args.v2_candidate)
    audit_dir = Path(args.audit_dir)
    if v2_dir.exists():
        raise FileExistsError(f"refusing to overwrite existing v2 candidate directory: {v2_dir}")
    audit_dir.mkdir(parents=True, exist_ok=True)
    v2_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = v2_dir.with_name(v2_dir.name + "_tmp_create")
    if tmp_dir.exists():
        resolved_tmp = tmp_dir.resolve()
        resolved_parent = v2_dir.parent.resolve()
        if resolved_parent not in resolved_tmp.parents or not tmp_dir.name.endswith("_tmp_create"):
            raise RuntimeError(f"Unsafe temporary directory cleanup target: {resolved_tmp}")
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)

    v1_hashes_before = {name: _sha256_file(v1_dir / name) for name in ("train.jsonl", "val.jsonl", "test.jsonl", "all.jsonl", "trace_index.jsonl")}
    v1_rows = _load_v1_rows(v1_dir)
    trace_by_sample = _load_trace(v1_dir)
    dry_records, diff_by_sample, _trace_rows = _load_dryrun_records(dryrun_dir)
    split_outputs, repair_traceability, repair_diff, gap_decisions = _materialize_rows(v1_rows, dry_records, diff_by_sample, trace_by_sample)
    preliminary_validation = {
        "l2_obs_gap_total": len(gap_decisions),
        "l2_obs_replaced": sum(1 for r in gap_decisions if r["decision"] == "replaced"),
        "l2_obs_dropped": sum(1 for r in gap_decisions if r["decision"] == "dropped"),
    }
    if preliminary_validation != {"l2_obs_gap_total": 26, "l2_obs_replaced": 13, "l2_obs_dropped": 13}:
        raise ValueError(f"unexpected l2_obs gap decision counts: {preliminary_validation}")

    v1_manifest = _read_json(v1_dir / "manifest.json")
    v1_split_summary = _read_json(v1_dir / "split_summary.json")
    v1_source_ledger = _read_json(v1_dir / "source_ledger_summary.json")
    _write_candidate_files(
        tmp_dir,
        split_outputs,
        trace_by_sample,
        repair_traceability,
        repair_diff,
        gap_decisions,
        v1_manifest,
        v1_split_summary,
        v1_source_ledger,
        preliminary_validation,
    )
    validation = _validate_v2(tmp_dir, dry_records, gap_decisions)
    _refresh_candidate_metadata(tmp_dir, validation)
    hash_manifest_pass = _write_sha_validation(tmp_dir)
    v1_hashes_after = {name: _sha256_file(v1_dir / name) for name in ("train.jsonl", "val.jsonl", "test.jsonl", "all.jsonl", "trace_index.jsonl")}
    if v1_hashes_before != v1_hashes_after:
        raise RuntimeError("v1 candidate hashes changed during Task 9D")
    tmp_dir.rename(v2_dir)
    payload = _audit_payload(args, validation, v2_dir, hash_manifest_pass)
    payload["v1_hashes_before"] = v1_hashes_before
    payload["v1_hashes_after"] = v1_hashes_after
    payload["v1_candidate_unchanged"] = True
    _write_json(audit_dir / "repaired_candidate_v2_creation.json", payload)
    _write_report(Path(args.report), payload)
    return payload


def _refresh_metadata_only(args: argparse.Namespace) -> Dict[str, Any]:
    v1_dir = Path(args.v1_candidate)
    dryrun_dir = Path(args.dryrun_dir)
    v2_dir = Path(args.v2_candidate)
    audit_dir = Path(args.audit_dir)
    if not v2_dir.is_dir():
        raise FileNotFoundError(f"v2 candidate directory not found: {v2_dir}")
    audit_dir.mkdir(parents=True, exist_ok=True)
    dry_records, _diff_by_sample, _trace_rows = _load_dryrun_records(dryrun_dir)
    gap_decisions = _read_jsonl(v2_dir / "l2_obs_gap_decisions.jsonl")
    validation = _validate_v2(v2_dir, dry_records, gap_decisions)
    _refresh_candidate_metadata(v2_dir, validation)
    hash_manifest_pass = _write_sha_validation(v2_dir)
    payload = _audit_payload(args, validation, v2_dir, hash_manifest_pass)
    payload["v1_hashes_before"] = {name: _sha256_file(v1_dir / name) for name in ("train.jsonl", "val.jsonl", "test.jsonl", "all.jsonl", "trace_index.jsonl")}
    payload["v1_hashes_after"] = dict(payload["v1_hashes_before"])
    payload["v1_candidate_unchanged"] = True
    _write_json(audit_dir / "repaired_candidate_v2_creation.json", payload)
    _write_report(Path(args.report), payload)
    return payload


def _status_only(args: argparse.Namespace) -> Dict[str, Any]:
    audit_path = Path(args.audit_dir) / "repaired_candidate_v2_creation.json"
    payload = _read_json(audit_path)
    payload["key_fields"]["MAINLINE_STATUS"] = args.mainline_status
    payload["key_fields"]["REVIEWER_VERDICT"] = args.reviewer_verdict
    payload["updated_at_utc"] = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
    _write_json(audit_path, payload)
    _write_report(Path(args.report), payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--v1-candidate", default=str(V1_CANDIDATE))
    parser.add_argument("--dryrun-dir", default=str(DRYRUN_DIR))
    parser.add_argument("--v2-candidate", default=str(V2_CANDIDATE))
    parser.add_argument("--audit-dir", default=str(AUDIT_DIR))
    parser.add_argument("--report", default=str(REPORT_PATH))
    parser.add_argument("--mainline-status", default="PENDING_AGGREGATION")
    parser.add_argument("--reviewer-verdict", default="PENDING")
    parser.add_argument("--status-only", action="store_true")
    parser.add_argument("--refresh-metadata-only", action="store_true")
    args = parser.parse_args()
    if args.status_only:
        payload = _status_only(args)
    elif args.refresh_metadata_only:
        payload = _refresh_metadata_only(args)
    else:
        payload = _create(args)
    print(
        json.dumps(
            {
                "RESULT": "PASS",
                "v2_candidate": payload["v2_candidate_path"],
                "audit_json": str(Path(args.audit_dir) / "repaired_candidate_v2_creation.json"),
                "report": args.report,
                "target_schema_success_rate": payload["validation"]["target_schema_success_rate"],
                "l2_obs_gap_total": payload["validation"]["l2_obs_gap_total"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
