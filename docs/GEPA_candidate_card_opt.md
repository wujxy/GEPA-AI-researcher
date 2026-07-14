# GEPA Candidate Card 与 Execution Kernel 重构执行参考

## 0. 文档目的

本文档用于指导 GEPA 在进入 `v1.1 Reliable Loop Kernel` 前，先完成 Candidate 任务调度、父子继承、执行隔离与运行环境生命周期的专项重构。

该阶段建议命名为：

```text
v1.0.5 Candidate Execution Kernel
```

本次重构不增加新的 agent，不引入 Planner / Critic，也不改变当前 gate、Judge 和 Pareto selection 的核心算法。它只重建执行地基，使后续协议、状态机、Event Store 和 Context Plane 建立在正确对象边界上。

核心目标：

> Candidate 是持久业务档案；Revision 是不可变代码成果；Execution 是一次具体任务；Sandbox 是一次 Execution 临时租用、用后销毁的运行资源。

## Implemented v1.0.5 shape

```text
CandidateCard -> CandidateStore
ExecutionSpec -> ExecutionService
ExecutionRecord -> ExecutionStore
SandboxSession -> RepositoryMaterializer
ArtifactRef -> ArtifactStore
GitResultService -> commit audit and readonly guard
RunnerAdapter -> transient agent execution config
```

---

## 1. 当前问题与重构判断

### 1.1 当前复杂化的来源

当前实现将多个本应独立的对象绑定在一起：

```text
Candidate
≈ Git branch
≈ persistent worktree
≈ current execution
≈ candidate artifacts
≈ evaluation workspace
≈ registry canonical record
```

这会引起：

1. 同一 Candidate 多次执行时记录互相覆盖。
2. implementation、feedback 和 pareto 复用可变 workspace 或 artifacts。
3. 父子代的代码继承依赖长期存在的 worktree。
4. Agent 容器需要访问共享 Git common directory。
5. Candidate 状态和基础设施状态混在一起。
6. resume 依赖磁盘残留，而不是持久业务事实。
7. worktree、branch、artifact 和 Registry 各自有生命周期，清理困难。
8. 评分对象可能与实际继承的 commit 不一致。

### 1.2 重构后的核心关系

```text
Proposal
    ↓
CandidateFactory
    ↓
CandidateCard
    ↓
CandidateScheduler
    ↓
ExecutionSpec
    ↓
ExecutionService
    ↓
Ephemeral Sandbox
    ↓
ExecutionRecord + ArtifactRefs + result_revision
    ↓
CandidateCard 更新
    ↓
Judge / Gate / Pareto
```

关键约束：

- Candidate 不拥有 Sandbox。
- Candidate 可以引用多个 Execution。
- 每次 Execution 创建独立 Sandbox。
- 父子代继承 Revision，不继承 Sandbox。
- Judge 评价 Revision，不评价某个长期工作目录。
- 临时 checkout 删除后，所有业务状态仍能恢复。

---

## 2. 设计原则

### 2.1 持久事实与临时资源分离

持久化：

```text
Candidate Card
Proposal
Revision SHA
ExecutionSpec / ExecutionRecord
ArtifactRef
Metrics
Judgment
TypedFailure
```

不持久化为业务状态：

```text
workspace path
worktree directory
container instance
process PID
scratch
temporary HOME
shell session
runtime cache
```

后者可以在 ExecutionRecord 中作为调试元数据短期记录，但不能成为恢复和父子继承的权威来源。

### 2.2 继承 Revision，不继承 Sandbox

父代完成后：

```text
parent.base_revision = A
parent.result_revision = B
```

子代创建时：

```text
child.parent_candidate_ids = [parent.id]
child.base_revision = parent.result_revision = B
```

子代执行：

```text
checkout B 到新的 Sandbox
    ↓
继续修改
    ↓
形成 C
    ↓
child.result_revision = C
```

销毁父代 Sandbox 不会丢失父代成果，因为父代成果由 commit B、Candidate Card、Artifact 和 Judgment 表示。

### 2.3 每次执行从确定输入开始

每个 Execution 必须声明：

```text
input_revision
phase
writable
dataset / evaluation contract
budget
capability policy
environment identity
```

禁止使用“当前目录大概是哪个版本”作为输入语义。

### 2.4 Agent 不管理 Git 控制面

Agent 只负责：

- 读取文件
- 修改文件
- 运行命令
- 收集结果

宿主 Harness 负责：

- materialize input revision
- 检查 diff
- scope / forbidden path validation
- deterministic test contract
- commit
- result revision identity
- cleanup

