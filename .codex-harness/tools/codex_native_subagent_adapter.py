#!/usr/bin/env python3
"""Capture native Codex subagent spawn traces from local session JSONL logs.

This adapter is intentionally parser-only. It reads existing Codex session
logs, writes redacted trace files under .codex-harness/reports/, and never
calls Codex execution, subagent spawning, board, server, HDC, or SSH workflows.
The --dry-run flag keeps these same parser-only semantics and still writes
trace files.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import collections
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
HARNESS_ROOT = ROOT / ".codex-harness"
REPORTS_ROOT = HARNESS_ROOT / "reports"
SCHEMA = "codex-harness.native_subagent_capture.v1"
DEFAULT_RESULT_FILES = {
    "project-explorer": "01_project_explorer_result.md",
    "project-implementer": "02_project_implementer_result.md",
    "project-reviewer": "03_project_reviewer_result.md",
    "project-repairer": "04_project_repairer_result.md",
}
SUPPORTED_WORKFLOWS = {
    "explorer-reviewer": ["project-explorer", "project-reviewer"],
    "explorer-implementer-reviewer": [
        "project-explorer",
        "project-implementer",
        "project-reviewer",
    ],
}
FORBIDDEN_REPORT_PREFIXES = (
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


class AdapterError(Exception):
    """User-facing adapter error."""


class NotFoundError(AdapterError):
    """Parent session or spawn events were not found."""


def issue(code: str, message: str, severity: str = "partial", **details: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "code": code,
        "severity": severity,
        "message": message,
    }
    payload.update(details)
    return payload


def repo_rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def ensure_run_dir(path: Path) -> Path:
    resolved = (ROOT / path).resolve() if not path.is_absolute() else path.resolve()
    try:
        rel = resolved.relative_to(ROOT).as_posix()
    except ValueError as exc:
        raise AdapterError(f"run-dir must be inside repo: {path}") from exc

    rel_lower = rel.lower().replace("\\", "/")
    if rel_lower.startswith(FORBIDDEN_REPORT_PREFIXES):
        raise AdapterError(f"refusing forbidden run-dir: {rel}")
    try:
        resolved.relative_to(REPORTS_ROOT.resolve())
    except ValueError as exc:
        raise AdapterError(
            f"run-dir must be under .codex-harness/reports/: {repo_rel(resolved)}"
        ) from exc
    return resolved


def ensure_log_root(path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.exists():
        raise AdapterError(f"log-root does not exist: {path}")
    if not resolved.is_dir():
        raise AdapterError(f"log-root is not a directory: {path}")
    return resolved


def resolve_repo_path(path_text: str) -> Path:
    path = Path(path_text)
    return path if path.is_absolute() else ROOT / path


def load_expected_workflow(spec: str) -> dict[str, Any]:
    if spec in SUPPORTED_WORKFLOWS:
        return {
            "name": spec,
            "source": "preset",
            "manifest_path": None,
            "schema_version": "preset",
            "run_id": None,
            "parent_thread_id": None,
            "log_root": None,
            "cwd": None,
            "scope": {},
            "privacy": {},
            "match": {},
            "expected_roles": [
                {
                    "order": index,
                    "role": role,
                    "agent_type": role,
                    "call_id": None,
                    "required": True,
                    "result_file": DEFAULT_RESULT_FILES.get(role),
                }
                for index, role in enumerate(SUPPORTED_WORKFLOWS[spec], 1)
            ],
        }

    path = resolve_repo_path(spec).resolve()
    if not path.exists():
        raise AdapterError(
            f"expected-workflow must be a preset or JSON manifest path: {spec}"
        )
    if path.suffix.lower() != ".json":
        raise AdapterError(f"expected-workflow manifest must be JSON: {repo_rel(path)}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise AdapterError(
            f"expected-workflow manifest is malformed JSON at {repo_rel(path)}: {exc.msg}"
        ) from exc
    if not isinstance(payload, dict):
        raise AdapterError(f"expected-workflow manifest must be an object: {repo_rel(path)}")

    raw_roles = payload.get("expected_roles")
    if not isinstance(raw_roles, list) or not raw_roles:
        raise AdapterError(
            f"expected-workflow manifest must contain non-empty expected_roles: {repo_rel(path)}"
        )
    expected_roles: list[dict[str, Any]] = []
    for index, item in enumerate(raw_roles, 1):
        if isinstance(item, str):
            expected_roles.append(
                {
                    "order": index,
                    "role": item,
                    "agent_type": item,
                    "call_id": None,
                    "required": True,
                    "result_file": DEFAULT_RESULT_FILES.get(item),
                }
            )
            continue
        if not isinstance(item, dict):
            raise AdapterError(
                f"expected_roles[{index}] must be a string or object in {repo_rel(path)}"
            )
        role = item.get("agent_type") or item.get("role") or item.get("agent_role")
        if not isinstance(role, str) or not role:
            raise AdapterError(
                f"expected_roles[{index}] is missing role/agent_role/agent_type in {repo_rel(path)}"
            )
        order = item.get("order", index)
        if not isinstance(order, int) or order < 1:
            raise AdapterError(
                f"expected_roles[{index}].order must be a positive integer in {repo_rel(path)}"
            )
        call_id = item.get("call_id")
        if call_id is not None and (not isinstance(call_id, str) or not call_id):
            raise AdapterError(
                f"expected_roles[{index}].call_id must be a non-empty string in {repo_rel(path)}"
            )
        required = item.get("required", True)
        if not isinstance(required, bool):
            raise AdapterError(
                f"expected_roles[{index}].required must be boolean in {repo_rel(path)}"
            )
        result_file = item.get("result_file")
        if result_file is None:
            result_file = DEFAULT_RESULT_FILES.get(role)
        if result_file is not None and (not isinstance(result_file, str) or not result_file):
            raise AdapterError(
                f"expected_roles[{index}].result_file must be a non-empty string in {repo_rel(path)}"
            )
        expected_roles.append(
            {
                "order": order,
                "role": role,
                "agent_type": role,
                "call_id": call_id,
                "required": required,
                "result_file": result_file,
            }
        )
    expected_roles = sorted(expected_roles, key=lambda item: item["order"])

    return {
        "name": payload.get("name") if isinstance(payload.get("name"), str) else path.stem,
        "source": "json_manifest",
        "manifest_path": str(path),
        "schema_version": payload.get("schema_version"),
        "run_id": payload.get("run_id"),
        "parent_thread_id": payload.get("parent_thread_id"),
        "log_root": payload.get("log_root"),
        "cwd": payload.get("cwd"),
        "scope": payload.get("scope") if isinstance(payload.get("scope"), dict) else {},
        "privacy": payload.get("privacy") if isinstance(payload.get("privacy"), dict) else {},
        "match": payload.get("match") if isinstance(payload.get("match"), dict) else {},
        "expected_roles": expected_roles,
    }


def expected_call_ids(expected: dict[str, Any]) -> list[str]:
    call_ids: list[str] = []
    for item in expected.get("expected_roles") or []:
        if isinstance(item, dict) and isinstance(item.get("call_id"), str):
            call_ids.append(item["call_id"])
    return call_ids


def workflow_scope(args: argparse.Namespace, expected: dict[str, Any]) -> str:
    if expected_call_ids(expected):
        return "expected_call_ids"
    if effective_start_line(args, expected) is not None or effective_end_line(args, expected) is not None:
        return "parent_line_range"
    return "whole_parent_session"


def effective_parent_thread_id(args: argparse.Namespace, expected: dict[str, Any]) -> str:
    value = args.parent_thread_id or expected.get("parent_thread_id")
    if not isinstance(value, str) or not value:
        raise AdapterError("--parent-thread-id is required unless expected workflow manifest provides parent_thread_id")
    return value


def effective_log_root(args: argparse.Namespace, expected: dict[str, Any]) -> str:
    value = args.log_root or expected.get("log_root")
    if not isinstance(value, str) or not value:
        raise AdapterError("--log-root is required unless expected workflow manifest provides log_root")
    return value


def effective_start_line(args: argparse.Namespace, expected: dict[str, Any]) -> int | None:
    scope = expected.get("scope") if isinstance(expected.get("scope"), dict) else {}
    value = args.parent_start_line if args.parent_start_line is not None else scope.get("parent_start_line")
    return value if isinstance(value, int) else None


def effective_end_line(args: argparse.Namespace, expected: dict[str, Any]) -> int | None:
    scope = expected.get("scope") if isinstance(expected.get("scope"), dict) else {}
    value = args.parent_end_line if args.parent_end_line is not None else scope.get("parent_end_line")
    return value if isinstance(value, int) else None


def manifest_bool(expected: dict[str, Any], section: str, key: str, default: bool) -> bool:
    payload = expected.get(section) if isinstance(expected.get(section), dict) else {}
    value = payload.get(key, default)
    return value if isinstance(value, bool) else default


def validate_line_range(start: int | None, end: int | None) -> None:
    if start is not None and start < 1:
        raise AdapterError("--parent-start-line must be >= 1")
    if end is not None and end < 1:
        raise AdapterError("--parent-end-line must be >= 1")
    if start is not None and end is not None and start > end:
        raise AdapterError("--parent-start-line must be <= --parent-end-line")


def line_in_range(line: Any, start: int | None, end: int | None) -> bool:
    if not isinstance(line, int):
        return False
    if start is not None and line < start:
        return False
    if end is not None and line > end:
        return False
    return True


def rollout_files(log_root: Path) -> list[Path]:
    return sorted(log_root.rglob("rollout-*.jsonl"), key=lambda item: item.as_posix())


def read_jsonl(path: Path, warnings: list[str]) -> Iterable[tuple[int, dict[str, Any]]]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_no, line in enumerate(handle, 1):
                text = line.strip()
                if not text:
                    continue
                try:
                    obj = json.loads(text)
                except json.JSONDecodeError as exc:
                    warnings.append(f"{path}: malformed JSONL line {line_no}: {exc.msg}")
                    continue
                if isinstance(obj, dict):
                    yield line_no, obj
                else:
                    warnings.append(f"{path}: non-object JSONL line {line_no}")
    except OSError as exc:
        warnings.append(f"{path}: failed to read: {exc}")


def nested_get(value: Any, path: list[str]) -> Any:
    current = value
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def text_digest(text: str | None, include_excerpt: bool) -> dict[str, Any]:
    if text is None:
        return {
            "present": False,
            "chars": 0,
            "sha256": None,
            "excerpt": None if include_excerpt else "[redacted]",
        }
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    info: dict[str, Any] = {
        "present": True,
        "chars": len(text),
        "sha256": digest,
    }
    if include_excerpt:
        info["excerpt"] = sanitize_excerpt(text)
    else:
        info["excerpt"] = "[redacted]"
    return info


def sanitize_excerpt(text: str, limit: int = 240) -> str:
    compact = re.sub(r"\s+", " ", text.replace("\x00", "")).strip()
    if len(compact) <= limit:
        return compact
    return compact[:limit].rstrip() + "..."


def parse_arguments(payload: dict[str, Any], warnings: list[str], path: Path, line_no: int) -> dict[str, Any]:
    raw = payload.get("arguments")
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        warnings.append(f"{path}: spawn_agent arguments are not a JSON string at line {line_no}")
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        warnings.append(f"{path}: malformed spawn_agent arguments at line {line_no}: {exc.msg}")
        return {}
    if not isinstance(parsed, dict):
        warnings.append(f"{path}: spawn_agent arguments did not decode to object at line {line_no}")
        return {}
    return parsed


def parse_output_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return {}
    text = raw.strip()
    if not text.startswith("{"):
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def scan_session_index(files: list[Path], parent_thread_id: str) -> tuple[dict[str, dict[str, Any]], dict[str, Path], Path | None, dict[str, Any]]:
    session_index: dict[str, dict[str, Any]] = {}
    filename_index: dict[str, Path] = {}
    parent_by_filename: Path | None = None
    warnings: list[str] = []
    lines_seen = 0
    malformed_lines = 0

    for path in files:
        filename_thread_id = thread_id_from_filename(path)
        if filename_thread_id:
            filename_index[filename_thread_id] = path
        if parent_thread_id in path.name:
            parent_by_filename = path
        before = len(warnings)
        for line_no, obj in read_jsonl(path, warnings):
            lines_seen += 1
            if obj.get("type") != "session_meta":
                continue
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                continue
            session_id = payload.get("id")
            if isinstance(session_id, str) and session_id:
                session_index[session_id] = {
                    "path": path,
                    "line": line_no,
                    "payload": payload,
                }
                break
        malformed_lines += len(warnings) - before

    parent_path = None
    if parent_thread_id in session_index:
        parent_path = session_index[parent_thread_id]["path"]
    elif parent_by_filename is not None:
        parent_path = parent_by_filename

    stats = {
        "rollout_files_scanned": len(files),
        "json_objects_seen_until_meta": lines_seen,
        "malformed_lines": malformed_lines,
        "warnings": warnings,
    }
    return session_index, filename_index, parent_path, stats


def thread_id_from_filename(path: Path) -> str | None:
    match = re.search(
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        path.name,
        flags=re.IGNORECASE,
    )
    return match.group(1) if match else None


def parse_parent(path: Path, include_excerpt: bool) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    spawn_calls: list[dict[str, Any]] = []
    spawn_ends: list[dict[str, Any]] = []
    spawn_outputs: list[dict[str, Any]] = []
    waits: list[dict[str, Any]] = []
    warnings: list[str] = []

    for line_no, obj in read_jsonl(path, warnings):
        obj_type = obj.get("type")
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            continue

        if obj_type == "response_item":
            if payload.get("type") == "function_call" and payload.get("name") == "spawn_agent":
                args = parse_arguments(payload, warnings, path, line_no)
                message = args.get("message") if isinstance(args.get("message"), str) else None
                spawn_calls.append(
                    {
                        "line": line_no,
                        "timestamp": obj.get("timestamp"),
                        "function_call_id": payload.get("call_id"),
                        "agent_type": args.get("agent_type"),
                        "reasoning_effort": args.get("reasoning_effort"),
                        "model": args.get("model"),
                        "has_message": message is not None,
                        "message": text_digest(message, include_excerpt),
                        "arguments_keys": sorted(str(key) for key in args.keys()),
                    }
                )
            elif payload.get("type") == "function_call_output":
                raw_output = payload.get("output")
                output_text = raw_output if isinstance(raw_output, str) else None
                output_obj = parse_output_object(raw_output)
                agent_id = output_obj.get("agent_id") if isinstance(output_obj.get("agent_id"), str) else None
                nickname = output_obj.get("nickname") if isinstance(output_obj.get("nickname"), str) else None
                spawn_outputs.append(
                    {
                        "line": line_no,
                        "timestamp": obj.get("timestamp"),
                        "call_id": payload.get("call_id"),
                        "output": text_digest(output_text, include_excerpt),
                        "agent_id": agent_id,
                        "nickname": nickname,
                        "decoded_keys": sorted(str(key) for key in output_obj.keys()),
                    }
                )
            continue

        if obj_type != "event_msg":
            continue
        event_type = payload.get("type")
        if event_type == "collab_agent_spawn_end":
            prompt = payload.get("prompt") if isinstance(payload.get("prompt"), str) else None
            spawn_ends.append(
                {
                    "line": line_no,
                    "timestamp": obj.get("timestamp"),
                    "call_id": payload.get("call_id"),
                    "sender_thread_id": payload.get("sender_thread_id"),
                    "new_thread_id": payload.get("new_thread_id"),
                    "new_agent_nickname": payload.get("new_agent_nickname"),
                    "new_agent_role": payload.get("new_agent_role"),
                    "model": payload.get("model"),
                    "reasoning_effort": payload.get("reasoning_effort"),
                    "status": payload.get("status"),
                    "prompt": text_digest(prompt, include_excerpt),
                    "canonical_prompt_source": "collab_agent_spawn_end.prompt",
                }
            )
        elif event_type == "collab_waiting_end":
            waits.append(
                {
                    "line": line_no,
                    "timestamp": obj.get("timestamp"),
                    "call_id": payload.get("call_id"),
                    "sender_thread_id": payload.get("sender_thread_id"),
                    "agent_statuses": payload.get("agent_statuses"),
                    "statuses": payload.get("statuses"),
                }
            )

    return spawn_calls, spawn_ends, spawn_outputs, waits, warnings


def parse_child(path: Path, parent_thread_id: str, include_excerpt: bool) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    meta: dict[str, Any] = {}
    task_complete: dict[str, Any] | None = None
    fallback: dict[str, Any] | None = None

    for line_no, obj in read_jsonl(path, warnings):
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            continue
        if obj.get("type") == "session_meta" and not meta:
            source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
            thread_spawn = nested_get(source, ["subagent", "thread_spawn"])
            if not isinstance(thread_spawn, dict):
                thread_spawn = {}
            meta = {
                "line": line_no,
                "thread_id": payload.get("id"),
                "agent_role": payload.get("agent_role") or thread_spawn.get("agent_role"),
                "agent_nickname": payload.get("agent_nickname") or thread_spawn.get("agent_nickname"),
                "agent_path": thread_spawn.get("agent_path"),
                "depth": thread_spawn.get("depth"),
                "parent_thread_id": thread_spawn.get("parent_thread_id"),
                "parent_thread_id_matches": (
                    thread_spawn.get("parent_thread_id") == parent_thread_id
                    if thread_spawn.get("parent_thread_id") is not None
                    else None
                ),
            }
        if obj.get("type") == "response_item":
            if payload.get("type") == "message" and payload.get("role") == "assistant":
                text = message_text(payload)
                if text is not None:
                    fallback = {
                        "line": line_no,
                        "timestamp": obj.get("timestamp"),
                        "source": "fallback_last_assistant_message",
                        "last_agent_message": text_digest(text, include_excerpt),
                    }
        if obj.get("type") == "event_msg" and payload.get("type") == "task_complete":
            last_message = payload.get("last_agent_message")
            task_complete = {
                "line": line_no,
                "timestamp": obj.get("timestamp"),
                "source": "task_complete.last_agent_message",
                "turn_id": payload.get("turn_id"),
                "completed_at": payload.get("completed_at"),
                "duration_ms": payload.get("duration_ms"),
                "time_to_first_token_ms": payload.get("time_to_first_token_ms"),
                "last_agent_message": text_digest(
                    last_message if isinstance(last_message, str) else None,
                    include_excerpt,
                ),
            }

    if not meta:
        warnings.append(f"{path}: child session_meta not found")
    if task_complete is None:
        task_complete = {
            "source": None,
            "last_agent_message": text_digest(None, include_excerpt),
        }
    if fallback is None:
        fallback = {
            "source": None,
            "last_agent_message": text_digest(None, include_excerpt),
        }
    return {"session_meta": meta, "task_complete": task_complete, "fallback": fallback}, warnings


def message_text(payload: dict[str, Any]) -> str | None:
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    chunks: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str) and item.get("type") in {"output_text", "text"}:
            chunks.append(text)
    return "\n".join(chunks) if chunks else None


def extract_child_result_text(path: Path) -> dict[str, Any]:
    warnings: list[str] = []
    task_complete_text: str | None = None
    task_complete_line: int | None = None
    fallback_text: str | None = None
    fallback_line: int | None = None
    for line_no, obj in read_jsonl(path, warnings):
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            continue
        if obj.get("type") == "response_item":
            if payload.get("type") == "message" and payload.get("role") == "assistant":
                text = message_text(payload)
                if text is not None:
                    fallback_text = text
                    fallback_line = line_no
        if obj.get("type") == "event_msg" and payload.get("type") == "task_complete":
            text = payload.get("last_agent_message")
            if isinstance(text, str):
                task_complete_text = text
                task_complete_line = line_no
    if task_complete_text is not None:
        return {
            "text": task_complete_text,
            "line": task_complete_line,
            "source": "task_complete.last_agent_message",
            "fallback_used": False,
            "warnings": warnings,
        }
    if fallback_text is not None:
        return {
            "text": fallback_text,
            "line": fallback_line,
            "source": "fallback_last_assistant_message",
            "fallback_used": True,
            "warnings": warnings,
        }
    return {
        "text": None,
        "line": None,
        "source": None,
        "fallback_used": False,
        "warnings": warnings,
    }


def wait_status_for_thread(waits: list[dict[str, Any]], thread_id: str, include_excerpt: bool) -> dict[str, Any]:
    for wait in waits:
        statuses = wait.get("agent_statuses")
        if isinstance(statuses, list):
            for item in statuses:
                if not isinstance(item, dict) or item.get("thread_id") != thread_id:
                    continue
                status = item.get("status")
                return summarize_wait_status(wait, status, include_excerpt)
        status_map = wait.get("statuses")
        if isinstance(status_map, dict) and thread_id in status_map:
            return summarize_wait_status(wait, status_map.get(thread_id), include_excerpt)
    return {"source": None, "present": False}


def summarize_wait_status(wait: dict[str, Any], status: Any, include_excerpt: bool) -> dict[str, Any]:
    completed_text = None
    state = None
    if isinstance(status, dict):
        if isinstance(status.get("completed"), str):
            completed_text = status.get("completed")
            state = "completed"
        else:
            state = next(iter(status.keys()), None) if status else None
    elif isinstance(status, str):
        state = status
    return {
        "source": "parent.collab_waiting_end",
        "present": True,
        "line": wait.get("line"),
        "timestamp": wait.get("timestamp"),
        "call_id": wait.get("call_id"),
        "state": state,
        "completed_metadata_only": text_digest(completed_text, include_excerpt),
        "used_as_primary_result": False,
    }


def association_method(child_thread_id: str | None, child_path: Path | None, meta: dict[str, Any]) -> str | None:
    if child_path is None:
        return None
    methods = []
    if child_thread_id and child_thread_id in child_path.name:
        methods.append("filename_suffix")
    if meta.get("thread_id") == child_thread_id:
        methods.append("session_meta.id")
    if meta.get("parent_thread_id_matches") is True:
        methods.append("thread_spawn.parent_thread_id")
    return "+".join(methods) if methods else "path_lookup"


def filter_parent_events(
    spawn_calls: list[dict[str, Any]],
    spawn_ends: list[dict[str, Any]],
    spawn_outputs: list[dict[str, Any]],
    scope: str,
    start: int | None,
    end: int | None,
    call_ids: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    call_id_set = set(call_ids)
    if scope == "expected_call_ids":
        scoped_calls = [
            call for call in spawn_calls
            if call.get("function_call_id") in call_id_set
        ]
        scoped_ends = [
            spawn_end for spawn_end in spawn_ends
            if spawn_end.get("call_id") in call_id_set
        ]
        scoped_outputs = [
            output for output in spawn_outputs
            if output.get("call_id") in call_id_set
        ]
    elif scope == "parent_line_range":
        scoped_calls = [
            call for call in spawn_calls
            if line_in_range(call.get("line"), start, end)
        ]
        scoped_ends = [
            spawn_end for spawn_end in spawn_ends
            if line_in_range(spawn_end.get("line"), start, end)
        ]
        scoped_outputs = [
            output for output in spawn_outputs
            if line_in_range(output.get("line"), start, end)
        ]
    else:
        scoped_calls = list(spawn_calls)
        scoped_ends = list(spawn_ends)
        scoped_outputs = list(spawn_outputs)

    scoped_call_ids = {
        call.get("function_call_id")
        for call in scoped_calls
        if call.get("function_call_id")
    }
    scoped_end_call_ids = {
        spawn_end.get("call_id")
        for spawn_end in scoped_ends
        if spawn_end.get("call_id")
    }
    scoped_output_call_ids = {
        output.get("call_id")
        for output in scoped_outputs
        if output.get("call_id")
    }
    outside = {
        "spawn_call_count": len(spawn_calls) - len(scoped_calls),
        "spawn_end_count": len(spawn_ends) - len(scoped_ends),
        "function_call_output_count": len(spawn_outputs) - len(scoped_outputs),
        "spawn_call_ids": [
            call.get("function_call_id")
            for call in spawn_calls
            if call.get("function_call_id") not in scoped_call_ids
        ],
        "spawn_end_call_ids": [
            spawn_end.get("call_id")
            for spawn_end in spawn_ends
            if spawn_end.get("call_id") not in scoped_end_call_ids
        ],
        "function_call_output_call_ids": [
            output.get("call_id")
            for output in spawn_outputs
            if output.get("call_id") not in scoped_output_call_ids
        ],
    }
    return scoped_calls, scoped_ends, scoped_outputs, outside


def expected_workflow_summary(
    expected_spec: dict[str, Any],
    spawns: list[dict[str, Any]],
    scope: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    expected_entries = expected_spec["expected_roles"]
    expected = [item["role"] for item in expected_entries]
    required_expected = [item["role"] for item in expected_entries if item.get("required", True)]
    expected_ids = [item.get("call_id") for item in expected_entries if item.get("call_id")]
    role_stream = [spawn.get("agent_role") for spawn in spawns]
    observed = [role for role in role_stream if isinstance(role, str)]
    expected_counter = collections.Counter(required_expected)
    observed_counter = collections.Counter(observed)
    missing = []
    for role, count in expected_counter.items():
        if observed_counter[role] < count:
            missing.extend([role] * (count - observed_counter[role]))
    expected_positions = {role: index for index, role in enumerate(expected)}
    ordered_positions = [
        expected_positions[role]
        for role in observed
        if role in expected_positions
    ]
    order_ok = ordered_positions == sorted(ordered_positions)
    prefix_ok = observed == expected[: len(observed)] if len(observed) <= len(expected) else False
    unexpected_roles = [
        role
        for role in observed
        if role not in expected_positions
    ]
    repeated_expected_roles = [
        role
        for role in expected
        if observed.count(role) > 1
    ]

    authoritative = scope in {"parent_line_range", "expected_call_ids"}
    workflow_issues: list[dict[str, Any]] = []
    for role in missing:
        workflow_issues.append(
            issue(
                "missing_required_role",
                "in-scope required role is missing",
                severity="partial",
                role=role,
            )
        )
    if unexpected_roles and authoritative:
        workflow_issues.append(
            issue(
                "unexpected_in_scope_role",
                "in-scope unexpected role was captured",
                severity="partial",
                roles=unexpected_roles,
            )
        )
    if authoritative and observed != expected:
        workflow_issues.append(
            issue(
                "workflow_order_or_cardinality_mismatch",
                "in-scope observed roles do not exactly match expected workflow order",
                severity="partial",
                expected_roles=expected,
                observed_roles=observed,
            )
        )

    observed_by_call_id = {
        spawn.get("call_id"): spawn
        for spawn in spawns
        if isinstance(spawn.get("call_id"), str)
    }
    for item in expected_entries:
        call_id = item.get("call_id")
        if not call_id:
            continue
        spawn = observed_by_call_id.get(call_id)
        if spawn is None:
            workflow_issues.append(
                issue(
                    "missing_expected_call_id",
                    "expected call_id was not captured in scope",
                    severity="partial",
                    call_id=call_id,
                    role=item["role"],
                )
            )
            continue
        if spawn.get("agent_type") != item["role"] or spawn.get("agent_role") != item["role"]:
            workflow_issues.append(
                issue(
                    "expected_call_id_role_mismatch",
                    "expected call_id was captured with a different role",
                    severity="partial",
                    call_id=call_id,
                    expected_role=item["role"],
                    agent_type=spawn.get("agent_type"),
                    agent_role=spawn.get("agent_role"),
                )
            )

    return {
        "name": expected_spec["name"],
        "source": expected_spec["source"],
        "manifest_path": expected_spec.get("manifest_path"),
        "schema_version": expected_spec.get("schema_version"),
        "run_id": expected_spec.get("run_id"),
        "cwd": expected_spec.get("cwd"),
        "summary_label": "scoped_workflow_instance" if authoritative else "unscoped_session_inventory",
        "workflow_instance_status": "scoped" if authoritative else "unscoped",
        "validation_scope": scope,
        "workflow_validation_scope": scope,
        "order_validation_authoritative": authoritative,
        "expected_roles": expected,
        "required_expected_roles": required_expected,
        "expected_role_specs": expected_entries,
        "expected_call_ids": expected_ids,
        "observed_roles": observed,
        "total_role_stream": role_stream,
        "unexpected_roles": unexpected_roles,
        "repeated_expected_roles": repeated_expected_roles,
        "missing_expected_roles": missing,
        "order_ok_for_expected_roles": order_ok,
        "prefix_ok": prefix_ok,
        "notes": [
            "Scoped validation is authoritative only for parent line range or expected call_id manifests.",
            "Outside-scope records are reported separately and do not fail scoped acceptance.",
        ],
    }, workflow_issues


def classify_capture(
    spawn_calls: list[dict[str, Any]],
    spawn_ends: list[dict[str, Any]],
    spawns: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    issues: list[dict[str, Any]] = []

    matched_call_lines = {
        spawn["parent_spawn_call"]["line"]
        for spawn in spawns
        if isinstance(spawn.get("parent_spawn_call"), dict)
        and isinstance(spawn["parent_spawn_call"].get("line"), int)
    }
    unmatched_calls = [
        call for call in spawn_calls
        if call.get("line") not in matched_call_lines
    ]
    unpaired_spawn_ends = unpaired_spawn_end_call_ids(spawn_calls, spawn_ends)

    if unmatched_calls or unpaired_spawn_ends:
        issues.append(
            issue(
                "parent_spawn_call_end_count_mismatch",
                "parent spawn_agent records are not all deterministically linked to child threads",
                spawn_call_count=len(spawn_calls),
                spawn_end_count=len(spawn_ends),
                unmatched_spawn_call_count=len(unmatched_calls),
                unpaired_spawn_end_call_ids=unpaired_spawn_ends,
            )
        )

    for call in unmatched_calls:
        issues.append(
            issue(
                "unmatched_spawn_agent_call",
                "spawn_agent call has no deterministic child link",
                line=call.get("line"),
                function_call_id=call.get("function_call_id"),
                agent_type=call.get("agent_type"),
            )
        )

    for spawn in spawns:
        if not isinstance(spawn.get("parent_spawn_call"), dict):
            issues.append(
                issue(
                    "spawn_end_without_spawn_agent_call",
                    "collab_agent_spawn_end has no matched parent spawn_agent call",
                    spawn_index=spawn.get("index"),
                    call_id=spawn.get("call_id"),
                    child_thread_id=spawn.get("child_thread_id"),
                )
            )
        link_mode = spawn.get("link_mode")
        proof = spawn.get("child_link_proof") if isinstance(spawn.get("child_link_proof"), dict) else {}
        if link_mode == "collab_agent_spawn_end":
            if not spawn.get("spawn_end_line"):
                issues.append(
                    issue(
                        "missing_collab_agent_spawn_end_line",
                        "collab_agent_spawn_end link mode requires a spawn_end line",
                        spawn_index=spawn.get("index"),
                        call_id=spawn.get("call_id"),
                    )
                )
        elif link_mode == "function_call_output_agent_id":
            agent_id = spawn.get("function_call_output_agent_id")
            if (
                spawn.get("function_call_output_present") is not True
                or not isinstance(agent_id, str)
                or agent_id != spawn.get("child_thread_id")
                or proof.get("exact_match") is not True
            ):
                issues.append(
                    issue(
                        "invalid_function_call_output_child_link",
                        "function_call_output_agent_id link mode requires output.agent_id to exactly equal child_thread_id",
                        spawn_index=spawn.get("index"),
                        call_id=spawn.get("call_id"),
                        function_call_output_agent_id=agent_id,
                        child_thread_id=spawn.get("child_thread_id"),
                    )
                )
        else:
            issues.append(
                issue(
                    "unsupported_child_link_mode",
                    "spawn record does not use a supported deterministic child link mode",
                    spawn_index=spawn.get("index"),
                    call_id=spawn.get("call_id"),
                    link_mode=link_mode,
                )
            )
        if not spawn.get("child_thread_id"):
            issues.append(
                issue(
                    "spawn_without_child_thread_id",
                    "spawn record does not include a child thread id",
                    spawn_index=spawn.get("index"),
                    call_id=spawn.get("call_id"),
                )
            )
        child = spawn.get("child") if isinstance(spawn.get("child"), dict) else {}
        if not child.get("log_path"):
            issues.append(
                issue(
                    "spawn_without_child_log",
                    "linked child log was not found",
                    spawn_index=spawn.get("index"),
                    call_id=spawn.get("call_id"),
                    child_thread_id=spawn.get("child_thread_id"),
                )
            )
        meta = child.get("session_meta") if isinstance(child.get("session_meta"), dict) else {}
        if child.get("log_path") and not meta:
            issues.append(
                issue(
                    "missing_child_session_meta",
                    "child session_meta is not available",
                    spawn_index=spawn.get("index"),
                    call_id=spawn.get("call_id"),
                    child_thread_id=spawn.get("child_thread_id"),
                )
            )
        if meta.get("parent_thread_id_matches") is False:
            issues.append(
                issue(
                    "child_parent_thread_id_mismatch",
                    "child session_meta parent_thread_id does not match parent",
                    spawn_index=spawn.get("index"),
                    call_id=spawn.get("call_id"),
                    child_thread_id=spawn.get("child_thread_id"),
                    child_parent_thread_id=meta.get("parent_thread_id"),
                )
            )
        result = spawn.get("result") if isinstance(spawn.get("result"), dict) else {}
        if not result.get("present"):
            issues.append(
                issue(
                    "missing_child_result",
                    "child task_complete.last_agent_message is not available",
                    spawn_index=spawn.get("index"),
                    call_id=spawn.get("call_id"),
                    child_thread_id=spawn.get("child_thread_id"),
                )
            )

    status = "partial" if issues else "complete"
    return status, issues


def classify_match_requirements(
    spawns: list[dict[str, Any]],
    parent_thread_id: str,
    expected: dict[str, Any],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if manifest_bool(expected, "match", "require_same_parent_thread", False):
        for spawn in spawns:
            link_mode = spawn.get("link_mode")
            if link_mode == "collab_agent_spawn_end":
                sender = spawn.get("spawn_end_sender_thread_id")
                if sender == parent_thread_id:
                    continue
                issues.append(
                    issue(
                        "spawn_end_sender_parent_mismatch",
                        "collab_agent_spawn_end sender_thread_id does not match parent",
                        spawn_index=spawn.get("index"),
                        call_id=spawn.get("call_id"),
                        sender_thread_id=sender,
                        parent_thread_id=parent_thread_id,
                    )
                )
                continue
            child = spawn.get("child") if isinstance(spawn.get("child"), dict) else {}
            meta = child.get("session_meta") if isinstance(child.get("session_meta"), dict) else {}
            child_parent = meta.get("parent_thread_id")
            if link_mode == "function_call_output_agent_id" and child_parent == parent_thread_id:
                continue
            issues.append(
                issue(
                    "child_link_parent_mismatch",
                    "deterministic child link does not prove the expected parent thread",
                    spawn_index=spawn.get("index"),
                    call_id=spawn.get("call_id"),
                    link_mode=link_mode,
                    child_parent_thread_id=child_parent,
                    parent_thread_id=parent_thread_id,
                )
            )
    if manifest_bool(expected, "match", "require_child_parent_thread_match", False):
        for spawn in spawns:
            child = spawn.get("child") if isinstance(spawn.get("child"), dict) else {}
            meta = child.get("session_meta") if isinstance(child.get("session_meta"), dict) else {}
            if meta.get("parent_thread_id_matches") is not True:
                issues.append(
                    issue(
                        "child_parent_thread_match_required",
                        "child session_meta parent_thread_id did not positively match parent",
                        spawn_index=spawn.get("index"),
                        call_id=spawn.get("call_id"),
                        child_thread_id=spawn.get("child_thread_id"),
                    )
                )
    return issues


def unmatched_call_ids(
    spawn_calls: list[dict[str, Any]],
    spawns: list[dict[str, Any]],
) -> list[str]:
    linked_call_ids = {
        spawn.get("call_id")
        for spawn in spawns
        if isinstance(spawn.get("call_id"), str)
    }
    return [
        str(call.get("function_call_id"))
        for call in spawn_calls
        if isinstance(call.get("function_call_id"), str)
        and call.get("function_call_id") not in linked_call_ids
    ]


def unpaired_spawn_end_call_ids(
    spawn_calls: list[dict[str, Any]],
    spawn_ends: list[dict[str, Any]],
) -> list[str]:
    call_ids = {
        call.get("function_call_id")
        for call in spawn_calls
        if isinstance(call.get("function_call_id"), str)
    }
    return [
        str(spawn_end.get("call_id"))
        for spawn_end in spawn_ends
        if isinstance(spawn_end.get("call_id"), str)
        and spawn_end.get("call_id") not in call_ids
    ]


def partial_record_count(
    spawn_calls: list[dict[str, Any]],
    spawn_ends: list[dict[str, Any]],
    spawns: list[dict[str, Any]],
) -> int:
    return (
        sum(1 for spawn in spawns if spawn.get("capture_status") != "complete")
        + len(unmatched_call_ids(spawn_calls, spawns))
        + len(unpaired_spawn_end_call_ids(spawn_calls, spawn_ends))
    )


def build_spawns(
    spawn_calls: list[dict[str, Any]],
    spawn_ends: list[dict[str, Any]],
    spawn_outputs: list[dict[str, Any]],
    waits: list[dict[str, Any]],
    session_index: dict[str, dict[str, Any]],
    filename_index: dict[str, Path],
    parent_thread_id: str,
    include_excerpt: bool,
) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    by_call_id = {
        call.get("function_call_id"): call
        for call in spawn_calls
        if call.get("function_call_id")
    }
    outputs_by_call_id = {
        output.get("call_id"): output
        for output in spawn_outputs
        if output.get("call_id")
    }
    matched_call_lines: set[int] = set()
    spawns: list[dict[str, Any]] = []

    def load_child(child_thread_id: Any) -> tuple[Path | None, dict[str, Any], list[str]]:
        child_path = None
        child_data = {"session_meta": {}, "task_complete": {"source": None, "last_agent_message": text_digest(None, include_excerpt)}}
        child_warnings: list[str] = []
        if isinstance(child_thread_id, str) and child_thread_id:
            indexed = session_index.get(child_thread_id)
            if indexed:
                child_path = indexed["path"]
            else:
                child_path = filename_index.get(child_thread_id)
            if child_path:
                child_data, child_warnings = parse_child(child_path, parent_thread_id, include_excerpt)
                warnings.extend(child_warnings)
            else:
                warnings.append(f"child log not found for thread {child_thread_id}")
        return child_path, child_data, child_warnings

    def add_spawn(
        *,
        call: dict[str, Any] | None,
        spawn_end: dict[str, Any] | None,
        output: dict[str, Any] | None,
        child_thread_id: str | None,
        link_mode: str,
        match_method: str | None,
    ) -> None:
        if call is not None and isinstance(call.get("line"), int):
            matched_call_lines.add(call["line"])

        child_path, child_data, child_warnings = load_child(child_thread_id)
        meta = child_data.get("session_meta") if isinstance(child_data.get("session_meta"), dict) else {}
        task_complete = child_data.get("task_complete") if isinstance(child_data.get("task_complete"), dict) else {}
        fallback = child_data.get("fallback") if isinstance(child_data.get("fallback"), dict) else {}
        result_digest = task_complete.get("last_agent_message") if isinstance(task_complete.get("last_agent_message"), dict) else text_digest(None, include_excerpt)
        result_source = task_complete.get("source") if result_digest.get("present") else None
        fallback_digest = fallback.get("last_agent_message") if isinstance(fallback.get("last_agent_message"), dict) else text_digest(None, include_excerpt)
        fallback_available = bool(fallback_digest.get("present"))
        fallback_used = False
        if result_source is None and fallback_available:
            result_digest = fallback_digest
            result_source = fallback.get("source")
            fallback_used = True
        spawn_warnings = []
        if meta.get("parent_thread_id_matches") is False:
            spawn_warnings.append("child thread_spawn.parent_thread_id does not match parent")
        if result_source is None:
            spawn_warnings.append("child task_complete.last_agent_message not available")
        elif fallback_used:
            spawn_warnings.append("fallback_last_assistant_message used because task_complete.last_agent_message is unavailable")
        call_id = (
            (spawn_end or {}).get("call_id")
            or (call or {}).get("function_call_id")
        )
        output_agent_id = (output or {}).get("agent_id")
        output_present = output is not None
        exact_output_link = (
            link_mode != "function_call_output_agent_id"
            or (
                isinstance(output_agent_id, str)
                and isinstance(child_thread_id, str)
                and output_agent_id == child_thread_id
            )
        )
        prompt = (spawn_end or {}).get("prompt")
        canonical_prompt_source = "collab_agent_spawn_end.prompt"
        if link_mode == "function_call_output_agent_id":
            prompt = (call or {}).get("message")
            canonical_prompt_source = "spawn_agent.arguments.message"
        if not isinstance(prompt, dict):
            prompt = text_digest(None, include_excerpt)
        spawn_capture_status = (
            "complete"
            if child_path and result_digest.get("present") and exact_output_link
            else "partial"
        )
        child_link_proof = {
            "mode": link_mode,
            "call_id": call_id,
            "spawn_agent_line": (call or {}).get("line"),
            "collab_agent_spawn_end_line": (spawn_end or {}).get("line"),
            "function_call_output_line": (output or {}).get("line"),
            "new_thread_id": (spawn_end or {}).get("new_thread_id"),
            "agent_id": output_agent_id,
            "child_thread_id": child_thread_id,
            "exact_match": (
                (spawn_end or {}).get("new_thread_id") == child_thread_id
                if link_mode == "collab_agent_spawn_end"
                else exact_output_link
            ),
        }
        spawns.append(
            {
                "index": len(spawns) + 1,
                "capture_status": spawn_capture_status,
                "parent_thread_id": parent_thread_id,
                "parent_spawn_call": call,
                "spawn_end_line": (spawn_end or {}).get("line"),
                "spawn_end_timestamp": (spawn_end or {}).get("timestamp"),
                "spawn_end_sender_thread_id": (spawn_end or {}).get("sender_thread_id"),
                "spawn_call_match_method": match_method if call else None,
                "call_id": call_id,
                "agent_type": (call or {}).get("agent_type"),
                "agent_role": (spawn_end or {}).get("new_agent_role") or meta.get("agent_role") or (call or {}).get("agent_type"),
                "agent_nickname": (spawn_end or {}).get("new_agent_nickname") or (output or {}).get("nickname") or meta.get("agent_nickname"),
                "model": (spawn_end or {}).get("model") or (call or {}).get("model"),
                "reasoning_effort": (spawn_end or {}).get("reasoning_effort") or (call or {}).get("reasoning_effort"),
                "status": (spawn_end or {}).get("status"),
                "child_thread_id": child_thread_id,
                "link_mode": link_mode,
                "collab_agent_spawn_end_present": spawn_end is not None,
                "function_call_output_present": output_present,
                "function_call_output_agent_id": output_agent_id,
                "function_call_output_nickname": (output or {}).get("nickname"),
                "child_link_proof": child_link_proof,
                "association_method": association_method(
                    child_thread_id if isinstance(child_thread_id, str) else None,
                    child_path,
                    meta,
                ),
                "canonical_prompt_source": canonical_prompt_source,
                "prompt": prompt,
                "child": {
                    "log_path": str(child_path) if child_path else None,
                    "session_meta": meta,
                    "task_complete": {
                        "line": task_complete.get("line"),
                        "source": task_complete.get("source"),
                        "last_agent_message": result_digest,
                    },
                    "fallback": {
                        "line": fallback.get("line"),
                        "source": fallback.get("source"),
                        "last_agent_message": fallback_digest,
                        "available": fallback_available,
                    },
                },
                "wait_status": wait_status_for_thread(
                    waits,
                    child_thread_id if isinstance(child_thread_id, str) else "",
                    include_excerpt,
                ),
                "result_source": result_source,
                "result": result_digest,
                "fallback_used": fallback_used,
                "result_from_parent_wait_used": False,
                "warnings": spawn_warnings,
            }
        )

    for index, spawn_end in enumerate(spawn_ends, 1):
        call = by_call_id.get(spawn_end.get("call_id"))
        match_method = "call_id"
        if call is None and index - 1 < len(spawn_calls):
            call = spawn_calls[index - 1]
            match_method = "ordinal"
        output = outputs_by_call_id.get(spawn_end.get("call_id"))
        child_thread_id = spawn_end.get("new_thread_id")
        add_spawn(
            call=call,
            spawn_end=spawn_end,
            output=output,
            child_thread_id=child_thread_id if isinstance(child_thread_id, str) else None,
            link_mode="collab_agent_spawn_end",
            match_method=match_method,
        )

    for call in spawn_calls:
        if call.get("line") in matched_call_lines:
            continue
        output = outputs_by_call_id.get(call.get("function_call_id"))
        child_thread_id = (output or {}).get("agent_id")
        if isinstance(child_thread_id, str) and child_thread_id:
            add_spawn(
                call=call,
                spawn_end=None,
                output=output,
                child_thread_id=child_thread_id,
                link_mode="function_call_output_agent_id",
                match_method="function_call_output.call_id",
            )

    unmatched = [call for call in spawn_calls if call.get("line") not in matched_call_lines]
    for call in unmatched:
        warnings.append(f"spawn_agent call at parent line {call.get('line')} has no deterministic child link")
    spawns.sort(
        key=lambda spawn: (
            (spawn.get("parent_spawn_call") or {}).get("line")
            or spawn.get("spawn_end_line")
            or 0
        )
    )
    for index, spawn in enumerate(spawns, 1):
        spawn["index"] = index
    return spawns, warnings


def expected_result_map(expected: dict[str, Any]) -> dict[str, dict[str, Any]]:
    mapping: dict[str, dict[str, Any]] = {}
    for item in expected.get("expected_role_specs") or []:
        if not isinstance(item, dict):
            continue
        call_id = item.get("call_id")
        if isinstance(call_id, str) and call_id:
            mapping[call_id] = item
        role = item.get("role")
        if isinstance(role, str) and role and role not in mapping:
            mapping[role] = item
    for item in expected.get("expected_roles") or []:
        if not isinstance(item, dict):
            continue
        call_id = item.get("call_id")
        if isinstance(call_id, str) and call_id:
            mapping[call_id] = item
            continue
        role = item.get("role")
        if isinstance(role, str) and role and role not in mapping:
            mapping[role] = item
    return mapping


def result_path(run_dir: Path, result_file: str) -> Path:
    candidate = (run_dir / result_file).resolve()
    try:
        candidate.relative_to(run_dir.resolve())
    except ValueError as exc:
        raise AdapterError(f"result_file escapes run-dir: {result_file}") from exc
    return candidate


def result_file_for_spawn(spawn: dict[str, Any], expected: dict[str, Any]) -> str:
    mapping = expected_result_map(expected)
    spec = mapping.get(spawn.get("call_id")) or mapping.get(spawn.get("agent_role"))
    if isinstance(spec, dict) and isinstance(spec.get("result_file"), str):
        return spec["result_file"]
    role = spawn.get("agent_role")
    return DEFAULT_RESULT_FILES.get(role, f"{spawn.get('index', 0):02d}_subagent_result.md")


def result_header(
    trace: dict[str, Any],
    spawn: dict[str, Any],
    result: dict[str, Any],
    status: str,
) -> str:
    child = spawn.get("child") if isinstance(spawn.get("child"), dict) else {}
    meta = child.get("session_meta") if isinstance(child.get("session_meta"), dict) else {}
    task_complete = child.get("task_complete") if isinstance(child.get("task_complete"), dict) else {}
    prompt = spawn.get("prompt") if isinstance(spawn.get("prompt"), dict) else {}
    result_digest = text_digest(result.get("text") if isinstance(result.get("text"), str) else None, False)
    parent_call = spawn.get("parent_spawn_call") if isinstance(spawn.get("parent_spawn_call"), dict) else {}
    lines = [
        "---",
        "capture_backend: codex-native-subagent",
        f"schema_version: {trace['schema']}",
        f"run_id: {trace['expected_workflow'].get('run_id') or trace['expected_workflow'].get('name')}",
        f"role: {spawn.get('agent_role') or spawn.get('agent_type')}",
        f"parent_thread_id: {spawn.get('parent_thread_id')}",
        f"child_thread_id: {spawn.get('child_thread_id')}",
        f"call_id: {spawn.get('call_id')}",
        f"workflow_validation_scope: {trace['scope']['workflow_validation_scope']}",
        f"source_parent_log: {trace['source_paths']['parent_log']}",
        f"source_spawn_line: {parent_call.get('line')}",
        f"source_spawn_end_line: {spawn.get('spawn_end_line')}",
        f"source_child_log: {child.get('log_path')}",
        f"source_task_complete_line: {result.get('line') or task_complete.get('line')}",
        f"link_mode: {spawn.get('link_mode')}",
        f"canonical_prompt_source: {spawn.get('canonical_prompt_source')}",
        f"prompt_sha256: {prompt.get('sha256')}",
        f"result_sha256: {result_digest.get('sha256')}",
        f"prompt_redacted: {str(not trace['privacy'].get('include_prompts')).lower()}",
        f"result_source: {result.get('source')}",
        f"fallback_used: {str(bool(result.get('fallback_used'))).lower()}",
        f"status: {status}",
        "---",
        "",
    ]
    if meta.get("parent_thread_id_matches") is False:
        lines.extend(["RESULT: FAIL", "", "child parent_thread_id mismatch", ""])
    return "\n".join(lines)


def failure_result_header(trace: dict[str, Any], spec: dict[str, Any], status: str) -> str:
    lines = [
        "---",
        "capture_backend: codex-native-subagent",
        f"schema_version: {trace['schema']}",
        f"run_id: {trace['expected_workflow'].get('run_id') or trace['expected_workflow'].get('name')}",
        f"role: {spec.get('role') or spec.get('agent_type')}",
        f"parent_thread_id: {trace['summary']['parent_thread_id']}",
        "child_thread_id: null",
        f"call_id: {spec.get('call_id')}",
        f"workflow_validation_scope: {trace['scope']['workflow_validation_scope']}",
        f"source_parent_log: {trace['source_paths']['parent_log']}",
        "source_spawn_line: null",
        "source_spawn_end_line: null",
        "source_child_log: null",
        "source_task_complete_line: null",
        "prompt_sha256: null",
        "result_sha256: null",
        f"prompt_redacted: {str(not trace['privacy'].get('include_prompts')).lower()}",
        "result_source: null",
        "fallback_used: false",
        f"status: {status}",
        "---",
        "",
    ]
    return "\n".join(lines)


def write_result_files(trace: dict[str, Any]) -> list[dict[str, Any]]:
    run_dir = Path(trace["source_paths"]["json_trace"]).parent
    written: list[dict[str, Any]] = []
    written_keys: set[str] = set()
    for spawn in trace["spawns"]:
        result_file = result_file_for_spawn(spawn, trace["expected_workflow"])
        path = result_path(run_dir, result_file)
        child = spawn.get("child") if isinstance(spawn.get("child"), dict) else {}
        child_log = child.get("log_path")
        status = "complete"
        result = {"text": None, "line": None, "source": None, "fallback_used": False}
        if child_log and Path(child_log).exists():
            result = extract_child_result_text(Path(child_log))
        if not result.get("text"):
            status = "capture_failed"
            body = "RESULT: CAPTURE_FAILED\n\nNo child result text was available from task_complete or fallback assistant message.\n"
        else:
            body = str(result["text"]).rstrip() + "\n"
        header = result_header(trace, spawn, result, status)
        path.write_text(header + body, encoding="utf-8")
        record = {
            "role": spawn.get("agent_role") or spawn.get("agent_type"),
            "call_id": spawn.get("call_id"),
            "path": str(path),
            "status": status,
            "result_source": result.get("source"),
            "fallback_used": bool(result.get("fallback_used")),
        }
        written.append(record)
        if isinstance(spawn.get("call_id"), str):
            written_keys.add(spawn["call_id"])
        if isinstance(spawn.get("agent_role"), str):
            written_keys.add(spawn["agent_role"])

    for spec in trace["expected_workflow"].get("expected_role_specs") or []:
        if not isinstance(spec, dict) or not spec.get("required", True):
            continue
        key = spec.get("call_id") or spec.get("role")
        if key in written_keys:
            continue
        result_file = spec.get("result_file") or DEFAULT_RESULT_FILES.get(spec.get("role"), "missing_result.md")
        path = result_path(run_dir, result_file)
        body = (
            failure_result_header(trace, spec, "capture_failed")
            + "RESULT: CAPTURE_FAILED\n\nRequired in-scope subagent result was not captured.\n"
        )
        path.write_text(body, encoding="utf-8")
        written.append(
            {
                "role": spec.get("role"),
                "call_id": spec.get("call_id"),
                "path": str(path),
                "status": "capture_failed",
                "result_source": None,
                "fallback_used": False,
            }
        )
    return written


def render_markdown(trace: dict[str, Any]) -> str:
    lines = [
        "# Native Subagent Capture",
        "",
        f"- Schema: `{trace['schema']}`",
        f"- Capture status: `{trace['capture_status']}`",
        f"- Parent thread: `{trace['summary']['parent_thread_id']}`",
        f"- Workflow inventory: `{trace['expected_workflow']['summary_label']}`",
        f"- Workflow instance status: `{trace['expected_workflow']['workflow_instance_status']}`",
        f"- Workflow validation scope: `{trace['scope']['workflow_validation_scope']}`",
        f"- Parent line range: `{trace['scope']['parent_start_line']}` to `{trace['scope']['parent_end_line']}`",
        f"- Parent log: `{trace['source_paths']['parent_log']}`",
        f"- Privacy: prompts/results redacted by default; hashes and char counts recorded.",
        f"- Path note: {trace['privacy']['local_path_note']}",
        "",
        "## Role Table",
        "",
        "| # | role | nickname | child_thread_id | link_mode | prompt_chars | result_source | result_chars | warnings |",
        "|---|---|---|---|---|---:|---|---:|---|",
    ]
    for spawn in trace["spawns"]:
        warning_text = "; ".join(spawn.get("warnings") or []) or ""
        lines.append(
            "| {index} | `{role}` | `{nickname}` | `{thread}` | `{link_mode}` | {prompt_chars} | `{source}` | {result_chars} | {warnings} |".format(
                index=spawn.get("index"),
                role=spawn.get("agent_role") or "",
                nickname=spawn.get("agent_nickname") or "",
                thread=spawn.get("child_thread_id") or "",
                link_mode=spawn.get("link_mode") or "",
                prompt_chars=(spawn.get("prompt") or {}).get("chars", 0),
                source=spawn.get("result_source") or "",
                result_chars=(spawn.get("result") or {}).get("chars", 0),
                warnings=warning_text.replace("|", "\\|"),
            )
        )
    lines.extend(
        [
            "",
            "## Workflow Validation",
            "",
            f"- Expected roles: `{', '.join(trace['expected_workflow']['expected_roles'])}`",
            f"- Expected call IDs: `{', '.join(trace['expected_workflow']['expected_call_ids']) or '(none)'}`",
            f"- Observed roles: `{', '.join(trace['expected_workflow']['observed_roles'])}`",
            f"- Total role stream: `{', '.join(str(role) for role in trace['expected_workflow']['total_role_stream'])}`",
            f"- Unexpected roles: `{', '.join(trace['expected_workflow']['unexpected_roles']) or '(none)'}`",
            f"- Repeated expected roles: `{', '.join(trace['expected_workflow']['repeated_expected_roles']) or '(none)'}`",
            f"- Missing expected roles: `{', '.join(trace['expected_workflow']['missing_expected_roles']) or '(none)'}`",
            f"- Order ok for expected roles: `{trace['expected_workflow']['order_ok_for_expected_roles']}`",
            f"- Order validation authoritative: `{trace['expected_workflow']['order_validation_authoritative']}`",
            f"- Workflow validation scope: `{trace['expected_workflow']['workflow_validation_scope']}`",
            "- Note: outside-scope records are reported below and do not fail scoped acceptance.",
            "",
            "## Outside Scope",
            "",
            f"- Whole parent spawn_agent calls: `{trace['summary']['whole_parent_spawn_call_count']}`",
            f"- Whole parent collab_agent_spawn_end events: `{trace['summary']['whole_parent_spawn_end_count']}`",
            f"- Whole parent function_call_output events: `{trace['summary']['whole_parent_function_call_output_count']}`",
            f"- Ignored spawn_agent calls: `{trace['scope']['outside_scope']['spawn_call_count']}`",
            f"- Ignored collab_agent_spawn_end events: `{trace['scope']['outside_scope']['spawn_end_count']}`",
            f"- Ignored function_call_output events: `{trace['scope']['outside_scope']['function_call_output_count']}`",
            f"- Ignored partial spawn records: `{trace['scope']['outside_scope']['partial_spawn_count']}`",
            f"- Ignored whole-parent issue count: `{trace['scope']['outside_scope']['ignored_issue_count']}`",
            "",
            "## Capture Issues",
            "",
        ]
    )
    capture_issues = trace.get("capture_issues") or []
    if capture_issues:
        for item in capture_issues:
            detail = ", ".join(
                f"{key}={value}"
                for key, value in item.items()
                if key not in {"message", "severity"}
            )
            lines.append(f"- {item['message']} ({detail})")
    else:
        lines.append("- (none)")
    lines.extend(
        [
            "",
            "## Source Paths",
            "",
            f"- Log root: `{trace['source_paths']['log_root']}`",
            f"- Parent log: `{trace['source_paths']['parent_log']}`",
            f"- JSON trace: `{trace['source_paths']['json_trace']}`",
            f"- Markdown trace: `{trace['source_paths']['markdown_trace']}`",
            "",
            "## Guardrails",
            "",
        ]
    )
    for guardrail in trace["guardrails"]:
        lines.append(f"- {guardrail}")
    lines.extend(["", "## Warnings", ""])
    warnings = trace.get("warnings") or []
    if warnings:
        lines.extend(f"- {warning}" for warning in warnings)
    else:
        lines.append("- (none)")
    lines.append("")
    return "\n".join(lines)


def build_trace(args: argparse.Namespace) -> dict[str, Any]:
    expected_spec = load_expected_workflow(args.expected_workflow)
    parent_thread_id = effective_parent_thread_id(args, expected_spec)
    log_root = ensure_log_root(Path(effective_log_root(args, expected_spec)))
    run_dir = ensure_run_dir(Path(args.run_dir))
    parent_start_line = effective_start_line(args, expected_spec)
    parent_end_line = effective_end_line(args, expected_spec)
    validate_line_range(parent_start_line, parent_end_line)
    scope = workflow_scope(args, expected_spec)
    expected_ids = expected_call_ids(expected_spec)
    files = rollout_files(log_root)
    if not files:
        raise NotFoundError(f"no rollout-*.jsonl files found under {log_root}")

    session_index, filename_index, parent_path, scan_stats = scan_session_index(files, parent_thread_id)
    warnings = list(scan_stats.pop("warnings"))
    if parent_path is None:
        raise NotFoundError(f"parent thread not found: {parent_thread_id}")

    spawn_calls, spawn_ends, spawn_outputs, waits, parent_warnings = parse_parent(parent_path, args.include_excerpts)
    warnings.extend(parent_warnings)
    if not spawn_calls and not spawn_ends:
        raise NotFoundError(f"no spawn_agent or collab_agent_spawn_end events found in {parent_path}")

    scoped_spawn_calls, scoped_spawn_ends, scoped_spawn_outputs, outside_scope = filter_parent_events(
        spawn_calls,
        spawn_ends,
        spawn_outputs,
        scope,
        parent_start_line,
        parent_end_line,
        expected_ids,
    )
    if not scoped_spawn_calls and not scoped_spawn_ends:
        raise NotFoundError(
            f"no in-scope spawn_agent or collab_agent_spawn_end events found in {parent_path}"
        )

    all_spawns, all_spawn_warnings = build_spawns(
        spawn_calls,
        spawn_ends,
        spawn_outputs,
        waits,
        session_index,
        filename_index,
        parent_thread_id,
        args.include_excerpts,
    )
    spawns, spawn_warnings = build_spawns(
        scoped_spawn_calls,
        scoped_spawn_ends,
        scoped_spawn_outputs,
        waits,
        session_index,
        filename_index,
        parent_thread_id,
        args.include_excerpts,
    )
    warnings.extend(spawn_warnings)

    expected, workflow_issues = expected_workflow_summary(expected_spec, spawns, scope)
    for role in expected["missing_expected_roles"]:
        warnings.append(f"in-scope workflow missing expected role: {role}")

    capture_status, capture_issues = classify_capture(scoped_spawn_calls, scoped_spawn_ends, spawns)
    capture_issues.extend(workflow_issues)
    capture_issues.extend(classify_match_requirements(spawns, parent_thread_id, expected_spec))
    if capture_issues:
        capture_status = "partial"
    if capture_issues:
        warnings.extend(item["message"] for item in capture_issues)

    outside_capture_status, outside_capture_issues = classify_capture(spawn_calls, spawn_ends, all_spawns)
    scoped_call_ids = {
        call.get("function_call_id")
        for call in scoped_spawn_calls
        if call.get("function_call_id")
    }
    scoped_end_call_ids = {
        spawn_end.get("call_id")
        for spawn_end in scoped_spawn_ends
        if spawn_end.get("call_id")
    }
    outside_spawns = [
        spawn
        for spawn in all_spawns
        if spawn.get("call_id") not in scoped_call_ids
    ]
    outside_partial_spawns = [
        spawn
        for spawn in outside_spawns
        if spawn.get("capture_status") == "partial"
    ]
    if outside_scope["spawn_call_count"] or outside_scope["spawn_end_count"]:
        warnings.append(
            "outside-scope parent records ignored for strict validation: "
            f"spawn_agent={outside_scope['spawn_call_count']}, "
            f"collab_agent_spawn_end={outside_scope['spawn_end_count']}"
        )
    if outside_partial_spawns:
        warnings.append(
            f"outside-scope partial spawn records ignored for strict validation: {len(outside_partial_spawns)}"
        )

    missing_spawn_end_call_ids = unmatched_call_ids(scoped_spawn_calls, spawns)
    in_scope_partial_count = partial_record_count(scoped_spawn_calls, scoped_spawn_ends, spawns)
    out_of_scope_partial_count = partial_record_count(
        [
            call for call in spawn_calls
            if call.get("function_call_id") not in scoped_call_ids
        ],
        [
            spawn_end for spawn_end in spawn_ends
            if spawn_end.get("call_id") not in scoped_end_call_ids
        ],
        outside_spawns,
    )
    adapter_exit_code = 1 if capture_status == "partial" and not args.allow_partial else 0

    json_path = run_dir / "spawn_capture_trace.json"
    md_path = run_dir / "spawn_capture_trace.md"
    primary_spawn = spawns[0] if len(spawns) == 1 else {}
    primary_prompt = primary_spawn.get("prompt") if isinstance(primary_spawn.get("prompt"), dict) else {}
    trace = {
        "schema": SCHEMA,
        "adapter_exit_code": adapter_exit_code,
        "allow_partial": bool(args.allow_partial),
        "capture_status": capture_status,
        "workflow_validation_scope": scope,
        "expected_call_ids_present": bool(expected_ids),
        "missing_spawn_end_call_ids": missing_spawn_end_call_ids,
        "unresolved_link_call_ids": missing_spawn_end_call_ids,
        "in_scope_partial_count": in_scope_partial_count,
        "out_of_scope_partial_count": out_of_scope_partial_count,
        "link_mode": primary_spawn.get("link_mode") if primary_spawn else ("mixed" if spawns else None),
        "collab_agent_spawn_end_present": primary_spawn.get("collab_agent_spawn_end_present") if primary_spawn else any(spawn.get("collab_agent_spawn_end_present") for spawn in spawns),
        "function_call_output_present": primary_spawn.get("function_call_output_present") if primary_spawn else any(spawn.get("function_call_output_present") for spawn in spawns),
        "function_call_output_agent_id": primary_spawn.get("function_call_output_agent_id") if primary_spawn else None,
        "child_thread_id": primary_spawn.get("child_thread_id") if primary_spawn else None,
        "child_link_proof": primary_spawn.get("child_link_proof") if primary_spawn else None,
        "canonical_prompt_source": primary_spawn.get("canonical_prompt_source") if primary_spawn else None,
        "prompt_sha256": primary_prompt.get("sha256"),
        "capture_issues": capture_issues,
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "invocation": {
            "dry_run": bool(args.dry_run),
            "allow_partial": bool(args.allow_partial),
            "capture_results": bool(args.capture_results),
            "include_excerpts": bool(args.include_excerpts),
            "adapter_exit_code": adapter_exit_code,
        },
        "privacy": {
            "redact_prompts": not args.include_excerpts and manifest_bool(expected_spec, "privacy", "redact_prompts", True),
            "redact_results": not args.include_excerpts,
            "include_excerpts": bool(args.include_excerpts),
            "include_prompts": bool(args.include_excerpts) or manifest_bool(expected_spec, "privacy", "include_prompts", False),
            "store_prompt_hash": manifest_bool(expected_spec, "privacy", "store_prompt_hash", True),
            "hash_algorithm": "sha256",
            "raw_events_written": False,
            "local_paths_emitted": True,
            "local_path_note": "Trace v0 emits local source paths for auditability; do not publish without review.",
        },
        "summary": {
            "adapter_exit_code": adapter_exit_code,
            "allow_partial": bool(args.allow_partial),
            "capture_status": capture_status,
            "parent_thread_id": parent_thread_id,
            "workflow_validation_scope": scope,
            "expected_call_ids_present": bool(expected_ids),
            "missing_spawn_end_call_ids": missing_spawn_end_call_ids,
            "unresolved_link_call_ids": missing_spawn_end_call_ids,
            "in_scope_partial_count": in_scope_partial_count,
            "out_of_scope_partial_count": out_of_scope_partial_count,
            "link_mode": primary_spawn.get("link_mode") if primary_spawn else ("mixed" if spawns else None),
            "collab_agent_spawn_end_present": primary_spawn.get("collab_agent_spawn_end_present") if primary_spawn else any(spawn.get("collab_agent_spawn_end_present") for spawn in spawns),
            "function_call_output_present": primary_spawn.get("function_call_output_present") if primary_spawn else any(spawn.get("function_call_output_present") for spawn in spawns),
            "function_call_output_agent_id": primary_spawn.get("function_call_output_agent_id") if primary_spawn else None,
            "child_thread_id": primary_spawn.get("child_thread_id") if primary_spawn else None,
            "child_link_proof": primary_spawn.get("child_link_proof") if primary_spawn else None,
            "canonical_prompt_source": primary_spawn.get("canonical_prompt_source") if primary_spawn else None,
            "prompt_sha256": primary_prompt.get("sha256"),
            "parent_start_line": parent_start_line,
            "parent_end_line": parent_end_line,
            "spawn_count": len(spawns),
            "spawn_call_count": len(scoped_spawn_calls),
            "spawn_end_count": len(scoped_spawn_ends),
            "function_call_output_count": len(scoped_spawn_outputs),
            "whole_parent_spawn_call_count": len(spawn_calls),
            "whole_parent_spawn_end_count": len(spawn_ends),
            "whole_parent_function_call_output_count": len(spawn_outputs),
            "child_logs_found": sum(1 for spawn in spawns if spawn["child"]["log_path"]),
            "capture_issue_count": len(capture_issues),
            "warnings_count": len(warnings),
            **scan_stats,
        },
        "expected_workflow": expected,
        "scope": {
            "workflow_validation_scope": scope,
            "parent_start_line": parent_start_line,
            "parent_end_line": parent_end_line,
            "expected_call_ids": expected_ids,
            "outside_scope": {
                **outside_scope,
                "partial_spawn_count": len(outside_partial_spawns),
                "ignored_issue_count": len(outside_capture_issues),
                "whole_parent_capture_status": outside_capture_status,
                "ignored_spawn_call_count": outside_scope["spawn_call_count"],
            },
        },
        "source_paths": {
            "log_root": str(log_root),
            "parent_log": str(parent_path),
            "json_trace": str(json_path),
            "markdown_trace": str(md_path),
        },
        "result_capture": {
            "enabled": bool(args.capture_results),
            "files": [],
            "source_preference": [
                "task_complete.last_agent_message",
                "fallback_last_assistant_message",
            ],
        },
        "guardrails": [
            "Parser-only dry-run capture.",
            "Does not call spawn_agent from Python.",
            "Does not call codex exec.",
            "Does not connect to board, server, HDC, SSH, or app-server.",
            "Does not run collection, aggregation, or business scripts.",
            "Does not claim .codex/agents/*.toml role resolution is confirmed.",
            "Parent wait status is metadata only and is never primary child result.",
            "Partial capture exits nonzero unless --allow-partial is set.",
        ],
        "spawns": spawns,
        "warnings": warnings,
    }
    return trace


def write_trace(trace: dict[str, Any]) -> None:
    json_path = Path(trace["source_paths"]["json_trace"])
    md_path = Path(trace["source_paths"]["markdown_trace"])
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(trace, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(trace), encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture native Codex subagent spawn metadata from session JSONL logs."
    )
    parser.add_argument("--log-root", help="Codex sessions root containing rollout-*.jsonl logs.")
    parser.add_argument("--parent-thread-id", help="Parent Codex thread/session id to inspect.")
    parser.add_argument(
        "--expected-workflow",
        required=True,
        help=(
            "Expected workflow preset or JSON manifest path. JSON manifests may use "
            "expected_roles entries with role/agent_role/agent_type and optional call_id."
        ),
    )
    parser.add_argument(
        "--parent-start-line",
        type=int,
        default=None,
        help="First parent log line included in scoped validation.",
    )
    parser.add_argument(
        "--parent-end-line",
        type=int,
        default=None,
        help="Last parent log line included in scoped validation.",
    )
    parser.add_argument("--run-dir", required=True, help="Output directory under .codex-harness/reports/.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parser-only capture mode; no external execution is performed and trace files are still written.",
    )
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="Return success even when capture_status is partial. Default exits 1 for partial captures.",
    )
    parser.add_argument(
        "--include-excerpts",
        action="store_true",
        help="Include short sanitized prompt/result excerpts. Default records hashes and char counts only.",
    )
    parser.add_argument(
        "--capture-results",
        action="store_true",
        help="Write in-scope child result markdown files using task_complete.last_agent_message, with fallback metadata.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        trace = build_trace(args)
        if args.capture_results:
            trace["result_capture"]["files"] = write_result_files(trace)
        write_trace(trace)
    except NotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    except AdapterError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"ERROR: failed to write trace: {exc}", file=sys.stderr)
        return 2

    print(f"wrote {trace['source_paths']['json_trace']}")
    print(f"wrote {trace['source_paths']['markdown_trace']}")
    print(f"capture_status: {trace['capture_status']}")
    if trace["warnings"]:
        print(f"warnings: {len(trace['warnings'])}")
    if trace["capture_status"] == "partial" and not args.allow_partial:
        print("ERROR: partial capture; pass --allow-partial to treat it as success", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
