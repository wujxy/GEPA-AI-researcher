from __future__ import annotations

import json
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from .io_utils import write_json
from .schemas import AgentCallRecord, RoundUsageSummary, RunUsageSummary, TokenUsage


KNOWN_ROLES = ("proposer", "executor", "judger", "gater")
TOKEN_KEYS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
    "processed_tokens",
)


def normalize_usage(envelope: dict[str, Any] | None) -> TokenUsage:
    raw = dict((envelope or {}).get("usage") or {})
    if not raw:
        return TokenUsage(available=False)
    values = {
        "input_tokens": _non_negative_int(raw.get("input_tokens")),
        "output_tokens": _non_negative_int(raw.get("output_tokens")),
        "cache_creation_input_tokens": _non_negative_int(raw.get("cache_creation_input_tokens"), default=0),
        "cache_read_input_tokens": _non_negative_int(raw.get("cache_read_input_tokens"), default=0),
    }
    if values["input_tokens"] is None or values["output_tokens"] is None:
        return TokenUsage(available=False)
    processed = sum(int(value or 0) for value in values.values())
    return TokenUsage(**values, processed_tokens=processed, available=True)


def _non_negative_int(value: Any, default: int | None = None) -> int | None:
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, number)


class UsageTracker:
    """Thread-safe call ledger and deterministic usage aggregation."""

    def __init__(self, run_dir: Path, config: dict[str, Any] | None = None):
        self.run_dir = run_dir
        self.config = config or {}
        self.enabled = bool(self.config.get("enabled", True))
        self.persist_raw = bool(self.config.get("persist_raw_envelope", True))
        self.usage_dir = run_dir / "usage"
        self.calls_path = self.usage_dir / "agent_calls.jsonl"
        self._lock = threading.Lock()
        self._records: dict[str, AgentCallRecord] = {}
        self._load_existing()

    def _load_existing(self) -> None:
        if not self.calls_path.exists():
            return
        for line in self.calls_path.read_text(encoding="utf-8").splitlines():
            try:
                data = json.loads(line)
                record = _record_from_dict(data)
            except (ValueError, TypeError, KeyError):
                continue
            self._records[record.call_id] = record

    def record(self, record: AgentCallRecord, raw_envelope: dict[str, Any] | None = None) -> None:
        if not self.enabled:
            return
        with self._lock:
            if record.call_id in self._records:
                return
            self.usage_dir.mkdir(parents=True, exist_ok=True)
            with self.calls_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
            self._records[record.call_id] = record
            if self.persist_raw and raw_envelope is not None:
                write_json(self.usage_dir / "raw" / f"{record.call_id}.json", raw_envelope)

    def round_summary(self, round_id: int, persist: bool = True) -> RoundUsageSummary:
        records = [record for record in self._records.values() if record.context.round_id == round_id]
        summary = RoundUsageSummary(
            round_id=round_id,
            calls=len(records),
            unavailable_calls=sum(not record.usage.available for record in records),
            by_role=_group(records, lambda record: record.context.role, include_known_roles=True),
            by_candidate=_group(
                records,
                lambda record: record.context.candidate_id or "<batch-or-run>",
            ),
            totals=_totals(records),
        )
        if self.enabled and persist:
            write_json(self.usage_dir / f"round_{round_id:04d}.json", summary.to_dict())
        return summary

    def run_summary(self, persist: bool = True) -> RunUsageSummary:
        records = list(self._records.values())
        summary = RunUsageSummary(
            calls=len(records),
            unavailable_calls=sum(not record.usage.available for record in records),
            by_role=_group(records, lambda record: record.context.role, include_known_roles=True),
            by_round=_group(records, lambda record: str(record.context.round_id)),
            by_candidate=_group(
                records,
                lambda record: record.context.candidate_id or "<batch-or-run>",
            ),
            totals=_totals(records),
        )
        if self.enabled and persist:
            write_json(self.usage_dir / "run_summary.json", summary.to_dict())
        return summary

    def records(self) -> list[AgentCallRecord]:
        with self._lock:
            return list(self._records.values())