### 2.5 正确性优先于运行性能

第一版允许使用独立临时 clone 或完整代码树复制。

暂不为了速度引入：

- 长期 worktree pool
- overlay 管理器
- 分布式 worker
- workspace lease recovery
- 复杂容器缓存

稳定后再逐步优化为 local bare mirror、reflink、copy-on-write 或 Apptainer overlay。

---

## 3. 领域对象设计

### 3.1 CandidateCard

Candidate Card 回答：

> 这个优化候选是什么、从哪里来、实现成了哪个代码版本、经历了哪些执行、最终是否被接受？

建议 Python 结构：

```python
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class CandidateStatus(str, Enum):
    GENERATED = "generated"
    ADMITTED = "admitted"
    MATERIALIZING = "materializing"
    MATERIALIZED = "materialized"
    EVALUATING = "evaluating"
    EVALUATED = "evaluated"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    IMPLEMENTATION_FAILED = "implementation_failed"
    EVALUATION_FAILED = "evaluation_failed"
    CANCELLED = "cancelled"


@dataclass
class CandidateCard:
    candidate_id: str
    round_id: int
    parent_candidate_ids: tuple[str, ...]

    proposal_id: str
    proposal: "ProposalIdea"

    base_revision: str
    status: CandidateStatus = CandidateStatus.GENERATED
    result_revision: str | None = None

    execution_ids: list[str] = field(default_factory=list)
    judgment_ids: list[str] = field(default_factory=list)
    artifact_ids: list[str] = field(default_factory=list)

    final_decision: str | None = None
    score_summary: dict[str, float] = field(default_factory=dict)

    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
```

Candidate Card 不应包含：

```text
workspace_path
worktree_branch
container_repo_path
scratch_path
container_home
process_id
runtime_backend_instance
```

#### CandidateCard 不变量

1. `candidate_id` 创建后不可变。
2. `parent_candidate_ids` 创建后不可变。
3. `base_revision` 创建后不可变。
4. `proposal` 创建后不可被 Runner 偷换语义。
5. `result_revision` 只能从空变为一个经过 Harness 审核的 revision。
6. `execution_ids` 只能追加，不覆盖。
7. Candidate 被 accepted/rejected 后，不能重新进入 implementation；需要继续优化时创建子 Candidate。

---

### 3.2 RevisionRef

Revision 回答：

> 某个 Candidate 确定交付的代码状态是什么？

```python
@dataclass(frozen=True)
class RevisionRef:
    commit_sha: str
    repository_id: str
    parent_sha: str | None
    producing_candidate_id: str | None
    producing_execution_id: str | None
```

第一版可以只在 CandidateCard 中保存 SHA，但代码内部最好使用 `RevisionRef`，避免把任意字符串当作合法 revision。

Revision 必须具备：

- 可 checkout
- 可 hash 验证
- 可找到 parent
- 可找到 producing execution
- 不依赖旧 Sandbox 存在

---

### 3.3 ExecutionSpec

ExecutionSpec 回答：

> 现在要对哪个 Candidate 的哪个 Revision，执行什么阶段任务？

```python
class ExecutionPhase(str, Enum):
    IMPLEMENTATION = "implementation"
    FEEDBACK_EVAL = "feedback_eval"
    PARETO_EVAL = "pareto_eval"
    ROBUSTNESS_EVAL = "robustness_eval"
    REPAIR = "repair"


@dataclass(frozen=True)
class ExecutionBudget:
    wall_seconds: int
    max_tokens: int | None = None
    max_files_changed: int | None = None
    max_commands: int | None = None


@dataclass(frozen=True)
class CapabilityPolicy:
    repo_writable: bool
    network_allowed: bool
    allowed_tools: tuple[str, ...]
    forbidden_paths: tuple[str, ...]


@dataclass(frozen=True)
class ExecutionSpec:
    execution_id: str
    run_id: str
    round_id: int
    candidate_id: str
    phase: ExecutionPhase

    input_revision: str
    dataset_ref: str | None
    evaluator_version: str | None

    budget: ExecutionBudget
    capability_policy: CapabilityPolicy

    created_at: datetime
```

ExecutionSpec 创建后不可变。Scheduler 若需要重试，应创建新的 attempt/execution ID，而不是原地重写旧任务。

---

### 3.4 ExecutionRecord

ExecutionRecord 回答：

> 这一次具体执行发生了什么、是否成功、产生了哪些证据？

