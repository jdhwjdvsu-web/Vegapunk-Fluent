<div align="center">
  <img src="./assets/vegapunk_logo.png" alt="Vegapunk Logo" width="180" />
  <h1>Vegapunk V1.0</h1>
  <p><strong>从科研问题、假设生成和实验验证，到可审查中文论文的端到端闭环系统</strong></p>
  <p>
    <a href="https://arxiv.org/abs/2505.16938">InternAgent 1.0 论文</a> ·
    <a href="https://huggingface.co/papers/2602.08990">InternAgent 1.5 技术报告</a> ·
    <a href="https://discovery.intern-ai.org.cn">项目主页</a> ·
    <a href="https://huggingface.co/collections/InternScience/internagent">Hugging Face</a>
  </p>
</div>

## 项目概览

Vegapunk 是一个面向长程自主科学发现的多智能体框架。系统能够围绕一个研究任务完成背景调研、假设生成、反思与演化、方法设计、实验执行、结果评估和跨轮次记忆。

本仓库进一步将 [PaperOrchestra](https://github.com/declare-lab/paper-orchestra) 完整源码固定在上游提交 `ca1b3fa01c2970fc7cda32d16245db38d57b3f56`，并 vendoring 到 `third_party/paper_orchestra/`。一次实验模式的 Discovery Launch 完成后，Vegapunk 会从系统自然产物中确定一个论文候选，构造上游要求的原料，再由原始 PaperOrchestra Agent、提示词和同步控制流生成 ICLR 2025 LaTeX 与 PDF。

> [!IMPORTANT]
> PaperOrchestra 的文本、JSON、视觉理解与图片生成均通过 Catalog-driven Unified Model Runtime 执行。文本和视觉能力遵循各自固定的 Catalog binding，图片生成使用同一 Provider 下独立的 image binding。首个可运行基线只读取系统自然产物，不读取源码或 `code_summary.json`，并保留上游自主文献与绘图流程。

## 核心能力

| 能力 | 说明 |
| --- | --- |
| 多智能体科研编排 | 串联生成、调研、反思、证据检索、演化、排序、方法开发与精炼 Agent |
| 多轮 Discovery Loop | 支持 `fresh` 与 `incremental` 模式，在多轮实验间更新候选和基线 |
| 可插拔实验执行 | 支持 Codex CLI、Qwen Code 和 OpenHands 后端，以及顺序、并行和可选 MCTS 搜索 |
| 算法发现与论文复现 | 提供 `tasks/` 算法任务，并通过 `sci_tasks` 子模块支持 ResearchClawBench 论文复现任务 |
| 记忆与深度研究 | 提供任务记忆、在线记忆、IdeaGraph、经验生成、MCP 工具和独立 QA/Deep Research 流程 |
| 自动论文闭环 | Discovery 结束后自动执行候选选择、原料整理、提纲、写作、内容反思、PDF 编译和 VLM 版式审查 |
| Fluent 实验规划 Agent V3 | 使用 LangGraph 持久规划自然语言 CFD 任务，服务端验证受限 Mapping DSL，并复用现有 Optuna、Runner、MCP、Gate 与审批链 |
| 源码忠实移植 | 完整保留固定上游源码、Agent、提示词、同步 pipeline 和自主绘图，仅在模型、输入输出和宿主执行边界做适配 |

## 系统架构

下图展示 Vegapunk 核心系统的入口、Discovery 执行、多智能体系统、Deep Research、任务、模型、工具、记忆与外部服务之间的关系。

![Vegapunk 系统架构](./diagram%20%281%29.png)

自动科研与论文产出的主路径如下：

```text
研究任务
  → 背景调研与假设生成
  → 反思、证据检索、演化与排序
  → 方法开发与候选实验
  → 多轮 Discovery Loop
  → discovery_summary.json
  → Terminal Candidate Selection
  → idea_sparse.md + experimental_log.md
  → Vendored PaperOrchestra 写作、绘图与审查
  → ICLR 2025 LaTeX
  → final_paper.pdf
```

论文阶段遵循以下边界：

- 每个 Discovery Launch 只发生一次 Paper Handoff，最多保留一篇成功 Paper；再次进入已成功的 Launch 会直接复用现有结果。
- 优先使用最后一轮中的成功候选；若最后一轮无成功实验，才向前回退到最近的成功轮次。
- `idea_sparse.md` 只呈现 Launch `prompt.json` 与被选候选的 `notes.txt`。
- `experimental_log.md` 呈现候选级实验叙述、按编号排序的 `run_N/final_info.json`，以及存在时的 `report/report.md` 和 `traceback.log`；`report.md` 缺失不阻塞流程。
- 首个基线不传 Research Draft、代码、代码差异或 `code_summary.json`。上游 Plotting Agent 仍可自主规划、生成、批评和修订图片。
- PaperOrchestra 作为一个子进程完整执行，不提供逐 stage 持久恢复；失败不会改写已经完成的 Discovery 产物，诊断信息保存在运行目录的日志中。

## 快速开始

### 前置条件

- Conda 或兼容的 Python 环境管理器
- Python 3.11
- 完整流程需要已在 `config/model_catalog.yaml` 选择的 Relay 或 Qwen Provider
- 至少一个实验执行后端：Codex CLI、Qwen Code 或 OpenHands
- 生成英文论文时需要 `pdflatex`、`latexmk` 和 `bibtex`；自动生成中文伴随稿还需要 `xelatex`，以及 TeX Live 中的 `ctex`、`xeCJK` 和 Fandol 字体集

> [!NOTE]
> GPU 不是框架启动和 `AutoDebug` 任务的必要条件。系统会在可用时分配 CUDA GPU，否则实验可按任务实现回退到 CPU；是否需要 GPU 主要取决于具体研究任务。

### 安装

```bash
git clone --recurse-submodules https://github.com/v6582374-netizen/Vegapunk.git
cd Vegapunk

conda create -n Vegapunk python=3.11
conda activate Vegapunk
pip install -r requirements.txt
```

如果仓库已经克隆但尚未初始化论文复现任务：

```bash
git submodule update --init --recursive
```

### 配置模型与凭据

在项目根目录创建 `.env`，按实际使用的服务填写：

```dotenv
# Relay Provider
OPENAI_API_KEY=

# Qwen DashScope Provider
DASHSCOPE_API_KEY=

# 若统一模型运行时选择 Anthropic Provider，则填写对应的 Anthropic API 密钥
ANTHROPIC_API_KEY=
```

模型 Provider、Canonical Model Identity 和能力绑定位于 `config/model_catalog.yaml`。
工作流、记忆和实验参数位于 `config/default_config.yaml`。
切换 Provider 时只修改 Catalog 的固定绑定，并为所选 Provider 配置对应密钥。

默认 Catalog 使用 `relay/gpt-5.6-sol` 作为 Active Text Model 和 vision binding，`relay/gpt-image-2` 作为 image-generation binding，并使用本地 `BAAI/bge-base-en-v1.5` embedding。
所有文本和视觉调用使用声明的 Responses protocol，不会静默回退到 Chat Completions。
Runtime 统一负责 Provider 并发与有界重试，且不会使用 provider-side background execution。

> [!WARNING]
> 默认配置面向完整研究运行，包含多候选、多轮 Discovery 和多次内容精炼，可能产生较长运行时间与较高模型费用。首次使用前请重点检查 `generation_count`、`top_ideas_count`、`loop_rounds`、`max_runs` 和并发设置。

### 运行发现—实验—论文闭环

`AutoDebug` 使用合成回归数据，不需要额外下载数据，适合作为首个端到端任务：

```bash
python launch_discovery.py \
  --config config/default_config.yaml \
  --task AutoDebug \
  --exp_backend codex
```

实验模式完成 `discovery_summary.json` 后，会同步触发固定运行目录 `paper_orchestra_runs/paper/`，并等待 PaperOrchestra 成功或失败后再退出。

也可以使用统一入口：

```bash
python launch.py --mode discovery --task AutoDebug --exp_backend codex
```

### 为历史 Discovery Launch 生成论文

```bash
python launch_paper.py \
  --launch-dir results/<task>/<timestamp>_launch \
  --config config/default_config.yaml
```

该命令与自动 Handoff 使用同一服务：成功结果会直接复用；失败运行可以重新执行，但不会创建第二篇论文或提供逐阶段恢复。

### 运行独立研究问答

```bash
python launch_qa.py \
  --question "记忆增强型大模型最近有哪些进展？" \
  --output answer.md
```

QA 模式走 Deep Research 流程，不执行实验，也不会触发论文生成。

## 输出与可追溯性

一次完整运行的核心目录如下：

```text
results/<task>/<timestamp>_launch/
├── prompt.json
├── discovery_summary.json
├── session_<id>/
│   ├── ideas.json
│   ├── traj.json
│   └── <candidate_experiment>/
│       ├── code/
│       ├── experiment_report.txt
│       └── run_<n>/
│           ├── final_info.json
│           └── code/
└── paper_orchestra_runs/
    └── paper/
        ├── candidate_selection.json
        ├── vegapunk_runtime.json
        ├── raw_materials/
        │   ├── idea_sparse.md
        │   └── experimental_log.md
        ├── outline.json
        ├── latex_writeup/
        ├── content_refinement_workdir/
        │   ├── final_refined_paper.tex
        │   └── final_paper.zh-CN.tex
        ├── stdout.log
        ├── stderr.log
        ├── final_paper.pdf
        └── final_paper.zh-CN.pdf
```

其中：

- `final_info.json` 是实验指标的主要事实源。
- `candidate_selection.json` 保存论文候选选择及所有例外情况的来源和理由。
- `raw_materials/` 是从 Native Discovery Artifacts 确定性投影出的 Paper Input Bundle。
- `vegapunk_runtime.json` 是子进程使用的 Relay Provider、模型与别名配置；凭据仍只来自环境变量。
- `stdout.log` 与 `stderr.log` 保留完整上游进程诊断。
- `final_paper.pdf` 与 `content_refinement_workdir/final_refined_paper.tex` 是 PaperOrchestra 默认返回的英文权威版本。
- `final_paper.zh-CN.pdf` 与 `content_refinement_workdir/final_paper.zh-CN.tex` 是在英文版本完成后，通过同一 Relay Provider 自动翻译和 XeLaTeX 编译的中文伴随版本；公式、引用、参考文献、标识符、代码、数值、URL 与 raster 图片内容保持原样。

有关每类实验文件的可信层级和用途，请参阅 [实验产物说明](./docs/EXPERIMENT_ARTIFACTS.md)。

## 配置入口

| 路径 | 用途 |
| --- | --- |
| `config/model_catalog.yaml` | Relay、Qwen 与本地 embedding 的 Provider、模型、能力、重试和并发配置 |
| `config/default_config.yaml` | Agent、记忆、Discovery Loop 和实验执行配置 |
| `config/paper_orchestra.yaml` | Vendored 根目录、上游模板、绘图开关与绘图批评轮数 |
| `tasks/<task>/prompt.json` | 算法发现任务、数据、基线、指标与约束 |

## 项目结构

```text
Vegapunk/
├── launch.py                   # discovery / qa 统一入口
├── launch_discovery.py         # 多轮发现与实验主流程
├── launch_qa.py                # 独立 Deep Research 问答
├── launch_paper.py             # 为历史 Launch 触发同一个单-Paper服务
├── vegapunk/
│   ├── mas/                    # Agent、工作流、模型、工具与记忆
│   ├── paper_orchestra/        # 候选、原料、Runtime 与上游子进程适配
│   ├── stage.py                # Idea、实验与报告阶段适配
│   └── experiments_utils_*     # 各实验执行后端
├── third_party/paper_orchestra/ # 固定上游完整源码及最小兼容改动
├── tasks/                      # 算法发现任务
├── sci_tasks/                  # 科学论文复现任务子模块
├── config/                     # 系统配置
├── docs/                       # 架构、Spec、ADR 与专题说明
└── tests/paper_orchestra/      # 自动论文闭环测试
```

## 验证

运行 PaperOrchestra 单元与集成测试：

```bash
python -m unittest discover -s tests/paper_orchestra -v
```

检查 Python 语法与代码风格：

```bash
python -m compileall launch_discovery.py launch_paper.py vegapunk/paper_orchestra third_party/paper_orchestra
flake8 launch_discovery.py launch_paper.py vegapunk/paper_orchestra tests/paper_orchestra
```

## 延伸阅读

- [项目流程架构](./docs/architecture.md)
- [实验自然产物与可信层级](./docs/EXPERIMENT_ARTIFACTS.md)
- [PaperOrchestra 移植对等性审计](./docs/paper_orchestra_port_parity_audit.md)
- [架构决策记录 ADR](./docs/adr/)
- [记忆模块](./docs/memory_module.zh-CN.md)
- [Deep Research](./docs/deep_research.zh-CN.md)
- [统一模型运行时 PRD](./docs/prd/unified-model-runtime.md)
- [科学论文复现任务](./docs/sci_tasks.md)

## 上游项目

本仓库在以下项目基础上进行集成与适配：

- [InternAgent](https://github.com/InternScience/InternAgent)：自主科学发现、多智能体编排、实验执行、记忆与 Deep Research 核心。
- [PaperOrchestra](https://github.com/declare-lab/paper-orchestra)：提纲、写作、内容反思、论文审查和版式审查架构。

PaperOrchestra 的固定提交、完整导入策略与适配边界记录在 [移植对等性审计](./docs/paper_orchestra_port_parity_audit.md) 和 [ADR-0101](./docs/adr/0101-vendor-the-complete-upstream-paperorchestra-before-adapting-it.md) 中。
