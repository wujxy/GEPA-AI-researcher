# GEPA-AI-researcher 与 OMILREC 自动优化实验阶段报告

> 面向组会的介绍性与总结性报告  
> 状态截至：2026-07-10 12:39（UTC+8）

## 一、摘要

GEPA-AI-researcher 的第一版目标，是探索一种面向科研与工程优化任务的全自动闭环：系统不再依赖人工逐轮提出优化方案，而是由模型自动生成 proposal、自动执行实验、自动评估结果，并利用上一轮的执行轨迹和反馈继续提出下一轮 proposal。

本阶段选择 OMILREC 性能优化作为首个真实复杂任务。原因是 OMILREC 同时具备明确的性能目标、严格的数值与物理正确性门禁、较长的构建测试链条，以及大量已有优化经验，适合检验自动科研 loop 是否能够在真实工程约束下稳定工作。

目前的主要结论是：

- GEPA 的 proposal—execution—evaluation—feedback 闭环已经跑通；
- loop 能够连续运行、保存轨迹、处理失败，并基于反馈继续生成候选；
- OMILREC 的构建、数值验证、端到端测试、多线程测试和性能测试已经被纳入自动 executor；
- proposal 已表现出一定的自迭代能力，能够从失败中提取“哪些方向无收益、哪些方向仍待验证”；
- 但截至当前，尚未发现超过测量噪声的性能提升，前两轮没有 child 优于初始 seed；
- 当前系统只能称为“流程层面基本自动”，还不能称为“结果可信、可长期无人值守的全自动优化系统”；
- 第一次 OMILREC 实测暴露出工作区隔离、候选硬约束和实验归属等 P0 问题，同时暴露出性能测量、证据管理、超时恢复和 proposal 质量等 P1/P2 问题。

因此，第一阶段的价值不仅是寻找 OMILREC 新优化，更重要的是用一个真实任务找出了 GEPA 从原型走向可靠自动科研基础设施所需补齐的关键能力。

## 二、GEPA-AI-researcher 要解决什么问题

### 2.1 传统优化流程中的人工瓶颈

复杂科研软件的优化通常依赖如下人工循环：

1. 人工阅读代码、profiling 和历史报告；
2. 人工提出一个优化假设；
3. 人工修改代码并运行实验；
4. 人工判断速度、正确性和副作用；
5. 人工记录成功与失败，再决定下一步。

执行测试本身可以脚本化，但“下一步试什么”仍高度依赖人的持续参与。随着项目进入深水区，容易出现重复尝试已失败方向、实验记录不完整、不同人的判断标准不一致，以及大量时间消耗在低价值候选上。

GEPA-AI-researcher 希望把这一流程变成一个有边界、可审计的自动闭环：

```text
自动提出假设
  -> 自动执行实验
  -> 自动评估与归因
  -> 保留完整轨迹
  -> 基于反馈提出下一代假设
```

核心目标不是简单地“调用多个 agent”，而是让 proposal 能够由真实实验结果驱动，自我修正并持续迭代。

### 2.2 第一版 GEPA 的目标

第一版聚焦两个目标：

1. **全自动闭环**：在预先给定目标、资源、预算和安全约束后，系统能够自动完成 proposal、execution、evaluation 和下一轮调度；
2. **Proposal 自迭代**：下一代 proposal 不只是重新随机生成，而是读取父候选的执行轨迹、失败原因和 judge 反馈，形成有针对性的反思式 mutation。

第一版并不追求无限运行，也不追求模型训练或大规模树搜索。当前更关注闭环是否可运行、过程是否可追踪、失败是否能沉淀为下一轮知识，以及自动执行是否足够可靠。

## 三、GEPA Loop 架构简介

### 3.1 核心角色

当前 loop 包含四个主要角色：

| 角色 | 主要职责 |
|---|---|
| Proposer | 根据任务目标、历史候选、执行轨迹和 judge 反馈提出下一批 proposal |
| Executor | 在真实代码和运行环境中实现 proposal，执行构建、测试与性能测量 |
| Judger | 根据 executor 的结构化结果判断正确性、性能收益、失败类型和后续建议 |
| Gater | 比较 child 与 parent，更新候选池、Pareto frontier 和停止条件 |

