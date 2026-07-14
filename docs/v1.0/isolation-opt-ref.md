# GEPA Isolation and Provenance Design Reference

## 背景

本文件整理 `br101_gepa_clean/full_loop_br101.log` 暴露出的 GEPA loop 失败原因，并给出面向下一版架构的隔离与 provenance 优化建议。

这次失败的直接表现是：初始化阶段生成的三个 seed candidate 均完成了 executor 执行，但全部被判定为 `provenance_failed`，最终 candidate pool 为空，orchestrator 抛出：

```text
RuntimeError: all seed candidates failed provenance verification - no valid seeds available for mutation
```

从设计角度看，这不是单纯的某个候选优化失败，而是当前隔离模型、executor 职责边界、provenance 硬门槛之间的责任划分不清。

## 当前失败链路

日志显示三个 seed 的代码实现和部分验证并非完全失败：

- seed 候选均从指定 baseline worktree 启动。
- `start_sha`、`ancestry`、`commit_count`、`changed_files`、`branch` 等 source provenance 检查通过。
- 多数候选报告中显示 build 和 `test_fcn` 数值验证通过。
- `quick_bench.sh --evtmax 100` 因 JUNO 环境问题失败：`IndexError: Unable to find PC2ADC_THRES`。
- `metrics.primary` 因 benchmark 未成功而为 `None`。
- 每个 worktree 中 `benchmarks/drift.csv` 被验证流程修改后留在 dirty 状态。
- candidate 的 `expected_artifacts` 含有自然语言项，例如 `ms/evt`、`gate pass/fail results`，其中 `ms/evt` 被 provenance 逻辑按路径解释，导致 artifact missing。

最终 provenance 同时报出：

```text
DIRTY_SOURCE
EXPECTED_ARTIFACT_MISSING
```

JudgerAdapter 对任意 `provenance_failed` 采用硬拒绝策略，直接给 0 分。因此三个 seed 均未进入 active pool，后续 Pareto selection 和 mutation loop 没有机会启动。

## 根因判断

### Worktree 不是完整隔离边界

当前 GEPA 以 git worktree 作为 per-candidate workspace。这个设计能隔离源码分支，但不能隔离运行时副产物。

在 omilrec 任务中，executor 需要 build、运行测试、运行 benchmark、生成 ROOT/TEMP 文件、写入 benchmark CSV、触发 pytest cache。这些行为天然会产生大量任务运行副产物。把它们全部留在 git worktree 中，会迫使 provenance 在源码修改、验证产物、环境副作用之间做细粒度区分。

这导致 provenance 被迫承担过多职责：

- 判断源码血统是否可信。
- 判断工作区是否干净。
- 判断任务验证产物是否完整。
- 判断 benchmark 环境失败是否应拒绝候选。
- 将 executor 运行现场的杂质转化为候选失败。

这些职责混在一起后，任何非源码层面的污染都可能把候选打成不可进入 pool 的硬失败。

### GEPA core 不应内置任务特定 validation

omilrec 的 validation 包括 `test_fcn.exe`、`diff_drift.py`、`quick_bench.sh`、multithread consistency 等。这些都是 omilrec-opt 任务流程的一部分，不应成为 GEPA core 的独立内建层。

GEPA core 的通用职责应是：

```text
proposal -> isolated execution -> audited delivery -> judgment -> mutation
```

任务特定流程应由 executor 结合 task context 或 skill 完成。对 omilrec 来说，executor 应参考或调用 `omilrec-opt` workflow/skill，在隔离执行环境内完成实现、验证、benchmark 和结果汇总。

### Provenance 当前过度检查执行现场

provenance 应审计 executor 的交付物是否可信，而不是审计 sandbox 内部运行现场是否一尘不染。

当前 provenance 直接检查 git worktree dirty 状态，并把 generated validation artifacts 与 source pollution 放在同一级别。这在 worktree-only 隔离模型下很容易误伤。

更合理的做法是：executor 在隔离环境内产生任意运行副产物，最后只导出约定的交付物。provenance 对导出的 patch/report/artifacts 做审计。

