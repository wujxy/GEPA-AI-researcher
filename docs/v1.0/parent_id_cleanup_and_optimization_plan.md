# GEPA 代码重构优化执行计划

## 🔴 P0 任务：parent_id 删除修复和测试验证

### 问题分析

虽然您删除了 schema 中的 `parent_id` 字段，但代码中仍有 8 处引用 `candidate.parent_id`，这会导致运行时错误：

| 文件 | 行号 | 代码片段 | 严重程度 |
|------|------|----------|----------|
| `adapters.py` | 182 | `parent_candidate_id=candidate.parent_id` | 🔴 高 - 会崩溃 |
| `adapters.py` | 194 | `if candidate.parent_id:` | 🔴 高 - 会崩溃 |
| `adapters.py` | 197 | `raise RuntimeError(f"parent {candidate.parent_id} ...` | 🔴 高 - 会崩溃 |
| `adapters.py` | 204 | `parent_candidate_id=candidate.parent_id` | 🔴 高 - 会崩溃 |
| `agent_components.py` | 252 | `parent_id=state.best_candidate_id` | 🔴 高 - 会崩溃 |
| `agent_components.py` | 354 | `parent_id=state.best_candidate_id` | 🔴 高 - 会崩溃 |
| `agent_components.py` | 464 | `parent_candidate_id=candidate.parent_id` | 🔴 高 - 会崩溃 |
| `agent_components.py` | 555 | `parent_candidate_id=candidate.parent_id` | 🔴 高 - 会崩溃 |

### 修复策略

#### 1. 修复 adapters.py (4 处)

```python
# adapters.py:182
parent_candidate_id=candidate.parent_ids[0] if candidate.parent_ids else None

# adapters.py:194-197
if candidate.parent_ids:
    parent_id = candidate.parent_ids[0]  # 使用第一个 parent
    parent_sha = self.registry.verified_result_sha(parent_id, require_accepted=True) or ""
    if not parent_sha and str(config.get("workspace", {}).get("mode")) == "git_worktree":
        raise RuntimeError(f"parent {parent_id} has no accepted verified result SHA")

# adapters.py:204
parent_candidate_id=candidate.parent_ids[0] if candidate.parent_ids else None
```

#### 2. 修复 agent_components.py (4 处)

```python
# agent_components.py:252 和 354 (删除 parent_id 参数)
# 修复前:
# return Candidate(
#     ...
#     parent_id=state.best_candidate_id,
#     ...
# )

# 修复后:
return Candidate(
    ...
    # parent_id 已删除
    ...
)

# agent_components.py:464 和 555
parent_candidate_id=candidate.parent_ids[0] if candidate.parent_ids else None
```

#### 3. 修复测试文件

更新以下测试文件中的 `parent_id` 引用：
- `tests/_fakes.py`
- `tests/test_p0_safety.py`
- `tests/test_agent_components.py`
- `tests/test_gepa_upgrade.py`
- `tests/test_context_views.py`

### 测试验证

1. ✅ 创建 `tests/test_parent_id_cleanup.py` (已完成)
2. 🔧 修复核心代码中的 parent_id 引用
3. 🔧 修复测试文件中的 parent_id 引用
4. 🔄 运行 `pytest tests/test_parent_id_cleanup.py`
5. 🔄 运行 `pytest tests/test_gepa_mini_flow.py`
6. 🔄 运行完整测试套件 `pytest tests/`
7. 🔄 运行 GEPA mini flow 集成测试

---

## 📋 P1 任务：分析并标记必要的显示和日志功能

### 分析方法

1. **分析 orchestrator.py 中的日志输出**
   - 识别哪些日志对调试关键
   - 识别哪些日志可以删除或简化

2. **分析 display.py 中的格式化函数**
   - 识别哪些被核心 loop 使用
   - 识别哪些可以删除

### 预期保留的功能

#### 必须保留：
- ✅ Round 开始/结束日志 (`f"Round {round_id + 1}/{max_rounds} started"`)
- ✅ 关键决策点日志 (accept/discard, stop decision)
- ✅ 错误和警告信息
- ✅ 最终报告 (`_write_final_report`)
- ✅ Run header (`format_run_header`)