Orchestrator 负责串联这些角色，并持久化 candidate、trace、judgment、score matrix、frontier 和 generation history。

### 3.2 当前迭代流程

```text
任务目标与先验知识
  -> 初始化 seed candidate
  -> 在评价集上建立 seed 分数
  -> 从 Pareto frontier 选择 parent
  -> Proposer 生成一批反思式 mutation
  -> Executor 在反馈样本上执行候选
  -> Judger 生成分数与可行动反馈
  -> Gate 判断 child 是否优于 parent
  -> 优胜者进入完整评价并更新 frontier
  -> 反馈进入下一代 proposal
```

当前实验设置每轮生成 3 个候选，最多并发 3 个 executor，最多运行 6 轮；连续若干轮没有提升后自动停止。

### 3.3 与普通 Agent Loop 的差异

GEPA 的重点不是让 agent 持续自由探索，而是建立“候选—证据—评分—选择”的演化关系：

- 每个 proposal 都应是一个可证伪的小假设；
- 每个结论都应来自实际执行轨迹；
- 失败结果也应进入记忆，减少后续重复尝试；
- 只有比 parent 更好的候选才能进入下一代；
- loop 必须受预算、门禁和停止条件约束。

## 四、当前 GEPA-AI-researcher 的稳定性

### 4.1 已经验证的能力

当前 orchestrator 已能够：

- 连续运行多个 generation；
- 保存 proposal、executor trace、judgment 和 frontier；
- 并行调度多个 candidate；
- 在单个 executor 失败时继续处理其他候选；
- 根据分数判断 candidate 是否改进 parent；
- 在没有改进时保留现有最佳候选；
- 将失败原因和 judge 建议注入下一轮 proposer；
- 使用最大轮数和 no-improvement patience 控制运行边界。

从“控制流是否跑通”的角度看，第一版闭环已经成立。

### 4.2 当前稳定性的限制

从“实验结果是否完全可信”的角度看，系统仍处于原型阶段：

- 并行 executor 共享 Git 仓库和构建运行目录，存在相互污染；
- 部分 executor 超过 2400 秒后超时；
- 同一轮中观察到 Git state drift，需要重新 checkout 才能继续 benchmark；
- candidate、branch、commit 和测试产物之间的对应关系不够稳定；
- 性能测试存在较明显波动，且并行运行可能产生资源竞争；
- executor 返回的结构化结果尚缺少 orchestrator 侧的独立证据复核；
- 长流程失败后缺少细粒度 checkpoint 和可靠恢复。

因此，当前系统的稳定性可以概括为：

> Loop 控制层已经基本稳定，实验执行层和证据可信层仍需重点加强。

## 五、Proposal 的迭代性

### 5.1 自迭代机制已经出现

当前 proposer 会读取：

- 父候选的 proposal；
- executor 的成功或失败轨迹；
- 性能和正确性指标；
- judge 给出的失败分类和下一步建议；
- seeds 中记录的已尝试和已否定方向。

例如，O2 将全量 bulk 距离计算改成稀疏索引计算后没有获得速度收益，反馈明确指出：减少循环次数不一定更快，标量 gather 可能破坏连续访存和编译器向量化。随后 proposal 转向了不依赖该机制的 O5 inline，以及其他 pure code motion 或 precompute 方向。

这说明 proposal 已不是完全独立的重复采样，而是在利用实验反馈进行 mutation。

### 5.2 当前迭代质量仍不充分

当前 proposal 自迭代仍暴露出明显问题：

- judge 已将 O2 标记为 retired，但后续反馈仍可能再次推荐 O2；
- O5 已证明 GCC Release 优化会自动 inline，但相关建议仍可能重复出现；
- RecNpe cache 候选基于错误代码理解，目标计算在当前版本中已被历史优化消除；
- 为填满固定 batch，proposer 会生成成熟度不足或风险类别不匹配的候选；
- 某些 proposal 的修改范围与本轮 frozen-file 约束冲突；
- 当前反馈主要是自然语言，缺少结构化的“已验证、已否定、未测试、可重试”知识状态。

因此，当前 proposal 已具备“反思式迭代形式”，但还没有达到“稳定累积知识并持续提升 proposal 质量”的目标。

