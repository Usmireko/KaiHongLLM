#!/usr/bin/env python3
"""V15 parallelism and runtime-limit benchmark.

This tool is intentionally report-only. It does not alter strict capture or
aggregation policy, and it keeps JSONL as the authoritative backend.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import codex_app_server_shadow_adapter as shadow


ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = ROOT / ".codex-harness" / "tools"
REPORTS_ROOT = ROOT / ".codex-harness" / "reports"
DEFAULT_OUT_DIR = REPORTS_ROOT / "v15_parallelism_benchmark_probe"
V13_DIR = REPORTS_ROOT / "v13_fan_out_fan_in_parity_probe"
V25_SOURCE_DIR = REPORTS_ROOT / "v2_5_positive_source_probe"
V8_DIR = REPORTS_ROOT / "v8_app_server_shadow_probe"
V11_MATRIX = REPORTS_ROOT / "v11_app_server_parity_probe" / "v11_parity_matrix.json"
V12_DIR = REPORTS_ROOT / "v12_runner_backend_selection_probe"
V14_DIR = REPORTS_ROOT / "v14_backend_policy_probe"


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if default is not None:
            return default
        raise


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def sha256_file(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_iso_ms(value: str | None) -> float | None:
    if not value or not isinstance(value, str):
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value).timestamp() * 1000.0
    except ValueError:
        return None


def record_ts_ms(record: dict[str, Any]) -> float | None:
    ts = record.get("ts")
    if isinstance(ts, (int, float)):
        return float(ts) * 1000.0
    return parse_iso_ms(record.get("timestamp"))


def event_payload(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("payload")
    return payload if isinstance(payload, dict) else record


def event_item(payload: dict[str, Any]) -> dict[str, Any]:
    item = shadow.event_item(payload)
    return item if isinstance(item, dict) else {}


def iter_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                records.append(obj)
    return records


def run_command_timed(args: list[str], cwd: Path = ROOT, timeout: int = 240) -> dict[str, Any]:
    start = time.perf_counter()
    proc = subprocess.run(
        args,
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    elapsed_ms = round((time.perf_counter() - start) * 1000.0, 3)
    return {
        "command": args,
        "exit_code": proc.returncode,
        "elapsed_ms": elapsed_ms,
        "stdout_tail": proc.stdout[-4000:],
        "stderr_tail": proc.stderr[-4000:],
    }


def app_server_call_times(events_path: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for record in iter_jsonl(events_path):
        payload = event_payload(record)
        item = event_item(payload)
        item_type = item.get("type") or payload.get("type")
        ts_ms = record_ts_ms(record)
        call_id = item.get("call_id") or item.get("id")
        name = item.get("name")

        if item_type == "function_call" and name == "spawn_agent" and call_id:
            entry = out.setdefault(str(call_id), {"call_id": str(call_id)})
            entry["app_spawn_event_ts_ms"] = ts_ms
            entry["app_spawn_status"] = item.get("status")
            entry["spawn_agent_arguments"] = shadow.parse_json_maybe(item.get("arguments"))
            continue

        if item_type == "function_call_output" and call_id:
            output = shadow.parse_json_maybe(item.get("output"))
            entry = out.setdefault(str(call_id), {"call_id": str(call_id)})
            entry["app_function_call_output_ts_ms"] = ts_ms
            entry["function_call_output"] = output
            if isinstance(output, dict):
                entry["function_call_output_agent_id"] = output.get("agent_id")
                entry["function_call_output_nickname"] = output.get("nickname")
            continue

        if item_type == "collabAgentToolCall":
            tool_id = item.get("id") or item.get("toolCallId")
            if tool_id:
                entry = out.setdefault(str(tool_id), {"call_id": str(tool_id)})
                status = item.get("status")
                if status == "started":
                    entry["app_collab_started_ts_ms"] = ts_ms
                elif status == "completed":
                    entry["app_collab_completed_ts_ms"] = ts_ms
                else:
                    entry.setdefault("app_collab_other_events", []).append({"status": status, "ts_ms": ts_ms})
                for key in ("senderThreadId", "receiverThreadIds", "prompt"):
                    if key in item:
                        entry[key] = item.get(key)
    return out


def parent_log_timestamps(parent_log_path: Path, line_numbers: list[int]) -> dict[int, float | None]:
    wanted = set(line_numbers)
    out: dict[int, float | None] = {}
    if not parent_log_path.exists():
        return {line: None for line in wanted}
    with parent_log_path.open("r", encoding="utf-8", errors="replace") as f:
        for idx, line in enumerate(f, start=1):
            if idx not in wanted:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                out[idx] = None
                continue
            out[idx] = parse_iso_ms(obj.get("timestamp")) or record_ts_ms(obj)
    for line in wanted:
        out.setdefault(line, None)
    return out


def child_log_timing(log_path: Path) -> dict[str, Any]:
    timing: dict[str, Any] = {"child_log_path": str(log_path), "child_log_exists": log_path.exists()}
    first_token_candidates: list[float] = []
    for record in iter_jsonl(log_path):
        wrapper_type = record.get("type")
        wrapper_ts = parse_iso_ms(record.get("timestamp")) or record_ts_ms(record)
        payload = record.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        payload_type = payload.get("type")

        if wrapper_type == "session_meta":
            timing.setdefault("session_meta_wrapper_ts_ms", wrapper_ts)
            timing.setdefault("session_meta_payload_ts_ms", parse_iso_ms(payload.get("timestamp")))
            thread_spawn = ((payload.get("source") or {}).get("subagent") or {}).get("thread_spawn") or {}
            timing["parent_thread_id"] = payload.get("parent_thread_id") or thread_spawn.get("parent_thread_id")
        elif wrapper_type == "event_msg" and payload_type == "task_started":
            timing.setdefault("task_started_wrapper_ts_ms", wrapper_ts)
            if isinstance(payload.get("started_at"), (int, float)):
                timing.setdefault("task_started_payload_started_at_ms", float(payload["started_at"]) * 1000.0)
        elif wrapper_type == "event_msg" and payload_type == "task_complete":
            timing.setdefault("task_complete_wrapper_ts_ms", wrapper_ts)
            if isinstance(payload.get("completed_at"), (int, float)):
                timing.setdefault("task_complete_completed_at_ms", float(payload["completed_at"]) * 1000.0)
            for key in ("duration_ms", "time_to_first_token_ms"):
                if isinstance(payload.get(key), (int, float)):
                    timing[key] = payload[key]
        elif wrapper_type == "event_msg" and payload_type == "agent_message":
            if wrapper_ts is not None:
                first_token_candidates.append(wrapper_ts)
        elif wrapper_type == "response_item" and payload_type == "message":
            if wrapper_ts is not None:
                first_token_candidates.append(wrapper_ts)

    if first_token_candidates:
        timing["first_token_observed_ts_ms"] = min(first_token_candidates)

    start = (
        timing.get("task_started_payload_started_at_ms")
        or timing.get("session_meta_payload_ts_ms")
        or timing.get("session_meta_wrapper_ts_ms")
    )
    end = timing.get("task_complete_completed_at_ms") or timing.get("task_complete_wrapper_ts_ms")
    timing["active_start_ts_ms"] = start
    timing["active_end_ts_ms"] = end
    if isinstance(start, (int, float)) and isinstance(end, (int, float)):
        timing["active_duration_ms"] = round(end - start, 3)
    return timing


def fmt_ms(ms: float | int | None) -> str:
    if ms is None:
        return "n/a"
    return f"{float(ms):.3f}"


def measure_fanout_timing(out_dir: Path) -> dict[str, Any]:
    trace_path = V13_DIR / "jsonl_authoritative_run" / "spawn_capture_trace.json"
    expected_path = V13_DIR / "jsonl_authoritative_run" / "expected_workflow.json"
    events_path = V13_DIR / "app_server_events.jsonl"
    trace = read_json(trace_path, {})
    expected = read_json(expected_path, {})
    app_times = app_server_call_times(events_path)

    roles_by_call_id = {
        role.get("call_id"): role
        for role in expected.get("expected_roles", [])
        if isinstance(role, dict) and role.get("call_id")
    }
    spawns = trace.get("spawns") or []
    line_numbers: list[int] = []
    for spawn in spawns:
        parent_spawn = spawn.get("parent_spawn_call") or {}
        child_link_proof = spawn.get("child_link_proof") or {}
        function_output_line = spawn.get("function_call_output_line") or child_link_proof.get("function_call_output_line")
        for line in (parent_spawn.get("line"), function_output_line):
            if isinstance(line, int):
                line_numbers.append(line)

    parent_log_path = Path(trace.get("source_paths", {}).get("parent_log") or "")
    line_ts = parent_log_timestamps(parent_log_path, line_numbers)

    entries: list[dict[str, Any]] = []
    for spawn in spawns:
        call_id = spawn.get("call_id")
        role_spec = roles_by_call_id.get(call_id, {})
        role_id = role_spec.get("logical_role") or role_spec.get("role_id") or role_spec.get("role") or spawn.get("role")
        parent_spawn = spawn.get("parent_spawn_call") or {}
        child_log_path_value = spawn.get("child_log_path") or (spawn.get("child") or {}).get("log_path")
        child_log_path = Path(child_log_path_value) if child_log_path_value else Path("__missing_child_log__")
        child_timing = child_log_timing(child_log_path)
        parent_spawn_line = parent_spawn.get("line")
        output_line = spawn.get("function_call_output_line") or (spawn.get("child_link_proof") or {}).get("function_call_output_line")
        app_entry = app_times.get(call_id, {})
        entry = {
            "role_id": role_id,
            "role": role_spec.get("agent_type") or role_spec.get("role") or spawn.get("role"),
            "call_id": call_id,
            "child_thread_id": spawn.get("child_thread_id"),
            "parent_spawn_line": parent_spawn_line,
            "function_call_output_line": output_line,
            "jsonl_parent_spawn_ts_ms": line_ts.get(parent_spawn_line),
            "jsonl_function_call_output_ts_ms": line_ts.get(output_line),
            "app_spawn_event_ts_ms": app_entry.get("app_spawn_event_ts_ms"),
            "app_function_call_output_ts_ms": app_entry.get("app_function_call_output_ts_ms"),
            "app_collab_started_ts_ms": app_entry.get("app_collab_started_ts_ms"),
            "app_collab_completed_ts_ms": app_entry.get("app_collab_completed_ts_ms"),
            "receiverThreadIds": app_entry.get("receiverThreadIds"),
            "function_call_output_agent_id": app_entry.get("function_call_output_agent_id"),
            "child_timing": child_timing,
        }
        for lhs, rhs, out_key in [
            ("app_spawn_event_ts_ms", "jsonl_parent_spawn_ts_ms", "app_minus_jsonl_spawn_ms"),
            (
                "app_function_call_output_ts_ms",
                "jsonl_function_call_output_ts_ms",
                "app_minus_jsonl_function_output_ms",
            ),
        ]:
            if isinstance(entry.get(lhs), (int, float)) and isinstance(entry.get(rhs), (int, float)):
                entry[out_key] = round(float(entry[lhs]) - float(entry[rhs]), 3)
        entries.append(entry)

    producers = [entry for entry in entries if str(entry.get("role_id", "")).startswith("producer[")]
    overlap_ms: float | None = None
    wall_clock_parallel = False
    scheduling_classification = "INSUFFICIENT_TIMING"
    if len(producers) >= 2:
        p0, p1 = producers[0], producers[1]
        s0 = p0["child_timing"].get("active_start_ts_ms")
        e0 = p0["child_timing"].get("active_end_ts_ms")
        s1 = p1["child_timing"].get("active_start_ts_ms")
        e1 = p1["child_timing"].get("active_end_ts_ms")
        if all(isinstance(v, (int, float)) for v in (s0, e0, s1, e1)):
            overlap_ms = max(0.0, min(float(e0), float(e1)) - max(float(s0), float(s1)))
            wall_clock_parallel = overlap_ms > 0
            scheduling_classification = "WALL_CLOCK_OVERLAP" if wall_clock_parallel else "SERIAL_OR_NON_OVERLAPPING"

    result = {
        "source": "accepted V13 fan-out/fan-in run",
        "jsonl_trace": str(trace_path),
        "app_server_events": str(events_path),
        "parent_log_path": str(parent_log_path),
        "capture_status": trace.get("capture_status"),
        "workflow_validation_scope": trace.get("workflow_validation_scope"),
        "allow_partial": trace.get("allow_partial"),
        "entries": entries,
        "producer_overlap_ms": overlap_ms,
        "wall_clock_parallel": wall_clock_parallel,
        "scheduling_classification": scheduling_classification,
        "logical_correctness_source": "V13 parity accepted baseline; V15 does not reclassify correctness.",
    }
    write_json(out_dir / "fanout_timing_trace.json", result)
    write_fanout_report(out_dir / "fanout_timing_report.md", result)
    return result


def write_fanout_report(path: Path, data: dict[str, Any]) -> None:
    lines = [
        "# V15 Fan-Out/Fan-In Timing Benchmark",
        "",
        "RESULT: BENCHMARK_RECORDED",
        "",
        "This report measures timing evidence only. Logical fan-out/fan-in correctness remains the V13 accepted baseline.",
        "",
        f"- Source run: {data['source']}",
        f"- JSONL capture_status: {data.get('capture_status')}",
        f"- workflow_validation_scope: {data.get('workflow_validation_scope')}",
        f"- allow_partial: {data.get('allow_partial')}",
        f"- producer overlap window ms: {fmt_ms(data.get('producer_overlap_ms'))}",
        f"- wall-clock parallel evidence: {data.get('wall_clock_parallel')}",
        f"- scheduling classification: {data.get('scheduling_classification')}",
        "",
        "## Role Timing",
        "",
        "| role_id | call_id | child_thread_id | spawn_ts_ms | child_start_ms | first_token_ms | complete_ms | duration_ms |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for entry in data.get("entries", []):
        child = entry.get("child_timing") or {}
        lines.append(
            "| {role_id} | {call_id} | {child_thread_id} | {spawn} | {start} | {first} | {complete} | {duration} |".format(
                role_id=entry.get("role_id"),
                call_id=entry.get("call_id"),
                child_thread_id=entry.get("child_thread_id"),
                spawn=fmt_ms(entry.get("jsonl_parent_spawn_ts_ms")),
                start=fmt_ms(child.get("active_start_ts_ms")),
                first=fmt_ms(child.get("first_token_observed_ts_ms")),
                complete=fmt_ms(child.get("active_end_ts_ms")),
                duration=fmt_ms(child.get("duration_ms") or child.get("active_duration_ms")),
            )
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "- Logical fan-out/fan-in correctness: covered by V13 parity acceptance, not revalidated as a new gate here.",
            "- Wall-clock parallelism: claimed only when producer active windows overlap.",
            "- Runtime scheduling: represented by observed spawn, child start, first-token, and completion timestamps.",
        ]
    )
    write_text(path, "\n".join(lines) + "\n")


def v15_width_prompt(width: int) -> str:
    tasks = "\n".join(
        f"- producer[{i}]: ask a native project-explorer subagent for a two-line harness-only summary."
        for i in range(width)
    )
    return f"""V15 runtime thread-limit probe width={width}.

