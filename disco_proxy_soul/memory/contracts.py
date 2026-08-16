"""Memory contracts for v2.

Two backends share this protocol:

- FileMemoryBackend — default, complete, no network
- ExternalMemoryBackend — optional HTTP or in-process hook. Not required
  for standalone file memory.

Rolling history, outreach counters, and saved pins stay off this protocol.
Those belong to the Discord window (ConversationStore), not identity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Sequence


@dataclass(frozen=True)
class Scope:
    channel_id: str
    persona_id: str


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
