from __future__ import annotations

import unittest

from gepa_researcher.domain.candidate import CandidateCard, CandidateStatus, ProposalIdea
from gepa_researcher.domain.execution import (
    CapabilityPolicy,
    ExecutionBudget,
    ExecutionPhase,
    ExecutionRecord,
    ExecutionSpec,
    ExecutionStatus,
)
from gepa_researcher.domain.revision import RevisionRef
from gepa_researcher.models.schemas import Candidate


class CandidateExecutionDomainTest(unittest.TestCase):
    def test_candidate_card_serializes_without_workspace_identity(self):
        proposal = ProposalIdea(
            proposal_id="proposal-1",
            hypothesis="inline hot function",
            scope="src/hot.cc",
            proposed_change="replace call with inline body",
            rationale="reduce call overhead",
            expected_improvement="lower latency",
            risk="low",
            prompt_text="Strategy: inline",
            target_files=("src/hot.cc",),
            executor_contract={"instructions": "edit src/hot.cc and run benchmark"},
            expected_artifacts=("benchmark.json",),
            metadata={"strategy": "safe-pattern #1"},
        )
        card = CandidateCard(
            candidate_id="cand_001_000",
            round_id=1,
            parent_candidate_ids=("seed_000",),
            proposal_id=proposal.proposal_id,
            proposal=proposal,
            base_revision="a" * 40,
            status=CandidateStatus.ADMITTED,
        )

        data = card.to_dict()

        self.assertEqual(data["base_revision"], "a" * 40)
        self.assertEqual(data["execution_ids"], [])
        self.assertNotIn("workspace_path", str(data))
        self.assertNotIn("worktree_path", str(data))
        self.assertEqual(CandidateCard.from_dict(data).proposal.hypothesis, "inline hot function")

    def test_execution_spec_and_record_are_execution_id_keyed(self):
        spec = ExecutionSpec(
            execution_id="exec-001",
            run_id="run-001",
            round_id=1,
            candidate_id="cand_001_000",
            phase=ExecutionPhase.IMPLEMENTATION,
            input_revision="a" * 40,
            dataset_ref=None,
            evaluator_version=None,
            budget=ExecutionBudget(wall_seconds=600, max_tokens=None, max_files_changed=3, max_commands=None),
            capability_policy=CapabilityPolicy(
                repo_writable=True,
                network_allowed=False,
                allowed_tools=("bash", "git"),
                forbidden_paths=("tests/**",),
            ),
        )
        record = ExecutionRecord.from_spec(spec)

        self.assertEqual(record.execution_id, "exec-001")
        self.assertEqual(record.status, ExecutionStatus.PENDING)
        self.assertEqual(record.input_revision, "a" * 40)
        self.assertEqual(record.phase, ExecutionPhase.IMPLEMENTATION)

    def test_revision_ref_rejects_non_sha_values(self):
        self.assertEqual(RevisionRef.validate_sha("b" * 40), "b" * 40)
        with self.assertRaises(ValueError):
            RevisionRef.validate_sha("/tmp/worktree")

    def test_proposal_idea_can_be_lifted_from_legacy_candidate(self):
        candidate = Candidate(
            candidate_id="cand_001_000",
            round_id=1,
            hypothesis="cache geometry",
            scope="src/geometry.cc",
            proposed_change="cache geometry constants",
            rationale="avoid recomputation",
            expected_improvement="latency",
            risk="stale cache",
            prompt_text="Strategy: cache",
            created_at="now",
            executor_contract={"instructions": "implement and benchmark"},
            expected_artifacts=["benchmark.json"],
            target_files=["src/geometry.cc"],
            artifacts={"strategy": "cache", "analysis_plan": ["inspect geometry"]},
        )

        proposal = ProposalIdea.from_candidate(candidate)

        self.assertEqual(proposal.proposal_id, "cand_001_000")
        self.assertEqual(proposal.target_files, ("src/geometry.cc",))
        self.assertEqual(proposal.metadata["strategy"], "cache")
        self.assertEqual(proposal.metadata["analysis_plan"], ["inspect geometry"])


if __name__ == "__main__":
    unittest.main()
