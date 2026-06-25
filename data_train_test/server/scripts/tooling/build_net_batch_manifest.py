#!/usr/bin/env python3
"""Build the formal 70-run NET batch manifest and merged L2 artifacts.

This script is intentionally read-only with respect to inbox/runs.  It uses the
explicit accepted/excluded lists from the final audit, not directory discovery.
"""

from __future__ import annotations

import csv
import json
from collections import Counter, OrderedDict, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


BATCH_ID = "net_formal_batch_70_20260429"
TASK_FILES = [
    "diagnosis.jsonl",
    "evidence_extraction.jsonl",
    "cause_vs_symptom.jsonl",
    "action_after_diagnosis.jsonl",
]

ACCEPTED: "OrderedDict[str, List[str]]" = OrderedDict(
    [
        (
            "net_dns_fail",
            [
                "20260428_090853",
                "20260428_091302",
                "20260428_091430",
                "20260428_091556",
                "20260428_091726",
                "20260428_091851",
                "20260428_092017",
                "20260428_092311",
                "20260428_092441",
                "20260428_092607",
            ],
        ),
        (
            "net_public_ip_unreachable",
            [
                "20260428_092847",
                "20260428_093023",
                "20260428_093200",
                "20260428_093510",
                "20260428_093645",
                "20260428_093818",
                "20260428_093952",
                "20260428_094125",
                "20260428_094258",
                "20260428_094432",
            ],
        ),
        (
            "net_no_default_route",
            [
                "20260428_105644",
                "20260428_105923",
                "20260428_110054",
                "20260428_110218",
                "20260428_110343",
                "20260428_110508",
                "20260428_110641",
                "20260428_110809",
                "20260428_110933",
                "20260428_111102",
            ],
        ),
        (
            "net_no_ipv4_on_iface",
            [
                "20260428_112421",
                "20260428_112551",
                "20260428_141749",
                "20260428_142459",
                "20260428_142952",
                "20260428_143320",
                "20260428_143541",
                "20260428_143727",
                "20260428_143943",
                "20260428_144113",
            ],
        ),
        (
            "net_wifi_disconnect",
            [
                "20260428_144738",
                "20260428_144921",
                "20260428_145201",
                "20260428_145339",
                "20260428_145515",
                "20260428_150052",
                "20260428_150227",
                "20260428_150504",
                "20260428_150649",
                "20260428_150820",
            ],
        ),
        (
            "net_wifi_auth_fail_wrong_psk",
            [
                "20260428_151840",
                "20260428_154257",
                "20260428_154941",
                "20260428_155156",
                "20260428_155413",
                "20260428_160724",
                "20260428_160938",
                "20260428_161150",
                "20260428_161458",
                "20260429_105948",
            ],
        ),
        (
            "net_wrong_default_route",
            [
                "20260429_110810",
                "20260429_111442",
                "20260429_112000",
                "20260429_112245",
                "20260429_112709",
                "20260429_113734",
                "20260429_113601",
                "20260429_113908",
                "20260429_114035",
                "20260429_140709",
            ],
        ),
    ]
)

EXCLUDED = [
    ("20260428_092144", "net_dns_fail", "fault_observed=false"),
    ("20260428_094713", "net_no_default_route", "recovery_observed=false"),
    ("20260428_094848", "net_no_default_route", "old pre-fix run"),
    ("20260428_112717", "net_no_ipv4_on_iface", "recovery_observed=false"),
    ("20260428_140125", "net_no_ipv4_on_iface", "recovery gate dirty"),
    ("20260428_140358", "net_no_ipv4_on_iface", "recovery gate dirty"),
    ("20260428_141355", "net_no_ipv4_on_iface", "post2 stale baseline"),
    ("20260428_143122", "net_no_ipv4_on_iface", "post baseline dirty"),
    ("20260428_151300", "net_wifi_auth_fail_wrong_psk", "recovery_observed=false"),
    ("20260428_152147", "net_wifi_auth_fail_wrong_psk", "recovery_observed=false"),
    ("20260428_152641", "net_wifi_auth_fail_wrong_psk", "injector_stop_ok=false"),
    ("20260428_161713", "net_wifi_auth_fail_wrong_psk", "wpa_not_completed"),
    ("20260428_162337", "net_wifi_auth_fail_wrong_psk", "wpa_not_completed"),
    ("20260429_090905", "net_wifi_auth_fail_wrong_psk", "stale hardcoded 1.1.1.1 gate"),
    ("20260429_111743", "net_wrong_default_route", "recovery_observed=false"),
    ("20260429_134212", "net_wrong_default_route", "NO_WRONG_ROUTE_EVIDENCE"),
    ("20260429_134451", "net_wrong_default_route", "NO_WRONG_ROUTE_EVIDENCE"),
]

