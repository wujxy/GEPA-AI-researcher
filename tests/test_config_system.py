from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

import yaml

from gepa_researcher.cli import main as cli_main
from gepa_researcher.agents.agent_components import AgentJudger, AgentProposer
from gepa_researcher.config import ConfigError, load_and_resolve, sanitize_snapshot
from gepa_researcher.config.contracts import role_contract
from gepa_researcher.storage.io_utils import read_json
from gepa_researcher.orchestrator import ResearchOrchestrator
from gepa_researcher.models.schemas import Candidate, LoopState, SampleTrace, Trace
from tests._fakes import fake_components


class CapturingClient:
    def __init__(self, data):
        self.data = data
        self.prompts = []

    def run_json(self, prompt, label="agent"):
        self.prompts.append(prompt)
        return type("Result", (), {"text": "{}", "data": self.data})()


class ConfigSystemTest(unittest.TestCase):
    def _git_repo(self, root: Path) -> Path:
        repo = root / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "tests@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "GEPA Tests"], check=True)
        (repo / "README.md").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)
        return repo

    def _profile(self, repo: Path) -> dict:
        return {
            "kind": "project_profile",
            "name": "fixture-project",
            "source": {
                "path": str(repo),
                "default_ref": "HEAD",
                "workspace_mode": "git_worktree",
            },
            "isolation": {
                "backend": "apptainer",
                "image": "docker://almalinux:9",
                "workdir": "/workspace/repo",
                "command": "claude",
                "env": {"pass": ["PATH"], "set": {"FIXTURE_ENV": "1"}},
            },
            "docs": ["context.md"],
            "provided_paths": [
                {"path": "/cvmfs/example", "mode": "ro", "role": "environment"},
                {"path": "scratch", "mode": "rw", "role": "scratch"},
                {"path": "input.csv", "mode": "ro", "role": "data_file"},
                {"path": "context.md", "mode": "ro", "role": "context"},
            ],
            "reference": {
                "commands": [
                    "source /opt/setup.sh",
                    "?source InstallArea/setup.sh",
                    "command -v python3",
                    "cmake --build build",
                ],
            },
            "repo_overlays": [
                {
                    "source": "assets/fixtures/time_pdf.bin",
                    "target": "TEMP/fixtures/time_pdf.bin",
                    "mode": "ro",
                    "purpose": "fixture",
                }
            ],
            "skills": ["fixture-skill"],
            "agent": {
                "timeout_seconds": 40,
                "extra_args": ["--token", "supersecret", "--allowedTools", "Read"],
            },
            "safety": {
                "editable_paths": ["src/**"],
                "frozen_paths": ["tests/**"],
                "max_files_per_candidate": 4,
                "max_commits_per_candidate": 2,
            },
        }

    def _task(self, profile_name: str = "profile.yaml") -> dict:
        return {
            "kind": "task",
            "task": {"name": "fixture-task", "goal": "Minimize fixture latency."},
            "project": {"profile": profile_name, "ref": "HEAD"},
            "metric": {
                "name": "latency",
                "direction": "minimize",
                "command": "python3 benchmark.py",
                "unit": "ms",
                "repeats": 3,
            },
            "validation": {
                "checks": [
                    {
                        "name": "tests",
                        "command": "python3 -m unittest",
                        "success_criteria": "command exits zero",
                    }
                ]
            },
            "safety": {
                "editable_paths": ["src/example.py"],
                "frozen_paths": ["docs/**"],
                "max_files_per_candidate": 2,
                "max_commits_per_candidate": 1,
            },
            "loop": {"max_rounds": 1, "patience": 1, "candidates_per_round": 3},
        }

    def _write_fixture(self, root: Path) -> tuple[Path, Path]:
        repo = self._git_repo(root)
        asset = root / "assets" / "fixtures" / "time_pdf.bin"
        asset.parent.mkdir(parents=True)
        asset.write_bytes(b"fixture")
        profile_path = root / "profile.yaml"
        task_path = root / "task.yaml"
        profile_path.write_text(yaml.safe_dump(self._profile(repo), sort_keys=False), encoding="utf-8")
        task_path.write_text(yaml.safe_dump(self._task(), sort_keys=False), encoding="utf-8")
        return task_path, repo

    def test_yaml_and_json_resolve_equivalently(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_path, _ = self._write_fixture(root)
            json_path = root / "task.json"
            json_path.write_text(json.dumps(self._task()), encoding="utf-8")

            yaml_config = load_and_resolve(task_path)
            json_config = load_and_resolve(json_path)

            for field in ("task", "workspace", "candidate_policy", "budget", "generation", "contracts"):
                self.assertEqual(yaml_config[field], json_config[field])

    def test_resolver_applies_defaults_paths_git_sha_and_safety_ceiling(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_path, repo = self._write_fixture(Path(tmp))
            config = load_and_resolve(task_path)

            expected_sha = subprocess.check_output(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
            ).strip()
            self.assertEqual(config["workspace"]["resolved_sha"], expected_sha)
            self.assertEqual(config["workspace"]["baseline_ref"], expected_sha)
            self.assertEqual(config["generation"]["batch_size"], 3)
            self.assertEqual(config["initialization"]["seed_count"], 3)
            self.assertEqual(config["executor"]["max_workers"], 3)
            self.assertEqual(config["candidate_policy"]["allowed_target_globs"], ["src/example.py"])
            self.assertEqual(config["candidate_policy"]["frozen_globs"], ["tests/**", "docs/**"])
            self.assertEqual(config["candidate_policy"]["max_commits"], 1)
            self.assertTrue(Path(config["task"]["data_files"][0]).is_absolute())
            self.assertTrue(Path(config["context"]["paths"][0]).is_absolute())
            self.assertNotIn("lifecycle", config.get("execution", {}))

    def test_max_parallel_candidates_is_not_capped_by_candidates_per_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_path, _ = self._write_fixture(root)
            task = yaml.safe_load(task_path.read_text(encoding="utf-8"))
            task["loop"] = {
                "seed_count": 6,
                "max_rounds": 1,
                "min_rounds": 0,
                "patience": 1,
                "candidates_per_round": 3,
                "max_parallel_candidates": 5,
            }
            task_path.write_text(yaml.safe_dump(task, sort_keys=False), encoding="utf-8")

            config = load_and_resolve(task_path)

            self.assertEqual(config["generation"]["batch_size"], 3)
            self.assertEqual(config["initialization"]["seed_count"], 6)
            self.assertEqual(config["executor"]["max_workers"], 5)

    def test_resolver_maps_extended_task_knobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_path, _ = self._write_fixture(root)
            task = yaml.safe_load(task_path.read_text(encoding="utf-8"))
            task["task"]["samples"] = [
                {"sample_id": "feedback"},
                {"sample_id": "pareto"},
            ]
            task["loop"] = {
                "seed_count": 2,
                "max_rounds": 1,
                "min_rounds": 1,
                "patience": 1,
                "candidates_per_round": 4,
                "enable_merge": True,
            }
            task["selection"] = {
                "frontier_policy": "pareto",
                "acceptance_policy": "minibatch_improves_then_pareto",
                "minibatch_size": 2,
                "parent_sampling": "pareto_win_weighted",
                "feedback_sample_ids": ["feedback"],
                "pareto_sample_ids": ["pareto"],
            }
            task["judger"] = {"pass_threshold": 0.9}
            task["usage_tracking"] = {
                "enabled": False,
                "persist_raw_envelope": False,
                "print_round_summary": False,
                "print_run_summary": False,
            }
            task["evidence"] = {
                "visualize_when_applicable": True,
                "plot_selection_policy": "proposer_selects",
                "artifact_formats": ["png"],
                "guidance": "plot only useful diagnostics",
            }
            task_path.write_text(yaml.safe_dump(task, sort_keys=False), encoding="utf-8")

            config = load_and_resolve(task_path)

            self.assertEqual(config["task"]["samples"], [{"sample_id": "feedback"}, {"sample_id": "pareto"}])
            self.assertEqual(config["budget"]["min_rounds"], 1)
            self.assertEqual(config["initialization"]["seed_count"], 2)
            self.assertEqual(config["generation"], {"batch_size": 4, "enable_merge": True})
            self.assertEqual(config["gepa"]["minibatch_size"], 2)
            self.assertEqual(config["gepa"]["feedback_sample_ids"], ["feedback"])
            self.assertEqual(config["gepa"]["pareto_sample_ids"], ["pareto"])
            self.assertEqual(config["judger"]["pass_threshold"], 0.9)
            self.assertFalse(config["usage_tracking"]["enabled"])
            self.assertTrue(config["evidence"]["visualize_when_applicable"])
            self.assertEqual(config["evidence"]["artifact_formats"], ["png"])

    def test_runtime_dsl_compiles_to_reference_only_runtime_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_path, _ = self._write_fixture(root)

            config = load_and_resolve(task_path)

            asset = (root / "assets" / "fixtures" / "time_pdf.bin").resolve()
            mounts = config["_runtime_spec"]["mounts"]
            self.assertEqual(config["executor"]["runtime_backend"], "apptainer")
            self.assertEqual(config["agent"]["command"], "claude")
            self.assertEqual(config["runtime"]["command"], "claude")
            self.assertEqual(config["_runtime_spec"]["setup"], [])
            self.assertEqual(config["_runtime_spec"]["check"], [])
            self.assertIn({"source": str(asset), "target": "/workspace/repo/TEMP/fixtures/time_pdf.bin", "mode": "ro"}, mounts)
            self.assertIn({"source": "/cvmfs/example", "target": "/cvmfs/example", "mode": "ro"}, mounts)
            self.assertIn({"source": str((root / "scratch").resolve()), "target": str((root / "scratch").resolve()), "mode": "rw"}, mounts)
            self.assertIn({"source": str((root / "context.md").resolve()), "target": str((root / "context.md").resolve()), "mode": "ro"}, mounts)
            self.assertEqual(
                config["contracts"]["reference"]["commands"],
                ["source /opt/setup.sh", "?source InstallArea/setup.sh", "command -v python3", "cmake --build build"],
            )

    def test_runtime_repo_mount_source_resolves_relative_to_profile_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_path, _ = self._write_fixture(root)

            config = load_and_resolve(task_path)

            mount = config["_runtime_spec"]["mounts"][0]
            self.assertEqual(mount["source"], str((root / "assets" / "fixtures" / "time_pdf.bin").resolve()))
            self.assertEqual(mount["target"], "/workspace/repo/TEMP/fixtures/time_pdf.bin")

    def test_runtime_missing_mount_source_raises_precise_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_path, _ = self._write_fixture(root)
            profile_path = root / "profile.yaml"
            profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
            profile["repo_overlays"] = [{"source": "assets/missing.bin", "target": "TEMP/missing.bin"}]
            profile_path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, r"repo_overlays\[0\]\.source: source does not exist: .*resolved relative to profile_dir"):
                load_and_resolve(task_path)

    def test_profile_rejects_unknown_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_path, _ = self._write_fixture(root)
            profile_path = root / "profile.yaml"
            profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
            profile["typo"] = {"setup_commands": ["source setup.sh"]}
            profile_path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, r"typo: unknown field"):
                load_and_resolve(task_path)

    def test_profile_rejects_legacy_runtime_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_path, _ = self._write_fixture(root)
            profile_path = root / "profile.yaml"
            profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
            profile["runtime"] = {"backend": "local"}
            profile_path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, r"runtime: unknown field"):
                load_and_resolve(task_path)

    def test_apptainer_runtime_image_is_optional_and_left_for_materialization(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_path, _ = self._write_fixture(root)
            profile_path = root / "profile.yaml"
            profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
            profile["isolation"].pop("image", None)
            profile_path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")

            config = load_and_resolve(task_path)

            self.assertEqual(config["_runtime_spec"]["image"], "")

    def test_new_schema_rejects_unknown_field_with_precise_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_path, _ = self._write_fixture(root)
            task = yaml.safe_load(task_path.read_text(encoding="utf-8"))
            task["metric"]["typo"] = True
            task_path.write_text(yaml.safe_dump(task), encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, r"metric\.typo: unknown field"):
                load_and_resolve(task_path)

    def test_task_cannot_broaden_profile_editable_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_path, _ = self._write_fixture(root)
            task = yaml.safe_load(task_path.read_text(encoding="utf-8"))
            task["safety"]["editable_paths"] = ["other/**"]
            task_path.write_text(yaml.safe_dump(task), encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "broaden profile policy"):
                load_and_resolve(task_path)

    def test_legacy_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.json"
            legacy = {
                "resume": True,
                "run_dir": str(Path(tmp) / "run"),
                "task": {"name": "legacy", "goal": "Minimize latency."},
                "budget": {"max_rounds": 1, "no_improvement_patience": 1},
                "judger": {"pass_threshold": 0.85},
                "gepa": {"frontier_policy": "pareto"},
            }
            path.write_text(json.dumps(legacy), encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, r"kind: run configuration must have kind: task"):
                load_and_resolve(path)

    def test_schema_version_task_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_path, _ = self._write_fixture(root)
            task = yaml.safe_load(task_path.read_text(encoding="utf-8"))
            task["schema_version"] = 2
            task_path.write_text(yaml.safe_dump(task), encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, r"schema_version: unknown field"):
                load_and_resolve(task_path)

    def test_snapshot_sanitizer_redacts_mapping_and_cli_secret_values(self):
        payload = {
            "api_token": "abc",
            "agent": {"extra_args": ["--token", "secret-value", "--api-key=inline", "Read"]},
        }

        sanitized = sanitize_snapshot(payload)

        self.assertEqual(sanitized["api_token"], "<redacted>")
        self.assertEqual(
            sanitized["agent"]["extra_args"],
            ["--token", "<redacted>", "--api-key=<redacted>", "Read"],
        )

    def test_role_contracts_expose_only_role_relevant_sections(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_path, _ = self._write_fixture(Path(tmp))
            config = load_and_resolve(task_path)

            proposer = role_contract(config, "proposer")
            judger = role_contract(config, "judger")

            self.assertIn("runtime", proposer)
            self.assertNotIn("validation", proposer)
            self.assertIn("validation", judger)
            self.assertNotIn("runtime", judger)
            self.assertNotIn("agent", json.dumps(proposer))

    def test_proposer_prompt_uses_contract_without_agent_backend_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_path, _ = self._write_fixture(Path(tmp))
            config = load_and_resolve(task_path)
            client = CapturingClient({
                "hypothesis": "h",
                "scope": "src",
                "proposed_change": "c",
                "rationale": "r",
                "expected_improvement": "latency",
                "risk": "risk",
                "strategy": "small change",
                "target_files": ["src/example.py"],
                "safety_class": "safe",
                "analysis_plan": [],
            })

            AgentProposer(client).propose(LoopState(task_name="fixture"), config)
            prompt = client.prompts[0]

            self.assertIn("Proposer contract (authoritative)", prompt)
            self.assertIn('"direction": "minimize"', prompt)
            self.assertNotIn("supersecret", prompt)
            self.assertNotIn("usage_tracking", prompt)
            self.assertNotIn('"validation"', prompt)

    def test_judger_prompt_uses_only_judging_contract_and_execution_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_path, _ = self._write_fixture(Path(tmp))
            config = load_and_resolve(task_path)
            config["_prior_context"] = {"notes": ["private proposer context"]}
            candidate = Candidate(
                candidate_id="cand_000",
                round_id=0,
                hypothesis="h",
                scope="src",
                proposed_change="c",
                rationale="r",
                expected_improvement="latency",
                risk="risk",
                prompt_text="",
                created_at="now",
            )
            trace = Trace(
                candidate_id=candidate.candidate_id,
                round_id=0,
                samples=[SampleTrace(
                    sample_id="task_execution",
                    input="candidate",
                    output="measured",
                    expected="better",
                    logs="validated",
                )],
            )
            client = CapturingClient({
                "score": 0.8,
                "passed": True,
                "per_sample_scores": [],
                "failure_categories": [],
                "actionable_feedback": [],
                "confidence": "high",
            })

            AgentJudger(client).judge(candidate, trace, config)
            prompt = client.prompts[0]

            self.assertIn("Judger contract (authoritative)", prompt)
            self.assertIn('"validation"', prompt)
            self.assertNotIn('"runtime"', prompt)
            self.assertNotIn("supersecret", prompt)
            self.assertNotIn("private proposer context", prompt)

    def test_new_config_runs_offline_mini_loop_and_saves_resolved_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = self._task()
            task["project"] = {
                "inline": {
                    "source": {"workspace_mode": "artifact_directory"},
                    "isolation": {
                        "backend": "local",
                        "workdir": "/workspace/repo",
                        "command": "claude",
                        "env": {"pass": [], "set": {}},
                    },
                    "agent": {"timeout_seconds": 30},
                }
            }
            task.pop("safety")
            task["loop"] = {"max_rounds": 1, "patience": 1, "candidates_per_round": 2}
            task_path = root / "task.yaml"
            task_path.write_text(yaml.safe_dump(task), encoding="utf-8")
            run_dir = root / "run"
            config = load_and_resolve(task_path, run_dir=run_dir)

            with redirect_stdout(StringIO()):
                state = ResearchOrchestrator(
                    config=config,
                    config_path=task_path,
                    components=fake_components(),
                ).run()

            snapshot = read_json(run_dir / "config.snapshot.json")
            self.assertTrue(state.history)
            self.assertEqual(snapshot["_meta"]["schema_version"], "canonical")
            self.assertEqual(snapshot["contracts"]["metric"]["name"], "latency")
            self.assertEqual(snapshot["run_dir"], str(run_dir))

    def test_resume_requires_explicit_run_dir_for_new_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_path, _ = self._write_fixture(Path(tmp))
            with self.assertRaisesRegex(ConfigError, "--resume requires"):
                load_and_resolve(task_path, resume=True)

    def test_artifact_project_repo_is_context_not_source_execution(self):
        config = load_and_resolve(Path("examples/function_discovery/task.yaml"))

        self.assertEqual(config["workspace"]["mode"], "artifact_directory")
        self.assertNotIn("repo_paths", config["task"])

    def test_validate_resolve_and_explain_do_not_create_run_or_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_path, _ = self._write_fixture(root)
            commands = [
                ["validate", "--no-materialize"],
                ["resolve"],
                ["explain"],
            ]
            for command in commands:
                stdout = StringIO()
                stderr = StringIO()
                with self.assertRaises(SystemExit) as raised:
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        cli_main([*command, "--config", str(task_path)])
                self.assertEqual(raised.exception.code, 0)
            self.assertFalse((root / "runs").exists())
            self.assertFalse(any(path.name == "worktrees" for path in root.rglob("*")))

    def test_cli_reports_invalid_config_with_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_path, _ = self._write_fixture(root)
            task = yaml.safe_load(task_path.read_text(encoding="utf-8"))
            task["metric"]["direction"] = "sideways"
            task_path.write_text(yaml.safe_dump(task), encoding="utf-8")
            stderr = StringIO()
            with self.assertRaises(SystemExit) as raised:
                with redirect_stderr(stderr):
                    cli_main(["validate", "--config", str(task_path)])
            self.assertEqual(raised.exception.code, 2)
            self.assertIn("metric.direction", stderr.getvalue())



if __name__ == "__main__":
    unittest.main()
