import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from gepa_researcher.loop.admission import CandidateAdmissionGate
from gepa_researcher.agents.agent_client import AgentError, ClaudeCodeClient
from gepa_researcher.agents.adapters import JudgerAdapter
from gepa_researcher.domain.candidate import CandidateCard, CandidateStatus, ProposalIdea
from gepa_researcher.domain.execution import CapabilityPolicy, ExecutionBudget, ExecutionPhase, ExecutionSpec
from gepa_researcher.execution.git_result import GitResultService
from gepa_researcher.execution.materializer import RepositoryMaterializer
from gepa_researcher.storage.provenance import audit_commit
from gepa_researcher.models.schemas import (
    AgentCallContext,
    AgentCallRecord,
    Candidate,
    Judgment,
    TokenUsage,
    SampleTrace,
    Trace,
    TraceBatch,
)
from gepa_researcher.services.execution_service import ExecutionService
from gepa_researcher.storage.execution_store import ExecutionStore
from gepa_researcher.storage.usage import UsageTracker, normalize_usage
from gepa_researcher.execution.workspace import WorkspaceManager


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


def _card(candidate_id: str, baseline: str) -> CandidateCard:
    candidate = _candidate(candidate_id)
    proposal = ProposalIdea.from_candidate(candidate)
    return CandidateCard(
        candidate_id=candidate_id,
        round_id=0,
        parent_candidate_ids=(),
        proposal_id=proposal.proposal_id,
        proposal=proposal,
        base_revision=baseline,
        status=CandidateStatus.ADMITTED,
    )


def _spec(card: CandidateCard, baseline: str) -> ExecutionSpec:
    return ExecutionSpec(
        execution_id=f"exec-{card.candidate_id}",
        run_id="run",
        round_id=card.round_id,
        candidate_id=card.candidate_id,
        phase=ExecutionPhase.IMPLEMENTATION,
        input_revision=baseline,
        dataset_ref=None,
        evaluator_version=None,
        budget=ExecutionBudget(wall_seconds=600),
        capability_policy=CapabilityPolicy(repo_writable=True, network_allowed=False),
    )


def _execution_service(root: Path, repo: Path, baseline: str, runner, frozen_globs: list[str] | None = None) -> ExecutionService:
    run_dir = root / "run"
    return ExecutionService(
        run_dir=run_dir,
        config={"executor": {"runtime_backend": "local"}},
        materializer=RepositoryMaterializer(
            run_dir=run_dir,
            workspace_config={
                "mode": "git_worktree",
                "repo_path": str(repo),
                "root": str(run_dir / "sandboxes"),
                "baseline_ref": baseline,
                "branch_prefix": "gepa/test",
            },
        ),
        execution_store=ExecutionStore(run_dir),
        git_result_service=GitResultService({"frozen_globs": list(frozen_globs or [])}),
        runner=runner,
    )