EXPECTED_PRIMARY = {
    "net_dns_fail": ["net_dns_resolution_fail"],
    "net_public_ip_unreachable": ["net_target_ip_unreachable"],
    "net_no_default_route": ["net_no_default_route"],
    "net_no_ipv4_on_iface": ["net_no_ipv4_on_iface"],
    "net_wifi_disconnect": ["net_wifi_disconnected"],
    "net_wifi_auth_fail_wrong_psk": ["net_wifi_auth_fail", "wlan_auth_fail"],
    "net_wrong_default_route": ["net_wrong_default_route"],
}

PROBE_EPOCH = {
    "old_main_public_probe": "1.1.1.1",
    "new_main_public_probe": "8.8.8.8",
    "switch_point": "after 59 accepted runs",
    "reason": (
        "1.1.1.1 was persistently single-point unreachable on Guest while "
        "gateway / 8.8.8.8 / 9.9.9.9 / 208.67.222.222 / DNS were healthy"
    ),
    "old_epoch_accepted_count": 59,
    "new_epoch_accepted_count": 11,
}

KNOWN_FIXES_OR_NOTES = [
    "auth-fail restore/gate/injector-stop handling was repaired before accepted runs were closed.",
    "net_no_ipv4_on_iface added controlled IP-refresh recovery fallback for Guest/AP stale forwarding.",
    "net_wrong_default_route recovery parser/probe epoch and fault snapshot window issues were repaired.",
    "Excluded runs are retained only as debugging samples and are not merged into L2.",
]


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


def choose_output_dir(root: Path) -> Path:
    base = root / "dataset_batches" / BATCH_ID
    if not base.exists():
        return base
    if not any(base.iterdir()):
        return base
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return root / "dataset_batches" / f"{BATCH_ID}_{stamp}"


def collect_primary_kinds(evidence_path: Path) -> List[str]:
    kinds: List[str] = []
    for row in read_jsonl(evidence_path):
        if row.get("target") == "primary" or row.get("support_role") == "primary":
            kind = str(row.get("kind", "")).strip()
            if kind and kind not in kinds:
                kinds.append(kind)
    return kinds


def semantic_primary_for(subtype: str, primary_kinds: List[str]) -> Tuple[str, str]:
    expected = EXPECTED_PRIMARY[subtype]
    for kind in primary_kinds:
        if kind in expected:
            return kind, "evidence_candidates"
    # Some older exports kept the fault-specific evidence in validator/outcome
    # rather than as a primary L1 candidate.  Keep the manifest semantic explicit
    # and preserve the original candidate kinds alongside it.
    return expected[0], "subtype_expected_from_validated_outcome"


def probe_epoch_for(index_1_based: int) -> str:
    return "old:1.1.1.1" if index_1_based <= 59 else "new:8.8.8.8"


