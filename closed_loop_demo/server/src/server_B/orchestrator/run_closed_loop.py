#!/usr/bin/env python3

import argparse
import copy
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

try:
    from action_r1_strategy_library import get_strategy
except ImportError:
    from server_B.orchestrator.action_r1_strategy_library import get_strategy


PRIMARY_3CM_ADAPTER = "/home/xrh/qwen3_os_fault/outputs/training_expanded_300_rca_aware_3cg_r1_20260611/train_3cg_r1_20260611_104816/adapter"
ROLLBACK_3CM_ADAPTER = "/home/xrh/qwen3_os_fault/outputs/training_mixed_11class_rca_aware_3bz_r1_20260609/train_3bz_r1_20260609_100500/adapter"
LABEL_FALLBACK_3CM_ADAPTER = "/home/xrh/qwen3_os_fault/outputs/training_mixed_11class_3bs_r4_20260608/train_r4_20260608_165610/adapter"
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
CPU_MAIN_LABELS = {"cpu_single_point_high_load", "cpu_concurrency_scheduling_pressure"}
MEM_MAIN_LABELS = {"mem_process_leak_growth", "mem_system_pressure_oom_risk"}
NET_MAIN_LABELS = {
    "net_dns_fail",
    "net_public_ip_unreachable",
    "net_no_default_route",
    "net_wrong_default_route",
    "net_no_ipv4_on_iface",
    "net_wifi_disconnect",
    "net_gateway_unreachable",
}
SUPPORTED_MAIN_LABELS = {label: "cpu" for label in CPU_MAIN_LABELS}
SUPPORTED_MAIN_LABELS.update({label: "mem" for label in MEM_MAIN_LABELS})
SUPPORTED_MAIN_LABELS.update({label: "net" for label in NET_MAIN_LABELS})
EXCLUDED_MAIN_LABELS = {"mem_oomsafe"}
SUPPORTED_FAMILIES = {"cpu", "mem", "net"}
CPU_LOAD_PATTERN_DETAILS = {"busy_loop", "multicore", "oversub"}
# legacy 3CM-R2-R1 outputs stored the CPU label itself in load_pattern_detail
CPU_LOAD_PATTERN_ALLOWED = CPU_LOAD_PATTERN_DETAILS | CPU_MAIN_LABELS
CREDENTIAL_SHAPED_KEYS = {"psk", "password", "passwd", "passphrase", "wpa_passphrase", "secret", "credential"}
CREDENTIAL_VALUE_RE = re.compile(r"(?i)\b(psk|password|passwd|passphrase|wpa_passphrase|secret)\b\s*[=:]\s*(?!<redacted_credential>)\S+")
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
DISALLOWED_CANDIDATE_ACTION_KEYS = {
    "actions",
    "actions_v2",
    "suggested_actions",
    "action_plan",
    "action_plans",
    "recovery",
    "recovery_plan",
    "recovery_actions",
    "recovery_commands",
    "recovery_steps",
    "commands",
    "downlink",
    "downlink_command",
    "action_downlink",
    "actions_device",
    "latest_actions_device",
    "actions_device_txt",
}
RECOVERY_STEP_HINT_KEYS = {"recovery_step", "recovery_steps", "recovery_instructions"}
STAGE2_ACTION_HINT_FIELDS = {"action_r1_suggestion_id", "action_r1_summary"}
STAGE2_CONTROL_HINT_FIELDS = {
    "execution_enabled",
    "manual_approval_required",
    "automatic_recovery_enabled",
    "action_command_enabled",
}
PROCESS_CMD_METADATA_ROOTS = {
    "candidate_processes",
    "primary_suspect",
    "secondary_suspects",
    "suspect_processes",
}
PROCESS_CMDLINE_METADATA_KEYS = {"observed_cmdline", "process_cmdline"}
STAGE3_ACTION_R1_REGISTRY = {
    "cpu_single_point_high_load": {
        "action_r1_suggestion_id": "cpu_single_point_manual_triage",
        "action_r1_summary": "人工确认是否为非关键高负载进程；保留日志与进程快照后，按审批流程降优先级、限流、温和终止异常进程或迁移任务。当前系统不自动处置。",
        "manual_steps": [
            "确认 run_id 绑定、loadavg/CPU 峰值和触发 marker 是否一致。",
            "确认高负载是否来自已知非关键压力源；若是，按审批流程停止压力源后观察指标回落。",
            "若疑似真实业务异常，先保留日志与进程快照，再按人工审批流程降载、限流、重启异常进程或迁移任务。",
            "复测 load/CPU 曲线，确认恢复后再结束处置记录。",
        ],
    },
    "cpu_concurrency_scheduling_pressure": {
        "action_r1_suggestion_id": "cpu_concurrency_manual_triage",
        "action_r1_summary": "人工确认并发 worker 与 runqueue 压力来源；保留调度与进程快照，按审批流程降载、限流或迁移任务，并复测 CPU/load 回落。当前系统不自动处置。",
        "manual_steps": [
            "确认 runqueue/load 峰值、worker 数量和触发 marker 属于同一 run_id。",
            "若为已知非关键并发压力源，按审批流程停止压力源并观察调度压力回落。",
            "若为业务并发异常，保留日志与进程快照后按审批流程降载、限流或迁移任务。",
            "复测 CPU/load 曲线并记录人工结论。",
        ],
    },
    "mem_process_leak_growth": {
        "action_r1_suggestion_id": "mem_leak_manual_triage",
        "action_r1_summary": "人工确认 RSS/PSS 是否持续增长；保留进程快照和内存曲线。非关键进程可按审批流程重启或降级，关键进程先导出日志再人工处置。当前系统不自动处置。",
        "manual_steps": [
            "确认 RSS/PSS 或可用内存曲线是否持续恶化。",
            "保留疑似进程快照、内存曲线和相关日志。",
            "非关键进程按审批流程重启或降级；关键进程先导出日志再人工处置。",
            "复测内存曲线，确认增长停止或可用内存恢复。",
        ],
    },
    "mem_system_pressure_oom_risk": {
        "action_r1_suggestion_id": "mem_pressure_manual_triage_bounded",
        "action_r1_summary": "人工确认系统内存压力是否接近安全下限；保留内存曲线和进程快照，对非关键负载按审批流程降级、迁移或重启，避免诱发 OOM。当前系统不自动处置。",
        "manual_steps": [
            "确认 mem_available 与安全下限的距离，避免诱发 OOM。",
            "保留内存曲线、进程快照和触发 marker。",
            "对非关键负载按审批流程降级、迁移或重启；关键业务先升级人工审批。",
            "复测可用内存与系统稳定性。",
        ],
    },
    "net_dns_fail": {
        "action_r1_suggestion_id": "net_dns_manual_resolver_check",
        "action_r1_summary": "人工检查 resolver 与 DNS 连通性；若确认 resolver 异常，按审批流程恢复可信 DNS 并复测。保留网络快照和恢复门结果。当前系统不自动修改网络配置。",
        "manual_steps": [
            "确认 DNS 失败证据、resolver 状态和 run_id 绑定一致。",
            "保留网络快照、DNS 探测结果和恢复门记录。",
            "若确认 resolver 异常，按审批流程恢复可信 DNS 配置。",
            "复测 DNS 与公网 IP 连通性。",
        ],
    },
    "net_public_ip_unreachable": {
        "action_r1_suggestion_id": "net_public_ip_manual_reachability_check",
        "action_r1_summary": "人工核对公网 IP 出口连通性、路由和防火墙影响；保留探测结果，按审批流程恢复出口策略并复测。当前系统不自动修改网络配置。",
        "manual_steps": [
            "确认公网 IP 探测失败与 run_id、接口和路由快照一致。",
            "保留出口连通性、路由和恢复门记录。",
            "若确认出口策略异常，按审批流程恢复可信策略。",
            "复测公网 IP、DNS 和默认路由状态。",
        ],
    },
    "net_no_default_route": {
        "action_r1_suggestion_id": "net_route_manual_restore_check",
        "action_r1_summary": "人工核对默认路由缺失、网关备份和外部连通性；按审批流程恢复可信默认路由并复测。当前系统不自动修改路由。",
        "manual_steps": [
            "确认默认路由缺失证据、网关备份和 run_id 绑定一致。",
            "保留路由表、接口状态和恢复门记录。",
            "按审批流程恢复可信默认路由。",
            "复测默认路由、公网 IP 和 DNS 连通性。",
        ],
    },
    "net_wrong_default_route": {
        "action_r1_suggestion_id": "net_gateway_manual_validation_check",
        "action_r1_summary": "人工核对默认网关是否偏离可信备份；确认异常后按审批流程恢复正确网关并复测。当前系统不自动修改路由。",
        "manual_steps": [
            "确认默认网关与可信备份不一致且属于同一 run_id。",
            "保留路由表、网关备份和外部探测结果。",
            "按审批流程恢复正确默认网关并确认错误路由已移除。",
            "复测公网 IP、DNS 和恢复门结果。",
        ],
    },
    "net_no_ipv4_on_iface": {
        "action_r1_suggestion_id": "net_ipv4_manual_recovery_check",
        "action_r1_summary": "人工核对接口 IPv4、掩码和网关状态；确认异常后按审批流程恢复可信地址配置并复测。当前系统不自动修改接口配置。",
        "manual_steps": [
            "确认接口 IPv4 缺失证据与 run_id 绑定一致。",
            "保留接口快照、地址备份和恢复门记录。",
            "按审批流程恢复可信 IPv4、掩码和网关配置。",
            "复测接口地址、默认路由、公网 IP 和 DNS。",
        ],
    },
    "net_wifi_disconnect": {
        "action_r1_suggestion_id": "net_wifi_manual_reconnect_check",
        "action_r1_summary": "人工核对 Wi-Fi 关联状态、IPv4 和默认路由；确认断连后按审批流程恢复连接并复测。当前系统不自动修改无线配置。",
        "manual_steps": [
            "确认 Wi-Fi 关联状态异常与 run_id 绑定一致。",
            "保留关联状态、接口地址、路由和恢复门记录。",
            "按审批流程恢复可信 Wi-Fi 连接。",
            "复测 Wi-Fi 关联、IPv4、默认路由、公网 IP 和 DNS。",
        ],
    },
    "net_gateway_unreachable": {
        "action_r1_suggestion_id": "net_gateway_manual_reachability_check",
        "action_r1_summary": "Manual review: confirm IPv4 and default route remain present while the default gateway is unreachable; preserve gateway, public-IP, DNS, route, and recovery-gate evidence. The system does not automatically change routes.",
        "manual_steps": [
            "Confirm run_id-bound fault evidence: interface IPv4 present, default route present, gateway ping failed, and public-IP/DNS failures are downstream symptoms.",
            "Preserve route table, gateway probe, public-IP probe, DNS probe, and recovery-gate snapshots.",
            "If approved, inspect the gateway path or restore a trusted route/profile manually outside this pipeline.",
            "Retest gateway, public IP, DNS, and recovery gate before closing the incident.",
        ],
    },
    "legacy_net_wifi_auth_fail_wrong_psk_static_only": {
        "action_r1_suggestion_id": "net_wifi_credential_manual_review",
        "action_r1_summary": "人工复核 Wi-Fi 凭据配置状态；确认认证异常后按审批流程恢复可信配置并复测。报告保持凭据脱敏。当前系统不自动修改无线配置。",
        "manual_steps": [
            "确认认证失败证据与 run_id 绑定一致，且凭据材料已脱敏。",
            "保留认证状态、接口状态和恢复门记录。",
            "按审批流程恢复可信 Wi-Fi 配置。",
            "复测 Wi-Fi 关联、IPv4、默认路由、公网 IP 和 DNS。",
        ],
    },
}


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundle", help="path to bundle_*.tar.gz")
    ap.add_argument("--run_dir", help="path to run directory")
    ap.add_argument("--out_root", default="server_B/storage/runs")
    return ap.parse_args()


