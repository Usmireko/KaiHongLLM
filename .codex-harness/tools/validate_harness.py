"""Read-only validator for the .codex-harness P0 scaffold.

The validator only inspects files under .codex-harness. It does not connect to
boards or servers, run Codex or Claude, start an app server, mutate business
scripts, or enforce an automatic review gate.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


HARNESS_ROOT = Path(__file__).resolve().parents[1]

REQUIRED_DIRS = [
    "docs",
    "templates",
    "tools",
    "schemas",
]

REQUIRED_DOCS = [
    "README.md",
    "docs/task-spec.md",
    "docs/task-router.md",
    "docs/path-lease.md",
    "docs/budget-circuit-breaker.md",
    "docs/agent-result-bundle.md",
    "docs/roles.md",
]

REQUIRED_TEMPLATES = [
    "templates/task_spec.yaml",
    "templates/task_router.yaml",
    "templates/path_lease.yaml",
    "templates/budget_circuit_breaker.yaml",
    "templates/agent_result_bundle.md",
    "templates/roles.yaml",
    "templates/routing_note.md",
]

REQUIRED_SCHEMAS = [
    "schemas/task_spec.schema.json",
]

TASK_SPEC_CORE_KEYS = [
    "risk_level",
    "task_type",
    "scope",
    "allowed_paths",
    "forbidden_paths",
    "validation_commands",
    "max_repair_depth",
    "codex_audit_required",
]

EXAMPLE_TASK_SPEC_REQUIRED_KEYS = [
    *TASK_SPEC_CORE_KEYS,
    "expected_outputs",
    "rollback_plan",
]

DANGEROUS_FORBIDDEN_PATHS = [
    "storage/runs",
    "dataset/raw",
    ".git",
]

ROLE_REQUIRED_IDS = {
    "explorer",
    "implementer",
    "reviewer",
    "adversarial_reviewer",
    "validator",
    "repairer",
}

ROLE_REQUIRED_FIELDS = {
    "role_id",
    "purpose",
    "backend_agent",
    "review_mode",
    "allowed_task_types",
    "write_permission",
    "write_scope_source",
    "allowed_paths",
    "forbidden_paths",
    "allowed_tools",
    "forbidden_tools",
    "board_access",
    "server_access",
    "max_repair_depth",
    "requires_human_gate",
    "requires_review_after",
    "output_contract",
    "stop_conditions",
    "escalation_rules",
    "claude_assist_policy",
}

ALLOWED_ROLE_WRITE_PERMISSIONS = {
    "none",
    "scoped",
    "forbidden",
}

ALLOWED_ROLE_ACCESS = {
    "forbidden",
    "requires_human_gate",
    "allowed_by_task_spec",
}

ALLOWED_ROLE_REVIEW_MODES = {
    "none",
    "standard",
    "adversarial",
    "self-review",
    "targeted-repair",
}

ALLOWED_BACKEND_AGENTS = {
    None,
    "project-explorer",
    "project-implementer",
    "project-reviewer",
    "project-repairer",
}

EXPECTED_ROLE_BACKENDS = {
    "explorer": "project-explorer",
    "implementer": "project-implementer",
    "reviewer": "project-reviewer",
    "adversarial_reviewer": "project-reviewer",
    "validator": None,
    "repairer": "project-repairer",
}

NO_REPAIR_ROLES = {
    "explorer",
    "reviewer",
    "adversarial_reviewer",
    "validator",
}

ROLE_FORBIDDEN_PATH_DEFAULTS = [
    "storage/runs",
    "dataset/raw",
    "models",
    "checkpoints",
    ".git",
]

WRITE_FORBIDDEN_ROLES = {
    "explorer",
    "reviewer",
    "adversarial_reviewer",
}

NO_GIT_WRITE_ROLES = {
    "implementer",
    "repairer",
}

ROUTING_NOTE_REQUIRED_SECTIONS = {
    "TaskSpec source": ["taskspec source"],
    "Risk and task type": ["risk and task type", "task summary"],
    "Scope": ["scope"],
    "Path policy": ["path policy"],
    "Selected roles": ["selected roles"],
    "Role sequence": ["role sequence", "selected roles"],
    "Backend agent mapping": ["backend agent mapping", "backend_agent mapping"],
    "Review mode": ["review mode"],
    "Validation plan": ["validation plan"],
    "Repair policy": ["repair policy"],
    "Human gate": ["human gate"],
    "Board access policy": ["board access policy"],
    "Server access policy": ["server access policy"],
    "Claude assist policy": ["claude assist policy"],
    "Stop conditions": ["stop conditions"],
    "Escalation rules": ["escalation rules"],
    "AgentResultBundle expectation": [
        "agentresultbundle expectation",
        "final agentresultbundle expectation",
    ],
}

ROUTING_NOTE_NON_EXECUTION_TERMS = [
    "does not spawn agents",
    "does not dispatch work",
    "scheduler",
    "multi-agent concurrency",
    "automatic review gate",
    "execute real collection",
]

CONSISTENCY_TASK_SPEC = "tasks/examples/subagent_roles_rehearsal_task_spec.yaml"
CONSISTENCY_ROUTING_NOTE = "tasks/runs/p1_5_subagent_roles_rehearsal_routing_note.md"

CONSISTENCY_ROUTE_KEYS = [
    "low_risk_route",
    "medium_risk_route",
    "high_risk_route",
]

CONSISTENCY_NON_EXECUTION_TERMS = [
    "does not spawn agents",
    "scheduler",
    "multi-agent concurrency",
    "automatic review gate",
]

CONSISTENCY_NON_EXECUTION_PHRASES = {
    "no scheduler": [
        "does not dispatch work, start a scheduler",
        "did not run a scheduler",
        "no scheduler",
    ],
    "no multi-agent concurrency": [
        "run multi-agent concurrency",
        "did not start multi-agent concurrency",
        "no multi-agent concurrency",
    ],
    "no auto review gate": [
        "enable an automatic review gate",
        "did not enable an automatic review gate",
        "no auto review gate",
    ],
}

CONSISTENCY_EXTERNAL_TERMS = [
    "board",
    "server",
    "hdc",
    "real collection",
]

CONSISTENCY_TASKSPEC_FALSE_FLAGS = [
    "allow_hdc",
    "allow_ssh",
    "allow_board_connection",
    "allow_server_connection",
    "allow_app_server",
    "allow_real_collection",
    "allow_scheduler",
    "allow_multi_agent_concurrency",
    "allow_auto_review_gate",
]

ALLOWED_RISK_LEVELS = {
    "low",
    "medium",
    "high",
}

ALLOWED_CODEX_AUDIT_REQUIRED = {
    "none",
    "review",
    "adversarial-review",
    "manual",
}

POLICY_RISK_TERMS = [
    "app-server",
    "app server",
    "scheduler",
    "multi-agent",
    "multi agent",
    "auto review gate",
    "automatic review gate",
]

PY_EXECUTION_PATTERNS = [
    re.compile(r"\bimport\s+subprocess\b"),
    re.compile(r"\bfrom\s+subprocess\s+import\b"),
    re.compile(r"\bos\.(system|popen|spawn|exec)"),
    re.compile(r"\bPopen\s*\("),
    re.compile(r"\bStart-Process\b", re.IGNORECASE),
]


class Recorder:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.info: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def note(self, message: str) -> None:
        self.info.append(message)


def rel(path: Path) -> str:
    return path.relative_to(HARNESS_ROOT).as_posix()


def load_yaml_if_available(path: Path, recorder: Recorder) -> tuple[bool, Any | None]:
    try:
        import yaml  # type: ignore
    except ImportError:
        recorder.warn(
            "PyYAML is not installed; skipped strict YAML parse for "
            f"{rel(path)}. Install PyYAML for stronger template validation."
        )
        return False, None

    try:
        with path.open("r", encoding="utf-8") as handle:
            return True, yaml.safe_load(handle)
    except Exception as exc:  # pragma: no cover - message is the validation output.
        recorder.error(f"YAML parse failed for {rel(path)}: {exc}")
        return True, None


def iter_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            keys.add(str(key))
            keys.update(iter_keys(child))
    elif isinstance(value, list):
        for child in value:
            keys.update(iter_keys(child))
    return keys


def flatten_strings(value: Any) -> list[str]:
    strings: list[str] = []
    if isinstance(value, dict):
        for child in value.values():
            strings.extend(flatten_strings(child))
    elif isinstance(value, list):
        for child in value:
            strings.extend(flatten_strings(child))
    elif isinstance(value, str):
        strings.append(value)
    return strings


def nested_get(value: Any, path: list[str]) -> Any:
    current = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def normalize_path_value(value: str) -> str:
    return value.replace("\\", "/").strip().strip('"').strip("'").rstrip("/*")


def has_dangerous_path(values: list[str], dangerous: str) -> bool:
    wanted = dangerous.rstrip("/*")
    normalized = [normalize_path_value(value) for value in values]
    return any(value == wanted or value.startswith(wanted + "/") for value in normalized)


def text_has_key(text: str, key: str) -> bool:
    return re.search(rf"(^|\s){re.escape(key)}\s*:", text, re.MULTILINE) is not None


def text_value_for_key(text: str, key: str) -> str | None:
    match = re.search(
        rf"^\s*{re.escape(key)}\s*:\s*[\"']?([^\"'\r\n#]+)",
        text,
        re.MULTILINE,
    )
    if not match:
        return None
    return match.group(1).strip()


def normalize_scalar_value(value: str | None) -> Any:
    if value is None:
        return None
    cleaned = value.strip().strip('"').strip("'")
    lowered = cleaned.lower()
    if lowered == "null":
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if re.fullmatch(r"-?\d+", cleaned):
        return int(cleaned)
    return cleaned


def coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value.strip())
    return None


def text_scalar_for_key(text: str, key: str) -> Any:
    return normalize_scalar_value(text_value_for_key(text, key))


def text_list_under_key(text: str, key: str) -> list[str]:
    lines = text.splitlines()
    values: list[str] = []
    key_indent: int | None = None
    in_block = False

    for line in lines:
        if not in_block:
            match = re.match(rf"^(\s*){re.escape(key)}\s*:\s*$", line)
            if match:
                key_indent = len(match.group(1))
                in_block = True
            continue

        if not line.strip() or line.lstrip().startswith("#"):
            continue

        indent = len(line) - len(line.lstrip(" "))
        if key_indent is not None and indent <= key_indent:
            break

        item = re.match(r"^\s*-\s*[\"']?([^\"'\r\n#]+)", line)
        if item:
            values.append(item.group(1).strip())

    return values


def text_role_blocks(text: str) -> dict[str, str]:
    role_matches = list(
        re.finditer(
            r"^  -\s+role_id\s*:\s*[\"']?([^\"'\r\n#]+)",
            text,
            re.MULTILINE,
        )
    )
    blocks: dict[str, str] = {}
    for index, match in enumerate(role_matches):
        role_id = match.group(1).strip()
        start = match.start()
        end = role_matches[index + 1].start() if index + 1 < len(role_matches) else len(text)
        blocks[role_id] = text[start:end]
    return blocks


def strings_contain_token(values: list[str], token: str) -> bool:
    token_lower = token.lower()
    return any(token_lower in value.lower() for value in values)


def tool_list_allows(values: list[str], tool: str) -> bool:
    tool_lower = tool.lower()
    return any(tool_lower in value.lower() for value in values)


def normalize_role_reference(value: str) -> str:
    cleaned = value.strip().strip("`").strip('"').strip("'")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if cleaned.startswith("optional "):
        cleaned = cleaned.removeprefix("optional ").strip()
    return cleaned.split(" ", 1)[0].strip()


def markdown_headings(text: str) -> set[str]:
    headings: set[str] = set()
    for match in re.finditer(r"^#{1,6}\s+(.+?)\s*$", text, re.MULTILINE):
        heading = match.group(1).strip().strip("#").strip()
        headings.add(heading.lower())
    return headings


def check_required_paths(recorder: Recorder) -> None:
    for directory in REQUIRED_DIRS:
        path = HARNESS_ROOT / directory
        if not path.is_dir():
            recorder.error(f"Missing required directory: {directory}")

    for file_name in [*REQUIRED_DOCS, *REQUIRED_TEMPLATES, *REQUIRED_SCHEMAS]:
        path = HARNESS_ROOT / file_name
        if not path.is_file():
            recorder.error(f"Missing required file: {file_name}")


def check_yaml_templates(recorder: Recorder) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    yaml_files = sorted((HARNESS_ROOT / "templates").glob("*.yaml"))
    for path in yaml_files:
        parsed_ok, data = load_yaml_if_available(path, recorder)
        if parsed_ok and data is not None:
            parsed[rel(path)] = data
    if yaml_files:
        recorder.note(f"Checked YAML templates: {len(yaml_files)}")
    return parsed


def check_task_spec_core_fields(parsed_yaml: dict[str, Any], recorder: Recorder) -> None:
    task_spec_path = HARNESS_ROOT / "templates/task_spec.yaml"
    if not task_spec_path.is_file():
        return

    parsed = parsed_yaml.get("templates/task_spec.yaml")
    if parsed is not None:
        keys = iter_keys(parsed)
        missing = [key for key in TASK_SPEC_CORE_KEYS if key not in keys]
    else:
        text = task_spec_path.read_text(encoding="utf-8")
        missing = [
            key
            for key in TASK_SPEC_CORE_KEYS
            if re.search(rf"(^|\s){re.escape(key)}\s*:", text, re.MULTILINE) is None
        ]

    for key in missing:
        recorder.error(f"TaskSpec missing required core field: {key}")


def check_forbidden_paths(parsed_yaml: dict[str, Any], recorder: Recorder) -> None:
    task_spec_path = HARNESS_ROOT / "templates/task_spec.yaml"
    if not task_spec_path.is_file():
        return

    parsed = parsed_yaml.get("templates/task_spec.yaml")
    forbidden_values: list[str]
    if isinstance(parsed, dict):
        paths = parsed.get("paths", {})
        forbidden = paths.get("forbidden_paths") if isinstance(paths, dict) else None
        forbidden_values = flatten_strings(forbidden)
    else:
        text = task_spec_path.read_text(encoding="utf-8")
        forbidden_values = re.findall(r"^\s*-\s*[\"']?([^\"'\r\n#]+)", text, re.MULTILINE)

    for dangerous in DANGEROUS_FORBIDDEN_PATHS:
        if not has_dangerous_path(forbidden_values, dangerous):
            recorder.error(f"TaskSpec forbidden_paths missing dangerous area: {dangerous}")


def check_example_task_specs(recorder: Recorder) -> None:
    examples_dir = HARNESS_ROOT / "tasks/examples"
    if not examples_dir.exists():
        recorder.note("No example TaskSpec directory found.")
        return

    example_files = sorted(examples_dir.glob("*.yaml"))
    if not example_files:
        recorder.note("No example TaskSpec YAML files found.")
        return

    checked = 0
    for path in example_files:
        checked += 1
        parsed_ok, data = load_yaml_if_available(path, recorder)
        if parsed_ok and isinstance(data, dict):
            check_example_task_spec_parsed(path, data, recorder)
        else:
            check_example_task_spec_text(path, recorder)

    recorder.note(f"Checked example TaskSpecs: {checked}")


def check_example_task_spec_parsed(path: Path, data: dict[str, Any], recorder: Recorder) -> None:
    keys = iter_keys(data)
    for key in EXAMPLE_TASK_SPEC_REQUIRED_KEYS:
        if key not in keys:
            recorder.error(f"{rel(path)} missing required example TaskSpec field: {key}")

    risk_level = nested_get(data, ["task", "risk_level"])
    if risk_level not in ALLOWED_RISK_LEVELS:
        recorder.error(
            f"{rel(path)} has invalid risk_level: {risk_level!r}; "
            "expected low, medium, or high."
        )

    codex_audit_required = nested_get(data, ["review", "codex_audit_required"])
    if codex_audit_required not in ALLOWED_CODEX_AUDIT_REQUIRED:
        recorder.error(
            f"{rel(path)} has invalid codex_audit_required: "
            f"{codex_audit_required!r}; expected none, review, "
            "adversarial-review, or manual."
        )

    forbidden_paths = nested_get(data, ["paths", "forbidden_paths"])
    forbidden_values = flatten_strings(forbidden_paths)
    for dangerous in DANGEROUS_FORBIDDEN_PATHS:
        if not has_dangerous_path(forbidden_values, dangerous):
            recorder.error(
                f"{rel(path)} forbidden_paths missing dangerous area: {dangerous}"
            )


def check_example_task_spec_text(path: Path, recorder: Recorder) -> None:
    text = path.read_text(encoding="utf-8")
    recorder.warn(
        f"Using conservative text checks for {rel(path)} because strict YAML "
        "parsing is unavailable."
    )

    for key in EXAMPLE_TASK_SPEC_REQUIRED_KEYS:
        if not text_has_key(text, key):
            recorder.error(f"{rel(path)} missing required example TaskSpec field: {key}")

    risk_level = text_value_for_key(text, "risk_level")
    if risk_level not in ALLOWED_RISK_LEVELS:
        recorder.error(
            f"{rel(path)} has invalid risk_level: {risk_level!r}; "
            "expected low, medium, or high."
        )

    codex_audit_required = text_value_for_key(text, "codex_audit_required")
    if codex_audit_required not in ALLOWED_CODEX_AUDIT_REQUIRED:
        recorder.error(
            f"{rel(path)} has invalid codex_audit_required: "
            f"{codex_audit_required!r}; expected none, review, "
            "adversarial-review, or manual."
        )

    forbidden_values = text_list_under_key(text, "forbidden_paths")
    for dangerous in DANGEROUS_FORBIDDEN_PATHS:
        if not has_dangerous_path(forbidden_values, dangerous):
            recorder.error(
                f"{rel(path)} forbidden_paths missing dangerous area: {dangerous}"
            )


def role_has_forbidden_path(role_values: list[str], inherited_values: list[str], required: str) -> bool:
    combined = [*role_values, *inherited_values]
    return has_dangerous_path(combined, required) or strings_contain_token(combined, required)


def check_role_common(role_id: str, role: dict[str, Any], inherited_forbidden: list[str], recorder: Recorder) -> None:
    for field in sorted(ROLE_REQUIRED_FIELDS):
        if field not in role:
            recorder.error(f"Role {role_id} missing required field: {field}")

    write_permission = role.get("write_permission")
    if write_permission not in ALLOWED_ROLE_WRITE_PERMISSIONS:
        recorder.error(
            f"Role {role_id} has invalid write_permission: {write_permission!r}; "
            "expected none, scoped, or forbidden."
        )

    for access_field in ["board_access", "server_access"]:
        access_value = role.get(access_field)
        if access_value not in ALLOWED_ROLE_ACCESS:
            recorder.error(
                f"Role {role_id} has invalid {access_field}: {access_value!r}; "
                "expected forbidden, requires_human_gate, or allowed_by_task_spec."
            )
        if access_value != "forbidden":
            requires_gate = role.get("requires_human_gate") is True
            task_spec_limited = access_value == "allowed_by_task_spec"
            if not requires_gate and not task_spec_limited:
                recorder.error(
                    f"Role {role_id} {access_field} is {access_value!r} but "
                    "requires_human_gate is not true and access is not allowed_by_task_spec."
                )

    review_mode = role.get("review_mode")
    if review_mode not in ALLOWED_ROLE_REVIEW_MODES:
        recorder.error(
            f"Role {role_id} has invalid review_mode: {review_mode!r}; "
            "expected one of: " + ", ".join(sorted(ALLOWED_ROLE_REVIEW_MODES))
        )

    backend_agent = role.get("backend_agent")
    if backend_agent not in ALLOWED_BACKEND_AGENTS:
        recorder.error(f"Role {role_id} has invalid backend_agent: {backend_agent!r}")

    expected_backend = EXPECTED_ROLE_BACKENDS.get(role_id)
    if role_id in EXPECTED_ROLE_BACKENDS and backend_agent != expected_backend:
        recorder.error(
            f"Role {role_id} backend_agent should be {expected_backend!r}, got {backend_agent!r}."
        )

    if role_id == "adversarial_reviewer" and review_mode != "adversarial":
        recorder.error("Role adversarial_reviewer review_mode should be 'adversarial'.")

    max_repair_depth = role.get("max_repair_depth")
    if role_id in NO_REPAIR_ROLES and max_repair_depth != 0:
        recorder.error(f"Role {role_id} should not allow repair; max_repair_depth must be 0.")
    if role_id == "implementer" and max_repair_depth not in {0, None}:
        recorder.error("Role implementer should not self-recursively repair; max_repair_depth must be 0.")
    if role_id == "repairer":
        if max_repair_depth != 1:
            recorder.error("Role repairer default max_repair_depth should be 1.")
        absolute_max = role.get("absolute_max_repair_depth")
        if absolute_max is not None and absolute_max > 2:
            recorder.error("Role repairer absolute_max_repair_depth must not exceed 2.")

    role_forbidden_values = flatten_strings(role.get("forbidden_paths"))
    for required_path in ROLE_FORBIDDEN_PATH_DEFAULTS:
        if not role_has_forbidden_path(role_forbidden_values, inherited_forbidden, required_path):
            recorder.error(
                f"Role {role_id} forbidden_paths or inherited policy missing default forbidden area: "
                f"{required_path}"
            )

    allowed_tools = flatten_strings(role.get("allowed_tools"))
    forbidden_tools = flatten_strings(role.get("forbidden_tools"))

    if role_id in WRITE_FORBIDDEN_ROLES:
        if write_permission != "none":
            recorder.error(f"Role {role_id} is read-only and must have write_permission: none.")
        for write_tool in ["apply_patch", "file_write"]:
            if tool_list_allows(allowed_tools, write_tool):
                recorder.error(f"Role {role_id} must not allow write tool: {write_tool}.")

    if role_id in NO_GIT_WRITE_ROLES:
        for git_tool in ["git_add", "git_commit"]:
            if tool_list_allows(allowed_tools, git_tool):
                recorder.error(f"Role {role_id} must not allow {git_tool}.")
            if not tool_list_allows(forbidden_tools, git_tool):
                recorder.error(f"Role {role_id} forbidden_tools should include {git_tool}.")

    if role_id == "validator":
        if any(tool_list_allows(allowed_tools, tool) for tool in ["apply_patch", "file_write"]):
            recorder.error("Role validator must not allow write tools.")
        if tool_list_allows(allowed_tools, "automatic_repair"):
            recorder.error("Role validator must not allow automatic_repair.")
        if not tool_list_allows(forbidden_tools, "automatic_repair"):
            recorder.error("Role validator forbidden_tools should include automatic_repair.")

    for risky_tool in ["hdc", "ssh", "scp", "app_server", "collection_runner"]:
        for allowed_tool in allowed_tools:
            lowered = allowed_tool.lower()
            if risky_tool in lowered and "taskspec" not in lowered and "human gate" not in lowered:
                recorder.error(
                    f"Role {role_id} allowed_tools appears to allow {risky_tool} without TaskSpec/human gate: "
                    f"{allowed_tool!r}"
                )


def check_roles_registry_parsed(path: Path, data: dict[str, Any], recorder: Recorder) -> None:
    roles_value = data.get("roles")
    if not isinstance(roles_value, list):
        recorder.error(f"{rel(path)} missing top-level roles list.")
        return

    role_map: dict[str, dict[str, Any]] = {}
    for index, role in enumerate(roles_value):
        if not isinstance(role, dict):
            recorder.error(f"{rel(path)} roles[{index}] is not an object.")
            continue
        role_id = role.get("role_id")
        if not isinstance(role_id, str):
            recorder.error(f"{rel(path)} roles[{index}] missing string role_id.")
            continue
        if role_id in role_map:
            recorder.error(f"{rel(path)} duplicate role_id: {role_id}")
        role_map[role_id] = role

    missing_roles = sorted(ROLE_REQUIRED_IDS - set(role_map))
    for role_id in missing_roles:
        recorder.error(f"{rel(path)} missing required role: {role_id}")

    extra_roles = sorted(set(role_map) - ROLE_REQUIRED_IDS)
    for role_id in extra_roles:
        recorder.warn(f"{rel(path)} contains extra role not in P1-2 required set: {role_id}")

    inherited_forbidden = flatten_strings(nested_get(data, ["global_policy", "default_forbidden_paths"]))
    for role_id in sorted(role_map):
        check_role_common(role_id, role_map[role_id], inherited_forbidden, recorder)

    recorder.note(f"Checked roles registry roles: {len(role_map)}")


def check_roles_registry_text(path: Path, recorder: Recorder) -> None:
    text = path.read_text(encoding="utf-8")
    recorder.warn(
        f"Using conservative text checks for {rel(path)} because strict YAML "
        "parsing is unavailable."
    )

    blocks = text_role_blocks(text)
    missing_roles = sorted(ROLE_REQUIRED_IDS - set(blocks))
    for role_id in missing_roles:
        recorder.error(f"{rel(path)} missing required role: {role_id}")

    inherited_forbidden = text_list_under_key(text, "default_forbidden_paths")
    for role_id, block in sorted(blocks.items()):
        role: dict[str, Any] = {}
        for field in ROLE_REQUIRED_FIELDS:
            if not text_has_key(block, field):
                recorder.error(f"Role {role_id} missing required field: {field}")
                continue
            scalar = text_scalar_for_key(block, field)
            list_value = text_list_under_key(block, field)
            role[field] = list_value if list_value else scalar

        absolute_max = text_scalar_for_key(block, "absolute_max_repair_depth")
        if absolute_max is not None:
            role["absolute_max_repair_depth"] = absolute_max

        check_role_common(role_id, role, inherited_forbidden, recorder)

    recorder.note(f"Checked roles registry roles: {len(blocks)}")


def check_roles_registry(recorder: Recorder) -> None:
    roles_path = HARNESS_ROOT / "templates/roles.yaml"
    if not roles_path.is_file():
        recorder.error("Missing roles registry template: templates/roles.yaml")
        return

    parsed_ok, data = load_yaml_if_available(roles_path, recorder)
    if parsed_ok and isinstance(data, dict):
        check_roles_registry_parsed(roles_path, data, recorder)
    else:
        check_roles_registry_text(roles_path, recorder)


def check_routing_note_template(recorder: Recorder) -> None:
    routing_note_path = HARNESS_ROOT / "templates/routing_note.md"
    if not routing_note_path.is_file():
        recorder.error("Missing routing note template: templates/routing_note.md")
        return

    text = routing_note_path.read_text(encoding="utf-8", errors="replace")
    headings = markdown_headings(text)
    full_text = text.lower()

    for section_name, aliases in ROUTING_NOTE_REQUIRED_SECTIONS.items():
        if not any(alias in headings for alias in aliases):
            recorder.error(
                "Routing note template missing required section or equivalent heading: "
                f"{section_name}"
            )

    for term in ROUTING_NOTE_NON_EXECUTION_TERMS:
        if term not in full_text:
            recorder.error(
                "Routing note template missing non-execution statement term: "
                f"{term}"
            )

    recorder.note("Checked routing note template sections.")


def load_consistency_role_map(recorder: Recorder) -> dict[str, dict[str, Any]]:
    roles_path = HARNESS_ROOT / "templates/roles.yaml"
    if not roles_path.is_file():
        recorder.error("Consistency check missing roles registry: templates/roles.yaml")
        return {}

    parsed_ok, data = load_yaml_if_available(roles_path, recorder)
    if parsed_ok and isinstance(data, dict):
        roles_value = data.get("roles")
        if not isinstance(roles_value, list):
            recorder.error("Consistency check could not find top-level roles list.")
            return {}
        role_map: dict[str, dict[str, Any]] = {}
        for role in roles_value:
            if isinstance(role, dict) and isinstance(role.get("role_id"), str):
                role_map[role["role_id"]] = role
        return role_map

    text = roles_path.read_text(encoding="utf-8", errors="replace")
    blocks = text_role_blocks(text)
    role_map = {}
    for role_id, block in blocks.items():
        role: dict[str, Any] = {
            "role_id": role_id,
            "backend_agent": text_scalar_for_key(block, "backend_agent"),
            "review_mode": text_scalar_for_key(block, "review_mode"),
            "max_repair_depth": text_scalar_for_key(block, "max_repair_depth"),
        }
        absolute_max = text_scalar_for_key(block, "absolute_max_repair_depth")
        if absolute_max is not None:
            role["absolute_max_repair_depth"] = absolute_max
        role_map[role_id] = role
    return role_map


def routing_note_line_for_role(text: str, role_id: str) -> str | None:
    role_token = f"`{role_id}`"
    for line in text.splitlines():
        if role_token in line and "|" in line:
            return line
    return None


def routing_note_role_references(text: str) -> set[str]:
    role_ids: set[str] = set()
    role_like_pattern = re.compile(r"^[a-z][a-z0-9_]*(?:_reviewer|_worker)?$")

    for token in re.findall(r"`([a-z][a-z0-9_]*?)`", text):
        if (
            token in ROLE_REQUIRED_IDS
            or token.endswith("_reviewer")
            or token.endswith("_worker")
            or token == "orchestrator"
        ):
            role_ids.add(token)

    for line in text.splitlines():
        if "|" not in line:
            continue
        cells = [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]
        if not cells:
            continue
        candidate = cells[0]
        if candidate.lower() in {"harness role", "---"}:
            continue
        if role_like_pattern.fullmatch(candidate) and (
            "project-" in line or candidate in ROLE_REQUIRED_IDS
        ):
            role_ids.add(candidate)

    return role_ids


def check_consistency_role_mappings(
    role_map: dict[str, dict[str, Any]], routing_text: str, recorder: Recorder
) -> None:
    if not role_map:
        return

    missing_roles = sorted(ROLE_REQUIRED_IDS - set(role_map))
    for role_id in missing_roles:
        recorder.error(f"Consistency check missing role in roles.yaml: {role_id}")

    routing_role_tokens = routing_note_role_references(routing_text)
    unknown_tokens = sorted(routing_role_tokens - set(role_map))
    for role_id in unknown_tokens:
        recorder.error(
            f"Routing note references role_id not present in roles.yaml: {role_id}"
        )

    for role_id, expected_backend in EXPECTED_ROLE_BACKENDS.items():
        role = role_map.get(role_id)
        if not role:
            continue

        actual_backend = role.get("backend_agent")
        if actual_backend != expected_backend:
            recorder.error(
                f"Consistency check backend mismatch in roles.yaml for {role_id}: "
                f"expected {expected_backend!r}, got {actual_backend!r}."
            )

        line = routing_note_line_for_role(routing_text, role_id)
        if line is None:
            recorder.error(
                f"Routing note backend mapping table missing role: {role_id}"
            )
            continue

        line_lower = line.lower()
        if expected_backend is None:
            if "null" not in line_lower:
                recorder.error(
                    f"Routing note backend mapping for {role_id} should be null."
                )
        elif expected_backend not in line:
            recorder.error(
                f"Routing note backend mapping for {role_id} should mention "
                f"{expected_backend}."
            )

        if role_id == "adversarial_reviewer":
            role_review_mode = role.get("review_mode")
            if role_review_mode != "adversarial":
                recorder.error(
                    "Consistency check roles.yaml adversarial_reviewer "
                    "review_mode should be adversarial."
                )
            if "adversarial" not in line_lower:
                recorder.error(
                    "Routing note backend mapping for adversarial_reviewer "
                    "should mention review_mode=adversarial."
                )


def check_consistency_repair_depth(
    role_map: dict[str, dict[str, Any]], routing_text: str, recorder: Recorder
) -> None:
    repairer = role_map.get("repairer", {})
    repairer_depth = coerce_int(repairer.get("max_repair_depth"))
    absolute_max = coerce_int(repairer.get("absolute_max_repair_depth"))

    if repairer_depth != 1:
        recorder.error(
            "Consistency check expects roles.yaml repairer max_repair_depth to be 1."
        )
    if absolute_max is not None and absolute_max > 2:
        recorder.error(
            "Consistency check expects roles.yaml repairer absolute_max_repair_depth <= 2."
        )

    max_allowed = absolute_max if absolute_max is not None else repairer_depth
    if max_allowed is None:
        max_allowed = 1

    repair_depth_values = [
        int(match.group(1))
        for match in re.finditer(
            r"(?:max_repair_depth|repair(?:er)? depth|absolute maximum)"
            r"[^0-9]{0,80}(\d+)",
            routing_text.lower(),
        )
    ]
    if not repair_depth_values:
        recorder.error("Routing note does not declare repair depth policy.")
        return

    for value in repair_depth_values:
        if value > max_allowed:
            recorder.error(
                f"Routing note repair depth {value} exceeds roles.yaml maximum {max_allowed}."
            )


def check_consistency_routing_policy(routing_text: str, recorder: Recorder) -> None:
    routing_lower = routing_text.lower()

    for term in CONSISTENCY_NON_EXECUTION_TERMS:
        if term not in routing_lower:
            recorder.error(
                f"Routing note missing required non-execution statement: {term}"
            )

    if "does not spawn agents" not in routing_lower and "no agents are spawned" not in routing_lower:
        recorder.error("Routing note must explicitly state that no agents are spawned.")

    for label, phrases in CONSISTENCY_NON_EXECUTION_PHRASES.items():
        if not any(phrase in routing_lower for phrase in phrases):
            recorder.error(f"Routing note missing explicit non-execution phrase: {label}")

    external_mentions = [
        term for term in CONSISTENCY_EXTERNAL_TERMS if term in routing_lower
    ]
    if external_mentions and "human gate" not in routing_lower:
        recorder.error(
            "Routing note mentions external/runtime actions but does not mark human gate."
        )


def check_consistency_task_spec_parsed(data: dict[str, Any], role_map: dict[str, dict[str, Any]], recorder: Recorder) -> None:
    task_type = nested_get(data, ["intent", "task_type"])
    if task_type != "scaffold":
        recorder.error(
            "Rehearsal TaskSpec should remain task_type=scaffold, got "
            f"{task_type!r}."
        )

    executable = nested_get(data, ["role_selection_rehearsal", "executable"])
    if executable is not False:
        recorder.error("Rehearsal TaskSpec role_selection_rehearsal.executable must be false.")

    manual_only = nested_get(data, ["validation", "manual_only"])
    no_runtime_execution = nested_get(data, ["validation", "no_runtime_execution"])
    if manual_only is not True:
        recorder.error("Rehearsal TaskSpec validation.manual_only must be true.")
    if no_runtime_execution is not True:
        recorder.error("Rehearsal TaskSpec validation.no_runtime_execution must be true.")

    for flag in CONSISTENCY_TASKSPEC_FALSE_FLAGS:
        value = nested_get(data, ["limits", flag])
        if value is not False:
            recorder.error(f"Rehearsal TaskSpec limits.{flag} must be false.")

    for route_key in CONSISTENCY_ROUTE_KEYS:
        route_roles = nested_get(data, ["role_selection_rehearsal", route_key, "roles"])
        if not isinstance(route_roles, list):
            recorder.error(f"Rehearsal TaskSpec {route_key}.roles must be a list.")
            continue
        for role_ref in route_roles:
            role_id = normalize_role_reference(str(role_ref))
            if role_id not in role_map:
                recorder.error(
                    f"Rehearsal TaskSpec {route_key} references role not in roles.yaml: "
                    f"{role_id}"
                )


def check_consistency_task_spec_text(path: Path, role_map: dict[str, dict[str, Any]], recorder: Recorder) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    lowered = text.lower()

    for required in ["rehearsal", "not an execution plan", "must not be consumed by a runner"]:
        if required not in lowered:
            recorder.error(
                f"Rehearsal TaskSpec missing non-executable semantic text: {required}"
            )

    if text_scalar_for_key(text, "executable") is not False:
        recorder.error("Rehearsal TaskSpec executable must be false.")
    if text_scalar_for_key(text, "manual_only") is not True:
        recorder.error("Rehearsal TaskSpec manual_only must be true.")
    if text_scalar_for_key(text, "no_runtime_execution") is not True:
        recorder.error("Rehearsal TaskSpec no_runtime_execution must be true.")

    for flag in CONSISTENCY_TASKSPEC_FALSE_FLAGS:
        if text_scalar_for_key(text, flag) is not False:
            recorder.error(f"Rehearsal TaskSpec {flag} must be false.")

    for role_id in ROLE_REQUIRED_IDS:
        if f'"{role_id}"' in text or f"`{role_id}`" in text:
            if role_id not in role_map:
                recorder.error(
                    f"Rehearsal TaskSpec references role not in roles.yaml: {role_id}"
                )


def check_p1_rehearsal_consistency(recorder: Recorder) -> None:
    roles_path = HARNESS_ROOT / "templates/roles.yaml"
    task_spec_path = HARNESS_ROOT / CONSISTENCY_TASK_SPEC
    routing_note_path = HARNESS_ROOT / CONSISTENCY_ROUTING_NOTE

    for path in [roles_path, task_spec_path, routing_note_path]:
        if not path.is_file():
            recorder.error(f"Consistency check missing required file: {rel(path)}")
            return

    role_map = load_consistency_role_map(recorder)
    routing_text = routing_note_path.read_text(encoding="utf-8", errors="replace")

    check_consistency_role_mappings(role_map, routing_text, recorder)
    check_consistency_repair_depth(role_map, routing_text, recorder)
    check_consistency_routing_policy(routing_text, recorder)

    parsed_ok, task_spec = load_yaml_if_available(task_spec_path, recorder)
    if parsed_ok and isinstance(task_spec, dict):
        check_consistency_task_spec_parsed(task_spec, role_map, recorder)
    else:
        recorder.warn(
            "Using conservative text checks for P1 rehearsal TaskSpec consistency "
            "because strict YAML parsing is unavailable."
        )
        check_consistency_task_spec_text(task_spec_path, role_map, recorder)

    recorder.note("Checked P1 rehearsal TaskSpec, roles registry, and routing note consistency.")


def check_schema_json(recorder: Recorder) -> None:
    schema_path = HARNESS_ROOT / "schemas/task_spec.schema.json"
    if not schema_path.is_file():
        return
    try:
        data = json.loads(schema_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        recorder.error(f"JSON schema parse failed for {rel(schema_path)}: {exc}")
        return

    if data.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        recorder.warn("task_spec.schema.json does not declare draft 2020-12.")


def check_policy_keywords(recorder: Recorder) -> None:
    scan_exts = {".md", ".yaml", ".json"}
    for path in sorted(HARNESS_ROOT.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in scan_exts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for line_number, line in enumerate(text.splitlines(), start=1):
            lowered = line.lower()
            for term in POLICY_RISK_TERMS:
                if term in lowered:
                    recorder.warn(
                        "Policy risk keyword mention: "
                        f"{rel(path)}:{line_number}: {term}"
                    )


def check_python_execution_entries(recorder: Recorder) -> None:
    for path in sorted((HARNESS_ROOT / "tools").glob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if line.lstrip().startswith("#"):
                continue
            for pattern in PY_EXECUTION_PATTERNS:
                if pattern.search(line):
                    recorder.warn(
                        "Potential execution entry in tool code: "
                        f"{rel(path)}:{line_number}: {line.strip()}"
                    )


def check_read_only_boundary(recorder: Recorder) -> None:
    for path in sorted(HARNESS_ROOT.rglob("*")):
        if not path.is_file():
            continue
        try:
            path.relative_to(HARNESS_ROOT)
        except ValueError:
            recorder.error(f"Internal error: path escaped harness root: {path}")
    recorder.note("Read-only boundary: inspected .codex-harness files only.")


def run_scaffold_checks(recorder: Recorder) -> None:
    check_required_paths(recorder)
    parsed_yaml = check_yaml_templates(recorder)
    check_task_spec_core_fields(parsed_yaml, recorder)
    check_forbidden_paths(parsed_yaml, recorder)
    check_schema_json(recorder)
    check_policy_keywords(recorder)
    check_python_execution_entries(recorder)
    check_read_only_boundary(recorder)


def run_example_checks(recorder: Recorder) -> None:
    check_example_task_specs(recorder)
    check_policy_keywords(recorder)
    check_read_only_boundary(recorder)


def run_roles_checks(recorder: Recorder) -> None:
    check_roles_registry(recorder)
    check_policy_keywords(recorder)
    check_read_only_boundary(recorder)


def run_routing_checks(recorder: Recorder) -> None:
    check_routing_note_template(recorder)
    check_policy_keywords(recorder)
    check_read_only_boundary(recorder)


def run_consistency_checks(recorder: Recorder) -> None:
    check_p1_rehearsal_consistency(recorder)
    check_policy_keywords(recorder)
    check_read_only_boundary(recorder)


def print_report(recorder: Recorder) -> None:
    if recorder.errors:
        result = "FAIL"
    elif recorder.warnings:
        result = "PASS_WITH_WARNINGS"
    else:
        result = "PASS"

    print(f"RESULT: {result}")
    print()

    print("Errors:")
    if recorder.errors:
        for message in recorder.errors:
            print(f"- {message}")
    else:
        print("- none")
    print()

    print("Warnings:")
    if recorder.warnings:
        for message in recorder.warnings:
            print(f"- {message}")
    else:
        print("- none")
    print()

    print("Info:")
    if recorder.info:
        for message in recorder.info:
            print(f"- {message}")
    else:
        print("- none")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate the documentation-only .codex-harness P0 scaffold."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--scaffold-only",
        action="store_true",
        help="Only check core scaffold directories, docs, templates, schema, README, and risk keywords.",
    )
    mode.add_argument(
        "--examples-only",
        action="store_true",
        help="Only check example TaskSpec YAML files under .codex-harness/tasks/examples.",
    )
    mode.add_argument(
        "--roles-only",
        action="store_true",
        help="Only check the non-executable roles registry under .codex-harness/templates/roles.yaml.",
    )
    mode.add_argument(
        "--routing-only",
        action="store_true",
        help="Only check the non-executable routing note template under .codex-harness/templates/routing_note.md.",
    )
    mode.add_argument(
        "--consistency-only",
        action="store_true",
        help="Only check P1 rehearsal consistency across TaskSpec, roles.yaml, and routing note.",
    )
    parser.add_argument(
        "--fail-on-warnings",
        action="store_true",
        help="Return a non-zero exit code when warnings are present.",
    )
    args = parser.parse_args()

    recorder = Recorder()
    recorder.note(f"Harness root: {HARNESS_ROOT}")

    if args.scaffold_only:
        recorder.note("Mode: scaffold-only")
        run_scaffold_checks(recorder)
    elif args.examples_only:
        recorder.note("Mode: examples-only")
        run_example_checks(recorder)
    elif args.roles_only:
        recorder.note("Mode: roles-only")
        run_roles_checks(recorder)
    elif args.routing_only:
        recorder.note("Mode: routing-only")
        run_routing_checks(recorder)
    elif args.consistency_only:
        recorder.note("Mode: consistency-only")
        run_consistency_checks(recorder)
    else:
        recorder.note("Mode: scaffold-examples-roles-routing-and-consistency")
        run_scaffold_checks(recorder)
        check_example_task_specs(recorder)
        check_roles_registry(recorder)
        check_routing_note_template(recorder)
        check_p1_rehearsal_consistency(recorder)

    print_report(recorder)

    if recorder.errors:
        return 1
    if args.fail_on_warnings and recorder.warnings:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