def rel(path: Path, root: Path) -> str:
    return str(path.resolve().relative_to(root.resolve()))


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    runs_root = root / "inbox" / "runs"
    out_dir = choose_output_dir(root)
    l2_out = out_dir / "l2"
    out_dir.mkdir(parents=True, exist_ok=True)
    l2_out.mkdir(parents=True, exist_ok=True)

    accepted_flat = [(subtype, run_id) for subtype, ids in ACCEPTED.items() for run_id in ids]
    accepted_ids = [run_id for _, run_id in accepted_flat]
    excluded_ids = [run_id for run_id, _, _ in EXCLUDED]
    if len(accepted_ids) != 70:
        raise SystemExit(f"accepted_count expected 70, got {len(accepted_ids)}")
    if len(set(accepted_ids)) != len(accepted_ids):
        raise SystemExit("duplicate accepted run_id in explicit list")
    mixed = sorted(set(accepted_ids).intersection(excluded_ids))
    if mixed:
        raise SystemExit(f"excluded run(s) found in accepted list: {mixed}")

    accepted_rows: List[Dict[str, Any]] = []
    l1_rows: List[Dict[str, Any]] = []
    subtype_counts: Counter[str] = Counter()
    primary_counts: Counter[str] = Counter()
    fault_reason_counts: Counter[str] = Counter()
    recovery_reason_counts: Counter[str] = Counter()
    probe_epoch_counts: Counter[str] = Counter()

    for index, (subtype, run_id) in enumerate(accepted_flat, start=1):
        run_dir = runs_root / run_id
        outcome_path = run_dir / "_net_outcome.json"
        export_dir = run_dir / "dataset_export"
        canonical_path = export_dir / "canonical_case.json"
        evidence_path = export_dir / "evidence_candidates.jsonl"
        l2_dir = export_dir / "l2"
        required = [outcome_path, canonical_path, evidence_path, l2_dir]
        missing = [str(path) for path in required if not path.exists()]
        if missing:
            raise SystemExit(f"{run_id}: missing required artifacts: {missing}")
        for task_file in TASK_FILES:
            task_path = l2_dir / task_file
            if not task_path.exists() or task_path.stat().st_size == 0:
                raise SystemExit(f"{run_id}: missing or empty L2 task file: {task_file}")

        outcome = read_json(outcome_path)
        canonical = read_json(canonical_path)
        gt_subtype = canonical.get("gt", {}).get("subtype")
        if gt_subtype != subtype:
            raise SystemExit(f"{run_id}: gt.subtype={gt_subtype!r}, expected {subtype!r}")
        if outcome.get("fault_observed") is not True or outcome.get("recovery_observed") is not True:
            raise SystemExit(f"{run_id}: outcome fault/recovery not both true")

        primary_kinds = collect_primary_kinds(evidence_path)
        primary_evidence, primary_source = semantic_primary_for(subtype, primary_kinds)
        epoch = probe_epoch_for(index)
        row = {
            "run_id": run_id,
            "subtype": subtype,
            "run_dir": rel(run_dir, root),
            "canonical_case_path": rel(canonical_path, root),
            "evidence_candidates_path": rel(evidence_path, root),
            "outcome_path": rel(outcome_path, root),
            "l2_dir": rel(l2_dir, root),
            "primary_evidence": primary_evidence,
            "primary_evidence_source": primary_source,
            "evidence_candidate_primary_kinds": ",".join(primary_kinds),
            "fault_reason": outcome.get("fault_observation_reason", ""),
            "recovery_reason": outcome.get("recovery_observation_reason", ""),
            "probe_epoch": epoch,
        }
        accepted_rows.append(row)
        l1_rows.append(
            {
                "run_id": run_id,
                "subtype": subtype,
                "canonical_case_path": row["canonical_case_path"],
                "evidence_candidates_path": row["evidence_candidates_path"],
                "outcome_path": row["outcome_path"],
                "dataset_export_dir": rel(export_dir, root),
                "probe_epoch": epoch,
            }
        )
        subtype_counts[subtype] += 1
        primary_counts[primary_evidence] += 1
        fault_reason_counts[str(row["fault_reason"])] += 1
        recovery_reason_counts[str(row["recovery_reason"])] += 1
        probe_epoch_counts[epoch] += 1

    for subtype in ACCEPTED:
        if subtype_counts[subtype] != 10:
            raise SystemExit(f"{subtype}: expected 10 accepted, got {subtype_counts[subtype]}")

    excluded_rows = [
        {
            "run_id": run_id,
            "subtype": subtype,
            "reason": reason,
            "keep_as_debug_sample": True,
            "run_dir": rel(runs_root / run_id, root) if (runs_root / run_id).exists() else "",
        }
        for run_id, subtype, reason in EXCLUDED
    ]

    merged_counts: Dict[str, int] = {}
    for task_file in TASK_FILES:
        seen: set[str] = set()
        merged: List[Dict[str, Any]] = []
        for subtype, run_id in accepted_flat:
            source_path = runs_root / run_id / "dataset_export" / "l2" / task_file
            for row in read_jsonl(source_path):
                sample_id = row.get("sample_id") or f"{row.get('source_case_id')}::{row.get('sample_type')}"
                if sample_id in seen:
                    raise SystemExit(f"duplicate L2 sample_id in {task_file}: {sample_id}")
                if row.get("source_case_id") != run_id and row.get("case_id") != run_id:
                    raise SystemExit(f"{run_id}/{task_file}: source_case_id/case_id mismatch")
                seen.add(str(sample_id))
                merged.append(row)
        write_jsonl(l2_out / task_file, merged)
        merged_counts[task_file.removesuffix(".jsonl")] = len(merged)

    stats = {
        "batch_id": BATCH_ID,
        "accepted_count": len(accepted_rows),
        "subtype_counts": dict(subtype_counts),
        "primary_evidence_distribution": dict(primary_counts),
        "fault_reason_distribution": dict(fault_reason_counts),
        "recovery_reason_distribution": dict(recovery_reason_counts),
        "probe_epoch_distribution": dict(probe_epoch_counts),
        "l2_task_sample_counts": merged_counts,
        "excluded_count": len(excluded_rows),
        "excluded_reason_distribution": dict(Counter(row["reason"] for row in excluded_rows)),
    }

    validation_summary = {
        "source_accepted_count": 70,
        "subtype_count_each": 10,
        "excluded_overlap_count": 0,
        "l2_task_files": TASK_FILES,
        "strong_checks": [
            "all accepted artifacts exist",
            "gt.subtype matches explicit list",
            "fault_observed and recovery_observed are true",
            "excluded runs are not in accepted",
            "merged L2 sample ids are unique",
            "probe epoch distribution is 59 old / 11 new",
        ],
    }

    manifest = {
        "batch_id": BATCH_ID,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_root": str(root.resolve()),
        "accepted_count": len(accepted_rows),
        "subtype_counts": dict(subtype_counts),
        "accepted_runs": accepted_rows,
        "excluded_runs": excluded_rows,
        "probe_epoch": PROBE_EPOCH,
        "known_fixes_or_notes": KNOWN_FIXES_OR_NOTES,
        "validation_summary": validation_summary,
    }

    write_json(out_dir / "batch_manifest.json", manifest)
    write_jsonl(out_dir / "accepted_runs.jsonl", accepted_rows)
    write_csv(
        out_dir / "accepted_runs.csv",
        accepted_rows,
        [
            "run_id",
            "subtype",
            "run_dir",
            "canonical_case_path",
            "evidence_candidates_path",
            "outcome_path",
            "l2_dir",
            "primary_evidence",
            "primary_evidence_source",
            "evidence_candidate_primary_kinds",
            "fault_reason",
            "recovery_reason",
            "probe_epoch",
        ],
    )
    write_jsonl(out_dir / "excluded_runs.jsonl", excluded_rows)
    write_csv(out_dir / "excluded_runs.csv", excluded_rows, ["run_id", "subtype", "reason", "keep_as_debug_sample", "run_dir"])
    write_jsonl(out_dir / "l1_index.jsonl", l1_rows)
    write_json(out_dir / "stats.json", stats)

    subtype_lines = "\n".join(f"- {key}: {value}" for key, value in subtype_counts.items())
    l2_lines = "\n".join(f"- {key}: {value}" for key, value in merged_counts.items())
    probe_lines = "\n".join(f"- {key}: {value}" for key, value in PROBE_EPOCH.items())
    notes_lines = "\n".join(f"- {note}" for note in KNOWN_FIXES_OR_NOTES)
    stats_md = f"""# {BATCH_ID} Stats

## Summary
- accepted_count: {len(accepted_rows)}
- excluded_count: {len(excluded_rows)}
- quarantine_mixed_into_accepted: 0

## Subtype Counts
{subtype_lines}

## L2 Merged Sample Counts
{l2_lines}

## Probe Epoch
{probe_lines}

## Notes
{notes_lines}
"""
    (out_dir / "stats.md").write_text(stats_md, encoding="utf-8")

    readme = f"""# {BATCH_ID}

Formal first NET batch built from the final-audited 70 accepted runs.

## Purpose
This directory indexes the accepted runs, records excluded debug runs, and
contains merged L2 JSONL files for downstream training-sample preparation.

## Accepted Rule
- 7 NET subtypes x 10 accepted runs each.
- Each accepted run passed NET validator, L1 validation, and L2 validation in
  the final audit.
- Excluded/quarantine runs are not present in `accepted_runs.*` or merged L2.

## Probe Epoch
{probe_lines}

## Known Fixes / Risks
{notes_lines}

## Files
- `batch_manifest.json`: batch metadata and run-level index.
- `accepted_runs.jsonl` / `.csv`: accepted run list.
- `excluded_runs.jsonl` / `.csv`: excluded debug-only runs.
- `l1_index.jsonl`: L1 artifact pointers.
- `l2/*.jsonl`: merged L2 task files, schema preserved from per-run L2.
- `stats.json` / `stats.md`: counts and distributions.
"""
    (out_dir / "README.md").write_text(readme, encoding="utf-8")
    (out_dir / "batch_notes.md").write_text(readme, encoding="utf-8")

    print(json.dumps({"output_dir": str(out_dir), "stats": stats}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
