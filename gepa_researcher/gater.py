from __future__ import annotations

from typing import Any

from .schemas import Candidate, Decision, Judgment, LoopState


class SimpleGater:
    """Budgeted best-so-far gate for the first version."""

    def decide(self, state: LoopState, candidate: Candidate, judgment: Judgment, config: dict[str, Any]) -> Decision:
        improved = judgment.score > state.best_score
        max_rounds = config["budget"]["max_rounds"]
        patience = config["budget"]["no_improvement_patience"]

        if improved:
            decision = "keep"
            reason = f"Score improved from {state.best_score:.4f} to {judgment.score:.4f}."
            no_improvement = 0
            best_so_far = candidate.candidate_id
        else:
            decision = "reject"
            reason = f"Score {judgment.score:.4f} did not improve over best {state.best_score:.4f}."
            no_improvement = state.no_improvement_rounds + 1
            best_so_far = state.best_candidate_id

        stop = False
        if judgment.passed:
            stop = True
            decision = "stop"
            reason += " Pass threshold reached."
        elif state.round_id + 1 >= max_rounds:
            stop = True
            decision = "stop"
            reason += " Max rounds reached."
        elif no_improvement >= patience:
            stop = True
            decision = "stop"
            reason += " No-improvement patience exhausted."

        return Decision(
            candidate_id=candidate.candidate_id,
            round_id=candidate.round_id,
            decision=decision,
            reason=reason,
            best_so_far=best_so_far,
            stop=stop,
        )
