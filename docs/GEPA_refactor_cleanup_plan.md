# GEPA AI Researcher 框架精简与重构方案

## 1. 重构目标

当前 GEPA AI Researcher 经过多轮迭代后，已经同时保留了：

- 早期单 candidate 版本；
- 当前 GEPA 多 candidate loop；
- 交互式配置与 CLI；
- Git worktree 隔离执行；
- provenance 校验；
- usage tracking；
- 多套日志展示；
- 多轮开发中不断累积的内部测试；
- 多个兼容接口与旧 schema。

下一步重构目标不是继续增加抽象层，而是进行一次 **以删除为主的收缩式重构**：

> 在不破坏 GEPA loop、候选隔离执行、确定性 Gate、状态持久化和恢复功能的前提下，删除不再使用的内部测试、旧接口、兼容层、展示层和重复模块，使代码重新回到最小、清晰、可维护的核心框架。

重构应遵循以下原则：

1. **保留行为，不保留旧结构。**
2. **LLM 负责提出、执行与解释，确定性代码负责安全、合法性与生存选择。**
3. **删除未被主调用链使用的接口。**
4. **测试只锁定关键外部行为，不测试无价值的内部实现细节。**
5. **所有删除和拆分都必须分阶段完成，并通过行为回归测试。**

---

## 2. 必须保留的 GEPA 核心行为

无论代码怎样重构，以下行为必须保持不变。

### 2.1 初始化阶段

- 生成 seed candidate；
- seed 经过执行前 Admission；
- seed 在 `D_pareto` 上执行；
- 得到 Trace 和 Judgment；
- 初始化 Candidate Pool；
- 初始化 Score Matrix；
- 初始化 Pareto Frontier。

### 2.2 正式迭代阶段

每轮必须保持以下逻辑：

1. 从 active candidate 中建立 Pareto frontier；
2. 从 frontier 中选择 parent；
3. proposer 基于 parent、trace、历史反馈提出 batch proposals；
4. proposal 经过 Admission Gate；
5. parent 与 admitted children 在 `D_feedback` 上执行；
6. executor 产出实验 Trace；
7. provenance 检查执行是否合法；
8. judger 根据 Trace 生成 Judgment；
9. feedback gate 选择优于 parent 的 child；
10. improvers 在 `D_pareto` 上完整评估；
11. final gate 决定 accepted / discarded；
12. 更新 Candidate Pool、Score Matrix、Frontier 和下一轮反馈；
13. 更新 loop state 并判断是否停止。

### 2.3 执行隔离与可追溯性

必须保留：

- 每个 candidate 使用独立 Git worktree；
- child 从 parent 的 verified result SHA 开始；
- executor 禁止自行切换 branch；
- candidate 第一次执行允许修改；
- `materialize_once` 后的再次评估为 `evaluate_only`；
- worktree branch 必须正确；
- 修改文件必须在 admitted 范围内；
- commit 数量必须在预算内；
- controller repository 不得被 candidate 污染；
- provenance 失败必须 fail closed。

### 2.4 状态与恢复

必须保留：

- `state`；
- accepted / discarded candidate；
- Score Matrix；
- Pareto frontier；
- execution registry；
- candidate result SHA；
- trace / judgment artifacts；
- resume 后不能重复实现已经 materialize 的 candidate。

---

## 3. 当前代码的主要问题

## 3.1 多代实现并存

当前仓库中同时保留：

- 早期单 candidate Gate；
- 当前 GEPA Gate；
- 交互式 manager；
- 当前 orchestrator；
- 多种 agent 兼容接口；
- 多套日志与 display helper；
- 多套测试。

这些功能存在明显重叠。

## 3.2 Orchestrator 成为 God Object

当前 orchestrator 同时负责：

- 配置；
- 上下文；
- agent 创建；
- seed 初始化；
- proposal；
- Admission；
- workspace；
- executor 调度；
- provenance；
- judger；
- Gate；
- Pareto；
- pool；
- Score Matrix；
- state；
- usage；
- display；
- artifact；
- resume。

