#!/usr/bin/env python3
"""Offline App Server shadow adapter for native subagent capture evidence.

This tool reads previously captured App Server event JSONL and compares the
observed subagent link/result evidence against the accepted JSONL adapter trace.
It is intentionally shadow-only: it does not spawn agents, execute Codex, run
aggregation, or mutate the accepted JSONL baseline.
"""

from __future__ import annotations

import argparse
import queue
import shutil
import subprocess
import threading
import datetime as _dt
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
REPORTS_ROOT = ROOT / ".codex-harness" / "reports"
SCHEMA = "codex-harness.app_server_shadow_capture.v1"
CLASS_EQUIVALENT = "SHADOW_EQUIVALENT"
CLASS_LINK_EQUIVALENT_RESULT_UNPROVEN = "SHADOW_LINK_EQUIVALENT_RESULT_UNPROVEN"
CLASS_LINK_MISMATCH = "SHADOW_LINK_MISMATCH"
CLASS_RESULT_MISMATCH = "SHADOW_RESULT_MISMATCH"
CLASS_INSUFFICIENT_EVENTS = "SHADOW_INSUFFICIENT_EVENTS"
CLASS_V9_BLOCKED = "V9_BLOCKED_ISOLATED_APP_SERVER_CLIENT_UNAVAILABLE"
CLASS_V10_EQUIVALENT = "V10_CANDIDATE_EQUIVALENT"
CLASS_V10_NOT_EQUIVALENT = "V10_CANDIDATE_NOT_EQUIVALENT"
V11_EQUIVALENT = "CANDIDATE_EQUIVALENT"
V11_LINK_EQUIVALENT_RESULT_MISMATCH = "LINK_EQUIVALENT_RESULT_MISMATCH"
V11_LINK_EQUIVALENT_RESULT_UNPROVEN = "LINK_EQUIVALENT_RESULT_UNPROVEN"
V11_LINK_MISMATCH = "LINK_MISMATCH"
V11_CANDIDATE_CAPTURE_FAILED = "CANDIDATE_CAPTURE_FAILED"
V11_JSONL_CAPTURE_FAILED = "JSONL_CAPTURE_FAILED"
V11_AGGREGATION_MISMATCH = "AGGREGATION_MISMATCH"
V11_BLOCKED_BY_RUNTIME_LIMIT = "BLOCKED_BY_RUNTIME_LIMIT"
NATIVE_ADAPTER_SCRIPT = ROOT / ".codex-harness" / "tools" / "codex_native_subagent_adapter.py"
AGGREGATOR_SCRIPT = ROOT / ".codex-harness" / "tools" / "aggregate_subagent_results.py"
DEFAULT_LIVE_TIMEOUT_SECONDS = 180
DEFAULT_RESULT_FILES = {
    "project-explorer": "01_project_explorer_result.md",
    "project-implementer": "02_project_implementer_result.md",
    "project-reviewer": "03_project_reviewer_result.md",
    "project-repairer": "04_project_repairer_result.md",
}
V11_PATTERN_SPECS = {
    "project-explorer-only": {
        "slug": "project_explorer_only",
        "label": "project-explorer-only",
        "roles": [
            {
                "logical_role": "project-explorer",
                "agent_type": "project-explorer",
                "result_file": "01_project_explorer_result.md",
                "instruction": "Confirm the V11 project-explorer-only parity probe in a read-only way.",
            }
        ],
    },
    "explorer-implementer-reviewer": {
        "slug": "explorer_implementer_reviewer",
        "label": "explorer-implementer-reviewer",
        "roles": [
            {
                "logical_role": "project-explorer",
                "agent_type": "project-explorer",
                "result_file": "01_project_explorer_result.md",
                "instruction": "Explore the harmless V11 parity probe constraints.",
            },
            {
                "logical_role": "project-implementer",
                "agent_type": "project-implementer",
                "result_file": "02_project_implementer_result.md",
                "instruction": "Confirm that no implementation is needed for the harmless V11 parity probe.",
            },
            {
                "logical_role": "project-reviewer",
                "agent_type": "project-reviewer",
                "result_file": "03_project_reviewer_result.md",
                "instruction": "Review the harmless V11 parity probe and use RESULT: PASS only if no blocking issue exists.",
            },
        ],
    },
    "producer-reviewer": {
        "slug": "producer_reviewer",
        "label": "producer-reviewer",
        "roles": [
            {
                "logical_role": "producer",
                "agent_type": "project-implementer",
                "result_file": "01_producer_result.md",
                "instruction": "Produce a no-op confirmation for the harmless V11 producer-reviewer parity probe.",
            },
            {
                "logical_role": "reviewer",
                "agent_type": "project-reviewer",
                "result_file": "02_reviewer_result.md",
                "instruction": "Review the producer no-op confirmation and use RESULT: PASS only if no blocking issue exists.",
            },
        ],
    },
    "planner-generator-evaluator": {
        "slug": "planner_generator_evaluator",
        "label": "planner-generator-evaluator",
        "roles": [
            {
                "logical_role": "planner",
                "agent_type": "project-explorer",
                "result_file": "01_planner_result.md",
                "instruction": "Plan the harmless V11 planner-generator-evaluator parity probe.",
            },
            {
                "logical_role": "generator",
                "agent_type": "project-implementer",
                "result_file": "02_generator_result.md",
                "instruction": "Generate a no-op confirmation for the harmless V11 parity probe.",
            },
            {
                "logical_role": "evaluator",
                "agent_type": "project-reviewer",
                "result_file": "03_evaluator_result.md",
                "instruction": "Evaluate the no-op generated confirmation and use RESULT: PASS only if no blocking issue exists.",
            },
        ],
    },
}
V11_REQUIRED_PATTERNS = [
    "project-explorer-only",
    "explorer-implementer-reviewer",
    "producer-reviewer",
    "planner-generator-evaluator",
]
V13_PATTERN_SPEC = {
    "slug": "fan_out_fan_in",
    "label": "fan-out-fan-in",
    "roles": [
        {
            "logical_role": "producer[0]",
            "agent_type": "project-implementer",
            "result_file": "01_producer_0_result.md",
            "instruction": (
                "Produce the first independent V13 summary. Output exactly "
                "RESULT: PASS followed by V13 producer[0] alpha independent summary."
            ),
        },
        {
            "logical_role": "producer[1]",
            "agent_type": "project-implementer",
            "result_file": "02_producer_1_result.md",
            "instruction": (
                "Produce the second independent V13 summary. Output exactly "
                "RESULT: PASS followed by V13 producer[1] beta independent summary."
            ),
        },
        {
            "logical_role": "reviewer",
            "agent_type": "project-reviewer",
            "result_file": "03_reviewer_result.md",
            "instruction": (
                "Review both producer outputs. Output RESULT: PASS only if both "
                "producer outputs are present and strict conditions are satisfied."
            ),
        },
    ],
}


class ShadowError(Exception):
    """User-facing shadow adapter error."""


def repo_rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def resolve_path(path_text: str) -> Path:
    path = Path(path_text)
    return path if path.is_absolute() else ROOT / path


def ensure_input_file(path_text: str, label: str) -> Path:
    path = resolve_path(path_text).resolve()
    if not path.is_file():
        raise ShadowError(f"{label} does not exist or is not a file: {path_text}")
    return path


def ensure_out_dir(path_text: str) -> Path:
    path = resolve_path(path_text).resolve()
    try:
        path.relative_to(REPORTS_ROOT.resolve())
    except ValueError as exc:
        raise ShadowError(
            f"out-dir must be under .codex-harness/reports/: {repo_rel(path)}"
        ) from exc
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_live_workspace(path_text: str) -> Path:
    path = resolve_path(path_text).resolve()
    try:
        rel = path.relative_to(ROOT).as_posix().lower()
    except ValueError as exc:
        raise ShadowError(f"live workspace must be inside repo: {path_text}") from exc
    if not rel.startswith(".codex-harness/"):
        raise ShadowError(
            "live workspace must be scratch-only or harness-only under .codex-harness/"
        )
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_live_expected_workflow_path(path_text: str | None, out_dir: Path) -> Path:
    if not path_text:
        return out_dir / "expected_workflow.json"
    path = resolve_path(path_text).resolve()
    try:
        path.relative_to(out_dir.resolve())
    except ValueError as exc:
        raise ShadowError(
            "live --expected-workflow must point inside the live --out-dir "
            "because live mode writes concrete call_id mappings"
        ) from exc
    return path


