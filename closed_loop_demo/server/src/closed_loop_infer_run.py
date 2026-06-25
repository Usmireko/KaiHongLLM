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

ALLOWED_FAMILIES = {"cpu", "mem", "net", "background", "io", "other"}
CPU_MAIN_LABELS = {"cpu_single_point_high_load", "cpu_concurrency_scheduling_pressure"}
EXPANDED_RCA_REQUIRED_FIELDS = [
    "family",
    "main_label",
    "display_name_zh",
    "diagnosis_summary",
    "evidence",
    "root_object",
    "cause",
    "symptom",
    "candidate_caveat",
    "safety_caveat",
    "load_pattern_detail",
    "mem_evidence_caveat",
    "net_evidence_caveat",
]
ROOT_OBJECT_KEYS = ["schema_version", "object_type", "object_name", "object_id", "scope", "evidence_refs", "attributes"]
CPU_LOAD_PATTERN_DETAILS = {"busy_loop", "multicore", "oversub"}
EXCLUDED_MAIN_LABELS = {"mem_oomsafe"}

# 3CM-R2-R3: 11-label expanded RCA registry (NET7 + CPU2 + MEM2).
# Suggestion summaries must stay free of command-shaped markers
# ("sh ", "ps ", "top ", "cat ", "|", ";", "`", ...).
LABEL_REGISTRY: Dict[str, Dict[str, Any]] = {
    "cpu_single_point_high_load": {
        "family": "cpu",
        "display_name_zh": "CPU 单点高负载",
        "object_type": "subsystem",
        "object_name": "cpu_scheduler",
        "object_id": "cpu",
        "load_pattern_detail": "busy_loop",
        "action_r1_suggestion_id": "cpu_single_point_manual_triage",
        "action_r1_summary": "人工确认是否为预期压力注入或异常进程；若为演示注入，停止压力源并观察 load/CPU 是否回落；若为真实业务进程，保留日志与进程快照后按审批流程降载、限流、重启异常进程或迁移任务。本 demo 不自动处置。",
        "manual_steps": [
            "确认 run_id 绑定、loadavg/CPU 峰值和触发 marker 是否一致。",
            "确认高负载是否来自预期压力注入；若是，停止注入后观察指标回落。",
            "若疑似真实业务异常，先保留日志与进程快照，再按人工审批流程降载、限流、重启异常进程或迁移任务。",
            "复测 load/CPU 曲线，确认恢复后再结束演示记录。",
        ],
        "cause_summary": "Bounded CPU live-smoke load caused CPU subsystem pressure in the accepted run.",
        "symptom_summary": "Stage1/Stage2 observed sustained high load1 during the CPU live-smoke window.",
        "caveat": "No exact process CPU percentage is asserted without pidstat coverage; busy_loop stays in the cpu family.",
    },
    "cpu_concurrency_scheduling_pressure": {
        "family": "cpu",
        "display_name_zh": "CPU 并发调度压力",
        "object_type": "subsystem",
        "object_name": "cpu_runqueue",
        "object_id": "cpu",
        "load_pattern_detail": "oversub",
        "action_r1_suggestion_id": "cpu_concurrency_manual_triage",
        "action_r1_summary": "人工确认并发 worker 与 runqueue 压力是否符合预期；保留调度与进程快照，按审批流程降载、限流或迁移任务，并复测 CPU/load 回落。本 demo 不自动处置。",
        "manual_steps": [
            "确认 runqueue/load 峰值、worker 数量和触发 marker 属于同一 run_id。",
            "若为演示并发压力，停止压力源并观察调度压力回落。",
            "若为业务并发异常，保留日志与进程快照后按审批流程降载、限流或迁移任务。",
            "复测 CPU/load 曲线并记录人工结论。",
        ],
        "cause_summary": "Oversubscribed concurrent workers caused scheduling pressure on the CPU runqueue in the accepted run.",
        "symptom_summary": "Stage1/Stage2 observed load1 far above core count with many concurrent runnable workers.",
        "caveat": "Concurrency attribution is profile/marker based when pidstat per-PID deltas are incomplete.",
    },
    "mem_process_leak_growth": {
        "family": "mem",
        "display_name_zh": "内存进程泄漏增长",
        "object_type": "process",
        "object_name": "leaking_process_candidate",
        "object_id": "mem_leak",
        "load_pattern_detail": "not_applicable",
        "action_r1_suggestion_id": "mem_leak_manual_triage",
        "action_r1_summary": "人工确认 RSS/PSS 是否持续增长；保留进程快照和内存曲线。非关键进程可按审批流程重启或降级，关键进程先导出日志再人工处置。本 demo 不自动处置。",
        "manual_steps": [
            "确认 RSS/PSS 或可用内存曲线是否持续恶化。",
            "保留疑似进程快照、内存曲线和相关日志。",
            "非关键进程按审批流程重启或降级；关键进程先导出日志再人工处置。",
            "复测内存曲线，确认增长停止或可用内存恢复。",
        ],
        "cause_summary": "A bounded leak-profile injector grew process memory and reduced available memory in the accepted run.",
        "symptom_summary": "Stage1/Stage2 observed mem_available_kb dropping during the MEM live-smoke window.",
        "caveat": "Per-process leak attribution is caveated when RSS deltas are not captured; the family stays mem.",
    },
    "mem_system_pressure_oom_risk": {
        "family": "mem",
        "display_name_zh": "系统内存压力（OOM 风险）",
        "object_type": "subsystem",
        "object_name": "system_memory",
        "object_id": "mem",
        "load_pattern_detail": "not_applicable",
        "action_r1_suggestion_id": "mem_pressure_manual_triage_bounded",
        "action_r1_summary": "人工确认系统内存压力是否接近安全下限；保留内存曲线和进程快照，对非关键负载按审批流程降级、迁移或重启，避免诱发 OOM。本 demo 不自动处置。",
        "manual_steps": [
            "确认 mem_available 与安全下限的距离，避免诱发 OOM。",
            "保留内存曲线、进程快照和触发 marker。",
            "对非关键负载按审批流程降级、迁移或重启；关键业务先升级人工审批。",
            "复测可用内存与系统稳定性。",
        ],
        "cause_summary": "A bounded pressure-profile injector pushed system available memory toward the safety floor in the accepted run.",
        "symptom_summary": "Stage1/Stage2 observed low mem_available_kb under a bounded, non-destructive pressure profile.",
        "caveat": "Pressure stays bounded above the safety floor; no destructive OOM is induced and mem_oomsafe is never emitted.",
    },
    "net_dns_fail": {
        "family": "net",
        "display_name_zh": "DNS 解析失败",
        "object_type": "service",
        "object_name": "dns_resolver",
        "object_id": "net_dns",
        "load_pattern_detail": "not_applicable",
        "action_r1_suggestion_id": "net_dns_manual_resolver_check",
        "action_r1_summary": "人工检查 resolver 与 DNS 连通性；若确认 resolver 异常，按审批流程恢复可信 DNS 并复测。保留网络快照和恢复门结果。本 demo 不自动修改网络配置。",
        "manual_steps": [
            "确认 DNS 失败证据、resolver 状态和 run_id 绑定一致。",
            "保留网络快照、DNS 探测结果和恢复门记录。",
            "若确认 resolver 异常，按审批流程恢复可信 DNS 配置。",
            "复测 DNS 与公网 IP 连通性。",
        ],
        "cause_summary": "域名解析失败，同时直接 IP 连通性仍可用；问题集中在 DNS/resolver 路径，而不是 Wi-Fi 断连、IPv4 缺失或默认路由缺失。",
        "symptom_summary": "Stage1/Stage2 observed DNS resolution failures with resolver block or frozen DNS proxy markers.",
        "caveat": "DNS cleanup caveat: resolver residue (frozen DNS proxy, blocked DNS endpoints, modified resolv targets) must be re-checked and the DNS cleanup verify must pass before declaring recovery.",
    },
    "net_public_ip_unreachable": {
        "family": "net",
        "display_name_zh": "公网 IP 不可达",
        "object_type": "endpoint",
        "object_name": "public_ip_target",
        "object_id": "net_public_ip",
        "load_pattern_detail": "not_applicable",
        "action_r1_suggestion_id": "net_public_ip_manual_reachability_check",
        "action_r1_summary": "人工核对公网 IP 出口连通性、路由和防火墙影响；保留探测结果，按审批流程恢复出口策略并复测。本 demo 不自动修改网络配置。",
        "manual_steps": [
            "确认公网 IP 探测失败与 run_id、接口和路由快照一致。",
            "保留出口连通性、路由和恢复门记录。",
            "若确认出口策略异常，按审批流程恢复可信策略。",
            "复测公网 IP、DNS 和默认路由状态。",
        ],
        "cause_summary": "Egress traffic to the target public IP was blocked while DNS and route context remained intact in the accepted run.",
        "symptom_summary": "Stage1/Stage2 observed public IP probe failures with an egress block marker present.",
        "caveat": "Recovery gate caveat: the egress block must be removed and the recovery gate re-checked before declaring reachability restored.",
    },
    "net_no_default_route": {
        "family": "net",
        "display_name_zh": "默认路由缺失",
        "object_type": "route",
        "object_name": "default_route",
        "object_id": "net_default_route",
        "load_pattern_detail": "not_applicable",
        "action_r1_suggestion_id": "net_route_manual_restore_check",
        "action_r1_summary": "人工核对默认路由缺失、网关备份和外部连通性；按审批流程恢复可信默认路由并复测。本 demo 不自动修改路由。",
        "manual_steps": [
            "确认默认路由缺失证据、网关备份和 run_id 绑定一致。",
            "保留路由表、接口状态和恢复门记录。",
            "按审批流程恢复可信默认路由。",
            "复测默认路由、公网 IP 和 DNS 连通性。",
        ],
        "cause_summary": "The default route was removed, breaking external reachability while the interface stayed up in the accepted run.",
        "symptom_summary": "Stage1/Stage2 observed a missing default route entry with a saved route backup marker.",
        "caveat": "Route evidence is rendered from the routing table snapshot; the recovery gate must confirm public and DNS reachability after restore.",
    },
    "net_wrong_default_route": {
        "family": "net",
        "display_name_zh": "默认路由错误",
        "object_type": "route",
        "object_name": "default_route_gateway",
        "object_id": "net_wrong_route",
        "load_pattern_detail": "not_applicable",
        "action_r1_suggestion_id": "net_gateway_manual_validation_check",
        "action_r1_summary": "人工核对默认网关是否偏离可信备份；确认异常后按审批流程恢复正确网关并复测。本 demo 不自动修改路由。",
        "manual_steps": [
            "确认默认网关与可信备份不一致且属于同一 run_id。",
            "保留路由表、网关备份和外部探测结果。",
            "按审批流程恢复正确默认网关并确认错误路由已移除。",
            "复测公网 IP、DNS 和恢复门结果。",
        ],
        "cause_summary": "The default route pointed at a wrong gateway, black-holing external traffic in the accepted run.",
        "symptom_summary": "Stage1/Stage2 observed a wrong-route marker with external probes failing while the link stayed up.",
        "caveat": "This is the most route-fragile NET fault; final recovery gate must verify the real gateway is restored and the fake gateway absent.",
    },
    "net_no_ipv4_on_iface": {
        "family": "net",
        "display_name_zh": "接口无 IPv4 地址",
        "object_type": "interface",
        "object_name": "wlan_ipv4_address",
        "object_id": "net_ipv4",
        "load_pattern_detail": "not_applicable",
        "action_r1_suggestion_id": "net_ipv4_manual_recovery_check",
        "action_r1_summary": "人工核对接口 IPv4、掩码和网关状态；确认异常后按审批流程恢复可信地址配置并复测。本 demo 不自动修改接口配置。",
        "manual_steps": [
            "确认接口 IPv4 缺失证据与 run_id 绑定一致。",
            "保留接口快照、地址备份和恢复门记录。",
            "按审批流程恢复可信 IPv4、掩码和网关配置。",
            "复测接口地址、默认路由、公网 IP 和 DNS。",
        ],
        "cause_summary": "The interface lost its IPv4 address, breaking L3 connectivity while association stayed up in the accepted run.",
        "symptom_summary": "Stage1/Stage2 observed a missing IPv4 address on the WLAN interface with backups recorded.",
        "caveat": "Interface evidence is rendered from interface snapshots; controlled IP renewal may be needed and the final recovery gate must pass.",
    },
    "net_wifi_disconnect": {
        "family": "net",
        "display_name_zh": "Wi-Fi 断开",
        "object_type": "interface",
        "object_name": "wlan_association",
        "object_id": "net_wifi_assoc",
        "load_pattern_detail": "not_applicable",
        "action_r1_suggestion_id": "net_wifi_manual_reconnect_check",
        "action_r1_summary": "人工核对 Wi-Fi 关联状态、IPv4 和默认路由；确认断连后按审批流程恢复连接并复测。本 demo 不自动修改无线配置。",
        "manual_steps": [
            "确认 Wi-Fi 关联状态异常与 run_id 绑定一致。",
            "保留关联状态、接口地址、路由和恢复门记录。",
            "按审批流程恢复可信 Wi-Fi 连接。",
            "复测 Wi-Fi 关联、IPv4、默认路由、公网 IP 和 DNS。",
        ],
        "cause_summary": "The Wi-Fi association was repeatedly torn down by a bounded injector in the accepted run.",
        "symptom_summary": "Stage1/Stage2 observed the WLAN association leaving COMPLETED state with the disconnect injector marker present.",
        "caveat": "Credential redaction enforced: no PSK or passphrase material appears in evidence; injector stop and recovery gate must both pass.",
    },
    "net_gateway_unreachable": {
        "family": "net",
        "display_name_zh": "默认网关不可达",
        "object_type": "route",
        "object_name": "default_gateway",
        "object_id": "net_gateway",
        "load_pattern_detail": "not_applicable",
        "action_r1_suggestion_id": "net_gateway_manual_reachability_check",
        "action_r1_summary": "人工核对默认网关可达性、路由快照和外部连通性；按审批流程恢复可信网关路径并复测。本 demo 不自动执行恢复动作。",
        "manual_steps": [
            "确认 wlan0 IPv4 与默认路由仍存在，但默认网关 ping 失败。",
            "保留路由表、网关探测、公网 IP/DNS 探测和恢复门记录。",
            "按审批流程核对可信网关或切换到已批准的备用路径。",
            "复测默认网关、公网 IP、DNS 和恢复门结果。",
        ],
        "cause_summary": "The default gateway path was unreachable while the interface kept IPv4 and a default route in the accepted run.",
        "symptom_summary": "Stage1/Stage2 observed gateway ping failure with downstream public IP/DNS degradation.",
        "caveat": "Action R1 is suggestion-only; recovery is not dispatched automatically and must be approved before any route or profile change.",
    },
}

