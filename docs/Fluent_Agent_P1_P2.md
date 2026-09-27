# Agent P1 / P2：证据完整性与运行一致性

2026-09-27。本轮按 P1 → P2 完成实现和离线回归；没有调用真实 Fluent、重启服务或修改历史 Case/运行产物。这里的 P1/P2 是架构加固优先级，不是原 Pre-Simulation Phase 1/2。

## P1：事实、推断与结果不能混用

- `pre_simulation/evidence.py` 从 Profile、确定性可行域、Registry、物理特征建立程序事实目录。PriorReasoner 只能逐字引用事实；推断留在 reasoning_summary/assumptions。Fusion 与 Validator 均校验事实文本、核验状态及版本，不只检查 source_id。
- Evidence/Fusion/Validator 策略升级到 2。当前没有已验证的变量—目标趋势或外部 Provider。因此所有证据等级均不能单靠高 confidence/速度/Reynolds 收窄域：recommended=feasible，high_potential=null。拒绝不受支持的提案，不静默修改提案。这是安全收紧，不是宣称已实现搜索域减缩能力。
- 结果只从 COMPLETE、有限值、身份一致、baseline_reloaded、solve_confirmed、gate_executed、PASS Gate 的样本选取。报告最佳观测点与已评估跨度，旧 observed_optimal_region 仅留 null 读取兼容，不再生成区域。
- BEST_OBSERVED 表示样本最佳；BEST_VERIFIED 还需身份、基线、Gate 与容差均通过独立复算。两者均不是全局最优；improvement_verified=false。未做对照实验，不能证明先验节省预算或设计优于基线。
- 温度默认只使用绝对容差，不按绝对 Kelvin/Celsius 值取百分比。可显式配置 absolute 或 reference_scale；后者需要正有限物理尺度与 policy_basis。默认 1e-6 的绝对容差会非常严格，正式运行前应由用户按目标单位明确批准合理容差，不自动放宽。
- verification/request.json 排他预留复算身份；result.json 绑定完整复算契约摘要。同契约结果复用，不再求解；历史/不同契约或未完成预留阻断重试。超时保持 ORPHANED，不冒充普通失败。
- UI 使用后端 BEST_VERIFIED 标记复核成功；图示显示有效样本的目标差值，不把绝对温度百分比表述为已验证设计收益。

## P2：持久化真源与保守恢复

`runtime_reconciliation.py` 在新提交前、tell 恢复前及结束时核对 Optuna、语义/native TrialLedger、完整结果、未知状态日志和独立复算状态，输出 runtime_reconciliation.json。它仅读取业务状态并写审计报告，不执行 Fluent，不更改 Optuna 或 Ledger。

| 状态 | 自动动作 |
| --- | --- |
| RUNNING + 完整匹配 Gate 文件，无未知状态/冲突 | 仅补 Optuna tell，再补合法 Ledger 转移；不重新求解 |
| 已 tell + Ledger 尚在 RESULT_READY/GATED 或可信运行态 | 仅补持久化到 TOLD，记录 PERSISTENCE_ONLY_NO_SOLVE |
| 任一来源 ORPHANED/UNKNOWN；复算预留未完成 | 阻断 Campaign，人工诊断 |
| Optuna COMPLETE 无完整结果或 value/constraint 不一致 | 阻断，不以数据库完成标志冒充可用结果 |
| Ledger TOLD 但 Optuna RUNNING；Campaign/参数身份不一致 | 阻断，不自动选择一方覆盖另一方 |

Optuna 的 tell 状态与匹配的完整 Gate 证据共同构成结果真源；未知运行状态拥有否决权。LangGraph/UI 显示不是求解完成证据。不存在自动清除 ORPHANED 或取消未知任务的逻辑。

Runner 的 session_audit.jsonl 记录只读 session_status 观察。只接收工具显式给出的 fluent_session_id/session_id，不用 PID、连接成功或随机 UUID 冒充 Solver ID。开会话、每次 Trial 前和清理前检查连续性；已知 ID 变化/消失或连接未知时阻断，不向替换会话发送退出/断连。Trial 文件携带身份可用性。同一 Runner 仍串行复用会话。

ModelInspector 继续拒绝无法只读核对 Case 的活动会话。无活动会话时同时追踪 Case、配对 Data、连接配置摘要；缓存依赖包括 scanner、规则、Registry、映射、证据策略、PyFluent/FastMCP 版本。已有 Partial Refresh 接口保留，不声称默认 MCP 已支持活动会话的局部刷新。

## 新增与修改文件

新增实现：pre_simulation/evidence.py、session_identity.py、runtime_reconciliation.py。

修改实现：pre_simulation/prior.py、validation.py、workflow.py、model_inspector.py；task_schema.py、verification.py、experiment_orchestrator.py、optimizer.py、runtime_reliability.py、runner.py、web.py；integrations/fluent/ui/app.js。

新增测试：test_evidence_integrity.py、test_verification_integrity.py、test_runtime_reconciliation.py、test_session_identity.py。

更新测试：test_pre_simulation_prior.py、test_pre_simulation_validation.py、test_pre_simulation_workflow.py、test_pre_simulation_inspector.py、test_planning.py、test_optimizer.py、test_runtime_reliability.py、test_thermal_guard.py。旧断言仅在对应行为明确收紧时更新；不删除覆盖。数值 Gate 拒绝测试的模拟结果补齐真实 Runner 必带的基线重载标记，仍验证失败候选消耗预算。

## 验收与仍有限制

回归入口：`python -m pytest tests/fluent tests/models -q`。覆盖事实改写、confidence 越权、无趋势缩域、温度容差、重复/未知复算、跨账本 orphan、Gate/tell 崩溃窗口、Session 身份变化、Profile Data/依赖失效以及原流程。

最终结果：285 passed，80 warnings，29.96 秒。警告为现有 FastAPI 生命周期弃用、Authlib 弃用及 Optuna constraints_func 实验接口提示。UI JavaScript 的 Node 语法检查通过，`git diff --check` 通过。未安装 Ruff，未把本轮描述为已通过 Ruff 检查；UI 未启动浏览器做视觉验收，以避免干扰现有服务。

本轮不构成真实 Fluent 验收。同步 MCP 仍没有可查询的持久 Job ID；缺失 Solver ID 时只能记录 UNAVAILABLE，不能证明进程未被替换；活动会话的安全只读状态摘要尚缺；多控制器并发需要额外 Campaign 锁。Provider、经验证物理趋势、几何/网格独立性和先验效率对照实验均未新增。正式运行应使用新完整审批；历史产物保留，不补签、不清除状态。
