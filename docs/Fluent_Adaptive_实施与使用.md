# Fluent Adaptive：参数自动适配第一版

更新：2026-09-07。改造目录仅为 `D:\Vegapunk-Fluent`，不修改 `D:\Vegapunk`。

## 本轮交付边界

| 阶段 | 状态 | 当前实际能力 |
| --- | --- | --- |
| 1 Dynamic Parameters | 已实现并实测 | 默认推荐 2 个，Web/UI 支持 1～5 个；优化、单点、保存与恢复均使用列表 |
| 2 Model Introspection | 首版已实现 | 通过独占的 MCP 会话读取版本、维度、求解器、边界、材料、物理场和 Reports；保留扫描代码与输出 |
| 3 Parameter Resolver | 通用规则首版 | 按类型和活动物理模型发现参数；不依赖 Case 名称、入口名称或材料名称 |
| 4 Auto Recommendation | 首版已实现 | 可解释的关键词与安全优先级评分，默认 Top 2；不是 LLM，也不自动批准计算 |
| 5 Profile / Model Library | 首版已实现 | SQLite 模型库，SHA256、版本与规则身份、人工范围、选择、历史输出目录引用；变更后重新分析 |
| 6 Objective / Gate Adaptation | 尚未实现 | 新 Case 的自动目标与质量门推荐、确认和报告创建仍待开发；当前拒绝误用旧 Case 模板 |

不能将本次交付称为“任意 Fluent Case 全自动优化”。它完成了前置参数发现链路的第一版，以及真实 Mixing Elbow 五参数闭环回归。

## 使用方法

1. 启动项目专用的 Windows PyFluent-MCP，再启动 WSL 中的 Web。MCP 不得暴露到公网；WSL NAT 下使用 Windows 的 WSL 虚拟网卡地址，不要直接使用 WSL 自己的 localhost。
2. 进入“模型与数据”，填写 Windows Case 绝对路径，选择 2D / 3D。MCP 地址目前在“实验方案 → 高级设置”。Case 文件须能由 Controller 读取；Windows 路径在 WSL 自动映射到 `/mnt/<盘符>/...`。
3. 可填写 Fluent 产品版本，例如本次实测的 `26.1.0`。不填版本时每次重新扫描，避免默认 Fluent 升级后误用旧缓存。
4. 填写研究问题，点击“分析模型”或“生成实验方案”。扫描不迭代、不改原始 Case；如 MCP 已有会话则拒绝接管。
5. 默认推荐两个参数。每个下拉框按边界条件、湍流、材料物性分组。点击“＋ 添加优化变量”最多添加到 5 个；“× 删除”最少保留 1 个。
6. 检查单位、MIN / MAX。“保存范围”保存选择和人工范围；重复参数、非有限值、越界值和空范围会被拒绝。出口表压原值为 0 时不臆测范围，必须手填。
7. 仅对已有明确 Objective/Gate 合同的模型批准计算。默认配置的已验收 Mixing Elbow 以 SHA256 识别，沿用出口平均温度 MIN 与质量守恒质量门。其他 Case 可以扫描、选参数和保存 Profile，但会阻止计算，直到目标/质量门适配阶段完成。
8. 单点页也支持 1～5 个参数，使用精确值计算；现有温度云图导出仍属于已验收模板，尚非通用后处理。

“清空对话”不影响计算。“新建任务”归档旧任务、生成新任务 ID 和输出目录、清空当前模型选择及对话；Campaign 身份由后续实际实验配置生成，不把任务 ID 冒充 Campaign ID。旧 Case、审计证据和结果不删除。

同一输出目录的历史优化搜索空间不能随意改变。完成一次优化后要改参数组合或范围，先新建任务；原目录仅用于同一 Campaign 的续跑。

## 参数规则和安全范围

