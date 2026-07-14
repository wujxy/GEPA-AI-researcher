from __future__ import annotations

import threading
from pathlib import Path

from .io_utils import append_jsonl, read_json, write_json
from ..domain.candidate import CandidateCard


class CandidateStore:
    def __init__(self, run_dir: Path):
        self.run_dir = Path(run_dir)
        self.root = self.run_dir / "candidates"
        self.index_path = self.run_dir / "candidates.jsonl"
        self._lock = threading.RLock()

    def save(self, card: CandidateCard) -> None:
        card.validate_invariants()
        payload = card.to_dict()
        with self._lock:
            write_json(self.root / f"{card.candidate_id}.json", payload)
            append_jsonl(self.index_path, payload)

    def get(self, candidate_id: str) -> CandidateCard | None:
        with self._lock:
            path = self.root / f"{candidate_id}.json"
            if not path.exists():
                return None
            return CandidateCard.from_dict(read_json(path))

    def list_by_round(self, round_id: int) -> list[CandidateCard]:
        return [card for card in self._all_cards() if card.round_id == round_id]

    def list_children(self, parent_candidate_id: str) -> list[CandidateCard]:
        return [
            card
            for card in self._all_cards()
            if parent_candidate_id in set(card.parent_candidate_ids)
        ]

    def list_all(self) -> list[CandidateCard]:
        return self._all_cards()

    def _all_cards(self) -> list[CandidateCard]:
        with self._lock:
            if not self.root.exists():
                return []
            cards = [
                CandidateCard.from_dict(read_json(path))
                for path in sorted(self.root.glob("*.json"))
            ]
            return sorted(cards, key=lambda card: (card.round_id, card.candidate_id))
