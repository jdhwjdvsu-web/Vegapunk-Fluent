# Vegapunk-Fluent Adaptive Experiment Framework

更新：2026-09-14。本页记录 V2 的模型自适应基础；Agent 编排已由 V3 取代，当前说明见 [Fluent Agent V3 架构与使用](Fluent_Agent_V3_架构与使用.md)。改造目录仅为 `D:\Vegapunk-Fluent`，未修改原始 `D:\Vegapunk`。双风扇 Case 暂不实施，Mixing Elbow 仅作为回归样例。

## 当前闭环

1. 选择已准备好的 Fluent Case，填写研究问题并扫描模型。
2. 系统读取边界、材料、物理模型、cell zones 和报告状态，生成参数与指标目录。
3. LangGraph Agent V3 把自然语言解析为 Task Object，由模型完成三分类和 MappingSpec 设计，再由服务端按目录与 DSL 确定性验证；模型不能执行代码、修改 Fluent 或批准实验。
4. 用户核对 1～5 个参数、范围、目标方向、Gate 和预算，点击“批准并开始实验”。
5. 服务端保存方案并生成审批指纹，然后执行 Optuna → Runner → MCP → Fluent。
6. 每个 Trial 重载受保护基线；最佳可行候选再用新会话从基线独立复算。

## 参数、指标和报告

参数首批覆盖 velocity-inlet 速度、温度、湍流强度和水力直径，pressure-outlet 表压和回流温度，以及部分流体/固体常数物性。只开放扫描时确认活动、非只读、常数状态的节点。

指标首批覆盖边界面平均静压/速度/温度、表面最高温度、流体或固体 cell zone 的体积平均/最高温度，以及入口/出口总质量流量。`ReportSpec.kind` 支持 `surface`、`volume`、`flux`；体积报告使用 `report_definitions.volume`、`volume-max` / `volume-average` 和 `cell_zones`。

组合指标（例如压降）、Species/盐守恒、热流和任意既有自定义报告的安全导入仍待扩展。

## 审批与执行安全

审批记录由服务端生成，不接受浏览器或大模型传来的 `approved: true`。指纹绑定 Case SHA256、模型结构、Fluent 版本、参数和范围、目标、约束、Gate、求解设置、规则版本与预算；任一实质变更都会让旧审批失效。

大模型不能扩大预算、放宽范围、降低 Gate 或直接运行 Fluent。未知表达式、UDF、表格、多项式和未知单位不会作为普通参数开放。每次 Trial 与独立复算均从基线重新建立状态。

## 大模型助手（V3 已替换本节的单次回复架构）

助手复用 `config/model_catalog.yaml` 和 `UnifiedModelRuntime`。运行环境需要提供目录声明的密钥环境变量；当前默认是 `OPENAI_API_KEY`。缺少密钥或运行时失败时，状态接口明确返回 `agent.configured = false`，聊天接口报错，不再用本地关键词回复冒充大模型。

V2 的 `message + plan_patch` 只保留为旧 Python 调用兼容。Web 核心已迁移到持久
LangGraph StateGraph，输出严格 `SimulationTaskObject`、`VariableBinding`、
`MappingSpec` 和 `ResolvedTaskObject`。服务端仍过滤目录外 ID，并额外校验受限 DSL、
单位、坐标、正反映射和审批指纹。

## 网页操作

- “分析模型”生成/刷新 Model Profile；Case 或扫描/规则版本变化时缓存失效。
- 参数下拉默认 2 个，允许 1～5 个；目标下拉只显示当前 Case 可执行的指标。
- 质量守恒仅在发现完整入口和出口时启用。
- “批准并开始实验”依次保存方案、创建审批记录、提交运行；不需要额外 Windows 权限确认。
- “清空对话”只创建新的聊天 revision，不停止计算或删除证据。
- “新建任务”在空闲时归档旧任务并创建新 ID/输出目录；运行中会拒绝。

旧单点温度云图仍属于 Mixing Elbow 已验收模板，新模型不会绕过旧 `contract_confirmed` Gate 使用。通用 Fluent 图像导出仍需按所选目标字段和区域扩展。

## 证据文件

- `model_selection.json`：模型 Profile 快照。
- `experiment_plan.json`：规范化方案。
- `plan_approval.json`：审批指纹和已批准 `OptimizationDirective`。
- `agent_conversation.jsonl`：对话与方案建议审计。
- `trial_results/`、`generated_code/`、`solver_stdout/`：Trial 证据。
- `best_result.json`：最佳可行候选。
- `verification/result.json`：独立复算数值、Gate、容差和状态。
- `demo_summary.json`：运行摘要与复算状态。

## 当前验证与剩余工作

V2 在 2026-09-13 的历史基线为 **75 passed**。V3 的最终回归结果和新增覆盖以
`Fluent_Agent_V3_架构与使用.md` 及本次交付报告为准。

本轮没有启动双风扇建模，也没有声称完成第二个真实 Case。下一步是补齐网页约束/Residual/复算容差编辑、组合与组分指标、按目标自动导出 Fluent 原生图像，并用用户准备好的非双风扇 Case 做真实端到端验证。
