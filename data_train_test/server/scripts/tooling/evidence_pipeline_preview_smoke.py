#!/usr/bin/env python3
"""Read-only smoke runner for the evidence preview pipeline.

The runner executes the existing preview tools in stdout-only JSON dry-run mode,
parses their summaries, and reports aggregate gates. It does not pass output
paths to child tools and writes nothing unless --output is explicitly provided.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


sys.dont_write_bytecode = True

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
    "accepted",
    "dataset",
    "dataset_batches",
    "datasets",
    "demo_public_dataset",
    "eval",
    "frozen",
    "generated",
    "inbox",
    "inbox_net",
    "ledger",
    "test",
    "train",
    "training_views",
    "val",
}

CHAIN_HARD_SAFETY_KEYS = (
    "injector_in_diagnostic_buckets",
    "gt_like_in_diagnostic_buckets",
    "unredacted_sensitive_context",
    "recovery_primary_as_root_candidate",
    "generic_noise_as_diagnostic",
)

PROMPT_HARD_SAFETY_KEYS = (
    "prompt_hard_violations",
    "unredacted_sensitive_terms",
    "injector_in_diagnostic_prompt",
    "do_not_use_as_support",
    "missing_output_contract",
    "missing_case_ref",
)

VALIDATOR_HARD_SAFETY_KEYS = (
    "sensitive_text_violations",
    "injector_or_label_leakage",
    "do_not_use_semantic_leakage",
    "external_context_claims",
    "coverage_gap_false_root",
)


class ToolSpec:
    def __init__(
        self,
        name: str,
        script: str,
        *,
        accepts_input: bool = True,
        include_no_root: bool = False,
    ) -> None:
        self.name = name
        self.script = script
        self.accepts_input = accepts_input
        self.include_no_root = include_no_root


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a read-only evidence preview pipeline smoke.")
    parser.add_argument("--root", default=".", help="Repository root. Default: current directory.")
    parser.add_argument("--input", default=None, help="Optional evidence_candidates.jsonl file or directory.")
    parser.add_argument("--stdout-only", action="store_true", help="Force stdout output only for this runner.")
    parser.add_argument("--dry-run", action="store_true", help="Explicit safety flag; child tools always run dry.")
    parser.add_argument("--format", choices=("json", "markdown", "both", "jsonl"), default="both")
    parser.add_argument("--max-examples", type=int, default=10)
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    parser.add_argument("--skip-audit", action="store_true")
    parser.add_argument("--skip-context", action="store_true")
    parser.add_argument("--skip-prompt", action="store_true")
    parser.add_argument("--skip-validator", action="store_true")
    parser.add_argument("--include-no-root", action="store_true")
    parser.add_argument("--strict", action="store_true", help="Escalate WARN gates to FAIL.")
    parser.add_argument("--fail-on-warn", action="store_true", help="Exit non-zero when WARN gates are present.")
    parser.add_argument("--output", default=None, help="Optional guarded output path.")
    return parser.parse_args(argv)


def safe_json_dump(value: Any, *, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return safe_json_dump(value)
    except TypeError:
        return str(value)


def to_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def to_float(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return 0.0


def tail_text(value: Optional[str], limit: int = 4000) -> str:
    if not value:
        return ""
    text = value.strip()
    if len(text) <= limit:
        return text
    return text[-limit:]


def validate_output_path(path: Path, root: Path) -> None:
    resolved = path.resolve()
    root_resolved = root.resolve()
    if resolved.exists():
        raise ValueError(f"refusing to overwrite existing output path: {resolved}")
    if resolved.name.lower() in PROTECTED_OUTPUT_FILENAMES or "ledger" in resolved.name.lower():
        raise ValueError(f"refusing protected output filename: {resolved}")
    if not resolved.parent.exists():
        raise ValueError(f"output parent does not exist: {resolved.parent}")
    try:
        rel_parts = [part.lower() for part in resolved.relative_to(root_resolved).parts]
    except ValueError as exc:
        raise ValueError(f"refusing output path outside repository root: {resolved}") from exc
    for part in rel_parts[:-1]:
        if part in PROTECTED_OUTPUT_DIRS or "frozen" in part or "accepted" in part or "ledger" in part:
            raise ValueError(f"refusing output under protected repository path: {resolved}")


def selected_tools(args: argparse.Namespace) -> List[ToolSpec]:
    tools: List[ToolSpec] = []
    if not args.skip_audit:
        tools.append(ToolSpec("schema_audit", "tools/audit_evidence_schema_stats.py", accepts_input=False))
    tools.append(ToolSpec("block_adapter", "tools/evidence_block_schema_adapter.py"))
    if not args.skip_context:
        tools.append(ToolSpec("context_expander", "tools/evidence_context_expander.py"))
    tools.append(ToolSpec("chain_builder", "tools/evidence_chain_builder.py"))
    tools.append(ToolSpec("regression_check", "tools/evidence_chain_regression_check.py"))
    if not args.skip_prompt:
        tools.append(ToolSpec("prompt_preview", "tools/evidence_prompt_integration_preview.py", include_no_root=True))
    if not args.skip_validator:
        tools.append(ToolSpec("response_validator", "tools/evidence_response_validator.py", include_no_root=True))
    return tools


def build_command(spec: ToolSpec, args: argparse.Namespace) -> List[str]:
    command = [
        sys.executable,
        "-B",
        spec.script,
        "--root",
        ".",
        "--stdout-only",
        "--dry-run",
        "--format",
        "json",
        "--max-examples",
        str(args.max_examples),
    ]
    if args.input and spec.accepts_input:
        command.extend(["--input", args.input])
    if args.include_no_root and spec.include_no_root:
        command.append("--include-no-root")
    return command


def parse_json_stdout(stdout: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    text = stdout.strip()
    if not text:
        return None, "empty stdout"
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        first = text.find("{")
        last = text.rfind("}")
        if first >= 0 and last > first:
            try:
                value = json.loads(text[first : last + 1])
            except json.JSONDecodeError:
                return None, str(exc)
        else:
            return None, str(exc)
    if not isinstance(value, dict):
        return None, "JSON output is not an object"
    return value, None


def run_tool(spec: ToolSpec, root: Path, args: argparse.Namespace) -> Dict[str, Any]:
    command = build_command(spec, args)
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    started = time.monotonic()
    stdout = ""
    stderr = ""
    returncode: Optional[int] = None
    timed_out = False
    parse_error: Optional[str] = None
    summary: Optional[Dict[str, Any]] = None

    try:
        completed = subprocess.run(
            command,
            cwd=str(root),
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=args.timeout_seconds,
            env=env,
        )
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        returncode = completed.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        returncode = None
        stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
        stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", "replace")

    elapsed = round(time.monotonic() - started, 4)
    if not timed_out and returncode == 0:
        summary, parse_error = parse_json_stdout(stdout)
    elif stdout.strip():
        summary, parse_error = parse_json_stdout(stdout)

    status = "PASS"
    if timed_out or returncode not in (0, None):
        status = "FAIL"
    if parse_error:
        status = "FAIL"

    result: Dict[str, Any] = {
        "tool": spec.name,
        "script": spec.script,
        "command": command,
        "returncode": returncode,
        "timed_out": timed_out,
        "elapsed_seconds": elapsed,
        "status": status,
        "summary": summary,
    }
    if parse_error:
        result["parse_error"] = parse_error
        result["stdout_tail"] = tail_text(stdout)
    if stderr.strip():
        result["stderr_tail"] = tail_text(stderr)
    return result


def child_status(summary: Optional[Dict[str, Any]]) -> str:
    if not isinstance(summary, dict):
        return "UNKNOWN"
    status = str(summary.get("status") or "").upper()
    if status in {"PASS", "WARN", "FAIL"}:
        return status
    gates = summary.get("gates")
    if isinstance(gates, dict):
        values = {str(value).upper() for value in gates.values()}
        if "FAIL" in values:
            return "FAIL"
        if "WARN" in values:
            return "WARN"
        if values:
            return "PASS"
    return "UNKNOWN"


def prefixed_counts(prefix: str, mapping: Any, keys: Sequence[str]) -> Dict[str, int]:
    if not isinstance(mapping, dict):
        return {f"{prefix}.{key}": 0 for key in keys}
    return {f"{prefix}.{key}": to_int(mapping.get(key, 0)) for key in keys}


def aggregate_hard_safety(results: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    metrics: Dict[str, int] = {}
    for result in results:
        tool = str(result.get("tool") or "")
        summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
        if tool == "block_adapter":
            risk = summary.get("risk_summary") if isinstance(summary, dict) else {}
            metrics["block_adapter.usable_injector_markers"] = to_int((risk or {}).get("usable_injector_markers", 0))
        elif tool in {"chain_builder", "regression_check"}:
            metrics.update(prefixed_counts(tool, (summary or {}).get("safety"), CHAIN_HARD_SAFETY_KEYS))
        elif tool == "prompt_preview":
            metrics.update(prefixed_counts(tool, (summary or {}).get("safety"), PROMPT_HARD_SAFETY_KEYS))
        elif tool == "response_validator":
            metrics.update(prefixed_counts(tool, (summary or {}).get("safety"), VALIDATOR_HARD_SAFETY_KEYS))
            evidence_reference = (summary or {}).get("evidence_reference")
            if isinstance(evidence_reference, dict):
                metrics["response_validator.unknown_evidence_id"] = to_int(evidence_reference.get("unknown_evidence_id", 0))
                metrics["response_validator.do_not_use_evidence_cited"] = to_int(
                    evidence_reference.get("do_not_use_evidence_cited", 0)
                )
                metrics["response_validator.forbidden_role_used"] = to_int(evidence_reference.get("forbidden_role_used", 0))
    return {
        "total": sum(metrics.values()),
        "metrics": dict(sorted(metrics.items())),
    }


def public_hard_safety(hard_safety: Dict[str, Any]) -> Dict[str, int]:
    metrics = hard_safety.get("metrics") if isinstance(hard_safety.get("metrics"), dict) else {}

    def metric(name: str) -> int:
        return to_int(metrics.get(name, 0))

    return {
        "injector_usable": metric("block_adapter.usable_injector_markers"),
        "injector_in_diagnostic_buckets": max(
            metric("chain_builder.injector_in_diagnostic_buckets"),
            metric("regression_check.injector_in_diagnostic_buckets"),
        ),
        "gt_like_in_diagnostic_buckets": max(
            metric("chain_builder.gt_like_in_diagnostic_buckets"),
            metric("regression_check.gt_like_in_diagnostic_buckets"),
        ),
        "unredacted_sensitive_context": max(
            metric("chain_builder.unredacted_sensitive_context"),
            metric("regression_check.unredacted_sensitive_context"),
        ),
        "recovery_primary_as_root_candidate": max(
            metric("chain_builder.recovery_primary_as_root_candidate"),
            metric("regression_check.recovery_primary_as_root_candidate"),
        ),
        "generic_noise_as_diagnostic": max(
            metric("chain_builder.generic_noise_as_diagnostic"),
            metric("regression_check.generic_noise_as_diagnostic"),
        ),
        "prompt_hard_violations": (
            metric("prompt_preview.prompt_hard_violations")
            + metric("prompt_preview.unredacted_sensitive_terms")
            + metric("prompt_preview.injector_in_diagnostic_prompt")
            + metric("prompt_preview.do_not_use_as_support")
            + metric("prompt_preview.missing_output_contract")
            + metric("prompt_preview.missing_case_ref")
        ),
        "validator_sensitive_text_violations": (
            metric("response_validator.sensitive_text_violations")
            + metric("response_validator.injector_or_label_leakage")
        ),
        "validator_do_not_use_evidence_cited": (
            metric("response_validator.do_not_use_evidence_cited")
            + metric("response_validator.do_not_use_semantic_leakage")
        ),
        "validator_unknown_evidence_id": metric("response_validator.unknown_evidence_id"),
        "validator_coverage_gap_false_root": metric("response_validator.coverage_gap_false_root"),
        "validator_forbidden_role_used": metric("response_validator.forbidden_role_used"),
        "validator_external_context_claims": metric("response_validator.external_context_claims"),
    }


def warning_counts_from_summary(tool: str, summary: Dict[str, Any]) -> Counter:
    warnings: Counter = Counter()
    status = child_status(summary)
    if status == "WARN":
        warnings[f"{tool}:status_warn"] += 1

    raw_warnings = summary.get("warnings")
    if isinstance(raw_warnings, dict):
        for key, value in raw_warnings.items():
            warnings[f"{tool}:{key}"] += to_int(value) or 1
    elif isinstance(raw_warnings, list):
        for item in raw_warnings:
            warnings[f"{tool}:{item}"] += 1

    if tool == "schema_audit":
        counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
        parse_errors = to_int(counts.get("parse_errors", 0))
        if parse_errors:
            warnings["schema_audit:parse_errors"] += parse_errors
        risks = summary.get("risks") if isinstance(summary.get("risks"), dict) else {}
        injector = risks.get("injector_marker") if isinstance(risks.get("injector_marker"), dict) else {}
        for key in ("primary_support_role_rows", "primary_target_rows"):
            value = to_int(injector.get(key, 0))
            if value:
                warnings[f"schema_audit:injector_marker.{key}"] += value
        l2 = summary.get("l2") if isinstance(summary.get("l2"), dict) else {}
        empty_targets = l2.get("empty_evidence_targets") if isinstance(l2.get("empty_evidence_targets"), dict) else {}
        empty_count = to_int(empty_targets.get("samples_with_all_empty_evidence_arrays", 0))
        if empty_count:
            warnings["schema_audit:all_empty_evidence_targets"] += empty_count

    if tool == "context_expander":
        counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
        context = summary.get("context_resolution") if isinstance(summary.get("context_resolution"), dict) else {}
        output_blocks = to_int(counts.get("output_blocks", 0))
        inline_only = to_int(context.get("inline_only", 0))
        if output_blocks and inline_only == output_blocks:
            warnings["context_expander:no_context_sources_found"] += 1
        if output_blocks and inline_only / max(1, output_blocks) >= 0.75:
            warnings["context_expander:high_inline_only_ratio"] += 1
        redaction = summary.get("redaction") if isinstance(summary.get("redaction"), dict) else {}
        redacted = to_int(redaction.get("lines_redacted", 0))
        if redacted:
            warnings["context_expander:sensitive_context_redacted"] += redacted

    gates = summary.get("gates")
    if isinstance(gates, dict):
        for key, value in gates.items():
            gate_value = str(value).upper()
            if gate_value == "WARN":
                warnings[f"{tool}:gate.{key}"] += 1
    return warnings


def aggregate_warnings(results: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    warnings: Counter = Counter()
    for result in results:
        tool = str(result.get("tool") or "")
        summary = result.get("summary")
        if isinstance(summary, dict):
            warnings.update(warning_counts_from_summary(tool, summary))
    return dict(warnings.most_common())


def collect_coverage(results_by_tool: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    coverage: Dict[str, Any] = {}

    audit = results_by_tool.get("schema_audit", {})
    audit_summary = audit.get("summary") if isinstance(audit.get("summary"), dict) else {}
    audit_counts = audit_summary.get("counts") if isinstance(audit_summary.get("counts"), dict) else {}
    for key in ("l1_evidence_files", "l1_evidence_rows", "l2_jsonl_files", "l2_samples", "parse_errors"):
        coverage[f"schema_audit_{key}"] = to_int(audit_counts.get(key, 0))

    adapter = results_by_tool.get("block_adapter", {})
    adapter_summary = adapter.get("summary") if isinstance(adapter.get("summary"), dict) else {}
    adapter_counts = adapter_summary.get("counts") if isinstance(adapter_summary.get("counts"), dict) else {}
    for key in ("files", "rows", "blocks", "usable_blocks", "excluded_blocks", "parse_errors"):
        coverage[f"block_adapter_{key}"] = to_int(adapter_counts.get(key, 0))
    adapter_context = adapter_summary.get("context_coverage") if isinstance(adapter_summary.get("context_coverage"), dict) else {}
    for key in ("with_context_before", "with_context_after", "without_context"):
        coverage[f"block_adapter_{key}"] = to_int(adapter_context.get(key, 0))

    context = results_by_tool.get("context_expander", {})
    context_summary = context.get("summary") if isinstance(context.get("summary"), dict) else {}
    context_counts = context_summary.get("counts") if isinstance(context_summary.get("counts"), dict) else {}
    for key in ("input_blocks", "normalized_blocks", "selected_blocks", "expanded_blocks", "output_blocks", "parse_errors"):
        coverage[f"context_expander_{key}"] = to_int(context_counts.get(key, 0))
    context_resolution = (
        context_summary.get("context_resolution") if isinstance(context_summary.get("context_resolution"), dict) else {}
    )
    for key in ("inline_only", "not_found", "protected_path_skipped"):
        coverage[f"context_expander_{key}"] = to_int(context_resolution.get(key, 0))

    chain = results_by_tool.get("chain_builder", {})
    chain_summary = chain.get("summary") if isinstance(chain.get("summary"), dict) else {}
    chain_counts = chain_summary.get("counts") if isinstance(chain_summary.get("counts"), dict) else {}
    for key in (
        "evidence_files",
        "input_rows",
        "expanded_blocks",
        "chains",
        "chains_with_root_candidates",
        "chains_without_root_candidates",
        "parse_errors",
    ):
        coverage[f"chain_builder_{key}"] = to_int(chain_counts.get(key, 0))

    regression = results_by_tool.get("regression_check", {})
    regression_summary = regression.get("summary") if isinstance(regression.get("summary"), dict) else {}
    regression_coverage = (
        regression_summary.get("coverage") if isinstance(regression_summary.get("coverage"), dict) else {}
    )
    for key in (
        "no_root_ratio",
        "do_not_use_ratio",
        "chains_with_context",
        "chains_without_context",
        "chains_with_supporting_evidence",
        "chains_with_symptoms",
        "chains_with_root_candidate_but_no_supporting_evidence",
    ):
        value = regression_coverage.get(key, 0)
        coverage[f"regression_check_{key}"] = to_float(value) if "ratio" in key else to_int(value)

    prompt = results_by_tool.get("prompt_preview", {})
    prompt_summary = prompt.get("summary") if isinstance(prompt.get("summary"), dict) else {}
    prompt_counts = prompt_summary.get("counts") if isinstance(prompt_summary.get("counts"), dict) else {}
    for key in (
        "chains",
        "selected_chains",
        "prompt_records",
        "diagnosis_ready_prompts",
        "review_ready_prompts",
        "coverage_gap_prompts",
        "not_ready_prompts",
    ):
        coverage[f"prompt_preview_{key}"] = to_int(prompt_counts.get(key, 0))

    validator = results_by_tool.get("response_validator", {})
    validator_summary = validator.get("summary") if isinstance(validator.get("summary"), dict) else {}
    validator_counts = validator_summary.get("counts") if isinstance(validator_summary.get("counts"), dict) else {}
    for key in ("prompt_records", "response_records", "validation_results", "pass", "warn", "fail"):
        coverage[f"response_validator_{key}"] = to_int(validator_counts.get(key, 0))

    return coverage


def collect_key_counts(coverage: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "l1_evidence_files": to_int(coverage.get("schema_audit_l1_evidence_files") or coverage.get("block_adapter_files")),
        "l1_evidence_rows": to_int(coverage.get("schema_audit_l1_evidence_rows") or coverage.get("block_adapter_rows")),
        "normalized_blocks": to_int(coverage.get("context_expander_normalized_blocks") or coverage.get("block_adapter_blocks")),
        "expanded_blocks": to_int(coverage.get("chain_builder_expanded_blocks") or coverage.get("context_expander_output_blocks")),
        "chains": to_int(coverage.get("chain_builder_chains")),
        "chains_with_root_candidates": to_int(coverage.get("chain_builder_chains_with_root_candidates")),
        "chains_without_root_candidates": to_int(coverage.get("chain_builder_chains_without_root_candidates")),
        "prompt_records": to_int(coverage.get("prompt_preview_prompt_records")),
        "validator_prompt_records": to_int(coverage.get("response_validator_prompt_records")),
    }


def collect_public_coverage(coverage: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "no_root_ratio": to_float(coverage.get("regression_check_no_root_ratio")),
        "do_not_use_ratio": to_float(coverage.get("regression_check_do_not_use_ratio")),
        "prompt_review_ready": to_int(coverage.get("prompt_preview_review_ready_prompts")),
        "prompt_not_ready": to_int(coverage.get("prompt_preview_not_ready_prompts")),
        "validator_warn": to_int(coverage.get("response_validator_warn")),
        "validator_fail": to_int(coverage.get("response_validator_fail")),
    }


def collect_readiness(results_by_tool: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    readiness: Dict[str, Any] = {}
    regression = results_by_tool.get("regression_check", {})
    regression_summary = regression.get("summary") if isinstance(regression.get("summary"), dict) else {}
    prompt_readiness = regression_summary.get("prompt_payload_readiness")
    if isinstance(prompt_readiness, dict):
        readiness["regression_prompt_payload"] = prompt_readiness

    prompt = results_by_tool.get("prompt_preview", {})
    prompt_summary = prompt.get("summary") if isinstance(prompt.get("summary"), dict) else {}
    prompt_counts = prompt_summary.get("readiness")
    if isinstance(prompt_counts, dict):
        readiness["prompt_preview"] = prompt_counts

    validator = results_by_tool.get("response_validator", {})
    validator_summary = validator.get("summary") if isinstance(validator.get("summary"), dict) else {}
    validator_counts = validator_summary.get("counts")
    if isinstance(validator_counts, dict):
        readiness["response_validator"] = validator_counts
    return readiness


def compact_tool_summary(tool: str, summary: Dict[str, Any]) -> Dict[str, Any]:
    counts = summary.get("counts") if isinstance(summary.get("counts"), dict) else {}
    if tool == "schema_audit":
        return {
            "l1_evidence_files": to_int(counts.get("l1_evidence_files")),
            "l1_evidence_rows": to_int(counts.get("l1_evidence_rows")),
            "l2_samples": to_int(counts.get("l2_samples")),
            "parse_errors": to_int(counts.get("parse_errors")),
        }
    if tool == "block_adapter":
        risk = summary.get("risk_summary") if isinstance(summary.get("risk_summary"), dict) else {}
        return {
            "files": to_int(counts.get("files")),
            "rows": to_int(counts.get("rows")),
            "usable_blocks": to_int(counts.get("usable_blocks")),
            "excluded_blocks": to_int(counts.get("excluded_blocks")),
            "usable_injector_markers": to_int(risk.get("usable_injector_markers")),
        }
    if tool == "context_expander":
        redaction = summary.get("redaction") if isinstance(summary.get("redaction"), dict) else {}
        return {
            "evidence_files": to_int(counts.get("evidence_files")),
            "evidence_rows": to_int(counts.get("evidence_rows")),
            "selected_blocks": to_int(counts.get("selected_blocks")),
            "expanded_blocks": to_int(counts.get("expanded_blocks")),
            "output_blocks": to_int(counts.get("output_blocks")),
            "redacted_lines": to_int(redaction.get("lines_redacted")),
        }
    if tool == "chain_builder":
        return {
            "chains": to_int(counts.get("chains")),
            "chains_with_root_candidates": to_int(counts.get("chains_with_root_candidates")),
            "chains_without_root_candidates": to_int(counts.get("chains_without_root_candidates")),
            "expanded_blocks": to_int(counts.get("expanded_blocks")),
        }
    if tool == "regression_check":
        readiness = summary.get("prompt_payload_readiness") if isinstance(summary.get("prompt_payload_readiness"), dict) else {}
        return {
            "status": summary.get("status"),
            "chains": to_int(counts.get("chains")),
            "chains_with_root_candidates": to_int(counts.get("chains_with_root_candidates")),
            "chains_without_root_candidates": to_int(counts.get("chains_without_root_candidates")),
            "no_root_ratio": to_float((summary.get("coverage") or {}).get("no_root_ratio") if isinstance(summary.get("coverage"), dict) else 0),
            "prompt_payload_readiness": {
                "ready": to_int(readiness.get("ready")),
                "review_ready": to_int(readiness.get("review_ready")),
                "not_ready": to_int(readiness.get("not_ready")),
                "root_candidate_chains": to_int(readiness.get("root_candidate_chains")),
                "no_root_chains": to_int(readiness.get("no_root_chains")),
                "blockers": readiness.get("blockers") or {},
                "review_warnings": readiness.get("review_warnings") or {},
            },
        }
    if tool == "prompt_preview":
        return {
            "status": summary.get("status"),
            "prompt_records": to_int(counts.get("prompt_records")),
            "diagnosis_ready_prompts": to_int(counts.get("diagnosis_ready_prompts")),
            "review_ready_prompts": to_int(counts.get("review_ready_prompts")),
            "coverage_gap_prompts": to_int(counts.get("coverage_gap_prompts")),
            "not_ready_prompts": to_int(counts.get("not_ready_prompts")),
        }
    if tool == "response_validator":
        return {
            "status": summary.get("status"),
            "mode": summary.get("mode"),
            "prompt_records": to_int(counts.get("prompt_records")),
            "pass": to_int(counts.get("pass")),
            "warn": to_int(counts.get("warn")),
            "fail": to_int(counts.get("fail")),
        }
    return {}


def compact_tool_hard_safety(tool: str, summary: Dict[str, Any]) -> Dict[str, int]:
    if tool == "block_adapter":
        risk = summary.get("risk_summary") if isinstance(summary.get("risk_summary"), dict) else {}
        return {"injector_usable": to_int(risk.get("usable_injector_markers"))}
    if tool in {"chain_builder", "regression_check"}:
        safety = summary.get("safety") if isinstance(summary.get("safety"), dict) else {}
        return {key: to_int(safety.get(key)) for key in CHAIN_HARD_SAFETY_KEYS}
    if tool == "prompt_preview":
        safety = summary.get("safety") if isinstance(summary.get("safety"), dict) else {}
        return {key: to_int(safety.get(key)) for key in PROMPT_HARD_SAFETY_KEYS}
    if tool == "response_validator":
        safety = summary.get("safety") if isinstance(summary.get("safety"), dict) else {}
        evidence = summary.get("evidence_reference") if isinstance(summary.get("evidence_reference"), dict) else {}
        result = {key: to_int(safety.get(key)) for key in VALIDATOR_HARD_SAFETY_KEYS}
        result["do_not_use_evidence_cited"] = to_int(evidence.get("do_not_use_evidence_cited"))
        result["unknown_evidence_id"] = to_int(evidence.get("unknown_evidence_id"))
        result["forbidden_role_used"] = to_int(evidence.get("forbidden_role_used"))
        return result
    return {}


def compute_gates(
    results: Sequence[Dict[str, Any]],
    hard_safety: Dict[str, int],
    hard_safety_detail: Dict[str, Any],
    coverage: Dict[str, Any],
    warnings: Dict[str, int],
    args: argparse.Namespace,
) -> Dict[str, str]:
    by_tool = {str(result.get("tool")): result for result in results}
    gates: Dict[str, str] = {}
    if any(result.get("timed_out") or result.get("returncode") != 0 for result in results):
        gates["tool_execution_gate"] = "FAIL"
    elif any(result.get("parse_error") for result in results):
        gates["tool_execution_gate"] = "FAIL"
    else:
        gates["tool_execution_gate"] = "PASS"

    tool_gate_names = (
        ("schema_audit", "audit_gate"),
        ("block_adapter", "adapter_gate"),
        ("context_expander", "context_gate"),
        ("chain_builder", "chain_gate"),
        ("regression_check", "regression_gate"),
        ("prompt_preview", "prompt_gate"),
        ("response_validator", "validator_gate"),
    )
    for tool_name, gate_name in tool_gate_names:
        result = by_tool.get(tool_name)
        if result is None:
            gates[gate_name] = "SKIPPED" if gate_name in {"audit_gate", "context_gate", "prompt_gate", "validator_gate"} else "FAIL"
            continue
        if result.get("status") == "FAIL":
            gates[gate_name] = "FAIL"
            continue
        status = child_status(result.get("summary"))
        if status == "FAIL":
            gates[gate_name] = "FAIL"
        elif status == "WARN" or warning_counts_from_summary(tool_name, result.get("summary") or {}):
            gates[gate_name] = "WARN"
        else:
            gates[gate_name] = "PASS"

    if to_int(hard_safety_detail.get("total", 0)) != 0 or any(to_int(value) != 0 for value in hard_safety.values()):
        gates["hard_safety_gate"] = "FAIL"
    else:
        gates["hard_safety_gate"] = "PASS"

    no_root_count = (
        to_int(coverage.get("chain_builder_chains_without_root_candidates", 0))
        + to_int(coverage.get("prompt_preview_coverage_gap_prompts", 0))
    )
    no_root_ratio = to_float(coverage.get("regression_check_no_root_ratio", 0.0))
    if no_root_count or no_root_ratio:
        gates["coverage_gate"] = "WARN"
    else:
        gates["coverage_gate"] = "PASS"

    prompt_not_ready = to_int(coverage.get("prompt_preview_not_ready_prompts", 0))
    validator_warn = to_int(coverage.get("response_validator_warn", 0))
    if prompt_not_ready or validator_warn:
        if gates.get("prompt_gate") == "PASS":
            gates["prompt_gate"] = "WARN"
        if gates.get("validator_gate") == "PASS":
            gates["validator_gate"] = "WARN"

    return gates


def final_status(gates: Dict[str, str]) -> str:
    if any(value == "FAIL" for value in gates.values()):
        return "FAIL"
    if any(value == "WARN" for value in gates.values()):
        return "WARN"
    return "PASS"


def build_summary(results: Sequence[Dict[str, Any]], root: Path, args: argparse.Namespace) -> Dict[str, Any]:
    results_by_tool = {str(result.get("tool")): result for result in results}
    hard_safety_detail = aggregate_hard_safety(results)
    hard_safety = public_hard_safety(hard_safety_detail)
    coverage = collect_coverage(results_by_tool)
    key_counts = collect_key_counts(coverage)
    public_coverage = collect_public_coverage(coverage)
    readiness = collect_readiness(results_by_tool)
    warnings = aggregate_warnings(results)
    gates = compute_gates(results, hard_safety, hard_safety_detail, coverage, warnings, args)
    status = final_status(gates)
    if status == "WARN" and (args.strict or args.fail_on_warn):
        status = "FAIL"
    failures = []
    for result in results:
        if result.get("status") == "FAIL":
            failures.append(
                {
                    "tool": result.get("tool"),
                    "returncode": result.get("returncode"),
                    "timed_out": result.get("timed_out"),
                    "parse_error": result.get("parse_error"),
                    "stderr_tail": result.get("stderr_tail"),
                }
            )
    public_results: List[Dict[str, Any]] = []
    for result in results:
        public_results.append(
            {
                "tool": result.get("tool"),
                "command": [Path(str(result.get("command", ["python"])[0])).name, *[str(part) for part in (result.get("command") or [])[1:]]]
                if result.get("command")
                else [],
                "status": result.get("status"),
                "returncode": result.get("returncode"),
                "duration_seconds": result.get("elapsed_seconds"),
                "json_parse_ok": not bool(result.get("parse_error")) and isinstance(result.get("summary"), dict),
                "summary": compact_tool_summary(str(result.get("tool") or ""), result.get("summary") if isinstance(result.get("summary"), dict) else {}),
                "hard_safety": compact_tool_hard_safety(str(result.get("tool") or ""), result.get("summary") if isinstance(result.get("summary"), dict) else {}),
                "warnings": dict(warning_counts_from_summary(str(result.get("tool") or ""), result.get("summary") if isinstance(result.get("summary"), dict) else {}).most_common()),
                "errors": [item for item in (result.get("parse_error"), result.get("stderr_tail")) if item],
                "stdout_snippet": result.get("stdout_tail", ""),
                "stderr_snippet": result.get("stderr_tail", ""),
            }
        )
    examples = {
        "failed_tools": failures[: args.max_examples],
        "warning_tools": [
            {
                "tool": result.get("tool"),
                "warnings": dict(warning_counts_from_summary(str(result.get("tool") or ""), result.get("summary") if isinstance(result.get("summary"), dict) else {}).most_common()),
                "summary": compact_tool_summary(str(result.get("tool") or ""), result.get("summary") if isinstance(result.get("summary"), dict) else {}),
            }
            for result in results
            if warning_counts_from_summary(str(result.get("tool") or ""), result.get("summary") if isinstance(result.get("summary"), dict) else {})
            or child_status(result.get("summary")) == "WARN"
        ][: args.max_examples],
        "safety_warnings": [
            {"metric": key, "value": value}
            for key, value in hard_safety.items()
            if to_int(value) != 0
        ][: args.max_examples],
    }
    return {
        "root": str(root.resolve()),
        "dry_run": True,
        "status": status,
        "strict": bool(args.strict or args.fail_on_warn),
        "tool_results": public_results,
        "gates": gates,
        "key_counts": key_counts,
        "hard_safety": hard_safety,
        "coverage": public_coverage,
        "warnings": warnings,
        "examples": examples,
        "details": {
            "input": args.input,
            "requested_dry_run": bool(args.dry_run),
            "stdout_only": bool(args.stdout_only),
            "format": args.format,
            "max_examples": args.max_examples,
            "timeout_seconds": args.timeout_seconds,
            "hard_safety_detail": hard_safety_detail,
            "coverage_detail": coverage,
            "readiness": readiness,
        },
    }


def markdown_table(title: str, values: Dict[str, Any]) -> List[str]:
    lines = [f"## {title}", "| metric | value |", "|---|---:|"]
    if not values:
        lines.append("| none | 0 |")
        return lines
    for key, value in values.items():
        rendered = safe_json_dump(value) if isinstance(value, (dict, list)) else safe_text(value)
        lines.append(f"| `{key}` | {rendered} |")
    return lines


def print_markdown_summary(summary: Dict[str, Any]) -> str:
    details = summary.get("details") if isinstance(summary.get("details"), dict) else {}
    lines = [
        "# Evidence Pipeline Preview Smoke Report",
        "",
        "This smoke runner is read-only by default and does not modify existing evidence files.",
        "This smoke runner does not call any LLM and does not generate diagnosis answers.",
        "WARN can be acceptable when only review warnings or no-root coverage gaps remain.",
        "Hard safety metrics must remain zero before any live LLM experiment.",
        "",
        "## Status",
        f"- `status`: {summary.get('status')}",
        f"- `strict`: {summary.get('strict')}",
        f"- `root`: {summary.get('root')}",
        f"- `input`: {details.get('input') or '<none>'}",
        "",
    ]
    lines.append("## Tool Execution Summary")
    lines.append("| tool | status | returncode | seconds | json_parse_ok |")
    lines.append("|---|---|---:|---:|---|")
    for result in summary.get("tool_results") or []:
        lines.append(
            "| `{tool}` | `{status}` | {returncode} | {seconds} | {json_parse_ok} |".format(
                tool=result.get("tool"),
                status=result.get("status"),
                returncode=safe_text(result.get("returncode")),
                seconds=safe_text(result.get("duration_seconds")),
                json_parse_ok=safe_text(result.get("json_parse_ok")),
            )
        )
    lines.append("")
    lines.extend(markdown_table("Gate Summary", summary.get("gates") or {}))
    lines.append("")
    lines.extend(markdown_table("Key Counts", summary.get("key_counts") or {}))
    lines.append("")
    lines.extend(markdown_table("Hard Safety Metrics", summary.get("hard_safety") or {}))
    lines.append("")
    lines.extend(markdown_table("Coverage and Readiness", summary.get("coverage") or {}))
    lines.append("")
    if summary.get("warnings"):
        lines.extend(markdown_table("Warnings", summary.get("warnings") or {}))
        lines.append("")
    examples = summary.get("examples") if isinstance(summary.get("examples"), dict) else {}
    lines.append("## Failed / Warning Tool Examples")
    for label in ("failed_tools", "warning_tools", "safety_warnings"):
        items = examples.get(label) or []
        lines.append(f"### {label}")
        if not items:
            lines.append("- none")
        else:
            for item in items:
                lines.append(f"- `{safe_json_dump(item)}`")
    lines.extend(
        [
            "",
            "## Notes",
            "- Commands are limited to known preview tools with `--dry-run`, `--stdout-only`, and `--format json`.",
            "- No LLM/API/external service is called.",
            "- JSON summaries from the existing tools are treated as the source of truth for aggregation.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def render_jsonl(summary: Dict[str, Any]) -> str:
    lines: List[str] = []
    for result in summary.get("tool_results") or []:
        lines.append(
            safe_json_dump(
                {
                    "type": "tool_result",
                    "tool": result.get("tool"),
                    "status": result.get("status"),
                    "returncode": result.get("returncode"),
                    "duration_seconds": result.get("duration_seconds"),
                    "json_parse_ok": result.get("json_parse_ok"),
                    "summary": result.get("summary") or {},
                }
            )
        )
    for gate, status in (summary.get("gates") or {}).items():
        lines.append(safe_json_dump({"type": "gate_result", "gate": gate, "status": status}))
    for metric, value in (summary.get("hard_safety") or {}).items():
        lines.append(safe_json_dump({"type": "hard_safety_metric", "metric": metric, "value": value}))
    for metric, value in (summary.get("coverage") or {}).items():
        lines.append(safe_json_dump({"type": "coverage_metric", "metric": metric, "value": value}))
    return "\n".join(lines) + "\n"


def render_output(summary: Dict[str, Any], output_format: str) -> str:
    if output_format == "json":
        return safe_json_dump(summary, pretty=True) + "\n"
    if output_format == "markdown":
        return print_markdown_summary(summary)
    if output_format == "jsonl":
        return render_jsonl(summary)
    return "----- JSON -----\n" + safe_json_dump(summary, pretty=True) + "\n\n----- MARKDOWN -----\n" + print_markdown_summary(summary)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.max_examples < 0:
        sys.stderr.write("ERROR: --max-examples must be >= 0\n")
        return 2
    if args.timeout_seconds <= 0:
        sys.stderr.write("ERROR: --timeout-seconds must be > 0\n")
        return 2
    root = Path(args.root).resolve()
    if not root.is_dir():
        sys.stderr.write(f"ERROR: root is not a directory: {root}\n")
        return 2
    output_path: Optional[Path] = None
    if args.output and not args.stdout_only:
        output_path = Path(args.output)
        if not output_path.is_absolute():
            output_path = root / output_path
        try:
            validate_output_path(output_path, root)
        except ValueError as exc:
            sys.stderr.write(f"ERROR: {exc}\n")
            return 2

    results = [run_tool(spec, root, args) for spec in selected_tools(args)]
    summary = build_summary(results, root, args)
    rendered = render_output(summary, args.format)
    if output_path is not None:
        output_path.write_text(rendered, encoding="utf-8", newline="\n")
    else:
        sys.stdout.write(rendered)
    return 1 if summary.get("status") == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
