"""Resident runtime boundary for the Discord body.

Discord owns transport and participation behavior.  The embedded application
owns the resident response and resident-facing control operations.  These
protocols name that existing boundary; the broad embedded facade is not the
future transport-neutral Everthread contract.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Protocol, Sequence, runtime_checkable

from .app import CompanionApp
from .config import RuntimeConfig
from .memory.contracts import MemoryRecord, TurnProvenance
from .models.contracts import ContentPart
from .persona.schema import PersonaPackage


@runtime_checkable
class ResidentRuntime(Protocol):
    """The current one-cognition path shared by every Discord surface."""

    async def respond(
        self,
        channel_id: str,
        user_text: str,
        parts: Sequence[ContentPart] | None = None,
        recall_source: str = "automatic",
        interaction_mode: str | None = None,
        provenance: TurnProvenance | None = None,
        ambient_context: str = "",
        store_history: bool = True,
    ) -> str: ...


class EmbeddedDiscordRuntime(ResidentRuntime, Protocol):
    """Legacy embedded operations used by the Discord body and commands.

    The public attributes reflect the existing embedded command surface.  They
    are compatibility only.  A connected Everthread runtime must use a smaller
    transport-neutral contract, replace raw store access with explicit
    operations, and remain the sole resident-content writer.
    """

    config: RuntimeConfig
    persona: PersonaPackage
    outreach: Any
    history: Any
    facts: Any
    moments: Any
    journal: Any
    saved: Any
    primary_model: str
    catalog: dict[str, str]
    presence_loaded: bool

    def set_primary_model(self, model_ref: str) -> str: ...

    def record_exchange(
        self,
        channel_id: str,
        user_text: str,
        reply: str,
        provenance: TurnProvenance | None,
    ) -> None: ...

    async def list_memories(
        self, channel_id: str, author_id: int | str | None = None
    ) -> list[MemoryRecord]: ...

    async def recall_command(
        self,
        channel_id: str,
        query: str,
        author_id: int | str | None = None,
    ) -> list[MemoryRecord]: ...

    async def pin_exchange(
        self,
        channel_id: str,
        channel_name: str,
        user_text: str,
        reply: str,
        author_id: int | str | None = None,
    ) -> None: ...

    def forget_exchange(self, channel_id: str, assistant_text: str) -> bool: ...

    async def keep_moment(
        self, channel_id: str, text: str, author_id: int | str | None = None
    ) -> None: ...

    async def prune(self, channel_id: str) -> int: ...

    async def redist(
        self, channel_id: str, author_id: int | str | None = None
    ) -> tuple[int, int]: ...

    async def maybe_reach_out(self) -> str | None: ...

    def toggle_presence(self) -> bool: ...

    def reload_persona(self) -> PersonaPackage: ...

    def export_paths(self) -> dict[str, Path]: ...


RuntimeFactory = Callable[[RuntimeConfig], EmbeddedDiscordRuntime]


def build_embedded_runtime(
    config: RuntimeConfig,
    *,
    factory: RuntimeFactory | None = None,
) -> EmbeddedDiscordRuntime:
    """Construct exactly one resident runtime for the Discord process."""

    runtime_factory = factory or CompanionApp.from_env
    return runtime_factory(config)
