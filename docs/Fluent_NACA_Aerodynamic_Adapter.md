# Fluent NACA 气动闭环适配

本适配使 Vegapunk-Fluent 能够安全复用 Fluent Case 中已有的 lift/drag
Report Definition，并支持二维 velocity-inlet 攻角代理和 `Cl/Cd` 目标。

## 已实现能力

1. `pressure_inlet` 与 `velocity_inlet` 的方向均通过完整 XYZ 虚拟分量暴露。
   二维 Fluent 向量在目录中补零 Z；执行时必须整组提交并满足单位向量约束。
2. 扫描允许 Fluent 文件中小于 `1e-3` 的方向舍入误差，并只向目录发布归一化值；
   执行写入仍使用 `1e-6` 单位向量门槛。
3. 当实验选择已有 lift/drag 或派生升阻比且只使用一个方向入口时，改变方向会同步：
   - drag vector = `(dx, dy)`；
   - lift vector = `(-dy, dx)`；
   - 保留 Fluent Report Definition 的其余尾部分量；
   - 所有写入均进行回读验证。
4. Case 中已有的 lift/drag 报告会进入 metric catalog，不会被重新创建或覆盖。
5. `derived_ratio` 是固定白名单派生指标，仅允许两个已命名 Fluent 报告相除，
   并带有限数转换及分母 epsilon 保护。
6. 当气动系数实验改变唯一 velocity-inlet 的速度时，自动同步
   `setup.reference_values.velocity` 并回读验证。
7. Runner 记录 `iterations_requested`、`iterations_actual`、首末 Fluent
   迭代编号和计数来源。实际计数来自本次 Solver stdout 中通过格式验证的残差行。

## 安全边界

- 多个方向入口同时配合 lift/drag 报告时拒绝自动联动，必须先明确力报告属于哪个入口。
- 三维升力方向没有唯一数学定义；当前自动力向量联动只采用二维 XY 约定。
- `derived_ratio` 不接受任意表达式、Python、Fluent 路径或用户提供代码。
- 已有报告必须由模型扫描发现；Agent 只能从 metric catalog 中选择。
- 迭代实际值依赖 Fluent 每步输出残差。本字段同时保存计数来源，不能与历史绝对迭代编号混用。

## NACA0012 实机回归

官方 `Flying on Earth` case 在 6°、40 m/s 下通过通用 Runner 执行：

- `Cl = 0.6338329824`
- `Cd = 0.02625485624`
- `Cl/Cd = 24.14155220`
- 请求迭代：200
- 实际迭代：147
- 最终 continuity：`9.4567e-7`
- x/y velocity、k、omega 残差均低于 `1e-6`

审计文件位于：

`runs/naca0012_improvements_20260916/generic_runner_6deg/`
