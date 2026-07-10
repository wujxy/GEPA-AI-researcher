# OMILREC GEPA 优化 Loop：当前状态与 P0 风险

> 状态快照：2026-07-10 11:32（UTC+8）  
> 目标仓库：`/datafs/users/wujxy/agent-sci/omilrec_opt/omilrecv2`  
> GEPA 运行目录：`/datafs/users/wujxy/agent-sci/omilrec_opt/gepa_runs`  
> 主日志：`/datafs/users/wujxy/agent-sci/omilrec_opt/full_loop.log`

本文记录当前 OMILREC 自动优化 loop 的工作方式、已验证结果和三个必须优先解决的 P0 级工程风险。运行仍在继续，因此“当前进展”是带时间戳的观测快照，不代表本轮最终 judgment。

## 1. 优化目标与验证契约

OMILREC 从 PMT 电荷和时间信息重建事件顶点与能量。单事件依次执行四个拟合阶段：

| 阶段 | 拟合目标 | 主要数据 | 典型 FCN 调用数/事件 |
|---|---|---|---:|
| QMLE | 电荷顶点与能量 | charge、nPE map | ~120 |
| TMLE | 时间顶点 | hit time、time PDF | ~240 |
| QTMLE | 联合顶点 | charge + time | ~80 |
| ENERGY | 固定顶点后的能量重拟合 | charge、3D map | ~30 |

Minuit 每个事件约调用 `omilrec::calculate_ev_likelihood` 470 次。主要成本是 PMT 几何、nPE 插值、charge likelihood 和 time likelihood；约 80% 的事件耗时位于 FCN 及其相关拟合路径。

当前 loop 基线为 `main` 的 `2453e60`（v1.12.1）。主要优化目标是降低单事件重建时间，同时保持以下逐提交门禁：

| 门禁 | 阈值/要求 |
|---|---|
| FCN drift | 相对误差不超过 `1e-13` |
| Drift ratchet | 不允许相邻提交的数值漂移无解释地扩大 |
| E2E consistency | 顶点差不超过 4 mm，能量差不超过 7 keV |
| Multithread | 排序后 bit-identical |
| Speed guard | 相对基线回退不超过 5% |
| Speed verdict | 100 events、3 次重复，收益必须超过测量噪声 |

这里应区分三个概念：

1. `correctness_passed`：正确性和数值门禁通过；
2. `performance_guard_passed`：性能未超过允许的回退上限；
3. `objective_improved`：性能提升超过测量噪声，确实完成优化目标。

一个候选可以满足前两项但不满足第三项。

## 2. GEPA 自动化流程

当前自动化 loop 的逻辑为：

```text
Prior context + human goal
  -> seed candidate(s)
  -> D_pareto 初始评估
  -> Pareto frontier / score matrix
  -> 按 Pareto 胜出权重选择 parent
  -> proposer 反思并生成 mutation batch
  -> D_feedback minibatch 执行和评分
  -> child 是否改进 parent 的 gate
  -> D_pareto 完整评估
  -> 更新 candidate pool、score matrix 和 frontier
```

当前配置的关键参数：

- `max_rounds = 6`
- `no_improvement_patience = 3`
- `batch_size = 3`
- `max_workers = 3`
- `enable_merge = false`
- `per_candidate_workspace = true`
- 单次 agent 超时 2400 秒
- acceptance policy 为 `minibatch_improves_then_pareto`

Proposer、executor 和 judger 均通过 Claude Code agent 运行。GEPA orchestrator 负责保留 candidate、trace、judgment、score matrix 和 frontier。

## 3. 当前运行情况

### 3.1 Seed：O2 restrict QTMLE `vd_dstn`

Seed 候选尝试只对 `time_eligible_idx` 计算 QTMLE 的 `vd_dstn`，避免对所有 PMT 计算 time likelihood 所需距离。

执行结果：

| 指标 | 结果 |
|---|---:|
| Baseline | 177.70 ms/event |
| Candidate | 183.18 ms/event |
| 性能变化 | **+3.08% 回退** |
| Benchmark sigma | 0.28% |
| FCN 最大相对漂移 | `6.613e-15` |
| E2E | 通过 |
| Multithread | 通过 |
| GEPA score | 0.45 |

