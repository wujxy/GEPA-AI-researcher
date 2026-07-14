from __future__ import annotations

from pathlib import Path
from time import perf_counter
from typing import Any, Protocol

from ..domain.candidate import CandidateCard
from ..domain.execution import ExecutionFailure, ExecutionPhase, ExecutionRecord, ExecutionSpec
from ..agents.agent_client import AgentError
from ..agents.agent_components import AgentProtocolError
from ..execution.git_result import GitResultService
from ..execution.git_result import GitResultError
from ..execution.materializer import MaterializerError, RepositoryMaterializer
from ..execution.runtime_backend import RuntimeBackendError
from ..execution.runtime_backend import runtime_backend_for
from ..execution.sandbox import SandboxSession
from ..models.schemas import SampleTrace, Trace
from ..domain.artifact import ArtifactKind
from ..storage.artifact_store import ArtifactStore
from ..storage.execution_store import ExecutionStore


class Runner(Protocol):
    def run(
        self,
        card: CandidateCard,
        spec: ExecutionSpec,
        runtime_lease: Any,
        session: SandboxSession,
        config: dict[str, Any],
    ) -> Trace:
        ...


class ExecutionService:
    def __init__(
        self,
        *,
        run_dir: Path,
        config: dict[str, Any],
        materializer: RepositoryMaterializer,
        execution_store: ExecutionStore,
        git_result_service: GitResultService,
        runner: Runner,
        artifact_store: ArtifactStore | None = None,
    ):
        self.run_dir = Path(run_dir)
        self.config = config
        self.materializer = materializer
        self.execution_store = execution_store
        self.git_result_service = git_result_service
        self.runner = runner
        self.artifact_store = artifact_store or ArtifactStore(self.run_dir)

    def execute(self, spec: ExecutionSpec, card: CandidateCard) -> tuple[ExecutionRecord, Trace]:
        record = self.execution_store.create_pending(spec)
        session: SandboxSession | None = None
        try:
            self.execution_store.mark_preparing(spec.execution_id)
            session = self.materializer.materialize(spec)
            runtime_lease = runtime_backend_for(self.config, self.run_dir).prepare(spec, session, record)
            before = None
            if spec.phase != ExecutionPhase.IMPLEMENTATION and session.mode == "git_worktree":
                before = self.git_result_service.snapshot(session)

            self.execution_store.mark_running(spec.execution_id)
            started = perf_counter()
            trace = self.runner.run(card, spec, runtime_lease, session, self.config)
            wall_seconds = round(perf_counter() - started, 4)

            self.execution_store.mark_collecting(spec.execution_id)
            result_revision = None
            audit = None
            if spec.phase == ExecutionPhase.IMPLEMENTATION:
                if session.mode == "git_worktree":
                    result_revision, audit = self.git_result_service.finalize_implementation(spec, session)
                    if audit.commit_count <= 0:
                        raise NoCandidateCommitError(
                            f"no candidate commit produced: execution_id={spec.execution_id}",
                            details=self._no_candidate_commit_details(spec, session, audit, trace),
                        )
                else:
                    result_revision = spec.input_revision
            else:
                if session.mode == "git_worktree":
                    self.git_result_service.assert_readonly_unchanged(spec, session, before)

            record = self.execution_store.mark_succeeded(
                spec.execution_id,
                result_revision=result_revision,
                metrics=_metrics_from_trace(trace),
            )
            self._attach_execution_artifacts(trace, record, runtime_lease, wall_seconds, audit)
            record = self._index_trace_artifact(record, trace, spec.phase.value)
            return record, trace
        except Exception as exc:
            failure = _failure_from_exception(exc)
            record = self.execution_store.mark_failed(spec.execution_id, failure)
            trace = self._failure_trace(card, spec, record, exc)
            record = self._index_trace_artifact(record, trace, spec.phase.value)
            return record, trace
        finally:
            if session is not None:
                try:
                    self.materializer.cleanup(session)
                except Exception as cleanup_exc:
                    if "record" in locals():
                        record.failure = record.failure or ExecutionFailure(
                            code="SANDBOX_CLEANUP_FAILED",
                            message=str(cleanup_exc),
                            retryable=True,
                        )
                        record.failure.details.setdefault("cleanup_failure", str(cleanup_exc))
                        self.execution_store.save(record)

    def _attach_execution_artifacts(
        self,
        trace: Trace,
        record: ExecutionRecord,
        runtime_lease: Any,
        wall_seconds: float,
        audit: Any,
    ) -> None:
        if not trace.samples:
            return
        payload = {
            "execution_record": record.to_dict(),
            "runtime_lease": runtime_lease.to_dict(),
            "executor_wall_seconds": wall_seconds,
        }
        if audit is not None:
            payload["commit_audit"] = audit.to_dict()
            if audit.frozen_violations:
                trace.samples[0].error = "frozen path edited: " + ", ".join(audit.frozen_violations)
                payload["failure_category"] = "frozen_violation"
        trace.samples[0].artifacts.update(payload)

    def _no_candidate_commit_details(
        self,
        spec: ExecutionSpec,
        session: SandboxSession,
        audit: Any,
        trace: Trace,
    ) -> dict[str, Any]:
        details: dict[str, Any] = {
            "execution_id": spec.execution_id,
            "candidate_id": spec.candidate_id,
            "phase": spec.phase.value,
            "input_revision": spec.input_revision,
            "actual_head": audit.result_sha,
            "commit_count": audit.commit_count,
            "changed_files": list(audit.changed_files),
            "worktree_status": audit.worktree_status,
            "fallback_commit_created": audit.fallback_commit_created,
        }
        claimed = _claimed_commit_sha(trace)
        if claimed:
            details["claimed_commit_sha"] = claimed
            details["claimed_commit_exists"] = self.git_result_service.commit_exists(session, claimed)
        return details

    def _failure_trace(
        self,
        card: CandidateCard,
        spec: ExecutionSpec,
        record: ExecutionRecord,
        exc: Exception,
    ) -> Trace:
        return Trace(
            candidate_id=card.candidate_id,
            round_id=card.round_id,
            samples=[
                SampleTrace(
                    sample_id="execution_failure",
                    input=card.proposal.prompt_text,
                    output="",
                    expected="execution completed",
                    logs="execution failed",
                    error=f"{type(exc).__name__}: {exc}",
                    artifacts={
                        "execution_id": spec.execution_id,
                        "execution_record": record.to_dict(),
                    },
                )
            ],
        )

    def _index_trace_artifact(self, record: ExecutionRecord, trace: Trace, phase: str) -> ExecutionRecord:
        ref = self.artifact_store.put_json(
            record.execution_id,
            ArtifactKind.EXECUTION_TRACE,
            "execution_trace.json",
            trace.to_dict(),
            metadata={"phase": phase, "candidate_id": record.candidate_id},
        )
        record.artifact_refs = [*record.artifact_refs, ref]
        self.execution_store.save(record)
        return record