这导致：

- 文件过长；
- 依赖过多；
- 修改某一功能容易影响整个 loop；
- 测试不得不覆盖大量内部细节。

## 3.3 Schema 混合了多个时期的设计

Candidate、Decision、EvaluationResult、Judgment 等对象中存在：

- 重复字段；
- 旧版兼容字段；
- 运行状态与 proposal 内容混在一起；
- raw agent 输出直接塞入 candidate artifacts；
- `parent_id` 与 `parent_ids` 双重表示。

## 3.4 多套 Gate 共存

当前至少存在：

- Admission Gate；
- Provenance Gate；
- GEPA feedback/final Gate；
- 早期 `SimpleGater`。

其中早期 Gate 已不参与当前主 loop，但仍保留在代码和 schema 中。

## 3.5 测试代码过度增长

当前测试覆盖了：

- smoke；
- mini flow；
- upgrade；
- P0 safety；
- agent client；
- agent components；
- context views；
- display。

多套测试重复覆盖相同流程，同时又测试大量：

- formatter；
- prompt 细节；
- helper；
- dataclass；
- 内部实现。

测试代码逐渐接近“第二套框架”。

---

## 4. 可以直接删除的遗留部分

以下内容不属于当前 GEPA loop 核心，可作为第一批删除对象。

## 4.1 删除旧 `gater.py`

旧模块通常包含：

- `SimpleGater`；
- `DecisionKind`；
- `Decision`；
- best-so-far 单 candidate 判断。

当前主 loop 使用的是 GEPA feedback gate 与 final gate，因此旧 Gate 应删除。

同时清理：

- orchestrator 中旧 `Decision` import；
- schema 中只服务旧 Gate 的字段；
- 相关旧测试。

---

## 4.2 删除 `manager.py`

`ResearchManager` 主要服务：

- 交互式问答创建配置；
- `init_config()`；
- `run_config()`；
- 早期 CLI 包装。

当前实际工作流是直接提供完整 JSON config 并启动 orchestrator，因此 manager 不属于科研 loop 核心。

建议直接删除。

---

## 4.3 删除 CLI 中的 `init` 和 `chat`

CLI 最终只保留：

```bash
gepa-run --config config.json
```

或：

```bash
python -m gepa_researcher.cli --config config.json
```

删除：

- `init`；
- `chat`；
- 旧版别名；
- 交互式配置创建。

CLI 应缩减为一个简单的 run-only 入口。

---

## 4.4 删除旧 Schema

优先检查并删除：

- `DecisionKind`；
- `Decision`；
- `EvaluationResult`；
- 只服务旧版本的兼容字段。

删除前使用全仓库搜索确认：

```bash
rg "DecisionKind|Decision\b|EvaluationResult" .
```

---

## 4.5 删除旧文档和仓库内测试日志

建议删除或归档：

- 旧版 orchestrator 设计文档；
- 仓库内 `test.log`；
- 已完成的临时实施计划；
- 早期 superpowers plan；
- 不再使用的根目录测试配置。

当前架构文档只保留一份。

---

## 5. 应简化而不是直接删除的模块

## 5.1 Display / 日志展示

当前大量 formatter 只用于：

- 标题；
- Proposal 展示；
- Trace 展示；
- Judgment 展示；
- Gate 总结；
- usage 总结。

建议将 display 系统替换为一个最小事件 logger：

```python
logger.event(
    "candidate_judged",
    candidate_id=candidate_id,
    score=score,
    passed=passed,
)
```

控制台只显示关键单行事件，完整数据写入 JSON artifact。

可删除：

- 大量 format helper；
- display 专用测试；
- 空格、换行、标题格式测试。

---

## 5.2 Usage tracking

Usage tracking 可以保留，但应变成可选旁路。

建议只保留：

```python
UsageRecorder.record(call_context, result)
UsageRecorder.summary()
```

字段控制在：

- role；
- candidate ID；
- round；
- duration；
- input token；
- output token；
- cache token；
- cost；
- error。

