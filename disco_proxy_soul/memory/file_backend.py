"""File-backed long-term memory. Default standalone backend."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .contracts import MemoryBackend, MemoryRecord, Scope
from .recall import prefilter_recall_candidates
from .store import load_json, save_json


def record_to_dict(record: MemoryRecord) -> dict:
    return {
        "id": record.memory_id or "",
        "timestamp": record.timestamp or datetime.now().isoformat(),
        "summary": record.summary,
        "tags": list(record.tags),
        "significance": record.significance,
        **({"metadata": record.metadata} if record.metadata else {}),
    }


def dict_to_record(data: dict) -> MemoryRecord:
    tags = data.get("tags", [])
    if not isinstance(tags, list):
        tags = [tags] if tags else []
    metadata = data.get("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    return MemoryRecord(
        summary=str(data.get("summary", "")),
        tags=tuple(str(tag) for tag in tags if tag),
        significance=float(data.get("significance", 0.5) or 0.5),
        timestamp=data.get("timestamp"),
        memory_id=data.get("id") or data.get("memory_id"),
        metadata={str(key): str(value) for key, value in metadata.items()},
    )


class FileMemoryBackend:
    """JSON file per host. Implements MemoryBackend."""

    def __init__(self, path: Path) -> None:
        self.path = path
        raw = load_json(str(path), {})
        self._store: dict[str, list[dict]] = raw if isinstance(raw, dict) else {}

    def _key(self, scope: Scope) -> str:
        return scope.storage_key

    def _persist(self) -> None:
        save_json(str(self.path), self._store)

    async def list(self, scope: Scope) -> list[MemoryRecord]:
        return [dict_to_record(item) for item in self._store.get(self._key(scope), [])]

    async def save(self, scope: Scope, record: MemoryRecord) -> MemoryRecord:
        key = self._key(scope)
        stored = dict(record_to_dict(record))
        if not stored["id"]:
            stored["id"] = f"{key}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self._store.setdefault(key, []).append(stored)
        self._persist()
        return dict_to_record(stored)

    async def recall(
        self, scope: Scope, query: str, limit: int = 5
    ) -> list[MemoryRecord]:
        records = self._store.get(self._key(scope), [])
        picked = prefilter_recall_candidates(records, query, limit)
        return [dict_to_record(item) for item in picked]

    async def replace_all(self, scope: Scope, records: list[MemoryRecord]) -> None:
        key = self._key(scope)
        self._store[key] = [record_to_dict(record) for record in records]
        self._persist()
