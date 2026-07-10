from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from .admission import CandidateAdmissionGate
from .agent_client import AgentError, ClaudeCodeClient
from .agent_components import AgentExecutor, AgentJudger, AgentProposer
from .adapters import ExecutorAdapter, JudgerAdapter
from .context import load_prior_context
from .display import (
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
from .gate import GEPAGate
from .io_utils import append_jsonl, read_json, write_json
from .pareto import ParetoSelector
from .pool import CandidatePool
from .provenance import ProvenanceVerifier
from .registry import ExecutionRegistry
from .runtime import config_for_eval, parent_trace_artifacts, recent_trace_summaries, resolve_dataset_split, select_feedback_minibatch
from .schemas import Candidate, CandidateBatch, Decision, EvaluationBatch, GateDecision, GenerationDecision, Judgment, JudgmentBatch, LoopState, ParetoFrontier, ScoreMatrix, Trace
from .score_matrix import ScoreMatrixBuilder
from .usage import UsageTracker, format_round_usage, format_run_usage
from .workspace import WorkspaceManager


class ResearchOrchestrator:
    def __init__(self, config: dict[str, Any], config_path: Path, components: tuple | None = None):
        self.config = config
        self.config_path = config_path
        self.run_dir = self._resolve_run_dir(config, config_path)
        self.usage_tracker = UsageTracker(self.run_dir, config.get("usage_tracking", {}))
        self.registry = ExecutionRegistry(self.run_dir)
        self.workspace_manager = WorkspaceManager(self.run_dir, config)
        self.provenance = ProvenanceVerifier()
        self.admission = CandidateAdmissionGate()
        self.dataset_split = resolve_dataset_split(config)
        self.prior_context = load_prior_context(config, config_path.parent)
        # Dependency injection: callers (and tests) may pass a (proposer, executor,
        # judger) tuple directly; otherwise components are built from config. This
        # keeps the orchestrator free of any task-specific or mock component code.
        if components is not None:
            self.proposer, self.executor, self.judger = components
        else:
            self.proposer, self.executor, self.judger = self._build_components()
        self.gate = GEPAGate()
        self.pareto = ParetoSelector()

    def run(self) -> LoopState:
        controller_snapshot = self.workspace_manager.controller_snapshot()
        state = self._load_or_initialize_state()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        write_json(self.run_dir / "config.snapshot.json", self.config)
        write_json(self.run_dir / "dataset_split.json", self.dataset_split.to_dict())
        write_json(self.run_dir / "prior_context.json", self.prior_context)
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
            write_json(self.run_dir / "state.json", state.to_dict())
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

    def _log(self, message: str) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{stamp}] {message}", flush=True)

    def _log_block(self, text: str) -> None:
        for line in text.splitlines():
            self._log(line)

    def _write_live_artifact(self, round_id: int, name: str, data: dict[str, Any]) -> None:
        write_json(self.run_dir / "live" / f"round_{round_id:03d}_{name}.json", data)

    def _log_candidate(self, candidate: Candidate) -> None:
        self._log(f"Hypothesis: {candidate.hypothesis}")
        self._log(f"Proposed change: {candidate.proposed_change}")
        if candidate.rationale:
            self._log(f"Rationale: {candidate.rationale}")
        if candidate.expected_improvement:
            self._log(f"Expected improvement: {candidate.expected_improvement}")
        if candidate.risk:
            self._log(f"Risk: {candidate.risk}")

    def _log_trace(self, trace: Trace) -> None:
        if not trace.samples:
            self._log("Executor summary: no samples returned")
            return
        sample = trace.samples[0]
        artifacts = sample.artifacts
        summary = artifacts.get("summary") or sample.logs or sample.output
        self._log(f"Executor summary: {summary}")
        implementation = artifacts.get("implementation")
        if implementation:
            self._log(f"Implementation: {implementation}")
        metrics = artifacts.get("metrics")
        if metrics:
            self._log(f"Metrics: {metrics}")
        diagnostics = artifacts.get("diagnostics")
        if diagnostics:
            self._log(f"Diagnostics: {diagnostics}")

    def _log_judgment(self, judgment: Judgment) -> None:
        self._log(f"Judgment: score={judgment.score:.4f}, passed={judgment.passed}, confidence={judgment.confidence}")
        if judgment.failure_categories:
            self._log(f"Failure categories: {judgment.failure_categories}")
        if judgment.actionable_feedback:
            self._log(f"Feedback: {judgment.actionable_feedback}")

    def _log_generation_decision(self, decision: GenerationDecision) -> None:
        self._log(f"Generation decision: kept={decision.kept}, rejected={len(decision.rejected)}, stop={decision.stop}")
        if decision.next_feedback:
            self._log(f"Next feedback: {decision.next_feedback}")

    def _resolve_run_dir(self, config: dict[str, Any], config_path: Path) -> Path:
        run_dir = config.get("run_dir")
        if run_dir:
            return Path(run_dir).expanduser().resolve()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return (config_path.parent / "runs" / stamp).resolve()

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

    def _load_or_initialize_state(self) -> LoopState:
        state_path = self.run_dir / "state.json"
        if state_path.exists() and self.config.get("resume", False):
            return LoopState.from_dict(read_json(state_path))
        return LoopState(task_name=self.config["task"]["name"])

    def _initialize_pool_if_needed(self, state: LoopState) -> None:
        pool = CandidatePool.load(self.run_dir)
        matrix_path = self.run_dir / "score_matrix.json"
        if pool.active_ids() and matrix_path.exists():
            return
        self._log("GEPA initialization started")
        seed_count = int(self.config.get("initialization", {}).get("seed_count", 1))
        seed_config = deepcopy(self.config)
        seed_config.setdefault("generation", {})["batch_size"] = seed_count
        seed_config["_prior_context"] = self.prior_context
        seed_config["_agent_phase"] = "initialization"
        seed_state = LoopState(task_name=state.task_name, round_id=-1)
        if hasattr(self.proposer, "propose_batch"):
            batch = self.proposer.propose_batch(seed_state, seed_config)
        else:
            batch = CandidateBatch(round_id=-1, candidates=[self.proposer.propose(seed_state, seed_config)])
        for index, candidate in enumerate(batch.candidates[:seed_count]):
            candidate.candidate_id = f"seed_{index:03d}"
            candidate.round_id = -1
            candidate.parent_id = None
            candidate.parent_ids = []
            candidate.generation = 0
            candidate.status = "seed"
            candidate.mutation_note = candidate.mutation_note or "Initial GEPA seed candidate."
            candidate.executor_contract.setdefault("instructions", "Execute this seed candidate on D_pareto.")
        batch.candidates = batch.candidates[:seed_count]
        batch.round_id = -1
        admissions, admitted_seeds = self._admit_candidates(batch.candidates, pool)
        self._persist_candidate_batch(batch)
        self._persist_admission_decisions(-1, admissions)
        self._log_block(format_phase_header(-1, 0, "initialization proposer"))
        self._log_block(format_candidate_list(batch.candidates))
        for candidate in batch.candidates:
            self._log_block(format_proposal_summary(candidate, "initialization", role="seed"))
        if not admitted_seeds:
            raise RuntimeError("all seed candidates were rejected by the admission gate")
        trace_batch, judgment_batch = self._evaluate_candidates(admitted_seeds, -1, "pareto", self.dataset_split.pareto_ids, max_rounds=0)
        self._persist_judgment_batch(judgment_batch)
        matrix = ScoreMatrixBuilder.from_batch(judgment_batch, {candidate.candidate_id for candidate in admitted_seeds})
        ScoreMatrixBuilder.persist(matrix, matrix_path)
        for candidate in admitted_seeds:
            pool.add_accepted(candidate)
            self.registry.mark_candidate_status(candidate.candidate_id, "accepted")
        pool.persist()
        frontier = self.pareto.select(matrix, pool.active_ids())
        self._persist_frontier(frontier)
        best = max(matrix.aggregate_scores.items(), key=lambda item: item[1], default=(None, None))
        init_feedback: list[str] = []
        for judgment in judgment_batch.judgments:
            init_feedback.extend(judgment.actionable_feedback)
        if best[0] is not None:
            state.best_candidate_id = str(best[0])
            state.best_score = float(best[1])
        state.history.append({
            "round_id": -1,
            "kept": [candidate.candidate_id for candidate in admitted_seeds],
            "rejected": [candidate.candidate_id for candidate in batch.candidates if candidate not in admitted_seeds],
            "best_candidate_id": state.best_candidate_id,
            "best_score": state.best_score,
            "next_feedback": list(dict.fromkeys(init_feedback)),
            "stop": False,
            "initialization": True,
        })
        write_json(self.run_dir / "initialization.json", {"candidate_batch": batch.to_dict(), "trace_batch": trace_batch.to_dict(), "judgment_batch": judgment_batch.to_dict(), "score_matrix": matrix.to_dict()})
        self._log("GEPA initialization finished")

    def run_generation(self, round_id: int, state: LoopState) -> GenerationDecision:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        state.round_id = round_id
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
        proposal_config = self._config_with_gepa_context(state, pool, active_matrix, frontier, parents)
        proposal_config["_agent_phase"] = "mutation"
        if hasattr(self.proposer, "propose_batch"):
            candidate_batch = self.proposer.propose_batch(state, proposal_config)
        else:
            candidate_batch = CandidateBatch(round_id=round_id, candidates=[self.proposer.propose(state, proposal_config)])
        self._attach_parent_context(candidate_batch.candidates, parents, round_id)
        admissions, admitted_candidates = self._admit_candidates(candidate_batch.candidates, pool)
        self._write_live_artifact(round_id, "candidate_batch", candidate_batch.to_dict())
        self._write_live_artifact(round_id, "admission_decisions", {"decisions": [item.to_dict() for item in admissions]})
        self._persist_candidate_batch(candidate_batch)
        self._persist_admission_decisions(round_id, admissions)
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
        gate_decision = GateDecision(round_id=round_id, accepted=list(gate_decision.accepted), discarded=final_discarded, reason_by_candidate=reasons)
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
        trace_batch = ExecutorAdapter(
            self.executor,
            self.run_dir,
            workspace_manager=self.workspace_manager,
            registry=self.registry,
            provenance=self.provenance,
        ).run_many(candidates, round_id, eval_config)
        for trace in trace_batch.traces:
            self._log_block(format_trace_summary(trace, phase, sample_ids))
        judgment_batch = JudgerAdapter(self.judger).evaluate_many(candidates, trace_batch, eval_config)
        judgment_batch.artifacts.update({"phase": phase, "sample_ids": list(sample_ids)})
        for judgment in judgment_batch.judgments:
            self._log_block(format_judgment_summary(judgment, phase))
        self._persist_evaluation_batch(EvaluationBatch(
            round_id=round_id,
            phase=phase,  # type: ignore[arg-type]
            candidate_ids=[candidate.candidate_id for candidate in candidates],
            sample_ids=list(sample_ids),
            trace_paths={trace.candidate_id: str(self.run_dir / "traces" / f"round_{trace.round_id:03d}" / trace.candidate_id / "trace.json") for trace in trace_batch.traces},
            judgment_paths={judgment.candidate_id: str(self.run_dir / "traces" / f"round_{round_id:03d}" / judgment.candidate_id / "judgment.json") for judgment in judgment_batch.judgments},
        ))
        return trace_batch, judgment_batch

    def _candidate_for_round(self, candidate: Candidate, round_id: int) -> Candidate:
        clone = deepcopy(candidate)
        clone.round_id = round_id
        return clone

    def _config_with_gepa_context(
        self,
        state: LoopState,
        pool: CandidatePool,
        matrix: ScoreMatrix,
        frontier: ParetoFrontier,
        parents: list[Candidate],
    ) -> dict[str, Any]:
        config = deepcopy(self.config)
        config["_prior_context"] = self.prior_context
        config["_gepa_context"] = {
            "state": state.to_dict(),
            "candidate_pool": pool.snapshot().to_dict(),
            "score_matrix": matrix.to_dict(),
            "pareto_frontier": frontier.to_dict(),
            "parents": [parent.to_dict() for parent in parents],
            "parent_traces": parent_trace_artifacts(self.run_dir, [parent.candidate_id for parent in parents]),
            "parent_executions": {
                parent.candidate_id: self.registry.execution(parent.candidate_id) or {}
                for parent in parents
            },
            "recent_feedback": self._recent_feedback(state),
            "recent_traces": recent_trace_summaries(self.run_dir),
            "dataset_split": self.dataset_split.to_dict(),
        }
        return config

    def _attach_parent_context(self, candidates: list[Candidate], parents: list[Candidate], round_id: int) -> None:
        parent_ids = [parent.candidate_id for parent in parents]
        generation = max((parent.generation for parent in parents), default=-1) + 1
        for candidate in candidates:
            candidate.round_id = round_id
            if not candidate.parent_ids:
                candidate.parent_ids = list(parent_ids)
            candidate.parent_id = candidate.parent_ids[0] if candidate.parent_ids else None
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
                self.registry.mark_candidate_status(candidate_id, "accepted")
        for candidate_id in gate_decision.discarded:
            if candidate_id in by_id:
                pool.add_discarded(by_id[candidate_id], gate_decision.reason_by_candidate.get(candidate_id, "discarded"))
                self.registry.mark_candidate_status(candidate_id, "discarded")

    def _admit_candidates(self, candidates: list[Candidate], pool: CandidatePool):
        batch_ids = [candidate.candidate_id for candidate in candidates]
        duplicate_ids = {candidate_id for candidate_id in batch_ids if batch_ids.count(candidate_id) > 1}
        decisions = []
        admitted = []
        known_ids = self.registry.known_candidate_ids()
        accepted_parents = set(pool.accepted_ids)
        for candidate in candidates:
            decision = self.admission.evaluate(
                candidate,
                self.config,
                known_candidate_ids=known_ids | duplicate_ids,
                accepted_parent_ids=accepted_parents,
                batch_candidate_ids=set(batch_ids),
            )
            decisions.append(decision)
            self.registry.record_admission(decision)
            if decision.admitted:
                admitted.append(candidate)
            else:
                self.registry.mark_candidate_status(candidate.candidate_id, "rejected_pre_gate")
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

    def _persist_round(self, candidate: Candidate, trace: Trace, judgment: Judgment, decision: Decision) -> None:
        round_dir = self.run_dir / "traces" / f"round_{candidate.round_id:03d}"
        write_json(round_dir / "candidate.json", candidate.to_dict())
        write_json(round_dir / "trace.json", trace.to_dict())
        write_json(round_dir / "judgment.json", judgment.to_dict())
        write_json(round_dir / "decision.json", decision.to_dict())
        append_jsonl(self.run_dir / "candidates.jsonl", candidate.to_dict())
        append_jsonl(self.run_dir / "judgments.jsonl", judgment.to_dict())
        append_jsonl(self.run_dir / "decisions.jsonl", decision.to_dict())

    def _persist_candidate_batch(self, batch: CandidateBatch) -> None:
        round_dir = self.run_dir / "traces" / f"round_{batch.round_id:03d}"
        write_json(round_dir / "candidate_batch.json", batch.to_dict())
        for candidate in batch.candidates:
            write_json(round_dir / candidate.candidate_id / "candidate.json", candidate.to_dict())
            append_jsonl(self.run_dir / "candidates.jsonl", candidate.to_dict())

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

    def _persist_evaluation_batch(self, batch: EvaluationBatch) -> None:
        round_dir = self.run_dir / "traces" / f"round_{batch.round_id:03d}"
        write_json(round_dir / f"evaluation_{batch.phase}.json", batch.to_dict())

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

    def _update_state(self, state: LoopState, candidate: Candidate, judgment: Judgment, decision: Decision) -> None:
        improved = judgment.score > state.best_score
        if improved:
            state.best_score = judgment.score
            state.best_candidate_id = candidate.candidate_id
            state.no_improvement_rounds = 0
        else:
            state.no_improvement_rounds += 1
        state.history.append({"round_id": candidate.round_id, "candidate_id": candidate.candidate_id, "score": judgment.score, "passed": judgment.passed, "decision": decision.decision, "failure_categories": judgment.failure_categories, "actionable_feedback": judgment.actionable_feedback, "prompt_text": candidate.prompt_text})
        state.round_id = candidate.round_id + 1

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


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the bounded GEPA-style research loop.")
    parser.add_argument("--config", required=True, help="Path to a JSON config file.")
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    config = read_json(config_path)
    orchestrator = ResearchOrchestrator(config=config, config_path=config_path)
    try:
        state = orchestrator.run()
    except AgentError as exc:
        print(f"Agent runtime error: {exc}")
        print(f"Artifacts, if any: {orchestrator.run_dir}")
        raise SystemExit(2) from exc
    print(f"Run complete. Best={state.best_candidate_id} score={state.best_score:.4f}")
    print(f"Artifacts: {orchestrator.run_dir}")


if __name__ == "__main__":
    main()
