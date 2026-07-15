from __future__ import annotations

import subprocess
from pathlib import Path

from gepa_researcher.domain.candidate import CandidateCard, CandidateStatus, ProposalIdea
from gepa_researcher.domain.execution import CapabilityPolicy, ExecutionBudget, ExecutionPhase, ExecutionSpec, ExecutionStatus
from gepa_researcher.execution.git_result import GitResultService
from gepa_researcher.execution.materializer import RepositoryMaterializer
from gepa_researcher.execution.runtime_backend import RuntimeLease
from gepa_researcher.execution.sandbox import SandboxSession
from gepa_researcher.agents.adapters import RunnerAdapter
from gepa_researcher.models.schemas import SampleTrace, Trace
from gepa_researcher.services.execution_service import ExecutionService
from gepa_researcher.storage.execution_store import ExecutionStore


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
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "baseline")
    return repo, _git(repo, "rev-parse", "HEAD")


def _proposal() -> ProposalIdea:
    return ProposalIdea(
        proposal_id="proposal-1",
        hypothesis="inline hot function",
        scope="src/hot.cc",
        proposed_change="change hot return",
        rationale="exercise execution service",
        expected_improvement="latency",
        risk="low",
        prompt_text="prompt",
        target_files=("src/hot.cc",),
    )


def _card(base_revision: str, *, result_revision: str | None = None) -> CandidateCard:
    proposal = _proposal()
    return CandidateCard(
        candidate_id="cand_001_000",
        round_id=1,
        parent_candidate_ids=("seed_000",),
        proposal_id=proposal.proposal_id,
        proposal=proposal,
        base_revision=base_revision,
        status=CandidateStatus.MATERIALIZED if result_revision else CandidateStatus.ADMITTED,
        result_revision=result_revision,
    )


def _spec(card: CandidateCard, phase: ExecutionPhase, input_revision: str) -> ExecutionSpec:
    return ExecutionSpec(
        execution_id=f"exec-{phase.value}",
        run_id="run-001",
        round_id=card.round_id,
        candidate_id=card.candidate_id,
        phase=phase,
        input_revision=input_revision,
        dataset_ref="dataset:feedback" if phase != ExecutionPhase.IMPLEMENTATION else None,
        evaluator_version=None,
        budget=ExecutionBudget(wall_seconds=600),
        capability_policy=CapabilityPolicy(
            repo_writable=phase == ExecutionPhase.IMPLEMENTATION,
            network_allowed=False,
            allowed_tools=("bash", "git"),
            forbidden_paths=(),
        ),
    )


class CommittingRunner:
    def run(self, card, spec, runtime_lease, session, config):
        _git(session.repo_path, "config", "user.email", "test@example.invalid")
        _git(session.repo_path, "config", "user.name", "GEPA Test")
        (session.repo_path / "src" / "hot.cc").write_text("int hot() { return 2; }\n", encoding="utf-8")
        _git(session.repo_path, "add", "src/hot.cc")
        _git(session.repo_path, "commit", "-m", "candidate")
        return Trace(
            candidate_id=card.candidate_id,
            round_id=card.round_id,
            samples=[SampleTrace("task", "in", "out", "expected", "ok")],
        )


class ReadonlyRunner:
    def run(self, card, spec, runtime_lease, session, config):
        return Trace(
            candidate_id=card.candidate_id,
            round_id=card.round_id,
            samples=[SampleTrace("task", "in", "out", "expected", "ok")],
        )


class NonCommittingRunner:
    def run(self, card, spec, runtime_lease, session, config):
        return Trace(
            candidate_id=card.candidate_id,
            round_id=card.round_id,
            samples=[SampleTrace("task", "in", "out", "expected", "no source change")],
        )


class DirtyNonCommittingRunner:
    def run(self, card, spec, runtime_lease, session, config):
        (session.repo_path / "src" / "hot.cc").write_text("int hot() { return 3; }\n", encoding="utf-8")
        return Trace(
            candidate_id=card.candidate_id,
            round_id=card.round_id,
            samples=[SampleTrace("task", "in", "out", "expected", "left source diff uncommitted")],
        )