def run_ingest(bundle: Path, out_root: Path) -> Path:
    script = Path(__file__).resolve().parents[1] / "ingest" / "ingest_bundle.py"
    cmd = [sys.executable, str(script), "--bundle", str(bundle), "--out_root", str(out_root)]
    result = subprocess.check_output(cmd, text=True).strip().splitlines()
    if not result:
        raise RuntimeError("ingest_bundle.py returned empty output")
    return Path(result[-1]).resolve()


def run_infer(run_dir: Path) -> Path:
    out_dir = run_dir / "_server_out"
    out_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    if not env.get("WK_QWEN3_ENABLE_STAGE2"):
        env["WK_QWEN3_ENABLE_STAGE2"] = "0"
    if is_3cm_candidate_mode():
        env["WK_3CM_RUNTIME_PROOF_PATH"] = str(out_dir / "adapter_runtime_proof.json")

    script_candidates = [
        Path(os.environ["WK_CLOSED_LOOP_INFER_SCRIPT"]) if os.environ.get("WK_CLOSED_LOOP_INFER_SCRIPT") else None,
        Path(__file__).resolve().parents[2] / "closed_loop_infer_run.py",
        Path("/home/xrh/qwen3_os_fault/closed_loop_demo/server/src/closed_loop_infer_run.py"),
    ]
    script = next((p for p in script_candidates if p is not None and p.exists()), script_candidates[1])
    cmd = [sys.executable, str(script), "--run_dir", str(run_dir), "--out_dir", str(out_dir)]
    rc = subprocess.call(cmd, env=env)
    if rc != 0:
        raise RuntimeError(f"closed_loop_infer_run.py failed rc={rc}")
    return out_dir


def truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def is_3cm_candidate_mode() -> bool:
    return truthy_env("WK_3CM_CANDIDATE_MODE") or truthy_env("WK_3CM_ACTION_SUGGESTION_ONLY")


def atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def write_adapter_selection(out_dir: Path) -> None:
    if not is_3cm_candidate_mode():
        return
    selection = {
        "schema_version": "3cm_adapter_selection_v1",
        "mode": "candidate_shadow_only",
        "primary_adapter": os.environ.get("WK_QWEN3_ADAPTER_PRIMARY") or os.environ.get("QWEN3_ADAPTER_DIR", ""),
        "rollback_adapter": os.environ.get("WK_QWEN3_ADAPTER_ROLLBACK", ""),
        "label_fallback_adapter": os.environ.get("WK_QWEN3_ADAPTER_LABEL_FALLBACK", ""),
        "qwen3_adapter_dir": os.environ.get("QWEN3_ADAPTER_DIR", ""),
        "stage2_enabled": os.environ.get("WK_QWEN3_ENABLE_STAGE2", ""),
    }
    checks = {
        "primary_matches_3cg_r1": selection["primary_adapter"] == PRIMARY_3CM_ADAPTER,
        "qwen3_adapter_dir_matches_primary": selection["qwen3_adapter_dir"] == PRIMARY_3CM_ADAPTER,
        "rollback_matches_3bz_r1": selection["rollback_adapter"] == ROLLBACK_3CM_ADAPTER,
        "label_fallback_matches_3bs_r4": selection["label_fallback_adapter"] == LABEL_FALLBACK_3CM_ADAPTER,
        "stage2_enabled": selection["stage2_enabled"] == "1",
    }
    selection["checks"] = checks
    selection["status"] = "PASS" if all(checks.values()) else "FAIL"
    atomic_write_json(out_dir / "adapter_selection.json", selection)
    if selection["status"] != "PASS":
        raise RuntimeError("3CM adapter selection preflight failed")


def require_3cm_runtime_proof(out_dir: Path) -> None:
    if not is_3cm_candidate_mode():
        return
    infer_error = out_dir / "infer_error.txt"
    if infer_error.exists() and infer_error.stat().st_size > 0:
        raise RuntimeError("3CM candidate inference fell back; infer_error.txt exists")
    proof_path = out_dir / "adapter_runtime_proof.json"
    if not proof_path.exists():
        raise RuntimeError("3CM adapter runtime proof missing")
    proof = json.loads(proof_path.read_text(encoding="utf-8"))
    checks = {
        "model_loaded": proof.get("model_loaded") is True,
        "adapter_loaded": proof.get("adapter_loaded") is True,
        "generation_ok": proof.get("generation_ok") is True,
        "candidate_adapter": proof.get("adapter") == PRIMARY_3CM_ADAPTER,
    }
    if not all(checks.values()):
        raise RuntimeError("3CM adapter runtime proof failed: " + json.dumps(checks, ensure_ascii=False))


