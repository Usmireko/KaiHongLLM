#!/usr/bin/env python3
"""
Validate L1/L2 dataset artifacts in a directory.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

import dataset_models as models


def read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def validate_l1_dir(input_dir: Path) -> Tuple[int, List[models.ValidationIssue]]:
    issues: List[models.ValidationIssue] = []
    canonical_path = input_dir / "canonical_case.json"
    evidence_path = input_dir / "evidence_candidates.jsonl"
    if not canonical_path.exists() and not evidence_path.exists():
        return 0, issues

    if not canonical_path.exists():
        issues.append(models.ValidationIssue("canonical_case.json", "missing required file"))
    if not evidence_path.exists():
        issues.append(models.ValidationIssue("evidence_candidates.jsonl", "missing required file"))
    if issues:
        return 0, issues

    canonical = read_json(canonical_path)
    issues.extend(models.validate_canonical_case(canonical, "canonical_case.json"))

    evidence_rows = read_jsonl(evidence_path)
    for index, row in enumerate(evidence_rows):
        issues.extend(models.validate_evidence_candidate(row, f"evidence_candidates.jsonl[{index}]"))

    return len(evidence_rows) + 1, issues


def validate_l2_dir(input_dir: Path) -> Tuple[int, List[models.ValidationIssue]]:
    issues: List[models.ValidationIssue] = []
    record_count = 0
    task_files = {
        "diagnosis": input_dir / "diagnosis.jsonl",
        "evidence_extraction": input_dir / "evidence_extraction.jsonl",
        "cause_vs_symptom": input_dir / "cause_vs_symptom.jsonl",
        "action_after_diagnosis": input_dir / "action_after_diagnosis.jsonl",
    }

    if not any(path.exists() for path in task_files.values()):
        return 0, issues

    for task_name, path in task_files.items():
        if not path.exists():
            issues.append(models.ValidationIssue(str(path.name), "missing required file"))
            continue
        rows = read_jsonl(path)
        record_count += len(rows)
        for index, row in enumerate(rows):
            record_path = f"{path.name}[{index}]"
            if task_name == "diagnosis":
                issues.extend(models.validate_l2_diagnosis(row, record_path))
            elif task_name == "evidence_extraction":
                issues.extend(models.validate_l2_evidence_extraction(row, record_path))
            elif task_name == "cause_vs_symptom":
                issues.extend(models.validate_l2_cause_vs_symptom(row, record_path))
            elif task_name == "action_after_diagnosis":
                issues.extend(models.validate_l2_action_after_diagnosis(row, record_path))

    return record_count, issues


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate dataset artifacts in a directory.")
    parser.add_argument("--input-dir", required=True, help="Directory containing L1 or L2 artifacts.")
    args = parser.parse_args()

    input_dir = Path(args.input_dir).resolve()
    if not input_dir.exists():
        print(f"Input directory not found: {input_dir}", file=sys.stderr)
        sys.exit(2)

    total_checked = 0
    all_issues: List[models.ValidationIssue] = []

    checked, issues = validate_l1_dir(input_dir)
    total_checked += checked
    all_issues.extend(issues)

    checked, issues = validate_l2_dir(input_dir)
    total_checked += checked
    all_issues.extend(issues)

    if total_checked == 0:
        print(f"No supported L1/L2 artifacts found under: {input_dir}", file=sys.stderr)
        sys.exit(2)

    if all_issues:
        print("Validation failed:", file=sys.stderr)
        for issue in all_issues:
            print(f"- {issue.path}: {issue.message}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps({"input_dir": str(input_dir), "checked_objects": total_checked, "validation_status": "passed"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
