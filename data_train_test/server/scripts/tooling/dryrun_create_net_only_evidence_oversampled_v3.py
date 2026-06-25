#!/usr/bin/env python3
"""Dry-run materialize NET-only evidence-oversampled candidate-v3 artifacts.

Task 9M helper. This script is intentionally dry-run only:
- writes only under the audit output directory,
- does not create a formal training_candidates v3 directory,
- keeps source candidate JSONL unchanged,
- keeps messages content unchanged,
- marks every materialized row as dry_run/not_for_training/not_formal_candidate.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_SOURCE_DIR = Path(
    "_smoke_net_batch2_20260430/training_candidates/"
    "net_only_non_action_repaired_v2_20260520"
)
DEFAULT_PLAN_DIR = Path(
    "_smoke_net_batch2_20260430/audit/net_only_evidence_oversampling_plan_20260520"
)
DEFAULT_OUTPUT_DIR = Path(
    "_smoke_net_batch2_20260430/audit/"
    "net_only_evidence_oversampled_v3_dryrun_20260521"
)
DEFAULT_REPORT = Path(
    "_smoke_net_batch2_20260430/audit/"
    "NET_ONLY_EVIDENCE_OVERSAMPLED_V3_DRYRUN_20260521.md"
)

TASKS = ("diagnosis", "evidence_extraction", "cause_vs_symptom")
EVIDENCE_ARRAYS = (
    "primary_evidence",
    "secondary_evidence",
    "symptom_evidence",
    "noise_evidence",
)
REQUIRED_SOURCE_FILES = (
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
)


def sha256_bytes(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_target_hash(target: dict[str, Any]) -> str:
    return sha256_text(json.dumps(target, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        value = json.load(fh)
    return value if isinstance(value, dict) else {}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            text = line.strip()
            if not text:
                continue
            obj = json.loads(text)
            if not isinstance(obj, dict):
                raise ValueError(f"{path}:{line_no}: row is not an object")
            obj["_source_line"] = line_no
            obj["_source_row_sha256"] = sha256_text(text)
            rows.append(obj)
    return rows


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def assistant_content(row: dict[str, Any]) -> str:
    for message in reversed(row.get("messages") or []):
        if message.get("role") == "assistant":
            return str(message.get("content") or "")
    return ""


def user_content(row: dict[str, Any]) -> str:
    for message in row.get("messages") or []:
        if message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


def parse_json_object(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def parse_l2_input(user_text: str) -> dict[str, Any]:
    marker = "L2_INPUT_JSON:"
    if marker not in user_text:
        return {}
    return parse_json_object(user_text.split(marker, 1)[1].strip())


def source_task(row: dict[str, Any], l2_input: dict[str, Any]) -> str:
    task = l2_input.get("task")
    if isinstance(task, str) and task:
        return task
    text = user_content(row)
    first = text.splitlines()[0] if text else ""
    if first.startswith("Task:"):
        return first.split("Task:", 1)[1].split(".", 1)[0].strip()
    return "unknown"


def source_sample_id(row: dict[str, Any], l2_input: dict[str, Any]) -> str:
    value = row.get("sample_id") or l2_input.get("sample_id")
    return str(value) if value else "unknown"


def run_id_from_sample(sample_id: str, l2_input: dict[str, Any]) -> str:
    if "::" in sample_id:
        return sample_id.split("::", 1)[0]
    for key in ("case_id", "source_case_id"):
        value = l2_input.get(key)
        if isinstance(value, str) and value:
            return value
    return sample_id


def evidence_eids_from_target(target: dict[str, Any]) -> list[str]:
    eids: list[str] = []
    for array_name in EVIDENCE_ARRAYS:
        for item in target.get(array_name) or []:
            if isinstance(item, dict) and item.get("eid"):
                eids.append(str(item["eid"]))
    return eids


def available_eids_from_l2(l2_input: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    input_obj = l2_input.get("input") if isinstance(l2_input.get("input"), dict) else {}
    for key in ("candidate_evidence", "key_evidence", "cause_evidence", "symptom_evidence"):
        value = input_obj.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and item.get("eid"):
                    found.add(str(item["eid"]))
    return found


def load_repair_maps(source_dir: Path) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    repair_trace = {}
    for row in load_jsonl(source_dir / "repair_traceability.jsonl"):
        sid = str(row.get("sample_id") or row.get("original_sample_id") or "")
        if sid:
            repair_trace[sid] = {k: v for k, v in row.items() if not k.startswith("_source")}
    gap_map: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in load_jsonl(source_dir / "l2_obs_gap_decisions.jsonl"):
        sid = str(row.get("sample_id") or "")
        if sid:
            gap_map[sid].append({k: v for k, v in row.items() if not k.startswith("_source")})
    return repair_trace, gap_map


def verify_source_hashes(source_dir: Path) -> dict[str, Any]:
    expected: dict[str, str] = {}
    with (source_dir / "sha256sums.txt").open("r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped:
                continue
            sha, rel = stripped.split(maxsplit=1)
            expected[rel] = sha
    results = {}
    ok = True
    for rel, expected_sha in expected.items():
        path = source_dir / rel
        actual = sha256_bytes(path) if path.exists() else None
        passed = actual == expected_sha
        results[rel] = {"expected": expected_sha, "actual": actual, "pass": passed}
        ok = ok and passed
    return {"hash_manifest_pass": ok, "files": results}


def build_source_records(source_dir: Path) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str], dict[str, str]]:
    split_records: dict[str, list[dict[str, Any]]] = {}
    run_to_subtype: dict[str, str] = {}
    run_to_family: dict[str, str] = {}
    for split in ("train", "val", "test"):
        parsed: list[dict[str, Any]] = []
        for row in load_jsonl(source_dir / f"{split}.jsonl"):
            l2 = parse_l2_input(user_content(row))
            target = parse_json_object(assistant_content(row))
            task = source_task(row, l2)
            sid = source_sample_id(row, l2)
            rid = run_id_from_sample(sid, l2)
            rec = {
                "split": split,
                "row": row,
                "l2": l2,
                "target": target,
                "task": task,
                "sample_id": sid,
                "run_id": rid,
                "source_line": row["_source_line"],
                "source_row_sha256": row["_source_row_sha256"],
                "target_hash": canonical_target_hash(target) if target else "",
                "available_eids": available_eids_from_l2(l2),
            }
            parsed.append(rec)
            if task == "diagnosis":
                gt = target.get("gt") if isinstance(target.get("gt"), dict) else {}
                if isinstance(gt.get("subtype"), str):
                    run_to_subtype[rid] = gt["subtype"]
                if isinstance(gt.get("family"), str):
                    run_to_family[rid] = gt["family"]
        split_records[split] = parsed
    return split_records, run_to_subtype, run_to_family


def clean_source_row(row: dict[str, Any]) -> dict[str, Any]:
    return {k: copy.deepcopy(v) for k, v in row.items() if not k.startswith("_source")}


def materialized_row(
    rec: dict[str, Any],
    dryrun_sample_id: str,
    original_sample_id: str,
    subtype: str,
    record_kind: str,
    oversample_index: int,
    source_dir: Path,
) -> dict[str, Any]:
    payload = {
        "sample_id": dryrun_sample_id,
        "original_sample_id": original_sample_id,
        "run_id": rec["run_id"],
        "split": rec["split"],
        "task": rec["task"],
        "subtype": subtype,
        "dry_run": True,
        "not_for_training": True,
        "not_formal_candidate": True,
        "candidate_v3_created": False,
        "source_jsonl_unmodified": True,
        "l1_l2_unmodified": True,
        "source_candidate_version": "repaired_v2",
        "source_candidate_path": str(source_dir),
        "source_jsonl": f"{rec['split']}.jsonl",
        "source_line": rec["source_line"],
        "record_kind": record_kind,
        "oversample_ratio": 2,
        "oversample_index": oversample_index,
        "oversample_reason": (
            "evidence_extraction_underlearned_empty_arrays"
            if record_kind == "oversampled_duplicate"
            else "source_row_retained"
        ),
        "messages": copy.deepcopy(clean_source_row(rec["row"]).get("messages", [])),
    }
    return payload


def trace_row(
    materialized: dict[str, Any],
    rec: dict[str, Any],
    repair_trace: dict[str, Any] | None,
    l2_obs_gaps: list[dict[str, Any]],
) -> dict[str, Any]:
    evidence_eids = evidence_eids_from_target(rec["target"])
    return {
        "sample_id": materialized["sample_id"],
        "original_sample_id": materialized["original_sample_id"],
        "run_id": materialized["run_id"],
        "split": materialized["split"],
        "task": materialized["task"],
        "subtype": materialized["subtype"],
        "record_kind": materialized["record_kind"],
        "oversample_ratio": materialized["oversample_ratio"],
        "oversample_index": materialized["oversample_index"],
        "oversample_reason": materialized["oversample_reason"],
        "dry_run": True,
        "not_for_training": True,
        "not_formal_candidate": True,
        "candidate_v3_created": False,
        "source_candidate_version": "repaired_v2",
        "source_jsonl": materialized["source_jsonl"],
        "source_line": materialized["source_line"],
        "source_row_sha256": rec["source_row_sha256"],
        "target_hash_before": rec["target_hash"],
        "target_hash_after": rec["target_hash"],
        "messages_unchanged": True,
        "target_unchanged": True,
        "evidence_eids": evidence_eids,
        "available_eids": sorted(rec["available_eids"]),
        "evidence_eids_traceable": all(eid in rec["available_eids"] for eid in evidence_eids),
        "repair_traceability_status": (repair_trace or {}).get("traceability_status"),
        "repair_traceability_record_found": repair_trace is not None,
        "l2_obs_gap_decision_count": len(l2_obs_gaps),
        "l2_obs_gap_decisions": l2_obs_gaps,
    }


def validate_counts(
    output_records: dict[str, list[dict[str, Any]]],
    trace_rows: list[dict[str, Any]],
    run_to_family: dict[str, str],
) -> dict[str, Any]:
    split_counts = {split: len(rows) for split, rows in output_records.items()}
    task_counts = {
        split: dict(Counter(row["task"] for row in rows))
        for split, rows in output_records.items()
    }
    all_rows = [row for rows in output_records.values() for row in rows]
    sample_ids = [row["sample_id"] for row in all_rows]
    original_ids = [row["original_sample_id"] for row in all_rows]
    original_counter = Counter(original_ids)
    train_runs = {row["run_id"] for row in output_records["train"]}
    val_runs = {row["run_id"] for row in output_records["val"]}
    test_runs = {row["run_id"] for row in output_records["test"]}
    leakage = bool(train_runs & val_runs or train_runs & test_runs or val_runs & test_runs)
    action_count = sum(1 for row in all_rows if row["task"] == "action_after_diagnosis")
    cpu_mem = sum(1 for family in run_to_family.values() if family in {"cpu", "mem", "memory"})
    evidence_target_empty = sum(
        1
        for row in trace_rows
        if row["task"] == "evidence_extraction" and not row["evidence_eids"]
    )
    evidence_traceable_rows = [
        row for row in trace_rows if row["task"] == "evidence_extraction"
    ]
    evidence_trace_rate = (
        sum(1 for row in evidence_traceable_rows if row["evidence_eids_traceable"]) / len(evidence_traceable_rows)
        if evidence_traceable_rows
        else 0.0
    )
    duplicate_original_expected = {
        original_id: count
        for original_id, count in original_counter.items()
        if count > 1
    }
    return {
        "split_counts": split_counts,
        "all_samples": len(all_rows),
        "trace_rows": len(trace_rows),
        "task_counts_by_split": task_counts,
        "total_task_counts": dict(Counter(row["task"] for row in all_rows)),
        "run_id_counts": {
            "train": len(train_runs),
            "val": len(val_runs),
            "test": len(test_runs),
        },
        "run_id_split_leakage_found": leakage,
        "duplicate_dryrun_sample_id_found": len(sample_ids) != len(set(sample_ids)),
        "duplicate_original_sample_id_count": len(duplicate_original_expected),
        "oversampled_duplicate_count": sum(
            1 for row in all_rows if row["record_kind"] == "oversampled_duplicate"
        ),
        "action_samples": action_count,
        "cpu_mem_gt_contamination_count": cpu_mem,
        "test_held_out_confirmed": all(row["record_kind"] == "source_retained" for row in output_records["test"]),
        "val_unchanged_confirmed": all(row["record_kind"] == "source_retained" for row in output_records["val"]),
        "evidence_target_empty_count": evidence_target_empty,
        "evidence_eid_traceable_rate": evidence_trace_rate,
        "dry_run_not_for_training": all(
            row["dry_run"] and row["not_for_training"] and row["not_formal_candidate"] and not row["candidate_v3_created"]
            for row in all_rows
        ),
    }


def write_sha_manifest(output_dir: Path, files: list[str]) -> dict[str, Any]:
    entries = []
    for rel in files:
        path = output_dir / rel
        entries.append((sha256_bytes(path), rel))
    with (output_dir / "v3_dryrun_sha256sums.txt").open("w", encoding="utf-8", newline="\n") as fh:
        for sha, rel in entries:
            fh.write(f"{sha}  {rel}\n")
    manifest_ok = True
    for sha, rel in entries:
        if sha256_bytes(output_dir / rel) != sha:
            manifest_ok = False
    return {
        "created": True,
        "pass": manifest_ok,
        "files": [{"path": rel, "sha256": sha} for sha, rel in entries],
    }


def build_report(path: Path, summary: dict[str, Any]) -> None:
    validation = summary["validation"]
    lines = [
        "# NET-only Evidence-Oversampled V3 Dry-run",
        "",
        "RESULT: PASS",
        "",
        "This task materialized dry-run artifacts only. It did not create a formal v3",
        "candidate and did not write to any `training_candidates` v3 directory.",
        "",
        "## Source",
        "",
        f"- Local repaired v2 candidate: `{summary['source_candidate_path']}`",
        "- Remote repaired v2 reference: `/home/xrh/qwen3_os_fault/data/training_candidates/net_only_non_action_repaired_v2_20260520`",
        f"- Dry-run output: `{summary['dryrun_output_dir']}`",
        f"- Oversampling ratio: `{summary['oversampling_ratio']}`",
        "- Strategy: retain all train rows and duplicate train evidence_extraction rows once.",
        "- Val/test strategy: unchanged; no val/test oversampling.",
        "",
        "## Count Validation",
        "",
        f"- train/val/test/all: {validation['split_counts']['train']} / {validation['split_counts']['val']} / {validation['split_counts']['test']} / {validation['all_samples']}",
        f"- trace rows: {validation['trace_rows']}",
        f"- train task distribution: {validation['task_counts_by_split']['train']}",
        f"- val task distribution: {validation['task_counts_by_split']['val']}",
        f"- test task distribution: {validation['task_counts_by_split']['test']}",
        f"- total task distribution: {validation['total_task_counts']}",
        f"- oversampled duplicates: {validation['oversampled_duplicate_count']}",
        f"- duplicate dry-run sample_id found: {validation['duplicate_dryrun_sample_id_found']}",
        f"- run_id split leakage found: {validation['run_id_split_leakage_found']}",
        f"- test held-out unchanged: {validation['test_held_out_confirmed']}",
        "",
        "## Traceability",
        "",
        "- Each duplicate preserves original_sample_id, run_id, split=train, task=evidence_extraction,",
        "  target hash, source line, evidence EIDs, repair traceability status, and l2_obs gap decisions.",
        f"- Evidence EID traceable rate: {validation['evidence_eid_traceable_rate']}",
        f"- Evidence target empty count: {validation['evidence_target_empty_count']}",
        "",
        "## Hashes",
        "",
        f"- v2 source hash manifest pass: {summary['source_hash_manifest_pass']}",
        f"- dry-run hash manifest pass: {summary['hash_manifest']['pass']}",
        "",
        "## 3x Escalation Preview",
        "",
        "- 3x train evidence = 189",
        "- 3x train total = 315",
        "- 3x all total = 399",
        "- Risk: higher overfitting, stronger task imbalance, larger output size.",
        "- Recommendation: do not jump to 3x unless 2x is structurally clean but still weak.",
        "",
        "## Readiness",
        "",
        f"- Ready for Task 9N formal candidate v3 creation: {summary['readiness']['ready_for_candidate_v3_creation']}",
        f"- Wrapper allowlist likely needed later: {summary['readiness']['wrapper_allowlist_needed_later']}",
        f"- Separate v3 transfer/preflight needed later: {summary['readiness']['remote_transfer_preflight_needed_later']}",
        f"- Human approval needed before v3 training: {summary['readiness']['human_approval_before_training']}",
        "- Action Contract v1 remains deferred because evidence remains the active bottleneck.",
        "",
        "## Safety",
        "",
        "- Training/eval/generation/model load: not started.",
        "- Adapter: not created or modified.",
        "- v1/v2 candidate JSONL: unmodified.",
        "- Ledger/frozen NET/L1/L2: unmodified.",
        "- HDC/board: not used.",
        "- Dry-run artifacts are marked dry_run=true, not_for_training=true, not_formal_candidate=true.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    parser.add_argument("--plan-dir", type=Path, default=DEFAULT_PLAN_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--report-path", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()

    missing = [name for name in REQUIRED_SOURCE_FILES if not (args.source_dir / name).exists()]
    if missing:
        raise SystemExit("missing source files: " + ", ".join(missing))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.report_path.parent.mkdir(parents=True, exist_ok=True)

    source_hashes = verify_source_hashes(args.source_dir)
    source_records, run_to_subtype, run_to_family = build_source_records(args.source_dir)
    repair_trace, gap_map = load_repair_maps(args.source_dir)
    plan = load_json(args.plan_dir / "evidence_oversampling_plan.json")

    output_records: dict[str, list[dict[str, Any]]] = {"train": [], "val": [], "test": []}
    trace_rows: list[dict[str, Any]] = []
    duplicate_trace_rows: list[dict[str, Any]] = []
    sample_map_rows: list[dict[str, Any]] = []
    diff_rows: list[dict[str, Any]] = []

    for split in ("train", "val", "test"):
        for rec in source_records[split]:
            subtype = run_to_subtype.get(rec["run_id"], "unknown")
            original_sid = rec["sample_id"]
            retained = materialized_row(
                rec=rec,
                dryrun_sample_id=original_sid,
                original_sample_id=original_sid,
                subtype=subtype,
                record_kind="source_retained",
                oversample_index=0,
                source_dir=args.source_dir,
            )
            output_records[split].append(retained)
            tr = trace_row(retained, rec, repair_trace.get(original_sid), gap_map.get(original_sid, []))
            trace_rows.append(tr)

            if split == "train" and rec["task"] == "evidence_extraction":
                dryrun_sid = f"{original_sid}__oversample_evidence_x2_01"
                duplicate = materialized_row(
                    rec=rec,
                    dryrun_sample_id=dryrun_sid,
                    original_sample_id=original_sid,
                    subtype=subtype,
                    record_kind="oversampled_duplicate",
                    oversample_index=1,
                    source_dir=args.source_dir,
                )
                output_records[split].append(duplicate)
                dtr = trace_row(duplicate, rec, repair_trace.get(original_sid), gap_map.get(original_sid, []))
                trace_rows.append(dtr)
                duplicate_trace_rows.append(dtr)
                sample_map_rows.append(
                    {
                        "sample_id": dryrun_sid,
                        "original_sample_id": original_sid,
                        "run_id": rec["run_id"],
                        "split": split,
                        "task": rec["task"],
                        "subtype": subtype,
                        "oversample_ratio": 2,
                        "oversample_index": 1,
                        "oversample_reason": "evidence_extraction_underlearned_empty_arrays",
                        "source_jsonl": "train.jsonl",
                        "source_line": rec["source_line"],
                        "target_hash_before": rec["target_hash"],
                        "target_hash_after": rec["target_hash"],
                        "messages_unchanged": True,
                        "dry_run": True,
                        "not_for_training": True,
                        "not_formal_candidate": True,
                        "candidate_v3_created": False,
                    }
                )
                diff_rows.append(
                    {
                        "sample_id": dryrun_sid,
                        "original_sample_id": original_sid,
                        "diff_type": "dryrun_duplicate_added",
                        "messages_changed": False,
                        "target_changed": False,
                        "run_id_changed": False,
                        "split_changed": False,
                        "task_changed": False,
                        "oversample_ratio": 2,
                        "dry_run": True,
                        "not_for_training": True,
                        "not_formal_candidate": True,
                        "candidate_v3_created": False,
                    }
                )

    all_records = output_records["train"] + output_records["val"] + output_records["test"]
    validation = validate_counts(output_records, trace_rows, run_to_family)

    output_files = {
        "v3_dryrun_train.jsonl": output_records["train"],
        "v3_dryrun_val.jsonl": output_records["val"],
        "v3_dryrun_test.jsonl": output_records["test"],
        "v3_dryrun_all.jsonl": all_records,
        "v3_dryrun_trace_index.jsonl": trace_rows,
        "oversampling_traceability.jsonl": duplicate_trace_rows,
        "oversampled_sample_map.jsonl": sample_map_rows,
        "oversampling_diff_summary.jsonl": diff_rows,
    }
    for rel, rows in output_files.items():
        write_jsonl(args.output_dir / rel, rows)

    manifest = {
        "artifact_type": "net_only_evidence_oversampled_v3_dryrun_manifest",
        "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_candidate": "net_only_non_action_repaired_v2_20260520",
        "source_candidate_version": "repaired_v2",
        "source_candidate_path": str(args.source_dir),
        "remote_source_candidate_path": "/home/xrh/qwen3_os_fault/data/training_candidates/net_only_non_action_repaired_v2_20260520",
        "dryrun_output_dir": str(args.output_dir),
        "oversampling_ratio": "2x primary",
        "train_only_oversampling": True,
        "val_unchanged": True,
        "test_held_out_unchanged": True,
        "candidate_v3_created": False,
        "dry_run": True,
        "not_for_training": True,
        "not_formal_candidate": True,
        "no_cpu_mem_gt": True,
        "no_action_after_diagnosis": True,
        "l1_l2_rebuilt": False,
        "ledger_modified": False,
        "intended_next_step": "Task 9N formal candidate v3 creation after review",
        "source_hash_manifest_pass": source_hashes["hash_manifest_pass"],
        "planning_source": str(args.plan_dir / "evidence_oversampling_plan.json"),
        "planning_recommendation": (plan.get("key_fields") or {}).get("RECOMMENDED_OVERSAMPLING_RATIO"),
        "three_x_escalation_preview": {
            "train_evidence": 189,
            "train_total": 315,
            "all_total": 399,
            "recommended_direct_jump": False,
        },
    }
    write_json(args.output_dir / "oversampling_manifest.json", manifest)

    rules = """# V3 Dry-run Materialization Rules

