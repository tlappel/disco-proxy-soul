"""Pinned exchanges (📌)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path


class SavedStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, channel_name: str, partner: str, companion: str, user_msg: str, reply: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n---\n📌 {stamp} | #{channel_name}\n")
            handle.write(f"**{partner}:** {user_msg}\n")
            handle.write(f"**{companion}:** {reply}\n")

    def read_tail(self, max_chars: int = 1800) -> str:
        if not self.path.exists():
            return ""
        text = self.path.read_text(encoding="utf-8")
        return text[-max_chars:] if len(text) > max_chars else text
