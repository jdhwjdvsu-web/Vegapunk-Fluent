# Fluent 实验规划 Agent V3：架构与使用

## 1. 范围与结论

V3 把一次性的方案回复改造成可追问、可修订、可审批、可恢复的 LangGraph
工作流。输入仍是自然语言；执行前的权威契约是严格 Pydantic
`ResolvedTaskObject`。既有 `FluentExperimentRunner`、Optuna ask/tell、
PyFluent-MCP、Job Service、Campaign/Trial/Attempt/Job ID、Case SHA256、Gate、
Trial 证据与独立复算均被保留。

本阶段没有运行真实 Fluent、真实模型 API 或真实 Workbench，也没有实现几何修改
和重网格。自动测试只使用 Fake Runtime 与 Fake Runner。

## 2. LangGraph 的职责

LangGraph 是 V3 唯一新增的 Agent 编排框架，负责 `StateGraph`、条件边、最多三次
修订、多轮状态、SQLite Checkpoint、人工审批 `interrupt`、恢复和结果解释路由。
它不替代模型、不决定数值安全、不批准任务，也不替代 Fluent Runner 或 Job
Service 的幂等性。

每个 Web Task 的稳定 `task_id` 同时作为 LangGraph `thread_id`。Checkpoint 默认
位于当前任务输出根的 `model_library/agent_checkpoints.sqlite3`。

## 3. UnifiedModelRuntime 的职责

`UnifiedModelRuntime.generate_json()` 仍是所有模型节点的结构化调用边界。每次调用
收到阶段名、固定研究问题、对话历史、任务身份和服务端提供的 Case 目录；响应按
对应 Pydantic JSON Schema 校验。模型负责语义判断和映射设计，服务端负责事实与
安全验证。调用有确定性超时，返回后再次核对 Task、Revision、Model、Case SHA256
和 Model Signature SHA256。

`simulation_agent.py` 仅保留运行时加载及旧 Python 调用兼容；Web 核心不再调用旧
的单次 `SimulationAgent.respond()`。

## 4. SimulationTaskObject

任务对象包含变量、范围、单位、目标、工程约束、求解要求、终止条件、假设、缺失
信息与问题。数值拒绝 bool、NaN 和 Infinity；变量上限必须大于下限；Trial、迭代
和时间使用服务端硬上限。单位、范围、目标或参考条件缺失时状态为
`needs_information`，不会自行补猜。

示例：

```json
{
  "schema_version": 3,
  "research_question": "在 wind_direction=30 deg 下优化风扇安装角",
  "variables": [{
    "id": "fan_angle",
    "name": "风扇安装角",
    "semantic_key": "fan_installation_angle",
    "minimum": -30,
    "maximum": 60,
    "initial_value": 0,
    "unit": "deg",
    "description": "固定几何代理变量"
  }],
  "objective": {
    "semantic_metric": "outlet_velocity",
    "metric_key": "surface-product-velocity-avg-…",
    "direction": "maximize",
    "aggregation": "area_average",
    "location": "product",
    "unit": "m/s"
  },
  "constraints": [],
  "solver_requirements": {"iterations": 100, "initialization": "hybrid"},
  "termination": {
    "max_trials": 20,
    "max_wall_time_seconds": 3600,
    "no_improvement_trials": 5,
    "target_objective": null,
    "maximum_failed_trials": 10
  },
  "assumptions": ["XY 坐标；+X=0 deg；逆时针为正"],
  "questions": [],
  "needs_information": false
}
```

## 5. FluentAgentState

持久状态记录 `thread_id/task_id/conversation_revision`、三项模型身份、原始研究
问题、消息、Task Object、分类、MappingSpec、Resolved Task、追问、校验错误、
几何阻断、审批指纹、Campaign/Trial/Job、完成/可行/失败计数、最优结果、终止原因
和状态。

“继续”“好的”只进入 `latest_user_message` 和消息历史，不覆盖首轮
`research_question`。清空对话递增 `conversation_revision`；新任务获得新
`task_id`。等待模型返回期间若任一身份变化，旧响应以
`stale_response_discarded` 丢弃。

## 6. 节点和条件边

```text
START → task_parser_agent → task_schema_validator
  ├─ 信息不足 → clarification_router → END（等待下一轮）
  └─ 完整 → variable_classifier_agent → classification_validator
       ├─ 无效且 <3 次 → variable_classifier_agent
       ├─ 达上限 → clarification_router → END（人工处理）
       └─ 有效 → mapping_designer_agent → mapping_spec_validator
            ├─ 无效且 <3 次 → mapping_reviewer_agent → mapping_spec_validator
            ├─ 达上限 → clarification_router → END（人工处理）
            └─ 有效 → resolved_task_compiler
                 ├─ GEOMETRY_UNSUPPORTED → END
                 └─ human_approval_gate（暂停）
                      → experiment_orchestrator
                      → result_interpreter_agent
                      → geometry_recommendation_builder → END
```

