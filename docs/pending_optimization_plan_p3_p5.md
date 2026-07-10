# GEPA 框架优化执行计划 P3-P5

## 📋 执行概述

**已完成任务：** P0-P2 全部完成
- ✅ P0: parent_id 删除修复（16 处引用，55/55 测试通过）
- ✅ P1: 显示和日志功能优化（-41 行代码，55/55 测试通过）
- ✅ P2: 提取 RunStore 统一存储抽象（+250 行抽象，-100 行代码，55/55 测试通过）

**待执行任务：** P3-P5
- 🔄 P3: 拆分 Orchestrator 为 LoopEngine（高风险，高收益）
- 🔄 P4: 精简测试结构（中风险，中收益）
- 🔄 P5: Schema 进一步精简（中风险，中收益）

---

## 🎯 P3：拆分 Orchestrator 为 LoopEngine

### 目标

提取 `LoopEngine` 类，专注于 GEPA 算法和状态转换，使主 loop 简化到 30 行以内。

### 当前问题

**orchestrator.py (690 行) 仍然是 God Object：**

```python
class ResearchOrchestrator:
    def __init__(self, config, config_path, components):
        # 12 个依赖
        self.config = config
        self.config_path = config_path
        self.run_dir = self._resolve_run_dir(config, config_path)
        self.usage_tracker = UsageTracker(...)
        self.store = RunStore(self.run_dir)
        self.registry = ExecutionRegistry(self.run_dir)
        self.workspace_manager = WorkspaceManager(self.run_dir, config)
        self.provenance = ProvenanceVerifier()
        self.admission = CandidateAdmissionGate()
        self.dataset_split = resolve_dataset_split(config)
        self.prior_context = load_prior_context(config, config_path.parent)
        self.proposer, self.executor, self.judger = components
        self.gate = GEPAGate()
        self.pareto = ParetoSelector()

    def run(self) -> LoopState:
        # 108 行：整个 GEPA loop 控制逻辑
        # 混合了配置、状态、调度、日志等职责
        ...

    def run_generation(self, round_id: int, state: LoopState) -> GenerationDecision:
        # 148 行：单轮 GEPA 执行逻辑
        # 混合了提议、评估、决策、持久化等职责
        ...
```

**问题：**
- ❌ 职责过多（配置、状态、调度、日志、存储）
- ❌ 代码过长，难以理解和维护
- ❌ 难以测试特定逻辑
- ❌ 扩展困难

### 目标架构

#### 创建 LoopEngine 类

**职责：** 专注于 GEPA 算法和状态转换

```python
class LoopEngine:
    """GEPA loop 算法引擎，专注于状态转换逻辑。"""

    def __init__(
        self,
        gate: GEPAGate,
        pareto: ParetoSelector,
        admission: CandidateAdmissionGate,
    ):
        self.gate = gate
        self.pareto = pareto
        self.admission = admission

    def initialize(
        self,
        state: LoopState,
        seeds: list[Candidate],
        seed_results: list[tuple[Trace, Judgment]],
    ) -> LoopState:
        """初始化 GEPA loop，处理 seeds。"""
        ...

    def plan_generation(
        self,
        state: LoopState,
        pool: CandidatePool,
        matrix: ScoreMatrix,
        frontier: ParetoFrontier,
        parents: list[Candidate],
        max_parents: int,
    ) -> Generation:
        """规划下一轮生成。"""
        ...

    def apply_generation_results(
        self,
        state: LoopState,
        generation: Generation,
        results: GenerationResults,
    ) -> LoopState:
        """应用生成结果，更新状态。"""
        ...

    def should_stop(self, state: LoopState) -> bool:
        """判断是否应该停止。"""
        ...

class Generation:
    """单轮生成的完整上下文。"""
    round_id: int
    candidates: list[Candidate]
    parents: list[Candidate]
    feedback_candidates: list[Candidate]
    feedback_results: list[tuple[Trace, Judgment]]
    pareto_candidates: list[Candidate]
    pareto_results: list[tuple[Trace, Judgment]]
    ...

class GenerationResults:
    """单轮生成的完整结果。"""
    trace_batch: TraceBatch
    feedback_judgment_batch: JudgmentBatch
    pareto_judgment_batch: JudgmentBatch
    gate_decision: GateDecision
    decision: GenerationDecision
    ...
```

