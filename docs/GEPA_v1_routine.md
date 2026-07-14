# GEPA v1.x 到 v2.0 演进路线参考

## 0. 文档目的

本文档用于指导 GEPA 从当前 v1.0 基础 loop，经由 v1.0.5 Candidate Execution Kernel、v1.1 Reliable Loop Kernel 与 v1.2 Global Context Plane，演进到 v2.0 agentic architecture。

路线基于以下已确定判断：

- 当前 GEPA v1.0 已经具备基础 evolutionary loop，但 Proposer、Executor、Judger 之间的信息交付不稳定。
- 当前 Candidate、Git revision、Execution、workspace/worktree 与 Sandbox 的生命周期混杂，持久工作区、共享 Git 控制面和复用 artifacts 带来了隔离、恢复和归因问题。
- 当前 Proposer 实际混合承担了 Optimizer 和 Planner 的职责，导致 proposal 方向与执行计划容易混杂。
- 当前 Executor 更接近 Runner，缺少内部规划和执行反馈循环。
- 当前 Judger 评分偏松，容易出现分数饱和，使 Proposer 无法获得足够清晰的优化信号。
- 新架构确定为：

```text
Optimizer
    ↓
Executor(Planner → Runner → Critic)
    ↓
Judge
```

其中五个逻辑角色为：

1. Optimizer
2. Planner
3. Runner
4. Critic
5. Judge

但顶层子系统仍保持三个：

```text
Optimizer / Executor / Judge
```

---

## 1. 版本总览

| 版本 | 名称 | 核心目标 | 主要价值 |
|---|---|---|---|
| v1.0 | Current GEPA | 当前基础 evolutionary loop | 已可运行、可作为重构基线 |
| v1.0.5 | Candidate Execution Kernel | 拆分 Candidate、Revision、Execution、Artifact 与 Sandbox | 简化任务调度与隔离，使父子继承、执行复现和结果归因有清晰边界 |
| v1.1 | Reliable Loop Kernel | 稳定协议、状态机、错误恢复、模块边界 | 让信息可靠流动，避免 loop 因 agent 输出异常崩溃 |
| v1.2 | Global Context Plane | 全局信息池、文件缓存、角色视图、Prompt 组装 | 统一管理 agent 输入、运行日志、中间信息和用户输出 |
| v2.0 | Stateful Agentic GEPA | Optimizer + Executor(Planner → Runner → Critic) + Judge | 实现职责分离、内部执行循环、相对评价和可归因优化 |

四个阶段的关系：

```text
v1.0.5: Candidate、Revision、Execution 与 Sandbox 的边界是否正确
        ↓
v1.1: 信息能否可靠流动
        ↓
v1.2: 每个角色能否获得正确的信息
        ↓
v2.0: 每个角色能否基于这些信息正确工作
```

---

## 2. 总体架构原则

### 2.1 LLM 输出不能直接控制 Loop

核心原则：

> LLM 输出不能直接驱动状态转移。只有经过验证的结构化对象才能进入 loop。

错误方式：

```text
LLM raw text
    ↓
字符串匹配
    ↓
直接推进 loop
```

正确方式：

```text
LLM raw response
    ↓
Agent Gateway
    ↓
Syntax validation
    ↓
Schema validation
    ↓
Semantic validation
    ↓
Validated Domain Object
    ↓
State Machine transition
```

JSON 只是传输格式，不是系统接口本身。真正的接口应该是经过验证的领域对象，例如：

- `ProposalIdea`
- `ExecutionPlan`
- `ExecutionAttempt`
- `CritiqueDecision`
- `ExecutionSubmission`
- `JudgmentReport`
- `GateDecision`
- `TypedFailure`

### 2.2 模块内部可以软，模块边界必须硬

必须硬的内容：

- 消息身份
- schema version
- run / round / candidate / attempt id
- 状态转换
- 权限和预算
- artifact identity
- 错误语义
- 可恢复状态

可以软的内容：

- optimization hypothesis
- rationale
- diagnostic analysis
- implementation explanation
- qualitative feedback
- judge textual comments

原则：

> 硬容器装软内容。

也就是说，自然语言分析可以存在，但必须被放在明确 schema 字段内，而不是散落在不可解析文本中。

### 2.3 职责分离优先于堆叠 Agent

不应为了增加智能而增加角色。每个角色必须回答一个不同的问题：

| 问题 | 角色 |
|---|---|
| 往哪里优化？ | Optimizer |
| 抽象想法如何落地？ | Planner |
| 如何实际修改和运行？ | Runner |
| 当前执行是否足以提交？ | Critic |
| 最终是否真的更好？ | Judge |

### 2.4 执行反馈和价值评价必须分离

Critic 只回答：

> 当前实现是否正确、完整、可验证，是否应该继续、重规划、提交或中止？

Judge 只回答：

> 相比 parent 或 baseline，这次交付是否带来了真实价值，提升多少，风险如何？

Critic 不应给 proposal 最终价值评分。

Judge 不应参与执行调试。

### 2.5 全局事实唯一，角色视图不同

不为每个角色维护独立 memory。所有角色共享同一个事实底座，但通过不同 `ContextView` 获取不同信息。

```text
Global Context Plane
    ├── Optimizer View
    ├── Planner View
    ├── Runner View
    ├── Critic View
    ├── Judge View
    └── User View
```

---

## 3. v1.0: Current GEPA

### 3.1 当前定位

v1.0 是已经搭好的基础 GEPA loop，主要价值是提供可运行基线。

当前 loop 大致为：

```text
Pareto Parent Selection
    ↓
Reflective Mutation / Proposal Generation
    ↓
Admission Gate
    ↓
Execution / Judge
    ↓
Parent Improvement Gate
    ↓
Pareto Update
```

### 3.2 当前主要问题

1. Proposer、Executor、Judger 之间的交付不稳定。
2. agent 输出非 JSON 或 schema 缺字段时容易导致 loop 崩溃。
3. Proposer 同时承担方向选择和执行计划职责，proposal 容易过细或过虚。
4. Executor 更像 Runner，缺少 Planner 和 Critic。
5. Judger 评分偏松，基本满足要求就容易高分，导致优化信号饱和。
6. 信息上下文拼接碎片化，不利于后续角色分离。

### 3.3 v1.0 发布边界

v1.0 不追求架构完整性，只作为当前可运行版本发布。

v1.0 后续不再继续堆叠复杂能力，而是进入 v1.1 的可靠性重构。

---

## 4. v1.0.5: Candidate Execution Kernel

### 4.1 版本定位

v1.0.5 是正式进入 v1.1 前的执行地基层重构，不增加新的 agent 智能，也不改变当前 evolutionary selection、gate 和 Judge 的核心行为。

它只解决一个核心问题：

