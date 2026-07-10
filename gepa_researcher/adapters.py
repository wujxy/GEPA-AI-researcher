from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from time import perf_counter
from typing import Any
import uuid

from .io_utils import append_jsonl, write_json
from .provenance import ProvenanceVerifier
from .registry import ExecutionRegistry
from .schemas import (
    Candidate,
    CandidateBatch,
    ExecutionRecord,
    Judgment,
    JudgmentBatch,
    SampleTrace,
    Trace,
    TraceBatch,
    WorkspaceLease,
)
from .workspace import WorkspaceManager


class ExecutorAdapter:
    """Small adapter around the external executor.

    GEPA does not care whether execution is local, Claude Code, Codex, human, or
    HPC. This adapter only preserves workspace isolation and failure archival.
    """

    def __init__(
        self,
        executor: Any,
        run_dir: Path,
        workspace_manager: WorkspaceManager | None = None,
        registry: ExecutionRegistry | None = None,
        provenance: ProvenanceVerifier | None = None,
    ):
        self.executor = executor
        self.run_dir = run_dir
        self.workspace_manager = workspace_manager
        self.registry = registry or ExecutionRegistry(run_dir)
        self.provenance = provenance or ProvenanceVerifier()

    def run_many(self, candidates: list[Candidate], round_id: int, config: dict[str, Any]) -> TraceBatch:
        executor_config = config.get("executor", {})
        max_workers = int(executor_config.get("max_workers", executor_config.get("max_parallel_executors", 1)))
        fail_fast = bool(executor_config.get("fail_fast", False))
        traces_by_id: dict[str, Trace] = {}
        failed_ids: list[str] = []

        batch = CandidateBatch(round_id=round_id, candidates=candidates)
        workspace_mode = str(config.get("workspace", {}).get("mode", "artifact_directory"))
        if (
            max_workers > 1
            and config.get("task", {}).get("repo_paths")
            and workspace_mode != "git_worktree"
        ):
            raise RuntimeError("parallel source execution requires workspace.mode=git_worktree")
        self.workspace_manager = self.workspace_manager or WorkspaceManager(self.run_dir, config)
        prepared: dict[str, tuple[WorkspaceLease, ExecutionRecord, bool]] = {}
        preparation_failures: dict[str, Trace] = {}
        for candidate in candidates:
            try:
                prepared[candidate.candidate_id] = self._prepare_execution(candidate, config)
            except Exception as exc:
                preparation_failures[candidate.candidate_id] = self._failure_trace(candidate, exc)

        if max_workers <= 1 or len(candidates) <= 1:
            for candidate in candidates:
                trace = preparation_failures.get(candidate.candidate_id)
                if trace is None:
                    trace = self._run_one_safely(candidate, config, prepared[candidate.candidate_id])
                traces_by_id[candidate.candidate_id] = trace
                if self._trace_failed(trace):
                    failed_ids.append(candidate.candidate_id)
                    if fail_fast:
                        break
            return self._finish_batch(batch, traces_by_id, failed_ids)

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for candidate_id, trace in preparation_failures.items():
                traces_by_id[candidate_id] = trace
                failed_ids.append(candidate_id)
            futures = {
                pool.submit(self._run_one_safely, candidate, config, prepared[candidate.candidate_id]): candidate
                for candidate in candidates
                if candidate.candidate_id in prepared
            }
            for future in as_completed(futures):
                candidate = futures[future]
                trace = future.result()
                traces_by_id[candidate.candidate_id] = trace
                if self._trace_failed(trace):
                    failed_ids.append(candidate.candidate_id)
                if fail_fast and self._trace_failed(trace):
                    break
        return self._finish_batch(batch, traces_by_id, failed_ids)

    def _run_one_safely(
        self,
        candidate: Candidate,
        config: dict[str, Any],
        prepared: tuple[WorkspaceLease, ExecutionRecord, bool],
    ) -> Trace:
        try:
            return self._execute_one(candidate, config, prepared)
        except Exception as exc:
            return self._failure_trace(candidate, exc)

    def _execute_one(
        self,
        candidate: Candidate,
        config: dict[str, Any],
        prepared: tuple[WorkspaceLease, ExecutionRecord, bool],
    ) -> Trace:
        lease, record, canonical_execution = prepared
        candidate_config = dict(config)
        candidate_config["_candidate_workspace"] = lease.artifact_path
        candidate_config["_candidate_repo"] = lease.worktree_path
        candidate_config["_execution_id"] = record.execution_id
        candidate_config["_execution_mode"] = record.execution_mode
        candidate_config["_candidate_env"] = {
            "GEPA_CANDIDATE_ID": candidate.candidate_id,
            "GEPA_EXECUTION_ID": record.execution_id,
            "GEPA_PARENT_SHA": record.requested_parent_sha,
            "GEPA_WORKTREE": lease.worktree_path,
        }
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
        report = self.provenance.verify(candidate, lease, record, candidate_config)
        if canonical_execution:
            self.registry.record_execution(record)
        self.registry.record_provenance(report)
        if trace.samples:
            trace.samples[0].artifacts.update(
                {
                    "workspace_lease": lease.to_dict(),
                    "execution_record": record.to_dict(),
                    "provenance": report.to_dict(),
                }
            )
            if not report.verified:
                trace.samples[0].error = "provenance verification failed: " + ", ".join(report.failure_codes)
                trace.samples[0].artifacts["failure_category"] = "provenance_failed"
        return trace

    def _prepare_execution(
        self,
        candidate: Candidate,
        config: dict[str, Any],
    ) -> tuple[WorkspaceLease, ExecutionRecord, bool]:
        existing = self.registry.execution(candidate.candidate_id)
        existing_workspace = self.registry.workspace(candidate.candidate_id)
        lifecycle = str(config.get("execution", {}).get("lifecycle", "stateless"))
        if lifecycle == "materialize_once" and existing and existing_workspace:
            result_sha = str(existing.get("result_sha") or "")
            if not result_sha:
                raise RuntimeError(f"candidate {candidate.candidate_id} has no verified result SHA")
            lease = WorkspaceLease(**existing_workspace)
            eval_lease = WorkspaceLease(
                **{
                    **lease.to_dict(),
                    "requested_parent_sha": result_sha,
                    "actual_start_sha": result_sha,
                }
            )
            record = ExecutionRecord(
                execution_id=str(uuid.uuid4()),
                candidate_id=candidate.candidate_id,
                round_id=candidate.round_id,
                parent_candidate_id=candidate.parent_ids[0] if candidate.parent_ids else None,
                requested_parent_sha=result_sha,
                actual_start_sha=result_sha,
                result_sha=result_sha,
                branch_name=lease.branch_name,
                worktree_path=lease.worktree_path,
                execution_mode="evaluate_only",
                status="evaluating",
            )
            return eval_lease, record, False

        parent_sha = ""
        if candidate.parent_ids:
            parent_id = candidate.parent_ids[0]  # 使用第一个 parent
            parent_sha = self.registry.verified_result_sha(parent_id, require_accepted=True) or ""
            if not parent_sha and str(config.get("workspace", {}).get("mode")) == "git_worktree":
                raise RuntimeError(f"parent {parent_id} has no accepted verified result SHA")
        lease = self.workspace_manager.prepare(candidate, parent_sha)
        self.registry.record_workspace(lease)
        record = ExecutionRecord(
            execution_id=str(uuid.uuid4()),
            candidate_id=candidate.candidate_id,
            round_id=candidate.round_id,
            parent_candidate_id=candidate.parent_ids[0] if candidate.parent_ids else None,
            requested_parent_sha=lease.requested_parent_sha,
            actual_start_sha=lease.actual_start_sha,
            result_sha=None,
            branch_name=lease.branch_name,
            worktree_path=lease.worktree_path,
            execution_mode="implement_and_validate",
            status="executing",
        )
        self.registry.record_execution(record)
        return lease, record, True

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
        for trace in traces:
            self._persist_trace(trace)
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
            provenance_failed = any(
                sample.artifacts.get("failure_category") == "provenance_failed"
                for sample in trace.samples
            )
            if provenance_failed:
                judgments.append(
                    Judgment(
                        candidate_id=candidate.candidate_id,
                        round_id=candidate.round_id,
                        score=0.0,
                        passed=False,
                        per_sample_scores=[],
                        failure_categories=["provenance_failed"],
                        actionable_feedback=["Fix workspace/commit provenance before judging this candidate."],
                        confidence="high",
                        artifacts={"deterministic": True},
                    )
                )
            else:
                judgments.append(self.judger.judge(candidate, trace, config))

        best = max(judgments, key=lambda judgment: judgment.score, default=None)
        summary = {
            "candidate_count": len(judgments),
            "best_candidate_id": best.candidate_id if best else None,
            "best_score": best.score if best else None,
            "failed_candidate_ids": list(trace_batch.failed_candidate_ids),
        }
        return JudgmentBatch(round_id=trace_batch.round_id, judgments=judgments, summary=summary)
