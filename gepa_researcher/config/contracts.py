from __future__ import annotations

import json
from typing import Any


ROLE_FIELDS = {
    "proposer": ("objective", "metric", "resources", "safety", "runtime"),
    "executor": ("objective", "metric", "validation", "resources", "safety", "runtime"),
    "judger": ("objective", "metric", "validation"),
}


def role_contract(config: dict[str, Any], role: str) -> dict[str, Any] | None:
    contracts = config.get("contracts")
    if not isinstance(contracts, dict):
        return None
    return {field: contracts.get(field, {}) for field in ROLE_FIELDS[role]}


def format_role_contract(config: dict[str, Any], role: str) -> str:
    contract = role_contract(config, role)
    if contract is None:
        return ""
    return f"{role.capitalize()} contract (authoritative):\n" + json.dumps(
        contract,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
