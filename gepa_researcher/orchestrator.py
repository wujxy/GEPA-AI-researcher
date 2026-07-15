from __future__ import annotations

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from .loop.admission import CandidateAdmissionGate
from .agents.agent_client import ClaudeCodeClient
from .agents.agent_components import AgentExecutor, AgentJudger, AgentProposer
from .agents.adapters import JudgerAdapter, RunnerAdapter
from .domain.candidate import CandidateCard, CandidateStatus, ProposalIdea
from .domain.execution import ExecutionPhase, ExecutionStatus
from .execution.git_result import GitResultService
from .execution.materializer import RepositoryMaterializer
from .loop.context import load_prior_context
from .display import (
    format_admission_summary,
    format_agent_action,
    format_candidate_list,
    format_gate_summary,
    format_generation_summary,
    format_judgment_summary,
    format_phase_header,
    format_proposal_summary,
    format_round_header,
    format_run_finish,
    format_run_header,
    format_trace_summary,
)
from .loop.gate import GEPAGate
from .storage.io_utils import append_jsonl, write_json
from .loop.pareto import ParetoSelector
from .storage.pool import CandidatePool
from .loop.runtime import config_for_eval, resolve_dataset_split, select_feedback_minibatch
from .models.schemas import Candidate, CandidateBatch, GateDecision, GenerationDecision, Judgment, JudgmentBatch, LoopState, ParetoFrontier, ScoreMatrix, Trace, TraceBatch
from .loop.score_matrix import ScoreMatrixBuilder
from .services.candidate_scheduler import CandidateScheduler
from .services.execution_service import ExecutionService
from .storage.candidate_store import CandidateStore
from .storage.artifact_store import ArtifactStore
from .storage.execution_store import ExecutionStore
from .storage.event_store import EventStore
from .storage.store import RunStore
from .context.plane import GlobalContextPlane
from .context.blocks import SourceRef
from .context.presentation import PresentationStream
from .context.views import ContextViewBuilder
from .storage.usage import UsageTracker, format_round_usage, format_run_usage
from .execution.workspace import WorkspaceManager