```python
class ExecutionStatus(str, Enum):
    PENDING = "pending"
    PREPARING = "preparing"
    RUNNING = "running"
    COLLECTING = "collecting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class ExecutionRecord:
    execution_id: str
    candidate_id: str
    phase: ExecutionPhase
    input_revision: str

    status: ExecutionStatus
    started_at: datetime | None = None
    finished_at: datetime | None = None

    result_revision: str | None = None
    metrics: dict[str, float] = field(default_factory=dict)
    artifact_refs: list["ArtifactRef"] = field(default_factory=list)
    failure: "TypedExecutionFailure | None" = None

    environment_hash: str | None = None
    dataset_hash: str | None = None
    evaluator_version: str | None = None
```

Store 必须以 `execution_id` 为主键：

```python
execution_store.append(record)
execution_store.get(execution_id)
execution_store.list_for_candidate(candidate_id)
```

禁止：

```python
latest_execution_by_candidate[candidate_id] = record
```

---

### 3.5 ArtifactRef

Artifact 回答：

> 大体积输出在哪里、由谁生成、内容是否可验证？

```python
class ArtifactKind(str, Enum):
    RAW_AGENT_RESPONSE = "raw_agent_response"
    STDOUT = "stdout"
    STDERR = "stderr"
    GIT_DIFF = "git_diff"
    TEST_REPORT = "test_report"
    METRICS = "metrics"
    BENCHMARK = "benchmark"
    EXECUTION_TRACE = "execution_trace"
    SUBMISSION = "submission"


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    kind: ArtifactKind
    execution_id: str
    relative_path: str
    sha256: str
    size_bytes: int
    created_at: datetime
    reusable: bool = False
```

Artifact 路径应相对于当前 run 的 artifact root，不在 Candidate Card 中存绝对宿主路径。

---

### 3.6 TypedExecutionFailure

```python
@dataclass(frozen=True)
class TypedExecutionFailure:
    code: str
    phase: ExecutionPhase
    retryable: bool
    message: str
    details_artifact_id: str | None = None
```

建议错误码：

```text
SANDBOX_PREPARE_FAILED
REVISION_NOT_FOUND
AGENT_PROCESS_FAILED
AGENT_TIMEOUT
SCOPE_VIOLATION
FORBIDDEN_PATH_CHANGED
VALIDATION_FAILED
COMMIT_FAILED
ARTIFACT_COLLECTION_FAILED
SANDBOX_CLEANUP_FAILED
EXECUTION_CANCELLED
```

失败不再统一压成 `score=0`。

---

### 3.7 SandboxSession

SandboxSession 是临时基础设施对象，不进入 Candidate Store。

```python
@dataclass
class SandboxSession:
    sandbox_id: str
    execution_id: str

    host_repo_path: Path
    host_artifact_path: Path
    host_scratch_path: Path
    host_home_path: Path

    guest_repo_path: str = "/workspace/repo"
    guest_artifact_path: str = "/workspace/artifacts"
    guest_scratch_path: str = "/workspace/scratch"

    backend_name: str = "apptainer"
```

它只在：

```python
with sandbox_factory.create(spec) as sandbox:
    ...
```

上下文中存在。

---

## 4. 父子代迭代模型

### 4.1 单父代继承

```text
baseline A
    ↓
Parent Candidate P
    ↓ implementation
revision B
    ↓
Child Candidate C
base_revision = B
    ↓ implementation in new sandbox
revision C
```

Candidate 图：

```text
P ──▶ C
```

Revision 图：

```text
A ──▶ B ──▶ C
```

两种血缘分别保存：

```yaml
candidate_lineage:
  parent_candidate_ids:
    - P
revision_lineage:
  base_revision: B
  result_revision: C
```

### 4.2 同父多子

```text
revision B
├── Candidate C1 / Sandbox S1 → revision C1
└── Candidate C2 / Sandbox S2 → revision C2
```

两个子代只共享不可变输入 revision B，不共享 writable repo、scratch、HOME 和 artifacts。

### 4.3 多父代 Proposal

第一版不要假装自动完成代码 merge。

可采用：

```text
semantic parents: [P1, P2]
code base parent: P1
base_revision: P1.result_revision
```

Planner/Executor 可参考 P2 的 idea 与 Judgment，但代码只从一个明确 revision 开始。

未来真正支持代码融合时，应显式产生：

```text
merge / composition execution
    ↓
merge revision M
    ↓
child.base_revision = M
```

而不是简单取 `parent_ids[0]` 却宣称完成多父代融合。

### 4.4 父代评估复用

父代 metrics 可以复用，但必须由稳定 evaluation key 判断：

```text
evaluation_key = hash(
    revision_sha,
    dataset_hash,
    evaluator_version,
    evaluation_config_hash,
    environment_hash,
)
```