不要让 usage tracking 深度参与 orchestrator 主逻辑。

---

## 5.3 Context 模块

将 `context.py` 与 `context_views.py` 收缩成四个明确入口：

```python
load_prior_context()
build_proposer_context()
build_executor_context()
build_judger_context()
```

角色上下文应严格隔离：

### Proposer 看到

- 当前任务目标；
- 当前代码事实；
- frontier；
- parent 摘要；
- 机制级历史；
- retired hypotheses；
- 最近一轮 canonical feedback。

### Executor 看到

- candidate contract；
- target files；
- workspace；
- execution mode；
- commands / success criteria；
- artifact 输出要求。

### Judger 看到

- hypothesis；
- patch 摘要；
- metrics；
- validation；
- provenance；
- artifacts；
- errors。

禁止：

- proposer 看到 `agent_raw`；
- executor 看到完整 Pareto 历史；
- judger 看到无关 proposal 建议；
- raw prompt 和 raw agent output 回流到下一轮。

---

## 5.4 Pool、Registry 与持久化

当前状态写入分散在：

- Candidate Pool；
- Execution Registry；
- orchestrator；
- io utilities；
- live artifact 方法。

建议整合成：

```python
class RunStore:
    load_state()
    save_state()

    load_pool()
    save_pool()

    load_score_matrix()
    save_score_matrix()

    save_trace()
    save_judgment()
    save_generation()

    register_execution()
    get_execution()
```

Loop 不再关心 JSON 文件路径和命名。

---

## 6. 推荐的新架构

不要把当前 orchestrator 再拆成大量小工具类，而是收敛为三个主要组件。

## 6.1 LoopEngine

只负责 GEPA 算法与状态转换：

```python
initialize()
plan_generation()
select_parents()
propose_children()
apply_feedback_gate()
apply_pareto_gate()
update_frontier()
should_stop()
```

它不负责：

- Git；
- subprocess；
- Claude；
- 文件路径；
- JSON 写入。

---

## 6.2 EvaluationService

负责完整 candidate 评估链：

```text
workspace preparation
→ executor
→ provenance
→ judger
```

对外只暴露：

```python
evaluate(
    candidates,
    phase,
    sample_ids,
) -> EvaluationBatch
```

---

## 6.3 RunStore

负责：

- state；
- pool；
- score matrix；
- traces；
- judgments；
- execution registry；
- resume；
- artifacts。

---

## 6.4 主 loop 的目标形式

重构后主循环应接近：

```python
state = store.load_or_create_state()

if not state.initialized:
    seeds = agents.propose_seeds()
    results = evaluator.evaluate(
        seeds,
        phase="pareto",
        sample_ids=dataset.pareto_ids,
    )
    state = loop.initialize(state, seeds, results)
    store.save_all(state)

while not state.stop:
    generation = loop.plan_generation(state)

    results = evaluator.evaluate_generation(generation)

    state = loop.apply_generation_results(
        state,
        generation,
        results,
    )

    store.save_all(state)
```

主 loop 应当可以一眼读懂。

---

## 7. 推荐的最终目录结构

目标可以收缩到：

```text
gepa_researcher/
├── __init__.py
├── cli.py
├── config.py
├── models.py
├── client.py
├── agents.py
├── context.py
├── loop.py
├── evaluation.py
├── policy.py
├── workspace.py
└── store.py
```

## 模块职责

| 模块 | 职责 |
|---|---|
| `cli.py` | 唯一 run 入口 |
| `config.py` | typed config、dataset split |
| `models.py` | 精简后的核心数据模型 |
| `client.py` | Claude/subprocess/JSON envelope |
| `agents.py` | proposer、executor、judger |
| `context.py` | 三种角色上下文 |
| `loop.py` | GEPA 状态机 |
| `evaluation.py` | candidate 并发评估 |
| `policy.py` | admission、gate、pareto、score |
| `workspace.py` | worktree 与 provenance |
| `store.py` | pool、registry、state、artifact |

---