> 将 Candidate、Git Revision、Execution、Artifact 与 Sandbox 严格拆分，取消 Candidate 对长期 workspace/worktree 的所有权，使任务调度、父子继承、隔离、复现和清理具有简单且一致的语义。

该阶段完成前，不建议直接建立完整 Candidate 状态机、Event Store 和 Global Context Plane，否则可能把当前错误的 `candidate → workspace → execution` 绑定关系固化到后续架构中。

### 4.2 核心判断

当前复杂化的根源不是 Apptainer 本身，而是多个生命周期对象被混成了同一个对象：

```text
Candidate
≈ Git branch
≈ persistent worktree
≈ current execution
≈ candidate artifacts
≈ evaluation environment
```

v1.0.5 必须改为：

```text
Candidate Card
    ├── 持久化优化意图、父子血缘和最终结果引用
    ├── base_revision
    ├── result_revision
    └── execution_ids / judgment_ids

Execution
    ├── 描述一次明确的实现、反馈评估或 Pareto 评估
    ├── 输入 revision
    ├── phase / budget / permissions
    └── 产生 ExecutionRecord 与 ArtifactRefs

Sandbox
    ├── 只服务于一次 Execution
    ├── 从指定 revision 创建干净代码视图
    └── 执行结束后销毁
```

核心原则：

> 持久化 Candidate、Revision、ExecutionResult 和 Artifact；workspace、checkout、container、scratch 和 process 都是可重建的临时资源。

### 4.3 范围边界

v1.0.5 必须做：

- 定义最小 `CandidateCard`
- 定义不可变 `RevisionRef`
- 定义 `ExecutionSpec` 与 `ExecutionRecord`
- 定义 `ArtifactRef` 与 typed execution failure
- 建立一次 Execution 一个独立临时 Sandbox 的模型
- 父子代通过 `parent.result_revision → child.base_revision` 继承
- implementation、feedback、pareto 分别使用独立执行目录和 artifacts
- 宿主 Harness 统一检查 diff、权限和创建 commit
- 移除 Candidate 对 workspace path、worktree branch 和 container path 的持久引用
- Registry 按 execution ID 追加记录，不按 candidate ID 覆盖
- 建立显式 create / collect / close / cleanup 生命周期
- 保持当前 Proposer、Executor、Judger 行为作为回归基线

v1.0.5 不做：

- 不引入 Planner
- 不引入 Critic
- 不实现高级 Optimizer
- 不建立完整 Global Context Plane
- 不实现复杂 Event Sourcing
- 不实现分布式 Scheduler
- 不优先优化 clone 或 Sandbox 启动性能
- 不保留跨 phase 的可变 workspace

一句话原则：

> 先把一次候选实验做成可独立重放的任务，再讨论更聪明的 agent loop。

### 4.4 Candidate Card

Candidate Card 是候选方案的持久业务档案，不是运行中 workspace 的容器。

建议最小结构：

```yaml
candidate:
  candidate_id:
  round_id:
  status:
  created_at:

lineage:
  parent_candidate_ids: []
  base_revision:
  result_revision:

proposal:
  proposal_id:
  hypothesis:
  rationale:
  expected_effect:
  scope:
  preserve:
  risks:

references:
  execution_ids: []
  judgment_ids: []
  artifact_ids: []

final:
  decision:
  score_summary:
```

Candidate Card 不得保存：

- host workspace path
- guest container path
- worktree branch
- process PID
- scratch path
- container HOME
- 当前 stdout / stderr

这些信息属于单次 Execution 或 SandboxSession。

### 4.5 Revision 与父子继承

父子迭代不继承父代 Sandbox，而继承父代确定的代码版本。

标准关系：

```text
parent.result_revision
        ↓
child.base_revision
        ↓
new ephemeral sandbox
        ↓
child implementation
        ↓
child.result_revision
```

因此：

- 销毁的是父代运行环境，不是父代代码成果。
- 子代从父代 commit 的完整代码状态开始，不从原始 baseline 重来。
- 子代继承父代 Proposal、Judge feedback、metrics 和显式 Artifact references。
- 子代不继承父代未提交修改、后台进程、临时 HOME、缓存和脏文件。
- 同一父代可以安全创建多个彼此隔离的子代 Sandbox。

Candidate 血缘与 Revision 血缘必须分别记录：

```yaml
parent_candidate_ids:
  - cand_parent
base_revision: <parent result sha>
result_revision: <child result sha>
```

### 4.6 Execution 模型

一次 Candidate 可以产生多个 Execution：

```text
Candidate
├── implementation execution
├── feedback evaluation execution
├── pareto evaluation execution
├── robustness evaluation execution
└── repair execution（若未来需要）
```

建议结构：

```yaml
execution:
  execution_id:
  candidate_id:
  phase:
  input_revision:
  writable:
  dataset_ref:
  evaluator_version:
  environment_hash:
  budget:
  capability_policy:
  status:
  result_revision:
  artifact_refs: []
  failure:
```

所有 execution 按 `execution_id` 追加保存。不得再使用：

```text
latest_execution_by_candidate[candidate_id] = execution_id
```

覆盖历史。

### 4.7 Ephemeral Sandbox

每次 Execution 创建一个逻辑上全新的环境：

```text
executions/<execution_id>/
├── manifest.json
├── artifacts/
├── stdout.log
└── stderr.log

tmp/<execution_id>/
├── repo/
├── scratch/
└── home/
```

执行结束后：

- `executions/<execution_id>/` 中的记录和 artifacts 保留
- `tmp/<execution_id>/` 中的 repo、scratch、home 和容器状态删除

实现阶段的 repo 可写；评估阶段的 repo 默认只读。各 phase 不共享可变 artifacts 目录。

第一版优先采用独立临时 clone 或等价的独立代码树，不继续依赖长期 worktree。稳定后再考虑 local mirror、reflink、overlay 或 warm pool 等性能优化。

### 4.8 宿主 Harness 接管 Git

Agent 负责：

- 阅读和修改代码
- 运行测试与 benchmark
- 产生执行说明和证据

宿主 Harness 负责：

- 校验起始 revision
- 检查 changed files
- 检查 forbidden paths 和 scope
- 保存 Git diff
- 执行必要的 deterministic validation
- 创建 commit
- 记录 result revision
- 清理 Sandbox

容器不得再获得 controller repository 的共享 Git common directory 写权限。

### 4.9 状态边界

Candidate 状态只描述业务生命周期：

```text
GENERATED
    ↓
ADMITTED
    ↓
MATERIALIZED
    ↓
EVALUATED
    ↓
ACCEPTED / REJECTED
```

Execution 状态描述一次任务：

```text
PENDING
    ↓
PREPARING
    ↓
RUNNING
    ↓
COLLECTING
    ↓
SUCCEEDED / FAILED / CANCELLED
```

Sandbox 状态描述临时资源：

```text
CREATED
    ↓
ACTIVE
    ↓
CLOSED
    ↓
DELETED
```