节点名与代码一一对应，共 13 个。外部 WebRuntime 在批准后执行既有优化链，并把
完成结果写回 `experiment_orchestrator` 边界，Graph 再决定是否解释几何结果。

## 7. 三类变量

- `DIRECT`：模型选择一个当前 Case 已扫描、活动、非只读、常数数值参数。例如入口
  速度 `velocity` 一对一绑定扫描得到的参数 ID。
- `MAPPED_PROXY`：模型选择当前 Case 可执行的代理能力，并生成正反向 Mapping DSL。
  风扇安装角属于此类时只代表固定几何下的入射角代理。
- `GEOMETRY_UNSUPPORTED`：需要几何/网格能力且没有通过验证的代理。Resolved Task
  的 `executable=false`，不能审批或进入 Runner，并报告
  `geometry_parameterization/remeshing/mesh_quality_validation` 等所需能力。

分类枚举只有这三项。分类本身由模型完成，Capability Registry 不代替模型分类。

## 8. Capability Registry

`CapabilityRegistry` 版本为 `3.0`。它从当前 Model Profile 的扫描参数和指标构造
Case-scoped 事实，包括 capability/version、semantic keys、zone/physics 要求、
参数与指标 ID、单位、硬边界、坐标系、假设和 executable。

静态声明不等于可执行。例如 `mapped.fan_incidence_2d.v1` 只有在扫描目录真实出现
`flow_direction_x` 和 `flow_direction_y` 规则时才可执行；可选 Z 分量也只能引用
真实扫描 ID。Registry 从不生成 Fluent Setting Path。

## 9. 为什么分类与映射由模型完成

用户语言与 Case 能力之间存在语义判断：同一“角度”可能是可直接修改的参数、可由
流向代理的参考量，也可能必须重建几何。因此模型选择分类、Capability、代理变量、
参数 ID、指标 ID 和公式；服务端只给候选事实并检查结果。这保留了模型推理能力，
同时避免让模型拥有批准、代码执行或目录外访问权限。

## 10. Mapping DSL 与确定性验证

允许的运算只有 `add/subtract/multiply/divide/negate/sin_deg/cos_deg/tan_deg/`
`deg_to_rad/rad_to_deg/wrap_angle/clamp/normalize_vector/constant/reference`。
表达式是递归 JSON 树，不使用 Python、`eval/exec`、MCP 命令、网络调用、文件路径
或 Fluent Setting Path。

Validator 检查 Schema、白名单、引用闭包、Capability/Parameter/Metric 目录、Case
可执行性、源与输出单位、坐标系、角度零点和正方向、有限数、参数硬边界、方向向量
归一化，以及边界/中点样本上的正反向一致性。错误以结构化列表返回
`mapping_reviewer_agent`。第三次仍失败则转人工处理。

MappingSpec 审批后写入 `execution_contract` 并参与 Campaign 指纹；Trial 期间不会
再次调用模型。改映射必须使用新的审批和 Campaign。

## 11. 风扇角度代理：正向映射

模型可以在明确参考风向和坐标约定后生成：

```json
{
  "relative_incidence_angle": {
    "op": "subtract",
    "args": [{"ref": "wind_direction"}, {"ref": "fan_angle"}]
  },
  "flow-x-id": {"op": "cos_deg", "args": [{"ref": "relative_incidence_angle"}]},
  "flow-y-id": {"op": "sin_deg", "args": [{"ref": "relative_incidence_angle"}]},
  "flow-z-id": {"value": 0}
}
```

这里的 `flow-*-id` 必须是本次扫描真实 ID。`wind_direction` 未知或坐标约定缺失时
Graph 先提问。

## 12. 反向几何映射

对应反向树为：

```json
{
  "fan_angle": {
    "op": "subtract",
    "args": [{"ref": "wind_direction"}, {"ref": "relative_incidence_angle"}]
  }
}
```

服务端使用已批准的反向树复算建议值。模型给出的几何值与反向树不一致时，拒绝
持久化建议。

## 13. 代理最优不等于真实几何验证

改变入口方向是在固定几何和固定网格上的代理实验。真实安装角可能同时改变流道、
间隙、局部网格质量和物理边界，因此代理最优只能作为候选。所有建议固定带有
`verification_required=true`、`automatic_execution=false` 和代理结果声明。

## 14. GeometryRecommendation

只有存在 MAPPED_PROXY、至少一个可行 Trial 和最优结果时才调用解释节点并生成：

```json
{
  "result_class": "geometry_recommendation",
  "source_campaign_id": "campaign-…",
  "source_trial_id": "trial-0007",
  "capability_id": "mapped.fan_incidence_2d.v1",
  "mapping_id": "fan-angle-proxy",
  "mapping_version": "1.0",
  "proxy_variable": "relative_incidence_angle",
  "optimal_proxy_value": 20.0,
  "recommended_geometry_parameter": "fan_angle",
  "recommended_geometry_value": 10.0,
  "unit": "deg",
  "reference_conditions": {"wind_direction": 30.0},
  "assumptions": ["fixed geometry proxy"],
  "confidence": 0.8,
  "verification_required": true,
  "verification_route": "FUTURE_WORKBENCH_MCP",
  "automatic_execution": false,
  "future_handoff_request": {"delta": 1.0}
}
```