- 首批规则：velocity-inlet 的速度、温度、湍流强度、水力直径；pressure-outlet 的表压和回流温度；流体常数密度、黏度、比热、导热系数；固体常数密度和导热系数。
- 扫描器也识别其他边界类型，但目前不为所有类型开放修改规则。没有命中规则不意味着该模型没有可调参数。
- 必须读到活动、非只读的节点；带 option 的值必须是常数选项。表达式、UDF、表格、多项式和未知字符串不作为普通标量开放。
- 明确的长度常量 `1 [in]` / `4 [in]` 支持严格换算为米；不使用 eval。未支持的单位或表达式会被排除。
- 硬边界是 Fluent 可读取的 min/max 与软件保护区间的交集，不是“物理上一定安全”的承诺。
- 推荐范围是待确认的启发式：正值通常为原值的 70%～130%；温度采用原值 ±10 K；无法可靠推导时留空。
- 材料目录可能包含尚未分配到实际区域的材料。首版材料推荐优先级较低，正式优化前仍需核对材料分配与实验意义。
- 页面只发送参数 key 和数值，不接受任意 Fluent 路径、Python 或 TUI。
- 同一服务内扫描、计算互斥；已连接的外部 Fluent 会话不被接管。跨站浏览器控制请求被拒绝，远程页面默认只读。

## 数据与模块

`vegapunk/fluent/`：

- `model_introspection.py`：固定形态只读扫描，validate_code → run_code，记录审计材料。
- `parameter_rules.py`：版本化类型规则、常量与单位解析、范围生成。
- `model_profile.py`：参数契约和唯一的 V1 数量限制 `MAX_PARAMETERS = 5`。
- `parameter_ranker.py`：确定性的评分与推荐理由。
- `profile_store.py`：SQLite 存储与 Case 内容身份。
- `adaptive.py`：模型选择、缓存、范围、运行前身份检查和任务连接。
- `legacy_catalog.py`：只保留旧 Python 调用/测试兼容数据；Web 实时目录不再使用它。

输出根目录的 `model_library/models.sqlite3` 保存 Profile；`model_library/scans/` 保存扫描代码、验证结果和 stdout；各任务的 `model_selection.json` 保存任务自己的 Profile 快照。历史优化及 Optuna 结果按已有输出目录引用，模型库不复制或覆盖原始 Case。

缓存包含 Case SHA256、指定产品版本、维度、MCP endpoint、规则和扫描器版本。同一 Case 指定版本时可直接复用；文件内容变化产生新 Profile，旧历史保留。显式“强制重新分析”可刷新缓存。

## 本次验证证据

- Fluent 自动测试：67 passed（包含原有 40 项回归），另外通过 JavaScript 语法检查。
- 真实扫描：Fluent **2026 R1**，3D pressure-based；Energy ON、k-omega；6 个边界区、air 和 aluminum；扫描得到 16 个当前规则可编辑候选（含尚未分配的固体材料常数），无扫描警告。
- 浏览器验证：默认两个参数，增至五个后添加按钮禁用，删除到三个并保存，重载后恢复列表，无浏览器错误日志。
- 真实模型库复用检查：cached=True，16 个候选与已保存的五参数选择恢复，未重新启动 Fluent。
- 真实五参数优化：两路速度、热入口温度、冷入口湍流强度和水力直径；1 Trial，100 次请求迭代，约 24.1 秒 Trial 耗时，Gate PASS。
- 出口平均温度：`294.7196743957768 K`；质量守恒相对误差：`9.940999621348032e-7`，限值 `0.001`。
- 实测结果：`runs/adaptive_validation/web/trial_results/trial-0000.json`；对应 generated_code、solver_stdout、run_log.jsonl 和 study.sqlite3 同目录保留。
- 原始 Case SHA256 复核不变：`8145813f50f989a521ed8d1b02c891c7ca22ccc5fafa35a4eeba011949f19cbc`。

复测自动测试：`python -m pytest tests/fluent -q`。真实五参数复测脚本是 `scripts/validate_adaptive_live.py`，默认只检查目录，只有显式传 `--run` 才会计算；请使用隔离 Web 输出目录。

## 下一阶段

1. 实现 Objective/Gate 推荐及用户确认：先支持已有报告和常见单相流/传热报告，再扩展压降、组分与 RO 工程指标。
2. 按物理模型过滤无关报告与材料，明确不支持的 UDF、多相、膜参数和派生目标。
3. 用第二个不同区域命名、不同物性的真实 Case 完成端到端验收；当前通用性已有改名/不同材料的模拟签名单元测试，但不能代替真实第二 Case 计算。
4. 如要真正的对话 Agent，另行接入大模型结构化工具调用；本轮规则推荐不冒充这一能力。
