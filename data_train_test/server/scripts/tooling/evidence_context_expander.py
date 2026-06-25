#!/usr/bin/env python3
"""Read-only preview tool for evidence context expansion.

The tool adapts legacy L1 evidence rows or consumes normalized Evidence Blocks,
selects usable diagnostic observations, and builds conservative case-local
context windows. It writes nothing by default.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, DefaultDict, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple


sys.dont_write_bytecode = True

IGNORED_DIRS = {
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".venv",
    "venv",
    "node_modules",
}

BINARY_SUFFIXES = {
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".ico",
    ".zip",
    ".rar",
    ".7z",
    ".tar",
    ".gz",
    ".bin",
    ".exe",
    ".dll",
    ".so",
    ".pyc",
}

PROTECTED_CONTEXT_FRAGMENTS = {
    "frozen",
    "accepted",
    "training_views",
    "eval",
    ".codex-harness",
    "reports",
    "ledger",
}

PROTECTED_CONTEXT_FILENAMES = {
    "train.jsonl",
    "val.jsonl",
    "test.jsonl",
}

PROTECTED_OUTPUT_FILENAMES = {
    "evidence_candidates.jsonl",
    "diagnosis.jsonl",
    "evidence_extraction.jsonl",
    "cause_vs_symptom.jsonl",
    "action_after_diagnosis.jsonl",
    "train.jsonl",
    "val.jsonl",
    "test.jsonl",
}

PROTECTED_OUTPUT_DIRS = {
    "dataset_batches",
    "demo_public_dataset",
    "frozen",
    "accepted",
    "ledger",
    "eval",
    "training_views",
    "inbox",
    "inbox_net",
}

SENSITIVE_TERMS = (
    "ground_truth",
    "root_cause",
    "gt_",
    "gt_family",
    "gt_subtype",
    "primary_subtype",
    "scenario_tag",
    "fault_type",
    "label",
    "fault_inject",
    "injector",
    "fault_net_",
    "net_dns_fail",
    "net_link_down",
    "net_route_missing",
)

SENSITIVE_PATTERNS = (
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    re.compile(r"\b[0-9a-f]{2}(?::[0-9a-f]{2}){5}\b", re.IGNORECASE),
    re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE),
    re.compile(r"\b(?:callerToken|accessTokenId|ssid|bssid|psk)\b", re.IGNORECASE),
)

SELECTABLE_ROLES = {"root_candidate", "supporting", "propagation"}
MERGE_GAP_LINES = 2
SOURCE_MANIFEST_CACHE: Dict[Path, Dict[str, Any]] = {}


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Preview context expansion for normalized evidence blocks.")
    parser.add_argument("--root", default=".", help="Repository root. Default: current directory.")
    parser.add_argument("--input", default=None, help="Optional evidence_candidates.jsonl file or directory.")
    parser.add_argument("--blocks-jsonl", default=None, help="Optional normalized Evidence Block JSONL input.")
    parser.add_argument("--stdout-only", action="store_true", help="Force stdout output only.")
    parser.add_argument("--dry-run", action="store_true", help="Explicit safety flag; the tool is read-only by default.")
    parser.add_argument("--format", choices=("json", "markdown", "both", "jsonl"), default="both")
    parser.add_argument("--max-examples", type=int, default=10)
    parser.add_argument("--limit", type=int, default=None, help="Optional maximum output rows.")
    parser.add_argument("--before", type=int, default=4)
    parser.add_argument("--after", type=int, default=6)
    parser.add_argument("--max-blocks-per-case", type=int, default=8)
    parser.add_argument("--max-chars-per-case", type=int, default=12000)
    parser.add_argument("--min-utility", type=float, default=0.25)
    parser.add_argument("--include-symptoms", action="store_true")
    parser.add_argument("--include-excluded", action="store_true")
    parser.add_argument("--redact-sensitive", dest="redact_sensitive", action="store_true", default=True)
    parser.add_argument("--no-redact-sensitive", dest="redact_sensitive", action="store_false")
    parser.add_argument("--output", default=None, help="Optional explicit output path.")
    return parser.parse_args(argv)


def safe_json_dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def read_text_lossy(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def is_relative_to_path(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def rel_path(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def path_has_ignored_part(path: Path) -> bool:
    return any(part in IGNORED_DIRS for part in path.parts)


def iter_evidence_files(root: Path, input_path: Optional[str] = None) -> Iterator[Path]:
    if input_path:
        selected = Path(input_path)
        if not selected.is_absolute():
            selected = root / selected
        if selected.is_file():
            if selected.suffix.lower() not in BINARY_SUFFIXES:
                yield selected
            return
        if selected.is_dir():
            base = selected
        else:
            return
    else:
        base = root

    for path in sorted(base.rglob("evidence_candidates.jsonl")):
        if path_has_ignored_part(path):
            continue
        if path.is_file() and path.suffix.lower() not in BINARY_SUFFIXES:
            yield path


def read_jsonl(path: Path, root: Path, max_examples: int) -> Tuple[List[Tuple[int, Dict[str, Any]]], List[Dict[str, Any]]]:
    rows: List[Tuple[int, Dict[str, Any]]] = []
    errors: List[Dict[str, Any]] = []
    try:
        text = read_text_lossy(path)
    except OSError as exc:
        return rows, [{"file": rel_path(path, root), "line": None, "error": str(exc), "snippet": ""}]

    for line_no, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            if len(errors) < max_examples:
                errors.append({"file": rel_path(path, root), "line": line_no, "error": str(exc), "snippet": line[:240]})
            continue
        if isinstance(value, dict):
            rows.append((line_no, value))
        elif len(errors) < max_examples:
            errors.append({"file": rel_path(path, root), "line": line_no, "error": "JSONL row is not an object", "snippet": line[:240]})
    return rows, errors


def load_adapter() -> Any:
    adapter_path = Path(__file__).resolve().with_name("evidence_block_schema_adapter.py")
    spec = importlib.util.spec_from_file_location("evidence_block_schema_adapter_local", adapter_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load adapter from {adapter_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_normalized_blocks_from_jsonl(path: Path, root: Path, max_examples: int) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    rows, errors = read_jsonl(path, root, max_examples)
    blocks: List[Dict[str, Any]] = []
    for line_no, row in rows:
        row.setdefault("_source_jsonl", rel_path(path, root))
        row.setdefault("_source_line", line_no)
        blocks.append(row)
    return blocks, errors


def adapt_legacy_rows(root: Path, input_path: Optional[str], max_examples: int) -> Tuple[List[Path], List[Dict[str, Any]], List[Dict[str, Any]], int]:
    adapter = load_adapter()
    files = list(iter_evidence_files(root, input_path))
    blocks: List[Dict[str, Any]] = []
    parse_errors: List[Dict[str, Any]] = []
    case_seq: DefaultDict[str, int] = defaultdict(int)
    evidence_rows = 0
    for path in files:
        rows, errors = adapter.read_jsonl(path, root, max_examples)
        parse_errors.extend(errors)
        evidence_rows += len(rows)
        for line_no, row in rows:
            blocks.append(adapter.adapt_row(row, path, line_no, root, case_seq))
    return files, blocks, parse_errors, evidence_rows


def has_risk(block: Dict[str, Any], risk: str) -> bool:
    return risk in set(block.get("risk_flags") or [])


def is_injector_block(block: Dict[str, Any]) -> bool:
    return (
        block.get("source") == "fault_inject"
        or block.get("anomaly_type") == "injector_marker"
        or block.get("causal_role") == "excluded_provenance"
        or has_risk(block, "injector_marker")
    )


def is_selected_candidate(block: Dict[str, Any], args: argparse.Namespace, allow_symptom: bool = False) -> Tuple[bool, str]:
    if is_injector_block(block):
        return False, "injector"
    if not block.get("usable_for_diagnosis"):
        return False, "nonusable"
    if has_risk(block, "generic_noise") or block.get("anomaly_type") == "noise":
        return False, "generic_noise"
    if float(block.get("utility_score") or 0.0) < args.min_utility:
        return False, "below_min_utility"
    if has_risk(block, "recovery_primary"):
        return False, "recovery_primary"

    role = str(block.get("causal_role") or "unknown")
    anomaly = str(block.get("anomaly_type") or "unknown")
    utility = float(block.get("utility_score") or 0.0)
    if role in SELECTABLE_ROLES:
        return True, "selected"
    if role == "symptom":
        return (allow_symptom, "selected" if allow_symptom else "symptom_deferred")
    if role == "unknown" and anomaly != "unknown" and utility >= max(args.min_utility, 0.60):
        return True, "selected"
    return False, "role_not_selected"


def select_blocks(blocks: Sequence[Dict[str, Any]], args: argparse.Namespace) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    counts = Counter()
    standard_by_case: DefaultDict[str, List[Dict[str, Any]]] = defaultdict(list)
    symptoms_by_case: DefaultDict[str, List[Dict[str, Any]]] = defaultdict(list)

    for block in blocks:
        if block.get("usable_for_diagnosis"):
            counts["usable_considered"] += 1
        selected, reason = is_selected_candidate(block, args, allow_symptom=args.include_symptoms)
        if selected:
            role = str(block.get("causal_role") or "unknown")
            if role == "symptom":
                symptoms_by_case[str(block.get("case_id") or "UNKNOWN")].append(block)
            else:
                standard_by_case[str(block.get("case_id") or "UNKNOWN")].append(block)
            continue
        if reason == "injector":
            counts["excluded_injector"] += 1
        elif reason == "nonusable":
            counts["excluded_nonusable"] += 1
        elif reason == "below_min_utility":
            counts["excluded_below_min_utility"] += 1
        elif reason == "generic_noise":
            counts["excluded_generic_noise"] += 1
        elif reason == "recovery_primary":
            counts["excluded_recovery_primary"] += 1
        elif reason == "symptom_deferred":
            symptoms_by_case[str(block.get("case_id") or "UNKNOWN")].append(block)

    selected_blocks: List[Dict[str, Any]] = []
    all_cases = set(standard_by_case) | set(symptoms_by_case)
    for case_id in sorted(all_cases):
        selected_blocks.extend(standard_by_case.get(case_id, []))
        if args.include_symptoms or not standard_by_case.get(case_id):
            selected_blocks.extend(symptoms_by_case.get(case_id, []))

    for block in selected_blocks:
        role = block.get("causal_role")
        if role == "root_candidate":
            counts["selected_root_candidates"] += 1
        elif role == "supporting":
            counts["selected_supporting"] += 1
        elif role == "symptom":
            counts["selected_symptoms"] += 1
    return selected_blocks, dict(counts)


def is_protected_context_path(path: Path) -> bool:
    parts = [part.lower() for part in path.parts]
    if path.name.lower() in PROTECTED_CONTEXT_FILENAMES:
        return True
    return any(part in PROTECTED_CONTEXT_FRAGMENTS for part in parts)


def context_roots(case_dir: Path, root: Path) -> List[Path]:
    roots = [case_dir]
    if case_dir.name == "dataset_export":
        roots.append(case_dir.parent)
    if root not in roots:
        roots.append(root)
    unique: List[Path] = []
    for item in roots:
        resolved = item.resolve()
        if resolved not in unique:
            unique.append(resolved)
    return unique


def read_source_manifest(case_dir: Path, run_root: Path) -> Dict[str, Any]:
    manifest_path = case_dir / "source_manifest.json"
    if not manifest_path.is_file():
        manifest_path = run_root / "source_manifest.json"
    resolved = manifest_path.resolve()
    if resolved in SOURCE_MANIFEST_CACHE:
        return SOURCE_MANIFEST_CACHE[resolved]
    try:
        data = json.loads(read_text_lossy(resolved))
    except (OSError, json.JSONDecodeError):
        data = {}
    SOURCE_MANIFEST_CACHE[resolved] = data if isinstance(data, dict) else {}
    return SOURCE_MANIFEST_CACHE[resolved]


def manifest_rel_paths(manifest: Dict[str, Any], key: str) -> List[str]:
    key_l = key.lower().replace("\\", "/").strip("/")
    results: List[str] = []
    files = manifest.get("files")
    if not isinstance(files, list):
        return results
    for item in files:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").lower()
        rel = str(item.get("rel_path") or "")
        rel_norm = rel.lower().replace("\\", "/").strip("/")
        if key_l and (key_l == name or key_l == rel_norm or key_l in {rel_norm.split("/")[0], rel_norm}):
            results.append(rel)
    return results


def is_evidence_jsonl(path: Path) -> bool:
    return path.name.lower() == "evidence_candidates.jsonl"


def directory_context_candidates(directory: Path, block: Dict[str, Any]) -> List[Path]:
    if not directory.is_dir():
        return []
    name = directory.name.lower()
    kind = str(block.get("legacy_kind") or "").lower()
    source = str(block.get("source") or "").lower()
    candidates: List[Path] = []
    if name == "net" or source in {"dns", "route", "wifi", "state"}:
        preferred = ["probe_fault.txt", "net_fault.txt", "probe_post.txt", "net_post.txt", "probe_pre.txt", "net_pre.txt"]
        candidates.extend(directory / item for item in preferred)
    elif name == "procs" or source == "process":
        candidates.extend(sorted(directory.glob("procs_*.txt"))[:5])
    elif "faultlog" in name:
        candidates.extend(sorted(path for path in directory.iterdir() if path.is_file())[:5])
    elif "_probe_dmesg_recv" in name or "dmesg" in kind:
        candidates.extend(directory / item for item in ("_probe_dmesg_after.log", "_probe_dmesg_before.log"))
    return [path for path in candidates if path.is_file()]


def candidate_paths_from_ref(file_value: Any, root: Path, case_dir: Path, run_root: Path) -> List[Path]:
    if file_value in (None, ""):
        return []
    raw = Path(str(file_value))
    candidates: List[Path] = []
    if raw.is_absolute():
        candidates.append(raw)
    else:
        candidates.append(case_dir / raw)
        candidates.append(run_root / raw)
        candidates.append(root / raw)
    return candidates


def resolve_context_source(block: Dict[str, Any], root: Path) -> Tuple[Optional[Path], str, List[str], int]:
    notes: List[str] = []
    protected_skipped = 0
    case_rel = block.get("case_rel") or ""
    case_dir = root / str(case_rel) if case_rel else root
    roots = context_roots(case_dir, root)
    run_root = roots[1] if case_dir.name == "dataset_export" and len(roots) > 1 else case_dir
    manifest = read_source_manifest(case_dir, run_root)
    raw_refs = block.get("raw_refs") if isinstance(block.get("raw_refs"), list) else []

    candidates: List[Tuple[Path, str]] = []
    for ref in raw_refs:
        if not isinstance(ref, dict):
            continue
        for candidate in candidate_paths_from_ref(ref.get("file"), root, case_dir, run_root):
            candidates.append((candidate, "resolved_from_raw_ref"))

    source_rel = block.get("legacy_source_rel") or block.get("source_rel")
    if source_rel:
        for manifest_rel in manifest_rel_paths(manifest, str(source_rel)):
            for candidate in candidate_paths_from_ref(manifest_rel, root, case_dir, run_root):
                candidates.append((candidate, "resolved_from_source_rel"))
        for candidate in candidate_paths_from_ref(source_rel, root, case_dir, run_root):
            candidates.append((candidate, "resolved_from_source_rel"))

    seen = set()
    for candidate, reason in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if not any(is_relative_to_path(resolved, allowed_root) for allowed_root in roots):
            notes.append(f"context path outside case/run/repo roots skipped: {resolved}")
            continue
        if is_protected_context_path(resolved):
            protected_skipped += 1
            notes.append(f"protected context path skipped: {resolved}")
            continue
        expanded_candidates = directory_context_candidates(resolved, block) if resolved.is_dir() else [resolved]
        for expanded in expanded_candidates:
            expanded_resolved = expanded.resolve()
            if not any(is_relative_to_path(expanded_resolved, allowed_root) for allowed_root in roots):
                notes.append(f"context path outside case/run/repo roots skipped: {expanded_resolved}")
                continue
            if is_protected_context_path(expanded_resolved):
                protected_skipped += 1
                notes.append(f"protected context path skipped: {expanded_resolved}")
                continue
            if expanded_resolved.is_file() and not is_evidence_jsonl(expanded_resolved) and expanded_resolved.suffix.lower() not in BINARY_SUFFIXES:
                return expanded_resolved, reason, notes, protected_skipped
    return None, "not_found", notes, protected_skipped


def line_range_from_span(span: Any) -> Tuple[Optional[int], Optional[int]]:
    if isinstance(span, dict):
        keys = (
            ("line_start", "line_end"),
            ("start_line", "end_line"),
            ("line", "line"),
            ("start", "end"),
        )
        for start_key, end_key in keys:
            if start_key in span:
                try:
                    start = int(span.get(start_key))
                    end = int(span.get(end_key, start))
                    if start > 0 and end >= start:
                        return start, end
                except (TypeError, ValueError):
                    pass
    if isinstance(span, list) and len(span) == 2:
        try:
            start = int(span[0])
            end = int(span[1])
            if start > 0 and end >= start:
                return start, end
        except (TypeError, ValueError):
            pass
    return None, None


def infer_line_range(block: Dict[str, Any], source_path: Path) -> Tuple[Optional[int], Optional[int], str]:
    raw_refs = block.get("raw_refs") if isinstance(block.get("raw_refs"), list) else []
    for ref in raw_refs:
        if not isinstance(ref, dict):
            continue
        ref_file = ref.get("file")
        if ref_file:
            ref_path = Path(str(ref_file))
            if ref_path.name and ref_path.name != source_path.name:
                continue
        try:
            start = int(ref.get("line_start"))
            end = int(ref.get("line_end", start))
            if start > 0 and end >= start:
                return start, end, "raw_ref_line"
        except (TypeError, ValueError):
            pass
        start, end = line_range_from_span(ref.get("span"))
        if start is not None:
            return start, end, "span_line"

    start, end = line_range_from_span(block.get("span"))
    if start is not None:
        return start, end, "span_line"

    found = find_text_line(source_path, block)
    if found is not None:
        return found, found, "text_search_first_match"
    return None, None, "line_not_found"


def distinctive_needles(block: Dict[str, Any]) -> List[str]:
    raw_observation = str(block.get("raw_observation") or "")
    text = raw_observation.strip()
    kind = str(block.get("legacy_kind") or "").lower()
    anomaly = str(block.get("anomaly_type") or "").lower()
    needles: List[str] = []
    if not text:
        compact = ""
    else:
        needles.append(text)
        compact = " ".join(text.split())
    if len(compact) > 80:
        needles.append(compact[:80])
    elif len(compact) >= 20:
        needles.append(compact)
    process_match = re.search(r"\bprocess\s+([A-Za-z0-9_.:-]+)", text)
    if process_match:
        needles.append(process_match.group(1))
    if "dns" in kind or anomaly == "dns_failure":
        needles.extend(["dns_host_probe_primary", "Try again", "nslookup", "no such host"])
    if "ping" in kind or anomaly == "reachability_loss":
        needles.extend(["Network unreachable", "packet loss", "ping:"])
    if "auth" in kind or anomaly == "auth_failure":
        needles.extend(["wpa_cli_probe", "wpa_state", "4WAY_HANDSHAKE", "ASSOCIATING", "SCANNING"])
    if "route" in kind or anomaly == "route_missing":
        needles.extend(["proc_net_route", "default route", "Gateway"])
    if "iface" in kind or anomaly in {"disconnect", "link_flap", "ip_config_missing"}:
        needles.extend(["ifconfig_wlan", "link_state_sysfs", "wlan0"])
    deduped: List[str] = []
    for needle in needles:
        if needle and needle not in deduped:
            deduped.append(needle)
    return deduped


def find_text_line(source_path: Path, block: Dict[str, Any]) -> Optional[int]:
    try:
        lines = read_text_lossy(source_path).splitlines()
    except OSError:
        return None
    needles = distinctive_needles(block)
    for needle in needles:
        if not needle:
            continue
        needle_l = needle.lower()
        for idx, line in enumerate(lines, 1):
            if needle_l in line.lower():
                return idx
    return None


def read_context_window(
    source_path: Path,
    line_start: int,
    line_end: int,
    before: int,
    after: int,
) -> Tuple[int, int, List[Tuple[int, str]], List[Tuple[int, str]], List[Tuple[int, str]]]:
    lines = read_text_lossy(source_path).splitlines()
    total = len(lines)
    start = max(1, line_start - max(0, before))
    end = min(total, line_end + max(0, after))
    before_lines = [(idx, lines[idx - 1]) for idx in range(start, max(start, line_start))]
    key_lines = [(idx, lines[idx - 1]) for idx in range(line_start, min(line_end, total) + 1)]
    after_lines = [(idx, lines[idx - 1]) for idx in range(min(line_end, total) + 1, end + 1)]
    return start, end, before_lines, key_lines, after_lines


def is_sensitive_line(line: str) -> bool:
    text = line.lower()
    return any(term in text for term in SENSITIVE_TERMS) or any(pattern.search(line) for pattern in SENSITIVE_PATTERNS)


def redact_sensitive_lines(lines: List[Tuple[Optional[int], str]], enabled: bool) -> Tuple[List[str], List[Dict[str, Any]], bool]:
    output: List[str] = []
    redactions: List[Dict[str, Any]] = []
    sensitive_unredacted = False
    for line_no, line in lines:
        if is_sensitive_line(line):
            redactions.append({"line": line_no, "reason": "gt_like_or_injector_label"})
            if enabled:
                output.append("[REDACTED_SENSITIVE_CONTEXT]")
            else:
                sensitive_unredacted = True
                output.append(line)
        else:
            output.append(line)
    return output, redactions, sensitive_unredacted


def inline_expanded_block(block: Dict[str, Any], reason: str, notes: List[str], redact_sensitive: bool = True) -> Dict[str, Any]:
    key_text, redactions, sensitive_unredacted = redact_sensitive_lines(
        [(None, str(block.get("raw_observation") or ""))],
        redact_sensitive,
    )
    risk_flags = sorted(set(block.get("risk_flags") or []))
    if redactions and redact_sensitive:
        risk_flags = sorted(set(risk_flags + ["sensitive_context_redacted"]))
    if sensitive_unredacted:
        risk_flags = sorted(set(risk_flags + ["sensitive_context_unredacted"]))
    key_observation = key_text[0] if key_text else str(block.get("raw_observation") or "")
    return {
        "expanded_id": "",
        "case_id": block.get("case_id"),
        "case_rel": block.get("case_rel"),
        "source_file": None,
        "context_found": False,
        "context_reason": reason,
        "line_start": None,
        "line_end": None,
        "key_line_start": None,
        "key_line_end": None,
        "context_before": [],
        "key_observation": key_observation,
        "context_after": [],
        "context_block": key_observation,
        "merged_evidence_ids": [block.get("evidence_id")],
        "legacy_eids": [block.get("legacy_eid")] if block.get("legacy_eid") else [],
        "sources": [block.get("source")],
        "objects": [block.get("object")],
        "anomaly_types": [block.get("anomaly_type")],
        "causal_roles": [block.get("causal_role")],
        "max_utility_score": float(block.get("utility_score") or 0.0),
        "block_score": 0.0,
        "density_score": 0.0,
        "usable_for_diagnosis": True,
        "risk_flags": risk_flags,
        "redactions": redactions,
        "raw_refs": block.get("raw_refs") or [],
        "notes": "; ".join(note for note in notes if note),
    }


def make_expanded_block(
    block: Dict[str, Any],
    source_path: Optional[Path],
    root: Path,
    args: argparse.Namespace,
    reason: str,
    notes: List[str],
) -> Dict[str, Any]:
    if source_path is None:
        expanded = inline_expanded_block(block, reason, notes, args.redact_sensitive)
        expanded["block_score"] = score_expanded_block(expanded)
        expanded["density_score"] = density_score(expanded)
        return expanded

    line_start, line_end, line_reason = infer_line_range(block, source_path)
    if line_start is None or line_end is None:
        notes = notes + ["line range not found; inline-only fallback"]
        expanded = inline_expanded_block(block, "not_found", notes, args.redact_sensitive)
        expanded["block_score"] = score_expanded_block(expanded)
        expanded["density_score"] = density_score(expanded)
        return expanded

    try:
        window_start, window_end, before_lines, key_lines, after_lines = read_context_window(
            source_path, line_start, line_end, args.before, args.after
        )
    except OSError as exc:
        expanded = inline_expanded_block(block, "not_found", notes + [f"context read failed: {exc}"], args.redact_sensitive)
        expanded["block_score"] = score_expanded_block(expanded)
        expanded["density_score"] = density_score(expanded)
        return expanded

    before_text, before_redactions, before_unredacted = redact_sensitive_lines(before_lines, args.redact_sensitive)
    key_text, key_redactions, key_unredacted = redact_sensitive_lines(key_lines, args.redact_sensitive)
    after_text, after_redactions, after_unredacted = redact_sensitive_lines(after_lines, args.redact_sensitive)
    redactions = before_redactions + key_redactions + after_redactions
    risk_flags = sorted(set(block.get("risk_flags") or []))
    if redactions and args.redact_sensitive:
        risk_flags = sorted(set(risk_flags + ["sensitive_context_redacted"]))
    if before_unredacted or key_unredacted or after_unredacted:
        risk_flags = sorted(set(risk_flags + ["sensitive_context_unredacted"]))

    context_lines = before_text + key_text + after_text
    line_texts = (
        [(line_no, text) for (line_no, _), text in zip(before_lines, before_text)]
        + [(line_no, text) for (line_no, _), text in zip(key_lines, key_text)]
        + [(line_no, text) for (line_no, _), text in zip(after_lines, after_text)]
    )
    expanded = {
        "expanded_id": "",
        "case_id": block.get("case_id"),
        "case_rel": block.get("case_rel"),
        "source_file": rel_path(source_path, root),
        "context_found": True,
        "context_reason": "resolved_by_text_search" if line_reason == "text_search_first_match" else reason,
        "line_start": window_start,
        "line_end": window_end,
        "key_line_start": line_start,
        "key_line_end": line_end,
        "context_before": before_text,
        "key_observation": "\n".join(key_text) if key_text else str(block.get("raw_observation") or ""),
        "context_after": after_text,
        "context_block": "\n".join(context_lines),
        "merged_evidence_ids": [block.get("evidence_id")],
        "legacy_eids": [block.get("legacy_eid")] if block.get("legacy_eid") else [],
        "sources": [block.get("source")],
        "objects": [block.get("object")],
        "anomaly_types": [block.get("anomaly_type")],
        "causal_roles": [block.get("causal_role")],
        "max_utility_score": float(block.get("utility_score") or 0.0),
        "block_score": 0.0,
        "density_score": 0.0,
        "usable_for_diagnosis": True,
        "risk_flags": risk_flags,
        "redactions": redactions,
        "raw_refs": block.get("raw_refs") or [],
        "notes": "; ".join(note for note in notes + ([line_reason] if line_reason == "text_search_first_match" else []) if note),
        "_line_texts": line_texts,
    }
    expanded["block_score"] = score_expanded_block(expanded)
    expanded["density_score"] = density_score(expanded)
    return expanded


def distinct_count(values: Iterable[Any]) -> int:
    return len({str(value) for value in values if value not in (None, "", "unknown")})


def score_expanded_block(block: Dict[str, Any]) -> float:
    score = float(block.get("max_utility_score") or 0.0)
    score += 0.10 * min(3, distinct_count(block.get("anomaly_types") or []))
    score += 0.05 * min(3, distinct_count(block.get("objects") or []))
    roles = set(block.get("causal_roles") or [])
    if "root_candidate" in roles:
        score += 0.05
    if "supporting" in roles:
        score += 0.03
    if not block.get("context_found"):
        score -= 0.30
    risks = set(block.get("risk_flags") or [])
    if "injector_marker" in risks or block.get("source") == "fault_inject":
        score -= 0.50
    if "sensitive_context_unredacted" in risks:
        score -= 0.30
    return round(max(0.0, min(2.0, score)), 4)


def context_line_count(block: Dict[str, Any]) -> int:
    if block.get("line_start") and block.get("line_end"):
        return max(1, int(block["line_end"]) - int(block["line_start"]) + 1)
    text = block.get("context_block") or ""
    return max(1, len(str(text).splitlines()) or 1)


def density_score(block: Dict[str, Any]) -> float:
    return round(float(block.get("block_score") or 0.0) / max(1, context_line_count(block)), 6)


def merge_two_blocks(base: Dict[str, Any], other: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    merged["line_start"] = min(int(base["line_start"]), int(other["line_start"]))
    merged["line_end"] = max(int(base["line_end"]), int(other["line_end"]))
    merged["key_line_start"] = min(int(base["key_line_start"]), int(other["key_line_start"]))
    merged["key_line_end"] = max(int(base["key_line_end"]), int(other["key_line_end"]))
    for key in ("merged_evidence_ids", "legacy_eids", "sources", "objects", "anomaly_types", "causal_roles", "risk_flags"):
        merged[key] = sorted({value for value in (base.get(key) or []) + (other.get(key) or []) if value not in (None, "")})
    merged["max_utility_score"] = max(float(base.get("max_utility_score") or 0.0), float(other.get("max_utility_score") or 0.0))
    redaction_map: Dict[Tuple[Any, Any], Dict[str, Any]] = {}
    for redaction in (base.get("redactions") or []) + (other.get("redactions") or []):
        if not isinstance(redaction, dict):
            continue
        redaction_map[(redaction.get("line"), redaction.get("reason"))] = redaction
    merged["redactions"] = [redaction_map[key] for key in sorted(redaction_map, key=lambda item: (str(item[0]), str(item[1])))]
    merged["raw_refs"] = (base.get("raw_refs") or []) + (other.get("raw_refs") or [])
    key_parts = [str(base.get("key_observation") or ""), str(other.get("key_observation") or "")]
    merged["key_observation"] = "\n---\n".join(part for part in key_parts if part)
    merged["context_before"] = []
    merged["context_after"] = []
    line_map: Dict[int, str] = {}
    for line_no, text in (base.get("_line_texts") or []) + (other.get("_line_texts") or []):
        try:
            line_map[int(line_no)] = str(text)
        except (TypeError, ValueError):
            pass
    if line_map:
        merged["_line_texts"] = [(line_no, line_map[line_no]) for line_no in sorted(line_map)]
        merged["context_block"] = "\n".join(text for _, text in merged["_line_texts"])
    else:
        seen_lines = []
        for text in "\n".join(str(part or "") for part in (base.get("context_block"), other.get("context_block"))).splitlines():
            if text not in seen_lines:
                seen_lines.append(text)
        merged["context_block"] = "\n".join(seen_lines)
    merged["notes"] = "; ".join(part for part in (base.get("notes"), other.get("notes"), "merged overlapping context window") if part)
    merged["block_score"] = score_expanded_block(merged)
    merged["density_score"] = density_score(merged)
    return merged


def merge_overlapping_blocks(blocks: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    grouped: DefaultDict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    output: List[Dict[str, Any]] = []
    for block in blocks:
        if not block.get("context_found") or not block.get("source_file"):
            output.append(block)
            continue
        grouped[(str(block.get("case_id")), str(block.get("source_file")))].append(block)

    for group in grouped.values():
        ordered = sorted(group, key=lambda item: (int(item.get("line_start") or 0), int(item.get("line_end") or 0)))
        current: Optional[Dict[str, Any]] = None
        for block in ordered:
            if current is None:
                current = block
                continue
            if int(block["line_start"]) <= int(current["line_end"]) + MERGE_GAP_LINES:
                current = merge_two_blocks(current, block)
            else:
                output.append(current)
                current = block
        if current is not None:
            output.append(current)
    return output


def assign_expanded_ids(blocks: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    output = []
    for idx, block in enumerate(blocks, 1):
        item = dict(block)
        item["expanded_id"] = f"C{idx:03d}"
        item.pop("_line_texts", None)
        output.append(item)
    return output


def prune_blocks_by_case(
    blocks: Sequence[Dict[str, Any]],
    max_blocks_per_case: int,
    max_chars_per_case: int,
) -> Tuple[List[Dict[str, Any]], Dict[str, int], List[Dict[str, Any]]]:
    by_case: DefaultDict[str, List[Dict[str, Any]]] = defaultdict(list)
    for block in blocks:
        by_case[str(block.get("case_id") or "UNKNOWN")].append(block)

    pruned_counts = Counter()
    pruned_examples: List[Dict[str, Any]] = []
    kept: List[Dict[str, Any]] = []
    for case_id, case_blocks in sorted(by_case.items()):
        ordered = sorted(
            case_blocks,
            key=lambda item: (
                -float(item.get("block_score") or 0.0),
                -float(item.get("density_score") or 0.0),
                len(str(item.get("context_block") or "")),
            ),
        )
        char_count = 0
        count_kept = 0
        for block in ordered:
            block_chars = len(str(block.get("context_block") or ""))
            if count_kept >= max_blocks_per_case:
                pruned_counts["blocks_pruned_by_count"] += 1
                if len(pruned_examples) < 10:
                    pruned_examples.append(example_from_block(block))
                continue
            if char_count + block_chars > max_chars_per_case and count_kept > 0:
                pruned_counts["blocks_pruned_by_char_budget"] += 1
                if len(pruned_examples) < 10:
                    pruned_examples.append(example_from_block(block))
                continue
            kept.append(block)
            count_kept += 1
            char_count += block_chars
    pruned_counts["cases"] = len(by_case)
    return kept, dict(pruned_counts), pruned_examples


def example_from_block(block: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "expanded_id": block.get("expanded_id"),
        "case_id": block.get("case_id"),
        "source_file": block.get("source_file"),
        "context_found": block.get("context_found"),
        "context_reason": block.get("context_reason"),
        "merged_evidence_ids": block.get("merged_evidence_ids"),
        "legacy_eids": block.get("legacy_eids"),
        "anomaly_types": block.get("anomaly_types"),
        "causal_roles": block.get("causal_roles"),
        "block_score": block.get("block_score"),
        "risk_flags": block.get("risk_flags"),
        "preview": str(block.get("context_block") or "")[:160],
    }


def example_from_normalized_block(block: Dict[str, Any], reason: str) -> Dict[str, Any]:
    raw_observation = str(block.get("raw_observation") or "")
    return {
        "selection_reason": reason,
        "evidence_id": block.get("evidence_id"),
        "legacy_eid": block.get("legacy_eid"),
        "case_id": block.get("case_id"),
        "case_rel": block.get("case_rel"),
        "source": block.get("source"),
        "anomaly_type": block.get("anomaly_type"),
        "causal_role": block.get("causal_role"),
        "usable_for_diagnosis": block.get("usable_for_diagnosis"),
        "utility_score": block.get("utility_score"),
        "risk_flags": block.get("risk_flags") or [],
        "exclusion_reason": block.get("exclusion_reason"),
        "raw_observation": raw_observation[:200],
    }


def excluded_examples_for_report(blocks: Sequence[Dict[str, Any]], args: argparse.Namespace) -> List[Dict[str, Any]]:
    if not args.include_excluded:
        return []
    examples: List[Dict[str, Any]] = []
    for block in blocks:
        selected, reason = is_selected_candidate(block, args, allow_symptom=args.include_symptoms)
        if selected:
            continue
        if reason in {"injector", "nonusable", "generic_noise", "recovery_primary", "below_min_utility", "role_not_selected"}:
            examples.append(example_from_normalized_block(block, reason))
        if len(examples) >= args.max_examples:
            break
    return examples


def expand_blocks(blocks: Sequence[Dict[str, Any]], root: Path, args: argparse.Namespace) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    resolution = Counter()
    expanded: List[Dict[str, Any]] = []
    for block in blocks:
        source_path, reason, notes, protected_skipped = resolve_context_source(block, root)
        if protected_skipped:
            resolution["protected_path_skipped"] += protected_skipped
        expanded_block = make_expanded_block(block, source_path, root, args, reason, notes)
        if expanded_block.get("context_found"):
            resolution[expanded_block.get("context_reason") or reason] += 1
        else:
            resolution["inline_only"] += 1
            resolution["not_found"] += 1
        expanded.append(expanded_block)
    return expanded, dict(resolution)


def build_summary(
    root: Path,
    dry_run: bool,
    evidence_files: Sequence[Path],
    evidence_rows: int,
    normalized_blocks: Sequence[Dict[str, Any]],
    selected_blocks: Sequence[Dict[str, Any]],
    expanded_blocks: Sequence[Dict[str, Any]],
    merged_blocks: Sequence[Dict[str, Any]],
    output_blocks: Sequence[Dict[str, Any]],
    parse_errors: Sequence[Dict[str, Any]],
    selection_counts: Dict[str, int],
    resolution_counts: Dict[str, int],
    pruning_counts: Dict[str, int],
    pruned_examples: Sequence[Dict[str, Any]],
    args: argparse.Namespace,
) -> Dict[str, Any]:
    distributions = {
        "source": Counter(),
        "object": Counter(),
        "anomaly_type": Counter(),
        "causal_role": Counter(),
        "risk_flags": Counter(),
    }
    redaction = Counter()
    for block in output_blocks:
        for value in block.get("sources") or []:
            distributions["source"][str(value)] += 1
        for value in block.get("objects") or []:
            distributions["object"][str(value)] += 1
        for value in block.get("anomaly_types") or []:
            distributions["anomaly_type"][str(value)] += 1
        for value in block.get("causal_roles") or []:
            distributions["causal_role"][str(value)] += 1
        for value in block.get("risk_flags") or []:
            distributions["risk_flags"][str(value)] += 1
        if block.get("redactions"):
            redaction["blocks_with_redactions"] += 1
            redaction["lines_redacted"] += len(block.get("redactions") or [])

    examples = {
        "expanded_blocks": [example_from_block(block) for block in output_blocks[: args.max_examples]],
        "inline_only": [example_from_block(block) for block in output_blocks if not block.get("context_found")][: args.max_examples],
        "redacted_blocks": [example_from_block(block) for block in output_blocks if block.get("redactions")][: args.max_examples],
        "pruned_blocks": list(pruned_examples)[: args.max_examples],
        "excluded_blocks": excluded_examples_for_report(normalized_blocks, args),
        "parse_errors": list(parse_errors)[: args.max_examples],
    }

    return {
        "root": str(root),
        "dry_run": bool(dry_run),
        "counts": {
            "evidence_files": len(evidence_files),
            "evidence_rows": evidence_rows,
            "normalized_blocks": len(normalized_blocks),
            "selected_blocks": len(selected_blocks),
            "expanded_blocks": len(expanded_blocks),
            "merged_blocks": len(merged_blocks),
            "output_blocks": len(output_blocks),
            "parse_errors": len(parse_errors),
        },
        "selection": {
            "usable_considered": selection_counts.get("usable_considered", 0),
            "excluded_nonusable": selection_counts.get("excluded_nonusable", 0),
            "excluded_below_min_utility": selection_counts.get("excluded_below_min_utility", 0),
            "excluded_injector": selection_counts.get("excluded_injector", 0),
            "excluded_generic_noise": selection_counts.get("excluded_generic_noise", 0),
            "excluded_recovery_primary": selection_counts.get("excluded_recovery_primary", 0),
            "selected_root_candidates": selection_counts.get("selected_root_candidates", 0),
            "selected_supporting": selection_counts.get("selected_supporting", 0),
            "selected_symptoms": selection_counts.get("selected_symptoms", 0),
        },
        "context_resolution": {
            "resolved_from_source_rel": resolution_counts.get("resolved_from_source_rel", 0),
            "resolved_from_raw_ref": resolution_counts.get("resolved_from_raw_ref", 0),
            "resolved_by_text_search": resolution_counts.get("resolved_by_text_search", 0),
            "inline_only": resolution_counts.get("inline_only", 0),
            "not_found": resolution_counts.get("not_found", 0),
            "protected_path_skipped": resolution_counts.get("protected_path_skipped", 0),
        },
        "redaction": {
            "enabled": bool(args.redact_sensitive),
            "blocks_with_redactions": redaction["blocks_with_redactions"],
            "lines_redacted": redaction["lines_redacted"],
        },
        "pruning": {
            "cases": pruning_counts.get("cases", 0),
            "blocks_pruned_by_count": pruning_counts.get("blocks_pruned_by_count", 0),
            "blocks_pruned_by_char_budget": pruning_counts.get("blocks_pruned_by_char_budget", 0),
        },
        "distributions": {key: dict(counter.most_common()) for key, counter in distributions.items()},
        "examples": examples,
    }


def percentile(values: Sequence[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 4)
    rank = (len(ordered) - 1) * p
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction, 4)


def print_json_summary(summary: Dict[str, Any]) -> str:
    return json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True)


def markdown_table(counter: Dict[str, int], key_title: str = "value") -> List[str]:
    lines = [f"| {key_title} | count |", "|---|---:|"]
    if not counter:
        lines.append("| none | 0 |")
    else:
        for key, value in counter.items():
            lines.append(f"| `{key}` | {value} |")
    return lines


def warnings_from_summary(summary: Dict[str, Any]) -> List[str]:
    warnings: List[str] = []
    if (
        summary["distributions"]["source"].get("fault_inject")
        or summary["distributions"]["anomaly_type"].get("injector_marker")
        or summary["distributions"]["causal_role"].get("excluded_provenance")
    ):
        warnings.append("BLOCKED_USABLE_INJECTOR_MARKERS")
    if summary["counts"]["output_blocks"] and summary["context_resolution"]["inline_only"] == summary["counts"]["output_blocks"]:
        warnings.append("NO_CONTEXT_SOURCES_FOUND")
    if summary["counts"]["output_blocks"]:
        ratio = summary["context_resolution"]["inline_only"] / max(1, summary["counts"]["output_blocks"])
        if ratio >= 0.75:
            warnings.append("HIGH_INLINE_ONLY_RATIO")
    if summary["redaction"]["lines_redacted"]:
        warnings.append("SENSITIVE_CONTEXT_REDACTED")
    return warnings


def print_markdown_summary(summary: Dict[str, Any]) -> str:
    lines = [
        "# Evidence Context Expansion Preview Report",
        "",
        "This tool is read-only by default and does not modify existing evidence files.",
        "Injector/provenance markers are excluded from diagnostic context by design.",
        "Sensitive GT-like or injector-label context is redacted by default.",
        "",
        "## Counts",
    ]
    for key, value in summary["counts"].items():
        lines.append(f"- `{key}`: {value}")
    lines.extend(["", "## Selection"])
    for key, value in summary["selection"].items():
        lines.append(f"- `{key}`: {value}")
    lines.extend(["", "## Context Resolution"])
    for key, value in summary["context_resolution"].items():
        lines.append(f"- `{key}`: {value}")
    lines.extend(["", "## Redaction"])
    for key, value in summary["redaction"].items():
        lines.append(f"- `{key}`: {value}")
    lines.extend(["", "## Pruning"])
    for key, value in summary["pruning"].items():
        lines.append(f"- `{key}`: {value}")
    lines.extend(["", "## Distributions", "", "### Source"])
    lines.extend(markdown_table(summary["distributions"]["source"], "source"))
    lines.extend(["", "### Anomaly Type"])
    lines.extend(markdown_table(summary["distributions"]["anomaly_type"], "anomaly_type"))
    lines.extend(["", "### Causal Role"])
    lines.extend(markdown_table(summary["distributions"]["causal_role"], "causal_role"))
    lines.extend(["", "## Examples"])
    for title, key in (
        ("Expanded blocks", "expanded_blocks"),
        ("Inline-only blocks", "inline_only"),
        ("Redacted blocks", "redacted_blocks"),
        ("Pruned blocks", "pruned_blocks"),
        ("Excluded blocks (--include-excluded)", "excluded_blocks"),
        ("Parse errors", "parse_errors"),
    ):
        lines.append(f"### {title}")
        examples = summary["examples"].get(key) or []
        if not examples:
            lines.append("- none")
        else:
            for item in examples:
                lines.append(f"- `{item}`")
        lines.append("")
    warnings = warnings_from_summary(summary)
    lines.append("## Warnings")
    if not warnings:
        lines.append("- none")
    else:
        for warning in warnings:
            lines.append(f"- `{warning}`")
    return "\n".join(lines)


def print_jsonl_blocks(blocks: Sequence[Dict[str, Any]], limit: Optional[int]) -> str:
    selected = blocks[:limit] if limit is not None else blocks
    return "\n".join(safe_json_dump(block) for block in selected)


def validate_output_path(output_path: Path, root: Path) -> None:
    resolved = output_path.resolve()
    root_resolved = root.resolve()
    if resolved.exists():
        raise ValueError(f"refusing to overwrite existing output path: {resolved}")
    if not resolved.parent.exists():
        raise ValueError(f"refusing output path with missing parent directory: {resolved}")
    name = resolved.name.lower()
    if name in PROTECTED_OUTPUT_FILENAMES or "ledger" in name:
        raise ValueError(f"refusing protected output filename: {resolved}")
    try:
        rel_parts = [part.lower() for part in resolved.relative_to(root_resolved).parts]
    except ValueError:
        rel_parts = []
    for part in rel_parts[:-1]:
        if part in PROTECTED_OUTPUT_DIRS or "frozen" in part or "accepted" in part or "ledger" in part:
            raise ValueError(f"refusing output under protected repository path: {resolved}")


def render_output(summary: Dict[str, Any], blocks: Sequence[Dict[str, Any]], args: argparse.Namespace) -> str:
    if args.format == "json":
        return print_json_summary(summary)
    if args.format == "markdown":
        return print_markdown_summary(summary)
    if args.format == "jsonl":
        return print_jsonl_blocks(blocks, args.limit)
    return "\n".join(
        [
            "----- JSON -----",
            print_json_summary(summary),
            "",
            "----- MARKDOWN -----",
            print_markdown_summary(summary),
        ]
    )


def run(args: argparse.Namespace) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    root = Path(args.root).resolve()
    if args.blocks_jsonl:
        path = Path(args.blocks_jsonl)
        if not path.is_absolute():
            path = root / path
        blocks, parse_errors = load_normalized_blocks_from_jsonl(path, root, args.max_examples)
        evidence_files: List[Path] = []
        evidence_rows = 0
    else:
        evidence_files, blocks, parse_errors, evidence_rows = adapt_legacy_rows(root, args.input, args.max_examples)

    selected_blocks, selection_counts = select_blocks(blocks, args)
    expanded_blocks, resolution_counts = expand_blocks(selected_blocks, root, args)
    merged_blocks = merge_overlapping_blocks(expanded_blocks)
    scored = sorted(
        merged_blocks,
        key=lambda item: (
            str(item.get("case_id") or ""),
            -float(item.get("block_score") or 0.0),
            -float(item.get("density_score") or 0.0),
        ),
    )
    pruned, pruning_counts, pruned_examples = prune_blocks_by_case(
        scored,
        max(1, args.max_blocks_per_case),
        max(1, args.max_chars_per_case),
    )
    output_blocks = assign_expanded_ids(pruned)
    if args.limit is not None and args.format != "jsonl":
        output_blocks = output_blocks[: args.limit]

    summary = build_summary(
        root,
        args.dry_run,
        evidence_files,
        evidence_rows,
        blocks,
        selected_blocks,
        expanded_blocks,
        merged_blocks,
        output_blocks,
        parse_errors,
        selection_counts,
        resolution_counts,
        pruning_counts,
        pruned_examples,
        args,
    )
    return summary, output_blocks


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    root = Path(args.root).resolve()
    try:
        summary, blocks = run(args)
    except RuntimeError as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 2
    output = render_output(summary, blocks, args)
    if args.output and not args.stdout_only:
        output_path = Path(args.output)
        try:
            validate_output_path(output_path, root)
        except ValueError as exc:
            sys.stderr.write(f"ERROR: {exc}\n")
            return 2
        output_path.write_text(output + ("\n" if output else ""), encoding="utf-8")
    else:
        if output:
            sys.stdout.write(output)
            if not output.endswith("\n"):
                sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