Candidate 不记录 `CONTAINER_STARTED`、`PROCESS_RUNNING`、`WORKTREE_READY` 等基础设施状态。

### 4.10 建议模块结构

```text
gepa_researcher/
├── core/
│   ├── candidate.py
│   ├── revision.py
│   ├── execution.py
│   ├── artifacts.py
│   └── failures.py
│
├── application/
│   ├── candidate_scheduler.py
│   ├── execution_service.py
│   └── candidate_service.py
│
├── runtime/
│   ├── sandbox.py
│   ├── local_sandbox.py
│   ├── apptainer_sandbox.py
│   ├── repository_materializer.py
│   └── git_result_service.py
│
└── storage/
    ├── candidate_store.py
    ├── execution_store.py
    └── artifact_store.py
```

依赖方向：

```text
CandidateScheduler
        ↓
ExecutionService
        ↓
SandboxFactory
```

- Scheduler 不知道 Apptainer bind 参数。
- Sandbox 不知道 Pareto 和 Proposal 优化逻辑。
- Judge 不知道 host path。
- Candidate 不拥有 workspace。

### 4.11 任务拆解

1. 冻结当前 smoke loop 行为基线。
2. 列出现有 Candidate、workspace、Registry、RuntimeBackend 和 Git audit 的全部状态。
3. 定义最小 `CandidateCard`、`ExecutionSpec`、`ExecutionRecord`、`RevisionRef` 和 `ArtifactRef`。
4. 建立按 execution ID 追加的 ExecutionStore。
5. 实现一次性 Sandbox 接口。
6. 实现从指定 revision 创建临时代码树。
7. 将 implementation、feedback、pareto 改为独立 execution。
8. 将 Git diff 检查和 commit 移到宿主 Harness。
9. 接入 RunnerAdapter，暂不修改 agent prompt 与智能逻辑。
10. 实现 success / failure / cancellation 下的统一清理。
11. 将 Candidate 与 workspace/worktree 解耦。
12. 删除 legacy canonical record、长期 workspace 复用和 candidate 级共享 artifacts。
13. 回归当前 gate、Judge 和 Pareto 行为。
14. 验证父子 revision 继承与同父多子并行。

### 4.12 交付物

- `CandidateCard`
- `RevisionRef`
- `ExecutionSpec`
- `ExecutionRecord`
- `ArtifactRef`
- `TypedExecutionFailure`
- `CandidateScheduler`
- `ExecutionService`
- `Sandbox` protocol
- `ApptainerSandbox`
- `RepositoryMaterializer`
- host-side `GitResultService`
- execution-scoped artifact layout
- legacy worktree migration notes
- Candidate execution fault-injection tests

### 4.13 验收标准

1. Candidate Card 不包含任何长期 workspace、container 或 process 字段。
2. 每个 execution 都有唯一 execution ID 和独立 artifact 目录。
3. implementation、feedback 和 pareto 不复用可变工作区。
4. 父代 Sandbox 删除后，子代仍能从父代 `result_revision` 完整创建并继续优化。
5. 同一父代可以并行派生多个彼此隔离的子代。
6. evaluation repo 为只读，修改源码会直接失败。
7. Agent 无法写 controller repository 的 Git common directory。
8. Agent 退出后由宿主 Harness 创建和验证 commit。
9. 任一 execution 失败不会污染其他 candidate。
10. Sandbox 在成功、异常、超时和取消后都能清理。
11. ExecutionStore 保留完整历史，不按 candidate ID 覆盖。
12. 删除临时 Sandbox 后，Candidate、Revision、metrics 和 artifacts 仍可重放。
13. 原有 smoke loop 的 gate、Judge 和 frontier 语义保持一致。
14. 不依赖现存 worktree 目录即可 resume 未完成的后续 phase。

---

## 5. v1.1: Reliable Loop Kernel

### 5.1 版本目标

v1.1 建立在 v1.0.5 已完成 Candidate、Revision、Execution 与 Sandbox 解耦的基础上，不增加新的 agent 智能，也不正式引入 Planner 和 Critic。

v1.1 只解决一个核心问题：

> 让当前 Proposer → Executor → Judger 的信息交付稳定、可验证、可恢复。

发布目标：

> 任一 agent 的单次输出错误，都不得直接导致整个 run 崩溃或状态不可恢复。

### 5.2 范围边界

v1.1 必须做：

- 统一 Agent Gateway
- schema registry
- typed failures
- 状态机
- event store
- artifact references
- resume / replay
- orchestrator 拆分
- 故障注入测试

v1.1 不做：

- 不引入 Planner
- 不引入 Critic
- 不做 optimizer momentum
- 不做 proposal learning rate
- 不做向量数据库
- 不做复杂长期 memory
- 不重写 Judge 评分思想
- 不追求更聪明的 proposal

一句话原则：

> 不让它更聪明，先让它不会莫名其妙死掉。

### 5.3 必须硬化的协议字段

所有跨模块消息必须包含：

```yaml
message_id:
message_type:
schema_version:
run_id:
round_id:
candidate_id:
attempt_id:
sender:
receiver:
created_at:
```

系统控制字段必须由 harness 产生，而不是由 LLM 自己填写：

- `candidate_id`
- `round_id`
- `attempt_id`
- `parent_ids`
- workspace path
- budget
- tool permissions
- state
- timestamps
- schema version

### 5.4 Candidate 生命周期状态机

基础状态：

```text
GENERATED
    ↓
ADMITTED
    ↓
EXECUTING
    ↓
EXECUTED
    ↓
JUDGED
    ↓
ACCEPTED / REJECTED
```

非法状态转换必须被拒绝，例如：

```text
GENERATED → JUDGED
EXECUTING → ACCEPTED
REJECTED → EXECUTING
```

失败状态不应被统一压成 `score=0`，而应保留错误语义：

```yaml
status: failed
error:
  code: AGENT_OUTPUT_SCHEMA_INVALID
  phase: proposer
  retryable: true
  message: ...
  raw_artifact_ref: ...
```

### 5.5 Agent Gateway

v1.1 最优先建立统一 `AgentGateway`。

所有 agent 调用都走统一管线：

```text
1. build request
2. persist request
3. invoke backend
4. persist raw response
5. parse response
6. validate schema
7. validate semantics
8. repair if allowed
9. return typed result or typed failure
```

建议接口：

```python
class AgentGateway:
    def invoke(
        self,
        request: AgentRequest[T],
        output_type: type[T],
    ) -> AgentResult[T]:
        ...
```

Proposer、Executor、Judger 不应各自实现一套 JSON 抽取、修复和错误处理。

### 5.6 输出解析失败处理层次

采用四级机制：

```text
Level 1: direct schema parse
        ↓ failed
Level 2: deterministic JSON extraction
        ↓ failed
Level 3: format-only LLM repair
        ↓ failed
Level 4: TypedFailure, loop continues
```