def write_actions_device(actions_json: Path, out_path: Path) -> int:
    if not actions_json.exists():
        out_path.write_text("", encoding="utf-8")
        return 0

    data = json.loads(actions_json.read_text(encoding="utf-8"))
    actions: List[dict] = data.get("actions") or []
    lines: List[str] = []
    for action in actions:
        cmd = action.get("cmd")
        if cmd:
            lines.append(str(cmd))

    out_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return len(lines)


def strip_command_fields(value: Any) -> Any:
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            key = str(k)
            if key.lower() in COMMAND_SHAPED_KEYS:
                continue
            out[key] = strip_command_fields(v)
        return out
    if isinstance(value, list):
        return [strip_command_fields(v) for v in value]
    return value


def contains_command_field(value: Any) -> bool:
    if isinstance(value, dict):
        for k, v in value.items():
            if str(k).lower() in COMMAND_SHAPED_KEYS:
                return True
            if contains_command_field(v):
                return True
    elif isinstance(value, list):
        return any(contains_command_field(v) for v in value)
    return False


def is_allowed_command_template_path(path: List[str]) -> bool:
    return bool(path) and path[-1] == "command_template" and "machine_suggestions" in path[:-1]


def contains_command_like_value(value: Any, path: List[str] = None) -> bool:
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
            next_path = path + [str(k)]
            if contains_command_like_value(v, next_path):
                return True
        return False
    if isinstance(value, list):
        return any(contains_command_like_value(v, path + [f"[{idx}]"]) for idx, v in enumerate(value))
    if isinstance(value, str):
        return any(re.search(pattern, value) for pattern in command_patterns)
    return False


def contains_command_template_outside_machine_suggestions(value: Any, path: List[str] = None) -> bool:
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


def collect_machine_suggestion_lists(value: Any, path: List[str] = None) -> List[Tuple[List[str], Any]]:
    path = path or []
    found: List[Tuple[List[str], Any]] = []
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


def validate_action_r1_strategy_safety(value: Any) -> List[str]:
    failures: List[str] = []
    if contains_command_template_outside_machine_suggestions(value):
        failures.append("command_template_outside_machine_suggestions")
    if contains_command_field(value):
        failures.append("disallowed_command_field")
    if contains_command_like_value(value):
        failures.append("disallowed_command_like_value_outside_machine_suggestions")

    def walk(node: Any, path: List[str] = None) -> None:
        path = path or []
        if isinstance(node, dict):
            for k, v in node.items():
                key = str(k)
                key_l = key.lower()
                next_path = path + [key]
                where = ".".join(next_path)
                if key_l in {"execution_enabled", "automatic_recovery_enabled", "action_command_enabled"} and v is not False:
                    failures.append(f"{where}_not_false")
                if key_l == "manual_approval_required" and v is not True:
                    failures.append(f"{where}_not_true")
                if key_l == "auto_execute" and v is not False:
                    failures.append(f"{where}_not_false")
                if key_l == "dispatch_channel" and str(v) != "none":
                    failures.append(f"{where}_not_none")
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
            failures.append(f"{where}_not_nonempty_list")
            continue
        for idx, item in enumerate(items):
            item_where = f"{where}.[{idx}]"
            if not isinstance(item, dict):
                failures.append(f"{item_where}_not_object")
                continue
            missing = sorted(field for field in REQUIRED_MACHINE_SUGGESTION_FIELDS if field not in item)
            if missing:
                failures.append(f"{item_where}.missing_fields:{','.join(missing)}")
            if item.get("risk") not in {"low", "medium", "high"}:
                failures.append(f"{item_where}.risk_invalid")
            if not str(item.get("command_template") or "").strip():
                failures.append(f"{item_where}.command_template_missing")
            if item.get("requires_manual_approval") is not True:
                failures.append(f"{item_where}.requires_manual_approval_not_true")
            if item.get("auto_execute") is not False:
                failures.append(f"{item_where}.auto_execute_not_false")
            if item.get("dispatch_channel") != "none":
                failures.append(f"{item_where}.dispatch_channel_not_none")
    return failures


def contains_credential_material(value: Any) -> bool:
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


def is_process_cmdline_metadata_path(path: List[str], key: str) -> bool:
    if key.lower() not in PROCESS_CMDLINE_METADATA_KEYS or not path:
        return False
    return path[0] in PROCESS_CMD_METADATA_ROOTS


def contains_candidate_action_payload(value: Any, path: List[str] = None) -> bool:
    path = path or []
    if isinstance(value, dict):
        for k, v in value.items():
            key = str(k).lower()
            next_path = path + [str(k)]
            if key in DISALLOWED_CANDIDATE_ACTION_KEYS or key in COMMAND_SHAPED_KEYS:
                return True
            if key in RECOVERY_STEP_HINT_KEYS and (contains_command_field(v) or contains_command_like_value(v)):
                return True
            if key in PROCESS_CMDLINE_METADATA_KEYS:
                if not is_process_cmdline_metadata_path(path, key):
                    return True
                if contains_command_like_value(v):
                    return True
            if contains_candidate_action_payload(v, next_path):
                return True
    elif isinstance(value, list):
        return any(contains_candidate_action_payload(v, path + [f"[{idx}]"]) for idx, v in enumerate(value))
    return False


def is_process_cmd_metadata_path(path: List[str], key: str) -> bool:
    if key.lower() != "cmd" or not path:
        return False
    return path[0] in PROCESS_CMD_METADATA_ROOTS


def format_json_path(path: List[str]) -> str:
    out = ""
    for part in path:
        if part.startswith("["):
            out += part
        else:
            out = f"{out}.{part}" if out else part
    return out


