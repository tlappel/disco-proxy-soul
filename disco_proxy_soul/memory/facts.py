"""Always-on facts file. Seeded from the persona package on first run."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from ..persona.schema import format_facts
from .store import load_json, save_json

_TYPE_GUARDS: dict[str, type] = {
    "background": list,
    "current_situation": list,
    "emotional_needs": list,
    "important_people": dict,
    "preferences": dict,
}


class FactStore:
    def __init__(self, path: Path, seed: dict[str, Any]) -> None:
        self.path = path
        loaded = load_json(str(path), None)
        if not isinstance(loaded, dict) or not loaded:
            self._facts = dict(seed)
            if self._facts and "last_updated" not in self._facts:
                self._facts["last_updated"] = datetime.now().isoformat()
            if self._facts:
                save_json(str(path), self._facts)
        else:
            self._facts = loaded

    def raw(self) -> dict[str, Any]:
        return dict(self._facts)

    def format(self) -> str:
        return format_facts(self._facts)

    def apply_updates(self, updates: dict[str, Any]) -> list[str]:
        safe: dict[str, Any] = {}
        for key, value in updates.items():
            expected = _TYPE_GUARDS.get(key)
            if expected and not isinstance(value, expected):
                print(
                    f"[memory] Facts type mismatch — skipping '{key}' "
                    f"(got {type(value).__name__}, expected {expected.__name__})"
                )
                continue
            safe[key] = value
        if not safe:
            return []
        self._facts.update(safe)
        self._facts["last_updated"] = datetime.now().isoformat()
        save_json(str(self.path), self._facts)
        return list(safe.keys())
