#!/usr/bin/env python3
import argparse
import json
import os
import re
import socket
import sys
import time
import subprocess
import tarfile
from pathlib import Path

MAX_ERROR_BYTES = 2048
COMMAND_SHAPED_KEYS = {
    "cmd",
    "command",
    "commands",
    "action_command",
    "shell",
    "exec",
    "argv",
    "args",
    "script",
    "script_path",
    "executable",
    "command_line",
    "recovery_command",
    "downlink",
    "downlink_command",
    "action_downlink",
    "actions_device",
    "latest_actions_device",
}
REQUIRED_MACHINE_SUGGESTION_FIELDS = {
    "risk",
    "command_template",
    "purpose",
    "when_to_use",
    "precondition",
    "expected_effect",
    "verification",
    "rollback",
    "requires_manual_approval",
    "auto_execute",
    "dispatch_channel",
}
CREDENTIAL_SHAPED_KEYS = {"psk", "password", "passwd", "passphrase", "wpa_passphrase", "secret", "credential"}
CREDENTIAL_VALUE_RE = re.compile(r"(?i)\b(psk|password|passwd|passphrase|wpa_passphrase|secret)\b\s*[=:]\s*(?!<redacted_credential>)\S+")


def now_utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def sanitize_token(val: str) -> str:
    out = []
    for ch in (val or ""):
        if ch.isalnum() or ch in ("-", "_", "."):
            out.append(ch)
    s = "".join(out)
    return s if s else "unknown"

def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)

def unlink_if_exists(p: Path) -> None:
    try:
        p.unlink()
    except FileNotFoundError:
        return
    except Exception:
        return


def parse_name(filename: str):
    if filename.endswith("__bundle.tar.gz"):
        return filename[:-len("__bundle.tar.gz")], "bundle"
    if filename.endswith("__action_result.tar.gz"):
        return filename[:-len("__action_result.tar.gz")], "action_result"
    return None, None

def safe_read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="ignore").strip()
    except Exception:
        return ""

def write_latest(out_root: Path, device_id: str, run_id: str, actions_data: bytes) -> None:
    dev = out_root / device_id
    atomic_write(dev / "latest_actions_device.txt", actions_data)
    atomic_write(dev / "latest_run_id.txt", (run_id + "\n").encode("utf-8"))
    atomic_write(dev / "latest_action_run_id.txt", (run_id + "\n").encode("utf-8"))

def write_latest_suggestions(out_root: Path, device_id: str, run_id: str, suggestions_data: bytes) -> None:
    dev = out_root / device_id
    unlink_if_exists(dev / "latest_actions_device.txt")
    unlink_if_exists(dev / "latest_action_run_id.txt")
    unlink_if_exists(dev / "latest_run_id.txt")
    atomic_write(dev / "latest_stage3_suggestions.json", suggestions_data)
    atomic_write(dev / "latest_suggestion_run_id.txt", (run_id + "\n").encode("utf-8"))

def clear_latest_actions(out_root: Path, device_id: str) -> None:
    dev = out_root / device_id
    unlink_if_exists(dev / "latest_actions_device.txt")
    unlink_if_exists(dev / "latest_action_run_id.txt")
    unlink_if_exists(dev / "latest_run_id.txt")
    unlink_if_exists(dev / "latest_stage3_suggestions.json")
    unlink_if_exists(dev / "latest_suggestion_run_id.txt")


def contains_command_field(value) -> bool:
    if isinstance(value, dict):
        for k, v in value.items():
            if str(k).lower() in COMMAND_SHAPED_KEYS:
                return True
            if contains_command_field(v):
                return True
    elif isinstance(value, list):
        return any(contains_command_field(v) for v in value)
    return False


def is_allowed_command_template_path(path) -> bool:
    return bool(path) and path[-1] == "command_template" and "machine_suggestions" in path[:-1]