即使多次修复失败，也不应让 loop 崩溃，而应产生结构化失败对象，由状态机决定：

- 跳过该 candidate
- 重试当前 stage
- 降级为保守 candidate
- 停止本轮但保留可恢复状态

### 5.7 Event Store

v1.1 需要建立最小 Event Store，而不是等到 v1.2。

每次状态变化记录一个 append-only event：

```yaml
event_type: executor.completed
run_id:
round_id:
candidate_id:
attempt_id:
payload_ref:
timestamp:
```

第一版能力：

- append-only
- 可按 run / round / candidate 查询
- 可重放
- 可从事件恢复状态
- 可检测重复事件

### 5.8 Artifact Store

大体积内容不应反复塞进 JSON，而应通过 artifact reference 传递。

```yaml
artifact_ref:
  artifact_id:
  kind:
  path:
  sha256:
  producer:
  created_at:
```

适合保存为 artifact 的内容：

- raw agent response
- stdout / stderr
- Git diff
- modified files
- benchmark output
- metrics
- reports
- traces

### 5.9 建议模块结构

```text
gepa_researcher/
├── core/
│   ├── contracts/
│   │   ├── proposal.py
│   │   ├── execution.py
│   │   ├── judgment.py
│   │   └── errors.py
│   ├── protocol.py
│   ├── state_machine.py
│   └── identifiers.py
│
├── application/
│   ├── loop_engine.py
│   ├── stages/
│   │   ├── propose_stage.py
│   │   ├── execute_stage.py
│   │   ├── judge_stage.py
│   │   └── gate_stage.py
│   └── recovery.py
│
├── ports/
│   ├── proposer.py
│   ├── executor.py
│   ├── judger.py
│   ├── event_store.py
│   └── artifact_store.py
│
├── adapters/
│   ├── agent_gateway.py
│   ├── claude_backend.py
│   ├── apptainer_runtime.py
│   └── filesystem_store.py
│
└── observability/
    ├── events.py
    ├── logs.py
    └── user_stream.py
```

`loop_engine.py` 不应理解 prompt，也不应解析 agent 输出。它只消费结构化对象和状态机事件。

### 5.10 v1.1 任务拆解

1. 绘制现有信息流图。
2. 列出全部跨模块对象。
3. 区分系统控制字段和 agent 决策字段。
4. 定义协议对象和错误模型。
5. 建立 schema registry。
6. 建立 AgentGateway。
7. 接入统一 parse / validate / repair / failure 管线。
8. 建立 Candidate 状态机。
9. 建立最小 Event Store。
10. 建立 Artifact Store 和 artifact ref。
11. 将 Orchestrator 拆分为 stage-based loop engine。
12. 加入 resume / replay。
13. 迁移现有 Proposer / Executor / Judger 到新协议。
14. 编写故障注入测试。

### 5.11 v1.1 交付物

- `AgentGateway`
- `TypedFailure`
- core contracts
- schema registry
- state machine
- event store
- artifact store
- stage-based loop engine
- resume / replay 工具
- fault-injection test suite
- v1.1 migration notes

### 5.12 v1.1 验收标准

至少通过以下故障注入：

1. Proposer 输出 Markdown 包裹 JSON。
2. Proposer 输出截断 JSON。
3. Executor 输出自然语言。
4. Judger 缺少必须字段。
5. LLM 调用超时。
6. Executor 进程崩溃。
7. 测试执行一半中断。
8. 同一事件重复写入。
9. Loop 中途退出后 resume。
10. 某个 candidate 失败但其他 candidate 继续执行。
11. 旧 schema 数据能迁移或给出明确错误。
12. 若干完整 rounds 不出现未捕获异常。

---

## 6. v1.2: Global Context Plane

### 6.1 版本目标

v1.2 解决的问题不是“存更多信息”，而是：

> 建立唯一可信的信息来源，并为不同角色生成不同、受预算约束的 Context View。

v1.2 是 v2.0 的信息底座。

发布目标：

> 每个角色都能稳定获得职责所需信息，同时不能访问不属于它的信息。

### 6.2 范围边界

v1.2 必须做：

- global information pool
- entity / event / artifact 分层
- version-aware file cache
- role-specific Context Views
- PromptAssembler
- token budget / cropping
- user presentation stream
- source traceability

v1.2 不做：

- 暂不加入 Planner / Critic loop
- 暂不加入 proposal learning rate
- 暂不做长期 memory 自动演化
- 暂不让 context controller 自己修改事实
- 暂不默认引入向量数据库
- 暂不把 summarizer 变成新的自由 agent

Context Manager 应主要是数据与视图系统，不是新的全能大脑。

### 6.3 Context Plane 组成

```text
Global Context Plane
├── Event Store
├── Entity Store
├── Artifact Store
├── File Cache
└── Context View Builder
```

### 6.4 全局信息池分层

#### 5.4.1 Immutable Run Facts

运行开始后原则上不变：

- task goal
- task config
- project profile
- resolved config
- source commit
- validation contract
- metric definition
- safety policy
- runtime capabilities

#### 5.4.2 Loop State

当前运行状态：

- current round
- active candidates
- Pareto frontier
- score matrix
- budget usage
- no-improvement count
- current phase
- pending operations

#### 5.4.3 Episode Records

每个 candidate 的完整生命周期：

```text
ProposalIdea
    ↓
ExecutionAttempt 1
    ↓
ExecutionAttempt 2
    ↓
Final ExecutionSubmission
    ↓
Judgment
    ↓
GateDecision
```

即使 v1.2 还没有 Planner / Critic，也应采用能容纳未来 attempt 的模型。

#### 5.4.4 Artifacts

保存大体积或不可直接塞入 prompt 的内容：

- raw agent response
- stdout / stderr
- Git diff
- modified files
- metrics
- benchmark output
- reports
- traces
- screenshots
- generated datasets

#### 5.4.5 Derived Knowledge

从原始事实派生：

- round summary
- candidate summary
- failure summary
- parent-child comparison
- repeated failure patterns
- discovered project facts
- historical insights

派生知识必须保留来源：

```yaml
summary:
  text:
  source_event_ids:
  generated_at:
  summarizer_version:
```

#### 5.4.6 User-Facing Events

用户输出单独管理：

- round started
- candidate proposed
- execution progress
- candidate failed
- score changed
- final summary

不要把 console print、内部日志、用户展示混在一起。

建议结构：

```text
Internal Event Bus
    ├── Persistent Event Store
    ├── Debug Logger
    ├── Metrics Collector
    └── User Presentation Stream
```

### 6.5 文件缓存设计

第一版做版本感知文件缓存，不直接做复杂 RAG。

```yaml
file_record:
  repository_id:
  commit_sha:
  path:
  content_hash:
  size:
  language:
  raw_artifact_ref:
  summary:
  symbols:
  dependencies:
  last_accessed:
```

