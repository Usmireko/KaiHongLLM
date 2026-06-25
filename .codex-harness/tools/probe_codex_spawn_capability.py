#!/usr/bin/env python3
"""Detect whether the local Codex CLI exposes scripted subagent spawning.

This probe is intentionally non-executing with respect to model calls: it only
runs Codex help/version/list-style commands and parses their output. It does
not call agents, run `codex exec <prompt>`, connect to external systems, or
modify project files unless `--out-file` is explicitly provided.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]

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

PROBE_COMMANDS = [
    ("codex_version", ["codex", "--version"], True),
    ("codex_help", ["codex", "--help"], True),
    ("codex_exec_help", ["codex", "exec", "--help"], True),
    ("codex_agents_help", ["codex", "agents", "--help"], False),
    ("codex_agents_list", ["codex", "agents", "list"], False),
    ("codex_run_help", ["codex", "run", "--help"], False),
    ("codex_features_list", ["codex", "features", "list"], False),
]


class ProbeError(Exception):
    """User-facing probe error."""


def repo_rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def ensure_repo_output_path(path: Path) -> Path:
    resolved = path.resolve()
    try:
        rel = resolved.relative_to(ROOT).as_posix()
    except ValueError as exc:
        raise ProbeError(f"Refusing to write report outside repo: {path}") from exc

    rel_lower = rel.lower().replace("\\", "/")
    if rel_lower.startswith(DEFAULT_FORBIDDEN_PREFIXES):
        raise ProbeError(f"Refusing to write report under forbidden path: {rel}")
    return resolved


def run_command(args: list[str], timeout: int = 30) -> dict[str, Any]:
    """Run a fixed Codex CLI metadata command without invoking model execution."""
    try:
        if os.name == "nt":
            completed = subprocess.run(
                subprocess.list2cmdline(args),
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        else:
            completed = subprocess.run(
                args,
                shell=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
    except FileNotFoundError as exc:
        return {
            "command": " ".join(args),
            "returncode": 127,
            "stdout": "",
            "stderr": str(exc),
            "available": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": " ".join(args),
            "returncode": 124,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "command timed out",
            "available": False,
        }

    return {
        "command": " ".join(args),
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "available": completed.returncode == 0,
    }


def has_token(text: str, token: str) -> bool:
    return token.lower() in text.lower()


def has_regex(text: str, pattern: str) -> bool:
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def first_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def build_report(command_results: dict[str, dict[str, Any]], allow_generic_note: bool) -> dict[str, Any]:
    version_text = command_results["codex_version"]["stdout"] + command_results["codex_version"]["stderr"]
    help_text = command_results["codex_help"]["stdout"] + command_results["codex_help"]["stderr"]
    exec_help_text = (
        command_results["codex_exec_help"]["stdout"]
        + command_results["codex_exec_help"]["stderr"]
    )
    agents_help_text = (
        command_results["codex_agents_help"]["stdout"]
        + command_results["codex_agents_help"]["stderr"]
    )
    agents_list_text = (
        command_results["codex_agents_list"]["stdout"]
        + command_results["codex_agents_list"]["stderr"]
    )
    features_text = (
        command_results["codex_features_list"]["stdout"]
        + command_results["codex_features_list"]["stderr"]
    )
    combined_help = "\n".join([help_text, exec_help_text, agents_help_text, agents_list_text])

    exec_available = command_results["codex_exec_help"]["available"] and has_regex(
        exec_help_text, r"Usage:\s+codex exec|Run Codex non-interactively"
    )
    agents_usage_available = has_regex(agents_help_text, r"Usage:\s+codex agents(\s|$)")
    agents_command_available = command_results["codex_agents_help"]["available"] and agents_usage_available
    agents_list_available = command_results["codex_agents_list"]["available"] and (
        has_token(agents_list_text, "project-explorer")
        or has_token(agents_list_text, "agents")
    )
    explicit_agent_option = has_regex(combined_help, r"(^|\s)--agent(\s|,|$)")
    agents_run_available = agents_command_available and has_regex(
        agents_help_text, r"(^|\s)run(\s|$)"
    )
    explicit_agent_selection_available = explicit_agent_option or agents_run_available

    generic_codex_exec_supported = exec_available
    exec_read_only_sandbox_available = exec_available and (
        has_token(exec_help_text, "--sandbox")
        and has_token(exec_help_text, "read-only")
    )
    exec_output_capture_available = exec_available and (
        has_token(exec_help_text, "--output-last-message")
        or has_token(exec_help_text, "--json")
    )
    project_agent_spawn_supported = explicit_agent_selection_available and (
        explicit_agent_option or agents_run_available or agents_list_available
    )
    real_spawn_ready = bool(project_agent_spawn_supported)

    recommended_adapter_mode = "real-spawn" if real_spawn_ready else "prompt-bundle-only"
    if generic_codex_exec_supported and not real_spawn_ready:
        recommended_adapter_mode = "prompt-bundle-only; generic codex exec may be probed separately"

    return {
        "codex_version": first_line(version_text) or "UNKNOWN",
        "real_spawn_adapter_status": "READY" if real_spawn_ready else "NOT_READY",
        "exec_available": exec_available,
        "exec_read_only_sandbox_available": exec_read_only_sandbox_available,
        "exec_output_capture_available": exec_output_capture_available,
        "explicit_agent_selection_available": explicit_agent_selection_available,
        "agents_command_available": agents_command_available,
        "agents_list_available": agents_list_available,
        "agents_run_available": agents_run_available,
        "project_agent_spawn_supported": project_agent_spawn_supported,
        "generic_codex_exec_supported": generic_codex_exec_supported,
        "generic_codex_exec_is_subagent_spawn": False,
        "recommended_adapter_mode": recommended_adapter_mode,
        "notes": [
            "Probe ran only Codex help/version/list-style commands.",
            "No codex exec prompt was run; no model execution was triggered.",
            "Generic Codex exec is not equivalent to project-explorer.",
            "Do not label generic exec as .codex/agents subagent spawn.",
        ]
        + (
            [
                "--allow-generic-exec-probe was provided, but this version only prints this note and does not execute a model probe."
            ]
            if allow_generic_note
            else []
        ),
        "detected_tokens": {
            "--agent": explicit_agent_option,
            "agents run": agents_run_available,
            "agents list": agents_list_available,
            "--output-last-message": has_token(exec_help_text, "--output-last-message"),
            "--sandbox": has_token(exec_help_text, "--sandbox"),
            "--ephemeral": has_token(exec_help_text, "--ephemeral"),
            "--json": has_token(exec_help_text, "--json"),
            "multi_agent_feature_enabled": has_regex(features_text, r"\bmulti_agent\b.*\btrue\b"),
        },
        "commands": {
            key: {
                "command": result["command"],
                "returncode": result["returncode"],
                "available": result["available"],
                "stdout_first_line": first_line(result["stdout"]),
                "stderr_first_line": first_line(result["stderr"]),
            }
            for key, result in command_results.items()
        },
    }


def render_text(report: dict[str, Any]) -> str:
    lines = [
        "# Codex Real Spawn Capability Probe",
        "",
        f"codex_version: {report['codex_version']}",
        f"real_spawn_adapter_status: {report['real_spawn_adapter_status']}",
        f"exec_available: {report['exec_available']}",
        f"exec_read_only_sandbox_available: {report['exec_read_only_sandbox_available']}",
        f"exec_output_capture_available: {report['exec_output_capture_available']}",
        f"explicit_agent_selection_available: {report['explicit_agent_selection_available']}",
        f"agents_command_available: {report['agents_command_available']}",
        f"project_agent_spawn_supported: {report['project_agent_spawn_supported']}",
        f"generic_codex_exec_supported: {report['generic_codex_exec_supported']}",
        f"recommended_adapter_mode: {report['recommended_adapter_mode']}",
        "",
        "Important:",
        "- generic Codex exec is not equivalent to project-explorer",
        "- generic exec must not be labeled as .codex/agents subagent spawn",
        "- no Codex model execution was triggered by this probe",
        "",
        "Detected tokens:",
    ]
    for key, value in report["detected_tokens"].items():
        lines.append(f"- {key}: {value}")

    lines.extend(["", "Commands probed:"])
    for result in report["commands"].values():
        lines.append(
            f"- {result['command']}: returncode={result['returncode']}, available={result['available']}"
        )
        if result["stderr_first_line"]:
            lines.append(f"  stderr: {result['stderr_first_line']}")

    lines.extend(["", "Notes:"])
    for note in report["notes"]:
        lines.append(f"- {note}")
    return "\n".join(lines) + "\n"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Detect whether Codex CLI supports scripted .codex/agents spawning."
    )
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument("--json", action="store_true", help="Print JSON report.")
    output_group.add_argument("--text", action="store_true", help="Print text report. Default.")
    parser.add_argument(
        "--allow-generic-exec-probe",
        action="store_true",
        help="Print a note about generic exec probing; does not execute a model probe.",
    )
    parser.add_argument("--out-file", type=Path, help="Optional path to write the report.")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Return non-zero if real subagent spawn is not available.",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    command_results: dict[str, dict[str, Any]] = {}
    for key, command, required in PROBE_COMMANDS:
        result = run_command(command)
        command_results[key] = result
        if required and result["returncode"] != 0:
            # Keep reporting structured capability data even when a required
            # metadata command fails.
            pass

    report = build_report(command_results, args.allow_generic_exec_probe)
    output = json.dumps(report, indent=2, sort_keys=True) + "\n" if args.json else render_text(report)

    if args.out_file:
        out_path = ensure_repo_output_path(args.out_file)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output, encoding="utf-8")

    sys.stdout.write(output)

    if args.strict and report["real_spawn_adapter_status"] != "READY":
        return 2
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except ProbeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
