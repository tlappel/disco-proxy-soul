"""Rolling conversation history — the Discord window, not long-term memory."""

from __future__ import annotations

from pathlib import Path
from typing import Any

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

    def append(self, channel_id: str, role: str, content: Any) -> None:
        if isinstance(content, str) and not content.strip():
            print(f"[history] refused empty {role} turn for {channel_id}")
            return
        self._messages.setdefault(channel_id, []).append(
            {"role": role, "content": content}
        )
        self.persist()

    def replace(self, channel_id: str, messages: list[dict[str, Any]]) -> None:
        self._messages[channel_id] = list(messages)
        self.persist()

    def clear(self, channel_id: str) -> None:
        self._messages[channel_id] = []
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