#### 简化的 Orchestrator

**职责：** 专注于流程协调和组件管理

```python
class ResearchOrchestrator:
    """GEPA loop 协调器，专注于流程控制。"""

    def __init__(self, config: dict[str, Any], config_path: Path, components: tuple | None = None):
        # 组件管理
        self.config = config
        self.config_path = config_path
        self.run_dir = self._resolve_run_dir(config, config_path)
        self.proposer, self.executor, self.judger = self._build_components(components)

        # 工具服务
        self.store = RunStore(self.run_dir)
        self.registry = ExecutionRegistry(self.run_dir)
        self.workspace_manager = WorkspaceManager(self.run_dir, config)
        self.provenance = ProvenanceVerifier()

        # 核心引擎
        self.loop_engine = LoopEngine(
            gate=GEPAGate(),
            pareto=ParetoSelector(),
            admission=CandidateAdmissionGate(),
        )

        # 运行时数据
        self.usage_tracker = UsageTracker(self.run_dir, config.get("usage_tracking", {}))
        self.dataset_split = resolve_dataset_split(config)
        self.prior_context = load_prior_context(config, config_path.parent)

    def run(self) -> LoopState:
        """简化的主 loop（目标：30 行）。"""
        # 准备
        controller_snapshot = self.workspace_manager.controller_snapshot()
        self._assert_run_dir_reusable()
        state = self.store.load_or_create_state(self.config["task"]["name"], self.config.get("resume", False))
        self._save_runtime_metadata()

        # 初始化（如果需要）
        if not state.initialized:
            self._initialize_loop(state)

        # 主循环（目标：15-20 行）
        max_rounds = int(self.config["budget"]["max_rounds"])
        for round_id in range(state.round_id, max_rounds):
            # 规划生成
            generation = self.loop_engine.plan_generation(
                state,
                pool,
                matrix,
                frontier,
                parents,
                max_parents,
            )

            # 执行生成
            results = self._execute_generation(generation)

            # 应用结果
            state = self.loop_engine.apply_generation_results(state, generation, results)

            # 持久化
            self._persist_generation(generation, results, state)

            # 检查停止条件
            if self.loop_engine.should_stop(state):
                break

        # 完成
        self._write_final_report(state)
        return state
```

### 实施步骤

#### 步骤 1：创建数据模型（30 分钟）

```python
# loop.py (新文件)
@dataclass
class Generation:
    """单轮生成的完整上下文。"""
    round_id: int
    candidates: list[Candidate]
    parents: list[Candidate]
    config: dict[str, Any]
    status: str = "planned"

@dataclass
class GenerationResults:
    """单轮生成的完整结果。"""
    generation: Generation
    trace_batch: TraceBatch
    feedback_judgment_batch: JudgmentBatch
    pareto_judgment_batch: JudgmentBatch
    gate_decision: GateDecision
    decision: GenerationDecision
```

#### 步骤 2：创建 LoopEngine（2-3 小时）

```python
# loop.py (继续)

class LoopEngine:
    """GEPA loop 算法引擎。"""

    def __init__(
        self,
        gate: GEPAGate,
        pareto: ParetoSelector,
        admission: CandidateAdmissionGate,
    ):
        self.gate = gate
        self.pareto = pareto
        self.admission = admission

    def initialize(
        self,
        state: LoopState,
        seeds: list[Candidate],
        seed_results: list[tuple[Trace, Judgment]],
    ) -> LoopState:
        """初始化 GEPA loop。"""
        # 实现 initialization 逻辑
        # 返回更新后的 state
        ...

    def plan_generation(
        self,
        state: LoopState,
        pool: CandidatePool,
        matrix: ScoreMatrix,
        frontier: ParetoFrontier,
        parents: list[Candidate],
        max_parents: int,
    ) -> Generation:
        """规划下一轮生成。"""
        # 实现 run_generation 中的规划逻辑
        # 返回 Generation 对象
        ...

    def should_stop(self, state: LoopState) -> bool:
        """判断是否应该停止。"""
        # 实现 _generation_decision_from_gate 中的停止逻辑
        ...
```

