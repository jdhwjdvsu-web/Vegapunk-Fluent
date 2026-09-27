# P0：完整实验审批与统一执行门禁

实施日期：2026-09-26。本批只修改契约、启动校验与测试，没有启动 Fluent、重启服务或重放旧 Campaign。

## 两种明确的实验模式

| 模式 | 审批来源 | 是否使用 Pre-Simulation Prior |
| --- | --- | --- |
| `V3_APPROVED` | UI 的 V3 完整实验审批 | 否；不能声称完成了物理先验规划 |
| `FROZEN_PRIOR` | PreSimulationWorkflow 的完整实验冻结审批 | 是；必须保留 Frozen Prior 元数据 |

两种模式都通过 `run_resolved_optimization()` 的同一道门禁：

```text
编译（不等于审批） → 完整实验审批快照 → 启动前契约/基线核对
→ Existing Optuna → MappingRunnerAdapter → Existing Runner/MCP/Fluent
```

本批没有让普通 UI 自动调用 PriorReasoner，没有新增 UI，也没有把原有手动/单点兼容入口改称为 Frozen Prior 流程。若以后 UI 提供 Frozen Prior 模式，必须走完整预规划与审批，不得只更换模式名称。

## 审批覆盖什么

`ExecutionApproval` 是不可变的版本化对象。它保存审批 ID、审批人、模式、规范化 JSON 快照及 SHA256，快照覆盖：

- 完整 ResolvedTaskObject：目标、约束、固定条件、变量、映射、预算与复算要求；
- 编译后的 semantic/native ExperimentSpec：实际报告、参数、求解及连接配置；
- 当前 CapabilityRegistry 完整公开契约和 Gate 版本；
- Case 文件路径及明确的内容指纹；热续算的配对 Data 路径和指纹。

Frozen Prior 另保存 `FrozenExperimentInputs`：原始完整 V3 Task、模板、endpoint、Case/Data 指纹及 Gate 版本。编译时重新生成并逐项比较，防止复用旧 Prior 审批来更换目标、固定条件或预算。

规划后 Task 若发生变化，审批前要求重新规划。审批后的执行契约若发生变化，启动前要求重新审批。编译后的 Case/Data 若变化，在创建 Runner 前拒绝执行。

配置变化也会使本批完整审批失效；暂未引入“仅调整运行超时”的授权豁免规则。

## 持久化与历史兼容

每个新 Campaign 保存 `execution_approval.json`。相同目录中已有不同审批时拒绝运行，不覆盖原文件。历史 Campaign 没有该文件时要求使用新的目录。

- 旧 V3 审批缺少完整执行快照：UI 标记过期，必须重新审批。
- 旧 Frozen Prior 只有范围/预设审批：保持可读取，不能直接编译执行。
- prior-only Schema/验证测试仍有效，但不授予执行权。
- 历史 Phase 7/7R 文件未迁移、未补签、未覆盖。
- 原 Phase 7R 验证脚本使用历史冻结对象，不能直接重放；新的真实验证必须使用新完整审批。

摘要分别记录 `execution_mode`、`pre_simulation_applied` 与审批快照哈希，避免将普通 V3 实验误报为物理先验实验。

## 权限与剩余边界

这里的审批是受控服务端流程及受信任的本地审批存储，不是数字签名。SHA256 用于发现内容漂移，不能防止拥有审批目录写权限的人重建整个审批记录。

本批未解决物理推理的证据内容校验、复算容差、活动会话身份核验或运行状态自动对账；这些属于后续 P1/P2。未新增 Provider、变量类型、优化算法或真实 Campaign。

## 验证

新增 `test_execution_approval.py`：20 项测试，覆盖缺审批、任务/映射/原生参数/Gate 改动、冻结目标/约束/固定条件/预算输入漂移、旧审批拒绝、Case/Data 漂移、审计文件保护和规划后 Task 改变。

UI 测试补充：普通 V3 模式明确标识；旧审批在 Runner 启动前拒绝；恢复完整审批后原启动幂等性仍成立。

完整 `tests/fluent`：228 passed，69 warnings。未删除旧测试。当前 Python 环境没有 ruff，未执行 Ruff 静态检查。
