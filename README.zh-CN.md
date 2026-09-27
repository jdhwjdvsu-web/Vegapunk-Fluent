# Vegapunk：面向长程自主科学发现的统一智能体框架
> *从科研问题、假设生成和实验验证，到可审查论文的端到端闭环系统*

Vegapunk 是一个面向长程自主科学发现的多智能体框架。
系统能够围绕一个研究任务完成背景调研、假设生成、反思与演化、方法设计、实验执行、结果评估和跨轮次记忆。

论文与历史来源：
[InternAgent 1.0](https://arxiv.org/abs/2505.16938)、
[InternAgent 1.5](https://huggingface.co/papers/2602.08990) 和
[ResearchClawBench](https://github.com/InternScience/ResearchClawBench)。

---

## 🚀 快速开始

### 安装

```bash
conda create -n Vegapunk python=3.11
conda activate Vegapunk
pip install -r requirements.txt
```

### 配置 API 密钥

在仓库根目录创建 `.env`，并填入你的 API 密钥。

`.env` 中的关键字段：

```
OPENAI_API_KEY=        # OpenAI Responses API 密钥
DASHSCOPE_API_KEY=     # 千问 DashScope API 密钥
ANTHROPIC_API_KEY=     # 若统一模型运行时选择 Anthropic Provider，则填写
```

所有模型 Provider、Canonical Model Identity 与能力绑定统一配置于
`config/model_catalog.yaml`。
切换 Provider 时只修改该 Catalog 的固定绑定，并设置所选 Provider 的密钥。

默认 Catalog 使用 `qwen/qwen3.7-max` 作为 Active Text Model，`qwen/qwen3.6-plus` 作为 vision binding，`qwen/qwen-image-2.0-pro` 作为 image-generation binding，并使用本地 `BAAI/bge-base-en-v1.5` embedding。
所有文本和视觉调用都使用声明的 Responses protocol，不会静默降级为 Chat Completions。
Runtime 统一负责 Provider 并发与有界重试，且不会使用 provider-side background execution。

### 运行发现实验

`AutoDebug` 是一个自包含的玩具任务（不需要下载数据集或模型），推荐作为首次运行任务，用于验证你的配置是否正确。

```bash
python launch_discovery.py \
    --config ./config/default_config.yaml \
    --task AutoDebug \
    --exp_backend codex
```

实验模式完成全部 Discovery 工作后，会自动发生一次 Paper Handoff，并在
`paper_orchestra_runs/paper/` 中最多生成一篇 Paper。仓库完整 vendoring 了固定提交
`ca1b3fa01c2970fc7cda32d16245db38d57b3f56` 的上游 PaperOrchestra，保留原始
Agent、提示词、同步 pipeline、文献流程和自主绘图，只在宿主输入输出与模型调用边界做适配。

首个可运行基线只把系统自然产物投影为 `idea_sparse.md` 与
`experimental_log.md`：不读取源码、代码差异或 `code_summary.json`。
`report/report.md` 存在时会进入实验记录，缺失时不会阻塞实验、候选选择或论文生成。

为已有 Discovery Launch 单独触发同一个单-Paper服务：

```bash
python launch_paper.py \
    --launch-dir results/<task>/<timestamp>_launch \
    --config ./config/default_config.yaml
```

成功结果会直接复用；该基线不提供逐 stage 持久恢复，也不会为同一 Launch 创建多个论文版本。

### 运行 QA 查询

QA 模式使用 Vegapunk 的深度研究流水线直接回答研究问题，不运行实验循环，而是基于文献综合生成答案。

```bash
python launch_qa.py --question "What are recent advances in memory-augmented LLMs?"

# 可选：将答案保存到文件
python launch_qa.py -q "What are recent advances in memory-augmented LLMs?" -o answer.md
```

### Fluent 实验规划 Agent V3

`integrations/fluent` 提供基于 LangGraph 的有状态 Fluent 实验规划入口：自然语言
任务会形成严格的 `SimulationTaskObject`，并在 Case 扫描目录内完成 DIRECT、
MAPPED_PROXY、GEOMETRY_UNSUPPORTED 三分类、受限 Mapping DSL、安全校验和人工审批。
执行阶段继续复用既有 Optuna、Fluent Runner、PyFluent-MCP、Gate 与 Job Service。
几何代理结果当前只生成建议，不会自动调用 Workbench。完整说明见
[Fluent Agent V3 架构与使用](docs/Fluent_Agent_V3_架构与使用.md)。

### 统一启动器

`launch.py` 是两种模式的统一入口：

```bash
python launch.py --mode discovery --task AutoDebug --exp_backend codex
python launch.py --mode qa --question "What are recent advances in memory-augmented LLMs?"
```

**配置提示：**
- 配置文件位于 `config/` 中，`default_config.yaml` 是主要起点。
- 结果保存在 `results/` 下，日志保存在 `logs/` 下。
- 如需跳过想法生成，并基于已有想法运行实验：添加 `--skip_idea_generation --idea_path <path/to/ideas.json>`。
- `scripts/` 中提供了可直接使用的示例脚本。

### 研究任务

**算法发现任务**位于 `tasks/` 下。每个任务都包含 `prompt.json`（任务描述）、基线 `code/` 和 `launcher.sh`。不同任务所需的数据集和环境相关设置各不相同，请参考各任务目录中的代码。

**科学论文复现任务**（`sci_tasks`）是一种独立模式：Vegapunk 会获得一篇已发表论文及其数据，并被要求自主复现关键发现。
这些任务位于 `sci_tasks/tasks/` 下，来自 [ResearchClawBench](https://github.com/InternScience/ResearchClawBench) 基准。
完整指南见 [docs/sci_tasks.md](docs/sci_tasks.md)。

### 快速启动脚本

`scripts/` 下提供了可直接使用的示例脚本：

| 脚本 | 说明 |
|---|---|
| `run_discovery.sh` | 运行完整发现实验（想法生成 -> 实验） |
| `run_skip-idea.sh` | 基于已有想法文件运行实验，并跳过生成阶段 |
| `run_sci.sh` | 运行科学论文复现任务（默认使用 `Astronomy_000`） |
| `run_qa.sh` | 通过深度研究回答研究问题 |

---

## 🔧 高级功能

### 记忆模块

Vegapunk 包含一个持久化记忆模块，可跨会话记录实验结果，帮助智能体避开此前失败的方向，并在成功经验基础上继续推进。
配置和设置见 [docs/memory_module.md](docs/memory_module.md)。

### 深度研究

深度研究（DR）模块会将研究问题分解为子任务，并行地从学术数据库和网络中收集信息，再将发现综合为直接答案或结构化报告。配置和设置见 [docs/deep_research.md](docs/deep_research.md)。

---

## 📝 引用

```bibtex
@article{feng2026internagent,
  title={InternAgent-1.5: A Unified Agentic Framework for Long-Horizon Autonomous Scientific Discovery},
  author={Shiyang Feng and Runmin Ma and Xiangchao Yan and Yue Fan and Yusong Hu and Songtao Huang and Shuaiyu Zhang and Zongsheng Cao and Tianshuo Peng and Jiakang Yuan and Zijie Guo and Zhijie Zhong and Shangheng Du and Weida Wang and Jinxin Shi and Yuhao Zhou and Xiaohan He and Zhiyin Yu and Fangchen Yu and Bihao Zhan and Qihao Zheng and Jiamin Wu and Mianxin Liu and Chi Zhang and Shaowei Hou and Shuya Li and Yankai Jiang and Wenjie Lou and Lilong Wang and Zifu Wang and Jiong Wang and Wanghan Xu and Yue Deng and Dongrui Liu and Yiheng Wang and Wenlong Zhang and Fenghua Ling and Shufei Zhang and Xiaosong Wang and Shuangjia Zheng and Xun Huang and Siqi Sun and Shuyue Hu and Peng Ye and Chunfeng Song and Bin Wang and Conghui He and Yihao Liu and Xin Li and Qibin Hou and Tao Chen and Xiangyu Yue and Bin Wang and Liang He and Dahua Lin and Bowen Zhou and Bo Zhang and Lei Bai},
  journal={arXiv preprint arXiv:2602.08990},
  year={2026}
}
```

```bibtex
@article{team2025internagent,
  title={InternAgent: When Agent Becomes the Scientist--Building Closed-Loop System from Hypothesis to Verification},
  author={Team, InternAgent and Zhang, Bo and Feng, Shiyang and Yan, Xiangchao and Yuan, Jiakang and Ma, Runmin and Hu, Yusong and Yu, Zhiyin and He, Xiaohan and Huang, Songtao and others},
  journal={arXiv e-prints},
  pages={arXiv--2505},
  year={2025}
}
```

```bibtex
@article{hu2025flowsearch,
  title={FlowSearch: Advancing deep research with dynamic structured knowledge flow},
  author={Yusong Hu and Runmin Ma and Yue Fan and Jinxin Shi and Zongsheng Cao and Yuhao Zhou and Jiakang Yuan and Xiangchao Yan and Wenlong Zhang and Lei Bai and Bo Zhang},
  journal={arXiv preprint arXiv:2510.08521},
  year={2025}
}
```

```bibtex
@article{du2025automlgen,
  title={AutoMLGen: Navigating Fine-Grained Optimization for Coding Agents},
  author={Shangheng Du and Xiangchao Yan and Dengyang Jiang and Jiakang Yuan and Yusong Hu and Xin Li and Liang He and Bo Zhang and Lei Bai},
  journal={arXiv preprint arXiv:2510.08521},
  year={2025}
}
```