对应实现提交最初记录为 `0ad0ce0`。该候选属于 `green-no-gain`：正确性门禁全部通过，性能回退也没有超过 5% guard，但回退显著高于测量噪声，因此没有完成速度优化目标，不应成为默认 stacking parent。

主要经验是：减少循环元素数不一定更快。标量间接索引 gather 破坏了 `bulk::vd_dstn` 的连续访存和编译器向量化，少算一部分 PMT 仍可能慢于全量 bulk loop。

### 3.2 Round 000

Proposer 生成了三个 child：

| Candidate | 提案 | 初步风险判断 |
|---|---|---|
| `cand_000_000` | O5：内联 `RecHelper::*_at_bin` | 符合 safe source optimization 主线 |
| `cand_000_001` | 独立 PGO 实验 | 要求修改 frozen 的 `CMakeLists.txt`，与当前策略冲突 |
| `cand_000_002` | RecNpe bin/cache 探索 | 假设依赖 trial point，方案仍偏探索性 |

截至快照时间：

- orchestrator 仍在运行；
- `round_000` 处于 feedback execution 阶段；
- parent 和 child 被调度到并行 executor 池；
- parent 的 feedback trace 已生成，child 尚未形成完整 judgment；
- frontier 仍以 `seed_000` 为唯一已评分 parent；
- 运行中的结果存在下述共享工作区污染，当前 round 的性能数据需要在隔离机制完善后复核。

## 4. P0-1：并行 Executor 没有完全隔离

### 4.1 当前问题

配置虽然声明 `per_candidate_workspace: true`，但该配置目前只形成 candidate artifact 目录，没有为每个 executor 创建独立代码工作区。多个 Claude executor 的实际 cwd 都是：

```text
/datafs/users/wujxy/agent-sci/omilrec_opt/omilrecv2
```

因此它们共享：

- Git working tree、index、HEAD 和 branch；
- `build/`；
- `InstallArea/`；
- `TEMP/`；
- benchmark/test 输出；
- `benchmarks/speed.csv` 和 `benchmarks/drift.csv`；
- 可能存在固定文件名的 `/tmp`、ROOT、pytest 或 SNiPER 运行产物。

风险不仅是源码覆盖，还包括源码、动态库、测试 binary、benchmark 数字和 commit 之间失去一致性。

### 4.2 目标设计

每个 candidate 必须拥有独立 Git worktree 和全部可变运行状态：

```text
gepa_runs/worktrees/round_000/cand_000_000/
  repo/                 # 独立 git worktree
    build/
    InstallArea/
    TEMP/
  artifacts/
    execution.json
    speed.csv
    drift.csv
    logs/
```

由 orchestrator 创建 worktree：

```bash
git -C <source-repo> worktree add \
  -b gepa/<run-id>/round-000/cand-000-000 \
  <candidate-worktree>/repo \
  <verified-parent-sha>
```

Git object database可以共享；working tree、index、branch、build、install、TEMP 和 metrics 文件不得共享。

### 4.3 执行约束

- orchestrator 独占 worktree 和 branch 生命周期；
- executor 不允许执行 `git checkout`、`git switch` 或创建 worktree；
- executor cwd 必须是自己的 worktree；
- executor 只能写当前 worktree 和 candidate artifact 目录；
- `--add-dir` 不应给予整个主 workspace 可写权限；
- `CMAKE_BINARY_DIR`、`CMAKE_INSTALL_PREFIX` 和 TEMP 路径必须 candidate-local；
- 全局 fixture 可以只读共享；
- executor 不直接追加全局 `seeds.md`、`speed.csv` 或 `drift.csv`；
- candidate 被接受后，由 orchestrator 串行合并全局 ledger。

### 4.4 验收标准

1. 每个 executor 的 `/proc/<pid>/cwd` 位于其 worktree；
2. 每个 candidate 的 HEAD、branch 和 index 独立；
3. build、InstallArea、TEMP 和 metrics 文件均位于 candidate 目录；
4. 主仓库在 round 前后保持 clean 且 HEAD 不变；
5. A candidate 的 artifact 不包含 B candidate 的 worktree 路径或 binary；
6. 任一 executor 超时或失败不影响其他 executor；
7. 并行隔离集成测试可重复通过。

## 5. P0-2：候选约束只有 Prompt，没有前置 Gate

### 5.1 当前问题

当前配置通过自然语言要求：