def contains_command_like_value(value, path=None) -> bool:
    path = path or []
    executable_start = r"^\s*(/system/bin/|/bin/|/sbin/|/vendor/bin/)?(hdc|sh|bash|sudo|rm|cat|dmesg|ps|top|svc|ifconfig|iptables|wpa_cli)\s+[-/A-Za-z0-9]"
    route_executable_start = r"^\s*(/system/bin/|/bin/|/sbin/|/vendor/bin/)?route\s+(add|del|delete|change|show|flush|replace|get|monitor|print)\b"
    ip_executable_start = r"^\s*(/system/bin/|/bin/|/sbin/|/vendor/bin/)?ip\s+(-[46]\s+)?(route|addr|address|link|rule|neigh|netns|tunnel|xfrm|maddr|monitor)\b"
    executable_after_separator = r"(?i)(;|&&|\|\|)\s*(/system/bin/|/bin/|/sbin/|/vendor/bin/)?(hdc|sh|bash|sudo|rm|cat|dmesg|ps|top|svc|ifconfig|route|iptables|ip|wpa_cli)\s+"
    executable_after_action_verb = r"(?i)\b(run|execute|exec|invoke|call)\s+(/system/bin/|/bin/|/sbin/|/vendor/bin/)?(hdc|sh|bash|sudo|rm|cat|dmesg|ps|top|svc|ifconfig|route|iptables|ip|wpa_cli)\s+"
    executable_script_path = r"(?i)(^|\s)(/data/|/system/|/vendor/|/bin/|/sbin/)[^ \t\n\r;|&`$]*((actiond|actions_poller)[^ \t\n\r;|&`$]*|\.sh)(\s|$)"
    command_patterns = (
        r"(?i)" + executable_start,
        r"(?i)" + route_executable_start,
        r"(?i)" + ip_executable_start,
        r"(?i)^\s*reboot\s*$",
        r"(?i)(`|\$\(|&&|\|\|)",
        r"(?i)\|\s*(/system/bin/|/bin/|/sbin/|/vendor/bin/)?(hdc|sh|bash|sudo|rm|cat|dmesg|ps|top|svc|ifconfig|route|iptables|ip|wpa_cli)\s+",
        executable_after_separator,
        executable_after_action_verb,
        executable_script_path,
    )
    if is_allowed_command_template_path(path):
        return False
    if isinstance(value, dict):
        for k, v in value.items():
            if contains_command_like_value(v, path + [str(k)]):
                return True
        return False
    if isinstance(value, list):
        return any(contains_command_like_value(v, path + [f"[{idx}]"]) for idx, v in enumerate(value))
    if isinstance(value, str):
        return any(re.search(pattern, value) for pattern in command_patterns)
    return False


def contains_command_template_outside_machine_suggestions(value, path=None) -> bool:
    path = path or []
    if isinstance(value, dict):
        for k, v in value.items():
            next_path = path + [str(k)]
            if str(k) == "command_template" and not is_allowed_command_template_path(next_path):
                return True
            if contains_command_template_outside_machine_suggestions(v, next_path):
                return True
    elif isinstance(value, list):
        return any(contains_command_template_outside_machine_suggestions(v, path + [f"[{idx}]"]) for idx, v in enumerate(value))
    return False


def collect_machine_suggestion_lists(value, path=None):
    path = path or []
    found = []
    if isinstance(value, dict):
        for k, v in value.items():
            next_path = path + [str(k)]
            if str(k) == "machine_suggestions":
                found.append((next_path, v))
            found.extend(collect_machine_suggestion_lists(v, next_path))
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            found.extend(collect_machine_suggestion_lists(item, path + [f"[{idx}]"]))
    return found


