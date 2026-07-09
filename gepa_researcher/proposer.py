from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .schemas import Candidate, CandidateBatch, LoopState


class RuleBasedProposer:
    """Small deterministic proposer for the first skeleton.

    Replace this with an LLM-backed proposer later. The interface should stay
    stable: state + config in, Candidate out.
    """

    def propose(self, state: LoopState, config: dict[str, Any]) -> Candidate:
        candidate_id = f"cand_{state.round_id:03d}"
        parent_ids = self._parent_ids(config)
        parent_id = parent_ids[0] if parent_ids else state.best_candidate_id
        initial_prompt = config["task"]["initial_prompt"]
        feedback = self._latest_feedback(state)

        if state.round_id == 0 and not parent_ids:
            prompt_text = initial_prompt
            hypothesis = "Baseline prompt establishes the initial score."
            proposed_change = "Use the initial answer prompt without modification."
            rationale = "The loop needs an auditable baseline before mutation."
            expected = "Creates a reference point for later rounds."
            risk = "Baseline may hallucinate or omit evidence."
        elif "unsupported_answer" in feedback or "missing_evidence" in feedback:
            prompt_text = (
                initial_prompt
                + "\n\nBefore answering, identify the evidence sentence in the context. "
                + "If the context does not contain enough evidence, answer exactly UNKNOWN. "
                + "Always cite the supporting evidence after the answer."
            )
            hypothesis = "Evidence-first answers will reduce hallucination."
            proposed_change = "Add evidence localization, UNKNOWN fallback, and citation requirement."
            rationale = "The previous candidate produced unsupported or weakly grounded answers."
            expected = "Improve evidence_support and no_hallucination without changing the task."
            risk = "The answer may become conservative and mark answerable cases as UNKNOWN."
        else:
            prompt_text = (
                ((state.history[-1].get("prompt_text") or initial_prompt) if state.history else initial_prompt)
                + "\n\nUse the final format: Answer: <answer> | Evidence: <short quote or UNKNOWN>."
            )
            hypothesis = "A stricter output format will improve judge parseability."
            proposed_change = "Add a fixed answer/evidence format."
            rationale = "Structured outputs make executor traces and judgments easier to compare."
            expected = "Improve format compliance and reduce ambiguous outputs."
            risk = "The format may be overfit to the current judge."

        return Candidate(
            candidate_id=candidate_id,
            round_id=state.round_id,
            parent_id=parent_id,
            hypothesis=hypothesis,
            target_module="answer_prompt",
            proposed_change=proposed_change,
            rationale=rationale,
            expected_improvement=expected,
            risk=risk,
            prompt_text=prompt_text,
            created_at=datetime.now(timezone.utc).isoformat(),
            parent_ids=parent_ids,
            generation=self._generation(config),
            executor_contract={
                "instructions": "Run this prompt on the configured task samples and return per-sample traces.",
                "expected_artifacts": ["trace samples", "per-sample outputs"],
            },
            expected_artifacts=["trace samples", "per-sample outputs"],
            mutation_note="Seed candidate." if not parent_ids else "Reflective mutation from Pareto frontier feedback.",
        )

    def propose_batch(self, state: LoopState, config: dict[str, Any]) -> CandidateBatch:
        batch_size = int(config.get("generation", {}).get("batch_size", 10))
        candidates = []
        for index in range(batch_size):
            candidate = self.propose(state, config)
            candidate.candidate_id = f"cand_{state.round_id:03d}_{index:03d}"
            candidate.hypothesis = f"{candidate.hypothesis} Variant {index + 1}."
            candidate.proposed_change = f"{candidate.proposed_change} Batch variant {index + 1}."
            candidate.prompt_text = f"{candidate.prompt_text}\n\nBatch variant: {index + 1}"
            candidates.append(candidate)
        return CandidateBatch(round_id=state.round_id, candidates=candidates)

    def _latest_feedback(self, state: LoopState) -> str:
        if not state.history:
            return ""
        latest = state.history[-1]
        feedback = list(latest.get("failure_categories", []))
        feedback.extend(latest.get("next_feedback", []))
        return " ".join(feedback)

    def _parent_ids(self, config: dict[str, Any]) -> list[str]:
        context = config.get("_gepa_context") or {}
        frontier = context.get("pareto_frontier") or {}
        return list(frontier.get("parent_ids") or [])

    def _generation(self, config: dict[str, Any]) -> int:
        parents = (config.get("_gepa_context") or {}).get("parents") or []
        if not parents:
            return 0
        return max(int(parent.get("generation", 0)) for parent in parents) + 1