- 只编辑 `OMILRECV2/src/*.cc|*.h`；
- 不修改 frozen files；
- 当前轮只接受 safe、bit-identical 优化；
- 一个 candidate 只实现一个 idea；
- 优先执行 seeds 中尚未 refute 的方向。

但 proposer 仍生成了 PGO 候选，并要求修改明确冻结的 `CMakeLists.txt`。该候选随后进入昂贵 executor 阶段，证明 prompt 不能作为硬约束。

### 5.2 建议增加确定性 Admission Gate

推荐流程：

```text
Proposer
  -> schema validation
  -> static policy gate
  -> parent/provenance gate
  -> novelty/known-failure gate
  -> cost/risk-class gate
  -> admitted candidate
  -> create worktree
  -> executor
```

该 gate 应是 orchestrator 的确定性程序逻辑，不应再依赖另一个 LLM 作唯一裁决。

### 5.3 前置 Gate 的检查项

#### Schema

Proposal 至少必须包含：

```json
{
  "candidate_id": "cand_000_000",
  "parent_candidate_id": "seed_000",
  "hypothesis": "...",
  "target_files": ["OMILRECV2/src/..."],
  "safety_class": "safe",
  "strategy": "safe-pattern #1",
  "proposed_change": "...",
  "expected_gain_ms_evt": 2.0,
  "validation_plan": [],
  "stop_conditions": []
}
```

#### 路径策略

- `target_files` 必须存在；
- 所有 target 必须匹配 allowlist；
- 任一 target 命中 frozen paths 立即拒绝；
- target file 与 executor contract 中的实际修改意图必须一致；
- 限制单 candidate 的最大文件数。

#### 安全与语义一致性

- `safety_class` 必须属于当前 run 允许集合；
- safe candidate 不得包含近似计算、精度降低或 FP 重排；
- “per-event invariant” 不得依赖 Minuit trial point；
- 禁止把多个编译选项或多个优化机制捆绑成一个 candidate；
- validation plan 必须包含当前 run 的全部强制门禁。

#### 重复与已知失败

逐步将 `seeds.md` outcome 结构化为 idea registry。相同 target、strategy 和机制已经 refuted，且没有新约束或新实现机制时，应直接拒绝重复执行。

#### 风险队列

建议至少区分：

- `safe-source`；
- `exploratory-source`；
- `build-tuning`；
- `algorithmic`；
- `external-compute`。

当前 OMILREC loop 可只开放 `safe-source`。PGO 应进入独立 build-tuning queue，不应与普通源码候选共享本轮 executor 池。

### 5.4 Gate 输出与验收标准

每个 proposal 必须生成可审计的 admission 记录：

```json
{
  "candidate_id": "cand_000_001",
  "admitted": false,
  "checks": {
    "schema": "pass",
    "paths": "fail",
    "safety": "fail",
    "parent": "pass",
    "novelty": "warning"
  },
  "failure_codes": [
    "FROZEN_PATH",
    "DISALLOWED_CANDIDATE_CLASS"
  ]
}
```

被拒绝的 proposal 仍进入 archive 和 proposer feedback，但不得创建 worktree、调用 executor 或消耗 benchmark 资源。

## 6. P0-3：Branch、Candidate 与 Commit 归属混乱

### 6.1 当前证据

共享工作树已经出现明确的归属错乱：

- O2 执行结果最初记录为提交 `0ad0ce0`；
- 后续观测到共享仓库进入 detached HEAD `6fd6b02`，其提交内容是 O5 inline；
- 名为 `opt-execute-O2-restrict-vd-dstn` 的分支却被推进到 PGO 提交 `e77cf99`；
- branch 名、candidate ID、提交内容和 executor contract 已不能可靠一一对应；
- 共享 `benchmarks/speed.csv` 同时处于修改状态。

这会直接破坏下一轮 stacking：即使 judge 选对了 candidate，orchestrator 仍可能把错误 SHA 作为下一轮 parent。

### 6.2 Provenance 数据模型

必须区分四个实体：

1. Candidate：优化提案；
2. Workspace/branch：执行容器；
3. Commit：实现结果；
4. Judgment：对某个已验证 commit 的评价。

建议记录：

```text
Candidate
  candidate_id
  parent_candidate_id
  requested_parent_sha
  branch_name
  worktree_path
  Execution
    execution_id
    actual_start_sha
    result_sha
    changed_files
    artifact_manifest
    provenance_status
```

Branch 名由 orchestrator 唯一生成，例如：

