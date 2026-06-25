#!/usr/bin/env python3
"""
Batch L2 sample derivation entrypoint.

Discovers usable dataset_export directories under a runs root, reuses
derive_l2_case_samples.py logic, validates aggregated outputs, writes unified
JSONL files, and emits a machine-readable summary.
"""

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Dict, List


def load_l2_module(script_path: Path):
    spec = importlib.util.spec_from_file_location("derive_l2_case_samples", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load module from: {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def discover_dataset_exports(runs_root: Path) -> List[Path]:
    candidates: List[Path] = []
    for candidate in sorted(runs_root.rglob("dataset_export")):
        if not candidate.is_dir():
            continue
        canonical_path = candidate / "canonical_case.json"
        evidence_path = candidate / "evidence_candidates.jsonl"
        if canonical_path.exists() and evidence_path.exists():
            candidates.append(candidate)
    return candidates


def build_summary_template(runs_root: Path, output_root: Path) -> Dict[str, Any]:
    return {
        "runs_root": str(runs_root),
        "output_root": str(output_root),
        "discovered_dataset_exports": 0,
        "success_count": 0,
        "failure_count": 0,
        "failed_cases": [],
        "family_counts": {},
        "written_files": {},
        "validation_status": "pending",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch derive validated L2 samples from dataset_export directories.")
    parser.add_argument("--runs-root", required=True, help="Root directory that contains run folders.")
    parser.add_argument("--output-root", required=True, help="Unified output directory for aggregated L2 JSONL files.")
    parser.add_argument(
        "--derive-script",
        default="derive_l2_case_samples.py",
        help="Path to derive_l2_case_samples.py. Defaults to the workspace copy.",
    )
    args = parser.parse_args()

    runs_root = Path(args.runs_root).resolve()
    output_root = Path(args.output_root).resolve()
    derive_script = Path(args.derive_script).resolve()

    if not runs_root.exists():
        print(f"Runs root not found: {runs_root}", file=sys.stderr)
        sys.exit(2)
    if not derive_script.exists():
        print(f"Derive script not found: {derive_script}", file=sys.stderr)
        sys.exit(2)

    module = load_l2_module(derive_script)
    module.ensure_dir(output_root)
    module.remove_stale_outputs(output_root)

    grouped: Dict[str, List[Dict[str, Any]]] = {
        "diagnosis": [],
        "evidence_extraction": [],
        "cause_vs_symptom": [],
        "action_after_diagnosis": [],
    }
    summary = build_summary_template(runs_root, output_root)

    dataset_exports = discover_dataset_exports(runs_root)
    summary["discovered_dataset_exports"] = len(dataset_exports)

    for dataset_export_dir in dataset_exports:
        try:
            records = module.derive_records(dataset_export_dir)
            case_id = records["diagnosis"]["case_id"]
            family = ((records["diagnosis"].get("target") or {}).get("gt") or {}).get("family", "unknown")
            for task_name, record in records.items():
                grouped[task_name].append(record)
            summary["success_count"] += 1
            summary["family_counts"][family] = int(summary["family_counts"].get(family, 0)) + 1
        except Exception as exc:  # noqa: BLE001
            summary["failure_count"] += 1
            summary["failed_cases"].append(
                {
                    "dataset_export": str(dataset_export_dir),
                    "reason": str(exc),
                }
            )

    if summary["success_count"] == 0:
        summary["validation_status"] = "failed"
        summary_path = output_root / "batch_summary.json"
        with summary_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(summary, handle, ensure_ascii=False, indent=2)
        print(json.dumps(summary, ensure_ascii=False, indent=2), file=sys.stderr)
        sys.exit(1)

    try:
        module.validate_records(grouped)
        summary["validation_status"] = "passed"
    except Exception as exc:  # noqa: BLE001
        summary["validation_status"] = "failed"
        summary["failed_cases"].append(
            {
                "dataset_export": "__aggregate_validation__",
                "reason": str(exc),
            }
        )
        summary["failure_count"] += 1
        summary_path = output_root / "batch_summary.json"
        with summary_path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(summary, handle, ensure_ascii=False, indent=2)
        print(str(exc), file=sys.stderr)
        print(json.dumps(summary, ensure_ascii=False, indent=2), file=sys.stderr)
        sys.exit(1)

    for task_name, file_name in module.TASK_OUTPUT_FILES.items():
        output_path = output_root / file_name
        module.write_jsonl(output_path, grouped[task_name])
        summary["written_files"][file_name] = len(grouped[task_name])

    summary_path = output_root / "batch_summary.json"
    with summary_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)

    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if summary["failure_count"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
