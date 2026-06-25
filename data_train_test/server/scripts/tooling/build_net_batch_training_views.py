#!/usr/bin/env python3
"""Create training statistics, run splits, and SFT file indexes for a NET batch."""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List


BATCH_DIR = Path("dataset_batches/net_formal_batch_70_20260429")
TASK_FILES = [
    "diagnosis.jsonl",
    "evidence_extraction.jsonl",
    "cause_vs_symptom.jsonl",
    "action_after_diagnosis.jsonl",
]
SPLITS = ("train", "val", "test")


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for raw in handle:
            line = raw.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def write_csv(path: Path, rows: List[Dict[str, Any]], fields: List[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def rel(path: Path, root: Path) -> str:
    return str(path.resolve().relative_to(root.resolve())).replace("\\", "/")


def assign_splits(accepted_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_subtype: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in accepted_rows:
        by_subtype[row["subtype"]].append(row)

    split_rows: List[Dict[str, Any]] = []
    for subtype in sorted(by_subtype):
        rows = by_subtype[subtype]
        if len(rows) != 10:
            raise SystemExit(f"{subtype}: expected 10 rows, got {len(rows)}")
        for idx, row in enumerate(rows):
            split = "train" if idx < 8 else ("val" if idx == 8 else "test")
            split_rows.append(
                {
                    "run_id": row["run_id"],
                    "subtype": subtype,
                    "split": split,
                    "split_policy": "stratified_by_subtype_manifest_order_8_1_1",
                    "probe_epoch": row["probe_epoch"],
                    "primary_evidence": row["primary_evidence"],
                }
            )
    return split_rows


def main() -> None:
    root = Path.cwd()
    batch_dir = (root / BATCH_DIR).resolve()
    if not batch_dir.exists():
        raise SystemExit(f"missing batch dir: {batch_dir}")

    manifest = read_json(batch_dir / "batch_manifest.json")
    accepted_rows = read_jsonl(batch_dir / "accepted_runs.jsonl")
    if len(accepted_rows) != 70:
        raise SystemExit(f"accepted_runs expected 70, got {len(accepted_rows)}")

    split_rows = assign_splits(accepted_rows)
    split_by_run = {row["run_id"]: row["split"] for row in split_rows}
    split_dir = batch_dir / "splits"
    l2_split_root = batch_dir / "l2_splits"
    split_dir.mkdir(exist_ok=True)
    for split in SPLITS:
        (l2_split_root / split).mkdir(parents=True, exist_ok=True)

    write_json(split_dir / "run_splits.json", {"batch_id": manifest["batch_id"], "splits": split_rows})
    write_jsonl(split_dir / "run_splits.jsonl", split_rows)
    write_csv(split_dir / "run_splits.csv", split_rows, ["run_id", "subtype", "split", "split_policy", "probe_epoch", "primary_evidence"])
    for split in SPLITS:
        ids = [row["run_id"] for row in split_rows if row["split"] == split]
        (split_dir / f"{split}_run_ids.txt").write_text("\n".join(ids) + "\n", encoding="utf-8")

    l2_full_dir = batch_dir / "l2"
    l2_counts_by_split: Dict[str, Dict[str, int]] = {split: {} for split in SPLITS}
    l2_counts_by_task: Dict[str, int] = {}
    task_subtype_counts: Dict[str, Dict[str, int]] = {}
    for task_file in TASK_FILES:
        task_name = task_file.removesuffix(".jsonl")
        rows = read_jsonl(l2_full_dir / task_file)
        l2_counts_by_task[task_name] = len(rows)
        by_split = {split: [] for split in SPLITS}
        subtype_counter: Counter[str] = Counter()
        for row in rows:
            run_id = str(row.get("source_case_id") or row.get("case_id") or "")
            if run_id not in split_by_run:
                raise SystemExit(f"{task_file}: source_case_id not in split plan: {run_id}")
            split = split_by_run[run_id]
            by_split[split].append(row)
            subtype = next(item["subtype"] for item in split_rows if item["run_id"] == run_id)
            subtype_counter[subtype] += 1
        task_subtype_counts[task_name] = dict(subtype_counter)
        for split, split_task_rows in by_split.items():
            out_path = l2_split_root / split / task_file
            write_jsonl(out_path, split_task_rows)
            l2_counts_by_split[split][task_name] = len(split_task_rows)

    subtype_split_counts: Dict[str, Dict[str, int]] = {}
    for row in split_rows:
        subtype_split_counts.setdefault(row["subtype"], {split: 0 for split in SPLITS})
        subtype_split_counts[row["subtype"]][row["split"]] += 1

    probe_split_counts: Dict[str, Dict[str, int]] = {}
    for row in split_rows:
        probe_split_counts.setdefault(row["probe_epoch"], {split: 0 for split in SPLITS})
        probe_split_counts[row["probe_epoch"]][row["split"]] += 1

    sft_index: Dict[str, Any] = {
        "batch_id": manifest["batch_id"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "split_policy": "stratified by subtype using manifest order: 8 train, 1 val, 1 test per subtype",
        "source_batch_dir": rel(batch_dir, root),
        "tasks": [],
    }
    for task_file in TASK_FILES:
        task_name = task_file.removesuffix(".jsonl")
        full_path = l2_full_dir / task_file
        task_entry = {
            "task_type": task_name,
            "full_file": rel(full_path, root),
            "full_count": l2_counts_by_task[task_name],
            "full_sha256": sha256_file(full_path),
            "splits": {},
        }
        for split in SPLITS:
            path = l2_split_root / split / task_file
            task_entry["splits"][split] = {
                "file": rel(path, root),
                "count": l2_counts_by_split[split][task_name],
                "sha256": sha256_file(path),
            }
        sft_index["tasks"].append(task_entry)
    write_json(batch_dir / "sft_file_index.json", sft_index)

    stats = {
        "batch_id": manifest["batch_id"],
        "accepted_count": len(accepted_rows),
        "split_counts": dict(Counter(row["split"] for row in split_rows)),
        "subtype_split_counts": subtype_split_counts,
        "probe_epoch_split_counts": probe_split_counts,
        "primary_evidence_distribution": read_json(batch_dir / "stats.json")["primary_evidence_distribution"],
        "fault_reason_distribution": read_json(batch_dir / "stats.json")["fault_reason_distribution"],
        "recovery_reason_distribution": read_json(batch_dir / "stats.json")["recovery_reason_distribution"],
        "l2_counts_by_task": l2_counts_by_task,
        "l2_counts_by_split": l2_counts_by_split,
        "l2_task_subtype_counts": task_subtype_counts,
    }
    write_json(batch_dir / "training_sample_stats.json", stats)

    split_lines = "\n".join(f"- {split}: {stats['split_counts'].get(split, 0)} runs" for split in SPLITS)
    task_lines = "\n".join(f"- {task}: {count} full, train/val/test={l2_counts_by_split['train'][task]}/{l2_counts_by_split['val'][task]}/{l2_counts_by_split['test'][task]}" for task, count in l2_counts_by_task.items())
    subtype_lines = "\n".join(f"- {subtype}: train/val/test={counts['train']}/{counts['val']}/{counts['test']}" for subtype, counts in subtype_split_counts.items())
    probe_lines = "\n".join(f"- {epoch}: train/val/test={counts['train']}/{counts['val']}/{counts['test']}" for epoch, counts in probe_split_counts.items())
    report = f"""# Training Sample Report - {manifest['batch_id']}

## Run Split
Policy: stratified by subtype using manifest order, `8 train / 1 val / 1 test` per subtype.

{split_lines}

## Subtype Coverage
{subtype_lines}

## L2 Task Files
{task_lines}

## Probe Epoch Coverage
{probe_lines}

## SFT Index
Use `sft_file_index.json` for machine-readable full and split file paths, counts, and sha256 checksums.

## Notes
- L2 row schema is preserved; split files copy original rows unchanged.
- Splits are by `run_id`, so all four task samples for the same run stay in the same split.
- Excluded/quarantine runs are not present in the split files.
"""
    (batch_dir / "training_sample_stats.md").write_text(report, encoding="utf-8")

    index_lines = ["# SFT File Index", "", "Machine-readable index: `sft_file_index.json`.", ""]
    for task in sft_index["tasks"]:
        index_lines.append(f"## {task['task_type']}")
        index_lines.append(f"- full: `{task['full_file']}` ({task['full_count']})")
        for split in SPLITS:
            s = task["splits"][split]
            index_lines.append(f"- {split}: `{s['file']}` ({s['count']})")
        index_lines.append("")
    (batch_dir / "sft_file_index.md").write_text("\n".join(index_lines), encoding="utf-8")

    print(json.dumps({"batch_dir": str(batch_dir), "split_counts": stats["split_counts"], "l2_counts_by_task": l2_counts_by_task}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
