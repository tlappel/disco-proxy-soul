"""Significant-moment journal (markdown append)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path


class JournalStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, summary: str, tags: list[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        tag_str = ", ".join(tags) if tags else ""
        entry = f"\n---\n📓 {timestamp} | {tag_str}\n{summary}\n"
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(entry)

    def read_tail(self, max_chars: int = 1800) -> str:
        if not self.path.exists():
            return ""
        text = self.path.read_text(encoding="utf-8")
        return text[-max_chars:] if len(text) > max_chars else text

    def entry_count(self) -> int:
        if not self.path.exists():
            return 0
        return self.path.read_text(encoding="utf-8").count("---")