def normalize_process_cmd_metadata(value: Any, path: List[str] = None) -> Tuple[Any, List[Dict[str, Any]]]:
    path = path or []
    normalized: List[Dict[str, Any]] = []
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            key = str(k)
            next_path = path + [key]
            if is_process_cmd_metadata_path(path, key):
                target_key = "observed_cmdline"
                if target_key in value:
                    target_key = "process_cmdline"
                redacted = contains_command_like_value(v)
                out[target_key] = "<redacted_command_like_process_cmdline>" if redacted else v
                normalized.append(
                    {
                        "path": format_json_path(next_path),
                        "field": "cmd",
                        "normalized_to": target_key,
                        "kind": "process_observation_metadata",
                        "value_preview": "<redacted_command_like_process_cmdline>" if redacted else str(v)[:120],
                        "redacted_command_like_value": redacted,
                    }
                )
                continue
            cleaned, nested = normalize_process_cmd_metadata(v, next_path)
            out[key] = cleaned
            normalized.extend(nested)
        return out, normalized
    if isinstance(value, list):
        out_list: List[Any] = []
        for idx, item in enumerate(value):
            cleaned, nested = normalize_process_cmd_metadata(item, path + [f"[{idx}]"])
            out_list.append(cleaned)
            normalized.extend(nested)
        return out_list, normalized
    return value, normalized


def strip_stage2_action_hints(value: Any, path: str = "") -> Tuple[Any, List[Dict[str, Any]]]:
    stripped: List[Dict[str, Any]] = []
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            key = str(k)
            key_l = key.lower()
            next_path = f"{path}.{key}" if path else key
            if key_l in STAGE2_ACTION_HINT_FIELDS or key_l in STAGE2_CONTROL_HINT_FIELDS:
                stripped.append(
                    {
                        "path": next_path,
                        "field": key_l,
                        "kind": "action_r1_hint" if key_l in STAGE2_ACTION_HINT_FIELDS else "stage2_control_hint",
                        "value_preview": str(v)[:240],
                        "value_type": type(v).__name__,
                        "value_length": len(str(v)),
                        "contains_command_like_value": contains_command_like_value(v),
                        "contains_credential_material": contains_credential_material(v),
                    }
                )
                continue
            cleaned, nested = strip_stage2_action_hints(v, next_path)
            out[key] = cleaned
            stripped.extend(nested)
        return out, stripped
    if isinstance(value, list):
        out_list: List[Any] = []
        for idx, item in enumerate(value):
            cleaned, nested = strip_stage2_action_hints(item, f"{path}[{idx}]")
            out_list.append(cleaned)
            stripped.extend(nested)
        return out_list, stripped
    return value, stripped


def stage2_control_hints_are_safe(stripped_hints: List[Dict[str, Any]]) -> Tuple[bool, List[str]]:
    failures: List[str] = []
    for item in stripped_hints:
        field = str(item.get("field") or "")
        preview = str(item.get("value_preview") or "").strip().lower()
        if field in ("execution_enabled", "automatic_recovery_enabled", "action_command_enabled") and preview == "true":
            failures.append(f"unsafe_stage2_control_hint:{item.get('path')}")
        if field == "manual_approval_required" and preview == "false":
            failures.append(f"unsafe_stage2_control_hint:{item.get('path')}")
    return not failures, failures


def root_object_is_v1(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and list(value.keys()) == ROOT_OBJECT_KEYS
        and value.get("schema_version") == "root_object.v1"
    )


def expected_binding_from_env() -> Tuple[str, str]:
    family = str(os.environ.get("WK_3CM_EXPECTED_FAMILY") or os.environ.get("WK_3CM_LIVE_SMOKE_EXPECTED_FAMILY") or "").strip().lower()
    label = str(os.environ.get("WK_3CM_EXPECTED_MAIN_LABEL") or "").strip().lower()
    if family not in SUPPORTED_FAMILIES:
        family = ""
    if label not in SUPPORTED_MAIN_LABELS:
        label = ""
    return family, label


