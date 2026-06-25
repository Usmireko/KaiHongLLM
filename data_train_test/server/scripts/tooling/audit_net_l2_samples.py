#!/usr/bin/env python3
"""Semantic spot-check for the frozen NET batch L2 samples.

The audit is read-only for dataset inputs. It writes reports under
dataset_batches/net_formal_batch_70_20260429/quality_audit.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


FROZEN_BATCH_DIR = Path("dataset_batches/net_formal_batch_70_20260429")
BATCH_DIR = FROZEN_BATCH_DIR
TASKS = ["diagnosis", "evidence_extraction", "cause_vs_symptom", "action_after_diagnosis"]
EXPECTED_PRIMARY = {
    "net_dns_fail": {"net_dns_resolution_fail"},
    "net_public_ip_unreachable": {"net_target_ip_unreachable"},
    "net_no_default_route": {"net_no_default_route"},
    "net_no_ipv4_on_iface": {"net_no_ipv4_on_iface"},
    "net_wifi_disconnect": {"net_wifi_disconnected"},
    "net_wifi_auth_fail_wrong_psk": {"net_wifi_auth_fail", "wlan_auth_fail"},
    "net_wrong_default_route": {"net_wrong_default_route"},
}
SUBTYPE_EXPECTED_HINTS = {
    "net_dns_fail": ["DNS host fail", "IP ping OK"],
    "net_public_ip_unreachable": ["target IP fail", "gateway/main probe OK"],
    "net_no_default_route": ["wlan0 IP present", "default route missing"],
    "net_no_ipv4_on_iface": ["wpa_state=COMPLETED", "wlan0 no IPv4"],
    "net_wifi_disconnect": ["wpa_state/interface disconnected"],
    "net_wifi_auth_fail_wrong_psk": ["active auth cycle", "wrong PSK wpa state"],
    "net_wrong_default_route": ["fake default route", "subnet route", "public probe fail"],
}


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for raw in handle:
            line = raw.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def primary_kinds_from_l1(run_id: str) -> List[str]:
    path = Path("inbox/runs") / run_id / "dataset_export" / "evidence_candidates.jsonl"
    kinds = []
    if not path.exists():
        return kinds
    for row in read_jsonl(path):
        if row.get("target") == "primary" or row.get("support_role") == "primary":
            kind = str(row.get("kind", ""))
            if kind and kind not in kinds:
                kinds.append(kind)
    return kinds


def expected_probe_for_epoch(epoch: str) -> str:
    return "8.8.8.8" if epoch.startswith("new:") else "1.1.1.1"


def full_l2_excluded_contamination(batch_dir: Path, excluded_ids: set[str]) -> Dict[str, Any]:
    hits: List[Dict[str, str]] = []
    search_dirs = [batch_dir / "l2"]
    for split in ("train", "val", "test"):
        search_dirs.append(batch_dir / "l2_splits" / split)
    for directory in search_dirs:
        if not directory.exists():
            continue
        for path in directory.glob("*.jsonl"):
            for idx, row in enumerate(read_jsonl(path)):
                run_id = str(row.get("source_case_id") or row.get("case_id") or "")
                if run_id in excluded_ids:
                    hits.append({"file": str(path), "line_index": str(idx), "run_id": run_id})
    return {"checked_dirs": [str(path) for path in search_dirs], "hit_count": len(hits), "hits": hits}


def probe_epoch_check(batch_dir: Path, sampled: List[Dict[str, Any]]) -> Dict[str, Any]:
    manifest_rows = {row["run_id"]: row for row in read_jsonl(batch_dir / "accepted_runs.jsonl")}
    findings = []
    seen_run_ids = []
    for sample in sampled:
        run_id = sample["run_id"]
        if run_id not in seen_run_ids:
            seen_run_ids.append(run_id)
    for run_id in seen_run_ids:
        epoch = manifest_rows.get(run_id, {}).get("probe_epoch", "")
        expected = expected_probe_for_epoch(epoch)
        run_dir = Path("inbox/runs") / run_id
        probe_text = ""
        for rel in ("net/probe_fault.txt", "net/probe_post2.txt", "net/probe_post.txt"):
            path = run_dir / rel
            if path.exists():
                probe_text += path.read_text(encoding="utf-8", errors="ignore") + "\n"
        l2_text = ""
        for task in TASKS:
            row = load_task_sample(batch_dir, task, run_id)
            l2_text += json.dumps(row, ensure_ascii=False) + "\n"
        findings.append(
            {
                "run_id": run_id,
                "probe_epoch": epoch,
                "expected_probe": expected,
                "raw_probe_mentions_expected": expected in probe_text,
                "l2_mentions_old_probe": "1.1.1.1" in l2_text,
                "l2_mentions_new_probe": "8.8.8.8" in l2_text,
            }
        )
    issues = [
        f"{item['run_id']}: raw probes do not mention expected {item['expected_probe']}"
        for item in findings
        if not item["raw_probe_mentions_expected"]
    ]
    issues.extend(
        f"{item['run_id']}: new epoch L2 still mentions old 1.1.1.1"
        for item in findings
        if item["probe_epoch"].startswith("new:") and item["l2_mentions_old_probe"]
    )
    return {"findings": findings, "issue_count": len(issues), "issues": issues}


def load_task_sample(batch_dir: Path, task: str, run_id: str) -> Dict[str, Any]:
    rows = read_jsonl(batch_dir / "l2_splits" / "train" / f"{task}.jsonl")
    for row in rows:
        if row.get("source_case_id") == run_id or row.get("case_id") == run_id:
            return row
    raise SystemExit(f"missing {task} sample for {run_id}")


def sample_subtype(sample: Dict[str, Any], task: str) -> str:
    target = sample.get("target", {})
    if task == "diagnosis":
        return target.get("gt", {}).get("subtype", "")
    if task == "cause_vs_symptom":
        return target.get("primary_subtype", "")
    if task == "action_after_diagnosis":
        return sample.get("input", {}).get("gt", {}).get("subtype", "")
    return ""


def evidence_kinds(items: Iterable[Dict[str, Any]]) -> List[str]:
    kinds: List[str] = []
    for item in items or []:
        kind = str(item.get("kind", ""))
        if kind and kind not in kinds:
            kinds.append(kind)
    return kinds


def audit_sample(sample: Dict[str, Any], task: str, subtype: str, run_id: str, split: str) -> Dict[str, Any]:
    status = "pass"
    issues: List[str] = []
    reason_parts: List[str] = []
    suggested_fix = ""
    expected = EXPECTED_PRIMARY[subtype]
    l1_primary = primary_kinds_from_l1(run_id)

    if task == "diagnosis":
        got_subtype = sample_subtype(sample, task)
        key_kinds = evidence_kinds(sample.get("input", {}).get("key_evidence", []))
        if got_subtype != subtype:
            status = "fail"
            issues.append("label_mismatch")
            reason_parts.append(f"diagnosis target subtype={got_subtype}, expected {subtype}")
        if not expected.intersection(key_kinds):
            status = "warn" if status == "pass" else status
            issues.append("insufficient_evidence")
            reason_parts.append("diagnosis input key_evidence lacks fault-specific primary evidence; it relies on injector/GT-like markers")
            suggested_fix = "Include subtype-specific fault evidence in diagnosis key_evidence, not only injector markers."
        else:
            reason_parts.append("diagnosis target matches subtype and key_evidence includes subtype-specific fault evidence")

    elif task == "evidence_extraction":
        primary = sample.get("target", {}).get("primary_evidence", [])
        primary_kinds = evidence_kinds(primary)
        if not expected.intersection(primary_kinds):
            status = "fail"
            issues.append("insufficient_evidence")
            issues.append("schema_ok_semantic_issue")
            reason_parts.append("target.primary_evidence lacks required subtype-specific fault evidence")
            suggested_fix = "Promote validated fault-phase evidence into L2 primary_evidence for this subtype."
        elif any("recovered" in str(item.get("text", "")).lower() for item in primary):
            status = "fail"
            issues.append("recovery_evidence_as_fault")
            reason_parts.append("primary evidence appears to include recovery-period language")
            suggested_fix = "Keep recovery evidence out of evidence_extraction primary_evidence."
        else:
            reason_parts.append("primary evidence aligns with subtype and fault-period semantics")

    elif task == "cause_vs_symptom":
        target = sample.get("target", {})
        got_subtype = target.get("primary_subtype", "")
        cause_eids = target.get("cause_eids", [])
        symptom_eids = target.get("symptom_eids", [])
        primary_evidence = sample.get("input", {}).get("primary_evidence", [])
        primary_kinds = evidence_kinds(primary_evidence)
        if got_subtype != subtype:
            status = "fail"
            issues.append("label_mismatch")
            reason_parts.append(f"primary_subtype={got_subtype}, expected {subtype}")
        if not expected.intersection(primary_kinds):
            status = "warn" if status == "pass" else status
            issues.append("cause_symptom_confusion")
            reason_parts.append("cause set is correct by GT but supported mainly by injector markers rather than subtype-specific primary evidence")
            suggested_fix = "Feed subtype-specific primary evidence into cause_vs_symptom input and cause_eids."
        elif subtype in {"net_no_default_route", "net_no_ipv4_on_iface", "net_wrong_default_route", "net_wifi_auth_fail_wrong_psk"} and not symptom_eids:
            status = "warn"
            issues.append("noisy_but_acceptable")
            reason_parts.append("primary cause is correct, but downstream symptoms such as ping/DNS fail are not explicitly separated")
            suggested_fix = "Optionally include ping/DNS failures as symptom_eids when present."
        else:
            reason_parts.append("primary cause matches GT and does not promote symptoms over root cause")
        if not cause_eids:
            status = "fail"
            issues.append("insufficient_evidence")
            reason_parts.append("cause_eids is empty")

    elif task == "action_after_diagnosis":
        actions = sample.get("target", {}).get("recommended_actions", [])
        text = json.dumps(actions, ensure_ascii=False).lower()
        dangerous = any(action.get("dangerous_command") is True for action in actions)
        if dangerous or "killall -9 sh" in text:
            status = "fail"
            issues.append("unsafe_action")
            reason_parts.append("action plan contains dangerous operation")
            suggested_fix = "Remove destructive operations and require operator-gated recovery."
        else:
            reason_parts.append("action plan is non-destructive and operator-gated where disruptive")
        if subtype in {"net_dns_fail", "net_public_ip_unreachable", "net_no_default_route", "net_no_ipv4_on_iface", "net_wrong_default_route"} and "reconnect_network_safe" in text:
            status = "warn" if status == "pass" else status
            issues.append("overbroad_recovery")
            reason_parts.append("action is safe but generic; subtype-specific recovery is under-specified")
            suggested_fix = {
                "net_dns_fail": "Prefer DNS/resolv/iptables-DNS restoration before network reconnect.",
                "net_public_ip_unreachable": "Prefer clearing the specific target DROP rule and rechecking target/main probes.",
                "net_no_default_route": "Prefer restoring default route and validating gateway/public/DNS.",
                "net_no_ipv4_on_iface": "Prefer restoring IPv4/default route and noting Guest/AP stale-forwarding fallback.",
                "net_wrong_default_route": "Prefer restoring real default route and verifying gateway/public/DNS.",
            }[subtype]

    else:
        status = "fail"
        issues.append("schema_ok_semantic_issue")
        reason_parts.append(f"unknown task {task}")

    if not issues:
        issues = []
    return {
        "run_id": run_id,
        "subtype": subtype,
        "task": task,
        "split": split,
        "sample_id": sample.get("sample_id", ""),
        "status": status,
        "issue_type": issues,
        "short_reason": "; ".join(reason_parts),
        "evidence_checked": [
            f"L2 sample {sample.get('sample_id', '')}",
            f"L1 primary kinds: {','.join(l1_primary) if l1_primary else 'none'}",
            f"expected semantic hints: {', '.join(SUBTYPE_EXPECTED_HINTS[subtype])}",
        ],
        "suggested_fix": suggested_fix,
    }


def is_relative_to_path(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Semantic spot-check for NET L2 samples. Refuses frozen-batch output by default."
    )
    parser.add_argument("--batch-dir", default=str(BATCH_DIR), help="Batch directory to audit.")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory for quality_audit outputs. Required unless --allow-frozen-output is intentional.",
    )
    parser.add_argument(
        "--allow-frozen-output",
        action="store_true",
        help="Explicitly allow writes under dataset_batches/net_formal_batch_70_20260429.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    batch_dir = Path(args.batch_dir)
    qa_dir = Path(args.output_dir) if args.output_dir else batch_dir / "quality_audit"
    if is_relative_to_path(qa_dir, FROZEN_BATCH_DIR) and not args.allow_frozen_output:
        raise SystemExit(
            "Refusing to write quality_audit under frozen batch. "
            "Use --output-dir outside dataset_batches/net_formal_batch_70_20260429 "
            "or pass --allow-frozen-output for an explicit formal-batch audit."
        )
    qa_dir.mkdir(parents=True, exist_ok=True)

    excluded_ids = {row["run_id"] for row in read_jsonl(batch_dir / "excluded_runs.jsonl")}
    split_rows = read_jsonl(batch_dir / "splits" / "run_splits.jsonl")
    selected: List[Dict[str, Any]] = []
    for subtype in sorted({row["subtype"] for row in split_rows}):
        trains = [row for row in split_rows if row["subtype"] == subtype and row["split"] == "train"]
        if not trains:
            raise SystemExit(f"no train run for {subtype}")
        selected.append(trains[0])

    samples: List[Dict[str, Any]] = []
    sampled_cases: List[Dict[str, Any]] = []
    for selected_row in selected:
        run_id = selected_row["run_id"]
        subtype = selected_row["subtype"]
        if run_id in excluded_ids:
            raise SystemExit(f"selected excluded run: {run_id}")
        for task in TASKS:
            sample = load_task_sample(batch_dir, task, run_id)
            audited = audit_sample(sample, task, subtype, run_id, selected_row["split"])
            samples.append(audited)
            sampled_cases.append(
                {
                    "run_id": run_id,
                    "subtype": subtype,
                    "split": selected_row["split"],
                    "task": task,
                    "sample_id": sample.get("sample_id", ""),
                }
            )

    status_counts = Counter(sample["status"] for sample in samples)
    contamination = full_l2_excluded_contamination(batch_dir, excluded_ids)
    epoch_check = probe_epoch_check(batch_dir, sampled_cases)
    per_task = defaultdict(Counter)
    per_subtype = defaultdict(Counter)
    issue_counts = Counter()
    for sample in samples:
        per_task[sample["task"]][sample["status"]] += 1
        per_subtype[sample["subtype"]][sample["status"]] += 1
        issue_counts.update(sample["issue_type"])

    blockers = []
    if status_counts.get("fail", 0):
        blockers.append("At least one sampled L2 item has semantic fail; do not start SFT until reviewed or regenerated.")
    if contamination["hit_count"]:
        blockers.append("Excluded/quarantine run_id found in full or split L2 files.")
    if epoch_check["issue_count"]:
        blockers.append("Probe epoch consistency issue found in sampled raw/L2 evidence.")

    audit = {
        "batch_id": batch_dir.name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "sample_policy": "First train run per subtype; four L2 task samples from the same run_id for cross-task consistency.",
        "checked_count": len(samples),
        "summary": {
            "pass_count": status_counts.get("pass", 0),
            "warn_count": status_counts.get("warn", 0),
            "fail_count": status_counts.get("fail", 0),
            "issue_type_counts": dict(issue_counts),
            "per_task_summary": {task: dict(per_task[task]) for task in TASKS},
            "per_subtype_summary": {subtype: dict(per_subtype[subtype]) for subtype in sorted(per_subtype)},
            "covered_subtypes": sorted(per_subtype),
            "covered_tasks": TASKS,
            "probe_epoch_check": epoch_check,
            "full_l2_excluded_contamination": contamination,
        },
        "samples": samples,
        "blockers": blockers,
        "recommended_next_step": (
            "Fix/regenerate L2 evidence_extraction for route-based faults before SFT smoke."
            if blockers
            else "Proceed to small SFT smoke; track warn items as generation-rule improvements."
        ),
    }

    (qa_dir / "l2_sample_audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_jsonl(qa_dir / "sampled_cases.jsonl", sampled_cases)

    warn_fail = [sample for sample in samples if sample["status"] != "pass"]
    subtype_lines = "\n".join(
        f"- {subtype}: pass={counts.get('pass',0)}, warn={counts.get('warn',0)}, fail={counts.get('fail',0)}"
        for subtype, counts in sorted(per_subtype.items())
    )
    task_lines = "\n".join(
        f"- {task}: pass={per_task[task].get('pass',0)}, warn={per_task[task].get('warn',0)}, fail={per_task[task].get('fail',0)}"
        for task in TASKS
    )
    detail_lines = "\n".join(
        f"- {s['run_id']} / {s['subtype']} / {s['task']}: {s['status'].upper()} "
        f"({','.join(s['issue_type']) or 'none'}) - {s['short_reason']}"
        for s in warn_fail
    ) or "- None"
    blockers_text = "\n".join(f"- {b}" for b in blockers) or "- None"
    md = f"""# L2 Sample Semantic Audit - {batch_dir.name}

## Overall
- checked_count: {len(samples)}
- pass: {status_counts.get('pass', 0)}
- warn: {status_counts.get('warn', 0)}
- fail: {status_counts.get('fail', 0)}

## Sampling
First train run per subtype; the same run_id is used for diagnosis, evidence_extraction,
cause_vs_symptom, and action_after_diagnosis.

## Per Task
{task_lines}

## Per Subtype
{subtype_lines}

## Warn/Fail Details
{detail_lines}

## Blockers
{blockers_text}

## Probe Epoch Check
- issues: {epoch_check['issue_count']}
- sampled runs checked against expected raw probe endpoint and stale old-probe mentions.

## Full L2 Excluded Contamination Check
- hit_count: {contamination['hit_count']}
- checked full L2 plus train/val/test split L2 files.

## SFT Recommendation
{audit['recommended_next_step']}
"""
    (qa_dir / "l2_sample_audit.md").write_text(md, encoding="utf-8")
    print(json.dumps({"quality_audit_dir": str(qa_dir), "summary": audit["summary"], "blockers": blockers}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