class StagedFrozenAndDirtyAllowedRunner:
    def run(self, card, spec, runtime_lease, session, config):
        (session.repo_path / "src" / "hot.cc").write_text("int hot() { return 4; }\n", encoding="utf-8")
        (session.repo_path / "tests").mkdir(exist_ok=True)
        (session.repo_path / "tests" / "fixture.root").write_text("tampered staged fixture\n", encoding="utf-8")
        _git(session.repo_path, "add", "tests/fixture.root")
        return Trace(
            candidate_id=card.candidate_id,
            round_id=card.round_id,
            samples=[SampleTrace("task", "in", "out", "expected", "staged frozen plus dirty source")],
        )


class ClaimingNoCommitRunner:
    def run(self, card, spec, runtime_lease, session, config):
        return Trace(
            candidate_id=card.candidate_id,
            round_id=card.round_id,
            samples=[
                SampleTrace(
                    "task",
                    "in",
                    "out",
                    "expected",
                    "claimed commit without changing HEAD",
                    artifacts={"implementation": {"commit_sha": "f" * 40}},
                )
            ],
        )


def _service(
    tmp_path: Path,
    repo: Path,
    baseline: str,
    runner,
    *,
    candidate_policy: dict | None = None,
) -> tuple[ExecutionService, ExecutionStore]:
    run_dir = tmp_path / "run"
    store = ExecutionStore(run_dir)
    service = ExecutionService(
        run_dir=run_dir,
        config={"executor": {"runtime_backend": "local"}},
        materializer=RepositoryMaterializer(
            run_dir=run_dir,
            workspace_config={
                "mode": "git_worktree",
                "repo_path": str(repo),
                "baseline_ref": baseline,
                "root": str(run_dir / "sandboxes"),
                "branch_prefix": "gepa/test",
            },
        ),
        execution_store=store,
        git_result_service=GitResultService(candidate_policy=candidate_policy or {}),
        runner=runner,
    )
    return service, store


def test_implementation_execution_creates_result_revision_and_updates_store(tmp_path: Path):
    repo, baseline = _make_repo(tmp_path)
    card = _card(baseline)
    spec = _spec(card, ExecutionPhase.IMPLEMENTATION, baseline)
    service, store = _service(tmp_path, repo, baseline, CommittingRunner())

    record, trace = service.execute(spec, card)

    assert record.status == ExecutionStatus.SUCCEEDED
    assert record.result_revision is not None
    assert record.result_revision != baseline
    assert trace.candidate_id == card.candidate_id
    assert [item.execution_id for item in store.list_for_candidate(card.candidate_id)] == [spec.execution_id]


def test_execution_service_indexes_trace_artifact(tmp_path: Path):
    repo, baseline = _make_repo(tmp_path)
    card = _card(baseline)
    spec = _spec(card, ExecutionPhase.IMPLEMENTATION, baseline)
    service, store = _service(tmp_path, repo, baseline, CommittingRunner())

    record, _ = service.execute(spec, card)

    persisted = store.get(spec.execution_id)
    assert [artifact.kind.value for artifact in persisted.artifact_refs] == ["execution_trace"]
    artifact_path = service.run_dir / persisted.artifact_refs[0].path
    assert artifact_path.exists()
    assert "cand_001_000" in artifact_path.read_text(encoding="utf-8")


def test_git_implementation_without_commit_fails_without_result_revision(tmp_path: Path):
    repo, baseline = _make_repo(tmp_path)
    card = _card(baseline)
    spec = _spec(card, ExecutionPhase.IMPLEMENTATION, baseline)
    service, store = _service(tmp_path, repo, baseline, NonCommittingRunner())

    record, trace = service.execute(spec, card)

    assert record.status == ExecutionStatus.FAILED
    assert record.failure.code == "NO_CANDIDATE_COMMIT_EMPTY"
    assert record.result_revision is None
    assert "no candidate commit produced" in trace.samples[0].error
    assert store.get(spec.execution_id).result_revision is None


def test_git_implementation_with_allowed_uncommitted_diff_gets_harness_commit(tmp_path: Path):
    repo, baseline = _make_repo(tmp_path)
    card = _card(baseline)
    spec = _spec(card, ExecutionPhase.IMPLEMENTATION, baseline)
    service, _ = _service(
        tmp_path,
        repo,
        baseline,
        DirtyNonCommittingRunner(),
        candidate_policy={"allowed_target_globs": ["src/**"], "frozen_globs": ["tests/**"]},
    )

    record, trace = service.execute(spec, card)

    assert record.status == ExecutionStatus.SUCCEEDED
    assert record.result_revision is not None
    assert record.result_revision != baseline
    audit = trace.samples[0].artifacts["commit_audit"]
    assert audit["harness_commit_created"] is True
    assert audit["harness_committed_files"] == ["src/hot.cc"]
    assert audit["commit_failure_reason"] is None
    assert "int hot() { return 3; }" in _git(repo, "show", f"{record.result_revision}:src/hot.cc")