key 完全相同才允许复用。否则重新创建 evaluation execution。

---

## 5. Execution 与 Sandbox 生命周期

### 5.1 标准流程

```text
ExecutionSpec created
    ↓
ExecutionRecord = PENDING
    ↓
prepare sandbox from input_revision
    ↓
ExecutionRecord = PREPARING
    ↓
run agent/evaluator
    ↓
ExecutionRecord = RUNNING
    ↓
collect logs, metrics, diff, artifacts
    ↓
ExecutionRecord = COLLECTING
    ↓
commit or validate immutable revision
    ↓
ExecutionRecord = SUCCEEDED / FAILED
    ↓
close processes
    ↓
delete tmp sandbox
```

### 5.2 Sandbox 必须在以下情况清理

- 正常成功
- Agent 返回失败
- Agent 超时
- 用户取消
- Python 异常
- artifact 收集失败
- Git commit 失败
- Judge 阶段失败

实现上应使用 context manager 与 `finally`：

```python
sandbox = None
try:
    sandbox = sandbox_factory.create(spec)
    return execution_runner.run(spec, sandbox)
finally:
    if sandbox is not None:
        sandbox.close_processes()
        sandbox.cleanup()
```

### 5.3 清理失败处理

清理失败不能覆盖原始执行结果。

例如：

```yaml
execution_status: succeeded
cleanup_status: failed
cleanup_failure:
  code: SANDBOX_CLEANUP_FAILED
```

随后由 run-level janitor 重试清理。

---

## 6. 目录与存储布局

推荐布局：

```text
runs/<run_id>/
├── run.json
│
├── candidates/
│   ├── cand_001.json
│   └── cand_002.json
│
├── executions/
│   ├── exec_001/
│   │   ├── execution.json
│   │   ├── stdout.log
│   │   ├── stderr.log
│   │   └── artifacts/
│   │       ├── diff.patch
│   │       ├── metrics.json
│   │       └── test_report.json
│   └── exec_002/
│
├── judgments/
│   └── judgment_001.json
│
├── repository-cache/
│   └── project.git
│
└── tmp/
    ├── exec_001/
    │   ├── repo/
    │   ├── scratch/
    │   └── home/
    └── exec_002/
```

`tmp/` 可以随时删除；删除后不能影响：

- Candidate 查询
- Revision checkout
- Execution history
- metrics
- Judge report
- resume

---

## 7. 核心服务接口

### 7.1 CandidateFactory

```python
class CandidateFactory:
    def create_child(
        self,
        *,
        round_id: int,
        parent_cards: list[CandidateCard],
        proposal: ProposalIdea,
        code_base_parent_id: str,
    ) -> CandidateCard:
        parent = find_parent(code_base_parent_id)
        if parent.result_revision is None:
            raise ParentNotMaterialized(...)

        return CandidateCard(
            candidate_id=new_candidate_id(),
            round_id=round_id,
            parent_candidate_ids=tuple(p.candidate_id for p in parent_cards),
            proposal_id=proposal.proposal_id,
            proposal=proposal,
            base_revision=parent.result_revision,
        )
```

### 7.2 CandidateScheduler

Scheduler 只决定下一步任务，不执行底层操作。

```python
class CandidateScheduler:
    def next_execution(self, card: CandidateCard) -> ExecutionSpec | None:
        if card.status == CandidateStatus.ADMITTED:
            return make_implementation_spec(card)

        if card.status == CandidateStatus.MATERIALIZED:
            return make_feedback_eval_spec(card)

        if feedback_passed(card) and not pareto_done(card):
            return make_pareto_eval_spec(card)

        return None
```

第一版只需本地队列与并发上限：

```python
asyncio.Semaphore(max_parallel_executions)
```

不做复杂资源编排。

### 7.3 ExecutionService

```python
class ExecutionService:
    def execute(self, spec: ExecutionSpec) -> ExecutionRecord:
        record = self.store.create_pending(spec)

        try:
            with self.sandbox_factory.create(spec) as sandbox:
                self.store.mark_preparing(spec.execution_id)
                raw_result = self.runner.run(spec, sandbox)

                self.store.mark_collecting(spec.execution_id)
                artifacts = self.artifact_collector.collect(spec, sandbox, raw_result)

                result_revision = None
                if spec.phase == ExecutionPhase.IMPLEMENTATION:
                    result_revision = self.git_result_service.finalize(spec, sandbox)
                else:
                    self.readonly_guard.assert_unchanged(spec, sandbox)

                return self.store.mark_succeeded(
                    spec.execution_id,
                    artifacts=artifacts,
                    result_revision=result_revision,
                    metrics=raw_result.metrics,
                )
        except Exception as exc:
            failure = self.failure_mapper.map(spec, exc)
            return self.store.mark_failed(spec.execution_id, failure)
```