NET_MAIN_LABELS = {k for k, v in LABEL_REGISTRY.items() if v["family"] == "net"}
MEM_MAIN_LABELS = {k for k, v in LABEL_REGISTRY.items() if v["family"] == "mem"}

# Stage1 trigger tag -> (family, main_label) binding for the old two-window demo.
TRIGGER_TAG_LABEL_MAP: Dict[str, Tuple[str, str]] = {
    "auto_cpu": ("cpu", "cpu_single_point_high_load"),
    "auto_cpu_concurrency": ("cpu", "cpu_concurrency_scheduling_pressure"),
    "auto_mem": ("mem", "mem_process_leak_growth"),
    "auto_mem_pressure": ("mem", "mem_system_pressure_oom_risk"),
}
for _net_label in sorted(NET_MAIN_LABELS):
    TRIGGER_TAG_LABEL_MAP["auto_" + _net_label] = ("net", _net_label)

CREDENTIAL_VALUE_RE = re.compile(r"(?i)\b(psk|password|passwd|passphrase|wpa_passphrase|secret)\b\s*[=:]\s*\S+")

def redact_credentials(text: Any) -> str:
    """Never expose PSK/passphrase material in evidence or suggestions."""
    if text is None:
        return ""
    return CREDENTIAL_VALUE_RE.sub(lambda m: m.group(1) + "=<redacted_credential>", str(text))