def validate_action_r1_strategy_safety(value):
    failures = []
    if contains_command_template_outside_machine_suggestions(value):
        failures.append("command_template_outside_machine_suggestions")
    if contains_command_field(value):
        failures.append("disallowed_command_field")
    if contains_command_like_value(value):
        failures.append("disallowed_command_like_value_outside_machine_suggestions")

    def walk(node, path=None):
        path = path or []
        if isinstance(node, dict):
            for k, v in node.items():
                key = str(k)
                key_l = key.lower()
                next_path = path + [key]
                where = ".".join(next_path)
                if key_l in {"execution_enabled", "automatic_recovery_enabled", "action_command_enabled"} and v is not False:
                    failures.append("%s_not_false" % where)
                if key_l == "manual_approval_required" and v is not True:
                    failures.append("%s_not_true" % where)
                if key_l == "auto_execute" and v is not False:
                    failures.append("%s_not_false" % where)
                if key_l == "dispatch_channel" and str(v) != "none":
                    failures.append("%s_not_none" % where)
                walk(v, next_path)
        elif isinstance(node, list):
            for idx, item in enumerate(node):
                walk(item, path + [f"[{idx}]"])

    walk(value)

    machine_lists = collect_machine_suggestion_lists(value)
    if not machine_lists:
        failures.append("machine_suggestions_missing")
    for path, items in machine_lists:
        where = ".".join(path)
        if not isinstance(items, list) or not items:
            failures.append("%s_not_nonempty_list" % where)
            continue
        for idx, item in enumerate(items):
            item_where = "%s.[%d]" % (where, idx)
            if not isinstance(item, dict):
                failures.append("%s_not_object" % item_where)
                continue
            missing = sorted(field for field in REQUIRED_MACHINE_SUGGESTION_FIELDS if field not in item)
            if missing:
                failures.append("%s.missing_fields:%s" % (item_where, ",".join(missing)))
            if item.get("risk") not in {"low", "medium", "high"}:
                failures.append("%s.risk_invalid" % item_where)
            if not str(item.get("command_template") or "").strip():
                failures.append("%s.command_template_missing" % item_where)
            if item.get("requires_manual_approval") is not True:
                failures.append("%s.requires_manual_approval_not_true" % item_where)
            if item.get("auto_execute") is not False:
                failures.append("%s.auto_execute_not_false" % item_where)
            if item.get("dispatch_channel") != "none":
                failures.append("%s.dispatch_channel_not_none" % item_where)
    return failures


def contains_credential_material(value) -> bool:
    if isinstance(value, dict):
        for k, v in value.items():
            if str(k).lower() in CREDENTIAL_SHAPED_KEYS:
                return True
            if contains_credential_material(v):
                return True
    elif isinstance(value, list):
        return any(contains_credential_material(v) for v in value)
    elif isinstance(value, str):
        return bool(CREDENTIAL_VALUE_RE.search(value))
    return False


def validate_stage3_suggestions_for_publish(run_dir: Path, run_id: str, suggestions_path: Path):
    data = json.loads(suggestions_path.read_text(encoding="utf-8"))
    failures = []
    if data.get("schema_version") != "3cm_stage3_suggestions_v1":
        failures.append("invalid_schema_version")
    if data.get("source") != "validated_stage2_rca":
        failures.append("source_not_validated_stage2_rca")
    if data.get("stage3_source") != "validated_stage2_rca_only":
        failures.append("stage3_source_not_validated_stage2_rca_only")
    if data.get("run_id") != run_id:
        failures.append("run_id_mismatch")
    validation_path = run_dir / "_server_out" / "stage2_schema_validation.json"
    try:
        validation = json.loads(validation_path.read_text(encoding="utf-8"))
    except Exception as exc:
        validation = {}
        failures.append("stage2_schema_validation_missing_or_invalid:%s" % exc)
    if validation.get("status") != "PASS":
        failures.append("stage2_schema_validation_not_pass")
    if validation.get("accepted_run_id") != run_id or validation.get("input_bundle_run_id") != run_id:
        failures.append("stage2_run_id_binding_mismatch")
    if validation.get("run_id_match") is not True:
        failures.append("stage2_run_id_match_not_true")
    if data.get("legacy_action_files_ignored") is not True:
        failures.append("legacy_action_files_not_marked_ignored")
    if data.get("execution_enabled") is not False:
        failures.append("execution_enabled_not_false")
    if data.get("automatic_recovery_enabled") is not False:
        failures.append("automatic_recovery_enabled_not_false")
    if data.get("action_command_enabled") is not False:
        failures.append("action_command_enabled_not_false")
    if data.get("manual_approval_required") is not True:
        failures.append("manual_approval_required_not_true")
    strategy_failures = validate_action_r1_strategy_safety(data)
    if strategy_failures:
        failures.append("stage3_strategy_safety:" + "|".join(strategy_failures))
    if contains_credential_material(data):
        failures.append("stage3_contains_credential_material")
    if (run_dir / "_server_out" / "actions_device.txt").exists() or (run_dir / "actions_device.txt").exists():
        failures.append("legacy_action_downlink_present")
    if failures:
        raise RuntimeError("stage3_suggestions publish validation failed: " + ",".join(failures))
    return data