```text
gepa/<run-id>/round-000/cand-000-000
```

Proposer 只选择 `parent_candidate_id`，不应自由填写真实 `branch_from`。Orchestrator 从 registry 查询 parent 的 verified result SHA，并用它创建 worktree。

### 6.3 执行前验证

创建 worktree 后持久化：

```json
{
  "candidate_id": "cand_000_000",
  "requested_parent_sha": "2453e60...",
  "actual_start_sha": "2453e60...",
  "branch_name": "gepa/.../cand-000-000",
  "worktree_path": "...",
  "working_tree_clean": true
}
```

必须满足：

```text
actual_start_sha == requested_parent_sha
```

否则 candidate 立即终止，不能让 executor 自行切换或修复分支。

### 6.4 执行后独立验证

Executor 返回 JSON 后，orchestrator 必须独立检查：

1. `result_sha` 等于 worktree 的真实 HEAD；
2. `requested_parent_sha` 是 `result_sha` 的祖先；
3. commit 数量符合策略，通常应为一个；
4. changed files 与 proposal 声明及 allowlist 一致；
5. worktree clean；
6. commit message 包含 candidate ID；
7. artifact 中的 candidate ID、execution ID 和 commit SHA 一致；
8. 测试使用的 binary 和动态库来自当前 candidate worktree。

测试和 benchmark artifact 应绑定以下信息：

```json
{
  "candidate_id": "cand_000_000",
  "execution_id": "exec_<uuid>",
  "parent_sha": "2453e60...",
  "commit_sha": "abc123...",
  "binary_sha256": "...",
  "library_sha256": "...",
  "machine_tag": "...",
  "command": "...",
  "exit_code": 0
}
```

未通过 provenance verifier 的 candidate 不得进入 judge。

### 6.5 状态机与 Stacking 规则

推荐状态机：

```text
proposed
  -> rejected_pre_gate
  -> admitted
  -> workspace_ready
  -> executing
  -> execution_complete
  -> provenance_verified
  -> judged
  -> accepted | discarded
```

只有同时满足以下条件的 candidate 才能作为下一轮 stacking parent：

- `provenance_verified`；
- correctness gates 通过；
- `objective_improved = true`；
- judgment 状态为 `accepted`。

O2 这类 `green-no-gain` candidate 的提交应保留用于审计，但状态应是 `discarded_no_improvement`，后续 candidate 直接从上一个 accepted parent 创建新 worktree，不需要在共享主仓库执行 revert。

## 7. 三个 P0 风险的边界

| 风险 | 解决的问题 | 建议组件 |
|---|---|---|
| P0-1 Worktree 隔离 | 不同 executor 是否共享可变文件和运行状态 | Workspace manager |
| P0-2 前置 Gate | Proposal 是否有资格消耗 executor 和 benchmark 资源 | Candidate admission controller |
| P0-3 Provenance | Candidate、parent、branch、commit、binary 和 artifact 是否一一对应 | Execution registry + provenance verifier |

三项必须共同完成。只增加 worktree 不能阻止越界 proposal，也不能自动证明测试结果属于正确 commit；只增加前置 gate 也无法消除并行 build 和 Git 状态污染。

## 8. 推荐实施顺序

1. 引入 candidate registry 和不可变 ID/parent 数据模型；
2. 实现 proposal admission gate，先阻止新的越界执行；
3. 实现 orchestrator-owned Git worktree manager；
4. 将 build、InstallArea、TEMP 和 metrics 改为 candidate-local；
5. 实现执行前后 provenance verifier；
6. 只有 provenance verified 的结果才能进入 judger 和 frontier；
7. 增加双 candidate 并行隔离集成测试；
8. 在新机制下重新验证当前 `round_000` 候选的性能结果。

## 9. 相关文件

- `gepa_omilrec_config.json`：本次 loop 配置；
- `run_omilrec_gepa.sh`：启动脚本；
- `full_loop.log`：当前主日志；
- `gepa_runs/frontier.json`：当前 Pareto frontier；
- `gepa_runs/score_matrix.json`：评分矩阵；
- `gepa_runs/traces/`：candidate、execution 和 judgment trace；
- `NEUTRIX_OPT_EXECUTE_OMILREC_WORKFLOW.md`：OMILREC 优化与验证流程说明；
- `seeds.md`：优化方向与 outcome ledger。
