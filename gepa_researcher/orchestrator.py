from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
from typing import Any

from .agent_client import AgentError, ClaudeCodeClient
from .agent_components import AgentExecutor, AgentGater, AgentJudger, AgentProposer
from .executor import PaperQAExecutor
from .gater import SimpleGater
from .io_utils import append_jsonl, read_json, write_json
from .judger import PaperQAJudger
from .proposer import RuleBasedProposer
from .schemas import Candidate, Decision, Judgment, LoopState, Trace


class ResearchOrchestrator:
    def __init__(self, config: dict[str, Any], config_path: Path):
        self.config = config
        self.config_path = config_path
        self.run_dir = self._resolve_run_dir(config, config_path)
        self.proposer, self.executor, self.judger, self.gater = self._build_components()

    def run(self) -> LoopState:
        state = self._load_or_initialize_state()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        write_json(self.run_dir / "config.snapshot.json", self.config)
        max_rounds = int(self.config["budget"]["max_rounds"])
        self._log(f"Starting run '{state.task_name}'")
        self._log(f"Artifacts: {self.run_dir}")

        for round_id in range(state.round_id, max_rounds):
            state.round_id = round_id
            self._log(f"Round {round_id + 1}/{max_rounds} started")
            self._log("proposer started")
            candidate = self.proposer.propose(state, self.config)
            self._log(f"proposer finished: {candidate.candidate_id}")
            self._write_live_artifact(round_id, "candidate", candidate.to_dict())
            self._log_candidate(candidate)

            self._log("executor started")
            trace = self.executor.execute(candidate, self.config)
            self._log(f"executor finished: {len(trace.samples)} sample trace(s)")
            self._write_live_artifact(round_id, "trace", trace.to_dict())
            self._log_trace(trace)

            self._log("judger started")
            judgment = self.judger.judge(candidate, trace, self.config)
            self._log(f"judger finished: score={judgment.score:.4f}, passed={judgment.passed}")
            self._write_live_artifact(round_id, "judgment", judgment.to_dict())
            self._log_judgment(judgment)

            self._log("gater started")
            decision = self.gater.decide(state, candidate, judgment, self.config)
            self._log(f"gater finished: decision={decision.decision}, stop={decision.stop}")
            self._write_live_artifact(round_id, "decision", decision.to_dict())
            self._log_decision(decision)

            self._persist_round(candidate, trace, judgment, decision)
            self._update_state(state, candidate, judgment, decision)
            write_json(self.run_dir / "state.json", state.to_dict())
            self._log(f"Round {round_id + 1}/{max_rounds} persisted")

            if decision.stop:
                self._log(f"Stopping after round {round_id + 1}: {decision.reason}")
                break

        self._write_final_report(state)
        self._log(f"Final report written: {self.run_dir / 'final_report.md'}")
        return state

    def _log(self, message: str) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{stamp}] {message}", flush=True)

    def _write_live_artifact(self, round_id: int, name: str, data: dict[str, Any]) -> None:
        write_json(self.run_dir / "live" / f"round_{round_id:03d}_{name}.json", data)

    def _log_candidate(self, candidate: Candidate) -> None:
        self._log(f"Hypothesis: {candidate.hypothesis}")
        self._log(f"Proposed change: {candidate.proposed_change}")
        if candidate.rationale:
            self._log(f"Rationale: {candidate.rationale}")
        if candidate.expected_improvement:
            self._log(f"Expected improvement: {candidate.expected_improvement}")
        if candidate.risk:
            self._log(f"Risk: {candidate.risk}")

    def _log_trace(self, trace: Trace) -> None:
        if not trace.samples:
            self._log("Executor summary: no samples returned")
            return
        sample = trace.samples[0]
        artifacts = sample.artifacts
        summary = artifacts.get("summary") or sample.logs or sample.output
        self._log(f"Executor summary: {summary}")
        model_expression = artifacts.get("model_expression")
        if model_expression:
            self._log(f"Model expression: {model_expression}")
        metrics = artifacts.get("metrics")
        if metrics:
            self._log(f"Metrics: {metrics}")
        diagnostics = artifacts.get("diagnostics")
        if diagnostics:
            self._log(f"Diagnostics: {diagnostics}")

    def _log_judgment(self, judgment: Judgment) -> None:
        self._log(f"Judgment: score={judgment.score:.4f}, passed={judgment.passed}, confidence={judgment.confidence}")
        if judgment.failure_categories:
            self._log(f"Failure categories: {judgment.failure_categories}")
        if judgment.actionable_feedback:
            self._log(f"Feedback: {judgment.actionable_feedback}")

    def _log_decision(self, decision: Decision) -> None:
        self._log(f"Decision: {decision.decision}, stop={decision.stop}, best_so_far={decision.best_so_far}")
        if decision.reason:
            self._log(f"Decision reason: {decision.reason}")

    def _resolve_run_dir(self, config: dict[str, Any], config_path: Path) -> Path:
        run_dir = config.get("run_dir")
        if run_dir:
            return Path(run_dir).expanduser().resolve()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return (config_path.parent / "runs" / stamp).resolve()

    def _build_components(self):
        mode = self.config.get("components", {}).get("mode", "local_mock")
        if mode == "claude_code_agents":
            agent_config = self.config.get("agent", {})
            client = ClaudeCodeClient(
                command=agent_config.get("command", "claude"),
                cwd=Path(agent_config.get("cwd", self.config_path.parent.parent.parent)).resolve(),
                timeout_seconds=int(agent_config.get("timeout_seconds", 600)),
                extra_args=list(agent_config.get("extra_args", [])),
            )
            return (
                AgentProposer(client),
                AgentExecutor(client, self.run_dir),
                AgentJudger(client),
                AgentGater(client),
            )
        if mode == "local_mock":
            return RuleBasedProposer(), PaperQAExecutor(), PaperQAJudger(), SimpleGater()
        raise ValueError(f"Unknown component mode: {mode}")

    def _load_or_initialize_state(self) -> LoopState:
        state_path = self.run_dir / "state.json"
        if state_path.exists() and self.config.get("resume", False):
            return LoopState.from_dict(read_json(state_path))
        return LoopState(task_name=self.config["task"]["name"])

    def _persist_round(self, candidate: Candidate, trace: Trace, judgment: Judgment, decision: Decision) -> None:
        round_dir = self.run_dir / "traces" / f"round_{candidate.round_id:03d}"
        write_json(round_dir / "candidate.json", candidate.to_dict())
        write_json(round_dir / "trace.json", trace.to_dict())
        write_json(round_dir / "judgment.json", judgment.to_dict())
        write_json(round_dir / "decision.json", decision.to_dict())
        append_jsonl(self.run_dir / "candidates.jsonl", candidate.to_dict())
        append_jsonl(self.run_dir / "judgments.jsonl", judgment.to_dict())
        append_jsonl(self.run_dir / "decisions.jsonl", decision.to_dict())

    def _update_state(self, state: LoopState, candidate: Candidate, judgment: Judgment, decision: Decision) -> None:
        improved = judgment.score > state.best_score
        if improved:
            state.best_score = judgment.score
            state.best_candidate_id = candidate.candidate_id
            state.no_improvement_rounds = 0
        else:
            state.no_improvement_rounds += 1

        state.history.append(
            {
                "round_id": candidate.round_id,
                "candidate_id": candidate.candidate_id,
                "score": judgment.score,
                "passed": judgment.passed,
                "decision": decision.decision,
                "failure_categories": judgment.failure_categories,
                "actionable_feedback": judgment.actionable_feedback,
                "prompt_text": candidate.prompt_text,
            }
        )
        state.round_id = candidate.round_id + 1

    def _write_final_report(self, state: LoopState) -> None:
        lines = [
            f"# Run Summary: {state.task_name}",
            "",
            f"- Best candidate: `{state.best_candidate_id}`",
            f"- Best score: `{state.best_score:.4f}`",
            f"- Completed rounds: `{len(state.history)}`",
            "",
            "## Round History",
            "",
        ]
        for item in state.history:
            lines.extend(
                [
                    f"### Round {item['round_id']} - {item['candidate_id']}",
                    "",
                    f"- Score: `{item['score']}`",
                    f"- Passed: `{item['passed']}`",
                    f"- Decision: `{item['decision']}`",
                    f"- Failures: {', '.join(item['failure_categories']) or 'none'}",
                    "- Feedback:",
                    *[f"  - {feedback}" for feedback in item["actionable_feedback"]],
                    "",
                    "<details><summary>Candidate prompt/model</summary>",
                    "",
                    "```text",
                    item.get("prompt_text", ""),
                    "```",
                    "",
                    "</details>",
                    "",
                ]
            )
        (self.run_dir / "final_report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the bounded GEPA-style research loop.")
    parser.add_argument("--config", required=True, help="Path to a JSON config file.")
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    config = read_json(config_path)
    orchestrator = ResearchOrchestrator(config=config, config_path=config_path)
    try:
        state = orchestrator.run()
    except AgentError as exc:
        print(f"Agent runtime error: {exc}")
        print(f"Artifacts, if any: {orchestrator.run_dir}")
        raise SystemExit(2) from exc
    print(f"Run complete. Best={state.best_candidate_id} score={state.best_score:.4f}")
    print(f"Artifacts: {orchestrator.run_dir}")


if __name__ == "__main__":
    main()