缓存 key 不能只用路径：

```text
错误:
cache["src/main.py"]

正确:
cache[(repo_id, commit_sha, path, content_hash)]
```

缓存分三档：

1. metadata cache: path、size、hash、language
2. content cache: 文件原文
3. semantic cache: 摘要、符号、依赖、重要片段

v1.2 初期检索方式：

- 精确路径
- 关键词
- symbol
- dependency
- recent access
- explicit references

暂不默认引入 embedding。

### 6.6 Role-specific Context Views

#### Optimizer View

应包含：

- task goal
- current frontier
- parent summaries
- parent-child score deltas
- Judge feedback
- repeated failures
- explored directions
- budget
- project capability summary

不应默认包含：

- 全量 stdout
- 每个文件完整内容
- Runner 每一步命令
- Judge 隐藏测试

#### Executor View

应包含：

- 当前 candidate
- task constraints
- project profile
- workspace
- relevant docs
- relevant source files
- validation commands
- previous attempt evidence
- allowed tools
- budget

不应默认包含：

- 其他并行 candidate 的实时结果
- Judge 隐藏评分细节
- 全局所有历史轨迹

#### Judge View

应包含：

- task goal
- rubric
- baseline / parent evidence
- candidate proposal
- final execution submission
- metrics
- validation
- diff summary
- hidden evaluation data

不应包含：

- Optimizer 对 candidate 的预期分数
- Executor 自我评价为“非常成功”的锚定文本
- 与评价无关的其他 candidate 主观描述

#### User View

应包含：

- 当前进度
- candidate 一句话说明
- 成败
- 重要指标
- 最终推荐
- 可追溯 artifact

不输出内部 agent 的长篇原始推理。

### 6.7 Context View 生成策略

采用三段式：

```text
Mandatory Context
    +
Relevant Retrieved Context
    +
Budgeted Recent Context
```

Mandatory Context：

- goal
- constraints
- output schema
- current object
- permissions

Relevant Retrieved Context：

- candidate 相关信息
- path 相关信息
- failure signature 相关信息
- parent-child comparison

Budgeted Recent Context：

- 最近事件
- 最近失败
- 最近 judge feedback
- 受 token budget 控制

不能继续依赖“最近三条 history”作为主要策略。最近不等于相关。

### 6.8 PromptAssembler

Prompt 不再手工散落拼接，而由统一组装器生成。

```text
PromptAssembler
├── Role Identity
├── Role Responsibility
├── Capability Policy
├── Output Contract
├── Task View
├── Episode View
├── Retrieved Evidence
└── Runtime Budget
```

建议接口：

```python
prompt = assembler.build(
    role=Role.EXECUTOR,
    context_view=view,
    contract=ExecutionResult,
    capability_set=executor_capabilities,
)
```

Prompt 文件只描述角色行为，不自己到各处读取 state、config、history。

### 6.9 v1.2 任务拆解

1. 定义 Context 数据分类。
2. 接入 v1.1 Event Store。
3. 建立 Entity Store。
4. 建立版本感知 File Cache。
5. 建立 Artifact indexing。
6. 建立 Role View Builder。
7. 建立 PromptAssembler。
8. 迁移现有 prompt。
9. 建立 User Presentation Stream。
10. 添加 context provenance。
11. 添加 token budget 和 deterministic cropping。
12. 添加 context view 权限测试。

### 6.10 v1.2 交付物

- GlobalContextPlane
- EntityStore
- version-aware FileCache
- ContextViewBuilder
- PromptAssembler
- role-specific context policies
- user presentation stream
- context provenance metadata
- context budget tests
- v1.2 migration notes

### 6.11 v1.2 验收标准

1. 所有 agent prompt 都通过 `ContextViewBuilder + PromptAssembler` 生成。
2. 不再通过临时私有字典在多个模块间传递大量状态。
3. 任一 prompt 字段都能追溯到 Context Store 来源。
4. 文件修改后缓存不会返回旧内容。
5. resume 后可以重建同样的 Context View。
6. 不同角色拿不到无权限信息。
7. Context 超预算时有确定性的裁剪策略。
8. 用户展示、内部日志、agent context 三者分离。
9. 原始事件和派生摘要均可追踪来源。
10. 替换 LLM backend 不需要重写 Context 管理。

---

## 7. v2.0: Stateful Agentic GEPA

### 7.1 版本目标

v2.0 正式实现：

```text
Optimizer
    ↓ ProposalIdea

Executor
├── Planner
├── Runner
└── Critic
    ↓ ExecutionSubmission

Judge
    ↓ JudgmentReport

Optimizer State Update
```

发布目标：

> Proposal 的方向质量、计划质量、执行质量和最终价值可以分别观察、分别归因。

### 7.2 范围边界

v2.0 必须做：

- Stateful Optimizer
- `ProposalIdea` contract
- Planner
- Runner
- Critic
- Executor internal loop
- differential Judge
- capability isolation
- optimizer feedback update

v2.0 不应做：

- 不让 Executor 完全自由发挥并改变 proposal 含义。
- 不让 Critic 修改代码。
- 不让 Judge 参与执行调试。
- 不让 Optimizer 直接指定代码行级改动。
- 不把 Planner 独立成顶层系统。
- 不把五个角色各自维护独立事实 memory。

### 7.3 Optimizer

职责：

- 分析全局状态
- 决定优化方向
- 提出抽象 idea
- 选择父代
- 控制方向多样性
- 控制优化幅度
- 学习历史方向效用

输出：

```yaml
proposal_idea:
  direction:
  problem:
  hypothesis:
  rationale:
  expected_effect:
  evidence_refs:
  scope:
  preserve:
  risks:
```

Optimizer 不输出：

- 具体改哪一行
- 完整执行命令
- 详细代码结构
- 逐步实现方案

### 7.4 Planner

职责：

- 将抽象 proposal 转换为可执行计划
- 检查项目结构
- 明确目标组件
- 拆分实现步骤
- 定义成功标准
- 定义允许偏移
- 定义验证计划
- 估算成本
- 标记风险

输出：

```yaml
execution_plan:
  proposal_id:
  understanding:
  inspect_steps:
  implementation_steps:
  validation_steps:
  allowed_deviations:
  forbidden_changes:
  completion_criteria:
```

Planner 不直接修改文件。

### 7.5 Runner

职责：

- 读文件
- 修改代码
- 运行命令
- 执行测试
- 收集 metrics
- 保存 artifacts
- 报告环境错误

输出：

```yaml
execution_attempt:
  plan_id:
  attempt_id:
  actions:
  changed_files:
  test_results:
  metrics:
  errors:
  artifact_refs:
```

Runner 不判断 proposal 是否有价值。

### 7.6 Critic

职责：