### 7.4 Sandbox Protocol

借鉴 mini-swe-agent 的 Environment 简洁边界：

```python
from typing import Protocol


class Sandbox(Protocol):
    def execute(
        self,
        command: list[str],
        *,
        cwd: str,
        timeout_seconds: int,
        env: dict[str, str] | None = None,
    ) -> "CommandResult": ...

    def read_file(self, path: str) -> bytes: ...
    def write_file(self, path: str, content: bytes) -> None: ...
    def close_processes(self) -> None: ...
    def cleanup(self) -> None: ...
```

上层 Runner 不需要理解 Apptainer bind、宿主路径或容器路径。

### 7.5 RepositoryMaterializer

```python
class RepositoryMaterializer:
    def materialize(
        self,
        *,
        revision_sha: str,
        destination: Path,
        writable: bool,
    ) -> "MaterializedRepository": ...
```

第一版可用：

```bash
git clone --no-checkout <local-source-or-mirror> <destination>
git -C <destination> checkout --detach <revision_sha>
```

实现阶段可写；evaluation 阶段可通过只读 bind 或文件权限提供只读视图。

### 7.6 GitResultService

```python
class GitResultService:
    def finalize(
        self,
        spec: ExecutionSpec,
        sandbox: SandboxSession,
    ) -> str:
        self.assert_head_is(spec.input_revision, sandbox)
        diff = self.collect_diff(sandbox)
        self.scope_policy.validate(diff)
        self.validation_contract.run(sandbox)
        return self.commit_result(spec, sandbox, diff)
```

提交信息建议包含：

```text
GEPA candidate <candidate_id>

execution: <execution_id>
base: <input_revision>
proposal: <proposal_id>
```

---

## 8. Implementation 与 Evaluation 的不同权限

### 8.1 Implementation Execution

```yaml
phase: implementation
input_revision: parent.result_revision
capabilities:
  repo_writable: true
  artifacts_writable: true
  scratch_writable: true
```

流程：

```text
materialize parent revision
    ↓
Agent 修改和测试
    ↓
Harness scope validation
    ↓
Harness commit
    ↓
child.result_revision
```

### 8.2 Feedback Evaluation

```yaml
phase: feedback_eval
input_revision: candidate.result_revision
capabilities:
  repo_writable: false
  artifacts_writable: true
  scratch_writable: true
```

流程：

```text
fresh sandbox from result_revision
    ↓
run D_feedback
    ↓
collect metrics
    ↓
assert source unchanged
    ↓
destroy sandbox
```

### 8.3 Pareto Evaluation

与 feedback 使用另一个 execution ID 和 artifact root：

```text
exec_feedback/artifacts ≠ exec_pareto/artifacts
```

不能依赖 feedback execution 留下的可变文件。

---

## 9. Apptainer 隔离设计

### 9.1 固定容器路径

```text
/workspace/repo
/workspace/artifacts
/workspace/scratch
/home/agent
```

只有 Sandbox 后端知道对应宿主路径。

### 9.2 推荐 bind

Implementation：

```text
host repo      → /workspace/repo:rw
host artifacts → /workspace/artifacts:rw
host scratch   → /workspace/scratch:rw
host home      → /home/agent:rw
```

Evaluation：

```text
host repo      → /workspace/repo:ro
host artifacts → /workspace/artifacts:rw
host scratch   → /workspace/scratch:rw
host home      → /home/agent:rw
```

禁止：

```text
controller .git common directory → container:rw
```

### 9.3 Git 策略

优先方案：Sandbox 内代码树可包含独立 `.git`，但只影响当前临时 clone；最终 commit 仍由 Harness 统一创建。

更严格方案：Agent 看到不含可写 Git 控制面的代码树，Harness 在宿主侧对 diff 建立 commit。

第一版选择哪种都可以，但必须保证 Agent 无法修改 controller repo 和其他 candidate 的 refs/worktree metadata。

---

## 10. Store 与持久化

### 10.1 CandidateStore

```python
save(card)
get(candidate_id)
list_by_round(round_id)
list_children(parent_candidate_id)
```

### 10.2 ExecutionStore

```python
append(record)
get(execution_id)
list_for_candidate(candidate_id)
list_active()
list_by_phase(candidate_id, phase)
```

ExecutionStore 不覆盖历史。

