from __future__ import annotations

import random

from .schemas import ParetoFrontier, ScoreMatrix


class ParetoSelector:
    def select(self, matrix: ScoreMatrix, active_candidate_ids: list[str]) -> ParetoFrontier:
        active = list(dict.fromkeys(active_candidate_ids))
        if not active:
            return ParetoFrontier(round_id=matrix.round_id, candidate_ids=[], per_task_best={})

        per_task_best: dict[str, list[str]] = {}
        best_ids: set[str] = set()
        win_counts: dict[str, int] = {candidate_id: 0 for candidate_id in active}
        for task_id, scores in matrix.task_scores.items():
            task_scores = {candidate_id: scores[candidate_id] for candidate_id in active if candidate_id in scores}
            if not task_scores:
                continue
            best_score = max(task_scores.values())
            winners = sorted(candidate_id for candidate_id, score in task_scores.items() if score == best_score)
            per_task_best[task_id] = winners
            best_ids.update(winners)
            for winner in winners:
                win_counts[winner] = win_counts.get(winner, 0) + 1

        nondominated = {
            candidate_id
            for candidate_id in active
            if not self._is_dominated(candidate_id, active, matrix)
        }
        candidate_ids = sorted(best_ids | nondominated)
        return ParetoFrontier(
            round_id=matrix.round_id,
            candidate_ids=candidate_ids,
            per_task_best=per_task_best,
            artifacts={"active_candidate_count": len(active), "win_counts": win_counts},
        )

    def sample_parent_ids(self, frontier: ParetoFrontier, max_parents: int, seed: int = 0) -> list[str]:
        ids = list(frontier.candidate_ids)
        if not ids or max_parents <= 0:
            return []
        weights_by_id = dict(frontier.artifacts.get("win_counts", {}))
        rng = random.Random(seed)
        chosen: list[str] = []
        remaining = list(ids)
        while remaining and len(chosen) < max_parents:
            weights = [max(1, int(weights_by_id.get(candidate_id, 0))) for candidate_id in remaining]
            total = sum(weights)
            pick = rng.uniform(0, total)
            cursor = 0.0
            selected = remaining[-1]
            for candidate_id, weight in zip(remaining, weights):
                cursor += weight
                if pick <= cursor:
                    selected = candidate_id
                    break
            chosen.append(selected)
            remaining.remove(selected)
        return chosen

    def _is_dominated(self, candidate_id: str, active: list[str], matrix: ScoreMatrix) -> bool:
        for other_id in active:
            if other_id == candidate_id:
                continue
            if self._dominates(other_id, candidate_id, matrix):
                return True
        return False

    def _dominates(self, left_id: str, right_id: str, matrix: ScoreMatrix) -> bool:
        saw_strict = False
        task_ids = list(matrix.task_scores) or ["aggregate"]
        for task_id in task_ids:
            scores = matrix.task_scores.get(task_id, {})
            left = scores.get(left_id, float("-inf"))
            right = scores.get(right_id, float("-inf"))
            if left < right:
                return False
            if left > right:
                saw_strict = True
        return saw_strict