#### 步骤 3：重构 orchestrator 主 loop（1-2 小时）

```python
# orchestrator.py

def run(self) -> LoopState:
    """简化的主 loop（目标：30 行）。"""
    # 准备（5 行）
    controller_snapshot = self.workspace_manager.controller_snapshot()
    self._assert_run_dir_reusable()
    state = self.store.load_or_create_state(self.config["task"]["name"], self.config.get("resume", False))
    self._save_runtime_metadata()

    # 初始化（5 行）
    if not state.initialized:
        self._initialize_loop(state)

    # 主循环（15-20 行）
    max_rounds = int(self.config["budget"]["max_rounds"])
    for round_id in range(state.round_id, max_rounds):
        # 规划、执行、应用、持久化、检查停止
        ...

    # 完成（5 行）
    self._write_final_report(state)
    return state
```

#### 步骤 4：迁移方法到 LoopEngine（2-3 小时）

```python
# 将以下方法从 orchestrator 迁移到 loop_engine：
# - _initialize_pool_if_needed -> LoopEngine.initialize
# - _config_with_gepa_context -> 保留在 orchestrator（配置相关）
# - _attach_parent_context -> 保留在 orchestrator（配置相关）
# - _admit_candidates -> 保留在 orchestrator（需要多个服务）
# - _apply_gate_decision -> LoopEngine.apply_generation_results
# - _generation_decision_from_gate -> LoopEngine.should_stop

# 将以下方法保留在 orchestrator（流程相关）：
# - run (重构后)
# - _initialize_loop (流程协调)
# - _execute_generation (执行协调)
# - _persist_generation (持久化协调)
# - _save_runtime_metadata (配置保存)
# - _write_final_report (报告生成)
```

#### 步骤 5：运行测试验证（1 小时）

```bash
# 运行完整测试套件
python -m pytest tests/ -v

# 重点测试：
# - tests/test_gepa_mini_flow.py (GEPA loop 完整流程)
# - tests/test_p0_safety.py (安全测试)
# - tests/test_smoke.py (冒烟测试)
```

#### 步骤 6：文档更新（30 分钟）

```python
# 更新以下文档：
# - README.md (架构说明)
# - CLAUDE.md (开发指南)
# - docs/p3_completion_report.md (完成报告)
```

### 预期效果

| 指标 | 之前 | 之后 | 改进 |
|------|------|------|------|
| **主 loop 行数** | 108 行 | 30 行 | -72% |
| **orchestrator.py** | 690 行 | 400 行 | -42% |
| **loop.py** | 0 行 | 300 行 | +300 行 |
| **职责分离** | 混合 | 清晰 | ✅ 显著改善 |
| **测试难度** | 高 | 低 | ✅ 易于测试 |

### 风险评估

| 风险 | 级别 | 缓解措施 |
|------|------|----------|
| **核心逻辑重构** | 高 | 完整测试验证，分步实施 |
| **状态同步问题** | 高 | 使用明确的数据结构传递状态 |
| **测试失败** | 中 | 保持原有测试，只调整导入 |
| **功能影响** | 中 | 保留所有现有接口 |

### 验收标准

- ✅ 主 loop 在 30 行以内
- ✅ 所有测试通过（55/55）
- ✅ GEPA loop 功能完全保持
- ✅ Resume 功能正常
- ✅ 代码可读性显著提升

---

## 🧪 P4：精简测试结构

### 目标

从 8 个测试文件精简到 5 个，删除重复测试，提高测试质量。

### 当前问题

**8 个测试文件，约 500 行测试代码：**