### 10.3 ArtifactStore

```python
put(execution_id, kind, file_path) -> ArtifactRef
verify(artifact_ref)
open(artifact_ref)
```

### 10.4 第一版存储实现

可先采用：

```text
JSON files + append-only JSONL index + filesystem artifacts
```

不必立即引入数据库。

后续 v1.1 Event Store 可以围绕这些对象产生事件，而不是取代它们的领域含义。

---

## 11. 状态转换

### 11.1 Candidate 状态机

```text
GENERATED
    ↓ admission passed
ADMITTED
    ↓ implementation scheduled
MATERIALIZING
    ├── implementation failed → IMPLEMENTATION_FAILED
    └── result revision created → MATERIALIZED
        ↓ evaluation scheduled
EVALUATING
    ├── evaluation failed → EVALUATION_FAILED
    └── evaluation complete → EVALUATED
        ├── gate/judge accept → ACCEPTED
        └── gate/judge reject → REJECTED
```

### 11.2 Execution 状态机

```text
PENDING
    ↓
PREPARING
    ↓
RUNNING
    ↓
COLLECTING
    ├── SUCCEEDED
    ├── FAILED
    └── CANCELLED
```

### 11.3 允许的重试

重试不复活旧 ExecutionRecord，而创建新 execution：

```text
exec_001 failed
    ↓ retry policy
exec_002 created with same candidate + phase + input revision
```

两者都保留，便于归因和后续 v1.1 replay。

---

## 12. Resume 与恢复语义

### 12.1 不恢复旧 Sandbox

resume 时不尝试恢复旧容器、旧 worktree 或旧 shell。

根据持久对象判断：

```text
Candidate has no result_revision
    → 重新创建 implementation execution

Candidate has result_revision but no feedback success
    → 创建 feedback execution

Feedback passed but no pareto success
    → 创建 pareto execution
```

### 12.2 中断中的 Execution

启动时扫描：

```text
status in {PREPARING, RUNNING, COLLECTING}
```

若对应进程已经不存在：

```text
标记为 FAILED / INTERRUPTED
创建新的 retry execution
```

不要假装继续使用残留工作区。

### 12.3 Artifact 与 Revision 验证

resume 前验证：

- `result_revision` 可解析
- 必须 artifacts 的 hash 正确
- evaluation key 是否仍匹配
- environment/evaluator version 是否变化

---

## 13. 从当前实现迁移

本节按当前代码职责给出迁移方向，具体文件名可随仓库调整。

### 13.1 `execution/workspace.py`

当前职责应拆分：

```text
worktree prepare
controller protection
workspace reuse
clean checks
```

迁移为：

```text
RepositoryMaterializer
SandboxFactory
GitResultService
CleanupService
```

最终不再保留 Candidate 级长期 worktree manager。

### 13.2 `execution/runtime_backend.py`

收缩为 Sandbox 后端：

```text
LocalSandbox
ApptainerSandbox
```

只暴露 execute/read/write/cleanup 等统一接口，不向上层泄漏 bind 拼接和 host/guest 路径转换。

### 13.3 `agents/adapters.py`

将 Candidate 调度、legacy canonical record 和 agent 调用拆开：

```text
CandidateScheduler
ExecutionService
RunnerAdapter
```

Adapter 只负责把 ExecutionSpec 转换为 Runner 请求，再把 Runner 输出转换为领域结果。

### 13.4 Legacy registry shape

替换：

```text
candidate-keyed workspace records
candidate-keyed execution records
```

为：

```text
CandidateStore[candidate_id]
ExecutionStore[execution_id]
ArtifactStore[artifact_id]
```

### 13.5 `orchestrator.py`

Orchestrator 只保留业务流程：

```text
select parent
propose child
admit candidate
schedule implementation
gate feedback
schedule pareto
judge
update pool
```

不再直接处理：

```text
worktree path
Apptainer bind
Git common directory
subprocess cwd
cleanup chmod
```

---

## 14. 推荐实施阶段

### Phase A：冻结基线

目标：确保重构只改变执行底座，不改变优化算法行为。

任务：

1. 固定一个小型任务仓库和 baseline revision。
2. 固定 seed、两个 child、feedback gate、pareto evaluation。
3. 保存每个 Proposal、diff、commit、metric、Judge 结果和 frontier。
4. 增加一条父代产生两个子代的测试路径。

交付：

```text
legacy_smoke_fixture/
expected_candidate_lineage.json
expected_metrics.json
expected_frontier.json
```

### Phase B：建立领域对象

任务：