def write_status(out_root: Path, device_id: str, status: str) -> None:
    dev = out_root / device_id
    atomic_write(dev / "latest_infer_status.txt", (status + "\n").encode("utf-8"))

def write_error(out_root: Path, device_id: str, reason: str) -> None:
    dev = out_root / device_id
    payload = (reason or "error").encode("utf-8", errors="ignore")
    atomic_write(dev / "latest_error.txt", payload[:MAX_ERROR_BYTES])

def clear_error(out_root: Path, device_id: str) -> None:
    p = out_root / device_id / "latest_error.txt"
    if p.exists():
        try:
            p.unlink()
        except Exception:
            atomic_write(p, b"")

def parse_run_dir(stdout: str):
    for line in (stdout or "").splitlines():
        if line.startswith("run_dir="):
            return line.split("=", 1)[1].strip()
    return None

def tail_text(text: str, max_lines: int = 20, max_bytes: int = 4096) -> str:
    lines = (text or "").splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    out = "\n".join(lines).strip()
    if not out:
        return ""
    data = out.encode("utf-8", errors="ignore")
    if len(data) > max_bytes:
        data = data[-max_bytes:]
        out = data.decode("utf-8", errors="ignore")
    return out

def ensure_fallback_diagnosis(out_dir: Path, stderr_text: str) -> None:
    diag = out_dir / "diagnosis.json"
    try:
        if diag.exists() and diag.stat().st_size > 0:
            return
    except Exception:
        pass
    msg = tail_text(stderr_text) or "infer_failed"
    err_type = "cuda_oom" if "out of memory" in msg.lower() else "infer_failed"
    payload = {
        "ok": False,
        "error": {
            "type": err_type,
            "message": msg,
            "hint": "check infer.stderr for details",
        },
        "when": now_utc(),
        "out_dir": str(out_dir),
    }
    atomic_write(diag, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))

def write_infer_logs(run_dir: Path, stdout: str, stderr: str) -> Path:
    out_dir = run_dir / "_server_out"
    out_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(out_dir / "infer.stdout", (stdout or "").encode("utf-8", errors="ignore"))
    atomic_write(out_dir / "infer.stderr", (stderr or "").encode("utf-8", errors="ignore"))
    return out_dir

