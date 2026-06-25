#!/usr/bin/env python3
"""
build_fault_samples.py

从 run_wukong_collect.ps1 生成的 run_* 目录中抽取结构化 JSON 样本，输出 JSONL。
设计为在 WSL / Linux / Windows Python 3.8+ 环境下运行，仅依赖标准库。

用法示例：
  python build_fault_samples.py --runs-root /path/to/inbox/runs --output dataset.jsonl

也可以仅处理单个 run 目录：
  python build_fault_samples.py --run-dir /path/to/inbox/runs/20251209_043939
"""
import argparse
import csv
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

# ---------- 工具函数 ----------

DATE_MS_RE = re.compile(r"/Date\((\d+)\)/")

def parse_ms_from_date(s: str) -> Optional[int]:
    """从 PowerShell 的 /Date(1765xxxx)/ 形式中解析毫秒时间戳。"""
    if not isinstance(s, str):
        return None
    m = DATE_MS_RE.search(s)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None

def safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        return default

def list_first_csv(path: str) -> Optional[str]:
    if not os.path.isdir(path):
        return None
    for name in sorted(os.listdir(path)):
        if name.lower().endswith(".csv"):
            return os.path.join(path, name)
    return None

def read_json(path: str) -> Dict[str, Any]:
    # _run_meta.json 带 BOM，用 utf-8-sig 读
    with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
        return json.load(f)

# ---------- 场景 / 标签推断 ----------

SCENARIO_MAP: Dict[str, Dict[str, Any]] = {
    # 背景 & 基线
    "bg_idle": {
        "family": "background",
        "severity": "normal",
        "is_anomaly": False,
        "root_process": None,
    },
    "cpu_baseline": {
        "family": "cpu",
        "severity": "normal",
        "is_anomaly": False,
        "root_process": "cpu_stress_demo",
    },
    "mem_mild": {
        "family": "mem",
        "severity": "mild",
        "is_anomaly": False,
        "root_process": "memory_leak_demo",
    },
    # CPU 故障
    "cpu_busy_loop": {
        "family": "cpu",
        "severity": "severe",
        "is_anomaly": True,
        "root_process": "cpu_stress_demo",
    },
    "cpu_multicore": {
        "family": "cpu",
        "severity": "severe",
        "is_anomaly": True,
        "root_process": "cpu_stress_demo",
    },
    "cpu_oversub": {
        "family": "cpu",
        "severity": "critical",
        "is_anomaly": True,
        "root_process": "cpu_stress_demo",
    },
    # 内存故障
    "mem_moderate": {
        "family": "mem",
        "severity": "moderate",
        "is_anomaly": True,
        "root_process": "memory_leak_demo",
    },
    "mem_severe": {
        "family": "mem",
        "severity": "severe",
        "is_anomaly": True,
        "root_process": "memory_leak_demo",
    },
    "mem_oomsafe": {
        "family": "mem",
        "severity": "critical",
        "is_anomaly": True,
        "root_process": "memory_leak_demo",
    },
}

def infer_scene(meta: Dict[str, Any]) -> Dict[str, Any]:
    scenario_tag = meta.get("scenario_tag") or meta.get("scenario") or "unknown"
    info = SCENARIO_MAP.get(scenario_tag, {
        "family": "other",
        "severity": "unknown",
        "is_anomaly": False,
        "root_process": None,
    })
    return {
        "family": info["family"],
        "scenario_tag": scenario_tag,
        "severity": info["severity"],
        "source": "wukong_inject",
        "is_anomaly": info["is_anomaly"],
        "root_process": info["root_process"],
    }

# ---------- metrics 处理 ----------

