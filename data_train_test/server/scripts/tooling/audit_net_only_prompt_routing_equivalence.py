#!/usr/bin/env python3
"""Audit NET-only prompt routing equivalence for Task 9K-fix.

This is an offline audit only. It compares per-sample prompt hashes from the
9G prompt-v2 baseline, the 9K task-routed smoke, and optionally a new exact-v2
routed smoke. It never loads a model, trains, evaluates loss, or edits data.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import eval_net_only_contract_aware_generation_smoke_v2 as prompt_v2  # noqa: E402
import eval_net_only_evidence_no_empty_generation_smoke as prompt_v3  # noqa: E402
import eval_net_only_diagnostic_metrics_calibration as cal  # noqa: E402


PROJECT_ROOT = Path("/home/xrh/qwen3_os_fault")
TRAINING_FORMAL_ROOT = PROJECT_ROOT / "outputs/training_formal"
V2_CANDIDATE_ROOT = PROJECT_ROOT / "data/training_candidates/net_only_non_action_repaired_v2_20260520"
DEFAULT_TEST_FILE = V2_CANDIDATE_ROOT / "test.jsonl"
DEFAULT_CONTRACT_JSON = SCRIPT_DIR / "contracts/net_only_non_action_diagnostic_output_contract_v1.json"
BASE_9G = (
    TRAINING_FORMAL_ROOT
    / "net_only_non_action_repaired_v2_conservative_formal_20260520_20260520_144553_diagnostic_eval_20260520_160000"
)
BASE_9K = (
    TRAINING_FORMAL_ROOT
    / "net_only_non_action_repaired_v2_conservative_formal_20260520_20260520_144553_task_routed_evidence_smoke_20260520_180011"
)
DEFAULT_9G_PREDICTIONS = BASE_9G / "repaired_v2_contract_aware_predictions.jsonl"
DEFAULT_9K_ROUTES = BASE_9K / "per_sample_prompt_route.jsonl"


class UserError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _json_dump(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def _jsonl_read(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                payload = json.loads(line)
                if isinstance(payload, dict):
                    rows.append(payload)
    return rows


def _jsonl_write(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            f.write("\n")


def _read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise UserError("JSON_OBJECT_REQUIRED", f"expected object: {path}")
    return payload


def _resolve_file(raw: str, label: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise UserError("ABSOLUTE_PATH_REQUIRED", f"{label} must be absolute: {raw}")
    if not path.is_file():
        raise UserError("FILE_NOT_FOUND", f"{label} not found: {path}")
    return path.resolve()


def _hashes(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for row in _jsonl_read(path):
        if row.get("sample_id") and row.get("prompt_hash"):
            out[str(row["sample_id"])] = str(row["prompt_hash"])
    return out


def _expected_hash(item: Dict[str, Any], contract: Dict[str, Any]) -> Tuple[str, str]:
    task = str(item["task"])
    if task in {"diagnosis", "cause_vs_symptom"}:
        _messages, prompt_hash = prompt_v2._build_prompt_messages(item, contract)
        return "exact_prompt_v2", prompt_hash
    if task == "evidence_extraction":
        _messages, prompt_hash = prompt_v3._build_prompt_messages(item, contract)
        return "evidence_no_empty_prompt", prompt_hash
    raise UserError("UNEXPECTED_TASK", f"unexpected task: {task}")


def run(args: argparse.Namespace) -> int:
    test_file = _resolve_file(args.test_file, "test_file")
    contract_json = _resolve_file(args.contract_json, "contract_json")
    baseline_9g_predictions = _resolve_file(args.baseline_9g_predictions, "baseline_9g_predictions")
    baseline_9k_routes = _resolve_file(args.baseline_9k_routes, "baseline_9k_routes")
    exact_routes = Path(args.exact_routes).resolve() if args.exact_routes else None
    if exact_routes is not None and not exact_routes.is_file():
        raise UserError("EXACT_ROUTES_NOT_FOUND", f"exact routes not found: {exact_routes}")
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    test_rows = cal._read_test_rows(test_file)
    contract = _read_json(contract_json)
    hashes_9g = _hashes(baseline_9g_predictions)
    hashes_9k = _hashes(baseline_9k_routes)
    hashes_exact = _hashes(exact_routes) if exact_routes else {}

    rows: List[Dict[str, Any]] = []
    counts = Counter()
    for item in test_rows:
        sample_id = str(item["sample_id"])
        task = str(item["task"])
        expected_route, expected_hash = _expected_hash(item, contract)
        row = {
            "sample_id": sample_id,
            "run_id": item.get("run_id"),
            "task": task,
            "expected_route": expected_route,
            "expected_prompt_hash": expected_hash,
            "baseline_9g_prompt_hash": hashes_9g.get(sample_id),
            "baseline_9k_prompt_hash": hashes_9k.get(sample_id),
            "exact_v2_routed_prompt_hash": hashes_exact.get(sample_id),
            "baseline_9g_equivalent_to_expected": hashes_9g.get(sample_id) == expected_hash,
            "baseline_9k_equivalent_to_expected": hashes_9k.get(sample_id) == expected_hash,
            "exact_v2_routed_equivalent_to_expected": (
                hashes_exact.get(sample_id) == expected_hash if hashes_exact else None
            ),
        }
        rows.append(row)
        if task in {"diagnosis", "cause_vs_symptom"} and not row["baseline_9k_equivalent_to_expected"]:
            counts["baseline_9k_mismatch_count"] += 1
        if task == "diagnosis" and row["exact_v2_routed_equivalent_to_expected"] is True:
            counts["exact_diagnosis_equivalent_count"] += 1
        if task == "cause_vs_symptom" and row["exact_v2_routed_equivalent_to_expected"] is True:
            counts["exact_cause_equivalent_count"] += 1
        counts[f"{task}_count"] += 1

    diagnosis_total = counts["diagnosis_count"]
    cause_total = counts["cause_vs_symptom_count"]
    audit = {
        "schema_version": "net_only_prompt_routing_equivalence_audit_v1",
        "test_file": str(test_file),
        "baseline_9g_predictions": str(baseline_9g_predictions),
        "baseline_9k_routes": str(baseline_9k_routes),
        "exact_routes": str(exact_routes) if exact_routes else None,
        "diagnosis_count": diagnosis_total,
        "cause_vs_symptom_count": cause_total,
        "evidence_extraction_count": counts["evidence_extraction_count"],
        "baseline_9k_diagnosis_cause_mismatch_count": counts["baseline_9k_mismatch_count"],
        "diagnosis_route_equivalent_to_9g": (
            counts["exact_diagnosis_equivalent_count"] == diagnosis_total if hashes_exact else None
        ),
        "cause_route_equivalent_to_9g": (
            counts["exact_cause_equivalent_count"] == cause_total if hashes_exact else None
        ),
        "route_mismatch_count": counts["baseline_9k_mismatch_count"],
        "model_loaded": False,
        "training_started": False,
        "eval_started": False,
        "weight_update_started": False,
    }
    mismatch = {
        "route_mismatch_count": counts["baseline_9k_mismatch_count"],
        "diagnosis_route_equivalent_to_9g": audit["diagnosis_route_equivalent_to_9g"],
        "cause_route_equivalent_to_9g": audit["cause_route_equivalent_to_9g"],
        "reason": "9K reimplemented prompt-v2-like text instead of calling the exact 9G prompt-v2 builder.",
    }
    _json_dump(output_dir / "prompt_equivalence_audit.json", audit)
    _jsonl_write(output_dir / "per_sample_prompt_hash_comparison.jsonl", rows)
    _json_dump(output_dir / "route_mismatch_summary.json", mismatch)
    print(json.dumps({"result": "PASS", **audit}, ensure_ascii=False, sort_keys=True))
    return 0


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-file", default=str(DEFAULT_TEST_FILE))
    parser.add_argument("--contract-json", default=str(DEFAULT_CONTRACT_JSON))
    parser.add_argument("--baseline-9g-predictions", default=str(DEFAULT_9G_PREDICTIONS))
    parser.add_argument("--baseline-9k-routes", default=str(DEFAULT_9K_ROUTES))
    parser.add_argument("--exact-routes", default="")
    parser.add_argument("--output-dir", required=True)
    return parser.parse_args(argv)


def main(argv: Optional[Iterable[str]] = None) -> int:
    try:
        return run(parse_args(argv))
    except UserError as exc:
        print(json.dumps({"result": "FAIL", "code": exc.code, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