## 8. Schema 精简建议

## 8.1 Candidate 只保存 Proposal

建议：

```python
@dataclass
class Candidate:
    id: str
    round_id: int
    generation: int
    parent_ids: list[str]

    hypothesis: str
    proposed_change: str
    rationale: str
    expected_improvement: str
    risk: str

    target_files: list[str]
    strategy: str
    executor_contract: dict
```

删除或移出 Candidate：

- status；
- admission status；
- branch；
- worktree；
- result SHA；
- raw output；
- execution mode；
- provenance；
- score。

这些应进入：

```python
CandidateRecord
```

或 `RunStore`。

---

## 8.2 Raw Agent Output 不进入 Candidate

原始 agent response 应单独保存：

```text
runs/<run_id>/raw_calls/<call_id>.json
```

Candidate 只保存：

```python
raw_call_id: str | None
```

禁止将：

```python
artifacts["agent_raw"]
```

放回下一轮上下文。

---

## 8.3 删除重复 Parent 字段

统一使用：

```python
parent_ids: list[str]
```

删除：

```python
parent_id
```

不再通过 `__post_init__()` 双向同步。

---

## 9. 测试收缩方案

测试不能全部删除，但应从内部实现测试收缩成关键行为测试。

最终建议只保留五组。

## 9.1 `test_loop_contract.py`

使用 Fake proposer / executor / judger 运行：

```text
seed
→ initialization
→ proposal batch
→ D_feedback
→ improver
→ D_pareto
→ pool / matrix / frontier
```

断言：

- agent 调用顺序；
- candidate IDs；
- accepted / discarded；
- Score Matrix；
- frontier；
- next feedback；
- stop decision；
- state 持久化。

它替代：

- smoke；
- mini flow；
- upgrade 中的大部分重复测试。

---

## 9.2 `test_policy.py`

测试：

- frozen path 被拒绝；
- parent 未接受被拒绝；
- duplicate fingerprint 被拒绝；
- child 未提升被拒绝；
- parent judgment 缺失时 fail closed；
- task-best candidate 被接受；
- provenance / correctness 失败不能进入 selection。

---

## 9.3 `test_workspace.py`

使用临时 Git 仓库测试：

- worktree 隔离；
- parent SHA；
- branch；
- commit budget；
- 修改路径；
- evaluate-only；
- controller repo 不变。

这是必须保留的安全测试。

---

## 9.4 `test_agent_protocol.py`

只测试：

- 正常 JSON envelope；
- 非法 JSON；
- timeout；
- subprocess 非零退出；
- proposer schema；
- executor schema；
- judger schema。

不要测试完整 prompt 的每句话。

---

## 9.5 `test_store_resume.py`

测试：

- state / pool / matrix / registry 写入；
- resume 恢复；
- materialized candidate 不重复实现；
- 损坏 artifact 明确失败。

---

## 9.6 建议删除或合并的测试

| 当前测试 | 处理 |
|---|---|
| `test_display.py` | 整体删除 |
| `test_smoke.py` | 合入 loop contract |
| `test_gepa_mini_flow.py` | 合入 loop contract |
| `test_gepa_upgrade.py` | 合入 loop / policy |
| `test_context_views.py` | 只保留 1–2 个泄漏测试 |
| `test_agent_components.py` | 大幅缩减 |
| `test_p0_safety.py` | 合入 policy / workspace |

目标：

```text
5 个测试文件
20–30 个关键测试
```

测试只保证外部行为，不锁死内部实现。

---

## 10. 接口统一建议

## 10.1 统一 Agent Protocol

只保留：

```python
Proposer.propose_batch(...)
Executor.execute(...)
Judger.judge(...)
```

删除：

- `propose()` fallback；
- `hasattr()` 兼容判断；
- `inspect.signature()` 判断不同版本 client；
- 多套参数形式；
- 旧组件兼容层。

这是内部框架，不需要长期兼容旧 API。

---

## 10.2 删除无意义的 Component Mode

如果当前生产环境只支持默认 Claude agents，则配置中不需要：

