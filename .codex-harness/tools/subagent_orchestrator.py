#!/usr/bin/env python3
"""Codex harness subagent orchestration probe and assisted runner.

This tool is deliberately strict about vocabulary:
- app-server bridge means Codex app-server / VSCode-source thread execution.
- project-agent spawn means observable project subagent metadata such as
  thread_spawn, agent_role, agent_path, or CollabAgentTool=spawnAgent.
- generic codex exec is never reported as project-agent spawn.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
HARNESS_ROOT = ROOT / ".codex-harness"
REPORTS_ROOT = HARNESS_ROOT / "reports"
TOOLS_ROOT = HARNESS_ROOT / "tools"

sys.path.insert(0, str(TOOLS_ROOT))
from dispatch_subagents import (  # noqa: E402
    BACKEND_FALLBACKS,
    build_bundle,
    build_prompt,
    parse_roles,
    parse_task_spec,
    read_text_file,
    select_roles,
)


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

SUPPORTED_WORKFLOWS = ("explorer-reviewer", "explorer-implementer-reviewer")
SUPPORTED_MODES = ("vscode-plugin", "vscode-plugin-assisted", "probe-only")


class OrchestratorError(Exception):
    """User-facing orchestrator error."""


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
        raise OrchestratorError(f"Refusing to {purpose} outside repo: {path}") from exc
    rel_lower = rel.lower().replace("\\", "/")
    if rel_lower.startswith(DEFAULT_FORBIDDEN_PREFIXES):
        raise OrchestratorError(f"Refusing to {purpose} forbidden path: {rel}")
    return resolved


def ensure_run_dir(path: Path) -> Path:
    resolved = ensure_repo_path(path, "use run-dir")
    try:
        resolved.relative_to(REPORTS_ROOT.resolve())
    except ValueError as exc:
        raise OrchestratorError(
            f"run-dir must be under .codex-harness/reports/: {repo_rel(resolved)}"
        ) from exc
    return resolved


def run_cmd(args: list[str], timeout: int = 30) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            args,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "args": args,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "args": args,
            "returncode": 124,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
            "timed_out": True,
        }


def command_excerpt(result: dict[str, Any], max_chars: int = 2200) -> str:
    text = (result.get("stdout") or "") + (result.get("stderr") or "")
    text = text.replace("\x1b", "")
    if len(text) <= max_chars:
        return text.rstrip()
    return text[:max_chars].rstrip() + "\n...[truncated]"


def detect_bridge_signals(text: str) -> dict[str, bool]:
    return {
        "app_server_started": "method\": \"initialize\"" in text
        or "initialize response:" in text
        or "thread/start response:" in text,
        "vscode_source_thread": '"source": "vscode"' in text or "source: VsCode" in text,
        "turn_completed": "turn/completed" in text or "turn/completed notification" in text,
        "turn_failed": "turn/completed notification: Failed" in text
        or '"status": "failed"' in text,
        "model_version_blocker": "requires a newer version of Codex" in text,
        "spawn_agent_tool": "spawnAgent" in text,
        "thread_spawn_metadata": "thread_spawn" in text
        or "agent_role" in text
        or "agent_path" in text,
        "project_agent_name": any(
            name in text
            for name in (
                "project-explorer",
                "project-implementer",
                "project-reviewer",
                "project-repairer",
            )
        ),
    }


def load_task(task_spec: Path, workflow: str) -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[str]]:
    task_text = read_text_file(task_spec)
    roles_text = read_text_file(HARNESS_ROOT / "templates" / "roles.yaml")
    task = parse_task_spec(task_spec, task_text)
    task["workflow"] = workflow
    roles = parse_roles(roles_text)
    sequence = select_roles(task, None, None, workflow)
    return task, roles, sequence


def render_generated_prompts(
    task: dict[str, Any],
    roles: dict[str, dict[str, Any]],
    sequence: list[str],
    prior_results: dict[str, str],
) -> str:
    parts = [
        "# Generated Subagent Prompts",
        "",
        f"generated_at: {_dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()}",
        f"task_spec: {task['path']}",
        f"workflow: {task['workflow']}",
        "",
    ]
    for index, role_id in enumerate(sequence, start=1):
        prompt = build_prompt(role_id, roles.get(role_id, {}), task)
        if role_id == "reviewer" and prior_results:
            prompt += "\n#### Prior Agent Results\n"
            for agent_name, result_text in prior_results.items():
                prompt += f"\n##### {agent_name}\n\n{result_text.rstrip()}\n"
        backend = roles.get(role_id, {}).get("backend_agent", BACKEND_FALLBACKS.get(role_id))
        parts.extend(
            [
                f"## {index:02d} {role_id} -> {backend}",
                "",
                prompt.rstrip(),
                "",
            ]
        )
    return "\n".join(parts).rstrip() + "\n"


def result_file_for_role(role_id: str) -> str | None:
    backend = BACKEND_FALLBACKS.get(role_id)
    if backend is None:
        return None
    return AGENT_RESULT_FILES.get(backend)


def run_aggregator(run_dir: Path, strict: bool) -> dict[str, Any]:
    args = [
        sys.executable,
        str(HARNESS_ROOT / "tools" / "aggregate_subagent_results.py"),
        "--run-dir",
        repo_rel(run_dir),
        "--dry-run",
        "--out-file",
        repo_rel(run_dir / "aggregation_summary.md"),
    ]
    if strict:
        args.append("--strict")
    return run_cmd(args, timeout=30)


def probe_vscode_plugin_bridge(run_dir: Path, live: bool) -> tuple[list[dict[str, Any]], dict[str, bool]]:
    probes: list[dict[str, Any]] = []
    for args in (
        ["codex", "app-server", "--help"],
        ["codex", "debug", "app-server", "send-message-v2", "--help"],
        ["codex", "features", "list"],
    ):
        probes.append(run_cmd(args, timeout=20))

    schema_path = run_dir / "app_server_schema_probe" / "codex_app_server_protocol.v2.schemas.json"
    schema_text = ""
    if schema_path.is_file():
        schema_text = schema_path.read_text(encoding="utf-8", errors="replace")
    schema_signals = detect_bridge_signals(schema_text)

    live_signals = {
        "app_server_started": False,
        "vscode_source_thread": False,
        "turn_completed": False,
        "turn_failed": False,
        "model_version_blocker": False,
        "spawn_agent_tool": False,
        "thread_spawn_metadata": schema_signals["thread_spawn_metadata"],
        "project_agent_name": False,
    }

    if live:
        live_probe = run_cmd(
            [
                "codex",
                "debug",
                "app-server",
                "send-message-v2",
                "Reply with exactly: APP_SERVER_SMOKE_OK",
            ],
            timeout=120,
        )
        probes.append(live_probe)
        live_text = (live_probe.get("stdout") or "") + (live_probe.get("stderr") or "")
        live_signals.update(detect_bridge_signals(live_text))

    feature_text = "\n".join(command_excerpt(item, 20000) for item in probes)
    signals = {
        "app_server_help": "generate-json-schema" in feature_text
        and "stdio://" in feature_text,
        "send_message_v2_help": "send-message-v2" in feature_text
        and "<USER_MESSAGE>" in feature_text,
        "multi_agent_feature": bool(re.search(r"multi_agent\s+stable\s+true", feature_text)),
        "schema_has_collab_spawn_tool": "spawnAgent" in schema_text,
        "schema_has_thread_spawn_metadata": schema_signals["thread_spawn_metadata"],
        "live_app_server_started": live_signals["app_server_started"],
        "live_vscode_source_thread": live_signals["vscode_source_thread"],
        "live_turn_completed": live_signals["turn_completed"]
        and not live_signals["turn_failed"],
        "live_model_version_blocker": live_signals["model_version_blocker"],
        "live_project_agent_spawn_evidence": live_signals["spawn_agent_tool"]
        and live_signals["thread_spawn_metadata"]
        and live_signals["project_agent_name"],
    }
    return probes, signals


def render_trace(
    task: dict[str, Any],
    sequence: list[str],
    mode: str,
    probes: list[dict[str, Any]],
    signals: dict[str, bool],
    bridge_status: str,
) -> str:
    lines = [
        "# Subagent Orchestrator Trace",
        "",
        f"mode: {mode}",
        f"bridge_status: {bridge_status}",
        f"task_spec: {task['path']}",
        f"workflow: {task['workflow']}",
        "",
        "selected_roles:",
        *[f"- {role_id} -> {BACKEND_FALLBACKS.get(role_id)}" for role_id in sequence],
        "",
        "bridge_signals:",
        *[f"- {key}: {str(value).lower()}" for key, value in signals.items()],
        "",
        "decision:",
    ]
    if bridge_status == "READY":
        lines.append("- app-server project-agent spawn evidence was observed.")
    elif bridge_status == "BLOCKED":
        lines.append("- app-server protocol exposes subagent metadata, but no callable external spawn endpoint was found.")
        if signals.get("live_turn_completed"):
            lines.append(
                "- live app-server debug turn completed, but it did not launch or capture a project-agent spawn."
            )
        else:
            lines.append("- live app-server debug turn did not complete in this environment.")
        lines.append("- no project-explorer/project-reviewer subagent result was captured.")
    else:
        lines.append("- vscode-plugin-assisted prompts were generated for manual UI execution.")

    lines.extend(["", "commands:"])
    for probe in probes:
        lines.extend(
            [
                f"## {' '.join(probe['args'])}",
                f"returncode: {probe['returncode']}",
                f"timed_out: {str(probe['timed_out']).lower()}",
                "excerpt:",
                "```text",
                command_excerpt(probe),
                "```",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate serial subagent prompts and probe VSCode/app-server orchestration."
    )
    parser.add_argument("--task-spec", required=True, help="TaskSpec path.")
    parser.add_argument("--run-dir", required=True, help="Run directory under .codex-harness/reports/.")
    parser.add_argument(
        "--workflow",
        choices=SUPPORTED_WORKFLOWS,
        default="explorer-reviewer",
    )
    parser.add_argument(
        "--mode",
        choices=SUPPORTED_MODES,
        default="vscode-plugin",
        help="vscode-plugin probes app-server; assisted only generates prompts and trace.",
    )
    parser.add_argument(
        "--live-app-server-probe",
        action="store_true",
        help="Run a tiny app-server debug turn to check current runtime viability.",
    )
    parser.add_argument("--strict", action="store_true", help="Pass --strict to aggregator.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        task_spec = ensure_repo_path(ROOT / args.task_spec, "read TaskSpec")
        if not task_spec.is_file():
            raise OrchestratorError(f"TaskSpec not found: {repo_rel(task_spec)}")
        run_dir = ensure_run_dir(ROOT / args.run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)

        task, roles, sequence = load_task(task_spec, args.workflow)
        warnings = [
            "Using app-server/VScode bridge probe; generic codex exec is not used.",
            "No project-agent spawn is claimed unless thread_spawn or CollabAgentTool evidence is observed.",
        ]
        prompt_bundle = build_bundle(task, roles, sequence, warnings, args.mode)
        (run_dir / "prompt_bundle.md").write_text(prompt_bundle, encoding="utf-8")
        generated = render_generated_prompts(task, roles, sequence, {})
        (run_dir / "generated_prompts.md").write_text(generated, encoding="utf-8")

        probes: list[dict[str, Any]] = []
        signals: dict[str, bool] = {}
        bridge_status = "ASSISTED_ONLY"
        if args.mode in {"vscode-plugin", "probe-only"}:
            probes, signals = probe_vscode_plugin_bridge(run_dir, args.live_app_server_probe)
            bridge_status = "READY" if signals.get("live_project_agent_spawn_evidence") else "BLOCKED"
        else:
            signals = {
                "app_server_help": False,
                "send_message_v2_help": False,
                "multi_agent_feature": False,
                "schema_has_collab_spawn_tool": False,
                "schema_has_thread_spawn_metadata": False,
                "live_app_server_started": False,
                "live_vscode_source_thread": False,
                "live_turn_completed": False,
                "live_model_version_blocker": False,
                "live_project_agent_spawn_evidence": False,
            }

        trace = render_trace(task, sequence, args.mode, probes, signals, bridge_status)
        (run_dir / "orchestrator_trace.md").write_text(trace, encoding="utf-8")

        existing_expected = [
            name
            for role_id in sequence
            for name in [result_file_for_role(role_id)]
            if name and (run_dir / name).is_file()
        ]
        if existing_expected:
            aggregate_result = run_aggregator(run_dir, args.strict)
            if aggregate_result["returncode"] != 0 and not (run_dir / "aggregation_summary.md").exists():
                (run_dir / "aggregation_summary.md").write_text(
                    command_excerpt(aggregate_result, 10000),
                    encoding="utf-8",
                )
        else:
            (run_dir / "aggregation_summary.md").write_text(
                "# Aggregation Summary\n\n"
                "overall_status: NOT_RUN\n\n"
                "Reason: no automatic project-agent result files were captured.\n",
                encoding="utf-8",
            )

        print(trace, end="")
        return 0 if bridge_status == "READY" else 4
    except OrchestratorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
