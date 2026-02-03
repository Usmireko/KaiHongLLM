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
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

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

def is_cuda_oom(msg: str) -> bool:
    if not msg:
        return False
    low = msg.lower()
    return "cuda out of memory" in low or ("out of memory" in low and "cuda" in low)

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
        "你是一个面向 KaiHongOS / OpenHarmony 的系统故障诊断助手。\n"
        "你可以综合系统指标（metrics）、进程快照（ps）、内核日志（dmesg）、应用日志（hilog）等信息，\n"
        "判断当前 run 是否存在故障、属于哪一类故障场景，并给出简要的根因分析和排查 / 恢复建议。\n"
        "回答时要条理清晰、尽量简洁，优先使用中文。"
    )
    
DEFAULT_SYSTEM_PROMPT = (
    "你是一个操作系统故障诊断与自愈助手。"
    "你必须仅依据提供的 metrics/events/procs/dmesg/hilog 证据做判断，"
    "不要使用任何脚本场景标签、注入类型或 obs_* 字段作为结论依据。"
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
                       proc_lines: List[str],
                       dmesg_lines: List[str],
                       hilog_lines: List[str],
                       run_window_start_ms: Optional[int],
                       run_window_end_ms: Optional[int],
                       ps_lines: Optional[List[str]] = None,
                       top_lines: Optional[List[str]] = None,
                       loadavg_lines: Optional[List[str]] = None,
                       meminfo_lines: Optional[List[str]] = None) -> str:
    lines: List[str] = []

    lines.append(f"【run_id】{run_id}")
    lines.append(f"【脚本版本】{meta.get('script_version')}")
    lines.append(f"【run_window_source】{meta.get('run_window_source')}")
    lines.append(
        f"【run_window_board_ms】start={meta.get('run_window_board_ms_start')}, end={meta.get('run_window_board_ms_end')}"
    )

    lines.append("【注意】请仅基于 metrics/events/procs/dmesg/hilog 的证据进行诊断，不要假设任何脚本场景标签或注入类型。")

    # metrics window and summary
    if metrics_rows:
        lines.append("【metrics时间窗口（窗口内）】")
        lines.append(f"  start_ms={run_window_start_ms}, end_ms={run_window_end_ms}, rows={len(metrics_rows)}")

        load1 = [safe_int(r.get("load1_x100")) for r in metrics_rows]
        cpu = [safe_int(r.get("cpu_util_total_x100")) for r in metrics_rows]
        mem_free = [safe_int(r.get("mem_free_kb")) for r in metrics_rows]
        mem_avail = [safe_int(r.get("mem_available_kb")) for r in metrics_rows]

        load_stats = calc_stats(load1)
        cpu_stats = calc_stats(cpu)
        mem_free_stats = calc_stats(mem_free)
        mem_avail_stats = calc_stats(mem_avail)
        mem_avail_drop = None
        if mem_avail_stats["min"] is not None and mem_avail_stats["max"] is not None:
            mem_avail_drop = mem_avail_stats["max"] - mem_avail_stats["min"]

        lines.append("【metrics摘要】")
        lines.append(f"  load1_peak_x100={load_stats['max']}")
        lines.append(f"  cpu_util_peak_x100={cpu_stats['max']}")
        lines.append(f"  mem_available_kb: min={mem_avail_stats['min']} max={mem_avail_stats['max']} drop_kb={mem_avail_drop}")
        lines.append(f"  mem_free_kb: min={mem_free_stats['min']} max={mem_free_stats['max']}")

        # sampled points
        lines.append("【metrics采样点】(相对窗口start的秒数, mem_available_kb, load1_x100, cpu_util_total_x100)")
        start_ms = run_window_start_ms or (safe_int(metrics_rows[0].get("ts_ms")) if metrics_rows else None)
        for row in metrics_rows[:16]:
            ts = safe_int(row.get("ts_ms"))
            rel_sec = None
            if ts is not None and start_ms is not None:
                rel_sec = round((ts - start_ms) / 1000.0, 1)
            t_str = f"+{rel_sec}s" if rel_sec is not None else str(ts)
            lines.append(
                "  t={t}, mem_available_kb={ma}, load1_x100={l1}, cpu_util_total_x100={cpu}".format(
                    t=t_str,
                    ma=row.get("mem_available_kb"),
                    l1=row.get("load1_x100"),
                    cpu=row.get("cpu_util_total_x100"),
                )
            )
    else:
        lines.append("【metrics】本次run 未获取到有效的metrics 序列。")

    # events summary
    if events:
        total = len(events)
        tag_counts: Dict[str, int] = {}
        for ev in events:
            tag = ev.get("tag") or "unknown"
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
        lines.append("【events摘要（窗口内）】")
        lines.append(
            "  total={total}, cpu_hotspot={cpu_hotspot}, mem_pressure={mem_pressure}, io_pressure={io_pressure}".format(
                total=total,
                cpu_hotspot=tag_counts.get("cpu_hotspot", 0),
                mem_pressure=tag_counts.get("mem_pressure", 0),
                io_pressure=tag_counts.get("io_pressure", 0),
            )
        )
        lines.append(f"  tag_counts={tag_counts}")
        lines.append("【events（窗口内，截断）】")
        for ev in events[:8]:
            lines.append(
                "  ts={ts}, level={level}, component={component}, tag={tag}, msg={msg}".format(
                    ts=ev.get("ts"),
                    level=ev.get("level"),
                    component=ev.get("component"),
                    tag=ev.get("tag"),
                    msg=ev.get("msg"),
                )
            )

    # snapshots (ps/top/loadavg/meminfo)
    if loadavg_lines:
        lines.append("【/proc/loadavg（采样）】")
        lines.extend(["  " + ln for ln in loadavg_lines[:20]])
        lines.append("")

    if meminfo_lines:
        lines.append("【/proc/meminfo（采样）】")
        lines.extend(["  " + ln for ln in meminfo_lines[:80]])
        lines.append("")

    if ps_lines:
        lines.append("【ps 输出（采样）】")
        lines.extend(["  " + ln for ln in ps_lines[:120]])
        lines.append("")

    if top_lines:
        lines.append("【top 输出（采样）】")
        lines.extend(["  " + ln for ln in top_lines[:120]])
        lines.append("")

    # proc snapshot (raw excerpt)
    if proc_lines:
        lines.append("【进程快照（procs，截断）】")
        lines.append("说明：优先从以下快照中定位“可疑进程/线程”（高CPU/高RSS/异常状态）。如果字段缺失，请在结论中注明证据不足。")
        for ln in proc_lines[:120]:
            lines.append("  " + ln)
        lines.append("")
    lines.append("请根据以上信息回答：")
    lines.append("1) 当前 run 是否为故障 (是/否)？属于哪一个故障家族 (cpu/mem/background/other)？")
    lines.append("2) 根因依据（2-4 句，必须给出证据链）。若进程/线程层面证据充足，请明确指出可疑进程/线程（PID/进程名/命令行）及其异常表现（CPU/RSS/状态/日志线索）；若证据不足，请说明缺了哪些进程级证据（例如缺少top/ps字段、缺少按CPU排序的进程快照等）。")
    lines.append("3) 1-2 条可执行排查/恢复建议。")
    lines.append("4) 诊断置信度（0~1）。")
    return "\n".join(lines)

