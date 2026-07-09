from __future__ import annotations

from pathlib import Path
from typing import Any


def load_prior_context(config: dict[str, Any], base_dir: Path, max_chars: int = 12000) -> dict[str, Any]:
    context = config.get("context", {}) or {}
    result: dict[str, Any] = {
        "notes": list(context.get("notes", [])) if isinstance(context.get("notes", []), list) else [str(context.get("notes"))],
        "skills": list(context.get("skills", [])) if isinstance(context.get("skills", []), list) else [str(context.get("skills"))],
        "documents": [],
        "warnings": [],
    }
    remaining = max_chars
    for raw_path in context.get("paths", []) or []:
        path = Path(str(raw_path)).expanduser()
        if not path.is_absolute():
            path = (base_dir / path).resolve()
        if not path.exists():
            result["warnings"].append(f"missing context path: {path}")
            continue
        targets = [path]
        if path.is_dir():
            targets = sorted(
                item for item in path.rglob("*")
                if item.is_file() and item.suffix.lower() in {".md", ".txt", ".json", ".py", ".csv"}
            )[:20]
        for target in targets:
            if remaining <= 0:
                break
            try:
                text = target.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:
                result["warnings"].append(f"could not read {target}: {type(exc).__name__}: {exc}")
                continue
            snippet = text[: min(remaining, 2500)]
            result["documents"].append({"path": str(target), "text": snippet})
            remaining -= len(snippet)
    return result
