#!/usr/bin/env python3
"""Probe generic `codex exec` read-only prompt execution.

This tool is deliberately not a project-agent spawn adapter. It may run generic
`codex exec` only when `--run` is explicitly provided, and every report marks
the result as not project-explorer, not `.codex/agents` spawn, and not real
subagent spawning.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]

STATUS_SCOPE = [
    ".codex-harness",
    ".codex",
    ".claude",
    ".agents",
    "AGENTS.md",
]
BUSINESS_STATUS_SCOPE = [
    "run_wukong_weekend.ps1",
    "run_wukong_collect_refactor.ps1",
    "net_fault.sh",
    "ensure_wifi_connected.ps1",
    "check_wifi_state.ps1",
    "wk_validate_run_net.ps1",
    "server_B",
    "board",
    "board_A",
    "faults",
    "tools",
]
FORBIDDEN_PREFIXES = (
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
DEFAULT_PROMPT = (
    "Read `.codex-harness/README.md` and summarize in 5 bullet points what "
    "the current Codex harness dispatcher status is. Do not modify files. Do "
    "not run commands. Do not access board/server/HDC/SSH/app-server. Do not "
    "perform git add or git commit. This is generic codex exec, not "
    "project-explorer."
)


class ProbeError(Exception):
    """User-facing probe error."""


def repo_rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def ensure_repo_safe_path(path: Path, purpose: str) -> Path:
    resolved = path.resolve()
    try:
        rel = resolved.relative_to(ROOT).as_posix()
    except ValueError as exc:
        raise ProbeError(f"Refusing to {purpose} outside repo: {path}") from exc

    rel_lower = rel.lower().replace("\\", "/")
    if rel_lower.startswith(FORBIDDEN_PREFIXES):
        raise ProbeError(f"Refusing to {purpose} under forbidden path: {rel}")
    return resolved


def run_command(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
    if os.name == "nt":
        return subprocess.run(
            subprocess.list2cmdline(args),
            cwd=ROOT,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    return subprocess.run(
        args,
        cwd=ROOT,
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def get_status(paths: list[str]) -> str:
    completed = run_command(["git", "status", "--short", "--", *paths], timeout=30)
    return completed.stdout.rstrip()


def get_exec_help() -> tuple[str, dict[str, bool], int]:
    completed = run_command(["codex", "exec", "--help"], timeout=30)
    text = completed.stdout + completed.stderr
    capabilities = {
        "codex_exec_available": completed.returncode == 0
        and "Run Codex non-interactively" in text,
        "read_only_sandbox_available": "--sandbox" in text and "read-only" in text,
        "ephemeral_available": "--ephemeral" in text,
        "output_capture_available": "--output-last-message" in text,
    }
    return text, capabilities, completed.returncode


def read_prompt(args: argparse.Namespace) -> str:
    if args.prompt_file:
        prompt_path = ensure_repo_safe_path(args.prompt_file, "read prompt file")
        if not prompt_path.is_file():
            raise ProbeError(f"Prompt file not found: {repo_rel(prompt_path)}")
        return prompt_path.read_text(encoding="utf-8")
    if args.prompt:
        return args.prompt
    return DEFAULT_PROMPT


def shell_join(args: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(args)
    return shlex.join(args)


def status_lines(text: str) -> set[str]:
    return {line.strip() for line in text.splitlines() if line.strip()}


def status_entry_path(line: str) -> str:
    if len(line) >= 4:
        return line[3:].strip().replace("\\", "/")
    return line.strip().replace("\\", "/")


def is_allowed_change(line: str, allowed_prefixes: list[str]) -> bool:
    path = status_entry_path(line)
    return any(path == prefix or path.startswith(prefix.rstrip("/") + "/") for prefix in allowed_prefixes)


def unexpected_changes(before: str, after: str, allowed_prefixes: list[str]) -> list[str]:
    new_or_changed = sorted(status_lines(after) - status_lines(before))
    return [line for line in new_or_changed if not is_allowed_change(line, allowed_prefixes)]


def build_command(prompt: str, out_file: Path, cd_path: Path, sandbox: str) -> list[str]:
    return [
        "codex",
        "exec",
        "--sandbox",
        sandbox,
        "--ephemeral",
        "--output-last-message",
        str(out_file),
        "--cd",
        str(cd_path),
        prompt,
    ]


def result_status(
    run_requested: bool,
    capability_ok: bool,
    exec_returncode: int | None,
    unexpected: list[str],
) -> str:
    if not capability_ok:
        return "FAIL"
    if not run_requested:
        return "PASS"
    if exec_returncode != 0 or unexpected:
        return "FAIL"
    return "PASS"


def make_report(
    args: argparse.Namespace,
    prompt: str,
    out_file: Path,
    cd_path: Path,
    command: list[str],
    capabilities: dict[str, bool],
    exec_help_returncode: int,
    status_before: str,
    status_after: str,
    business_status_after: str,
    exec_returncode: int | None,
    exec_stdout: str,
    exec_stderr: str,
    unexpected: list[str],
) -> dict[str, Any]:
    capability_ok = all(
        [
            capabilities["codex_exec_available"],
            capabilities["read_only_sandbox_available"],
            capabilities["ephemeral_available"],
            capabilities["output_capture_available"],
        ]
    )
    status = result_status(args.run, capability_ok, exec_returncode, unexpected)
    prompt_scope = ".codex-harness/README.md"
    if ".codex-harness/README.md" not in prompt and "`.codex-harness/README.md`" not in prompt:
        status = "PASS_WITH_WARNINGS" if status == "PASS" else status

    return {
        "adapter_type": "generic_codex_exec_probe",
        "real_project_agent_spawn": False,
        "project_agent_name": None,
        "not_equivalent_to_project_explorer": True,
        "not_dot_codex_agents_spawn": True,
        "not_real_subagent_spawn_adapter": True,
        "codex_exec_available": capabilities["codex_exec_available"],
        "read_only_sandbox_available": capabilities["read_only_sandbox_available"],
        "ephemeral_available": capabilities["ephemeral_available"],
        "output_capture_available": capabilities["output_capture_available"],
        "exec_help_returncode": exec_help_returncode,
        "run_requested": args.run,
        "dry_run": not args.run,
        "sandbox": args.sandbox,
        "command_attempted": shell_join(command) if args.run else None,
        "command_preview": shell_join(command),
        "prompt_scope": prompt_scope,
        "prompt_text": prompt,
        "out_file": repo_rel(out_file),
        "cd": repo_rel(cd_path),
        "git_status_before": status_before,
        "git_status_after": status_after,
        "business_git_status_after": business_status_after,
        "unexpected_file_changes": unexpected,
        "exec_returncode": exec_returncode,
        "exec_stdout_first_line": first_line(exec_stdout),
        "exec_stderr_first_line": first_line(exec_stderr),
        "result_status": status,
        "next_recommended_step": (
            "Use generic exec only as a read-only prompt execution experiment; keep "
            "project-agent spawn gated by probe_codex_spawn_capability.py."
        ),
    }


def first_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def render_text(report: dict[str, Any]) -> str:
    lines = [
        "# Generic Codex Exec Adapter Probe",
        "",
        f"adapter_type: {report['adapter_type']}",
        f"real_project_agent_spawn: {report['real_project_agent_spawn']}",
        f"project_agent_name: {report['project_agent_name']}",
        f"not_equivalent_to_project_explorer: {report['not_equivalent_to_project_explorer']}",
        f"not_dot_codex_agents_spawn: {report['not_dot_codex_agents_spawn']}",
        f"not_real_subagent_spawn_adapter: {report['not_real_subagent_spawn_adapter']}",
        "",
        f"codex_exec_available: {report['codex_exec_available']}",
        f"read_only_sandbox_available: {report['read_only_sandbox_available']}",
        f"ephemeral_available: {report['ephemeral_available']}",
        f"output_capture_available: {report['output_capture_available']}",
        f"run_requested: {report['run_requested']}",
        f"dry_run: {report['dry_run']}",
        f"sandbox: {report['sandbox']}",
        f"prompt_scope: {report['prompt_scope']}",
        f"out_file: {report['out_file']}",
        f"cd: {report['cd']}",
        "",
        "command_preview:",
        report["command_preview"],
        "",
        "command_attempted:",
        report["command_attempted"] or "(none; dry-run only)",
        "",
        "git_status_before:",
        report["git_status_before"] or "(clean for scoped status)",
        "",
        "git_status_after:",
        report["git_status_after"] or "(clean for scoped status)",
        "",
        "business_git_status_after:",
        report["business_git_status_after"] or "(clean for scoped status)",
        "",
        "unexpected_file_changes:",
    ]
    if report["unexpected_file_changes"]:
        lines.extend(f"- {line}" for line in report["unexpected_file_changes"])
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            f"exec_returncode: {report['exec_returncode']}",
            f"exec_stdout_first_line: {report['exec_stdout_first_line']}",
            f"exec_stderr_first_line: {report['exec_stderr_first_line']}",
            f"result_status: {report['result_status']}",
            f"next_recommended_step: {report['next_recommended_step']}",
        ]
    )
    return "\n".join(lines) + "\n"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe generic codex exec read-only execution without project-agent spawn."
    )
    prompt_group = parser.add_mutually_exclusive_group()
    prompt_group.add_argument("--prompt-file", type=Path, help="Read probe prompt from file.")
    prompt_group.add_argument("--prompt", help="Probe prompt text.")
    parser.add_argument("--out-file", type=Path, help="Where codex writes the last message.")
    run_group = parser.add_mutually_exclusive_group()
    run_group.add_argument("--dry-run", action="store_true", help="Print command without model execution.")
    run_group.add_argument("--run", action="store_true", help="Explicitly run generic codex exec.")
    parser.add_argument(
        "--sandbox",
        default="read-only",
        choices=["read-only"],
        help="Only read-only sandbox is allowed.",
    )
    parser.add_argument(
        "--ephemeral",
        action="store_true",
        default=True,
        help="Always use --ephemeral. Present for report clarity.",
    )
    parser.add_argument("--cd", type=Path, default=ROOT, help="Codex working root. Default repo root.")
    parser.add_argument("--json", action="store_true", help="Print JSON summary.")
    parser.add_argument("--strict", action="store_true", help="Return non-zero on warning/failure.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        prompt = read_prompt(args)
        cd_path = ensure_repo_safe_path(args.cd, "use --cd")
        if not cd_path.is_dir():
            raise ProbeError(f"--cd is not a directory: {repo_rel(cd_path)}")

        out_file = ensure_repo_safe_path(
            args.out_file
            if args.out_file
            else ROOT / ".codex-harness" / "reports" / "p2_16_generic_codex_exec_probe" / "generic_exec_result.md",
            "write out-file",
        )
        if not repo_rel(out_file).startswith(".codex-harness/reports/"):
            raise ProbeError("out-file must be under .codex-harness/reports/")

        _help_text, capabilities, help_returncode = get_exec_help()
        command = build_command(prompt, out_file, cd_path, args.sandbox)
        status_before = get_status(STATUS_SCOPE)
        exec_returncode: int | None = None
        exec_stdout = ""
        exec_stderr = ""

        capability_ok = all(capabilities.values())
        if args.run:
            if not capability_ok:
                raise ProbeError(
                    "Refusing to run: codex exec lacks required read-only/ephemeral/output capture capability."
                )
            out_file.parent.mkdir(parents=True, exist_ok=True)
            completed = run_command(command, timeout=180)
            exec_returncode = completed.returncode
            exec_stdout = completed.stdout
            exec_stderr = completed.stderr

        status_after = get_status(STATUS_SCOPE)
        business_status_after = get_status(BUSINESS_STATUS_SCOPE)
        allowed_prefixes = [repo_rel(out_file), repo_rel(out_file.parent)]
        unexpected = unexpected_changes(status_before, status_after, allowed_prefixes)
        report = make_report(
            args,
            prompt,
            out_file,
            cd_path,
            command,
            capabilities,
            help_returncode,
            status_before,
            status_after,
            business_status_after,
            exec_returncode,
            exec_stdout,
            exec_stderr,
            unexpected,
        )
        output = json.dumps(report, indent=2, sort_keys=True) + "\n" if args.json else render_text(report)
        sys.stdout.write(output)

        if args.strict and report["result_status"] != "PASS":
            return 2
        if report["result_status"] == "FAIL":
            return 1
        return 0
    except (ProbeError, subprocess.TimeoutExpired) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
