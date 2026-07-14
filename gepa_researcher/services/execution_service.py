from __future__ import annotations

from pathlib import Path
from time import perf_counter
from typing import Any, Protocol

from ..domain.candidate import CandidateCard
from ..domain.execution import ExecutionFailure, ExecutionPhase, ExecutionRecord, ExecutionSpec
from ..execution.git_result import GitResultService
from ..execution.materializer import RepositoryMaterializer
from ..execution.runtime_backend import runtime_backend_for
from ..execution.sandbox import SandboxSession
from ..models.schemas import SampleTrace, Trace
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
    ):
        self.run_dir = Path(run_dir)
        self.config = config
        self.materializer = materializer
        self.execution_store = execution_store
        self.git_result_service = git_result_service
        self.runner = runner

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
                        raise RuntimeError(f"no candidate commit produced: execution_id={spec.execution_id}")
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
            return record, trace
        except Exception as exc:
            failure = ExecutionFailure(
                code=type(exc).__name__,
                message=str(exc),
                retryable=False,
            )
            record = self.execution_store.mark_failed(spec.execution_id, failure)
            trace = self._failure_trace(card, spec, record, exc)
            return record, trace
        finally:
            if session is not None:
                self.materializer.cleanup(session)

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