## 建议的新分层

### 1. Orchestrator

作用：

- 管理 run 状态。
- 调度 proposer、executor、provenance、judger。
- 维护 candidate pool、score matrix、Pareto frontier。
- 不理解任务特定 validation 细节。

不应负责：

- 直接执行 omilrec validation 脚本。
- 解释 omilrec drift、ROOT 输出、benchmark CSV 的任务语义。
- 因任务运行副产物污染而直接拒绝 candidate。

### 2. Proposer

作用：

- 生成 proposal candidate。
- 指定 hypothesis、target files、risk、expected improvement。
- 给 executor 提供任务执行意图。

建议：

- `expected_artifacts` 不应混用自然语言和路径。
- 区分 `required_artifact_paths`、`optional_artifact_paths`、`reported_fields`。
- 例如 `ms/evt` 应是 metric field，而不是 artifact path。

### 3. Admission Gate

作用：

- 在执行前检查 candidate 是否允许尝试。
- 检查 schema、target files、frozen paths、safety class、strategy、重复度。

建议硬门槛：

- schema 缺失。
- target files 越界。
- 修改 frozen paths。
- safety class 或 strategy 不在允许列表。

Admission Gate 应保护执行前边界，不负责判断执行后结果好坏。

### 4. Isolated Execution Workspace

建议从 worktree-only 升级为 sandbox/apptainer 级隔离。

输入：

- proposal candidate JSON。
- parent/base source snapshot 或 git reference。
- task context。
- task skill，例如 omilrec-opt workflow。
- executor runtime config。

执行：

- 在 sandbox/apptainer 内复制或挂载源码。
- executor 在隔离环境中实现 candidate。
- executor 在隔离环境中运行任务专属 workflow。
- 所有 build、TEMP、pytest cache、benchmark output、CSV 修改都留在隔离环境内。

输出：

- source patch 或 result commit。
- structured executor report。
- logs。
- required evidence artifacts。
- optional diagnostics artifacts。

worktree 可以继续作为 source snapshot 的实现手段，但不应被视为完整运行时隔离区。

### 5. Executor

作用：

- 在隔离环境内实现 proposal。
- 调用任务专属 workflow 完成 validation 和 benchmark。
- 汇总任务结果。
- 交付结构化结果，而不是留下一个由 GEPA core 解释的脏 worktree。

示例交付协议：

```json
{
  "candidate_id": "seed_001",
  "base_sha": "...",
  "source_patch": "artifacts/source.patch",
  "result_commit": "...",
  "changed_files": [
    "OMILRECV2/src/omilrec_likelihood.cc"
  ],
  "task_result": {
    "status": "completed",
    "metric": {
      "name": "ms_per_event",
      "value": 162.3,
      "unit": "ms/evt"
    },
    "validation": {
      "passed": true,
      "checks": []
    },
    "diagnostics": []
  },
  "artifacts": {
    "logs": [],
    "reports": [],
    "binaries": []
  }
}
```

当 benchmark 环境失败时，应显式表达：

```json
{
  "task_result": {
    "status": "partial",
    "metric": {
      "name": "ms_per_event",
      "value": null,
      "reason": "metric_unavailable"
    },
    "infrastructure_errors": [
      "PC2ADC_THRES missing"
    ]
  }
}
```

这样 judger 可以区分 candidate 本身失败和 infrastructure failure。

### 6. Provenance

作用：

- 审计 executor 交付物是否可信。
- 确认输出 patch/commit 可追溯、可应用、未越界。
- 不审计 sandbox 内部运行现场的临时文件状态。

建议拆成两类：

#### Source Provenance

建议硬门槛：

- base/start SHA 不匹配。
- parent ancestry 不成立。
- patch 无法干净应用到 parent。
- 修改文件越过 admitted target files。
- 修改 frozen paths。
- commit count 超出策略。
- executor report 缺失。

#### Evidence Provenance

建议分级：

- required evidence 缺失：可硬拒或降级，取决于任务策略。
- optional evidence 缺失：warning。
- metric unavailable：交给 judger 评分，不直接作为 source provenance failure。
- validation log 缺失：降低 confidence。