def validate_stage2_expanded_rca_schema(out_dir: Path, run_dir: Path) -> Dict[str, Any]:
    diag_path = out_dir / "diagnosis_v2.json"
    if not diag_path.exists():
        diag_path = out_dir / "diagnosis.json"
    failures: List[str] = []
    if not diag_path.exists():
        failures.append("diagnosis_missing")
        report = {"status": "FAIL", "failures": failures, "diagnosis_path": str(diag_path)}
        atomic_write_json(out_dir / "stage2_schema_validation.json", report)
        raise RuntimeError("3CM Stage2 schema validation failed: " + ",".join(failures))

    raw_diag = json.loads(diag_path.read_text(encoding="utf-8"))
    if not isinstance(raw_diag, dict):
        failures.append("diagnosis_not_object")
        raw_diag = {}
    diag, stripped_stage2_hints = strip_stage2_action_hints(copy.deepcopy(raw_diag))
    diag, normalized_process_cmd_fields = normalize_process_cmd_metadata(diag)
    if normalized_process_cmd_fields:
        diag["process_cmd_metadata_normalized"] = True
        diag["normalized_process_cmd_paths"] = [item["path"] for item in normalized_process_cmd_fields]
        diag["process_cmd_metadata_reason"] = "observation metadata, not action command"
    _, unsafe_control_failures = stage2_control_hints_are_safe(stripped_stage2_hints)
    failures.extend(unsafe_control_failures)

    expected_family, expected_label = expected_binding_from_env()
    family = diag.get("family")
    main_label = diag.get("main_label")

    missing = [k for k in EXPANDED_RCA_REQUIRED_FIELDS if k not in diag or diag.get(k) in (None, "", [])]
    if missing:
        failures.append("missing_fields:" + ",".join(missing))
    if diag.get("ok") is False:
        failures.append("diagnosis_ok_false")

    # 11-label allowlist gate (NET7 + CPU2 + MEM2); excluded labels always fail.
    if main_label in EXCLUDED_MAIN_LABELS:
        failures.append(f"excluded_main_label:{main_label}")
    elif main_label not in SUPPORTED_MAIN_LABELS:
        failures.append(f"unsupported_main_label:{main_label}")
    if family not in SUPPORTED_FAMILIES:
        failures.append(f"unsupported_family:{family}")
    elif main_label in SUPPORTED_MAIN_LABELS and SUPPORTED_MAIN_LABELS[main_label] != family:
        failures.append(f"family_label_mismatch:{family}!={SUPPORTED_MAIN_LABELS[main_label]}")
    if diag.get("family_hint") != family:
        failures.append(f"family_hint_mismatch:{diag.get('family_hint')}!={family}")

    # live demo expected binding: fail closed on contradiction with Stage1 trigger
    if expected_family and family != expected_family:
        failures.append(f"expected_family_mismatch:{family}!={expected_family}")
    if expected_label and main_label != expected_label:
        failures.append(f"expected_main_label_mismatch:{main_label}!={expected_label}")

    # load_pattern_detail stays auxiliary: bounded vocabulary per family
    lpd = diag.get("load_pattern_detail")
    if family == "cpu":
        if lpd not in CPU_LOAD_PATTERN_ALLOWED:
            failures.append(f"invalid_cpu_load_pattern_detail:{lpd}")
    elif family in ("mem", "net"):
        if lpd != "not_applicable":
            failures.append(f"invalid_load_pattern_detail:{lpd}")

    if not isinstance(diag.get("evidence"), list) or not diag.get("evidence"):
        failures.append("evidence_missing")
    if not root_object_is_v1(diag.get("root_object")):
        failures.append("root_object_not_v1")
    cause = diag.get("cause") if isinstance(diag.get("cause"), dict) else {}
    symptom = diag.get("symptom") if isinstance(diag.get("symptom"), dict) else {}
    if not cause.get("summary"):
        failures.append("cause_summary_missing")
    if not symptom.get("summary"):
        failures.append("symptom_summary_missing")
    binding = diag.get("run_id_binding") if isinstance(diag.get("run_id_binding"), dict) else {}
    accepted_run_id = diag.get("accepted_run_id") or binding.get("accepted_run_id")
    input_bundle_run_id = diag.get("input_bundle_run_id") or binding.get("input_bundle_run_id")
    run_id_match = diag.get("run_id_match")
    if run_id_match is None:
        run_id_match = binding.get("run_id_match")
    if accepted_run_id != run_dir.name:
        failures.append(f"accepted_run_id_mismatch:{accepted_run_id}!={run_dir.name}")
    if input_bundle_run_id != run_dir.name:
        failures.append(f"input_bundle_run_id_mismatch:{input_bundle_run_id}!={run_dir.name}")
    if run_id_match is not True:
        failures.append("run_id_match_not_true")
    ignored_stale = diag.get("ignored_stale_bundles_count")
    if ignored_stale is None:
        try:
            ignored_stale = int(os.environ.get("WK_3CM_IGNORED_STALE_BUNDLES_COUNT", "0"))
        except ValueError:
            ignored_stale = 0

    rca_subset = {k: diag.get(k) for k in EXPANDED_RCA_REQUIRED_FIELDS if k in diag}
    if contains_command_field(rca_subset):
        failures.append("rca_required_fields_contain_command_key")
    if contains_command_like_value(rca_subset):
        failures.append("rca_required_fields_contain_command_like_value")
    if contains_candidate_action_payload(diag):
        failures.append("diagnosis_contains_action_or_recovery_payload")
    if contains_credential_material(rca_subset):
        failures.append("rca_required_fields_contain_credential_material")

    report = {
        "schema_version": "3cm_stage2_schema_validation_v2",
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
        "diagnosis_path": str(diag_path),
        "accepted_stage2_rca_path": str(out_dir / "stage2_accepted_rca.json"),
        "run_dir": str(run_dir),
        "accepted_run_id": accepted_run_id,
        "input_bundle_run_id": input_bundle_run_id,
        "run_id_match": run_id_match is True,
        "ignored_stale_bundles_count": ignored_stale,
        "expected_family": expected_family,
        "expected_main_label": expected_label,
        "family": diag.get("family"),
        "family_hint": diag.get("family_hint"),
        "main_label": diag.get("main_label"),
        "load_pattern_detail": diag.get("load_pattern_detail"),
        "stripped_stage2_action_hint_fields": stripped_stage2_hints,
        "stripped_stage2_action_hint_reason": (
            "Stage2 accepted RCA excludes Action R1 suggestion/control fields; Stage3 regenerates suggestion-only output from validated RCA and registry."
            if stripped_stage2_hints
            else ""
        ),
        "process_cmd_metadata_normalized": bool(normalized_process_cmd_fields),
        "normalized_process_cmd_paths": [item["path"] for item in normalized_process_cmd_fields],
        "process_cmd_metadata_reason": (
            "Process observation metadata cmd fields were renamed to observed_cmdline/process_cmdline before command payload scanning."
            if normalized_process_cmd_fields
            else ""
        ),
    }
    atomic_write_json(out_dir / "stage2_schema_validation.json", report)
    action_hint_audit = {
        "schema_version": "3cm_stage2_action_hint_audit_v1",
        "status": "PASS" if not unsafe_control_failures else "FAIL",
        "diagnosis_path": str(diag_path),
        "stripped_stage2_action_hint_fields": stripped_stage2_hints,
        "stripped_stage2_action_hint_reason": report["stripped_stage2_action_hint_reason"],
        "unsafe_control_failures": unsafe_control_failures,
    }
    atomic_write_json(out_dir / "stage2_action_hint_audit.json", action_hint_audit)
    if failures:
        raise RuntimeError("3CM Stage2 schema validation failed: " + ",".join(failures))
    accepted_rca = copy.deepcopy(diag)
    accepted_rca["accepted_run_id"] = accepted_run_id
    accepted_rca["input_bundle_run_id"] = input_bundle_run_id
    accepted_rca["run_id_match"] = run_id_match is True
    accepted_rca["ignored_stale_bundles_count"] = ignored_stale
    if stripped_stage2_hints:
        accepted_rca["stripped_stage2_action_hint_fields"] = stripped_stage2_hints
        accepted_rca["stripped_stage2_action_hint_reason"] = report["stripped_stage2_action_hint_reason"]
    if normalized_process_cmd_fields:
        accepted_rca["normalized_process_cmd_fields"] = normalized_process_cmd_fields
    accepted_rca["stage2_schema_validation"] = {
        "schema_version": report["schema_version"],
        "status": report["status"],
        "run_id_match": report["run_id_match"],
        "expected_family": report["expected_family"],
        "expected_main_label": report["expected_main_label"],
    }
    atomic_write_json(out_dir / "stage2_accepted_rca.json", accepted_rca)
    return accepted_rca