def format_round_usage(summary: RoundUsageSummary) -> str:
    lines = [
        f"Token Usage | Round {summary.round_id + 1}",
        "role       calls   input   cache_create   cache_read   output   processed   cost_usd   unavailable",
    ]
    for role in [*KNOWN_ROLES, *sorted(set(summary.by_role) - set(KNOWN_ROLES))]:
        row = summary.by_role.get(role, _empty_totals())
        lines.append(_format_row(role, row))
    lines.append(_format_row("TOTAL", summary.totals))
    lines.append("Token Usage by Candidate")
    lines.append("candidate             calls   processed   cost_usd   unavailable")
    for candidate_id, row in sorted(summary.by_candidate.items()):
        lines.append(
            f"{candidate_id:<21} {row['calls']:>5} {row['processed_tokens']:>11} "
            f"{row['total_cost_usd']:>10.6f} {row['unavailable_calls']:>13}"
        )
    return "\n".join(lines)


def format_run_usage(summary: RunUsageSummary) -> str:
    lines = [
        "Token Usage | Run Summary",
        "role       calls   input   cache_create   cache_read   output   processed   cost_usd   unavailable",
    ]
    for role in [*KNOWN_ROLES, *sorted(set(summary.by_role) - set(KNOWN_ROLES))]:
        lines.append(_format_row(role, summary.by_role.get(role, _empty_totals())))
    lines.append(_format_row("TOTAL", summary.totals))
    lines.append("Token Usage by Round")
    lines.append("round       calls   processed   cost_usd   unavailable")
    for round_id, row in sorted(summary.by_round.items(), key=lambda item: int(item[0])):
        lines.append(
            f"{round_id:<11} {row['calls']:>5} {row['processed_tokens']:>11} "
            f"{row['total_cost_usd']:>10.6f} {row['unavailable_calls']:>13}"
        )
    lines.append("Token Usage by Candidate")
    lines.append("candidate             calls   processed   cost_usd   unavailable")
    for candidate_id, row in sorted(summary.by_candidate.items()):
        lines.append(
            f"{candidate_id:<21} {row['calls']:>5} {row['processed_tokens']:>11} "
            f"{row['total_cost_usd']:>10.6f} {row['unavailable_calls']:>13}"
        )
    return "\n".join(lines)


def _format_row(label: str, row: dict[str, Any]) -> str:
    return (
        f"{label:<10} {row['calls']:>5} {row['input_tokens']:>7} "
        f"{row['cache_creation_input_tokens']:>14} {row['cache_read_input_tokens']:>12} "
        f"{row['output_tokens']:>8} {row['processed_tokens']:>11} "
        f"{row['total_cost_usd']:>10.6f} {row['unavailable_calls']:>13}"
    )


def _group(
    records: Iterable[AgentCallRecord],
    key_fn,
    include_known_roles: bool = False,
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[AgentCallRecord]] = defaultdict(list)
    for record in records:
        grouped[str(key_fn(record))].append(record)
    if include_known_roles:
        for role in KNOWN_ROLES:
            grouped.setdefault(role, [])
    return {key: _totals(group) for key, group in grouped.items()}


def _totals(records: Iterable[AgentCallRecord]) -> dict[str, Any]:
    rows = list(records)
    totals = _empty_totals()
    totals["calls"] = len(rows)
    totals["unavailable_calls"] = sum(not record.usage.available for record in rows)
    for record in rows:
        if record.usage.available:
            for key in TOKEN_KEYS:
                totals[key] += int(getattr(record.usage, key) or 0)
        totals["total_cost_usd"] += float(record.total_cost_usd or 0.0)
    return totals


def _empty_totals() -> dict[str, Any]:
    return {
        "calls": 0,
        "unavailable_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "processed_tokens": 0,
        "total_cost_usd": 0.0,
    }


def _record_from_dict(data: dict[str, Any]) -> AgentCallRecord:
    from .schemas import AgentCallContext

    usage_data = dict(data.get("usage") or {})
    context_data = dict(data.get("context") or {})
    return AgentCallRecord(
        call_id=str(data["call_id"]),
        context=AgentCallContext(**context_data),
        status=str(data.get("status", "unknown")),
        started_at=str(data.get("started_at", "")),
        finished_at=str(data.get("finished_at", "")),
        duration_ms=int(data.get("duration_ms", 0)),
        usage=TokenUsage(**usage_data),
        model=data.get("model"),
        total_cost_usd=data.get("total_cost_usd"),
        model_usage=dict(data.get("model_usage") or {}),
        session_id=data.get("session_id"),
        error=data.get("error"),
    )
