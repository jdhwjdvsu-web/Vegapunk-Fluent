# Fluent 无人值守 V1 实施核对

核对日期：2026-08-31

核对范围：评审文档“科研可用、串行、可断点恢复的无人值守 V1”

原则：代码能力、模拟测试和真实 Fluent 验收分开记录；没有真实运行证据时不标记完成。

## 总体结论

Mixing Elbow 系统准入已经完成：3 次冷启动、同一会话连续 10 次重复性测试和固定 20 点故障容错计划均取得真实证据，`baseline_assessment.json` 与 `acceptance_summary.json` 均为 `passed=true`。13 个正常优化点全部通过 Gate；WSL Controller 中断后恢复了同一个 Job；完整 Fluent 进程树终止被识别为可安全重试的失败；Job MCP 重启把未确认停止的 Job 保守恢复为 `ORPHANED`，没有重复提交。

这表示“科研可用、串行、可断点恢复的无人值守 V1”的系统链路已通过 Mixing Elbow 准入，但不等于水处理科研模型已经验收。水处理 Case 的盐分/组分报告、阈值和网格无关性仍需使用真实模型完成。

## 分阶段状态

| 阶段 | 当前状态 | 已完成 | 仍需完成 |
|---|---|---|---|
| 0 基准与范围冻结 | Mixing Elbow 已通过 | Case SHA-256 固定；3 次冷启动；同一会话连续 10 次；结果/耗时/Fluent+Cortex 内存记录；基准分析 `passed=true` | 水处理真实 Case 的人工网格无关性 |
| 1 Windows MCP Job 化 | 核心闭环与重启已实测，取消验收未完成 | 五个 Job 工具；稳定 Job ID；原子 JSON；事件日志；单 Worker；真实重启把运行中 Job 标记 `ORPHANED`；旧 Fluent 树按精确 PID 清理；完成后重连查询与幂等重提已验证 | 运行中取消仍需下游确认 Fluent 实际停止；通用自动旧进程回收尚未开放 |
| 2 Controller 与恢复 | Mixing Elbow 故障路径已通过 | 状态账本；心跳；超时；安全失败重试；不安全 ORPHANED 禁止重试；中断 WSL Controller 后恢复同一个 Job 到 `TOLD`；人工终止 Fluent 与重启 Job MCP 已实测 | 许可证服务器断开/重新检出仍需有相应环境后验证 |
| 3 最小四级 Gate | Mixing Elbow 已通过，水处理待配置 | 参数门；线性组合约束；有限值；残差；last-N 稳定性；质量及组分守恒；PASS/CONSTRAINT/DIVERGED 分类；真实质量守恒与故障夹具已验证 | 填写水处理盐分报告名与阈值；真实水处理 monitor history；网格精度人工结论 |
| 4 固定 20 点容错 | 已通过 | 独立 Campaign 完成 13 正常点与 7 故障点；20 条 evidence records；无重复计算、非终态、无效最优或不一致文件 | 水处理 Case 上可按需复验，不是系统 V1 阻塞项 |
| 5 串行 Optuna | Mixing Elbow 准入完成 | 固定 seed；TPE startup；ask/tell；SQLite 单 writer；Campaign 指纹；约束向量；DIVERGED→FAIL；恢复与优化摘要；13 点全部 PASS | 配置水处理模型后建议跑 20～50 个正式 Trial |
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

## 真实 V1 准入证据（2026-08-31）

- 基线：3 次冷启动 + 同一会话连续 10 次，13 次出口温度和质量流量相对离散度均为 0。
- 连续内存：1580.546875 MB → 1591.0390625 MB，增长 0.6638%，低于 10% 门限。
- 正常 Campaign：`autofluentacceptancev01-9e07d955ed25`，13/13 `PASS`。
- Controller 重启：`job-bc99c727a0dd44aeb614` 只进入一次 `RUNNING`，恢复到 `TOLD/PASS`。
- Fluent 终止：`job-e067789f9f9597f8e71a` 进入 `FAILED`，gRPC 丢失被记录，`retry_safe=true`，无结果文件。
- Job MCP 重启：`job-1bb2faebed35740e9512` 进入 `ORPHANED`，`retry_safe=false`，无自动重复提交。
- 固定 20 点：`acceptance_summary.json` 为 `passed=true`，全部异常列表为空。
- 可提交摘要：`docs/fluent_v1_acceptance_20260831_summary.json`。
- 本机完整证据：`runs/fluent_v1_acceptance_20260831/`；Windows Job 原始账本位于外部 Job Store。

## 下一次真实 Fluent 操作顺序

1. 提供水处理 `.cas.h5`/`.cas` 基线和粗、中、细三套网格，冻结 SHA-256。
2. 明确允许外部修改的 1～5 个参数及单位、上下限。
3. 明确目标报告、质量流报告、盐分/组分报告和工程阈值。
4. 对水处理模型完成 3 次冷启动、连续 10 次与人工网格无关性结论。
5. 先跑 1 个水处理端到端 Trial，再启动 20～50 Trial 正式串行 Campaign。

## 明确暂缓

Worker Pool、多 Fluent 实例、SQLite 并行写、几何变化、自动重划网格、多目标 NSGA-II、多保真、代理模型、完整 ResourceManager、自动外循环审批均不属于本轮 V1。