#### 可以删除或简化：
- ❌ 详细的 candidate 信息展示 (`_log_candidate`, `_log_trace`, `_log_judgment`)
- ❌ 重复的格式化输出
- ⚠️ Phase header 可以简化
- ⚠️ Agent action 可以简化

### 分析输出

创建一个详细的日志使用分析报告，包括：
1. 每个 log 函数的使用位置
2. 每个 format 函数的调用者
3. 用户必需的 vs 可选的输出

---

## 🔄 P2-P5 任务：顺序执行（确保 GEPA loop 功能）

### 执行原则

**每一步都必须：**
1. 完成代码修改
2. 运行测试验证
3. 运行 GEPA mini flow 集成测试
4. 确认没有功能下降
5. 只有通过后才进入下一步

### P2: 提取 RunStore (2-3 天)

#### 目标

创建统一的存储抽象，解决状态写入分散的问题。

#### 验收标准

- ✅ 所有状态读写通过 `RunStore`
- ✅ Resume 功能正常工作
- ✅ 测试全部通过
- ✅ GEPA mini flow 测试通过

### P3: 拆分 Orchestrator (3-4 天)

#### 目标

提取 `LoopEngine`，简化主 loop。

#### 验收标准

- ✅ 主 loop 在 30 行以内
- ✅ 所有功能正常
- ✅ 测试全部通过
- ✅ GEPA mini flow 测试通过

### P4: 精简测试 (2-3 天)

#### 目标

创建新的测试结构，删除重复测试。

#### 验收标准

- ✅ 5 个测试文件
- ✅ 覆盖所有关键行为
- ✅ 测试全部通过
- ✅ GEPA mini flow 测试通过

### P5: Schema 精简 (1-2 天)

#### 目标

清理 schema，分离 proposal 和运行时状态。

#### 验收标准

- ✅ `Candidate` 只包含 proposal 字段
- ✅ 所有功能正常
- ✅ 测试全部通过
- ✅ GEPA mini flow 测试通过

---

## 📊 验证测试套件

### 测试清单

每个阶段完成后，必须运行以下测试：

1. **单元测试**
   ```bash
   pytest tests/test_parent_id_cleanup.py
   pytest tests/test_agent_components.py
   pytest tests/test_context_views.py
   ```

2. **集成测试**
   ```bash
   pytest tests/test_gepa_mini_flow.py
   pytest tests/test_p0_safety.py
   pytest tests/test_gepa_upgrade.py
   ```

3. **完整测试套件**
   ```bash
   pytest tests/
   ```

### 功能验证

每个阶段完成后，必须验证：

- ✅ GEPA loop 正常运行
- ✅ Seed 初始化正常
- ✅ Mutation 生成正常
- ✅ Feedback gate 正常
- ✅ Pareto gate 正常
- ✅ Resume 功能正常
- ✅ Provenance 验证正常
- ✅ Worktree 隔离正常

---

## 🚨 风险控制

### 回滚策略

如果任何阶段导致功能下降：
1. 立即回滚当前 PR
2. 分析问题根因
3. 修复后重新提交
4. 再次验证

### 验证流程

每次修改后：
1. 运行相关单元测试
2. 运行集成测试
3. 运行 GEPA mini flow
4. 检查日志输出
5. 验证最终结果

---

## 📝 当前待办清单

- [x] 创建 `test_parent_id_cleanup.py`
- [ ] 修复 `adapters.py` 中的 4 处 `parent_id` 引用
- [ ] 修复 `agent_components.py` 中的 4 处 `parent_id` 引用
- [ ] 修复所有测试文件中的 `parent_id` 引用
- [ ] 运行测试验证 parent_id 修复
- [ ] 分析并标记必要的显示和日志功能
- [ ] 提取 RunStore 统一存储抽象
- [ ] 拆分 Orchestrator 为 LoopEngine
- [ ] 精简测试结构
- [ ] Schema 进一步精简

---

**执行原则：**
1. **P0 任务优先**：parent_id 修复必须先完成
2. **逐步验证**：每步都必须验证功能
3. **保证日志**：必要的显示和日志输出必须保留
4. **功能不下降**：GEPA loop 功能必须保持不变