def suggestion_from_action(action: Any, idx: int) -> Dict[str, Any]:
    if not isinstance(action, dict):
        return {
            "action_r1_suggestion_id": f"r1_suggestion_{idx}",
            "action_r1_summary": str(action)[:240],
            "execution_enabled": False,
            "manual_approval_required": True,
        }
    suggestion_id = str(action.get("action_r1_suggestion_id") or action.get("id") or action.get("name") or f"r1_suggestion_{idx}")
    summary = str(action.get("action_r1_summary") or action.get("summary") or action.get("why") or action.get("name") or "Manual review suggestion")
    if any(ch in summary for ch in (";", "&", "|", "`", "$", "<", ">")):
        raise RuntimeError("3CM source action contains command-shaped value")
    return {
        "action_r1_suggestion_id": suggestion_id[:120],
        "action_r1_summary": summary[:500],
        "execution_enabled": False,
        "manual_approval_required": True,
    }


def legacy_action_file_audit(out_dir: Path) -> Dict[str, Any]:
    files: List[Dict[str, Any]] = []
    command_bearing = False
    for name in ("actions.json", "actions_v2.json"):
        path = out_dir / name
        info: Dict[str, Any] = {
            "name": name,
            "present": path.exists(),
            "ignored": True,
            "reason": "3CM candidate Stage3 uses validated Stage2 RCA only",
        }
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                info["contains_command_fields"] = contains_command_field(data)
                info["contains_command_like_values"] = contains_command_like_value(data)
                command_bearing = command_bearing or bool(info["contains_command_fields"] or info["contains_command_like_values"])
            except Exception as exc:
                info["parse_error"] = str(exc)
        files.append(info)
    return {
        "legacy_action_files_present": any(item["present"] for item in files),
        "legacy_action_files_ignored": True,
        "ignored_legacy_action_reason": "candidate_suggestion_only_stage3_source_is_validated_stage2_rca",
        "legacy_action_files_command_bearing": command_bearing,
        "legacy_action_files": files,
    }


def stage3_suggestion_from_registry(diagnosis: Dict[str, Any]) -> Dict[str, Any]:
    main_label = str(diagnosis.get("main_label") or "")
    try:
        info = get_strategy(main_label)
    except KeyError:
        raise RuntimeError(f"3CM Stage3 Action R1 registry missing main_label:{main_label}")
    info["action_r1_suggestion_id"] = str(info.get("action_r1_suggestion_id"))[:120]
    info["action_r1_summary"] = str(info.get("action_r1_summary"))[:500]
    info["manual_steps"] = [str(step)[:240] for step in info.get("manual_steps", []) if str(step).strip()][:6]
    info["source"] = "action_r1_strategy_library"
    info["registry_key"] = main_label
    return info