def expected_binding_from_run_id(run_id: str) -> Tuple[str, str]:
    """Map a real_<tag>_<ts> run_id back to (expected_family, expected_main_label)."""
    rid = str(run_id or "").lower()
    if rid.startswith("real_"):
        rid = rid[len("real_"):]
    # strip trailing _YYYYMMDD_HHMMSS style timestamp tokens
    rid = re.sub(r"(_\d{8}_\d{6}|_\d{10,})$", "", rid)
    if rid in TRIGGER_TAG_LABEL_MAP:
        return TRIGGER_TAG_LABEL_MAP[rid]
    for tag, binding in TRIGGER_TAG_LABEL_MAP.items():
        if rid.startswith(tag + "_") or rid == tag:
            return binding
    if re.search(r"(^|_)cpu(_|$)", rid):
        return ("cpu", "")
    if re.search(r"(^|_)mem(_|$)", rid):
        return ("mem", "")
    if re.search(r"(^|_)net(_|$)", rid):
        return ("net", "")
    return ("", "")

def clean_family_token(value: Any) -> str:
    token = str(value or "").strip().lower()
    token = token.strip(" \t\r\n\"'`,，。:：;；[]{}()")
    token = re.sub(r"[^a-z_]", "", token)
    if token == "memory":
        token = "mem"
    if token == "network":
        token = "net"
    return token if token in ALLOWED_FAMILIES else "other"

