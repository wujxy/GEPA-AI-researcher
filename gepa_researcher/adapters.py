from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from time import perf_counter
from typing import Any

from .io_utils import append_jsonl, write_json
from .schemas import Candidate, CandidateBatch, Judgment, JudgmentBatch, SampleTrace, Trace, TraceBatch


class ExecutorAdapter:
    """Small adapter around the external executor.

    GEPA does not care whether execution is local, Claude Code, Codex, human, or
    HPC. This adapter only preserves workspace isolation and failure archival.
    """

    def __init__(self, executor: Any, run_dir: Path):
        self.executor = executor
        self.run_dir = run_dir

    def run_many(self, candidates: list[Candidate], round_id: int, config: dict[str, Any]) -> TraceBatch:
        executor_config = config.get("executor", {})
        max_workers = int(executor_config.get("max_workers", executor_config.get("max_parallel_executors", 1)))
        fail_fast = bool(executor_config.get("fail_fast", False))
        traces_by_id: dict[str, Trace] = {}
        failed_ids: list[str] = []

        batch = CandidateBatch(round_id=round_id, candidates=candidates)
        if max_workers <= 1 or len(candidates) <= 1:
            for candidate in candidates:
                trace = self._run_one_safely(candidate, config)
                traces_by_id[candidate.candidate_id] = trace
                if self._trace_failed(trace):
                    failed_ids.append(candidate.candidate_id)
                    if fail_fast:
                        break
            return self._finish_batch(batch, traces_by_id, failed_ids)

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(self._run_one_safely, candidate, config): candidate for candidate in candidates}
            for future in as_completed(futures):
                candidate = futures[future]
                trace = future.result()
                traces_by_id[candidate.candidate_id] = trace
                if self._trace_failed(trace):
                    failed_ids.append(candidate.candidate_id)
                if fail_fast and self._trace_failed(trace):
                    break
        return self._finish_batch(batch, traces_by_id, failed_ids)

    def _run_one_safely(self, candidate: Candidate, config: dict[str, Any]) -> Trace:
        try:
            return self._execute_one(candidate, config)
        except Exception as exc:
            return self._failure_trace(candidate, exc)

    def _execute_one(self, candidate: Candidate, config: dict[str, Any]) -> Trace:
        candidate_config = dict(config)
        candidate_config["_candidate_workspace"] = str(self._workspace(candidate))
        candidate_config["_executor_timeout_seconds"] = int(
            config.get("executor", {}).get(
                "executor_timeout_seconds",
                config.get("agent", {}).get("timeout_seconds", 600),
            )
        )
        start = perf_counter()
        trace = self.executor.execute(candidate, candidate_config)
        if trace.samples:
            trace.samples[0].artifacts.setdefault("executor_wall_seconds", round(perf_counter() - start, 4))
        self._persist_trace(trace)
        return trace

    def _finish_batch(
        self,
        batch: CandidateBatch,
        traces_by_id: dict[str, Trace],
        failed_ids: list[str],
    ) -> TraceBatch:
        traces = [
            traces_by_id[candidate.candidate_id]
            for candidate in batch.candidates
            if candidate.candidate_id in traces_by_id
        ]
        return TraceBatch(round_id=batch.round_id, traces=traces, failed_candidate_ids=failed_ids)

    def _workspace(self, candidate: Candidate) -> Path:
        return self.run_dir / "agent_work" / f"round_{candidate.round_id:03d}" / candidate.candidate_id

    def _trace_path(self, trace: Trace) -> Path:
        return self.run_dir / "traces" / f"round_{trace.round_id:03d}" / trace.candidate_id / "trace.json"

    def _persist_trace(self, trace: Trace) -> None:
        write_json(self._trace_path(trace), trace.to_dict())
        append_jsonl(self.run_dir / "traces.jsonl", trace.to_dict())

    def _failure_trace(self, candidate: Candidate, exc: Exception) -> Trace:
        trace = Trace(
            candidate_id=candidate.candidate_id,
            round_id=candidate.round_id,
            samples=[
                SampleTrace(
                    sample_id="executor_failure",
                    input=candidate.prompt_text,
                    output="",
                    expected="executor completed",
                    logs="executor failed",
                    error=f"{type(exc).__name__}: {exc}",
                    artifacts={"workspace": str(self._workspace(candidate))},
                )
            ],
        )
        self._persist_trace(trace)
        return trace

    def _trace_failed(self, trace: Trace) -> bool:
        return any(sample.error for sample in trace.samples)


class JudgerAdapter:
    def __init__(self, judger: Any):
        self.judger = judger

    def evaluate_many(
        self,
        candidates: list[Candidate],
        trace_batch: TraceBatch,
        config: dict[str, Any],
    ) -> JudgmentBatch:
        trace_by_id = {trace.candidate_id: trace for trace in trace_batch.traces}
        judgments: list[Judgment] = []
        for candidate in candidates:
            trace = trace_by_id.get(candidate.candidate_id)
            if trace is None:
                trace = Trace(
                    candidate_id=candidate.candidate_id,
                    round_id=candidate.round_id,
                    samples=[
                        SampleTrace(
                            sample_id="missing_trace",
                            input=candidate.prompt_text,
                            output="",
                            expected="executor trace",
                            logs="executor returned no trace",
                            error="missing trace",
                        )
                    ],
                )
            judgments.append(self.judger.judge(candidate, trace, config))

        best = max(judgments, key=lambda judgment: judgment.score, default=None)
        summary = {
            "candidate_count": len(judgments),
            "best_candidate_id": best.candidate_id if best else None,
            "best_score": best.score if best else None,
            "failed_candidate_ids": list(trace_batch.failed_candidate_ids),
        }
        return JudgmentBatch(round_id=trace_batch.round_id, judgments=judgments, summary=summary)
