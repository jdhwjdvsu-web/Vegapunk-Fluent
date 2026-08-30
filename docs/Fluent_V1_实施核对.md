# Fluent 无人值守 V1 实施核对

核对日期：2026-08-30

核对范围：评审文档“科研可用、串行、可断点恢复的无人值守 V1”

原则：代码能力、模拟测试和真实 Fluent 验收分开记录；没有真实运行证据时不标记完成。

## 总体结论

当前已经从“可演示的串行 Optuna walking skeleton”推进为“具备 Job、账本、恢复与完整 Gate 语义的 V1 代码骨架”。自动测试全部通过，Windows Job MCP 已完成实际启动和 `worker_health` 调用，并用真实 Mixing Elbow case 完成 1 个端到端 Job Trial。

系统仍不能宣布“无人值守 V1 验收完成”，因为基准重复性、真实 Windows Fluent Job 断线续查、Fluent 终止/服务重启故障注入和固定 20 点准入尚未在真实求解器上执行。

## 分阶段状态

| 阶段 | 当前状态 | 已完成 | 仍需完成 |
|---|---|---|---|
| 0 基准与范围冻结 | 部分完成 | 固定 Mixing Elbow case；基准重载；新增重复性分析器；容差由输入数据配置 | 冷启动 3 次、连续 10 次、进程内存记录、执行顺序测试、人工网格无关性 |
| 1 Windows MCP Job 化 | 核心闭环已实测，取消验收未完成 | 五个 Job 工具；稳定 Job ID；原子 JSON；事件日志；单 Worker；重启标记 `ORPHANED`；真实 Job 完成后新客户端可查询；重复提交返回原 `job_id` 且 `idempotent=true` | 运行中取消仍需下游确认 Fluent 实际停止；Job 服务进程崩溃后的真实旧进程清理测试 |
| 2 Controller 与恢复 | 正常路径已实测，故障恢复待验收 | V1 Trial 状态账本；心跳；Trial 超时；安全失败重试；未确认停止的 `ORPHANED` 禁止自动重试；结果后只补 Gate/Tell；SQLite pending tell 恢复；真实 Trial 到达 `TOLD` | 增加旧 Fluent 进程确认终止能力后再开放 ORPHANED 自动重试；人工杀 Fluent、关闭 Job MCP、终止 WSL Controller 的真实故障注入；许可证重新检出行为确认 |
| 3 最小四级 Gate | 大部分代码完成 | 参数白名单/类型/单位/上下限；安全线性组合约束；基线摘要；有限值；残差；last-N 监测量斜率/波动；质量守恒；通用盐分/组分守恒；PASS/CONSTRAINT/DIVERGED 分类 | 填写实际盐分报告名与阈值；Stage-0 case SHA-256；网格精度人工结论；真实 monitor history 验证 |
| 4 固定 20 点容错 | 工具完成、真实运行未完成 | 固定 20 点计划；结果一致性/重复计算/无效最优检查器；模拟准入测试 | 在隔离 Campaign 完成 13 正常点和 7 故障点，并生成真实 evidence records |
| 5 串行 Optuna | 代码完成、准入待定 | 固定 seed；TPE startup；ask/tell；SQLite 单 writer；Campaign 指纹；约束向量；DIVERGED→FAIL；中断后补 tell；完整优化摘要 | 必须先通过真实固定 20 点；建议再跑 20～50 个正式 Trial |
| 6 Vegapunk 外循环 | 部分完成 | `optimization_summary.json`；兼容 `final_info.json`；正式 `OptimizationDirective` 文件契约与人工批准校验；UI 人工批准门 | 子 Campaign 创建入口、Vegapunk 评分链实际接线；自动批准仍暂缓 |

## 本轮新增代码

- `vegapunk/fluent/campaign.py`：Campaign 创建、兼容性指纹、基线指纹。
- `vegapunk/fluent/history.py`：原子 JSON/JSONL 与 Trial 状态账本。
- `vegapunk/fluent/resources.py`：单 Worker 的许可证钩子、内存、磁盘、Trial/Campaign 预算保护。
- `integrations/fluent/job_store.py`：Windows Job 持久化与幂等。
- `integrations/fluent/job_service.py`：单 Worker 异步队列、状态、结果与安全取消语义。
- `integrations/fluent/mcp_server.py`：五个 V1 Job MCP 工具。
- `vegapunk/fluent/job_client.py`、`controller.py`：WSL 侧 Job 客户端、心跳、超时、重试和恢复。
- `vegapunk/fluent/baseline.py`：Stage-0 重复性和内存增长分析。
- `vegapunk/fluent/acceptance.py`：固定 20 点准入证据检查。
- `validity.py`、`codegen.py`：参数组合、last-N 数值稳定性、质量及盐分/组分守恒和 Gate 分类。
- `optimizer.py`：Campaign 隔离、约束型 TPE、失败映射、pending tell 恢复及完整优化摘要。
- `directive.py`：OptimizationDirective 文件契约与人工批准门。

## 真实 Job 闭环证据

- Campaign：`autofluentoptunademov01-d571931609fe`
- Job：`job-a4ff42fb7bac240d12af`
- 参数：`cold_inlet_velocity = 0.6621780832 m/s`
- 目标：`outlet-temp-avg = 294.784340698 K`
- 质量守恒相对误差：`1.5698248687e-06`
- Gate：`PASS`
- Job 状态：`SUCCEEDED`
- 重新连接查询：成功；重复提交：返回同一 Job，`idempotent=true`
- 可提交摘要：`docs/fluent_job_v1_smoke_summary.json`
- 本机完整 WSL 证据目录：`runs/fluent_job_v1_smoke/`

## 下一次真实 Fluent 操作顺序

1. 对选定 case 计算 SHA-256，写入 `connection.baseline_sha256`。
2. 记录 3 次冷启动和 10 次连续运行的指标、耗时、Fluent 内存，运行 `vegapunk.fluent.baseline`。
3. Windows 同时启动端口 18000 的 PyFluent-MCP 和端口 18001 的 Job MCP。
4. 用 `FLUENT_JOB_MODE=1` 先执行 1 个 Trial，断开 WSL 客户端后重新查询同一个 `job_id`。
5. 在隔离 Campaign 执行 `config/fluent/acceptance_v1.json`，按计划人工注入三类进程故障。
6. 只有 `acceptance_summary.json` 为 `passed=true` 后，才开始 20～50 Trial 的正式 Optuna Campaign。

## 明确暂缓

Worker Pool、多 Fluent 实例、SQLite 并行写、几何变化、自动重划网格、多目标 NSGA-II、多保真、代理模型、完整 ResourceManager、自动外循环审批均不属于本轮 V1。