1. 实现 CandidateCard。
2. 实现 RevisionRef。
3. 实现 ExecutionSpec / ExecutionRecord。
4. 实现 ArtifactRef。
5. 实现 TypedExecutionFailure。
6. 编写序列化和不变量测试。

此阶段暂不替换旧 runtime。

### Phase C：建立 Store

任务：

1. CandidateStore。
2. ExecutionStore。
3. ArtifactStore。
4. 按 execution ID 追加记录。
5. 支持查询 Candidate 的全部 execution。

### Phase D：实现 Ephemeral Sandbox

任务：

1. Sandbox protocol。
2. LocalSandbox。
3. ApptainerSandbox。
4. RepositoryMaterializer。
5. execution-scoped repo/artifacts/scratch/home。
6. context manager cleanup。

先让一个独立 Execution 能从指定 SHA 启动并销毁。

### Phase E：宿主侧 Git 交付

任务：

1. 起始 revision 验证。
2. diff 收集。
3. scope/forbidden path 校验。
4. deterministic validation。
5. Harness commit。
6. result revision 记录。
7. commit failure rollback。

### Phase F：接回现有 Executor

任务：

1. 旧 Executor 作为 RunnerAdapter 使用。
2. 不修改当前 prompt 和输出思想。
3. implementation 使用 writable sandbox。
4. feedback / pareto 使用独立 read-only sandbox。
5. CandidateCard 追加 execution refs。

### Phase G：替换旧 Scheduler/Registry

任务：

1. CandidateScheduler 根据状态生成 ExecutionSpec。
2. ExecutionStore 取代 candidate-keyed legacy execution record。
3. 删除 workspace reuse。
4. 删除 candidate-level shared artifacts。
5. 删除 Git common directory bind。
6. 删除 controller chmod 保护。

### Phase H：故障与恢复

任务：

1. timeout。
2. cancel。
3. sandbox prepare failure。
4. artifact collection failure。
5. commit failure。
6. cleanup retry。
7. interrupted execution resume。

### Phase I：删除遗留设计

仅在新 smoke loop 全部通过后删除：

- persistent worktree lifecycle
- legacy canonical record flag
- legacy materialize-once / evaluate-only workspace reuse
- candidate workspace record
- candidate-scoped mutable artifacts
- controller repo chmod protection
- shared Git common directory RW bind

---

## 15. 端到端伪代码

```python
def run_candidate(candidate: CandidateCard) -> CandidateCard:
    impl_spec = scheduler.make_implementation(candidate)
    impl_record = execution_service.execute(impl_spec)
    candidate.execution_ids.append(impl_record.execution_id)

    if impl_record.status != ExecutionStatus.SUCCEEDED:
        candidate.status = CandidateStatus.IMPLEMENTATION_FAILED
        candidate_store.save(candidate)
        return candidate

    candidate.result_revision = impl_record.result_revision
    candidate.status = CandidateStatus.MATERIALIZED
    candidate_store.save(candidate)

    feedback_spec = scheduler.make_feedback_eval(candidate)
    feedback_record = execution_service.execute(feedback_spec)
    candidate.execution_ids.append(feedback_record.execution_id)

    if feedback_record.status != ExecutionStatus.SUCCEEDED:
        candidate.status = CandidateStatus.EVALUATION_FAILED
        candidate_store.save(candidate)
        return candidate

    if not feedback_gate.accept(feedback_record.metrics):
        candidate.status = CandidateStatus.REJECTED
        candidate_store.save(candidate)
        return candidate

    pareto_spec = scheduler.make_pareto_eval(candidate)
    pareto_record = execution_service.execute(pareto_spec)
    candidate.execution_ids.append(pareto_record.execution_id)

    if pareto_record.status != ExecutionStatus.SUCCEEDED:
        candidate.status = CandidateStatus.EVALUATION_FAILED
        candidate_store.save(candidate)
        return candidate

    judgment = judge.compare(
        parent_revision=get_parent_revision(candidate),
        candidate_revision=candidate.result_revision,
        feedback=feedback_record,
        pareto=pareto_record,
    )

    candidate.judgment_ids.append(judgment.judgment_id)
    candidate.status = (
        CandidateStatus.ACCEPTED
        if judgment.accepted
        else CandidateStatus.REJECTED
    )
    candidate_store.save(candidate)
    return candidate
```

父子生成：

```python
def make_child(parent: CandidateCard, proposal: ProposalIdea) -> CandidateCard:
    assert parent.result_revision is not None

    return CandidateCard(
        candidate_id=id_factory.new_candidate_id(),
        round_id=parent.round_id + 1,
        parent_candidate_ids=(parent.candidate_id,),
        proposal_id=proposal.proposal_id,
        proposal=proposal,
        base_revision=parent.result_revision,
    )
```

