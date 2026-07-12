我这边没能完成实际写入：提权读写被系统额度拦截，普通沙箱又触发 `bwrap: Can't bind mount /oldroot/`，所以现在不能安全地改文件或重新扫日志。下面是我建议立即打到 `GEPA-AI-researcher/gepa_researcher/agent_components.py` executor prompt 的最小 patch 方向。

核心改动：把原来的 `Return only a JSON object, no prose outside JSON.` 扩成强约束：

```text
Final delivery contract:
- Your final response MUST be exactly one parseable JSON object matching the schema below.
- Do not wrap the JSON in Markdown or a code fence.
- Do not include prose, status updates, apologies, commentary, or natural-language wrap-up outside JSON.
- Never finish with natural language such as "waiting for results", "still running", "will continue", or "need more time".
- If a command is still running, wait for it to finish unless the configured timeout or candidate contract forces you to stop.
- If execution is incomplete, blocked, interrupted, or a command produced no metric, still return the JSON schema with validation.passed=false.
- Use null for unavailable metrics, add the reason to errors, and add a stable category to failure_categories.
- A partial or failed run is acceptable only if it is reported as JSON.
- Set validation.passed=true only when all required validation and metric gates passed.
```

同时把 schema 补成：

```json
{
  "summary": "what you executed",
  "implementation": {"changed_files": [], "commands_run": [], "notes": ""},
  "metrics": {"primary": null, "baseline": null, "delta": null, "reps": []},
  "validation": {"passed": false, "checks": [], "regressions": []},
  "failure_categories": [],
  "diagnostics": ["diagnostic or failure finding"],
  "artifact_paths": ["relative or absolute paths"],
  "errors": []
}
```

基于已看到的这次 `full_loop.log`，除了 `seed_001` 自然语言收尾导致 `invalid_result` 外，还暴露了这些问题：

1. **executor 交付格式确实是首要问题**  
   `seed_001` 的最终输出是类似“等待实际 ms/evt 结果”的自然语言，说明它把中间状态当成了最终交付。这个优先用 prompt 合同修，方向是对的。

2. **异常后的 GEPA 记账曾不完整**  
   executor 抛异常后，`execution_registry.json` 里会残留 `executing`，而 failure trace 里 workspace 指向也不够准。我之前已经修过 `adapters.py`，让异常路径写入 `failed`、真实 worktree、lease 和 execution record；但这次已产生的 run 记录不会自动回填。

3. **单个 executor 成本和耗时偏高**  
   初始化里几个 seed 都是十几到三十分钟级，且反复做 fresh baseline / quick bench。不是“资源不足导致错误”，但会显著放大不稳定：一次非 JSON 收尾就浪费很多时间和 token。

4. **benchmark 产物污染 worktree**  
   `benchmarks/drift.csv` / `speed.csv` 这类结果文件会被 executor 写脏，但它们不是优化源码本身。建议后续把 benchmark 输出固定导向 artifacts/run 目录，或在 skill 中明确“不得把 benchmark csv 当作候选改动提交”。

5. **环境约束还可再固化**  
   日志里 executor 提到 PyROOT 需要手动预设 `PYTHONPATH/LD_LIBRARY_PATH`。这类环境前置条件最好写进 Br1.11 pack 的流程 skill / profile，而不是依赖 executor 自己发现。

我的判断：这次核心失败不是工作空间资源缺失，而是 **executor prompt 的最终交付契约不够硬**，叠加 GEPA 对异常输出的容错和记账还需要更强。第一步先改 prompt 是正确路径。