def is_3cm_candidate_mode() -> bool:
    return os.environ.get("WK_3CM_CANDIDATE_MODE", "").strip() == "1"

def detect_live_smoke_context(run_dir: Path, out_dir: Path, meta: Dict[str, Any]) -> Dict[str, Any]:
    expected = clean_family_token(
        os.environ.get("WK_3CM_LIVE_SMOKE_EXPECTED_FAMILY")
        or os.environ.get("WK_3CM_EXPECTED_FAMILY")
        or ""
    )
    expected_label = str(os.environ.get("WK_3CM_EXPECTED_MAIN_LABEL") or "").strip().lower()
    if expected_label and expected_label not in LABEL_REGISTRY:
        expected_label = ""
    run_id = run_dir.name

    # Stage1 trigger binding: scenario_tag from the trigger bundle, else the run_id tag.
    # This is a demo binding only (non-leaking live_demo_context), not accepted provenance.
    tag = str(meta.get("scenario_tag") or "").strip().lower()
    tag_family, tag_label = ("", "")
    if tag in TRIGGER_TAG_LABEL_MAP:
        tag_family, tag_label = TRIGGER_TAG_LABEL_MAP[tag]
    if not tag_family:
        tag_family, tag_label = expected_binding_from_run_id(run_id)

    if expected in ("", "other") and tag_family:
        expected = tag_family
    if not expected_label and tag_label:
        expected_label = tag_label
    if expected_label and LABEL_REGISTRY[expected_label]["family"] != expected:
        # env/tag disagreement: keep the family binding, drop the label (fail-closed later)
        expected_label = ""

    load_pattern = str(os.environ.get("WK_3CM_LOAD_PATTERN_DETAIL") or "").strip().lower()
    if load_pattern not in CPU_LOAD_PATTERN_DETAILS:
        load_pattern = ""

    accepted_env = str(os.environ.get("WK_3CM_ACCEPTED_RUN_ID") or "").strip()
    run_id_match = (not accepted_env) or (accepted_env == run_id)
    return {
        "schema_version": "3cm_live_smoke_context_v2",
        "candidate_mode": is_3cm_candidate_mode(),
        "expected_family": expected if expected != "other" else "",
        "expected_main_label": expected_label,
        "load_pattern_detail": load_pattern,
        "trigger_tag": tag,
        "accepted_run_id": accepted_env or run_id,
        "input_bundle_run_id": run_id,
        "run_id_match": run_id_match,
        "source_run_dir": str(run_dir),
        "source_out_dir": str(out_dir),
        "device_id": meta.get("device_id") or meta.get("board_id") or "",
        "ignored_stale_bundles_count": safe_int(os.environ.get("WK_3CM_IGNORED_STALE_BUNDLES_COUNT")) or 0,
    }