class ResearchOrchestrator:
    def __init__(self, config: dict[str, Any], config_path: Path, components: tuple | None = None):
        self.config = deepcopy(config)
        self.config_path = config_path
        self.run_dir = self._resolve_run_dir(self.config, config_path)
        self.config["run_dir"] = str(self.run_dir)
        if self.config.get("workspace", {}).get("mode") == "git_worktree":
            workspace = self.config.setdefault("workspace", {})
            workspace.setdefault("root", str(self.run_dir / "worktrees"))
            workspace.setdefault("branch_prefix", "gepa/<run-id>")
        self.usage_tracker = UsageTracker(self.run_dir, self.config.get("usage_tracking", {}))
        self.store = RunStore(self.run_dir)
        self.event_store = EventStore(self.run_dir)
        self.presentation_stream = PresentationStream(self.run_dir)
        self.candidate_store = CandidateStore(self.run_dir)
        self.execution_store = ExecutionStore(self.run_dir, event_store=self.event_store)
        self.artifact_store = ArtifactStore(self.run_dir)
        self.context_plane = GlobalContextPlane(
            self.run_dir,
            self.config,
            candidate_store=self.candidate_store,
            execution_store=self.execution_store,
            event_store=self.event_store,
            artifact_store=self.artifact_store,
            store=self.store,
        )
        self.workspace_manager = WorkspaceManager(self.run_dir, self.config)
        self.admission = CandidateAdmissionGate()
        self.dataset_split = resolve_dataset_split(self.config)
        self.prior_context = load_prior_context(self.config, config_path.parent)
        # Dependency injection: callers (and tests) may pass a (proposer, executor,
        # judger) tuple directly; otherwise components are built from config. This
        # keeps the orchestrator free of any task-specific or mock component code.
        if components is not None:
            self.proposer, self.executor, self.judger = components
        else:
            self.proposer, self.executor, self.judger = self._build_components()
        executor_timeout = int(
            self.config.get("executor", {}).get(
                "executor_timeout_seconds",
                self.config.get("agent", {}).get("timeout_seconds", 600),
            )
        )
        self.scheduler = CandidateScheduler(
            run_id=self.run_dir.name,
            wall_seconds=executor_timeout,
            forbidden_paths=tuple((self.config.get("candidate_policy") or {}).get("frozen_globs", []) or ()),
        )
        self.execution_service = ExecutionService(
            run_dir=self.run_dir,
            config=self.config,
            materializer=RepositoryMaterializer(self.run_dir, self.config.get("workspace", {})),
            execution_store=self.execution_store,
            git_result_service=GitResultService(self._git_result_policy()),
            runner=RunnerAdapter(self.executor, self.run_dir),
            artifact_store=self.artifact_store,
        )
        self.gate = GEPAGate()
        self.pareto = ParetoSelector()

    def _git_result_policy(self) -> dict[str, Any]:
        policy = dict(self.config.get("candidate_policy", {}) or {})
        workspace = dict(self.config.get("workspace", {}) or {})
        policy["readonly_allowed_dirty_globs"] = [
            *list(workspace.get("pre_materialized_lfs_paths") or []),
            *list(workspace.get("generated_tracked_paths") or []),
            *list(workspace.get("clean_start_ignore_globs") or []),
        ]
        return policy

    def run(self) -> LoopState:
        with self.workspace_manager.protect_controller():
            return self._run_protected()

    def _run_protected(self) -> LoopState:
        controller_snapshot = self.workspace_manager.controller_snapshot()
        self._assert_run_dir_reusable()
        state = self.store.load_or_create_state(self.config["task"]["name"], self.config.get("resume", False))
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.store.save_config(self.config)
        self._recover_interrupted_executions()
        self.store.save_dataset_split(self.dataset_split.to_dict())
        self.store.save_prior_context(self.prior_context)
        max_rounds = int(self.config["budget"]["max_rounds"])
        self._log_block(format_run_header(
            state.task_name,
            str(self.run_dir),
            self.config.get("components", {}).get("mode", "claude_code_agents"),
            max_rounds,
            int(self.config.get("generation", {}).get("batch_size", 1)),
            self.dataset_split,
        ))
        self._initialize_pool_if_needed(state)
        self.workspace_manager.assert_controller_unchanged(controller_snapshot)
        if self.config.get("usage_tracking", {}).get("print_round_summary", True):
            self._log_block(format_round_usage(self.usage_tracker.round_summary(-1)))

        for round_id in range(state.round_id, max_rounds):
            round_controller_snapshot = self.workspace_manager.controller_snapshot()
            state.round_id = round_id
            self._log(f"Round {round_id + 1}/{max_rounds} started")
            decision = self.run_generation(round_id, state)
            self._update_state_from_generation(state, decision)
            self.store.save_state(state)
            self._log(f"Round {round_id + 1}/{max_rounds} persisted")
            self.workspace_manager.assert_controller_unchanged(round_controller_snapshot)
            if self.config.get("usage_tracking", {}).get("print_round_summary", True):
                self._log_block(format_round_usage(self.usage_tracker.round_summary(round_id)))

            if decision.stop:
                self._log(f"Stopping after round {round_id + 1}")
                break

        self._write_final_report(state)
        self.workspace_manager.assert_controller_unchanged(controller_snapshot)
        run_usage = self.usage_tracker.run_summary()
        if self.config.get("usage_tracking", {}).get("print_run_summary", True):
            self._log_block(format_run_usage(run_usage))
        self._log_block(format_run_finish(state, str(self.run_dir / "final_report.md"), str(self.run_dir)))
        return state

    def _assert_run_dir_reusable(self) -> None:
        if self.config.get("resume", False):
            return
        if self.run_dir.exists() and any(self.run_dir.iterdir()):
            raise RuntimeError(
                f"run_dir is not empty but resume=false: {self.run_dir}. "
                "Use a fresh GEPA_RUN_ID or set resume=true deliberately."
            )

    def _log(self, message: str) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{stamp}] {message}", flush=True)

    def _log_block(self, text: str) -> None:
        [self._log(line) for line in text.splitlines()]

    def _recover_interrupted_executions(self) -> None:
        interrupted = self.execution_store.mark_active_interrupted("run startup found active execution from a previous process")
        for record in interrupted:
            self._log(
                "recovered interrupted execution: "
                f"execution_id={record.execution_id} previous_status={record.failure.details.get('previous_status') if record.failure else 'unknown'}"
            )

    def _write_live_artifact(self, round_id: int, name: str, data: dict[str, Any]) -> None:
        write_json(self.run_dir / "live" / f"round_{round_id:03d}_{name}.json", data)

    def _resolve_run_dir(self, config: dict[str, Any], config_path: Path) -> Path:
        run_dir = config.get("run_dir")
        if run_dir:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            run_id = os.environ.get("GEPA_RUN_ID") or stamp
            resolved = str(run_dir).replace("<timestamp>", stamp).replace("<run-id>", run_id)
            return Path(resolved).expanduser().resolve()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return (config_path.parent / "runs" / stamp).resolve()

    def _baseline_revision(self) -> str:
        workspace = self.config.get("workspace", {})
        if str(workspace.get("mode", "artifact_directory")) != "git_worktree":
            return "0" * 40
        repo_path = Path(str(workspace["repo_path"])).expanduser().resolve()
        ref = str(workspace.get("baseline_ref") or "HEAD")
        completed = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "--verify", f"{ref}^{{commit}}"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"cannot resolve workspace baseline ref {ref!r}: {completed.stderr.strip()}")
        return completed.stdout.strip()

    def _known_candidate_ids(self) -> set[str]:
        return {card.candidate_id for card in self.candidate_store.list_all()}

    def _card_from_candidate(self, candidate: Candidate, *, status: CandidateStatus) -> CandidateCard:
        existing = self.candidate_store.get(candidate.candidate_id)
        if existing is not None:
            existing.status = status
            existing.touch()
            return existing
        proposal = ProposalIdea.from_candidate(candidate)
        return CandidateCard(
            candidate_id=candidate.candidate_id,
            round_id=candidate.round_id,
            parent_candidate_ids=tuple(candidate.parent_ids),
            proposal_id=proposal.proposal_id,
            proposal=proposal,
            base_revision=self._base_revision_for_candidate(candidate),
            status=status,
        )

    def _base_revision_for_candidate(self, candidate: Candidate) -> str:
        if not candidate.parent_ids:
            return self._baseline_revision()
        parent = self.candidate_store.get(candidate.parent_ids[0])
        if parent is None or parent.result_revision is None:
            return self._baseline_revision()
        return parent.result_revision

    def _save_card_status(
        self,
        candidate_id: str,
        status: CandidateStatus,
        *,
        final_decision: str | None = None,
        score_summary: dict[str, float] | None = None,
    ) -> None:
        card = self.candidate_store.get(candidate_id)
        if card is None:
            return
        terminal_statuses = {
            CandidateStatus.ACCEPTED,
            CandidateStatus.REJECTED,
            CandidateStatus.IMPLEMENTATION_FAILED,
            CandidateStatus.EVALUATION_FAILED,
            CandidateStatus.CANCELLED,
        }
        if card.status in terminal_statuses and card.status != status:
            if final_decision is not None:
                card.final_decision = final_decision
            if score_summary is not None:
                card.score_summary.update(score_summary)
            card.touch()
            self.candidate_store.save(card)
            return
        if status == CandidateStatus.ACCEPTED and card.status != CandidateStatus.ACCEPTED:
            card.transition("gate_accepted", final_decision=final_decision or "accepted")
        elif status == CandidateStatus.REJECTED and card.status != CandidateStatus.REJECTED:
            card.transition("gate_rejected", final_decision=final_decision or "rejected")
        elif card.status != status:
            card.status = status
            if final_decision is not None:
                card.final_decision = final_decision
            card.touch()
        elif final_decision is not None:
            card.final_decision = final_decision
            card.touch()
        if score_summary is not None:
            card.score_summary.update(score_summary)
            card.touch()
        self.candidate_store.save(card)

    def _build_components(self):
        mode = self.config.get("components", {}).get("mode", "claude_code_agents")
        if mode == "claude_code_agents":
            agent_config = self.config.get("agent", {})
            client = ClaudeCodeClient(
                command=agent_config.get("command", "claude"),
                cwd=Path(agent_config.get("cwd", self.config_path.parent.parent.parent)).resolve(),
                timeout_seconds=int(agent_config.get("timeout_seconds", 600)),
                extra_args=list(agent_config.get("extra_args", [])),
                usage_tracker=self.usage_tracker,
                model=agent_config.get("model"),
                env=dict(agent_config.get("env") or {}),
            )
            return (
                AgentProposer(client),
                AgentExecutor(client, self.run_dir),
                AgentJudger(client),
            )
        raise ValueError(
            f"Unknown component mode: {mode!r}. GEPA ships only the generic "
            "'claude_code_agents' mode; pass components= to __init__ for custom "
            "or test components."
        )


    def _initialize_pool_if_needed(self, state: LoopState) -> None:
        pool = CandidatePool.load(self.run_dir)
        matrix_path = self.run_dir / "score_matrix.json"
        if pool.active_ids() and matrix_path.exists():
            if self.config.get("resume", False):
                return
            raise RuntimeError(
                f"run_dir already contains initialized GEPA state but resume=false: {self.run_dir}. "
                "Use a fresh run_dir (for example one containing <run-id>) or set resume=true."
            )
        self._log("GEPA initialization started")
        seed_count = int(self.config.get("initialization", {}).get("seed_count", 1))
        seed_config = deepcopy(self.config)
        seed_config.setdefault("generation", {})["batch_size"] = seed_count
        seed_config["_prior_context"] = self.prior_context
        seed_config["_agent_phase"] = "initialization"
        seed_state = LoopState(task_name=state.task_name, round_id=-1)
        batch = self.proposer.propose_batch(seed_state, seed_config)
        seed_candidates = list(batch.candidates)
        while len(seed_candidates) < seed_count:
            missing_index = len(seed_candidates)
            self._log(
                f"initialization proposer returned {len(seed_candidates)}/{seed_count} seed(s); "
                f"requesting seed {missing_index + 1}"
            )
            seed_candidates.append(self.proposer.propose(seed_state, seed_config))
        batch.candidates = seed_candidates[:seed_count]
        for index, candidate in enumerate(batch.candidates):
            candidate.candidate_id = f"seed_{index:03d}"
            candidate.round_id = -1
            candidate.parent_ids = []
            candidate.generation = 0
            candidate.status = "seed"
            candidate.mutation_note = candidate.mutation_note or "Initial GEPA seed candidate."
            candidate.executor_contract.setdefault("instructions", "Execute this seed candidate on D_pareto.")
        batch.round_id = -1
        admissions, admitted_seeds = self._admit_candidates(batch.candidates, pool)
        self.store.save_candidate_batch(batch)
        self._persist_admission_decisions(-1, admissions)
        self._log_block(format_admission_summary(admissions))
        self._log_block(format_phase_header(-1, 0, "initialization proposer"))
        self._log_block(format_candidate_list(batch.candidates))
        for candidate in batch.candidates:
            self._log_block(format_proposal_summary(candidate, "initialization", role="seed"))
        if not admitted_seeds:
            raise RuntimeError("all seed candidates were rejected by the admission gate")
        trace_batch, judgment_batch = self._evaluate_candidates(admitted_seeds, -1, "pareto", self.dataset_split.pareto_ids, max_rounds=0)
        self.store.save_judgment_batch(judgment_batch)
        matrix = ScoreMatrixBuilder.from_batch(judgment_batch, {candidate.candidate_id for candidate in admitted_seeds})
        ScoreMatrixBuilder.persist(matrix, matrix_path)
        for candidate in admitted_seeds:
            judgment = next((j for j in judgment_batch.judgments if j.candidate_id == candidate.candidate_id), None)
            eligible = False
            reject_reason = "missing judgment"
            if judgment is not None:
                eligible, reject_reason = self.gate._candidate_eligible(judgment, self.config)
            if eligible and not self._candidate_has_stackable_result(candidate.candidate_id):
                eligible = False
                reject_reason = "missing accepted result SHA"
            if eligible:
                pool.add_accepted(candidate)
                self._save_card_status(candidate.candidate_id, CandidateStatus.ACCEPTED, final_decision="accepted")
            else:
                self._save_card_status(candidate.candidate_id, CandidateStatus.REJECTED, final_decision=reject_reason)
                self._log(f"seed {candidate.candidate_id} rejected during initialization: {reject_reason}")
        pool.persist()
        if not pool.active_ids():
            raise RuntimeError("all seed candidates were rejected during initialization - no valid seeds available for mutation")
        frontier = self.pareto.select(matrix, pool.active_ids())
        self._persist_frontier(frontier)
        active_scores = {candidate_id: score for candidate_id, score in matrix.aggregate_scores.items() if candidate_id in set(pool.active_ids())}
        best = max(active_scores.items(), key=lambda item: item[1], default=(None, None))
        init_feedback: list[str] = []
        for judgment in judgment_batch.judgments:
            init_feedback.extend(judgment.actionable_feedback)
        if best[0] is not None:
            state.best_candidate_id = str(best[0])
            state.best_score = float(best[1])
        state.history.append({
            "round_id": -1,
            "kept": pool.active_ids(),
            "rejected": [candidate.candidate_id for candidate in batch.candidates if candidate.candidate_id not in pool.active_ids()],
            "best_candidate_id": state.best_candidate_id,
            "best_score": state.best_score,
            "next_feedback": list(dict.fromkeys(init_feedback)),
            "stop": False,
            "initialization": True,
        })
        write_json(self.run_dir / "initialization.json", {"candidate_batch": batch.to_dict(), "trace_batch": trace_batch.to_dict(), "judgment_batch": judgment_batch.to_dict(), "score_matrix": matrix.to_dict()})
        self._log("GEPA initialization finished")

    def _candidate_has_stackable_result(self, candidate_id: str) -> bool:
        card = self.candidate_store.get(candidate_id)
        if card is None:
            return False
        return bool(card.result_revision)

    def run_generation(self, round_id: int, state: LoopState) -> GenerationDecision:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        state.round_id = round_id
        self.presentation_stream.append(
            event_type="round_started",
            message=f"Round {round_id + 1} started",
            round_id=round_id,
            source_refs=[SourceRef(source_type="run", source_id=self.run_dir.name)],
        )
        pool = CandidatePool.load(self.run_dir)
        matrix_path = self.run_dir / "score_matrix.json"
        previous_matrix = ScoreMatrixBuilder.load(matrix_path, round_id=round_id)
        active_matrix = ScoreMatrixBuilder.filter_candidates(previous_matrix, set(pool.active_ids()))
        frontier = self.pareto.select(active_matrix, pool.active_ids())
        max_parents = 2 if self.config.get("generation", {}).get("enable_merge", False) else 1
        frontier.parent_ids = self.pareto.sample_parent_ids(frontier, max_parents, seed=round_id)
        parents = self.gate.select_parents(frontier, pool.active, self.config)
        self._write_live_artifact(round_id, "frontier_before", frontier.to_dict())
        self._persist_frontier(frontier)
        self._log_block(format_round_header(
            round_id,
            int(self.config["budget"]["max_rounds"]),
            state,
            frontier,
            parents,
        ))
        self._log(f"pareto frontier selected: {frontier.candidate_ids or 'none'}")

        self._log_block(format_phase_header(round_id, int(self.config["budget"]["max_rounds"]), "proposer mutation"))
        self._log("proposer mutation started")
        proposal_config = deepcopy(self.config)
        proposal_config["_prior_context"] = self.prior_context
        proposal_config["_agent_phase"] = "mutation"
        proposal_config["_context_view"] = (
            ContextViewBuilder(self.context_plane)
            .for_proposer(state, parent_ids=list(frontier.parent_ids), frontier=frontier)
            .to_dict()
        )
        candidate_batch = self.proposer.propose_batch(state, proposal_config)
        self._attach_parent_context(candidate_batch.candidates, parents, round_id)
        admissions, admitted_candidates = self._admit_candidates(candidate_batch.candidates, pool)
        self._write_live_artifact(round_id, "candidate_batch", candidate_batch.to_dict())
        self._write_live_artifact(round_id, "admission_decisions", {"decisions": [item.to_dict() for item in admissions]})
        self._persist_candidate_batch(candidate_batch)
        self.store.save_admission_decisions(round_id, admissions)
        for candidate in candidate_batch.candidates:
            self.presentation_stream.append(
                event_type="candidate_proposed",
                message=f"Candidate {candidate.candidate_id} proposed",
                round_id=round_id,
                candidate_id=candidate.candidate_id,
                source_refs=[SourceRef(source_type="candidate", source_id=candidate.candidate_id)],
            )
        self._log_block(format_admission_summary(admissions))
        self._log(f"proposer mutation finished: {len(candidate_batch.candidates)} candidate(s)")
        self._log_block(format_candidate_list(candidate_batch.candidates))
        for candidate in candidate_batch.candidates:
            self._log_block(format_proposal_summary(candidate, "mutation", score=None, role="child"))

        feedback_ids = select_feedback_minibatch(
            self.dataset_split,
            round_id,
            int(self.config.get("gepa", {}).get("minibatch_size", 1)),
        )
        parent_eval_candidates = [self._candidate_for_round(parent, round_id) for parent in parents]
        feedback_candidates = parent_eval_candidates + admitted_candidates if admitted_candidates else []
        known_scores = dict(active_matrix.aggregate_scores)
        self._log("feedback minibatch eval started")
        feedback_trace_batch, feedback_judgment_batch = self._evaluate_candidates(
            feedback_candidates,
            round_id,
            "feedback",
            feedback_ids,
            known_scores=known_scores,
            role_by_candidate={
                **{parent.candidate_id: "parent" for parent in parent_eval_candidates},
                **{candidate.candidate_id: "child" for candidate in admitted_candidates},
            },
        )
        self._write_live_artifact(round_id, "feedback_trace_batch", feedback_trace_batch.to_dict())
        self._write_live_artifact(round_id, "feedback_judgment_batch", feedback_judgment_batch.to_dict())
        self._log(f"executor adapter finished: {len(feedback_trace_batch.traces)} feedback trace(s), failures={len(feedback_trace_batch.failed_candidate_ids)}")
        self._persist_judgment_batch(feedback_judgment_batch)

        parent_judgments = {
            judgment.candidate_id: judgment
            for judgment in feedback_judgment_batch.judgments
            if judgment.candidate_id in {parent.candidate_id for parent in parents}
        }
        child_judgments = [
            judgment for judgment in feedback_judgment_batch.judgments
            if judgment.candidate_id in {candidate.candidate_id for candidate in admitted_candidates}
        ]
        improvers = self.gate.minibatch_improvers(admitted_candidates, child_judgments, parent_judgments)
        improver_ids = {candidate.candidate_id for candidate in improvers}
        self._log(f"feedback gate improvers: {sorted(improver_ids) or 'none'}")

        pareto_judgment_batch = JudgmentBatch(round_id=round_id, judgments=[], summary={}, artifacts={"phase": "pareto", "sample_ids": list(self.dataset_split.pareto_ids)})
        if improvers:
            self._log("D_pareto full eval started")
            feedback_scores = {judgment.candidate_id: judgment.score for judgment in feedback_judgment_batch.judgments}
            pareto_trace_batch, pareto_judgment_batch = self._evaluate_candidates(
                improvers,
                round_id,
                "pareto",
                self.dataset_split.pareto_ids,
                known_scores=feedback_scores,
                role_by_candidate={candidate.candidate_id: "improver" for candidate in improvers},
            )
            self._write_live_artifact(round_id, "pareto_trace_batch", pareto_trace_batch.to_dict())
            self._write_live_artifact(round_id, "pareto_judgment_batch", pareto_judgment_batch.to_dict())
            self._persist_judgment_batch(pareto_judgment_batch)

        self._log("gepa gate started")
        full_update = ScoreMatrixBuilder.from_batch(pareto_judgment_batch, improver_ids)
        trial_matrix = ScoreMatrixBuilder.merge(active_matrix, full_update)
        gate_decision = self.gate.accept_or_discard(
            round_id,
            improvers,
            pareto_judgment_batch.judgments,
            active_matrix,
            trial_matrix,
            had_active_pool=bool(pool.active_ids()),
        )
        all_child_ids = {candidate.candidate_id for candidate in candidate_batch.candidates}
        final_discarded = list(dict.fromkeys([candidate_id for candidate_id in all_child_ids if candidate_id not in gate_decision.accepted]))
        reasons = dict(gate_decision.reason_by_candidate)
        admission_by_id = {decision.candidate_id: decision for decision in admissions}
        for candidate in candidate_batch.candidates:
            admission_decision = admission_by_id[candidate.candidate_id]
            if not admission_decision.admitted:
                reasons[candidate.candidate_id] = (
                    "discarded by admission gate: " + ", ".join(admission_decision.failure_codes)
                )
            elif candidate.candidate_id not in improver_ids:
                reasons[candidate.candidate_id] = "discarded: did not improve over parent on D_feedback minibatch"
            elif candidate.candidate_id in final_discarded and candidate.candidate_id not in reasons:
                reasons[candidate.candidate_id] = "discarded: failed D_pareto add criteria"
        reason_codes = dict(gate_decision.reason_code_by_candidate)
        for candidate_id in final_discarded:
            reason_codes.setdefault(candidate_id, "NOT_ACCEPTED_BY_GATE")
        gate_decision = GateDecision(
            round_id=round_id,
            accepted=list(gate_decision.accepted),
            discarded=final_discarded,
            reason_by_candidate=reasons,
            reason_code_by_candidate=reason_codes,
        )
        self._apply_gate_decision(pool, candidate_batch.candidates, gate_decision)
        accepted_update = ScoreMatrixBuilder.from_batch(pareto_judgment_batch, set(gate_decision.accepted))
        next_matrix = ScoreMatrixBuilder.merge(active_matrix, accepted_update)
        ScoreMatrixBuilder.persist(next_matrix, matrix_path)
        next_frontier = self.pareto.select(next_matrix, pool.active_ids())
        self._persist_frontier(next_frontier)
        pool.persist()
        self._write_live_artifact(round_id, "gate_decision", gate_decision.to_dict())
        self._write_live_artifact(round_id, "score_matrix", next_matrix.to_dict())
        self._write_live_artifact(round_id, "frontier_after", next_frontier.to_dict())
        self._persist_gate_decision(gate_decision)
        self._log_block(format_gate_summary(gate_decision))

        decision = self._generation_decision_from_gate(
            state,
            round_id,
            gate_decision,
            feedback_judgment_batch,
            pareto_judgment_batch,
            next_matrix,
            next_frontier,
        )
        self._write_live_artifact(round_id, "generation_decision", decision.to_dict())
        self._persist_generation_decision(decision)
        self._log("gepa gate finished")
        self._log_block(format_generation_summary(decision, next_frontier))
        return decision

    def _evaluate_candidates(
        self,
        candidates: list[Candidate],
        round_id: int,
        phase: str,
        sample_ids: list[str],
        known_scores: dict[str, float] | None = None,
        role_by_candidate: dict[str, str] | None = None,
        max_rounds: int | None = None,
    ) -> tuple[Any, JudgmentBatch]:
        total_rounds = max_rounds if max_rounds is not None else int(self.config.get("budget", {}).get("max_rounds", round_id + 1))
        self._log_block(format_phase_header(round_id, total_rounds, f"{phase} eval", sample_ids))
        known_scores = known_scores or {}
        role_by_candidate = role_by_candidate or {}
        for candidate in candidates:
            self._log_block(format_agent_action("executor", "running", candidate.candidate_id, phase))
            self._log_block(format_proposal_summary(
                candidate,
                phase,
                score=known_scores.get(candidate.candidate_id),
                role=role_by_candidate.get(candidate.candidate_id),
            ))
        eval_config = config_for_eval(self.config, sample_ids, phase, self.prior_context)
        eval_config["_run_dir"] = str(self.run_dir)
        traces = self._execute_candidate_batch(candidates, phase, sample_ids, eval_config)
        failed_ids = [
            trace.candidate_id
            for trace in traces
            if any(sample.error for sample in trace.samples)
        ]
        for trace in traces:
            self._persist_trace(trace)
        trace_batch = TraceBatch(round_id=round_id, traces=traces, failed_candidate_ids=failed_ids)
        for trace in trace_batch.traces:
            self._log_block(format_trace_summary(trace, phase, sample_ids))
        judgment_batch = JudgerAdapter(self.judger).evaluate_many(candidates, trace_batch, eval_config)
        judgment_batch.artifacts.update({"phase": phase, "sample_ids": list(sample_ids)})
        for judgment in judgment_batch.judgments:
            self._log_block(format_judgment_summary(judgment, phase))
        return trace_batch, judgment_batch

    def _execute_candidate_batch(
        self,
        candidates: list[Candidate],
        phase: str,
        sample_ids: list[str],
        eval_config: dict[str, Any],
    ) -> list[Trace]:
        max_workers = min(
            len(candidates),
            max(1, int(self.config.get("executor", {}).get("max_workers", 1))),
        )
        if max_workers <= 1:
            return [
                self._execute_candidate_for_eval(
                    self._ensure_candidate_card(candidate),
                    phase,
                    sample_ids,
                    eval_config,
                )
                for candidate in candidates
            ]

        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="gepa-candidate") as pool:
            futures = [
                pool.submit(
                    self._execute_candidate_for_eval,
                    self._ensure_candidate_card(candidate),
                    phase,
                    sample_ids,
                    eval_config,
                )
                for candidate in candidates
            ]
            return [future.result() for future in futures]

    def _ensure_candidate_card(self, candidate: Candidate) -> CandidateCard:
        card = self.candidate_store.get(candidate.candidate_id)
        if card is not None:
            return card
        card = self._card_from_candidate(candidate, status=CandidateStatus.ADMITTED)
        self.candidate_store.save(card)
        return card

    def _execute_candidate_for_eval(
        self,
        card: CandidateCard,
        phase: str,
        sample_ids: list[str],
        eval_config: dict[str, Any],
    ) -> Trace:
        if card.result_revision is None:
            impl_spec = self.scheduler.make_implementation(card)
            card.transition("implementation_started")
            self.candidate_store.save(card)
            impl_record, impl_trace = self.execution_service.execute(impl_spec, card)
            self._record_card_execution(card, impl_record.execution_id)
            if impl_record.status == ExecutionStatus.SUCCEEDED and impl_record.result_revision:
                card.transition("implementation_succeeded", result_revision=impl_record.result_revision)
                self.candidate_store.save(card)
            else:
                card.transition("implementation_failed")
                self.candidate_store.save(card)
                self.presentation_stream.append(
                    event_type="candidate_failed",
                    message=f"Candidate {card.candidate_id} failed implementation",
                    level="warning",
                    round_id=card.round_id,
                    candidate_id=card.candidate_id,
                    source_refs=[
                        SourceRef(source_type="candidate", source_id=card.candidate_id),
                        SourceRef(source_type="execution", source_id=impl_record.execution_id),
                    ],
                )
                return impl_trace

        dataset_ref = f"{phase}:{','.join(sample_ids)}"
        if phase == "feedback":
            eval_spec = self.scheduler.make_feedback_eval(card, dataset_ref=dataset_ref)
        else:
            eval_spec = self.scheduler.make_pareto_eval(card, dataset_ref=dataset_ref)
        if card.status not in {CandidateStatus.ACCEPTED, CandidateStatus.REJECTED}:
            card.transition("evaluation_started")
            self.candidate_store.save(card)
        eval_record, eval_trace = self.execution_service.execute(eval_spec, card)
        self._record_card_execution(card, eval_record.execution_id)
        if eval_record.status == ExecutionStatus.SUCCEEDED:
            if card.status not in {CandidateStatus.ACCEPTED, CandidateStatus.REJECTED}:
                card.transition("evaluation_succeeded")
        else:
            card.transition("evaluation_failed")
            self.presentation_stream.append(
                event_type="candidate_failed",
                message=f"Candidate {card.candidate_id} failed {phase} evaluation",
                level="warning",
                round_id=card.round_id,
                candidate_id=card.candidate_id,
                source_refs=[
                    SourceRef(source_type="candidate", source_id=card.candidate_id),
                    SourceRef(source_type="execution", source_id=eval_record.execution_id),
                ],
            )
        self.candidate_store.save(card)
        return eval_trace

    def _record_card_execution(self, card: CandidateCard, execution_id: str) -> None:
        if execution_id not in card.execution_ids:
            card.execution_ids.append(execution_id)
            card.touch()
            self.candidate_store.save(card)

    def _candidate_for_round(self, candidate: Candidate, round_id: int) -> Candidate:
        clone = deepcopy(candidate)
        clone.round_id = round_id
        return clone

    def _attach_parent_context(self, candidates: list[Candidate], parents: list[Candidate], round_id: int) -> None:
        parent_ids = [parent.candidate_id for parent in parents]
        generation = max((parent.generation for parent in parents), default=-1) + 1
        for candidate in candidates:
            candidate.round_id = round_id
            if not candidate.parent_ids:
                candidate.parent_ids = list(parent_ids)
            candidate.generation = max(candidate.generation, generation)
            candidate.executor_contract.setdefault("expected_artifacts", candidate.expected_artifacts)
            candidate.executor_contract.setdefault("instructions", "Execute this candidate under the configured GEPA evaluation phase.")
            if not candidate.mutation_note:
                candidate.mutation_note = "Reflective mutation from Pareto frontier parent(s)."

    def _recent_feedback(self, state: LoopState) -> list[str]:
        if not state.history:
            return []
        latest = state.history[-1]
        return list(latest.get("next_feedback", []))

    def _apply_gate_decision(
        self,
        pool: CandidatePool,
        candidates: list[Candidate],
        gate_decision: GateDecision,
    ) -> None:
        by_id = {candidate.candidate_id: candidate for candidate in candidates}
        for candidate_id in gate_decision.accepted:
            if candidate_id in by_id:
                pool.add_accepted(by_id[candidate_id])
                self._save_card_status(candidate_id, CandidateStatus.ACCEPTED, final_decision="accepted")
        for candidate_id in gate_decision.discarded:
            if candidate_id in by_id:
                pool.add_discarded(by_id[candidate_id], gate_decision.reason_by_candidate.get(candidate_id, "discarded"))
                self._save_card_status(
                    candidate_id,
                    CandidateStatus.REJECTED,
                    final_decision=gate_decision.reason_by_candidate.get(candidate_id, "discarded"),
                )

    def _admit_candidates(self, candidates: list[Candidate], pool: CandidatePool):
        batch_ids = [candidate.candidate_id for candidate in candidates]
        duplicate_ids = {candidate_id for candidate_id in batch_ids if batch_ids.count(candidate_id) > 1}
        decisions = []
        admitted = []
        known_ids = self._known_candidate_ids()
        accepted_parents = set(pool.accepted_ids)
        for candidate in candidates:
            decision = self.admission.evaluate(
                candidate,
                self.config,
                known_candidate_ids=known_ids | duplicate_ids,
                accepted_parent_ids=accepted_parents,
                batch_candidate_ids=set(batch_ids),
            )
            if decision.admitted and candidate.parent_ids and self.candidate_store.get(candidate.parent_ids[0]) is not None:
                parent = self.candidate_store.get(candidate.parent_ids[0])
                if parent is not None and parent.result_revision is None:
                    decision.admitted = False
                    decision.failure_codes.append("PARENT_RESULT_MISSING")
                    decision.details.append(f"parent has no result_revision: {candidate.parent_ids[0]}")
                    decision.checks["parent_result"] = "fail"
            decisions.append(decision)
            card_status = CandidateStatus.ADMITTED if decision.admitted else CandidateStatus.REJECTED
            card = self._card_from_candidate(candidate, status=card_status)
            if not decision.admitted:
                card.final_decision = "rejected_pre_gate"
            self.candidate_store.save(card)
            if decision.admitted:
                admitted.append(candidate)
            else:
                candidate.status = "rejected_pre_gate"
        return decisions, admitted

    def _generation_decision_from_gate(
        self,
        state: LoopState,
        round_id: int,
        gate_decision: GateDecision,
        feedback_judgment_batch: JudgmentBatch,
        pareto_judgment_batch: JudgmentBatch,
        matrix: ScoreMatrix,
        frontier: ParetoFrontier,
    ) -> GenerationDecision:
        max_rounds = int(self.config.get("budget", {}).get("max_rounds", state.round_id + 1))
        patience = int(self.config.get("budget", {}).get("no_improvement_patience", 999999))
        pass_threshold = float(self.config.get("judger", {}).get("pass_threshold", 1.0))
        best_candidate_id = None
        best_score = None
        if matrix.aggregate_scores:
            best_candidate_id, best_score = max(matrix.aggregate_scores.items(), key=lambda item: item[1])
        improved = best_score is not None and float(best_score) > state.best_score
        no_improvement = 0 if improved else state.no_improvement_rounds + 1
        stop = False
        min_rounds = min(max_rounds, int(self.config.get("budget", {}).get("min_rounds", 2)))
        if round_id + 1 >= max_rounds:
            stop = True
        elif best_score is not None and float(best_score) >= pass_threshold and round_id + 1 >= min_rounds:
            stop = True
        elif no_improvement >= patience:
            stop = True

        feedback: list[str] = []
        for judgment in feedback_judgment_batch.judgments:
            if judgment.candidate_id in gate_decision.accepted or not gate_decision.accepted:
                feedback.extend(judgment.actionable_feedback)
        for judgment in pareto_judgment_batch.judgments:
            if judgment.candidate_id in gate_decision.accepted or not gate_decision.accepted:
                feedback.extend(judgment.actionable_feedback)
        return GenerationDecision(
            round_id=round_id,
            kept=list(gate_decision.accepted),
            rejected=list(gate_decision.discarded),
            next_feedback=list(dict.fromkeys(feedback)),
            stop=stop,
            artifacts={
                "best_candidate_id": best_candidate_id,
                "best_score": best_score,
                "frontier": frontier.to_dict(),
                "gate_decision": gate_decision.to_dict(),
                "score_matrix_path": str(self.run_dir / "score_matrix.json"),
            },
        )

    def _persist_candidate_batch(self, batch: CandidateBatch) -> None:
        round_dir = self.run_dir / "traces" / f"round_{batch.round_id:03d}"
        write_json(round_dir / "candidate_batch.json", batch.to_dict())
        for candidate in batch.candidates:
            write_json(round_dir / candidate.candidate_id / "candidate.json", candidate.to_dict())

    def _persist_trace(self, trace: Trace) -> None:
        round_dir = self.run_dir / "traces" / f"round_{trace.round_id:03d}"
        write_json(round_dir / trace.candidate_id / "trace.json", trace.to_dict())
        append_jsonl(self.run_dir / "traces.jsonl", trace.to_dict())

    def _persist_admission_decisions(self, round_id: int, decisions) -> None:
        round_dir = self.run_dir / "traces" / f"round_{round_id:03d}"
        payload = {"round_id": round_id, "decisions": [decision.to_dict() for decision in decisions]}
        write_json(round_dir / "admission_decisions.json", payload)
        for decision in decisions:
            append_jsonl(self.run_dir / "admission_decisions.jsonl", decision.to_dict())

    def _persist_judgment_batch(self, batch: JudgmentBatch) -> None:
        round_dir = self.run_dir / "traces" / f"round_{batch.round_id:03d}"
        write_json(round_dir / "judgment_batch.json", batch.to_dict())
        for judgment in batch.judgments:
            write_json(round_dir / judgment.candidate_id / "judgment.json", judgment.to_dict())
            append_jsonl(self.run_dir / "judgments.jsonl", judgment.to_dict())

    def _persist_generation_decision(self, decision: GenerationDecision) -> None:
        round_dir = self.run_dir / "traces" / f"round_{decision.round_id:03d}"
        write_json(round_dir / "generation_decision.json", decision.to_dict())
        append_jsonl(self.run_dir / "decisions.jsonl", decision.to_dict())

    def _persist_gate_decision(self, decision: GateDecision) -> None:
        round_dir = self.run_dir / "traces" / f"round_{decision.round_id:03d}"
        write_json(round_dir / "gate_decision.json", decision.to_dict())
        append_jsonl(self.run_dir / "gate_decisions.jsonl", decision.to_dict())

    def _persist_frontier(self, frontier: ParetoFrontier) -> None:
        write_json(self.run_dir / "frontier.json", frontier.to_dict())

    def _update_state_from_generation(self, state: LoopState, decision: GenerationDecision) -> None:
        best_score = decision.artifacts.get("best_score")
        best_candidate_id = decision.artifacts.get("best_candidate_id")
        improved = best_score is not None and float(best_score) > state.best_score
        if improved:
            state.best_score = float(best_score)
            state.best_candidate_id = str(best_candidate_id)
            state.no_improvement_rounds = 0
        else:
            state.no_improvement_rounds += 1
        state.history.append({"round_id": decision.round_id, "kept": decision.kept, "rejected": decision.rejected, "best_candidate_id": best_candidate_id, "best_score": best_score, "next_feedback": decision.next_feedback, "stop": decision.stop})
        state.round_id = decision.round_id + 1

    def _write_final_report(self, state: LoopState) -> None:
        lines = [
            f"# Run Summary: {state.task_name}",
            "",
            f"- Best candidate: `{state.best_candidate_id}`",
            f"- Best score: `{state.best_score:.4f}`",
            f"- Completed rounds: `{len(state.history)}`",
            "",
            "## Round History",
            "",
        ]
        for item in state.history:
            lines.extend([
                f"### Round {item['round_id']} - generation",
                "",
                f"- Best candidate: `{item.get('best_candidate_id')}`",
                f"- Best score: `{item.get('best_score')}`",
                f"- Kept: {', '.join(item.get('kept', [])) or 'none'}",
                f"- Rejected: `{len(item.get('rejected', []))}`",
                f"- Stop: `{item.get('stop')}`",
                "- Next feedback:",
                *[f"  - {feedback}" for feedback in item.get("next_feedback", [])],
                "",
            ])
        (self.run_dir / "final_report.md").write_text("\n".join(lines), encoding="utf-8")