def load_metrics(run_dir: str) -> Tuple[Optional[List[Dict[str, Any]]], Optional[Dict[str, Any]]]:
    """读取 metrics CSV，返回 (rows, summary)。
    rows: 每行是 dict，包含 ts_ms 及其它字段
    summary: start_ms, end_ms, mem / cpu 简单统计
    """
    metrics_dir = os.path.join(run_dir, "metrics")
    csv_path = list_first_csv(metrics_dir)
    if not csv_path:
        return None, None

    rows: List[Dict[str, Any]] = []
    with open(csv_path, "r", encoding="utf-8", errors="ignore") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if "ts_ms" not in row:
                continue
            row["ts_ms"] = safe_int(row["ts_ms"], 0)
            for key in list(row.keys()):
                if key == "ts_ms":
                    continue
                v = row[key]
                if v is None or v == "" or v == "-":
                    continue
                try:
                    row[key] = int(v)
                except Exception:
                    # 保留原始字符串
                    pass
            rows.append(row)

    if not rows:
        return [], None

    rows.sort(key=lambda r: r["ts_ms"])
    start_ms = rows[0]["ts_ms"]
    end_ms = rows[-1]["ts_ms"]

    def field_stats(field: str) -> Dict[str, Optional[int]]:
        vals = [r[field] for r in rows if isinstance(r.get(field), int)]
        if not vals:
            return {"min": None, "max": None}
        return {"min": min(vals), "max": max(vals)}

    summary = {
        "start_ms": start_ms,
        "end_ms": end_ms,
        "mem_free_kb": field_stats("mem_free_kb"),
        "mem_available_kb": field_stats("mem_available_kb"),
        "cpu_util_total_x100": field_stats("cpu_util_total_x100"),
    }
    return rows, summary

