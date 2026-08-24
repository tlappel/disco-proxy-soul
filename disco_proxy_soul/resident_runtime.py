"""Small transport-neutral contract for an optional external resident runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class RuntimeSource:
    source_id: str
    text: str
    occurred_at: datetime
    conversation_id: str
    conversation_label: str | None
    surface: str
    actor_id: str
    actor_label: str
    disclosure_scope: str
    interaction_mode: str
    source_group_id: str | None = None
    source_group_position: int | None = None


@dataclass(frozen=True)
class RuntimeTurn:
    resident_id: str
    person_label: str
    resident_label: str
    sources: tuple[RuntimeSource, ...]
    response_source_id: str


@dataclass(frozen=True)
class RuntimeOutcome:
    outcome_id: str
    text: str
    recovered: bool = False


@dataclass(frozen=True)
class RuntimeDeliveryPreparation:
    disposition: str
    attempt_id: str | None


class ResidentRuntime(Protocol):
    async def start(self) -> None: ...

    async def complete(self, turn: RuntimeTurn) -> RuntimeOutcome: ...

    async def prepare_delivery(
        self,
        *,
        resident_id: str,
        outcome_id: str,
        logical_delivery_id: str,
        target: str,
    ) -> RuntimeDeliveryPreparation: ...

    async def record_delivery_result(
        self,
        *,
        resident_id: str,
        attempt_id: str,
        status: str,
        external_ids: tuple[str, ...] = (),
        error_class: str | None = None,
    ) -> None: ...

    async def close(self) -> None: ...


class EverthreadRuntimeAdapter:
    """Adapt Everthread's synchronous application runtime to Disco's event loop."""

    def __init__(
        self, runtime_factory: Callable[[], tuple[Any, Callable[[], None]]]
    ) -> None:
        self._runtime_factory = runtime_factory
        self._runtime: Any | None = None
        self._close_resources: Callable[[], None] | None = None
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="everthread-runtime"
        )
        self._start_lock = asyncio.Lock()
        self._closed = False

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("Everthread runtime adapter is closed")
        if self._runtime is not None:
            return
        async with self._start_lock:
            if self._runtime is None:
                self._runtime, self._close_resources = await self._run(
                    self._runtime_factory
                )

    async def complete(self, turn: RuntimeTurn) -> RuntimeOutcome:
        await self.start()
        command = _everthread_turn(turn)
        result = await self._run(self._runtime.complete_turn, command)
        return RuntimeOutcome(result.outcome_id, result.text, result.recovered)

    async def prepare_delivery(
        self,
        *,
        resident_id: str,
        outcome_id: str,
        logical_delivery_id: str,
        target: str,
    ) -> RuntimeDeliveryPreparation:
        from everthread.application.delivery import DeliveryAttemptCommand

        await self.start()
        prepared = await self._run(
            self._runtime.prepare_delivery,
            DeliveryAttemptCommand(
                resident_id=resident_id,
                outcome_id=outcome_id,
                surface="discord",
                logical_delivery_id=logical_delivery_id,
                target=target,
            ),
        )
        return RuntimeDeliveryPreparation(
            disposition=prepared.disposition.value,
            attempt_id=prepared.attempt_event_id,
        )

    async def record_delivery_result(
        self,
        *,
        resident_id: str,
        attempt_id: str,
        status: str,
        external_ids: tuple[str, ...] = (),
        error_class: str | None = None,
    ) -> None:
        from everthread.application.delivery import (
            DeliveryResultCommand,
            DeliveryStatus,
        )

        await self.start()
        await self._run(
            self._runtime.record_delivery_result,
            DeliveryResultCommand(
                resident_id=resident_id,
                attempt_event_id=attempt_id,
                status=DeliveryStatus(status),
                external_ids=external_ids,
                error_class=error_class,
            ),
        )

    async def close(self) -> None:
        if self._closed:
            return
        try:
            if self._close_resources is not None:
                await self._run(self._close_resources)
        finally:
            self._runtime = None
            self._close_resources = None
            self._closed = True
            self._executor.shutdown(wait=True)

    async def _run(self, function: Callable[..., Any], *args: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await asyncio.shield(
            loop.run_in_executor(self._executor, function, *args)
        )


def _everthread_turn(turn: RuntimeTurn) -> Any:
    try:
        from everthread.application.connected_runtime import (
            AdmittedConversationSource,
            ConnectedTurnCommand,
        )
        from everthread.domain.events import (
            ConversationSource,
            DisclosureScope,
            SourceRef,
        )
    except ImportError as exc:
        raise RuntimeError(
            "connected runtime requires the Everthread package; standalone Disco is unchanged"
        ) from exc

    sources = tuple(
        AdmittedConversationSource(
            text=source.text,
            occurred_at=source.occurred_at,
            source_ref=SourceRef("discord.message", source.source_id),
            conversation_source=ConversationSource(
                conversation_id=source.conversation_id,
                conversation_label=source.conversation_label,
                surface=source.surface,
                actor_id=source.actor_id,
                actor_label=source.actor_label,
                disclosure_scope=DisclosureScope(source.disclosure_scope),
                interaction_mode=source.interaction_mode,
                source_group_id=source.source_group_id,
                source_group_position=source.source_group_position,
            ),
        )
        for source in turn.sources
    )
    return ConnectedTurnCommand(
        resident_id=turn.resident_id,
        person_label=turn.person_label,
        resident_label=turn.resident_label,
        sources=sources,
        response_source_ref=SourceRef("disco.response", turn.response_source_id),
    )
