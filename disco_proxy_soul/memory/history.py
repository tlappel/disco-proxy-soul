"""Rolling conversation history — the Discord window, not long-term memory."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .contracts import TurnProvenance
from .store import load_json, save_json


class ConversationStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        raw = load_json(str(path), {})
        if not isinstance(raw, dict):
            raw = {}
        self._messages: dict[str, list[dict[str, Any]]] = {
            str(key): list(value) for key, value in raw.items() if isinstance(value, list)
        }

    def get(self, channel_id: str) -> list[dict[str, Any]]:
        return list(self._messages.get(channel_id, []))

    def append(
        self,
        channel_id: str,
        role: str,
        content: Any,
        provenance: TurnProvenance | None = None,
    ) -> None:
        if isinstance(content, str) and not content.strip():
            print(f"[history] refused empty {role} turn for {channel_id}")
            return
        entry: dict[str, Any] = {"role": role, "content": content}
        if provenance is not None:
            if provenance.channel_id != channel_id:
                raise ValueError("history provenance channel does not match storage channel")
            entry["provenance"] = provenance.to_dict()
        self._messages.setdefault(channel_id, []).append(entry)
        self.persist()

    def recent_for_continuity(
        self,
        continuity_id: str,
        *,
        exclude_channel_id: str,
        limit: int,
        max_chars: int,
        max_age_minutes: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return bounded, timestamp-ordered turns from other linked surfaces."""

        if not continuity_id or limit <= 0 or max_chars <= 0:
            return []
        candidates: list[tuple[str, str, dict[str, Any]]] = []
        seen: set[tuple[str, str]] = set()
        cutoff = (
            datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
            if max_age_minutes is not None and max_age_minutes > 0
            else None
        )
        for channel_id, messages in self._messages.items():
            if channel_id == exclude_channel_id:
                continue
            for entry in messages:
                provenance = TurnProvenance.from_dict(entry.get("provenance"))
                if provenance is None or provenance.continuity_id != continuity_id:
                    continue
                if cutoff is not None:
                    try:
                        timestamp = datetime.fromisoformat(provenance.timestamp)
                        if timestamp.tzinfo is None:
                            timestamp = timestamp.replace(tzinfo=timezone.utc)
                    except ValueError:
                        continue
                    if timestamp < cutoff:
                        continue
                source_id = provenance.source_id
                role = str(entry.get("role") or "")
                if source_id:
                    dedupe_key = (source_id, role)
                    if dedupe_key in seen:
                        continue
                    seen.add(dedupe_key)
                candidates.append((provenance.timestamp, channel_id, entry))
        candidates.sort(key=lambda item: item[0])

        selected: list[dict[str, Any]] = []
        used_chars = 0
        for _, _, entry in reversed(candidates):
            content = entry.get("content")
            size = len(content) if isinstance(content, str) else 7
            if selected and used_chars + size > max_chars:
                break
            if size > max_chars:
                continue
            selected.append(entry)
            used_chars += size
            if len(selected) >= limit:
                break
        selected.reverse()
        return selected

    def replace(self, channel_id: str, messages: list[dict[str, Any]]) -> None:
        self._messages[channel_id] = list(messages)
        self.persist()

    def clear(self, channel_id: str) -> None:
        self._messages[channel_id] = []
        self.persist()

    def trim(self, channel_id: str, max_messages: int) -> None:
        messages = self._messages.get(channel_id, [])
        if max_messages <= 0:
            self._messages[channel_id] = []
        elif len(messages) > max_messages:
            self._messages[channel_id] = messages[-max_messages:]
        else:
            return
        self.persist()

    def drop_exchange(self, channel_id: str, assistant_text: str) -> bool:
        history = self._messages.get(channel_id, [])
        for index, entry in enumerate(history):
            if entry.get("role") == "assistant" and entry.get("content") == assistant_text:
                del history[max(0, index - 1):index + 1]
                self.persist()
                return True
        return False

    def persist(self) -> None:
        save_json(str(self.path), self._messages)

    def stats(self, current_channel_id: str) -> dict[str, Any]:
        counts = {
            key: len(messages)
            for key, messages in self._messages.items()
            if messages
        }
        largest = sorted(counts.items(), key=lambda item: item[1], reverse=True)
        return {
            "current": counts.get(current_channel_id, 0),
            "channels": len(counts),
            "empty_channels": sum(1 for messages in self._messages.values() if not messages),
            "total": sum(counts.values()),
            "largest": largest[:5],
        }
