#!/usr/bin/env python3
"""Messages-only Qwen3 direct runner skeleton for evidence diagnosis prompts.

This tool performs local and remote preflight checks for the project-owned
Qwen3 direct deployment. Dry-run mode is intentionally non-generative: it does
not copy prompts to the server, does not load model weights, and does not call
any model endpoint.
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
MANUAL_EXPERIMENTS = (REPO_ROOT / "manual_experiments").resolve()

DEFAULT_PACK_DIR = Path("manual_experiments/evidence_diag_20_20260525_v2")
DEFAULT_OUTPUT_DIR = Path("manual_experiments/evidence_qwen3_direct_diag20_20260527")
DEFAULT_FULL20_OUTPUT_DIR = Path("manual_experiments/evidence_qwen3_direct_diag20_20260527_full20")
DEFAULT_DIAG50_OUTPUT_DIR = Path("manual_experiments/evidence_qwen3_direct_diag50_20260527")
DEFAULT_LINKFLAP_V3_OUTPUT_DIR = Path("manual_experiments/evidence_qwen3_direct_linkflap_v3_20260528")
DEFAULT_LINKFLAP_V4_OUTPUT_DIR = Path("manual_experiments/evidence_qwen3_direct_linkflap_v4_20260528")
DEFAULT_LINKFLAP_P1_OUTPUT_DIR = Path("manual_experiments/evidence_qwen3_direct_linkflap_p1_20260529")
DEFAULT_LINKFLAP_P1_1_OUTPUT_DIR = Path("manual_experiments/evidence_qwen3_direct_linkflap_p1_1_20260530")
DEFAULT_LINKFLAP_P1_2_OUTPUT_DIR = Path("manual_experiments/evidence_qwen3_direct_linkflap_p1_2_20260530")
DEFAULT_DIAG50_LINKFLAP_V3_OUTPUT_DIR = Path(
    "manual_experiments/evidence_qwen3_direct_diag50_linkflap_v3_20260528"
)
DEFAULT_DIAG50_LINKFLAP_P1_1_OUTPUT_DIR = Path(
    "manual_experiments/evidence_qwen3_direct_diag50_linkflap_p1_1_20260530"
)
DEFAULT_DIAG50_LINKFLAP_P1_2_OUTPUT_DIR = Path(
    "manual_experiments/evidence_qwen3_direct_diag50_linkflap_p1_2_20260530"
)
DEFAULT_REMOTE_HOST = "qwen3-server"
DEFAULT_REMOTE_WORK_DIR = "/home/xrh/qwen3_os_fault"
DEFAULT_REMOTE_WRAPPER = "/home/xrh/qwen3_os_fault/evidence_messages_infer.py"
DEFAULT_REMOTE_BASE_MODEL = "/home/xrh/models/Qwen/Qwen3-8B"
DEFAULT_REMOTE_ADAPTER = "/home/xrh/qwen3_os_fault/qwen3_8b_fault_qlora"
DEFAULT_REMOTE_TMP_DIR = "/home/xrh/qwen3_os_fault/tmp/evidence_direct_adapter"
DEFAULT_REMOTE_PYTHON = "python"
DEFAULT_TIMEOUT_SECONDS = 1800

ALLOWED_MESSAGE_ROLES = {"system", "user"}
REMOTE_SCOPE = "/home/xrh"
ALLOWED_OUTPUT_DIR = (REPO_ROOT / DEFAULT_OUTPUT_DIR).resolve()
ALLOWED_FULL20_OUTPUT_DIR = (REPO_ROOT / DEFAULT_FULL20_OUTPUT_DIR).resolve()
ALLOWED_DIAG50_OUTPUT_DIR = (REPO_ROOT / DEFAULT_DIAG50_OUTPUT_DIR).resolve()
ALLOWED_LINKFLAP_V3_OUTPUT_DIR = (REPO_ROOT / DEFAULT_LINKFLAP_V3_OUTPUT_DIR).resolve()
ALLOWED_LINKFLAP_V4_OUTPUT_DIR = (REPO_ROOT / DEFAULT_LINKFLAP_V4_OUTPUT_DIR).resolve()
ALLOWED_LINKFLAP_P1_OUTPUT_DIR = (REPO_ROOT / DEFAULT_LINKFLAP_P1_OUTPUT_DIR).resolve()
ALLOWED_LINKFLAP_P1_1_OUTPUT_DIR = (REPO_ROOT / DEFAULT_LINKFLAP_P1_1_OUTPUT_DIR).resolve()
ALLOWED_LINKFLAP_P1_2_OUTPUT_DIR = (REPO_ROOT / DEFAULT_LINKFLAP_P1_2_OUTPUT_DIR).resolve()
ALLOWED_DIAG50_LINKFLAP_V3_OUTPUT_DIR = (REPO_ROOT / DEFAULT_DIAG50_LINKFLAP_V3_OUTPUT_DIR).resolve()
ALLOWED_DIAG50_LINKFLAP_P1_1_OUTPUT_DIR = (REPO_ROOT / DEFAULT_DIAG50_LINKFLAP_P1_1_OUTPUT_DIR).resolve()
ALLOWED_DIAG50_LINKFLAP_P1_2_OUTPUT_DIR = (REPO_ROOT / DEFAULT_DIAG50_LINKFLAP_P1_2_OUTPUT_DIR).resolve()
ALLOWED_OUTPUT_DIRS = {
    ALLOWED_OUTPUT_DIR,
    ALLOWED_FULL20_OUTPUT_DIR,
    ALLOWED_DIAG50_OUTPUT_DIR,
    ALLOWED_LINKFLAP_V3_OUTPUT_DIR,
    ALLOWED_LINKFLAP_V4_OUTPUT_DIR,
    ALLOWED_LINKFLAP_P1_OUTPUT_DIR,
    ALLOWED_LINKFLAP_P1_1_OUTPUT_DIR,
    ALLOWED_LINKFLAP_P1_2_OUTPUT_DIR,
    ALLOWED_DIAG50_LINKFLAP_V3_OUTPUT_DIR,
    ALLOWED_DIAG50_LINKFLAP_P1_1_OUTPUT_DIR,
    ALLOWED_DIAG50_LINKFLAP_P1_2_OUTPUT_DIR,
}
ALLOWED_PROMPTS_JSONL = (REPO_ROOT / DEFAULT_PACK_DIR / "prompts.jsonl").resolve()
ALLOWED_DIAG50_PROMPTS_JSONL = (ALLOWED_DIAG50_OUTPUT_DIR / "prompts.jsonl").resolve()
ALLOWED_LINKFLAP_V3_PROMPTS_JSONL = (ALLOWED_LINKFLAP_V3_OUTPUT_DIR / "prompts.jsonl").resolve()
ALLOWED_LINKFLAP_V4_PROMPTS_JSONL = (ALLOWED_LINKFLAP_V4_OUTPUT_DIR / "prompts.jsonl").resolve()
ALLOWED_LINKFLAP_P1_PROMPTS_JSONL = (ALLOWED_LINKFLAP_P1_OUTPUT_DIR / "prompts.jsonl").resolve()
ALLOWED_LINKFLAP_P1_1_PROMPTS_JSONL = (ALLOWED_LINKFLAP_P1_1_OUTPUT_DIR / "prompts.jsonl").resolve()
ALLOWED_LINKFLAP_P1_2_PROMPTS_JSONL = (ALLOWED_LINKFLAP_P1_2_OUTPUT_DIR / "prompts.jsonl").resolve()
ALLOWED_DIAG50_LINKFLAP_V3_PROMPTS_JSONL = (ALLOWED_DIAG50_LINKFLAP_V3_OUTPUT_DIR / "prompts.jsonl").resolve()
ALLOWED_DIAG50_LINKFLAP_P1_1_PROMPTS_JSONL = (
    ALLOWED_DIAG50_LINKFLAP_P1_1_OUTPUT_DIR / "prompts.jsonl"
).resolve()
ALLOWED_DIAG50_LINKFLAP_P1_2_PROMPTS_JSONL = (
    ALLOWED_DIAG50_LINKFLAP_P1_2_OUTPUT_DIR / "prompts.jsonl"
).resolve()
ALLOWED_PROMPT_KEYS = {
    "blockers",
    "case_ref",
    "chain_confidence",
    "chain_score",
    "expected_behavior",
    "expected_output_schema",
    "experiment_id",
    "messages",
    "prompt_id",
    "prompt_steering_version",
    "readiness",
    "review_warnings",
    "sample_group",
    "sample_rank",
    "selection_config",
    "selection_reason",
    "source_chain_summary",
    "static_validation",
}
FORBIDDEN_PROMPT_KEYS = {
    "answer",
    "answers",
    "assistant",
    "fault_subtype",
    "ground_truth",
    "gt",
    "gt_label",
    "label",
    "labels",
    "raw_case_id",
    "root_cause_label",
    "scenario",
    "scenario_tag",
    "subtype",
    "test_label",
}


class RunnerError(RuntimeError):
    """Structured runner failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def resolve_local(path: Path | str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = REPO_ROOT / p
    return p.resolve()


def repo_rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


def is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def require_under(path: Path, root: Path, code: str) -> None:
    if not is_under(path, root):
        raise RunnerError(code, f"{path} is outside {root}")


def require_remote_scope(path: str, name: str) -> None:
    if not path.startswith("/"):
        raise RunnerError("REMOTE_PATH_OUT_OF_SCOPE", f"{name} must be an absolute path under {REMOTE_SCOPE}: {path}")
    normalized = posixpath.normpath(path)
    if normalized == REMOTE_SCOPE or normalized.startswith(REMOTE_SCOPE + "/"):
        return
    raise RunnerError("REMOTE_PATH_OUT_OF_SCOPE", f"{name} must stay under {REMOTE_SCOPE}: {path}")


def require_safe_remote_python(value: str) -> None:
    if not value:
        raise RunnerError("REMOTE_PYTHON_EMPTY", "--remote-python must not be empty")
    unsafe_chars = {";", "&", "|", "`", "$", "<", ">", "\\", "'", '"', " ", "\t", "\n", "\r"}
    if any(char in value for char in unsafe_chars):
        raise RunnerError("REMOTE_PYTHON_UNSAFE", "--remote-python contains unsupported shell characters")
    if "/" in value:
        require_remote_scope(value, "remote_python")
        return
    if value not in {"python", "python3"}:
        raise RunnerError(
            "REMOTE_PYTHON_UNSAFE",
            "--remote-python must be python, python3, or an absolute executable under /home/xrh",
        )


def prompts_jsonl_allowed_for_output(output_dir: Path, prompts_jsonl: Path) -> bool:
    if output_dir == ALLOWED_DIAG50_OUTPUT_DIR:
        return prompts_jsonl == ALLOWED_DIAG50_PROMPTS_JSONL
    if output_dir == ALLOWED_LINKFLAP_V3_OUTPUT_DIR:
        return prompts_jsonl == ALLOWED_LINKFLAP_V3_PROMPTS_JSONL
    if output_dir == ALLOWED_LINKFLAP_V4_OUTPUT_DIR:
        return prompts_jsonl == ALLOWED_LINKFLAP_V4_PROMPTS_JSONL
    if output_dir == ALLOWED_LINKFLAP_P1_OUTPUT_DIR:
        return prompts_jsonl == ALLOWED_LINKFLAP_P1_PROMPTS_JSONL
    if output_dir == ALLOWED_LINKFLAP_P1_1_OUTPUT_DIR:
        return prompts_jsonl == ALLOWED_LINKFLAP_P1_1_PROMPTS_JSONL
    if output_dir == ALLOWED_LINKFLAP_P1_2_OUTPUT_DIR:
        return prompts_jsonl == ALLOWED_LINKFLAP_P1_2_PROMPTS_JSONL
    if output_dir == ALLOWED_DIAG50_LINKFLAP_V3_OUTPUT_DIR:
        return prompts_jsonl == ALLOWED_DIAG50_LINKFLAP_V3_PROMPTS_JSONL
    if output_dir == ALLOWED_DIAG50_LINKFLAP_P1_1_OUTPUT_DIR:
        return prompts_jsonl == ALLOWED_DIAG50_LINKFLAP_P1_1_PROMPTS_JSONL
    if output_dir == ALLOWED_DIAG50_LINKFLAP_P1_2_OUTPUT_DIR:
        return prompts_jsonl == ALLOWED_DIAG50_LINKFLAP_P1_2_PROMPTS_JSONL
    return prompts_jsonl == ALLOWED_PROMPTS_JSONL


def prompts_allowlist_message(output_dir: Path) -> str:
    if output_dir == ALLOWED_DIAG50_OUTPUT_DIR:
        return str(ALLOWED_DIAG50_PROMPTS_JSONL)
    if output_dir == ALLOWED_LINKFLAP_V3_OUTPUT_DIR:
        return str(ALLOWED_LINKFLAP_V3_PROMPTS_JSONL)
    if output_dir == ALLOWED_LINKFLAP_V4_OUTPUT_DIR:
        return str(ALLOWED_LINKFLAP_V4_PROMPTS_JSONL)
    if output_dir == ALLOWED_LINKFLAP_P1_OUTPUT_DIR:
        return str(ALLOWED_LINKFLAP_P1_PROMPTS_JSONL)
    if output_dir == ALLOWED_LINKFLAP_P1_1_OUTPUT_DIR:
        return str(ALLOWED_LINKFLAP_P1_1_PROMPTS_JSONL)
    if output_dir == ALLOWED_LINKFLAP_P1_2_OUTPUT_DIR:
        return str(ALLOWED_LINKFLAP_P1_2_PROMPTS_JSONL)
    if output_dir == ALLOWED_DIAG50_LINKFLAP_V3_OUTPUT_DIR:
        return str(ALLOWED_DIAG50_LINKFLAP_V3_PROMPTS_JSONL)
    if output_dir == ALLOWED_DIAG50_LINKFLAP_P1_1_OUTPUT_DIR:
        return str(ALLOWED_DIAG50_LINKFLAP_P1_1_PROMPTS_JSONL)
    if output_dir == ALLOWED_DIAG50_LINKFLAP_P1_2_OUTPUT_DIR:
        return str(ALLOWED_DIAG50_LINKFLAP_P1_2_PROMPTS_JSONL)
    return str(ALLOWED_PROMPTS_JSONL)


def posix_quote(value: str) -> str:
    return shlex.quote(value)


def run_cmd(args: List[str], timeout: int = 60, input_text: Optional[str] = None) -> Dict[str, Any]:
    try:
        proc = subprocess.run(
            args,
            input=input_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return {
            "args": args,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "args": args,
            "returncode": None,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or f"timeout after {timeout}s",
            "timed_out": True,
        }


def run_ssh(host: str, remote_command: str, timeout: int = 60) -> Dict[str, Any]:
    return run_cmd(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            host,
            remote_command,
        ],
        timeout=timeout,
    )


def remote_path_exists(host: str, path: str, kind: str) -> Tuple[bool, Dict[str, Any]]:
    test_flag = "-e"
    if kind == "file":
        test_flag = "-f"
    elif kind == "dir":
        test_flag = "-d"
    result = run_ssh(host, f"test {test_flag} {posix_quote(path)}", timeout=30)
    return result["returncode"] == 0, result


def remote_executable_exists(host: str, path: str) -> Tuple[bool, Dict[str, Any]]:
    result = run_ssh(host, f"test -x {posix_quote(path)}", timeout=30)
    return result["returncode"] == 0, result


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise RunnerError("PROMPT_JSONL_PARSE_ERROR", f"{path}:{line_number}: {exc}") from exc
            if not isinstance(obj, dict):
                raise RunnerError("PROMPT_RECORD_NOT_OBJECT", f"{path}:{line_number}: record is not an object")
            validate_prompt_record_keys(obj, line_number)
            records.append(obj)
    return records


def validate_prompt_record_keys(record: Dict[str, Any], line_number: int) -> None:
    for key in record:
        lowered = key.lower()
        if lowered in FORBIDDEN_PROMPT_KEYS:
            raise RunnerError("FORBIDDEN_PROMPT_KEY", f"line {line_number} contains forbidden key {key!r}")
        if lowered not in ALLOWED_PROMPT_KEYS:
            raise RunnerError("UNEXPECTED_PROMPT_KEY", f"line {line_number} contains unexpected key {key!r}")


def extract_messages_only(record: Dict[str, Any], index: int) -> Dict[str, Any]:
    prompt_id = record.get("prompt_id")
    case_ref = record.get("case_ref")
    messages = record.get("messages")

    if not isinstance(prompt_id, str) or not prompt_id:
        raise RunnerError("PROMPT_ID_MISSING", f"selected record {index} is missing prompt_id")
    if not isinstance(case_ref, str) or not case_ref:
        raise RunnerError("CASE_REF_MISSING", f"selected record {index} is missing case_ref")
    if not isinstance(messages, list) or not messages:
        raise RunnerError("MESSAGES_MISSING", f"selected record {index} is missing messages")

    clean_messages: List[Dict[str, str]] = []
    for message_index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise RunnerError("MESSAGE_NOT_OBJECT", f"{prompt_id} message {message_index} is not an object")
        role = message.get("role")
        content = message.get("content")
        if role not in ALLOWED_MESSAGE_ROLES:
            raise RunnerError(
                "UNSAFE_MESSAGE_ROLE",
                f"{prompt_id} message {message_index} has unsupported role {role!r}",
            )
        if not isinstance(content, str) or not content.strip():
            raise RunnerError("MESSAGE_CONTENT_MISSING", f"{prompt_id} message {message_index} has empty content")
        clean_messages.append({"role": role, "content": content})

    return {"prompt_id": prompt_id, "case_ref": case_ref, "messages": clean_messages}


def select_messages_only(records: List[Dict[str, Any]], start_index: int, limit: int) -> List[Dict[str, Any]]:
    if start_index < 0:
        raise RunnerError("START_INDEX_INVALID", "--start-index must be >= 0")
    if limit < 1:
        raise RunnerError("LIMIT_INVALID", "--limit must be >= 1")
    selected = records[start_index : start_index + limit]
    return [extract_messages_only(record, start_index + offset) for offset, record in enumerate(selected)]


def extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    text = text.strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    first = text.find("{")
    last = text.rfind("}")
    if first == -1 or last == -1 or last < first:
        return None
    try:
        obj = json.loads(text[first : last + 1])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def hard_prompt_safety_zero(report: Dict[str, Any]) -> bool:
    evidence = report.get("evidence_reference", {}) if isinstance(report.get("evidence_reference"), dict) else {}
    safety = report.get("safety", {}) if isinstance(report.get("safety"), dict) else {}
    schema = report.get("schema", {}) if isinstance(report.get("schema"), dict) else {}
    hard_values = [
        evidence.get("unknown_evidence_id", 0),
        evidence.get("do_not_use_evidence_cited", 0),
        evidence.get("forbidden_role_used", 0),
        safety.get("sensitive_text_violations", 0),
        safety.get("injector_or_label_leakage", 0),
        safety.get("coverage_gap_false_root", 0),
        schema.get("parse_errors", 0),
        schema.get("invalid", 0),
    ]
    return all(value == 0 for value in hard_values)


def run_prompt_only_validator(prompts_jsonl: Path) -> Tuple[bool, Optional[Dict[str, Any]], Dict[str, Any]]:
    validator = REPO_ROOT / "tools" / "evidence_response_validator.py"
    cmd = [
        sys.executable,
        "-B",
        str(validator),
        "--prompts-jsonl",
        str(prompts_jsonl),
        "--dry-run",
        "--stdout-only",
        "--format",
        "json",
        "--max-examples",
        "10",
    ]
    result = run_cmd(cmd, timeout=60)
    parsed = extract_json_object(result.get("stdout", ""))
    ok = result["returncode"] == 0 and parsed is not None and hard_prompt_safety_zero(parsed)
    return ok, parsed, result


def write_jsonl(path: Path, records: Iterable[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def read_any_jsonl(path: Path) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    if not path.exists():
        return records
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise RunnerError("JSONL_PARSE_ERROR", f"{path}:{line_number}: {exc}") from exc
            if not isinstance(obj, dict):
                raise RunnerError("JSONL_RECORD_NOT_OBJECT", f"{path}:{line_number}: record is not an object")
            records.append(obj)
    return records


def jsonl_count(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


def hard_safety_counts(report: Optional[Dict[str, Any]]) -> Dict[str, int]:
    if not isinstance(report, dict):
        return {
            "unknown_evidence_id": 0,
            "do_not_use_evidence_cited": 0,
            "forbidden_role_used": 0,
            "sensitive_text_violations": 0,
            "injector_or_label_leakage": 0,
            "coverage_gap_false_root": 0,
        }
    evidence = report.get("evidence_reference", {}) if isinstance(report.get("evidence_reference"), dict) else {}
    safety = report.get("safety", {}) if isinstance(report.get("safety"), dict) else {}
    return {
        "unknown_evidence_id": int(evidence.get("unknown_evidence_id", 0) or 0),
        "do_not_use_evidence_cited": int(evidence.get("do_not_use_evidence_cited", 0) or 0),
        "forbidden_role_used": int(evidence.get("forbidden_role_used", 0) or 0),
        "sensitive_text_violations": int(safety.get("sensitive_text_violations", 0) or 0),
        "injector_or_label_leakage": int(safety.get("injector_or_label_leakage", 0) or 0),
        "coverage_gap_false_root": int(safety.get("coverage_gap_false_root", 0) or 0),
    }


def hard_safety_zero(report: Optional[Dict[str, Any]]) -> bool:
    return all(value == 0 for value in hard_safety_counts(report).values())


def validator_status_counts(report: Optional[Dict[str, Any]]) -> Dict[str, int]:
    counts = report.get("counts", {}) if isinstance(report, dict) and isinstance(report.get("counts"), dict) else {}
    return {
        "pass": int(counts.get("pass", 0) or 0),
        "warn": int(counts.get("warn", 0) or 0),
        "fail": int(counts.get("fail", 0) or 0),
    }


def main_categories(report: Optional[Dict[str, Any]]) -> Dict[str, int]:
    if not isinstance(report, dict):
        return {}
    merged: Dict[str, int] = {}
    for section_name in ("warnings", "violations"):
        section = report.get(section_name)
        if isinstance(section, dict):
            for key, value in section.items():
                try:
                    count = int(value)
                except (TypeError, ValueError):
                    continue
                if count:
                    merged[str(key)] = merged.get(str(key), 0) + count
    schema = report.get("schema") if isinstance(report.get("schema"), dict) else {}
    parse_errors = int(schema.get("parse_errors", 0) or 0)
    if parse_errors and "response_parse_error" not in merged:
        merged["response_parse_error"] = parse_errors
    return dict(sorted(merged.items(), key=lambda item: (-item[1], item[0])))


def run_response_validator(prompts_jsonl: Path, responses_jsonl: Path, output_dir: Path) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    validator = REPO_ROOT / "tools" / "evidence_response_validator.py"
    base_cmd = [
        sys.executable,
        "-B",
        str(validator),
        "--prompts-jsonl",
        str(prompts_jsonl),
        "--responses-jsonl",
        str(responses_jsonl),
        "--dry-run",
        "--stdout-only",
        "--max-examples",
        "10",
    ]
    json_cmd = base_cmd + ["--format", "json"]
    markdown_cmd = base_cmd + ["--format", "markdown"]
    json_result = run_cmd(json_cmd, timeout=120)
    markdown_result = run_cmd(markdown_cmd, timeout=120)
    parsed = extract_json_object(json_result.get("stdout", ""))
    if parsed is None:
        parsed = {
            "status": "FAIL",
            "counts": {"pass": 0, "warn": 0, "fail": 1},
            "schema": {"valid": 0, "invalid": 1, "parse_errors": 1},
            "evidence_reference": {},
            "safety": {},
            "warnings": {},
            "violations": {"validator_output_parse_error": 1},
        }
    write_json(output_dir / "validator_report.json", parsed)
    markdown_text = markdown_result.get("stdout", "")
    if not markdown_text.strip():
        markdown_text = "# Evidence Response Validator Report\n\nValidator markdown output was empty.\n"
    (output_dir / "validator_report.md").write_text(markdown_text, encoding="utf-8")
    return parsed, json_result, markdown_result


def run_scp_to_remote(host: str, local_path: Path, remote_path: str, timeout: int) -> Dict[str, Any]:
    return run_cmd(["scp", str(local_path), f"{host}:{remote_path}"], timeout=timeout)


def run_scp_from_remote(host: str, remote_path: str, local_path: Path, timeout: int) -> Dict[str, Any]:
    local_path.parent.mkdir(parents=True, exist_ok=True)
    return run_cmd(["scp", f"{host}:{remote_path}", str(local_path)], timeout=timeout)


def command_tail(result: Dict[str, Any], limit: int = 4000) -> Dict[str, Any]:
    stdout = str(result.get("stdout") or "")
    stderr = str(result.get("stderr") or "")
    return {
        "returncode": result.get("returncode"),
        "timed_out": bool(result.get("timed_out")),
        "stdout_tail": stdout[-limit:],
        "stderr_tail": stderr[-limit:],
    }


def write_one_case_summary(
    path: Path,
    run_report: Dict[str, Any],
    validator_report: Optional[Dict[str, Any]],
    raw_records: List[Dict[str, Any]],
) -> None:
    counts = validator_status_counts(validator_report)
    hard_counts = hard_safety_counts(validator_report)
    categories = main_categories(validator_report)
    parse_success = sum(1 for record in raw_records if record.get("parse_ok") is True)
    status = run_report.get("status", "BLOCKED")
    if status == "PASS" and validator_report:
        validator_status = validator_report.get("status", "unknown")
        if validator_status == "FAIL" or not hard_safety_zero(validator_report):
            recommendation = "stop due safety failure or validator failure"
        elif validator_status == "PASS":
            recommendation = "run official Qwen3 direct 20-case"
        else:
            recommendation = "run official Qwen3 direct 20-case after reviewing warnings"
    elif status == "BLOCKED":
        recommendation = "fix runtime"
    else:
        recommendation = "fix wrapper output parsing"

    lines = [
        "# Qwen3 Direct One-case Evidence Diagnosis Smoke",
        "",
        "## 1. Run Status",
        "",
        f"- Status: `{status}`",
        f"- Model called: `{str(run_report.get('model_called')).lower()}`",
        f"- Prompts sent count: `{run_report.get('prompts_sent_count', 0)}`",
        "",
        "## 2. Model Runtime",
        "",
        f"- Remote host: `{run_report.get('remote_host')}`",
        f"- Remote Python: `{run_report.get('remote_python')}`",
        f"- Model path: `{run_report.get('base_model')}`",
        f"- Adapter path: `{run_report.get('adapter')}`",
        "",
        "## 3. Input Prompt",
        "",
        f"- Prompt pack: `{run_report.get('prompt_pack')}`",
        f"- Prompt id: `{run_report.get('prompt_id')}`",
        f"- Case ref: `{run_report.get('case_ref')}`",
        "- Diagnostic payload sent to model: `messages` only",
        "",
        "## 4. Output Files",
        "",
        f"- Responses: `{run_report.get('responses_jsonl')}`",
        f"- Raw responses: `{run_report.get('responses_raw_jsonl')}`",
        f"- Response count: `{run_report.get('response_count', 0)}`",
        f"- Raw response count: `{run_report.get('raw_response_count', 0)}`",
        "",
        "## 5. Response Format",
        "",
        f"- Parse success count: `{parse_success}`",
        f"- Raw records with errors: `{sum(1 for record in raw_records if record.get('error'))}`",
        "",
        "## 6. Validator Result",
        "",
        f"- Validator status: `{validator_report.get('status', 'not_run') if validator_report else 'not_run'}`",
        f"- Pass / warn / fail: `{counts['pass']} / {counts['warn']} / {counts['fail']}`",
        "",
        "## 7. Safety Checks",
        "",
    ]
    for key, value in hard_counts.items():
        lines.append(f"- `{key}`: `{value}`")
    if categories:
        lines.extend(["", "Main warning/failure categories:"])
        for key, value in categories.items():
            lines.append(f"- `{key}`: `{value}`")
    lines.extend(
        [
            "",
            "## 8. Recommendation",
            "",
            f"- `{recommendation}`",
            "",
            "- No final RCA accuracy was computed.",
            "- No GT labels, subtype labels, scenario tags, train/eval/test artifacts, or closed-loop schemas were used.",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_full20_summary(
    path: Path,
    run_report: Dict[str, Any],
    validator_report: Optional[Dict[str, Any]],
    raw_records: List[Dict[str, Any]],
) -> None:
    counts = validator_status_counts(validator_report)
    hard_counts = hard_safety_counts(validator_report)
    categories = main_categories(validator_report)
    parse_success = sum(1 for record in raw_records if record.get("parse_ok") is True)
    contract_success = sum(1 for record in raw_records if record.get("contract_ok") is True)
    repair_retry_count = sum(1 for record in raw_records if record.get("repair_retry_used") is True)
    normalization_actions_count = sum(
        len(record.get("normalization_actions") or [])
        for record in raw_records
        if isinstance(record.get("normalization_actions"), list)
    )
    validator_status = validator_report.get("status", "not_run") if validator_report else "not_run"
    before_counts = run_report.get("repair_baseline_validator_counts")
    before_categories = run_report.get("repair_baseline_categories")
    if not isinstance(before_counts, dict):
        before_counts = {"pass": 11, "warn": 4, "fail": 5}
    if not isinstance(before_categories, dict):
        before_categories = {"response_parse_error": 3, "unknown_evidence_role": 2}
    if not hard_safety_zero(validator_report):
        recommendation = "stop due safety failure"
    elif validator_status == "FAIL":
        recommendation = "fix wrapper output parsing and response role contract before expansion"
    elif validator_status == "WARN":
        recommendation = "analyze warnings first"
    elif validator_status == "PASS":
        recommendation = "run official 50-case Qwen3 direct"
    else:
        recommendation = "fix runtime"

    required_categories = [
        "root_cause_not_aligned_with_candidate_anomaly",
        "evidence_roles_contains_unused_ids",
        "concrete_root_without_evidence",
        "root_candidate_omitted",
        "external_context_claim",
        "response_parse_error",
    ]
    lines = [
        "# Qwen3 Direct 20-case Evidence Diagnosis Summary",
        "",
        "## 1. Run Status",
        "",
        f"- Status: `{run_report.get('status', 'BLOCKED')}`",
        f"- Official backend, not Ollama: `true`",
        f"- Model called: `{str(run_report.get('model_called')).lower()}`",
        f"- Prompts sent count: `{run_report.get('prompts_sent_count', 0)}`",
        "",
        "## 2. Model Runtime",
        "",
        f"- Remote host: `{run_report.get('remote_host')}`",
        f"- Remote Python: `{run_report.get('remote_python')}`",
        f"- Base model: `{run_report.get('base_model')}`",
        f"- Adapter: `{run_report.get('adapter')}`",
        "",
        "## 3. Input Pack",
        "",
        f"- Prompt pack: `{run_report.get('prompt_pack')}`",
        f"- Prompt count: `{run_report.get('prompt_count', 20)}`",
        "- Diagnostic payload sent to model: `messages` only",
        "",
        "## 4. Output Files",
        "",
        f"- Responses: `{run_report.get('responses_jsonl')}`",
        f"- Raw responses: `{run_report.get('responses_raw_jsonl')}`",
        f"- Response count: `{run_report.get('response_count', 0)}`",
        f"- Raw response count: `{run_report.get('raw_response_count', 0)}`",
        "",
        "## 5. Response Format",
        "",
        f"- Parse success count: `{parse_success}`",
        f"- Contract success count: `{contract_success}`",
        f"- Repair retry count: `{repair_retry_count}`",
        f"- Normalization actions count: `{normalization_actions_count}`",
        f"- Raw records with errors: `{sum(1 for record in raw_records if record.get('error'))}`",
        "",
        "## 6. Validator Result",
        "",
        "Before repair gate:",
        f"- Pass / warn / fail: `{before_counts.get('pass', 11)} / {before_counts.get('warn', 4)} / {before_counts.get('fail', 5)}`",
        f"- `response_parse_error`: `{before_categories.get('response_parse_error', 3)}`",
        f"- `unknown_evidence_role`: `{before_categories.get('unknown_evidence_role', 2)}`",
        "",
        "After repair gate:",
        f"- Validator status: `{validator_status}`",
        f"- Pass / warn / fail: `{counts['pass']} / {counts['warn']} / {counts['fail']}`",
        f"- `response_parse_error`: `{categories.get('response_parse_error', 0)}`",
        f"- `unknown_evidence_role`: `{categories.get('unknown_evidence_role', 0)}`",
        "",
        "## 7. Hard Safety Results",
        "",
    ]
    for key, value in hard_counts.items():
        lines.append(f"- `{key}`: `{value}`")
    lines.extend(["", "## 8. Warning Categories", ""])
    for key in required_categories:
        lines.append(f"- `{key}`: `{categories.get(key, 0)}`")
    extras = {key: value for key, value in categories.items() if key not in required_categories}
    if extras:
        lines.append("")
        lines.append("Other categories:")
        for key, value in extras.items():
            lines.append(f"- `{key}`: `{value}`")
    lines.extend(
        [
            "",
            "## 9. Comparison to One-case Smoke",
            "",
            "- One-case official Qwen3 direct smoke: `PASS`",
            "- One-case hard safety counts: all zero",
            "",
            "## 10. Recommendation",
            "",
            f"- `{recommendation}`",
            "",
            "- No final RCA accuracy was computed.",
            "- No GT labels, subtype labels, scenario tags, train/eval/test artifacts, or closed-loop schemas were used.",
            "- Ollama and OpenAI-compatible endpoints were not used.",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_full20_runbook(path: Path) -> None:
    lines = [
        "# Qwen3 Direct 20-case Evidence Diagnosis Runbook",
        "",
        "## 1. Purpose",
        "",
        "Run the v2 20-case evidence prompt pack through the official project-owned Qwen3 direct backend.",
        "",
        "## 2. Scope",
        "",
        "- Backend assets must stay under `/home/xrh`.",
        "- Remote temporary files must stay under `/home/xrh/qwen3_os_fault/tmp/evidence_direct_adapter/`.",
        "- Local outputs are written under `manual_experiments/evidence_qwen3_direct_diag20_20260527_full20/`.",
        "",
        "## 3. Rerun Command",
        "",
        "```powershell",
        "$env:PYTHONDONTWRITEBYTECODE=1",
        "python -B tools/evidence_qwen3_direct_runner.py `",
        "  --pack-dir manual_experiments/evidence_diag_20_20260525_v2 `",
        "  --output-dir manual_experiments/evidence_qwen3_direct_diag20_20260527_full20 `",
        "  --run `",
        "  --start-index 0 `",
        "  --limit 20 `",
        "  --remote-python /home/xrh/qwen3_os_fault/.venv_qwen3/bin/python `",
        "  --timeout-seconds 3600",
        "```",
        "",
        "## 4. Validation Command",
        "",
        "```powershell",
        "$env:PYTHONDONTWRITEBYTECODE=1",
        "python -B tools/evidence_response_validator.py `",
        "  --prompts-jsonl manual_experiments/evidence_diag_20_20260525_v2/prompts.jsonl `",
        "  --responses-jsonl manual_experiments/evidence_qwen3_direct_diag20_20260527_full20/responses.jsonl `",
        "  --dry-run `",
        "  --stdout-only `",
        "  --format both `",
        "  --max-examples 20",
        "```",
        "",
        "## 5. Safety Rules",
        "",
        "- Do not use Ollama, `/api/chat`, `/api/tags`, port `11434`, `/chat/completions`, or any OpenAI-compatible server.",
        "- Do not read GT/eval/test/train artifacts.",
        "- Do not compute final RCA accuracy here.",
        "- Send only each prompt record's `messages` as diagnostic model input.",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_diag50_summary(
    path: Path,
    run_report: Dict[str, Any],
    validator_report: Optional[Dict[str, Any]],
    raw_records: List[Dict[str, Any]],
) -> None:
    counts = validator_status_counts(validator_report)
    hard_counts = hard_safety_counts(validator_report)
    categories = main_categories(validator_report)
    parse_success = sum(1 for record in raw_records if record.get("parse_ok") is True)
    contract_success = sum(1 for record in raw_records if record.get("contract_ok") is True)
    validator_status = validator_report.get("status", "not_run") if validator_report else "not_run"
    if not hard_safety_zero(validator_report):
        recommendation = "stop due safety failure"
    elif validator_status == "FAIL":
        recommendation = "fix wrapper parsing before larger runs"
    elif validator_status == "WARN":
        recommendation = "run warning analysis for 50 before larger runs"
    elif validator_status == "PASS":
        recommendation = "optional larger run or comparison after review"
    else:
        recommendation = "fix runtime"

    required_categories = [
        "response_parse_error",
        "unknown_evidence_role",
        "root_cause_not_aligned_with_candidate_anomaly",
        "evidence_roles_contains_unused_ids",
        "concrete_root_without_evidence",
        "root_candidate_omitted",
        "external_context_claim",
    ]
    lines = [
        "# Qwen3 Direct 50-case Evidence Diagnosis Summary",
        "",
        "## 1. Run Status",
        "",
        f"- Status: `{run_report.get('status', 'BLOCKED')}`",
        "- Official backend, not Ollama: `true`",
        f"- Model called: `{str(run_report.get('model_called')).lower()}`",
        f"- Prompts sent count: `{run_report.get('prompts_sent_count', 0)}`",
        "",
        "## 2. Model Runtime",
        "",
        f"- Remote host: `{run_report.get('remote_host')}`",
        f"- Remote Python: `{run_report.get('remote_python')}`",
        f"- Base model: `{run_report.get('base_model')}`",
        f"- Adapter: `{run_report.get('adapter')}`",
        "",
        "## 3. Input Pack",
        "",
        f"- Prompt pack: `{run_report.get('prompt_pack')}`",
        f"- Prompt count: `{run_report.get('prompt_count', 50)}`",
        "- Diagnostic payload sent to model: `messages` only",
        "",
        "## 4. Output Files",
        "",
        f"- Responses: `{run_report.get('responses_jsonl')}`",
        f"- Raw responses: `{run_report.get('responses_raw_jsonl')}`",
        f"- Response count: `{run_report.get('response_count', 0)}`",
        f"- Raw response count: `{run_report.get('raw_response_count', 0)}`",
        "",
        "## 5. Response Format",
        "",
        f"- Parse success count: `{parse_success}`",
        f"- Contract success count: `{contract_success}`",
        f"- Repair retry count: `{run_report.get('repair_retry_count', 0)}`",
        f"- Normalization actions count: `{run_report.get('normalization_actions_count', 0)}`",
        "",
        "## 6. Validator Result",
        "",
        f"- Validator status: `{validator_status}`",
        f"- Pass / warn / fail: `{counts['pass']} / {counts['warn']} / {counts['fail']}`",
        "",
        "## 7. Hard Safety Results",
        "",
    ]
    for key, value in hard_counts.items():
        lines.append(f"- `{key}`: `{value}`")
    lines.extend(["", "## 8. Warning Categories", ""])
    for key in required_categories:
        lines.append(f"- `{key}`: `{categories.get(key, 0)}`")
    extras = {key: value for key, value in categories.items() if key not in required_categories}
    if extras:
        lines.append("")
        lines.append("Other categories:")
        for key, value in extras.items():
            lines.append(f"- `{key}`: `{value}`")
    lines.extend(
        [
            "",
            "## 9. Comparison to Official 20-case Run",
            "",
            "- Official 20-case validator: `WARN`",
            "- Official 20-case pass / warn / fail: `14 / 6 / 0`",
            "- Official 20-case hard safety: all zero",
            "- Official 20-case warnings: 6 benign",
            "",
            "## 10. Recommendation",
            "",
            f"- `{recommendation}`",
            "",
            "- No final RCA accuracy was computed.",
            "- No GT labels, subtype labels, scenario tags, train/eval/test artifacts, or closed-loop schemas were used.",
            "- Ollama and OpenAI-compatible endpoints were not used.",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_diag50_runbook(path: Path) -> None:
    lines = [
        "# Qwen3 Direct 50-case Evidence Diagnosis Runbook",
        "",
        "## 1. Purpose",
        "",
        "Run the v2 50-case evidence prompt pack through the official project-owned Qwen3 direct backend.",
        "",
        "## 2. Scope",
        "",
        "- Backend assets must stay under `/home/xrh`.",
        "- Remote temporary files must stay under `/home/xrh/qwen3_os_fault/tmp/evidence_direct_adapter/`.",
        "- Local outputs are written under `manual_experiments/evidence_qwen3_direct_diag50_20260527/`.",
        "",
        "## 3. Rerun Command",
        "",
        "```powershell",
        "$env:PYTHONDONTWRITEBYTECODE=1",
        "python -B tools/evidence_qwen3_direct_runner.py `",
        "  --pack-dir manual_experiments/evidence_qwen3_direct_diag50_20260527 `",
        "  --output-dir manual_experiments/evidence_qwen3_direct_diag50_20260527 `",
        "  --run `",
        "  --start-index 0 `",
        "  --limit 50 `",
        "  --remote-python /home/xrh/qwen3_os_fault/.venv_qwen3/bin/python `",
        "  --timeout-seconds 7200",
        "```",
        "",
        "## 4. Validation Command",
        "",
        "```powershell",
        "$env:PYTHONDONTWRITEBYTECODE=1",
        "python -B tools/evidence_response_validator.py `",
        "  --prompts-jsonl manual_experiments/evidence_qwen3_direct_diag50_20260527/prompts.jsonl `",
        "  --responses-jsonl manual_experiments/evidence_qwen3_direct_diag50_20260527/responses.jsonl `",
        "  --dry-run `",
        "  --stdout-only `",
        "  --format both `",
        "  --max-examples 20",
        "```",
        "",
        "## 5. Safety Rules",
        "",
        "- Do not use Ollama, `/api/chat`, `/api/tags`, port `11434`, `/chat/completions`, or any OpenAI-compatible server.",
        "- Do not read GT/eval/test/train artifacts.",
        "- Do not compute final RCA accuracy here.",
        "- Send only each prompt record's `messages` as diagnostic model input.",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_run_summary(
    output_dir: Path,
    run_report: Dict[str, Any],
    validator_report: Optional[Dict[str, Any]],
    raw_records: List[Dict[str, Any]],
) -> None:
    if output_dir == ALLOWED_FULL20_OUTPUT_DIR:
        write_full20_summary(output_dir / "qwen3_direct_20case_summary.md", run_report, validator_report, raw_records)
    elif output_dir == ALLOWED_DIAG50_OUTPUT_DIR:
        write_diag50_summary(output_dir / "qwen3_direct_50case_summary.md", run_report, validator_report, raw_records)
    elif output_dir == ALLOWED_LINKFLAP_V3_OUTPUT_DIR:
        write_diag50_summary(output_dir / "qwen3_direct_linkflap_v3_summary.md", run_report, validator_report, raw_records)
    elif output_dir == ALLOWED_LINKFLAP_V4_OUTPUT_DIR:
        write_diag50_summary(output_dir / "qwen3_direct_linkflap_v4_summary.md", run_report, validator_report, raw_records)
    elif output_dir == ALLOWED_LINKFLAP_P1_OUTPUT_DIR:
        write_diag50_summary(output_dir / "qwen3_direct_linkflap_p1_summary.md", run_report, validator_report, raw_records)
    elif output_dir == ALLOWED_LINKFLAP_P1_1_OUTPUT_DIR:
        write_diag50_summary(output_dir / "qwen3_direct_linkflap_p1_1_summary.md", run_report, validator_report, raw_records)
    elif output_dir == ALLOWED_LINKFLAP_P1_2_OUTPUT_DIR:
        write_diag50_summary(output_dir / "qwen3_direct_linkflap_p1_2_summary.md", run_report, validator_report, raw_records)
    elif output_dir == ALLOWED_DIAG50_LINKFLAP_V3_OUTPUT_DIR:
        write_diag50_summary(output_dir / "qwen3_direct_v3_50case_summary.md", run_report, validator_report, raw_records)
    elif output_dir == ALLOWED_DIAG50_LINKFLAP_P1_1_OUTPUT_DIR:
        write_diag50_summary(output_dir / "qwen3_direct_p1_1_50case_summary.md", run_report, validator_report, raw_records)
    elif output_dir == ALLOWED_DIAG50_LINKFLAP_P1_2_OUTPUT_DIR:
        write_diag50_summary(output_dir / "qwen3_direct_p1_2_50case_summary.md", run_report, validator_report, raw_records)
    else:
        write_one_case_summary(output_dir / "qwen3_direct_one_case_summary.md", run_report, validator_report, raw_records)


def parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack-dir", default=str(DEFAULT_PACK_DIR))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--prompts-jsonl", default=None)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Run non-generative preflight checks.")
    mode.add_argument("--run", action="store_true", help="Future real model execution mode.")
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--remote-host", default=DEFAULT_REMOTE_HOST)
    parser.add_argument("--remote-work-dir", default=DEFAULT_REMOTE_WORK_DIR)
    parser.add_argument("--remote-wrapper", default=DEFAULT_REMOTE_WRAPPER)
    parser.add_argument("--remote-base-model", default=DEFAULT_REMOTE_BASE_MODEL)
    parser.add_argument("--remote-adapter", default=DEFAULT_REMOTE_ADAPTER)
    parser.add_argument("--remote-tmp-dir", default=DEFAULT_REMOTE_TMP_DIR)
    parser.add_argument(
        "--remote-python",
        default=DEFAULT_REMOTE_PYTHON,
        help="Remote Python executable for wrapper self-check; use a simple command or /home/xrh path.",
    )
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-new-tokens", type=int, default=800)
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--stdout-only", action="store_true")
    return parser.parse_args(list(argv) if argv is not None else None)


def build_next_command(args: argparse.Namespace) -> str:
    remote_python_line = ""
    if getattr(args, "remote_python", DEFAULT_REMOTE_PYTHON) != DEFAULT_REMOTE_PYTHON:
        remote_python_line = f"  --remote-python {args.remote_python} `\n"
    output_dir_line = f"  --output-dir {args.output_dir} `\n"
    return (
        "$env:PYTHONDONTWRITEBYTECODE=1\n"
        "python -B tools/evidence_qwen3_direct_runner.py `\n"
        f"  --pack-dir {args.pack_dir} `\n"
        f"{output_dir_line}"
        "  --run `\n"
        f"{remote_python_line}"
        f"  --start-index {args.start_index} `\n"
        f"  --limit {args.limit}"
    )


def write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_markdown_report(path: Path, report: Dict[str, Any]) -> None:
    checks = report.get("checks", {})
    risks = report.get("risks", [])
    prompt_validator = report.get("prompt_validator", {})
    lines = [
        "# Qwen3 Direct Adapter Preflight Report",
        "",
        "## 1. Status",
        "",
        f"- Status: `{report.get('status')}`",
        f"- Model called: `{str(report.get('model_called')).lower()}`",
        f"- Prompts sent to model: `{str(report.get('prompts_sent')).lower()}`",
        "",
        "## 2. Local Runner",
        "",
        f"- Runner: `{report.get('local_runner')}`",
        f"- Output directory: `{report.get('output_dir')}`",
        "",
        "## 3. Remote Wrapper",
        "",
        f"- Host: `{report.get('remote_host')}`",
        f"- Remote scope: `{report.get('remote_scope')}`",
        f"- Wrapper: `{report.get('remote_wrapper')}`",
        "",
        "## 4. Model Assets",
        "",
        f"- Base model: `{report.get('base_model')}`",
        f"- Adapter: `{report.get('adapter')}`",
        "",
        "## 5. Prompt Pack",
        "",
        f"- Prompt pack: `{report.get('prompt_pack')}`",
        f"- Prompt count: `{report.get('prompt_count')}`",
        f"- Selected dry-run count: `{report.get('selected_count')}`",
        "",
        "## 6. Safety Checks",
        "",
    ]
    for key in sorted(checks):
        lines.append(f"- `{key}`: `{str(checks[key]).lower()}`")
    lines.extend(
        [
            f"- Prompt validator status: `{prompt_validator.get('status', 'unknown')}`",
            "",
            "## 7. Dry-run Result",
            "",
            "Dry-run completed without model loading or generation."
            if report.get("status") != "FAIL"
            else "Dry-run did not pass all required checks.",
            "",
            "## 8. Next Command",
            "",
            "```powershell",
            str(report.get("next_command", "")),
            "```",
            "",
            "## 9. Notes",
            "",
            "- No model was called.",
            "- No prompts were sent to a model.",
            "- No responses were created or modified.",
            "- Ollama remains out-of-scope.",
            "- This adapter accepts messages-only prompt records.",
        ]
    )
    if risks:
        lines.extend(["", "Risks:"])
        for risk in risks:
            lines.append(f"- {risk}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_runbook(path: Path) -> None:
    lines = [
        "# Qwen3 Direct Evidence Diagnosis Runner Runbook",
        "",
        "## 1. Purpose",
        "",
        "Run evidence diagnosis prompts through the project-owned Qwen3 direct deployment without Ollama.",
        "",
        "## 2. Scope",
        "",
        "The runner is scoped to messages-only prompt records and remote files under `/home/xrh`.",
        "",
        "## 3. Files",
        "",
        "- Local runner: `tools/evidence_qwen3_direct_runner.py`",
        "- Remote wrapper: `/home/xrh/qwen3_os_fault/evidence_messages_infer.py`",
        "- Recommended runtime can be supplied with `--remote-python /home/xrh/qwen3_os_fault/.venv_qwen3/bin/python`.",
        "- Base model: `/home/xrh/models/Qwen/Qwen3-8B`",
        "- Adapter: `/home/xrh/qwen3_os_fault/qwen3_8b_fault_qlora`",
        "",
        "## 4. Dry-run Command",
        "",
        "```powershell",
        "$env:PYTHONDONTWRITEBYTECODE=1",
        "python -B tools/evidence_qwen3_direct_runner.py `",
        "  --pack-dir manual_experiments/evidence_diag_20_20260525_v2 `",
        "  --output-dir manual_experiments/evidence_qwen3_direct_diag20_20260527 `",
        "  --dry-run `",
        "  --remote-python /home/xrh/qwen3_os_fault/.venv_qwen3/bin/python `",
        "  --limit 1 `",
        "  --stdout-only",
        "```",
        "",
        "## 5. Future One-case Run Command",
        "",
        "Do not run until a scoped follow-up implements and reviews the real copy/call/validate flow.",
        "The remote wrapper also requires `EVIDENCE_QWEN3_DIRECT_ALLOW_MODEL=1` before model loading.",
        "",
        "```powershell",
        '$env:EVIDENCE_QWEN3_DIRECT_ALLOW_MODEL="1"',
        "$env:PYTHONDONTWRITEBYTECODE=1",
        "python -B tools/evidence_qwen3_direct_runner.py `",
        "  --pack-dir manual_experiments/evidence_diag_20_20260525_v2 `",
        "  --output-dir manual_experiments/evidence_qwen3_direct_diag20_20260527 `",
        "  --run `",
        "  --remote-python /home/xrh/qwen3_os_fault/.venv_qwen3/bin/python `",
        "  --start-index 0 `",
        "  --limit 1",
        "```",
        "",
        "## 6. Response Validation",
        "",
        "Validate any future response JSONL with `tools/evidence_response_validator.py` before use.",
        "",
        "## 7. Safety Rules",
        "",
        "- Send only each prompt record's `messages` field to the model.",
        "- Keep `prompt_id` and `case_ref` as control-plane metadata only.",
        "- Do not read train, eval, test, GT label, subtype, scenario tag, or run-folder schemas.",
        "- Do not use Ollama, port 11434, `/api/chat`, `/api/tags`, or `/chat/completions`.",
        "",
        "## 8. Notes",
        "",
        "This runbook documents the skeleton and preflight path only.",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_preflight(args: argparse.Namespace) -> Dict[str, Any]:
    pack_dir = resolve_local(args.pack_dir)
    output_dir = resolve_local(args.output_dir)
    prompts_jsonl = resolve_local(args.prompts_jsonl) if args.prompts_jsonl else pack_dir / "prompts.jsonl"

    require_under(pack_dir, MANUAL_EXPERIMENTS, "PACK_DIR_OUT_OF_SCOPE")
    require_under(output_dir, MANUAL_EXPERIMENTS, "OUTPUT_DIR_OUT_OF_SCOPE")
    require_under(prompts_jsonl, MANUAL_EXPERIMENTS, "PROMPTS_JSONL_OUT_OF_SCOPE")
    if output_dir not in ALLOWED_OUTPUT_DIRS:
        raise RunnerError(
            "OUTPUT_DIR_NOT_ALLOWLISTED",
            "this runner may write only under approved Qwen3 direct output directories",
        )
    if not prompts_jsonl_allowed_for_output(output_dir, prompts_jsonl):
        raise RunnerError(
            "PROMPTS_JSONL_NOT_ALLOWLISTED",
            f"this run is pinned to {prompts_allowlist_message(output_dir)}",
        )

    for name, remote_path in [
        ("remote_work_dir", args.remote_work_dir),
        ("remote_wrapper", args.remote_wrapper),
        ("remote_base_model", args.remote_base_model),
        ("remote_adapter", args.remote_adapter),
        ("remote_tmp_dir", args.remote_tmp_dir),
    ]:
        require_remote_scope(remote_path, name)
    require_safe_remote_python(args.remote_python)

    if not prompts_jsonl.exists():
        raise RunnerError("PROMPTS_JSONL_MISSING", f"Prompt file not found: {prompts_jsonl}")

    records = read_jsonl(prompts_jsonl)
    selected = select_messages_only(records, args.start_index, args.limit)
    local_prompt_safety_ok, prompt_validator_json, prompt_validator_cmd = run_prompt_only_validator(prompts_jsonl)

    ssh_check = run_ssh(args.remote_host, "printf ready", timeout=30)
    remote_ssh_ok = ssh_check["returncode"] == 0

    wrapper_exists, wrapper_check = remote_path_exists(args.remote_host, args.remote_wrapper, "file")
    base_exists, base_check = remote_path_exists(args.remote_host, args.remote_base_model, "dir")
    adapter_exists, adapter_check = remote_path_exists(args.remote_host, args.remote_adapter, "dir")
    work_dir_exists, work_dir_check = remote_path_exists(args.remote_host, args.remote_work_dir, "dir")
    if "/" in args.remote_python:
        remote_python_exists, remote_python_check = remote_executable_exists(args.remote_host, args.remote_python)
    else:
        remote_python_exists = True
        remote_python_check = {"returncode": 0}

    self_check_json: Optional[Dict[str, Any]] = None
    remote_self_check_ok = False
    self_check_cmd: Optional[Dict[str, Any]] = None
    if remote_ssh_ok and wrapper_exists:
        remote_command = (
            "PYTHONDONTWRITEBYTECODE=1 "
            f"{posix_quote(args.remote_python)} {posix_quote(args.remote_wrapper)} --self-check "
            f"--base-model {posix_quote(args.remote_base_model)} "
            f"--adapter {posix_quote(args.remote_adapter)}"
        )
        self_check_cmd = run_ssh(args.remote_host, remote_command, timeout=60)
        self_check_json = extract_json_object(self_check_cmd.get("stdout", ""))
        remote_self_check_ok = (
            self_check_cmd["returncode"] == 0
            and self_check_json is not None
            and self_check_json.get("status") == "PASS"
            and self_check_json.get("model_loaded") is False
            and self_check_json.get("model_called") is False
        )

    checks = {
        "local_prompt_file_exists": prompts_jsonl.exists(),
        "local_prompt_safety_ok": local_prompt_safety_ok,
        "remote_ssh_ok": remote_ssh_ok,
        "remote_python_executable": remote_python_exists,
        "remote_work_dir_exists": work_dir_exists,
        "remote_wrapper_exists": wrapper_exists,
        "remote_base_model_exists": base_exists,
        "remote_adapter_exists": adapter_exists,
        "remote_self_check_ok": remote_self_check_ok,
    }
    risks: List[str] = []
    if prompt_validator_json and prompt_validator_json.get("status") == "WARN":
        risks.append("Prompt-only validator status is WARN by design; hard prompt safety counters are zero.")
    for key, value in checks.items():
        if not value:
            risks.append(f"Check failed: {key}")

    status = "PASS" if all(checks.values()) else "FAIL"

    report = {
        "status": status,
        "created_at": utc_now(),
        "model_called": False,
        "prompts_sent": False,
        "remote_scope": REMOTE_SCOPE,
        "local_runner": "tools/evidence_qwen3_direct_runner.py",
        "output_dir": repo_rel(output_dir),
        "remote_host": args.remote_host,
        "remote_python": args.remote_python,
        "remote_wrapper": args.remote_wrapper,
        "remote_work_dir": args.remote_work_dir,
        "remote_tmp_dir": args.remote_tmp_dir,
        "base_model": args.remote_base_model,
        "adapter": args.remote_adapter,
        "prompt_pack": repo_rel(prompts_jsonl),
        "prompt_count": len(records),
        "selected_count": len(selected),
        "start_index": args.start_index,
        "limit": args.limit,
        "checks": checks,
        "prompt_validator": {
            "status": prompt_validator_json.get("status") if prompt_validator_json else "unknown",
            "hard_safety_zero": local_prompt_safety_ok,
            "returncode": prompt_validator_cmd["returncode"],
        },
        "remote_self_check": self_check_json,
        "risks": risks,
        "next_command": build_next_command(args),
        "debug": {
            "ssh_returncode": ssh_check["returncode"],
            "wrapper_check_returncode": wrapper_check["returncode"],
            "base_check_returncode": base_check["returncode"],
            "adapter_check_returncode": adapter_check["returncode"],
            "work_dir_check_returncode": work_dir_check["returncode"],
            "remote_python_check_returncode": remote_python_check["returncode"],
            "self_check_returncode": None if self_check_cmd is None else self_check_cmd["returncode"],
        },
    }

    if not args.stdout_only and output_dir == ALLOWED_OUTPUT_DIR:
        output_dir.mkdir(parents=True, exist_ok=True)
        write_json(output_dir / "adapter_preflight_report.json", report)
        write_markdown_report(output_dir / "adapter_preflight_report.md", report)
        write_runbook(output_dir / "RUNBOOK.md")
    elif not args.stdout_only and output_dir == ALLOWED_FULL20_OUTPUT_DIR:
        output_dir.mkdir(parents=True, exist_ok=True)
        write_full20_runbook(output_dir / "RUNBOOK.md")
    elif not args.stdout_only and output_dir == ALLOWED_DIAG50_OUTPUT_DIR:
        output_dir.mkdir(parents=True, exist_ok=True)
        write_diag50_runbook(output_dir / "RUNBOOK.md")
    elif not args.stdout_only and output_dir == ALLOWED_DIAG50_LINKFLAP_V3_OUTPUT_DIR:
        output_dir.mkdir(parents=True, exist_ok=True)
        write_diag50_runbook(output_dir / "RUNBOOK.md")
    elif not args.stdout_only and output_dir == ALLOWED_DIAG50_LINKFLAP_P1_1_OUTPUT_DIR:
        output_dir.mkdir(parents=True, exist_ok=True)
        write_diag50_runbook(output_dir / "RUNBOOK.md")
    elif not args.stdout_only and output_dir == ALLOWED_LINKFLAP_P1_2_OUTPUT_DIR:
        output_dir.mkdir(parents=True, exist_ok=True)
        write_diag50_runbook(output_dir / "RUNBOOK.md")
    elif not args.stdout_only and output_dir == ALLOWED_DIAG50_LINKFLAP_P1_2_OUTPUT_DIR:
        output_dir.mkdir(parents=True, exist_ok=True)
        write_diag50_runbook(output_dir / "RUNBOOK.md")
    return report


def run_future_mode(args: argparse.Namespace) -> int:
    if args.start_index < 0:
        raise RunnerError("START_INDEX_INVALID", "--start-index must be >= 0")
    if args.timeout_seconds < 60:
        raise RunnerError("TIMEOUT_TOO_SMALL", "--timeout-seconds must be at least 60")

    pack_dir = resolve_local(args.pack_dir)
    output_dir = resolve_local(args.output_dir)
    prompts_jsonl = resolve_local(args.prompts_jsonl) if args.prompts_jsonl else pack_dir / "prompts.jsonl"
    require_under(pack_dir, MANUAL_EXPERIMENTS, "PACK_DIR_OUT_OF_SCOPE")
    require_under(output_dir, MANUAL_EXPERIMENTS, "OUTPUT_DIR_OUT_OF_SCOPE")
    require_under(prompts_jsonl, MANUAL_EXPERIMENTS, "PROMPTS_JSONL_OUT_OF_SCOPE")
    if output_dir not in ALLOWED_OUTPUT_DIRS:
        raise RunnerError(
            "OUTPUT_DIR_NOT_ALLOWLISTED",
            "real run may write outputs only under approved Qwen3 direct output directories",
        )
    if output_dir == ALLOWED_OUTPUT_DIR and args.limit != 1:
        raise RunnerError("ONE_CASE_LIMIT_REQUIRED", "official one-case smoke must use --limit 1")
    if output_dir == ALLOWED_FULL20_OUTPUT_DIR and (args.start_index != 0 or args.limit != 20):
        raise RunnerError("FULL20_SELECTION_REQUIRED", "official full20 run must use --start-index 0 --limit 20")
    if output_dir == ALLOWED_DIAG50_OUTPUT_DIR and (args.start_index != 0 or args.limit != 50):
        raise RunnerError("DIAG50_SELECTION_REQUIRED", "official diag50 run must use --start-index 0 --limit 50")
    if output_dir == ALLOWED_LINKFLAP_V3_OUTPUT_DIR and (args.start_index != 0 or args.limit != 6):
        raise RunnerError(
            "LINKFLAP_V3_SELECTION_REQUIRED",
            "official targeted link-flap v3 run must use --start-index 0 --limit 6",
        )
    if output_dir == ALLOWED_LINKFLAP_V4_OUTPUT_DIR and (args.start_index != 0 or args.limit != 6):
        raise RunnerError(
            "LINKFLAP_V4_SELECTION_REQUIRED",
            "official targeted link-flap v4 run must use --start-index 0 --limit 6",
        )
    if output_dir == ALLOWED_LINKFLAP_P1_OUTPUT_DIR and (args.start_index != 0 or args.limit != 7):
        raise RunnerError(
            "LINKFLAP_P1_SELECTION_REQUIRED",
            "official targeted link-flap P1 run must use --start-index 0 --limit 7",
        )
    if output_dir == ALLOWED_LINKFLAP_P1_1_OUTPUT_DIR and (args.start_index != 0 or args.limit != 7):
        raise RunnerError(
            "LINKFLAP_P1_1_SELECTION_REQUIRED",
            "official targeted link-flap P1.1 run must use --start-index 0 --limit 7",
        )
    if output_dir == ALLOWED_LINKFLAP_P1_2_OUTPUT_DIR and (args.start_index != 0 or args.limit != 12):
        raise RunnerError(
            "LINKFLAP_P1_2_SELECTION_REQUIRED",
            "official targeted link-flap P1.2 gate run must use --start-index 0 --limit 12",
        )
    if output_dir == ALLOWED_DIAG50_LINKFLAP_V3_OUTPUT_DIR and (args.start_index != 0 or args.limit != 50):
        raise RunnerError(
            "DIAG50_LINKFLAP_V3_SELECTION_REQUIRED",
            "official full v3 50-case run must use --start-index 0 --limit 50",
        )
    if output_dir == ALLOWED_DIAG50_LINKFLAP_P1_1_OUTPUT_DIR and (args.start_index != 0 or args.limit != 50):
        raise RunnerError(
            "DIAG50_LINKFLAP_P1_1_SELECTION_REQUIRED",
            "official full P1.1 50-case run must use --start-index 0 --limit 50",
        )
    if output_dir == ALLOWED_DIAG50_LINKFLAP_P1_2_OUTPUT_DIR and (args.start_index != 0 or args.limit != 50):
        raise RunnerError(
            "DIAG50_LINKFLAP_P1_2_SELECTION_REQUIRED",
            "official full P1.2 50-case run must use --start-index 0 --limit 50",
        )
    if not prompts_jsonl_allowed_for_output(output_dir, prompts_jsonl):
        raise RunnerError(
            "PROMPTS_JSONL_NOT_ALLOWLISTED",
            f"real run is pinned to {prompts_allowlist_message(output_dir)}",
        )
    for name, remote_path in [
        ("remote_work_dir", args.remote_work_dir),
        ("remote_wrapper", args.remote_wrapper),
        ("remote_base_model", args.remote_base_model),
        ("remote_adapter", args.remote_adapter),
        ("remote_tmp_dir", args.remote_tmp_dir),
    ]:
        require_remote_scope(remote_path, name)
    require_safe_remote_python(args.remote_python)

    responses_jsonl = output_dir / "responses.jsonl"
    responses_raw_jsonl = output_dir / "responses_raw.jsonl"
    baseline_validator_report: Optional[Dict[str, Any]] = None
    if output_dir == ALLOWED_FULL20_OUTPUT_DIR and args.overwrite:
        baseline_path = output_dir / "validator_report.json"
        if baseline_path.exists():
            try:
                baseline_obj = json.loads(baseline_path.read_text(encoding="utf-8"))
                if isinstance(baseline_obj, dict):
                    baseline_validator_report = baseline_obj
            except json.JSONDecodeError:
                baseline_validator_report = None
    if not args.overwrite:
        existing = [repo_rel(path) for path in (responses_jsonl, responses_raw_jsonl) if path.exists()]
        if existing:
            raise RunnerError("OUTPUT_ALREADY_EXISTS", f"refusing to overwrite existing outputs: {existing}")

    # Reuse preflight checks without refreshing the read-only preflight reports.
    original_stdout_only = args.stdout_only
    args.stdout_only = True
    try:
        preflight = run_preflight(args)
    finally:
        args.stdout_only = original_stdout_only
    if preflight.get("status") == "FAIL":
        raise RunnerError("PREFLIGHT_FAILED", "preflight failed; refusing to call the model")

    records = read_jsonl(prompts_jsonl)
    selected = select_messages_only(records, args.start_index, args.limit)
    if len(selected) != args.limit:
        raise RunnerError("SELECTED_COUNT_INVALID", f"expected {args.limit} selected prompts, got {len(selected)}")

    output_dir.mkdir(parents=True, exist_ok=True)
    local_tmp_dir = output_dir / "tmp"
    local_tmp_dir.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    local_input = local_tmp_dir / f"input_{run_id}.jsonl"
    write_jsonl(local_input, selected)

    remote_tmp_dir = posixpath.normpath(args.remote_tmp_dir)
    remote_input = posixpath.join(remote_tmp_dir, f"input_{run_id}.jsonl")
    remote_output = posixpath.join(remote_tmp_dir, f"responses_{run_id}.jsonl")
    remote_raw_output = posixpath.join(remote_tmp_dir, f"responses_raw_{run_id}.jsonl")
    remote_manifest = posixpath.join(remote_tmp_dir, f"run_manifest_{run_id}.json")
    for name, path in [
        ("remote_input", remote_input),
        ("remote_output", remote_output),
        ("remote_raw_output", remote_raw_output),
        ("remote_manifest", remote_manifest),
    ]:
        require_remote_scope(path, name)

    manifest: Dict[str, Any] = {
        "run_id": run_id,
        "created_at": utc_now(),
        "status": "BLOCKED",
        "model_called": False,
        "prompts_sent_count": 0,
        "remote_host": args.remote_host,
        "remote_python": args.remote_python,
        "remote_wrapper": args.remote_wrapper,
        "remote_work_dir": args.remote_work_dir,
        "remote_tmp_dir": remote_tmp_dir,
        "remote_input": remote_input,
        "remote_output": remote_output,
        "remote_raw_output": remote_raw_output,
        "remote_manifest": remote_manifest,
        "base_model": args.remote_base_model,
        "adapter": args.remote_adapter,
        "prompt_pack": repo_rel(prompts_jsonl),
        "prompt_count": len(records),
        "selected_count": len(selected),
        "start_index": args.start_index,
        "limit": args.limit,
        "prompt_ids": [record["prompt_id"] for record in selected],
        "case_refs": [record["case_ref"] for record in selected],
        "prompt_id": selected[0]["prompt_id"] if len(selected) == 1 else None,
        "case_ref": selected[0]["case_ref"] if len(selected) == 1 else None,
        "responses_jsonl": repo_rel(responses_jsonl),
        "responses_raw_jsonl": repo_rel(responses_raw_jsonl),
        "response_count": 0,
        "raw_response_count": 0,
        "validator_status": "not_run",
    }
    if baseline_validator_report is not None:
        manifest["repair_baseline_validator_status"] = baseline_validator_report.get("status")
        manifest["repair_baseline_validator_counts"] = validator_status_counts(baseline_validator_report)
        manifest["repair_baseline_categories"] = main_categories(baseline_validator_report)

    mkdir_result = run_ssh(args.remote_host, f"mkdir -p {posix_quote(remote_tmp_dir)}", timeout=60)
    manifest["remote_mkdir"] = command_tail(mkdir_result)
    if mkdir_result["returncode"] != 0:
        write_json(output_dir / "remote_run_manifest.json", manifest)
        write_run_summary(output_dir, manifest, None, [])
        raise RunnerError("REMOTE_TMP_CREATE_FAILED", "failed to create remote adapter tmp directory")

    scp_in = run_scp_to_remote(args.remote_host, local_input, remote_input, timeout=120)
    manifest["scp_input"] = command_tail(scp_in)
    if scp_in["returncode"] != 0:
        write_json(output_dir / "remote_run_manifest.json", manifest)
        write_run_summary(output_dir, manifest, None, [])
        raise RunnerError("REMOTE_INPUT_COPY_FAILED", "failed to copy messages-only input to remote tmp")

    remote_args = [
        args.remote_python,
        args.remote_wrapper,
        "--run",
        "--input-jsonl",
        remote_input,
        "--output-jsonl",
        remote_output,
        "--raw-output-jsonl",
        remote_raw_output,
        "--base-model",
        args.remote_base_model,
        "--adapter",
        args.remote_adapter,
        "--temperature",
        f"{args.temperature:g}",
        "--max-new-tokens",
        str(args.max_new_tokens),
        "--limit",
        str(args.limit),
    ]
    remote_command = (
        f"cd {posix_quote(args.remote_work_dir)} && "
        "PYTHONDONTWRITEBYTECODE=1 "
        "EVIDENCE_QWEN3_DIRECT_ALLOW_MODEL=1 "
        + " ".join(posix_quote(str(item)) for item in remote_args)
    )
    remote_run = run_ssh(args.remote_host, remote_command, timeout=args.timeout_seconds)
    manifest["remote_run"] = command_tail(remote_run)
    manifest["model_called"] = True
    manifest["prompts_sent_count"] = len(selected)
    if remote_run["returncode"] != 0:
        write_json(output_dir / "remote_run_manifest.json", manifest)
        remote_manifest_local = local_tmp_dir / f"run_manifest_{run_id}.json"
        write_json(remote_manifest_local, manifest)
        run_scp_to_remote(args.remote_host, remote_manifest_local, remote_manifest, timeout=120)
        write_run_summary(output_dir, manifest, None, [])
        return 2

    scp_responses = run_scp_from_remote(args.remote_host, remote_output, responses_jsonl, timeout=120)
    scp_raw = run_scp_from_remote(args.remote_host, remote_raw_output, responses_raw_jsonl, timeout=120)
    manifest["scp_responses"] = command_tail(scp_responses)
    manifest["scp_raw_responses"] = command_tail(scp_raw)
    if scp_responses["returncode"] != 0 or scp_raw["returncode"] != 0:
        write_json(output_dir / "remote_run_manifest.json", manifest)
        write_run_summary(output_dir, manifest, None, [])
        raise RunnerError("REMOTE_OUTPUT_COPY_FAILED", "failed to copy remote outputs back to local output dir")

    response_count = jsonl_count(responses_jsonl)
    raw_response_count = jsonl_count(responses_raw_jsonl)
    manifest["response_count"] = response_count
    manifest["raw_response_count"] = raw_response_count
    if response_count != len(selected) or raw_response_count != len(selected):
        manifest["status"] = "FAIL"
        write_json(output_dir / "remote_run_manifest.json", manifest)
        raw_records = read_any_jsonl(responses_raw_jsonl)
        write_run_summary(output_dir, manifest, None, raw_records)
        raise RunnerError("UNEXPECTED_RESPONSE_COUNT", f"expected exactly {len(selected)} responses and raw responses")

    validator_report, validator_json_cmd, validator_md_cmd = run_response_validator(prompts_jsonl, responses_jsonl, output_dir)
    raw_records = read_any_jsonl(responses_raw_jsonl)
    manifest["validator_status"] = validator_report.get("status")
    manifest["validator_counts"] = validator_status_counts(validator_report)
    manifest["hard_safety_counts"] = hard_safety_counts(validator_report)
    manifest["parse_success_count"] = sum(1 for record in raw_records if record.get("parse_ok") is True)
    manifest["local_contract_success_count"] = sum(1 for record in raw_records if record.get("contract_ok") is True)
    manifest["repair_retry_count"] = sum(1 for record in raw_records if record.get("repair_retry_used") is True)
    manifest["normalization_actions_count"] = sum(
        len(record.get("normalization_actions") or [])
        for record in raw_records
        if isinstance(record.get("normalization_actions"), list)
    )
    manifest["validator_json_command"] = command_tail(validator_json_cmd)
    manifest["validator_markdown_command"] = command_tail(validator_md_cmd)
    if validator_report.get("status") == "FAIL" or not hard_safety_zero(validator_report):
        manifest["status"] = "FAIL"
    elif validator_report.get("status") == "WARN":
        manifest["status"] = "WARN"
    else:
        manifest["status"] = "PASS"

    write_json(output_dir / "remote_run_manifest.json", manifest)
    remote_manifest_local = local_tmp_dir / f"run_manifest_{run_id}.json"
    write_json(remote_manifest_local, manifest)
    run_scp_to_remote(args.remote_host, remote_manifest_local, remote_manifest, timeout=120)
    write_run_summary(output_dir, manifest, validator_report, raw_records)
    if output_dir == ALLOWED_FULL20_OUTPUT_DIR:
        write_full20_runbook(output_dir / "RUNBOOK.md")
    elif output_dir == ALLOWED_DIAG50_OUTPUT_DIR:
        write_diag50_runbook(output_dir / "RUNBOOK.md")
    elif output_dir == ALLOWED_LINKFLAP_V4_OUTPUT_DIR:
        write_diag50_runbook(output_dir / "RUNBOOK.md")
    elif output_dir == ALLOWED_LINKFLAP_P1_OUTPUT_DIR:
        write_diag50_runbook(output_dir / "RUNBOOK.md")
    elif output_dir == ALLOWED_LINKFLAP_P1_1_OUTPUT_DIR:
        write_diag50_runbook(output_dir / "RUNBOOK.md")
    elif output_dir == ALLOWED_LINKFLAP_P1_2_OUTPUT_DIR:
        write_diag50_runbook(output_dir / "RUNBOOK.md")
    elif output_dir == ALLOWED_DIAG50_LINKFLAP_V3_OUTPUT_DIR:
        write_diag50_runbook(output_dir / "RUNBOOK.md")
    elif output_dir == ALLOWED_DIAG50_LINKFLAP_P1_1_OUTPUT_DIR:
        write_diag50_runbook(output_dir / "RUNBOOK.md")
    elif output_dir == ALLOWED_DIAG50_LINKFLAP_P1_2_OUTPUT_DIR:
        write_diag50_runbook(output_dir / "RUNBOOK.md")

    if args.stdout_only:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0 if manifest["status"] in {"PASS", "WARN"} else 2


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = parse_args(argv)
    try:
        if args.run:
            return run_future_mode(args)
        report = run_preflight(args)
        if args.stdout_only:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report.get("status") != "FAIL" else 2
    except RunnerError as exc:
        payload = {"status": "FAIL", "error": exc.code, "message": exc.message, "model_called": False}
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