> 判断当前 attempt 是否充分实现 proposal，是否应该继续、重规划、提交或中止。

状态输出：

```text
CONTINUE
REPLAN
SUBMIT
ABORT
```

结构化输出：

```yaml
critique_decision:
  verdict: continue | replan | submit | abort
  plan_adherence:
  implementation_gaps:
  unresolved_errors:
  validation_gaps:
  feedback_to_planner:
  feedback_to_runner:
```

Critic 不修改代码，不给 proposal 最终价值评分。

### 7.7 Judge

职责：

> 相比 parent / baseline，这次交付到底带来了多大价值？

Judge 应从绝对评分改为相对评价。

建议输出：

```yaml
judgment:
  validity:
    hard_gates_passed:
    evidence_complete:

  relative_effect:
    parent_score:
    candidate_score:
    delta:
    uncertainty:

  dimensions:
    task_performance:
    robustness:
    correctness:
    generality:
    cost:
    maintainability:

  overall:
    score:
    confidence:
    verdict:

  feedback:
    direction_feedback:
    implementation_feedback:
```

Judge 要区分三类信息：

1. 是否有效
2. 提升多少
3. 为什么提升或失败

### 7.8 权限矩阵

| 角色 | 可以读取 | 可以写入 / 执行 |
|---|---|---|
| Optimizer | 全局摘要、frontier、历史 Judgment、insights | 只能写 `ProposalIdea` |
| Planner | Proposal、项目地图、相关文件、约束、历史 attempts | 只能写 `ExecutionPlan` |
| Runner | Plan、workspace、工具说明、相关文件 | 可修改 candidate workspace、运行命令 |
| Critic | Proposal、Plan、attempt trace、diff、测试结果 | 只能写 `CritiqueDecision` |
| Judge | Proposal、最终 submission、parent baseline、rubric、evaluation | 只能写 `JudgmentReport` |

硬约束：

- Optimizer 不直接触碰源码。
- Planner 不直接改代码。
- Critic 不直接修代码。
- Judge 不允许修改 candidate workspace。
- Runner 不读取隐藏 Judge 结果。

### 7.9 Executor 内部状态机

```text
PROPOSAL_RECEIVED
    ↓
PLANNING
    ↓
PLAN_READY
    ↓
ATTEMPT_RUNNING
    ↓
ATTEMPT_COMPLETED
    ↓
CRITIQUING
    ├── CONTINUE → ATTEMPT_RUNNING
    ├── REPLAN   → PLANNING
    ├── SUBMIT   → SUBMISSION_READY
    └── ABORT    → ABORTED
```

额外终止状态：

```text
BUDGET_EXHAUSTED
ENVIRONMENT_BLOCKED
PROPOSAL_INFEASIBLE
SCOPE_VIOLATION
NO_PROGRESS
```

### 7.10 Executor 循环边界

建议配置：

```yaml
executor_loop:
  max_plans: 2
  max_attempts_per_plan: 3
  max_total_attempts: 4
  max_wall_seconds:
  max_tokens:
  max_files_changed:
```

停止条件：

- Critic 选择 submit
- Critic 选择 abort
- 达到完成标准
- 预算耗尽
- 同一错误重复
- 无新进展
- 越过 proposal scope

### 7.11 Judge 校准路线

第一层：硬指标优先。

由程序计算的指标不让 LLM 猜：

- benchmark
- test pass rate
- latency
- memory
- changed files
- regression count
- parent delta

第二层：相对评价。

核心问题从：

> 是否满足任务？

变为：

> 比 parent 好多少？

第三层：锚点样例。

为每个任务配置：

- barely passing example
- useful improvement example
- exceptional example
- regression example

监控指标：

- score saturation rate
- 同分率
- Judge 重复评价方差
- Judge 与硬 metric 的相关性
- parent-child delta 分布

### 7.12 v2.0 任务拆解

建议不要一次上线五个角色，而是渐进实现：

1. 拆分 `ProposalIdea` 与 `ExecutionPlan`。
2. 上线 Planner + 旧 Runner。
3. 上线 Critic 单次反馈。
4. 开启 Executor 多 attempt loop。
5. 重构 Judge relative scoring。
6. 最后增加 Stateful Optimizer。

关键顺序：

> 先做 Executor，再做高级 Optimizer。

原因：

如果 Executor 还不能可靠实现抽象 proposal，Optimizer 提出的方向越聪明，失败归因反而越混乱。

### 7.13 v2.0 交付物

- `ProposalIdea` contract
- `ExecutionPlan` contract
- Planner role prompt and adapter
- Runner role adapter
- Critic role prompt and adapter
- Executor internal loop
- differential `JudgmentReport`
- capability isolation layer
- optimizer state
- optimizer feedback update
- role-specific evaluation dashboard or logs

### 7.14 v2.0 验收标准

1. Optimizer 只输出抽象优化方向，不输出代码级计划。
2. Planner 能把 proposal 转成可执行计划。
3. Runner 只负责执行计划和收集证据。
4. Critic 能基于 attempt 决定 continue / replan / submit / abort。
5. Executor 能在预算内进行多次 attempt。
6. Judge 主要输出相对 parent 的 delta，而不是宽松绝对分。
7. 方向失败、计划失败、执行失败、价值不足能被区分记录。
8. 权限矩阵由 harness 强制，而不是只靠 prompt。
9. Optimizer update 能利用 Judge 的 direction feedback 和 implementation feedback。
10. 一个 candidate 的失败不会污染其他 candidate 的状态。

---

## 8. 迁移策略

### 8.1 总体迁移原则

1. 先包裹旧实现，再逐步替换内部。
2. 保持 v1.0 行为作为回归基线。
3. 每次只改变一个系统层级。
4. 新旧 schema 并存一段时间，提供迁移或明确错误。
5. 所有重要中间对象都进入 Event Store。

### 8.2 v1.0 到 v1.0.5

迁移重点：

- 冻结当前 Proposer / Executor / Judger、gate 与 Pareto 行为作为基线。
- 先建立 Candidate Card、Revision、Execution 和 Artifact 的最小对象模型。
- 用一次性 Sandbox 替换 Candidate 级长期 worktree。
- 将 implementation、feedback 和 pareto 拆为独立 execution。
- 将 Git audit 与 commit 移到宿主 Harness。
- Registry 改为按 execution ID 追加记录。
- 暂不重写 agent prompt，不引入 Planner、Critic 和新 Judge 思想。

迁移完成标志：

- Candidate 不再拥有 workspace。
- 父子代通过 revision 继承，父 Sandbox 可安全删除。
- 每次 execution 可独立重放和清理。
- candidate 间不存在共享可变 Git 控制面和 artifacts。
- 当前 smoke loop 的业务结果保持一致。

### 8.3 v1.0.5 到 v1.1

迁移重点：

