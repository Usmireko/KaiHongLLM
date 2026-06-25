#!/usr/bin/env python3
"""Dry-run prompt bundle dispatcher for Codex harness roles.

This tool is intentionally non-executing: it reads a TaskSpec and roles.yaml,
selects a serial role sequence, and emits prompts for manual subagent handoff.
It does not call Codex, spawn agents, run validators, or connect to external
systems.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import io
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
HARNESS_ROOT = ROOT / ".codex-harness"
ROLES_PATH = HARNESS_ROOT / "templates" / "roles.yaml"
REHEARSAL_TASK_SPEC = HARNESS_ROOT / "tasks" / "examples" / "subagent_roles_rehearsal_task_spec.yaml"

DEFAULT_FORBIDDEN_PATHS = [
    ".git/**",
    "storage/runs/**",
    "dataset/raw/**",
    "dataset/l0/**",
    "dataset/l1/**",
    "dataset/l2/**",
    "models/**",
    "checkpoints/**",
    "inbox/runs/**",
    "_tmp/**",
    "_bundles/**",
]

FORBIDDEN_READ_PREFIXES = (
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
    ".git/",
)

ROLE_ORDER = {
    "validation-only": ["validator"],
    "review-only": ["reviewer"],
    "repair": ["repairer"],
}

SUPPORTED_WORKFLOWS = {
    "auto",
    "explorer-reviewer",
    "explorer-implementer-reviewer",
}

BACKEND_FALLBACKS = {
    "explorer": "project-explorer",
    "implementer": "project-implementer",
    "reviewer": "project-reviewer",
    "adversarial_reviewer": "project-reviewer",
    "validator": None,
    "repairer": "project-repairer",
}

SELF_CHECK_CASES = [
    {
        "name": "low",
        "task": {"risk_level": "low", "task_type": "implementation"},
        "expected": ["implementer"],
    },
    {
        "name": "low with review requested",
        "task": {
            "risk_level": "low",
            "task_type": "implementation",
            "codex_audit_required": "standard",
        },
        "expected": ["implementer", "reviewer"],
    },
    {
        "name": "medium",
        "task": {"risk_level": "medium", "task_type": "implementation"},
        "expected": ["explorer", "implementer", "reviewer"],
    },
    {
        "name": "high",
        "task": {"risk_level": "high", "task_type": "implementation"},
        "expected": ["explorer", "implementer", "adversarial_reviewer", "reviewer"],
    },
    {
        "name": "validation-only",
        "task": {"risk_level": "medium", "task_type": "validation-only"},
        "expected": ["validator"],
    },
    {
        "name": "review-only standard",
        "task": {"risk_level": "medium", "task_type": "review-only"},
        "expected": ["reviewer"],
    },
    {
        "name": "review-only adversarial",
        "task": {
            "risk_level": "high",
            "task_type": "review-only",
            "adversarial_review_required": True,
        },
        "expected": ["adversarial_reviewer"],
    },
    {
        "name": "repair",
        "task": {"risk_level": "medium", "task_type": "repair"},
        "expected": ["repairer"],
    },
    {
        "name": "workflow explorer-reviewer",
        "task": {"risk_level": "low", "task_type": "review-only"},
        "workflow": "explorer-reviewer",
        "expected": ["explorer", "reviewer"],
    },
    {
        "name": "workflow explorer-implementer-reviewer",
        "task": {"risk_level": "medium", "task_type": "implementation"},
        "workflow": "explorer-implementer-reviewer",
        "expected": ["explorer", "implementer", "reviewer"],
    },
]


class DispatchError(Exception):
    """User-facing dispatcher error."""


def repo_rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def read_text_file(path: Path) -> str:
    resolved = path.resolve()
    try:
        rel = resolved.relative_to(ROOT).as_posix()
    except ValueError as exc:
        raise DispatchError(f"Refusing to read path outside repo: {path}") from exc

    rel_lower = rel.lower().replace("\\", "/")
    if rel_lower.startswith(FORBIDDEN_READ_PREFIXES):
        raise DispatchError(f"Refusing to read default-forbidden path: {rel}")
    if not resolved.is_file():
        raise DispatchError(f"File not found: {rel}")
    return resolved.read_text(encoding="utf-8", errors="replace")


def strip_quotes(value: str) -> str:
    value = value.strip()
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    return value


def scalar_for_key(text: str, key: str, default: str = "") -> str:
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*:\s*(.+?)\s*$", re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return default
    value = match.group(1).split("#", 1)[0].strip()
    if value in {">", "|"}:
        return default
    return strip_quotes(value)


def bool_for_key(text: str, key: str, default: bool = False) -> bool:
    value = scalar_for_key(text, key, "")
    if not value:
        return default
    return value.lower() == "true"


def section_block(text: str, section: str) -> str:
    pattern = re.compile(rf"^{re.escape(section)}:\s*$", re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return ""
    start = match.end()
    next_top = re.search(r"^(?!\s)[A-Za-z0-9_-]+:\s*$", text[start:], re.MULTILINE)
    end = start + next_top.start() if next_top else len(text)
    return text[start:end]


def list_for_key(text: str, key: str) -> list[str]:
    pattern = re.compile(rf"^(\s*){re.escape(key)}\s*:\s*$", re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return []

    base_indent = len(match.group(1))
    items: list[str] = []
    for line in text[match.end() :].splitlines():
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if indent <= base_indent and not stripped.startswith("- "):
            break
        if stripped.startswith("- "):
            items.append(strip_quotes(stripped[2:].strip()))
    return items


def parse_task_spec(task_path: Path, text: str) -> dict[str, Any]:
    task_block = section_block(text, "task")
    intent_block = section_block(text, "intent")
    paths_block = section_block(text, "paths")
    validation_block = section_block(text, "validation")
    review_block = section_block(text, "review")
    limits_block = section_block(text, "limits")

    summary = scalar_for_key(task_block, "title") or scalar_for_key(intent_block, "objective")
    task_type = scalar_for_key(intent_block, "task_type", "implementation")
    risk_level = scalar_for_key(task_block, "risk_level", "medium").lower()

    return {
        "path": repo_rel(task_path),
        "id": scalar_for_key(task_block, "id", "unknown-task"),
        "title": summary or "Untitled TaskSpec",
        "risk_level": risk_level,
        "task_type": task_type,
        "objective": summary or scalar_for_key(intent_block, "expected_output", ""),
        "allowed_paths": list_for_key(paths_block, "allowed_paths"),
        "write_allowed": list_for_key(paths_block, "write_allowed"),
        "read_only": list_for_key(paths_block, "read_only"),
        "forbidden_paths": merge_unique(
            DEFAULT_FORBIDDEN_PATHS, list_for_key(paths_block, "forbidden_paths")
        ),
        "validation_commands": list_for_key(validation_block, "validation_commands"),
        "manual_only": bool_for_key(validation_block, "manual_only", True),
        "no_runtime_execution": bool_for_key(validation_block, "no_runtime_execution", True),
        "codex_audit_required": scalar_for_key(review_block, "codex_audit_required", "none"),
        "adversarial_review_required": bool_for_key(
            review_block, "adversarial_review_required", False
        ),
        "allow_hdc": bool_for_key(limits_block, "allow_hdc", False),
        "allow_ssh": bool_for_key(limits_block, "allow_ssh", False),
        "allow_board_connection": bool_for_key(
            limits_block, "allow_board_connection", False
        ),
        "allow_server_connection": bool_for_key(
            limits_block, "allow_server_connection", False
        ),
        "allow_app_server": bool_for_key(limits_block, "allow_app_server", False),
        "allow_real_collection": bool_for_key(
            limits_block, "allow_real_collection", False
        ),
        "allow_multi_agent_concurrency": bool_for_key(
            limits_block, "allow_multi_agent_concurrency", False
        ),
        "max_repair_depth": scalar_for_key(limits_block, "max_repair_depth", ""),
        "absolute_max_repair_depth": scalar_for_key(
            limits_block, "absolute_max_repair_depth", ""
        ),
    }


def merge_unique(*groups: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for group in groups:
        for item in group:
            if item and item not in seen:
                result.append(item)
                seen.add(item)
    return result


def parse_roles(text: str) -> dict[str, dict[str, Any]]:
    roles: dict[str, dict[str, Any]] = {}
    lines = text.splitlines()
    current: dict[str, Any] | None = None
    current_key: str | None = None

    for raw in lines:
        stripped = raw.strip()
        if stripped.startswith("- role_id:"):
            role_id = strip_quotes(stripped.split(":", 1)[1].strip())
            current = {"role_id": role_id}
            roles[role_id] = current
            current_key = None
            continue
        if current is None:
            continue

        if re.match(r"^[A-Za-z0-9_]+:", stripped):
            key, value = stripped.split(":", 1)
            value = value.strip()
            if value:
                current[key] = None if value == "null" else strip_quotes(value)
                current_key = None
            else:
                current[key] = []
                current_key = key
            continue

        if current_key and stripped.startswith("- "):
            current.setdefault(current_key, []).append(strip_quotes(stripped[2:].strip()))

    for role_id, backend in BACKEND_FALLBACKS.items():
        roles.setdefault(role_id, {"role_id": role_id, "backend_agent": backend})
    return roles


def requests_review(task: dict[str, Any]) -> bool:
    audit = str(task.get("codex_audit_required", "none")).lower()
    return (
        audit not in {"", "none", "false"}
        or bool(task.get("adversarial_review_required"))
    )


def select_roles(
    task: dict[str, Any],
    risk_override: str | None,
    type_override: str | None,
    workflow: str = "auto",
) -> list[str]:
    if workflow not in SUPPORTED_WORKFLOWS:
        raise DispatchError(f"Unknown workflow: {workflow}")
    if workflow == "explorer-reviewer":
        return ["explorer", "reviewer"]
    if workflow == "explorer-implementer-reviewer":
        return ["explorer", "implementer", "reviewer"]

    task_type = (type_override or task["task_type"] or "implementation").lower()
    risk = (risk_override or task["risk_level"] or "medium").lower()

    if task_type in {"validation-only", "validation"}:
        return ROLE_ORDER["validation-only"]
    if task_type in {"review-only", "review"}:
        if risk == "high" or task.get("adversarial_review_required"):
            return ["adversarial_reviewer"]
        return ROLE_ORDER["review-only"]
    if task_type == "repair":
        return ROLE_ORDER["repair"]

    if risk == "low":
        roles = ["implementer"]
        if requests_review(task):
            roles.append("reviewer")
        return roles
    if risk == "high":
        return ["explorer", "implementer", "adversarial_reviewer", "reviewer"]
    return ["explorer", "implementer", "reviewer"]


def self_check_task(case_task: dict[str, Any]) -> dict[str, Any]:
    task = {
        "path": "<synthetic-self-check>",
        "id": "SELF-CHECK",
        "title": "Synthetic routing self-check case",
        "risk_level": "medium",
        "task_type": "implementation",
        "objective": "Check dispatcher role routing only.",
        "allowed_paths": [".codex-harness/**"],
        "write_allowed": [],
        "read_only": [".codex-harness/templates/roles.yaml"],
        "forbidden_paths": DEFAULT_FORBIDDEN_PATHS[:],
        "validation_commands": [],
        "manual_only": True,
        "no_runtime_execution": True,
        "codex_audit_required": "none",
        "adversarial_review_required": False,
        "allow_hdc": False,
        "allow_ssh": False,
        "allow_board_connection": False,
        "allow_server_connection": False,
        "allow_app_server": False,
        "allow_real_collection": False,
        "allow_multi_agent_concurrency": False,
        "max_repair_depth": "1",
        "absolute_max_repair_depth": "2",
    }
    task.update(case_task)
    return task


def run_self_check_routing() -> int:
    print("# Codex Subagent Dispatcher v0 Routing Self-Check")
    print()
    print("- real spawn adapter inactive")
    print("- no scheduler")
    print("- no parallel fan-out")
    print("- no automatic git add/commit")
    print("- no agents were called")
    print()

    failures = 0
    for case in SELF_CHECK_CASES:
        task = self_check_task(case["task"])
        workflow = case.get("workflow", "auto")
        actual = select_roles(task, None, None, workflow)
        expected = case["expected"]
        ok = actual == expected
        status = "PASS" if ok else "FAIL"
        print(f"{status}: {case['name']}")
        print(f"  expected: {' -> '.join(expected)}")
        print(f"  actual:   {' -> '.join(actual)}")
        if not ok:
            failures += 1

    print()
    if failures:
        print(f"RESULT: FAIL ({failures} routing case(s) failed)")
        return 1
    print("RESULT: PASS")
    return 0


def role_output_contract(role: dict[str, Any]) -> list[str]:
    value = role.get("required_sections")
    if isinstance(value, list) and value:
        return value
    contract = role.get("output_contract")
    if isinstance(contract, list) and contract:
        return contract
    defaults = {
        "explorer": [
            "Relevant files",
            "Data/control flow",
            "Suggested edit points",
            "Validation commands",
            "Risks",
        ],
        "implementer": [
            "Files changed",
            "Key changes",
            "Validation commands run",
            "Validation result",
            "Remaining risks",
        ],
        "reviewer": ["Critical", "High", "Medium", "Low", "Recommended fix order"],
        "adversarial_reviewer": [
            "Critical",
            "High",
            "Medium",
            "Low",
            "Semantic boundary assessment",
            "Recommended fix order",
        ],
        "validator": [
            "Commands run",
            "RESULT",
            "Failed criteria",
            "Warnings",
            "Evidence summary",
            "Next fix",
        ],
        "repairer": [
            "Repair depth",
            "Trigger",
            "Failed command or review finding",
            "Hypothesis",
            "Files touched",
            "Patch summary",
            "Re-validation",
            "Result",
        ],
    }
    return defaults.get(str(role.get("role_id")), ["AgentResultBundle"])


def role_specific_non_actions(role_id: str, task: dict[str, Any]) -> list[str]:
    common = [
        "Do not call Codex CLI or spawn a real subagent from this prompt.",
        "Do not connect to board, HDC, SSH, server, SCP, or app-server.",
        "Do not run real collection.",
        "Do not use parallel fan-out or scheduler behavior.",
        "Do not run git add or git commit.",
        "Do not read default-forbidden data directories.",
    ]
    if role_id in {"explorer", "reviewer", "adversarial_reviewer", "validator"}:
        common.insert(0, "Do not modify files.")
    if role_id == "implementer":
        common.extend(
            [
                "Modify only TaskSpec write_allowed / allowed_paths.",
                "Stop before touching untracked business scripts; require a baseline commit decision.",
                "Do not connect to board, HDC, SSH, server, SCP, or app-server.",
                "Do not run real collection.",
                "Do not broaden scope or change files outside the declared write scope.",
                "Do not broaden scope or change public semantics unexpectedly.",
            ]
        )
    if role_id == "reviewer":
        common.extend(
            [
                "Review only; inspect diffs, paths, interfaces, consumers, PowerShell quoting, BusyBox/Toybox compatibility, GT/OBS, L1/L2, and subtype boundaries.",
            ]
        )
    workflow = task.get("workflow")
    if workflow == "explorer-reviewer":
        common.extend(
            [
                "This is a read-only explorer -> reviewer workflow.",
                "Do not call or emulate an implementer.",
                "Do not call or emulate a repairer.",
            ]
        )
        if role_id == "reviewer":
            common.extend(
                [
                    "Review the explorer output as the primary input.",
                    "Do not modify files.",
                    "Do not call implementer or repairer.",
                ]
            )
    if workflow == "explorer-implementer-reviewer":
        if role_id == "explorer":
            common.extend(
                [
                    "This explorer step is read-only.",
                    "Find scope and candidate change points for the implementer.",
                    "Output exploration result for implementer handoff.",
                ]
            )
        if role_id == "implementer":
            common.extend(
                [
                    "Use the explorer output as context, but modify only TaskSpec allowed_paths / write_allowed paths.",
                    "Do not expand scope.",
                    "Stop on untracked business scripts unless a baseline commit has already been made by the human operator.",
                ]
            )
        if role_id == "reviewer":
            common.extend(
                [
                    "Review the implementer diff/result as the primary input.",
                    "Do not modify files.",
                    "Do not call repairer.",
                    "Check path scope, diff, validation, PowerShell quoting, BusyBox/Toybox constraints, GT/OBS, L1/L2, and subtype boundary when relevant.",
                ]
            )
    if role_id == "adversarial_reviewer":
        common.extend(
            [
                "Use adversarial review mode on project-reviewer.",
                "Focus on semantic boundaries, recovery gates, trigger/lock/cooldown/stage flow, and safety gates.",
            ]
        )
    if role_id == "repairer":
        depth = task.get("max_repair_depth") or "roles.yaml limit"
        absolute = task.get("absolute_max_repair_depth") or "roles.yaml absolute limit"
        common.extend(
            [
                "Do not enter repair unless reviewer/validator gave a concrete blocking issue.",
                f"Do not exceed max repair depth {depth}; absolute maximum {absolute}.",
                "Modify only files inside the original TaskSpec write scope.",
            ]
        )
    return common


def policy_line(task: dict[str, Any]) -> str:
    external_allowed = any(
        bool(task.get(key))
        for key in (
            "allow_hdc",
            "allow_ssh",
            "allow_board_connection",
            "allow_server_connection",
            "allow_app_server",
            "allow_real_collection",
        )
    )
    if external_allowed:
        return "External access requires explicit TaskSpec authorization plus human gate; this dry-run prompt still does not execute it."
    return "Board/HDC/server/SSH/SCP/app-server access is forbidden by default for this bundle."


def format_list(items: list[str], empty: str = "(none)") -> str:
    if not items:
        return f"- {empty}"
    return "\n".join(f"- {item}" for item in items)


def build_prompt(role_id: str, role: dict[str, Any], task: dict[str, Any]) -> str:
    backend = role.get("backend_agent", BACKEND_FALLBACKS.get(role_id))
    if role_id == "adversarial_reviewer":
        backend = "project-reviewer with review_mode=adversarial"

    purpose = role.get("purpose") or f"Harness role {role_id}"
    write_permission = role.get("write_permission") or (
        "none" if role_id in {"explorer", "reviewer", "adversarial_reviewer"} else "scoped"
    )
    stop_conditions = role.get("stop_conditions")
    if not isinstance(stop_conditions, list) or not stop_conditions:
        stop_conditions = [
            "Requested action exceeds TaskSpec scope.",
            "Requested action requires forbidden external access.",
            "Requested action would enable real spawn, concurrency, auto review, or auto repair.",
        ]

    body = [
        f"### Prompt Bundle: {role_id}",
        "",
        f"- Backend agent: {backend if backend is not None else 'null / manual validator role'}",
        f"- Role id: {role_id}",
        f"- Role purpose: {purpose}",
        f"- Write permission: {write_permission}",
        "",
        "#### Task Summary",
        f"- TaskSpec: {task['path']}",
        f"- Task ID: {task['id']}",
        f"- Title: {task['title']}",
        f"- Task type: {task['task_type']}",
        f"- Risk level: {task['risk_level']}",
        f"- Workflow: {task.get('workflow', 'auto')}",
        "",
        "#### Workflow Policy",
        format_list(workflow_policy_lines(role_id, task)),
        "",
        "#### Allowed Paths",
        format_list(task["allowed_paths"]),
        "",
        "#### Write Scope",
        format_list(task["write_allowed"], "(no write scope declared)"),
        "",
        "#### Read-Only Paths",
        format_list(task["read_only"]),
        "",
        "#### Forbidden Paths",
        format_list(task["forbidden_paths"]),
        "",
        "#### Board / Server / HDC Policy",
        f"- {policy_line(task)}",
        "- Real collection is forbidden by default.",
        "",
        "#### Validation Policy",
        "- Do not execute validation commands unless this role is explicitly authorized by the human operator.",
        "- In this dispatcher dry-run, validation commands are prompt text only.",
        format_list(task["validation_commands"], "(no validation commands declared)"),
        "",
        "#### Git Policy",
        "- Do not run git add.",
        "- Do not run git commit.",
        "- Do not run git reset --hard or git clean.",
        "",
        "#### Output Contract",
        format_list(role_output_contract(role)),
        "",
        "#### Stop Conditions",
        format_list(stop_conditions),
        "",
        "#### Explicit Non-Actions",
        format_list(role_specific_non_actions(role_id, task)),
    ]
    return "\n".join(body).rstrip() + "\n"


def workflow_policy_lines(role_id: str, task: dict[str, Any]) -> list[str]:
    workflow = task.get("workflow")
    if workflow == "auto":
        return ["Workflow auto: use selected role routing only; do not enable scheduler or real spawn."]

    if workflow == "explorer-reviewer":
        lines = [
            "Workflow explorer-reviewer is read-only.",
            "Planned roles are explorer -> reviewer.",
            "No implementer is planned or allowed.",
            "No repairer is planned or allowed.",
            "No file modification is allowed.",
            "No git add or git commit is allowed.",
            "No board/server/HDC/SSH/app-server access is allowed.",
            "No real collection is allowed.",
            "No scheduler is allowed.",
            "No parallel fan-out is allowed.",
            "No auto review gate is allowed.",
            "No automatic repair loop is allowed.",
        ]
        if role_id == "reviewer":
            lines.extend(
                [
                    "Reviewer reviews explorer output.",
                    "Reviewer may not modify files.",
                    "Reviewer may not call implementer or repairer.",
                ]
            )
        return lines

    if workflow == "explorer-implementer-reviewer":
        lines = [
            "Workflow explorer-implementer-reviewer is serial and dry-run planned.",
            "Planned roles are explorer -> implementer -> reviewer.",
            "Real spawn adapter is inactive.",
            "No scheduler is allowed.",
            "No parallel fan-out is allowed.",
            "No auto review gate is allowed.",
            "No automatic repair loop is allowed.",
            "No automatic git add or git commit is allowed.",
            "Board/server/HDC/SSH/app-server access is forbidden unless TaskSpec human gate explicitly allows it.",
            "No real collection is allowed by default.",
        ]
        if role_id == "explorer":
            lines.extend(
                [
                    "Explorer is read-only.",
                    "Explorer may not modify files.",
                    "Explorer finds scope and candidate change points.",
                    "Explorer outputs exploration result for implementer.",
                ]
            )
        if role_id == "implementer":
            lines.extend(
                [
                    "Implementer may modify only TaskSpec allowed_paths / write_allowed paths.",
                    "Implementer may not run git add.",
                    "Implementer may not run git commit.",
                    "Implementer may not connect to board/server/HDC/SSH/app-server.",
                    "Implementer may not run real collection.",
                    "Implementer may not expand scope.",
                    "Implementer must stop on untracked business script unless baseline committed.",
                ]
            )
        if role_id == "reviewer":
            lines.extend(
                [
                    "Reviewer is read-only.",
                    "Reviewer reviews implementer diff/result.",
                    "Reviewer may not modify files.",
                    "Reviewer may not call repairer.",
                    "Reviewer checks path scope, diff, validation, PowerShell quoting, BusyBox/Toybox constraints, GT/OBS, L1/L2, and subtype boundary when relevant.",
                ]
            )
        return lines

    return [
        "Workflow is unknown to prompt policy; stop rather than enabling scheduler or real spawn."
    ]


def build_bundle(
    task: dict[str, Any],
    roles: dict[str, dict[str, Any]],
    sequence: list[str],
    warnings: list[str],
    mode: str,
) -> str:
    now = _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()
    parts = [
        "# Codex Subagent Dispatcher v0 Prompt Bundle",
        "",
        f"- Generated at: {now}",
        f"- Mode: {mode}",
        f"- Workflow: {task.get('workflow', 'auto')}",
        "- Dry run: true",
        "- Real Codex spawn adapter is not active.",
        "- No agents were called.",
        "- No validation commands were executed.",
        "- No board/server/HDC/SSH/app-server connection was attempted.",
        "",
        "## Warnings",
        format_list(warnings, "(none)"),
        "",
        "## Selected Role Sequence",
        format_list(sequence),
        "",
        "## Backend Mapping",
    ]

    for role_id in sequence:
        role = roles.get(role_id, {"role_id": role_id})
        backend = role.get("backend_agent", BACKEND_FALLBACKS.get(role_id))
        if role_id == "adversarial_reviewer":
            backend = "project-reviewer with review_mode=adversarial"
        parts.append(f"- {role_id} -> {backend if backend is not None else 'null'}")

    parts.extend(["", "## Per-Agent Prompts", ""])
    for role_id in sequence:
        parts.append(build_prompt(role_id, roles.get(role_id, {}), task))
    return "\n".join(parts).rstrip() + "\n"


def write_bundle(out_dir: Path, bundle: str) -> Path:
    resolved = out_dir.resolve()
    try:
        rel = resolved.relative_to(ROOT).as_posix()
    except ValueError as exc:
        raise DispatchError(f"Refusing to write outside repo: {out_dir}") from exc
    rel_lower = rel.lower().replace("\\", "/")
    if rel_lower.startswith(FORBIDDEN_READ_PREFIXES):
        raise DispatchError(f"Refusing to write default-forbidden path: {rel}")
    resolved.mkdir(parents=True, exist_ok=True)
    output_path = resolved / "prompt_bundle.md"
    output_path.write_text(bundle, encoding="utf-8")
    return output_path


def run_real_spawn_gate() -> int:
    """Gate real-spawn mode without spawning agents or running model prompts."""
    try:
        from probe_codex_spawn_capability import PROBE_COMMANDS, build_report, run_command
    except ImportError as exc:
        print("ERROR: real spawn capability detector is not importable.", file=sys.stderr)
        print(f"detail: {exc}", file=sys.stderr)
        return 2

    command_results: dict[str, dict[str, Any]] = {}
    for key, command, _required in PROBE_COMMANDS:
        command_results[key] = run_command(command)

    report = build_report(command_results, allow_generic_note=False)
    status = report.get("real_spawn_adapter_status")
    supported = bool(report.get("project_agent_spawn_supported"))

    print("# Codex Subagent Dispatcher v0 Real-Spawn Gate")
    print()
    print(f"real_spawn_adapter_status: {status}")
    print(f"project_agent_spawn_supported: {supported}")
    print(f"explicit_agent_selection_available: {report.get('explicit_agent_selection_available')}")
    print(f"agents_command_available: {report.get('agents_command_available')}")
    print(f"generic_codex_exec_supported: {report.get('generic_codex_exec_supported')}")
    print()
    print("Safety:")
    print("- no Codex agent was called")
    print("- no codex exec prompt was run")
    print("- no scheduler was started")
    print("- no parallel fan-out was used")
    print("- no auto review gate was used")
    print()

    if not supported or status != "READY":
        print("RESULT: FAIL_FAST")
        print("Reason: real spawn adapter NOT_READY.")
        print("generic codex exec is not equivalent to project agent spawn.")
        print("Use --mode prompt-bundle instead.")
        return 2

    print("RESULT: NOT_IMPLEMENTED")
    print(
        "real spawn capability detected, but real spawn adapter implementation is not enabled in P2-12."
    )
    print("Use --mode prompt-bundle instead.")
    return 3


def run_self_check_modes() -> int:
    """Check dispatcher modes without writing bundles or spawning agents."""
    print("# Codex Subagent Dispatcher v0 Mode Self-Check")
    print()
    print("- prompt-bundle is checked in memory only")
    print("- real-spawn gate is checked without Codex exec prompt execution")
    print("- no result files are generated")
    print("- no agents are called")
    print("- no scheduler, parallel fan-out, or auto review gate is used")
    print()

    failures = 0
    warnings = [
        "Using conservative standard-library text parsing; PyYAML is not required or used.",
        "real Codex spawn adapter is not active.",
    ]

    try:
        task_text = read_text_file(REHEARSAL_TASK_SPEC)
        roles_text = read_text_file(ROLES_PATH)
        task = parse_task_spec(REHEARSAL_TASK_SPEC, task_text)
        task["workflow"] = "explorer-reviewer"
        roles = parse_roles(roles_text)
        sequence = select_roles(task, None, None, "explorer-reviewer")
        bundle = build_bundle(task, roles, sequence, warnings, "prompt-bundle")
        prompt_checks = {
            "planned roles explorer -> reviewer": sequence == ["explorer", "reviewer"],
            "backend project-explorer": "- explorer -> project-explorer" in bundle,
            "backend project-reviewer": "- reviewer -> project-reviewer" in bundle,
            "no agents called declaration": "- No agents were called." in bundle,
            "real spawn inactive declaration": "- Real Codex spawn adapter is not active." in bundle,
        }
        prompt_ok = all(prompt_checks.values())
    except DispatchError as exc:
        prompt_checks = {f"dispatch error: {exc}": False}
        prompt_ok = False

    print(f"{'PASS' if prompt_ok else 'FAIL'}: prompt-bundle-mode")
    for label, ok in prompt_checks.items():
        print(f"  {'ok' if ok else 'missing'}: {label}")
    print("  ok: no result files generated")
    print("  ok: no spawn attempted")
    if not prompt_ok:
        failures += 1
    print()

    gate_output_buffer = io.StringIO()
    with contextlib.redirect_stdout(gate_output_buffer):
        gate_rc = run_real_spawn_gate()
    gate_output = gate_output_buffer.getvalue()
    gate_checks = {
        "gate returned non-zero": gate_rc != 0,
        "NOT_READY reported": "real_spawn_adapter_status: NOT_READY" in gate_output,
        "generic exec warning reported": "generic codex exec is not equivalent to project agent spawn"
        in gate_output,
        "no codex exec prompt declaration": "- no codex exec prompt was run" in gate_output,
        "no agent call declaration": "- no Codex agent was called" in gate_output,
    }
    gate_ok = all(gate_checks.values())

    print(f"{'PASS' if gate_ok else 'FAIL'}: real-spawn-gate-not-ready")
    print(f"  returncode: {gate_rc}")
    for label, ok in gate_checks.items():
        print(f"  {'ok' if ok else 'missing'}: {label}")
    if not gate_ok:
        failures += 1
    print()

    if failures:
        print("RESULT: FAIL")
        return 2
    print("RESULT: PASS")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a dry-run per-role Codex prompt bundle from a TaskSpec."
    )
    parser.add_argument(
        "--task-spec",
        help="Path to TaskSpec YAML file. Required unless --self-check-routing is used.",
    )
    parser.add_argument(
        "--self-check-routing",
        action="store_true",
        help="Run built-in synthetic routing matrix checks and exit.",
    )
    parser.add_argument(
        "--self-check-modes",
        action="store_true",
        help="Run dispatcher mode regression checks and exit.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Generate prompts only. This is always true in dispatcher v0.",
    )
    parser.add_argument(
        "--out-dir",
        help="Optional directory for prompt_bundle.md. Default writes nothing.",
    )
    parser.add_argument(
        "--mode",
        default="prompt-bundle",
        choices=["prompt-bundle", "real-spawn"],
        help=(
            "Dispatcher mode. prompt-bundle generates a manual prompt bundle. "
            "real-spawn runs the capability gate and is currently disabled until "
            "a future real spawn adapter is implemented."
        ),
    )
    parser.add_argument(
        "--risk-level",
        choices=["low", "medium", "high"],
        help="Override TaskSpec risk_level for routing only.",
    )
    parser.add_argument(
        "--task-type",
        help="Override TaskSpec task_type for routing only, e.g. review-only, validation-only, repair.",
    )
    parser.add_argument(
        "--workflow",
        default="auto",
        choices=sorted(SUPPORTED_WORKFLOWS),
        help="Dry-run workflow override. auto keeps existing routing; explorer-reviewer forces explorer -> reviewer; explorer-implementer-reviewer forces explorer -> implementer -> reviewer.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    warnings = [
        "Using conservative standard-library text parsing; PyYAML is not required or used.",
        "real Codex spawn adapter is not active.",
    ]

    try:
        if args.self_check_routing:
            return run_self_check_routing()
        if args.self_check_modes:
            return run_self_check_modes()
        if not args.task_spec:
            raise DispatchError(
                "--task-spec is required unless --self-check-routing or --self-check-modes is used."
            )
        if args.mode == "real-spawn":
            return run_real_spawn_gate()

        task_path = (ROOT / args.task_spec).resolve()
        task_text = read_text_file(task_path)
        roles_text = read_text_file(ROLES_PATH)

        task = parse_task_spec(task_path, task_text)
        if args.risk_level:
            task["risk_level"] = args.risk_level
        if args.task_type:
            task["task_type"] = args.task_type
        task["workflow"] = args.workflow

        roles = parse_roles(roles_text)
        sequence = select_roles(task, args.risk_level, args.task_type, args.workflow)
        bundle = build_bundle(task, roles, sequence, warnings, args.mode)

        if args.out_dir:
            output_path = write_bundle(ROOT / args.out_dir, bundle)
            print(f"[DISPATCHER_DRY_RUN] wrote prompt bundle: {repo_rel(output_path)}")
            print()

        print(bundle, end="")
        return 0
    except DispatchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
