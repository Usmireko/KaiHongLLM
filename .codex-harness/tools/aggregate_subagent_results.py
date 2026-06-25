#!/usr/bin/env python3
"""Dry-run aggregator for manually saved Codex subagent result bundles.

The aggregator reads only a small, fixed set of Markdown files from a run
directory, checks for expected AgentResultBundle sections, and prints a workflow
summary. It does not execute agents, run validation commands, or mutate source
result files.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]

ALLOWED_RESULT_FILES = [
    "01_project_explorer_result.md",
    "02_project_implementer_result.md",
    "03_project_reviewer_result.md",
    "04_project_repairer_result.md",
    "agent_result_bundle.md",
    "prompt_bundle.md",
    "expected_workflow.json",
    "spawn_capture_trace.json",
    "spawn_capture_trace.md",
    "aggregation_summary.md",
    "aggregation_trace.json",
    "v1_acceptance_report.md",
    "v2_acceptance_report.md",
]

NATIVE_EXPECTED_WORKFLOW = "expected_workflow.json"
NATIVE_CAPTURE_TRACE = "spawn_capture_trace.json"
STRICT_DISALLOWED_RESULT_MARKERS = (
    "RESULT: WARN",
    "RESULT: NEEDS_FIX",
    "RESULT: CAPTURE_FAILED",
    "RESULT: FAIL",
)
AGENT_FILES = {
    "project-explorer": "01_project_explorer_result.md",
    "project-implementer": "02_project_implementer_result.md",
    "project-reviewer": "03_project_reviewer_result.md",
    "project-repairer": "04_project_repairer_result.md",
}

FILE_AGENTS = {filename: agent for agent, filename in AGENT_FILES.items()}

KNOWN_BACKEND_AGENTS = set(AGENT_FILES)

DEFAULT_FORBIDDEN_PREFIXES = (
    ".git/",
    "storage/runs/",
    "dataset/raw/",
    "dataset/l0/",
    "dataset/l1/",
    "dataset/l2/",
    "models/",
    "checkpoints/",
    "inbox/runs/",
    "_tmp/",
    "_bundles/",
)

BASIC_SECTION_GROUPS = [
    ("Summary", [r"\bSummary\b"]),
    ("Files Changed", [r"\bFiles Changed\b", r"\bFiles changed\b"]),
    ("Validation Commands", [r"\bValidation Commands\b", r"\bCommands Run\b"]),
    ("Validation Result", [r"\bValidation Result\b", r"\bRESULT\b"]),
    ("Remaining Risks", [r"\bRemaining Risks\b", r"\bRemaining risks\b"]),
    ("Next Recommended Task", [r"\bNext Recommended Task\b", r"\bNext Fix\b"]),
]


class AggregateError(Exception):
    """User-facing aggregator error."""


def repo_rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def ensure_repo_path(path: Path, purpose: str) -> Path:
    resolved = path.resolve()
    try:
        rel = resolved.relative_to(ROOT).as_posix()
    except ValueError as exc:
        raise AggregateError(f"Refusing to {purpose} outside repo: {path}") from exc
    rel_lower = rel.lower().replace("\\", "/")
    if rel_lower.startswith(DEFAULT_FORBIDDEN_PREFIXES):
        raise AggregateError(f"Refusing to {purpose} default-forbidden path: {rel}")
    return resolved


def read_result_file(path: Path) -> str:
    resolved = ensure_repo_path(path, "read")
    if not resolved.is_file():
        raise AggregateError(f"Result file not found: {repo_rel(resolved)}")
    return resolved.read_text(encoding="utf-8", errors="replace")


def has_any(text: str, patterns: list[str]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def missing_basic_sections(text: str) -> list[str]:
    missing: list[str] = []
    for label, patterns in BASIC_SECTION_GROUPS:
        if not has_any(text, patterns):
            missing.append(label)
    return missing


def files_changed_none(text: str) -> bool:
    patterns = [
        r"files changed\s*[:\n\r -]*(none|no files|not modified|n/a)",
        r"files changed[^\n\r]*:\s*none",
        r"-\s*none",
    ]
    return has_any(text, patterns)


def mentions_read_only(text: str) -> bool:
    return has_any(text, [r"read[- ]only", r"did not modify files", r"no files were modified"])


def mentions_human_gate(text: str) -> bool:
    return has_any(text, [r"human gate", r"explicit.*authorization"])


def mentions_external_access(text: str) -> bool:
    return has_any(text, [r"\bHDC\b", r"\bSSH\b", r"\bserver\b", r"\bboard\b", r"app-server"])


def declares_no_external_access(text: str) -> bool:
    return has_any(
        text,
        [
            r"no .*HDC",
            r"no .*board",
            r"no .*server",
            r"did not .*HDC",
            r"did not .*board",
            r"did not .*server",
            r"not connect",
        ],
    )


def declares_no_git_add_commit(text: str) -> bool:
    return has_any(
        text,
        [
            r"no git add",
            r"no git commit",
            r"did not run git add",
            r"did not run git commit",
            r"not run git add",
            r"not run git commit",
        ],
    )


def has_blocking_issue(text: str) -> bool:
    if has_any(text, [r"no blocking issues", r"no blockers", r"blocking issues:\s*none"]):
        return False
    return has_any(
        text,
        [
            r"\bblocking issue\b",
            r"\bBLOCKER\b",
            r"request changes",
            r"not safe to (commit|proceed)",
            r"RESULT:\s*FAIL",
        ],
    )


def reviewer_has_verdict(text: str) -> bool:
    return has_any(text, [r"review verdict", r"verdict", r"approve", r"request changes"])


def reviewer_has_blocking_section(text: str) -> bool:
    return has_any(text, [r"blocking issues", r"blockers", r"no blocking"])


def reviewer_has_proceed_decision(text: str) -> bool:
    return has_any(text, [r"safe to (commit|proceed)", r"not safe to (commit|proceed)"])


def repair_scope_not_expanded(text: str) -> bool:
    return has_any(
        text,
        [
            r"scope (not )?expanded",
            r"did not expand scope",
            r"scope did not expand",
            r"no scope expansion",
        ],
    )


def repair_depth_present(text: str) -> bool:
    return has_any(text, [r"repair depth", r"depth\s*[:=]\s*\d+"])


def implementer_has_files_scope(text: str) -> bool:
    return has_any(text, [r"files changed", r"write scope", r"changed files"])


def check_agent_result(agent: str, text: str, strict: bool) -> dict[str, Any]:
    missing = missing_basic_sections(text)
    warnings: list[str] = []
    blockers: list[str] = []

    if missing:
        target = blockers if strict else warnings
        target.append(f"{agent}: missing basic section(s): {', '.join(missing)}")

    if agent == "project-explorer":
        if not mentions_read_only(text):
            warnings.append("project-explorer: read-only declaration not found")
        if not files_changed_none(text):
            warnings.append("project-explorer: files changed none declaration not found")

    if agent == "project-implementer":
        if not implementer_has_files_scope(text):
            warnings.append("project-implementer: files changed/write scope not found")
        if not declares_no_git_add_commit(text):
            warnings.append("project-implementer: no git add/commit declaration not found")
        if mentions_external_access(text) and not (
            declares_no_external_access(text) or mentions_human_gate(text)
        ):
            blockers.append(
                "project-implementer: board/server/HDC mention lacks no-access or human-gate declaration"
            )

    if agent == "project-reviewer":
        if not reviewer_has_verdict(text):
            warnings.append("project-reviewer: review verdict not found")
        if not reviewer_has_blocking_section(text):
            warnings.append("project-reviewer: blocking issues section not found")
        if not reviewer_has_proceed_decision(text):
            warnings.append("project-reviewer: safe to commit/proceed decision not found")

    if agent == "project-repairer":
        if not repair_depth_present(text):
            warnings.append("project-repairer: repair depth not found")
        if not repair_scope_not_expanded(text):
            warnings.append("project-repairer: repair scope non-expansion declaration not found")

    if has_blocking_issue(text):
        blockers.append(f"{agent}: blocking issue signal found")

    return {
        "agent": agent,
        "missing_sections": missing,
        "warnings": warnings,
        "blockers": blockers,
    }


def status_from_findings(
    blockers: list[str],
    warnings: list[str],
    missing_results: list[str],
    strict: bool,
) -> str:
    if blockers:
        return "FAIL" if strict else "PASS_WITH_BLOCKERS"
    if missing_results or warnings:
        return "PASS_WITH_WARNINGS"
    return "PASS"


def format_list(items: list[str], empty: str = "none") -> str:
    if not items:
        return f"- {empty}"
    return "\n".join(f"- {item}" for item in items)


def parse_planned_agents(prompt_text: str) -> list[str]:
    agents: list[str] = []
    in_mapping = False
    for raw in prompt_text.splitlines():
        line = raw.strip()
        if line.lower() == "## backend mapping":
            in_mapping = True
            continue
        if in_mapping and line.startswith("## "):
            break
        if not in_mapping:
            continue
        match = re.search(r"->\s*(project-[a-z-]+)", line)
        if match:
            agent = match.group(1)
            if agent in KNOWN_BACKEND_AGENTS:
                agents.append(agent)

    if agents:
        return agents

    # Fallback for prompt bundles that include only per-agent prompt headers.
    for agent in AGENT_FILES:
        if re.search(rf"\b{re.escape(agent)}\b", prompt_text):
            agents.append(agent)
    return agents


def check_prompt_result_consistency(
    run_dir: Path,
    strict: bool,
    result_agents: list[str],
) -> dict[str, Any]:
    prompt_path = run_dir / "prompt_bundle.md"
    if not prompt_path.exists():
        return {
            "prompt_bundle_detected": False,
            "planned_agents": [],
            "result_agents": result_agents,
            "missing_planned_results": [],
            "extra_unplanned_results": [],
            "sequence_match": "not_checked",
            "warnings": [],
            "blockers": [],
            "section_line": None,
        }

    prompt_text = read_result_file(prompt_path)
    planned_agents = parse_planned_agents(prompt_text)
    missing = [agent for agent in planned_agents if not (run_dir / AGENT_FILES[agent]).exists()]
    extra = [agent for agent in result_agents if agent not in planned_agents]
    result_planned_order = [agent for agent in result_agents if agent in planned_agents]
    sequence_match = result_planned_order == planned_agents

    warnings: list[str] = []
    blockers: list[str] = []
    if missing:
        message = "missing planned result(s): " + ", ".join(
            f"{agent} -> {AGENT_FILES[agent]}" for agent in missing
        )
        (blockers if strict else warnings).append(message)
    if extra:
        warnings.append("extra unplanned result(s): " + ", ".join(extra))
    if planned_agents and not sequence_match and not missing:
        message = "planned/result sequence mismatch"
        (blockers if strict else warnings).append(message)

    section_line = (
        "prompt_bundle consistency: "
        + ("PASS" if not warnings and not blockers else "FAIL" if blockers else "WARN")
    )

    return {
        "prompt_bundle_detected": True,
        "planned_agents": planned_agents,
        "result_agents": result_agents,
        "missing_planned_results": missing,
        "extra_unplanned_results": extra,
        "sequence_match": sequence_match,
        "warnings": warnings,
        "blockers": blockers,
        "section_line": section_line,
    }


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise AggregateError(f"Invalid JSON in {repo_rel(path)}: {exc}") from exc


def parse_front_matter(text: str) -> dict[str, str]:
    text = text.lstrip("\ufeff")
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return {}
    header: dict[str, str] = {}
    lines = text.splitlines()
    for raw in lines[1:]:
        if raw.strip() == "---":
            break
        if ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        header[key.strip()] = value.strip()
    return header


def content_lines_after_front_matter(text: str) -> list[str]:
    lines = text.lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        return lines
    for index, raw in enumerate(lines[1:], 1):
        if raw.strip() == "---":
            return lines[index + 1 :]
    return lines


def extract_reviewer_verdict(text: str) -> str | None:
    in_code_block = False
    for raw in content_lines_after_front_matter(text):
        stripped = raw.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue
        if raw.startswith((" ", "\t", ">")):
            continue
        if not raw.startswith("RESULT:"):
            continue
        if raw == "RESULT: PASS":
            return "PASS"
        match = re.match(r"^RESULT: ([A-Z_]+)$", raw)
        return match.group(1) if match else "INVALID"
    return None


def disallowed_result_markers(text: str) -> list[str]:
    found: list[str] = []
    for marker in STRICT_DISALLOWED_RESULT_MARKERS:
        if re.search(rf"^{re.escape(marker)}\s*$", text, flags=re.MULTILINE):
            found.append(marker)
    return found


def expected_role_name(role_spec: dict[str, Any]) -> str:
    return str(role_spec.get("agent_type") or role_spec.get("role") or "")


def result_file_for_role(role_spec: dict[str, Any]) -> str:
    role = expected_role_name(role_spec)
    return str(role_spec.get("result_file") or AGENT_FILES.get(role, ""))


def get_workflow_validation_scope(trace: dict[str, Any]) -> str:
    return str(
        trace.get("summary", {}).get("workflow_validation_scope")
        or trace.get("expected_workflow", {}).get("workflow_validation_scope")
        or trace.get("expected_workflow", {}).get("validation_scope")
        or ""
    )


def trace_allow_partial_value(trace: dict[str, Any]) -> bool | None:
    locations = (
        trace,
        trace.get("summary", {}),
        trace.get("invocation", {}),
        trace.get("options", {}),
    )
    for item in locations:
        if isinstance(item, dict) and "allow_partial" in item:
            return bool(item.get("allow_partial"))
    return None


def trace_allow_partial(trace: dict[str, Any]) -> bool:
    return bool(trace_allow_partial_value(trace))


def trace_adapter_exit_code(trace: dict[str, Any]) -> int | None:
    locations = (
        trace,
        trace.get("summary", {}),
        trace.get("invocation", {}),
    )
    for item in locations:
        if not isinstance(item, dict) or "adapter_exit_code" not in item:
            continue
        value = item.get("adapter_exit_code")
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value.strip())
    return None


def trace_int(trace: dict[str, Any], key: str) -> int | None:
    for item in (trace, trace.get("summary", {})):
        if not isinstance(item, dict) or key not in item:
            continue
        value = item.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value.strip())
    return None


def trace_list(trace: dict[str, Any], key: str) -> list[str] | None:
    for item in (trace, trace.get("summary", {})):
        if not isinstance(item, dict) or key not in item:
            continue
        value = item.get(key)
        if isinstance(value, list):
            return [str(entry) for entry in value]
    return None


def build_native_maps(trace: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    by_call_id: dict[str, dict[str, Any]] = {}
    by_role: dict[str, list[dict[str, Any]]] = {}
    for spawn in trace.get("spawns", []):
        if not isinstance(spawn, dict):
            continue
        call_id = str(spawn.get("call_id") or "")
        role = str(spawn.get("agent_type") or spawn.get("agent_role") or "")
        if call_id:
            by_call_id[call_id] = spawn
        if role:
            by_role.setdefault(role, []).append(spawn)
    return by_call_id, by_role


def build_result_capture_map(trace: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    mapping: dict[tuple[str, str], dict[str, Any]] = {}
    for item in trace.get("result_capture", {}).get("files", []):
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "")
        call_id = str(item.get("call_id") or "")
        if role or call_id:
            mapping[(role, call_id)] = item
    return mapping


def strict_link_blockers(spawn: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    link_mode = str(spawn.get("link_mode") or "")
    child_thread_id = str(spawn.get("child_thread_id") or "")
    proof = spawn.get("child_link_proof") if isinstance(spawn.get("child_link_proof"), dict) else {}

    if link_mode == "collab_agent_spawn_end":
        if not spawn.get("spawn_end_line"):
            blockers.append("collab_agent_spawn_end link_mode is missing spawn_end_line")
        proof_thread_id = str(proof.get("new_thread_id") or child_thread_id)
        if child_thread_id and proof_thread_id != child_thread_id:
            blockers.append("collab_agent_spawn_end child link proof does not match child_thread_id")
    elif link_mode == "function_call_output_agent_id":
        agent_id = str(spawn.get("function_call_output_agent_id") or proof.get("agent_id") or "")
        if spawn.get("function_call_output_present") is not True:
            blockers.append("function_call_output_agent_id link_mode is missing function_call_output")
        if not child_thread_id:
            blockers.append("function_call_output_agent_id link_mode is missing child_thread_id")
        if not agent_id or agent_id != child_thread_id:
            blockers.append("function_call_output_agent_id does not exactly match child_thread_id")
        if proof.get("exact_match") is not True:
            blockers.append("function_call_output_agent_id proof exact_match is not true")
    else:
        blockers.append(f"unsupported deterministic child link_mode: {link_mode or '<missing>'}")

    return blockers


def aggregate_native_strict(run_dir: Path) -> tuple[dict[str, Any], str, int]:
    expected_path = run_dir / NATIVE_EXPECTED_WORKFLOW
    trace_path = run_dir / NATIVE_CAPTURE_TRACE
    blockers: list[str] = []
    warnings: list[str] = []
    role_results: list[dict[str, Any]] = []

    if not expected_path.exists():
        blockers.append(f"missing {NATIVE_EXPECTED_WORKFLOW}")
        manifest: dict[str, Any] = {"expected_roles": []}
    else:
        manifest = load_json(expected_path)

    if not trace_path.exists():
        blockers.append(f"missing {NATIVE_CAPTURE_TRACE}")
        trace: dict[str, Any] = {}
    else:
        trace = load_json(trace_path)

    parent_thread_id = str(manifest.get("parent_thread_id") or trace.get("summary", {}).get("parent_thread_id") or "")
    expected_roles = manifest.get("expected_roles", [])
    if not isinstance(expected_roles, list):
        blockers.append("expected_workflow.json expected_roles is not a list")
        expected_roles = []
    required_roles = [item for item in expected_roles if bool(item.get("required", True))]

    adapter_exit_code = trace_adapter_exit_code(trace)
    allow_partial = trace_allow_partial_value(trace)
    capture_status = str(trace.get("capture_status") or trace.get("summary", {}).get("capture_status") or "")
    workflow_validation_scope = get_workflow_validation_scope(trace)
    expected_call_ids_present = bool(
        trace.get("expected_call_ids_present")
        or trace.get("summary", {}).get("expected_call_ids_present")
    )
    missing_spawn_end_call_ids = trace_list(trace, "missing_spawn_end_call_ids")
    in_scope_partial_count = trace_int(trace, "in_scope_partial_count")
    out_of_scope_partial_count = trace_int(trace, "out_of_scope_partial_count")

    if adapter_exit_code is None:
        blockers.append("trace adapter_exit_code is missing")
    elif adapter_exit_code != 0:
        blockers.append(f"trace adapter_exit_code is nonzero: {adapter_exit_code}")
    if allow_partial is None:
        blockers.append("trace allow_partial is missing")
    elif allow_partial:
        blockers.append("trace indicates --allow-partial was used")
    if capture_status != "complete":
        blockers.append(f"trace capture_status is not complete: {capture_status or '<missing>'}")
    if workflow_validation_scope != "expected_call_ids":
        blockers.append(
            "workflow_validation_scope is not expected_call_ids: "
            f"{workflow_validation_scope or '<missing>'}"
        )
    if not expected_call_ids_present:
        blockers.append("trace expected_call_ids_present is missing or false")
    if missing_spawn_end_call_ids is None:
        blockers.append("trace missing_spawn_end_call_ids is missing")
    elif missing_spawn_end_call_ids:
        blockers.append(
            "in-scope spawn_agent call(s) missing collab_agent_spawn_end: "
            + ", ".join(missing_spawn_end_call_ids)
        )
    if in_scope_partial_count is None:
        blockers.append("trace in_scope_partial_count is missing")
    elif in_scope_partial_count:
        blockers.append(f"trace in_scope_partial_count is nonzero: {in_scope_partial_count}")
    if out_of_scope_partial_count is None:
        blockers.append("trace out_of_scope_partial_count is missing")
    elif out_of_scope_partial_count:
        warnings.append(f"trace out_of_scope_partial_count is nonzero: {out_of_scope_partial_count}")

    missing_expected_roles = trace.get("expected_workflow", {}).get("missing_expected_roles", [])
    if missing_expected_roles:
        blockers.append("missing expected role(s): " + ", ".join(map(str, missing_expected_roles)))

    by_call_id, by_role = build_native_maps(trace)
    capture_files = build_result_capture_map(trace)
    expected_call_ids = [
        str(item.get("call_id"))
        for item in expected_roles
        if isinstance(item, dict) and item.get("call_id")
    ]
    missing_expected_call_ids = [
        call_id for call_id in expected_call_ids
        if call_id not in by_call_id
    ]
    if missing_expected_call_ids:
        blockers.append(
            "expected call_id(s) missing from trace: "
            + ", ".join(missing_expected_call_ids)
        )
    observed_required = 0
    reviewer_verdict: str | None = None

    for spec in expected_roles:
        role = expected_role_name(spec)
        call_id = str(spec.get("call_id") or "")
        result_file = result_file_for_role(spec)
        required = bool(spec.get("required", True))
        role_blockers: list[str] = []
        role_warnings: list[str] = []

        spawn = by_call_id.get(call_id) if call_id else None
        if spawn is None and role:
            candidates = by_role.get(role, [])
            spawn = candidates[0] if len(candidates) == 1 else None
        if required and spawn is None:
            role_blockers.append("expected role missing from trace")

        if spawn is not None:
            observed_required += 1 if required else 0
            if not isinstance(spawn.get("parent_spawn_call"), dict):
                role_blockers.append("missing parent spawn_agent call")
            role_blockers.extend(strict_link_blockers(spawn))
            child_meta = spawn.get("child", {}).get("session_meta", {})
            child_parent = str(child_meta.get("parent_thread_id") or "")
            if not child_meta:
                role_blockers.append("missing child session_meta")
            elif parent_thread_id and child_parent != parent_thread_id:
                role_blockers.append(
                    f"child parent_thread_id mismatch: {child_parent or '<missing>'}"
                )
            if str(spawn.get("parent_thread_id") or "") and parent_thread_id:
                if str(spawn.get("parent_thread_id")) != parent_thread_id:
                    role_blockers.append("spawn parent_thread_id does not match manifest")
            task_complete = spawn.get("child", {}).get("task_complete", {})
            task_message = task_complete.get("last_agent_message", {}) if isinstance(task_complete, dict) else {}
            if not (
                isinstance(task_complete, dict)
                and task_complete.get("source") == "task_complete.last_agent_message"
                and isinstance(task_message, dict)
                and task_message.get("present") is True
            ):
                role_blockers.append("missing child task_complete.last_agent_message")

        if not result_file:
            role_blockers.append("expected result_file missing in manifest and no default mapping")
            result_path = run_dir / "__missing_result_file__"
            text = ""
            header: dict[str, str] = {}
        else:
            result_path = run_dir / result_file
            if not result_path.exists():
                role_blockers.append(f"expected result_file missing: {result_file}")
                text = ""
                header = {}
            else:
                text = read_result_file(result_path)
                header = parse_front_matter(text)

        if result_file and result_path.exists():
            status = str(header.get("status") or "").upper()
            if status in {"CAPTURE_FAILED", "FAIL"}:
                role_blockers.append(f"result header status is {status}")
            for marker in disallowed_result_markers(text):
                role_blockers.append(f"result file contains {marker}")
            if parent_thread_id and str(header.get("parent_thread_id") or "") != parent_thread_id:
                role_blockers.append("result header parent_thread_id mismatch")
            if call_id and str(header.get("call_id") or "") != call_id:
                role_blockers.append("result header call_id mismatch")
            if not str(header.get("result_sha256") or ""):
                role_blockers.append("result_sha256 is missing")

            capture_item = capture_files.get((role, call_id)) or capture_files.get((role, ""))
            if capture_item and str(capture_item.get("status") or "").upper() in {"CAPTURE_FAILED", "FAIL"}:
                role_blockers.append(
                    f"trace result_capture status is {capture_item.get('status')}"
                )
            elif not capture_item:
                role_warnings.append("trace result_capture file entry not found")

            if role == "project-reviewer":
                reviewer_verdict = extract_reviewer_verdict(text)
                if reviewer_verdict is None:
                    role_blockers.append("project-reviewer verdict missing; strict status NEEDS_REVIEW")
                elif reviewer_verdict != "PASS":
                    role_blockers.append(f"project-reviewer verdict is not PASS: {reviewer_verdict}")

        role_results.append(
            {
                "role": role,
                "required": required,
                "call_id": call_id,
                "result_file": result_file,
                "capture_status": spawn.get("capture_status") if spawn else "missing",
                "link_mode": spawn.get("link_mode") if spawn else None,
                "child_thread_id": spawn.get("child_thread_id") if spawn else None,
                "result_header_status": header.get("status") if header else None,
                "result_sha256": header.get("result_sha256") if header else None,
                "reviewer_verdict": reviewer_verdict if role == "project-reviewer" else None,
                "blockers": role_blockers,
                "warnings": role_warnings,
            }
        )
        blockers.extend(f"{role}: {item}" for item in role_blockers)
        warnings.extend(f"{role}: {item}" for item in role_warnings)

    if observed_required != len(required_roles):
        blockers.append(
            "required role count does not match expected_workflow.json: "
            f"observed={observed_required}, expected={len(required_roles)}"
        )

    overall = "FAIL" if blockers else "PASS"
    source_run_dir = repo_rel(run_dir)
    generated_at = datetime.now(timezone.utc).isoformat()
    bundle: dict[str, Any] = {
        "schema": "codex-harness.native_subagent_aggregation.v2",
        "generated_at": generated_at,
        "source_run_dir": source_run_dir,
        "overall_status": overall,
        "strict_mode": True,
        "adapter_exit_code": adapter_exit_code,
        "capture_status": capture_status,
        "workflow_validation_scope": workflow_validation_scope,
        "allow_partial": allow_partial,
        "expected_call_ids_present": expected_call_ids_present,
        "missing_spawn_end_call_ids": missing_spawn_end_call_ids,
        "in_scope_partial_count": in_scope_partial_count,
        "out_of_scope_partial_count": out_of_scope_partial_count,
        "link_modes": [
            str(spawn.get("link_mode") or "")
            for spawn in trace.get("spawns", [])
            if isinstance(spawn, dict)
        ],
        "parent_thread_id": parent_thread_id,
        "required_role_count": len(required_roles),
        "observed_required_role_count": observed_required,
        "reviewer_verdict": reviewer_verdict,
        "source_readiness_gate": {
            "adapter_exit_code_ok": adapter_exit_code == 0,
            "allow_partial_ok": allow_partial is False,
            "capture_status_ok": capture_status == "complete",
            "workflow_validation_scope_ok": workflow_validation_scope == "expected_call_ids",
            "expected_call_ids_present": expected_call_ids_present,
            "missing_spawn_end_call_ids": missing_spawn_end_call_ids or [],
            "missing_expected_call_ids": missing_expected_call_ids,
            "strict_link_modes_ok": not any(
                strict_link_blockers(spawn)
                for spawn in trace.get("spawns", [])
                if isinstance(spawn, dict)
            ),
            "reviewer_pass": reviewer_verdict == "PASS",
        },
        "role_results": role_results,
        "blocking_issues": blockers,
        "non_blocking_warnings": warnings,
    }
    summary = render_native_summary(bundle)
    return bundle, summary, 1 if overall == "FAIL" else 0


def render_native_summary(bundle: dict[str, Any]) -> str:
    lines = [
        "# Codex Native Subagent Strict Aggregation",
        "",
        f"source_run_dir: {bundle['source_run_dir']}",
        f"overall_status: {bundle['overall_status']}",
        f"adapter_exit_code: {bundle.get('adapter_exit_code') if bundle.get('adapter_exit_code') is not None else 'missing'}",
        f"capture_status: {bundle['capture_status']}",
        f"workflow_validation_scope: {bundle['workflow_validation_scope']}",
        f"allow_partial: {str(bundle['allow_partial']).lower() if bundle.get('allow_partial') is not None else 'missing'}",
        f"parent_thread_id: {bundle['parent_thread_id']}",
        f"reviewer_verdict: {bundle.get('reviewer_verdict') or 'NEEDS_REVIEW'}",
        "",
        "role_results:",
    ]
    for item in bundle["role_results"]:
        status = "PASS" if not item["blockers"] else "FAIL"
        lines.append(
            f"- {item['role']}: {status} call_id={item.get('call_id') or '<none>'} "
            f"link_mode={item.get('link_mode') or '<none>'} "
            f"result_file={item.get('result_file') or '<none>'}"
        )
        for blocker in item["blockers"]:
            lines.append(f"  blocker: {blocker}")
        for warning in item["warnings"]:
            lines.append(f"  warning: {warning}")
    lines.extend(
        [
            "",
            "blocking_issues:",
            format_list(bundle["blocking_issues"]),
            "",
            "non_blocking_warnings:",
            format_list(bundle["non_blocking_warnings"]),
            "",
            "strict_native_rules:",
            "- expected_workflow.json and spawn_capture_trace.json required",
            "- adapter_exit_code must be present and zero",
            "- allow_partial must be present and false",
            "- capture_status must be complete",
            "- workflow_validation_scope must be expected_call_ids",
            "- in-scope spawn_agent calls must have deterministic child links",
            "- accepted link modes are collab_agent_spawn_end or exact function_call_output.agent_id",
            "- required role, child session_meta, parent thread, call_id, result file, and result_sha256 are checked",
            "- project-reviewer must explicitly report top-level RESULT: PASS",
            "",
        ]
    )
    return "\n".join(lines)


def render_agent_result_bundle(bundle: dict[str, Any]) -> str:
    status = bundle["overall_status"]
    lines = [
        "# AgentResultBundle",
        "",
        "## Summary",
        f"RESULT: {status}",
        f"source_run_dir: {bundle['source_run_dir']}",
        f"adapter_exit_code: {bundle.get('adapter_exit_code') if bundle.get('adapter_exit_code') is not None else 'missing'}",
        f"capture_status: {bundle['capture_status']}",
        f"reviewer_verdict: {bundle.get('reviewer_verdict') or 'NEEDS_REVIEW'}",
        "",
        "## Files/logs inspected",
        "- expected_workflow.json",
        "- spawn_capture_trace.json",
        "- captured subagent result markdown files",
        "",
        "## Aggregation result",
        f"overall_status: {status}",
        "",
        "## Blocking issues",
        format_list(bundle["blocking_issues"]),
        "",
        "## Non-blocking warnings",
        format_list(bundle["non_blocking_warnings"]),
        "",
        "## Boundary check",
        "- did not call spawn_agent from Python",
        "- did not use codex exec for subagents",
        "- did not modify business files",
        "- did not call downstream aggregator beyond this strict aggregation step",
        "",
    ]
    return "\n".join(lines)


def render_v2_acceptance_report(bundle: dict[str, Any]) -> str:
    v2_status = "V2_PASS" if bundle["overall_status"] == "PASS" else "V2_NOT_PASS"
    lines = [
        "# V2 Strict Aggregation Acceptance Report",
        "",
        f"result: {v2_status}",
        f"source_run_dir: {bundle['source_run_dir']}",
        f"strict_aggregation_exit_expected: {'0' if bundle['overall_status'] == 'PASS' else 'nonzero'}",
        f"adapter_exit_code: {bundle.get('adapter_exit_code') if bundle.get('adapter_exit_code') is not None else 'missing'}",
        f"capture_status: {bundle['capture_status']}",
        f"workflow_validation_scope: {bundle['workflow_validation_scope']}",
        f"allow_partial: {str(bundle['allow_partial']).lower() if bundle.get('allow_partial') is not None else 'missing'}",
        f"reviewer_verdict: {bundle.get('reviewer_verdict') or 'NEEDS_REVIEW'}",
        "",
        "acceptance_notes:",
    ]
    if bundle["overall_status"] == "PASS":
        lines.append("- Strict aggregation may be accepted for this source run.")
    else:
        lines.append("- Strict aggregation did not pass; do not claim V2_PASS.")
        lines.append("- The blockers below must be resolved or the source run must provide a PASS reviewer verdict.")
    lines.extend(["", "blocking_issues:", format_list(bundle["blocking_issues"]), ""])
    return "\n".join(lines)


def write_v2_outputs(out_dir: Path, bundle: dict[str, Any], summary: str) -> None:
    resolved = ensure_repo_path(out_dir, "write")
    resolved.mkdir(parents=True, exist_ok=True)
    (resolved / "aggregation_summary.md").write_text(summary, encoding="utf-8")
    (resolved / "agent_result_bundle.md").write_text(render_agent_result_bundle(bundle), encoding="utf-8")
    (resolved / "aggregation_trace.json").write_text(
        json.dumps(bundle, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (resolved / "v2_acceptance_report.md").write_text(
        render_v2_acceptance_report(bundle),
        encoding="utf-8",
    )


def native_strict_inputs_present(run_dir: Path) -> bool:
    return (run_dir / NATIVE_EXPECTED_WORKFLOW).exists() or (run_dir / NATIVE_CAPTURE_TRACE).exists()


def aggregate(run_dir: Path, strict: bool) -> tuple[str, int, dict[str, Any] | None]:
    resolved_run_dir = ensure_repo_path(run_dir, "read")
    if not resolved_run_dir.is_dir():
        raise AggregateError(f"Run directory not found: {repo_rel(resolved_run_dir)}")

    if strict and native_strict_inputs_present(resolved_run_dir):
        bundle, summary, exit_code = aggregate_native_strict(resolved_run_dir)
        return summary, exit_code, bundle

    detected_agents: list[str] = []
    missing_results: list[str] = []
    section_lines: list[str] = []
    warnings: list[str] = []
    blockers: list[str] = []

    for filename in ALLOWED_RESULT_FILES:
        if filename not in FILE_AGENTS:
            continue
        agent = FILE_AGENTS[filename]
        path = resolved_run_dir / filename
        if not path.exists():
            missing_results.append(filename)
            continue
        text = read_result_file(path)
        detected_agents.append(agent)
        check = check_agent_result(agent, text, strict)
        missing = check["missing_sections"]
        section_status = "PASS" if not missing else ("FAIL" if strict else "WARN")
        section_lines.append(
            f"{agent}: {section_status}"
            + (f" missing={', '.join(missing)}" if missing else "")
        )
        warnings.extend(check["warnings"])
        blockers.extend(check["blockers"])

    consistency = check_prompt_result_consistency(resolved_run_dir, strict, detected_agents)
    if consistency["prompt_bundle_detected"]:
        planned_missing_files = {
            AGENT_FILES[agent] for agent in consistency["missing_planned_results"]
        }
        missing_results = [name for name in missing_results if name in planned_missing_files]
        if consistency["section_line"]:
            section_lines.append(consistency["section_line"])
        warnings.extend(consistency["warnings"])
        blockers.extend(consistency["blockers"])

    for filename in ("agent_result_bundle.md", "prompt_bundle.md"):
        path = resolved_run_dir / filename
        if path.exists():
            text = read_result_file(path)
            missing = [] if filename == "prompt_bundle.md" else missing_basic_sections(text)
            section_status = "PASS" if not missing else ("FAIL" if strict else "WARN")
            section_lines.append(
                f"{filename}: {section_status}"
                + (f" missing={', '.join(missing)}" if missing else "")
            )
            if missing:
                target = blockers if strict else warnings
                target.append(f"{filename}: missing basic section(s): {', '.join(missing)}")
            if has_blocking_issue(text):
                blockers.append(f"{filename}: blocking issue signal found")

    if strict and missing_results:
        blockers.extend(f"missing expected result file: {name}" for name in missing_results)

    overall = status_from_findings(blockers, warnings, missing_results, strict)
    recommended = (
        "Resolve blocking issues before proceeding."
        if blockers
        else "Fill missing result files or warning sections, then re-run aggregation."
        if warnings or missing_results
        else "Manual aggregation checks passed; proceed to human review."
    )

    lines = [
        "# Codex Subagent Result Aggregation Dry Run",
        "",
        "real spawn adapter inactive",
        "no scheduler",
        "no parallel fan-out",
        "no automatic git add/commit",
        "no validation commands were executed",
        "",
        f"overall_status: {overall}",
        "",
        "detected_agents:",
        format_list(detected_agents),
        "",
        "missing_results:",
        format_list(missing_results),
        "",
        f"prompt_bundle_detected: {str(consistency['prompt_bundle_detected']).lower()}",
        "",
        "planned_agents:",
        format_list(consistency["planned_agents"]),
        "",
        "result_agents:",
        format_list(consistency["result_agents"]),
        "",
        "missing_planned_results:",
        format_list(consistency["missing_planned_results"]),
        "",
        "extra_unplanned_results:",
        format_list(consistency["extra_unplanned_results"]),
        "",
        f"sequence_match: {consistency['sequence_match']}",
        "",
        "section_check_summary:",
        format_list(section_lines),
        "",
        "blocking_issues:",
        format_list(blockers),
        "",
        "non_blocking_warnings:",
        format_list(warnings),
        "",
        f"recommended_next_step: {recommended}",
        "",
    ]
    return "\n".join(lines), 1 if overall == "FAIL" else 0, None


def write_summary(out_file: Path, text: str) -> None:
    resolved = ensure_repo_path(out_file, "write")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(text, encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Aggregate manually saved subagent result bundles without executing agents."
    )
    parser.add_argument("--run-dir", required=True, help="Directory containing saved result files.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Aggregate only. This is always true in aggregator v0.",
    )
    parser.add_argument("--out-file", help="Optional file path for aggregate summary.")
    parser.add_argument(
        "--out-dir",
        help=(
            "Optional output directory for V2 native strict aggregation files "
            "(aggregation_summary.md, agent_result_bundle.md, aggregation_trace.json, v2_acceptance_report.md)."
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat missing sections and missing expected result files as blockers.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        summary, exit_code, bundle = aggregate(ROOT / args.run_dir, args.strict)
        if args.out_file:
            write_summary(ROOT / args.out_file, summary)
            print(f"[AGGREGATOR_DRY_RUN] wrote summary: {repo_rel(ROOT / args.out_file)}")
            print()
        if args.out_dir:
            if bundle is None:
                raise AggregateError("--out-dir is only supported for native strict aggregation inputs")
            write_v2_outputs(ROOT / args.out_dir, bundle, summary)
            print(f"[AGGREGATOR_DRY_RUN] wrote V2 outputs: {repo_rel(ROOT / args.out_dir)}")
            print()
        print(summary, end="")
        return exit_code
    except AggregateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