- 旧 Proposer / Executor / Judger 暂不重写 prompt。
- 在 v1.0.5 对象模型上接入 AgentGateway。
- 将旧输出包装成新 contract。
- 将旧异常转换为 TypedFailure。
- 将旧 orchestrator 拆为 stage。
- 为 Candidate 与 Execution 建立明确状态机。
- 将执行记录接入 Event Store 和 Artifact Store。

迁移完成标志：

- 同一任务在 v1.1 下能跑通。
- agent 输出异常不会直接崩溃。
- 运行可 resume。
- event log 可从 Candidate / Execution / Revision 事件重放基本状态。

### 8.4 v1.1 到 v1.2

迁移重点：

- 将 `_gepa_context` 一类临时上下文迁移到 Global Context Plane。
- 所有 prompt 通过 ContextViewBuilder 和 PromptAssembler 生成。
- 当前 Proposer / Executor / Judger 仍可保持三角色结构。
- 先实现精确检索和 deterministic cropping，不引入 embedding。

迁移完成标志：

- prompt 输入来源可追踪。
- 不同角色 context 不混用。
- 文件缓存版本正确。
- 用户输出从内部日志中分离。

### 8.5 v1.2 到 v2.0

迁移重点：

- 先拆 `ProposalIdea` 和 `ExecutionPlan`。
- 当前 Executor 先作为 Runner 使用。
- 增加 Planner，但不立刻增加 Critic。
- 增加 Critic 单次反馈后，再开启多 attempt loop。
- Judge 改为 differential scoring。
- 最后引入 Stateful Optimizer。

迁移完成标志：

- 五个逻辑角色职责清晰。
- 权限和信息访问由 harness 控制。
- 方向、计划、执行、价值可以分别归因。

---

## 9. 测试策略

### 9.1 v1.0.5 测试重点

测试目标：

> Candidate、Revision、Execution 和 Sandbox 的边界正确，父子代可继承、单次执行可隔离、临时环境可销毁。

测试类型：

- Candidate Card serialization tests
- revision lineage tests
- execution identity tests
- ephemeral sandbox lifecycle tests
- host-side Git commit tests
- read-only evaluation tests
- execution-scoped artifact tests
- cleanup / cancellation tests
- same-parent multi-child isolation tests
- legacy smoke-loop regression tests

关键场景：

- 父代 Sandbox 删除后创建子代
- 两个子代从同一父 revision 并行执行
- implementation 留下脏文件后不污染 evaluation
- evaluation 尝试修改源码
- Agent 执行 Git 危险命令
- 进程超时和被取消
- Sandbox 创建到一半失败
- commit 创建失败后的回滚
- 删除所有 tmp 目录后重放 Candidate 与 Execution 记录

### 9.2 v1.1 测试重点

测试目标：

> 协议、状态机、错误恢复可靠。

测试类型：

- schema parse tests
- output repair tests
- typed failure tests
- state transition tests
- event store append / replay tests
- artifact reference tests
- resume tests
- fault injection tests

关键故障场景：

- 非 JSON 输出
- 截断 JSON
- 缺字段
- 超时
- 进程崩溃
- 重复事件
- candidate 局部失败
- 中途退出恢复

### 9.3 v1.2 测试重点

测试目标：

> Context 可追溯、可裁剪、可隔离。

测试类型：

- context provenance tests
- role permission tests
- file cache invalidation tests
- deterministic cropping tests
- prompt assembly snapshot tests
- resume context reconstruction tests
- user stream rendering tests

关键场景：

- 同一路径不同 content hash
- context 超预算
- 角色请求无权限信息
- 派生摘要来源追踪
- prompt 快照稳定

### 9.4 v2.0 测试重点

测试目标：

> 职责分离、执行循环、相对评价、信用归因可靠。

测试类型：

- ProposalIdea contract tests
- ExecutionPlan contract tests
- Planner fidelity tests
- Runner action trace tests
- Critic decision tests
- Executor loop budget tests
- Judge calibration tests
- optimizer feedback update tests
- end-to-end attribution tests

关键场景：

- idea 有价值但 plan 失败
- plan 合理但 runner 实现失败
- runner 通过测试但 proposal 无价值
- Critic 要求 replan
- Critic 要求 abort
- Judge 识别微小提升而非满分
- 同一方向多次失败进入降权

---

## 10. 风险与应对

### 10.1 风险：schema 过度膨胀

表现：

- 每个自然语言判断都被拆成大量字段。
- 修改 schema 的成本超过业务实现。

应对：

- 只有会用于程序分支、权限判断、查询、比较或状态恢复的字段才结构化。
- 供 LLM 理解的分析内容可以保留文本。

### 10.2 风险：Context Manager 变成新 Agent

表现：

- Context Manager 自己决定下一步怎么优化。
- Context Manager 修改事实或覆盖原始记录。

应对：

- Context Manager 只负责保存、索引、筛选、组装、权限和预算。
- 原始事件不可变。
- 派生知识必须保留来源。

### 10.3 风险：Critic 变成第二个 Runner

表现：

- Critic 不只是反馈，而是直接修改代码。

应对：

- Critic 只能写 `CritiqueDecision`。
- 修改能力只属于 Runner。

### 10.4 风险：Judge 继续分数饱和

表现：

- 大量 candidate 都接近满分。
- Optimizer 无法分辨小改进和大改进。

应对：

- 引入 parent-relative scoring。
- 强制报告 delta 和 uncertainty。
- 监控 score saturation rate。
- 使用 hard metrics 和锚点样例校准。

### 10.5 风险：Executor 自由度过高导致 proposal 漂移

表现：

- Runner 修改范围超过 proposal scope。
- 实际完成的是另一个 idea。

应对：

- Planner 固定目标、范围、禁区和完成标准。
- Critic 检查 plan adherence。
- Harness 强制 max files、forbidden paths、budget。
- 大偏移必须 replan 或 abort。

### 10.6 风险：过早引入高级 Optimizer

表现：

- Optimizer 输出很复杂，但 Executor 无法稳定实现。
- 失败归因更加混乱。

应对：

- v2.0 先做 Executor，再做 Stateful Optimizer。
- 先让抽象 proposal 可被可靠落地。

---

## 11. 反过度设计原则

### 11.1 不要为了发明而发明

新增模块必须解决明确问题：

- 信息是否更稳定？
- 归因是否更清晰？
- 权限是否更可控？
- 反馈是否更可学习？
- 故障是否更可恢复？

如果答案都不是，就先不要加。

### 11.2 不要把所有内容结构化

判断标准：

> 该字段是否会用于程序分支、权限判断、查询、比较或状态恢复？

是，则结构化。

否，则可以保留为文本。

### 11.3 不要每个角色一套 memory

所有角色共享 Global Context Plane。不同角色只是拿到不同 Context View。

### 11.4 不要默认引入向量数据库

v1.2 初期先做：

