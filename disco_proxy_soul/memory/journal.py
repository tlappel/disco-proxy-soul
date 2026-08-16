"""Markdown append logs: host moments and the companion's journal."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path


class MarkdownLog:
    """Append-only markdown file. Used for moments and for her journal."""

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


def migrate_journal_to_moments(data_dir: Path, persona_id: str) -> bool:
    """Move the old host highlight file to *_moments.md once.

    Existing *_journal.md was host + partner highlights, not her keep.
    If moments already exists, leave both files alone.
    """
    old = data_dir / f"{persona_id}_journal.md"
    new = data_dir / f"{persona_id}_moments.md"
    if new.exists() or not old.exists():
        return False
    old.rename(new)
    return True


# Older import name
JournalStore = MarkdownLog
