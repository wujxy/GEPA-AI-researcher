from __future__ import annotations

from ..models.schemas import Candidate, GateDecision, Judgment, ParetoFrontier, ScoreMatrix


DEFAULT_HARD_FAILURE_CATEGORIES = {
    "frozen_violation",
    "incomplete_validation",
    "implementation_uncertainty",
    "missing_metrics",
    "no_implementation",
    "invalid_hypothesis",
    "scope_mismatch",
    "duplicate_baseline",
    "baseline_mismatch",
    "accumulated_prior_changes",
}


class GEPAGate:
    def select_parents(
        self,
        frontier: ParetoFrontier,
        candidates: dict[str, Candidate],
        config: dict,
    ) -> list[Candidate]:
        max_parents = 2 if config.get("generation", {}).get("enable_merge", False) else 1
        parent_ids = list(frontier.parent_ids or frontier.candidate_ids[:max_parents])
        frontier.parent_ids = parent_ids
        return [candidates[parent_id] for parent_id in parent_ids if parent_id in candidates]

    def minibatch_improvers(
        self,
        candidates: list[Candidate],
        judgments: list[Judgment],
        parent_judgments: dict[str, Judgment],
    ) -> list[Candidate]:
        judgment_by_id = {judgment.candidate_id: judgment for judgment in judgments}
        improvers: list[Candidate] = []
        for candidate in candidates:
            child_score = judgment_by_id.get(candidate.candidate_id)
            if child_score is None or not self._eligible_judgment(child_score, {}):
                continue
            parent_scores = [
                parent_judgments[parent_id].score
                for parent_id in candidate.parent_ids
                if parent_id in parent_judgments
            ]
            if not parent_scores or float(child_score.score) > max(parent_scores):
                improvers.append(candidate)
        return improvers

    def accept_or_discard(
        self,
        round_id: int,
        candidates: list[Candidate],
        judgments: list[Judgment],
        previous_matrix: ScoreMatrix,
        trial_matrix: ScoreMatrix,
        had_active_pool: bool,
    ) -> GateDecision:
        judgment_by_id = {judgment.candidate_id: judgment for judgment in judgments}
        accepted: list[str] = []
        discarded: list[str] = []
        reasons: dict[str, str] = {}
        reason_codes: dict[str, str] = {}
        task_best = self._task_best_ids(trial_matrix)

        for candidate in candidates:
            judgment = judgment_by_id.get(candidate.candidate_id)
            if judgment is None:
                discarded.append(candidate.candidate_id)
                reasons[candidate.candidate_id] = "discarded: missing judgment"
                reason_codes[candidate.candidate_id] = "MISSING_JUDGMENT"
                continue
            eligible, eligibility_reason = self._candidate_eligible(judgment, {})
            if not eligible:
                discarded.append(candidate.candidate_id)
                reasons[candidate.candidate_id] = eligibility_reason
                reason_codes[candidate.candidate_id] = _reason_code_for_ineligible(judgment)
                continue
            if not had_active_pool:
                accepted.append(candidate.candidate_id)
                reasons[candidate.candidate_id] = "accepted: initial pool seed"
                reason_codes[candidate.candidate_id] = "INITIAL_POOL_SEED"
                continue
            if candidate.candidate_id in task_best:
                accepted.append(candidate.candidate_id)
                reasons[candidate.candidate_id] = "accepted: candidate is best on at least one D_pareto task"
                reason_codes[candidate.candidate_id] = "TASK_BEST"
                continue
            if self._improves_parent(candidate, judgment, previous_matrix):
                accepted.append(candidate.candidate_id)
                reasons[candidate.candidate_id] = "accepted: D_pareto aggregate score improves over parent"
                reason_codes[candidate.candidate_id] = "PARENT_IMPROVER"
                continue
            discarded.append(candidate.candidate_id)
            reasons[candidate.candidate_id] = "discarded: no D_pareto parent or task-best improvement"
            reason_codes[candidate.candidate_id] = "NOT_PARENT_OR_TASK_BEST_IMPROVER"

        return GateDecision(
            round_id=round_id,
            accepted=accepted,
            discarded=discarded,
            reason_by_candidate=reasons,
            reason_code_by_candidate=reason_codes,
        )


    def _candidate_eligible(self, judgment: Judgment, config: dict) -> tuple[bool, str]:
        if not judgment.passed:
            return False, "discarded: judgment did not pass required validation/quality gates"
        hard_categories = set(config.get("gepa", {}).get("gate_hard_failure_categories") or DEFAULT_HARD_FAILURE_CATEGORIES)
        matched = sorted(category for category in judgment.failure_categories if category in hard_categories)
        if matched:
            return False, "discarded: hard failure categories present: " + ", ".join(matched)
        return True, "eligible"

    def _eligible_judgment(self, judgment: Judgment, config: dict) -> bool:
        eligible, _ = self._candidate_eligible(judgment, config)
        return eligible

    def _task_best_ids(self, matrix: ScoreMatrix) -> set[str]:
        best_ids: set[str] = set()
        for scores in matrix.task_scores.values():
            if not scores:
                continue
            best = max(scores.values())
            best_ids.update(candidate_id for candidate_id, score in scores.items() if score == best)
        return best_ids

    def _improves_parent(self, candidate: Candidate, judgment: Judgment, previous_matrix: ScoreMatrix) -> bool:
        parent_scores = [
            previous_matrix.aggregate_scores[parent_id]
            for parent_id in candidate.parent_ids
            if parent_id in previous_matrix.aggregate_scores
        ]
        if not parent_scores:
            return False
        return float(judgment.score) > max(parent_scores)


def _reason_code_for_ineligible(judgment: Judgment) -> str:
    if not judgment.passed:
        return "JUDGMENT_FAILED"
    matched = sorted(category for category in judgment.failure_categories if category in DEFAULT_HARD_FAILURE_CATEGORIES)
    if matched:
        return "HARD_FAILURE_CATEGORY"
    return "JUDGMENT_INELIGIBLE"
