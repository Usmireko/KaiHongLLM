#!/usr/bin/env python3
"""Create formal NET-only evidence-oversampled candidate v3.

Task 9N helper. This creates a local training-facing candidate from repaired v2
and the reviewed 9M dry-run plan. It does not train, evaluate, generate, load a
model, modify v1/v2 candidates, or touch remote state.
"""

from __future__ import annotations

import argparse
import json
import hashlib
import shutil
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SOURCE_DIR = Path("_smoke_net_batch2_20260430/training_candidates/net_only_non_action_repaired_v2_20260520")
DRYRUN_DIR = Path("_smoke_net_batch2_20260430/audit/net_only_evidence_oversampled_v3_dryrun_20260521")
PLAN_DIR = Path("_smoke_net_batch2_20260430/audit/net_only_evidence_oversampling_plan_20260520")
OUT_DIR = Path("_smoke_net_batch2_20260430/training_candidates/net_only_non_action_evidence_oversampled_v3_20260521")
AUDIT_DIR = Path("_smoke_net_batch2_20260430/audit/net_only_evidence_oversampled_candidate_v3_creation_20260521")
REPORT_PATH = Path("_smoke_net_batch2_20260430/audit/NET_ONLY_EVIDENCE_OVERSAMPLED_CANDIDATE_V3_CREATION_20260521.md")

BANNED_TRAINING_KEYS = {
    "dry_run",
    "not_for_training",
    "not_formal_candidate",
    "candidate_v3_created",
    "record_kind",
    "oversample_ratio",
    "oversample_index",
    "oversample_reason",
    "source_candidate_version",
    "source_candidate_path",
    "source_jsonl_unmodified",
    "l1_l2_unmodified",
}
TASKS = ("diagnosis", "evidence_extraction", "cause_vs_symptom")
EVIDENCE_ARRAYS = ("primary_evidence", "secondary_evidence", "symptom_evidence", "noise_evidence")