def write_stage3_suggestions(source_path: Path, out_path: Path, diagnosis: Dict[str, Any] = None) -> int:
    source_path = Path(source_path)
    if diagnosis is not None:
        out_dir = source_path if source_path.is_dir() else source_path.parent
        suggestion = stage3_suggestion_from_registry(diagnosis)
        legacy_audit = legacy_action_file_audit(out_dir)
        suggestions = {
            "schema_version": "3cm_stage3_suggestions_v1",
            "mode": "action_r1_suggestion_only",
            "run_id": diagnosis.get("accepted_run_id"),
            "family": diagnosis.get("family"),
            "main_label": diagnosis.get("main_label"),
            "display_name_zh": diagnosis.get("display_name_zh"),
            "diagnosis_summary": diagnosis.get("diagnosis_summary"),
            "evidence": diagnosis.get("evidence"),
            "root_object": diagnosis.get("root_object"),
            "cause": diagnosis.get("cause"),
            "symptom": diagnosis.get("symptom"),
            "candidate_caveat": diagnosis.get("candidate_caveat"),
            "safety_caveat": diagnosis.get("safety_caveat"),
            "load_pattern_detail": diagnosis.get("load_pattern_detail"),
            "mem_evidence_caveat": diagnosis.get("mem_evidence_caveat"),
            "net_evidence_caveat": diagnosis.get("net_evidence_caveat"),
            "action_r1_suggestion_id": suggestion["action_r1_suggestion_id"],
            "action_r1_summary": suggestion["action_r1_summary"],
            "action_r1_title": suggestion.get("action_r1_title"),
            "execution_enabled": False,
            "automatic_recovery_enabled": False,
            "action_command_enabled": False,
            "manual_approval_required": True,
            "suggestions": [suggestion],
            "source": "validated_stage2_rca",
            "stage3_source": "validated_stage2_rca_only",
            "action_r1_source": "action_r1_strategy_library",
            "action_r1_registry_key": diagnosis.get("main_label"),
            "legacy_action_files_present": legacy_audit["legacy_action_files_present"],
            "legacy_action_files_ignored": legacy_audit["legacy_action_files_ignored"],
            "ignored_legacy_action_reason": legacy_audit["ignored_legacy_action_reason"],
            "stripped_stage2_action_hint_fields": diagnosis.get("stripped_stage2_action_hint_fields", []),
            "stripped_stage2_action_hint_reason": diagnosis.get("stripped_stage2_action_hint_reason", ""),
            "stage2_rca": {
                "family": diagnosis.get("family"),
                "family_hint": diagnosis.get("family_hint"),
                "main_label": diagnosis.get("main_label"),
                "display_name_zh": diagnosis.get("display_name_zh"),
                "confidence": diagnosis.get("confidence"),
                "diagnosis_summary": diagnosis.get("diagnosis_summary"),
                "evidence": diagnosis.get("evidence"),
                "load_pattern_detail": diagnosis.get("load_pattern_detail"),
                "accepted_run_id": diagnosis.get("accepted_run_id"),
                "input_bundle_run_id": diagnosis.get("input_bundle_run_id"),
                "run_id_match": diagnosis.get("run_id_match"),
                "ignored_stale_bundles_count": diagnosis.get("ignored_stale_bundles_count"),
                "root_object": diagnosis.get("root_object"),
                "cause": diagnosis.get("cause"),
                "symptom": diagnosis.get("symptom"),
                "candidate_caveat": diagnosis.get("candidate_caveat"),
                "safety_caveat": diagnosis.get("safety_caveat"),
                "mem_evidence_caveat": diagnosis.get("mem_evidence_caveat"),
                "net_evidence_caveat": diagnosis.get("net_evidence_caveat"),
            },
        }
        atomic_write_json(out_dir / "stage3_legacy_action_audit.json", legacy_audit)
        safety_failures = validate_action_r1_strategy_safety(suggestions)
        if safety_failures:
            raise RuntimeError("3CM stage3 suggestions strategy safety failed: " + ",".join(safety_failures))
        if contains_credential_material(suggestions):
            raise RuntimeError("3CM stage3 suggestions contain credential material")
        atomic_write_json(out_path, suggestions)
        return 1

    actions_json = source_path
    source: Dict[str, Any] = {"schema_version": 1, "actions": []}
    if actions_json.exists():
        source = json.loads(actions_json.read_text(encoding="utf-8"))
    actions = source.get("actions") if isinstance(source, dict) else []
    if not isinstance(actions, list):
        actions = []
    suggestions = {
        "schema_version": "3cm_stage3_suggestions_v1",
        "mode": "action_r1_suggestion_only",
        "execution_enabled": False,
        "automatic_recovery_enabled": False,
        "action_command_enabled": False,
        "manual_approval_required": True,
        "suggestions": [suggestion_from_action(action, idx + 1) for idx, action in enumerate(actions)],
        "source": "actions_v2.json" if actions_json.name == "actions_v2.json" else actions_json.name,
    }
    if contains_command_field(suggestions):
        raise RuntimeError("3CM stage3 suggestions contain command fields")
    atomic_write_json(out_path, suggestions)
    return len(actions)


def write_infer_done_marker(out_dir: Path) -> None:
    marker = out_dir / ".infer_done"
    tmp = Path(str(marker) + ".tmp")
    tmp.write_text("ok\n", encoding="utf-8")
    os.replace(tmp, marker)


def main() -> None:
    args = parse_args()
    if not args.bundle and not args.run_dir:
        raise SystemExit("--bundle or --run_dir required")

    out_root = Path(args.out_root).expanduser().resolve()
    if args.bundle:
        run_dir = run_ingest(Path(args.bundle).expanduser().resolve(), out_root)
    else:
        run_dir = Path(args.run_dir).expanduser().resolve()

    if is_3cm_candidate_mode():
        accepted_env = str(os.environ.get("WK_3CM_ACCEPTED_RUN_ID") or "").strip()
        if accepted_env and accepted_env != run_dir.name:
            # Stage1->Stage2 binding: fail closed before inference on stale bundle
            raise RuntimeError(
                "3CM run_id binding mismatch before inference: accepted_run_id=%s input_bundle_run_id=%s"
                % (accepted_env, run_dir.name)
            )

    out_dir = run_infer(run_dir)
    write_adapter_selection(out_dir)
    require_3cm_runtime_proof(out_dir)
    if is_3cm_candidate_mode():
        diagnosis = validate_stage2_expanded_rca_schema(out_dir, run_dir)
        suggestions_path = out_dir / "stage3_suggestions.json"
        count = write_stage3_suggestions(out_dir, suggestions_path, diagnosis)
    else:
        actions_path = out_dir / "actions.json"
        actions_device_path = out_dir / "actions_device.txt"
        count = write_actions_device(actions_path, actions_device_path)
    write_infer_done_marker(out_dir)

    print(f"run_dir={run_dir}")
    print(f"out_dir={out_dir}")
    if is_3cm_candidate_mode():
        print(f"stage3_suggestions={suggestions_path} count={count}")
    else:
        print(f"actions_device={actions_device_path} count={count}")


if __name__ == "__main__":
    main()