```
tests/
├── _fakes.py                    # 测试辅助工具
├── test_agent_client.py         # Agent 客户端测试 (3)
├── test_agent_components.py     # Agent 组件测试 (6)
├── test_context_views.py         # 上下文视图测试 (4)
├── test_gepa_mini_flow.py        # GEPA mini flow 测试 (8)
├── test_gepa_upgrade.py         # 升级兼容性测试 (6)
├── test_p0_safety.py            # P0 安全测试 (11)
└── test_smoke.py                # 冒烟测试 (6)
```

**问题：**
- ❌ 测试重复覆盖相同流程
- ❌ 测试大量内部实现细节
- ❌ 没有清晰的测试分层
- ❌ 测试代码接近"第二套框架"

### 目标测试结构

```
tests/
├── _fakes.py                    # 测试辅助工具
├── test_loop_contract.py         # ✅ 核心 loop 行为测试
├── test_policy.py               # ✅ Gate 和 admission 策略测试
├── test_workspace_provenance.py  # ✅ Workspace 和 provenance 安全测试
├── test_agent_protocol.py        # ✅ Agent 协议测试
└── test_store_resume.py         # ✅ 存储和恢复测试
```

### 实施步骤

#### 步骤 1：创建 test_loop_contract.py（2-3 小时）

```python
# 核心 loop 行为测试
class LoopContractTest(unittest.TestCase):
    """测试 GEPA loop 的核心行为。"""

    def test_initialization_creates_seeds_and_pool(self):
        """测试初始化创建 seeds 和 pool。"""
        # 使用 Fake proposer/executor/judger
        # 验证：
        # - seeds 正确创建
        # - pool 正确初始化
        # - matrix 正确构建
        # - frontier 正确选择
        ...

    def test_generation_executes_complete_cycle(self):
        """测试单轮完整执行周期。"""
        # 验证：
        # - parent 选择
        # - proposal 生成
        # - admission gate
        # - feedback 评估
        # - gate 决策
        # - 状态更新
        ...

    def test_pareto_gate_selects_frontier(self):
        """测试 Pareto gate 选择前沿。"""
        # 验证：
        # - frontier 选择正确
        # - 多目标最优保留
        # - 非前沿候选被丢弃
        ...

    def test_stop_conditions_work(self):
        """测试停止条件。"""
        # 验证：
        # - max_rounds 停止
        # - pass_threshold 停止
        # - patience 停止
        ...

    def test_resume_preserves_state(self):
        """测试 resume 恢复状态。"""
        # 验证：
        # - state 正确加载
        # - 不重复执行已完成的候选
        # - 从正确轮次继续
        ...
```

#### 步骤 2：创建 test_policy.py（1-2 小时）

```python
# Gate 和 admission 策略测试
class PolicyTest(unittest.TestCase):
    """测试 GEPA 策略逻辑。"""

    def test_admission_rejects_frozen_targets(self):
        """测试 admission 拒绝 frozen 目标。"""
        ...

    def test_admission_rejects_duplicate_fingerprints(self):
        """测试 admission 拒绝重复指纹。"""
        ...

    def test_admission_rejects_unaccepted_parents(self):
        """测试 admission 拒绝未接受的父代。"""
        ...

    def test_feedback_gate_requires_improvement(self):
        """测试 feedback gate 需要改进。"""
        ...

    def test_pareto_gate_accepts_task_best(self):
        """测试 Pareto gate 接受任务最优。"""
        ...

    def test_gate_fail_closed_on_missing_parent(self):
        """测试 gate 在缺少父代时 fail closed。"""
        ...

    def test_provenance_fail_blocks_acceptance(self):
        """测试 provenance 失败阻止接受。"""
        ...
```

#### 步骤 3：合并现有测试到新结构（2-3 小时）

```python
# 合并策略：
# test_workspace_provenance.py ← test_p0_safety.py (保留相关测试)
# test_agent_protocol.py ← test_agent_client.py + test_agent_components.py (简化)
# test_store_resume.py ← 新创建 (基于现有 resume 测试)
# test_loop_contract.py ← test_gepa_mini_flow.py + test_gepa_upgrade.py (核心逻辑)
# test_policy.py ← 新创建 (策略逻辑)

# 删除：
# - test_smoke.py (功能已覆盖到新测试)
# - test_context_views.py (合并到其他测试)
```

