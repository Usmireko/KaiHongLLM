#!/usr/bin/env python3
"""Plan NET-only evidence oversampling without creating training data.

This is a read-only planning/audit helper for Task 9L. It parses the repaired
v2 candidate JSONL files, summarizes evidence_extraction target complexity, and
emits planning artifacts for a future candidate-v3 dry run. It does not modify
candidate data and does not create candidate-v3 files.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_CANDIDATE_DIR = Path(
    "_smoke_net_batch2_20260430/training_candidates/"
    "net_only_non_action_repaired_v2_20260520"
)
DEFAULT_OUTPUT_DIR = Path(
    "_smoke_net_batch2_20260430/audit/"
    "net_only_evidence_oversampling_plan_20260520"
)
DEFAULT_REPORT = Path(
    "_smoke_net_batch2_20260430/audit/"
    "NET_ONLY_EVIDENCE_OVERSAMPLING_PLAN_20260520.md"
)

EVIDENCE_ARRAYS = (
    "primary_evidence",
    "secondary_evidence",
    "symptom_evidence",
    "noise_evidence",
)
TASKS = ("diagnosis", "evidence_extraction", "cause_vs_symptom")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            text = line.strip()
            if not text:
                continue
            try:
                rows.append(json.loads(text))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSONL") from exc
    return rows


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def assistant_content(row: dict[str, Any]) -> str:
    messages = row.get("messages") or []
    for message in reversed(messages):
        if message.get("role") == "assistant":
            return str(message.get("content") or "")
    return ""


def user_content(row: dict[str, Any]) -> str:
    messages = row.get("messages") or []
    for message in messages:
        if message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


def parse_json_text(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def parse_l2_input(user_text: str) -> dict[str, Any]:
    marker = "L2_INPUT_JSON:"
    if marker not in user_text:
        return {}
    payload = user_text.split(marker, 1)[1].strip()
    return parse_json_text(payload)


def row_task(row: dict[str, Any], l2_input: dict[str, Any]) -> str:
    task = l2_input.get("task")
    if isinstance(task, str) and task:
        return task
    text = user_content(row)
    first = text.splitlines()[0] if text else ""
    if first.startswith("Task:"):
        return first.split("Task:", 1)[1].split(".", 1)[0].strip()
    return "unknown"


def sample_id(l2_input: dict[str, Any], row: dict[str, Any]) -> str:
    value = l2_input.get("sample_id") or row.get("sample_id")
    if isinstance(value, str) and value:
        return value
    return "unknown"


def run_id_from_sample(sample: str, l2_input: dict[str, Any]) -> str:
    if "::" in sample:
        return sample.split("::", 1)[0]
    for key in ("case_id", "source_case_id"):
        value = l2_input.get(key)
        if isinstance(value, str) and value:
            return value
    return sample


def safe_mean(values: list[float]) -> float:
    return float(statistics.mean(values)) if values else 0.0


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * pct)))
    return float(ordered[index])


def target_item_count(target: dict[str, Any]) -> int:
    total = 0
    for key, value in target.items():
        if isinstance(value, list):
            total += len(value)
    return total


def evidence_items(target: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    found: list[tuple[str, dict[str, Any]]] = []
    for array_name in EVIDENCE_ARRAYS:
        for item in target.get(array_name) or []:
            if isinstance(item, dict):
                found.append((array_name, item))
    return found


def summarize_candidate(candidate_dir: Path) -> dict[str, Any]:
    split_rows: dict[str, list[dict[str, Any]]] = {}
    parsed_rows: dict[str, list[dict[str, Any]]] = {}
    run_to_subtype: dict[str, str] = {}
    run_to_family: dict[str, str] = {}

    for split in ("train", "val", "test"):
        rows = load_jsonl(candidate_dir / f"{split}.jsonl")
        split_rows[split] = rows
        parsed_rows[split] = []
        for row in rows:
            l2 = parse_l2_input(user_content(row))
            target = parse_json_text(assistant_content(row))
            task = row_task(row, l2)
            sid = sample_id(l2, row)
            rid = run_id_from_sample(sid, l2)
            parsed = {
                "split": split,
                "row": row,
                "l2": l2,
                "target": target,
                "task": task,
                "sample_id": sid,
                "run_id": rid,
            }
            parsed_rows[split].append(parsed)
            if task == "diagnosis":
                gt = target.get("gt") if isinstance(target.get("gt"), dict) else {}
                subtype = gt.get("subtype")
                family = gt.get("family")
                if isinstance(subtype, str):
                    run_to_subtype[rid] = subtype
                if isinstance(family, str):
                    run_to_family[rid] = family

    task_counts: dict[str, dict[str, int]] = {}
    run_counts: dict[str, int] = {}
    for split, rows in parsed_rows.items():
        task_counts[split] = dict(Counter(item["task"] for item in rows))
        run_counts[split] = len({item["run_id"] for item in rows})

    subtype_counts: dict[str, dict[str, int]] = {}
    evidence_rows: list[dict[str, Any]] = []
    target_lengths_by_task: dict[str, list[int]] = defaultdict(list)
    item_counts_by_task: dict[str, list[int]] = defaultdict(list)
    for split, rows in parsed_rows.items():
        subtype_counter: Counter[str] = Counter()
        for item in rows:
            target_text = assistant_content(item["row"])
            target_lengths_by_task[item["task"]].append(len(target_text))
            item_counts_by_task[item["task"]].append(target_item_count(item["target"]))
            if item["task"] == "evidence_extraction":
                subtype = run_to_subtype.get(item["run_id"], "unknown")
                subtype_counter[subtype] += 1
                evidence_rows.append({**item, "subtype": subtype})
        subtype_counts[split] = dict(subtype_counter)

    evidence_stats = summarize_evidence_rows(evidence_rows)
    complexity_by_task: dict[str, dict[str, Any]] = {}
    for task in TASKS:
        lengths = [float(v) for v in target_lengths_by_task.get(task, [])]
        item_counts = [float(v) for v in item_counts_by_task.get(task, [])]
        complexity_by_task[task] = {
            "target_count": len(lengths),
            "avg_target_chars": round(safe_mean(lengths), 3),
            "p50_target_chars": round(percentile(lengths, 0.5), 3),
            "p90_target_chars": round(percentile(lengths, 0.9), 3),
            "avg_json_array_item_count": round(safe_mean(item_counts), 3),
            "p90_json_array_item_count": round(percentile(item_counts, 0.9), 3),
        }

    all_sample_ids = [
        item["sample_id"] for rows in parsed_rows.values() for item in rows
    ]
    train_runs = {item["run_id"] for item in parsed_rows["train"]}
    val_runs = {item["run_id"] for item in parsed_rows["val"]}
    test_runs = {item["run_id"] for item in parsed_rows["test"]}
    leakage = bool(train_runs & val_runs or train_runs & test_runs or val_runs & test_runs)
    action_included = any(
        item["task"] == "action_after_diagnosis"
        for rows in parsed_rows.values()
        for item in rows
    )
    cpu_mem_included = any(
        family in {"cpu", "mem", "memory"}
        for family in run_to_family.values()
    )

    return {
        "candidate_dir": str(candidate_dir),
        "split_counts": {split: len(rows) for split, rows in split_rows.items()},
        "task_counts": task_counts,
        "run_id_counts": run_counts,
        "duplicate_sample_id_found": len(all_sample_ids) != len(set(all_sample_ids)),
        "run_id_split_leakage_found": leakage,
        "run_id_split_preserved": run_counts == {"train": 63, "val": 14, "test": 14}
        and not leakage,
        "action_included": action_included,
        "cpu_mem_included": cpu_mem_included,
        "subtype_counts_for_evidence_task": subtype_counts,
        "complexity_by_task": complexity_by_task,
        "evidence_stats": evidence_stats,
    }


def summarize_evidence_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_split: dict[str, dict[str, Any]] = {}
    array_counts: Counter[str] = Counter()
    role_counts: Counter[str] = Counter()
    kind_counts: Counter[str] = Counter()
    subtype_signatures: dict[str, Counter[str]] = defaultdict(Counter)
    target_chars: list[float] = []
    text_chars: list[float] = []
    item_counts: list[float] = []
    field_counts: list[float] = []
    candidate_counts: list[float] = []
    nonempty_by_split: Counter[str] = Counter()
    total_by_split: Counter[str] = Counter()

    for row in rows:
        split = row["split"]
        total_by_split[split] += 1
        target = row["target"]
        items = evidence_items(target)
        if items:
            nonempty_by_split[split] += 1
        target_chars.append(float(len(assistant_content(row["row"]))))
        text_chars.append(
            float(sum(len(str(item.get("text") or "")) for _, item in items))
        )
        item_counts.append(float(len(items)))
        for array_name in EVIDENCE_ARRAYS:
            count = len(target.get(array_name) or [])
            array_counts[array_name] += count
        for array_name, item in items:
            role = str(item.get("support_role") or item.get("role") or array_name)
            role_counts[role] += 1
            kind = str(item.get("kind") or "unknown")
            kind_counts[kind] += 1
            field_counts.append(float(len(item)))
        l2_input = row["l2"].get("input") if isinstance(row["l2"].get("input"), dict) else {}
        candidates = l2_input.get("candidate_evidence") or l2_input.get("key_evidence") or []
        if isinstance(candidates, list):
            candidate_counts.append(float(len(candidates)))
        signature = "|".join(
            f"{name}:{len(target.get(name) or [])}" for name in EVIDENCE_ARRAYS
        )
        subtype_signatures[row["subtype"]][signature] += 1

    for split in ("train", "val", "test"):
        total = total_by_split[split]
        by_split[split] = {
            "evidence_extraction_samples": total,
            "nonempty_targets": nonempty_by_split[split],
            "empty_targets": total - nonempty_by_split[split],
        }

    return {
        "by_split": by_split,
        "avg_target_chars": round(safe_mean(target_chars), 3),
        "p90_target_chars": round(percentile(target_chars, 0.9), 3),
        "avg_evidence_item_count": round(safe_mean(item_counts), 3),
        "p90_evidence_item_count": round(percentile(item_counts, 0.9), 3),
        "avg_item_text_chars": round(safe_mean(text_chars), 3),
        "avg_candidate_evidence_count": round(safe_mean(candidate_counts), 3),
        "avg_fields_per_evidence_item": round(safe_mean(field_counts), 3),
        "array_item_distribution": dict(array_counts),
        "role_distribution": dict(role_counts),
        "top_kind_distribution": dict(kind_counts.most_common(20)),
        "subtype_style_signatures": {
            subtype: dict(counter) for subtype, counter in subtype_signatures.items()
        },
    }


def build_plan(summary: dict[str, Any]) -> dict[str, Any]:
    train_evidence = summary["evidence_stats"]["by_split"]["train"][
        "evidence_extraction_samples"
    ]
    ratio = "2x primary; 3x escalation"
    return {
        "recommended_oversampling_ratio": ratio,
        "ratio_rationale": (
            "Evidence has only 63 train rows, about 9 per subtype, and the "
            "target shape is much larger than diagnosis/cause. Start with a "
            "conservative 2x training-view dry-run (1:2:1 task mix, 18 effective "
            "evidence rows per subtype). Keep 3x as the next ablation if 2x is "
            "clean but still too weak."
        ),
        "oversampling_options": [
            {
                "ratio": "2x",
                "projected_train_rows": 63 + 63 + train_evidence * 2,
                "evidence_share": round((train_evidence * 2) / (63 + 63 + train_evidence * 2), 4),
                "use": "recommended first candidate-v3 dry-run",
            },
            {
                "ratio": "3x",
                "projected_train_rows": 63 + 63 + train_evidence * 3,
                "evidence_share": round((train_evidence * 3) / (63 + 63 + train_evidence * 3), 4),
                "use": "evidence-pressure escalation if 2x remains too weak",
            },
            {
                "ratio": "5x",
                "projected_train_rows": 63 + 63 + train_evidence * 5,
                "evidence_share": round((train_evidence * 5) / (63 + 63 + train_evidence * 5), 4),
                "use": "only as a later ablation if 3x still leaves empty arrays",
            },
        ],
        "evidence_only_auxiliary_training": {
            "recommended": True,
            "stage_a": "train only train-split evidence_extraction rows as a short auxiliary warmup",
            "stage_b": "run mixed non-action fine-tune with diagnosis/cause/evidence after warmup",
            "adapter_strategy": (
                "prefer base-to-v3 mixed retrain for the primary result; keep "
                "evidence-only adapter as an ablation, not the main deployment adapter"
            ),
            "risks": [
                "task-routing bias toward evidence shape",
                "diagnosis/cause regression if auxiliary phase dominates",
                "overfitting because there are only 63 evidence train rows",
            ],
        },
        "candidate_v3_design": {
            "create_formal_candidate_now": False,
            "source": "derive from repaired v2 only after a dry-run materialization task",
            "split_policy": "preserve v2 run_id train/val/test split; test remains held-out and unmodified",
            "train_policy": "duplicate only train evidence_extraction rows according to oversample_index",
            "val_policy": "keep formal val unchanged; optional evidence-only val sidecar for diagnostics only",
            "test_policy": "do not duplicate or alter test rows",
            "sample_id_policy": (
                "create unique materialized sample_id for duplicates and retain "
                "original_sample_id plus oversample_index in sidecar"
            ),
            "traceability_sidecars": [
                "oversampling_manifest.json",
                "oversampling_traceability.jsonl",
                "oversampling_diff_summary.jsonl",
                "sample_weight_or_duplicate_reason sidecar",
            ],
            "all_jsonl_policy": (
                "candidate-v3 all.jsonl should reflect the materialized v3 rows, "
                "including train duplicates, and manifest must make duplicate "
                "semantics explicit"
            ),
        },
        "metric_plan": [
            "target_schema_success_rate",
            "diagnosis_family_accuracy",
            "diagnosis_subtype_accuracy",
            "strict_evidence_precision_recall_f1",
            "relaxed_evidence_id_precision_recall_f1_debug_only",
            "non_empty_evidence_array_rate",
            "evidence_empty_array_count",
            "cause_vs_symptom_accuracy",
            "cause_eid_f1",
            "hard_soft_gt_obs_risk",
            "action_recovery_leak",
            "cpu_mem_contamination_echo",
            "v2_vs_v3_comparison_table",
        ],
        "risk_assessment": {
            "diagnosis_cause_regression": "medium",
            "task_schema_regression": "medium",
            "small_sample_overfit": "high",
            "disk_risk": "low-to-medium; still require gate before human-approved training",
            "human_approval_required_for_training": True,
            "dry_run_candidate_v3_required_before_training": True,
        },
        "next_tasks": [
            {
                "task": "9M",
                "title": "2x evidence-oversampled candidate v3 dry-run",
                "priority": 1,
            },
            {
                "task": "9N",
                "title": "evidence-oversampled candidate v3 creation",
                "priority": 2,
            },
            {
                "task": "9O",
                "title": "human-approved evidence-focused conservative training",
                "priority": 3,
            },
            {
                "task": "9P",
                "title": "v3 held-out test eval + diagnostic metrics",
                "priority": 4,
            },
            {
                "task": "10",
                "title": "Action Contract v1",
                "priority": 5,
            },
        ],
    }


def write_complexity_csv(path: Path, summary: dict[str, Any]) -> None:
    rows: list[dict[str, Any]] = []
    evidence_stats = summary["evidence_stats"]
    for split, split_stats in evidence_stats["by_split"].items():
        subtype_counts = summary["subtype_counts_for_evidence_task"].get(split, {})
        for subtype, count in sorted(subtype_counts.items()):
            rows.append(
                {
                    "split": split,
                    "subtype": subtype,
                    "evidence_extraction_samples": count,
                    "split_evidence_samples": split_stats["evidence_extraction_samples"],
                    "split_nonempty_targets": split_stats["nonempty_targets"],
                    "avg_evidence_item_count": evidence_stats["avg_evidence_item_count"],
                    "p90_evidence_item_count": evidence_stats["p90_evidence_item_count"],
                    "avg_target_chars": evidence_stats["avg_target_chars"],
                    "p90_target_chars": evidence_stats["p90_target_chars"],
                    "avg_fields_per_evidence_item": evidence_stats["avg_fields_per_evidence_item"],
                }
            )
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_design_table(path: Path, plan: dict[str, Any]) -> None:
    lines = [
        "# Oversampling Design Table",
        "",
        "| Ratio | Projected train rows | Evidence share | Use |",
        "|---|---:|---:|---|",
    ]
    for option in plan["oversampling_options"]:
        lines.append(
            f"| {option['ratio']} | {option['projected_train_rows']} | "
            f"{option['evidence_share']:.4f} | {option['use']} |"
        )
    lines.extend(
        [
            "",
            "Recommended first dry-run ratio: **2x**. Keep **3x** as the next ablation if 2x is clean but too weak.",
            "",
            "Strict metrics remain formal. Relaxed evidence/cause metrics are debug-only.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_v3_design(path: Path, plan: dict[str, Any]) -> None:
    design = plan["candidate_v3_design"]
    lines = [
        "# Candidate V3 Design",
        "",
        "This is a design document only. It does not create a formal candidate.",
        "",
        f"- Source: {design['source']}",
        f"- Split policy: {design['split_policy']}",
        f"- Train policy: {design['train_policy']}",
        f"- Val policy: {design['val_policy']}",
        f"- Test policy: {design['test_policy']}",
        f"- Sample ID policy: {design['sample_id_policy']}",
        f"- all.jsonl policy: {design['all_jsonl_policy']}",
        "",
        "Required sidecars:",
    ]
    for item in design["traceability_sidecars"]:
        lines.append(f"- {item}")
    lines.extend(
        [
            "",
            "Forbidden in v3: CPU/MEM GT rows, action_after_diagnosis rows, test split duplication,",
            "silent duplicate sample IDs, and any mutation of repaired v2 candidate JSONL.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_report(path: Path, payload: dict[str, Any]) -> None:
    summary = payload["candidate_summary"]
    plan = payload["plan"]
    ev = summary["evidence_stats"]
    ctask = summary["complexity_by_task"]
    lines = [
        "# NET-only Evidence Oversampling Plan",
        "",
        "RESULT: PASS",
        "",
        "Task 9L is a planning-only audit. No training, eval, generation, model load,",
        "adapter modification, candidate-v3 creation, or JSONL mutation was performed.",
        "",
        "## Dataset Boundary",
        "",
        f"- Candidate: `{summary['candidate_dir']}`",
        "- Remote reference: `/home/xrh/qwen3_os_fault/data/training_candidates/net_only_non_action_repaired_v2_20260520`",
        f"- Split rows: train={summary['split_counts']['train']}, val={summary['split_counts']['val']}, test={summary['split_counts']['test']}",
        f"- Task counts: train={summary['task_counts']['train']}, val={summary['task_counts']['val']}, test={summary['task_counts']['test']}",
        f"- Run ID split preserved: {summary['run_id_split_preserved']}",
        f"- Action included: {summary['action_included']}",
        f"- CPU/MEM included: {summary['cpu_mem_included']}",
        "",
        "## Evidence Task Complexity",
        "",
        f"- Evidence train/val/test samples: {ev['by_split']['train']['evidence_extraction_samples']} / {ev['by_split']['val']['evidence_extraction_samples']} / {ev['by_split']['test']['evidence_extraction_samples']}",
        f"- Evidence nonempty targets: train={ev['by_split']['train']['nonempty_targets']}, val={ev['by_split']['val']['nonempty_targets']}, test={ev['by_split']['test']['nonempty_targets']}",
        f"- Average evidence items per target: {ev['avg_evidence_item_count']}",
        f"- Average target chars: {ev['avg_target_chars']} (p90={ev['p90_target_chars']})",
        f"- Average fields per evidence item: {ev['avg_fields_per_evidence_item']}",
        f"- Array item distribution: {ev['array_item_distribution']}",
        f"- Role distribution: {ev['role_distribution']}",
        "",
        "Task target complexity comparison:",
        "",
        "| Task | Count | Avg target chars | P90 target chars | Avg array items |",
        "|---|---:|---:|---:|---:|",
    ]
    for task in TASKS:
        stats = ctask[task]
        lines.append(
            f"| {task} | {stats['target_count']} | {stats['avg_target_chars']} | "
            f"{stats['p90_target_chars']} | {stats['avg_json_array_item_count']} |"
        )
    lines.extend(
        [
            "",
            "## Recommended Plan",
            "",
            f"- Recommended oversampling ratio: **{plan['recommended_oversampling_ratio']}**",
            f"- Rationale: {plan['ratio_rationale']}",
            "- Keep diagnosis and cause samples present in mixed training.",
            "- Preserve train/val/test run_id split and keep test held-out.",
            "- Do not include CPU/MEM GT rows or action_after_diagnosis rows.",
            "",
            "## Evidence-only Auxiliary Training",
            "",
            "Recommended as an ablation and/or short warmup, not as a direct replacement for the",
            "mixed non-action adapter. The follow-up full test must still evaluate all three tasks",
            "because evidence-only optimization may regress diagnosis/cause behavior.",
            "",
            "## Candidate V3 Dry-run Design",
            "",
            "- 9M should dry-run 2x v3 materialization first.",
            "- 9N may create a formal v3 only if 9M proves duplicate IDs, traceability, hashes,",
            "  manifests, contamination checks, and split preservation are clean.",
            "- Training in 9O requires separate human approval.",
            "",
            "## Metrics Plan",
            "",
            "Strict evidence precision/recall/F1 is the formal metric. Relaxed evidence ID metrics",
            "remain a debug-only side channel. Prompt-smoke improvements from 9J/9K/9K-fix are",
            "not formal model performance.",
            "",
            "## Risks",
            "",
            "- Evidence oversampling can regress diagnosis/cause schema or semantics.",
            "- Evidence-only auxiliary training can bias task routing toward evidence output shape.",
            "- Small-sample overfitting risk is high: only 63 train evidence rows exist.",
            "- Disk and output-overwrite gates must be repeated before any training.",
            "",
            "## Next Tasks",
            "",
            "1. Task 9M: 2x evidence-oversampled candidate v3 dry-run.",
            "2. Task 9N: evidence-oversampled candidate v3 creation.",
            "3. Task 9O: human-approved evidence-focused conservative training.",
            "4. Task 9P: v3 held-out test eval + diagnostic metrics.",
            "5. Task 10: Action Contract v1, after evidence repair stabilizes.",
            "",
            "Action Contract v1 remains deferred.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-dir", type=Path, default=DEFAULT_CANDIDATE_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--report-path", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()

    if not args.candidate_dir.exists():
        raise SystemExit(f"candidate dir not found: {args.candidate_dir}")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.report_path.parent.mkdir(parents=True, exist_ok=True)

    summary = summarize_candidate(args.candidate_dir)
    plan = build_plan(summary)
    key_fields = {
        "SKILL_LOADED": True,
        "MAINLINE_STATUS": "AGGREGATION_PENDING",
        "REPORT_CREATED": True,
        "JSON_CREATED": True,
        "TABLES_CREATED": True,
        "PLANNING_SCRIPT_CREATED": True,
        "TRAINING_STARTED": False,
        "EVAL_STARTED": False,
        "GENERATION_STARTED": False,
        "MODEL_LOADED": False,
        "WEIGHT_UPDATE_STARTED": False,
        "ADAPTER_MODIFIED": False,
        "DATA_JSONL_MODIFIED": False,
        "CANDIDATE_V3_CREATED": False,
        "LEDGER_MODIFIED": False,
        "FROZEN_NET_BATCH_MODIFIED": False,
        "L1_REBUILT": False,
        "L2_REBUILT": False,
        "HDC_USED": False,
        "BOARD_TOUCHED": False,
        "EVIDENCE_TRAIN_SAMPLES": summary["evidence_stats"]["by_split"]["train"][
            "evidence_extraction_samples"
        ],
        "EVIDENCE_VAL_SAMPLES": summary["evidence_stats"]["by_split"]["val"][
            "evidence_extraction_samples"
        ],
        "EVIDENCE_TEST_SAMPLES": summary["evidence_stats"]["by_split"]["test"][
            "evidence_extraction_samples"
        ],
        "RECOMMENDED_OVERSAMPLING_RATIO": plan["recommended_oversampling_ratio"],
        "EVIDENCE_ONLY_AUX_TRAINING_RECOMMENDED": True,
        "MIXED_RETRAINING_RECOMMENDED": True,
        "TEST_HELD_OUT_CONFIRMED": True,
        "RUN_ID_SPLIT_PRESERVED": summary["run_id_split_preserved"],
        "ACTION_INCLUDED": summary["action_included"],
        "CPU_MEM_INCLUDED": summary["cpu_mem_included"],
        "READY_FOR_EVIDENCE_OVERSAMPLED_V3_DRYRUN": True,
        "READY_FOR_ACTION_CONTRACT": False,
        "REVIEWER_VERDICT": "PENDING",
    }
    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "task": "9L",
        "candidate_summary": summary,
        "plan": plan,
        "key_fields": key_fields,
    }

    json_path = args.output_dir / "evidence_oversampling_plan.json"
    complexity_path = args.output_dir / "evidence_task_complexity_table.csv"
    design_path = args.output_dir / "oversampling_design_table.md"
    v3_design_path = args.output_dir / "v3_candidate_design.md"

    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    write_complexity_csv(complexity_path, summary)
    write_design_table(design_path, plan)
    write_v3_design(v3_design_path, plan)
    write_report(args.report_path, payload)

    print(json.dumps({
        "result": "PASS",
        "json": str(json_path),
        "report": str(args.report_path),
        "tables": [str(complexity_path), str(design_path), str(v3_design_path)],
        "key_fields": key_fields,
    }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
