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
from gepa_researcher.agents.adapters import ExecutorAdapter, JudgerAdapter
from gepa_researcher.storage.provenance import audit_commit
from gepa_researcher.storage.registry import ExecutionRegistry
from gepa_researcher.models.schemas import (
    AgentCallContext,
    AgentCallRecord,
    Candidate,
    ExecutionRecord,
    Judgment,
    TokenUsage,
    SampleTrace,
    Trace,
)
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

    def test_worktree_add_skips_lfs_smudge(self):
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
            captured_env = {}

            def fake_run(argv, **kwargs):
                if argv[1:3] == ["-C", str(repo)] and argv[3:6] == ["rev-parse", "--verify", f"{baseline}^{{commit}}"]:
                    return subprocess.CompletedProcess(argv, 0, stdout=baseline + "\n", stderr="")
                if "worktree" in argv and "add" in argv:
                    captured_env.update(kwargs.get("env") or {})
                    self.assertIn("filter.lfs.required=false", argv)
                    self.assertIn("filter.lfs.smudge=", argv)
                    self.assertIn("filter.lfs.process=", argv)
                    return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
                if argv[3:5] == ["rev-parse", "HEAD"]:
                    return subprocess.CompletedProcess(argv, 0, stdout=baseline + "\n", stderr="")
                return subprocess.run(argv, **kwargs)

            with patch("gepa_researcher.execution.workspace.subprocess.run", side_effect=fake_run):
                lease = WorkspaceManager(run_dir, config).prepare(_candidate())

            self.assertEqual(lease.actual_start_sha, baseline)
            self.assertEqual(captured_env.get("GIT_LFS_SKIP_SMUDGE"), "1")
            self.assertEqual(captured_env.get("GIT_TERMINAL_PROMPT"), "0")

    def test_worktree_materializes_pre_materialized_lfs_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, baseline = _make_repo(root)
            fixture_dir = repo / "tests" / "fixtures" / "v107_rev1"
            fixture_dir.mkdir(parents=True)
            (fixture_dir / "charge_pdf.bin").write_bytes(b"real-binary-fixture")
            (fixture_dir / "time_pdf.bin").write_bytes(b"another-real-fixture")
            _git(repo, "add", ".")
            _git(repo, "commit", "-m", "fixtures")
            baseline = _git(repo, "rev-parse", "HEAD")
            run_dir = root / "run"
            config = {
                "workspace": {
                    "mode": "git_worktree",
                    "repo_path": str(repo),
                    "root": str(run_dir / "worktrees"),
                    "baseline_ref": baseline,
                    "branch_prefix": "gepa/test",
                    "pre_materialized_lfs_paths": ["tests/fixtures/v107_rev1/*.bin"],
                },
            }

            lease = WorkspaceManager(run_dir, config).prepare(_candidate())

            materialized = Path(lease.worktree_path) / "tests" / "fixtures" / "v107_rev1"
            self.assertEqual((materialized / "charge_pdf.bin").read_bytes(), b"real-binary-fixture")
            self.assertEqual((materialized / "time_pdf.bin").read_bytes(), b"another-real-fixture")

    def test_worktree_rejects_non_relative_materialized_paths(self):
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
                    "pre_materialized_lfs_paths": ["../outside.bin"],
                },
            }

            with self.assertRaisesRegex(Exception, "repo-relative"):
                WorkspaceManager(run_dir, config).prepare(_candidate())

    def test_worktree_rejects_unmatched_materialized_paths(self):
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
                    "pre_materialized_lfs_paths": ["tests/fixtures/v107_rev1/*.bin"],
                },
            }

            with self.assertRaisesRegex(Exception, "matched no files"):
                WorkspaceManager(run_dir, config).prepare(_candidate())

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
                    "root": str(run_dir / "worktrees"),
                    "baseline_ref": baseline,
                    "branch_prefix": "gepa/test",
                },
            }
            candidate = _candidate()
            lease = WorkspaceManager(run_dir, config).prepare(candidate)
            worktree = Path(lease.worktree_path)
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
                    "root": str(run_dir / "worktrees"),
                    "baseline_ref": baseline,
                    "branch_prefix": "gepa/test",
                },
            }
            candidate = _candidate()
            lease = WorkspaceManager(run_dir, config).prepare(candidate)
            worktree = Path(lease.worktree_path)
            _git(worktree, "config", "user.email", "test@example.invalid")
            _git(worktree, "config", "user.name", "GEPA Test")
            (worktree / "src" / "hot.cc").write_text("int hot() { return 2; }\n", encoding="utf-8")
            (worktree / "tests").mkdir(exist_ok=True)
            (worktree / "tests" / "fixture.root").write_text("tampered\n", encoding="utf-8")
            _git(worktree, "add", "src/hot.cc", "tests/fixture.root")
            _git(worktree, "commit", "-m", "candidate")

            audit = audit_commit(repo=worktree, parent_sha=baseline, frozen_globs=["tests/**"])
            self.assertIn("tests/fixture.root", audit.frozen_violations)

    def test_registry_only_resolves_accepted_result_sha(self):
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
            # Not accepted yet -> no stackable SHA.
            self.assertIsNone(registry.accepted_result_sha("parent"))
            registry.mark_candidate_status("parent", "accepted")
            self.assertEqual(registry.accepted_result_sha("parent"), "b")

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
            )

            first = adapter.run_many([candidate], 0, config)
            self.assertFalse(first.failed_candidate_ids)
            registry.mark_candidate_status(candidate.candidate_id, "accepted")
            first_sha = registry.accepted_result_sha(candidate.candidate_id)
            second = adapter.run_many([candidate], 0, config)

            self.assertFalse(second.failed_candidate_ids)
            self.assertEqual(executor.modes, ["implement_and_validate", "evaluate_only"])
            self.assertEqual(registry.accepted_result_sha(candidate.candidate_id), first_sha)

    def test_existing_worktree_must_be_clean_before_evaluate_only(self):
        class CommittingExecutor:
            def __init__(self):
                self.calls = 0

            def execute(self, candidate, config):
                self.calls += 1
                repo = Path(config["_candidate_repo"])
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
            adapter = ExecutorAdapter(executor, run_dir, WorkspaceManager(run_dir, config), registry)

            first = adapter.run_many([candidate], 0, config)
            self.assertFalse(first.failed_candidate_ids)
            registry.mark_candidate_status(candidate.candidate_id, "accepted")
            workspace = registry.workspace(candidate.candidate_id)
            self.assertIsNotNone(workspace)
            dirty_file = Path(workspace["worktree_path"]) / "pre_applied.txt"
            dirty_file.write_text("pollution\n", encoding="utf-8")

            second = adapter.run_many([candidate], 0, config)

            self.assertEqual(executor.calls, 1)
            self.assertEqual(second.failed_candidate_ids, [candidate.candidate_id])
            self.assertIn("candidate worktree is not clean before execution", second.traces[0].samples[0].error)

    def test_br101_clean_source_with_runtime_debris_is_judged_normally(self):
        # Regression for the br101 incident: a candidate that commits clean
        # in-scope source and passes validation, but leaves untracked runtime
        # debris / a modified benchmark CSV and reports metric=None (e.g. a
        # broken benchmark harness) must NOT be force-zeroed or killed. With the
        # provenance layer removed it is simply audited (no frozen violation) and
        # handed to the judger, which scores it on the metric-unknown signal.
        class DebrisLeavingExecutor:
            def execute(self, candidate, config):
                repo = Path(config["_candidate_repo"])
                _git(repo, "config", "user.email", "test@example.invalid")
                _git(repo, "config", "user.name", "GEPA Test")
                (repo / "src" / "hot.cc").write_text("int hot() { return 2; }\n", encoding="utf-8")
                _git(repo, "add", "src/hot.cc")
                _git(repo, "commit", "-m", "candidate")
                # Runtime debris the executor does NOT commit (the br101 scene).
                (repo / "benchmarks.csv").write_text("commit,ms_evt\nabc,162.0\n", encoding="utf-8")
                return Trace(
                    candidate.candidate_id,
                    candidate.round_id,
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
                "executor": {"max_workers": 1},
            }
            candidate = _candidate()
            adapter = ExecutorAdapter(
                DebrisLeavingExecutor(),
                run_dir,
                WorkspaceManager(run_dir, config),
                ExecutionRegistry(run_dir),
            )
            trace_batch = adapter.run_many([candidate], 0, config)
            self.assertNotIn(candidate.candidate_id, trace_batch.failed_candidate_ids)

            sample = trace_batch.traces[0].samples[0]
            # Core regression: clean source + debris is not a frozen violation,
            # so no failure is stamped and the judger is called normally.
            self.assertEqual(sample.artifacts["commit_audit"]["frozen_violations"], [])
            self.assertNotEqual(sample.artifacts.get("failure_category"), "frozen_violation")

            judgment = JudgerAdapter(_Judger()).evaluate_many([candidate], trace_batch, config).judgments[0]
            self.assertNotIn("frozen_violation", judgment.failure_categories)
            self.assertNotEqual(judgment.score, 0.0)

    def test_frozen_path_edit_is_hard_rejected(self):
        # The one retained hard reject: an executor that edits a frozen path is
        # force-zeroed by the JudgerAdapter and never reaches the real judger.
        class FrozenEditingExecutor:
            def execute(self, candidate, config):
                repo = Path(config["_candidate_repo"])
                _git(repo, "config", "user.email", "test@example.invalid")
                _git(repo, "config", "user.name", "GEPA Test")
                (repo / "src" / "hot.cc").write_text("int hot() { return 2; }\n", encoding="utf-8")
                (repo / "tests").mkdir(exist_ok=True)
                (repo / "tests" / "fixture.root").write_text("tampered\n", encoding="utf-8")
                _git(repo, "add", "src/hot.cc", "tests/fixture.root")
                _git(repo, "commit", "-m", "candidate")
                return Trace(
                    candidate.candidate_id,
                    candidate.round_id,
                    [SampleTrace("task_execution", "in", "out", "expected", "ok")],
                )

        class _ExplodingJudger:
            def judge(self, candidate, trace, config):
                raise AssertionError("judger must not be called for a frozen-path violation")

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
                "candidate_policy": {"max_commits": 1, "frozen_globs": ["tests/**"]},
                "executor": {"max_workers": 1},
            }
            candidate = _candidate()
            adapter = ExecutorAdapter(
                FrozenEditingExecutor(),
                run_dir,
                WorkspaceManager(run_dir, config),
                ExecutionRegistry(run_dir),
            )
            trace_batch = adapter.run_many([candidate], 0, config)

            sample = trace_batch.traces[0].samples[0]
            self.assertEqual(sample.artifacts["failure_category"], "frozen_violation")
            self.assertIn("tests/fixture.root", sample.artifacts["commit_audit"]["frozen_violations"])

            judgment = JudgerAdapter(_ExplodingJudger()).evaluate_many([candidate], trace_batch, config).judgments[0]
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