Provenance 的核心对象应是导出的 patch/report/artifacts，而不是 sandbox 内部 dirty worktree。

### 7. Judger

作用：

- 对 provenance 通过或可评分的 executor report 打分。
- 使用任务结果、metric、validation、diagnostics、infrastructure errors 判断 candidate 价值。

建议：

- source provenance failed：不可评分，拒绝。
- task validation failed：可评分，通常低分。
- benchmark unavailable：可评分，但 confidence 降低。
- validation pass + metric unavailable：可进入 exploratory pool。
- validation pass + metric improved：高分。

Judger 不应把所有 provenance warning 统一变成 0 分。

### 8. Candidate Pool and Pareto Selection

作用：

- 保存可作为 parent 的 candidate。
- 根据 score matrix 和多目标信息选择 Pareto frontier。

建议：

- 初始化阶段不要因为 metric unavailable 全部清空 pool。
- 允许 `validation_pass_metric_unknown` 类型 candidate 进入 exploratory pool。
- Pareto score 中保留 failure category 和 confidence，避免把所有非理想结果压成 0。

## 硬门槛重设建议

| 检查项 | 建议所在层 | 建议硬度 |
| --- | --- | --- |
| proposal schema | Admission | 硬 |
| target files 越界 | Admission / Provenance | 硬 |
| frozen paths 修改 | Admission / Provenance | 硬 |
| sandbox/apptainer 创建失败 | Execution infra | 分类为 infra failure |
| executor 未交付 report | Provenance | 硬 |
| patch 无法应用到 parent | Provenance | 硬 |
| start/base SHA 不匹配 | Provenance | 硬 |
| parent ancestry 不成立 | Provenance | 硬 |
| commit count 超限 | Provenance | 中硬，可配置 |
| 编译失败 | Executor task result | 通常硬，但由任务策略决定 |
| task validation failed | Executor task result / Judger | 可评分或任务策略硬拒 |
| benchmark 环境失败 | Executor task result / Judger | 软，metric unavailable |
| sandbox 内 TEMP/cache/CSV 污染 | 不进入 GEPA core | 不检查 |
| required evidence artifact 缺失 | Evidence Provenance | 分级 |
| optional diagnostics 缺失 | Evidence Provenance | warning |

## 迁移路径

### 短期修复

- 修正 `expected_artifacts` 语义，避免把 `ms/evt` 这类 metric field 当成路径。
- 将 artifact schema 拆为 `required_artifact_paths`、`optional_artifact_paths`、`reported_fields`。
- 修复 generated tracked paths 匹配，确保 `benchmarks/drift.csv` 不会因路径解析问题被误判为 dirty source。
- 将 `provenance_failed` 细分为 `source_provenance_failed`、`evidence_missing`、`metric_unavailable`。
- JudgerAdapter 只对 source provenance failure 硬拒。

### 中期改造

- 引入 executor delivery protocol。
- executor 输出 patch/report/artifacts。
- provenance 审计导出的交付物，而不是直接审计执行后的 worktree。
- omilrec validation 全部收敛到 omilrec-opt workflow/skill 内，由 executor 调用。

### 长期改造

- 引入 sandbox/apptainer workspace backend。
- worktree 仅作为源码快照来源或 patch 应用目标。
- sandbox 内部运行副产物不暴露给 GEPA core。
- GEPA core 保持任务无关，只消费通用 executor report 和 judgment。

## 目标原则

最终设计应满足：

- GEPA core 任务无关。
- 任务专属 validation 属于 executor workflow。
- 运行时副产物被 sandbox 隔离。
- provenance 硬在交付物可信性，而不是执行现场洁净度。
- infrastructure failure 可被学习和记录，但不应污染 source provenance。
- candidate pool 能保留有学习价值的 partial result，避免初始化阶段因非候选原因全灭。

一句话总结：

```text
GEPA core should manage proposal -> isolated execution -> audited delivery -> judgment -> mutation.
Task validation belongs to the executor workflow.
Provenance should audit delivered evidence, not the cleanliness of the execution scene.
```
