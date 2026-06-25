#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_llm_sft_dataset.py

从 dataset.jsonl（结构来自 build_fault_samples.py）生成 LLM 微调用的对话数据 JSONL：
每行格式：
{
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ]
}
"""

import json
import argparse
import os
from typing import Dict, Any, List

SYSTEM_PROMPT = """你是一个面向 KaiHongOS / OpenHarmony 的系统故障诊断助手。
你可以综合系统指标（metrics）、进程快照（ps）、内核日志（dmesg）、应用日志（hilog）等信息，
判断当前 run 是否存在故障、属于哪一类故障场景，并给出简要的根因分析和排查 / 恢复建议。
回答时要条理清晰、尽量简洁，优先使用中文。"""

def truncate_list(lst, max_len):
    if len(lst) <= max_len:
        return lst
    return lst[:max_len]

def build_user_message(sample: Dict[str, Any]) -> str:
    run_id = sample.get("run_id", "unknown")
    scene = sample.get("scene", {})
    meta = sample.get("meta", {})
    metrics_window = sample.get("metrics_window", {}) or {}
    metrics_series = sample.get("metrics_series", {}) or {}
    events = sample.get("events", []) or []
    proc_snaps = sample.get("proc_snapshots", []) or []
    logs = sample.get("logs", {}) or {}

    lines: List[str] = []

    # 基本信息
    lines.append(f"【run_id】{run_id}")
    lines.append(f"【脚本版本】{meta.get('script_version')}")
    lines.append(f"【run_start_ms】{meta.get('run_start_ms')}")
    lines.append(f"【run_end_ms】{meta.get('run_end_ms')}")
    if meta.get("wukong_cmdline"):
        lines.append(f"【wukong_cmdline】{meta.get('wukong_cmdline')}")
    if meta.get("wukong_task_total") is not None:
        lines.append(f"【wukong_task_total】{meta.get('wukong_task_total')}")

    # metrics 窗口 + 摘要
    if metrics_window:
        mf = (metrics_window.get("mem_free_kb") or {})
        ma = (metrics_window.get("mem_available_kb") or {})
        cu = (metrics_window.get("cpu_util_total_x100") or {})
        lines.append("【metrics时间窗口】")
        lines.append(f"  start_ms={metrics_window.get('start_ms')}, end_ms={metrics_window.get('end_ms')}")
        lines.append("【metrics摘要】")
        lines.append(f"  mem_free_kb: min={mf.get('min')} max={mf.get('max')}")
        lines.append(f"  mem_available_kb: min={ma.get('min')} max={ma.get('max')}")
        lines.append(f"  cpu_util_total_x100: min={cu.get('min')} max={cu.get('max')}")
    else:
        lines.append("【metrics】本次 run 未获取到有效的 metrics 序列。")

    # metrics 序列（下采样后的）
    ts_list = metrics_series.get("ts_ms", []) or []
    mem_list = metrics_series.get("mem_free_kb", []) or []
    cpu_list = metrics_series.get("cpu_util_total_x100", []) or []

    # 相对时间（秒）
    run_start_ms = meta.get("run_start_ms") or (metrics_window.get("start_ms") if metrics_window else None)
    lines.append("【metrics采样点】(相对 run_start 的秒数, mem_free_kb, cpu_util_total_x100)")
    max_points = 16
    for i in range(min(len(ts_list), max_points)):
        ts = ts_list[i]
        mem = mem_list[i] if i < len(mem_list) else None
        cpu = cpu_list[i] if i < len(cpu_list) else None
        if run_start_ms is not None:
            rel_sec = round((ts - run_start_ms) / 1000.0, 1)
            t_str = f"+{rel_sec}s"
        else:
            t_str = str(ts)
        lines.append(f"  t={t_str}, mem_free_kb={mem}, cpu_util_total_x100={cpu}")

    # 事件
    if events:
        lines.append("【events（运行窗口内）】")
        for e in truncate_list(events, 8):
            lines.append(
                f"  ts={e.get('ts')}, level={e.get('level')}, "
                f"component={e.get('component')}, tag={e.get('tag')}, msg={e.get('msg')}"
            )

    # proc 快照（可变数量）
    if proc_snaps:
        lines.append("【进程快照（Top-RSS）】")
        for snap in truncate_list(proc_snaps, 4):
            lines.append(f"  snapshot ts_ms={snap.get('ts_ms')}, reason={snap.get('reason')}")
            for p in snap.get("top_rss", []) or []:
                lines.append(
                    f"    pid={p.get('pid')}, ppid={p.get('ppid')}, "
                    f"rss_kb={p.get('rss_kb')}, comm={p.get('comm')}"
                )

    # dmesg / hilog 片段
    dmesg_lines = logs.get("dmesg_snippets") or []
    hilog_lines = logs.get("hilog_snippets") or []
    if dmesg_lines:
        lines.append("【dmesg片段（截断）】")
        for l in truncate_list(dmesg_lines, 20):
            lines.append("  " + l)
    if hilog_lines:
        lines.append("【hilog片段（截断）】")
        for l in truncate_list(hilog_lines, 20):
            lines.append("  " + l)

    # 提问部分
    lines.append("")
    lines.append("请根据以上 metrics / 进程快照 / 日志信息，回答以下问题：")
    lines.append("1. 当前 run 是否为故障 (是/否)？属于哪一个故障家族 (cpu/mem/background/other)？")
    lines.append("2. 如果是故障，请给出最可能的具体场景标签（例如 cpu_multicore、mem_oomsafe、mem_severe 等）；如果是正常场景，请说明属于哪一类基线/背景场景。")
    lines.append("3. 用 2-4 句话说明你的根因分析依据，可以引用关键的指标趋势、进程信息或日志片段。")
    lines.append("4. 给出 1-2 条具体可执行的排查或恢复建议（正常场景可以给出观测与优化建议）。")

    return "\n".join(lines)


def build_assistant_message(sample: Dict[str, Any]) -> str:
    scene = sample.get("scene", {})
    label = sample.get("label", {})
    metrics_window = sample.get("metrics_window", {}) or {}

    family = scene.get("family", "other")
    scenario = scene.get("scenario_tag", "unknown")
    severity = scene.get("severity", "unknown")

    is_anomaly = label.get("is_anomaly", False)
    root_comp = (label.get("root_component") or {}).get("name")
    root_cause_text = label.get("root_cause_text") or ""
    confidence = label.get("confidence", 0.8)

    # 基于 is_anomaly / scenario 区分“正常基线” vs “故障”
    normal_scenarios = {"bg_idle", "cpu_baseline", "mem_mild"}
    is_normal_scene = (not is_anomaly) or (scenario in normal_scenarios)

    # 故障状态行
    if is_normal_scene:
        status_str = "否（正常）"
    else:
        status_str = "是（故障）"

    # 故障家族行，正常场景加解释
    family_explained = family
    if is_normal_scene:
        if scenario == "bg_idle":
            family_explained = f"{family}（背景场景）"
        elif scenario == "cpu_baseline":
            family_explained = f"{family}（CPU 基线压力场景）"
        elif scenario == "mem_mild":
            family_explained = f"{family}（轻微内存泄露基线场景）"

    # 尝试从 metrics_window 里拿一点数字放到描述里
    mem_stats = (metrics_window.get("mem_free_kb") or {})
    cpu_stats = (metrics_window.get("cpu_util_total_x100") or {})
    mem_min = mem_stats.get("min")
    mem_max = mem_stats.get("max")
    cpu_max = cpu_stats.get("max")

    lines: List[str] = []
    lines.append("1. 故障判定与家族")
    lines.append(f"   - 故障状态: {status_str}")
    lines.append(f"   - 故障家族: {family_explained}")
    lines.append(f"   - 场景标签: {scenario}")
    lines.append(f"   - 严重程度: {severity}")

    lines.append("")
    lines.append("2. 根因分析")

    if is_normal_scene:
        # ---- 正常 / 基线场景的分析 ----
        if scenario == "bg_idle":
            lines.append("   - 本次 run 未注入主动故障，仅包含系统服务和 WuKong 交互产生的负载，用于刻画背景/空闲场景的正常行为。")
            lines.append("   - 指标上未出现持续异常的 CPU 或内存异常波动，mem_free_kb 与 cpu_util_total_x100 均在合理范围内波动。")
        elif scenario == "cpu_baseline":
            lines.append(f"   - 本次 run 属于 CPU 基线压力场景，由 {root_comp or 'cpu_stress_demo'} 产生可控的 CPU 负载，用于模拟业务高峰期但仍然健康的负载水平。")
            lines.append(f"   - CPU 利用率峰值 cpu_util_total_x100 ≈ {cpu_max}，在预期设计范围内，未观察到异常抖动或长时间打满。")
        elif scenario == "mem_mild":
            lines.append(f"   - 本次 run 属于轻微内存泄露基线场景，由 {root_comp or 'memory_leak_demo'} 周期性分配少量内存，用于模拟正常业务中的缓慢内存漂移。")
            lines.append(f"   - mem_free_kb 在 {mem_min}–{mem_max} kB 范围内缓慢波动，未进入危险区间，可作为正常样本。")
        else:
            lines.append("   - 综合指标和日志，本次 run 没有明显故障特征，可以视为正常/基线场景，用于对比分析。")
    else:
        # ---- 故障场景的分析 ----
        if root_comp:
            lines.append(f"   - 初步判断根因相关组件/进程为: {root_comp}")
        if family == "mem":
            lines.append(f"   - 指标上表现为可用内存 mem_free_kb 在一段时间内降至约 {mem_min} kB，与场景 {scenario} 的高内存压力特征相符。")
        elif family == "cpu":
            lines.append(f"   - 指标上表现为 CPU 利用率峰值 cpu_util_total_x100 ≈ {cpu_max}，且持续处于较高水平，与场景 {scenario} 的高负载特征一致。")
        else:
            lines.append("   - 指标与日志表现出一定异常波动，需要结合具体场景标签和日志进一步定位。")

    if root_cause_text:
        lines.append(f"   - 综合说明: {root_cause_text}")

    lines.append("")
    lines.append("3. 建议的排查 / 恢复动作")

    if is_normal_scene:
        # ---- 正常/基线场景的建议 ----
        if scenario == "bg_idle":
            lines.append("   - 当前 run 可作为背景/空闲场景的对照样本，无需立即处置，建议保留用于与故障样本进行对比分析。")
            lines.append("   - 后续可以基于该场景的指标分布，设定 CPU/内存等指标的正常波动范围，用于告警阈值校准。")
        elif scenario == "cpu_baseline":
            lines.append("   - 当前 run 可作为 CPU 基线压力场景样本，用于刻画业务高峰期但系统仍健康时的指标形态。")
            lines.append("   - 建议持续观测该类场景下的延迟/卡顿指标，必要时再考虑优化调度策略或限流策略。")
        elif scenario == "mem_mild":
            lines.append("   - 当前 run 可作为轻微内存泄露基线样本，用于区分正常缓慢内存增长与真正的严重泄露故障。")
            lines.append("   - 建议在业务侧保持对长期运行进程的内存曲线监控，以便与本基线样本进行对比。")
        else:
            lines.append("   - 当前 run 未表现出明显故障特征，可作为正常行为样本用于对比和阈值校准。")
    else:
        # ---- 故障场景的建议 ----
        if family == "mem":
            lines.append("   - 建议重点关注内存分配频繁、RSS 占用较高的进程，检查是否存在未释放的缓存或对象。")
            lines.append("   - 可以通过临时重启可疑服务、优化内存池或加入 OOM 保护机制来降低风险。")
        elif family == "cpu":
            lines.append("   - 建议检查高 CPU 占用的进程及其线程栈，确认是否存在死循环或不必要的计算。")
            lines.append("   - 可考虑降低对应任务的优先级，或在业务层进行限频 / 限流，并配合监控告警。")
        else:
            lines.append("   - 建议结合关键日志（dmesg / hilog / faultlog）进一步排查具体异常事件。")
            lines.append("   - 可对可疑进程开启更细粒度监控或日志采集，以便后续复现和定位。")

    lines.append("")
    lines.append(f"4. 诊断置信度: {confidence:.2f}")

    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Convert dataset.jsonl to LLM SFT JSONL.")
    ap.add_argument("--input", required=True, help="输入的 dataset.jsonl 路径")
    ap.add_argument("--output", required=True, help="输出的 llm_sft.jsonl 路径")
    args = ap.parse_args()

    in_path = os.path.abspath(args.input)
    out_path = os.path.abspath(args.output)

    count_in = 0
    count_out = 0

    with open(in_path, "r", encoding="utf-8") as fin, \
         open(out_path, "w", encoding="utf-8") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            count_in += 1
            try:
                sample = json.loads(line)
            except Exception:
                print(f"[WARN] 跳过无法解析的 JSON 行 #{count_in}")
                continue

            # 没 metrics 的样本（ts_ms 为空）直接跳过 —— 之前已手工清理，这里做兜底
            metrics_series = sample.get("metrics_series", {}) or {}
            ts_list = metrics_series.get("ts_ms", []) or []
            if not ts_list:
                print(f"[WARN] run {sample.get('run_id')} metrics_series 为空, 跳过")
                continue

            user_msg = build_user_message(sample)
            assistant_msg = build_assistant_message(sample)

            item = {
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                    {"role": "assistant", "content": assistant_msg},
                ]
            }
            fout.write(json.dumps(item, ensure_ascii=False) + "\n")
            count_out += 1

    print(f"[INFO] 输入样本: {count_in} 条, 输出对话: {count_out} 条")
    print(f"[INFO] 已生成: {out_path}")


if __name__ == "__main__":
    main()