更准确的判断是：

> Proposal 自迭代机制已经跑通，但知识一致性、去重、代码事实核验和候选准入仍不足。

## 六、OMILREC-OPT 流程简介

OMILREC 是 JUNO 顶点与能量重建算法。其优化不是单纯追求更短运行时间，还必须维持严格的数值和物理一致性。

一次标准候选验证大致包括：

1. 从指定基线或已接受 parent 创建候选；
2. 实现一个独立、范围受控的优化；
3. Release 构建并形成可追踪提交；
4. 执行 FCN 数值漂移测试；
5. 检查相邻提交的 drift ratchet；
6. 多次运行 100-event benchmark；
7. 运行端到端顶点和能量一致性测试；
8. 运行单线程与多线程一致性测试；
9. 根据性能收益、测量噪声和全部正确性门禁决定接受或否定。

主要门禁包括：

| 项目 | 要求 |
|---|---|
| FCN 数值漂移 | 相对误差不超过 `1e-13` |
| 端到端重建 | 顶点偏差不超过 4 mm，能量偏差不超过 7 keV |
| 多线程 | 排序后 bit-identical |
| 性能保护 | 不允许超过 5% 的回退 |
| 性能结论 | 100 events、多次重复，提升需超过测量噪声 |

这套流程为 GEPA 提供了比较清晰的自动评价基础：正确性门禁决定候选是否安全，性能指标决定候选是否真正优于 parent。

## 七、GEPA 在 OMILREC-OPT 上的首次应用

### 7.1 自动化程度

当前系统已经自动完成：

- 从先验文档和 seeds 中生成 proposal；
- 修改 OMILREC 代码；
- 构建和提交候选；
- 运行 FCN、drift、E2E、MT 和 benchmark；
- 生成结构化执行结果；
- 自动 judge 和 gate；
- 将反馈交给下一轮 proposer；
- 在无提升时继续下一 generation。

从人工参与角度看，当前 loop 可以在启动后持续运行，不需要人为逐个批准 proposal 或手动启动测试，已经具备较高流程自动化。

但“无人干预运行”不等同于“可靠全自动”。由于并行隔离和 provenance 尚不完善，目前仍需要人检查结果是否受到共享工作区、Git 状态或 benchmark 并发的影响。

### 7.2 Proposal 是否带来提升

截至当前，尚未出现超过测量噪声的速度提升。

| 候选 | 结果 | 结论 |
|---|---|---|
| O2：限制 QTMLE `vd_dstn` 到 time-eligible PMT | 正确性通过；多次测量分别表现为回退或噪声内无变化 | 安全但无稳定收益，retired-no-gain |
| O5：显式 inline RecHelper 插值函数 | 正确性通过；约 +1.12%，处于约 2% 测量波动内 | GCC 已自动 inline，无可测收益 |
| RecNpe bin/cache | 未修改代码即提前终止 | Proposal 基于过时代码假设，目标工作已被历史优化消除 |
| dy/index pure code motion | Executor 超时 | 仍属未测试，不能判定优化方向失败 |
| Diagnostic baseline | 177.92 vs 177.7 ms/event | 验证基本测试链可运行，但暴露 Git state drift |

当前 Pareto frontier 仍是初始 `seed_000`，分数为 0.45；前两个正式 generation 均没有 child 改进 parent，`no_improvement_rounds=2`。Loop 已进入下一轮 proposal 生成。

需要注意：由于当前并行 executor 隔离不足，这些数字适合用于阶段性方向判断，但在形成正式性能结论前应在隔离环境中复测。

### 7.3 首次实验的积极结果

虽然尚未找到新速度提升，首次实验仍产生了重要价值：

- 验证了复杂工程门禁可以被自动 agent 调用；
- 验证了失败候选能够自动进入 judge 和下一轮反馈；
- 识别出部分“理论上少计算但实际上更慢”的方向；
- 发现编译器已经自动完成的优化，避免后续重复投入；
- 发现 seeds 与代码当前状态之间可能存在知识过期；
- 暴露了 GEPA 运行时在真实重型任务上的关键基础设施缺陷。

## 八、第一次 OMILREC 实测暴露出的痛点