def test_fallback_commit_does_not_include_pre_staged_frozen_paths(tmp_path: Path):
    repo, baseline = _make_repo(tmp_path)
    card = _card(baseline)
    spec = _spec(card, ExecutionPhase.IMPLEMENTATION, baseline)
    service, _ = _service(
        tmp_path,
        repo,
        baseline,
        StagedFrozenAndDirtyAllowedRunner(),
        candidate_policy={"allowed_target_globs": ["src/**"], "frozen_globs": ["tests/**"]},
    )

    record, trace = service.execute(spec, card)

    assert record.status == ExecutionStatus.SUCCEEDED
    audit = trace.samples[0].artifacts["commit_audit"]
    assert audit["harness_committed_files"] == ["src/hot.cc"]
    assert audit["changed_files"] == ["src/hot.cc"]
    assert "tests/fixture.root" not in _git(repo, "diff", "--name-only", baseline, record.result_revision)


def test_no_candidate_commit_failure_records_claimed_and_actual_commit_diagnostics(tmp_path: Path):
    repo, baseline = _make_repo(tmp_path)
    card = _card(baseline)
    spec = _spec(card, ExecutionPhase.IMPLEMENTATION, baseline)
    service, _ = _service(tmp_path, repo, baseline, ClaimingNoCommitRunner())

    record, _ = service.execute(spec, card)

    assert record.status == ExecutionStatus.FAILED
    assert record.failure.code == "NO_CANDIDATE_COMMIT_EMPTY"
    assert record.failure.details["claimed_commit_sha"] == "f" * 40
    assert record.failure.details["claimed_commit_exists"] is False
    assert record.failure.details["actual_head"] == baseline


def test_feedback_execution_uses_result_revision_as_readonly_input(tmp_path: Path):
    repo, baseline = _make_repo(tmp_path)
    card = _card(baseline)
    impl_spec = _spec(card, ExecutionPhase.IMPLEMENTATION, baseline)
    impl_service, _ = _service(tmp_path, repo, baseline, CommittingRunner())
    impl_record, _ = impl_service.execute(impl_spec, card)
    card.result_revision = impl_record.result_revision
    card.status = CandidateStatus.MATERIALIZED
    feedback_spec = _spec(card, ExecutionPhase.FEEDBACK_EVAL, card.result_revision)
    feedback_service, store = _service(tmp_path, repo, baseline, ReadonlyRunner())

    record, trace = feedback_service.execute(feedback_spec, card)

    assert record.status == ExecutionStatus.SUCCEEDED
    assert record.phase == ExecutionPhase.FEEDBACK_EVAL
    assert record.input_revision == card.result_revision
    assert record.result_revision is None
    assert trace.samples[0].artifacts["execution_record"]["execution_id"] == feedback_spec.execution_id
    assert [item.execution_id for item in store.list_for_candidate(card.candidate_id)] == [impl_spec.execution_id, feedback_spec.execution_id]


