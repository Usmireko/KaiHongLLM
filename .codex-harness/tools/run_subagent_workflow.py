#!/usr/bin/env python3
"""Semi-automatic dry-run runner for Codex subagent workflows.

The runner chains the current safe pieces of the harness:
1. probe Codex spawn capability,
2. fall back to prompt-bundle when real spawn is unavailable,
3. generate or reuse prompt_bundle.md,
4. optionally aggregate manually saved result bundles.

It never calls `codex exec <prompt>`, never spawns agents, and never executes
TaskSpec validation commands.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
HARNESS_ROOT = ROOT / ".codex-harness"
REPORTS_ROOT = HARNESS_ROOT / "reports"
WORKFLOWS_ROOT = HARNESS_ROOT / "workflows"
BACKEND_POLICY_PATH = HARNESS_ROOT / "backend_policy.json"

PROBE_SCRIPT = HARNESS_ROOT / "tools" / "probe_codex_spawn_capability.py"
DISPATCH_SCRIPT = HARNESS_ROOT / "tools" / "dispatch_subagents.py"
AGGREGATE_SCRIPT = HARNESS_ROOT / "tools" / "aggregate_subagent_results.py"
ORCHESTRATOR_SCRIPT = HARNESS_ROOT / "tools" / "subagent_orchestrator.py"
NATIVE_ADAPTER_SCRIPT = HARNESS_ROOT / "tools" / "codex_native_subagent_adapter.py"
APP_SERVER_SHADOW_ADAPTER_SCRIPT = HARNESS_ROOT / "tools" / "codex_app_server_shadow_adapter.py"
SENSITIVE_TASKSPEC_VALUE_REDACTION = "<OWNER_PROVIDED_WIFI_PSK_REDACTED>"

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

AGENT_RESULT_FILES = {
    "project-explorer": "01_project_explorer_result.md",
    "project-implementer": "02_project_implementer_result.md",
    "project-reviewer": "03_project_reviewer_result.md",
    "project-repairer": "04_project_repairer_result.md",
}

SUPPORTED_WORKFLOWS = ("auto", "explorer-reviewer", "explorer-implementer-reviewer")
SUPPORTED_WORKFLOW_PATTERNS = (
    "explorer-implementer-reviewer",
    "producer-reviewer",
    "planner-generator-evaluator",
    "fan-out-fan-in",
)
SUPPORTED_MODES = (
    "auto",
    "prompt-bundle",
    "real-spawn",
    "vscode-plugin",
    "vscode-plugin-assisted",
    "codex-native-subagent",
)
SUPPORTED_CAPTURE_BACKENDS = ("jsonl", "app-server-candidate", "dual")
NATIVE_PLACEHOLDER_PREFIX = "REPLACE_WITH_"
REQUIRED_BACKEND_POLICY_LINK_MODES = {
    "collab_agent_spawn_end",
    "function_call_output_agent_id",
    "app_server_collab_agent_tool_call",
    "app_server_function_call_output_agent_id",
}
EQUIVALENT_CLASSIFICATIONS = {
    "CANDIDATE_EQUIVALENT",
    "V10_CANDIDATE_EQUIVALENT",
    "SHADOW_EQUIVALENT",
}


class RunnerError(Exception):
    """User-facing runner error."""


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
        raise RunnerError(f"Refusing to {purpose} outside repo: {path}") from exc

    rel_lower = rel.lower().replace("\\", "/")
    if rel_lower.startswith(DEFAULT_FORBIDDEN_PREFIXES):
        raise RunnerError(f"Refusing to {purpose} forbidden path: {rel}")
    return resolved


def ensure_reports_run_dir(path: Path) -> Path:
    resolved = ensure_repo_path(path, "use run-dir")
    try:
        resolved.relative_to(REPORTS_ROOT.resolve())
    except ValueError as exc:
        raise RunnerError(
            f"run-dir must be under .codex-harness/reports/: {repo_rel(resolved)}"
        ) from exc
    return resolved


def run_tool(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def read_json_file(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise RunnerError(f"{label} not found: {repo_rel(path)}") from exc
    except json.JSONDecodeError as exc:
        raise RunnerError(f"{label} is malformed JSON: {repo_rel(path)}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RunnerError(f"{label} must be a JSON object: {repo_rel(path)}")
    return payload


def backend_policy_path_from_args(args: argparse.Namespace) -> Path:
    raw = getattr(args, "backend_policy_file", None) or str(BACKEND_POLICY_PATH)
    return ensure_repo_path(ROOT / raw, "read backend policy")


def load_backend_policy_for_status(args: argparse.Namespace) -> tuple[dict[str, Any], Path, bool]:
    path = backend_policy_path_from_args(args)
    payload = read_json_if_present(path)
    return payload or {}, path, path.is_file()


def workflow_roles(workflow: str) -> list[str]:
    if workflow in {"auto", "explorer-implementer-reviewer"}:
        return ["project-explorer", "project-implementer", "project-reviewer"]
    if workflow == "explorer-reviewer":
        return ["project-explorer", "project-reviewer"]
    raise RunnerError(f"Unsupported native workflow: {workflow}")


def unique_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def workflow_pattern_path(pattern_name: str) -> Path:
    return WORKFLOWS_ROOT / f"{pattern_name}.json"


def role_identity_keys(role: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    for key in ("id", "pattern_role", "role", "agent_type"):
        value = role.get(key)
        if isinstance(value, str) and value:
            keys.append(value)
    result_file = role.get("result_file")
    if isinstance(result_file, str) and result_file:
        keys.append(Path(result_file).stem)
    aliases = role.get("aliases")
    if isinstance(aliases, list):
        keys.extend(alias for alias in aliases if isinstance(alias, str) and alias)
    return unique_strings(keys)


def validate_dependency_list(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise RunnerError(f"{label} must be a list")
    bad = [item for item in value if not isinstance(item, str) or not item]
    if bad:
        raise RunnerError(f"{label} contains a non-string dependency")
    return list(value)


def validate_workflow_pattern(pattern: dict[str, Any], source: str) -> None:
    pattern_name = pattern.get("pattern_name")
    if pattern_name not in SUPPORTED_WORKFLOW_PATTERNS:
        raise RunnerError(f"Unsupported workflow pattern name in {source}: {pattern_name}")
    roles = pattern.get("roles")
    if not isinstance(roles, list) or not roles:
        raise RunnerError(f"Workflow pattern has no roles: {source}")

    role_ids: set[str] = set()
    key_owner: dict[str, str] = {}
    for index, role in enumerate(roles, 1):
        if not isinstance(role, dict):
            raise RunnerError(f"Workflow pattern role {index} is not an object: {source}")
        for key in ("id", "agent_type", "result_file"):
            if not isinstance(role.get(key), str) or not role.get(key):
                raise RunnerError(f"Workflow pattern role {index} missing {key}: {source}")
        role_id = role["id"]
        if role_id in role_ids:
            raise RunnerError(f"Workflow pattern has duplicate role id: {role_id}")
        role_ids.add(role_id)
        validate_dependency_list(role.get("depends_on"), f"role {role_id}.depends_on")
        aliases = role.get("aliases", [])
        if aliases is not None and not isinstance(aliases, list):
            raise RunnerError(f"role {role_id}.aliases must be a list")
        for key in role_identity_keys(role):
            owner = key_owner.get(key)
            if owner is not None and owner != role_id:
                raise RunnerError(f"Workflow pattern role alias/key is duplicated: {key}")
            key_owner[key] = role_id

    gate_role = pattern.get("gate_role")
    if not isinstance(gate_role, str) or not gate_role:
        raise RunnerError(f"Workflow pattern missing gate_role: {source}")
    if gate_role not in key_owner:
        raise RunnerError(f"Workflow pattern gate_role is not a known role: {gate_role}")

    graph = pattern.get("dependency_graph")
    if not isinstance(graph, dict):
        raise RunnerError(f"Workflow pattern dependency_graph must be an object: {source}")
    for node, deps in graph.items():
        if not isinstance(node, str) or node not in key_owner:
            raise RunnerError(f"Workflow pattern dependency_graph has unknown node: {node}")
        for dep in validate_dependency_list(deps, f"dependency_graph.{node}"):
            if dep not in key_owner:
                raise RunnerError(f"Workflow pattern dependency_graph has unknown dependency: {dep}")
    for role in roles:
        role_id = role["id"]
        for dep in validate_dependency_list(role.get("depends_on"), f"role {role_id}.depends_on"):
            if dep not in key_owner:
                raise RunnerError(f"Workflow pattern role {role_id} has unknown dependency: {dep}")


def load_supported_workflow_pattern(pattern_name: str) -> dict[str, Any]:
    if pattern_name not in SUPPORTED_WORKFLOW_PATTERNS:
        raise RunnerError(f"Unsupported workflow pattern name: {pattern_name}")
    path = workflow_pattern_path(pattern_name)
    if not path.is_file():
        raise RunnerError(f"Workflow pattern not found: {repo_rel(path)}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise RunnerError(f"Workflow pattern JSON is malformed: {repo_rel(path)}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RunnerError(f"Workflow pattern must be a JSON object: {repo_rel(path)}")
    validate_workflow_pattern(payload, repo_rel(path))
    return payload


def legacy_workflow_pattern(workflow: str) -> dict[str, Any]:
    roles = workflow_roles(workflow)
    return {
        "pattern_name": workflow if workflow != "auto" else "explorer-implementer-reviewer",
        "role_flow": " -> ".join(roles),
        "gate_role": roles[-1],
        "aggregation_pass_rule": "all required roles captured and gate role has top-level RESULT: PASS",
        "roles": [
            {
                "id": role,
                "agent_type": role,
                "required": True,
                "result_file": AGENT_RESULT_FILES[role],
                "depends_on": [] if index == 0 else [roles[index - 1]],
                "prompt_template": f"Perform the {role} responsibility for this harness source workflow.",
            }
            for index, role in enumerate(roles)
        ],
    }


def load_workflow_pattern(pattern_name: str | None, workflow: str) -> dict[str, Any]:
    if not pattern_name:
        return legacy_workflow_pattern(workflow)
    return load_supported_workflow_pattern(pattern_name)


def pattern_roles(pattern: dict[str, Any]) -> list[dict[str, Any]]:
    roles = pattern.get("roles")
    return roles if isinstance(roles, list) else []


def native_expected_workflow_template(
    run_dir: Path,
    pattern: dict[str, Any],
    task_spec: Path | None,
) -> dict[str, Any]:
    roles = pattern_roles(pattern)
    return {
        "schema_version": "codex-harness.expected_workflow.v1",
        "run_id": run_dir.name,
        "workflow_pattern": pattern.get("pattern_name"),
        "role_flow": pattern.get("role_flow"),
        "dependency_graph": pattern.get("dependency_graph") or {},
        "gate_role": pattern.get("gate_role"),
        "aggregation_pass_rule": pattern.get("aggregation_pass_rule"),
        "parent_thread_id": "REPLACE_WITH_PARENT_THREAD_ID",
        "log_root": "REPLACE_WITH_CODEX_SESSIONS_ROOT",
        "cwd": str(ROOT),
        "task_spec": repo_rel(task_spec) if task_spec else None,
        "expected_roles": [
            {
                "order": index,
                "pattern_role": role["id"],
                "agent_type": role["agent_type"],
                "call_id": f"{NATIVE_PLACEHOLDER_PREFIX}{role['id'].replace('-', '_').upper()}_CALL_ID",
                "required": bool(role.get("required", True)),
                "result_file": role["result_file"],
                "depends_on": role.get("depends_on") or [],
                "aliases": role.get("aliases") or [],
            }
            for index, role in enumerate(roles, 1)
        ],
        "privacy": {
            "redact_prompts": True,
            "store_prompt_hash": True,
            "include_prompts": False,
        },
        "match": {
            "require_same_parent_thread": True,
            "require_child_parent_thread_match": True,
        },
    }


def redact_task_spec_snapshot(text: str) -> str:
    """Redact credential-like scalar values before embedding TaskSpec text."""

    key_pattern = r"(psk|password|passphrase|secret|credential)"
    text = re.sub(
        rf"(^\s*[-*]?\s*(?:[A-Za-z0-9_.-]*{key_pattern}[A-Za-z0-9_.-]*)\s*:\s*)(.+)$",
        lambda m: m.group(1) + SENSITIVE_TASKSPEC_VALUE_REDACTION,
        text,
        flags=re.MULTILINE | re.IGNORECASE,
    )
    text = re.sub(
        rf"(\b(?:[A-Za-z0-9_.-]*{key_pattern}[A-Za-z0-9_.-]*)\b\s*=\s*)([^\s,;]+)",
        lambda m: m.group(1) + SENSITIVE_TASKSPEC_VALUE_REDACTION,
        text,
        flags=re.IGNORECASE,
    )
    return text


def render_native_prompt_bundle(pattern: dict[str, Any], task_spec: Path | None, run_dir: Path) -> str:
    roles = pattern_roles(pattern)
    task_spec_excerpt = ""
    if task_spec is not None:
        task_spec_text = task_spec.read_text(encoding="utf-8", errors="replace")
        task_spec_text = redact_task_spec_snapshot(task_spec_text)
        if len(task_spec_text) > 12000:
            task_spec_excerpt = task_spec_text[:12000].rstrip() + "\n\n... [TaskSpec excerpt truncated]\n"
        else:
            task_spec_excerpt = task_spec_text.rstrip() + "\n"
    lines = [
        "# Codex Native Subagent Prompt Bundle",
        "",
        f"run_dir: `{repo_rel(run_dir)}`",
        f"workflow_pattern: `{pattern.get('pattern_name')}`",
        f"role_flow: `{pattern.get('role_flow') or '(see dependency graph)'}`",
        f"gate_role: `{pattern.get('gate_role')}`",
        f"task_spec: `{repo_rel(task_spec) if task_spec else '(none)'}`",
        "",
        "## NEXT_ACTION_FOR_PARENT_CODEX",
        "",
        "Parent Codex must launch native subagents with the `spawn_agent` tool.",
        "The Python harness does not and cannot directly call native `spawn_agent`.",
        "The Python runner cannot directly spawn native subagents.",
        "Do not use `codex exec` for subagents.",
        "",
        "After each native spawn returns, record its parent `call_id`. When all",
        "children complete, run `--finalize-manifest` with `--call-id` or",
        "`--call-id-map` to write `expected_workflow.json`, or fill the manifest",
        "manually with the same concrete parent/log/call-id values.",
        "",
    ]
    if task_spec is not None:
        lines.extend(
            [
                "## TaskSpec Snapshot",
                "",
                "The following task-specific constraints are part of the subagent contract.",
                "",
                "```yaml",
                task_spec_excerpt.rstrip(),
                "```",
                "",
            ]
        )
    lines.extend([
        "## Backend Mapping",
        "",
    ])
    for index, role in enumerate(roles, 1):
        dependency_text = ", ".join(role.get("depends_on") or []) or "none"
        lines.append(
            f"- {index}. `{role['id']}` -> native `spawn_agent(agent_type=\"{role['agent_type']}\")`; "
            f"depends_on: {dependency_text}; result_file: `{role['result_file']}`"
        )
    lines.extend(["", "## Role Prompts", ""])
    for index, role in enumerate(roles, 1):
        lines.extend(
            [
                f"### {index}. {role['id']}",
                "",
                f"Launch this role with parent Codex native `spawn_agent` using `agent_type=\"{role['agent_type']}\"`.",
                "",
                "Required constraints:",
                "- Do not spawn subagents from Python.",
                "- Do not use `codex exec` for subagents.",
                "- Do not modify business files.",
                "- Do not claim `.codex/agents/*.toml` role resolution is confirmed.",
                "- Use top-level `RESULT: PASS`, `RESULT: NEEDS_FIX`, `RESULT: FAIL`, or `RESULT: CAPTURE_FAILED`.",
                "- Put non-blocking issues under `Non-Blocking Warnings`.",
                "",
                "Task:",
                f"- Pattern role: `{role['id']}`.",
                f"- {role.get('prompt_template') or 'Perform the assigned pattern responsibility.'}",
                "- If no blocking issue is found, output top-level `RESULT: PASS`.",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def render_next_action_for_parent_codex(run_dir: Path, pattern: dict[str, Any]) -> str:
    roles = pattern_roles(pattern)
    role_lines = "\n".join(
        f"- launch pattern role `{role['id']}` with parent Codex native "
        f"`spawn_agent(agent_type=\"{role['agent_type']}\")`"
        for role in roles
    )
    return (
        "# NEXT_ACTION_FOR_PARENT_CODEX\n\n"
        "Python has prepared this run only. It did not call `spawn_agent`, did not use "
        "`codex exec`, and did not run capture or aggregation.\n\n"
        "Parent Codex native runtime must launch subagents. The Python runner cannot "
        "directly spawn native subagents.\n\n"
        "Parent Codex must now:\n\n"
        f"{role_lines}\n"
        "- wait for each child to complete\n"
        "- inspect the parent log for the concrete `spawn_agent` call IDs\n"
        "- run `--finalize-manifest` with `--call-id` or `--call-id-map`, or fill concrete call_ids manually\n"
        "- ensure `expected_workflow.json` has concrete `parent_thread_id`, `log_root`, and `expected_roles[].call_id`\n"
        "- run runner `--capture-only`\n"
        "- run runner `--aggregate-only`\n\n"
        f"Run directory: `{repo_rel(run_dir)}`\n"
    )


def write_text_if_needed(path: Path, text: str, force: bool) -> str:
    if path.exists() and not force:
        return "reused-existing"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return "wrote"


def write_json_if_needed(path: Path, payload: dict[str, Any], force: bool) -> str:
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    return write_text_if_needed(path, text, force)


def run_probe() -> dict[str, Any]:
    completed = run_tool([sys.executable, str(PROBE_SCRIPT), "--json"])
    if completed.returncode != 0:
        raise RunnerError(
            "Capability probe failed with exit code "
            f"{completed.returncode}: {completed.stderr.strip()}"
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RunnerError("Capability probe did not return valid JSON.") from exc


def run_dispatch_prompt_bundle(
    task_spec: Path,
    workflow: str,
    run_dir: Path,
    force: bool,
) -> tuple[Path, str, bool]:
    prompt_path = run_dir / "prompt_bundle.md"
    if prompt_path.exists() and not force:
        return prompt_path, prompt_path.read_text(encoding="utf-8", errors="replace"), False

    run_dir.mkdir(parents=True, exist_ok=True)
    args = [
        sys.executable,
        str(DISPATCH_SCRIPT),
        "--task-spec",
        repo_rel(task_spec),
        "--dry-run",
        "--mode",
        "prompt-bundle",
        "--workflow",
        workflow,
        "--out-dir",
        repo_rel(run_dir),
    ]
    completed = run_tool(args)
    if completed.returncode != 0:
        raise RunnerError(
            "Prompt-bundle dispatch failed with exit code "
            f"{completed.returncode}: {completed.stderr.strip()}"
        )
    if not prompt_path.exists():
        raise RunnerError("Prompt-bundle dispatch completed but prompt_bundle.md was not written.")
    return prompt_path, prompt_path.read_text(encoding="utf-8", errors="replace"), True


def run_dispatch_real_spawn_gate(task_spec: Path, workflow: str) -> subprocess.CompletedProcess[str]:
    return run_tool(
        [
            sys.executable,
            str(DISPATCH_SCRIPT),
            "--task-spec",
            repo_rel(task_spec),
            "--mode",
            "real-spawn",
            "--workflow",
            workflow,
        ]
    )


def run_vscode_plugin_orchestrator(
    task_spec: Path,
    workflow: str,
    run_dir: Path,
    assisted: bool,
) -> subprocess.CompletedProcess[str]:
    mode = "vscode-plugin-assisted" if assisted else "vscode-plugin"
    return run_tool(
        [
            sys.executable,
            str(ORCHESTRATOR_SCRIPT),
            "--task-spec",
            repo_rel(task_spec),
            "--workflow",
            workflow if workflow != "auto" else "explorer-reviewer",
            "--run-dir",
            repo_rel(run_dir),
            "--mode",
            mode,
        ]
    )


def parse_planned_agents(prompt_bundle: str) -> list[str]:
    agents: list[str] = []
    in_mapping = False
    for line in prompt_bundle.splitlines():
        if line.strip() == "## Backend Mapping":
            in_mapping = True
            continue
        if in_mapping and line.startswith("## "):
            break
        if not in_mapping:
            continue
        match = re.match(r"-\s+[^-]+->\s+(.+?)\s*$", line.strip())
        if not match:
            continue
        backend = match.group(1).strip()
        for agent in AGENT_RESULT_FILES:
            if backend.startswith(agent):
                agents.append(agent)
                break
    return agents


def existing_result_files(run_dir: Path) -> list[str]:
    return [
        filename
        for filename in AGENT_RESULT_FILES.values()
        if (run_dir / filename).is_file()
    ]


def expected_result_files(planned_agents: list[str]) -> list[str]:
    return [AGENT_RESULT_FILES[agent] for agent in planned_agents if agent in AGENT_RESULT_FILES]


def run_aggregator(run_dir: Path, strict: bool) -> tuple[int, str, str]:
    args = [
        sys.executable,
        str(AGGREGATE_SCRIPT),
        "--run-dir",
        repo_rel(run_dir),
        "--dry-run",
    ]
    if strict:
        args.append("--strict")
    completed = run_tool(args)
    return completed.returncode, completed.stdout, completed.stderr


def status_from_aggregator_output(output: str, returncode: int) -> str:
    match = re.search(r"overall_status:\s*(\S+)", output)
    if match:
        return match.group(1)
    return "ERROR" if returncode else "UNKNOWN"


def format_list(items: list[str], empty: str = "(none)") -> str:
    if not items:
        return f"- {empty}"
    return "\n".join(f"- {item}" for item in items)


def render_report(report: dict[str, Any]) -> str:
    lines = [
        "# Codex Semi-Automatic Subagent Workflow Runner",
        "",
        f"workflow_status: {report['workflow_status']}",
        f"spawn_capability_status: {report['spawn_capability_status']}",
        f"selected_adapter: {report['selected_adapter']}",
        f"run_dir: {report['run_dir']}",
        f"prompt_bundle_path: {report['prompt_bundle_path']}",
        f"prompt_bundle_action: {report['prompt_bundle_action']}",
        "",
        "planned_agents:",
        format_list(report["planned_agents"]),
        "",
        "expected_result_files:",
        format_list(report["expected_result_files"]),
        "",
        "existing_result_files:",
        format_list(report["existing_result_files"]),
        "",
        f"aggregator_status: {report['aggregator_status']}",
        "",
        "next_manual_steps:",
        format_list(report["next_manual_steps"]),
        "",
        "Safety:",
        "- real spawn adapter inactive unless future implementation exists",
        "- no Codex agent was called",
        "- no generic codex exec prompt was run",
        "- no scheduler, parallel fan-out, or auto review gate was used",
        "- no TaskSpec validation commands were executed",
    ]
    if report.get("real_spawn_gate_output"):
        lines.extend(["", "real_spawn_gate_output:", report["real_spawn_gate_output"].rstrip()])
    if report.get("aggregator_output"):
        lines.extend(["", "aggregator_output:", report["aggregator_output"].rstrip()])
    if report.get("notes"):
        lines.extend(["", "notes:", format_list(report["notes"])])
    return "\n".join(lines).rstrip() + "\n"


def is_placeholder(value: Any) -> bool:
    return not isinstance(value, str) or not value or value.startswith(NATIVE_PLACEHOLDER_PREFIX)


def load_native_expected_workflow(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "expected_workflow.json"
    if not path.is_file():
        raise RunnerError(
            f"expected_workflow.json with concrete call_ids is required in {repo_rel(run_dir)}"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise RunnerError(f"expected_workflow.json is malformed: {exc}") from exc
    if not isinstance(payload, dict):
        raise RunnerError("expected_workflow.json must contain a JSON object")
    roles = payload.get("expected_roles")
    if not isinstance(roles, list) or not roles:
        raise RunnerError("expected_workflow.json must contain non-empty expected_roles")
    bad_roles = [
        str(item.get("agent_type") or item.get("role") or f"role[{index}]")
        for index, item in enumerate(roles, 1)
        if not isinstance(item, dict) or is_placeholder(item.get("call_id"))
    ]
    if bad_roles:
        raise RunnerError(
            "expected_workflow.json has missing or placeholder call_id for: "
            + ", ".join(bad_roles)
        )
    if is_placeholder(payload.get("parent_thread_id")):
        raise RunnerError("expected_workflow.json parent_thread_id is missing or placeholder")
    if is_placeholder(payload.get("log_root")):
        raise RunnerError("expected_workflow.json log_root is missing or placeholder")
    return payload


def load_native_workflow_template(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "expected_workflow.template.json"
    if not path.is_file():
        raise RunnerError(f"expected_workflow.template.json is required in {repo_rel(run_dir)}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise RunnerError(f"expected_workflow.template.json is malformed: {exc}") from exc
    if not isinstance(payload, dict):
        raise RunnerError("expected_workflow.template.json must contain a JSON object")
    return payload


def augment_template_with_pattern_metadata(template: dict[str, Any]) -> dict[str, Any]:
    manifest = json.loads(json.dumps(template))
    pattern_name = manifest.get("workflow_pattern") or manifest.get("pattern_name")
    if not isinstance(pattern_name, str) or not pattern_name:
        raise RunnerError("expected_workflow.template.json is missing workflow_pattern")
    pattern = load_supported_workflow_pattern(pattern_name)
    manifest.setdefault("workflow_pattern", pattern_name)
    manifest.setdefault("role_flow", pattern.get("role_flow"))
    manifest.setdefault("dependency_graph", pattern.get("dependency_graph") or {})
    manifest.setdefault("gate_role", pattern.get("gate_role"))
    manifest.setdefault("aggregation_pass_rule", pattern.get("aggregation_pass_rule"))

    pattern_roles_by_id = {role["id"]: role for role in pattern_roles(pattern)}
    expected_roles = manifest.get("expected_roles")
    if not isinstance(expected_roles, list):
        raise RunnerError("expected_workflow.template.json must contain expected_roles")
    for role in expected_roles:
        if not isinstance(role, dict):
            continue
        pattern_role = role.get("pattern_role") or role.get("role")
        pattern_role_meta = pattern_roles_by_id.get(pattern_role)
        if not pattern_role_meta:
            continue
        role.setdefault("depends_on", pattern_role_meta.get("depends_on") or [])
        role.setdefault("aliases", pattern_role_meta.get("aliases") or [])
    return manifest


def strict_privacy_is_present(manifest: dict[str, Any]) -> bool:
    privacy = manifest.get("privacy")
    return (
        isinstance(privacy, dict)
        and privacy.get("redact_prompts") is True
        and privacy.get("store_prompt_hash") is True
        and privacy.get("include_prompts") is False
    )


def validate_manifest_shape(manifest: dict[str, Any], require_call_ids: bool) -> None:
    pattern_name = manifest.get("workflow_pattern")
    if pattern_name not in SUPPORTED_WORKFLOW_PATTERNS:
        raise RunnerError(f"Unsupported workflow pattern name in manifest: {pattern_name}")
    if not strict_privacy_is_present(manifest):
        raise RunnerError(
            "Manifest privacy settings must be strict: redact_prompts=true, "
            "store_prompt_hash=true, include_prompts=false"
        )
    expected_roles = manifest.get("expected_roles")
    if not isinstance(expected_roles, list) or not expected_roles:
        raise RunnerError("Manifest expected_roles must be a non-empty list")

    known_keys: set[str] = set()
    role_ids: set[str] = set()
    required_role_ids: list[str] = []
    concrete_call_ids: list[str] = []
    for index, role in enumerate(expected_roles, 1):
        if not isinstance(role, dict):
            raise RunnerError(f"Manifest role {index} is not an object")
        pattern_role = role.get("pattern_role") or role.get("role")
        if not isinstance(pattern_role, str) or not pattern_role:
            raise RunnerError(f"Manifest role {index} is missing pattern_role")
        if pattern_role in role_ids:
            raise RunnerError(f"Manifest has duplicate pattern_role: {pattern_role}")
        role_ids.add(pattern_role)
        for key in role_identity_keys(role):
            known_keys.add(key)
        if not isinstance(role.get("result_file"), str) or not role.get("result_file"):
            raise RunnerError(f"Manifest role {pattern_role} is missing result_file")
        validate_dependency_list(role.get("depends_on"), f"manifest role {pattern_role}.depends_on")
        if bool(role.get("required", True)):
            required_role_ids.append(pattern_role)
            if require_call_ids and is_placeholder(role.get("call_id")):
                raise RunnerError(f"Required role lacks concrete call_id: {pattern_role}")
        call_id = role.get("call_id")
        if isinstance(call_id, str) and call_id and not call_id.startswith(NATIVE_PLACEHOLDER_PREFIX):
            concrete_call_ids.append(call_id)

    gate_role = manifest.get("gate_role")
    if not isinstance(gate_role, str) or gate_role not in known_keys:
        raise RunnerError(f"Manifest gate_role is missing or not a known role: {gate_role}")

    dependency_graph = manifest.get("dependency_graph")
    if not isinstance(dependency_graph, dict):
        raise RunnerError("Manifest dependency_graph must be an object")
    for node, deps in dependency_graph.items():
        if not isinstance(node, str) or node not in known_keys:
            raise RunnerError(f"Manifest dependency_graph has unknown node: {node}")
        for dep in validate_dependency_list(deps, f"manifest dependency_graph.{node}"):
            if dep not in known_keys:
                raise RunnerError(f"Manifest dependency_graph has unknown dependency: {dep}")
    for role in expected_roles:
        pattern_role = role.get("pattern_role") or role.get("role")
        for dep in validate_dependency_list(role.get("depends_on"), f"manifest role {pattern_role}.depends_on"):
            if dep not in known_keys:
                raise RunnerError(f"Manifest role {pattern_role} has unknown dependency: {dep}")

    if require_call_ids:
        duplicates = sorted({call_id for call_id in concrete_call_ids if concrete_call_ids.count(call_id) > 1})
        if duplicates:
            raise RunnerError("Duplicate call_id appears in manifest: " + ", ".join(duplicates))
        if not required_role_ids:
            raise RunnerError("Manifest has no required roles")


def parse_call_id_args(values: list[str] | None) -> dict[str, str]:
    mappings: dict[str, str] = {}
    for value in values or []:
        if "=" not in value:
            raise RunnerError(f"--call-id must use role=call_id format: {value}")
        role_key, call_id = value.split("=", 1)
        role_key = role_key.strip()
        call_id = call_id.strip()
        if not role_key or not call_id:
            raise RunnerError(f"--call-id must use non-empty role=call_id values: {value}")
        existing = mappings.get(role_key)
        if existing and existing != call_id:
            raise RunnerError(f"Conflicting --call-id values for role {role_key}")
        mappings[role_key] = call_id
    return mappings


def load_call_id_map(path_text: str | None) -> dict[str, str]:
    if not path_text:
        return {}
    path = Path(path_text)
    if not path.is_absolute():
        path = ROOT / path
    path = ensure_repo_path(path, "read call-id map")
    if not path.is_file():
        raise RunnerError(f"call-id map not found: {repo_rel(path)}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise RunnerError(f"call-id map JSON is malformed: {repo_rel(path)}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RunnerError("call-id map must be a JSON object")
    mappings: dict[str, str] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not key:
            raise RunnerError("call-id map contains a non-string role key")
        if not isinstance(value, str) or not value:
            raise RunnerError(f"call-id map value for {key} must be a non-empty string")
        mappings[key] = value
    return mappings


def merge_call_id_mappings(map_path: str | None, inline_values: list[str] | None) -> dict[str, str]:
    merged = load_call_id_map(map_path)
    inline = parse_call_id_args(inline_values)
    for key, value in inline.items():
        existing = merged.get(key)
        if existing and existing != value:
            raise RunnerError(f"Conflicting call_id mappings for {key}")
        merged[key] = value
    return merged


def apply_call_id_mappings(manifest: dict[str, Any], mappings: dict[str, str]) -> list[str]:
    expected_roles = manifest.get("expected_roles")
    if not isinstance(expected_roles, list):
        raise RunnerError("Manifest expected_roles must be a list")

    key_to_indexes: dict[str, list[int]] = {}
    for index, role in enumerate(expected_roles):
        if not isinstance(role, dict):
            continue
        for key in role_identity_keys(role):
            key_to_indexes.setdefault(key, []).append(index)

    assignments: dict[int, str] = {}
    consumed_keys: list[str] = []
    for role_key, call_id in mappings.items():
        indexes = key_to_indexes.get(role_key)
        if not indexes:
            raise RunnerError(f"call_id mapping references unknown role: {role_key}")
        if len(indexes) > 1:
            raise RunnerError(f"call_id mapping role key is ambiguous: {role_key}")
        role_index = indexes[0]
        existing = assignments.get(role_index)
        if existing and existing != call_id:
            role = expected_roles[role_index]
            role_name = role.get("pattern_role") if isinstance(role, dict) else role_key
            raise RunnerError(f"Conflicting call_id mappings for role {role_name}")
        assignments[role_index] = call_id
        consumed_keys.append(role_key)

    for role_index, call_id in assignments.items():
        role = expected_roles[role_index]
        if isinstance(role, dict):
            role["call_id"] = call_id

    missing = [
        str(role.get("pattern_role") or role.get("role") or f"role[{index}]")
        for index, role in enumerate(expected_roles, 1)
        if isinstance(role, dict)
        and bool(role.get("required", True))
        and is_placeholder(role.get("call_id"))
    ]
    if missing:
        raise RunnerError("Required role lacks call_id: " + ", ".join(missing))
    return consumed_keys


def native_finalize_manifest(args: argparse.Namespace) -> int:
    run_dir = ensure_reports_run_dir(ROOT / args.run_dir)
    manifest_path = run_dir / "expected_workflow.json"
    if manifest_path.exists() and not args.force:
        raise RunnerError(
            f"expected_workflow.json already exists in {repo_rel(run_dir)}; use --force to overwrite"
        )

    manifest = augment_template_with_pattern_metadata(load_native_workflow_template(run_dir))
    if args.parent_thread_id:
        manifest["parent_thread_id"] = args.parent_thread_id
    if args.log_root:
        manifest["log_root"] = args.log_root
    validate_manifest_shape(manifest, require_call_ids=False)

    mappings = merge_call_id_mappings(args.call_id_map, args.call_id)
    if not mappings:
        raise RunnerError("--finalize-manifest requires --call-id or --call-id-map")
    consumed_keys = apply_call_id_mappings(manifest, mappings)

    manifest["manifest_finalized"] = True
    manifest["call_id_manifest"] = {
        "synthetic": bool(args.synthetic_call_ids),
        "mapping_keys": sorted(consumed_keys),
        "source": "call_id_map" if args.call_id_map else "inline_call_id",
    }
    validate_manifest_shape(manifest, require_call_ids=True)

    write_json_if_needed(manifest_path, manifest, force=True)
    print("current_state: MANIFEST_FINALIZED")
    print(f"run_dir: {repo_rel(run_dir)}")
    print(f"expected_workflow.json: wrote")
    print(f"workflow_pattern: {manifest.get('workflow_pattern')}")
    print(f"synthetic_call_ids: {str(bool(args.synthetic_call_ids)).lower()}")
    print(f"required_roles: {len(manifest.get('expected_roles') or [])}")
    return 0


def native_prepare(args: argparse.Namespace, full: bool = False) -> int:
    run_dir = ensure_reports_run_dir(ROOT / args.run_dir)
    task_spec = None
    if args.task_spec:
        task_spec = ensure_repo_path(ROOT / args.task_spec, "read TaskSpec")
        if not task_spec.is_file():
            raise RunnerError(f"TaskSpec not found: {repo_rel(task_spec)}")

    pattern = load_workflow_pattern(args.workflow_pattern, args.workflow)
    template = native_expected_workflow_template(run_dir, pattern, task_spec)
    template_action = write_json_if_needed(
        run_dir / "expected_workflow.template.json",
        template,
        args.force,
    )
    prompt_action = write_text_if_needed(
        run_dir / "prompt_bundle.md",
        render_native_prompt_bundle(pattern, task_spec, run_dir),
        args.force,
    )
    next_action_text = render_next_action_for_parent_codex(run_dir, pattern)
    next_action_status = write_text_if_needed(
        run_dir / "next_action_for_parent_codex.md",
        next_action_text,
        args.force,
    )

    state = "PREPARED"
    print(f"current_state: {state}")
    print(f"run_dir: {repo_rel(run_dir)}")
    print(f"workflow_pattern: {pattern.get('pattern_name')}")
    print(f"prompt_bundle.md: {prompt_action}")
    print(f"expected_workflow.template.json: {template_action}")
    print(f"next_action_for_parent_codex.md: {next_action_status}")
    print()
    print(next_action_text, end="")
    if full:
        print()
        print("--full stops after prepare. Parent Codex must run native subagents before capture.")
    return 0


def run_jsonl_capture(expected_workflow: Path, out_dir: Path) -> subprocess.CompletedProcess[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    return run_tool(
        [
            sys.executable,
            str(NATIVE_ADAPTER_SCRIPT),
            "--expected-workflow",
            repo_rel(expected_workflow),
            "--run-dir",
            repo_rel(out_dir),
            "--dry-run",
            "--capture-results",
        ]
    )


def ensure_capture_trace_complete(trace_path: Path) -> dict[str, Any]:
    if not trace_path.is_file():
        raise RunnerError(f"spawn_capture_trace.json was not written: {repo_rel(trace_path)}")
    trace = json.loads(trace_path.read_text(encoding="utf-8-sig"))
    if trace.get("adapter_exit_code") != 0:
        raise RunnerError(f"adapter_exit_code is not zero: {trace.get('adapter_exit_code')}")
    return trace


def write_backend_metadata(
    run_dir: Path,
    *,
    capture_backend: str,
    authoritative_backend: str = "jsonl",
    candidate_backend_status: str = "not_requested",
    equivalence_status: str | None = None,
    backend_default: bool = False,
) -> None:
    write_json(
        run_dir / "backend_selection.json",
        {
            "schema": "codex-harness.runner_backend_selection.v12",
            "capture_backend": capture_backend,
            "default_capture_backend": "jsonl",
            "authoritative_backend": authoritative_backend,
            "candidate_backend_status": candidate_backend_status,
            "equivalence_status": equivalence_status,
            "backend_default": backend_default,
            "jsonl_replacement_claimed": False,
            "app_server_backend_accepted": False,
            "backend_policy_file": repo_rel(BACKEND_POLICY_PATH),
            "runner_invoked_python_spawn_agent": False,
            "runner_used_codex_exec_for_subagents": False,
        },
    )


def native_capture_jsonl(args: argparse.Namespace, run_dir: Path) -> int:
    load_native_expected_workflow(run_dir)
    completed = run_jsonl_capture(run_dir / "expected_workflow.json", run_dir)
    print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, file=sys.stderr, end="")
    if completed.returncode != 0:
        return completed.returncode
    ensure_capture_trace_complete(run_dir / "spawn_capture_trace.json")
    write_backend_metadata(
        run_dir,
        capture_backend="jsonl",
        authoritative_backend="jsonl",
        candidate_backend_status="not_requested",
        backend_default=True,
    )
    print("current_state: CAPTURE_COMPLETE")
    print("capture_backend: jsonl")
    print("authoritative_backend: jsonl")
    return 0


def run_app_server_candidate_capture(
    out_dir: Path,
    live_timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    return run_tool(
        [
            sys.executable,
            str(APP_SERVER_SHADOW_ADAPTER_SCRIPT),
            "--live",
            "--emit-candidate-run",
            "--workspace",
            repo_rel(out_dir / "scratch_workspace"),
            "--expected-workflow",
            repo_rel(out_dir / "expected_workflow.json"),
            "--out-dir",
            repo_rel(out_dir),
            "--live-timeout-seconds",
            str(live_timeout_seconds),
        ]
    )


def read_candidate_classification(candidate_dir: Path) -> str | None:
    trace = read_json_if_present(candidate_dir / "candidate_vs_jsonl_equivalence_trace.json")
    return str(trace.get("classification")) if trace and trace.get("classification") else None


def write_dual_backend_equivalence(run_dir: Path, candidate_dir: Path, jsonl_dir: Path) -> str:
    out_dir = run_dir / "backend_equivalence"
    out_dir.mkdir(parents=True, exist_ok=True)
    candidate_trace = read_json_if_present(candidate_dir / "candidate_vs_jsonl_equivalence_trace.json") or {}
    jsonl_trace = read_json_if_present(jsonl_dir / "spawn_capture_trace.json") or {}
    classification = str(candidate_trace.get("classification") or "CANDIDATE_NOT_RUN")
    payload = {
        "schema": "codex-harness.runner_backend_equivalence.v12",
        "classification": classification,
        "equivalence_scope": "app_server_candidate_controlled_run",
        "jsonl_authoritative_run": repo_rel(jsonl_dir),
        "app_server_candidate_run": repo_rel(candidate_dir),
        "authoritative_backend": "jsonl",
        "app_server_backend_accepted": False,
        "jsonl_replacement_claimed": False,
        "jsonl_capture_status": jsonl_trace.get("capture_status"),
        "jsonl_adapter_exit_code": jsonl_trace.get("adapter_exit_code"),
        "candidate_capture_status": candidate_trace.get("candidate_capture_status"),
        "candidate_adapter_exit_code": candidate_trace.get("candidate_adapter_exit_code"),
        "candidate_aggregation_status": candidate_trace.get("candidate_aggregation_status"),
        "jsonl_candidate_baseline_aggregation_status": candidate_trace.get("jsonl_aggregation_status"),
    }
    write_json(out_dir / "backend_equivalence_trace.json", payload)
    lines = [
        "# Runner Backend Equivalence",
        "",
        f"RESULT: {classification}",
        "",
        "- Authoritative backend: `jsonl`",
        "- App Server backend accepted: `false`",
        "- JSONL replacement claimed: `false`",
        "- Equivalence scope: `app_server_candidate_controlled_run`",
        f"- JSONL authoritative run: `{repo_rel(jsonl_dir)}`",
        f"- App Server candidate run: `{repo_rel(candidate_dir)}`",
        "",
    ]
    (out_dir / "backend_equivalence_report.md").write_text("\n".join(lines), encoding="utf-8")
    return classification


def native_capture_app_server_candidate(args: argparse.Namespace, run_dir: Path) -> int:
    candidate_dir = run_dir / "app_server_candidate"
    completed = run_app_server_candidate_capture(candidate_dir, args.live_timeout_seconds)
    print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, file=sys.stderr, end="")
    classification = read_candidate_classification(candidate_dir)
    status = "complete" if completed.returncode == 0 and classification else "failed"
    write_backend_metadata(
        run_dir,
        capture_backend="app-server-candidate",
        authoritative_backend="jsonl",
        candidate_backend_status=status,
        equivalence_status=classification,
        backend_default=False,
    )
    print("current_state: APP_SERVER_CANDIDATE_CAPTURE_" + ("COMPLETE" if status == "complete" else "FAILED"))
    print("capture_backend: app-server-candidate")
    print("authoritative_backend: jsonl")
    print("backend_default: false")
    print("jsonl_replacement_claimed: false")
    if classification:
        print(f"equivalence_status: {classification}")
    return completed.returncode


def native_capture_dual(args: argparse.Namespace, run_dir: Path) -> int:
    load_native_expected_workflow(run_dir)
    jsonl_dir = run_dir / "jsonl_authoritative"
    jsonl_dir.mkdir(parents=True, exist_ok=True)
    expected_copy = jsonl_dir / "expected_workflow.json"
    expected_copy.write_text(
        (run_dir / "expected_workflow.json").read_text(encoding="utf-8-sig"),
        encoding="utf-8",
    )
    jsonl_completed = run_jsonl_capture(expected_copy, jsonl_dir)
    print(jsonl_completed.stdout, end="")
    if jsonl_completed.stderr:
        print(jsonl_completed.stderr, file=sys.stderr, end="")
    if jsonl_completed.returncode != 0:
        return jsonl_completed.returncode
    ensure_capture_trace_complete(jsonl_dir / "spawn_capture_trace.json")

    candidate_dir = run_dir / "app_server_candidate"
    candidate_completed = run_app_server_candidate_capture(candidate_dir, args.live_timeout_seconds)
    print(candidate_completed.stdout, end="")
    if candidate_completed.stderr:
        print(candidate_completed.stderr, file=sys.stderr, end="")
    classification = write_dual_backend_equivalence(run_dir, candidate_dir, jsonl_dir)
    candidate_status = "complete" if candidate_completed.returncode == 0 and classification else "failed"
    write_backend_metadata(
        run_dir,
        capture_backend="dual",
        authoritative_backend="jsonl",
        candidate_backend_status=candidate_status,
        equivalence_status=classification,
        backend_default=False,
    )
    print("current_state: DUAL_CAPTURE_COMPLETE")
    print("capture_backend: dual")
    print("authoritative_backend: jsonl")
    print(f"candidate_backend_status: {candidate_status}")
    print(f"equivalence_status: {classification}")
    if args.strict and classification != "V10_CANDIDATE_EQUIVALENT":
        return 1
    return jsonl_completed.returncode if candidate_completed.returncode == 0 else candidate_completed.returncode


def native_capture(args: argparse.Namespace) -> int:
    run_dir = ensure_reports_run_dir(ROOT / args.run_dir)
    if args.capture_backend == "jsonl":
        return native_capture_jsonl(args, run_dir)
    if args.capture_backend == "app-server-candidate":
        return native_capture_app_server_candidate(args, run_dir)
    if args.capture_backend == "dual":
        return native_capture_dual(args, run_dir)
    raise RunnerError(f"Unsupported capture backend: {args.capture_backend}")


def native_aggregate(args: argparse.Namespace) -> int:
    run_dir = ensure_reports_run_dir(ROOT / args.run_dir)
    source_dir = run_dir
    if args.capture_backend == "dual" and (run_dir / "jsonl_authoritative" / "spawn_capture_trace.json").is_file():
        source_dir = run_dir / "jsonl_authoritative"
    elif args.capture_backend == "app-server-candidate":
        source_dir = run_dir / "app_server_candidate" / "app_server_candidate_run"
        if not source_dir.is_dir():
            raise RunnerError(
                "candidate aggregation requested but app_server_candidate/app_server_candidate_run is missing"
            )
    cmd = [
        sys.executable,
        str(AGGREGATE_SCRIPT),
        "--run-dir",
        repo_rel(source_dir),
        "--strict",
    ]
    if args.aggregation_dir:
        aggregation_dir = ensure_reports_run_dir(ROOT / args.aggregation_dir)
        cmd.extend(["--out-dir", repo_rel(aggregation_dir)])
    completed = run_tool(cmd)
    print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, file=sys.stderr, end="")
    print(f"authority: {'app-server-candidate' if args.capture_backend == 'app-server-candidate' else 'jsonl'}")
    print("authoritative_backend: jsonl")
    print(f"jsonl_replacement_claimed: false")
    print(f"backend_default: {str(args.capture_backend == 'jsonl').lower()}")
    if args.aggregate_candidate and args.capture_backend != "app-server-candidate":
        candidate_source = run_dir / "app_server_candidate" / "app_server_candidate_run"
        candidate_out = run_dir / "app_server_candidate_aggregation"
        if candidate_source.is_dir():
            candidate_cmd = [
                sys.executable,
                str(AGGREGATE_SCRIPT),
                "--run-dir",
                repo_rel(candidate_source),
                "--strict",
                "--out-dir",
                repo_rel(candidate_out),
            ]
            candidate_completed = run_tool(candidate_cmd)
            print(candidate_completed.stdout, end="")
            if candidate_completed.stderr:
                print(candidate_completed.stderr, file=sys.stderr, end="")
            write_json(
                candidate_out / "runner_candidate_aggregation_context.json",
                {
                    "authority": "app-server-candidate",
                    "authoritative_baseline": "JSONL",
                    "replacement_accepted": False,
                    "returncode": candidate_completed.returncode,
                },
            )
    return completed.returncode


def read_json_if_present(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def validate_backend_policy_payload(policy: dict[str, Any]) -> tuple[list[str], list[str]]:
    blockers: list[str] = []
    warnings: list[str] = []
    if policy.get("default_capture_backend") != "jsonl":
        blockers.append("default_capture_backend must be jsonl")
    if policy.get("authoritative_backend") != "jsonl":
        blockers.append("authoritative_backend must be jsonl")
    candidate_backends = policy.get("candidate_backends")
    if not isinstance(candidate_backends, list) or "app-server-candidate" not in candidate_backends:
        blockers.append("candidate_backends must include app-server-candidate")
    experimental_backends = policy.get("experimental_backends")
    if not isinstance(experimental_backends, list) or "dual" not in experimental_backends:
        blockers.append("experimental_backends must include dual")
    if policy.get("jsonl_replacement_claimed") is True:
        blockers.append("jsonl_replacement_claimed must be false")
    if policy.get("app_server_default_allowed") is not False:
        blockers.append("app_server_default_allowed must be false")
    if policy.get("app_server_authoritative_allowed") is not False:
        blockers.append("app_server_authoritative_allowed must be false")
    if policy.get("default_capture_backend") == "app-server-candidate":
        blockers.append("app-server-candidate must not be marked default")
    if policy.get("authoritative_backend") == "app-server-candidate":
        blockers.append("app-server-candidate must not be marked authoritative")
    dual_mode = policy.get("dual_mode")
    if not isinstance(dual_mode, dict) or dual_mode.get("authoritative_backend") != "jsonl":
        blockers.append("dual mode must declare authoritative_backend=jsonl")
    link_modes = policy.get("allowed_link_modes")
    if not isinstance(link_modes, list):
        blockers.append("allowed_link_modes must be a list")
    else:
        missing = REQUIRED_BACKEND_POLICY_LINK_MODES - {str(item) for item in link_modes}
        if missing:
            blockers.append("allowed_link_modes missing: " + ", ".join(sorted(missing)))
    if policy.get("app_server_backend_accepted") is True:
        warnings.append("app_server_backend_accepted appears in policy; replacement is not accepted")
    return blockers, warnings


def first_existing_json(paths: list[Path]) -> tuple[dict[str, Any] | None, Path | None]:
    for path in paths:
        payload = read_json_if_present(path)
        if payload is not None:
            return payload, path
    return None, None


def trace_has_equivalent_but_mismatch(trace: dict[str, Any]) -> bool:
    classification = str(trace.get("classification") or "")
    if classification not in EQUIVALENT_CLASSIFICATIONS:
        return False
    mismatch_keys = (
        "mismatched_call_ids",
        "link_mismatches",
        "result_mismatches",
        "result_unproven",
        "candidate_unresolved_link_call_ids",
        "jsonl_unresolved_link_call_ids",
    )
    if any(trace.get(key) for key in mismatch_keys):
        return True
    for item in trace.get("comparisons", []):
        if not isinstance(item, dict):
            continue
        for key in (
            "equivalent",
            "result_hash_match",
            "result_verdict_match",
            "prompt_hash_match",
            "child_thread_match",
            "child_parent_thread_match",
            "fallback_used_match",
        ):
            if key in item and item.get(key) is not True:
                return True
    return False


def validate_run_backend_policy(run_dir: Path) -> tuple[list[str], list[str], dict[str, Any]]:
    blockers: list[str] = []
    warnings: list[str] = []
    backend = read_json_if_present(run_dir / "backend_selection.json") or {}
    selected_backend = str(backend.get("capture_backend") or "jsonl")
    authoritative_backend = str(backend.get("authoritative_backend") or "jsonl")
    if backend.get("jsonl_replacement_claimed") is True:
        blockers.append("backend_selection.json claims JSONL replacement")
    if selected_backend == "app-server-candidate" and backend.get("backend_default") is True:
        blockers.append("app-server-candidate is marked as default in backend_selection.json")
    if authoritative_backend == "app-server-candidate":
        blockers.append("app-server-candidate is marked authoritative in backend_selection.json")
    if selected_backend == "dual" and authoritative_backend != "jsonl":
        blockers.append("dual backend must keep authoritative_backend=jsonl")

    candidate_run_dirs = [
        run_dir / "app_server_candidate_run",
        run_dir / "app_server_candidate" / "app_server_candidate_run",
    ]
    candidate_run_dir = next((path for path in candidate_run_dirs if path.is_dir()), None)
    candidate_trace, candidate_trace_path = first_existing_json(
        [
            run_dir / "app_server_candidate_run" / "spawn_capture_trace.json",
            run_dir / "app_server_candidate" / "app_server_candidate_run" / "spawn_capture_trace.json",
        ]
    )
    equivalence, equivalence_path = first_existing_json(
        [
            run_dir / "dual_backend_equivalence" / "equivalence_trace.json",
            run_dir / "backend_equivalence" / "backend_equivalence_trace.json",
            run_dir / "app_server_candidate" / "candidate_vs_jsonl_equivalence_trace.json",
            run_dir / "candidate_vs_jsonl_equivalence_trace.json",
        ]
    )
    candidate_aggregation, candidate_aggregation_path = first_existing_json(
        [
            run_dir / "app_server_candidate_aggregation" / "aggregation_trace.json",
            run_dir / "app_server_candidate" / "app_server_candidate_aggregation" / "aggregation_trace.json",
        ]
    )
    jsonl_aggregation, jsonl_aggregation_path = first_existing_json(
        [
            run_dir / "jsonl_authoritative_aggregation" / "aggregation_trace.json",
            run_dir / "jsonl_authoritative" / "aggregation_trace.json",
            run_dir / "aggregation" / "aggregation_trace.json",
            run_dir / "aggregation_trace.json",
        ]
    )
    root_aggregation = read_json_if_present(run_dir / "aggregation_trace.json")

    candidate_present = (
        selected_backend in {"app-server-candidate", "dual"}
        or candidate_run_dir is not None
        or candidate_trace is not None
        or candidate_aggregation is not None
    )
    if candidate_present and candidate_trace is None:
        blockers.append("candidate run lacks app-server-candidate spawn_capture_trace.json")
    if candidate_present and equivalence is None:
        blockers.append("candidate/dual run lacks equivalence metadata")

    if candidate_trace is not None:
        if candidate_trace.get("capture_backend") != "app-server-candidate":
            blockers.append("candidate spawn_capture_trace.json must have capture_backend=app-server-candidate")
        unresolved = candidate_trace.get("unresolved_link_call_ids") or candidate_trace.get("summary", {}).get("unresolved_link_call_ids")
        if unresolved and candidate_aggregation and candidate_aggregation.get("overall_status") == "PASS":
            blockers.append("candidate aggregation passed despite unresolved links")
        if candidate_trace.get("boundary", {}).get("replacement_accepted") is True:
            blockers.append("candidate trace claims replacement_accepted")

    if equivalence is not None:
        if equivalence.get("jsonl_replacement_claimed") is True or equivalence.get("replacement_claimed") is True:
            blockers.append("equivalence metadata claims JSONL replacement")
        if equivalence.get("authoritative_backend") not in (None, "jsonl"):
            blockers.append("equivalence metadata must keep authoritative_backend=jsonl")
        if trace_has_equivalent_but_mismatch(equivalence):
            blockers.append("equivalence classification is equivalent despite mismatches")
        classification = str(equivalence.get("classification") or "")
        candidate_status = str(equivalence.get("candidate_aggregation_status") or "")
        jsonl_status = str(equivalence.get("jsonl_aggregation_status") or "")
        if jsonl_status == "PASS" and candidate_status and candidate_status != "PASS":
            if classification in EQUIVALENT_CLASSIFICATIONS or classification in {"", "CANDIDATE_NOT_RUN"}:
                blockers.append("candidate mismatch/failure is not reported by equivalence metadata")

    candidate_status = str((candidate_aggregation or {}).get("overall_status") or "")
    jsonl_status = str((jsonl_aggregation or {}).get("overall_status") or "")
    root_status = str((root_aggregation or {}).get("overall_status") or "")
    if candidate_status == "PASS" and jsonl_status and jsonl_status != "PASS" and root_status == "PASS":
        blockers.append("authoritative/root aggregation passes despite JSONL authoritative failure")

    metadata = {
        "backend_selection_present": bool(backend),
        "selected_capture_backend": selected_backend,
        "authoritative_backend": authoritative_backend,
        "candidate_run_dir": repo_rel(candidate_run_dir) if candidate_run_dir else None,
        "candidate_trace_path": repo_rel(candidate_trace_path) if candidate_trace_path else None,
        "equivalence_path": repo_rel(equivalence_path) if equivalence_path else None,
        "candidate_aggregation_path": repo_rel(candidate_aggregation_path) if candidate_aggregation_path else None,
        "jsonl_aggregation_path": repo_rel(jsonl_aggregation_path) if jsonl_aggregation_path else None,
    }
    return blockers, warnings, metadata


def native_validate_backend_policy(args: argparse.Namespace) -> int:
    run_dir = ensure_reports_run_dir(ROOT / args.run_dir)
    policy_path = backend_policy_path_from_args(args)
    policy = read_json_file(policy_path, "backend policy")
    policy_blockers, policy_warnings = validate_backend_policy_payload(policy)
    run_blockers, run_warnings, metadata = validate_run_backend_policy(run_dir)
    blockers = policy_blockers + run_blockers
    warnings = policy_warnings + run_warnings
    print("backend_policy_validation: " + ("PASS" if not blockers else "FAIL"))
    print(f"backend_policy_file: {repo_rel(policy_path)}")
    print(f"run_dir: {repo_rel(run_dir)}")
    print(f"default_capture_backend: {policy.get('default_capture_backend')}")
    print(f"authoritative_backend: {policy.get('authoritative_backend')}")
    print(f"jsonl_replacement_claimed: {str(bool(policy.get('jsonl_replacement_claimed'))).lower()}")
    print(f"selected_capture_backend: {metadata['selected_capture_backend']}")
    print(f"run_authoritative_backend: {metadata['authoritative_backend']}")
    print("blocking_issues:")
    print(format_list(blockers))
    print("warnings:")
    print(format_list(warnings))
    return 1 if blockers else 0


def native_status(args: argparse.Namespace) -> int:
    run_dir = ensure_reports_run_dir(ROOT / args.run_dir)
    aggregation_dir = ensure_reports_run_dir(ROOT / args.aggregation_dir) if args.aggregation_dir else run_dir
    policy, policy_path, policy_present = load_backend_policy_for_status(args)
    expected = read_json_if_present(run_dir / "expected_workflow.json")
    template = read_json_if_present(run_dir / "expected_workflow.template.json")
    backend = read_json_if_present(run_dir / "backend_selection.json") or {}
    selected_backend = str(backend.get("capture_backend") or args.capture_backend)
    authoritative_backend = str(backend.get("authoritative_backend") or "jsonl")
    default_capture_backend = str(policy.get("default_capture_backend") or "jsonl")
    jsonl_replacement_claimed = bool(
        backend.get("jsonl_replacement_claimed") or policy.get("jsonl_replacement_claimed")
    )
    trace = read_json_if_present(run_dir / "spawn_capture_trace.json")
    jsonl_trace = read_json_if_present(run_dir / "jsonl_authoritative" / "spawn_capture_trace.json")
    if trace is None and jsonl_trace is not None:
        trace = jsonl_trace
    aggregation = read_json_if_present(aggregation_dir / "aggregation_trace.json")
    jsonl_aggregation = (
        aggregation
        or read_json_if_present(run_dir / "jsonl_authoritative_aggregation" / "aggregation_trace.json")
        or read_json_if_present(run_dir / "jsonl_authoritative" / "aggregation_trace.json")
    )
    candidate_equivalence = (
        read_json_if_present(run_dir / "backend_equivalence" / "backend_equivalence_trace.json")
        or read_json_if_present(run_dir / "dual_backend_equivalence" / "equivalence_trace.json")
        or read_json_if_present(run_dir / "app_server_candidate" / "candidate_vs_jsonl_equivalence_trace.json")
        or read_json_if_present(run_dir / "candidate_vs_jsonl_equivalence_trace.json")
    )
    candidate_aggregation = (
        read_json_if_present(run_dir / "app_server_candidate" / "app_server_candidate_aggregation" / "aggregation_trace.json")
        or read_json_if_present(run_dir / "app_server_candidate_aggregation" / "aggregation_trace.json")
    )

    state = "PREPARED" if expected or template else "CAPTURE_FAILED"
    if trace is not None:
        if trace.get("adapter_exit_code") == 0 and trace.get("capture_status") == "complete":
            state = "CAPTURE_COMPLETE"
        else:
            state = "CAPTURE_FAILED"
    if selected_backend == "app-server-candidate" and candidate_equivalence is not None:
        state = "CAPTURE_COMPLETE" if backend.get("candidate_backend_status") == "complete" else "CAPTURE_FAILED"
    if jsonl_aggregation is not None:
        state = "AGGREGATION_PASS" if jsonl_aggregation.get("overall_status") == "PASS" else "AGGREGATION_FAIL"

    print(f"current_state: {state}")
    print(f"run_dir: {repo_rel(run_dir)}")
    print(f"aggregation_dir: {repo_rel(aggregation_dir)}")
    print(f"selected_capture_backend: {selected_backend}")
    print(f"default_capture_backend: {default_capture_backend}")
    print(f"authoritative_backend: {authoritative_backend}")
    print(f"backend_default: {str(selected_backend == 'jsonl').lower()}")
    print(f"candidate_backend_status: {backend.get('candidate_backend_status') or ('present' if (candidate_equivalence or candidate_aggregation) else 'not_present')}")
    print(f"equivalence_status: {backend.get('equivalence_status') or (candidate_equivalence or {}).get('classification') or '(none)'}")
    print(f"jsonl_replacement_claimed: {str(jsonl_replacement_claimed).lower()}")
    print(f"backend_policy_file: {repo_rel(policy_path) if policy_present else '(missing)'}")
    print(f"expected_workflow_json: {str(expected is not None).lower()}")
    print(f"expected_workflow_template_json: {str(template is not None).lower()}")
    if trace is not None:
        print(f"adapter_exit_code: {trace.get('adapter_exit_code')}")
        print(f"capture_status: {trace.get('capture_status')}")
        print(f"workflow_validation_scope: {trace.get('workflow_validation_scope')}")
        print(f"unresolved_link_call_ids: {trace.get('unresolved_link_call_ids', trace.get('missing_spawn_end_call_ids'))}")
    if jsonl_aggregation is not None:
        print(f"jsonl_aggregation_status: {jsonl_aggregation.get('overall_status')}")
        print(f"aggregation_overall_status: {jsonl_aggregation.get('overall_status')}")
        print(f"reviewer_verdict: {jsonl_aggregation.get('reviewer_verdict')}")
    if candidate_aggregation is not None:
        print(f"candidate_aggregation_status: {candidate_aggregation.get('overall_status')}")
    return 0 if state in {"PREPARED", "CAPTURE_COMPLETE", "AGGREGATION_PASS"} else 1


def run_native_mode(args: argparse.Namespace) -> int:
    actions = [
        bool(args.prepare_only),
        bool(args.capture_only),
        bool(args.aggregate_only),
        bool(args.status),
        bool(args.full),
        bool(args.finalize_manifest),
        bool(args.validate_backend_policy),
    ]
    if sum(actions) != 1:
        raise RunnerError(
            "codex-native-subagent mode requires exactly one action: "
            "--prepare-only, --capture-only, --aggregate-only, --status, "
            "--full, --finalize-manifest, or --validate-backend-policy"
        )
    if args.workflow_pattern and not (args.prepare_only or args.full):
        raise RunnerError("--workflow-pattern is only used with native --prepare-only or --full")
    if args.capture_backend != "jsonl" and not (args.capture_only or args.aggregate_only or args.status):
        raise RunnerError("--capture-backend app-server-candidate|dual is only used with native capture/aggregate/status")
    if args.aggregate_candidate and not args.aggregate_only:
        raise RunnerError("--aggregate-candidate requires --aggregate-only")
    default_backend_policy_file = str(BACKEND_POLICY_PATH.relative_to(ROOT))
    if args.backend_policy_file != default_backend_policy_file and not (
        args.status or args.validate_backend_policy
    ):
        raise RunnerError("--backend-policy-file is only used with --status or --validate-backend-policy")
    manifest_flags = [
        bool(args.call_id),
        bool(args.call_id_map),
        bool(args.parent_thread_id),
        bool(args.log_root),
        bool(args.synthetic_call_ids),
    ]
    if any(manifest_flags) and not args.finalize_manifest:
        raise RunnerError("manifest finalization flags require --finalize-manifest")
    if args.prepare_only:
        return native_prepare(args)
    if args.full:
        return native_prepare(args, full=True)
    if args.finalize_manifest:
        return native_finalize_manifest(args)
    if args.validate_backend_policy:
        return native_validate_backend_policy(args)
    if args.capture_only:
        return native_capture(args)
    if args.aggregate_only:
        return native_aggregate(args)
    return native_status(args)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the dry-run subagent workflow chain without spawning agents."
    )
    parser.add_argument("--task-spec", help="TaskSpec path. Optional for codex-native-subagent prepare.")
    parser.add_argument(
        "--workflow",
        choices=SUPPORTED_WORKFLOWS,
        default="auto",
        help="Dispatcher workflow. Default: auto.",
    )
    parser.add_argument(
        "--workflow-pattern",
        choices=SUPPORTED_WORKFLOW_PATTERNS,
        help="Native prepare/full workflow pattern abstraction.",
    )
    parser.add_argument("--run-dir", required=True, help="Run directory under .codex-harness/reports/.")
    parser.add_argument(
        "--mode",
        choices=SUPPORTED_MODES,
        default="auto",
        help=(
            "auto falls back to prompt-bundle; real-spawn only runs a legacy gate; "
            "codex-native-subagent prepares/captures/aggregates logs but never spawns."
        ),
    )
    parser.add_argument("--prepare-only", action="store_true", help="Native mode: prepare prompt/template only.")
    parser.add_argument("--capture-only", action="store_true", help="Native mode: capture completed parent-spawned subagents.")
    parser.add_argument("--aggregate-only", action="store_true", help="Native mode: run strict aggregation only.")
    parser.add_argument("--status", action="store_true", help="Native mode: print current native workflow state.")
    parser.add_argument("--full", action="store_true", help="Native mode: prepare and stop for parent Codex native spawns.")
    parser.add_argument(
        "--finalize-manifest",
        action="store_true",
        help="Native mode: fill expected_workflow.template.json with concrete call_id mappings.",
    )
    parser.add_argument(
        "--validate-backend-policy",
        action="store_true",
        help="Native mode: validate JSONL-authoritative backend policy and candidate metadata.",
    )
    parser.add_argument(
        "--backend-policy-file",
        default=str(BACKEND_POLICY_PATH.relative_to(ROOT)),
        help="Backend policy JSON file. Default: .codex-harness/backend_policy.json.",
    )
    parser.add_argument(
        "--aggregation-dir",
        help="Native mode: strict aggregation output/status directory under .codex-harness/reports/.",
    )
    parser.add_argument(
        "--capture-backend",
        choices=SUPPORTED_CAPTURE_BACKENDS,
        default="jsonl",
        help=(
            "Native capture backend. Default jsonl is authoritative. "
            "app-server-candidate and dual are explicit candidate/shadow modes only."
        ),
    )
    parser.add_argument(
        "--live-timeout-seconds",
        type=int,
        default=180,
        help="Native app-server-candidate/dual timeout for the isolated App Server probe.",
    )
    parser.add_argument(
        "--aggregate-candidate",
        action="store_true",
        help="Native aggregate-only: also aggregate app-server-candidate artifacts when present.",
    )
    parser.add_argument(
        "--call-id",
        action="append",
        default=[],
        help="Native finalize: role=call_id mapping. May be repeated.",
    )
    parser.add_argument(
        "--call-id-map",
        help="Native finalize: JSON object mapping role IDs or aliases to call IDs.",
    )
    parser.add_argument(
        "--parent-thread-id",
        help="Native finalize: concrete parent Codex thread_id to write into expected_workflow.json.",
    )
    parser.add_argument(
        "--log-root",
        help="Native finalize: concrete Codex sessions/log root to write into expected_workflow.json.",
    )
    parser.add_argument(
        "--synthetic-call-ids",
        action="store_true",
        help="Native finalize: label the manifest as validation-only with synthetic call IDs.",
    )
    parser.add_argument(
        "--aggregate",
        action="store_true",
        help="Run aggregator if any expected result file exists.",
    )
    parser.add_argument("--strict", action="store_true", help="Pass --strict to aggregator.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Dry run only. This runner never spawns agents.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing generated files. Default reuses or protects them.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        if args.mode == "codex-native-subagent":
            return run_native_mode(args)
        if args.capture_backend != "jsonl" or args.aggregate_candidate:
            raise RunnerError("--capture-backend and --aggregate-candidate require --mode codex-native-subagent")
        if any(
            [
                args.prepare_only,
                args.capture_only,
                args.aggregate_only,
                args.status,
                args.full,
                args.finalize_manifest,
                args.validate_backend_policy,
            ]
        ):
            raise RunnerError("native action flags require --mode codex-native-subagent")
        if args.aggregation_dir:
            raise RunnerError("--aggregation-dir requires --mode codex-native-subagent")
        if args.call_id or args.call_id_map or args.parent_thread_id or args.log_root or args.synthetic_call_ids:
            raise RunnerError("native manifest flags require --mode codex-native-subagent")
        if args.backend_policy_file != str(BACKEND_POLICY_PATH.relative_to(ROOT)):
            raise RunnerError("--backend-policy-file requires --mode codex-native-subagent")
        if not args.task_spec:
            raise RunnerError("--task-spec is required unless --mode codex-native-subagent is used")
        task_spec = ensure_repo_path(ROOT / args.task_spec, "read TaskSpec")
        if not task_spec.is_file():
            raise RunnerError(f"TaskSpec not found: {repo_rel(task_spec)}")
        run_dir = ensure_reports_run_dir(ROOT / args.run_dir)

        capability = run_probe()
        spawn_status = capability.get("real_spawn_adapter_status", "UNKNOWN")
        project_spawn_supported = bool(capability.get("project_agent_spawn_supported"))
        notes: list[str] = []

        if args.mode == "real-spawn":
            gate = run_dispatch_real_spawn_gate(task_spec, args.workflow)
            report = {
                "workflow_status": "REAL_SPAWN_GATE_FAILED" if gate.returncode else "REAL_SPAWN_GATE_UNEXPECTED_PASS",
                "spawn_capability_status": spawn_status,
                "selected_adapter": "real-spawn-gate",
                "run_dir": repo_rel(run_dir),
                "prompt_bundle_path": "(not generated)",
                "prompt_bundle_action": "not generated in real-spawn mode",
                "planned_agents": [],
                "expected_result_files": [],
                "existing_result_files": existing_result_files(run_dir) if run_dir.exists() else [],
                "aggregator_status": "NOT_RUN",
                "next_manual_steps": [
                    "Use --mode auto or --mode prompt-bundle to generate manual prompts.",
                    "Do not treat generic codex exec as project-agent spawn.",
                ],
                "real_spawn_gate_output": gate.stdout + gate.stderr,
                "aggregator_output": "",
                "notes": notes,
            }
            print(render_report(report), end="")
            return gate.returncode or 3

        if args.mode in {"vscode-plugin", "vscode-plugin-assisted"}:
            completed = run_vscode_plugin_orchestrator(
                task_spec,
                args.workflow,
                run_dir,
                assisted=args.mode == "vscode-plugin-assisted",
            )
            print(completed.stdout, end="")
            if completed.stderr:
                print(completed.stderr, file=sys.stderr, end="")
            return completed.returncode

        selected_adapter = "prompt-bundle"
        if args.mode == "auto":
            if project_spawn_supported and spawn_status == "READY":
                notes.append(
                    "real spawn capability may be available, but runner real-spawn adapter is not implemented in P2-14"
                )
            else:
                notes.append("real spawn NOT_READY; falling back to prompt-bundle")

        prompt_path, prompt_text, wrote_prompt = run_dispatch_prompt_bundle(
            task_spec, args.workflow, run_dir, args.force
        )
        planned_agents = parse_planned_agents(prompt_text)
        expected_files = expected_result_files(planned_agents)
        existing_files = existing_result_files(run_dir)

        aggregator_status = "NOT_REQUESTED"
        aggregator_output = ""
        exit_code = 0
        if args.aggregate:
            if not any(filename in existing_files for filename in expected_files):
                aggregator_status = "WAITING_FOR_MANUAL_RESULTS"
            else:
                agg_rc, agg_stdout, agg_stderr = run_aggregator(run_dir, args.strict)
                aggregator_status = status_from_aggregator_output(agg_stdout, agg_rc)
                aggregator_output = agg_stdout + agg_stderr
                exit_code = agg_rc

        missing_expected = [filename for filename in expected_files if filename not in existing_files]
        next_steps = [
            f"Open {repo_rel(prompt_path)}.",
            "Run each backend agent prompt manually and serially.",
        ]
        if missing_expected:
            next_steps.append("Save missing manual result file(s):")
            next_steps.extend(missing_expected)
        if args.aggregate and aggregator_status == "WAITING_FOR_MANUAL_RESULTS":
            next_steps.append("Re-run with --aggregate after result files are saved.")

        report = {
            "workflow_status": "PROMPT_BUNDLE_READY"
            if aggregator_status in {"NOT_REQUESTED", "WAITING_FOR_MANUAL_RESULTS"}
            else "AGGREGATED",
            "spawn_capability_status": spawn_status,
            "selected_adapter": selected_adapter,
            "run_dir": repo_rel(run_dir),
            "prompt_bundle_path": repo_rel(prompt_path),
            "prompt_bundle_action": "wrote" if wrote_prompt else "reused-existing",
            "planned_agents": planned_agents,
            "expected_result_files": expected_files,
            "existing_result_files": existing_files,
            "aggregator_status": aggregator_status,
            "next_manual_steps": next_steps,
            "real_spawn_gate_output": "",
            "aggregator_output": aggregator_output,
            "notes": notes,
        }
        print(render_report(report), end="")
        return exit_code
    except RunnerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
