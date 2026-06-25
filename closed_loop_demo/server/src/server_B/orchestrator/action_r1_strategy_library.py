#!/usr/bin/env python3
"""Display-only Action R1 strategy library for the 3CM/3CR demo.

These strategies are recommendations for human-approved recovery planning.
They are never dispatched to a board action channel by this module.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List


FALSE_SAFETY = {
    "execution_enabled": False,
    "manual_approval_required": True,
    "automatic_recovery_enabled": False,
    "action_command_enabled": False,
}


def machine(
    risk: str,
    command_template: str,
    purpose: str,
    when_to_use: str,
    precondition: str,
    expected_effect: str,
    verification: str,
    rollback: str,
) -> Dict[str, Any]:
    return {
        "risk": risk,
        "command_template": command_template,
        "purpose": purpose,
        "when_to_use": when_to_use,
        "precondition": precondition,
        "expected_effect": expected_effect,
        "verification": verification,
        "rollback": rollback,
        "requires_manual_approval": True,
        "auto_execute": False,
        "dispatch_channel": "none",
    }


def strategy(
    suggestion_id: str,
    title: str,
    summary: str,
    manual_advice: List[str],
    machine_suggestions: List[Dict[str, Any]],
    verification_steps: List[str],
    rollback_notes: List[str],
    risk_summary: str,
    strategy_status: str = "display_only_strategy",
    **extra: Any,
) -> Dict[str, Any]:
    out = {
        "action_r1_suggestion_id": suggestion_id,
        "action_r1_title": title,
        "action_r1_summary": summary,
        "manual_advice": manual_advice,
        "manual_steps": manual_advice,
        "machine_suggestions": machine_suggestions,
        "verification_steps": verification_steps,
        "rollback_notes": rollback_notes,
        "risk_summary": risk_summary,
        "strategy_status": strategy_status,
        **FALSE_SAFETY,
    }
    out.update(extra)
    return out


ACTION_R1_STRATEGY_LIBRARY: Dict[str, Dict[str, Any]] = {
    "cpu_single_point_high_load": strategy(
        "cpu_single_point_risk_tiered_strategy",
        "CPU 单点高负载人工审批处置策略",
        "确认非关键高负载进程后，按低/中/高风险选择降优先级、限制 CPU 亲和性、温和终止或系统级恢复；当前系统仅展示策略，不自动执行命令。",
        [
            "确认高负载来源是否为非关键进程，避免影响关键业务链路。",
            "保留 run_id、loadavg/CPU 峰值、进程快照和触发 marker。",
            "仅对确认的非关键高负载进程进入处置流程，关键业务必须升级人工审批。",
        ],
        [
            machine("low", "renice +10 -p <confirmed_noncritical_pid>", "降低确认的非关键高负载进程优先级", "进程已确认非关键且仍持续抢占 CPU", "run_id 绑定、进程身份和业务影响已人工确认", "降低该进程调度优先级，缓解单点 CPU 压力", "复测 loadavg、CPU 利用率和业务心跳", "恢复原 nice 值或重启该非关键任务"),
            machine("low", "taskset -p <reduced_cpu_mask> <confirmed_noncritical_pid>", "限制确认的非关键高负载进程 CPU 亲和性", "降优先级不足以缓解抢占且可限制该进程运行核", "CPU mask、进程身份和业务影响已人工确认", "限制该进程占用的 CPU 核范围，降低对关键任务的干扰", "复测 loadavg、CPU 利用率、进程状态和业务心跳", "恢复原 CPU mask 或按服务手册恢复任务"),
            machine("medium", "kill -TERM <confirmed_noncritical_pid>", "温和终止确认的非关键异常进程", "降优先级无效且异常进程非关键", "日志与进程快照已保存，业务 owner 已批准", "释放 CPU 压力源", "确认进程退出、CPU/load 回落且无关键业务告警", "按服务手册重新启动或恢复任务"),
            machine("high", "reboot", "系统级恢复，最后手段", "系统不可交互或关键链路无法恢复", "人工审批、现场确认和日志留存完成", "重建系统运行状态", "确认设备重启后 CPU/load、网络和核心服务恢复", "如重启后异常复现，停止自动恢复并升级人工排障"),
        ],
        ["检查 loadavg/CPU 峰值是否回落", "确认非关键进程状态", "保留处置前后快照"],
        ["低风险动作可恢复 nice 值或 CPU mask", "中风险动作按服务手册重启", "高风险重启后保留启动日志"],
        "低风险限于非关键进程降优先级或限制 CPU 亲和性；中风险会终止进程；高风险系统级恢复只作为最后手段。",
    ),
    "cpu_concurrency_scheduling_pressure": strategy(
        "cpu_concurrency_risk_tiered_strategy",
        "CPU 并发调度压力处置策略",
        "针对并发 worker 或后台任务过载，按风险分级限制 CPU 亲和性、降低并发或系统级恢复。",
        ["确认 runqueue/load 峰值与 worker 数量匹配。", "识别可降载的后台任务或 worker_group。"],
        [
            machine("low", "taskset -p <reduced_cpu_mask> <confirmed_background_pid>", "限制后台任务 CPU 亲和性", "后台任务挤占关键 CPU 核", "进程为后台非关键任务且 CPU mask 已确认", "减少对关键任务调度影响", "复测 runqueue、loadavg 和关键服务延迟", "恢复原 CPU mask"),
            machine("medium", "<service_control> scale-down <worker_group>", "降低过载 worker 并发", "worker_group 并发超过业务承载能力", "容量影响和降载窗口已审批", "降低 runqueue 和上下文切换压力", "复测 worker 数、loadavg 和吞吐", "按容量计划逐步扩回 worker"),
            machine("high", "reboot", "系统级恢复，最后手段", "调度压力导致控制链路不可恢复", "人工审批和日志留存完成", "重建系统调度状态", "重启后复测 CPU/load 和服务状态", "若复现则保留现场并升级排障"),
        ],
        ["复测 runqueue/load 与 worker 数", "确认关键服务延迟恢复"],
        ["恢复 CPU mask", "按容量计划扩回 worker", "记录高风险重启原因"],
        "限制亲和性风险低，降并发影响业务容量，重启为高风险最后手段。",
    ),
    "mem_process_leak_growth": strategy(
        "mem_leak_risk_tiered_strategy",
        "进程内存泄漏增长处置策略",
        "确认泄漏进程后，按审批关闭非关键功能、温和终止泄漏进程或系统级恢复。",
        ["确认 RSS/PSS 或可用内存曲线持续恶化。", "保留疑似进程快照、内存曲线和日志。"],
        [
            machine("low", "<service_control> disable <noncritical_feature>", "关闭非关键泄漏触发功能", "泄漏与某非关键功能强相关", "功能 owner 批准且关闭影响可接受", "阻断泄漏增长触发路径", "复测 RSS/PSS 增长是否停止", "重新启用功能或回滚配置"),
            machine("medium", "kill -TERM <leaking_noncritical_pid>", "温和终止确认的非关键泄漏进程", "泄漏持续且进程非关键", "日志与快照已保存，重启手册可用", "释放泄漏占用内存", "确认内存回升且服务无关键告警", "按手册恢复进程或服务"),
            machine("high", "reboot", "系统级恢复，最后手段", "内存压力威胁系统可用性且无法局部恢复", "人工审批和现场确认完成", "释放全局内存并重建状态", "重启后复测 mem_available 和核心服务", "若复现则停止处置并升级根因排查"),
        ],
        ["检查 mem_available、RSS/PSS 曲线", "确认泄漏进程是否退出或停止增长"],
        ["回滚功能开关", "按服务手册恢复进程", "保留重启前后内存曲线"],
        "关闭功能风险低，终止进程影响服务，高风险重启必须人工审批。",
    ),
    "mem_system_pressure_oom_risk": strategy(
        "mem_pressure_risk_tiered_strategy",
        "系统内存压力 OOM 风险处置策略",
        "在不诱发 OOM 的前提下，暂停大内存任务、释放非关键进程或最后重启。",
        ["确认 mem_available 接近安全下限。", "优先识别非关键大内存任务。"],
        [
            machine("low", "<service_control> pause <memory_heavy_batch_job>", "暂停非关键大内存任务", "批处理或后台任务造成内存压力", "任务可暂停且业务窗口允许", "降低内存占用增长", "复测 mem_available 是否回升", "恢复任务或从检查点继续"),
            machine("medium", "kill -TERM <largest_confirmed_noncritical_memory_pid>", "释放非关键内存压力源", "存在已确认非关键大内存进程", "日志已保存且 owner 批准", "释放内存并降低 OOM 风险", "确认 mem_available 回升且无关键告警", "按服务手册重启或迁移任务"),
            machine("high", "reboot", "系统级恢复，最后手段", "系统接近不可用且无法局部释放内存", "人工审批和现场确认完成", "重建内存状态", "重启后复测内存与核心服务", "若复现则停止处置并升级排查"),
        ],
        ["检查 mem_available 与 OOM 日志", "确认被暂停/终止任务状态"],
        ["恢复暂停任务", "按手册重启被终止进程", "保留重启前后日志"],
        "低风险暂停任务可回滚；中风险终止进程需审批；高风险重启只作最后手段。",
    ),
    "net_dns_fail": strategy(
        "net_dns_risk_tiered_strategy",
        "DNS 解析失败处置策略",
        "确认 resolver/DNS 异常后，清理故障残留、恢复可信 resolver 或重建网络配置。",
        ["确认 DNS 失败与 run_id 绑定一致。", "保留 resolver、DNS 探测和恢复门记录。"],
        [
            machine("low", "NET_MODE=cleanup NET_WLAN_IFACE=wlan0 /data/local/tmp/net_fault.sh", "清理已确认的 DNS 故障残留", "DNS 故障残留已由人工确认", "仅在故障上下文明确且人工确认后使用", "移除故障残留", "复测 DNS 与公网 IP", "如误清理，按备份恢复网络配置"),
            machine("medium", "restore resolver to approved DNS template", "恢复可信 resolver 配置", "resolver 配置偏离可信模板", "可信模板已确认且变更窗口获批", "恢复 DNS 解析路径", "复测 DNS、默认路由和公网 IP", "回滚到变更前 resolver 备份"),
            machine("high", "restart network management service", "重建网络配置", "resolver 恢复无效且网络管理状态异常", "人工审批并确认不会中断关键链路", "重启网络管理状态机", "复测 DNS、IP、路由和 Wi-Fi", "按服务手册恢复网络服务并保留日志"),
        ],
        ["复测 DNS host", "复测公网 IP", "检查 resolver 与恢复门"],
        ["恢复 resolver 备份", "停止后续自动网络变更", "保留服务重启日志"],
        "清理故障残留风险低；修改 resolver 为中风险；重启网络服务会影响链路。",
    ),
    "net_public_ip_unreachable": strategy(
        "net_public_ip_risk_tiered_strategy",
        "公网 IP 不可达处置策略",
        "清理出网阻断残留，恢复默认出网路径，必要时切换备用网络配置。",
        ["确认 DNS 与接口状态，定位为公网 IP 出口问题。", "保留路由、防火墙和探测结果。"],
        [
            machine("low", "NET_MODE=cleanup NET_WLAN_IFACE=wlan0 /data/local/tmp/net_fault.sh", "清理出网故障残留", "出网故障残留已由人工确认", "人工确认故障上下文", "恢复被阻断的出网路径", "复测公网 IP 与 DNS", "按备份恢复网络配置"),
            machine("medium", "restore default route and resolver from approved baseline", "恢复出网路径", "默认路由或 resolver 偏离基线", "批准的网络基线可用", "恢复公网出口", "复测默认路由、公网 IP 和 DNS", "回滚到变更前路由/resolver"),
            machine("high", "switch to backup network profile", "切换备用网络配置", "主网络路径无法恢复", "人工审批且备用网络已验证", "恢复控制链路或业务出口", "复测完整网络恢复门", "切回主网络配置并保留切换记录"),
        ],
        ["复测公网 IP", "复测 DNS", "确认默认路由和接口"],
        ["恢复原路由/resolver", "切回主网络 profile", "记录切换窗口"],
        "低风险限于故障残留清理；中风险修改出网路径；高风险切换网络 profile。",
    ),
    "net_no_default_route": strategy(
        "net_no_default_route_risk_tiered_strategy",
        "默认路由缺失处置策略",
        "清理路由故障残留，恢复可信默认路由，必要时重启网络管理服务。",
        ["确认默认路由缺失且接口仍可用。", "核对可信网关备份。"],
        [
            machine("low", "NET_MODE=cleanup NET_WLAN_IFACE=wlan0 /data/local/tmp/net_fault.sh", "清理路由故障残留", "路由故障残留已由人工确认", "人工确认 run_id 与故障上下文", "恢复被删除的路由状态", "复测默认路由、公网 IP 和 DNS", "按备份重建路由"),
            machine("medium", "ip route add default via <gateway> dev <iface>", "恢复默认路由", "默认路由缺失且可信 gateway 已确认", "网关、接口、地址均已人工确认", "恢复 L3 出网路径", "复测 route、公网 IP 和 DNS", "删除新增路由并恢复备份"),
            machine("high", "restart network management service", "重建网络状态机", "路由恢复失败且管理服务状态异常", "人工审批并允许短暂断网", "重新生成路由状态", "复测完整网络恢复门", "按服务手册回滚网络服务状态"),
        ],
        ["检查默认路由", "公网 IP ping", "DNS ping"],
        ["删除临时默认路由", "恢复网关备份", "保留网络服务日志"],
        "恢复路由是中风险配置变更；重启网络服务为高风险。",
    ),
    "net_wrong_default_route": strategy(
        "net_wrong_default_route_risk_tiered_strategy",
        "默认路由错误处置策略",
        "移除确认的错误路由，恢复可信默认网关，必要时重置网络栈。",
        ["确认默认网关偏离可信备份。", "保留路由表和外部探测结果。"],
        [
            machine("low", "remove only the injected wrong default route", "移除确认的错误路由", "错误路由明确来自注入或误配置", "确认不会删除可信默认路由", "停止错误路径黑洞", "复测路由表与公网 IP", "恢复删除前路由备份"),
            machine("medium", "ip route replace default via <trusted_gateway> dev <iface>", "恢复可信默认网关", "可信 gateway 与 iface 已确认", "人工审批并保存路由备份", "恢复正确出网路径", "复测公网 IP、DNS 和路由表", "回滚到变更前路由备份"),
            machine("high", "reset network stack", "重置网络栈", "路由修复无效且网络状态机混乱", "人工审批且允许断网窗口", "重建网络路由/地址状态", "复测完整恢复门", "按网络基线恢复配置并保留日志"),
        ],
        ["确认 fake route absent", "确认 trusted gateway active", "复测公网 IP/DNS"],
        ["恢复路由备份", "停止后续网络变更", "记录 reset 前后状态"],
        "移除单条错误路由风险低于替换默认路由；网络栈 reset 为高风险。",
    ),
    "net_gateway_unreachable": strategy(
        "net_gateway_unreachable_risk_tiered_strategy",
        "Default gateway unreachable handling strategy",
        "Suggestion-only strategy: inspect gateway reachability, route evidence, and approved fallback path before any manual recovery.",
        [
            "Confirm wlan0 keeps IPv4 and a default route while gateway ping fails.",
            "Preserve run_id-bound gateway, route, public IP, DNS, and recovery gate evidence.",
        ],
        [
            machine("low", "NET_MODE=cleanup NET_WLAN_IFACE=wlan0 /data/local/tmp/net_fault.sh", "Clear bounded gateway fault residue", "Gateway fault residue was manually confirmed for the same run_id", "Manual approval recorded and no credential material is involved", "Remove injected route residue before retest", "Retest gateway, public IP, and DNS", "Restore route backup if cleanup changes trusted route state"),
            machine("medium", "ip route replace default via <trusted_gateway> dev <iface>", "Restore trusted default gateway", "Trusted gateway and iface have been manually approved", "Route backup and approval are recorded", "Restore outbound L3 path", "Retest gateway, public IP, DNS, and recovery gate", "Restore previous route backup"),
            machine("high", "switch to approved backup network profile", "Switch to backup network path", "Current gateway path cannot be recovered safely", "Backup profile is approved and available", "Restore control-plane connectivity", "Retest full recovery gate", "Switch back to original approved profile"),
        ],
        ["gateway ping", "route table", "public IP/DNS recovery"],
        ["restore route backup", "switch back to original profile", "preserve gateway evidence"],
        "Display-only: any route or profile change requires manual approval; no automatic recovery dispatch.",
        root_object_mapping={"schema_version": "root_object.v1", "object_type": "network_route", "object_name": "gateway", "object_id": "net_gateway"},
        evidence_pattern=["IPv4 present", "default route present", "gateway ping fails", "public IP fails", "DNS may be secondary"],
    ),
    "net_no_ipv4_on_iface": strategy(
        "net_no_ipv4_risk_tiered_strategy",
        "接口无 IPv4 地址处置策略",
        "重新获取 DHCP/IPv4，恢复接口启用状态，必要时重连已批准 Wi-Fi 配置。",
        ["确认接口无 IPv4 且物理/关联状态可用。", "保留接口快照和地址备份。"],
        [
            machine("low", "request DHCP renew on <iface>", "重新获取 IPv4", "接口 up 但无有效 IPv4", "DHCP 服务可达且不会覆盖静态配置", "获得新的 IPv4 租约", "检查 ifconfig、默认路由、公网 IP", "释放新租约并恢复原地址备份"),
            machine("medium", "ifconfig <iface> up", "恢复接口启用状态", "接口被确认 down 或未启用", "确认启用不会影响其他链路", "恢复接口 L2/L3 准备状态", "复测 IPv4、route 和 DNS", "按记录恢复接口前状态"),
            machine("high", "reconnect Wi-Fi using approved saved profile", "使用已批准配置重连网络", "DHCP renew 和接口 up 无效", "已保存可信 profile 且人工审批", "重建 Wi-Fi 与 IPv4 状态", "复测 Wi-Fi、IPv4、route、公网 IP、DNS", "切回原 profile 或手工恢复配置"),
        ],
        ["检查 IPv4 地址", "检查默认路由", "公网 IP/DNS 复测"],
        ["释放/恢复地址配置", "恢复接口状态", "切回原网络 profile"],
        "DHCP renew 较低风险；接口 up 和重连可能影响链路。",
    ),
    "net_wifi_disconnect": strategy(
        "net_wifi_disconnect_risk_tiered_strategy",
        "Wi-Fi 断开处置策略",
        "恢复 Wi-Fi 接口、使用已保存可信配置重连，最后才重启网络服务或设备。",
        ["确认 Wi-Fi association 断开且凭据不泄露。", "保留关联状态、IPv4、路由和恢复门记录。"],
        [
            machine("low", "ifconfig wlan0 up", "恢复 Wi-Fi 接口启用状态", "接口被确认 down", "确认 wlan0 为目标接口且不会影响其他链路", "重新启用 Wi-Fi 接口", "复测 association、IPv4 和 route", "恢复接口前状态或重新应用 profile"),
            machine("medium", "reconnect Wi-Fi using existing approved profile", "使用已保存配置重连 Wi-Fi", "接口 up 但未完成关联", "已保存可信 profile 且人工审批", "恢复 Wi-Fi 关联", "复测 Wi-Fi、IPv4、默认路由、公网 IP 和 DNS", "切回原 profile 或断开重连"),
            machine("high", "restart network service or reboot device", "最后手段恢复控制链路", "重连失败且控制链路不可恢复", "人工审批、现场确认和日志留存完成", "重建网络服务或系统状态", "复测完整网络恢复门", "按服务手册恢复网络服务并保留启动日志"),
        ],
        ["检查 association", "检查 IPv4/default route", "公网 IP/DNS 复测"],
        ["恢复接口/profile", "保留网络服务重启日志", "若复现升级排障"],
        "接口 up 风险较低；重连影响网络会话；网络服务重启/设备重启为高风险。",
    ),
    "net_wifi_auth_fail_wrong_psk": strategy(
        "net_wifi_auth_manual_only_side_condition",
        "Wi-Fi 认证失败凭据侧条件",
        "该项降级为 manual-only/auth-config side condition，不作为主 live NET 故障；不展示可自动修复的凭据动作。",
        ["人工复核认证失败状态，禁止暴露 PSK 或真实凭据。", "通过批准的安全渠道重新下发或修正凭据。"],
        [
            machine("low", "inspect Wi-Fi auth state without exposing PSK", "检查认证状态并保持凭据脱敏", "需要确认是否为凭据侧条件", "不得输出或记录 PSK/passphrase", "获得认证状态证据", "确认日志中无凭据泄漏", "删除临时诊断输出中的敏感材料"),
            machine("medium", "human re-provision credentials through approved secure channel", "人工通过安全渠道重新配置凭据", "已确认凭据配置错误", "必须由授权人员执行，禁止明文传递 PSK", "恢复可信 Wi-Fi 配置", "复测 association、IPv4、route、公网 IP 和 DNS", "回滚到上一个批准配置"),
        ],
        ["确认无 PSK 泄漏", "复测 Wi-Fi association", "复测网络恢复门"],
        ["恢复上一个批准配置", "撤销临时凭据变更", "保留审批记录"],
        "凭据类故障不适合作为自动恢复目标；只作为人工配置侧条件。",
        strategy_status="downgraded_manual_only",
        taxonomy_status="downgraded_manual_only",
        live_recommendation="static_only",
        replacement_recommendation="replace with net_gateway_unreachable or net_dhcp_lease_failure",
    ),
}


RECOMMENDED_NET_EXTENSION_STRATEGIES: Dict[str, Dict[str, Any]] = {
    "net_dhcp_lease_failure": strategy(
        "net_dhcp_lease_failure_future_strategy",
        "DHCP 租约失败推荐扩展策略",
        "推荐作为后续 NET taxonomy/data 扩展项，当前未训练、未 live 验证。",
        ["确认接口 up 但无法获得 DHCP lease。", "保留 DHCP 日志、接口状态和租约前后快照。"],
        [
            machine("low", "request DHCP renew on <iface>", "重新申请 DHCP 租约", "接口 up 且 DHCP 服务可达", "确认不是静态地址场景", "获取新 IPv4/网关/DNS", "复测 lease、IPv4、route、DNS", "释放租约并恢复旧地址配置"),
            machine("medium", "restart network management service", "重启网络管理以恢复 DHCP 状态机", "DHCP renew 无效且状态机异常", "人工审批并允许短暂断网", "重建 DHCP 状态", "复测 lease 和恢复门", "按服务手册恢复网络服务"),
            machine("high", "reconnect network profile", "重连网络 profile", "服务重启无效且链路需要重建", "批准的 profile 可用", "重建 Wi-Fi/L3 状态", "复测完整网络恢复门", "切回原 profile"),
        ],
        ["检查 DHCP lease", "检查 IPv4/route/DNS", "确认无 wrong-PSK 证据"],
        ["释放租约", "恢复旧地址", "切回原 profile"],
        "DHCP renew 风险低；网络服务重启/重连会中断链路。",
        strategy_status="recommended_extension_static_card",
        taxonomy_status="future_recommended_extension",
        demo_implication="strategy extension/static taxonomy update only; not current live matrix PASS",
        training_data_implication="future data collection and target rebuild/adapter retraining required",
        root_object_mapping={"schema_version": "root_object.v1", "object_type": "network_interface", "object_name": "network_dhcp", "object_id": "net_dhcp"},
        rationale="比 wrong-PSK 更常见、更可观测，也更适合作为可恢复网络故障。",
        evidence_pattern=["interface up", "no DHCP lease", "no IPv4 or stale lease", "gateway/DNS absent after lease failure"],
    ),
    "net_gateway_unreachable": strategy(
        "net_gateway_unreachable_future_strategy",
        "网关不可达推荐扩展策略",
        "推荐作为后续 NET taxonomy/data 扩展项，当前未训练、未 live 验证。",
        ["确认本机有 IPv4/默认路由但网关不可达。", "保留 gateway ping、route 和接口快照。"],
        [
            machine("low", "refresh ARP/neighbor entry for <gateway>", "刷新网关邻居状态", "疑似邻居缓存异常", "确认 gateway 地址可信", "恢复 gateway L2 邻居解析", "复测 gateway、公网 IP、DNS", "恢复原邻居状态或等待自然刷新"),
            machine("medium", "ip route replace default via <trusted_gateway> dev <iface>", "恢复可信网关路由", "默认网关错误或网关迁移", "可信 gateway 与 iface 已审批", "恢复出网路径", "复测 gateway、公网 IP、DNS", "回滚路由备份"),
            machine("high", "switch to backup network profile", "切换备用网络路径", "网关不可达且当前网络无法恢复", "备用 profile 已批准", "恢复控制链路", "复测完整恢复门", "切回主网络 profile"),
        ],
        ["gateway ping", "route table", "公网 IP/DNS"],
        ["恢复路由备份", "切回主 profile", "保留 gateway 证据"],
        "路由替换为中风险；切换网络 profile 为高风险。",
        strategy_status="recommended_extension_static_card",
        taxonomy_status="future_recommended_extension",
        demo_implication="strategy extension/static taxonomy update only; not current live matrix PASS",
        training_data_implication="future data collection and target rebuild/adapter retraining required",
        root_object_mapping={"schema_version": "root_object.v1", "object_type": "network_route", "object_name": "gateway", "object_id": "net_gateway"},
        rationale="网关不可达在真实网络故障中常见，且比 wrong-PSK 更适合恢复策略库。",
        evidence_pattern=["IPv4 present", "default route present", "gateway ping fails", "public IP fails", "DNS may be secondary"],
    ),
    "net_firewall_or_iptables_block": strategy(
        "net_firewall_or_iptables_block_future_strategy",
        "防火墙或 iptables 阻断推荐扩展策略",
        "推荐作为后续 NET taxonomy/data 扩展项，当前未训练、未 live 验证。",
        ["确认接口/路由正常但特定策略阻断流量。", "保留阻断规则、探测和恢复门记录。"],
        [
            machine("low", "remove only the confirmed injected/blocking rule", "仅移除确认的阻断规则", "阻断规则来源明确", "人工确认规则匹配且有备份", "恢复被阻断流量", "复测目标 IP/DNS 和规则表", "按备份恢复规则"),
            machine("medium", "restore approved firewall baseline", "恢复批准的防火墙基线", "规则集偏离批准基线", "基线文件可信且审批通过", "恢复预期网络策略", "复测规则、IP、DNS、业务端口", "回滚到变更前规则快照"),
            machine("high", "flush firewall rules only under emergency approval", "紧急清空规则，强警告", "控制链路不可恢复且已确认规则误阻断", "必须人工审批并确认安全影响", "快速恢复连通性", "立即恢复批准基线并复测", "恢复基线规则并进行安全审计"),
        ],
        ["检查阻断规则", "复测 IP/DNS/端口", "确认基线一致"],
        ["恢复规则备份", "重新应用批准基线", "保留安全审计记录"],
        "仅删除确认规则风险较低；恢复基线为中风险；flush-all 仅应急高风险。",
        strategy_status="recommended_extension_static_card",
        taxonomy_status="future_recommended_extension",
        demo_implication="strategy extension/static taxonomy update only; not current live matrix PASS",
        training_data_implication="future data collection and target rebuild/adapter retraining required",
        root_object_mapping={"schema_version": "root_object.v1", "object_type": "network_policy", "object_name": "firewall", "object_id": "net_firewall"},
        rationale="防火墙/iptables 阻断可观测、可回滚，比 wrong-PSK 更适合策略库展示。",
        evidence_pattern=["interface and route healthy", "specific traffic blocked", "blocking rule present", "cleanup restores reachability"],
    ),
}


def get_strategy(label: str) -> Dict[str, Any]:
    if label not in ACTION_R1_STRATEGY_LIBRARY:
        raise KeyError(label)
    return copy.deepcopy(ACTION_R1_STRATEGY_LIBRARY[label])


def get_extension_strategy(label: str) -> Dict[str, Any]:
    if label not in RECOMMENDED_NET_EXTENSION_STRATEGIES:
        raise KeyError(label)
    return copy.deepcopy(RECOMMENDED_NET_EXTENSION_STRATEGIES[label])


def all_current_strategies() -> Dict[str, Dict[str, Any]]:
    return copy.deepcopy(
        {
            label: strategy
            for label, strategy in ACTION_R1_STRATEGY_LIBRARY.items()
            if strategy.get("strategy_status") != "downgraded_manual_only"
        }
    )


def all_extension_strategies() -> Dict[str, Dict[str, Any]]:
    return copy.deepcopy(
        {
            label: strategy
            for label, strategy in RECOMMENDED_NET_EXTENSION_STRATEGIES.items()
            if label != "net_gateway_unreachable"
        }
    )
