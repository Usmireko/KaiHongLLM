# Subagent Templates

Codex may not automatically spawn subagents unless explicitly requested.

Use these templates when generating Codex prompts for this project.

---

## 1) Low-risk / ordinary implementation template

Use this for:
- local fixes
- small scripts
- documentation
- simple guards
- minor output formatting
- clearly scoped one-file changes

Template:

  请使用 os-fault-engineer skill。

  这是低风险/普通实现任务，不需要 subagents，除非探索后发现影响面扩大。

  按以下阶段执行：
  Scope -> Explore -> Plan -> Patch -> Validate -> Self-review -> Final

  要求：
  1. 每阶段输出简短 [PROGRESS]。
  2. 先列出候选文件，再修改。
  3. 做最小补丁。
  4. 运行最小可用验证。
  5. 递归修复最大深度为 3。
  6. 如果 Depth 3 后仍失败，停止并输出未解决原因。
  7. 最终输出：
     - Files changed
     - Key diff summary
     - Commands run
     - Validation result
     - Remaining risks
     - Recursion depth reached

  任务：
  <TASK>

---

## 2) Medium-risk implementation template

Use this for:
- localized parser/exporter/validator changes
- one fault subtype handling update
- localized PowerShell / hdc command adjustment
- one pipeline step change
- changes that need call-chain exploration but not full adversarial review

Template:

  请使用 os-fault-engineer skill。

  这是中风险任务，请使用以下 subagents：

  1. project-explorer
     - 只读
     - 找相关文件、调用链、数据流、验证命令和风险点
     - 不修改文件

  2. project-implementer
     - 根据 explorer 结果做最小补丁
     - 保持接口兼容
     - 修改后运行最小验证

  3. project-repairer
     - 仅在 validation 失败时启用
     - 每层只修一个明确问题
     - 最大递归修复深度为 3
     - Depth 3 后仍失败则停止

  任务：
  <TASK>

  最终由主 agent 汇总：
  1. explorer 发现
  2. implementer 修改
  3. repairer 修复情况，如有
  4. commands run
  5. validation result
  6. remaining risks
  7. recursion depth reached

---

## 3) High-risk implementation template

Use this for:
- network fault subtype boundary changes
- trigger / lock / cooldown / state-machine changes
- recovery gate semantics
- GT / OBS boundary
- L1 / L2 schema or semantics
- cross-end Windows / board / server coordination
- dataset label policy
- validator acceptance policy
- action recommendation safety gates

Template:

  请使用 os-fault-engineer skill。

  这是高风险任务，请显式使用以下 subagents：

  1. project-explorer
     - 只读
     - 找相关文件、调用链、数据流、验证命令和风险点
     - 不修改文件

  2. project-implementer
     - 根据 explorer 结果做最小补丁
     - 保持接口兼容
     - 修改后运行最小验证

  3. project-reviewer
     - 只读审查补丁
     - 重点检查：
       a. 漏改消费者
       b. PowerShell 引号
       c. BusyBox/Toybox 命令兼容性
       d. GT/OBS 分离
       e. 网络 subtype 边界
       f. L1/L2 导出和 validator 影响
       g. recovery gate 语义
       h. run_window 对齐
       i. evidence-gaming 风险

  4. project-repairer
     - 只在 reviewer 或 validation 发现明确问题时启用
     - 最大递归修复深度为 3
     - 每层只修一个明确问题
     - Depth 3 后仍失败则停止

  任务：
  <TASK>

  最终由主 agent 汇总：
  1. explorer 发现
  2. implementer 修改
  3. reviewer 问题
  4. repairer 修复情况，如有
  5. commands run
  6. validation result
  7. remaining risks
  8. recursion depth reached

---

## 4) Review-only template

Use this when Codex should review but not patch.

Template:

  请使用 project-reviewer 进行对抗审查，不要修改文件。

  审查对象：
  <PATCH_OR_FILES_OR_TASK_RESULT>

  重点检查：
  1. 是否漏改消费者
  2. 是否存在接口兼容问题
  3. 是否违反 BusyBox/Toybox 限制
  4. 是否存在 PowerShell hdc/ssh 引号风险
  5. 是否破坏 GT/OBS 分离
  6. 是否错误推广 OBS 为 GT
  7. 是否破坏网络故障 subtype 边界
  8. 是否遗漏 L1/L2/validator 链路
  9. 是否存在 recovery gate 语义误判
  10. 是否验证不足

  输出格式：
  - Critical
  - High
  - Medium
  - Low
  - 建议修复顺序

  只输出可执行问题，不要泛泛而谈。