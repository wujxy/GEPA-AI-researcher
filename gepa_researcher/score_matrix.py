from __future__ import annotations

from pathlib import Path

from .io_utils import read_json, write_json
from .schemas import Judgment, JudgmentBatch, ScoreMatrix


class ScoreMatrixBuilder:
    @staticmethod
    def empty(round_id: int = 0) -> ScoreMatrix:
        return ScoreMatrix(round_id=round_id)

    @staticmethod
    def load(path: Path, round_id: int = 0) -> ScoreMatrix:
        if not path.exists():
            return ScoreMatrixBuilder.empty(round_id)
        return ScoreMatrix.from_dict(read_json(path))

    @staticmethod
    def from_judgments(round_id: int, judgments: list[Judgment], candidate_ids: set[str] | None = None) -> ScoreMatrix:
        matrix = ScoreMatrix(round_id=round_id)
        allowed = candidate_ids
        for judgment in judgments:
            if allowed is not None and judgment.candidate_id not in allowed:
                continue
            scores = judgment.per_sample_scores or [{"sample_id": "aggregate", "score": judgment.score}]
            task_values: list[float] = []
            for row in scores:
                task_id = str(row.get("sample_id") or row.get("task_id") or "aggregate")
                score = float(row.get("score", judgment.score))
                matrix.task_scores.setdefault(task_id, {})[judgment.candidate_id] = score
                task_values.append(score)
            matrix.aggregate_scores[judgment.candidate_id] = (
                sum(task_values) / len(task_values) if task_values else float(judgment.score)
            )
        return matrix

    @staticmethod
    def from_batch(batch: JudgmentBatch, candidate_ids: set[str] | None = None) -> ScoreMatrix:
        phase = batch.artifacts.get("phase", "pareto")
        if phase != "pareto":
            return ScoreMatrixBuilder.empty(batch.round_id)
        matrix = ScoreMatrixBuilder.from_judgments(batch.round_id, batch.judgments, candidate_ids)
        matrix.artifacts["phase"] = "pareto"
        matrix.artifacts["sample_ids"] = list(batch.artifacts.get("sample_ids", []))
        return matrix

    @staticmethod
    def merge(base: ScoreMatrix, update: ScoreMatrix) -> ScoreMatrix:
        merged = ScoreMatrix.from_dict(base.to_dict())
        merged.round_id = max(base.round_id, update.round_id)
        for task_id, scores in update.task_scores.items():
            merged.task_scores.setdefault(task_id, {}).update(scores)
        merged.aggregate_scores.update(update.aggregate_scores)
        merged.artifacts.update(update.artifacts)
        return merged

    @staticmethod
    def filter_candidates(matrix: ScoreMatrix, candidate_ids: set[str]) -> ScoreMatrix:
        filtered = ScoreMatrix(round_id=matrix.round_id, artifacts=dict(matrix.artifacts))
        for task_id, scores in matrix.task_scores.items():
            kept = {candidate_id: score for candidate_id, score in scores.items() if candidate_id in candidate_ids}
            if kept:
                filtered.task_scores[task_id] = kept
        filtered.aggregate_scores = {
            candidate_id: score
            for candidate_id, score in matrix.aggregate_scores.items()
            if candidate_id in candidate_ids
        }
        return filtered

    @staticmethod
    def persist(matrix: ScoreMatrix, path: Path) -> None:
        write_json(path, matrix.to_dict())