Safety:
- Harness-only / scratch-only read-only task.
- Do not modify files.
- Do not run shell commands.
- Do not use codex exec.
- Parent Codex native runtime is responsible for subagent calls.

Launch {width} independent native project-explorer subagents if the runtime permits it:
{tasks}

Each child should return exactly:
RESULT: PASS
V15 width={width} producer[index] completed.

After all available children complete, parent should return:
RESULT: PASS
V15 width={width} parent completed.
"""


def write_width_expected_workflow(
    path: Path,
    live_spawns: list[dict[str, Any]],
    width: int,
    *,
    parent_thread_id: str | None,
    parent_log_path: str | None,
) -> None:
    roles = []
    for idx, spawn in enumerate(live_spawns):
        role_id = f"producer[{idx}]"
        roles.append(
            {
                "role_id": role_id,
                "role": "project-explorer",
                "call_id": spawn.get("call_id"),
                "required": True,
                "result_file": f"{idx + 1:02d}_producer_{idx}_result.md",
            }
        )
    expected = {
        "workflow_version": "v15-thread-limit-probe",
        "pattern_name": "fan-out-width-probe",
        "workflow_validation_scope": "expected_call_ids",
        "allow_partial": False,
        "synthetic": False,
        "requested_width": width,
        "parent_thread_id": parent_thread_id,
        "log_root": str(Path(parent_log_path).parents[3]) if parent_log_path else None,
        "expected_roles": roles,
        "strict_privacy": {
            "capture_backend": "jsonl",
            "authoritative_backend": "jsonl",
            "app_server_candidate_only": True,
        },
        "aggregation_pass_rule": "trace benchmark only; not a correctness acceptance run",
    }
    write_json(path, expected)


def scan_thread_limit_messages(events_path: Path) -> list[dict[str, Any]]:
    patterns = [
        re.compile(p, re.IGNORECASE)
        for p in [
            r"thread\s+limit\s+reached",
            r"concurrency\s+limit",
            r"too\s+many\s+(threads|agents|subagents)",
            r"maximum\s+.*(threads|agents|subagents)",
            r"failed\s+to\s+spawn",
            r"cannot\s+spawn",
            r"no\s+available\s+(thread|agent|subagent)",
        ]
    ]
    hits: list[dict[str, Any]] = []
    for idx, record in enumerate(iter_jsonl(events_path), start=1):
        payload = event_payload(record)
        item = event_item(payload) if isinstance(payload, dict) else {}
        if item.get("role") == "user":
            continue
        text = json.dumps(record, ensure_ascii=False)
        lower = text.lower()
        if "account/ratelimits/updated" in lower:
            continue
        if any(pattern.search(text) for pattern in patterns):
            hits.append({"line": idx, "ts_ms": record_ts_ms(record), "excerpt": text[:1000]})
    return hits


def runtime_thread_limit_probe(out_dir: Path, widths: list[int], timeout_seconds: int) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for width in widths:
        width_dir = out_dir / f"thread_limit_width_{width}"
        width_dir.mkdir(parents=True, exist_ok=True)
        prompt = v15_width_prompt(width)
        probe: dict[str, Any] = {
            "width": width,
            "out_dir": str(width_dir),
            "requested_spawns": width,
            "isolated_app_server": True,
            "workflow_validation_scope": "expected_call_ids when successful spawns can be mapped",
        }
        start = time.perf_counter()
        try:
            workspace = width_dir / "scratch_workspace"
            workspace.mkdir(parents=True, exist_ok=True)
            live = shadow.run_live_app_server_probe(
                workspace=workspace,
                out_dir=width_dir,
                timeout_seconds=timeout_seconds,
                probe_label=f"V15-width-{width}",
                prompt_text=prompt,
            )
            probe["live_probe_exit"] = "completed"
            probe["live_probe_elapsed_ms"] = round((time.perf_counter() - start) * 1000.0, 3)
            events_path = Path(live["events_path"])
            records, malformed = shadow.iter_event_records(events_path)
            live_spawns = shadow.extract_live_spawns(records)
            probe["app_server_events"] = str(events_path)
            probe["malformed_event_count"] = len(malformed)
            probe["parent_thread_id"] = live.get("parent_thread_id")
            probe["successful_spawns"] = len([s for s in live_spawns if s.get("child_thread_id")])
            probe["spawn_function_calls"] = len(live_spawns)
            probe["failed_spawns"] = max(0, width - probe["successful_spawns"])
            probe["spawns"] = live_spawns
            probe["runtime_thread_limit_messages"] = scan_thread_limit_messages(events_path)

            if live_spawns:
                expected_path = width_dir / "expected_workflow.json"
                jsonl_dir = width_dir / "jsonl_expected_call_ids_trace"
                write_width_expected_workflow(
                    expected_path,
                    live_spawns,
                    width,
                    parent_thread_id=live.get("parent_thread_id"),
                    parent_log_path=live.get("parent_log_path"),
                )
                adapter_run = run_command_timed(
                    [
                        sys.executable,
                        str(TOOLS_DIR / "codex_native_subagent_adapter.py"),
                        "--expected-workflow",
                        str(expected_path),
                        "--run-dir",
                        str(jsonl_dir),
                        "--dry-run",
                    ],
                    timeout=180,
                )
                trace_path = jsonl_dir / "spawn_capture_trace.json"
                probe["jsonl_trace_runtime"] = adapter_run
                probe["jsonl_trace_path"] = str(trace_path)
                trace_data = read_json(trace_path, {}) if trace_path.exists() else {}
                probe["jsonl_capture_status"] = trace_data.get("capture_status")
                probe["jsonl_adapter_exit_code"] = trace_data.get("adapter_exit_code")
                probe["jsonl_out_of_scope_partial_count"] = trace_data.get("out_of_scope_partial_count")
                probe["jsonl_unresolved_link_call_ids"] = trace_data.get("unresolved_link_call_ids")
                probe["jsonl_missing_spawn_end_call_ids"] = trace_data.get("missing_spawn_end_call_ids")
            else:
                probe["jsonl_capture_status"] = "not_run_no_successful_spawns"
        except Exception as exc:  # noqa: BLE001 - report-only benchmark
            probe["live_probe_exit"] = "blocked_or_failed"
            probe["live_probe_elapsed_ms"] = round((time.perf_counter() - start) * 1000.0, 3)
            probe["error"] = str(exc)
            probe["successful_spawns"] = 0
            probe["failed_spawns"] = width
            probe["runtime_thread_limit_messages"] = []
        results.append(probe)

    trace = {
        "benchmark": "runtime thread-limit probe",
        "safety": {
            "isolated_app_server_process": True,
            "workspace_scope": "scratch directories under .codex-harness/reports",
            "business_files_modified": False,
        },
        "widths": widths,
        "results": results,
        "strict_scope_observation": "When successful call_ids are available, JSONL capture uses expected_call_ids and records out_of_scope_partial_count separately.",
    }
    write_json(out_dir / "runtime_thread_limit_trace.json", trace)
    write_thread_limit_report(out_dir / "runtime_thread_limit_report.md", trace)
    return trace


def write_thread_limit_report(path: Path, data: dict[str, Any]) -> None:
    lines = [
        "# V15 Runtime Thread-Limit Probe",
        "",
        "RESULT: BENCHMARK_RECORDED",
        "",
        "This probe records runtime behavior. In-scope strict correctness is still governed by expected_call_ids capture.",
        "",
        "| width | live probe | successful spawns | failed spawns | JSONL capture_status | out_of_scope_partial_count | thread-limit messages |",
        "| ---: | --- | ---: | ---: | --- | ---: | ---: |",
    ]
    for result in data.get("results", []):
        lines.append(
            "| {width} | {exit} | {success} | {failed} | {status} | {partial} | {msgs} |".format(
                width=result.get("width"),
                exit=result.get("live_probe_exit"),
                success=result.get("successful_spawns"),
                failed=result.get("failed_spawns"),
                status=result.get("jsonl_capture_status"),
                partial=result.get("jsonl_out_of_scope_partial_count", "n/a"),
                msgs=len(result.get("runtime_thread_limit_messages") or []),
            )
        )
    lines.extend(
        [
            "",
            "## Boundary",
            "",
            "- Runtime failures are benchmark data, not relaxed acceptance.",
            "- Expected-call-id scoping is used only after concrete call IDs are observed.",
            "- Out-of-scope partials are recorded separately from expected role failures.",
        ]
    )
    write_text(path, "\n".join(lines) + "\n")


def backend_event_latency(out_dir: Path, fanout_trace: dict[str, Any]) -> dict[str, Any]:
    entries = []
    for entry in fanout_trace.get("entries", []):
        row = {
            "role_id": entry.get("role_id"),
            "call_id": entry.get("call_id"),
            "child_thread_id": entry.get("child_thread_id"),
            "jsonl_spawn_ts_ms": entry.get("jsonl_parent_spawn_ts_ms"),
            "app_spawn_event_ts_ms": entry.get("app_spawn_event_ts_ms"),
            "spawn_app_minus_jsonl_ms": entry.get("app_minus_jsonl_spawn_ms"),
            "jsonl_link_ts_ms": entry.get("jsonl_function_call_output_ts_ms"),
            "app_link_ts_ms": entry.get("app_function_call_output_ts_ms"),
            "link_app_minus_jsonl_ms": entry.get("app_minus_jsonl_function_output_ms"),
            "child_thread_observed_ts_ms": (entry.get("child_timing") or {}).get("session_meta_wrapper_ts_ms"),
            "task_result_observed_ts_ms": (entry.get("child_timing") or {}).get("task_complete_wrapper_ts_ms"),
        }
        entries.append(row)

    trace = {
        "source": "accepted V13 fan-out/fan-in run",
        "comparison_note": "App Server times are client observation times. JSONL times are session-log wrapper timestamps.",
        "entries": entries,
    }
    write_json(out_dir / "backend_event_latency_trace.json", trace)
    write_backend_latency_report(out_dir / "backend_event_latency_report.md", trace)
    return trace


def write_backend_latency_report(path: Path, data: dict[str, Any]) -> None:
    lines = [
        "# V15 Backend Event Latency Comparison",
        "",
        "RESULT: BENCHMARK_RECORDED",
        "",
        "App Server timestamps are live client observation times; JSONL timestamps are session-log event times. Small clock-path differences are expected.",
        "",
        "| role_id | call_id | spawn lag ms | link lag ms | child observed ms | result observed ms |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for entry in data.get("entries", []):
        lines.append(
            "| {role_id} | {call_id} | {spawn} | {link} | {child} | {result} |".format(
                role_id=entry.get("role_id"),
                call_id=entry.get("call_id"),
                spawn=fmt_ms(entry.get("spawn_app_minus_jsonl_ms")),
                link=fmt_ms(entry.get("link_app_minus_jsonl_ms")),
                child=fmt_ms(entry.get("child_thread_observed_ts_ms")),
                result=fmt_ms(entry.get("task_result_observed_ts_ms")),
            )
        )
    write_text(path, "\n".join(lines) + "\n")


def copy_expected_workflow(src: Path, dst_dir: Path) -> Path:
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / "expected_workflow.json"
    shutil.copy2(src, dst)
    return dst


def capture_overhead(out_dir: Path) -> dict[str, Any]:
    expected_src = V13_DIR / "jsonl_authoritative_run" / "expected_workflow.json"
    timings: dict[str, Any] = {}

    jsonl_capture_dir = out_dir / "overhead_jsonl_capture"
    expected_copy = copy_expected_workflow(expected_src, jsonl_capture_dir)
    timings["codex_native_subagent_adapter"] = run_command_timed(
        [
            sys.executable,
            str(TOOLS_DIR / "codex_native_subagent_adapter.py"),
            "--run-dir",
            str(jsonl_capture_dir),
            "--expected-workflow",
            str(expected_copy),
            "--capture-results",
        ],
        timeout=240,
    )

    aggregation_dir = out_dir / "overhead_jsonl_aggregation"
    timings["aggregate_subagent_results"] = run_command_timed(
        [
            sys.executable,
            str(TOOLS_DIR / "aggregate_subagent_results.py"),
            "--run-dir",
            str(jsonl_capture_dir),
            "--strict",
            "--out-dir",
            str(aggregation_dir),
        ],
        timeout=180,
    )

    shadow_dir = out_dir / "overhead_app_server_shadow_replay"
    timings["codex_app_server_shadow_adapter_offline"] = run_command_timed(
        [
            sys.executable,
            str(TOOLS_DIR / "codex_app_server_shadow_adapter.py"),
            "--app-server-events",
            str(V13_DIR / "app_server_events.jsonl"),
            "--jsonl-trace",
            str(V13_DIR / "jsonl_authoritative_run" / "spawn_capture_trace.json"),
            "--expected-workflow",
            str(expected_src),
            "--out-dir",
            str(shadow_dir),
        ],
        timeout=180,
    )

    timings["runner_status_v13"] = run_command_timed(
        [
            sys.executable,
            str(TOOLS_DIR / "run_subagent_workflow.py"),
            "--mode",
            "codex-native-subagent",
            "--status",
            "--run-dir",
            str(V13_DIR / "jsonl_authoritative_run"),
        ],
        timeout=120,
    )

    trace = {
        "benchmark": "capture overhead",
        "runs": timings,
        "jsonl_capture_trace": str(jsonl_capture_dir / "spawn_capture_trace.json"),
        "jsonl_aggregation_trace": str(aggregation_dir / "aggregation_trace.json"),
        "shadow_replay_trace": str(shadow_dir / "shadow_equivalence_trace.json"),
    }
    write_json(out_dir / "capture_overhead_trace.json", trace)
    write_capture_overhead_report(out_dir / "capture_overhead_report.md", trace)
    return trace


def write_capture_overhead_report(path: Path, data: dict[str, Any]) -> None:
    lines = [
        "# V15 Capture Overhead Benchmark",
        "",
        "RESULT: BENCHMARK_RECORDED",
        "",
        "| command | exit_code | elapsed_ms |",
        "| --- | ---: | ---: |",
    ]
    for name, result in (data.get("runs") or {}).items():
        lines.append(f"| {name} | {result.get('exit_code')} | {fmt_ms(result.get('elapsed_ms'))} |")
    lines.extend(
        [
            "",
            "JSONL remains the authoritative capture path. App Server measurements are shadow/candidate-only.",
        ]
    )
    write_text(path, "\n".join(lines) + "\n")


def read_classification(path: Path) -> str | None:
    if not path.exists():
        return None
    data = read_json(path, {})
    for key in ("classification", "overall_classification", "overall_result", "status", "result"):
        value = data.get(key)
        if isinstance(value, str):
            return value
    return None


def regression_checks(out_dir: Path) -> dict[str, Any]:
    baseline_hashes = {
        "v2_5_spawn_capture_trace": sha256_file(V25_SOURCE_DIR / "spawn_capture_trace.json"),
        "v13_jsonl_spawn_capture_trace": sha256_file(V13_DIR / "jsonl_authoritative_run" / "spawn_capture_trace.json"),
    }

    checks: dict[str, Any] = {
        "baseline_hashes_before": baseline_hashes,
        "commands": {},
        "file_checks": {},
    }
    checks["commands"]["v2_5_status"] = run_command_timed(
        [
            sys.executable,
            str(TOOLS_DIR / "run_subagent_workflow.py"),
            "--mode",
            "codex-native-subagent",
            "--status",
            "--run-dir",
            str(V25_SOURCE_DIR),
            "--aggregation-dir",
            str(REPORTS_ROOT / "v2_5_strict_aggregation_probe"),
        ],
        timeout=120,
    )
    checks["commands"]["v8_offline_replay"] = run_command_timed(
        [
            sys.executable,
            str(TOOLS_DIR / "codex_app_server_shadow_adapter.py"),
            "--app-server-events",
            str(REPORTS_ROOT / "v7_app_server_live_equivalence_probe" / "app_server_events.jsonl"),
            "--jsonl-trace",
            str(REPORTS_ROOT / "v7_app_server_live_equivalence_probe" / "jsonl_parallel_trace.json"),
            "--expected-workflow",
            str(REPORTS_ROOT / "v7_app_server_live_equivalence_probe" / "expected_workflow.json"),
            "--out-dir",
            str(out_dir / "v8_replay_regression"),
        ],
        timeout=180,
    )
    checks["commands"]["v12_status"] = run_command_timed(
        [
            sys.executable,
            str(TOOLS_DIR / "run_subagent_workflow.py"),
            "--mode",
            "codex-native-subagent",
            "--status",
            "--run-dir",
            str(V12_DIR / "jsonl_backend_regression"),
        ],
        timeout=120,
    )
    checks["commands"]["v14_policy_validation"] = run_command_timed(
        [
            sys.executable,
            str(TOOLS_DIR / "run_subagent_workflow.py"),
            "--mode",
            "codex-native-subagent",
            "--validate-backend-policy",
            "--run-dir",
            str(V14_DIR),
        ],
        timeout=120,
    )

    v8_trace = out_dir / "v8_replay_regression" / "shadow_equivalence_trace.json"
    checks["file_checks"]["v8_replay_classification"] = read_classification(v8_trace)
    checks["file_checks"]["v11_matrix_exists"] = V11_MATRIX.exists()
    v11 = read_json(V11_MATRIX, {}) if V11_MATRIX.exists() else {}
    checks["file_checks"]["v11_overall_status"] = (
        v11.get("overall_result") or v11.get("overall_status") or v11.get("result") or v11.get("classification")
    )
    v13 = read_json(V13_DIR / "v13_parity_matrix.json", {}) if (V13_DIR / "v13_parity_matrix.json").exists() else {}
    checks["file_checks"]["v13_classification"] = (
        v13.get("overall_result")
        or v13.get("classification")
        or v13.get("overall_status")
        or v13.get("result")
        or (v13.get("pattern") or {}).get("classification")
    )
    checks["file_checks"]["v14_negative_checks_exists"] = (V14_DIR / "negative_policy_checks.json").exists()
    checks["baseline_hashes_after"] = {
        "v2_5_spawn_capture_trace": sha256_file(V25_SOURCE_DIR / "spawn_capture_trace.json"),
        "v13_jsonl_spawn_capture_trace": sha256_file(V13_DIR / "jsonl_authoritative_run" / "spawn_capture_trace.json"),
    }

    checks["regression_pass"] = (
        checks["commands"]["v2_5_status"]["exit_code"] == 0
        and "AGGREGATION_PASS" in (
            checks["commands"]["v2_5_status"].get("stdout_tail", "")
            + checks["commands"]["v2_5_status"].get("stderr_tail", "")
        )
        and checks["commands"]["v8_offline_replay"]["exit_code"] == 0
        and checks["file_checks"].get("v8_replay_classification") == "SHADOW_EQUIVALENT"
        and checks["file_checks"].get("v11_matrix_exists") is True
        and str(checks["file_checks"].get("v13_classification", "")).upper() in {"V13_PASS", "CANDIDATE_EQUIVALENT"}
        and checks["commands"]["v12_status"]["exit_code"] == 0
        and (
            "authoritative_backend=jsonl" in (
                checks["commands"]["v12_status"].get("stdout_tail", "")
                + checks["commands"]["v12_status"].get("stderr_tail", "")
            )
            or "authoritative_backend: jsonl" in (
                checks["commands"]["v12_status"].get("stdout_tail", "")
                + checks["commands"]["v12_status"].get("stderr_tail", "")
            )
        )
        and checks["commands"]["v14_policy_validation"]["exit_code"] == 0
        and checks["baseline_hashes_before"] == checks["baseline_hashes_after"]
    )
    return checks


def acceptance_report(
    path: Path,
    fanout: dict[str, Any],
    thread_limit: dict[str, Any],
    latency: dict[str, Any],
    overhead: dict[str, Any],
    regressions: dict[str, Any],
) -> None:
    produced = [
        "fanout_timing_trace.json",
        "fanout_timing_report.md",
        "runtime_thread_limit_trace.json",
        "runtime_thread_limit_report.md",
        "backend_event_latency_trace.json",
        "backend_event_latency_report.md",
        "capture_overhead_trace.json",
        "capture_overhead_report.md",
    ]
    all_reports_exist = all((path.parent / name).exists() for name in produced)
    result = "V15_BENCHMARK_COMPLETE" if all_reports_exist else "V15_BENCHMARK_INCOMPLETE"
    lines = [
        "# V15 Acceptance Report",
        "",
        f"RESULT: {result}",
        "",
        "V15 is benchmark evidence, not a new correctness gate. JSONL remains default and authoritative; App Server remains candidate/shadow-only.",
        "",
        "## Produced Artifacts",
        "",
    ]
    for name in produced:
        lines.append(f"- {name}: {'present' if (path.parent / name).exists() else 'missing'}")
    lines.extend(
        [
            "",
            "## Benchmark Summary",
            "",
            f"- Fan-out scheduling classification: {fanout.get('scheduling_classification')}",
            f"- Producer overlap ms: {fmt_ms(fanout.get('producer_overlap_ms'))}",
            f"- Wall-clock parallel evidence: {fanout.get('wall_clock_parallel')}",
            f"- Thread-limit widths attempted: {', '.join(str(w) for w in thread_limit.get('widths', []))}",
            f"- Backend latency entries: {len(latency.get('entries', []))}",
            f"- Overhead commands measured: {len(overhead.get('runs', {}))}",
            "",
            "## Regression Checks",
            "",
            f"- Regression pass: {regressions.get('regression_pass')}",
            f"- V8 replay classification: {regressions.get('file_checks', {}).get('v8_replay_classification')}",
            f"- V11 matrix exists: {regressions.get('file_checks', {}).get('v11_matrix_exists')}",
            f"- V13 classification: {regressions.get('file_checks', {}).get('v13_classification')}",
            f"- JSONL baseline hashes unchanged: {regressions.get('baseline_hashes_before') == regressions.get('baseline_hashes_after')}",
            "",
            "## Boundary Statements",
            "",
            "- Logical fan-out/fan-in correctness: inherited from V13 accepted parity; V15 records timing only.",
            "- Wall-clock parallelism: claimed only when measured producer active windows overlap.",
            "- Runtime scheduling: recorded from spawn, child start, first-token, and task completion timestamps.",
            "- Runtime thread-limit behavior: recorded as probe data; strict expected_call_ids still protects in-scope acceptance.",
            "- App Server: candidate/shadow only. No replacement or authoritative claim is made.",
        ]
    )
    write_text(path, "\n".join(lines) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run V15 parallelism and runtime-limit benchmark.")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--width", action="append", type=int, default=None, help="Thread-limit fan-out width to probe.")
    parser.add_argument("--live-timeout-seconds", type=int, default=300)
    parser.add_argument("--skip-live", action="store_true", help="Skip live App Server thread-limit probes.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    fanout = measure_fanout_timing(out_dir)
    if args.skip_live:
        thread_limit = {
            "benchmark": "runtime thread-limit probe",
            "widths": args.width or [2, 3, 4],
            "results": [],
            "skipped": True,
            "reason": "--skip-live",
        }
        write_json(out_dir / "runtime_thread_limit_trace.json", thread_limit)
        write_thread_limit_report(out_dir / "runtime_thread_limit_report.md", thread_limit)
    else:
        thread_limit = runtime_thread_limit_probe(out_dir, args.width or [2, 3, 4], args.live_timeout_seconds)
    latency = backend_event_latency(out_dir, fanout)
    overhead = capture_overhead(out_dir)
    regressions = regression_checks(out_dir)
    write_json(out_dir / "v15_regression_trace.json", regressions)
    acceptance_report(out_dir / "v15_acceptance_report.md", fanout, thread_limit, latency, overhead, regressions)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