#### 步骤 4：删除冗余测试（30 分钟）

```python
# 删除以下测试（功能已被覆盖）：
# - test_smoke.py (6 个测试)
# - test_context_views.py (4 个测试中的部分冗余)

# 删除文件：
rm tests/test_smoke.py
```

#### 步骤 5：运行测试验证（30 分钟）

```bash
# 运行新测试结构
python -m pytest tests/ -v

# 验证：
# - 5 个测试文件
# - 20-30 个关键测试
# - 100% 通过率
```

#### 步骤 6：文档更新（30 分钟）

```python
# 更新测试文档
# - README.md (测试说明)
# - docs/p4_completion_report.md (完成报告)
```

### 预期效果

| 指标 | 之前 | 之后 | 改进 |
|------|------|------|------|
| **测试文件数** | 8 | 5 | -38% |
| **测试数量** | 55 | 25-30 | -45% |
| **测试代码行数** | 500 | 200-300 | -50% |
| **测试覆盖率** | 覆盖实现细节 | 覆盖关键行为 | ✅ 质量提升 |
| **测试速度** | 2.1s | ~1s | ✅ 更快 |

### 风险评估

| 风险 | 级别 | 缓解措施 |
|------|------|----------|
| **测试覆盖下降** | 中 | 保留关键行为测试，确保覆盖 |
| **漏测关键 bug** | 中 | 运行完整集成测试验证 |
| **测试失败** | 低 | 逐步合并，每步验证 |

### 验收标准

- ✅ 5 个测试文件
- ✅ 25-30 个关键测试
- ✅ 覆盖所有关键行为
- ✅ 测试全部通过
- ✅ 测试速度提升

---

## 📝 P5：Schema 进一步精简

### 目标

清理混合职责的 schema，分离 proposal 和运行时状态。

### 当前问题

**Candidate schema 混合了多种职责：**

```python
@dataclass
class Candidate:
    # 核心 proposal 字段（✅ 保留）
    candidate_id: str
    round_id: int
    parent_ids: list[str]
    generation: int
    hypothesis: str
    proposed_change: str
    rationale: str
    expected_improvement: str
    risk: str
    strategy: str
    target_files: list[str]
    executor_contract: dict[str, Any]
    expected_artifacts: list[str]

    # 元数据（✅ 保留）
    prompt_text: str
    created_at: str
    mutation_note: str
    merge_note: str
    safety_class: str
    expected_gain: float | None

    # 运行时状态（❌ 移到 CandidateRecord）
    status: str = "generated"
    admission_status: str = "pending"
    admission_decision_id: str | None

    # Artifacts（❌ 移到单独存储）
    artifacts: dict[str, Any] = field(default_factory=dict)
```

### 目标 Schema

#### 清理后的 Candidate

```python
@dataclass
class Candidate:
    """研究提案，只包含提案内容，不包含运行时状态。"""
    # 核心标识
    candidate_id: str
    round_id: int
    parent_ids: list[str]
    generation: int

    # 提案内容
    hypothesis: str
    scope: str
    proposed_change: str
    rationale: str
    expected_improvement: str
    risk: str

    # 执行指导
    strategy: str
    target_files: list[str]
    executor_contract: dict[str, Any]
    expected_artifacts: list[str]

    # 元数据
    prompt_text: str = ""
    created_at: str = ""
    mutation_note: str = ""
    merge_note: str = ""
    safety_class: str = ""
    expected_gain: float | None = None

    # ❌ 移除运行时状态字段
    # ❌ 移除 artifacts 字段
```

#### 新增 CandidateRecord

```python
@dataclass
class CandidateRecord:
    """候选运行时记录，不包含提案内容。"""
    candidate_id: str
    round_id: int

    # 运行时状态
    status: str  # "generated", "admitted", "discarded", "provenance_failed", "accepted"

    # Admission 状态
    admission_status: str
    admission_decision_id: str | None

    # 执行记录
    execution_record: ExecutionRecord | None
```