class AdmissionGateTest(unittest.TestCase):
    def test_rejects_frozen_path_before_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, baseline = _make_repo(Path(tmp))
            candidate = _candidate()
            candidate.target_files = ["CMakeLists.txt"]
            candidate.scope = "CMakeLists.txt"
            candidate.proposed_change = "modify CMakeLists.txt for PGO"
            config = {
                "workspace": {"repo_path": str(repo), "baseline_ref": baseline},
                "candidate_policy": {
                    "allowed_target_globs": ["src/**/*.cc"],
                    "frozen_globs": ["CMakeLists.txt"],
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

    def test_accepts_root_file_with_double_star_glob(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, baseline = _make_repo(Path(tmp))
            candidate = _candidate()
            candidate.strategy = "safe-pattern #1 (pure code motion)"
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

    def test_accepts_strategy_case_and_spacing_variant(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, baseline = _make_repo(Path(tmp))
            candidate = _candidate()
            candidate.strategy = "Safe pattern #1 (pure code motion) - inline tiny pure function"
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

    def test_target_may_exist_in_baseline_even_if_controller_branch_lacks_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, baseline = _make_repo(Path(tmp))
            _git(repo, "rm", "src/hot.cc")
            _git(repo, "commit", "-m", "controller branch removes target")
            self.assertFalse((repo / "src" / "hot.cc").exists())
            candidate = _candidate()
            config = {
                "workspace": {
                    "repo_path": str(repo),
                    "baseline_ref": baseline,
                },
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
    def test_controller_snapshot_ignores_editor_debris_but_not_source_changes(self):
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
            }
            manager = WorkspaceManager(run_dir, config)
            snapshot = manager.controller_snapshot()

            cache_dir = repo / ".cache" / "clangd" / "index"
            cache_dir.mkdir(parents=True)
            (cache_dir / "hot.cc.idx").write_text("index\n", encoding="utf-8")
            (repo / "judgement.json").write_text("{}\n", encoding="utf-8")
            manager.assert_controller_unchanged(snapshot)

            (repo / "src" / "external.cc").write_text("int external() { return 1; }\n", encoding="utf-8")
            with self.assertRaisesRegex(Exception, "controller repository changed"):
                manager.assert_controller_unchanged(snapshot)

    def test_protect_controller_recovers_stale_lock_and_readonly_checkout(self):
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
            }
            manager = WorkspaceManager(run_dir, config)
            lock = repo / ".gepa-running.lock"
            lock.write_text("GEPA controller checkout is in use.\npid=99999999\n", encoding="utf-8")
            for path in [repo, lock, repo / "src", repo / "src" / "hot.cc"]:
                path.chmod(path.stat().st_mode & ~0o222)

            with manager.protect_controller():
                self.assertTrue(lock.exists())
                self.assertIn(f"pid={os.getpid()}", lock.read_text(encoding="utf-8"))
                self.assertEqual(repo.stat().st_mode & 0o222, 0)

            self.assertFalse(lock.exists())
            self.assertNotEqual(repo.stat().st_mode & 0o200, 0)
            self.assertNotEqual((repo / "src" / "hot.cc").stat().st_mode & 0o200, 0)

    def test_protect_controller_write_protects_worktree_and_restores_modes(self):
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
            }
            manager = WorkspaceManager(run_dir, config)
            repo_mode_before = repo.stat().st_mode
            source_mode_before = (repo / "src").stat().st_mode

            with manager.protect_controller():
                self.assertTrue((repo / ".gepa-running.lock").exists())
                self.assertEqual(repo.stat().st_mode & 0o222, 0)
                self.assertEqual((repo / "src").stat().st_mode & 0o222, 0)
                with self.assertRaises(OSError):
                    (repo / "clangd.tmp").write_text("cache\n", encoding="utf-8")

            self.assertFalse((repo / ".gepa-running.lock").exists())
            self.assertEqual(repo.stat().st_mode, repo_mode_before)
            self.assertEqual((repo / "src").stat().st_mode, source_mode_before)

    def test_audit_commit_records_changed_files_and_ignores_runtime_debris(self):
        # The commit audit looks only at the delivered commit, never the working
        # tree. Untracked runtime debris (build output, benchmark CSV, pytest
        # cache) and modified-but-uncommitted files are not signals, so they do
        # not produce frozen violations or any failure.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, baseline = _make_repo(root)
            run_dir = root / "run"
            config = {
                "workspace": {
                    "mode": "git_worktree",
                    "repo_path": str(repo),
                    "root": str(run_dir / "sandboxes"),
                    "baseline_ref": baseline,
                    "branch_prefix": "gepa/test",
                },
            }
            card = _card("cand_000_000", baseline)
            session = RepositoryMaterializer(run_dir, config["workspace"]).materialize(_spec(card, baseline))
            worktree = session.repo_path
            _git(worktree, "config", "user.email", "test@example.invalid")
            _git(worktree, "config", "user.name", "GEPA Test")
            (worktree / "src" / "hot.cc").write_text("int hot() { return 2; }\n", encoding="utf-8")
            _git(worktree, "add", "src/hot.cc")
            _git(worktree, "commit", "-m", "candidate")

            # Debris: an untracked file plus a modified-but-uncommitted tracked file.
            (worktree / "unexpected.txt").write_text("dirty\n", encoding="utf-8")
            (worktree / "src" / "hot.cc").write_text("int hot() { return 3; }\n", encoding="utf-8")

            audit = audit_commit(repo=worktree, parent_sha=baseline, frozen_globs=["tests/**"])
            self.assertEqual(audit.changed_files, ["src/hot.cc"])
            self.assertEqual(audit.frozen_violations, [])
            self.assertEqual(audit.commit_count, 1)
            self.assertIsNotNone(audit.result_sha)

    def test_audit_commit_flags_frozen_path_edits(self):
        # The one retained hard guard: a commit that touches a frozen path is a
        # silent-corruption risk and must be flagged regardless of what the
        # candidate declared in target_files.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, baseline = _make_repo(root)
            run_dir = root / "run"
            config = {
                "workspace": {
                    "mode": "git_worktree",
                    "repo_path": str(repo),
                    "root": str(run_dir / "sandboxes"),
                    "baseline_ref": baseline,
                    "branch_prefix": "gepa/test",
                },
            }
            card = _card("cand_000_000", baseline)
            session = RepositoryMaterializer(run_dir, config["workspace"]).materialize(_spec(card, baseline))
            worktree = session.repo_path
            _git(worktree, "config", "user.email", "test@example.invalid")
            _git(worktree, "config", "user.name", "GEPA Test")
            (worktree / "src" / "hot.cc").write_text("int hot() { return 2; }\n", encoding="utf-8")
            (worktree / "tests").mkdir(exist_ok=True)
            (worktree / "tests" / "fixture.root").write_text("tampered\n", encoding="utf-8")
            _git(worktree, "add", "src/hot.cc", "tests/fixture.root")
            _git(worktree, "commit", "-m", "candidate")

            audit = audit_commit(repo=worktree, parent_sha=baseline, frozen_globs=["tests/**"])
            self.assertIn("tests/fixture.root", audit.frozen_violations)

    def test_br101_clean_source_with_runtime_debris_is_judged_normally(self):
        # Regression for the br101 incident: a candidate that commits clean
        # in-scope source and passes validation, but leaves untracked runtime
        # debris / a modified benchmark CSV and reports metric=None (e.g. a
        # broken benchmark harness) must NOT be force-zeroed or killed. With the
        # provenance layer removed it is simply audited (no frozen violation) and
        # handed to the judger, which scores it on the metric-unknown signal.
        class DebrisLeavingRunner:
            def run(self, card, spec, runtime_lease, session, config):
                repo = session.repo_path
                _git(repo, "config", "user.email", "test@example.invalid")
                _git(repo, "config", "user.name", "GEPA Test")
                (repo / "src" / "hot.cc").write_text("int hot() { return 2; }\n", encoding="utf-8")
                _git(repo, "add", "src/hot.cc")
                _git(repo, "commit", "-m", "candidate")
                # Runtime debris the executor does NOT commit (the br101 scene).
                (repo / "benchmarks.csv").write_text("commit,ms_evt\nabc,162.0\n", encoding="utf-8")
                return Trace(
                    card.candidate_id,
                    card.round_id,
                    [
                        SampleTrace(
                            "task_execution",
                            "in",
                            str({"metrics": {"primary": None}}),
                            "unknown",
                            "built + test_fcn green; benchmark harness broke",
                            artifacts={
                                "metrics": {"primary": None, "baseline": None, "delta": None},
                                "validation": {"passed": True, "checks": [], "regressions": []},
                            },
                        )
                    ],
                )

        class _Judger:
            def judge(self, candidate, trace, config):
                return Judgment(
                    candidate_id=candidate.candidate_id,
                    round_id=candidate.round_id,
                    score=0.4,
                    passed=False,
                    per_sample_scores=[],
                    failure_categories=["metric_unknown"],
                    actionable_feedback=["benchmark harness failed; retry measurement"],
                    confidence="medium",
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, baseline = _make_repo(root)
            candidate = _candidate()
            card = _card(candidate.candidate_id, baseline)
            service = _execution_service(root, repo, baseline, DebrisLeavingRunner())
            record, trace = service.execute(_spec(card, baseline), card)
            trace_batch = TraceBatch(round_id=0, traces=[trace], failed_candidate_ids=[])
            self.assertIsNotNone(record.result_revision)

            sample = trace.samples[0]
            # Core regression: clean source + debris is not a frozen violation,
            # so no failure is stamped and the judger is called normally.
            self.assertEqual(sample.artifacts["commit_audit"]["frozen_violations"], [])
            self.assertNotEqual(sample.artifacts.get("failure_category"), "frozen_violation")

            judgment = JudgerAdapter(_Judger()).evaluate_many([candidate], trace_batch, {}).judgments[0]
            self.assertNotIn("frozen_violation", judgment.failure_categories)
            self.assertNotEqual(judgment.score, 0.0)

    def test_frozen_path_edit_is_hard_rejected(self):
        # The one retained hard reject: an executor that edits a frozen path is
        # force-zeroed by the JudgerAdapter and never reaches the real judger.
        class FrozenEditingRunner:
            def run(self, card, spec, runtime_lease, session, config):
                repo = session.repo_path
                _git(repo, "config", "user.email", "test@example.invalid")
                _git(repo, "config", "user.name", "GEPA Test")
                (repo / "src" / "hot.cc").write_text("int hot() { return 2; }\n", encoding="utf-8")
                (repo / "tests").mkdir(exist_ok=True)
                (repo / "tests" / "fixture.root").write_text("tampered\n", encoding="utf-8")
                _git(repo, "add", "src/hot.cc", "tests/fixture.root")
                _git(repo, "commit", "-m", "candidate")
                return Trace(
                    card.candidate_id,
                    card.round_id,
                    [SampleTrace("task_execution", "in", "out", "expected", "ok")],
                )

        class _ExplodingJudger:
            def judge(self, candidate, trace, config):
                raise AssertionError("judger must not be called for a frozen-path violation")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, baseline = _make_repo(root)
            candidate = _candidate()
            card = _card(candidate.candidate_id, baseline)
            service = _execution_service(root, repo, baseline, FrozenEditingRunner(), frozen_globs=["tests/**"])
            _, trace = service.execute(_spec(card, baseline), card)
            trace_batch = TraceBatch(round_id=0, traces=[trace], failed_candidate_ids=[])

            sample = trace.samples[0]
            self.assertEqual(sample.artifacts["failure_category"], "frozen_violation")
            self.assertIn("tests/fixture.root", sample.artifacts["commit_audit"]["frozen_violations"])

            judgment = JudgerAdapter(_ExplodingJudger()).evaluate_many([candidate], trace_batch, {}).judgments[0]
            self.assertEqual(judgment.score, 0.0)
            self.assertFalse(judgment.passed)
            self.assertIn("frozen_violation", judgment.failure_categories)


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
