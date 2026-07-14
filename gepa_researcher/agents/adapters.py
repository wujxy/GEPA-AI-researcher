from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from time import perf_counter
from typing import Any
import uuid

from ..storage.io_utils import append_jsonl, write_json
from ..storage.provenance import audit_commit
from ..storage.registry import ExecutionRegistry
from ..execution.runtime_backend import runtime_backend_for
from ..models.schemas import (
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
from ..execution.workspace import WorkspaceManager


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
    ):
        self.executor = executor
        self.run_dir = run_dir
        self.workspace_manager = workspace_manager
        self.registry = registry or ExecutionRegistry(run_dir)

    def run_many(self, candidates: list[Candidate], round_id: int, config: dict[str, Any]) -> TraceBatch:
        executor_config = config.get("executor", {})
        max_workers = int(executor_config.get("max_workers", 1))
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
            lease, record, canonical_execution = prepared
            record.status = "failed"
            if canonical_execution:
                self.registry.record_execution(record)
            return self._failure_trace(candidate, exc, lease=lease, record=record)

    def _execute_one(
        self,
        candidate: Candidate,
        config: dict[str, Any],
        prepared: tuple[WorkspaceLease, ExecutionRecord, bool],
    ) -> Trace:
        lease, record, canonical_execution = prepared
        runtime_lease = runtime_backend_for(config, self.run_dir).prepare(candidate, lease, record)
        candidate_config = dict(config)
        candidate_config["_candidate_workspace"] = runtime_lease.artifact_path
        candidate_config["_candidate_repo"] = runtime_lease.repo_path
        candidate_config["_candidate_workspace_host"] = lease.artifact_path
        candidate_config["_candidate_repo_host"] = lease.worktree_path
        candidate_config["_executor_host_cwd"] = runtime_lease.host_cwd
        candidate_config["_executor_command"] = runtime_lease.command
        candidate_config["_executor_command_prefix"] = runtime_lease.command_prefix
        candidate_config["_executor_inherit_host_env"] = runtime_lease.inherit_host_env
        candidate_config["_executor_resolve_command_on_host"] = runtime_lease.backend != "apptainer"
        candidate_config["_runtime_lease"] = runtime_lease.to_dict()
        candidate_config["_execution_id"] = record.execution_id
        candidate_config["_execution_mode"] = record.execution_mode
        candidate_config["_candidate_env"] = dict(runtime_lease.env)
        candidate_config["_executor_timeout_seconds"] = int(
            config.get("executor", {}).get(
                "executor_timeout_seconds",
                config.get("agent", {}).get("timeout_seconds", 600),
            )
        )

        # Worktree integrity validation for evaluate_only mode
        worktree_before_snapshot = None
        if record.execution_mode == "evaluate_only" and lease.mode == "git_worktree":
            worktree_before_snapshot = self.workspace_manager.worktree_snapshot(lease.worktree_path)

        start = perf_counter()
        trace = self.executor.execute(candidate, candidate_config)

        # Post-execution worktree integrity check
        if record.execution_mode == "evaluate_only" and lease.mode == "git_worktree" and worktree_before_snapshot:
            worktree_after_snapshot = self.workspace_manager.worktree_snapshot(lease.worktree_path)
            if worktree_before_snapshot != worktree_after_snapshot:
                # Log warning but continue execution for backward compatibility
                if trace.samples:
                    trace.samples[0].artifacts["_worktree_integrity_warning"] = {
                        "before": worktree_before_snapshot,
                        "after": worktree_after_snapshot,
                        "note": "Worktree state changed during evaluate_only execution"
                    }
        if trace.samples:
            trace.samples[0].artifacts.setdefault("executor_wall_seconds", round(perf_counter() - start, 4))
        # Audit the delivered commit (read-only). No "provenance" layer: we keep
        # commit metadata for attribution and apply the one retained hard guard --
        # the executor must not edit a frozen path. Whether the candidate actually
        # worked (build, metric, validation) is the judger's call via the trace.
        # The audit only applies to git-worktree workspaces (the only mode that
        # produces a real commit to diff); other modes have no commit to audit.
        audit = None
        if lease.mode == "git_worktree":
            frozen_globs = list((config.get("candidate_policy") or {}).get("frozen_globs", []))
            audit = audit_commit(
                repo=Path(lease.worktree_path),
                parent_sha=lease.requested_parent_sha,
                frozen_globs=frozen_globs,
            )
            record.result_sha = audit.result_sha
            record.changed_files = audit.changed_files
            record.commit_count = audit.commit_count
            record.status = "frozen_violation" if audit.frozen_violations else "recorded"
        else:
            record.status = "recorded"
        if canonical_execution:
            self.registry.record_execution(record)
        if trace.samples:
            artifacts_update = {
                "workspace_lease": lease.to_dict(),
                "runtime_lease": runtime_lease.to_dict(),
                "execution_record": record.to_dict(),
            }
            if audit is not None:
                artifacts_update["commit_audit"] = audit.to_dict()
            trace.samples[0].artifacts.update(artifacts_update)
            if audit is not None and audit.frozen_violations:
                trace.samples[0].error = "frozen path edited: " + ", ".join(audit.frozen_violations)
                trace.samples[0].artifacts["failure_category"] = "frozen_violation"
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
            self.workspace_manager.assert_worktree_clean_for_execution(eval_lease.worktree_path)
            record.artifacts["clean_start_audit"] = {"passed": True}
            return eval_lease, record, False

        parent_sha = ""
        if candidate.parent_ids:
            parent_id = candidate.parent_ids[0]  # 使用第一个 parent
            parent_sha = self.registry.accepted_result_sha(parent_id, require_accepted=True) or ""
            if not parent_sha and str(config.get("workspace", {}).get("mode")) == "git_worktree":
                raise RuntimeError(f"parent {parent_id} has no accepted result SHA")
        lease = self.workspace_manager.prepare(candidate, parent_sha)
        self.workspace_manager.assert_worktree_clean_for_execution(lease.worktree_path)
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
            artifacts={"clean_start_audit": {"passed": True}},
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

    def _failure_trace(
        self,
        candidate: Candidate,
        exc: Exception,
        *,
        lease: WorkspaceLease | None = None,
        record: ExecutionRecord | None = None,
    ) -> Trace:
        artifacts = {
            "workspace": lease.artifact_path if lease is not None else str(self._workspace(candidate)),
        }
        if lease is not None:
            artifacts["workspace_lease"] = lease.to_dict()
            artifacts["worktree_path"] = lease.worktree_path
        if record is not None:
            artifacts["execution_record"] = record.to_dict()
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
                    artifacts=artifacts,
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
            frozen_violation = any(
                sample.artifacts.get("failure_category") == "frozen_violation"
                for sample in trace.samples
            )
            if frozen_violation:
                # The one hard reject: the executor edited a frozen path, which is
                # a silent-corruption risk the judger cannot infer from metrics.
                judgments.append(
                    Judgment(
                        candidate_id=candidate.candidate_id,
                        round_id=candidate.round_id,
                        score=0.0,
                        passed=False,
                        per_sample_scores=[],
                        failure_categories=["frozen_violation"],
                        actionable_feedback=["Candidate edited a frozen path; reject and do not mutate from it."],
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
