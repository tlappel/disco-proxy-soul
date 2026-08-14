"""Append-only memory archive. Redist never touches this."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .store import load_json, save_json


class ArchiveStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        raw = load_json(str(path), {})
        self._store: dict[str, list[dict[str, Any]]] = raw if isinstance(raw, dict) else {}

    def append(self, channel_id: str, record: dict[str, Any]) -> None:
        self._store.setdefault(channel_id, []).append(record)
        save_json(str(self.path), self._store)