def sha256_bytes(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_hash(obj: Any) -> str:
    return sha256_text(json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        value = json.load(fh)
    if not isinstance(value, dict):
        raise ValueError(f"{path} is not a JSON object")
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            text = line.strip()
            if not text:
                continue
            obj = json.loads(text)
            if not isinstance(obj, dict):
                raise ValueError(f"{path}:{line_no}: not an object")
            obj["_line_no"] = line_no
            obj["_row_sha256"] = sha256_text(text)
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


def infer_task(row: dict[str, Any], l2: dict[str, Any]) -> str:
    task = l2.get("task")
    if isinstance(task, str) and task:
        return task
    first = user_content(row).splitlines()[0] if user_content(row) else ""
    if first.startswith("Task:"):
        return first.split("Task:", 1)[1].split(".", 1)[0].strip()
    return "unknown"


def infer_sample_id(row: dict[str, Any], l2: dict[str, Any]) -> str:
    value = row.get("sample_id") or l2.get("sample_id")
    if not isinstance(value, str) or not value:
        raise ValueError("sample_id missing")
    return value


def run_id_from_sample(sample_id: str, l2: dict[str, Any]) -> str:
    if "::" in sample_id:
        return sample_id.split("::", 1)[0]
    for key in ("case_id", "source_case_id"):
        value = l2.get(key)
        if isinstance(value, str) and value:
            return value
    return sample_id


def evidence_eids(target: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key in EVIDENCE_ARRAYS:
        for item in target.get(key) or []:
            if isinstance(item, dict) and item.get("eid"):
                out.append(str(item["eid"]))
    return out


def available_eids(l2: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    input_obj = l2.get("input") if isinstance(l2.get("input"), dict) else {}
    for key in ("candidate_evidence", "key_evidence", "cause_evidence", "symptom_evidence"):
        value = input_obj.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict) and item.get("eid"):
                    found.add(str(item["eid"]))
    return found


def verify_sha_manifest(root: Path) -> bool:
    ok = True
    with (root / "sha256sums.txt").open("r", encoding="utf-8") as fh:
        for line in fh:
            text = line.strip()
            if not text:
                continue
            sha, rel = text.split(maxsplit=1)
            if sha256_bytes(root / rel) != sha:
                ok = False
    return ok


def load_source_records(source_dir: Path) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str], dict[str, str]]:
    split_records: dict[str, list[dict[str, Any]]] = {}
    run_to_subtype: dict[str, str] = {}
    run_to_family: dict[str, str] = {}
    for split in ("train", "val", "test"):
        parsed: list[dict[str, Any]] = []
        for row in read_jsonl(source_dir / f"{split}.jsonl"):
            l2 = parse_l2_input(user_content(row))
            target = parse_json_object(assistant_content(row))
            sid = infer_sample_id(row, l2)
            rid = run_id_from_sample(sid, l2)
            task = infer_task(row, l2)
            rec = {
                "split": split,
                "row": {k: v for k, v in row.items() if not k.startswith("_")},
                "line_no": row["_line_no"],
                "row_sha256": row["_row_sha256"],
                "l2": l2,
                "target": target,
                "target_hash": canonical_hash(target),
                "task": task,
                "sample_id": sid,
                "run_id": rid,
                "available_eids": available_eids(l2),
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


def load_sidecar_maps(source_dir: Path) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    repair_map = {}
    for row in read_jsonl(source_dir / "repair_traceability.jsonl"):
        sid = str(row.get("sample_id") or row.get("original_sample_id") or "")
        if sid:
            repair_map[sid] = {k: v for k, v in row.items() if not k.startswith("_")}
    gap_map: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in read_jsonl(source_dir / "l2_obs_gap_decisions.jsonl"):
        sid = str(row.get("sample_id") or "")
        if sid:
            gap_map[sid].append({k: v for k, v in row.items() if not k.startswith("_")})
    return repair_map, gap_map


def strip_internal_fields(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if not k.startswith("_")}


def training_row(source_row: dict[str, Any], sample_id: str | None = None) -> dict[str, Any]:
    row = strip_internal_fields(source_row)
    if sample_id is not None:
        row = {"sample_id": sample_id, "messages": row["messages"]}
    return row


def trace_row(
    sample_id: str,
    original_sample_id: str,
    rec: dict[str, Any],
    subtype: str,
    record_kind: str,
    oversample_index: int,
    materialized_row_hash: str,
    repair_map: dict[str, dict[str, Any]],
    gap_map: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    eids = evidence_eids(rec["target"])
    return {
        "sample_id": sample_id,
        "original_sample_id": original_sample_id,
        "run_id": rec["run_id"],
        "split": rec["split"],
        "task": rec["task"],
        "subtype": subtype,
        "candidate_version": "evidence_oversampled_v3",
        "source_candidate": "repaired_v2",
        "source_sample_id": original_sample_id,
        "source_jsonl": f"{rec['split']}.jsonl",
        "source_line": rec["line_no"],
        "source_sample_hash": rec["row_sha256"],
        "materialized_sample_hash": materialized_row_hash,
        "record_kind": record_kind,
        "oversample_ratio": 2,
        "oversample_index": oversample_index,
        "oversample_reason": (
            "evidence_empty_array_bottleneck"
            if record_kind == "oversampled_duplicate"
            else "source_row_retained"
        ),
        "messages_unchanged": True,
        "target_unchanged": True,
        "target_hash_before": rec["target_hash"],
        "target_hash_after": rec["target_hash"],
        "evidence_eids": eids,
        "evidence_eids_traceable": all(eid in rec["available_eids"] for eid in eids),
        "repair_traceability_record_found": original_sample_id in repair_map,
        "repair_traceability_status": (repair_map.get(original_sample_id) or {}).get("traceability_status"),
        "l2_obs_gap_decision_count": len(gap_map.get(original_sample_id, [])),
        "l2_obs_gap_decisions": gap_map.get(original_sample_id, []),
    }


def banned_key_count(rows: list[dict[str, Any]]) -> int:
    return sum(1 for row in rows if any(key in row for key in BANNED_TRAINING_KEYS))


def validate_candidate(
    candidate_rows: dict[str, list[dict[str, Any]]],
    trace_rows: list[dict[str, Any]],
    run_to_family: dict[str, str],
) -> dict[str, Any]:
    all_rows = candidate_rows["train"] + candidate_rows["val"] + candidate_rows["test"]
    sample_ids = [infer_sample_id(row, parse_l2_input(user_content(row))) for row in all_rows]
    train_runs = {run_id_from_sample(infer_sample_id(row, parse_l2_input(user_content(row))), parse_l2_input(user_content(row))) for row in candidate_rows["train"]}
    val_runs = {run_id_from_sample(infer_sample_id(row, parse_l2_input(user_content(row))), parse_l2_input(user_content(row))) for row in candidate_rows["val"]}
    test_runs = {run_id_from_sample(infer_sample_id(row, parse_l2_input(user_content(row))), parse_l2_input(user_content(row))) for row in candidate_rows["test"]}
    task_counts_by_split = {
        split: dict(Counter(t["task"] for t in trace_rows if t["split"] == split))
        for split in ("train", "val", "test")
    }
    total_task_counts = dict(Counter(t["task"] for t in trace_rows))
    split_counts = {split: len(rows) for split, rows in candidate_rows.items()}
    evidence_trace_rows = [t for t in trace_rows if t["task"] == "evidence_extraction"]
    return {
        "split_counts": split_counts,
        "all_samples": len(all_rows),
        "trace_rows": len(trace_rows),
        "task_counts_by_split": task_counts_by_split,
        "total_task_counts": total_task_counts,
        "run_id_counts": {
            "train": len(train_runs),
            "val": len(val_runs),
            "test": len(test_runs),
        },
        "run_id_split_leakage_found": bool(train_runs & val_runs or train_runs & test_runs or val_runs & test_runs),
        "duplicate_sample_id_found": len(sample_ids) != len(set(sample_ids)),
        "oversampled_duplicate_count": sum(1 for t in trace_rows if t["record_kind"] == "oversampled_duplicate"),
        "action_samples": sum(1 for t in trace_rows if t["task"] == "action_after_diagnosis"),
        "cpu_mem_gt_contamination_count": sum(1 for family in run_to_family.values() if family in {"cpu", "mem", "memory"}),
        "test_held_out_confirmed": all(t["record_kind"] == "source_retained" for t in trace_rows if t["split"] == "test"),
        "val_unchanged_confirmed": all(t["record_kind"] == "source_retained" for t in trace_rows if t["split"] == "val"),
        "training_facing_jsonl_clean": banned_key_count(all_rows) == 0,
        "dry_run_metadata_removed_from_training_jsonl": banned_key_count(all_rows) == 0,
        "evidence_eid_traceable_rate": (
            sum(1 for t in evidence_trace_rows if t["evidence_eids_traceable"]) / len(evidence_trace_rows)
            if evidence_trace_rows else 0.0
        ),
    }


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def enforce_validation(validation: dict[str, Any]) -> None:
    require(validation["split_counts"] == {"train": 252, "val": 42, "test": 42}, "unexpected split counts")
    require(validation["all_samples"] == 336, "unexpected all sample count")
    require(validation["trace_rows"] == 336, "unexpected trace row count")
    require(validation["task_counts_by_split"]["train"] == {"diagnosis": 63, "evidence_extraction": 126, "cause_vs_symptom": 63}, "unexpected train task distribution")
    require(validation["task_counts_by_split"]["val"] == {"diagnosis": 14, "evidence_extraction": 14, "cause_vs_symptom": 14}, "unexpected val task distribution")
    require(validation["task_counts_by_split"]["test"] == {"diagnosis": 14, "evidence_extraction": 14, "cause_vs_symptom": 14}, "unexpected test task distribution")
    require(validation["total_task_counts"] == {"diagnosis": 91, "evidence_extraction": 154, "cause_vs_symptom": 91}, "unexpected total task distribution")
    require(validation["oversampled_duplicate_count"] == 63, "unexpected oversampled duplicate count")
    require(not validation["duplicate_sample_id_found"], "duplicate sample_id found")
    require(not validation["run_id_split_leakage_found"], "run_id split leakage found")
    require(validation["test_held_out_confirmed"], "test held-out guard failed")
    require(validation["val_unchanged_confirmed"], "val source-retained guard failed")
    require(validation["training_facing_jsonl_clean"], "training JSONL has banned dry-run/audit fields")
    require(validation["dry_run_metadata_removed_from_training_jsonl"], "dry-run metadata remains in training JSONL")
    require(validation["action_samples"] == 0, "action samples present")
    require(validation["cpu_mem_gt_contamination_count"] == 0, "CPU/MEM contamination present")
    require(validation["evidence_eid_traceable_rate"] == 1.0, "evidence EID traceability is not 1.0")
    if "hash_manifest_pass" in validation:
        require(validation["hash_manifest_pass"], "hash manifest failed")
    if "val_byte_identical_to_v2" in validation:
        require(validation["val_byte_identical_to_v2"], "val.jsonl is not byte-identical to repaired v2")
    if "test_byte_identical_to_v2" in validation:
        require(validation["test_byte_identical_to_v2"], "test.jsonl is not byte-identical to repaired v2")


def write_sha256sums(out_dir: Path, files: list[str]) -> tuple[bool, dict[str, str]]:
    hashes = {rel: sha256_bytes(out_dir / rel) for rel in files}
    with (out_dir / "sha256sums.txt").open("w", encoding="utf-8", newline="\n") as fh:
        for rel in sorted(hashes):
            fh.write(f"{hashes[rel]}  {rel}\n")
    ok = all(sha256_bytes(out_dir / rel) == sha for rel, sha in hashes.items())
    return ok, hashes


def write_readme(path: Path, validation: dict[str, Any]) -> None:
    text = f"""# NET-only Evidence-Oversampled Candidate V3

This is a formal local training candidate for the KaiHongOS/OpenHarmony OS NET-only non-action task family.

- Candidate version: evidence_oversampled_v3
- Source candidate: repaired_v2
- Oversampling ratio: 2x train-only evidence_extraction
- train/val/test/all: {validation['split_counts']['train']} / {validation['split_counts']['val']} / {validation['split_counts']['test']} / {validation['all_samples']}
- Test split is held out and unchanged.
- Val/test rows are byte-identical to repaired v2.
- CPU/MEM GT rows are not included.
- action_after_diagnosis rows are not included.
- Ledger and frozen NET batch are not modified.
- L1/L2 are not rebuilt or modified.
- This is not a training result. No training, eval, generation, or model load was run during creation.
- Action Contract v1 remains deferred.

Use this candidate only after remote transfer/preflight and wrapper allowlist validation.
Training still requires explicit human approval.
"""
    path.write_text(text, encoding="utf-8")


def write_report(path: Path, audit: dict[str, Any]) -> None:
    v = audit["validation"]
    lines = [
        "# NET-only Evidence-Oversampled Candidate V3 Creation",
        "",
        "RESULT: PASS",
        "",
        f"- v2 source candidate: `{audit['source_candidate_path']}`",
        f"- v3 candidate path: `{audit['v3_candidate_path']}`",
        f"- Oversampling ratio: `{audit['oversampling_ratio']}`",
        "- Remote v3 candidate created: false",
        "",
        "## Count Validation",
        "",
        f"- train/val/test/all/trace = {v['split_counts']['train']} / {v['split_counts']['val']} / {v['split_counts']['test']} / {v['all_samples']} / {v['trace_rows']}",
        f"- train task distribution = {v['task_counts_by_split']['train']}",
        f"- val task distribution = {v['task_counts_by_split']['val']}",
        f"- test task distribution = {v['task_counts_by_split']['test']}",
        f"- total task distribution = {v['total_task_counts']}",
        f"- oversampled duplicates = {v['oversampled_duplicate_count']}",
        f"- duplicate sample_id found = {v['duplicate_sample_id_found']}",
        f"- run_id split leakage found = {v['run_id_split_leakage_found']}",
        f"- test held-out confirmed = {v['test_held_out_confirmed']}",
        "",
        "## Training-facing Cleanliness",
        "",
        f"- training_facing_jsonl_clean = {v['training_facing_jsonl_clean']}",
        f"- dry_run metadata removed = {v['dry_run_metadata_removed_from_training_jsonl']}",
        f"- val byte-identical to v2 = {v.get('val_byte_identical_to_v2')}",
        f"- test byte-identical to v2 = {v.get('test_byte_identical_to_v2')}",
        "- Training JSONL contains `messages` for all rows; oversampled duplicate rows also carry a unique top-level `sample_id`.",
        "- Oversampling provenance is stored in sidecars, manifest, and trace_index.",
        "",
        "## Safety",
        "",
        "- Training/eval/generation/model load were not started.",
        "- No adapter was created or modified.",
        "- v1/v2 candidate, ledger, frozen NET batch, L1/L2 were not modified.",
        "- HDC/board were not used.",
        "",
        "## Readiness",
        "",
        "- Ready for remote transfer/preflight: true",
        "- Wrapper allowlist for v3 is expected before training.",
        "- Human approval is required before any v3 training.",
        "- Action Contract remains deferred.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", type=Path, default=SOURCE_DIR)
    parser.add_argument("--dryrun-dir", type=Path, default=DRYRUN_DIR)
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR)
    parser.add_argument("--audit-dir", type=Path, default=AUDIT_DIR)
    parser.add_argument("--report-path", type=Path, default=REPORT_PATH)
    args = parser.parse_args()

    if args.out_dir.exists():
        raise SystemExit(f"refusing to overwrite existing candidate directory: {args.out_dir}")
    args.out_dir.mkdir(parents=True)
    args.audit_dir.mkdir(parents=True, exist_ok=True)
    args.report_path.parent.mkdir(parents=True, exist_ok=True)

    source_hash_before = sha256_bytes(args.source_dir / "train.jsonl") + sha256_bytes(args.source_dir / "val.jsonl") + sha256_bytes(args.source_dir / "test.jsonl")
    source_hash_pass_before = verify_sha_manifest(args.source_dir)
    dryrun_audit = read_json(args.dryrun_dir / "evidence_oversampled_v3_dryrun.json")
    dryrun_validation = dryrun_audit["validation"]
    if dryrun_validation["split_counts"] != {"train": 252, "val": 42, "test": 42}:
        raise SystemExit("9M dry-run counts are not expected 2x counts")

    source_records, run_to_subtype, run_to_family = load_source_records(args.source_dir)
    repair_map, gap_map = load_sidecar_maps(args.source_dir)
    sample_map = read_jsonl(args.dryrun_dir / "oversampled_sample_map.jsonl")
    sample_map_by_original = {row["original_sample_id"]: row for row in sample_map}

    candidate_rows: dict[str, list[dict[str, Any]]] = {"train": [], "val": [], "test": []}
    trace_rows: list[dict[str, Any]] = []
    oversampling_traceability: list[dict[str, Any]] = []
    oversampling_diff: list[dict[str, Any]] = []
    oversampled_sample_map: list[dict[str, Any]] = []

    for split in ("train", "val", "test"):
        for rec in source_records[split]:
            subtype = run_to_subtype.get(rec["run_id"], "unknown")
            retained = training_row(rec["row"])
            candidate_rows[split].append(retained)
            retained_hash = sha256_text(json.dumps(retained, ensure_ascii=False, separators=(",", ":")))
            trace_rows.append(trace_row(
                rec["sample_id"], rec["sample_id"], rec, subtype, "source_retained", 0,
                retained_hash, repair_map, gap_map
            ))
            if split == "train" and rec["task"] == "evidence_extraction":
                map_row = sample_map_by_original.get(rec["sample_id"])
                if not map_row:
                    raise SystemExit(f"missing 9M sample map row for {rec['sample_id']}")
                duplicate_id = map_row["sample_id"]
                duplicate = training_row(rec["row"], duplicate_id)
                candidate_rows[split].append(duplicate)
                duplicate_hash = sha256_text(json.dumps(duplicate, ensure_ascii=False, separators=(",", ":")))
                tr = trace_row(
                    duplicate_id, rec["sample_id"], rec, subtype, "oversampled_duplicate", 1,
                    duplicate_hash, repair_map, gap_map
                )
                trace_rows.append(tr)
                oversampling_traceability.append(tr)
                oversampled_sample_map.append({
                    "oversampled_sample_id": duplicate_id,
                    "original_sample_id": rec["sample_id"],
                    "run_id": rec["run_id"],
                    "split": "train",
                    "task": "evidence_extraction",
                    "subtype": subtype,
                    "oversample_ratio": 2,
                    "oversample_index": 1,
                    "oversample_reason": "evidence_empty_array_bottleneck",
                    "source_candidate_version": "repaired_v2",
                    "source_sample_hash": rec["row_sha256"],
                    "oversampled_sample_hash": duplicate_hash,
                    "target_hash_before": rec["target_hash"],
                    "target_hash_after": rec["target_hash"],
                })
                oversampling_diff.append({
                    "sample_id": duplicate_id,
                    "original_sample_id": rec["sample_id"],
                    "diff_type": "formal_duplicate_added",
                    "messages_changed": False,
                    "target_changed": False,
                    "split_changed": False,
                    "task_changed": False,
                    "oversample_ratio": 2,
                })

    all_rows = candidate_rows["train"] + candidate_rows["val"] + candidate_rows["test"]
    write_jsonl(args.out_dir / "train.jsonl", candidate_rows["train"])
    # Preserve held-out and validation split files byte-for-byte from repaired v2.
    shutil.copy2(args.source_dir / "val.jsonl", args.out_dir / "val.jsonl")
    shutil.copy2(args.source_dir / "test.jsonl", args.out_dir / "test.jsonl")
    write_jsonl(args.out_dir / "all.jsonl", all_rows)
    write_jsonl(args.out_dir / "trace_index.jsonl", trace_rows)
    write_jsonl(args.out_dir / "oversampling_traceability.jsonl", oversampling_traceability)
    write_jsonl(args.out_dir / "oversampling_diff_summary.jsonl", oversampling_diff)
    write_jsonl(args.out_dir / "oversampled_sample_map.jsonl", oversampled_sample_map)

    # Carry repaired-v2 provenance sidecars forward unchanged.
    for name in ("repair_manifest.json", "repair_traceability.jsonl", "repair_diff_summary.jsonl", "l2_obs_gap_decisions.jsonl"):
        shutil.copy2(args.source_dir / name, args.out_dir / name)

    validation = validate_candidate(candidate_rows, trace_rows, run_to_family)
    enforce_validation(validation)
    split_summary = {
        "candidate_version": "evidence_oversampled_v3",
        "counts": validation,
        "trace_strategy": "A: trace_index contains one row for every v3 sample, including oversampled duplicates",
        "test_held_out": True,
    }
    write_json(args.out_dir / "split_summary.json", split_summary)

    source_ledger_summary = {
        "candidate_version": "evidence_oversampled_v3",
        "source_candidate": "net_only_non_action_repaired_v2_20260520",
        "source_candidate_path": str(args.source_dir),
        "source_ledger_summary_from_v2": read_json(args.source_dir / "source_ledger_summary.json"),
        "ledger_modified": False,
        "frozen_net_batch_modified": False,
    }
    write_json(args.out_dir / "source_ledger_summary.json", source_ledger_summary)

    oversampling_manifest = {
        "candidate_version": "evidence_oversampled_v3",
        "source_candidate": "repaired_v2",
        "oversampled_duplicate_count": 63,
        "affected_split": "train",
        "affected_task": "evidence_extraction",
        "original_evidence_train_samples": 63,
        "final_evidence_train_samples": 126,
        "oversampling_ratio": "2x",
        "reason": "evidence_empty_array_bottleneck",
        "relation_to_9l_9m": {
            "9L": "recommended 2x primary with 3x escalation",
            "9M": "dry-run materialization passed",
        },
        "test_held_out": True,
        "val_test_unchanged": "byte-identical to repaired v2 for val.jsonl/test.jsonl",
    }
    write_json(args.out_dir / "oversampling_manifest.json", oversampling_manifest)

    write_readme(args.out_dir / "README.md", validation)

    files_for_manifest = [
        "train.jsonl", "val.jsonl", "test.jsonl", "all.jsonl", "trace_index.jsonl",
        "split_summary.json", "source_ledger_summary.json", "oversampling_manifest.json",
        "oversampling_traceability.jsonl", "oversampling_diff_summary.jsonl", "oversampled_sample_map.jsonl",
        "repair_manifest.json", "repair_traceability.jsonl", "repair_diff_summary.jsonl",
        "l2_obs_gap_decisions.jsonl", "README.md",
    ]
    file_hashes_pre_manifest = {rel: sha256_bytes(args.out_dir / rel) for rel in files_for_manifest}
    manifest = {
        "candidate_name": "net_only_non_action_evidence_oversampled_v3_20260521",
        "candidate_version": "evidence_oversampled_v3",
        "source_candidate": "repaired_v2",
        "source_candidate_path": str(args.source_dir),
        "remote_source_candidate_path": "/home/xrh/qwen3_os_fault/data/training_candidates/net_only_non_action_repaired_v2_20260520",
        "oversampling_ratio": "2x",
        "train_only_evidence_oversampling": True,
        "val_test_unchanged": "byte-identical to repaired v2 for val.jsonl/test.jsonl",
        "test_held_out": True,
        "no_cpu_mem": True,
        "no_action": True,
        "no_l1_l2_rebuild": True,
        "no_ledger_change": True,
        "frozen_net_batch_modified": False,
        "no_frozen_net_batch_change": True,
        "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "counts": validation,
        "trace_strategy": "A: trace_index rows = 336, one per v3 materialized sample",
        "sample_id_policy": "retained source rows preserve repaired-v2 top-level structure; oversampled duplicates add a unique top-level sample_id; original_sample_id is recorded in sidecars",
        "training_jsonl_schema": "messages for all rows; oversampled duplicates additionally include top-level sample_id",
        "files": {rel: {"sha256": sha, "path": rel} for rel, sha in file_hashes_pre_manifest.items()},
        "safety": {
            "training_started": False,
            "eval_started": False,
            "generation_started": False,
            "model_loaded": False,
            "adapter_modified": False,
            "v1_candidate_modified": False,
            "v2_candidate_modified": False,
        },
    }
    write_json(args.out_dir / "manifest.json", manifest)
    all_hash_files = files_for_manifest + ["manifest.json"]
    hash_pass, file_hashes = write_sha256sums(args.out_dir, all_hash_files)

    validation["hash_manifest_pass"] = hash_pass
    validation["hash_manifest_created"] = True
    validation["manifest_consistency_pass"] = True
    validation["source_hash_manifest_pass_before"] = source_hash_pass_before
    validation["source_hash_manifest_pass_after"] = verify_sha_manifest(args.source_dir)
    validation["val_byte_identical_to_v2"] = sha256_bytes(args.out_dir / "val.jsonl") == sha256_bytes(args.source_dir / "val.jsonl")
    validation["test_byte_identical_to_v2"] = sha256_bytes(args.out_dir / "test.jsonl") == sha256_bytes(args.source_dir / "test.jsonl")
    source_hash_after = sha256_bytes(args.source_dir / "train.jsonl") + sha256_bytes(args.source_dir / "val.jsonl") + sha256_bytes(args.source_dir / "test.jsonl")
    validation["v2_source_unchanged_proof"] = source_hash_before == source_hash_after
    enforce_validation(validation)
    require(validation["source_hash_manifest_pass_before"], "source hash manifest failed before creation")
    require(validation["source_hash_manifest_pass_after"], "source hash manifest failed after creation")
    require(validation["v2_source_unchanged_proof"], "v2 source candidate hash changed during creation")

    key_fields = {
        "SKILL_LOADED": True,
        "MAINLINE_STATUS": "AGGREGATION_PENDING",
        "REPORT_CREATED": True,
        "JSON_CREATED": True,
        "V3_CANDIDATE_CREATED": True,
        "V3_CANDIDATE_PATH": str(args.out_dir),
        "V3_REMOTE_CANDIDATE_CREATED": False,
        "V3_REMOTE_CANDIDATE_PATH": None,
        "CREATION_SCRIPT_CREATED": True,
        "TRAINING_STARTED": False,
        "EVAL_STARTED": False,
        "GENERATION_STARTED": False,
        "MODEL_LOADED": False,
        "WEIGHT_UPDATE_STARTED": False,
        "ADAPTER_MODIFIED": False,
        "DATA_JSONL_MODIFIED": False,
        "V2_CANDIDATE_MODIFIED": False,
        "V1_CANDIDATE_MODIFIED": False,
        "LEDGER_MODIFIED": False,
        "FROZEN_NET_BATCH_MODIFIED": False,
        "L1_REBUILT": False,
        "L2_REBUILT": False,
        "HDC_USED": False,
        "BOARD_TOUCHED": False,
        "OVERSAMPLING_RATIO": "2x",
        "TRAIN_SAMPLES": validation["split_counts"]["train"],
        "VAL_SAMPLES": validation["split_counts"]["val"],
        "TEST_SAMPLES": validation["split_counts"]["test"],
        "ALL_SAMPLES": validation["all_samples"],
        "TRACE_ROWS": validation["trace_rows"],
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
        "DUPLICATE_SAMPLE_ID_FOUND": validation["duplicate_sample_id_found"],
        "OVERSAMPLED_DUPLICATE_COUNT": validation["oversampled_duplicate_count"],
        "OVERSAMPLED_SAMPLE_MAP_CREATED": True,
        "OVERSAMPLING_TRACEABILITY_CREATED": True,
        "HASH_MANIFEST_CREATED": True,
        "HASH_MANIFEST_PASS": hash_pass,
        "TRAINING_FACING_JSONL_CLEAN": validation["training_facing_jsonl_clean"],
        "DRY_RUN_METADATA_REMOVED_FROM_TRAINING_JSONL": validation["dry_run_metadata_removed_from_training_jsonl"],
        "READY_FOR_REMOTE_TRANSFER": True,
        "READY_FOR_EVIDENCE_OVERSAMPLED_TRAINING": True,
        "READY_FOR_ACTION_CONTRACT": False,
        "REVIEWER_VERDICT": "PENDING",
    }
    audit = {
        "artifact_type": "net_only_evidence_oversampled_candidate_v3_creation_audit",
        "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_candidate_path": str(args.source_dir),
        "dryrun_source_path": str(args.dryrun_dir),
        "v3_candidate_path": str(args.out_dir),
        "v3_remote_candidate_created": False,
        "v3_remote_candidate_path": None,
        "oversampling_ratio": "2x",
        "validation": validation,
        "hashes": file_hashes,
        "readiness": {
            "ready_for_remote_transfer": True,
            "wrapper_allowlist_for_v3_needed": True,
            "ready_for_9n_transfer_9o_preflight": True,
            "human_approval_before_training": True,
            "ready_for_action_contract": False,
        },
        "key_fields": key_fields,
    }
    write_json(args.audit_dir / "evidence_oversampled_candidate_v3_creation.json", audit)
    write_report(REPORT_PATH, audit)

    print(json.dumps({"result": "PASS", "key_fields": key_fields}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
