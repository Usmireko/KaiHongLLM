#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
closed_loop_infer_run.py
- Input : --run_dir  (a Windows-collected run folder, already uploaded to server)
- Output: --out_dir  (write diagnosis.json / actions.json / logs)
Goal: glue "run folder" -> "LLM inference" -> "actions to execute"
NOTE: codex should adapt this script to your existing infer entrypoints and prompt format.
"""

import argparse
import copy
import csv
import json
import inspect
import os
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

try:
    CLK_TCK = os.sysconf(os.sysconf_names.get("SC_CLK_TCK", "SC_CLK_TCK"))
except Exception:
    CLK_TCK = 100

def read_text_tail(p: Path, max_lines: int = 200) -> str:
    if not p.exists():
        return ""
    try:
        with p.open("r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        return "".join(lines[-max_lines:])
    except Exception:
        return ""

def read_lines_tail(p: Path, max_lines: int = 200) -> List[str]:
    if not p.exists():
        return []
    try:
        with p.open("r", encoding="utf-8", errors="ignore") as f:
            lines = [ln.rstrip("\n") for ln in f.readlines()]
        return lines[-max_lines:]
    except Exception:
        return []

def read_text_tail_bytes(p: Path, max_bytes: int = 120000, max_lines: int = 4000) -> str:
    if not p.exists():
        return ""
    try:
        with p.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            offset = max(0, size - max_bytes)
            f.seek(offset, os.SEEK_SET)
            data = f.read()
        text = data.decode("utf-8", errors="ignore")
        lines = text.splitlines()
        if len(lines) > max_lines:
            lines = lines[-max_lines:]
        return "\n".join(lines)
    except Exception:
        return ""

def atomic_write_text(path: Path, data: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(data, encoding=encoding)
    os.replace(tmp, path)

def atomic_write_json(path: Path, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

def now_utc_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

def try_acquire_infer_lock(out_dir: Path) -> Optional[Path]:
    lock = out_dir / ".infer_lock"
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, (now_utc_iso() + "\n").encode("utf-8"))
        os.close(fd)
        return lock
    except FileExistsError:
        return None
    except Exception:
        return None

def ensure_error_diagnosis(diagnosis: Dict[str, Any], err_type: str, message: str, out_dir: Path) -> Dict[str, Any]:
    if not isinstance(diagnosis, dict):
        diagnosis = {}
    hint = "check infer_error.txt and infer.stderr for details"
    if err_type == "cuda_oom":
        hint = "GPU OOM: reduce batch/seq length or enable low_vram_policy=wait/skip"
    diagnosis["ok"] = False
    diagnosis["error"] = {
        "type": err_type,
        "message": message,
        "hint": hint,
    }
    diagnosis["when"] = now_utc_iso()
    diagnosis["out_dir"] = str(out_dir)
    if not diagnosis.get("summary"):
        diagnosis["summary"] = f"{err_type}: {message}"
    if not diagnosis.get("reason"):
        diagnosis["reason"] = message
    return diagnosis

def sanitize_llm_text(text: str) -> str:
    """
    Keep content, only remove tag wrappers like <think>...</think>.
    (Some models wrap the whole answer in <think>, we must not drop it.)
    """
    if not text:
        return ""
    # remove only the tags, keep inner content
    text = re.sub(r"</?\s*think\s*>", "", text, flags=re.IGNORECASE)
    # be defensive for other wrappers
    text = re.sub(r"</?\s*analysis\s*>", "", text, flags=re.IGNORECASE)
    return text.strip()
def sanitize_meta_for_llm(meta: Dict[str, Any]) -> Dict[str, Any]:
    """
    Remove fields that can leak labels / scripted scenario info into LLM input.
    Keep only operational metadata needed for context.
    """
    if not isinstance(meta, dict):
        return {}

    drop_keys = {
        "scenario_tag",
        "fault_type",
        "family",
        "severity",
        "gt_family",
        "gt_severity",
        "gt_fault",
        "gt_label",
        "labels",
        "scenario",
        "obs_primary",
        "obs_fault",
    }

    out: Dict[str, Any] = {}
    for k, v in meta.items():
        if not isinstance(k, str):
            continue
        ks = k.strip()
        # drop anything starting with obs_ (obs_fault_state/obs_cpu_hotspot/obs_mem_pressure ...)
        if ks.startswith("obs_"):
            continue
        if ks in drop_keys:
            continue
        out[ks] = v

    return out

def sanitize_event_msg_for_llm(msg: Any) -> str:
    """
    Remove label-leaking tokens from events (e.g. cli poke / run_end containing scenario tag).
    Keep as much useful context as possible.
    """
    if msg is None:
        return ""
    s = str(msg)

    # If it's a cli poke style line, it often embeds scenario_tag/fault_type twice
    # Example: "run_end:...:cpu_busy_loop:cpu_busy_loop"
    if "poke" in s or "run_end" in s or "run_begin" in s:
        # redact common scenario-like tokens (cpu_*/mem_*/bg_*/net_*/background_*)
        s = re.sub(r"\b(cpu|mem|bg|net|background)_[A-Za-z0-9_]+\b", "<redacted_scenario>", s)
        # redact obs_* tokens if any
        s = re.sub(r"\bobs_[A-Za-z0-9_]+\b", "<redacted_obs>", s)

        # also redact " :xxx:xxx " tail patterns conservatively
        # keep first two ':' groups, redact later groups
        parts = s.split(":")
        if len(parts) >= 6:
            s = ":".join(parts[:4] + ["<redacted>"] * (len(parts) - 4))

    return s

def event_has_label_leak(ev: Any) -> bool:
    if not isinstance(ev, dict):
        return False
    for k, v in ev.items():
        if isinstance(k, str):
            if "obs_" in k or k in ("scenario_tag", "fault_type"):
                return True
        if isinstance(v, str):
            if "obs_" in v or "scenario_tag" in v or "fault_type" in v:
                return True
    return False

def redact_label_leaks(text: str) -> str:
    if not text:
        return text
    s = str(text)
    s = re.sub(r"\bobs_[A-Za-z0-9_]+\b", "<redacted_obs>", s)
    s = s.replace("scenario_tag", "<redacted_meta>")
    s = s.replace("fault_type", "<redacted_meta>")
    return s

def query_gpu_mem() -> Tuple[Optional[Dict[str, int]], Optional[str]]:
    try:
        cmd = ["nvidia-smi", "--query-gpu=memory.free,memory.used,memory.total", "--format=csv,noheader,nounits"]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            return None, (res.stderr.strip() or res.stdout.strip() or f"nvidia-smi exit={res.returncode}")
        line = (res.stdout.strip().splitlines() or [""])[0]
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            return None, f"unexpected nvidia-smi output: {line}"
        free_mib = int(parts[0])
        used_mib = int(parts[1])
        total_mib = int(parts[2])
        return {"free_mib": free_mib, "used_mib": used_mib, "total_mib": total_mib}, None
    except Exception as exc:
        return None, repr(exc)
def wait_for_gpu(min_free_mib: int,
                 poll_sec: int,
                 max_wait_sec: int,
                 log_fn) -> bool:
    """
    Wait until GPU free memory >= min_free_mib.
    - poll_sec: check interval
    - max_wait_sec: 0 or <0 means wait forever
    - log_fn: logger callback
    Requirement: print used/total once per minute while waiting.
    """
    start = time.time()
    last_min_log = 0.0
    while True:
        info, err = query_gpu_mem()
        now = time.time()
        waited = int(now - start)

        if info is None:
            log_fn(f"[gpu_wait] nvidia-smi unavailable: {err}. abort wait.")
            return False

        free_mib = info.get("free_mib", -1)
        used_mib = info.get("used_mib", -1)
        total_mib = info.get("total_mib", -1)

        if free_mib >= min_free_mib:
            log_fn(f"[gpu_wait] ready: free_mib={free_mib} used_mib={used_mib} total_mib={total_mib} (need>={min_free_mib}) waited_sec={waited}")
            return True

        # every minute emit used/total
        if waited // 60 > int(last_min_log):
            last_min_log = waited // 60
            log_fn(f"[gpu_wait] waiting... used_mib={used_mib} total_mib={total_mib} free_mib={free_mib} need_free>={min_free_mib} waited_sec={waited}")

        if max_wait_sec and max_wait_sec > 0 and waited >= max_wait_sec:
            log_fn(f"[gpu_wait] timeout: used_mib={used_mib} total_mib={total_mib} free_mib={free_mib} need_free>={min_free_mib} waited_sec={waited}")
            return False

        time.sleep(max(1, int(poll_sec)))

def build_collect_actions() -> List[Dict[str, Any]]:
    cmds = [
        "dmesg | tail -n 200",
        "cat /proc/loadavg",
        "cat /proc/meminfo | head -n 40",
        "ps -A | head -n 80",
        "top -n 1 | head -n 80",
    ]
    seen = set()
    actions = []
    for cmd in cmds:
        if cmd in seen:
            continue
        seen.add(cmd)
        actions.append({
            "type": "collect",
            "target": "device",
            "cmd": cmd,
            "timeout_sec": 20,
            "risk": "low",
            "why": "fallback_collect",
        })
    return actions

def build_fallback_result(reason_flag: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    diagnosis = {
        "schema_version": 1,
        "fault_state": "unknown",
        "family": "other",
        "severity": "unknown",
        "root_cause": "",
        "evidence": [],
        "evidence_text": [],
        "confidence": 0.0,
        "risk_flags": [reason_flag],
    }
    actions = {"schema_version": 1, "actions": build_collect_actions()}
    return diagnosis, actions
def rewrite_actions_why(actions_obj: Dict[str, Any], old: str, new: str) -> None:
    acts = actions_obj.get("actions")
    if not isinstance(acts, list):
        return
    for a in acts:
        if isinstance(a, dict) and a.get("why") == old:
            a["why"] = new

def append_risk_flag(diagnosis: Dict[str, Any], flag: str) -> None:
    if not flag:
        return
    flags = diagnosis.get("risk_flags")
    if not isinstance(flags, list):
        flags = []
    if flag not in flags:
        flags.append(flag)
    diagnosis["risk_flags"] = flags

def build_suspect_processes(candidates: List[Dict[str, Any]], limit: int = 5) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in candidates[:limit]:
        pid = item.get("pid")
        name = item.get("name") or item.get("comm") or item.get("cmd")
        cmd = item.get("cmd") or item.get("comm") or item.get("name")
        out.append({
            "pid": pid,
            "name": name,
            "cmd": cmd,
            "rss_kb": item.get("rss_kb"),
            "stat": item.get("stat"),
            "source": item.get("source") or "procs",
        })
    return [x for x in out if x.get("pid") is not None]

def normalize_evidence_items(evidence: Any) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    if isinstance(evidence, list):
        for e in evidence:
            if isinstance(e, dict):
                text = e.get("text")
                if text:
                    items.append({
                        "text": str(text).strip(),
                        "source": e.get("source") or "unknown",
                        "gaps": e.get("gaps") or [],
                    })
            elif isinstance(e, str) and e.strip():
                items.append({"text": e.strip(), "source": "llm_summary", "gaps": []})
    return items

def build_suspects_list(candidate_processes: List[Dict[str, Any]],
                        primary_suspect: Optional[Dict[str, Any]],
                        secondary_suspects: List[Dict[str, Any]],
                        limit: int = 5) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    seen: set = set()

    def add_item(item: Dict[str, Any], role: str) -> None:
        if not isinstance(item, dict):
            return
        pid = item.get("pid")
        if pid is None or pid in seen:
            return
        seen.add(pid)
        name = item.get("name") or item.get("comm") or item.get("cmd")
        cpu_delta = item.get("cpu_delta_jiffies")
        cpu_pct = item.get("cpu_pct")
        evidence_missing: List[str] = []
        if cpu_delta is None:
            evidence_missing.append("pidstat_missing")
        if cpu_delta is not None and cpu_pct is None:
            evidence_missing.append("pidstat_interval_missing")
        evidence_ok = len(evidence_missing) == 0
        out.append({
            "pid": pid,
            "name": name,
            "role": role,
            "cpu_pct": cpu_pct,
            "rss_delta_kb": item.get("rss_delta_kb"),
            "score": item.get("score"),
            "evidence_ok": evidence_ok,
            "evidence_missing": evidence_missing,
        })

    if primary_suspect:
        add_item(primary_suspect, "primary")
    for sec in secondary_suspects or []:
        add_item(sec, "secondary")
    for cand in candidate_processes or []:
        add_item(cand, "candidate")
        if len(out) >= limit:
            break
    return out

def extract_next_checks(actions_obj: Dict[str, Any], limit: int = 4) -> List[str]:
    lines: List[str] = []
    acts = actions_obj.get("actions") if isinstance(actions_obj, dict) else None
    if isinstance(acts, list):
        for a in acts:
            if not isinstance(a, dict):
                continue
            cmd = a.get("cmd") or a.get("what") or a.get("name") or a.get("action")
            if cmd:
                lines.append(str(cmd).strip())
            if len(lines) >= limit:
                break
    if not lines:
        lines.append("补采 pidstat/procs 等进程级证据")
    return lines

def _truncate_text(value: Any, max_len: int) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if max_len <= 0 or len(text) <= max_len:
        return text
    return text[:max_len] + "..."

def _limit_list(value: Any, max_items: int) -> List[Any]:
    if not isinstance(value, list):
        return []
    if max_items <= 0:
        return []
    return value[:max_items]

def _compact_suspect_item(item: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if not isinstance(item, dict):
        return out
    name = item.get("name") or item.get("comm") or item.get("cmd")
    if name:
        out["name"] = _truncate_text(name, 60)
    pid = item.get("pid")
    if pid is not None:
        out["pid"] = pid
    if item.get("role"):
        out["role"] = _truncate_text(item.get("role"), 40)
    if item.get("cpu_pct") is not None:
        out["cpu_pct"] = item.get("cpu_pct")
    if item.get("rss_delta_kb") is not None:
        out["rss_delta_kb"] = item.get("rss_delta_kb")
    if item.get("score") is not None:
        out["score"] = item.get("score")
    if "evidence_ok" in item:
        out["evidence_ok"] = item.get("evidence_ok")
    gaps = item.get("evidence_missing")
    if isinstance(gaps, list) and gaps:
        out["evidence_missing"] = [_truncate_text(g, 40) for g in gaps[:3]]
    return out

def compact_diagnosis(diag: Dict[str, Any], actions_obj: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not isinstance(diag, dict):
        diag = {}
    compact: Dict[str, Any] = {
        "schema_version": 1,
        "fault_state": diag.get("fault_state", "unknown"),
        "family": diag.get("family") or diag.get("fault_family") or "other",
        "severity": diag.get("severity", "unknown"),
        "confidence": diag.get("confidence", 0.0),
    }

    if "ok" in diag:
        compact["ok"] = diag.get("ok")
    if isinstance(diag.get("error"), dict):
        err = diag.get("error") or {}
        compact["error"] = {
            "type": _truncate_text(err.get("type"), 60),
            "message": _truncate_text(err.get("message"), 200),
            "hint": _truncate_text(err.get("hint"), 200),
        }

    root_cause = diag.get("root_cause") or diag.get("hypothesis") or diag.get("summary") or diag.get("reason") or ""
    compact["root_cause"] = _truncate_text(root_cause, 500)

    for key in ("summary", "reason", "hypothesis"):
        val = _truncate_text(diag.get(key), 500)
        if val:
            compact[key] = val

    evidence_items = normalize_evidence_items(diag.get("evidence"))
    compact_evidence: List[Dict[str, Any]] = []
    for e in evidence_items[:8]:
        text = _truncate_text(e.get("text"), 200)
        if not text:
            continue
        item = {"text": text}
        src = _truncate_text(e.get("source"), 60)
        if src:
            item["source"] = src
        gaps = e.get("gaps")
        if isinstance(gaps, list) and gaps:
            item["gaps"] = [_truncate_text(g, 40) for g in gaps[:3]]
        compact_evidence.append(item)
    if compact_evidence:
        compact["evidence"] = compact_evidence

    next_checks: List[str] = []
    if isinstance(actions_obj, dict):
        next_checks = extract_next_checks(actions_obj, limit=8)
    elif isinstance(diag.get("next_checks"), list):
        next_checks = [str(x) for x in diag.get("next_checks") if x]
    if next_checks:
        compact["next_checks"] = [_truncate_text(x, 200) for x in next_checks[:8] if x]

    suspects_raw: List[Dict[str, Any]] = []
    if isinstance(diag.get("top_suspects"), list):
        suspects_raw = diag.get("top_suspects") or []
    elif isinstance(diag.get("suspects"), list):
        suspects_raw = diag.get("suspects") or []
    else:
        if isinstance(diag.get("primary_suspect"), dict):
            suspects_raw.append(diag.get("primary_suspect"))
        if isinstance(diag.get("secondary_suspects"), list):
            suspects_raw.extend(diag.get("secondary_suspects") or [])

    top_suspects: List[Dict[str, Any]] = []
    seen: set = set()
    for s in suspects_raw:
        if not isinstance(s, dict):
            continue
        pid = s.get("pid")
        if pid is not None:
            if pid in seen:
                continue
            seen.add(pid)
        item = _compact_suspect_item(s)
        if item:
            top_suspects.append(item)
        if len(top_suspects) >= 5:
            break
    if top_suspects:
        compact["top_suspects"] = top_suspects

    risk_flags = _limit_list(diag.get("risk_flags"), 8)
    if risk_flags:
        compact["risk_flags"] = [_truncate_text(x, 60) for x in risk_flags if x]

    return compact

def build_diagnosis_narrative(observations: List[str],
                              hypothesis: str,
                              evidence_items: List[Dict[str, Any]],
                              next_checks: List[str]) -> str:
    obs = [o for o in observations if o]
    evs = [e for e in evidence_items if isinstance(e, dict) and e.get("text")]
    if len(evs) < 2:
        evs.append({"text": "证据链不足，部分指标/日志缺失，结论存在不确定性", "source": "system", "gaps": ["evidence_insufficient"]})
    evs = evs[:5]
    nxt = [n for n in next_checks if n]

    lines: List[str] = []
    lines.append("Observation:")
    if obs:
        for o in obs:
            lines.append(f"- {o}")
    else:
        lines.append("- (暂无可用观测摘要)")

    lines.append("Hypothesis:")
    lines.append(f"- {hypothesis}" if hypothesis else "- (当前证据不足，无法形成明确根因假设)")

    lines.append("Evidence:")
    for e in evs:
        gaps = e.get("gaps") or []
        gap_text = ("; 缺口=" + ",".join(gaps)) if gaps else ""
        lines.append(f"- {e.get('text')}{gap_text} (source={e.get('source') or 'unknown'})")

    lines.append("NextChecks:")
    if nxt:
        for n in nxt:
            lines.append(f"- {n}")
    else:
        lines.append("- (暂无建议动作)")
    return "\n".join(lines)

def inject_process_candidates(diagnosis: Dict[str, Any],
                              candidate_processes: List[Dict[str, Any]],
                              primary_suspect: Optional[Dict[str, Any]],
                              secondary_suspects: List[Dict[str, Any]]) -> None:
    if candidate_processes:
        diagnosis["candidate_processes"] = candidate_processes
    if primary_suspect:
        diagnosis["primary_suspect"] = primary_suspect
    if secondary_suspects:
        diagnosis["secondary_suspects"] = secondary_suspects

    suspects = build_suspect_processes(candidate_processes, limit=5) if candidate_processes else []
    if suspects:
        diagnosis["suspect_processes"] = suspects

def is_cuda_oom(msg: str) -> bool:
    if not msg:
        return False
    low = msg.lower()
    return "cuda out of memory" in low or "out of memory" in low

def is_torch_oom(exc: BaseException) -> bool:
    try:
        import torch
        return isinstance(exc, torch.cuda.OutOfMemoryError)
    except Exception:
        return False

def load_first_system_prompt(path: Path) -> str:
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                line = f.readline().strip()
            if line:
                obj = json.loads(line)
                messages = obj.get("messages") or []
                for msg in messages:
                    if msg.get("role") == "system":
                        content = msg.get("content", "")
                        if content:
                            return content
        except Exception:
            pass
    # fallback (match training prompt style)
    return (
        "You are a KaiHongOS/OpenHarmony system fault diagnosis assistant.\n"
        "Use metrics, process snapshots (ps), kernel logs (dmesg), and app logs (hilog) as evidence.\n"
        "Decide whether the run is faulty, its family (cpu/mem/background/other), and provide concise root cause and suggestions.\n"
        "Answer clearly and briefly.\n"
    )

DEFAULT_SYSTEM_PROMPT = (
    "You are an OS fault diagnosis and self-healing assistant.\n"
    "Base conclusions only on provided metrics/events/procs/dmesg/hilog evidence; do not use scenario tags or obs_* fields as evidence.\n"
)

def load_system_prompt_safe(path: Path) -> str:
    try:
        s = load_first_system_prompt(path)
        if s and s.strip():
            return s
    except Exception:
        pass
    return DEFAULT_SYSTEM_PROMPT

def parse_label_kv(labels: List[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for item in labels or []:
        if "=" not in item:
            continue
        key, val = item.split("=", 1)
        key = key.strip()
        val = val.strip()
        if key and key not in out:
            out[key] = val
    return out

def parse_dotnet_date(val: Any) -> Optional[int]:
    if not val:
        return None
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, str):
        digits = "".join(ch for ch in val if ch.isdigit())
        if digits:
            try:
                return int(digits)
            except ValueError:
                return None
    return None

def build_model_supports_device_map(fn: Any) -> bool:
    try:
        sig = inspect.signature(fn)
    except Exception:
        return False
    if "device_map" in sig.parameters:
        return True
    for param in sig.parameters.values():
        if param.kind == param.VAR_KEYWORD:
            return True
    return False

def load_metrics_csv(metrics_path: Path) -> Tuple[List[Dict[str, Any]], List[str]]:
    if not metrics_path.exists():
        return [], []
    rows: List[Dict[str, Any]] = []
    try:
        with metrics_path.open("r", encoding="utf-8", errors="ignore") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
            fields = reader.fieldnames or []
    except Exception:
        return [], []
    return rows, list(fields)

def safe_int(val: Any) -> Optional[int]:
    if val is None:
        return None
    try:
        return int(float(val))
    except Exception:
        return None

def parse_proc_snapshot_lines(proc_lines: List[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for ln in proc_lines:
        parts = ln.split()
        if len(parts) < 5:
            continue
        pid = safe_int(parts[0])
        ppid = safe_int(parts[1])
        stat = parts[2]
        rss_kb = safe_int(parts[3])
        comm = " ".join(parts[4:])
        if pid is None:
            continue
        out.append({
            "pid": pid,
            "ppid": ppid,
            "stat": stat,
            "rss_kb": rss_kb,
            "comm": comm,
        })
    return out

def parse_proc_stat_raw(raw: str) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    l = raw.find("(")
    r = raw.rfind(")")
    if l < 0 or r < 0 or r <= l:
        return None
    pid_part = raw[:l].strip()
    pid_val = None
    if pid_part:
        pid_val = safe_int(pid_part.split()[0])
    comm = raw[l + 1:r]
    rest = raw[r + 2:].split()
    if len(rest) < 13:
        return None
    stat = rest[0]
    utime = safe_int(rest[11])
    stime = safe_int(rest[12])
    return {
        "pid": pid_val,
        "comm": comm,
        "stat": stat,
        "utime": utime,
        "stime": stime,
    }

def parse_pidstat_file(path: Path) -> Tuple[Dict[int, Dict[str, Any]], Optional[int]]:
    data: Dict[int, Dict[str, Any]] = {}
    t_ms: Optional[int] = None
    if not path.exists():
        return data, t_ms
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return data, t_ms
    for ln in lines:
        if not ln:
            continue
        if ln.startswith("#"):
            m = re.search(r"t_ms=(\d+)", ln)
            if m:
                t_ms = safe_int(m.group(1))
            continue
        ln = ln.strip()
        if not ln:
            continue
        pid_str = None
        raw = ln
        if " " in ln:
            pid_str, rest = ln.split(" ", 1)
            if pid_str.isdigit():
                raw = rest.strip()
            else:
                pid_str = None
        info = parse_proc_stat_raw(raw or ln)
        if not info:
            continue
        pid_val = safe_int(pid_str) if pid_str else info.get("pid")
        if pid_val is None:
            continue
        info["pid"] = pid_val
        data[pid_val] = info
    return data, t_ms

def build_process_evidence(candidates: List[Dict[str, Any]], top_n: int = 8) -> List[str]:
    lines: List[str] = []
    if not candidates:
        return lines
    for item in candidates[:top_n]:
        name = item.get("name") or item.get("comm") or item.get("cmd") or "proc"
        parts = []
        pid = item.get("pid")
        if pid is not None:
            parts.append(f"pid={pid}")
        rss_kb = item.get("rss_kb")
        if rss_kb is not None:
            parts.append(f"rss_kb={rss_kb}")
        stat = item.get("stat")
        if stat:
            parts.append(f"stat={stat}")
        cpu_delta = item.get("cpu_delta_jiffies")
        if cpu_delta is not None:
            parts.append(f"cpu_delta_jiffies={cpu_delta}")
        cpu_pct = item.get("cpu_pct")
        if cpu_pct is not None:
            parts.append(f"cpu_pct={cpu_pct}")
        line = f"  - {name}(" + ", ".join(parts) + ")"
        lines.append(line)
    return lines

def compute_metrics_window(rows: List[Dict[str, Any]], start_ms: Optional[int], end_ms: Optional[int]) -> List[Dict[str, Any]]:
    if not rows:
        return []
    if start_ms is None or end_ms is None or start_ms <= 0 or end_ms <= 0:
        return rows
    windowed = []
    for row in rows:
        ts = safe_int(row.get("ts_ms"))
        if ts is None:
            continue
        if start_ms <= ts <= end_ms:
            windowed.append(row)
    return windowed if windowed else rows

def calc_stats(values: List[Optional[int]]) -> Dict[str, Optional[int]]:
    vals = [v for v in values if v is not None]
    if not vals:
        return {"min": None, "max": None}
    return {"min": min(vals), "max": max(vals)}

def build_user_message(run_id: str,
                       meta: Dict[str, Any],
                       labels: Dict[str, str],
                       metrics_rows: List[Dict[str, Any]],
                       events: List[Dict[str, Any]],
                       process_candidates: List[Dict[str, Any]],
                       dmesg_lines: List[str],
                       hilog_lines: List[str],
                       run_window_start_ms: Optional[int],
                       run_window_end_ms: Optional[int]) -> str:
    lines: List[str] = []

    lines.append(f"[run_id] {run_id}")
    lines.append(f"[script_version] {meta.get('script_version')}")
    lines.append(f"[run_window_source] {meta.get('run_window_source')}")
    lines.append(
        f"[run_window_board_ms] start={meta.get('run_window_board_ms_start')}, end={meta.get('run_window_board_ms_end')}"
    )

    lines.append("[NOTE] Use only metrics/events/procs/dmesg/hilog evidence; do not use scenario tags or obs_* fields.")

    # metrics window and summary
    if metrics_rows:
        lines.append("[metrics window]")
        lines.append(f"  start_ms={run_window_start_ms}, end_ms={run_window_end_ms}, rows={len(metrics_rows)}")

        load1 = [safe_int(r.get('load1_x100')) for r in metrics_rows]
        cpu = [safe_int(r.get('cpu_util_total_x100')) for r in metrics_rows]
        mem_free = [safe_int(r.get('mem_free_kb')) for r in metrics_rows]
        mem_avail = [safe_int(r.get('mem_available_kb')) for r in metrics_rows]

        load_stats = calc_stats(load1)
        cpu_stats = calc_stats(cpu)
        mem_free_stats = calc_stats(mem_free)
        mem_avail_stats = calc_stats(mem_avail)
        mem_avail_drop = None
        if mem_avail_stats['min'] is not None and mem_avail_stats['max'] is not None:
            mem_avail_drop = mem_avail_stats['max'] - mem_avail_stats['min']

        lines.append('[metrics summary]')
        lines.append(f"  load1_peak_x100={load_stats['max']}")
        lines.append(f"  cpu_util_peak_x100={cpu_stats['max']}")
        lines.append(
            f"  mem_available_kb: min={mem_avail_stats['min']} max={mem_avail_stats['max']} drop_kb={mem_avail_drop}"
        )
        lines.append(f"  mem_free_kb: min={mem_free_stats['min']} max={mem_free_stats['max']}")

        # sampled points
        lines.append('[metrics samples] (relative seconds, mem_available_kb, load1_x100, cpu_util_total_x100)')
        start_ms = run_window_start_ms or (safe_int(metrics_rows[0].get('ts_ms')) if metrics_rows else None)
        for row in metrics_rows[:16]:
            ts = safe_int(row.get('ts_ms'))
            rel_sec = None
            if ts is not None and start_ms is not None:
                rel_sec = round((ts - start_ms) / 1000.0, 1)
            t_str = f"+{rel_sec}s" if rel_sec is not None else str(ts)
            lines.append(
                "  t={t}, mem_available_kb={ma}, load1_x100={l1}, cpu_util_total_x100={cpu}".format(
                    t=t_str,
                    ma=row.get('mem_available_kb'),
                    l1=row.get('load1_x100'),
                    cpu=row.get('cpu_util_total_x100'),
                )
            )
    else:
        lines.append('[metrics] no valid metrics rows')

    # events summary (filter obs_ / scenario_tag / fault_type leakage)
    safe_events = [ev for ev in events if not event_has_label_leak(ev)]
    if safe_events:
        total = len(safe_events)
        tag_counts: Dict[str, int] = {}
        for ev in safe_events:
            tag = ev.get('tag') or 'unknown'
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
        lines.append('[events summary]')
        lines.append(
            '  total={total}, cpu_hotspot={cpu_hotspot}, mem_pressure={mem_pressure}, io_pressure={io_pressure}'.format(
                total=total,
                cpu_hotspot=tag_counts.get('cpu_hotspot', 0),
                mem_pressure=tag_counts.get('mem_pressure', 0),
                io_pressure=tag_counts.get('io_pressure', 0),
            )
        )
        lines.append(f"  tag_counts={tag_counts}")
        lines.append('[events samples] (truncated)')
        for ev in safe_events[:8]:
            lines.append(
                '  ts={ts}, level={level}, component={component}, tag={tag}, msg={msg}'.format(
                    ts=ev.get('ts'),
                    level=ev.get('level'),
                    component=ev.get('component'),
                    tag=ev.get('tag'),
                    msg=redact_label_leaks(ev.get('msg')),
                )
            )
    else:
        lines.append('[events] none')

    # process evidence (structured candidates only; avoid raw ps/top lines)
    proc_lines = build_process_evidence(process_candidates, top_n=8)
    if proc_lines:
        lines.append('[PROCESS_EVIDENCE] (procs snapshot + pidstat delta)')
        lines.extend(proc_lines)
    else:
        lines.append('[PROCESS_EVIDENCE] (no usable process candidates)')

    if dmesg_lines:
        lines.append('[dmesg excerpt] (truncated)')
        for ln in dmesg_lines[:20]:
            lines.append('  ' + redact_label_leaks(ln))
    if hilog_lines:
        lines.append('[hilog excerpt] (truncated)')
        for ln in hilog_lines[:20]:
            lines.append('  ' + redact_label_leaks(ln))

    lines.append('')
    lines.append('Please answer:')
    lines.append('1) Is this run faulty? If yes, which family (cpu/mem/background/other)?')
    lines.append('2) 2-4 root-cause evidence items (cite metrics/events/processes)')
    lines.append('3) 1-2 actionable checks or fixes')
    lines.append('4) Confidence (0-1)')
    lines.append('Primary_suspect must include pid and must be selected from PROCESS_EVIDENCE; do not invent pids or processes.')
    lines.append('root_cause should cite evidence (metrics + PROCESS_EVIDENCE), but does not need to force pid= format.')
    return "\n".join(lines)
def parse_summary_to_struct(summary: str, fallback_severity: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    fault_state = "unknown"
    family = "other"
    confidence = 0.0
    root_lines: List[str] = []
    action_lines: List[str] = []
    risk_flags: List[str] = []

    def _split_kv(line: str) -> Tuple[Optional[str], Optional[str]]:
        for sep in (":", "："):
            if sep in line:
                k, v = line.split(sep, 1)
                return k.strip(), v.strip()
        return None, None

    lines = [ln.strip() for ln in summary.splitlines() if ln.strip()]
    section = None
    for ln in lines:
        if re.match(r"^1[\.|、)]", ln):
            section = "judge"
            continue
        if re.match(r"^2[\.|、)]", ln):
            section = "root"
            continue
        if re.match(r"^3[\.|、)]", ln):
            section = "actions"
            continue
        if re.match(r"^4[\.|、)]", ln):
            section = "confidence"
            m = re.search(r"(confidence|conf)\s*[:=]\s*([01](?:\.\d+)?)", ln, re.IGNORECASE)
            if m:
                try:
                    confidence = float(m.group(2))
                except Exception:
                    pass
            continue

        lower = ln.lower()
        if "fault_state" in lower or "fault state" in lower or "state" in lower:
            _, val = _split_kv(ln)
            if val:
                val_l = val.lower()
                if "fault" in val_l or "abnormal" in val_l or "anomaly" in val_l:
                    fault_state = "fault"
                elif "normal" in val_l:
                    fault_state = "normal"
        if "family" in lower or "fault_family" in lower:
            _, val = _split_kv(ln)
            if val:
                family = val.strip().split()[0].lower()
        if "confidence" in lower:
            _, val = _split_kv(ln)
            if val:
                try:
                    confidence = float(val)
                except Exception:
                    pass

        if section == "root" and ln.startswith("-"):
            root_lines.append(ln.lstrip("-").strip())
        elif section == "actions" and ln.startswith("-"):
            action_lines.append(ln.lstrip("-").strip())

    summary_lower = summary.lower()
    if fault_state == "unknown":
        if "fault" in summary_lower or "abnormal" in summary_lower or "anomaly" in summary_lower:
            fault_state = "fault"
        elif "normal" in summary_lower:
            fault_state = "normal"

    if family == "other":
        if "cpu" in summary_lower:
            family = "cpu"
        elif "mem" in summary_lower or "memory" in summary_lower:
            family = "mem"
        elif "background" in summary_lower or "bg" in summary_lower:
            family = "background"
        elif "net" in summary_lower or "network" in summary_lower:
            family = "net"
        elif "io" in summary_lower:
            family = "io"

    root_cause = " ".join(root_lines).strip()
    evidence_items = [{"text": ln, "source": "llm_summary", "gaps": []} for ln in root_lines if ln]
    diagnosis = {
        "schema_version": 1,
        "fault_state": fault_state,
        "family": family,
        "severity": fallback_severity or "unknown",
        "root_cause": root_cause,
        "evidence": evidence_items,
        "evidence_text": root_lines,
        "confidence": confidence,
        "risk_flags": risk_flags,
    }

    notes = {
        "schema_version": 1,
        "actions_manual": action_lines,
        "summary": summary.strip(),
    }
    return diagnosis, notes
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run_dir", required=True)
    ap.add_argument("--out_dir", required=True)
    ap.add_argument("--min_free_mib", type=int, default=None)
    ap.add_argument("--min_free_mib_stage2", type=int, default=None)
    ap.add_argument("--low_vram_wait_sec", type=int, default=None)
    ap.add_argument("--low_vram_policy", type=str, default=None, choices=["skip", "try", "wait"])
    ap.add_argument("--enable_stage2", type=int, default=None, choices=[0, 1])

    ap.add_argument("--wait_poll_sec", type=int, default=None)
    ap.add_argument("--wait_max_sec", type=int, default=None)
    ap.add_argument("--stage2_wait_poll_sec", type=int, default=None)
    ap.add_argument("--stage2_wait_max_sec", type=int, default=None)
    ap.add_argument("--stage2_tail_bytes", type=int, default=None)
    ap.add_argument("--stage2_tail_lines", type=int, default=None)
    args = ap.parse_args()

    run_dir = Path(args.run_dir).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    infer_log = out_dir / "infer.log"
    infer_ec = out_dir / "infer_ec.txt"
    raw_out = out_dir / "raw_model_output.txt"
    notes_path = out_dir / "notes.json"
    diagnosis_v2_path = out_dir / "diagnosis_v2.json"
    actions_v2_path = out_dir / "actions_v2.json"
    notes_v2_path = out_dir / "notes_v2.json"

    def log(msg: str) -> None:
        ts = datetime.utcnow().isoformat() + "Z"
        line = f"[{ts}] {msg}"
        try:
            with infer_log.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
        print(line, flush=True)

    lock_path = try_acquire_infer_lock(out_dir)
    if not lock_path:
        msg = f"[closed_loop] skip: infer lock exists out_dir={out_dir}"
        try:
            with infer_log.open("a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except Exception:
            pass
        print(msg, flush=True)
        return

    meta_path = run_dir / "_run_meta.json"
    dmesg_after = run_dir / "dmesg_after.utf8.log"
    hilog_full = run_dir / "hilog_text_full.log"
    metrics_dir = run_dir / "metrics"
    events_dir = run_dir / "events"
    procs_dir = run_dir / "procs"

    payload = {
        "ts": datetime.utcnow().isoformat() + "Z",
        "run_dir": str(run_dir),
        "run_id": run_dir.name,
        "has_run_meta": meta_path.exists(),
    }

    meta = None
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        payload["meta"] = {
            "scenario_tag": meta.get("scenario_tag"),
            "fault_type": meta.get("fault_type"),
            "labels": meta.get("labels", []),
            "obs_fault_state": meta.get("obs_fault_state"),
            "metrics_summary": meta.get("metrics_summary"),
            "obs_multi": meta.get("obs_multi"),
        }
    else:
        meta = {}

    exit_code = 0
    diagnosis = {
        "schema_version": 1,
        "fault_state": "unknown",
        "family": "other",
        "severity": "unknown",
        "root_cause": "",
        "evidence": [],
        "evidence_text": [],
        "confidence": 0.0,
        "risk_flags": ["inference_not_run"],
    }
    actions = {"schema_version": 1, "actions": []}
    notes = {"schema_version": 1, "actions_manual": [], "summary": ""}
    diagnosis_v2 = {
        "schema_version": 1,
        "fault_state": "unknown",
        "family": "other",
        "severity": "unknown",
        "root_cause": "",
        "evidence": [],
        "evidence_text": [],
        "confidence": 0.0,
        "risk_flags": ["v2_inference_not_run"],
    }
    actions_v2 = {"schema_version": 1, "actions": build_collect_actions()}
    notes_v2 = {"schema_version": 1, "actions_manual": [], "summary": ""}
    skip_stage2 = False
    skip_stage2_reason = ""
    candidate_processes: List[Dict[str, Any]] = []
    primary_suspect: Optional[Dict[str, Any]] = None
    secondary_suspects: List[Dict[str, Any]] = []
    pidstat_interval_ms: Optional[int] = None
    observations: List[str] = []
    try:
        log(f"[meta] run_dir={run_dir}")
        log(f"[meta] out_dir={out_dir}")

        os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"

        project_root = Path(__file__).resolve().parents[1]
        if str(project_root) not in sys.path:
            sys.path.insert(0, str(project_root))
        # --- system prompt (must be defined before building messages) ---
        sp_path = os.environ.get(
            "WK_QWEN3_SYSTEM_PROMPT_JSONL",
            str(project_root / "data" / "llm_sft_test.jsonl"),
        )
        system_prompt = load_system_prompt_safe(Path(sp_path))
        log(f"[prompt] system_prompt_source={sp_path} len={len(system_prompt)}")

        labels = parse_label_kv(meta.get("labels") or [])
        run_window_start_ms = meta.get("run_window_host_epoch_ms_start")
        run_window_end_ms = meta.get("run_window_host_epoch_ms_end")

        run_start_ms = parse_dotnet_date(meta.get("run_start")) or meta.get("host_epoch_ms_start")
        run_end_ms = parse_dotnet_date(meta.get("run_end"))
        if run_window_start_ms in (0, None):
            run_window_start_ms = run_start_ms
        if run_window_end_ms in (0, None):
            run_window_end_ms = run_end_ms

        metrics_file = None
        if metrics_dir.exists():
            candidates = sorted(metrics_dir.glob("sys_*.csv"))
            metrics_file = candidates[-1] if candidates else None
        metrics_rows, metrics_fields = load_metrics_csv(metrics_file) if metrics_file else ([], [])
        metrics_rows = compute_metrics_window(metrics_rows, run_window_start_ms, run_window_end_ms)

        events: List[Dict[str, Any]] = []
        if events_dir.exists():
            ev_files = sorted(events_dir.glob("events_*.jsonl"))
            if ev_files:
                try:
                    with ev_files[-1].open("r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                obj = json.loads(line)
                                obj["msg"] = sanitize_event_msg_for_llm(obj.get("msg"))
                                ts = safe_int(obj.get("ts"))
                                if run_window_start_ms and run_window_end_ms and ts is not None:
                                    if not (run_window_start_ms <= ts <= run_window_end_ms):
                                        continue
                                events.append(obj)
                            except Exception:
                                continue
                except Exception:
                    pass

        proc_lines: List[str] = []
        if procs_dir.exists():
            proc_files = sorted(procs_dir.glob("procs_*.txt"))
            if proc_files:
                proc_lines = read_lines_tail(proc_files[-1], 200)
                # drop header lines
                proc_lines = [ln for ln in proc_lines if ln.strip() and not ln.startswith("PID ")]

        proc_entries = parse_proc_snapshot_lines(proc_lines)
        pidstat0, pidstat0_ms = parse_pidstat_file(procs_dir / "pidstat_0.txt")
        pidstat1, pidstat1_ms = parse_pidstat_file(procs_dir / "pidstat_1.txt")
        pidstat_interval_ms = None
        if pidstat0_ms is not None and pidstat1_ms is not None and pidstat1_ms > pidstat0_ms:
            pidstat_interval_ms = pidstat1_ms - pidstat0_ms
        elif pidstat0 and pidstat1:
            pidstat_interval_ms = 1000

        candidate_processes: List[Dict[str, Any]] = []
        for proc in proc_entries:
            pid = proc.get("pid")
            if pid is None:
                continue
            info0 = pidstat0.get(pid) if pidstat0 else None
            info1 = pidstat1.get(pid) if pidstat1 else None
            cpu_delta = None
            if info0 and info1:
                u0 = info0.get("utime")
                s0 = info0.get("stime")
                u1 = info1.get("utime")
                s1 = info1.get("stime")
                if None not in (u0, s0, u1, s1):
                    delta = (u1 + s1) - (u0 + s0)
                    if delta >= 0:
                        cpu_delta = delta

            cpu_pct = None
            if cpu_delta is not None and pidstat_interval_ms and pidstat_interval_ms > 0:
                cpu_pct = round((cpu_delta / (CLK_TCK * (pidstat_interval_ms / 1000.0))) * 100.0, 2)

            score = cpu_delta if cpu_delta is not None else None
            signals: List[str] = []
            if cpu_delta is not None:
                signals.append("cpu_delta_jiffies")
            if proc.get("rss_kb") is not None:
                signals.append("rss_kb")
            if proc.get("stat"):
                signals.append("stat")

            candidate_processes.append({
                "pid": pid,
                "name": proc.get("comm"),
                "comm": proc.get("comm"),
                "cmd": proc.get("comm"),
                "stat": proc.get("stat"),
                "rss_kb": proc.get("rss_kb"),
                "cpu_delta_jiffies": cpu_delta,
                "cpu_pct": cpu_pct,
                "score": score,
                "signals": signals,
                "source": "pidstat" if cpu_delta is not None else "procs",
            })

        def _cand_sort_key(item: Dict[str, Any]) -> Tuple[int, float, float]:
            cpu = item.get("cpu_delta_jiffies")
            rss = item.get("rss_kb") or 0
            if cpu is None:
                return (0, float(rss), 0.0)
            return (1, float(cpu), float(rss))

        candidate_processes.sort(key=_cand_sort_key, reverse=True)
        if len(candidate_processes) > 80:
            candidate_processes = candidate_processes[:80]

        primary_suspect = candidate_processes[0] if candidate_processes else None
        secondary_suspects = candidate_processes[1:6] if len(candidate_processes) > 1 else []
        observations: List[str] = []
        if metrics_rows:
            observations.append(f"run_window 内 metrics 行数={len(metrics_rows)}")
        if events:
            observations.append(f"run_window 内 events 条数={len(events)}")
        if proc_entries:
            observations.append(f"进程快照条数={len(proc_entries)}")
        if pidstat0 or pidstat1:
            observations.append(f"pidstat 覆盖进程数: pidstat_0={len(pidstat0)} pidstat_1={len(pidstat1)}")
        else:
            observations.append("pidstat_0/1 缺失或为空")

        dmesg_lines = read_lines_tail(dmesg_after, 200)
        hilog_lines = read_lines_tail(hilog_full, 200)

        meta_llm = sanitize_meta_for_llm(meta)

        user_message = build_user_message(
            run_id=run_dir.name,
            meta=meta_llm,
            labels={},  # avoid leaking label fields into the prompt
            metrics_rows=metrics_rows,
            events=events,
            process_candidates=candidate_processes,
            dmesg_lines=dmesg_lines,
            hilog_lines=hilog_lines,
            run_window_start_ms=run_window_start_ms,
            run_window_end_ms=run_window_end_ms,
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        prompt_material = {
            "run_meta": meta_llm,  # sanitized
            "metrics_fields": metrics_fields,
            "events_count": len(events),
            "dmesg_after_tail": redact_label_leaks(read_text_tail(dmesg_after, 200)),
            "hilog_tail": redact_label_leaks(read_text_tail(hilog_full, 200)),
            "candidate_processes": candidate_processes,
            "primary_suspect": primary_suspect,
            "secondary_suspects": secondary_suspects,
            "pidstat_interval_ms": pidstat_interval_ms,
            "clk_tck": CLK_TCK,
        }

        (out_dir / "prompt_material.json").write_text(
            json.dumps(prompt_material, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        input_jsonl = out_dir / "llm_input.jsonl"
        input_jsonl.write_text(json.dumps({"messages": messages}, ensure_ascii=False) + "\n", encoding="utf-8")
        log(f"[closed_loop] wrote: {input_jsonl}")

        gpu_info, gpu_err = query_gpu_mem()
        if gpu_info:
            log(f"[gpu] free_mib={gpu_info['free_mib']} used_mib={gpu_info['used_mib']} total_mib={gpu_info.get('total_mib')}")
        else:
            log(f"[gpu] nvidia-smi unavailable: {gpu_err}")
        # ===== stage2 enable/disable (default: disabled) =====
        enable_stage2 = args.enable_stage2 if args.enable_stage2 is not None else int(
            os.environ.get("WK_QWEN3_ENABLE_STAGE2", "0")
        )
        enable_stage2 = 1 if int(enable_stage2) != 0 else 0
        if enable_stage2 == 0:
            skip_stage2 = True
            skip_stage2_reason = "disabled_by_config"
            log("[closed_loop] stage2 disabled by config (WK_QWEN3_ENABLE_STAGE2=0)")

        min_free_mib = args.min_free_mib if args.min_free_mib is not None else int(
            os.environ.get("WK_QWEN3_MIN_FREE_MIB", "8140")
        )
        low_vram_wait_sec = args.low_vram_wait_sec if args.low_vram_wait_sec is not None else int(
            os.environ.get("WK_QWEN3_LOW_VRAM_WAIT_SEC", "15")
        )
        low_vram_policy = args.low_vram_policy or os.environ.get("WK_QWEN3_LOW_VRAM_POLICY", "skip")
        low_vram_policy = low_vram_policy.strip().strip('"').strip("'").lower()
        if low_vram_policy not in ("skip", "try", "wait"):
            low_vram_policy = "skip"

        wait_poll_sec = args.wait_poll_sec if args.wait_poll_sec is not None else int(
            os.environ.get("WK_QWEN3_WAIT_POLL_SEC", "15")
        )
        wait_max_sec = args.wait_max_sec if args.wait_max_sec is not None else int(
            os.environ.get("WK_QWEN3_WAIT_MAX_SEC", "0")  # 0 means wait forever
        )
        # stage2 headroom only; keep smaller threshold than stage1
        min_free_mib_stage2 = args.min_free_mib_stage2 if args.min_free_mib_stage2 is not None else int(
            os.environ.get("WK_QWEN3_MIN_FREE_MIB_STAGE2", "4096")
        )
        stage2_wait_poll_sec = args.stage2_wait_poll_sec if args.stage2_wait_poll_sec is not None else int(
            os.environ.get("WK_QWEN3_STAGE2_WAIT_POLL_SEC", str(wait_poll_sec))
        )
        stage2_wait_max_sec = args.stage2_wait_max_sec if args.stage2_wait_max_sec is not None else int(
            os.environ.get("WK_QWEN3_STAGE2_WAIT_MAX_SEC", "900")  # 榛樿鏈€澶氱瓑 15 鍒嗛挓
        )
        stage2_tail_bytes = args.stage2_tail_bytes if args.stage2_tail_bytes is not None else int(
            os.environ.get("WK_QWEN3_STAGE2_TAIL_BYTES", "40000")
        )
        stage2_tail_lines = args.stage2_tail_lines if args.stage2_tail_lines is not None else int(
            os.environ.get("WK_QWEN3_STAGE2_TAIL_LINES", "1200")
        )
        log(f"[stage2] tail_limits: bytes={stage2_tail_bytes} lines={stage2_tail_lines}")

        log(
            f"[gpu] thresholds: stage1_min_free_mib={min_free_mib} "
            f"stage2_min_free_mib={min_free_mib_stage2} "
            f"stage1_wait_max_sec={wait_max_sec} stage2_wait_max_sec={stage2_wait_max_sec}"
        )

        log(f"[gpu] low_vram_policy={low_vram_policy} min_free_mib={min_free_mib} wait_sec={low_vram_wait_sec}")
        if low_vram_policy == "wait":
            log(f"[gpu] wait_policy: poll_sec={wait_poll_sec} max_wait_sec={wait_max_sec} (0=forever)")

        if gpu_info and gpu_info["free_mib"] < min_free_mib:
            log(f"[gpu] low free memory ({gpu_info['free_mib']} MiB < {min_free_mib} MiB), policy={low_vram_policy}")
            if low_vram_policy == "skip":
                log(f"[gpu] waiting {low_vram_wait_sec}s before stage2 skip...")
                time.sleep(low_vram_wait_sec)
                gpu_info2, gpu_err2 = query_gpu_mem()
                if gpu_info2:
                    log(f"[gpu] retry free_mib={gpu_info2['free_mib']} used_mib={gpu_info2['used_mib']} total_mib={gpu_info2.get('total_mib')}")
                else:
                    log(f"[gpu] retry failed: {gpu_err2}")
                if not gpu_info2 or gpu_info2["free_mib"] < min_free_mib:
                    if enable_stage2 == 1:
                        skip_stage2 = True
                        if not skip_stage2_reason:
                            skip_stage2_reason = "low_vram_fallback"
                        log("[gpu] stage2 will be skipped due to low VRAM")

            elif low_vram_policy == "wait":
                # wait before loading model to reduce stage1 OOM probability
                ok = wait_for_gpu(min_free_mib, wait_poll_sec, wait_max_sec, log)
                if not ok:
                    # if wait failed (no nvidia-smi or timeout), continue but mark risk; stage1 may still OOM
                    log("[gpu_wait] wait failed; continue with best-effort inference (may OOM)")

        log("[closed_loop] loading model...")
        from infer_qwen3_fault_2stage import build_model, stage1_reason, stage2_summarize
        try:
            tokenizer, model = build_model()
        except Exception as exc:
            msg = str(exc)
            if "dispatched on the CPU or the disk" in msg:
                log("[closed_loop] retry build_model with device_map=cuda:0")
                try:
                    import gc
                    import torch
                    gc.collect()
                    if torch.cuda.is_available():
                        torch.cuda.empty_cache()
                except Exception:
                    pass
                if build_model_supports_device_map(build_model):
                    tokenizer, model = build_model(device_map={"": 0})
                else:
                    log("[closed_loop] build_model has no device_map kw; rethrow")
                    raise
            else:
                raise
        # log model / cuda state (helps explain 18GiB cases)
        try:
            import torch
            info_after, _ = query_gpu_mem()
            if info_after:
                log(f"[gpu] after_load: free_mib={info_after['free_mib']} used_mib={info_after['used_mib']} total_mib={info_after['total_mib']}")
            is4 = bool(getattr(model, "is_loaded_in_4bit", False))
            is8 = bool(getattr(model, "is_loaded_in_8bit", False))
            dt = None
            try:
                dt = str(next(model.parameters()).dtype)
            except Exception:
                dt = str(getattr(getattr(model, "config", None), "torch_dtype", None))
            log(f"[model] dtype={dt} is_loaded_in_4bit={is4} is_loaded_in_8bit={is8}")
            log(f"[torch] cuda_alloc_mib={torch.cuda.memory_allocated()//(1024**2)} cuda_reserved_mib={torch.cuda.memory_reserved()//(1024**2)}")
        except Exception:
            pass
        analysis = stage1_reason(tokenizer, model, messages)
        summary = stage2_summarize(tokenizer, model, analysis)

        raw_out.write_text(
            "### stage1_analysis\n" + analysis + "\n\n### stage2_summary\n" + summary + "\n",
            encoding="utf-8",
        )

        summary_clean = sanitize_llm_text(summary)
        diagnosis, notes = parse_summary_to_struct(summary_clean, labels.get("severity", "unknown"))
        actions = {"schema_version": 1, "actions": build_collect_actions()}

        # stage2: v2 inference (prompt_material + llm_input + actions_exec.log tail)
        if skip_stage2:
            diagnosis_v2 = copy.deepcopy(diagnosis)
            actions_v2 = copy.deepcopy(actions)
            notes_v2 = copy.deepcopy(notes)

            reason = skip_stage2_reason or "disabled_by_config"
            notes_v2["summary"] = f"stage2_skipped: {reason}"

            # only low_vram skip should set GPU risk flag
            if reason == "low_vram_fallback":
                append_risk_flag(diagnosis_v2, "gpu_oom_or_low_mem_fallback")
        else:
            try:
                # stage2 鍓嶅啀鍋氫竴锟?wait锛堝彧锟?headroom锛屼笉瑕佺敤 stage1 鐨勫ぇ闃堝€硷級
                if enable_stage2 == 1 and low_vram_policy == "wait":
                    info3, _ = query_gpu_mem()
                    free3 = info3.get("free_mib", 0) if info3 else 0
                    if free3 < min_free_mib_stage2:
                        log(f"[gpu_wait] stage2 precheck low free_mib={free3} need>={min_free_mib_stage2}; entering stage2 wait...")
                        ok2 = wait_for_gpu(min_free_mib_stage2, stage2_wait_poll_sec, stage2_wait_max_sec, log)
                        if not ok2:
                            skip_stage2 = True
                            if not skip_stage2_reason:
                                skip_stage2_reason = "low_vram_fallback"
                            log("[gpu_wait] stage2 wait timeout/unavailable; stage2 will be skipped (inherit stage1)")
                # 锟?鍏抽敭锛氫竴鏃﹀喅锟?skip_stage2锛岀珛鍒荤户锟?stage1 骞堕€€锟?v2 娴佺▼
                if skip_stage2:
                    diagnosis_v2 = copy.deepcopy(diagnosis)
                    actions_v2 = copy.deepcopy(actions)
                    notes_v2 = copy.deepcopy(notes)
                    notes_v2["summary"] = "stage2_skipped: low_vram_fallback"
                    append_risk_flag(diagnosis_v2, "gpu_oom_or_low_mem_fallback")
                else:
                    prompt_material_path = out_dir / "prompt_material.json"
                    llm_input_path = out_dir / "llm_input.jsonl"
                    actions_exec_path = out_dir / "actions_exec.log"

                    # stage2 input tailing to reduce KV cache usage
                    _tail_bytes = stage2_tail_bytes
                    _tail_lines = stage2_tail_lines
                    prompt_text = read_text_tail_bytes(prompt_material_path, max_bytes=_tail_bytes, max_lines=_tail_lines)
                    llm_text = read_text_tail_bytes(llm_input_path, max_bytes=_tail_bytes, max_lines=_tail_lines)
                    actions_exec_text = read_text_tail_bytes(actions_exec_path, max_bytes=_tail_bytes, max_lines=_tail_lines)
                    if not actions_exec_text:
                        actions_exec_text = "(actions_exec.log missing or empty)"

                    stage2_user = (
                        "[prompt_material.json]\n"
                        f"{prompt_text}\n\n"
                        "[llm_input.jsonl]\n"
                        f"{llm_text}\n\n"
                        "[actions_exec.log tail]\n"
                        f"{actions_exec_text}\n\n"
                        "Please produce structured diagnosis and suggestions based on the above.\n"
                    )

                    messages_v2 = [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": stage2_user},
                    ]

                    analysis_v2 = stage1_reason(tokenizer, model, messages_v2)
                    summary_v2 = stage2_summarize(tokenizer, model, analysis_v2)
                    summary_v2_clean = sanitize_llm_text(summary_v2)

                    with raw_out.open("a", encoding="utf-8") as f:
                        f.write("\n### stage2_analysis_v2\n" + analysis_v2 + "\n\n### stage2_summary_v2\n" + summary_v2 + "\n")

                    diagnosis_v2, notes_v2 = parse_summary_to_struct(summary_v2_clean, labels.get("severity", "unknown"))
                    # 1) ensure severity fallback (avoid unknown)
                    if not diagnosis_v2.get("severity") or diagnosis_v2.get("severity") == "unknown":
                        diagnosis_v2["severity"] = labels.get("severity", "unknown")

                    # 2) 涓庝綘鏂囦欢鏈熬鐨勮鍒欎繚鎸佷竴鑷达細normal -> severity 寮哄埗 normal
                    if diagnosis_v2.get("fault_state") == "normal" and diagnosis_v2.get("severity") not in ("normal", "none"):
                        diagnosis_v2["severity"] = "normal"

                    actions_v2 = {"schema_version": 1, "actions": build_collect_actions()}
                    # 3) stage2_ok: rewrite actions why
                    rewrite_actions_why(actions_v2, old="fallback_collect", new="stage2_collect")
                    # 4) regenerate summary to align with diagnosis_v2
                    notes_v2["summary"] = "stage2_ok: {}/{}/{}".format(
                        diagnosis_v2.get("fault_state", "unknown"),
                        diagnosis_v2.get("family", "other"),
                        diagnosis_v2.get("severity", "unknown"),
                    )


            except Exception as exc:
                err_msg_v2 = str(exc)
                diagnosis_v2 = copy.deepcopy(diagnosis)
                actions_v2 = copy.deepcopy(actions)
                notes_v2 = copy.deepcopy(notes)

                if is_cuda_oom(err_msg_v2):
                    append_risk_flag(diagnosis_v2, "gpu_oom_or_low_mem_fallback")
                    notes_v2["summary"] = "stage2_failed: cuda_oom_fallback_to_stage1"
                else:
                    notes_v2["summary"] = f"stage2_failed: {err_msg_v2}"

                log(f"[closed_loop] stage2_failed: {err_msg_v2}")

    except Exception as exc:
        err_msg = str(exc)
        err_trace = traceback.format_exc()
        err_is_oom = is_torch_oom(exc) or is_cuda_oom(err_msg)
        err_type = "cuda_oom" if err_is_oom else "infer_failed"

        if "low_vram_fallback" in err_msg:
            log("[closed_loop] low_vram_fallback activated")
        elif err_is_oom:
            diagnosis, actions = build_fallback_result("gpu_oom_or_low_mem_fallback")
            notes = {"schema_version": 1, "actions_manual": [], "summary": "stage1_failed: cuda_oom_fallback"}
            log(f"[closed_loop] fallback after OOM: {err_msg}")
        else:
            diagnosis, actions = build_fallback_result("inference_failed")
            notes = {"schema_version": 1, "actions_manual": [], "summary": "inference_failed"}
            log(f"[closed_loop] inference_failed: {err_msg}")

        exit_code = 0

        # keep raw_out best-effort; never raise from except
        try:
            raw_out.write_text(err_msg + "\n", encoding="utf-8")
        except Exception:
            pass

        try:
            atomic_write_text(out_dir / "infer_error.txt", err_msg + "\n\n" + err_trace, encoding="utf-8")
        except Exception:
            pass

        # v2 fallback (align with stage1 failure reason)
        if "low_vram_fallback" in err_msg:
            diagnosis_v2 = copy.deepcopy(diagnosis)
            actions_v2 = copy.deepcopy(actions)
            notes_v2 = copy.deepcopy(notes)
            notes_v2["summary"] = "stage2_skipped: low_vram_fallback"
            append_risk_flag(diagnosis_v2, "gpu_oom_or_low_mem_fallback")
        elif err_is_oom:
            diagnosis_v2, actions_v2 = build_fallback_result("gpu_oom_or_low_mem_fallback")
            notes_v2 = {"schema_version": 1, "actions_manual": [], "summary": "stage1_failed: cuda_oom_fallback"}
        else:
            diagnosis_v2, actions_v2 = build_fallback_result("inference_failed")
            notes_v2 = {"schema_version": 1, "actions_manual": [], "summary": f"stage2_skipped: {err_msg}"}

        diagnosis = ensure_error_diagnosis(diagnosis, err_type, err_msg, out_dir)
        diagnosis_v2 = ensure_error_diagnosis(diagnosis_v2, err_type, err_msg, out_dir)

    finally:
        if diagnosis.get("fault_state") == "normal" and diagnosis.get("severity") not in ("normal", "none"):
            diagnosis["severity"] = "normal"
        if diagnosis_v2.get("fault_state") == "normal" and diagnosis_v2.get("severity") not in ("normal", "none"):
            diagnosis_v2["severity"] = "normal"

        inject_process_candidates(diagnosis, candidate_processes, primary_suspect, secondary_suspects)
        inject_process_candidates(diagnosis_v2, candidate_processes, primary_suspect, secondary_suspects)
        suspects_list = build_suspects_list(candidate_processes, primary_suspect, secondary_suspects, limit=5)
        missing_pids = [str(s.get("pid")) for s in suspects_list if not s.get("evidence_ok") and s.get("pid") is not None]

        def _enrich_diagnosis(diag: Dict[str, Any], acts: Dict[str, Any]) -> None:
            evidence_items = normalize_evidence_items(diag.get("evidence"))
            if missing_pids:
                msg = "pidstat 未覆盖这些 PID，无法评分: " + ",".join(missing_pids)
                already = any(isinstance(e, dict) and msg in str(e.get("text", "")) for e in evidence_items)
                if not already:
                    evidence_items.append({"text": msg, "source": "pidstat", "gaps": ["pidstat_missing"]})
            diag["evidence"] = evidence_items
            if not isinstance(diag.get("evidence_text"), list):
                diag["evidence_text"] = [e.get("text") for e in evidence_items if isinstance(e, dict) and e.get("text")]
            diag["observations"] = observations
            hypothesis = diag.get("root_cause") or diag.get("summary") or diag.get("reason") or ""
            if not hypothesis:
                hypothesis = "当前证据不足，无法形成明确根因假设"
            diag["hypothesis"] = hypothesis
            diag["suspects"] = suspects_list
            next_checks = extract_next_checks(acts)
            diag["narrative"] = build_diagnosis_narrative(observations, hypothesis, evidence_items, next_checks)

        _enrich_diagnosis(diagnosis, actions)
        _enrich_diagnosis(diagnosis_v2, actions_v2)
        diagnosis["clk_tck"] = CLK_TCK
        diagnosis_v2["clk_tck"] = CLK_TCK
        if pidstat_interval_ms is not None:
            diagnosis["pidstat_interval_ms"] = pidstat_interval_ms
            diagnosis_v2["pidstat_interval_ms"] = pidstat_interval_ms

        diagnosis_full = diagnosis_v2
        diagnosis_compact = compact_diagnosis(diagnosis_full, actions_v2)

        atomic_write_json(out_dir / "diagnosis.json", diagnosis_compact)
        (out_dir / "actions.json").write_text(json.dumps(actions, ensure_ascii=False, indent=2), encoding="utf-8")
        notes_path.write_text(json.dumps(notes, ensure_ascii=False, indent=2), encoding="utf-8")
        diagnosis_v2_path.write_text(json.dumps(diagnosis_full, ensure_ascii=False, indent=2), encoding="utf-8")
        actions_v2_path.write_text(json.dumps(actions_v2, ensure_ascii=False, indent=2), encoding="utf-8")
        notes_v2_path.write_text(json.dumps(notes_v2, ensure_ascii=False, indent=2), encoding="utf-8")
        infer_ec.write_text(str(exit_code) + "\n", encoding="utf-8")

        print(f"[closed_loop] wrote: {out_dir / 'diagnosis.json'}")
        print(f"[closed_loop] wrote: {out_dir / 'actions.json'}")
        print(f"[closed_loop] wrote: {infer_log}")
        print(f"[closed_loop] wrote: {infer_ec}")
        print(f"[closed_loop] wrote: {notes_path}")
        print(f"[closed_loop] wrote: {diagnosis_v2_path}")
        print(f"[closed_loop] wrote: {actions_v2_path}")
        print(f"[closed_loop] wrote: {notes_v2_path}")

        if lock_path:
            try:
                lock_path.unlink()
            except Exception:
                pass

if __name__ == "__main__":
    main()