### 实施步骤

#### 步骤 1：创建 CandidateRecord（30 分钟）

```python
# schemas.py

@dataclass
class CandidateRecord:
    """候选运行时记录，管理运行时状态。"""
    candidate_id: str
    round_id: int
    status: str  # "generated", "admitted", "discarded", "provenance_failed", "accepted"
    admission_status: str = "pending"
    admission_decision_id: str | None = None
    execution_record: ExecutionRecord | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CandidateRecord":
        return cls(**data)
```

#### 步骤 2：清理 Candidate schema（30 分钟）

```python
# schemas.py

@dataclass
class Candidate:
    """研究提案，只包含提案内容。"""
    candidate_id: str
    round_id: int
    parent_ids: list[str] = field(default_factory=list)
    generation: int = 0

    # 提案内容
    hypothesis: str
    scope: str
    proposed_change: str
    rationale: str
    expected_improvement: str
    risk: str

    # 执行指导
    strategy: str = ""
    target_files: list[str] = field(default_factory=list)
    executor_contract: dict[str, Any] = field(default_factory=dict)
    expected_artifacts: list[str] = field(default_factory=list)

    # 元数据
    prompt_text: str = ""
    created_at: str = ""
    mutation_note: str = ""
    merge_note: str = ""
    safety_class: str = ""
    expected_gain: float | None = None

    # ❌ 移除运行时状态字段
    # status: str = "generated"
    # admission_status: str = "pending"
    # admission_decision_id: str | None

    # ❌ 移除 artifacts 字段
    # artifacts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Candidate":
        # 兼容旧数据，忽略已删除字段
        return cls(
            candidate_id=data["candidate_id"],
            round_id=data["round_id"],
            parent_ids=data.get("parent_ids", []),
            generation=data.get("generation", 0),
            hypothesis=data["hypothesis"],
            scope=data.get("scope", "task_system"),
            proposed_change=data["proposed_change"],
            rationale=data["rationale"],
            expected_improvement=data.get("expected_improvement", ""),
            risk=data.get("risk", ""),
            strategy=data.get("strategy", ""),
            target_files=data.get("target_files", []),
            executor_contract=dict(data.get("executor_contract", {})),
            expected_artifacts=data.get("expected_artifacts", []),
            prompt_text=data.get("prompt_text", ""),
            created_at=data.get("created_at", ""),
            mutation_note=data.get("mutation_note", ""),
            merge_note=data.get("merge_note", ""),
            safety_class=data.get("safety_class", ""),
            expected_gain=data.get("expected_gain"),
        )
```

#### 步骤 3：更新使用 Candidate 的代码（2-3 小时）

```python
# 更新以下文件中的 Candidate 使用：
# - gepa_researcher/adapters.py (3 处)
# - gepa_researcher/agent_components.py (2 处)
# - gepa_researcher/context_views.py (1 处)
# - gepa_researcher/admission.py (1 处)
# - gepa_researcher/gate.py (2 处)
# - gepa_researcher/pool.py (1 处)
# - tests/*.py (8 个测试文件)

# 迁移策略：
# 1. 移除对 candidate.status 的引用
# 2. 移除对 candidate.admission_status 的引用
# 3. 移除对 candidate.artifacts 的引用
# 4. 使用 CandidateRecord 管理运行时状态
```

#### 步骤 4：更新 CandidatePool 使用 CandidateRecord（1-2 小时）

```python
# pool.py

class CandidatePool:
    def __init__(self):
        self.active = {}  # candidate_id -> Candidate
        self.accepted = {}  # candidate_id -> Candidate
        self.discarded = {}  # candidate_id -> (Candidate, reason)
        self.records = {}  # candidate_id -> CandidateRecord (新增)

    def add_accepted(self, candidate: Candidate) -> None:
        self.active[candidate.candidate_id] = candidate
        self.accepted[candidate.candidate_id] = candidate
        self.records[candidate.candidate_id] = CandidateRecord(
            candidate_id=candidate.candidate_id,
            round_id=candidate.round_id,
            status="accepted",
        )

    def mark_candidate_status(self, candidate_id: str, status: str) -> None:
        if candidate_id in self.records:
            self.records[candidate_id].status = status
        # ... 其他逻辑
```