## 8.1 P0：必须优先解决的可信性问题

### P0-1 并行 Executor 没有完全隔离

多个 executor 实际共享同一个 OMILREC Git 工作树，以及 `build`、`InstallArea`、`TEMP` 和全局 metrics 文件。这可能导致：

- 一个候选切换分支影响另一个候选；
- 构建产物和源代码不属于同一提交；
- 测试加载了其他候选生成的动态库；
- benchmark 结果写入同一文件并相互混淆；
- 一个 executor 的失败污染其他 executor。

改进方向：为每个 candidate 创建独立 Git worktree，并隔离 build、install、TEMP、日志和 metrics；只允许只读共享大型 fixture 和外部数据。

### P0-2 候选约束只有 Prompt，没有前置准入 Gate

当前允许路径、frozen files、安全类别和“一次一个优化”等规则主要写在 prompt 中。模型仍可能提出修改 frozen 文件、风险类别不匹配或自相矛盾的 proposal，并直接进入昂贵 executor。

改进方向：在 proposal 后、executor 前增加确定性的 admission gate，检查 proposal schema、目标路径、frozen files、安全类别、修改规模、历史重复和强制验证计划。不合格 proposal 保留为反馈，但不分配执行资源。

### P0-3 Candidate、Branch、Commit 和 Artifact 归属不清

首次运行中出现 branch 名与实际提交内容不一致、共享仓库 detached HEAD、候选执行中 Git state drift 等现象。若归属错误进入 frontier，会导致下一代从错误 parent commit 继续 stacking。

改进方向：由 orchestrator 独占 branch/worktree 生命周期；建立 candidate registry，记录 parent SHA、start SHA、result SHA、changed files、binary hash 和 artifact manifest；只有通过独立 provenance verifier 的候选才能进入 judge。

## 8.2 P1：影响评价质量和计算效率的问题

### P1-1 Benchmark 资源竞争和噪声

多个 executor 并行 benchmark 会争用 CPU、内存带宽、文件系统和数据缓存。当前不同重复之间可出现约 2% 甚至更大的波动，使小优化难以可靠排序。

改进方向：build 和 correctness test 可并行，但正式 benchmark 应使用全局串行锁、固定 CPU affinity 和相同 machine tag；parent/child 可采用交错测量降低时间漂移。

### P1-2 全局 Ledger 被多个 Executor 直接写入

多个 executor 会直接追加 `speed.csv`、`drift.csv` 和 `seeds.md`，容易产生候选归属错误、重复记录或并发写入。

改进方向：executor 只写 candidate-local artifact；候选完成并验证后，由 orchestrator 单线程汇总到全局 ledger。

### P1-3 Executor 结果缺少独立证据复核

当前 judge 主要读取 executor 自报的结构化 JSON。尚未充分独立确认测试是否实际运行、结果属于哪个 commit、三次 benchmark 是否完整，以及测试 binary 是否与候选一致。

改进方向：增加 orchestrator-side evidence verifier，直接检查 Git、日志、退出码、CSV、binary hash 和 artifact 完整性。

### P1-4 “通过门禁”和“优化成功”的语义混淆

一个候选即使性能回退，只要没有超过 5% guard，仍可能得到 `validation.passed=true`。这容易把“安全但无收益”理解为“成功优化”。

改进方向：分别记录 correctness passed、performance guard passed 和 objective improved；只有性能提升超过噪声的候选才能成为性能 parent。

### P1-5 重复执行 Parent 浪费资源

Feedback 阶段可能重复执行已经完整验证过的 parent，包括较长的 E2E 和多线程测试。

改进方向：缓存不可变的正确性结果；只有环境变化或需要同时间窗性能对照时才重测 parent，且性能重测不必重复全部正确性门禁。

### P1-6 Proposal 的事实核验和质量控制不足

部分 proposal 基于错误或过时的代码理解；固定 batch size 也会促使 proposer 为填满数量而生成成熟度不足的候选。

改进方向：proposal 前增加轻量代码事实核验和历史 outcome 检索；允许高质量候选不足时减少 batch；对 exploratory 和 build-tuning 候选使用单独队列。

## 8.3 P2：影响长期运行和可维护性的问题

### P2-1 总体超时不适合长验证链

