# 任务：撰写初始 GEPA-style Research Orchestrator 设计文档

请撰写一个 Markdown 设计文档，用于指导我们实现第一个最小可行的 AI researcher 闭环 orchestrator。文档目标不是写代码，而是明确系统边界、模块职责、数据结构、循环流程和第一版实验范围。

## 背景与目标

我们希望构建一个受 GEPA 启发的 AI researcher 初始闭环。这里的 GEPA-style 不表示完整复现 GEPA 论文，而是采用其核心思想：

- 用执行轨迹和评测轨迹作为学习信号
- 让 proposer 基于上一轮失败和反馈提出候选改动
- 用 executor 在固定任务集上真实执行候选
- 用 judger 输出分数和可操作反馈
- 用 gater 决定保留、丢弃、迭代或停止
- 用有限预算和日志记录保证可复现，而不是无限自主循环

第一版目标是跑通一个小题目上的闭环，例如“优化论文问答 prompt”或“优化一个小型 agent workflow 的 prompt”。不要设计成宏大的全自动 AI Scientist。

## 设计原则

文档中必须强调：

1. 第一版不要使用 `while true`，使用有限轮数，例如 `for round_id in range(max_rounds)`。
2. 第一版不要拆成多个独立 agent，先用一个 orchestrator 调用不同 role prompt。
3. 第一版不要拆成多个 skill。先写普通工程设计，跑通后再沉淀为 skill。
4. executor 必须真实执行任务，并保存 trace。
5. judger 必须输出 score 和 actionable feedback，而不只是“看起来不错”。
6. gater 是状态管理和选择器，不是另一个普通 reviewer。
7. 所有候选、轨迹、评估和决策必须落盘。
8. 系统必须有预算、停止条件和失败处理。

## 文档结构

请按以下结构撰写：

### 1. 系统目标

说明这个 orchestrator 的目标：

- 实现 `proposer -> executor -> judger -> gater` 的最小闭环
- 用小规模任务验证 GEPA-style 自我改进是否有效
- 产出可审计的实验日志和最终报告
- 为后续扩展到多 agent / skill / tree search 打基础

同时说明非目标：

- 不做完整 AI Scientist
- 不做无限自主研究
- 不做模型权重训练
- 不做复杂多 agent 编排
- 不追求论文级科研发现

### 2. GEPA-style 架构解释

解释四个组件：

#### Proposer
职责：根据当前目标、历史候选、judger feedback、失败 trace，提出一个新的候选改动。

输出示例字段：
- candidate_id
- parent_id
- hypothesis
- proposed_change
- target_module
- rationale
- expected_improvement
- risk

#### Executor
职责：对候选进行真实执行。

第一版 executor 应该是固定、可控、低自由度的，例如：
- 写入候选 prompt/config
- 在固定数据集上运行
- 保存每个样本的输入、输出、错误、耗时
- 生成 trace 文件

#### Judger
职责：基于 executor 输出进行评估。

必须输出：
- numeric score
- pass/fail
- per-sample evaluation
- failure categories
- actionable feedback
- confidence

强调优先使用硬指标，其次使用 rubric，最后才使用 LLM 评审。

#### Gater
职责：根据 score、feedback、成本和历史状态做决策。

可能决策：
- keep
- reject
- iterate
- merge later
- stop

第一版只需要支持：
- keep_best
- reject_worse
- continue_until_budget
- stop_on_no_improvement

### 3. Orchestrator 主循环

给出伪代码，要求类似：

```python
state = load_or_initialize_state()

for round_id in range(config.max_rounds):
    candidate = proposer(state)
    trace = executor(candidate, config.task)
    judgment = judger(candidate, trace, config.rubric)
    decision = gater(state, candidate, judgment)

    persist(candidate, trace, judgment, decision)
    state = update_state(state, candidate, judgment, decision)

    if decision.stop:
        break

write_final_report(state)

```