- path
- symbol
- keyword
- dependency
- recent access
- explicit reference

确认需求后再考虑 embedding。

### 11.5 不要让 Prompt 承担 Harness 职责

Prompt 可以提醒：

> 不要修改 benchmark。

但真正限制必须由 harness 执行：

- forbidden paths
- permission policy
- max changed files
- timeout
- tool access
- budget

---

## 12. 推荐实施顺序总表

### 12.1 v1.0.5 顺序

```text
1. 冻结当前 smoke loop 基线
2. 定义 Candidate / Revision / Execution / Artifact 最小对象
3. 建立按 execution ID 追加的 Store
4. 实现一次性 Sandbox protocol
5. 实现从 revision 创建干净代码树
6. 将 Git commit 移到宿主 Harness
7. 拆分 implementation / feedback / pareto execution
8. 接回旧 Executor 与 Judge
9. 完善 cleanup / cancellation / rollback
10. 删除长期 worktree 和 candidate 级共享 artifacts
```

### 12.2 v1.1 顺序

```text
1. 绘制现有信息流图
2. 列出全部跨模块对象
3. 定义协议和错误模型
4. 建立 AgentGateway
5. 建立状态机
6. 建立 Event / Artifact Store
7. 拆分 Orchestrator
8. 加 resume / replay
9. 做 fault-injection tests
10. 迁移现有三个 agent
```

### 12.3 v1.2 顺序

```text
1. 定义 Context 数据分类
2. 接入 v1.1 Event Store
3. 建立 Entity Store
4. 建立 File Cache
5. 建立 Role View Builder
6. 建立 PromptAssembler
7. 迁移现有 prompt
8. 建立 User Presentation Stream
9. 加 context provenance
10. 加预算和裁剪测试
```

### 12.4 v2.0 顺序

```text
1. 拆 ProposalIdea 与 ExecutionPlan
2. 上线 Planner + 旧 Runner
3. 上线 Critic 单次反馈
4. 开启 Executor 多 attempt loop
5. 重构 Judge relative scoring
6. 增加 Stateful Optimizer
```

---

## 13. 里程碑清单

### v1.0

- [ ] 当前基础 GEPA loop 可运行
- [ ] 当前版本作为 baseline 发布
- [ ] 记录当前已知问题
- [ ] 冻结 v1.0 后进入可靠性重构

### v1.0.5 Candidate Execution Kernel

- [ ] 冻结当前 smoke loop 行为基线
- [ ] 定义 `CandidateCard`
- [ ] 定义 `RevisionRef`
- [ ] 定义 `ExecutionSpec` / `ExecutionRecord`
- [ ] 定义 `ArtifactRef` / `TypedExecutionFailure`
- [ ] 实现按 execution ID 追加的 ExecutionStore
- [ ] 实现一次性 Sandbox protocol
- [ ] 实现 ApptainerSandbox
- [ ] 实现从指定 revision 创建干净代码树
- [ ] 将 Git diff、scope 检查和 commit 移到宿主 Harness
- [ ] 拆分 implementation / feedback / pareto execution
- [ ] 实现 execution-scoped artifacts
- [ ] 实现成功、失败、超时和取消后的清理
- [ ] 移除 Candidate 对 workspace/worktree 的持久绑定
- [ ] 验证父代 Sandbox 删除后子代仍可从父 revision 继续
- [ ] 验证同一父代可安全派生多个并行子代
- [ ] 确认原有 gate、Judge 和 frontier 行为不变

### v1.1 Reliable Loop Kernel

- [ ] 完成现有信息流图
- [ ] 完成跨模块对象清单
- [ ] 定义 core contracts
- [ ] 定义 typed errors
- [ ] 实现 AgentGateway
- [ ] 实现 parse / validate / repair / failure 管线
- [ ] 实现 Candidate 状态机
- [ ] 实现 Event Store
- [ ] 实现 Artifact Store
- [ ] 拆分 Orchestrator 为 stages
- [ ] 支持 resume / replay
- [ ] 迁移现有 Proposer / Executor / Judger
- [ ] 完成 fault-injection tests
- [ ] 确认 agent 单点失败不会破坏整个 run

### v1.2 Global Context Plane

- [ ] 定义 Context 数据分层
- [ ] 建立 Entity Store
- [ ] 接入 Event Store
- [ ] 建立 version-aware File Cache
- [ ] 建立 Artifact indexing
- [ ] 建立 ContextViewBuilder
- [ ] 建立 PromptAssembler
- [ ] 定义 role-specific context policies
- [ ] 迁移现有 prompt
- [ ] 建立 User Presentation Stream
- [ ] 添加 provenance
- [ ] 添加 deterministic cropping
- [ ] 完成角色权限测试
- [ ] 确认 prompt 输入均可追溯来源

### v2.0 Stateful Agentic GEPA

- [ ] 定义 `ProposalIdea`
- [ ] 定义 `ExecutionPlan`
- [ ] 将旧 Executor 收缩为 Runner
- [ ] 实现 Planner
- [ ] 实现 Critic
- [ ] 实现 Executor internal loop
- [ ] 实现 continue / replan / submit / abort 状态
- [ ] 实现 executor budget control
- [ ] 重构 Judge 为 relative scoring
- [ ] 引入 hard metrics 和 delta
- [ ] 实现 capability isolation
- [ ] 实现 Stateful Optimizer
- [ ] 实现 optimizer feedback update
- [ ] 验证方向、计划、执行、价值可分别归因

---

## 14. 最终路线定义

```text
GEPA v1.0
基础可运行 Loop。

GEPA v1.0.5 - Candidate Execution Kernel
建立 Candidate Card、Revision、Execution、Artifact 与一次性 Sandbox，
通过 revision 继承父子代码成果，通过独立 execution 实现隔离、复现和清理。

GEPA v1.1 - Reliable Loop Kernel
建立 contract-first 的信息流、状态机、故障恢复与模块边界，
保证 Proposer、Executor、Judger 的交付可靠且可恢复。

GEPA v1.2 - Global Context Plane
建立事件、实体、Artifact 和文件缓存构成的全局信息池，
通过角色视图和统一 Prompt Assembly 管理所有 agent 信息输入与用户输出。

GEPA v2.0 - Stateful Agentic GEPA
实现 Optimizer + Executor(Planner → Runner → Critic) + Judge，
由 harness 强制职责、权限和信息隔离，并实现可归因的多次执行与相对评价。
```

这条路线的阶段价值：

- v1.0.5 即使不改变 agent 行为，也能消除 workspace/worktree 生命周期混乱，建立可复现的 Candidate 执行底座。
- v1.1 即使没有新 agent，也显著提升可用性和稳定性。
- v1.2 即使没有新架构，也消除碎片化 context 和 prompt 拼接问题。
- v2.0 在可靠信息流和全局上下文底座之上，专心解决 agent optimization 本身。