完整流程包括修改、构建、FCN、benchmark、E2E 和多线程测试，单次 2400 秒总超时容易留下半完成状态，也无法定位究竟在哪一阶段超时。

改进方向：使用阶段级 timeout、心跳和 checkpoint；超时后清理整个进程组，并允许从最近成功阶段恢复。

### P2-2 Resume 与状态恢复不足

长时间 loop 若因节点、agent 或环境问题中断，当前恢复机制容易重复执行、重复写 ledger 或混入旧 run_dir。

改进方向：引入显式 run ID 和原子状态机，按 `proposed → admitted → executing → verified → judged` 恢复，避免重复工作。

### P2-3 日志时间与可观测性不统一

系统进程时间和主日志可能使用不同时间基准；当前主要依赖长文本日志追踪，很难快速看到每个 candidate 所处阶段和资源占用。

改进方向：统一带时区的 ISO-8601 时间；增加结构化事件日志和简洁 dashboard，显示 candidate 状态、PID、耗时、门禁和失败原因。

### P2-4 自然语言记忆容易产生矛盾

Judge 反馈、seeds 和 proposer prompt 中可能同时存在互相冲突的方向，例如一个方向已退休但又被推荐为下一目标。

改进方向：建立结构化 idea registry，明确 `open`、`accepted`、`refuted`、`retired-no-gain`、`untested` 和 `retryable-failure` 状态，并以 registry 作为 proposal 去重和选择的事实源。

## 九、下一阶段改进路线

建议按“先保证可信，再提升效率，最后增强智能”的顺序推进。

### 第一阶段：修复 P0，建立可信执行底座

1. Candidate registry 和不可变 parent/result SHA；
2. Proposal admission gate；
3. 每 candidate 独立 Git worktree；
4. 独立 build、InstallArea、TEMP 和 metrics；
5. 执行前后 provenance verifier；
6. 只有 verified candidate 才能进入 judge 和 frontier。

### 第二阶段：修复 P1，提高实验质量与资源效率

1. Benchmark 串行调度与 CPU 隔离；
2. Candidate-local artifact 和全局单线程汇总；
3. Parent 结果缓存；
4. 拆分正确性通过、性能保护通过和目标提升；
5. Proposal 代码事实核验、去重和风险分层。

### 第三阶段：修复 P2，支持长期无人值守

1. 阶段级 timeout、checkpoint 和 resume；
2. 原子 candidate 状态机；
3. 统一结构化日志与监控；
4. 结构化 idea registry 和长期反馈记忆；
5. 在稳定底座上再扩展 merge、多目标 Pareto 和更丰富的 proposal 搜索。

## 十、阶段性结论

第一版 GEPA-AI-researcher 已经证明：在给定目标、工具和门禁后，模型可以自动提出 OMILREC 优化、修改代码、完成较重的验证流程、判断候选是否提升，并将失败经验反馈到下一轮。这说明“全自动科研优化 loop”和“proposal 自迭代”在流程上是可行的。

但首次真实任务也表明，自动化系统的关键不只是 agent 能否完成命令，而是能否保证每个 proposal、代码提交、构建产物、测试结果和性能数字之间具有可靠、可审计的对应关系。目前尚未获得明确的 OMILREC 性能提升，proposal 的反馈利用也存在重复和事实偏差。

因此，当前阶段应将目标从“继续增加更多 agent 能力”转向“建立可信的实验基础设施”。完成 P0/P1 修复后，再评价 proposal 自迭代是否真正提高命中率和最终性能，结论会更可靠。

## 十一、汇报要点

组会汇报可归纳为五句话：

1. GEPA-AI-researcher 要解决的是科研优化中“下一步试什么”持续依赖人工的问题；
2. 第一版已经跑通 proposal、execution、judgment、gate 和反馈再生成的全自动闭环；
3. OMILREC 首次实测证明复杂构建与正确性门禁可以自动执行，但尚未找到可测速度提升；
4. Proposal 已能根据失败做反思式 mutation，但知识一致性和事实核验仍不足；
5. 当前最优先工作是修复 executor 隔离、候选前置 gate 和实验 provenance，之后再提升 benchmark、恢复机制和 proposal 智能。
