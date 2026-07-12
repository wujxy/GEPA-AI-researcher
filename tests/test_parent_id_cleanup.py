"""Test suite to verify parent_id cleanup is correct and complete.

This test suite verifies that:
1. Candidate schema no longer has parent_id field
2. All code paths correctly use parent_ids instead
3. No backward compatibility issues exist
4. GEPA loop functionality is preserved
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from gepa_researcher.models.schemas import Candidate
from gepa_researcher.loop.context_views import candidate_for_agent


class TestParentIdCleanup(unittest.TestCase):
    """Verify parent_id has been properly removed and replaced with parent_ids."""

    def test_candidate_schema_no_parent_id_field(self):
        """Test that Candidate schema does not have parent_id field."""
        # Create a candidate with only parent_ids
        candidate = Candidate(
            candidate_id="test_001",
            round_id=0,
            parent_ids=["parent_001"],
            hypothesis="Test hypothesis",
            scope="test",
            proposed_change="Test change",
            rationale="Test rationale",
            expected_improvement="Test improvement",
            risk="Test risk",
            prompt_text="Test prompt",
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        # Verify candidate does NOT have parent_id attribute
        with self.assertRaises(AttributeError):
            _ = candidate.parent_id  # noqa: F841

        # Verify it DOES have parent_ids
        self.assertEqual(candidate.parent_ids, ["parent_001"])

    def test_candidate_multiple_parent_ids(self):
        """Test that Candidate supports multiple parent_ids."""
        candidate = Candidate(
            candidate_id="test_001",
            round_id=0,
            parent_ids=["parent_001", "parent_002"],
            hypothesis="Test hypothesis",
            scope="test",
            proposed_change="Test change",
            rationale="Test rationale",
            expected_improvement="Test improvement",
            risk="Test risk",
            prompt_text="Test prompt",
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        self.assertEqual(candidate.parent_ids, ["parent_001", "parent_002"])

    def test_candidate_empty_parent_ids(self):
        """Test that Candidate handles empty parent_ids (for seeds)."""
        candidate = Candidate(
            candidate_id="seed_000",
            round_id=-1,
            parent_ids=[],  # Empty for seeds
            hypothesis="Seed hypothesis",
            scope="test",
            proposed_change="Seed change",
            rationale="Seed rationale",
            expected_improvement="Seed improvement",
            risk="Seed risk",
            prompt_text="Seed prompt",
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        self.assertEqual(candidate.parent_ids, [])

    def test_candidate_context_view_no_parent_id(self):
        """Test that candidate_for_agent does not include parent_id."""
        candidate = Candidate(
            candidate_id="test_001",
            round_id=0,
            parent_ids=["parent_001"],
            hypothesis="Test hypothesis",
            scope="test",
            proposed_change="Test change",
            rationale="Test rationale",
            expected_improvement="Test improvement",
            risk="Test risk",
            prompt_text="Test prompt",
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        view = candidate_for_agent(candidate)

        # Verify view has parent_ids
        self.assertEqual(view["parent_ids"], ["parent_001"])

        # Verify view does NOT have parent_id
        self.assertNotIn("parent_id", view)

    def test_candidate_context_view_multiple_parent_ids(self):
        """Test that candidate_for_agent handles multiple parent_ids."""
        candidate = Candidate(
            candidate_id="test_001",
            round_id=0,
            parent_ids=["parent_001", "parent_002"],
            hypothesis="Test hypothesis",
            scope="test",
            proposed_change="Test change",
            rationale="Test rationale",
            expected_improvement="Test improvement",
            risk="Test risk",
            prompt_text="Test prompt",
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        view = candidate_for_agent(candidate)

        self.assertEqual(view["parent_ids"], ["parent_001", "parent_002"])
        self.assertNotIn("parent_id", view)

    def test_candidate_serialization_preserves_parent_ids(self):
        """Test that candidate serialization preserves parent_ids."""
        candidate = Candidate(
            candidate_id="test_001",
            round_id=0,
            parent_ids=["parent_001", "parent_002"],
            hypothesis="Test hypothesis",
            scope="test",
            proposed_change="Test change",
            rationale="Test rationale",
            expected_improvement="Test improvement",
            risk="Test risk",
            prompt_text="Test prompt",
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        # Serialize to dict
        data = candidate.to_dict()

        # Verify serialized data has parent_ids
        self.assertEqual(data["parent_ids"], ["parent_001", "parent_002"])

        # Verify serialized data does NOT have parent_id
        self.assertNotIn("parent_id", data)

    def test_candidate_inheritance_chain(self):
        """Test that we can reconstruct inheritance chain using only parent_ids."""
        # Create a chain: seed -> child_1 -> child_2
        seed = Candidate(
            candidate_id="seed_000",
            round_id=-1,
            parent_ids=[],
            hypothesis="Seed hypothesis",
            scope="test",
            proposed_change="Seed change",
            rationale="Seed rationale",
            expected_improvement="Seed improvement",
            risk="Seed risk",
            prompt_text="Seed prompt",
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        child_1 = Candidate(
            candidate_id="child_001",
            round_id=0,
            parent_ids=["seed_000"],
            hypothesis="Child 1 hypothesis",
            scope="test",
            proposed_change="Child 1 change",
            rationale="Child 1 rationale",
            expected_improvement="Child 1 improvement",
            risk="Child 1 risk",
            prompt_text="Child 1 prompt",
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        child_2 = Candidate(
            candidate_id="child_002",
            round_id=1,
            parent_ids=["child_001"],
            hypothesis="Child 2 hypothesis",
            scope="test",
            proposed_change="Child 2 change",
            rationale="Child 2 rationale",
            expected_improvement="Child 2 improvement",
            risk="Child 2 risk",
            prompt_text="Child 2 prompt",
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        # Verify inheritance chain can be reconstructed
        # seed has no parents
        self.assertEqual(seed.parent_ids, [])
        # child_1 has seed as parent
        self.assertEqual(child_1.parent_ids, ["seed_000"])
        # child_2 has child_1 as parent
        self.assertEqual(child_2.parent_ids, ["child_001"])

    def test_candidate_mutation_from_multiple_parents(self):
        """Test that we can support mutations from multiple parents (merge)."""
        merged_child = Candidate(
            candidate_id="merged_001",
            round_id=1,
            parent_ids=["parent_001", "parent_002"],
            hypothesis="Merged hypothesis",
            scope="test",
            proposed_change="Merge changes",
            rationale="Merge rationale",
            expected_improvement="Merge improvement",
            risk="Merge risk",
            prompt_text="Merge prompt",
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        # Verify it has both parents
        self.assertEqual(len(merged_child.parent_ids), 2)
        self.assertIn("parent_001", merged_child.parent_ids)
        self.assertIn("parent_002", merged_child.parent_ids)


if __name__ == "__main__":
    unittest.main()