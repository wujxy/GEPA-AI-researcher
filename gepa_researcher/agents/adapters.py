from __future__ import annotations

from pathlib import Path
from typing import Any

from ..domain.candidate import CandidateCard
from ..domain.execution import ExecutionPhase, ExecutionSpec
from ..execution.runtime_backend import RuntimeLease
from ..execution.sandbox import SandboxSession
from ..context.plane import GlobalContextPlane
from ..context.views import ContextView, ContextViewBuilder
from ..models.schemas import (
    Candidate,
    Judgment,
    JudgmentBatch,
    SampleTrace,
    Trace,
    TraceBatch,
)
from ..storage.artifact_store import ArtifactStore
from ..storage.candidate_store import CandidateStore
from ..storage.event_store import EventStore
from ..storage.execution_store import ExecutionStore
from ..storage.store import RunStore


class RunnerAdapter:
    """Adapter from Execution Kernel domain objects to the existing executor."""

    def __init__(self, executor: Any, run_dir: Path):
        self.executor = executor
        self.run_dir = run_dir

    def run(
        self,
        card: CandidateCard,
        spec: ExecutionSpec,
        runtime_lease: RuntimeLease,
        session: SandboxSession,
        config: dict[str, Any],
    ) -> Trace:
        candidate = _candidate_from_card(card)
        candidate_config = dict(config)
        candidate_config["task"] = dict(candidate_config.get("task") or {})
        candidate_config["task"]["repo_paths"] = [str(runtime_lease.repo_path)]
        if isinstance(candidate_config.get("contracts"), dict):
            contracts = dict(candidate_config["contracts"])
            resources = dict(contracts.get("resources") or {})
            resources["repo_path"] = str(runtime_lease.repo_path)
            contracts["resources"] = resources
            candidate_config["contracts"] = contracts
        candidate_config["_candidate_workspace"] = runtime_lease.artifact_path
        candidate_config["_candidate_repo"] = runtime_lease.repo_path
        candidate_config["_candidate_workspace_host"] = str(session.artifact_path)
        candidate_config["_candidate_repo_host"] = str(session.repo_path)
        candidate_config["_executor_host_cwd"] = runtime_lease.host_cwd
        candidate_config["_executor_command"] = runtime_lease.command
        candidate_config["_executor_command_prefix"] = runtime_lease.command_prefix
        candidate_config["_executor_inherit_host_env"] = runtime_lease.inherit_host_env
        candidate_config["_executor_resolve_command_on_host"] = runtime_lease.backend != "apptainer"
        candidate_config["_runtime_lease"] = runtime_lease.to_dict()
        candidate_config["_execution_id"] = spec.execution_id
        candidate_config["_input_revision"] = spec.input_revision
        candidate_config["_execution_mode"] = _execution_mode_for_phase(spec.phase)
        candidate_config["_candidate_env"] = dict(runtime_lease.env)
        candidate_config["_eval_phase"] = spec.phase.value
        if spec.dataset_ref is not None and "_selected_sample_ids" not in candidate_config:
            candidate_config["_selected_sample_ids"] = [spec.dataset_ref]
        candidate_config["_executor_timeout_seconds"] = spec.budget.wall_seconds
        context_view = _build_executor_view(
            self.run_dir,
            candidate_config,
            candidate,
            Path(runtime_lease.artifact_path),
            Path(runtime_lease.repo_path),
            candidate_config["_execution_mode"],
        )
        if context_view is not None:
            candidate_config["_context_view"] = context_view.to_dict()
        return self.executor.execute(candidate, candidate_config)


def _candidate_from_card(card: CandidateCard) -> Candidate:
    proposal = card.proposal
    return Candidate(
        candidate_id=card.candidate_id,
        round_id=card.round_id,
        parent_ids=list(card.parent_candidate_ids),
        hypothesis=proposal.hypothesis,
        scope=proposal.scope,
        proposed_change=proposal.proposed_change,
        rationale=proposal.rationale,
        expected_improvement=proposal.expected_improvement,
        risk=proposal.risk,
        prompt_text=proposal.prompt_text,
        created_at=card.created_at,
        executor_contract=dict(proposal.executor_contract),
        expected_artifacts=list(proposal.expected_artifacts),
        target_files=list(proposal.target_files),
        status=card.status.value,
        artifacts=dict(proposal.metadata),
    )


def _execution_mode_for_phase(phase: ExecutionPhase) -> str:
    return "implement_and_validate" if phase == ExecutionPhase.IMPLEMENTATION else "evaluate_only"


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
                judger_config = dict(config)
                context_view = _build_judger_view(judger_config, candidate, trace)
                if context_view is not None:
                    judger_config["_context_view"] = context_view.to_dict()
                judgments.append(self.judger.judge(candidate, trace, judger_config))

        best = max(judgments, key=lambda judgment: judgment.score, default=None)
        summary = {
            "candidate_count": len(judgments),
            "best_candidate_id": best.candidate_id if best else None,
            "best_score": best.score if best else None,
            "failed_candidate_ids": list(trace_batch.failed_candidate_ids),
        }
        return JudgmentBatch(round_id=trace_batch.round_id, judgments=judgments, summary=summary)


def _context_plane(run_dir: Path, config: dict[str, Any]) -> GlobalContextPlane:
    event_store = EventStore(run_dir)
    return GlobalContextPlane(
        run_dir,
        config,
        candidate_store=CandidateStore(run_dir),
        execution_store=ExecutionStore(run_dir, event_store=event_store),
        event_store=event_store,
        artifact_store=ArtifactStore(run_dir),
        store=RunStore(run_dir),
    )


def _build_executor_view(
    run_dir: Path,
    config: dict[str, Any],
    candidate: Candidate,
    round_dir: Path,
    repo_dir: Path,
    execution_mode: str,
) -> ContextView | None:
    try:
        return ContextViewBuilder(_context_plane(run_dir, config)).for_executor(
            candidate,
            config,
            run_dir,
            round_dir,
            repo_dir,
            execution_mode,
        )
    except Exception:
        return None


def _build_judger_view(config: dict[str, Any], candidate: Candidate, trace: Trace) -> ContextView | None:
    run_dir_value = config.get("_run_dir") or config.get("run_dir")
    if not run_dir_value:
        return None
    try:
        return ContextViewBuilder(_context_plane(Path(run_dir_value), config)).for_judge(candidate, trace, config)
    except Exception:
        return None
