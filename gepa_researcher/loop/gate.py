from __future__ import annotations

from ..models.schemas import Candidate, GateDecision, Judgment, ParetoFrontier, ScoreMatrix


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
            if child_score is None:
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
        task_best = self._task_best_ids(trial_matrix)

        for candidate in candidates:
            judgment = judgment_by_id.get(candidate.candidate_id)
            if judgment is None:
                discarded.append(candidate.candidate_id)
                reasons[candidate.candidate_id] = "discarded: missing judgment"
                continue
            if not had_active_pool:
                accepted.append(candidate.candidate_id)
                reasons[candidate.candidate_id] = "accepted: initial pool seed"
                continue
            if candidate.candidate_id in task_best:
                accepted.append(candidate.candidate_id)
                reasons[candidate.candidate_id] = "accepted: candidate is best on at least one D_pareto task"
                continue
            if self._improves_parent(candidate, judgment, previous_matrix):
                accepted.append(candidate.candidate_id)
                reasons[candidate.candidate_id] = "accepted: D_pareto aggregate score improves over parent"
                continue
            discarded.append(candidate.candidate_id)
            reasons[candidate.candidate_id] = "discarded: no D_pareto parent or task-best improvement"

        return GateDecision(
            round_id=round_id,
            accepted=accepted,
            discarded=discarded,
            reason_by_candidate=reasons,
        )

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