def parse_summary_to_struct(summary: str, fallback_severity: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    fault_state = "unknown"
    family = "other"
    confidence = 0.0
    root_lines: List[str] = []
    action_lines: List[str] = []
    risk_flags: List[str] = []

    lines = [ln.strip() for ln in summary.splitlines() if ln.strip()]
    section = None
    for ln in lines:
        if ln.startswith("1."):
            section = "judge"
            continue
        if ln.startswith("2."):
            section = "root"
            continue
        if ln.startswith("3."):
            section = "actions"
            continue
        if ln.startswith("4."):
            section = "confidence"
            # parse inline confidence in same line
            parts = ln.split(":", 1) if ":" in ln else ln.split("：", 1)
            if len(parts) == 2:
                try:
                    confidence = float(parts[1].strip())
                except Exception:
                    pass
            continue

        if "故障状态" in ln:
            val = ln.split(":", 1)[-1] if ":" in ln else ln.split("：", 1)[-1]
            if "是" in val:
                fault_state = "fault"
            elif "否" in val:
                fault_state = "normal"
        elif "故障家族" in ln:
            val = ln.split(":", 1)[-1] if ":" in ln else ln.split("：", 1)[-1]
            family = val.strip().split()[0].lower()
        elif "诊断置信度" in ln:
            parts = ln.split(":", 1) if ":" in ln else ln.split("：", 1)
            if len(parts) == 2:
                try:
                    confidence = float(parts[1].strip())
                except Exception:
                    pass
        elif section == "root" and ln.startswith("-"):
            root_lines.append(ln.lstrip("-").strip())
        elif section == "actions" and ln.startswith("-"):
            action_lines.append(ln.lstrip("-").strip())

    root_cause = " ".join(root_lines).strip()
    diagnosis = {
        "schema_version": 1,
        "fault_state": fault_state,
        "family": family,
        "severity": fallback_severity or "unknown",
        "root_cause": root_cause,
        "evidence": root_lines,
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

    meta_path = run_dir / "_run_meta.json"
    dmesg_after = run_dir / "dmesg_after.utf8.log"
    hilog_full = run_dir / "hilog_text_full.log"
    snapshots_dir = run_dir / "snapshots"

    # Stage2 bundles typically store snapshots under snapshots/*.txt.
    dmesg_snap = snapshots_dir / "dmesg_tail.txt"
    hilog_snap = snapshots_dir / "hilog_tail.txt"
    ps_snap = snapshots_dir / "ps.txt"
    top_snap = snapshots_dir / "top.txt"
    loadavg_snap = snapshots_dir / "loadavg.txt"
    meminfo_snap = snapshots_dir / "meminfo.txt"

    # Prefer snapshots/* if present; otherwise fall back to legacy root-level logs.
    dmesg_path = dmesg_snap if dmesg_snap.exists() else dmesg_after
    hilog_path = hilog_snap if hilog_snap.exists() else hilog_full
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
        "confidence": 0.0,
        "risk_flags": ["v2_inference_not_run"],
    }
    actions_v2 = {"schema_version": 1, "actions": build_collect_actions()}
    notes_v2 = {"schema_version": 1, "actions_manual": [], "summary": ""}
    skip_stage2 = False
    skip_stage2_reason = ""
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
        procs_source: Optional[str] = None
        if procs_dir.exists():
            proc_files = sorted(procs_dir.glob("procs_*.txt"))
            if proc_files:
                procs_source = str(proc_files[-1].relative_to(run_dir))
                proc_lines = read_lines_tail(proc_files[-1], 200)
                # drop header lines
                proc_lines = [ln for ln in proc_lines if ln.strip() and not ln.lstrip().startswith("PID")]

        # Snapshot helpers (stage2 stores these under snapshots/*.txt)
        ps_lines = read_lines_tail(ps_snap, 200)
        top_lines = read_lines_tail(top_snap, 200)
        loadavg_lines = read_lines_tail(loadavg_snap, 50)
        meminfo_lines = read_lines_tail(meminfo_snap, 120)

        dmesg_lines = read_lines_tail(dmesg_path, 200)
        hilog_lines = read_lines_tail(hilog_path, 200)

        meta_llm = sanitize_meta_for_llm(meta)

        user_message = build_user_message(
            run_id=run_dir.name,
            meta=meta_llm,
            labels={},  # avoid leaking label fields into the prompt
            metrics_rows=metrics_rows,
            events=events,
            proc_lines=proc_lines,
            dmesg_lines=dmesg_lines,
            hilog_lines=hilog_lines,
            run_window_start_ms=run_window_start_ms,
            run_window_end_ms=run_window_end_ms,
            ps_lines=ps_lines,
            top_lines=top_lines,
            loadavg_lines=loadavg_lines,
            meminfo_lines=meminfo_lines,
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        prompt_material = {
            "run_meta": meta_llm,  # sanitized
            "metrics_fields": metrics_fields,
            "events_count": len(events),

            "dmesg_source": str(dmesg_path.relative_to(run_dir)) if dmesg_path.exists() else None,
            "dmesg_tail": "\n".join(dmesg_lines[:200]),

            "hilog_source": str(hilog_path.relative_to(run_dir)) if hilog_path.exists() else None,
            "hilog_tail": "\n".join(hilog_lines[:200]),

            "ps_source": str(ps_snap.relative_to(run_dir)) if ps_snap.exists() else None,
            "ps_tail": "\n".join(ps_lines[:200]),

            "top_source": str(top_snap.relative_to(run_dir)) if top_snap.exists() else None,
            "top_tail": "\n".join(top_lines[:200]),

            "loadavg_source": str(loadavg_snap.relative_to(run_dir)) if loadavg_snap.exists() else None,
            "loadavg_tail": "\n".join(loadavg_lines[:50]),

            "meminfo_source": str(meminfo_snap.relative_to(run_dir)) if meminfo_snap.exists() else None,
            "meminfo_tail": "\n".join(meminfo_lines[:120]),

            "procs_source": procs_source,
            "procs_tail": "\n".join(proc_lines[:200]),
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
        # stage2 只需要 headroom，不要沿用 stage1 的大阈值
        min_free_mib_stage2 = args.min_free_mib_stage2 if args.min_free_mib_stage2 is not None else int(
            os.environ.get("WK_QWEN3_MIN_FREE_MIB_STAGE2", "4096")
        )
        stage2_wait_poll_sec = args.stage2_wait_poll_sec if args.stage2_wait_poll_sec is not None else int(
            os.environ.get("WK_QWEN3_STAGE2_WAIT_POLL_SEC", str(wait_poll_sec))
        )
        stage2_wait_max_sec = args.stage2_wait_max_sec if args.stage2_wait_max_sec is not None else int(
            os.environ.get("WK_QWEN3_STAGE2_WAIT_MAX_SEC", "900")  # 默认最多等 15 分钟
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
                # stage2 前再做一次 wait（只要 headroom，不要用 stage1 的大阈值）
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
                # ✅ 关键：一旦决定 skip_stage2，立刻继承 stage1 并退出 v2 流程
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

                    # ✅ 建议给 stage2 输入裁剪（避免 KV cache 暴涨）
                    # 如果你已定义 stage2_tail_bytes/lines，就用它；否则用一个保守默认
                    _tail_bytes = stage2_tail_bytes
                    _tail_lines = stage2_tail_lines
                    prompt_text = read_text_tail_bytes(prompt_material_path, max_bytes=_tail_bytes, max_lines=_tail_lines)
                    llm_text = read_text_tail_bytes(llm_input_path, max_bytes=_tail_bytes, max_lines=_tail_lines)
                    actions_exec_text = read_text_tail_bytes(actions_exec_path, max_bytes=_tail_bytes, max_lines=_tail_lines)
                    if not actions_exec_text:
                        actions_exec_text = "(actions_exec.log missing or empty)"

                    stage2_user = (
                        "【prompt_material.json】\n"
                        f"{prompt_text}\n\n"
                        "【llm_input.jsonl】\n"
                        f"{llm_text}\n\n"
                        "【actions_exec.log tail】\n"
                        f"{actions_exec_text}\n\n"
                        "请基于以上信息给出结构化诊断与建议。"
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

                    # 1) severity 兜底（避免 summary 里出现 unknown）
                    if not diagnosis_v2.get("severity") or diagnosis_v2.get("severity") == "unknown":
                        diagnosis_v2["severity"] = labels.get("severity", "unknown")

                    # 2) 与你文件末尾的规则保持一致：normal -> severity 强制 normal
                    if diagnosis_v2.get("fault_state") == "normal" and diagnosis_v2.get("severity") not in ("normal", "none"):
                        diagnosis_v2["severity"] = "normal"

                    actions_v2 = {"schema_version": 1, "actions": build_collect_actions()}

                    # 3) stage2_ok 时 why 文案更准确
                    rewrite_actions_why(actions_v2, old="fallback_collect", new="stage2_collect")

                    # 4) 最后再生成 summary，保证与 diagnosis_v2 一致
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
    
        if "low_vram_fallback" in err_msg:
            log("[closed_loop] low_vram_fallback activated")
        elif "CUDA out of memory" in err_msg:
            diagnosis, actions = build_fallback_result("gpu_oom_or_low_mem_fallback")
            notes = {"schema_version": 1, "actions_manual": [], "summary": "stage1_failed: cuda_oom_fallback"}
            log(f"[closed_loop] fallback after OOM: {err_msg}")
        else:
            diagnosis, actions = build_fallback_result("inference_failed")
            notes = {"schema_version": 1, "actions_manual": [], "summary": "inference_failed"}
            log(f"[closed_loop] inference_failed: {err_msg}")
    
        exit_code = 0
    
        # 尝试把错误写进 raw_out（这段必须配 except，否则就会触发你看到的 try 语法报错）
        try:
            raw_out.write_text(err_msg + "\n", encoding="utf-8")
        except Exception:
            pass
        
        # v2 fallback（根据 stage1 的失败原因写 summary）
        if "low_vram_fallback" in err_msg:
            diagnosis_v2 = copy.deepcopy(diagnosis)
            actions_v2 = copy.deepcopy(actions)
            notes_v2 = copy.deepcopy(notes)
            notes_v2["summary"] = "stage2_skipped: low_vram_fallback"
            append_risk_flag(diagnosis_v2, "gpu_oom_or_low_mem_fallback")
        elif "CUDA out of memory" in err_msg:
            diagnosis_v2, actions_v2 = build_fallback_result("gpu_oom_or_low_mem_fallback")
            notes_v2 = {"schema_version": 1, "actions_manual": [], "summary": "stage1_failed: cuda_oom_fallback"}
        else:
            diagnosis_v2, actions_v2 = build_fallback_result("inference_failed")
            notes_v2 = {"schema_version": 1, "actions_manual": [], "summary": f"stage2_skipped: {err_msg}"}

    if diagnosis.get("fault_state") == "normal" and diagnosis.get("severity") not in ("normal", "none"):
        diagnosis["severity"] = "normal"
    if diagnosis_v2.get("fault_state") == "normal" and diagnosis_v2.get("severity") not in ("normal", "none"):
        diagnosis_v2["severity"] = "normal"

    (out_dir / "diagnosis.json").write_text(json.dumps(diagnosis, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "actions.json").write_text(json.dumps(actions, ensure_ascii=False, indent=2), encoding="utf-8")
    notes_path.write_text(json.dumps(notes, ensure_ascii=False, indent=2), encoding="utf-8")
    diagnosis_v2_path.write_text(json.dumps(diagnosis_v2, ensure_ascii=False, indent=2), encoding="utf-8")
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

if __name__ == "__main__":
    main()