def try_acquire_infer_lock(lock_base: Path) -> Path:
    lock = Path(str(lock_base) + ".infer_lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, (now_utc() + "\n").encode("utf-8"))
        os.close(fd)
        return lock
    except FileExistsError:
        return None
    except Exception:
        return None

def run_closed_loop(repo_root: Path, bundle_path: Path, runs_root: Path, run_id: str, ignored_stale_count: int = 0):
    script_candidates = [
        repo_root / "closed_loop_demo" / "server" / "src" / "server_B" / "orchestrator" / "run_closed_loop.py",
        repo_root / "server_B" / "orchestrator" / "run_closed_loop.py",
    ]
    script = next((p for p in script_candidates if p.exists()), script_candidates[0])
    cmd = [sys.executable, str(script), "--bundle", str(bundle_path), "--out_root", str(runs_root)]
    env = os.environ.copy()
    if run_id:
        # Stage1->Stage2 run binding: only the current accepted run_id may drive Stage2
        env["WK_3CM_ACCEPTED_RUN_ID"] = str(run_id)
    env["WK_3CM_IGNORED_STALE_BUNDLES_COUNT"] = str(max(0, int(ignored_stale_count)))
    proc = subprocess.run(cmd, text=True, capture_output=True, env=env)
    rd = parse_run_dir(proc.stdout or "")
    run_dir = Path(rd).expanduser().resolve() if rd else None
    if not run_dir and run_id:
        run_dir = (runs_root / run_id).resolve()
    out_dir = write_infer_logs(run_dir, proc.stdout or "", proc.stderr or "") if run_dir else None
    if proc.returncode != 0:
        if out_dir:
            ensure_fallback_diagnosis(out_dir, proc.stderr or "")
        stderr = (proc.stderr or "").strip()
        raise RuntimeError("run_closed_loop rc=%s stderr=%s" % (proc.returncode, stderr[:1024]))
    return run_dir

def find_actions(run_dir: Path):
    p = run_dir / "_server_out" / "actions_device.txt"
    if p.exists():
        return p
    p = run_dir / "actions_device.txt"
    if p.exists():
        return p
    return None

def find_stage3_suggestions(run_dir: Path):
    p = run_dir / "_server_out" / "stage3_suggestions.json"
    if p.exists():
        return p
    p = run_dir / "stage3_suggestions.json"
    if p.exists():
        return p
    return None

def truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def is_3cm_candidate_mode() -> bool:
    return truthy_env("WK_3CM_CANDIDATE_MODE") or truthy_env("WK_3CM_ACTION_SUGGESTION_ONLY")

def find_diagnosis(run_dir: Path):
    for name in ("diagnosis.json", "diagnosis_v2.json"):
        p = run_dir / "_server_out" / name
        if p.exists():
            return p
        p = run_dir / name
        if p.exists():
            return p
    return None

def extract_conclusion_text(diag_path: Path) -> str:
    """Return a short human-readable conclusion line from diagnosis.json."""
    try:
        raw = diag_path.read_text(encoding="utf-8", errors="ignore")
        d = json.loads(raw)
    except Exception:
        return ""
    if not isinstance(d, dict):
        return ""
    # If inference itself failed, surface the error type
    if not d.get("ok", True) and isinstance(d.get("error"), dict):
        err = d["error"]
        etype = (err.get("type") or "infer_failed")[:60]
        emsg = (err.get("message") or "")[:200]
        return ("infer_error type=%s msg=%s" % (etype, emsg)).strip()
    parts = []
    fault_state = d.get("fault_state") or ""
    if fault_state:
        parts.append("fault_state=%s" % fault_state)
    family = d.get("family") or ""
    if family and family != "other":
        parts.append("family=%s" % family)
    severity = d.get("severity") or ""
    if severity and severity not in ("unknown",):
        parts.append("severity=%s" % severity)
    confidence = d.get("confidence")
    if confidence is not None:
        try:
            parts.append("confidence=%.2f" % float(confidence))
        except Exception:
            pass
    # Prefer narrative > root_cause > hypothesis > summary > reason
    conclusion_text = ""
    for key in ("narrative", "root_cause", "hypothesis", "summary", "reason"):
        val = d.get(key)
        if val and isinstance(val, str) and val.strip():
            conclusion_text = val.strip()
            break
    if conclusion_text:
        # Truncate to 400 chars and collapse newlines to spaces
        conclusion_text = " ".join(conclusion_text.split())[:400]
        parts.append("conclusion=%s" % conclusion_text)
    return " | ".join(parts) if parts else ""

def write_conclusion(out_root: Path, device_id: str, text: str) -> None:
    dev = out_root / device_id
    payload = (text + "\n").encode("utf-8", errors="ignore")
    atomic_write(dev / "latest_conclusion.txt", payload)

def mark_infer(item: Path, status: str, device_id: str = "", run_id: str = "", extra: dict = None):
    payload = {
        "ts_utc": now_utc(),
        "status": status,
    }
    if device_id:
        payload["device_id"] = device_id
    if run_id:
        payload["run_id"] = run_id
    if extra:
        for k, v in extra.items():
            payload[k] = v
    Path(str(item) + ".infer_done").write_text(
        json.dumps(payload, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )

def cleanup_inbox_item(item: Path, delete_infer_done: bool = False) -> None:
    done = Path(str(item) + ".done")
    infer = Path(str(item) + ".infer_done")
    unlink_if_exists(item)
    unlink_if_exists(done)
    if delete_infer_done:
        unlink_if_exists(infer)

def safe_extract_tar(tar: tarfile.TarFile, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    members = []
    for m in tar.getmembers():
        name = (m.name or "").lstrip("/")
        if not name:
            continue
        # basic path traversal guard
        parts = Path(name).parts
        if ".." in parts:
            continue
        # normalize leading "./"
        if name.startswith("./"):
            name = name[2:]
        m.name = name
        members.append(m)
    tar.extractall(dst, members=members)

def unpack_action_result(tar_path: Path, run_dir: Path) -> Path:
    out_dir = run_dir / "_action_result"
    out_dir.mkdir(parents=True, exist_ok=True)
    marker = out_dir / ".unpack_done"
    if marker.exists():
        return out_dir
    with tarfile.open(tar_path, "r:*") as tar:
        safe_extract_tar(tar, out_dir)
    atomic_write(marker, (now_utc() + "\n").encode("utf-8"))
    return out_dir

def cleanup_processed_marks(device_dir: Path, keep_max: int, keep_days: int) -> None:
    marks = []
    for p in device_dir.glob("*.infer_done"):
        try:
            marks.append((p.stat().st_mtime, p))
        except Exception:
            continue
    if not marks:
        return
    marks.sort(key=lambda x: x[0], reverse=True)
    cutoff = None
    if keep_days > 0:
        cutoff = time.time() - (keep_days * 86400)
    for idx, (mtime, mark) in enumerate(marks):
        over = (keep_max > 0 and idx >= keep_max)
        old = (cutoff is not None and mtime < cutoff)
        if not (over or old):
            continue
        base = Path(str(mark)[:-len(".infer_done")])  # -> *.tar.gz
        unlink_if_exists(base)
        unlink_if_exists(Path(str(base) + ".done"))
        unlink_if_exists(mark)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inbox", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--runs_root", required=True)
    ap.add_argument("--poll_sec", type=int, default=2)
    args = ap.parse_args()

    inbox_root = Path(args.inbox).expanduser().resolve()
    out_root = Path(args.out).expanduser().resolve()
    runs_root = Path(args.runs_root).expanduser().resolve()
    repo_root = Path(__file__).resolve().parents[2]

    inbox_root.mkdir(parents=True, exist_ok=True)
    out_root.mkdir(parents=True, exist_ok=True)
    runs_root.mkdir(parents=True, exist_ok=True)

    print("[watcher] inbox=%s out=%s runs=%s" % (inbox_root, out_root, runs_root), flush=True)

    keep_max = int(os.environ.get("WK_TCP_INBOX_KEEP_MAX", "200"))
    keep_days = int(os.environ.get("WK_TCP_INBOX_KEEP_DAYS", "7"))
    cleanup_every = int(os.environ.get("WK_TCP_INBOX_CLEANUP_EVERY", "10"))
    delete_infer_done = (os.environ.get("WK_TCP_INBOX_DELETE_INFER_DONE", "0").strip() == "1")
    loop_n = 0

    while True:
        try:
            for device_dir in sorted(inbox_root.iterdir()):
                if not device_dir.is_dir():
                    continue
                device_id = sanitize_token(device_dir.name)

                # A) done!=ok files are marked as bad bundles to avoid retry loops.
                for done_item in sorted(device_dir.glob("*.done")):
                    base = done_item.with_suffix("")  # remove ".done"
                    infer_mark = Path(str(base) + ".infer_done")
                    if infer_mark.exists():
                        continue
                    done_txt = safe_read_text(done_item)
                    if done_txt.lstrip().startswith("ok"):
                        continue
                    run_id, kind = parse_name(base.name)
                    if not run_id or not kind:
                        mark_infer(base, "skip_bad_name", device_id=device_id, run_id="")
                    else:
                        mark_infer(base, "skip_bad_bundle", device_id=device_id, run_id=run_id, extra={"done_text": done_txt[:200]})

                # B) 鍙鐞嗏€滄渶鏂扮殑涓€涓?ok bundle鈥濓紝鍏朵綑鍘嗗彶 ok bundle 鏍囪 skip_stale锛岄槻姝㈡棫鍖呰鐩?latest
                bundles = []
                action_results = []
                for item in device_dir.iterdir():
                    if item.name.endswith(".tmp") or item.name.endswith(".done") or item.name.endswith(".infer_done"):
                        continue
                    run_id, kind = parse_name(item.name)
                    if not run_id or not kind:
                        continue
                    ready = Path(str(item) + ".done")
                    infer = Path(str(item) + ".infer_done")
                    if not ready.exists() or infer.exists():
                        continue
                    if not safe_read_text(ready).lstrip().startswith("ok"):
                        mark_infer(item, "skip_bad_bundle", device_id=device_id, run_id=run_id)
                        continue
                    if kind == "bundle":
                        bundles.append(item)
                    elif kind == "action_result":
                        action_results.append(item)

                # 鍏堟妸 action_result 閮芥爣璁颁竴涓嬶紙涓嶅奖鍝?latest锛?                for ar in sorted(action_results, key=lambda p: p.stat().st_mtime):
                    try:
                        run_id, _ = parse_name(ar.name)
                        print("[watcher] action_result ready device=%s run_id=%s" % (device_id, run_id), flush=True)
                        run_dir = runs_root / run_id
                        if not run_dir.exists():
                            # 閫氬父涓嶄細鍙戠敓锛坅ction_result 鍦ㄦ帹鐞嗗畬鎴愬悗鎵嶄細鍑虹幇锛夛紝浣嗛亣鍒板氨鍏堜笉澶勭悊锛岀暀寰呬笅娆?                            print("[watcher] action_result wait_run_dir device=%s run_id=%s" % (device_id, run_id), flush=True)
                            continue
                        out_dir = unpack_action_result(ar, run_dir)
                        mark_infer(ar, "ok_action_result_unpacked", device_id=device_id, run_id=run_id, extra={"out_dir": str(out_dir)})
                        cleanup_inbox_item(ar, delete_infer_done=delete_infer_done)
                    except Exception:
                        pass

                if bundles:
                    newest = max(bundles, key=lambda p: p.stat().st_mtime)
                    ignored_stale_count = 0
                    for b in bundles:
                        if b != newest:
                            try:
                                rid, _ = parse_name(b.name)
                                mark_infer(b, "skip_stale", device_id=device_id, run_id=rid)
                                ignored_stale_count += 1
                            except Exception:
                                ignored_stale_count += 1

                    run_id, _ = parse_name(newest.name)
                    lock_path = None
                    try:
                        print("[watcher] bundle ready device=%s run_id=%s ignored_stale=%d" % (device_id, run_id, ignored_stale_count), flush=True)
                        lock_path = try_acquire_infer_lock(newest)
                        if not lock_path:
                            print("[watcher] skip infer: lock exists rid=%s" % run_id, flush=True)
                            continue
                        run_dir = run_closed_loop(repo_root, newest, runs_root, run_id, ignored_stale_count)
                        if not run_dir:
                            run_dir = runs_root / run_id
                        try:
                            procs_dir = run_dir / "procs"
                            has0 = (procs_dir / "pidstat_0.txt").exists()
                            has1 = (procs_dir / "pidstat_1.txt").exists()
                            present = "yes" if (has0 and has1) else "no"
                            print("[watcher] pidstat files present: %s device=%s run_id=%s" % (present, device_id, run_id), flush=True)
                        except Exception:
                            pass
                        if is_3cm_candidate_mode():
                            suggestions_path = find_stage3_suggestions(run_dir)
                            if not suggestions_path:
                                raise FileNotFoundError("stage3_suggestions.json missing for run_id=%s" % run_id)
                            validate_stage3_suggestions_for_publish(run_dir, run_id, suggestions_path)
                            data = suggestions_path.read_bytes()
                            if not data.strip():
                                raise RuntimeError("stage3_suggestions.json empty for run_id=%s" % run_id)
                            write_latest_suggestions(out_root, device_id, run_id, data)
                        else:
                            actions_path = find_actions(run_dir)
                            if not actions_path:
                                raise FileNotFoundError("actions_device.txt missing for run_id=%s" % run_id)
                            data = actions_path.read_bytes()
                            if not data.strip():
                                raise RuntimeError("actions_device.txt empty for run_id=%s" % run_id)
                            write_latest(out_root, device_id, run_id, data)
                        write_status(out_root, device_id, "llm_ok")
                        clear_error(out_root, device_id)
                        diag_path = find_diagnosis(run_dir)
                        if diag_path:
                            conclusion = extract_conclusion_text(diag_path)
                            write_conclusion(out_root, device_id, conclusion)
                            print("[watcher] conclusion device=%s run_id=%s text=%s" % (device_id, run_id, conclusion[:120]), flush=True)
                        mark_infer(newest, "ok", device_id=device_id, run_id=run_id, extra={"run_dir": str(run_dir)})
                        cleanup_inbox_item(newest, delete_infer_done=delete_infer_done)
                        print("[watcher] bundle ok device=%s run_id=%s" % (device_id, run_id), flush=True)
                    except Exception as exc:
                        reason = str(exc)
                        # run_dir exists 杩欑鏄巻鍙查噸澶嶅寘锛氫笉鍐?fallback锛屼笉瑕嗙洊 latest锛屽彧鍋?skip
                        if "run_dir exists:" in reason:
                            mark_infer(newest, "skip_exists_run_dir", device_id=device_id, run_id=run_id, extra={"reason": reason[:512]})
                            # 杩欑閲嶅鍖呬篃娌″繀瑕佺暀鍦?inbox
                            cleanup_inbox_item(newest, delete_infer_done=delete_infer_done)
                            print("[watcher] bundle skip_exists device=%s run_id=%s" % (device_id, run_id), flush=True)
                        else:
                            if is_3cm_candidate_mode():
                                clear_latest_actions(out_root, device_id)
                            else:
                                fallback = ("echo INFER_FAILED device=%s run=%s\n" % (device_id, run_id)).encode("utf-8")
                                write_latest(out_root, device_id, run_id, fallback)
                            write_error(out_root, device_id, reason)
                            write_status(out_root, device_id, "fallback")
                            write_conclusion(out_root, device_id, "infer_error reason=%s" % reason[:300])
                            mark_infer(newest, "error", device_id=device_id, run_id=run_id, extra={"reason": reason[:1024]})
                            print("[watcher] bundle error device=%s run_id=%s reason=%s" % (device_id, run_id, reason), flush=True)

                    finally:
                        if lock_path:
                            unlink_if_exists(lock_path)

        except Exception as loop_exc:
            print("[watcher] loop_error: %s" % loop_exc, flush=True)
        loop_n += 1
        if cleanup_every > 0 and (loop_n % cleanup_every == 0):
            try:
                for device_dir in sorted(inbox_root.iterdir()):
                    if device_dir.is_dir():
                        cleanup_processed_marks(device_dir, keep_max=keep_max, keep_days=keep_days)
            except Exception:
                pass

        time.sleep(max(1, int(args.poll_sec)))

if __name__ == "__main__":
    main()
