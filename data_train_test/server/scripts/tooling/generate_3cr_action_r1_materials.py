#!/usr/bin/env python3
"""Generate 3CR Action R1 strategy and NET taxonomy sidecar materials."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "manual_experiments" / "action_r1_strategy_and_net_taxonomy_3CR"
sys.path.insert(0, str(ROOT))

from server_B.orchestrator.action_r1_strategy_library import (  # noqa: E402
    all_current_strategies,
    all_extension_strategies,
)


CURRENT_LABELS = [
    "cpu_single_point_high_load",
    "cpu_concurrency_scheduling_pressure",
    "mem_process_leak_growth",
    "mem_system_pressure_oom_risk",
    "net_dns_fail",
    "net_public_ip_unreachable",
    "net_no_default_route",
    "net_wrong_default_route",
    "net_gateway_unreachable",
    "net_no_ipv4_on_iface",
    "net_wifi_disconnect",
]

EXTENSION_LABELS = [
    "net_dhcp_lease_failure",
    "net_firewall_or_iptables_block",
]

REQUIRED_STRATEGY_FIELDS = [
    "action_r1_suggestion_id",
    "action_r1_title",
    "action_r1_summary",
    "manual_advice",
    "machine_suggestions",
    "verification_steps",
    "rollback_notes",
    "risk_summary",
    "execution_enabled",
    "manual_approval_required",
    "automatic_recovery_enabled",
    "action_command_enabled",
    "strategy_status",
]

REQUIRED_MACHINE_FIELDS = [
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
]


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_md(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def walk_items(value: Any, path: Tuple[str, ...] = ()) -> Iterable[Tuple[Tuple[str, ...], Any]]:
    yield path, value
    if isinstance(value, dict):
        for key, child in value.items():
            yield from walk_items(child, path + (str(key),))
    elif isinstance(value, list):
        for idx, child in enumerate(value):
            yield from walk_items(child, path + (f"[{idx}]",))


def has_key(value: Any, key_name: str) -> bool:
    if isinstance(value, dict):
        return any(str(key) == key_name or has_key(child, key_name) for key, child in value.items())
    if isinstance(value, list):
        return any(has_key(child, key_name) for child in value)
    return False


def has_auto_execute_true(value: Any) -> bool:
    for path, item in walk_items(value):
        if path and path[-1] == "auto_execute" and item is True:
            return True
    return False


def has_credential_leak(value: Any) -> bool:
    text = json.dumps(value, ensure_ascii=False)
    return bool(re.search(r"(?i)\b(psk|password|passwd|passphrase|secret)\b\s*[=:]\s*(?!<redacted_credential>)\S+", text))


def non_observation_low_risk(strategy: Dict[str, Any]) -> bool:
    diagnostic_only = re.compile(r"(?i)\b(inspect|check|collect|cat|grep|top|ps|pidstat|dmesg|observe)\b")
    for item in strategy.get("machine_suggestions") or []:
        if item.get("risk") != "low":
            continue
        cmd = str(item.get("command_template") or "")
        purpose = str(item.get("purpose") or "")
        if cmd and not diagnostic_only.search(cmd + " " + purpose):
            return True
    return False


def validate_strategy(label: str, strategy: Dict[str, Any], *, extension: bool = False) -> List[str]:
    failures: List[str] = []
    for field in REQUIRED_STRATEGY_FIELDS:
        if field not in strategy:
            failures.append(f"{label}:missing_field:{field}")
    if strategy.get("execution_enabled") is not False:
        failures.append(f"{label}:execution_enabled_not_false")
    if strategy.get("manual_approval_required") is not True:
        failures.append(f"{label}:manual_approval_required_not_true")
    if strategy.get("automatic_recovery_enabled") is not False:
        failures.append(f"{label}:automatic_recovery_enabled_not_false")
    if strategy.get("action_command_enabled") is not False:
        failures.append(f"{label}:action_command_enabled_not_false")
    if has_key(strategy, "action_command"):
        failures.append(f"{label}:action_command_key_present")
    if has_auto_execute_true(strategy):
        failures.append(f"{label}:auto_execute_true")
    if has_credential_leak(strategy):
        failures.append(f"{label}:credential_like_value_present")

    manual = strategy.get("manual_advice")
    if not isinstance(manual, list) or len([x for x in manual if str(x).strip()]) < 2:
        failures.append(f"{label}:manual_advice_lt_2")

    machines = strategy.get("machine_suggestions")
    if not isinstance(machines, list) or not machines:
        failures.append(f"{label}:machine_suggestions_missing")
        return failures

    risks = {str(item.get("risk")) for item in machines if isinstance(item, dict)}
    if label == "net_wifi_auth_fail_wrong_psk":
        if strategy.get("taxonomy_status") != "downgraded_manual_only":
            failures.append(f"{label}:taxonomy_status_not_downgraded")
        if strategy.get("live_recommendation") != "static_only":
            failures.append(f"{label}:live_recommendation_not_static_only")
        if "high" in risks:
            failures.append(f"{label}:wrong_psk_has_high_risk_machine_action")
    elif not extension:
        if not {"low", "medium", "high"}.issubset(risks):
            failures.append(f"{label}:risk_tiers_incomplete:{sorted(risks)}")
        if not non_observation_low_risk(strategy):
            failures.append(f"{label}:low_risk_is_observation_only")

    for idx, item in enumerate(machines):
        if not isinstance(item, dict):
            failures.append(f"{label}:machine_{idx}_not_object")
            continue
        for field in REQUIRED_MACHINE_FIELDS:
            if field not in item:
                failures.append(f"{label}:machine_{idx}_missing:{field}")
        if item.get("risk") not in {"low", "medium", "high"}:
            failures.append(f"{label}:machine_{idx}_risk_invalid")
        if not str(item.get("command_template") or "").strip():
            failures.append(f"{label}:machine_{idx}_command_template_empty")
        if item.get("requires_manual_approval") is not True:
            failures.append(f"{label}:machine_{idx}_manual_approval_not_true")
        if item.get("auto_execute") is not False:
            failures.append(f"{label}:machine_{idx}_auto_execute_not_false")
        if item.get("dispatch_channel") != "none":
            failures.append(f"{label}:machine_{idx}_dispatch_channel_not_none")
    return failures


def strategy_row(label: str, strategy: Dict[str, Any]) -> str:
    risks = ", ".join(item.get("risk", "") for item in strategy.get("machine_suggestions", []))
    status = strategy.get("taxonomy_status") or strategy.get("strategy_status")
    return f"| `{label}` | {strategy.get('action_r1_title')} | {risks} | {status} |"


def strategy_detail(label: str, strategy: Dict[str, Any]) -> str:
    lines = [f"### `{label}`", "", f"- 标题：{strategy.get('action_r1_title')}", f"- 摘要：{strategy.get('action_r1_summary')}", "- 人工建议："]
    for item in strategy.get("manual_advice", []):
        lines.append(f"  - {item}")
    lines.append("- 机器指令建议（仅展示，不自动执行）：")
    for item in strategy.get("machine_suggestions", []):
        lines.append(f"  - [{item.get('risk')}] `{item.get('command_template')}`")
        lines.append(f"    用途：{item.get('purpose')}")
        lines.append(f"    前提：{item.get('precondition')}")
        lines.append(f"    验证：{item.get('verification')}")
    lines.append(f"- 风险说明：{strategy.get('risk_summary')}")
    return "\n".join(lines)


def main() -> int:
    current = all_current_strategies()
    extensions = all_extension_strategies()
    failures: List[str] = []

    missing_current = [label for label in CURRENT_LABELS if label not in current]
    missing_extensions = [label for label in EXTENSION_LABELS if label not in extensions]
    failures.extend([f"missing_current_label:{label}" for label in missing_current])
    failures.extend([f"missing_extension_label:{label}" for label in missing_extensions])

    for label in CURRENT_LABELS:
        if label in current:
            failures.extend(validate_strategy(label, current[label]))
    for label in EXTENSION_LABELS:
        if label in extensions:
            strategy = extensions[label]
            failures.extend(validate_strategy(label, strategy, extension=True))
            if strategy.get("taxonomy_status") != "future_recommended_extension":
                failures.append(f"{label}:taxonomy_status_not_future_extension")
            if "not current live matrix PASS" not in str(strategy.get("demo_implication", "")):
                failures.append(f"{label}:demo_implication_unclear")

    status = "PASS_3CR_ACTION_R1_STRATEGY_AND_NET_TAXONOMY_READY" if not failures else "NEEDS_FIX_ACTION_R1_COMMAND_SAFETY_INVALID"
    safety = {
        "status": "PASS" if not failures else "FAIL",
        "failures": failures,
        "current_label_count": len(current),
        "extension_label_count": len(extensions),
        "wrong_psk_removed_from_current": "net_wifi_auth_fail_wrong_psk" not in current,
        "gateway_current": "net_gateway_unreachable" in current,
        "no_action_command_key": not has_key({"current": current, "extensions": extensions}, "action_command"),
        "no_auto_execute_true": not has_auto_execute_true({"current": current, "extensions": extensions}),
        "no_credential_like_assignment": not has_credential_leak({"current": current, "extensions": extensions}),
        "all_machine_suggestions_display_only": not failures,
    }

    schema = {
        "schema_name": "action_r1_strategy_schema_3cr",
        "required_strategy_fields": REQUIRED_STRATEGY_FIELDS,
        "required_machine_suggestion_fields": REQUIRED_MACHINE_FIELDS,
        "safety_rules": [
            "command_template 仅允许出现在 machine_suggestions 内。",
            "command_template 是展示模板，不是 action_command，也不写入 legacy downlink。",
            "execution_enabled=false, automatic_recovery_enabled=false, action_command_enabled=false。",
            "所有 machine_suggestions 必须 requires_manual_approval=true, auto_execute=false, dispatch_channel=none。",
        ],
    }

    source_diagnosis = {
        "old_weak_sources": [
            {
                "file": "server_B/orchestrator/run_closed_loop.py",
                "symbols": ["STAGE3_ACTION_R1_REGISTRY", "stage3_suggestion_from_registry"],
                "problem": "旧 Stage3 registry 主要输出人工继续观察/收集证据式 summary，缺少低/中/高风险机器指令模板。",
                "new_status": "stage3_suggestion_from_registry now loads action_r1_strategy_library.get_strategy(main_label).",
            },
            {
                "file": "closed_loop_infer_run.py",
                "symbols": ["LABEL_REGISTRY.action_r1_suggestion_id", "LABEL_REGISTRY.action_r1_summary"],
                "problem": "本地诊断兼容字段仍存在，但 run_closed_loop 会剥离 Stage2 action hint；它不是 Stage3 发布源。",
                "new_status": "保留兼容，不作为最终 Action R1 发布依据。",
            },
            {
                "file": "tools/demo_stage2.ps1",
                "symbols": ["Extract-ActionsLines", "Build-ConsoleSummary"],
                "problem": "旧 renderer 只打印 summary/manual_steps，看起来像观察建议。",
                "new_status": "renderer now prints manual_advice and risk-tiered machine_suggestions with display-only safety notice.",
            },
        ],
        "publish_gate": {
            "file": "server_B/tcp/watch_and_infer.py",
            "symbol": "validate_stage3_suggestions_for_publish",
            "new_status": "允许 machine_suggestions 内 display-only command_template；继续禁止 action_command、legacy downlink 和 auto_execute=true。",
        },
    }

    taxonomy = {
        "current_matrix_claim": "10/11 live/smoke PASS retained as historical demo matrix; not production baseline.",
        "wrong_psk_decision": {
            "label": "net_wifi_auth_fail_wrong_psk",
            "decision": "downgraded_manual_only_auth_config_side_condition",
            "reason": "凭据错误通常需要人工安全渠道重新配置，不适合作为可自动恢复的主 live NET 故障。",
            "live_recommendation": "static_only",
            "replacement_recommendation": "replace with net_gateway_unreachable or net_dhcp_lease_failure",
        },
        "recommended_extensions": {
            label: {
                "taxonomy_status": extensions[label].get("taxonomy_status"),
                "rationale": extensions[label].get("rationale"),
                "root_object_mapping": extensions[label].get("root_object_mapping"),
                "evidence_pattern": extensions[label].get("evidence_pattern"),
                "training_data_implication": extensions[label].get("training_data_implication"),
                "demo_implication": extensions[label].get("demo_implication"),
            }
            for label in EXTENSION_LABELS
        },
    }

    summary = {
        "status": status,
        "mode": "normal mode",
        "owner_approval": "APPROVE_3CR_ACTION_R1_STRATEGY_AND_NET_TAXONOMY_REFINEMENT_V1",
        "scope": "strategy library / renderer / sidecar materials only",
        "no_training": True,
        "no_adapter_mutation": True,
        "no_accepted_provenance_promotion": True,
        "no_action_execution": True,
        "candidate_shadow_only": True,
        "current_strategy_labels": CURRENT_LABELS,
        "recommended_net_extensions": EXTENSION_LABELS,
        "wrong_psk": taxonomy["wrong_psk_decision"],
        "static_validation_status": safety["status"],
    }

    static_smoke = {
        "status": "PASS" if not failures else "FAIL",
        "checks": {
            "11_current_labels_have_strategy": not missing_current,
            "2_recommended_extensions_exist": not missing_extensions,
            "wrong_psk_removed_from_current": safety["wrong_psk_removed_from_current"],
            "gateway_current": safety["gateway_current"],
            "no_action_command_key": safety["no_action_command_key"],
            "no_auto_execute_true": safety["no_auto_execute_true"],
            "requires_manual_approval_for_all_machine_suggestions": not any("manual_approval" in f for f in failures),
            "risk_present_for_all_machine_suggestions": not any("risk_invalid" in f for f in failures),
            "low_medium_high_present_where_applicable": not any("risk_tiers_incomplete" in f for f in failures),
            "low_risk_not_observation_only_where_applicable": not any("low_risk_is_observation_only" in f for f in failures),
            "no_psk_or_credential_assignment_leak": safety["no_credential_like_assignment"],
            "cpu_single_point_not_collect_more_evidence_primary": "collect_more_evidence" not in json.dumps(current["cpu_single_point_high_load"], ensure_ascii=False),
        },
        "failures": failures,
    }

    write_json(OUT_DIR / "3CR_summary.json", summary)
    write_json(OUT_DIR / "3CR_action_r1_source_diagnosis.json", source_diagnosis)
    write_json(OUT_DIR / "3CR_action_r1_schema.json", schema)
    write_json(OUT_DIR / "3CR_11label_strategy_library.json", current)
    write_json(OUT_DIR / "3CR_net_taxonomy_refinement.json", taxonomy)
    write_json(OUT_DIR / "3CR_recommended_net_extensions.json", extensions)
    write_json(OUT_DIR / "3CR_action_safety_validation.json", safety)
    write_json(OUT_DIR / "3CR_static_smoke_report.json", static_smoke)

    write_md(
        OUT_DIR / "3CR_summary.md",
        "\n".join(
            [
                "# 3CR Action R1 策略层与 NET taxonomy 修正摘要",
                "",
                f"- 状态：{status}",
                "- 模式：normal mode",
                "- 范围：仅策略库、renderer、答辩 sidecar 材料；不训练、不改 adapter、不提升 accepted provenance。",
                "- Action R1 新定义：面向常见故障的 risk-tiered recovery strategy library，输出人工建议与 display-only 机器指令模板。",
                "- 安全边界：不自动执行、不下发 legacy action downlink、不生成 action_command。",
                "- 当前 live/smoke 矩阵：保留 10/11 PASS 事实，但不宣称 production baseline。",
                "- wrong-PSK：降级为 manual-only/auth-config side condition/static-only。",
                "- NET 扩展：net_dhcp_lease_failure / net_firewall_or_iptables_block 仅作为 future taxonomy/data expansion cards；net_gateway_unreachable 已进入 revised current matrix。",
            ]
        ),
    )

    write_md(
        OUT_DIR / "3CR_action_r1_source_diagnosis.md",
        "# 3CR Action R1 来源诊断\n\n"
        "旧弱建议主要来自 `server_B/orchestrator/run_closed_loop.py::STAGE3_ACTION_R1_REGISTRY`，"
        "通过 `stage3_suggestion_from_registry` 写入 `stage3_suggestions.json`。"
        "`closed_loop_infer_run.py::LABEL_REGISTRY` 也保留了 action_r1 兼容字段，但 Stage3 会剥离 Stage2 action hint，"
        "所以它不是最终发布源。\n\n"
        "本次修正后，Stage3 发布源改为 `server_B/orchestrator/action_r1_strategy_library.py`；"
        "`tools/demo_stage2.ps1::Extract-ActionsLines` 会展示人工建议、低/中/高风险机器指令模板和不自动执行声明。"
    )

    strategy_md = [
        "# 3CR 11-label Action R1 策略库",
        "",
        "| Label | 策略标题 | 风险层 | 状态 |",
        "|---|---|---|---|",
    ]
    for label in CURRENT_LABELS:
        strategy_md.append(strategy_row(label, current[label]))
    strategy_md.append("")
    strategy_md.extend(strategy_detail(label, current[label]) for label in CURRENT_LABELS)
    write_md(OUT_DIR / "3CR_11label_strategy_library.md", "\n\n".join(strategy_md))

    write_md(
        OUT_DIR / "3CR_net_taxonomy_refinement.md",
        "# 3CR NET Taxonomy 修正\n\n"
        "- `net_wifi_auth_fail_wrong_psk`：降级为 manual-only/auth-config side condition/static-only，不再作为主 live NET 故障展示。\n"
        "- `net_dhcp_lease_failure`：建议作为后续 DHCP/IPv4 获取失败扩展，需要新采集、新 target rebuild 和 adapter retraining。\n"
        "- `net_gateway_unreachable`：建议作为真实网关不可达扩展，比 wrong-PSK 更常见、更可观察、更适合恢复策略。\n"
        "- `net_firewall_or_iptables_block`：建议作为策略阻断扩展，强调只移除确认规则，避免 flush-all 作为常规动作。\n\n"
        "这些扩展当前只作为 taxonomy/strategy cards，不属于 3CG-R1 已训练主标签，也不是当前 live matrix PASS。"
    )

    write_md(
        OUT_DIR / "3CR_wrong_psk_downgrade_note.md",
        "# wrong-PSK 降级说明\n\n"
        "`net_wifi_auth_fail_wrong_psk` 不适合作为主 live NET 故障：凭据错误通常无法自动修复，且真实凭据必须通过安全渠道人工处理。"
        "本次将其标注为 `manual-only / auth-config side condition / static-only`，保留脱敏检查与人工重配建议，不展示高风险自动恢复动作。"
    )

    extension_md = ["# 3CR 推荐 NET 扩展策略卡", ""]
    for label in EXTENSION_LABELS:
        extension_md.append(strategy_detail(label, extensions[label]))
        extension_md.append("")
        extension_md.append(f"- 数据影响：{extensions[label].get('training_data_implication')}")
        extension_md.append(f"- Demo 影响：{extensions[label].get('demo_implication')}")
        extension_md.append("")
    write_md(OUT_DIR / "3CR_recommended_net_extensions.md", "\n".join(extension_md))

    cpu = current["cpu_single_point_high_load"]
    low = next(item for item in cpu["machine_suggestions"] if item["risk"] == "low")
    medium = next(item for item in cpu["machine_suggestions"] if item["risk"] == "medium")
    high = next(item for item in cpu["machine_suggestions"] if item["risk"] == "high")
    write_md(
        OUT_DIR / "3CR_console_before_after_example.md",
        "# Demo Console Before/After 示例\n\n"
        "## Before\n\n"
        "旧输出接近：`cpu_live_smoke_collect_more_evidence: collect pidstat/procs/load`，容易被理解为继续观察，而不是恢复策略。\n\n"
        "## After\n\n"
        f"Action R1 恢复策略：{cpu['action_r1_title']}\n\n"
        "人工建议：\n"
        + "\n".join(f"{idx}. {item}" for idx, item in enumerate(cpu["manual_advice"], 1))
        + "\n\n机器指令建议（仅展示，不自动执行）：\n"
        f"- [低风险] `{low['command_template']}`：{low['purpose']}\n"
        f"- [中风险] `{medium['command_template']}`：{medium['purpose']}\n"
        f"- [高风险] `{high['command_template']}`：{high['purpose']}\n\n"
        "安全声明：本 demo 仅展示处置策略，不自动执行命令；所有机器指令需要人工确认。"
    )

    write_md(
        OUT_DIR / "3CR_midterm_material_update.md",
        "# 中期答辩材料更新口径\n\n"
        "1. Action R1 不是自动恢复执行器，而是 human-approved recovery strategy library。\n"
        "2. 当前 demo 继续保持 candidate/shadow only；Stage3 输出建议，不写 legacy action downlink。\n"
        "3. 10/11 live/smoke PASS 是演示矩阵事实，不是生产基线或 accepted provenance。\n"
        "4. wrong-PSK 已降级为 manual-only/static/auth-config side condition。\n"
        "5. 两个 NET 扩展是未来 taxonomy/data expansion plan；net_gateway_unreachable 已进入 revised current matrix，仍需要新采集、重建 target、重训/评估后才能宣称 live PASS。"
    )

    write_md(
        OUT_DIR / "3CR_manual_validation_command.md",
        "# 3CR 手动验证命令\n\n"
        "```powershell\n"
        "python -m py_compile server_B\\orchestrator\\action_r1_strategy_library.py server_B\\orchestrator\\run_closed_loop.py server_B\\tcp\\watch_and_infer.py closed_loop_infer_run.py tools\\generate_3cr_action_r1_materials.py\n"
        "powershell -NoProfile -ExecutionPolicy Bypass -File .\\tools\\demo_stage2.ps1 -Help\n"
        "python tools\\generate_3cr_action_r1_materials.py\n"
        "```\n\n"
        "本任务不要求 NET/MEM/wrong-PSK live smoke，也不执行 Action R1。"
    )

    audit_index = {
        "output_dir": str(OUT_DIR.relative_to(ROOT)).replace("\\", "/"),
        "required_files": [
            "3CR_summary.json",
            "3CR_summary.md",
            "3CR_action_r1_source_diagnosis.json",
            "3CR_action_r1_source_diagnosis.md",
            "3CR_action_r1_schema.json",
            "3CR_11label_strategy_library.json",
            "3CR_11label_strategy_library.md",
            "3CR_net_taxonomy_refinement.json",
            "3CR_net_taxonomy_refinement.md",
            "3CR_wrong_psk_downgrade_note.md",
            "3CR_recommended_net_extensions.json",
            "3CR_recommended_net_extensions.md",
            "3CR_console_before_after_example.md",
            "3CR_action_safety_validation.json",
            "3CR_static_smoke_report.json",
            "3CR_midterm_material_update.md",
            "3CR_manual_validation_command.md",
            "3CR_reproduction_audit_index.json",
        ],
        "source_files": [
            "server_B/orchestrator/action_r1_strategy_library.py",
            "server_B/orchestrator/run_closed_loop.py",
            "server_B/tcp/watch_and_infer.py",
            "tools/demo_stage2.ps1",
            "tools/generate_3cr_action_r1_materials.py",
        ],
        "status": status,
    }
    write_json(OUT_DIR / "3CR_reproduction_audit_index.json", audit_index)

    print(status)
    if failures:
        print("\n".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