def test_runner_adapter_builds_transient_agent_config_without_mutating_card(tmp_path: Path):
    repo, baseline = _make_repo(tmp_path)
    card = _card(baseline)
    spec = _spec(card, ExecutionPhase.FEEDBACK_EVAL, baseline)
    session = SandboxSession(
        execution_id=spec.execution_id,
        repo_path=repo,
        artifact_path=tmp_path / "artifacts",
        scratch_path=tmp_path / "scratch",
        input_revision=baseline,
        mode="git_worktree",
        temporary_paths=(repo,),
    )
    runtime_lease = RuntimeLease(
        backend="local",
        repo_path=str(repo),
        artifact_path=str(tmp_path / "artifacts"),
        host_cwd=str(repo),
        command="agent",
        command_prefix=[],
        env={"GEPA_EXECUTION_ID": spec.execution_id},
    )

    class RecordingExecutor:
        def __init__(self):
            self.config = None

        def execute(self, candidate, config):
            self.config = config
            return Trace(candidate.candidate_id, candidate.round_id, [SampleTrace("task", "in", "out", "expected", "ok")])

    executor = RecordingExecutor()
    controller_repo = tmp_path / "controller"
    trace = RunnerAdapter(executor, tmp_path / "run").run(
        card,
        spec,
        runtime_lease,
        session,
        {
            "task": {"goal": "test", "repo_paths": [str(controller_repo)]},
            "contracts": {"resources": {"repo_path": str(controller_repo), "docs": ["README.md"]}},
        },
    )

    assert trace.candidate_id == card.candidate_id
    assert executor.config["_candidate_repo"] == str(repo)
    assert executor.config["task"]["repo_paths"] == [str(repo)]
    assert executor.config["contracts"]["resources"]["repo_path"] == str(repo)
    assert executor.config["contracts"]["resources"]["docs"] == ["README.md"]
    assert executor.config["_execution_id"] == spec.execution_id
    assert executor.config["_execution_mode"] == "evaluate_only"
    assert executor.config["_context_view"]["role"] == "executor"
    assert executor.config["_context_view"]["envelope"]["candidate_id"] == card.candidate_id
    assert any(block["block_id"] == f"candidate:{card.candidate_id}" for block in executor.config["_context_view"]["blocks"])
    assert card.to_dict()["result_revision"] is None
    assert "repo_path" not in card.to_dict()


# ── Part G: verification-no-change unit tests ────────────────────────────
# Verify that an agent which legitimately determines no code change is needed
# (validation.passed=true, changed_files=[], no errors) yields a successful
# execution rather than NoCandidateCommitError.


class VerificationNoChangeRunner:
    """Agent correctly analyzed code, found no change needed, reported
    validation.passed=true with empty changed_files and no errors."""

    def run(self, card, spec, runtime_lease, session, config):
        return Trace(
            candidate_id=card.candidate_id,
            round_id=card.round_id,
            samples=[
                SampleTrace(
                    sample_id="verification_task",
                    input="verify if already precomputed",
                    output="no change needed",
                    expected="verification complete",
                    logs="analyzed code, found already optimal",
                    error=None,
                    artifacts={
                        "validation": {"passed": True, "checks": [], "regressions": []},
                        "implementation": {
                            "changed_files": [],
                            "commands_run": ["read OMILRECV2/src/omilrec_likelihood.cc"],
                            "commit_sha": None,
                            "committed_files": [],
                            "git_status_after_commit": "",
                            "notes": "Rsp_arr already precomputed in geometry phase.",
                        },
                        "errors": [],
                        "diagnostics": ["All loops already cache geometry. No optimization applicable."],
                        "metrics": {"primary": None, "baseline": None, "delta": None},
                    },
                )
            ],
        )


def test_verification_no_change_agent_succeeds_with_baseline_revision(
    tmp_path: Path,
):
    """Agent correctly determines no code change needed. Execution should
    succeed with result_revision == input_revision, not raise
    NoCandidateCommitError, and preserve the original trace."""
    repo, baseline = _make_repo(tmp_path)
    card = _card(baseline)
    spec = _spec(card, ExecutionPhase.IMPLEMENTATION, baseline)
    service, _ = _service(tmp_path, repo, baseline, VerificationNoChangeRunner())

    record, trace = service.execute(spec, card)

    assert record.status == ExecutionStatus.SUCCEEDED
    assert record.result_revision == baseline  # baseline because no change
    assert trace.samples[0].error is None  # original trace preserved, NOT replaced
    assert trace.samples[0].sample_id == "verification_task"  # not "execution_failure"
    audit = trace.samples[0].artifacts["commit_audit"]
    assert audit["verification_no_change"] is True
    # commit_failure_reason retains its original audit value (e.g. "empty")
    # because the harness correctly diagnosed that no source changed.
    # verification_no_change=True is the signal that this was accepted.


class ValidationFailedNoChangeRunner:
    """Agent changed nothing AND validation reported FAILED. Should still
    raise NoCandidateCommitError — not a legitimate verification."""

    def run(self, card, spec, runtime_lease, session, config):
        return Trace(
            candidate_id=card.candidate_id,
            round_id=card.round_id,
            samples=[
                SampleTrace(
                    sample_id="task",
                    input="in",
                    output="out",
                    expected="exp",
                    logs="",
                    error=None,
                    artifacts={
                        "validation": {"passed": False, "checks": [], "regressions": []},
                        "implementation": {"changed_files": [], "commit_sha": None, "committed_files": []},
                        "errors": [],
                        "metrics": {},
                    },
                )
            ],
        )