#### 步骤 5：运行测试验证（1 小时）

```bash
# 运行完整测试套件
python -m pytest tests/ -v

# 重点测试：
# - tests/test_parent_id_cleanup.py (schema 清理测试)
# - tests/test_gepa_mini_flow.py (schema 使用测试)
# - tests/test_p0_safety.py (安全测试)
```

#### 步骤 6：文档更新（30 分钟）

```python
# 更新文档：
# - README.md (schema 说明)
# - docs/p5_completion_report.md (完成报告)
```

### 预期效果

| 指标 | 之前 | 之后 | 改进 |
|------|------|------|------|
| **Candidate 字段数** | 19 | 18 | -1 |
| **职责混合** | 是 | 否 | ✅ 明确分离 |
| **状态管理** | 混合 | 独立 | ✅ 职责清晰 |
| **数据流** | 混乱 | 清晰 | ✅ 易于理解 |

### 风险评估

| 风险 | 级别 | 缓解措施 |
|------|------|----------|
| **向后兼容** | 中 | Candidate.from_dict 兼容旧数据 |
| **状态丢失** | 低 | CandidateRecord 管理状态 |
| **测试失败** | 低 | 分步迁移，逐步验证 |

### 验收标准

- ✅ Candidate 只包含 proposal 字段
- ✅ CandidateRecord 管理运行时状态
- ✅ 所有测试通过
- ✅ 向后兼容性保持

---

## 📊 P3-P5 总体效果预估

### 代码质量提升

| 方面 | 当前状态 | P3-P5 后 | 改进 |
|------|----------|----------|------|
| **orchestrator.py** | 690 行 | 400 行 | -42% |
| **测试文件数** | 8 个 | 5 个 | -38% |
| **测试代码行数** | 500 行 | 250 行 | -50% |
| **职责分离** | 混合 | 清晰 | ✅ 显著改善 |
| **可维护性** | 中等 | 高 | ✅ 显著提升 |
| **可测试性** | 困难 | 容易 | ✅ 显著提升 |

### 架构改进

| 方面 | 当前状态 | P3-P5 后 |
|------|----------|----------|
| **主 loop** | 108 行 | 30 行 |
| **核心引擎** | 无 | LoopEngine |
| **状态管理** | 混合 | 分离 |
| **存储抽象** | ✅ 完成 | ✅ 完成 |
| **测试分层** | 无 | 5 层清晰 |
| **Schema 清晰** | 混合 | 明确分离 |

---

## 🚧 实施顺序建议

### 推荐顺序：P5 → P4 → P3

**原因：**
1. **P5 风险最低** - Schema 清理，向后兼容，影响范围小
2. **P4 风险中等** - 测试重构，容易回滚
3. **P3 风险最高** - 核心重构，需要稳定基础

### 备选顺序：P3 → P4 → P5

**原因：**
1. **P3 收益最大** - 拆分 Orchestrator，架构改善最大
2. **依赖关系** - P4 和 P5 可能需要 P3 提供的新结构
3. **一步到位** - 避免重复迁移

---

## 📋 验收总结

### 总体目标

- ✅ 主 loop 在 30 行以内
- ✅ Orchestrator 减少到 400 行以内
- ✅ 5 个测试文件
- ✅ Schema 职责清晰分离
- ✅ 所有测试通过
- ✅ GEPA loop 功能完整保持

### 关键指标

| 指标 | 目标 | 当前 | 剩余 |
|------|------|------|------|
| **代码行数减少** | > 200 行 | 191 行 (P0-P2) | ~200 行 (P3-P5) |
| **测试文件数** | 5 个 | 8 个 | 3 个 |
| **架构清晰度** | 高 | 中 | 高 |
| **可维护性** | 高 | 中 | 高 |

---

**文档创建时间：** 2026-07-10
**任务范围：** P3-P5
**预计总工作量：** 8-12 天
**当前状态：** 待执行