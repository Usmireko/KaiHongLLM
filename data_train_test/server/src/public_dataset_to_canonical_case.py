#!/usr/bin/env python3
"""
Public dataset adapter with one concrete parser path.

Supported chain:
raw public dataset format (incident_json_v1)
-> normalized public adapter input JSON
-> canonical_case.json + evidence_candidates.jsonl
"""

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, payload: Dict[str, Any]) -> None:
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")


def read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def build_modalities(source_case: Dict[str, Any]) -> Dict[str, bool]:
    available_modalities = set(source_case.get("available_modalities") or [])
    return {
        # Missing public modalities are explicit false values. They are not
        # fabricated or backfilled from unrelated fields.
        "metrics": "metrics" in available_modalities,
        "events": "events" in available_modalities,
        "procs": "procs" in available_modalities,
        "dmesg_before": "dmesg_before" in available_modalities,
        "dmesg_after": "dmesg_after" in available_modalities,
        "hilog_full": "hilog_full" in available_modalities,
        "fault_inject": "fault_inject" in available_modalities,
        "faultlog_new": "faultlog_new" in available_modalities,
        "faultlog_all": "faultlog_all" in available_modalities,
        "device_context": "device_context" in available_modalities,
    }


def build_canonical_case(source_case: Dict[str, Any]) -> Dict[str, Any]:
    case_id = source_case["source_case_id"]
    modalities = build_modalities(source_case)

    return {
        "schema_version": "canonical_case_v1",
        "source_type": source_case.get("source_type", "public_dataset"),
        "source_case_id": case_id,
        "case_id": case_id,
        "source": {
            # These fields come from the public dataset itself, not board-side collection.
            "source_dataset": source_case["source_dataset"],
            "source_case_id": case_id,
            "source_type": source_case.get("source_type", "public_dataset"),
            "collector_version": None,
            "scenario_tag": source_case.get("scenario_tag"),
            "fault_type": source_case.get("fault_type"),
            "device_sn_hash": None,
        },
        "gt": {
            # Public labels keep confidence explicitly because they may be less
            # reliable than controlled injection labels.
            "run_kind": source_case.get("run_kind", "public_case"),
            "family": source_case["gt"]["family"],
            "subtype": source_case["gt"]["subtype"],
            "severity": source_case["gt"].get("severity"),
            "is_anomaly": bool(source_case["gt"].get("is_anomaly", True)),
            "confidence": source_case.get("gt_confidence", "medium"),
        },
        "obs": {
            "state": source_case.get("obs", {}).get("state", "unknown"),
            "primary_family": source_case.get("obs", {}).get("primary_family", "unknown"),
            "secondary_families": source_case.get("obs", {}).get("secondary_families") or [],
            "fault_families": source_case.get("obs", {}).get("fault_families") or [],
            "warn_families": source_case.get("obs", {}).get("warn_families") or [],
            "confounders": source_case.get("obs", {}).get("confounders") or [],
            "scores": source_case.get("obs", {}).get("scores") or {},
        },
        "timing": {
            # If public timing is unavailable, keep these fields present but null.
            "run_window_host_epoch_ms_start": source_case.get("timing", {}).get("run_window_host_epoch_ms_start"),
            "run_window_host_epoch_ms_end": source_case.get("timing", {}).get("run_window_host_epoch_ms_end"),
            "run_window_board_ms_start": source_case.get("timing", {}).get("run_window_board_ms_start"),
            "run_window_board_ms_end": source_case.get("timing", {}).get("run_window_board_ms_end"),
            "run_window_source": source_case.get("timing", {}).get("run_window_source"),
            "time_skew_ms": source_case.get("timing", {}).get("time_skew_ms"),
        },
        "modalities": modalities,
        "modality_mask": modalities,
        "quality_flags": source_case.get("quality_flags") or [],
        "derived": {
            # Any unavailable modality-derived structure stays empty or downgraded.
            "metrics_summary": source_case.get("derived", {}).get("metrics_summary") or {},
            "event_summary": source_case.get("derived", {}).get("event_summary") or {},
            "injector_summary": source_case.get("derived", {}).get("injector_summary") or {},
            "process_suspects": source_case.get("derived", {}).get("process_suspects") or {
                "exists": False,
                "snapshot_count": 0,
                "ranking_basis": [],
                "suspects": [],
            },
            "dmesg_before_markers": source_case.get("derived", {}).get("dmesg_before_markers") or {},
            "dmesg_after_markers": source_case.get("derived", {}).get("dmesg_after_markers") or {},
            "hilog_markers": source_case.get("derived", {}).get("hilog_markers") or {},
        },
        "paths_rel": source_case.get("paths_rel") or {},
    }