```yaml
components:
  mode: claude_code_agents
```

直接：

```python
ResearchOrchestrator(
    config,
    agents: AgentBundle | None = None,
)
```

未注入就创建默认 agent。

---

## 11. 分阶段实施计划

## PR 1：建立行为基线

新增唯一 golden loop test：

```text
seed
→ feedback
→ pareto
→ pool
→ matrix
→ frontier
```

记录当前 deterministic 输出。

此阶段不删除旧测试。

---

## PR 2：删除明确遗留代码

删除：

- `gater.py`；
- `manager.py`；
- CLI `init/chat`；
- `DecisionKind`；
- `Decision`；
- `EvaluationResult`；
- `test_display.py`；
- `docs/test.log`；
- 旧文档；
- 旧 import。

这一批风险最低。

---

## PR 3：收缩接口与 Schema

- 统一 Agent Protocol；
- 删除旧兼容分支；
- Candidate 只保留 proposal；
- raw output 改为 artifact reference；
- 删除 `parent_id`；
- 引入 typed config。

---

## PR 4：拆分 Orchestrator

提取：

- `LoopEngine`；
- `EvaluationService`；
- `RunStore`。

这一阶段只移动职责，不修改 Gate 算法。

---

## PR 5：合并小模块

合并：

```text
context + context_views
pool + registry + persistence
gate + pareto + score_matrix
workspace + provenance facade
```

避免过多只有几十行的小模块。

---

## PR 6：收缩测试

使用五组行为测试替代当前多套重叠测试。

删除：

- formatter 测试；
- helper 测试；
- prompt 文本细节测试；
- 旧升级路径测试；
- 已失效接口测试。

---

## 12. 每个 PR 的验收条件

每一步都必须保证：

```text
相同 fake 输入
→ 相同 candidate IDs
→ 相同 agent 调用顺序
→ 相同 accepted / discarded
→ 相同 Score Matrix
→ 相同 Pareto frontier
→ 相同 stop decision
```

同时必须验证：

- controller HEAD 不变；
- candidate branch 正确；
- child 从正确 parent SHA 开始；
- changed files 合法；
- commit budget 合法；
- `evaluate_only` 不产生 commit；
- resume 不重复 materialize candidate；
- provenance 失败 fail closed。

以下内容不要求兼容：

- 控制台文字；
- 标题格式；
- JSON 字段顺序；
- 内部类名；
- helper 名称；
- 模块路径；
- 旧 CLI；
- 旧 prompt 文案。

---

## 13. 第一批推荐直接删除清单

建议第一批先完成：

```text
删除 gepa_researcher/gater.py
删除 gepa_researcher/manager.py
将 cli.py 缩减为 run-only
删除 DecisionKind
删除 Decision
删除 EvaluationResult
删除 tests/test_display.py
删除 docs/test.log
删除或归档旧 orchestrator 文档
清理 orchestrator 中旧 Decision import
清理未使用的 display / legacy helper
更新 README，删除 init/chat 说明
```

这一批不会触及：

- 当前 GEPA selection；
- candidate worktree；
- provenance；
- materialize-once；
- Score Matrix；
- Pareto frontier；
- resume。

因此适合作为重构第一阶段。

---

## 14. 最终目标

重构后的 GEPA AI Researcher 应当具备以下特征：

- 主 loop 可以在一页代码内读懂；
- proposal、evaluation、policy、storage 职责明确；
- executor 不管理 Git 生命周期；
- LLM 不决定硬安全和 provenance；
- Candidate 只表达研究提案；
- raw output 不进入下一轮上下文；
- 只有一套 Gate；
- 只有一套 Agent Protocol；
- 只有一个 run CLI；
- 测试只锁定核心行为；
- 删除所有展示层和早期版本负担；
- 能安全 resume；
- 能在后续继续扩展，而不会再次堆成 God Object。

核心思想可以概括为：

> **保留 GEPA 的科研闭环与实验可信度，删除所有不直接服务该闭环的历史包袱。**
