from __future__ import annotations

from time import perf_counter
from typing import Any

from .schemas import Candidate, SampleTrace, Trace


class PaperQAExecutor:
    """Fixed, low-freedom executor for the initial paper-QA MVP."""

    def execute(self, candidate: Candidate, config: dict[str, Any]) -> Trace:
        samples = []
        selected_ids = set(config.get("_selected_sample_ids") or [])
        task_samples = config["task"].get("samples", [])
        if selected_ids and task_samples:
            task_samples = [sample for sample in task_samples if str(sample.get("sample_id")) in selected_ids]
        for sample in task_samples:
            start = perf_counter()
            output, logs = self._answer_sample(candidate.prompt_text, sample)
            latency_ms = int((perf_counter() - start) * 1000)
            samples.append(
                SampleTrace(
                    sample_id=sample["sample_id"],
                    input=sample["question"],
                    output=output,
                    expected=sample["expected_answer"],
                    logs=logs,
                    latency_ms=latency_ms,
                    artifacts={"eval_phase": config.get("_eval_phase", "pareto")},
                )
            )
        return Trace(candidate_id=candidate.candidate_id, round_id=candidate.round_id, samples=samples)

    def _answer_sample(self, prompt: str, sample: dict[str, Any]) -> tuple[str, str]:
        context = sample["context"]
        expected = sample["expected_answer"]
        evidence = sample.get("evidence", "")
        prompt_lower = prompt.lower()
        has_evidence_rule = "evidence" in prompt_lower or "cite" in prompt_lower
        has_unknown_rule = "unknown" in prompt_lower
        expected_present = expected.lower() in context.lower()

        if expected_present:
            if has_evidence_rule:
                return (
                    f"Answer: {expected} | Evidence: {evidence}",
                    "expected answer found in context; evidence-aware prompt used",
                )
            return (
                f"The answer is {expected}.",
                "expected answer found in context; baseline prompt used",
            )

        if has_unknown_rule:
            return (
                "Answer: UNKNOWN | Evidence: UNKNOWN",
                "expected answer absent; unknown fallback used",
            )
        return (
            f"The answer is probably {expected}.",
            "expected answer absent; baseline hallucinated likely answer",
        )