---

## 16. 测试清单

### 16.1 对象测试

- Candidate Card 创建后身份字段不可变。
- result_revision 只能由成功 implementation 设置。
- execution_ids 追加而非覆盖。
- ExecutionSpec 序列化稳定。
- Artifact hash 验证失败时拒绝读取。

### 16.2 父子继承测试

- 父 Sandbox 删除后，子代可从父 result revision 创建。
- 子代代码初始内容与父 revision 完全一致。
- 子 result revision 的 parent 是父 result revision。
- 子代不包含父代未提交文件。
- 一个父代同时产生两个子代不会串写。

### 16.3 隔离测试

- Candidate A 无法读取 Candidate B 的 writable artifacts。
- evaluation 无法修改 repo。
- Agent 无法修改 controller Git refs。
- feedback 与 pareto 使用不同 artifact root。
- execution HOME、scratch、process 独立。

### 16.4 生命周期测试

- 成功后 tmp repo 删除。
- Agent 失败后 tmp repo 删除。
- 超时后子进程全部终止。
- cancel 后记录为 CANCELLED。
- cleanup 失败被单独记录并可重试。

### 16.5 恢复测试

- 删除整个 `tmp/` 后可以 resume。
- 中断中的 Execution 被标记为 interrupted/failed。
- Candidate 有 result revision 时不会重复 implementation。
- 缺 feedback 时只补 feedback。
- evaluation key 相同可复用，变化后重新执行。

### 16.6 行为回归测试

- Proposal 数量与 legacy 基线一致。
- gate 结果一致。
- Judge 输入语义一致。
- Pareto frontier 一致。
- candidate 局部失败不会终止其他 candidate。

---

## 17. 验收标准

本阶段完成必须同时满足：

1. Candidate Card 中不存在 workspace、container、PID 或 branch 生命周期字段。
2. 所有 execution 以 execution ID 独立记录。
3. implementation、feedback、pareto 使用独立 Sandbox 和 artifact root。
4. 父代 Sandbox 删除后，子代仍从父 result revision 继续优化。
5. 同父多子可并行运行且不互相污染。
6. evaluation repo 强制只读。
7. Agent 不具有 controller Git common directory 的写权限。
8. Harness 统一完成 diff、scope validation 和 commit。
9. Sandbox 成功、失败、超时、取消后均能清理。
10. 删除所有临时目录后仍能查询、重放和继续调度。
11. 当前 gate、Judge、Pareto 业务行为保持回归一致。
12. 旧的长期 worktree、legacy canonical record 和 candidate 级共享 artifacts 已不再参与主流程。

---

## 18. 与后续路线的衔接

### 18.1 进入 v1.1

v1.1 可直接围绕以下对象建立协议和事件：

```text
CandidateCreated
ExecutionScheduled
ExecutionStarted
ExecutionSucceeded
ExecutionFailed
RevisionProduced
EvaluationCompleted
CandidateAccepted
CandidateRejected
```

AgentGateway 输出只需要落到明确的 Execution 与 Candidate contract，不再携带 workspace 隐式状态。

### 18.2 进入 v1.2

Global Context Plane 的主要实体可以稳定定义为：

```text
Candidate
Proposal
Revision
Execution
Artifact
Judgment
GateDecision
```

临时 Sandbox 不进入全局知识实体，只保留必要的 execution metadata。

### 18.3 进入 v2.0

Executor 内部扩展：

```text
Execution Episode
├── Planner → ExecutionPlan
├── Runner → ExecutionAttempt
└── Critic → CritiqueDecision
```

多次 attempt 可以共享同一次 implementation Execution 的 Sandbox；当该 Execution 结束后仍然销毁。最终只提交一个 `ExecutionSubmission` 和 `result_revision`。

这时职责关系为：

```text
Optimizer 创建 Candidate Card
Planner 创建 Plan
Runner 修改当前 Execution Sandbox
Critic 决定 continue / replan / submit / abort
Harness 创建 immutable Revision
Judge 比较 parent revision 与 child revision
Optimizer 学习 Judgment
```

---

## 19. 最终设计约束

本次重构应始终遵守三句话：

> Candidate Card 是候选方案的持久业务档案，不是 workspace 容器。

> Candidate 拥有 Execution 历史，Execution 临时租用 Sandbox。

> 父子代继承 Revision、知识和显式证据，不继承旧 Sandbox 与隐式运行状态。
