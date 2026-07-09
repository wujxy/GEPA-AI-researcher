from __future__ import annotations

from typing import Any

from .schemas import Candidate, Judgment, Trace


class PaperQAJudger:
    """Deterministic judge with numeric score and actionable feedback."""

    def judge(self, candidate: Candidate, trace: Trace, config: dict[str, Any]) -> Judgment:
        sample_lookup = {sample["sample_id"]: sample for sample in config["task"].get("samples", [])}
        per_sample = []
        failures: set[str] = set()

        for item in trace.samples:
            sample = sample_lookup.get(item.sample_id)
            if sample is None:
                failures.add("missing_sample")
                per_sample.append({"sample_id": item.sample_id, "score": 0.0, "notes": "sample missing from selected config"})
                continue
            output_lower = item.output.lower()
            expected_lower = item.expected.lower()
            expected_present = item.expected.lower() in sample["context"].lower()
            has_unknown = "unknown" in output_lower
            has_evidence = sample.get("evidence", "").lower() in output_lower
            has_format = item.output.startswith("Answer:")

            correctness = 1.0 if (expected_present and expected_lower in output_lower) or (not expected_present and has_unknown) else 0.0
            evidence_support = 1.0 if (expected_present and has_evidence) or (not expected_present and has_unknown) else 0.0
            format_compliance = 1.0 if has_format else 0.0
            score = 0.5 * correctness + 0.3 * evidence_support + 0.2 * format_compliance

            if correctness < 1.0:
                failures.add("incorrect_answer")
            if evidence_support < 1.0:
                failures.add("missing_evidence" if expected_present else "unsupported_answer")
            if format_compliance < 1.0:
                failures.add("format_noncompliance")

            per_sample.append(
                {
                    "sample_id": item.sample_id,
                    "score": round(score, 4),
                    "correctness": correctness,
                    "evidence_support": evidence_support,
                    "format_compliance": format_compliance,
                    "notes": item.logs,
                }
            )

        total = round(sum(row["score"] for row in per_sample) / max(len(per_sample), 1), 4)
        feedback = self._feedback_for(failures)
        return Judgment(
            candidate_id=candidate.candidate_id,
            round_id=candidate.round_id,
            score=total,
            passed=total >= config["judger"]["pass_threshold"],
            per_sample_scores=per_sample,
            failure_categories=sorted(failures),
            actionable_feedback=feedback,
            confidence="high",
            artifacts={"eval_phase": config.get("_eval_phase", "pareto"), "sample_ids": config.get("_selected_sample_ids", [])},
        )

    def _feedback_for(self, failures: set[str]) -> list[str]:
        feedback = []
        if "missing_sample" in failures:
            feedback.append("Ensure the selected evaluation samples are present in the task config.")
        if "missing_evidence" in failures:
            feedback.append("Require the answerer to quote or cite the supporting evidence sentence.")
        if "unsupported_answer" in failures:
            feedback.append("When the context lacks evidence, require an exact UNKNOWN answer.")
        if "format_noncompliance" in failures:
            feedback.append("Use a fixed parseable format with Answer and Evidence fields.")
        if "incorrect_answer" in failures:
            feedback.append("Ground the final answer in the provided context rather than prior knowledge.")
        return feedback or ["Candidate satisfies the current rubric; preserve this behavior."]