结果写入 `geometry_recommendation.json`。全部不可行时不会生成该文件。

## 15. 未来 Workbench MCP 接口

稳定端口为 `GeometryHandoffPort.submit(GeometryVerificationRequest) ->
GeometryHandoffResult`。默认 `SuggestionOnlyGeometryHandoff` 只把请求写入
`geometry_verification_request.json`，返回 `mode=suggestion_only`、
`submitted=false`、`requires_manual_geometry_update=true`。

请求包含 optimum-delta、optimum、optimum+delta 三个候选及参考条件。预留类
`WorkbenchMcpGeometryHandoff` 目前明确抛出 `NotImplementedError`，没有网络逻辑。
默认配置在 `config/fluent/agent_v3.json`：

```json
{
  "geometry_handoff": {
    "provider": "suggestion_only",
    "workbench_mcp_endpoint": null,
    "automatic_execution": false
  }
}
```

## 16. 人工审批与指纹

模型输出没有审批字段，Graph 在 `human_approval_gate` 使用 interrupt 暂停。审批
指纹绑定模型 ID、Case SHA256、Model Signature、完整 Task Object、三分类、Registry
版本、Capability/Parameter ID、完整 MappingSpec、DSL/Mapping 版本、正反表达式、
单位、坐标/角度约定、假设、终止条件、verification route 和
`automatic_geometry_execution=false`。任一变化使旧审批失效。

GEOMETRY_UNSUPPORTED、未回答追问、验证错误、缺少参数/单位/坐标约定、目录逃逸、
非有限数或模型身份变化均不能审批或执行。

## 17. 实验编排与不可行解修复

`compile_resolved_experiment` 生成两个现有 `ExperimentSpec`：语义 Spec 供 Optuna
采样，原生 Spec 供 Runner。`MappingRunnerAdapter` 每个 Trial 只执行冻结的 DSL，
再调用现有 `evaluate_point()`。日志、SQLite Study、Trial JSON、生成代码、stdout、
Case 重载、Gate 和 Job ID 行为不变。

优化器现分别统计 `solved_trials/completed_trials/feasible_trials/failed_trials`。
最佳 Trial 只从 constraint vector 全部 `<=0` 的 COMPLETE Trial 中选择。全部不可行
时 `best_trial_number/best_params/best_value` 均为 null，终止原因为
`budget_exhausted_without_feasible_solution`，且不做独立最佳候选复算或几何建议。

终止条件 `max_trials/max_wall_time_seconds/no_improvement_trials/target_objective/`
`maximum_failed_trials` 全由控制器确定性判断，不交给模型。

## 18. Web/API 使用

1. 启动现有 Web 工作台并分析 Case；扫描完成后 Registry 绑定当前 Profile。
2. 在右侧输入完整自然语言任务；若状态为 `needs_information`，继续回答问题。
3. 在“实验方案确认”查看 Task Object、分类、Capability、MappingSpec、Reviewer 错误
   和 Resolved Task。
4. 人工批准后才可启动实验。代理变量任务按已批准语义范围采样。
5. 结果页查看独立复算与 GeometryRecommendation。界面固定显示“当前仅生成几何
   修改建议，尚未连接 Workbench 自动执行”。

主要接口：`POST /api/agent/messages`、`GET /api/agent/state`、
`POST /api/agent/reset`、`GET /api/tasks/current/plan`、
`GET /api/tasks/current/resolved-task`、
`GET /api/tasks/current/geometry-recommendation`、
`GET /api/tasks/current/geometry-verification-request`。动态用户/模型内容通过 DOM
`textContent` 渲染，不插入 HTML。

## 19. 扩展 Capability、DSL 与当前验证边界

增加 Capability 时，在版本化 Registry 中声明语义键、Case 条件、真实扫描规则、
单位、硬范围、坐标和假设；增加相应 Fake Case 的“缺参数不可执行”和“全部参数可
执行”测试；最后递增 Registry 版本，使旧审批自动失效。

扩展 DSL 时，必须同时更新 Pydantic Schema、运算白名单、纯函数 evaluator、参数
数量/有限数规则、引用闭包和边界/逆映射测试。不得加入通用解释器、动态属性访问、
文件、网络或 Fluent Path 能力。

本次自动验证涵盖 Graph/Checkpoint、三分类、追问、修订上限、目录逃逸、DSL、
风扇正反映射、身份过期、审批指纹、代理执行、几何建议、suggestion-only 及全部/
部分不可行恢复。尚未进行真实 LLM 语义质量评估、真实 Fluent Campaign、真实
Workbench 几何重建/重网格或未来 MCP 适配器集成测试。