def build_evidence_rows(source_case: Dict[str, Any]) -> List[Dict[str, Any]]:
    evidence_rows: List[Dict[str, Any]] = []
    for index, row in enumerate(source_case.get("evidence_candidates") or [], start=1):
        evidence_rows.append(
            {
                "eid": row.get("eid", f"e{index}"),
                "source": row["source"],
                "source_rel": row.get("source_rel"),
                "kind": row["kind"],
                "target": row.get("target"),
                "support_role": row.get("support_role", "secondary"),
                "ts": row.get("ts"),
                "span": row.get("span"),
                "text": row["text"],
                "score": row.get("score"),
            }
        )
    return evidence_rows


def normalize_incident_json_v1(raw_case: Dict[str, Any]) -> Dict[str, Any]:
    label = raw_case.get("label") or {}
    observation = raw_case.get("observation") or {}
    evidence_rows = raw_case.get("evidence") or []
    process_suspects = raw_case.get("process_suspects") or []

    normalized_rows: List[Dict[str, Any]] = []
    for index, row in enumerate(evidence_rows, start=1):
        normalized_rows.append(
            {
                "eid": f"e{index}",
                "source": row.get("source", "public_dataset"),
                "source_rel": row.get("source_rel"),
                "kind": row["kind"],
                "support_role": row.get("support_role", "secondary"),
                "text": row["text"],
                "score": row.get("score"),
                "ts": row.get("ts"),
                "span": row.get("span"),
            }
        )

    return {
        "source_dataset": raw_case["dataset_name"],
        "source_case_id": raw_case["case_id"],
        "source_type": "public_dataset",
        "gt_confidence": label.get("confidence", "medium"),
        "scenario_tag": raw_case.get("scenario_tag", label.get("subtype")),
        "fault_type": raw_case.get("fault_type", label.get("subtype")),
        "run_kind": "public_case",
        "gt": {
            "family": label["family"],
            "subtype": label["subtype"],
            "severity": label.get("severity"),
            "is_anomaly": bool(label.get("is_anomaly", True)),
        },
        "obs": {
            "state": observation.get("state", "fault"),
            "primary_family": observation.get("primary_family", "unknown"),
            "secondary_families": observation.get("secondary_families") or [],
            "fault_families": observation.get("fault_families") or [],
            "warn_families": observation.get("warn_families") or [],
            "confounders": observation.get("confounders") or [],
            "scores": observation.get("scores") or {},
        },
        "available_modalities": raw_case.get("available_modalities") or [],
        "quality_flags": raw_case.get("quality_flags") or [],
        "derived": {
            "metrics_summary": raw_case.get("metrics_summary") or {},
            "event_summary": raw_case.get("event_summary") or {
                "count_in_window": len(evidence_rows),
                "notes": "No raw event stream available; derived from structured public evidence rows.",
            },
            # Public incident data usually has no real injector log. Keep it empty instead of fabricating.
            "injector_summary": {},
            "process_suspects": raw_case.get("process_suspects_block") or {
                "exists": bool(process_suspects),
                "snapshot_count": 0,
                "ranking_basis": raw_case.get("process_ranking_basis") or [],
                "suspects": process_suspects,
            },
            "dmesg_before_markers": {},
            "dmesg_after_markers": {},
            "hilog_markers": {},
        },
        "timing": raw_case.get("timing") or {},
        "paths_rel": {},
        "evidence_candidates": normalized_rows,
    }


def normalize_source_case(input_format: str, input_payload: Dict[str, Any]) -> Dict[str, Any]:
    if input_format == "normalized":
        return input_payload
    if input_format == "incident_json_v1":
        return normalize_incident_json_v1(input_payload)
    raise ValueError(f"Unsupported input format: {input_format}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Public dataset to canonical_case adapter.")
    parser.add_argument("--input-json", required=True, help="Raw or normalized public dataset case JSON.")
    parser.add_argument(
        "--input-format",
        default="normalized",
        choices=("normalized", "incident_json_v1"),
        help="Input format. Use incident_json_v1 for the concrete demo parser path.",
    )
    parser.add_argument("--normalized-out", help="Optional path to write the normalized adapter input JSON.")
    parser.add_argument("--output-dir", required=True, help="Output directory for canonical_case.json and evidence_candidates.jsonl.")
    args = parser.parse_args()

    input_payload = read_json(Path(args.input_json).resolve())
    normalized_case = normalize_source_case(args.input_format, input_payload)
    output_dir = Path(args.output_dir).resolve()

    required = ["source_dataset", "source_case_id", "source_type", "gt", "gt_confidence", "evidence_candidates"]
    missing = [field_name for field_name in required if field_name not in normalized_case]
    if missing:
        raise SystemExit(f"Missing required public adapter fields: {', '.join(missing)}")

    if args.normalized_out:
        write_json(Path(args.normalized_out).resolve(), normalized_case)

    canonical_case = build_canonical_case(normalized_case)
    evidence_rows = build_evidence_rows(normalized_case)

    write_json(output_dir / "canonical_case.json", canonical_case)
    write_jsonl(output_dir / "evidence_candidates.jsonl", evidence_rows)

    print(f"Wrote public adapter outputs to: {output_dir}")


if __name__ == "__main__":
    main()
