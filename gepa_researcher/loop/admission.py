from __future__ import annotations

import fnmatch
import hashlib
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any

from ..models.schemas import AdmissionDecision, Candidate


class CandidateAdmissionGate:
    """Deterministic, config-driven eligibility gate before expensive execution."""

    def evaluate(
        self,
        candidate: Candidate,
        config: dict[str, Any],
        *,
        known_candidate_ids: set[str] | None = None,
        accepted_parent_ids: set[str] | None = None,
        batch_candidate_ids: set[str] | None = None,
    ) -> AdmissionDecision:
        policy = dict(config.get("candidate_policy") or {})
        checks: dict[str, str] = {}
        codes: list[str] = []
        details: list[str] = []

        if not policy:
            decision = AdmissionDecision(
                decision_id=str(uuid.uuid4()),
                candidate_id=candidate.candidate_id,
                round_id=candidate.round_id,
                admitted=True,
                checks={"policy": "not_configured"},
            )
            self._attach(candidate, decision)
            return decision

        required = list(
            policy.get(
                "required_fields",
                ["hypothesis", "proposed_change", "target_files", "safety_class", "strategy"],
            )
        )
        missing = [name for name in required if not getattr(candidate, name, None)]
        if missing:
            codes.append("SCHEMA_INVALID")
            details.append(f"missing required fields: {missing}")
            checks["schema"] = "fail"
        else:
            checks["schema"] = "pass"

        known_candidate_ids = known_candidate_ids or set()
        batch_candidate_ids = batch_candidate_ids or set()
        if candidate.candidate_id in known_candidate_ids or candidate.candidate_id not in batch_candidate_ids:
            codes.append("DUPLICATE_CANDIDATE_ID")
            details.append(f"candidate id is not unique: {candidate.candidate_id}")
            checks["candidate_id"] = "fail"
        else:
            checks["candidate_id"] = "pass"

        parent_ids = list(candidate.parent_ids)
        accepted_parent_ids = accepted_parent_ids or set()
        if parent_ids and any(parent_id not in accepted_parent_ids for parent_id in parent_ids):
            codes.append("PARENT_NOT_ACCEPTED")
            details.append(f"unaccepted or unknown parent(s): {sorted(set(parent_ids) - accepted_parent_ids)}")
            checks["parent"] = "fail"
        else:
            checks["parent"] = "pass"

        max_targets = int(policy.get("max_target_files", 1000000))
        if len(candidate.target_files) > max_targets:
            codes.append("TOO_MANY_TARGETS")
            details.append(f"{len(candidate.target_files)} target files exceeds max {max_targets}")

        repo_path = Path(config.get("workspace", {}).get("repo_path", ".")).expanduser()
        baseline_ref = str(config.get("workspace", {}).get("baseline_ref", "HEAD"))
        allowed = list(policy.get("allowed_target_globs", []))
        frozen = list(policy.get("frozen_globs", []))
        paths_ok = True
        for target in candidate.target_files:
            normalized = target.replace("\\", "/").lstrip("./")
            if not (repo_path / normalized).exists() and not _git_tree_has_path(
                repo_path, baseline_ref, normalized
            ):
                codes.append("TARGET_NOT_FOUND")
                details.append(normalized)
                paths_ok = False
            if allowed and not _matches_any(normalized, allowed):
                codes.append("TARGET_NOT_ALLOWED")
                details.append(normalized)
                paths_ok = False
            if _matches_any(normalized, frozen):
                codes.append("FROZEN_PATH")
                details.append(normalized)
                paths_ok = False
        checks["paths"] = "pass" if paths_ok else "fail"

        allowed_safety = set(policy.get("allowed_safety_classes", []))
        if allowed_safety and candidate.safety_class not in allowed_safety:
            codes.append("DISALLOWED_SAFETY_CLASS")
            details.append(candidate.safety_class)
            checks["safety"] = "fail"
        else:
            checks["safety"] = "pass"

        allowed_strategies = set(policy.get("allowed_strategies", []))
        normalized_strategy = _normalize_strategy(candidate.strategy)
        if allowed_strategies and not any(
            normalized_strategy == _normalize_strategy(allowed)
            or normalized_strategy.startswith(f"{_normalize_strategy(allowed)} ")
            for allowed in allowed_strategies
        ):
            codes.append("DISALLOWED_STRATEGY")
            details.append(candidate.strategy)
            checks["strategy"] = "fail"
        else:
            checks["strategy"] = "pass"

        allowed_classes = set(policy.get("allowed_candidate_classes", ["safe-source"]))
        candidate_class = str(candidate.artifacts.get("candidate_class", "safe-source"))
        if allowed_classes and candidate_class not in allowed_classes:
            codes.append("DISALLOWED_CANDIDATE_CLASS")
            details.append(candidate_class)
            checks["candidate_class"] = "fail"
        else:
            checks["candidate_class"] = "pass"

        contract_targets = candidate.executor_contract.get("target_files")
        if contract_targets is not None and set(map(str, contract_targets)) != set(candidate.target_files):
            codes.append("CONTRACT_MISMATCH")
            details.append("executor_contract.target_files differs from candidate.target_files")
            checks["contract"] = "fail"
        else:
            checks["contract"] = "pass"

        searchable = " ".join(
            [
                candidate.scope,
                candidate.proposed_change,
                str(candidate.executor_contract.get("instructions", "")),
            ]
        )
        mentioned_frozen = [
            pattern
            for pattern in frozen
            if not any(char in pattern for char in "*?[")
            and re.search(rf"(?<![\w/]){re.escape(pattern)}(?![\w/])", searchable)
        ]
        if mentioned_frozen:
            codes.append("FROZEN_PATH")
            details.append(f"proposal text requests frozen path(s): {mentioned_frozen}")
            checks["contract"] = "fail"

        fingerprint = idea_fingerprint(candidate)
        refuted = set(map(str, policy.get("refuted_fingerprints", [])))
        if fingerprint in refuted:
            codes.append("REFUTED_DUPLICATE")
            details.append(fingerprint)
            checks["novelty"] = "fail"
        else:
            checks["novelty"] = "pass"

        decision = AdmissionDecision(
            decision_id=str(uuid.uuid4()),
            candidate_id=candidate.candidate_id,
            round_id=candidate.round_id,
            admitted=not codes,
            checks=checks,
            failure_codes=list(dict.fromkeys(codes)),
            details=details,
        )
        self._attach(candidate, decision)
        candidate.artifacts["idea_fingerprint"] = fingerprint
        return decision

    @staticmethod
    def _attach(candidate: Candidate, decision: AdmissionDecision) -> None:
        candidate.admission_decision_id = decision.decision_id
        candidate.admission_status = "admitted" if decision.admitted else "rejected"


def idea_fingerprint(candidate: Candidate) -> str:
    normalized = "|".join(
        [
            ",".join(sorted(path.lower() for path in candidate.target_files)),
            candidate.strategy.lower().strip(),
            re.sub(r"\s+", " ", candidate.proposed_change.lower()).strip(),
        ]
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]


def _normalize_strategy(value: str) -> str:
    normalized = re.sub(r"\s+", " ", str(value).strip().lower())
    normalized = normalized.replace("safe pattern", "safe-pattern")
    return normalized


def _matches_any(path: str, patterns: list[str]) -> bool:
    return any(
        fnmatch.fnmatch(path, pattern)
        or ("**/" in pattern and fnmatch.fnmatch(path, pattern.replace("**/", "")))
        for pattern in patterns
    )


def _git_tree_has_path(repo: Path, ref: str, path: str) -> bool:
    if not (repo / ".git").exists():
        return False
    return subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{ref}:{path}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0