def _metrics_from_trace(trace: Trace) -> dict[str, float]:
    metrics: dict[str, float] = {}
    for sample in trace.samples:
        raw = sample.artifacts.get("metrics") if sample.artifacts else None
        if not isinstance(raw, dict):
            continue
        for key, value in raw.items():
            if isinstance(value, (int, float)):
                metrics[str(key)] = float(value)
    return metrics


class NoCandidateCommitError(RuntimeError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = dict(details or {})


def _claimed_commit_sha(trace: Trace) -> str | None:
    for sample in trace.samples:
        implementation = sample.artifacts.get("implementation") if sample.artifacts else None
        if not isinstance(implementation, dict):
            continue
        commit_sha = implementation.get("commit_sha")
        if isinstance(commit_sha, str) and commit_sha:
            return commit_sha
    return None


def _failure_from_exception(exc: Exception) -> ExecutionFailure:
    if isinstance(exc, MaterializerError):
        code = "SANDBOX_PREPARE_FAILED"
        retryable = True
    elif isinstance(exc, RuntimeBackendError):
        code = "RUNTIME_PREPARE_FAILED"
        retryable = True
    elif isinstance(exc, AgentProtocolError):
        code = "AGENT_PROTOCOL_INVALID"
        retryable = True
    elif isinstance(exc, AgentError):
        code = "AGENT_PROCESS_FAILED"
        retryable = True
    elif isinstance(exc, NoCandidateCommitError):
        code = "NO_CANDIDATE_COMMIT"
        retryable = False
    elif isinstance(exc, GitResultError) and "read-only execution changed sandbox" in str(exc):
        code = "READONLY_EXECUTION_MUTATED_REPO"
        retryable = False
    elif isinstance(exc, GitResultError):
        code = "COMMIT_FAILED"
        retryable = False
    else:
        code = "AGENT_PROCESS_FAILED"
        retryable = False
    return ExecutionFailure(
        code=code,
        message=str(exc),
        retryable=retryable,
        details=dict(getattr(exc, "details", {}) or {}),
    )