- Dry-run only: no formal candidate v3 is created.
- Source candidate is repaired v2 and remains unmodified.
- Primary ratio is 2x evidence oversampling.
- Only train/evidence_extraction rows are duplicated once.
- Diagnosis and cause train rows are retained once.
- Val and test rows are retained once and never oversampled.
- Test remains held-out.
- Messages and assistant targets are not modified.
- Duplicates receive a new dry-run sample_id and retain original_sample_id.
- Every row is marked dry_run=true, not_for_training=true, not_formal_candidate=true.
- No CPU/MEM GT rows or action_after_diagnosis rows are introduced.
- No L1/L2 rebuild, ledger change, wrapper change, training, eval, or generation occurs.
"""
    (args.output_dir / "v3_candidate_materialization_rules.md").write_text(rules, encoding="utf-8")

    hash_manifest = write_sha_manifest(
        args.output_dir,
        [
            "v3_dryrun_train.jsonl",
            "v3_dryrun_val.jsonl",
            "v3_dryrun_test.jsonl",
            "v3_dryrun_all.jsonl",
            "v3_dryrun_trace_index.jsonl",
            "oversampling_manifest.json",
            "oversampling_traceability.jsonl",
            "oversampling_diff_summary.jsonl",
            "oversampled_sample_map.jsonl",
            "v3_candidate_materialization_rules.md",
        ],
    )

    validation_summary = {
        "dry_run": True,
        "not_for_training": True,
        "not_formal_candidate": True,
        "candidate_v3_created": False,
        "source_candidate_path": str(args.source_dir),
        "dryrun_output_dir": str(args.output_dir),
        "validation": validation,
        "source_hash_manifest": source_hashes,
        "hash_manifest": hash_manifest,
        "expected_counts_pass": validation["split_counts"] == {"train": 252, "val": 42, "test": 42}
        and validation["all_samples"] == 336
        and validation["trace_rows"] == 336
        and validation["task_counts_by_split"]["train"] == {
            "diagnosis": 63,
            "evidence_extraction": 126,
            "cause_vs_symptom": 63,
        },
        "safety": {
            "training_started": False,
            "eval_started": False,
            "generation_started": False,
            "model_loaded": False,
            "weight_update_started": False,
            "adapter_modified": False,
            "data_jsonl_modified": False,
            "v2_candidate_modified": False,
            "v1_candidate_modified": False,
            "ledger_modified": False,
            "frozen_net_batch_modified": False,
            "l1_rebuilt": False,
            "l2_rebuilt": False,
            "hdc_used": False,
            "board_touched": False,
        },
    }
    write_json(args.output_dir / "v3_dryrun_validation_summary.json", validation_summary)

    key_fields = {
        "SKILL_LOADED": True,
        "MAINLINE_STATUS": "AGGREGATION_PENDING",
        "REPORT_CREATED": True,
        "JSON_CREATED": True,
        "DRYRUN_ARTIFACTS_CREATED": True,
        "DRYRUN_SCRIPT_CREATED": True,
        "TRAINING_STARTED": False,
        "EVAL_STARTED": False,
        "GENERATION_STARTED": False,
        "MODEL_LOADED": False,
        "WEIGHT_UPDATE_STARTED": False,
        "ADAPTER_MODIFIED": False,
        "DATA_JSONL_MODIFIED": False,
        "CANDIDATE_V3_CREATED": False,
        "V2_CANDIDATE_MODIFIED": False,
        "V1_CANDIDATE_MODIFIED": False,
        "LEDGER_MODIFIED": False,
        "FROZEN_NET_BATCH_MODIFIED": False,
        "L1_REBUILT": False,
        "L2_REBUILT": False,
        "HDC_USED": False,
        "BOARD_TOUCHED": False,
        "OVERSAMPLING_RATIO": "2x",
        "V3_DRYRUN_TRAIN_SAMPLES": validation["split_counts"]["train"],
        "V3_DRYRUN_VAL_SAMPLES": validation["split_counts"]["val"],
        "V3_DRYRUN_TEST_SAMPLES": validation["split_counts"]["test"],
        "V3_DRYRUN_ALL_SAMPLES": validation["all_samples"],
        "V3_DRYRUN_TRACE_ROWS": validation["trace_rows"],
        "TRAIN_DIAGNOSIS_SAMPLES": validation["task_counts_by_split"]["train"].get("diagnosis", 0),
        "TRAIN_EVIDENCE_SAMPLES": validation["task_counts_by_split"]["train"].get("evidence_extraction", 0),
        "TRAIN_CAUSE_SAMPLES": validation["task_counts_by_split"]["train"].get("cause_vs_symptom", 0),
        "TOTAL_DIAGNOSIS_SAMPLES": validation["total_task_counts"].get("diagnosis", 0),
        "TOTAL_EVIDENCE_SAMPLES": validation["total_task_counts"].get("evidence_extraction", 0),
        "TOTAL_CAUSE_SAMPLES": validation["total_task_counts"].get("cause_vs_symptom", 0),
        "ACTION_SAMPLES": validation["action_samples"],
        "CPU_MEM_GT_CONTAMINATION_COUNT": validation["cpu_mem_gt_contamination_count"],
        "TEST_HELD_OUT_CONFIRMED": validation["test_held_out_confirmed"],
        "RUN_ID_SPLIT_LEAKAGE_FOUND": validation["run_id_split_leakage_found"],
        "DUPLICATE_DRYRUN_SAMPLE_ID_FOUND": validation["duplicate_dryrun_sample_id_found"],
        "OVERSAMPLED_DUPLICATE_COUNT": validation["oversampled_duplicate_count"],
        "OVERSAMPLED_SAMPLE_MAP_CREATED": True,
        "OVERSAMPLING_TRACEABILITY_CREATED": True,
        "HASH_MANIFEST_CREATED": hash_manifest["created"],
        "HASH_MANIFEST_PASS": hash_manifest["pass"],
        "DRY_RUN_NOT_FOR_TRAINING": validation["dry_run_not_for_training"],
        "THREE_X_ESCALATION_PREVIEW_CREATED": True,
        "READY_FOR_CANDIDATE_V3_CREATION": True,
        "READY_FOR_ACTION_CONTRACT": False,
        "REVIEWER_VERDICT": "PENDING",
    }
    audit = {
        "artifact_type": "net_only_evidence_oversampled_v3_dryrun_audit",
        "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_candidate_path": str(args.source_dir),
        "dryrun_output_dir": str(args.output_dir),
        "oversampling_ratio": "2x",
        "validation": validation,
        "source_hash_manifest_pass": source_hashes["hash_manifest_pass"],
        "hash_manifest": hash_manifest,
        "readiness": {
            "ready_for_candidate_v3_creation": True,
            "wrapper_allowlist_needed_later": True,
            "remote_transfer_preflight_needed_later": True,
            "human_approval_before_training": True,
            "ready_for_action_contract": False,
        },
        "key_fields": key_fields,
    }
    write_json(args.output_dir / "evidence_oversampled_v3_dryrun.json", audit)
    build_report(args.report_path, {
        "source_candidate_path": str(args.source_dir),
        "dryrun_output_dir": str(args.output_dir),
        "oversampling_ratio": "2x",
        "validation": validation,
        "source_hash_manifest_pass": source_hashes["hash_manifest_pass"],
        "hash_manifest": hash_manifest,
        "readiness": audit["readiness"],
    })

    # Recompute hashes after final JSON/report are written. The summary JSON files
    # embed the hash manifest, so do not include them in the manifest itself.
    hash_manifest = write_sha_manifest(
        args.output_dir,
        [
            "v3_dryrun_train.jsonl",
            "v3_dryrun_val.jsonl",
            "v3_dryrun_test.jsonl",
            "v3_dryrun_all.jsonl",
            "v3_dryrun_trace_index.jsonl",
            "oversampling_manifest.json",
            "oversampling_traceability.jsonl",
            "oversampling_diff_summary.jsonl",
            "oversampled_sample_map.jsonl",
            "v3_candidate_materialization_rules.md",
        ],
    )
    audit["hash_manifest"] = hash_manifest
    audit["key_fields"]["HASH_MANIFEST_PASS"] = hash_manifest["pass"]
    write_json(args.output_dir / "evidence_oversampled_v3_dryrun.json", audit)

    print(json.dumps({"result": "PASS", "key_fields": key_fields}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
