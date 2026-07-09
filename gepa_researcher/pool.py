from __future__ import annotations

from pathlib import Path

from .io_utils import append_jsonl, read_json, write_json
from .schemas import Candidate, CandidatePoolSnapshot


class CandidatePool:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.active: dict[str, Candidate] = {}
        self.accepted_ids: list[str] = []
        self.discarded_ids: list[str] = []

    @classmethod
    def load(cls, run_dir: Path) -> "CandidatePool":
        pool = cls(run_dir)
        path = run_dir / "candidate_pool.json"
        if not path.exists():
            return pool
        data = read_json(path)
        for candidate_id, candidate_data in data.get("candidates", {}).items():
            pool.active[candidate_id] = Candidate(**candidate_data)
        pool.accepted_ids = list(data.get("accepted_candidate_ids", []))
        pool.discarded_ids = list(data.get("discarded_candidate_ids", []))
        return pool

    def active_ids(self) -> list[str]:
        return list(self.active)

    def get(self, candidate_id: str) -> Candidate | None:
        return self.active.get(candidate_id)

    def add_accepted(self, candidate: Candidate) -> None:
        candidate.status = "accepted"
        self.active[candidate.candidate_id] = candidate
        if candidate.candidate_id not in self.accepted_ids:
            self.accepted_ids.append(candidate.candidate_id)
        append_jsonl(self.run_dir / "accepted_candidates.jsonl", candidate.to_dict())

    def add_discarded(self, candidate: Candidate, reason: str) -> None:
        candidate.status = "discarded"
        if candidate.candidate_id not in self.discarded_ids:
            self.discarded_ids.append(candidate.candidate_id)
        data = candidate.to_dict()
        data["discard_reason"] = reason
        append_jsonl(self.run_dir / "discarded_candidates.jsonl", data)

    def snapshot(self) -> CandidatePoolSnapshot:
        candidates = {candidate_id: candidate.to_dict() for candidate_id, candidate in self.active.items()}
        ancestry = {
            candidate_id: list(candidate.parent_ids)
            for candidate_id, candidate in self.active.items()
        }
        return CandidatePoolSnapshot(
            active_candidate_ids=self.active_ids(),
            accepted_candidate_ids=list(self.accepted_ids),
            discarded_candidate_ids=list(self.discarded_ids),
            ancestry=ancestry,
            candidates=candidates,
        )

    def persist(self) -> None:
        (self.run_dir / "accepted_candidates.jsonl").parent.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "accepted_candidates.jsonl").touch(exist_ok=True)
        (self.run_dir / "discarded_candidates.jsonl").touch(exist_ok=True)
        write_json(self.run_dir / "candidate_pool.json", self.snapshot().to_dict())