def downsample_series(rows: List[Dict[str, Any]], fields: List[str], max_points: int = 64) -> Dict[str, List[int]]:
    """对 metrics 做简单下采样，返回 {field: [values...]}，所有字段共用同一组 ts_ms。"""
    if not rows:
        return {"ts_ms": []}
    n = len(rows)
    step = max(1, n // max_points)
    indices = list(range(0, n, step))
    if indices[-1] != n - 1:
        indices.append(n - 1)
    ts = [rows[i]["ts_ms"] for i in indices]
    series: Dict[str, List[int]] = {"ts_ms": ts}
    for field in fields:
        vals: List[int] = []
        for i in indices:
            v = rows[i].get(field)
            if isinstance(v, int):
                vals.append(v)
            else:
                vals.append(0)
        series[field] = vals
    return series

# ---------- events 处理 ----------

def load_events(run_dir: str, run_start_ms: Optional[int], run_end_ms: Optional[int], margin_ms: int = 10000) -> List[Dict[str, Any]]:
    """读取 events/*.jsonl，按 run 窗口过滤。"""
    events_dir = os.path.join(run_dir, "events")
    if not os.path.isdir(events_dir):
        return []
    events: List[Dict[str, Any]] = []
    for name in sorted(os.listdir(events_dir)):
        if not name.lower().endswith(".jsonl"):
            continue
        path = os.path.join(events_dir, name)
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                ts = safe_int(obj.get("ts"))
                if run_start_ms is not None and run_end_ms is not None:
                    if ts < run_start_ms - margin_ms or ts > run_end_ms + margin_ms:
                        continue
                events.append(obj)
    return events

# ---------- procs 处理 ----------

PS_HEADER_RE = re.compile(r"^### ps snapshot at (\d+) ms, reason=(\S+)")

def parse_proc_snapshot_file(path: str) -> Optional[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        lines = [line.rstrip("\n") for line in f]

    if not lines:
        return None

    ts_ms = None
    reason = "unknown"
    m = PS_HEADER_RE.match(lines[0].strip())
    if m:
        ts_ms = int(m.group(1))
        reason = m.group(2)

    procs: List[Tuple[int, Dict[str, Any]]] = []

    for line in lines[1:]:
        if not line.strip():
            continue
        if "PID" in line and "PPID" in line and "RSS" in line:
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        pid = safe_int(parts[0])
        ppid = safe_int(parts[1])
        rss = safe_int(parts[3])
        comm = parts[4]
        procs.append((rss, {
            "pid": pid,
            "ppid": ppid,
            "rss_kb": rss,
            "comm": comm,
        }))

    if not procs:
        return None

    procs.sort(key=lambda x: x[0], reverse=True)
    top = [p[1] for p in procs[:3]]

    return {
        "ts_ms": ts_ms,
        "reason": reason,
        "top_rss": top,
    }

def load_proc_snapshots(run_dir: str) -> List[Dict[str, Any]]:
    procs_dir = os.path.join(run_dir, "procs")
    if not os.path.isdir(procs_dir):
        return []
    snapshots: List[Dict[str, Any]] = []
    for name in sorted(os.listdir(procs_dir)):
        if not name.startswith("procs_") or not name.endswith(".txt"):
            continue
        path = os.path.join(procs_dir, name)
        snap = parse_proc_snapshot_file(path)
        if snap:
            snapshots.append(snap)
    snapshots.sort(key=lambda s: (s["ts_ms"] if s["ts_ms"] is not None else 0))
    return snapshots

# ---------- dmesg / hilog / faultlog 摘要 ----------

def tail_lines(path: str, max_lines: int) -> List[str]:
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()
    return [line.rstrip("\n") for line in lines[-max_lines:]]

def load_dmesg_snippets(run_dir: str, before_lines: int = 20, after_lines: int = 50) -> List[str]:
    snippets: List[str] = []
    before_utf8 = os.path.join(run_dir, "dmesg_before.utf8.log")
    before_raw = os.path.join(run_dir, "dmesg_before.log")
    after_utf8 = os.path.join(run_dir, "dmesg_after.utf8.log")
    after_raw = os.path.join(run_dir, "dmesg_after.log")

    before = before_utf8 if os.path.isfile(before_utf8) else before_raw
    after = after_utf8 if os.path.isfile(after_utf8) else after_raw

    snippets.extend(tail_lines(before, before_lines))
    snippets.extend(tail_lines(after, after_lines))
    return snippets

def load_hilog_snippets(run_dir: str, max_lines: int = 80) -> List[str]:
    path = os.path.join(run_dir, "hilog_text_full.log")
    return tail_lines(path, max_lines)

def summarize_faultlog_new(run_dir: str) -> List[str]:
    base = os.path.join(run_dir, "faultlog_new")
    if not os.path.isdir(base):
        return []
    paths: List[str] = []
    for root, dirs, files in os.walk(base):
        for name in files:
            rel = os.path.relpath(os.path.join(root, name), run_dir)
            paths.append(rel)
    return sorted(paths)

# ---------- root_cause 文本生成 ----------

def build_root_cause_text(scene: Dict[str, Any], metrics_summary: Optional[Dict[str, Any]]) -> str:
    scenario = scene.get("scenario_tag", "unknown")
    family = scene.get("family", "other")
    root_proc = scene.get("root_process")

    if family == "mem":
        mem_stats = (metrics_summary or {}).get("mem_free_kb") or {}
        min_free = mem_stats.get("min")
        if scenario == "mem_oomsafe":
            return f"场景 {scenario} 注入进程 {root_proc or 'unknown'} 持续分配内存，使可用内存降至约 {min_free} kB，形成接近 OOM 的高压力，但在结束后释放，系统未发生崩溃。"
        elif scenario in ("mem_severe", "mem_moderate", "mem_mild"):
            return f"场景 {scenario} 通过进程 {root_proc or 'unknown'} 周期性分配内存，导致可用内存逐步下降，其中最低 mem_free_kb 约为 {min_free} kB。"
        else:
            return f"内存家族场景 {scenario} 导致可用内存出现明显波动。"

    if family == "cpu":
        return f"CPU 家族场景 {scenario} 通过进程 {root_proc or 'unknown'} 产生高 CPU 负载，可能导致系统调度延迟增加和前台应用卡顿。"

    if scene.get("family") == "background":
        return "背景场景，无主动故障注入，仅反映系统在 wukong 压力下的自然行为。"

    return f"场景 {scenario} 属于 {family} 家族，具体根因需结合日志进一步分析。"

# ---------- 主逻辑：构造单个 run 的样本 ----------

def build_sample_for_run(run_dir: str) -> Optional[Dict[str, Any]]:
    meta_path = os.path.join(run_dir, "_run_meta.json")
    if not os.path.isfile(meta_path):
        print(f"[WARN] skip {run_dir}: _run_meta.json not found")
        return None

    meta = read_json(meta_path)
    run_id = meta.get("run_id") or os.path.basename(run_dir)

    run_start_ms = parse_ms_from_date(meta.get("run_start", "")) or meta.get("board_epoch_ms_start")
    run_end_ms = parse_ms_from_date(meta.get("run_end", "")) or meta.get("board_epoch_ms_end")

    scene = infer_scene(meta)

    metrics_rows, metrics_summary = load_metrics(run_dir)
    if metrics_rows is None:
        print(f"[WARN] run {run_id}: no metrics found")
        metrics_rows = []
    metrics_series = downsample_series(metrics_rows, ["mem_free_kb", "cpu_util_total_x100"], max_points=64)

    events = load_events(run_dir, run_start_ms, run_end_ms, margin_ms=10000)
    proc_snaps = load_proc_snapshots(run_dir)
    dmesg_snip = load_dmesg_snippets(run_dir)
    hilog_snip = load_hilog_snippets(run_dir)
    faultlog_files = summarize_faultlog_new(run_dir)

    root_cause_text = build_root_cause_text(scene, metrics_summary)

    label = {
        "family": scene["family"],
        "scenario": scene["scenario_tag"],
        "is_anomaly": scene["is_anomaly"],
        "root_component": {
            "type": "process" if scene.get("root_process") else None,
            "name": scene.get("root_process"),
            "pid_hint": None,
        },
        "root_cause_text": root_cause_text,
        "confidence": 0.9 if scene["scenario_tag"] != "bg_idle" else 0.5,
    }

    sample: Dict[str, Any] = {
        "run_id": run_id,
        "scene": {
            "family": scene["family"],
            "scenario_tag": scene["scenario_tag"],
            "severity": scene["severity"],
            "source": scene["source"],
        },
        "meta": {
            "device_sn": meta.get("device_sn"),
            "script_version": meta.get("script_version"),
            "run_start_ms": run_start_ms,
            "run_end_ms": run_end_ms,
            "time_skew_ms": meta.get("time_skew_ms"),
            "wukong_cmdline": meta.get("wukong_cmdline"),
            "wukong_task_total": meta.get("wukong_task_total"),
        },
        "metrics_window": metrics_summary or {},
        "metrics_series": metrics_series,
        "events": events,
        "proc_snapshots": proc_snaps,
        "logs": {
            "dmesg_snippets": dmesg_snip,
            "hilog_snippets": hilog_snip,
            "faultlog_new_files": faultlog_files,
        },
        "label": label,
    }
    return sample

# ---------- 命令行入口 ----------

def find_run_dirs(root: str) -> List[str]:
    """在 root 下查找形如 2025xxxx_xxxxxx 的 run 目录。"""
    result: List[str] = []
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        if name.startswith("20") and "_" in name:
            result.append(path)
    return result

def main():
    ap = argparse.ArgumentParser(description="Build JSON samples from run_* directories.")
    ap.add_argument("--run-dir", help="单个 run_YYYYMMDD_xxxxxx 目录路径")
    ap.add_argument("--runs-root", help="包含多个 run_* 子目录的根目录")
    ap.add_argument("--output", help="输出 JSONL 文件路径（省略则打印到 stdout）")
    args = ap.parse_args()

    run_dirs: List[str] = []
    if args.run_dir:
        run_dirs.append(os.path.abspath(args.run_dir))
    if args.runs_root:
        root = os.path.abspath(args.runs_root)
        run_dirs.extend(find_run_dirs(root))

    if not run_dirs:
        ap.error("必须指定 --run-dir 或 --runs-root 之一")

    out_f = None
    if args.output:
        out_f = open(args.output, "w", encoding="utf-8")

    def write_line(obj: Dict[str, Any]):
        text = json.dumps(obj, ensure_ascii=False)
        if out_f:
            out_f.write(text + "\n")
        else:
            print(text)

    for rd in run_dirs:
        sample = build_sample_for_run(rd)
        if sample is None:
            continue
        write_line(sample)

    if out_f:
        out_f.close()

if __name__ == "__main__":
    main()