def sha256_text(text: str | None) -> str | None:
    if text is None:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ShadowError(f"{label} is malformed JSON: {repo_rel(path)}: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ShadowError(f"{label} must be a JSON object: {repo_rel(path)}")
    return payload


def load_expected_workflow(path_text: str | None) -> dict[str, Any] | None:
    if not path_text:
        return None
    return read_json(ensure_input_file(path_text, "expected-workflow"), "expected-workflow")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def parse_json_maybe(text: Any) -> Any:
    if not isinstance(text, str):
        return text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def summarize_event_file(path: Path) -> dict[str, Any]:
    counts: dict[str, int] = {}
    keyword_hits = {
        "collabAgentToolCall": 0,
        "function_call_output": 0,
        "receiverThreadIds": 0,
        "agent_id": 0,
        "spawn_agent": 0,
        "project-explorer": 0,
        "RESULT: PASS": 0,
        "turn/completed": 0,
        "rawResponseItem/completed": 0,
    }
    raw_item_types: list[str | None] = []
    line_count = 0
    for line_count, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        for keyword in keyword_hits:
            if keyword in line:
                keyword_hits[keyword] += 1
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = record.get("payload") if isinstance(record, dict) else None
        if isinstance(payload, dict):
            key = payload.get("method") or ("<response>" if "id" in payload else "<other>")
            counts[key] = counts.get(key, 0) + 1
            item = event_item(payload)
            if payload.get("method") == "rawResponseItem/completed" and item:
                raw_item_types.append(item.get("type"))
        else:
            counts["<non_dict_payload>"] = counts.get("<non_dict_payload>", 0) + 1
    return {
        "line_count": line_count,
        "event_counts": counts,
        "keyword_hits": keyword_hits,
        "raw_item_types": raw_item_types,
    }


def iter_event_records(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    malformed: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            malformed.append(
                {
                    "line": line_number,
                    "message": exc.msg,
                    "raw_excerpt": line[:200],
                }
            )
            continue
        if isinstance(parsed, dict):
            parsed["_line"] = line_number
            records.append(parsed)
    return records, malformed


def event_payload(record: dict[str, Any]) -> dict[str, Any] | None:
    payload = record.get("payload")
    return payload if isinstance(payload, dict) else None


def event_item(payload: dict[str, Any]) -> dict[str, Any] | None:
    params = payload.get("params")
    if not isinstance(params, dict):
        return None
    item = params.get("item")
    return item if isinstance(item, dict) else None


def params_thread_id(payload: dict[str, Any]) -> str | None:
    params = payload.get("params")
    if isinstance(params, dict) and isinstance(params.get("threadId"), str):
        return params["threadId"]
    return None


def find_thread_started(records: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    for record in records:
        payload = event_payload(record)
        if not payload:
            continue
        if payload.get("method") == "thread/start" and isinstance(payload.get("params"), dict):
            continue
        if payload.get("method") == "thread/started":
            thread = payload.get("params", {}).get("thread") if isinstance(payload.get("params"), dict) else None
            if isinstance(thread, dict):
                return thread.get("id"), thread.get("path")
        if "result" in payload and isinstance(payload.get("result"), dict):
            thread = payload["result"].get("thread")
            if isinstance(thread, dict) and isinstance(thread.get("id"), str):
                return thread.get("id"), thread.get("path")
    return None, None


def app_server_live_prompt(probe_label: str = "V9") -> str:
    return (
        f"{probe_label} App Server live shadow/candidate probe. Use exactly one "
        "native project-explorer subagent for this harmless read-only harness "
        "summary. Do not run shell commands. Do not inspect business files. "
        "Do not modify files. Ask the child to output exactly two lines: "
        f"RESULT: PASS, then App Server {probe_label} live child completed. "
        "After the child completes, reply exactly two lines: RESULT: PASS, "
        f"then Parent App Server {probe_label} live completed."
    )


def child_prompt_for_summary(probe_label: str = "V9") -> str:
    return (
        f"Harmless {probe_label} App Server live probe. Do not run shell commands. "
        "Do not inspect business files. Do not modify files. Output exactly two lines:\n"
        "RESULT: PASS\n"
        f"App Server {probe_label} live child completed"
    )


def app_server_pattern_prompt(pattern: dict[str, Any]) -> str:
    label = str(pattern["label"])
    lines = [
        f"V11 App Server parity probe for pattern {label}.",
        "Use native Codex subagents exactly in the order listed below.",
        "Use the requested agent_type for each native subagent call.",
        "Wait for each child before launching the next child.",
        "Do not run shell commands.",
        "Do not inspect business files.",
        "Do not modify files.",
        "Use only this scratch/harness-only task context.",
        "Each child must output exactly two lines:",
        "RESULT: PASS",
        "V11 <pattern> <logical_role> completed",
        "Reviewer/evaluator roles must not use RESULT: WARN as the top-level verdict.",
        "",
        "Subagent calls:",
    ]
    for index, role in enumerate(pattern["roles"], 1):
        lines.extend(
            [
                f"{index}. agent_type={role['agent_type']} logical_role={role['logical_role']}",
                f"   task={role['instruction']}",
                (
                    "   child_output_line_2="
                    f"V11 {label} {role['logical_role']} completed"
                ),
            ]
        )
    lines.extend(
        [
            "",
            "After all children complete, reply exactly two lines:",
            "RESULT: PASS",
            f"Parent V11 {label} completed",
        ]
    )
    return "\n".join(lines)


def app_server_v13_fanout_prompt(pattern: dict[str, Any]) -> str:
    return "\n".join(
        [
            "V13 App Server fan-out/fan-in parity probe.",
            "Use native Codex subagents exactly as requested below.",
            "Do not run shell commands.",
            "Do not inspect business files.",
            "Do not modify files.",
            "Use only this scratch/harness-only task context.",
            "Launch exactly two independent producer subagents first. If the runtime supports it, launch both producer calls before waiting; if it serializes calls, keep them independent and launch the reviewer only after both producer results are available.",
            "",
            "Producer calls:",
            "1. agent_type=project-implementer logical_role=producer[0]",
            "   task=Return exactly two lines:",
            "   RESULT: PASS",
            "   V13 producer[0] alpha independent summary",
            "2. agent_type=project-implementer logical_role=producer[1]",
            "   task=Return exactly two lines:",
            "   RESULT: PASS",
            "   V13 producer[1] beta independent summary",
            "",
            "Reviewer call:",
            "3. agent_type=project-reviewer logical_role=reviewer",
            "   Include both producer outputs in the reviewer prompt, including the tokens producer[0], producer[1], alpha, and beta.",
            "   The reviewer must return exactly two lines:",
            "   RESULT: PASS",
            "   V13 reviewer saw producer[0] alpha and producer[1] beta",
            "",
            "After all three children complete, reply exactly two lines:",
            "RESULT: PASS",
            f"Parent V13 {pattern['label']} completed",
        ]
    )


def log_live_event(events_path: Path, start_time: float, direction: str, payload: Any) -> None:
    record = {
        "ts": time.time(),
        "elapsed_s": round(time.time() - start_time, 3),
        "direction": direction,
        "payload": payload,
    }
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def run_live_app_server_probe(
    workspace: Path,
    out_dir: Path,
    timeout_seconds: int,
    probe_label: str = "V9",
    prompt_text: str | None = None,
) -> dict[str, Any]:
    events_path = out_dir / "app_server_events.jsonl"
    if events_path.exists():
        events_path.unlink()

    cmd = ["codex", "app-server", "--listen", "stdio://"]
    start_time = time.time()
    try:
        process = subprocess.Popen(
            cmd,
            cwd=workspace,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as exc:
        raise ShadowError(f"{CLASS_V9_BLOCKED}: unable to launch isolated App Server: {exc}") from exc

    output_queue: queue.Queue[tuple[str, str]] = queue.Queue()

    def reader(stream: Any, direction: str) -> None:
        for line in stream:
            output_queue.put((direction, line.rstrip("\n")))

    assert process.stdout is not None
    assert process.stderr is not None
    assert process.stdin is not None
    threading.Thread(target=reader, args=(process.stdout, "server_stdout"), daemon=True).start()
    threading.Thread(target=reader, args=(process.stderr, "server_stderr"), daemon=True).start()

    def send(payload: dict[str, Any]) -> None:
        log_live_event(events_path, start_time, "client_stdin", payload)
        assert process.stdin is not None
        process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        process.stdin.flush()

    parent_thread_id: str | None = None
    parent_turn_id: str | None = None
    parent_log_path: str | None = None
    turn_sent = False
    turn_start_response_seen = False
    parent_turn_completed = False
    child_turn_completed = False

    send(
        {
            "method": "initialize",
            "id": 1,
            "params": {
                "clientInfo": {"name": "v9-app-server-live-shadow", "version": "0"},
                "capabilities": {"experimentalApi": True},
            },
        }
    )
    send({"method": "initialized", "params": {}})
    send(
        {
            "method": "thread/start",
            "id": 2,
            "params": {
                "cwd": str(workspace),
                "approvalPolicy": "never",
                "sandbox": "read-only",
                "ephemeral": False,
                "experimentalRawEvents": True,
                "persistExtendedHistory": True,
            },
        }
    )

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            direction, line = output_queue.get(timeout=0.2)
        except queue.Empty:
            if process.poll() is not None:
                break
            continue
        try:
            payload: Any = json.loads(line)
        except json.JSONDecodeError:
            payload = line
        log_live_event(events_path, start_time, direction, payload)

        if not isinstance(payload, dict):
            continue
        if payload.get("id") == 2 and isinstance(payload.get("result"), dict):
            thread = payload["result"].get("thread")
            if isinstance(thread, dict):
                parent_thread_id = thread.get("id")
                parent_log_path = thread.get("path")
                if parent_thread_id and not turn_sent:
                    send(
                        {
                            "method": "turn/start",
                            "id": 3,
                            "params": {
                                "threadId": parent_thread_id,
                                "input": [
                                    {
                                        "type": "text",
                                        "text": prompt_text or app_server_live_prompt(probe_label),
                                        "text_elements": [],
                                    }
                                ],
                                "approvalPolicy": "never",
                                "effort": "low",
                            },
                        }
                    )
                    turn_sent = True
        if payload.get("id") == 3:
            turn_start_response_seen = True
            turn = payload.get("result", {}).get("turn") if isinstance(payload.get("result"), dict) else None
            if isinstance(turn, dict):
                parent_turn_id = turn.get("id")
        if payload.get("method") == "rawResponseItem/completed":
            item = event_item(payload)
            if (
                item
                and item.get("type") == "function_call"
                and item.get("name") == "spawn_agent"
                and isinstance(item.get("arguments"), str)
            ):
                args = parse_json_maybe(item["arguments"])
                if isinstance(args, dict) and "message" not in args:
                    # The model may choose concise arguments. This branch is only
                    # diagnostic; we do not mutate the stream.
                    pass
        if payload.get("method") == "turn/completed":
            params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
            if params.get("threadId") == parent_thread_id:
                parent_turn_completed = True
                break
            child_turn_completed = True

    try:
        process.terminate()
        process.wait(timeout=5)
    except Exception:
        process.kill()
    while True:
        try:
            direction, line = output_queue.get_nowait()
        except queue.Empty:
            break
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            payload = line
        log_live_event(events_path, start_time, direction, payload)

    summary = {
        "client_feasible": True,
        "cmd": cmd,
        "scratch_workspace": str(workspace),
        "parent_thread_id": parent_thread_id,
        "parent_turn_id": parent_turn_id,
        "parent_log_path": parent_log_path,
        "turn_start_response_seen": turn_start_response_seen,
        "parent_turn_completed_seen": parent_turn_completed,
        "child_turn_completed_seen": child_turn_completed,
        "process_returncode_after_terminate": process.returncode,
        "events_path": str(events_path),
    }
    summary.update(summarize_event_file(events_path))
    write_json(out_dir / "client_probe_summary.json", summary)
    if not parent_thread_id or not turn_start_response_seen or not parent_turn_completed:
        raise ShadowError(
            f"{CLASS_V9_BLOCKED}: isolated App Server client did not complete the parent turn"
        )
    return summary


def extract_live_spawn(records: list[dict[str, Any]]) -> dict[str, Any]:
    spawn_call: dict[str, Any] | None = None
    collab_completed: dict[str, Any] | None = None
    raw_output: dict[str, Any] | None = None
    for record in records:
        payload = event_payload(record)
        if not payload:
            continue
        item = event_item(payload)
        if not item:
            continue
        if item.get("type") == "function_call" and item.get("name") == "spawn_agent":
            args = parse_json_maybe(item.get("arguments"))
            spawn_call = {
                "call_id": item.get("call_id"),
                "agent_type": args.get("agent_type") if isinstance(args, dict) else None,
                "message": args.get("message") if isinstance(args, dict) else None,
                "line": record.get("_line"),
            }
        elif item.get("type") == "collabAgentToolCall" and item.get("tool") == "spawnAgent":
            if item.get("status") == "completed":
                collab_completed = {
                    "call_id": item.get("id"),
                    "senderThreadId": item.get("senderThreadId"),
                    "receiverThreadIds": item.get("receiverThreadIds"),
                    "line": record.get("_line"),
                }
        elif item.get("type") == "function_call_output":
            output = parse_json_maybe(item.get("output"))
            if isinstance(output, dict) and isinstance(output.get("agent_id"), str):
                raw_output = {
                    "call_id": item.get("call_id"),
                    "agent_id": output.get("agent_id"),
                    "line": record.get("_line"),
                }
    call_id = (spawn_call or {}).get("call_id") or (collab_completed or {}).get("call_id")
    if not isinstance(call_id, str):
        raise ShadowError("Live App Server events did not include a spawn_agent call id.")
    child_thread_id = None
    receivers = (collab_completed or {}).get("receiverThreadIds")
    if isinstance(receivers, list):
        string_receivers = [item for item in receivers if isinstance(item, str)]
        if len(string_receivers) == 1:
            child_thread_id = string_receivers[0]
    if child_thread_id is None and isinstance((raw_output or {}).get("agent_id"), str):
        child_thread_id = raw_output["agent_id"]
    if not child_thread_id:
        raise ShadowError("Live App Server events did not include a deterministic child thread id.")
    return {
        "call_id": call_id,
        "agent_type": (spawn_call or {}).get("agent_type") or "project-explorer",
        "child_thread_id": child_thread_id,
        "senderThreadId": (collab_completed or {}).get("senderThreadId"),
        "receiverThreadIds": receivers if isinstance(receivers, list) else [],
        "raw_function_call_output_agent_id": (raw_output or {}).get("agent_id"),
    }


def extract_live_spawns(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    spawn_calls: list[dict[str, Any]] = []
    collab_by_call: dict[str, dict[str, Any]] = {}
    output_by_call: dict[str, dict[str, Any]] = {}
    for record in records:
        payload = event_payload(record)
        if not payload:
            continue
        item = event_item(payload)
        if not item:
            continue
        item_type = item.get("type")
        if item_type == "function_call" and item.get("name") == "spawn_agent":
            call_id = item.get("call_id")
            if not isinstance(call_id, str):
                continue
            args = parse_json_maybe(item.get("arguments"))
            spawn_calls.append(
                {
                    "call_id": call_id,
                    "agent_type": args.get("agent_type") if isinstance(args, dict) else None,
                    "message": args.get("message") if isinstance(args, dict) else None,
                    "line": record.get("_line"),
                }
            )
        elif item_type == "collabAgentToolCall" and item.get("tool") == "spawnAgent":
            call_id = item.get("id")
            if isinstance(call_id, str) and item.get("status") == "completed":
                collab_by_call[call_id] = {
                    "call_id": call_id,
                    "senderThreadId": item.get("senderThreadId"),
                    "receiverThreadIds": item.get("receiverThreadIds")
                    if isinstance(item.get("receiverThreadIds"), list)
                    else [],
                    "line": record.get("_line"),
                }
        elif item_type == "function_call_output":
            call_id = item.get("call_id")
            if not isinstance(call_id, str):
                continue
            output = parse_json_maybe(item.get("output"))
            if isinstance(output, dict) and isinstance(output.get("agent_id"), str):
                output_by_call[call_id] = {
                    "call_id": call_id,
                    "agent_id": output.get("agent_id"),
                    "nickname": output.get("nickname"),
                    "line": record.get("_line"),
                }

    spawns: list[dict[str, Any]] = []
    for spawn in spawn_calls:
        call_id = spawn["call_id"]
        collab = collab_by_call.get(call_id, {})
        raw_output = output_by_call.get(call_id, {})
        receiver_ids = collab.get("receiverThreadIds") if isinstance(collab.get("receiverThreadIds"), list) else []
        string_receivers = [item for item in receiver_ids if isinstance(item, str)]
        child_thread_id = string_receivers[0] if len(string_receivers) == 1 else raw_output.get("agent_id")
        if not isinstance(child_thread_id, str):
            continue
        spawns.append(
            {
                "call_id": call_id,
                "agent_type": spawn.get("agent_type"),
                "message": spawn.get("message"),
                "line": spawn.get("line"),
                "child_thread_id": child_thread_id,
                "senderThreadId": collab.get("senderThreadId"),
                "receiverThreadIds": receiver_ids,
                "raw_function_call_output_agent_id": raw_output.get("agent_id"),
            }
        )
    return spawns


def write_live_expected_workflow(
    path: Path,
    live_summary: dict[str, Any],
    live_spawn: dict[str, Any],
    workspace: Path,
    run_id: str = "v9_app_server_live_shadow_probe",
    source_note: str = "V9 isolated App Server live shadow probe",
) -> None:
    payload = {
        "schema_version": "codex-harness.expected_workflow.v1",
        "run_id": run_id,
        "parent_thread_id": live_summary.get("parent_thread_id"),
        "log_root": str(Path.home() / ".codex" / "sessions"),
        "cwd": str(workspace),
        "expected_roles": [
            {
                "order": 1,
                "agent_type": live_spawn.get("agent_type") or "project-explorer",
                "call_id": live_spawn.get("call_id"),
                "required": True,
                "result_file": "01_project_explorer_result.md",
            }
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
        "notes": {
            "source": source_note,
            "app_server_thread_id": live_summary.get("parent_thread_id"),
            "child_thread_id": live_spawn.get("child_thread_id"),
            "app_server_link_evidence": "collabAgentToolCall.receiverThreadIds and rawResponseItem function_call_output.agent_id",
            "shadow_only": True,
        },
    }
    write_json(path, payload)


def write_pattern_expected_workflow(
    path: Path,
    *,
    live_summary: dict[str, Any],
    live_spawns: list[dict[str, Any]],
    pattern: dict[str, Any],
    workspace: Path,
    run_id: str,
) -> None:
    roles: list[dict[str, Any]] = []
    for index, role in enumerate(pattern["roles"], 1):
        observed = live_spawns[index - 1] if index - 1 < len(live_spawns) else {}
        roles.append(
            {
                "order": index,
                "agent_type": role["agent_type"],
                "logical_role": role["logical_role"],
                "observed_agent_type": observed.get("agent_type"),
                "call_id": observed.get("call_id"),
                "required": True,
                "result_file": role["result_file"],
            }
        )
    payload = {
        "schema_version": "codex-harness.expected_workflow.v1",
        "run_id": run_id,
        "pattern_name": pattern["label"],
        "parent_thread_id": live_summary.get("parent_thread_id"),
        "log_root": str(Path.home() / ".codex" / "sessions"),
        "cwd": str(workspace),
        "expected_roles": roles,
        "privacy": {
            "redact_prompts": True,
            "store_prompt_hash": True,
            "include_prompts": False,
            "strict_candidate_shadow_only": True,
        },
        "match": {
            "require_same_parent_thread": True,
            "require_child_parent_thread_match": True,
        },
        "notes": {
            "source": f"{run_id} isolated App Server parity probe",
            "app_server_thread_id": live_summary.get("parent_thread_id"),
            "pattern": pattern["label"],
            "authoritative_baseline": "JSONL",
            "replacement_accepted": False,
            "shadow_only": True,
        },
    }
    write_json(path, payload)


def run_native_jsonl_trace(
    expected_workflow_path: Path,
    out_dir: Path,
    capture_results: bool = False,
    require_success: bool = True,
) -> dict[str, Any]:
    command = [
        sys.executable,
        str(NATIVE_ADAPTER_SCRIPT),
        "--expected-workflow",
        str(expected_workflow_path),
        "--run-dir",
        str(out_dir),
        "--dry-run",
    ]
    if capture_results:
        command.append("--capture-results")
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    adapter_run = {
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }
    write_json(out_dir / "jsonl_adapter_invocation.json", adapter_run)
    if completed.returncode != 0 and require_success:
        raise ShadowError(
            "codex_native_subagent_adapter.py failed in live shadow mode "
            f"with exit code {completed.returncode}: {completed.stderr.strip()}"
        )
    source = out_dir / "spawn_capture_trace.json"
    target = out_dir / "jsonl_parallel_trace.json"
    if not source.is_file():
        if not require_success:
            fallback = {
                "schema": "codex-harness.native_subagent_capture.v1",
                "capture_backend": "codex-native-subagent",
                "adapter_exit_code": completed.returncode,
                "capture_status": "capture_failed",
                "allow_partial": False,
                "workflow_validation_scope": "expected_call_ids",
                "error": completed.stderr.strip() or completed.stdout.strip(),
            }
            write_json(target, fallback)
            return fallback
        raise ShadowError("Native JSONL adapter did not write spawn_capture_trace.json")
    shutil.copyfile(source, target)
    return read_json(target, "jsonl_parallel_trace")


def expected_role_specs(expected_workflow: dict[str, Any] | None, jsonl_trace: dict[str, Any]) -> list[dict[str, Any]]:
    if expected_workflow and isinstance(expected_workflow.get("expected_roles"), list):
        specs: list[dict[str, Any]] = []
        for index, item in enumerate(expected_workflow["expected_roles"], 1):
            if not isinstance(item, dict):
                continue
            role = item.get("agent_type") or item.get("role") or item.get("agent_role")
            logical_role = item.get("logical_role") or item.get("role_id")
            call_id = item.get("call_id")
            if isinstance(role, str) and isinstance(call_id, str):
                specs.append(
                    {
                        "order": item.get("order", index),
                        "role": role,
                        "agent_type": role,
                        "logical_role": logical_role if isinstance(logical_role, str) else role,
                        "call_id": call_id,
                        "result_file": item.get("result_file"),
                    }
                )
        if specs:
            return sorted(specs, key=lambda item: item.get("order") or 0)

    specs = []
    for index, spawn in enumerate(jsonl_trace.get("spawns", []) or [], 1):
        if not isinstance(spawn, dict):
            continue
        call_id = spawn.get("call_id")
        role = spawn.get("agent_type") or spawn.get("agent_role")
        if isinstance(call_id, str) and isinstance(role, str):
            specs.append(
                {
                    "order": index,
                    "role": role,
                    "agent_type": role,
                    "logical_role": role,
                    "call_id": call_id,
                    "result_file": None,
                }
            )
    return specs


def jsonl_spawn_by_call_id(jsonl_trace: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for spawn in jsonl_trace.get("spawns", []) or []:
        if isinstance(spawn, dict) and isinstance(spawn.get("call_id"), str):
            out[spawn["call_id"]] = spawn
    return out


def collect_app_server_evidence(
    records: list[dict[str, Any]],
    expected_specs: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    expected_call_ids = {spec["call_id"] for spec in expected_specs if isinstance(spec.get("call_id"), str)}
    evidence: dict[str, dict[str, Any]] = {
        call_id: {
            "call_id": call_id,
            "tool_call_id": call_id,
            "role": next((s.get("role") for s in expected_specs if s.get("call_id") == call_id), None),
            "raw_spawn_function_call": None,
            "raw_function_call_output": None,
            "collab_started": None,
            "collab_completed": None,
            "wait_collab_completed": [],
            "result_candidates": [],
            "child_parent_thread_id": None,
            "child_parent_thread_id_source": None,
        }
        for call_id in expected_call_ids
    }

    call_id_for_child: dict[str, str] = {}
    wait_call_ids: dict[str, str] = {}
    diagnostics = {
        "raw_function_call_count": 0,
        "raw_function_call_output_count": 0,
        "collab_agent_tool_call_count": 0,
        "agent_message_count": 0,
        "turn_completed_count": 0,
    }

    def ensure_call(call_id: str) -> dict[str, Any]:
        if call_id not in evidence:
            evidence[call_id] = {
                "call_id": call_id,
                "tool_call_id": call_id,
                "role": None,
                "raw_spawn_function_call": None,
                "raw_function_call_output": None,
                "collab_started": None,
                "collab_completed": None,
                "wait_collab_completed": [],
                "result_candidates": [],
                "child_parent_thread_id": None,
                "child_parent_thread_id_source": None,
            }
        return evidence[call_id]

    for record in records:
        payload = event_payload(record)
        if not payload:
            continue
        method = payload.get("method")
        if method == "turn/completed":
            diagnostics["turn_completed_count"] += 1
        item = event_item(payload)
        if not item:
            continue

        item_type = item.get("type")
        if item_type == "collabAgentToolCall":
            diagnostics["collab_agent_tool_call_count"] += 1
            call_id = item.get("id")
            if not isinstance(call_id, str):
                continue
            tool = item.get("tool")
            if tool == "spawnAgent":
                entry = ensure_call(call_id)
                collab_payload = {
                    "line": record.get("_line"),
                    "status": item.get("status"),
                    "tool": tool,
                    "senderThreadId": item.get("senderThreadId"),
                    "receiverThreadIds": item.get("receiverThreadIds") if isinstance(item.get("receiverThreadIds"), list) else [],
                    "prompt": item.get("prompt"),
                    "prompt_sha256": sha256_text(item.get("prompt") if isinstance(item.get("prompt"), str) else None),
                    "model": item.get("model"),
                    "reasoningEffort": item.get("reasoningEffort"),
                    "agentsStates": item.get("agentsStates") if isinstance(item.get("agentsStates"), dict) else {},
                }
                if item.get("status") == "completed":
                    entry["collab_completed"] = collab_payload
                    for child_id in collab_payload["receiverThreadIds"]:
                        if isinstance(child_id, str):
                            call_id_for_child[child_id] = call_id
                else:
                    entry["collab_started"] = collab_payload
            elif tool == "wait":
                receiver_ids = item.get("receiverThreadIds") if isinstance(item.get("receiverThreadIds"), list) else []
                agents_states = item.get("agentsStates") if isinstance(item.get("agentsStates"), dict) else {}
                for child_id in receiver_ids:
                    if not isinstance(child_id, str):
                        continue
                    owner_call_id = call_id_for_child.get(child_id)
                    if owner_call_id:
                        wait_call_ids[call_id] = owner_call_id
                        if item.get("status") == "completed":
                            entry = ensure_call(owner_call_id)
                            entry["wait_collab_completed"].append(
                                {
                                    "line": record.get("_line"),
                                    "wait_call_id": call_id,
                                    "senderThreadId": item.get("senderThreadId"),
                                    "receiverThreadIds": receiver_ids,
                                    "agentsStates": agents_states,
                                }
                            )
                            child_state = agents_states.get(child_id)
                            if isinstance(child_state, dict) and isinstance(child_state.get("message"), str):
                                entry["result_candidates"].append(
                                    {
                                        "source": "collabAgentToolCall.wait.agentsStates.message",
                                        "line": record.get("_line"),
                                        "thread_id": child_id,
                                        "text": child_state["message"],
                                        "sha256": sha256_text(child_state["message"]),
                                    }
                                )

        elif item_type == "function_call":
            diagnostics["raw_function_call_count"] += 1
            call_id = item.get("call_id")
            name = item.get("name")
            if not isinstance(call_id, str):
                continue
            if name == "spawn_agent":
                args = parse_json_maybe(item.get("arguments"))
                message = args.get("message") if isinstance(args, dict) and isinstance(args.get("message"), str) else None
                agent_type = args.get("agent_type") if isinstance(args, dict) and isinstance(args.get("agent_type"), str) else None
                entry = ensure_call(call_id)
                entry["raw_spawn_function_call"] = {
                    "line": record.get("_line"),
                    "name": name,
                    "agent_type": agent_type,
                    "message": message,
                    "message_sha256": sha256_text(message),
                    "arguments_keys": sorted(args.keys()) if isinstance(args, dict) else [],
                }
            elif name == "wait_agent":
                args = parse_json_maybe(item.get("arguments"))
                targets = args.get("targets") if isinstance(args, dict) and isinstance(args.get("targets"), list) else []
                for child_id in targets:
                    if isinstance(child_id, str) and child_id in call_id_for_child:
                        wait_call_ids[call_id] = call_id_for_child[child_id]

        elif item_type == "function_call_output":
            diagnostics["raw_function_call_output_count"] += 1
            call_id = item.get("call_id")
            if not isinstance(call_id, str):
                continue
            output = parse_json_maybe(item.get("output"))
            if call_id in evidence:
                entry = ensure_call(call_id)
                agent_id = output.get("agent_id") if isinstance(output, dict) and isinstance(output.get("agent_id"), str) else None
                entry["raw_function_call_output"] = {
                    "line": record.get("_line"),
                    "agent_id": agent_id,
                    "nickname": output.get("nickname") if isinstance(output, dict) else None,
                    "output_sha256": sha256_text(item.get("output") if isinstance(item.get("output"), str) else None),
                }
                if agent_id:
                    call_id_for_child[agent_id] = call_id
            elif call_id in wait_call_ids:
                owner_call_id = wait_call_ids[call_id]
                entry = ensure_call(owner_call_id)
                if isinstance(output, dict) and isinstance(output.get("status"), dict):
                    for child_id, status in output["status"].items():
                        if not isinstance(status, dict) or not isinstance(status.get("completed"), str):
                            continue
                        entry["result_candidates"].append(
                            {
                                "source": "rawResponseItem.function_call_output.wait_agent.status.completed",
                                "line": record.get("_line"),
                                "thread_id": child_id,
                                "text": status["completed"],
                                "sha256": sha256_text(status["completed"]),
                            }
                        )

        elif item_type == "agentMessage":
            diagnostics["agent_message_count"] += 1
            text = item.get("text")
            thread_id = params_thread_id(payload)
            if not isinstance(text, str) or not thread_id:
                continue
            owner_call_id = call_id_for_child.get(thread_id)
            if owner_call_id and item.get("phase") == "final_answer" and text:
                entry = ensure_call(owner_call_id)
                entry["result_candidates"].append(
                    {
                        "source": "item.completed.agentMessage.final_answer",
                        "line": record.get("_line"),
                        "thread_id": thread_id,
                        "text": text,
                        "sha256": sha256_text(text),
                    }
                )

    return evidence, diagnostics


def summarize_app_evidence(
    evidence: dict[str, dict[str, Any]],
    expected_specs: list[dict[str, Any]],
    parent_thread_id_hint: str | None,
    parent_log_path: str | None,
) -> dict[str, Any]:
    expected_call_ids = [spec["call_id"] for spec in expected_specs if isinstance(spec.get("call_id"), str)]
    unresolved_link_call_ids: list[str] = []
    unresolved_result_roles: list[str] = []
    spawns: list[dict[str, Any]] = []

    for spec in expected_specs:
        call_id = spec.get("call_id")
        if not isinstance(call_id, str):
            continue
        entry = evidence.get(call_id, {})
        collab = entry.get("collab_completed") if isinstance(entry.get("collab_completed"), dict) else None
        raw_output = entry.get("raw_function_call_output") if isinstance(entry.get("raw_function_call_output"), dict) else None
        raw_spawn = entry.get("raw_spawn_function_call") if isinstance(entry.get("raw_spawn_function_call"), dict) else None
        receiver_ids = collab.get("receiverThreadIds") if collab else []
        receiver_ids = receiver_ids if isinstance(receiver_ids, list) else []
        agent_id = raw_output.get("agent_id") if raw_output else None
        child_thread_id = None
        link_mode = None
        if len([x for x in receiver_ids if isinstance(x, str)]) == 1:
            child_thread_id = next(x for x in receiver_ids if isinstance(x, str))
            link_mode = "app_server_collab_agent_tool_call"
        elif isinstance(agent_id, str):
            child_thread_id = agent_id
            link_mode = "app_server_function_call_output_agent_id"
        if isinstance(agent_id, str) and child_thread_id and agent_id != child_thread_id:
            unresolved_link_call_ids.append(call_id)
        elif not child_thread_id:
            unresolved_link_call_ids.append(call_id)

        result_candidates = entry.get("result_candidates") if isinstance(entry.get("result_candidates"), list) else []
        best_result = choose_result_candidate(result_candidates, child_thread_id)
        if not best_result:
            unresolved_result_roles.append(spec.get("role") or call_id)

        prompt = None
        prompt_source = None
        if collab and isinstance(collab.get("prompt"), str):
            prompt = collab["prompt"]
            prompt_source = "collabAgentToolCall.prompt"
        elif raw_spawn and isinstance(raw_spawn.get("message"), str):
            prompt = raw_spawn["message"]
            prompt_source = "rawResponseItem.function_call.arguments.message"
        child_parent_thread_id = entry.get("child_parent_thread_id")
        child_parent_thread_id_source = entry.get("child_parent_thread_id_source")
        if child_parent_thread_id is None and collab and isinstance(collab.get("senderThreadId"), str):
            child_parent_thread_id = collab.get("senderThreadId")
            child_parent_thread_id_source = "collabAgentToolCall.senderThreadId"

        spawns.append(
            {
                "order": spec.get("order"),
                "role": spec.get("role"),
                "agent_type": spec.get("agent_type"),
                "logical_role": spec.get("logical_role") or spec.get("role"),
                "call_id": call_id,
                "tool_call_id": call_id,
                "link_mode": link_mode,
                "senderThreadId": collab.get("senderThreadId") if collab else None,
                "receiverThreadIds": receiver_ids,
                "child_thread_id": child_thread_id,
                "function_call_output_agent_id": agent_id,
                "function_call_output_present": raw_output is not None,
                "collabAgentToolCall_present": collab is not None,
                "collabAgentToolCall_status": collab.get("status") if collab else None,
                "child_parent_thread_id": child_parent_thread_id,
                "child_parent_thread_id_source": child_parent_thread_id_source,
                "prompt": {
                    "source": prompt_source,
                    "chars": len(prompt) if prompt is not None else 0,
                    "sha256": sha256_text(prompt),
                    "present": prompt is not None,
                },
                "raw_prompt_sha256": raw_spawn.get("message_sha256") if raw_spawn else None,
                "result_candidate_source": best_result.get("source") if best_result else None,
                "result_candidate_sha256": best_result.get("sha256") if best_result else None,
                "result_candidate_thread_id": best_result.get("thread_id") if best_result else None,
                "result_candidate_line": best_result.get("line") if best_result else None,
                "result_candidate_present": best_result is not None,
                "result_candidates_seen": [
                    {
                        "source": item.get("source"),
                        "line": item.get("line"),
                        "thread_id": item.get("thread_id"),
                        "sha256": item.get("sha256"),
                        "chars": len(item.get("text", "")) if isinstance(item.get("text"), str) else 0,
                    }
                    for item in result_candidates
                ],
            }
        )

    parent_thread_ids = [
        spawn.get("senderThreadId")
        for spawn in spawns
        if isinstance(spawn.get("senderThreadId"), str)
    ]
    parent_thread_id = parent_thread_ids[0] if parent_thread_ids else parent_thread_id_hint
    link_modes = sorted({spawn.get("link_mode") for spawn in spawns if spawn.get("link_mode")})
    capture_status = "complete" if not unresolved_link_call_ids else "partial"

    return {
        "schema": SCHEMA,
        "capture_backend": "app-server-shadow",
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "parent_thread_id": parent_thread_id,
        "parent_log_path": parent_log_path,
        "expected_call_ids": expected_call_ids,
        "expected_roles": [spec.get("role") for spec in expected_specs],
        "capture_status": capture_status,
        "link_mode": link_modes[0] if len(link_modes) == 1 else ("mixed" if link_modes else None),
        "link_modes": link_modes,
        "spawns": spawns,
        "unresolved_link_call_ids": unresolved_link_call_ids,
        "unresolved_result_roles": unresolved_result_roles,
    }


def choose_result_candidate(candidates: list[dict[str, Any]], child_thread_id: str | None) -> dict[str, Any] | None:
    if not candidates:
        return None
    if child_thread_id:
        child_candidates = [item for item in candidates if item.get("thread_id") == child_thread_id]
        candidates = child_candidates or candidates
    preferred_sources = [
        "item.completed.agentMessage.final_answer",
        "collabAgentToolCall.wait.agentsStates.message",
        "rawResponseItem.function_call_output.wait_agent.status.completed",
    ]
    for source in preferred_sources:
        for item in candidates:
            if item.get("source") == source:
                return item
    return candidates[0]


def compare_shadow_to_jsonl(
    shadow_trace: dict[str, Any],
    jsonl_trace: dict[str, Any],
    expected_specs: list[dict[str, Any]],
) -> dict[str, Any]:
    jsonl_by_call = jsonl_spawn_by_call_id(jsonl_trace)
    comparisons: list[dict[str, Any]] = []
    link_mismatches: list[str] = []
    result_mismatches: list[str] = []
    result_unproven: list[str] = []
    insufficient: list[str] = []

    jsonl_parent_thread_id = jsonl_trace.get("summary", {}).get("parent_thread_id") or jsonl_trace.get("parent_thread_id")
    shadow_parent_thread_id = shadow_trace.get("parent_thread_id")
    parent_thread_match = (
        isinstance(jsonl_parent_thread_id, str)
        and isinstance(shadow_parent_thread_id, str)
        and jsonl_parent_thread_id == shadow_parent_thread_id
    )

    shadow_by_call = {
        spawn.get("call_id"): spawn
        for spawn in shadow_trace.get("spawns", [])
        if isinstance(spawn, dict) and isinstance(spawn.get("call_id"), str)
    }

    for spec in expected_specs:
        call_id = spec.get("call_id")
        role = spec.get("role")
        if not isinstance(call_id, str):
            continue
        shadow_spawn = shadow_by_call.get(call_id)
        jsonl_spawn = jsonl_by_call.get(call_id)
        if not shadow_spawn or not jsonl_spawn:
            insufficient.append(call_id)
            comparisons.append(
                {
                    "role": role,
                    "call_id": call_id,
                    "status": "missing_shadow_or_jsonl_spawn",
                    "shadow_present": bool(shadow_spawn),
                    "jsonl_present": bool(jsonl_spawn),
                }
            )
            continue

        jsonl_child_id = jsonl_spawn.get("child_thread_id")
        shadow_child_id = shadow_spawn.get("child_thread_id")
        jsonl_child_parent_thread_id = None
        child = jsonl_spawn.get("child")
        if isinstance(child, dict):
            session_meta = child.get("session_meta")
            if isinstance(session_meta, dict) and isinstance(session_meta.get("parent_thread_id"), str):
                jsonl_child_parent_thread_id = session_meta["parent_thread_id"]
        app_child_parent_thread_id = shadow_spawn.get("child_parent_thread_id")
        jsonl_agent_id = jsonl_spawn.get("function_call_output_agent_id")
        app_receiver_ids = shadow_spawn.get("receiverThreadIds") if isinstance(shadow_spawn.get("receiverThreadIds"), list) else []
        app_agent_id = shadow_spawn.get("function_call_output_agent_id")
        child_thread_match = (
            isinstance(jsonl_child_id, str)
            and isinstance(shadow_child_id, str)
            and jsonl_child_id == shadow_child_id
        )
        agent_to_receiver_match = (
            isinstance(jsonl_agent_id, str)
            and jsonl_agent_id in [item for item in app_receiver_ids if isinstance(item, str)]
        )
        app_raw_agent_match = (
            not isinstance(app_agent_id, str)
            or not isinstance(shadow_child_id, str)
            or app_agent_id == shadow_child_id
        )
        child_parent_thread_match = (
            isinstance(jsonl_child_parent_thread_id, str)
            and isinstance(app_child_parent_thread_id, str)
            and jsonl_child_parent_thread_id == app_child_parent_thread_id
        )
        link_equivalent = (
            parent_thread_match
            and child_thread_match
            and child_parent_thread_match
            and agent_to_receiver_match
            and app_raw_agent_match
        )
        if not link_equivalent:
            link_mismatches.append(call_id)

        jsonl_prompt_sha = (
            jsonl_spawn.get("prompt", {}).get("sha256")
            if isinstance(jsonl_spawn.get("prompt"), dict)
            else None
        )
        shadow_prompt_sha = (
            shadow_spawn.get("prompt", {}).get("sha256")
            if isinstance(shadow_spawn.get("prompt"), dict)
            else None
        )
        prompt_hash_match = (
            isinstance(jsonl_prompt_sha, str)
            and isinstance(shadow_prompt_sha, str)
            and jsonl_prompt_sha == shadow_prompt_sha
        )

        jsonl_result_sha = (
            jsonl_spawn.get("result", {}).get("sha256")
            if isinstance(jsonl_spawn.get("result"), dict)
            else None
        )
        shadow_result_sha = shadow_spawn.get("result_candidate_sha256")
        if isinstance(jsonl_result_sha, str) and isinstance(shadow_result_sha, str):
            result_hash_match = jsonl_result_sha == shadow_result_sha
            if not result_hash_match:
                result_mismatches.append(call_id)
        else:
            result_hash_match = None
            result_unproven.append(call_id)

        jsonl_role = jsonl_spawn.get("agent_type") or jsonl_spawn.get("agent_role")
        shadow_role = shadow_spawn.get("agent_type") or shadow_spawn.get("role")
        role_match = isinstance(jsonl_role, str) and isinstance(shadow_role, str) and jsonl_role == shadow_role

        comparisons.append(
            {
                "role": role,
                "call_id": call_id,
                "parent_thread_match": parent_thread_match,
                "jsonl_parent_thread_id": jsonl_parent_thread_id,
                "app_server_parent_thread_id": shadow_parent_thread_id,
                "child_thread_match": child_thread_match,
                "jsonl_child_thread_id": jsonl_child_id,
                "app_server_child_thread_id": shadow_child_id,
                "jsonl_child_parent_thread_id": jsonl_child_parent_thread_id,
                "app_server_child_parent_thread_id": app_child_parent_thread_id,
                "app_server_child_parent_thread_id_source": shadow_spawn.get("child_parent_thread_id_source"),
                "child_parent_thread_match": child_parent_thread_match,
                "jsonl_function_call_output_agent_id": jsonl_agent_id,
                "app_server_receiverThreadIds": app_receiver_ids,
                "app_server_function_call_output_agent_id": app_agent_id,
                "agent_id_in_receiverThreadIds": agent_to_receiver_match,
                "app_raw_agent_matches_child": app_raw_agent_match,
                "prompt_hash_match": prompt_hash_match,
                "jsonl_prompt_sha256": jsonl_prompt_sha,
                "app_server_prompt_sha256": shadow_prompt_sha,
                "result_hash_match": result_hash_match,
                "jsonl_result_sha256": jsonl_result_sha,
                "app_server_result_candidate_sha256": shadow_result_sha,
                "result_candidate_source": shadow_spawn.get("result_candidate_source"),
                "role_match": role_match,
                "jsonl_role": jsonl_role,
                "app_server_role": shadow_role,
                "link_equivalent": link_equivalent,
            }
        )

    jsonl_capture_status = jsonl_trace.get("capture_status") or jsonl_trace.get("summary", {}).get("capture_status")
    shadow_capture_status = shadow_trace.get("capture_status")
    if insufficient:
        classification = CLASS_INSUFFICIENT_EVENTS
    elif link_mismatches:
        classification = CLASS_LINK_MISMATCH
    elif result_mismatches:
        classification = CLASS_RESULT_MISMATCH
    elif result_unproven:
        classification = CLASS_LINK_EQUIVALENT_RESULT_UNPROVEN
    else:
        classification = CLASS_EQUIVALENT

    return {
        "schema": "codex-harness.app_server_shadow_equivalence.v1",
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "classification": classification,
        "replacement_claimed": False,
        "capture_backend": "app-server-shadow",
        "baseline_backend": "jsonl-native-subagent-adapter",
        "parent_thread_match": parent_thread_match,
        "jsonl_capture_status": jsonl_capture_status,
        "app_server_shadow_capture_status": shadow_capture_status,
        "jsonl_adapter_exit_code": jsonl_trace.get("adapter_exit_code"),
        "jsonl_allow_partial": jsonl_trace.get("allow_partial"),
        "jsonl_workflow_validation_scope": jsonl_trace.get("workflow_validation_scope"),
        "link_mismatches": link_mismatches,
        "result_mismatches": result_mismatches,
        "result_unproven": result_unproven,
        "insufficient_call_ids": insufficient,
        "comparisons": comparisons,
    }


def write_shadow_trace_markdown(path: Path, shadow_trace: dict[str, Any]) -> None:
    lines = [
        "# App Server Shadow Trace",
        "",
        f"- Schema: `{shadow_trace.get('schema')}`",
        f"- Capture backend: `{shadow_trace.get('capture_backend')}`",
        f"- Capture status: `{shadow_trace.get('capture_status')}`",
        f"- Parent thread: `{shadow_trace.get('parent_thread_id')}`",
        f"- Link mode: `{shadow_trace.get('link_mode')}`",
        f"- Unresolved link call ids: `{', '.join(shadow_trace.get('unresolved_link_call_ids') or []) or '(none)'}`",
        f"- Unresolved result roles: `{', '.join(shadow_trace.get('unresolved_result_roles') or []) or '(none)'}`",
        "",
        "## Role Table",
        "",
        "| role | call_id | child_thread_id | link_mode | prompt_sha256 | result_source | result_sha256 |",
        "|---|---|---|---|---|---|---|",
    ]
    for spawn in shadow_trace.get("spawns", []):
        lines.append(
            "| {role} | `{call_id}` | `{child}` | `{link}` | `{prompt}` | `{source}` | `{result}` |".format(
                role=spawn.get("role") or "",
                call_id=spawn.get("call_id") or "",
                child=spawn.get("child_thread_id") or "",
                link=spawn.get("link_mode") or "",
                prompt=(spawn.get("prompt") or {}).get("sha256") or "",
                source=spawn.get("result_candidate_source") or "",
                result=spawn.get("result_candidate_sha256") or "",
            )
        )
    lines.extend(
        [
            "",
            "## Guardrails",
            "",
            "- Shadow-only parser.",
            "- Does not call `spawn_agent` from Python.",
            "- Does not use `codex exec`.",
            "- Does not run strict aggregation.",
            "- Does not modify the accepted JSONL trace.",
            "- Does not claim App Server capture backend acceptance.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def write_equivalence_report(path: Path, equivalence: dict[str, Any]) -> None:
    lines = [
        "# App Server Shadow Equivalence Report",
        "",
        f"RESULT: {equivalence.get('classification')}",
        "",
        "## Summary",
        "",
        f"- Classification: `{equivalence.get('classification')}`",
        f"- Replacement claimed: `{equivalence.get('replacement_claimed')}`",
        f"- Parent thread match: `{equivalence.get('parent_thread_match')}`",
        f"- JSONL capture status: `{equivalence.get('jsonl_capture_status')}`",
        f"- App Server shadow capture status: `{equivalence.get('app_server_shadow_capture_status')}`",
        f"- Link mismatches: `{', '.join(equivalence.get('link_mismatches') or []) or '(none)'}`",
        f"- Result mismatches: `{', '.join(equivalence.get('result_mismatches') or []) or '(none)'}`",
        f"- Result unproven: `{', '.join(equivalence.get('result_unproven') or []) or '(none)'}`",
        "",
        "## Comparisons",
        "",
        "| role | call_id | link | child_parent | result | prompt_hash | role |",
        "|---|---|---|---|---|---|---|",
    ]
    for item in equivalence.get("comparisons", []):
        result_value = item.get("result_hash_match")
        result_text = "unproven" if result_value is None else str(result_value)
        lines.append(
            "| {role} | `{call_id}` | `{link}` | `{child_parent}` | `{result}` | `{prompt}` | `{role_match}` |".format(
                role=item.get("role") or "",
                call_id=item.get("call_id") or "",
                link=item.get("link_equivalent"),
                child_parent=item.get("child_parent_thread_match"),
                result=result_text,
                prompt=item.get("prompt_hash_match"),
                role_match=item.get("role_match"),
            )
        )
    lines.extend(
        [
            "",
            "## Boundary",
            "",
            "This report is shadow-only. JSONL remains the accepted capture baseline.",
            "No authoritative App Server aggregation is performed.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def write_acceptance_report(
    path: Path,
    equivalence: dict[str, Any],
    phase: str = "V8",
    live_summary: dict[str, Any] | None = None,
) -> None:
    classification = equivalence.get("classification")
    if phase == "V9":
        claim = "V9_LIVE_SHADOW_READY" if classification else "V9_LIVE_SHADOW_NOT_READY"
        title = "V9 App Server Live Shadow Acceptance"
    else:
        claim = "V8_SHADOW_ADAPTER_READY" if classification else "V8_SHADOW_ADAPTER_NOT_READY"
        title = "V8 App Server Shadow Adapter Acceptance"
    lines = [
        f"# {title}",
        "",
        f"RESULT: {claim}",
        "",
        f"- Equivalence classification: `{classification}`",
        "- App Server capture backend accepted: `false`",
        "- JSONL baseline remains accepted: `true`",
        "- Strict aggregation relaxed: `false`",
        "- Runner behavior modified: `false`",
        "- Python spawn_agent attempted: `false`",
        "- codex exec subagent spawning used: `false`",
    ]
    if live_summary:
        lines.extend(
            [
                f"- Isolated App Server launched: `{live_summary.get('client_feasible')}`",
                f"- Parent turn completed: `{live_summary.get('parent_turn_completed_seen')}`",
                f"- Child turn completed: `{live_summary.get('child_turn_completed_seen')}`",
                f"- Workspace: `{live_summary.get('scratch_workspace')}`",
            ]
        )
    required_outputs = [
        "- `app_server_shadow_trace.json`",
        "- `app_server_shadow_trace.md`",
        "- `shadow_equivalence_trace.json`",
        "- `shadow_equivalence_report.md`",
    ]
    if phase == "V9":
        required_outputs.insert(2, "- `jsonl_parallel_trace.json`")
        required_outputs.insert(0, "- `app_server_events.jsonl`")
    lines.extend(["", "## Required Outputs", "", *required_outputs, ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def write_blocked_v9_report(out_dir: Path, message: str) -> None:
    lines = [
        "# V9 App Server Live Shadow Acceptance",
        "",
        f"RESULT: {CLASS_V9_BLOCKED}",
        "",
        f"- Reason: {message}",
        "- App Server capture backend accepted: `false`",
        "- JSONL baseline remains accepted: `true`",
        "- Runner behavior modified: `false`",
        "- Python spawn_agent attempted: `false`",
        "- codex exec subagent spawning used: `false`",
        "",
    ]
    (out_dir / "v9_acceptance_report.md").write_text("\n".join(lines), encoding="utf-8")


def write_blocked_v10_report(out_dir: Path, message: str) -> None:
    lines = [
        "# V10 App Server Capture Backend Candidate Acceptance",
        "",
        "RESULT: V10_CANDIDATE_BACKEND_NOT_READY",
        "",
        f"- Reason: {message}",
        "- App Server backend accepted: `false`",
        "- JSONL replacement claimed: `false`",
        "- JSONL remains authoritative: `true`",
        "- Runner behavior modified: `false`",
        "- Python spawn_agent attempted: `false`",
        "- codex exec subagent spawning used: `false`",
        "- Live VS Code App Server attached: `false`",
        "",
    ]
    (out_dir / "v10_acceptance_report.md").write_text("\n".join(lines), encoding="utf-8")


def load_shadow_bundle(
    *,
    app_events_path: Path,
    jsonl_trace_path: Path,
    expected_workflow_path: Path | None,
    live_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    expected_workflow = read_json(expected_workflow_path, "expected-workflow") if expected_workflow_path else None
    jsonl_trace = read_json(jsonl_trace_path, "jsonl-trace")
    expected_specs = expected_role_specs(expected_workflow, jsonl_trace)
    if not expected_specs:
        raise ShadowError("No expected call ids found in expected workflow or JSONL trace.")

    records, malformed = iter_event_records(app_events_path)
    parent_thread_id_hint, parent_log_path = find_thread_started(records)
    evidence, diagnostics = collect_app_server_evidence(records, expected_specs)
    shadow_trace = summarize_app_evidence(
        evidence=evidence,
        expected_specs=expected_specs,
        parent_thread_id_hint=parent_thread_id_hint,
        parent_log_path=parent_log_path,
    )
    shadow_trace["inputs"] = {
        "app_server_events": str(app_events_path),
        "jsonl_trace": str(jsonl_trace_path),
        "expected_workflow": str(expected_workflow_path) if expected_workflow_path else None,
    }
    shadow_trace["event_parse"] = {
        "records_seen": len(records),
        "malformed_lines": malformed,
        "diagnostics": diagnostics,
    }
    shadow_trace["boundary"] = {
        "shadow_only": True,
        "jsonl_baseline_unchanged": True,
        "authoritative_aggregation_performed": False,
        "app_server_backend_accepted": False,
    }

    equivalence = compare_shadow_to_jsonl(shadow_trace, jsonl_trace, expected_specs)
    equivalence["inputs"] = shadow_trace["inputs"]
    if live_summary:
        shadow_trace["live_summary"] = live_summary
        equivalence["live_summary"] = live_summary

    return {
        "expected_workflow": expected_workflow,
        "jsonl_trace": jsonl_trace,
        "expected_specs": expected_specs,
        "records": records,
        "malformed": malformed,
        "evidence": evidence,
        "shadow_trace": shadow_trace,
        "equivalence": equivalence,
    }


def run_offline_inputs(
    *,
    app_events_path: Path,
    jsonl_trace_path: Path,
    expected_workflow_path: Path | None,
    out_dir: Path,
    phase: str,
    live_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    bundle = load_shadow_bundle(
        app_events_path=app_events_path,
        jsonl_trace_path=jsonl_trace_path,
        expected_workflow_path=expected_workflow_path,
        live_summary=live_summary,
    )
    shadow_trace = bundle["shadow_trace"]
    equivalence = bundle["equivalence"]

    shadow_json = out_dir / "app_server_shadow_trace.json"
    shadow_md = out_dir / "app_server_shadow_trace.md"
    equivalence_json = out_dir / "shadow_equivalence_trace.json"
    equivalence_md = out_dir / "shadow_equivalence_report.md"
    acceptance_md = out_dir / ("v9_acceptance_report.md" if phase == "V9" else "v8_acceptance_report.md")

    shadow_json.write_text(json.dumps(shadow_trace, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    write_shadow_trace_markdown(shadow_md, shadow_trace)
    equivalence_json.write_text(json.dumps(equivalence, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    write_equivalence_report(equivalence_md, equivalence)
    write_acceptance_report(acceptance_md, equivalence, phase=phase, live_summary=live_summary)

    print(f"wrote {shadow_json}")
    print(f"wrote {shadow_md}")
    print(f"wrote {equivalence_json}")
    print(f"wrote {equivalence_md}")
    print(f"wrote {acceptance_md}")
    print(f"classification: {equivalence['classification']}")
    return equivalence


def result_file_for_spec(spec: dict[str, Any]) -> str:
    role = str(spec.get("role") or spec.get("agent_type") or "")
    result_file = spec.get("result_file")
    if isinstance(result_file, str) and result_file:
        return result_file
    return DEFAULT_RESULT_FILES.get(role, f"{role or 'unknown'}_result.md")


def top_level_result_verdict(text: str | None) -> str | None:
    if not isinstance(text, str):
        return None
    in_front_matter = False
    lines = text.lstrip("\ufeff").splitlines()
    if lines and lines[0].strip() == "---":
        in_front_matter = True
    in_code_block = False
    for raw in lines[1:] if in_front_matter else lines:
        stripped = raw.strip()
        if in_front_matter:
            if stripped == "---":
                in_front_matter = False
            continue
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_code_block = not in_code_block
            continue
        if in_code_block or raw.startswith((" ", "\t", ">")):
            continue
        if not raw.startswith("RESULT:"):
            continue
        if raw == "RESULT: PASS":
            return "PASS"
        if raw == "RESULT: NEEDS_FIX":
            return "NEEDS_FIX"
        if raw == "RESULT: FAIL":
            return "FAIL"
        if raw == "RESULT: CAPTURE_FAILED":
            return "CAPTURE_FAILED"
        return "INVALID"
    return None


def candidate_result_for_entry(
    entry: dict[str, Any],
    child_thread_id: str | None,
) -> tuple[dict[str, Any] | None, list[str]]:
    candidates = entry.get("result_candidates") if isinstance(entry.get("result_candidates"), list) else []
    if child_thread_id:
        child_candidates = [
            item for item in candidates
            if isinstance(item, dict) and item.get("thread_id") == child_thread_id
        ]
        candidates = child_candidates or candidates
    text_candidates = [
        item for item in candidates
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    ]
    if not text_candidates:
        return None, ["missing_app_server_result_candidate"]
    shas = {item.get("sha256") for item in text_candidates if isinstance(item.get("sha256"), str)}
    if len(shas) > 1:
        return None, ["ambiguous_app_server_result_candidates"]
    return choose_result_candidate(text_candidates, child_thread_id), []


def normalized_candidate_expected_workflow(
    expected_workflow: dict[str, Any] | None,
    expected_specs: list[dict[str, Any]],
    parent_thread_id: str | None,
    run_id: str,
) -> dict[str, Any]:
    payload = dict(expected_workflow or {})
    payload.setdefault("schema_version", "codex-harness.expected_workflow.v1")
    payload["run_id"] = run_id
    payload["parent_thread_id"] = parent_thread_id
    payload["expected_roles"] = [
        {
            "order": spec.get("order") or index,
            "agent_type": spec.get("agent_type") or spec.get("role"),
            "logical_role": spec.get("logical_role") or spec.get("role") or spec.get("agent_type"),
            "call_id": spec.get("call_id"),
            "required": bool(spec.get("required", True)),
            "result_file": result_file_for_spec(spec),
        }
        for index, spec in enumerate(expected_specs, 1)
    ]
    payload.setdefault("privacy", {})
    if isinstance(payload["privacy"], dict):
        payload["privacy"].update(
            {
                "redact_prompts": True,
                "store_prompt_hash": True,
                "include_prompts": False,
                "strict_candidate_shadow_only": True,
            }
        )
    payload.setdefault("match", {})
    if isinstance(payload["match"], dict):
        payload["match"].update(
            {
                "require_same_parent_thread": True,
                "require_child_parent_thread_match": True,
            }
        )
    payload.setdefault("notes", {})
    if isinstance(payload["notes"], dict):
        payload["notes"].update(
            {
                "source": "V10 App Server candidate run manifest",
                "candidate_backend": "app-server-candidate",
                "authoritative_baseline": "JSONL",
                "replacement_accepted": False,
            }
        )
    return payload


def write_candidate_result_markdown(
    path: Path,
    *,
    role: str | None,
    parent_thread_id: str | None,
    child_thread_id: str | None,
    call_id: str,
    link_mode: str | None,
    app_server_link_mode: str | None,
    prompt_sha256: str | None,
    result_sha256: str | None,
    result_source: str | None,
    fallback_used: bool,
    status: str,
    result_text: str | None,
) -> None:
    lines = [
        "---",
        "capture_backend: app-server-candidate",
        "schema_version: codex-harness.app_server_candidate_capture.v1",
        f"role: {role}",
        f"parent_thread_id: {parent_thread_id}",
        f"child_thread_id: {child_thread_id}",
        f"call_id: {call_id}",
        f"link_mode: {link_mode}",
        f"app_server_link_mode: {app_server_link_mode}",
        f"prompt_sha256: {prompt_sha256}",
        f"result_sha256: {result_sha256}",
        f"result_source: {result_source}",
        f"fallback_used: {str(fallback_used).lower()}",
        f"status: {status}",
        "---",
        "",
    ]
    if result_text:
        lines.append(result_text.rstrip())
    else:
        lines.extend(
            [
                "RESULT: CAPTURE_FAILED",
                "",
                "App Server candidate result extraction was missing or ambiguous.",
            ]
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def build_candidate_run(
    bundle: dict[str, Any],
    candidate_run_dir: Path,
    run_id: str = "v10_app_server_candidate_probe",
) -> dict[str, Any]:
    candidate_run_dir.mkdir(parents=True, exist_ok=True)
    expected_specs = bundle["expected_specs"]
    expected_workflow = bundle["expected_workflow"]
    shadow_trace = bundle["shadow_trace"]
    evidence = bundle["evidence"]
    parent_thread_id = shadow_trace.get("parent_thread_id")
    manifest = normalized_candidate_expected_workflow(
        expected_workflow,
        expected_specs,
        parent_thread_id if isinstance(parent_thread_id, str) else None,
        run_id,
    )
    write_json(candidate_run_dir / "expected_workflow.json", manifest)

    shadow_by_call = {
        spawn.get("call_id"): spawn
        for spawn in shadow_trace.get("spawns", [])
        if isinstance(spawn, dict) and isinstance(spawn.get("call_id"), str)
    }
    spawns: list[dict[str, Any]] = []
    result_files: list[dict[str, Any]] = []
    unresolved_links: list[str] = []
    unresolved_results: list[str] = []
    link_modes: list[str] = []

    for index, spec in enumerate(expected_specs, 1):
        call_id = spec.get("call_id")
        if not isinstance(call_id, str):
            continue
        role = spec.get("role") or spec.get("agent_type")
        shadow_spawn = shadow_by_call.get(call_id, {})
        entry = evidence.get(call_id, {}) if isinstance(evidence, dict) else {}
        child_thread_id = shadow_spawn.get("child_thread_id")
        app_link_mode = shadow_spawn.get("link_mode")
        app_agent_id = shadow_spawn.get("function_call_output_agent_id")
        receiver_ids = (
            shadow_spawn.get("receiverThreadIds")
            if isinstance(shadow_spawn.get("receiverThreadIds"), list)
            else []
        )
        if isinstance(app_agent_id, str) and app_agent_id == child_thread_id:
            link_mode = "function_call_output_agent_id"
        elif app_link_mode == "app_server_collab_agent_tool_call" and child_thread_id:
            link_mode = "app_server_collab_agent_tool_call"
        else:
            link_mode = None
        if link_mode != "function_call_output_agent_id":
            unresolved_links.append(call_id)
        else:
            link_modes.append(link_mode)

        result_candidate, result_issues = candidate_result_for_entry(
            entry if isinstance(entry, dict) else {},
            child_thread_id if isinstance(child_thread_id, str) else None,
        )
        result_text = result_candidate.get("text") if isinstance(result_candidate, dict) else None
        result_sha = sha256_text(result_text) if isinstance(result_text, str) else None
        result_source = result_candidate.get("source") if isinstance(result_candidate, dict) else None
        status = "complete" if result_text and not result_issues and link_mode == "function_call_output_agent_id" else "capture_failed"
        if status != "complete":
            unresolved_results.append(str(role or call_id))

        prompt = shadow_spawn.get("prompt") if isinstance(shadow_spawn.get("prompt"), dict) else {}
        prompt_sha = prompt.get("sha256") if isinstance(prompt.get("sha256"), str) else None
        raw_spawn = entry.get("raw_spawn_function_call") if isinstance(entry.get("raw_spawn_function_call"), dict) else {}
        collab = entry.get("collab_completed") if isinstance(entry.get("collab_completed"), dict) else {}
        child_parent_thread_id = shadow_spawn.get("child_parent_thread_id")
        child_parent_match = (
            isinstance(parent_thread_id, str)
            and isinstance(child_parent_thread_id, str)
            and child_parent_thread_id == parent_thread_id
        )
        spawn = {
            "index": index,
            "order": spec.get("order") or index,
            "agent_role": role,
            "agent_type": spec.get("agent_type") or role,
            "logical_role": spec.get("logical_role") or role,
            "call_id": call_id,
            "tool_call_id": call_id,
            "capture_status": status,
            "parent_thread_id": parent_thread_id,
            "child_thread_id": child_thread_id,
            "link_mode": link_mode,
            "app_server_link_mode": app_link_mode,
            "senderThreadId": shadow_spawn.get("senderThreadId"),
            "receiverThreadIds": receiver_ids,
            "function_call_output_agent_id": app_agent_id,
            "function_call_output_present": bool(shadow_spawn.get("function_call_output_present")),
            "collab_agent_spawn_end_present": False,
            "collabAgentToolCall_present": bool(shadow_spawn.get("collabAgentToolCall_present")),
            "collabAgentToolCall_status": shadow_spawn.get("collabAgentToolCall_status"),
            "fallback_used": False,
            "parent_spawn_call": {
                "function_call_id": call_id,
                "line": raw_spawn.get("line") or collab.get("line"),
                "agent_type": spec.get("agent_type") or role,
                "has_message": prompt_sha is not None,
                "message": {
                    "present": prompt_sha is not None,
                    "sha256": prompt_sha,
                    "chars": prompt.get("chars", 0),
                    "excerpt": "[redacted]",
                },
                "arguments_keys": raw_spawn.get("arguments_keys", []),
            },
            "canonical_prompt_source": prompt.get("source"),
            "prompt": {
                "present": prompt_sha is not None,
                "sha256": prompt_sha,
                "chars": prompt.get("chars", 0),
                "excerpt": "[redacted]",
            },
            "child_link_proof": {
                "mode": "function_call_output_agent_id" if link_mode == "function_call_output_agent_id" else app_link_mode,
                "app_server_link_mode": app_link_mode,
                "call_id": call_id,
                "agent_id": app_agent_id,
                "child_thread_id": child_thread_id,
                "receiverThreadIds": receiver_ids,
                "exact_match": link_mode == "function_call_output_agent_id",
            },
            "child": {
                "session_meta": {
                    "thread_id": child_thread_id,
                    "parent_thread_id": child_parent_thread_id,
                    "parent_thread_id_matches": child_parent_match,
                    "agent_role": role,
                    "source": shadow_spawn.get("child_parent_thread_id_source"),
                },
                "task_complete": {
                    "source": "task_complete.last_agent_message",
                    "app_server_result_source": result_source,
                    "last_agent_message": {
                        "present": result_text is not None,
                        "sha256": result_sha,
                        "chars": len(result_text) if isinstance(result_text, str) else 0,
                        "excerpt": "[redacted]",
                    },
                },
            },
            "result": {
                "present": result_text is not None,
                "sha256": result_sha,
                "chars": len(result_text) if isinstance(result_text, str) else 0,
                "excerpt": "[redacted]",
            },
            "result_source": result_source,
            "result_candidate_issues": result_issues,
            "result_from_parent_wait_used": False,
        }
        spawns.append(spawn)

        result_file = result_file_for_spec(spec)
        result_path = candidate_run_dir / result_file
        write_candidate_result_markdown(
            result_path,
            role=str(role) if role else None,
            parent_thread_id=parent_thread_id if isinstance(parent_thread_id, str) else None,
            child_thread_id=child_thread_id if isinstance(child_thread_id, str) else None,
            call_id=call_id,
            link_mode=link_mode,
            app_server_link_mode=app_link_mode,
            prompt_sha256=prompt_sha,
            result_sha256=result_sha,
            result_source=result_source,
            fallback_used=False,
            status=status,
            result_text=result_text,
        )
        result_files.append(
            {
                "role": role,
                "logical_role": spec.get("logical_role") or role,
                "call_id": call_id,
                "path": str(result_path),
                "status": status,
                "result_source": result_source,
                "fallback_used": False,
            }
        )

    capture_status = "complete" if not unresolved_links and not unresolved_results else "partial"
    first_spawn = spawns[0] if len(spawns) == 1 else {}
    first_prompt = first_spawn.get("prompt") if isinstance(first_spawn.get("prompt"), dict) else {}
    first_result = first_spawn.get("result") if isinstance(first_spawn.get("result"), dict) else {}
    first_child = first_spawn.get("child") if isinstance(first_spawn.get("child"), dict) else {}
    first_meta = first_child.get("session_meta") if isinstance(first_child.get("session_meta"), dict) else {}
    candidate_trace = {
        "schema": "codex-harness.app_server_candidate_capture.v1",
        "capture_backend": "app-server-candidate",
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "adapter_exit_code": 0 if capture_status == "complete" else 1,
        "allow_partial": False,
        "capture_status": capture_status,
        "workflow_validation_scope": "expected_call_ids",
        "expected_call_ids_present": True,
        "accepted_link_modes": sorted(set(link_modes)),
        "unresolved_link_call_ids": unresolved_links,
        "missing_spawn_end_call_ids": [],
        "in_scope_partial_count": len(unresolved_links) + len(unresolved_results),
        "out_of_scope_partial_count": 0,
        "parent_thread_id": parent_thread_id,
        "child_thread_id": spawns[0].get("child_thread_id") if len(spawns) == 1 else None,
        "call_id": first_spawn.get("call_id"),
        "collabAgentToolCall_id": first_spawn.get("tool_call_id"),
        "senderThreadId": first_spawn.get("senderThreadId"),
        "receiverThreadIds": first_spawn.get("receiverThreadIds"),
        "function_call_output_agent_id": first_spawn.get("function_call_output_agent_id"),
        "child_parent_thread_id": first_meta.get("parent_thread_id"),
        "prompt_sha256": first_prompt.get("sha256"),
        "result_sha256": first_result.get("sha256"),
        "result_source": first_spawn.get("result_source"),
        "fallback_used": bool(first_spawn.get("fallback_used")),
        "link_mode": link_modes[0] if len(set(link_modes)) == 1 and link_modes else None,
        "spawns": spawns,
        "expected_workflow": {
            "run_id": run_id,
            "workflow_validation_scope": "expected_call_ids",
            "validation_scope": "expected_call_ids",
            "expected_call_ids": [
                spec.get("call_id")
                for spec in expected_specs
                if isinstance(spec.get("call_id"), str)
            ],
            "expected_role_specs": manifest["expected_roles"],
            "expected_roles": [
                spec.get("agent_type") or spec.get("role")
                for spec in expected_specs
            ],
            "missing_expected_roles": [],
        },
        "scope": {
            "workflow_validation_scope": "expected_call_ids",
            "expected_call_ids": [
                spec.get("call_id")
                for spec in expected_specs
                if isinstance(spec.get("call_id"), str)
            ],
        },
        "summary": {
            "adapter_exit_code": 0 if capture_status == "complete" else 1,
            "allow_partial": False,
            "capture_status": capture_status,
            "workflow_validation_scope": "expected_call_ids",
            "expected_call_ids_present": True,
            "missing_spawn_end_call_ids": [],
            "in_scope_partial_count": len(unresolved_links) + len(unresolved_results),
            "out_of_scope_partial_count": 0,
            "unresolved_link_call_ids": unresolved_links,
            "parent_thread_id": parent_thread_id,
            "child_thread_id": first_spawn.get("child_thread_id"),
            "call_id": first_spawn.get("call_id"),
            "senderThreadId": first_spawn.get("senderThreadId"),
            "receiverThreadIds": first_spawn.get("receiverThreadIds"),
            "function_call_output_agent_id": first_spawn.get("function_call_output_agent_id"),
            "child_parent_thread_id": first_meta.get("parent_thread_id"),
            "prompt_sha256": first_prompt.get("sha256"),
            "result_sha256": first_result.get("sha256"),
            "result_source": first_spawn.get("result_source"),
            "fallback_used": bool(first_spawn.get("fallback_used")),
            "spawn_count": len(spawns),
            "link_mode": link_modes[0] if len(set(link_modes)) == 1 and link_modes else None,
        },
        "result_capture": {
            "enabled": True,
            "files": result_files,
            "source_preference": [
                "item.completed.agentMessage.final_answer",
                "collabAgentToolCall.wait.agentsStates.message",
                "rawResponseItem.function_call_output.wait_agent.status.completed",
            ],
        },
        "source_paths": {
            "json_trace": str(candidate_run_dir / "spawn_capture_trace.json"),
            "markdown_trace": str(candidate_run_dir / "spawn_capture_trace.md"),
            "app_server_events": shadow_trace.get("inputs", {}).get("app_server_events"),
            "jsonl_authoritative_trace": shadow_trace.get("inputs", {}).get("jsonl_trace"),
        },
        "boundary": {
            "authority": "app-server-candidate",
            "authoritative_baseline": "JSONL",
            "replacement_accepted": False,
            "shadow_only": True,
            "python_spawn_agent_attempted": False,
            "codex_exec_subagent_spawning_used": False,
        },
    }
    write_json(candidate_run_dir / "spawn_capture_trace.json", candidate_trace)
    write_json(candidate_run_dir / "app_server_candidate_trace.json", candidate_trace)
    write_candidate_trace_markdown(candidate_run_dir / "spawn_capture_trace.md", candidate_trace)
    write_candidate_report(candidate_run_dir / "app_server_candidate_report.md", candidate_trace)
    return candidate_trace


def write_candidate_trace_markdown(path: Path, trace: dict[str, Any]) -> None:
    lines = [
        "# App Server Candidate Capture Trace",
        "",
        f"- Capture backend: `{trace.get('capture_backend')}`",
        f"- Capture status: `{trace.get('capture_status')}`",
        f"- Adapter exit code: `{trace.get('adapter_exit_code')}`",
        f"- Workflow validation scope: `{trace.get('workflow_validation_scope')}`",
        f"- Parent thread: `{trace.get('parent_thread_id')}`",
        f"- Replacement accepted: `{trace.get('boundary', {}).get('replacement_accepted')}`",
        "",
        "| role | call_id | child_thread_id | link_mode | app_server_link_mode | result_sha256 | status |",
        "|---|---|---|---|---|---|---|",
    ]
    for spawn in trace.get("spawns", []):
        result = spawn.get("result") if isinstance(spawn.get("result"), dict) else {}
        lines.append(
            "| {role} | `{call}` | `{child}` | `{link}` | `{app_link}` | `{result}` | `{status}` |".format(
                role=spawn.get("agent_role") or "",
                call=spawn.get("call_id") or "",
                child=spawn.get("child_thread_id") or "",
                link=spawn.get("link_mode") or "",
                app_link=spawn.get("app_server_link_mode") or "",
                result=result.get("sha256") or "",
                status=spawn.get("capture_status") or "",
            )
        )
    lines.extend(
        [
            "",
            "## Boundary",
            "",
            "- App Server candidate artifacts are shadow/candidate only.",
            "- JSONL remains the authoritative baseline.",
            "- This tool did not call `spawn_agent` from Python.",
            "- This tool did not use `codex exec` for subagent spawning.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def write_candidate_report(path: Path, trace: dict[str, Any]) -> None:
    ready = trace.get("capture_status") == "complete" and trace.get("adapter_exit_code") == 0
    lines = [
        "# App Server Candidate Run Report",
        "",
        f"RESULT: {'APP_SERVER_CANDIDATE_RUN_COMPLETE' if ready else 'APP_SERVER_CANDIDATE_RUN_INCOMPLETE'}",
        "",
        f"- Capture backend: `{trace.get('capture_backend')}`",
        f"- Capture status: `{trace.get('capture_status')}`",
        f"- Accepted link modes: `{', '.join(trace.get('accepted_link_modes') or []) or '(none)'}`",
        f"- Unresolved links: `{', '.join(trace.get('unresolved_link_call_ids') or []) or '(none)'}`",
        "- Replacement accepted: `false`",
        "- Authoritative baseline: `JSONL`",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def run_strict_aggregation(run_dir: Path, out_dir: Path) -> dict[str, Any]:
    command = [
        sys.executable,
        str(AGGREGATOR_SCRIPT),
        "--run-dir",
        repo_rel(run_dir),
        "--strict",
        "--out-dir",
        repo_rel(out_dir),
    ]
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    run_record = {
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "authority": "app-server-candidate" if "app_server_candidate" in out_dir.name else "JSONL",
        "authoritative_baseline": "JSONL",
        "replacement_accepted": False,
    }
    write_json(out_dir / "aggregation_invocation.json", run_record)
    trace_path = out_dir / "aggregation_trace.json"
    if trace_path.is_file():
        trace = read_json(trace_path, "aggregation-trace")
    else:
        trace = {}
    write_json(
        out_dir / "candidate_aggregation_context.json",
        {
            "authority": run_record["authority"],
            "authoritative_baseline": "JSONL",
            "replacement_accepted": False,
            "aggregator_returncode": completed.returncode,
            "aggregation_overall_status": trace.get("overall_status"),
        },
    )
    (out_dir / "app_server_candidate_aggregation_report.md").write_text(
        "\n".join(
            [
                "# Candidate Aggregation Context",
                "",
                f"- Authority: `{run_record['authority']}`",
                "- Authoritative baseline: `JSONL`",
                "- Replacement accepted: `false`",
                f"- Aggregator return code: `{completed.returncode}`",
                f"- Aggregation overall status: `{trace.get('overall_status')}`",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return {
        "returncode": completed.returncode,
        "trace": trace,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def compare_candidate_to_jsonl(
    candidate_trace: dict[str, Any],
    jsonl_trace: dict[str, Any],
    candidate_aggregation: dict[str, Any] | None,
    jsonl_aggregation: dict[str, Any] | None,
) -> dict[str, Any]:
    jsonl_by_call = jsonl_spawn_by_call_id(jsonl_trace)
    result_files_by_call = {
        spec.get("call_id"): spec.get("result_file")
        for spec in candidate_trace.get("expected_workflow", {}).get("expected_role_specs", [])
        if isinstance(spec, dict) and isinstance(spec.get("call_id"), str)
    }
    specs_by_call = {
        spec.get("call_id"): spec
        for spec in candidate_trace.get("expected_workflow", {}).get("expected_role_specs", [])
        if isinstance(spec, dict) and isinstance(spec.get("call_id"), str)
    }
    comparisons: list[dict[str, Any]] = []
    mismatches: list[str] = []
    link_mismatches: list[str] = []
    result_mismatches: list[str] = []
    result_unproven: list[str] = []
    for candidate_spawn in candidate_trace.get("spawns", []):
        if not isinstance(candidate_spawn, dict):
            continue
        call_id = candidate_spawn.get("call_id")
        jsonl_spawn = jsonl_by_call.get(call_id) if isinstance(call_id, str) else None
        if not isinstance(jsonl_spawn, dict):
            mismatches.append(str(call_id))
            continue
        candidate_result = candidate_spawn.get("result") if isinstance(candidate_spawn.get("result"), dict) else {}
        jsonl_result = jsonl_spawn.get("result") if isinstance(jsonl_spawn.get("result"), dict) else {}
        candidate_prompt = candidate_spawn.get("prompt") if isinstance(candidate_spawn.get("prompt"), dict) else {}
        jsonl_prompt = jsonl_spawn.get("prompt") if isinstance(jsonl_spawn.get("prompt"), dict) else {}
        child = candidate_spawn.get("child") if isinstance(candidate_spawn.get("child"), dict) else {}
        meta = child.get("session_meta") if isinstance(child.get("session_meta"), dict) else {}
        jsonl_child = jsonl_spawn.get("child") if isinstance(jsonl_spawn.get("child"), dict) else {}
        jsonl_meta = jsonl_child.get("session_meta") if isinstance(jsonl_child.get("session_meta"), dict) else {}
        result_file = result_files_by_call.get(call_id) or result_file_for_spec(
            {"role": candidate_spawn.get("agent_role")}
        )
        spec = specs_by_call.get(call_id) if isinstance(call_id, str) else {}
        candidate_result_path = Path(candidate_trace["source_paths"]["json_trace"]).parent / result_file
        jsonl_result_path = Path(jsonl_trace["source_paths"]["json_trace"]).parent / result_file
        result_text = candidate_result_path.read_text(encoding="utf-8", errors="replace") if candidate_result_path.exists() else ""
        jsonl_result_text = jsonl_result_path.read_text(encoding="utf-8", errors="replace") if jsonl_result_path.exists() else ""
        candidate_fallback = bool(candidate_spawn.get("fallback_used"))
        jsonl_fallback = bool(jsonl_spawn.get("fallback_used"))
        comparison = {
            "role": candidate_spawn.get("agent_role"),
            "logical_role": spec.get("logical_role") if isinstance(spec, dict) else None,
            "call_id": call_id,
            "call_id_match": call_id == jsonl_spawn.get("call_id"),
            "parent_thread_match": candidate_spawn.get("parent_thread_id") == jsonl_spawn.get("parent_thread_id"),
            "child_thread_match": candidate_spawn.get("child_thread_id") == jsonl_spawn.get("child_thread_id"),
            "child_parent_thread_match": meta.get("parent_thread_id") == jsonl_meta.get("parent_thread_id"),
            "role_match": candidate_spawn.get("agent_type") == jsonl_spawn.get("agent_type"),
            "prompt_hash_match": candidate_prompt.get("sha256") == jsonl_prompt.get("sha256"),
            "result_hash_match": candidate_result.get("sha256") == jsonl_result.get("sha256"),
            "candidate_result_verdict": top_level_result_verdict(result_text),
            "jsonl_result_verdict": top_level_result_verdict(jsonl_result_text),
            "result_verdict_match": top_level_result_verdict(result_text) == top_level_result_verdict(jsonl_result_text),
            "jsonl_result_sha256": jsonl_result.get("sha256"),
            "candidate_result_sha256": candidate_result.get("sha256"),
            "fallback_used_match": candidate_fallback == jsonl_fallback,
            "candidate_fallback_used": candidate_fallback,
            "jsonl_fallback_used": jsonl_fallback,
            "candidate_link_mode": candidate_spawn.get("link_mode"),
            "app_server_link_mode": candidate_spawn.get("app_server_link_mode"),
            "jsonl_link_mode": jsonl_spawn.get("link_mode"),
        }
        link_ok = all(
            comparison[key] is True
            for key in (
                "call_id_match",
                "parent_thread_match",
                "child_thread_match",
                "child_parent_thread_match",
                "role_match",
                "prompt_hash_match",
            )
        )
        result_hash = comparison["result_hash_match"]
        if not link_ok:
            link_mismatches.append(str(call_id))
        if result_hash is None:
            result_unproven.append(str(call_id))
        elif result_hash is not True:
            result_mismatches.append(str(call_id))
        comparison["equivalent"] = all(
            comparison[key] is True
            for key in (
                "call_id_match",
                "parent_thread_match",
                "child_thread_match",
                "child_parent_thread_match",
                "role_match",
                "prompt_hash_match",
                "result_hash_match",
                "result_verdict_match",
                "fallback_used_match",
            )
        )
        if not comparison["equivalent"]:
            mismatches.append(str(call_id))
        comparisons.append(comparison)

    candidate_status = (candidate_aggregation or {}).get("trace", {}).get("overall_status")
    jsonl_status = (jsonl_aggregation or {}).get("trace", {}).get("overall_status")
    candidate_returncode = (candidate_aggregation or {}).get("returncode")
    jsonl_returncode = (jsonl_aggregation or {}).get("returncode")
    candidate_out_scope = candidate_trace.get("out_of_scope_partial_count") or candidate_trace.get("summary", {}).get("out_of_scope_partial_count")
    jsonl_out_scope = jsonl_trace.get("out_of_scope_partial_count") or jsonl_trace.get("summary", {}).get("out_of_scope_partial_count")
    aggregation_match = (
        candidate_status == jsonl_status
        and candidate_returncode == 0
        and jsonl_returncode == 0
    )
    classification = (
        CLASS_V10_EQUIVALENT
        if not mismatches
        and comparisons
        and aggregation_match
        and candidate_trace.get("capture_status") == "complete"
        else CLASS_V10_NOT_EQUIVALENT
    )
    return {
        "schema": "codex-harness.app_server_candidate_equivalence.v1",
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "classification": classification,
        "replacement_claimed": False,
        "app_server_backend_accepted": False,
        "authoritative_baseline": "JSONL",
        "candidate_capture_status": candidate_trace.get("capture_status"),
        "jsonl_capture_status": jsonl_trace.get("capture_status") or jsonl_trace.get("summary", {}).get("capture_status"),
        "candidate_adapter_exit_code": candidate_trace.get("adapter_exit_code"),
        "jsonl_adapter_exit_code": jsonl_trace.get("adapter_exit_code"),
        "candidate_aggregation_status": candidate_status,
        "jsonl_aggregation_status": jsonl_status,
        "candidate_aggregation_returncode": candidate_returncode,
        "jsonl_aggregation_returncode": jsonl_returncode,
        "aggregation_status_match": aggregation_match,
        "candidate_unresolved_link_call_ids": candidate_trace.get("unresolved_link_call_ids") or [],
        "jsonl_unresolved_link_call_ids": jsonl_trace.get("unresolved_link_call_ids") or jsonl_trace.get("summary", {}).get("unresolved_link_call_ids") or [],
        "candidate_out_of_scope_partial_count": candidate_out_scope,
        "jsonl_out_of_scope_partial_count": jsonl_out_scope,
        "out_of_scope_partial_count_match": candidate_out_scope == jsonl_out_scope,
        "mismatched_call_ids": mismatches,
        "link_mismatches": link_mismatches,
        "result_mismatches": result_mismatches,
        "result_unproven": result_unproven,
        "comparisons": comparisons,
    }


def write_candidate_equivalence_report(path: Path, trace: dict[str, Any]) -> None:
    lines = [
        "# App Server Candidate vs JSONL Equivalence Report",
        "",
        f"RESULT: {trace.get('classification')}",
        "",
        f"- Candidate capture status: `{trace.get('candidate_capture_status')}`",
        f"- JSONL capture status: `{trace.get('jsonl_capture_status')}`",
        f"- Candidate aggregation status: `{trace.get('candidate_aggregation_status')}`",
        f"- JSONL aggregation status: `{trace.get('jsonl_aggregation_status')}`",
        f"- Aggregation status match: `{trace.get('aggregation_status_match')}`",
        f"- Out-of-scope partial count match: `{trace.get('out_of_scope_partial_count_match')}`",
        f"- Mismatched call ids: `{', '.join(trace.get('mismatched_call_ids') or []) or '(none)'}`",
        "- App Server backend accepted: `false`",
        "- JSONL replacement claimed: `false`",
        "",
        "| role | call_id | link_mode | app_link | child | prompt | result | verdict | fallback |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for item in trace.get("comparisons", []):
        lines.append(
            "| {role} | `{call}` | `{link}` | `{app_link}` | `{child}` | `{prompt}` | `{result}` | `{verdict}` | `{fallback}` |".format(
                role=item.get("role") or "",
                call=item.get("call_id") or "",
                link=item.get("candidate_link_mode") or "",
                app_link=item.get("app_server_link_mode") or "",
                child=item.get("child_thread_match"),
                prompt=item.get("prompt_hash_match"),
                result=item.get("result_hash_match"),
                verdict=item.get("result_verdict_match"),
                fallback=item.get("fallback_used_match"),
            )
        )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_v10_acceptance_report(path: Path, equivalence: dict[str, Any]) -> None:
    ready = equivalence.get("classification") == CLASS_V10_EQUIVALENT
    lines = [
        "# V10 App Server Capture Backend Candidate Acceptance",
        "",
        f"RESULT: {'V10_CANDIDATE_BACKEND_READY' if ready else 'V10_CANDIDATE_BACKEND_NOT_READY'}",
        "",
        f"- Candidate equivalence: `{equivalence.get('classification')}`",
        "- App Server backend accepted: `false`",
        "- JSONL replacement claimed: `false`",
        "- JSONL remains authoritative: `true`",
        "- Strict aggregation relaxed: `false`",
        "- Runner behavior modified: `false`",
        "- Python spawn_agent attempted: `false`",
        "- codex exec subagent spawning used: `false`",
        "- Live VS Code App Server attached: `false`",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def run_v10_candidate_outputs(
    *,
    out_dir: Path,
    app_events_path: Path,
    jsonl_trace_path: Path,
    expected_workflow_path: Path,
    live_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    bundle = load_shadow_bundle(
        app_events_path=app_events_path,
        jsonl_trace_path=jsonl_trace_path,
        expected_workflow_path=expected_workflow_path,
        live_summary=live_summary,
    )
    candidate_run_dir = out_dir / "app_server_candidate_run"
    candidate_aggregation_dir = out_dir / "app_server_candidate_aggregation"
    jsonl_aggregation_dir = out_dir / "jsonl_authoritative_aggregation"
    candidate_trace = build_candidate_run(bundle, candidate_run_dir)
    candidate_aggregation = run_strict_aggregation(candidate_run_dir, candidate_aggregation_dir)
    jsonl_aggregation = run_strict_aggregation(jsonl_trace_path.parent, jsonl_aggregation_dir)
    equivalence = compare_candidate_to_jsonl(
        candidate_trace,
        bundle["jsonl_trace"],
        candidate_aggregation,
        jsonl_aggregation,
    )
    write_json(out_dir / "candidate_vs_jsonl_equivalence_trace.json", equivalence)
    write_candidate_equivalence_report(out_dir / "candidate_vs_jsonl_equivalence_report.md", equivalence)
    write_v10_acceptance_report(out_dir / "v10_acceptance_report.md", equivalence)
    return equivalence


def classify_v11_pattern(
    equivalence: dict[str, Any] | None,
    runtime_issue: str | None = None,
) -> str:
    if runtime_issue:
        return V11_BLOCKED_BY_RUNTIME_LIMIT
    if not equivalence:
        return V11_CANDIDATE_CAPTURE_FAILED
    if (
        equivalence.get("candidate_capture_status") != "complete"
        or equivalence.get("candidate_adapter_exit_code") != 0
        or equivalence.get("candidate_unresolved_link_call_ids")
    ):
        return V11_CANDIDATE_CAPTURE_FAILED
    if (
        equivalence.get("jsonl_capture_status") != "complete"
        or equivalence.get("jsonl_adapter_exit_code") != 0
        or equivalence.get("jsonl_unresolved_link_call_ids")
    ):
        return V11_JSONL_CAPTURE_FAILED
    if equivalence.get("link_mismatches"):
        return V11_LINK_MISMATCH
    if equivalence.get("result_unproven"):
        return V11_LINK_EQUIVALENT_RESULT_UNPROVEN
    if equivalence.get("result_mismatches"):
        return V11_LINK_EQUIVALENT_RESULT_MISMATCH
    verdict_mismatch = any(
        item.get("result_verdict_match") is not True
        for item in equivalence.get("comparisons", [])
        if isinstance(item, dict)
    )
    hash_mismatch = any(
        item.get("result_hash_match") is not True
        for item in equivalence.get("comparisons", [])
        if isinstance(item, dict)
    )
    if verdict_mismatch or hash_mismatch:
        return V11_LINK_EQUIVALENT_RESULT_MISMATCH
    if (
        equivalence.get("candidate_aggregation_returncode") != 0
        or equivalence.get("jsonl_aggregation_returncode") != 0
        or equivalence.get("aggregation_status_match") is not True
    ):
        return V11_AGGREGATION_MISMATCH
    if equivalence.get("mismatched_call_ids"):
        return V11_LINK_MISMATCH
    return V11_EQUIVALENT


def write_v11_pattern_equivalence_report(path: Path, trace: dict[str, Any]) -> None:
    lines = [
        "# V11 Pattern Equivalence Report",
        "",
        f"RESULT: {trace.get('classification')}",
        "",
        f"- Pattern: `{trace.get('pattern_name')}`",
        f"- Candidate capture status: `{trace.get('candidate_capture_status')}`",
        f"- JSONL capture status: `{trace.get('jsonl_capture_status')}`",
        f"- Candidate aggregation status: `{trace.get('candidate_aggregation_status')}`",
        f"- JSONL aggregation status: `{trace.get('jsonl_aggregation_status')}`",
        f"- Candidate unresolved links: `{', '.join(trace.get('candidate_unresolved_link_call_ids') or []) or '(none)'}`",
        f"- JSONL unresolved links: `{', '.join(trace.get('jsonl_unresolved_link_call_ids') or []) or '(none)'}`",
        f"- Mismatched call IDs: `{', '.join(trace.get('mismatched_call_ids') or []) or '(none)'}`",
        "- App Server backend accepted: `false`",
        "- JSONL replacement claimed: `false`",
        "",
        "| role | call_id | child | child_parent | role | prompt | result | verdict | fallback |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for item in trace.get("comparisons", []):
        lines.append(
            "| {role} | `{call}` | `{child}` | `{parent}` | `{role_match}` | `{prompt}` | `{result}` | `{verdict}` | `{fallback}` |".format(
                role=item.get("role") or "",
                call=item.get("call_id") or "",
                child=item.get("child_thread_match"),
                parent=item.get("child_parent_thread_match"),
                role_match=item.get("role_match"),
                prompt=item.get("prompt_hash_match"),
                result=item.get("result_hash_match"),
                verdict=item.get("result_verdict_match"),
                fallback=item.get("fallback_used_match"),
            )
        )
    if trace.get("runtime_issue"):
        lines.extend(["", f"Runtime issue: {trace['runtime_issue']}"])
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def blocked_v11_pattern_trace(pattern: dict[str, Any], message: str) -> dict[str, Any]:
    return {
        "schema": "codex-harness.app_server_candidate_parity.v11",
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "pattern_name": pattern["label"],
        "classification": V11_BLOCKED_BY_RUNTIME_LIMIT,
        "runtime_issue": message,
        "replacement_claimed": False,
        "app_server_backend_accepted": False,
        "authoritative_baseline": "JSONL",
        "comparisons": [],
    }


def app_server_spawn_message_for_call(records: list[dict[str, Any]], call_id: str) -> str | None:
    for record in records:
        payload = event_payload(record)
        if not payload:
            continue
        item = event_item(payload)
        if not item:
            continue
        if item.get("type") != "function_call" or item.get("name") != "spawn_agent":
            continue
        if item.get("call_id") != call_id:
            continue
        args = parse_json_maybe(item.get("arguments"))
        message = args.get("message") if isinstance(args, dict) else None
        return message if isinstance(message, str) else None
    return None


def content_lines_after_front_matter(text: str) -> list[str]:
    lines = text.lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        return lines
    for index, raw in enumerate(lines[1:], 1):
        if raw.strip() == "---":
            return lines[index + 1 :]
    return lines


def format_list(items: list[Any] | tuple[Any, ...] | None) -> str:
    if not items:
        return "- (none)"
    return "\n".join(f"- {item}" for item in items)


def result_text_without_front_matter(path: Path) -> str:
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return "\n".join(content_lines_after_front_matter(text))


def scan_runtime_limit_warnings(records: list[dict[str, Any]]) -> list[str]:
    warnings: list[str] = []
    for record in records:
        text = json.dumps(record, ensure_ascii=False).lower()
        if "thread limit" in text or "thread-limit" in text:
            warnings.append(f"possible thread-limit event at line {record.get('_line')}")
    return warnings


def validate_v13_concurrency(
    *,
    pattern_dir: Path,
    pattern: dict[str, Any],
    equivalence: dict[str, Any],
    candidate_trace: dict[str, Any],
    jsonl_trace: dict[str, Any],
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    manifest_roles = candidate_trace.get("expected_workflow", {}).get("expected_role_specs", [])
    roles_by_logical = {
        item.get("logical_role"): item
        for item in manifest_roles
        if isinstance(item, dict) and isinstance(item.get("logical_role"), str)
    }
    producer_roles = [
        roles_by_logical.get("producer[0]"),
        roles_by_logical.get("producer[1]"),
    ]
    reviewer_role = roles_by_logical.get("reviewer")
    producer_result_files = [
        str(item.get("result_file") or "")
        for item in producer_roles
        if isinstance(item, dict)
    ]
    producer_call_ids = [
        str(item.get("call_id") or "")
        for item in producer_roles
        if isinstance(item, dict)
    ]
    result_file_collision = len(producer_result_files) == 2 and len(set(producer_result_files)) == 2
    call_id_collision = len(producer_call_ids) == 2 and len(set(producer_call_ids)) == 2

    jsonl_by_call = jsonl_spawn_by_call_id(jsonl_trace)
    candidate_by_call = jsonl_spawn_by_call_id(candidate_trace)
    producer_child_ids = [
        jsonl_by_call.get(call_id, {}).get("child_thread_id")
        for call_id in producer_call_ids
    ]
    producer_child_ids = [item for item in producer_child_ids if isinstance(item, str)]
    child_thread_distinct = len(producer_child_ids) == 2 and len(set(producer_child_ids)) == 2

    required_result_files = [
        str(item.get("result_file") or "")
        for item in manifest_roles
        if isinstance(item, dict)
    ]
    jsonl_result_files_exist = all(
        (pattern_dir / "jsonl_authoritative_run" / name).is_file()
        for name in required_result_files
        if name
    )
    candidate_result_files_exist = all(
        (pattern_dir / "app_server_candidate_run" / name).is_file()
        for name in required_result_files
        if name
    )

    comparisons_by_logical = {
        item.get("logical_role"): item
        for item in equivalence.get("comparisons", [])
        if isinstance(item, dict)
    }
    producer_hashes_match = all(
        comparisons_by_logical.get(role, {}).get("result_hash_match") is True
        for role in ("producer[0]", "producer[1]")
    )
    reviewer_hash_matches = comparisons_by_logical.get("reviewer", {}).get("result_hash_match") is True

    reviewer_call_id = str(reviewer_role.get("call_id") or "") if isinstance(reviewer_role, dict) else ""
    reviewer_prompt = app_server_spawn_message_for_call(records, reviewer_call_id) if reviewer_call_id else None
    required_tokens = ("producer[0]", "producer[1]", "alpha", "beta")
    reviewer_prompt_refs = bool(reviewer_prompt) and all(token in reviewer_prompt for token in required_tokens)

    reviewer_result_file = str(reviewer_role.get("result_file") or "") if isinstance(reviewer_role, dict) else ""
    candidate_reviewer_text = result_text_without_front_matter(
        pattern_dir / "app_server_candidate_run" / reviewer_result_file
    )
    jsonl_reviewer_text = result_text_without_front_matter(
        pattern_dir / "jsonl_authoritative_run" / reviewer_result_file
    )
    reviewer_result_refs = all(token in candidate_reviewer_text for token in required_tokens) and all(
        token in jsonl_reviewer_text for token in required_tokens
    )
    reviewer_verdict_pass = (
        comparisons_by_logical.get("reviewer", {}).get("candidate_result_verdict") == "PASS"
        and comparisons_by_logical.get("reviewer", {}).get("jsonl_result_verdict") == "PASS"
    )

    jsonl_capture_complete = equivalence.get("jsonl_capture_status") == "complete"
    candidate_capture_complete = equivalence.get("candidate_capture_status") == "complete"
    workflow_scope_ok = (
        candidate_trace.get("workflow_validation_scope") == "expected_call_ids"
        and (
            jsonl_trace.get("workflow_validation_scope")
            or jsonl_trace.get("summary", {}).get("workflow_validation_scope")
        )
        == "expected_call_ids"
    )
    allow_partial_false = (
        candidate_trace.get("allow_partial") is False
        and (
            jsonl_trace.get("allow_partial")
            if "allow_partial" in jsonl_trace
            else jsonl_trace.get("summary", {}).get("allow_partial")
        )
        is False
    )
    unresolved_links_empty = not equivalence.get("candidate_unresolved_link_call_ids") and not equivalence.get(
        "jsonl_unresolved_link_call_ids"
    )
    jsonl_aggregation_pass = (
        equivalence.get("jsonl_aggregation_status") == "PASS"
        and equivalence.get("jsonl_aggregation_returncode") == 0
    )
    candidate_aggregation_pass = (
        equivalence.get("candidate_aggregation_status") == "PASS"
        and equivalence.get("candidate_aggregation_returncode") == 0
    )
    out_of_scope_recorded = (
        equivalence.get("candidate_out_of_scope_partial_count") is not None
        and equivalence.get("jsonl_out_of_scope_partial_count") is not None
    )
    candidate_child_ids = [
        candidate_by_call.get(call_id, {}).get("child_thread_id")
        for call_id in producer_call_ids
    ]
    candidate_child_ids = [item for item in candidate_child_ids if isinstance(item, str)]
    candidate_child_distinct = len(candidate_child_ids) == 2 and len(set(candidate_child_ids)) == 2

    checks = {
        "jsonl_capture_status_complete": jsonl_capture_complete,
        "app_server_candidate_capture_status_complete": candidate_capture_complete,
        "workflow_validation_scope_expected_call_ids": workflow_scope_ok,
        "allow_partial_false": allow_partial_false,
        "unresolved_link_call_ids_empty": unresolved_links_empty,
        "result_files_exist_for_all_roles": jsonl_result_files_exist and candidate_result_files_exist,
        "producer_result_file_no_collision": result_file_collision,
        "producer_call_id_no_collision": call_id_collision,
        "producer_child_thread_id_distinct": child_thread_distinct and candidate_child_distinct,
        "producer_result_hashes_match": producer_hashes_match,
        "reviewer_result_hash_matches": reviewer_hash_matches,
        "reviewer_prompt_references_both_producers": reviewer_prompt_refs,
        "reviewer_result_references_both_producers": reviewer_result_refs,
        "reviewer_top_level_verdict_pass": reviewer_verdict_pass,
        "jsonl_aggregation_pass": jsonl_aggregation_pass,
        "candidate_aggregation_pass": candidate_aggregation_pass,
        "dual_equivalence_classification_candidate_equivalent": equivalence.get("classification") == V11_EQUIVALENT,
        "out_of_scope_partial_count_recorded": out_of_scope_recorded,
    }
    failures = [name for name, ok in checks.items() if ok is not True]
    runtime_limit_warnings = scan_runtime_limit_warnings(records)
    return {
        "pattern_name": pattern["label"],
        "checks": checks,
        "failed_checks": failures,
        "producer_call_ids": producer_call_ids,
        "producer_child_thread_ids": producer_child_ids,
        "producer_result_files": producer_result_files,
        "reviewer_call_id": reviewer_call_id,
        "reviewer_prompt_reference_tokens_present": reviewer_prompt_refs,
        "reviewer_result_reference_tokens_present": reviewer_result_refs,
        "runtime_thread_limit_warnings": runtime_limit_warnings,
        "runtime_thread_limit_failures_out_of_scope_only": True,
    }


def classify_v13(equivalence: dict[str, Any], concurrency: dict[str, Any]) -> str:
    base = classify_v11_pattern(equivalence)
    if base != V11_EQUIVALENT:
        return base
    failures = set(concurrency.get("failed_checks") or [])
    if not failures:
        return V11_EQUIVALENT
    if {
        "jsonl_aggregation_pass",
        "candidate_aggregation_pass",
    } & failures:
        return V11_AGGREGATION_MISMATCH
    if {
        "producer_result_hashes_match",
        "reviewer_result_hash_matches",
        "reviewer_top_level_verdict_pass",
        "reviewer_result_references_both_producers",
    } & failures:
        return V11_LINK_EQUIVALENT_RESULT_MISMATCH
    if {
        "jsonl_capture_status_complete",
        "app_server_candidate_capture_status_complete",
        "result_files_exist_for_all_roles",
    } & failures:
        return V11_CANDIDATE_CAPTURE_FAILED
    return V11_LINK_MISMATCH


def write_v13_equivalence_report(path: Path, trace: dict[str, Any]) -> None:
    checks = trace.get("v13_concurrency_checks", {}).get("checks", {})
    lines = [
        "# V13 Fan-Out/Fan-In Dual Backend Equivalence",
        "",
        f"RESULT: {trace.get('classification')}",
        "",
        f"- Candidate capture status: `{trace.get('candidate_capture_status')}`",
        f"- JSONL capture status: `{trace.get('jsonl_capture_status')}`",
        f"- Candidate aggregation status: `{trace.get('candidate_aggregation_status')}`",
        f"- JSONL aggregation status: `{trace.get('jsonl_aggregation_status')}`",
        f"- Candidate unresolved links: `{', '.join(trace.get('candidate_unresolved_link_call_ids') or []) or '(none)'}`",
        f"- JSONL unresolved links: `{', '.join(trace.get('jsonl_unresolved_link_call_ids') or []) or '(none)'}`",
        "- App Server backend accepted: `false`",
        "- JSONL replacement claimed: `false`",
        "",
        "## Concurrency Checks",
        "",
    ]
    for name, value in checks.items():
        lines.append(f"- {name}: `{value}`")
    lines.extend(
        [
            "",
            "| logical role | agent role | call_id | child | result_hash_match | verdict_match |",
            "|---|---|---|---|---|---|",
        ]
    )
    for item in trace.get("comparisons", []):
        lines.append(
            "| {logical} | {role} | `{call}` | `{child}` | `{result}` | `{verdict}` |".format(
                logical=item.get("logical_role") or "",
                role=item.get("role") or "",
                call=item.get("call_id") or "",
                child=item.get("child_thread_match"),
                result=item.get("result_hash_match"),
                verdict=item.get("result_verdict_match"),
            )
        )
    if trace.get("v13_concurrency_checks", {}).get("failed_checks"):
        lines.extend(
            [
                "",
                "Failed checks:",
                format_list(trace["v13_concurrency_checks"]["failed_checks"]),
            ]
        )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_v13_outputs(out_dir: Path, equivalence: dict[str, Any]) -> None:
    classification = equivalence.get("classification")
    overall = "V13_PASS" if classification == V11_EQUIVALENT else "V13_NOT_PASS"
    matrix = {
        "schema": "codex-harness.app_server_candidate_fanout_parity_matrix.v13",
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "overall_result": overall,
        "app_server_backend_accepted": False,
        "jsonl_remains_authoritative": True,
        "replacement_claimed": False,
        "pattern": {
            "name": "fan-out-fan-in",
            "classification": classification,
            "candidate_capture_status": equivalence.get("candidate_capture_status"),
            "jsonl_capture_status": equivalence.get("jsonl_capture_status"),
            "candidate_aggregation_status": equivalence.get("candidate_aggregation_status"),
            "jsonl_aggregation_status": equivalence.get("jsonl_aggregation_status"),
            "failed_checks": equivalence.get("v13_concurrency_checks", {}).get("failed_checks") or [],
            "candidate_unresolved_link_call_ids": equivalence.get("candidate_unresolved_link_call_ids") or [],
            "jsonl_unresolved_link_call_ids": equivalence.get("jsonl_unresolved_link_call_ids") or [],
        },
    }
    write_json(out_dir / "v13_parity_matrix.json", matrix)
    matrix_lines = [
        "# V13 Fan-Out/Fan-In Parity Matrix",
        "",
        f"RESULT: {overall}",
        "",
        "- App Server backend accepted: `false`",
        "- JSONL replacement claimed: `false`",
        "- JSONL remains authoritative: `true`",
        "",
        "| pattern | classification | candidate_capture | jsonl_capture | candidate_aggregation | jsonl_aggregation | failed_checks |",
        "|---|---|---|---|---|---|---|",
        "| fan-out-fan-in | `{classification}` | `{candidate}` | `{jsonl}` | `{candidate_agg}` | `{jsonl_agg}` | `{failed}` |".format(
            classification=classification,
            candidate=equivalence.get("candidate_capture_status"),
            jsonl=equivalence.get("jsonl_capture_status"),
            candidate_agg=equivalence.get("candidate_aggregation_status"),
            jsonl_agg=equivalence.get("jsonl_aggregation_status"),
            failed=", ".join(matrix["pattern"]["failed_checks"]) or "(none)",
        ),
        "",
    ]
    (out_dir / "v13_parity_matrix.md").write_text("\n".join(matrix_lines), encoding="utf-8")

    report_lines = [
        "# V13 Fan-Out/Fan-In Parity Acceptance",
        "",
        f"RESULT: {overall}",
        "",
        f"- Fan-out/fan-in classification: `{classification}`",
        "- App Server backend accepted: `false`",
        "- JSONL replacement claimed: `false`",
        "- JSONL remains authoritative: `true`",
        "- Strict aggregation relaxed: `false`",
        "- Python spawn_agent attempted: `false`",
        "- codex exec subagent spawning used: `false`",
        "- Live VS Code App Server attached: `false`",
        "",
        "## Required Outputs",
        "",
        "- `jsonl_authoritative_run/`",
        "- `jsonl_authoritative_aggregation/`",
        "- `app_server_candidate_run/`",
        "- `app_server_candidate_aggregation/`",
        "- `dual_backend_equivalence/`",
        "- `v13_parity_matrix.json`",
        "- `v13_parity_matrix.md`",
        "",
        "## Concurrency Checks",
        "",
    ]
    for name, value in equivalence.get("v13_concurrency_checks", {}).get("checks", {}).items():
        report_lines.append(f"- {name}: `{value}`")
    failed = equivalence.get("v13_concurrency_checks", {}).get("failed_checks") or []
    report_lines.extend(["", "Failed checks:", format_list(failed), ""])
    (out_dir / "v13_acceptance_report.md").write_text("\n".join(report_lines), encoding="utf-8")


def blocked_v13_trace(message: str) -> dict[str, Any]:
    return {
        "schema": "codex-harness.app_server_candidate_fanout_parity.v13",
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "pattern_name": "fan-out-fan-in",
        "classification": V11_BLOCKED_BY_RUNTIME_LIMIT,
        "runtime_issue": message,
        "replacement_claimed": False,
        "app_server_backend_accepted": False,
        "authoritative_baseline": "JSONL",
        "comparisons": [],
        "v13_concurrency_checks": {"checks": {}, "failed_checks": ["runtime_blocked"]},
    }


def run_v13_fanout(args: argparse.Namespace) -> int:
    out_dir = ensure_out_dir(args.out_dir)
    pattern = V13_PATTERN_SPEC
    workspace = ensure_live_workspace(str(out_dir / "scratch_workspace"))
    expected_workflow_path = out_dir / "expected_workflow.json"
    runtime_issue: str | None = None
    equivalence: dict[str, Any] | None = None

    try:
        live_summary = run_live_app_server_probe(
            workspace=workspace,
            out_dir=out_dir,
            timeout_seconds=args.live_timeout_seconds,
            probe_label="V13",
            prompt_text=app_server_v13_fanout_prompt(pattern),
        )
        records, malformed = iter_event_records(out_dir / "app_server_events.jsonl")
        if malformed:
            runtime_issue = "App Server event capture contains malformed JSONL lines."
            raise ShadowError(runtime_issue)
        live_spawns = extract_live_spawns(records)
        expected_count = len(pattern["roles"])
        if len(live_spawns) != expected_count:
            runtime_issue = (
                f"observed {len(live_spawns)} spawn_agent calls; expected {expected_count}"
            )
            raise ShadowError(runtime_issue)
        for index, role in enumerate(pattern["roles"]):
            observed_type = live_spawns[index].get("agent_type")
            if observed_type != role["agent_type"]:
                runtime_issue = (
                    f"spawn {index + 1} role mismatch: observed {observed_type}, "
                    f"expected {role['agent_type']}"
                )
                raise ShadowError(runtime_issue)

        write_pattern_expected_workflow(
            expected_workflow_path,
            live_summary=live_summary,
            live_spawns=live_spawns,
            pattern=pattern,
            workspace=workspace,
            run_id="v13_fan_out_fan_in",
        )
        jsonl_run_dir = out_dir / "jsonl_authoritative_run"
        jsonl_run_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(expected_workflow_path, jsonl_run_dir / "expected_workflow.json")
        jsonl_trace = run_native_jsonl_trace(
            expected_workflow_path,
            jsonl_run_dir,
            capture_results=True,
            require_success=False,
        )
        bundle = load_shadow_bundle(
            app_events_path=out_dir / "app_server_events.jsonl",
            jsonl_trace_path=jsonl_run_dir / "jsonl_parallel_trace.json",
            expected_workflow_path=expected_workflow_path,
            live_summary=live_summary,
        )
        candidate_trace = build_candidate_run(
            bundle,
            out_dir / "app_server_candidate_run",
            run_id="v13_fan_out_fan_in",
        )
        candidate_aggregation = run_strict_aggregation(
            out_dir / "app_server_candidate_run",
            out_dir / "app_server_candidate_aggregation",
        )
        jsonl_aggregation = run_strict_aggregation(
            jsonl_run_dir,
            out_dir / "jsonl_authoritative_aggregation",
        )
        equivalence = compare_candidate_to_jsonl(
            candidate_trace,
            jsonl_trace,
            candidate_aggregation,
            jsonl_aggregation,
        )
        equivalence["v10_candidate_classification"] = equivalence.get("classification")
        equivalence["pattern_name"] = pattern["label"]
        equivalence["pattern_slug"] = pattern["slug"]
        equivalence["logical_roles"] = [
            {
                "logical_role": role["logical_role"],
                "agent_type": role["agent_type"],
                "result_file": role["result_file"],
            }
            for role in pattern["roles"]
        ]
        equivalence["classification"] = classify_v11_pattern(equivalence)
        concurrency = validate_v13_concurrency(
            pattern_dir=out_dir,
            pattern=pattern,
            equivalence=equivalence,
            candidate_trace=candidate_trace,
            jsonl_trace=jsonl_trace,
            records=records,
        )
        equivalence["v13_concurrency_checks"] = concurrency
        equivalence["classification"] = classify_v13(equivalence, concurrency)
    except ShadowError as exc:
        if runtime_issue is None:
            runtime_issue = str(exc)
        equivalence = blocked_v13_trace(runtime_issue)

    dual_dir = out_dir / "dual_backend_equivalence"
    dual_dir.mkdir(parents=True, exist_ok=True)
    write_json(dual_dir / "equivalence_trace.json", equivalence)
    write_v13_equivalence_report(dual_dir / "equivalence_report.md", equivalence)
    write_v13_outputs(out_dir, equivalence)
    print(f"classification: {equivalence.get('classification')}")
    return 0 if equivalence.get("classification") == V11_EQUIVALENT else 2


def run_v11_pattern(pattern_name: str, root_out_dir: Path, timeout_seconds: int) -> dict[str, Any]:
    pattern = V11_PATTERN_SPECS[pattern_name]
    pattern_dir = root_out_dir / pattern["slug"]
    pattern_dir.mkdir(parents=True, exist_ok=True)
    workspace = ensure_live_workspace(str(pattern_dir / "scratch_workspace"))
    expected_workflow_path = pattern_dir / "expected_workflow.json"
    runtime_issue: str | None = None
    equivalence: dict[str, Any] | None = None

    try:
        live_summary = run_live_app_server_probe(
            workspace=workspace,
            out_dir=pattern_dir,
            timeout_seconds=timeout_seconds,
            probe_label="V11",
            prompt_text=app_server_pattern_prompt(pattern),
        )
        records, malformed = iter_event_records(pattern_dir / "app_server_events.jsonl")
        if malformed:
            runtime_issue = "App Server event capture contains malformed JSONL lines."
            raise ShadowError(runtime_issue)
        live_spawns = extract_live_spawns(records)
        expected_count = len(pattern["roles"])
        if len(live_spawns) != expected_count:
            runtime_issue = (
                f"observed {len(live_spawns)} spawn_agent calls; expected {expected_count}"
            )
            raise ShadowError(runtime_issue)
        for index, role in enumerate(pattern["roles"]):
            observed_type = live_spawns[index].get("agent_type")
            if observed_type != role["agent_type"]:
                runtime_issue = (
                    f"spawn {index + 1} role mismatch: observed {observed_type}, "
                    f"expected {role['agent_type']}"
                )
                raise ShadowError(runtime_issue)

        write_pattern_expected_workflow(
            expected_workflow_path,
            live_summary=live_summary,
            live_spawns=live_spawns,
            pattern=pattern,
            workspace=workspace,
            run_id=f"v11_{pattern['slug']}",
        )
        jsonl_run_dir = pattern_dir / "jsonl_authoritative_run"
        jsonl_run_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(expected_workflow_path, jsonl_run_dir / "expected_workflow.json")
        jsonl_trace = run_native_jsonl_trace(
            expected_workflow_path,
            jsonl_run_dir,
            capture_results=True,
            require_success=False,
        )
        bundle = load_shadow_bundle(
            app_events_path=pattern_dir / "app_server_events.jsonl",
            jsonl_trace_path=jsonl_run_dir / "jsonl_parallel_trace.json",
            expected_workflow_path=expected_workflow_path,
            live_summary=live_summary,
        )
        candidate_trace = build_candidate_run(
            bundle,
            pattern_dir / "app_server_candidate_run",
            run_id=f"v11_{pattern['slug']}",
        )
        candidate_aggregation = run_strict_aggregation(
            pattern_dir / "app_server_candidate_run",
            pattern_dir / "app_server_candidate_aggregation",
        )
        jsonl_aggregation = run_strict_aggregation(
            jsonl_run_dir,
            pattern_dir / "jsonl_authoritative_aggregation",
        )
        equivalence = compare_candidate_to_jsonl(
            candidate_trace,
            jsonl_trace,
            candidate_aggregation,
            jsonl_aggregation,
        )
        equivalence["v10_candidate_classification"] = equivalence.get("classification")
        equivalence["pattern_name"] = pattern["label"]
        equivalence["pattern_slug"] = pattern["slug"]
        equivalence["logical_roles"] = [
            {
                "logical_role": role["logical_role"],
                "agent_type": role["agent_type"],
                "result_file": role["result_file"],
            }
            for role in pattern["roles"]
        ]
        equivalence["classification"] = classify_v11_pattern(equivalence)
    except ShadowError as exc:
        if runtime_issue is None:
            runtime_issue = str(exc)
        equivalence = blocked_v11_pattern_trace(pattern, runtime_issue)

    write_json(pattern_dir / "equivalence_trace.json", equivalence)
    write_v11_pattern_equivalence_report(pattern_dir / "equivalence_report.md", equivalence)
    return {
        "pattern_name": pattern["label"],
        "pattern_slug": pattern["slug"],
        "classification": equivalence.get("classification"),
        "runtime_issue": equivalence.get("runtime_issue"),
        "candidate_capture_status": equivalence.get("candidate_capture_status"),
        "jsonl_capture_status": equivalence.get("jsonl_capture_status"),
        "candidate_aggregation_status": equivalence.get("candidate_aggregation_status"),
        "jsonl_aggregation_status": equivalence.get("jsonl_aggregation_status"),
        "mismatched_call_ids": equivalence.get("mismatched_call_ids") or [],
        "candidate_unresolved_link_call_ids": equivalence.get("candidate_unresolved_link_call_ids") or [],
        "jsonl_unresolved_link_call_ids": equivalence.get("jsonl_unresolved_link_call_ids") or [],
        "out_dir": str(pattern_dir),
    }


def write_v11_matrix(out_dir: Path, results: list[dict[str, Any]]) -> None:
    all_required_pass = all(
        item.get("classification") == V11_EQUIVALENT
        for item in results
        if item.get("pattern_name") in V11_REQUIRED_PATTERNS
    )
    payload = {
        "schema": "codex-harness.app_server_candidate_parity_matrix.v11",
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "overall_result": "V11_PASS" if all_required_pass else "V11_PARTIAL_PARITY",
        "app_server_backend_accepted": False,
        "jsonl_remains_authoritative": True,
        "replacement_claimed": False,
        "patterns": results,
    }
    write_json(out_dir / "v11_parity_matrix.json", payload)

    lines = [
        "# V11 App Server Candidate Parity Matrix",
        "",
        f"RESULT: {payload['overall_result']}",
        "",
        "- App Server backend accepted: `false`",
        "- JSONL replacement claimed: `false`",
        "- JSONL remains authoritative: `true`",
        "",
        "| pattern | classification | candidate_capture | jsonl_capture | candidate_aggregation | jsonl_aggregation | unresolved_links |",
        "|---|---|---|---|---|---|---|",
    ]
    for item in results:
        unresolved = (item.get("candidate_unresolved_link_call_ids") or []) + (
            item.get("jsonl_unresolved_link_call_ids") or []
        )
        lines.append(
            "| {pattern} | `{classification}` | `{candidate}` | `{jsonl}` | `{candidate_agg}` | `{jsonl_agg}` | `{unresolved}` |".format(
                pattern=item.get("pattern_name"),
                classification=item.get("classification"),
                candidate=item.get("candidate_capture_status"),
                jsonl=item.get("jsonl_capture_status"),
                candidate_agg=item.get("candidate_aggregation_status"),
                jsonl_agg=item.get("jsonl_aggregation_status"),
                unresolved=", ".join(unresolved) or "(none)",
            )
        )
    lines.append("")
    (out_dir / "v11_parity_matrix.md").write_text("\n".join(lines), encoding="utf-8")

    report_lines = [
        "# V11 App Server Multi-Run Multi-Pattern Acceptance",
        "",
        f"RESULT: {payload['overall_result']}",
        "",
        "- Required patterns: `project-explorer-only`, `explorer-implementer-reviewer`, `producer-reviewer`, `planner-generator-evaluator`",
        "- Optional fan-out-fan-in: `not run`",
        "- App Server backend accepted: `false`",
        "- JSONL replacement claimed: `false`",
        "- JSONL remains authoritative: `true`",
        "- Strict aggregation relaxed: `false`",
        "- Runner behavior modified: `false`",
        "- Python spawn_agent attempted: `false`",
        "- codex exec subagent spawning used: `false`",
        "- Live VS Code App Server attached: `false`",
        "",
        "## Pattern Results",
        "",
    ]
    for item in results:
        report_lines.append(f"- {item['pattern_name']}: `{item['classification']}`")
        if item.get("runtime_issue"):
            report_lines.append(f"  runtime_issue: {item['runtime_issue']}")
    report_lines.append("")
    (out_dir / "v11_acceptance_report.md").write_text("\n".join(report_lines), encoding="utf-8")


def run_v11_parity(args: argparse.Namespace) -> int:
    out_dir = ensure_out_dir(args.out_dir)
    results: list[dict[str, Any]] = []
    for pattern_name in V11_REQUIRED_PATTERNS:
        print(f"[V11] running {pattern_name}")
        results.append(run_v11_pattern(pattern_name, out_dir, args.live_timeout_seconds))
    write_v11_matrix(out_dir, results)
    all_pass = all(item.get("classification") == V11_EQUIVALENT for item in results)
    print(f"classification: {'V11_PASS' if all_pass else 'V11_PARTIAL_PARITY'}")
    return 0 if all_pass else 2


def run_live(args: argparse.Namespace) -> int:
    out_dir = ensure_out_dir(args.out_dir)
    try:
        workspace = ensure_live_workspace(args.workspace)
        expected_workflow_path = ensure_live_expected_workflow_path(args.expected_workflow, out_dir)
        probe_label = "V10" if args.emit_candidate_run else "V9"
        live_summary = run_live_app_server_probe(
            workspace=workspace,
            out_dir=out_dir,
            timeout_seconds=args.live_timeout_seconds,
            probe_label=probe_label,
        )
        records, malformed = iter_event_records(out_dir / "app_server_events.jsonl")
        if malformed:
            raise ShadowError("Live App Server event capture contains malformed JSONL lines.")
        live_spawn = extract_live_spawn(records)
        write_live_expected_workflow(
            expected_workflow_path,
            live_summary,
            live_spawn,
            workspace,
            run_id="v10_app_server_candidate_probe" if args.emit_candidate_run else "v9_app_server_live_shadow_probe",
            source_note=(
                "V10 isolated App Server candidate backend probe"
                if args.emit_candidate_run
                else "V9 isolated App Server live shadow probe"
            ),
        )
        if args.emit_candidate_run:
            jsonl_run_dir = out_dir / "jsonl_authoritative_run"
            jsonl_run_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(expected_workflow_path, jsonl_run_dir / "expected_workflow.json")
            run_native_jsonl_trace(expected_workflow_path, jsonl_run_dir, capture_results=True)
            equivalence = run_v10_candidate_outputs(
                out_dir=out_dir,
                app_events_path=out_dir / "app_server_events.jsonl",
                jsonl_trace_path=jsonl_run_dir / "jsonl_parallel_trace.json",
                expected_workflow_path=expected_workflow_path,
                live_summary=live_summary,
            )
            print(f"classification: {equivalence['classification']}")
            return 0 if equivalence.get("classification") == CLASS_V10_EQUIVALENT else 2

        run_native_jsonl_trace(expected_workflow_path, out_dir)
        run_offline_inputs(
            app_events_path=out_dir / "app_server_events.jsonl",
            jsonl_trace_path=out_dir / "jsonl_parallel_trace.json",
            expected_workflow_path=expected_workflow_path,
            out_dir=out_dir,
            phase="V9",
            live_summary=live_summary,
        )
    except ShadowError as exc:
        message = str(exc)
        if args.emit_candidate_run:
            write_blocked_v10_report(out_dir, message)
        else:
            write_blocked_v9_report(out_dir, message)
        print(f"ERROR: {message}", file=sys.stderr)
        return 2
    return 0


def run(args: argparse.Namespace) -> int:
    if args.v13_fanout:
        return run_v13_fanout(args)
    if args.v11_parity:
        return run_v11_parity(args)
    if args.live:
        return run_live(args)

    if not args.app_server_events:
        raise ShadowError("--app-server-events is required unless --live is used")
    if not args.jsonl_trace:
        raise ShadowError("--jsonl-trace is required unless --live is used")
    app_events_path = ensure_input_file(args.app_server_events, "app-server-events")
    jsonl_trace_path = ensure_input_file(args.jsonl_trace, "jsonl-trace")
    expected_workflow_path = (
        ensure_input_file(args.expected_workflow, "expected-workflow")
        if args.expected_workflow
        else None
    )
    out_dir = ensure_out_dir(args.out_dir)
    run_offline_inputs(
        app_events_path=app_events_path,
        jsonl_trace_path=jsonl_trace_path,
        expected_workflow_path=expected_workflow_path,
        out_dir=out_dir,
        phase="V8",
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "App Server shadow adapter that compares captured App Server events "
            "against the accepted JSONL native subagent trace. Offline replay is "
            "the default; --live launches an isolated stdio App Server probe."
        )
    )
    parser.add_argument("--live", action="store_true", help="Run an isolated live App Server shadow probe.")
    parser.add_argument(
        "--emit-candidate-run",
        action="store_true",
        help=(
            "Emit V10 App Server candidate run artifacts in shadow mode. "
            "With --live, the JSONL authoritative run is captured in a sibling directory."
        ),
    )
    parser.add_argument(
        "--v11-parity",
        action="store_true",
        help=(
            "Run the V11 isolated App Server candidate parity suite across "
            "project-explorer-only, explorer-implementer-reviewer, producer-reviewer, "
            "and planner-generator-evaluator patterns."
        ),
    )
    parser.add_argument(
        "--v13-fanout",
        action="store_true",
        help=(
            "Run the V13 isolated fan-out/fan-in candidate parity probe with "
            "indexed producer roles and JSONL authoritative comparison."
        ),
    )
    parser.add_argument("--workspace", help="Live mode scratch workspace under .codex-harness/.")
    parser.add_argument(
        "--live-timeout-seconds",
        type=int,
        default=DEFAULT_LIVE_TIMEOUT_SECONDS,
        help="Live mode timeout for parent turn completion.",
    )
    parser.add_argument("--app-server-events", help="Captured App Server event JSONL.")
    parser.add_argument("--jsonl-trace", help="Accepted JSONL adapter trace JSON.")
    parser.add_argument(
        "--expected-workflow",
        help=(
            "Expected workflow manifest JSON. Offline mode reads it; live mode "
            "writes concrete call_id mapping and requires the path to be inside --out-dir."
        ),
    )
    parser.add_argument("--out-dir", required=True, help="Output directory under .codex-harness/reports/.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.live and not args.workspace:
        parser.error("--workspace is required with --live")
    try:
        return run(args)
    except ShadowError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