def root_object_is_v1(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and list(value.keys()) == ROOT_OBJECT_KEYS
        and value.get("schema_version") == "root_object.v1"
    )

def build_live_smoke_evidence(diag: Dict[str, Any],
                              observations: List[str],
                              suspects_list: List[Dict[str, Any]],
                              pidstat_interval_ms: Optional[int],
                              context: Dict[str, Any],
                              family: str,
                              metric_summary: Optional[Dict[str, Any]] = None,
                              net_state_lines: Optional[List[str]] = None,
                              net_outcome: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    evidence_items = normalize_evidence_items(diag.get("evidence"))
    metric_summary = metric_summary or {}
    net_state_lines = net_state_lines or []
    net_outcome = net_outcome or {}
    out: List[Dict[str, Any]] = []

    def add(eid: str, source: str, text: str, gaps: Optional[List[str]] = None) -> None:
        if not text:
            return
        out.append({
            "id": eid,
            "source": source,
            "text": redact_credentials(text),
            "gaps": gaps or [],
        })

    add(
        "run_id_binding",
        "stage1_stage2_binding",
        "accepted_run_id={0} input_bundle_run_id={1} run_id_match={2} ignored_stale_bundles={3}".format(
            context.get("accepted_run_id"),
            context.get("input_bundle_run_id"),
            str(bool(context.get("run_id_match"))).lower(),
            context.get("ignored_stale_bundles_count", 0),
        ),
    )
    add(
        "live_smoke_context",
        "3cm_candidate_live_smoke",
        "candidate_mode=true expected_family={0} trigger_tag={1}; this binding prevents stale bundles from driving Stage2.".format(
            family, context.get("trigger_tag") or "unknown",
        ),
    )
    if family != "net":
        for idx, obs in enumerate(observations[:4], start=1):
            add(f"observation_{idx}", "run_observation", str(obs))
        for idx, item in enumerate(evidence_items[:3], start=1):
            add(
                f"model_evidence_{idx}",
                str(item.get("source") or "model_summary"),
                str(item.get("text") or ""),
                item.get("gaps") if isinstance(item.get("gaps"), list) else [],
            )

    if family == "cpu":
        load_peak = metric_summary.get("load1_peak_x100")
        cpu_peak = metric_summary.get("cpu_util_peak_x100")
        if load_peak is not None or cpu_peak is not None:
            add(
                "cpu_load_metrics",
                "metrics",
                f"load1_peak_x100={load_peak} cpu_util_peak_x100={cpu_peak} within the accepted run window.",
            )
        if pidstat_interval_ms is None:
            add(
                "pidstat_gap",
                "pidstat",
                "pidstat_0/1 missing or not comparable; CPU attribution falls back to procs/top snapshots, load metrics and the injector/trigger marker bound to this run_id.",
                ["pidstat_missing"],
            )
        if suspects_list:
            names = []
            for s in suspects_list[:3]:
                label = str(s.get("name") or "unknown")
                if s.get("pid") is not None:
                    label += f"(pid={s.get('pid')})"
                names.append(label)
            add(
                "process_snapshot_candidates",
                "procs",
                "process snapshot candidates without confirmed CPU attribution: " + ", ".join(names),
                ["pidstat_missing"] if pidstat_interval_ms is None else [],
            )
    elif family == "mem":
        mem_min = metric_summary.get("mem_available_min_kb")
        mem_drop = metric_summary.get("mem_available_drop_kb")
        if mem_min is not None or mem_drop is not None:
            add(
                "mem_pressure_metrics",
                "metrics",
                f"mem_available_kb min={mem_min} drop_kb={mem_drop} within the accepted run window (bounded, non-destructive profile).",
            )
        else:
            add(
                "mem_metrics_gap",
                "metrics",
                "mem_available_kb window stats unavailable; MEM attribution stays caveated on injector/trigger markers bound to this run_id.",
                ["mem_metrics_missing"],
            )
        rss_named = [s for s in suspects_list[:3] if s.get("name")]
        if rss_named:
            names = []
            for s in rss_named:
                label = str(s.get("name") or "unknown")
                if s.get("pid") is not None:
                    label += f"(pid={s.get('pid')})"
                names.append(label)
            add(
                "mem_process_candidates",
                "procs",
                "memory growth process candidates without confirmed per-process RSS deltas: " + ", ".join(names),
            )
        add(
            "mem_safety_profile",
            "injection_profile",
            "bounded MEM profile only: safety floor stop is enforced and destructive OOM is not induced.",
        )
    elif family == "net":
        expected_label = str(context.get("expected_main_label") or "").strip().lower()
        if expected_label == "net_dns_fail":
            add(
                "net_dns_fault_semantics",
                "net_dns_semantics",
                "DNS resolution failed while direct IP connectivity context remained available; Wi-Fi/IPv4/default route are not the primary fault path.",
            )
        net_keywords = (
            "dns", "resolver", "resolv", "nameserver", "nslookup", "hostname",
            "public ip", "公网", "ping", "wlan", "ipv4", "inet ", "default",
            "route", "gateway", "wpa_state", "cleanup", "recovery_gate",
        )
        net_noise = ("pidstat", "process", "进程", "rss", "pss", "load1", "loadavg", "cpu", "metrics 行数")
        selected_net_lines: List[str] = []
        for ln in net_state_lines:
            clean = str(ln).strip()
            low = clean.lower()
            if not clean or clean.startswith("#") or low.startswith("total "):
                continue
            if any(noise in low for noise in net_noise):
                continue
            if any(key in low for key in net_keywords):
                selected_net_lines.append(clean)
            if len(selected_net_lines) >= 5:
                break
        for idx, ln in enumerate(selected_net_lines, start=1):
            add(f"net_state_{idx}", "net_state_snapshot", ln)
        if not selected_net_lines:
            add(
                "net_state_gap",
                "net_state_snapshot",
                "no DNS/interface/route net state lines were selected for the console summary; raw net_state is preserved in the report for manual review.",
                ["net_state_missing"],
            )
        if net_outcome:
            add(
                "net_outcome_gate",
                "net_outcome",
                "recovery/cleanup gate from _net_outcome.json: recovery_gate_ok={0} injector_stop_ok={1} cleanup={2}".format(
                    net_outcome.get("recovery_gate_ok", net_outcome.get("final_recovery_gate_ok", "unknown")),
                    net_outcome.get("injector_stop_ok", "unknown"),
                    net_outcome.get("cleanup", net_outcome.get("cleanup_status", "unknown")),
                ),
            )
        else:
            add(
                "net_cleanup_baseline_hook",
                "cleanup_baseline_gate",
                "cleanup/baseline validation pending: run the NET cleanup and baseline gate after the demo window and verify it passes.",
                ["net_cleanup_pending"],
            )
    return out[:8]

def normalize_3cm_expanded_rca_schema(diag: Dict[str, Any],
                                      actions_obj: Dict[str, Any],
                                      context: Dict[str, Any],
                                      observations: List[str],
                                      suspects_list: List[Dict[str, Any]],
                                      pidstat_interval_ms: Optional[int],
                                      metric_summary: Optional[Dict[str, Any]] = None,
                                      net_state_lines: Optional[List[str]] = None,
                                      net_outcome: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Conservative 11-label normalization.

    Only runs in 3CM candidate mode with a known expected family binding.
    It never changes the bound family/run_id, never invents unsupported
    labels, and never emits excluded labels (e.g. mem_oomsafe).
    """
    if not isinstance(diag, dict):
        diag = {}
    expected_family = context.get("expected_family") or ""
    if not context.get("candidate_mode") or expected_family not in ("cpu", "mem", "net"):
        diag["family"] = clean_family_token(diag.get("family"))
        return diag
    if not context.get("run_id_match"):
        # never repair a stale/mismatched bundle into a passing schema
        diag["family"] = clean_family_token(diag.get("family"))
        diag["normalization_failed_reason"] = "run_id_binding_mismatch"
        return diag

    family = clean_family_token(diag.get("family"))
    model_label = str(diag.get("main_label") or "").strip().lower()
    expected_label = str(context.get("expected_main_label") or "").strip().lower()

    # Resolve the target label without inventing subtypes:
    # 1) explicit Stage1/env binding wins;
    # 2) else keep a model label only if it is supported AND in the bound family;
    # 3) else fall back to the family default (single-point/leak) with caveat.
    target_label = ""
    if expected_label and expected_label in LABEL_REGISTRY and expected_label not in EXCLUDED_MAIN_LABELS:
        target_label = expected_label
    elif (
        model_label in LABEL_REGISTRY
        and model_label not in EXCLUDED_MAIN_LABELS
        and LABEL_REGISTRY[model_label]["family"] == expected_family
    ):
        target_label = model_label
    elif expected_family == "cpu":
        target_label = "cpu_single_point_high_load"
    elif expected_family == "mem":
        target_label = "mem_process_leak_growth"
    if not target_label:
        # NET without a concrete label binding cannot be safely normalized: fail closed.
        diag["family"] = family
        diag["normalization_failed_reason"] = "net_expected_label_unbound"
        return diag

    info = LABEL_REGISTRY[target_label]
    missing_required = [k for k in EXPANDED_RCA_REQUIRED_FIELDS if k not in diag or diag.get(k) in (None, "", [])]
    invalid_root = not root_object_is_v1(diag.get("root_object"))
    invalid_label = model_label != target_label
    needs_normalize = family != expected_family or missing_required or invalid_root or invalid_label
    if not needs_normalize:
        diag["family"] = expected_family
        diag["family_hint"] = expected_family
        diag["accepted_run_id"] = context.get("accepted_run_id")
        diag["input_bundle_run_id"] = context.get("input_bundle_run_id")
        diag["run_id_match"] = bool(context.get("run_id_match"))
        return diag

    evidence = build_live_smoke_evidence(
        diag=diag,
        observations=observations,
        suspects_list=suspects_list,
        pidstat_interval_ms=pidstat_interval_ms,
        context=context,
        family=expected_family,
        metric_summary=metric_summary,
        net_state_lines=net_state_lines,
        net_outcome=net_outcome,
    )
    evidence_refs = [e.get("id") for e in evidence if e.get("id")][:3]
    pidstat_missing = any("pidstat_missing" in (e.get("gaps") or []) for e in evidence)

    load_pattern_detail = "not_applicable"
    if expected_family == "cpu":
        load_pattern_detail = context.get("load_pattern_detail") or info.get("load_pattern_detail") or "busy_loop"
        if load_pattern_detail not in CPU_LOAD_PATTERN_DETAILS:
            load_pattern_detail = info.get("load_pattern_detail") or "busy_loop"

    if expected_family == "cpu":
        mem_caveat = "No memory root cause is accepted for this CPU-bound live smoke; memory-like observations are treated as secondary/noisy unless separately evidenced."
        net_caveat = "No network root cause is accepted for this CPU-bound live smoke."
    elif expected_family == "mem":
        mem_caveat = "MEM evidence is caveated: " + info["caveat"] + " Weak memory evidence keeps the diagnosis a caveated MEM RCA; the family is never switched."
        net_caveat = "No network root cause is accepted for this MEM-bound live smoke."
    else:
        mem_caveat = "No memory root cause is accepted for this NET-bound live smoke."
        net_caveat = "NET evidence is caveated: " + info["caveat"]

    diagnosis_summary = (
        "3CM live smoke accepted run is bound to the current {0} bundle (main_label={1}). "
        "The RCA is reported as a caveated candidate diagnosis because live evidence coverage is incomplete."
    ).format(expected_family, target_label)

    object_id = info.get("object_id") or expected_family
    if target_label == "mem_process_leak_growth":
        lead = suspects_list[0] if suspects_list else {}
        if lead.get("pid") is not None:
            object_id = f"pid_{lead.get('pid')}"

    previous_family = family
    diag.update({
        "schema_version": 1,
        "fault_state": "fault",
        "family": expected_family,
        "family_hint": expected_family,
        "main_label": target_label,
        "display_name_zh": info["display_name_zh"],
        "severity": diag.get("severity") if diag.get("severity") not in ("", None) else "unknown",
        "confidence": diag.get("confidence", 0.0),
        "diagnosis_summary": diagnosis_summary,
        "root_cause": info["cause_summary"] + " (caveated live-smoke candidate diagnosis)",
        "root_object": {
            "schema_version": "root_object.v1",
            "object_type": info["object_type"],
            "object_name": info["object_name"],
            "object_id": object_id,
            "scope": "accepted_run",
            "evidence_refs": evidence_refs,
            "attributes": {
                "accepted_run_id": context.get("accepted_run_id"),
                "input_bundle_run_id": context.get("input_bundle_run_id"),
                "run_id_match": bool(context.get("run_id_match")),
                "attribution_status": (
                    "process_unknown_pidstat_missing" if (expected_family == "cpu" and pidstat_missing)
                    else "candidate_evidence_available"
                ),
            },
        },
        "cause": {
            "summary": info["cause_summary"],
            "evidence_refs": evidence_refs,
            "caveat": info["caveat"],
        },
        "symptom": {
            "summary": info["symptom_summary"],
            "evidence_refs": evidence_refs,
            "caveat": "Evidence is from the bounded live-smoke window only; gaps are listed per evidence item.",
        },
        "evidence": evidence,
        "evidence_text": [e.get("text") for e in evidence if e.get("text")],
        "candidate_caveat": "Candidate demo output only; do not promote to training/provenance without manual evidence review.",
        "safety_caveat": "Suggestion-only path; no board recovery command is generated or executed automatically.",
        "load_pattern_detail": load_pattern_detail,
        "mem_evidence_caveat": mem_caveat,
        "net_evidence_caveat": net_caveat,
        "accepted_run_id": context.get("accepted_run_id"),
        "input_bundle_run_id": context.get("input_bundle_run_id"),
        "run_id_match": bool(context.get("run_id_match")),
        "run_id_binding": {
            "accepted_run_id": context.get("accepted_run_id"),
            "input_bundle_run_id": context.get("input_bundle_run_id"),
            "run_id_match": bool(context.get("run_id_match")),
        },
        "ignored_stale_bundles_count": context.get("ignored_stale_bundles_count", 0),
        "normalized": True,
        "normalization_reason": "3cm_expanded_11label_live_smoke_schema_repair",
        "previous_family": previous_family,
    })
    append_risk_flag(diag, f"{expected_family}_rca_schema_normalized")
    if expected_family == "cpu" and pidstat_missing:
        append_risk_flag(diag, "pidstat_missing_process_attribution_caveat")
    if expected_family == "mem":
        append_risk_flag(diag, "mem_bounded_profile_caveat")
    if expected_family == "net":
        append_risk_flag(diag, "net_cleanup_baseline_gate_caveat")
    return diag

def build_suspect_processes(candidates: List[Dict[str, Any]], limit: int = 5) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in candidates[:limit]:
        pid = item.get("pid")
        cmd = item.get("process_cmdline") or item.get("observed_cmdline") or item.get("cmd") or item.get("comm") or item.get("name")
        name = item.get("name") or item.get("comm") or cmd
        out.append({
            "pid": pid,
            "name": name,
            "observed_cmdline": cmd,
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
        name = item.get("name") or item.get("comm") or item.get("process_cmdline") or item.get("observed_cmdline") or item.get("cmd")
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
    name = item.get("name") or item.get("comm") or item.get("process_cmdline") or item.get("observed_cmdline") or item.get("cmd")
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

    for key in (
        "family_hint",
        "main_label",
        "display_name_zh",
        "diagnosis_summary",
        "root_object",
        "cause",
        "symptom",
        "candidate_caveat",
        "safety_caveat",
        "load_pattern_detail",
        "mem_evidence_caveat",
        "net_evidence_caveat",
        "accepted_run_id",
        "input_bundle_run_id",
        "run_id_match",
        "run_id_binding",
        "ignored_stale_bundles_count",
        "normalized",
        "normalization_reason",
    ):
        if key in diag:
            compact[key] = diag.get(key)

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
        name = item.get("name") or item.get("comm") or item.get("process_cmdline") or item.get("observed_cmdline") or item.get("cmd") or "proc"
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
                family = clean_family_token(val.strip().split()[0])
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
    metric_summary: Dict[str, Any] = {}
    net_state_lines: List[str] = []
    net_outcome: Dict[str, Any] = {}
    live_smoke_context: Dict[str, Any] = {}
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
        live_smoke_context = detect_live_smoke_context(run_dir, out_dir, meta)
        if live_smoke_context.get("candidate_mode") and not live_smoke_context.get("run_id_match"):
            # Stage1->Stage2 run binding: fail closed before inference on mismatch.
            raise RuntimeError(
                "3CM run_id binding mismatch: accepted_run_id={0} input_bundle_run_id={1}".format(
                    live_smoke_context.get("accepted_run_id"),
                    live_smoke_context.get("input_bundle_run_id"),
                )
            )
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
        if metrics_rows:
            _load_stats = calc_stats([safe_int(r.get("load1_x100")) for r in metrics_rows])
            _cpu_stats = calc_stats([safe_int(r.get("cpu_util_total_x100")) for r in metrics_rows])
            _mem_stats = calc_stats([safe_int(r.get("mem_available_kb")) for r in metrics_rows])
            _mem_drop = None
            if _mem_stats["min"] is not None and _mem_stats["max"] is not None:
                _mem_drop = _mem_stats["max"] - _mem_stats["min"]
            metric_summary = {
                "load1_peak_x100": _load_stats["max"],
                "cpu_util_peak_x100": _cpu_stats["max"],
                "mem_available_min_kb": _mem_stats["min"],
                "mem_available_drop_kb": _mem_drop,
            }

        # NET live-smoke evidence (credential-redacted): board net state snapshot
        # and the optional Wukong-path cleanup gate outcome.
        net_state_path = run_dir / "snapshots" / "net_state.txt"
        if net_state_path.exists():
            net_state_lines = [redact_credentials(ln) for ln in read_lines_tail(net_state_path, 80)]
        for _outcome_cand in (run_dir / "net" / "_net_outcome.json", run_dir / "_net_outcome.json"):
            if _outcome_cand.exists():
                try:
                    _outcome_obj = json.loads(_outcome_cand.read_text(encoding="utf-8", errors="ignore"))
                    if isinstance(_outcome_obj, dict):
                        net_outcome = _outcome_obj
                except Exception:
                    pass
                break

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
                "process_cmdline": proc.get("comm"),
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
        if live_smoke_context.get("candidate_mode") and live_smoke_context.get("expected_family"):
            user_message += (
                "\n\n[3CM_LIVE_SMOKE_CONTEXT]\n"
                f"  accepted_run_id={live_smoke_context.get('accepted_run_id')}\n"
                f"  expected_family={live_smoke_context.get('expected_family')}\n"
                "  This is a candidate live-smoke binding, not a training label. "
                "Keep weak evidence caveated and do not invent process attribution.\n"
                "  Required output fields include family, main_label, diagnosis_summary, evidence, "
                "root_object(root_object.v1), cause, symptom, and RCA caveats. "
                "Do not emit Action R1 fields in Stage2; Stage3 generates suggestion-only Action R1 separately.\n"
            )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        # GT/OBS separation: prompt_material.json is tailed back into the Stage2
        # model prompt, so the label-bearing binding fields must not leak into it.
        live_smoke_context_prompt_safe = {
            k: v for k, v in live_smoke_context.items()
            if k not in ("expected_main_label", "trigger_tag", "load_pattern_detail")
        }
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
            "live_smoke_context": live_smoke_context_prompt_safe,
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
        try:
            summary = stage2_summarize(tokenizer, model, analysis)
        except RuntimeError as exc:
            msg = str(exc)
            if live_smoke_context.get("candidate_mode") and "command-shaped field" in msg:
                log("[closed_loop] candidate Stage1 summary reused after command-shaped Stage2 summarizer refusal")
                summary = analysis
            else:
                raise

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
        diagnosis_v2 = normalize_3cm_expanded_rca_schema(
            diag=diagnosis_v2,
            actions_obj=actions_v2,
            context=live_smoke_context or {},
            observations=observations,
            suspects_list=suspects_list,
            pidstat_interval_ms=pidstat_interval_ms,
            metric_summary=metric_summary,
            net_state_lines=net_state_lines,
            net_outcome=net_outcome,
        )
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