def test_validation_failed_no_change_raises_no_candidate_commit(tmp_path: Path):
    """Agent changed nothing AND validation failed. Should still raise
    NoCandidateCommitError — not a legitimate verification-no-change."""
    repo, baseline = _make_repo(tmp_path)
    card = _card(baseline)
    spec = _spec(card, ExecutionPhase.IMPLEMENTATION, baseline)
    service, _ = _service(tmp_path, repo, baseline, ValidationFailedNoChangeRunner())

    record, _ = service.execute(spec, card)

    assert record.status == ExecutionStatus.FAILED
    assert record.failure.code == "NO_CANDIDATE_COMMIT_EMPTY"


class VerificationWithErrorsRunner:
    """Agent changed nothing, validation passed, but reported errors.
    Should still fail — infra issues are not a clean verification."""

    def run(self, card, spec, runtime_lease, session, config):
        return Trace(
            candidate_id=card.candidate_id,
            round_id=card.round_id,
            samples=[
                SampleTrace(
                    sample_id="task",
                    input="in",
                    output="out",
                    expected="exp",
                    logs="",
                    error=None,
                    artifacts={
                        "validation": {"passed": True, "checks": [], "regressions": []},
                        "implementation": {"changed_files": [], "commit_sha": None, "committed_files": []},
                        "errors": ["benchmark environment unavailable"],
                        "metrics": {},
                    },
                )
            ],
        )


def test_verification_with_agent_errors_raises_no_candidate_commit(tmp_path: Path):
    """Agent validation passed, no changes, but agent reported errors.
    Should raise NoCandidateCommitError — infra errors taint the verification."""
    repo, baseline = _make_repo(tmp_path)
    card = _card(baseline)
    spec = _spec(card, ExecutionPhase.IMPLEMENTATION, baseline)
    service, _ = _service(tmp_path, repo, baseline, VerificationWithErrorsRunner())

    record, _ = service.execute(spec, card)

    assert record.status == ExecutionStatus.FAILED
    assert record.failure.code == "NO_CANDIDATE_COMMIT_EMPTY"


class VerificationWithChangedFilesRunner:
    """Agent returned validation.passed=true AND has changed_files. This is
    NOT verification-no-change — agent actually made a change, and the normal
    harness commit should run. The test verifies the guard doesn't intercept."""

    def run(self, card, spec, runtime_lease, session, config):
        (session.repo_path / "src" / "hot.cc").write_text("int hot() { return 9; }\n", encoding="utf-8")
        return Trace(
            candidate_id=card.candidate_id,
            round_id=card.round_id,
            samples=[
                SampleTrace(
                    sample_id="task",
                    input="in",
                    output="out",
                    expected="exp",
                    logs="done",
                    error=None,
                    artifacts={
                        "validation": {"passed": True, "checks": [], "regressions": []},
                        "implementation": {"changed_files": ["src/hot.cc"], "commit_sha": None, "committed_files": []},
                        "errors": [],
                        "metrics": {},
                    },
                )
            ],
        )


def test_verification_with_changed_files_goes_through_harness_commit(tmp_path: Path):
    """Agent has changed_files — NOT verification-no-change. Harness should
    commit them normally (not NoCandidateCommitError, not verification flag)."""
    repo, baseline = _make_repo(tmp_path)
    card = _card(baseline)
    spec = _spec(card, ExecutionPhase.IMPLEMENTATION, baseline)
    service, _ = _service(
        tmp_path,
        repo,
        baseline,
        VerificationWithChangedFilesRunner(),
        candidate_policy={"allowed_target_globs": ["src/**"], "frozen_globs": ["tests/**"]},
    )

    record, trace = service.execute(spec, card)

    assert record.status == ExecutionStatus.SUCCEEDED
    assert record.result_revision is not None
    assert record.result_revision != baseline  # committed something
    audit = trace.samples[0].artifacts["commit_audit"]
    assert audit["verification_no_change"] is False
    assert audit["harness_commit_created"] is True
