import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from gepa_researcher.admission import CandidateAdmissionGate
from gepa_researcher.agent_client import AgentError, ClaudeCodeClient
from gepa_researcher.adapters import ExecutorAdapter
from gepa_researcher.provenance import ProvenanceVerifier
from gepa_researcher.registry import ExecutionRegistry
from gepa_researcher.schemas import (
    AgentCallContext,
    AgentCallRecord,
    Candidate,
    ExecutionRecord,
    ProvenanceReport,
    TokenUsage,
    SampleTrace,
    Trace,
)
from gepa_researcher.usage import UsageTracker, normalize_usage
from gepa_researcher.workspace import WorkspaceManager


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return completed.stdout.strip()


def _make_repo(root: Path) -> tuple[Path, str]:
    repo = root / "source"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "GEPA Test")
    (repo / "src").mkdir()
    (repo / "src" / "hot.cc").write_text("int hot() { return 1; }\n", encoding="utf-8")
    (repo / "CMakeLists.txt").write_text("project(fake)\n", encoding="utf-8")
    (repo / ".gitignore").write_text("build/\nInstallArea/\nTEMP/\nlogs/\nmetrics/\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "baseline")
    return repo, _git(repo, "rev-parse", "HEAD")


def _candidate(candidate_id: str = "cand_000_000", parent_id: str | None = None) -> Candidate:
    return Candidate(
        candidate_id=candidate_id,
        round_id=0,
        parent_id=parent_id,
        parent_ids=[parent_id] if parent_id else [],
        hypothesis="inline hot function",
        scope="src/hot.cc",
        proposed_change="inline the hot function",
        rationale="reduce calls",
        expected_improvement="speed",
        risk="low",
        prompt_text="prompt",
        created_at="now",
        target_files=["src/hot.cc"],
        safety_class="safe",
        strategy="safe-pattern #1",
    )


class AdmissionGateTest(unittest.TestCase):
    def test_rejects_frozen_build_tuning_before_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, baseline = _make_repo(Path(tmp))
            candidate = _candidate()
            candidate.target_files = ["CMakeLists.txt"]
            candidate.scope = "CMakeLists.txt"
            candidate.proposed_change = "modify CMakeLists.txt for PGO"
            candidate.artifacts["candidate_class"] = "build-tuning"
            config = {
                "workspace": {"repo_path": str(repo), "baseline_ref": baseline},
                "candidate_policy": {
                    "allowed_target_globs": ["src/**/*.cc"],
                    "frozen_globs": ["CMakeLists.txt"],
                    "allowed_safety_classes": ["safe"],
                    "allowed_strategies": ["safe-pattern #1"],
                    "allowed_candidate_classes": ["safe-source"],
                },
            }

            decision = CandidateAdmissionGate().evaluate(
                candidate,
                config,
                accepted_parent_ids=set(),
                batch_candidate_ids={candidate.candidate_id},
            )

            self.assertFalse(decision.admitted)
            self.assertIn("FROZEN_PATH", decision.failure_codes)
            self.assertIn("DISALLOWED_CANDIDATE_CLASS", decision.failure_codes)

    def test_accepts_root_file_with_double_star_glob(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, baseline = _make_repo(Path(tmp))
            candidate = _candidate()
            config = {
                "workspace": {"repo_path": str(repo), "baseline_ref": baseline},
                "candidate_policy": {
                    "allowed_target_globs": ["src/**/*.cc"],
                    "frozen_globs": [],
                    "allowed_safety_classes": ["safe"],
                    "allowed_strategies": ["safe-pattern #1"],
                },
            }
            decision = CandidateAdmissionGate().evaluate(
                candidate,
                config,
                accepted_parent_ids=set(),
                batch_candidate_ids={candidate.candidate_id},
            )
            self.assertTrue(decision.admitted, decision.to_dict())


class WorkspaceAndProvenanceTest(unittest.TestCase):
    def test_parallel_candidates_get_independent_worktrees_and_main_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, baseline = _make_repo(root)
            before_head = _git(repo, "rev-parse", "HEAD")
            before_status = _git(repo, "status", "--porcelain")
            run_dir = root / "run"
            config = {
                "workspace": {
                    "mode": "git_worktree",
                    "repo_path": str(repo),
                    "root": str(run_dir / "worktrees"),
                    "baseline_ref": baseline,
                    "branch_prefix": "gepa/test",
                },
                "candidate_policy": {"max_commits": 1},
            }
            manager = WorkspaceManager(run_dir, config)
            a = manager.prepare(_candidate("cand_a"))
            b = manager.prepare(_candidate("cand_b"))

            self.assertNotEqual(a.worktree_path, b.worktree_path)
            self.assertNotEqual(a.branch_name, b.branch_name)
            (Path(a.worktree_path) / "src" / "hot.cc").write_text("int hot() { return 2; }\n", encoding="utf-8")
            self.assertIn("return 1", (Path(b.worktree_path) / "src" / "hot.cc").read_text(encoding="utf-8"))
            self.assertEqual(_git(repo, "rev-parse", "HEAD"), before_head)
            self.assertEqual(_git(repo, "status", "--porcelain"), before_status)

    def test_verifies_one_admitted_commit_and_rejects_dirty_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, baseline = _make_repo(root)
            run_dir = root / "run"
            config = {
                "workspace": {
                    "mode": "git_worktree",
                    "repo_path": str(repo),
                    "root": str(run_dir / "worktrees"),
                    "baseline_ref": baseline,
                    "branch_prefix": "gepa/test",
                    "generated_tracked_paths": [],
                },
                "candidate_policy": {"max_commits": 1},
            }
            candidate = _candidate()
            lease = WorkspaceManager(run_dir, config).prepare(candidate)
            worktree = Path(lease.worktree_path)
            _git(worktree, "config", "user.email", "test@example.invalid")
            _git(worktree, "config", "user.name", "GEPA Test")
            (worktree / "src" / "hot.cc").write_text("int hot() { return 2; }\n", encoding="utf-8")
            _git(worktree, "add", "src/hot.cc")
            _git(worktree, "commit", "-m", "candidate")
            record = ExecutionRecord(
                execution_id="exec",
                candidate_id=candidate.candidate_id,
                round_id=0,
                parent_candidate_id=None,
                requested_parent_sha=baseline,
                actual_start_sha=baseline,
                result_sha=None,
                branch_name=lease.branch_name,
                worktree_path=lease.worktree_path,
            )

            report = ProvenanceVerifier().verify(candidate, lease, record, config)
            self.assertTrue(report.verified, report.to_dict())
            self.assertEqual(report.changed_files, ["src/hot.cc"])

            (worktree / "unexpected.txt").write_text("dirty\n", encoding="utf-8")
            dirty_report = ProvenanceVerifier().verify(candidate, lease, record, config)
            self.assertFalse(dirty_report.verified)
            self.assertIn("DIRTY_SOURCE", dirty_report.failure_codes)

    def test_registry_only_resolves_accepted_verified_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = ExecutionRegistry(Path(tmp))
            record = ExecutionRecord(
                execution_id="exec",
                candidate_id="parent",
                round_id=0,
                parent_candidate_id=None,
                requested_parent_sha="a",
                actual_start_sha="a",
                result_sha="b",
                branch_name="branch",
                worktree_path="/tmp/worktree",
            )
            registry.record_execution(record)
            registry.record_provenance(
                ProvenanceReport("exec", "parent", True, {"all": "pass"}, result_sha="b")
            )
            self.assertIsNone(registry.verified_result_sha("parent"))
            registry.mark_candidate_status("parent", "accepted")
            self.assertEqual(registry.verified_result_sha("parent"), "b")

    def test_materializes_once_then_uses_evaluate_only(self):
        class CommittingExecutor:
            def __init__(self):
                self.modes = []

            def execute(self, candidate, config):
                mode = config["_execution_mode"]
                self.modes.append(mode)
                repo = Path(config["_candidate_repo"])
                if mode == "implement_and_validate":
                    _git(repo, "config", "user.email", "test@example.invalid")
                    _git(repo, "config", "user.name", "GEPA Test")
                    (repo / "src" / "hot.cc").write_text("int hot() { return 2; }\n", encoding="utf-8")
                    _git(repo, "add", "src/hot.cc")
                    _git(repo, "commit", "-m", "candidate")
                return Trace(
                    candidate.candidate_id,
                    candidate.round_id,
                    [SampleTrace("task", "in", "out", "expected", "ok")],
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, baseline = _make_repo(root)
            run_dir = root / "run"
            config = {
                "workspace": {
                    "mode": "git_worktree",
                    "repo_path": str(repo),
                    "root": str(run_dir / "worktrees"),
                    "baseline_ref": baseline,
                    "branch_prefix": "gepa/test",
                },
                "candidate_policy": {"max_commits": 1},
                "execution": {"lifecycle": "materialize_once"},
                "executor": {"max_workers": 1},
            }
            candidate = _candidate()
            executor = CommittingExecutor()
            registry = ExecutionRegistry(run_dir)
            adapter = ExecutorAdapter(
                executor,
                run_dir,
                WorkspaceManager(run_dir, config),
                registry,
                ProvenanceVerifier(),
            )

            first = adapter.run_many([candidate], 0, config)
            self.assertFalse(first.failed_candidate_ids)
            registry.mark_candidate_status(candidate.candidate_id, "accepted")
            first_sha = registry.verified_result_sha(candidate.candidate_id)
            second = adapter.run_many([candidate], 0, config)

            self.assertFalse(second.failed_candidate_ids)
            self.assertEqual(executor.modes, ["implement_and_validate", "evaluate_only"])
            self.assertEqual(registry.verified_result_sha(candidate.candidate_id), first_sha)


class UsageTrackingTest(unittest.TestCase):
    def test_normalizes_cache_tokens_without_double_counting(self):
        usage = normalize_usage(
            {
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_creation_input_tokens": 20,
                    "cache_read_input_tokens": 30,
                }
            }
        )
        self.assertTrue(usage.available)
        self.assertEqual(usage.processed_tokens, 65)

    def test_agent_client_parses_envelope_and_persists_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "fake_claude.py"
            envelope = {
                "type": "result",
                "result": json.dumps({"ok": True}),
                "session_id": "session",
                "total_cost_usd": 0.125,
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_creation_input_tokens": 20,
                    "cache_read_input_tokens": 30,
                },
                "modelUsage": {"fake-model": {"inputTokens": 10}},
            }
            script.write_text(
                f"#!{sys.executable}\nimport json\nprint(json.dumps({envelope!r}))\n",
                encoding="utf-8",
            )
            os.chmod(script, 0o755)
            tracker = UsageTracker(root / "run", {"persist_raw_envelope": True})
            client = ClaudeCodeClient(command=str(script), timeout_seconds=5, usage_tracker=tracker)

            result = client.run_json(
                "hello",
                label="executor",
                call_context=AgentCallContext("executor", 0, "feedback", candidate_id="cand"),
            )

            self.assertEqual(result.data, {"ok": True})
            self.assertEqual(result.call_record.usage.processed_tokens, 65)
            summary = tracker.round_summary(0)
            self.assertEqual(summary.totals["processed_tokens"], 65)
            self.assertEqual(summary.by_candidate["cand"]["calls"], 1)
            self.assertTrue((root / "run" / "usage" / "agent_calls.jsonl").exists())

    def test_concurrent_records_are_complete_and_resume_is_deduplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            tracker = UsageTracker(run_dir)

            def add(index: int) -> None:
                tracker.record(
                    AgentCallRecord(
                        call_id=f"call-{index}",
                        context=AgentCallContext("executor", 0, "feedback", candidate_id=f"cand-{index % 3}"),
                        status="completed",
                        started_at="start",
                        finished_at="finish",
                        duration_ms=1,
                        usage=TokenUsage(1, 2, 3, 4, 10, True),
                    )
                )

            threads = [threading.Thread(target=add, args=(index,)) for index in range(100)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(len(tracker.records()), 100)
            self.assertEqual(tracker.round_summary(0).totals["processed_tokens"], 1000)
            resumed = UsageTracker(run_dir)
            self.assertEqual(len(resumed.records()), 100)
            resumed.record(tracker.records()[0])
            self.assertEqual(len(resumed.records()), 100)
            self.assertEqual(len((run_dir / "usage" / "agent_calls.jsonl").read_text().splitlines()), 100)


if __name__ == "__main__":
    unittest.main()
