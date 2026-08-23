"""Memory contracts for v2.

Two backends share this protocol:

- FileMemoryBackend — default, complete, no network
- ExternalMemoryBackend — optional HTTP or in-process hook. Not required
  for standalone file memory.

Rolling history, outreach counters, and saved pins stay off this protocol.
Those belong to the Discord window (ConversationStore), not identity.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Protocol, Sequence


@dataclass(frozen=True)
class Scope:
    channel_id: str
    persona_id: str
    continuity_id: str | None = None

    @property
    def storage_key(self) -> str:
        if self.continuity_id:
            return f"continuity:{self.continuity_id}"
        return self.channel_id


@dataclass(frozen=True)
class TurnProvenance:
    """Where a stored turn came from and which continuity may use it."""

    channel_id: str
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    guild_id: str | None = None
    channel_name: str | None = None
    surface: str = "text"
    author_id: str | None = None
    author_name: str | None = None
    author_kind: str = "human"
    trigger: str | None = None
    source_id: str | None = None
    continuity_id: str | None = None
    disclosure_scope: str = "private"

    def for_assistant(self, persona_id: str, companion_name: str) -> "TurnProvenance":
        return replace(
            self,
            author_id=f"companion:{persona_id}",
            author_name=companion_name,
            author_kind="resident",
        )

    def to_dict(self) -> dict[str, str]:
        values = {
            "timestamp": self.timestamp,
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "channel_name": self.channel_name,
            "surface": self.surface,
            "author_id": self.author_id,
            "author_name": self.author_name,
            "author_kind": self.author_kind,
            "trigger": self.trigger,
            "source_id": self.source_id,
            "continuity_id": self.continuity_id,
            "disclosure_scope": self.disclosure_scope,
        }
        return {key: value for key, value in values.items() if value not in (None, "")}

    @classmethod
    def from_dict(cls, data: object) -> "TurnProvenance | None":
        if not isinstance(data, dict) or not data.get("channel_id"):
            return None
        return cls(
            timestamp=str(data.get("timestamp") or ""),
            guild_id=_optional_text(data.get("guild_id")),
            channel_id=str(data["channel_id"]),
            channel_name=_optional_text(data.get("channel_name")),
            surface=str(data.get("surface") or "text"),
            author_id=_optional_text(data.get("author_id")),
            author_name=_optional_text(data.get("author_name")),
            author_kind=str(data.get("author_kind") or "human"),
            trigger=_optional_text(data.get("trigger")),
            source_id=_optional_text(data.get("source_id")),
            continuity_id=_optional_text(data.get("continuity_id")),
            disclosure_scope=str(data.get("disclosure_scope") or "private"),
        )


def _optional_text(value: object) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


@dataclass(frozen=True)
class MemoryRecord:
    summary: str
    tags: tuple[str, ...] = ()
    significance: float = 0.5
    timestamp: str | None = None
    memory_id: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


class MemoryBackend(Protocol):
    """Long-term memory. The file backend implements all of this."""

    async def recall(
        self, scope: Scope, query: str, limit: int = 5
    ) -> list[MemoryRecord]:
        """Return memories relevant to a query."""

    async def save(self, scope: Scope, record: MemoryRecord) -> MemoryRecord:
        """Persist a memory record and return the stored version."""

    async def list(self, scope: Scope) -> list[MemoryRecord]:
        """Return stored memories for the scope, oldest first."""

    async def replace_all(
        self, scope: Scope, records: Sequence[MemoryRecord]
    ) -> None:
        """Replace the live memory set (used by redist). Archive is untouched."""


class ExternalMemoryHooks(Protocol):
    """Optional sidecar. File-only mode never calls these.

    Hearth's NS daemon is one implementation. Any HTTP or in-process
    service that speaks this shape can sit here instead.
    """

    async def recent_context(self, scope: Scope) -> str | None:
        """Cross-surface recents to inject into the prompt, or None."""

    async def capture(self, title: str, text: str, kind: str = "memory") -> None:
        """Deliberate write for a later adapter. File-only mode never calls this.

        kind is a label, not a MemoryBackend method:
        "memory" (working chunk), "moment" (host/partner highlight),
        "journal" (her keep). Journal is not wired yet.
        """
