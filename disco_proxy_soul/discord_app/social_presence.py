"""Bounded, opt-in social attention for shared Discord text rooms."""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

from ..adapters.ollama_attention import AttentionDecision, OllamaAttentionError
from ..safety import sanitize_incoming_text

AttentionJudge = Callable[..., Awaitable[AttentionDecision]]
AVAILABILITY_MODES = {"unavailable", "listening", "open", "seeking"}


@dataclass(frozen=True)
class SocialMessage:
    guild_id: str
    channel_id: str
    channel_name: str
    message_id: str
    author_id: str
    author_name: str
    author_kind: str
    content: str
    occurred_at: datetime | None = None


@dataclass(frozen=True)
class SocialRoute:
    trigger: str
    ambient_context: str = ""
    discretionary: bool = False
    source_author_kind: str = "human"
    source_messages: tuple[SocialMessage, ...] = ()


@dataclass(frozen=True)
class SocialAvailability:
    mode: str = "open"
    note: str = ""
    expires_at: float | None = None


@dataclass
class SocialCounters:
    observed: int = 0
    direct_routes: int = 0
    ambient_gate_calls: int = 0
    ambient_considers: int = 0
    ambient_waits: int = 0
    ambient_ignores: int = 0
    stale_decisions: int = 0
    cooldown_suppressions: int = 0
    budget_suppressions: int = 0
    gate_failures: int = 0
    inflight_cancellations: int = 0
    direct_rate_suppressions: int = 0
    ai_chain_suppressions: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    gate_duration_ms: float = 0.0


@dataclass
class _ChannelState:
    buffer: deque[SocialMessage]
    generation: int = 0
    notice_confirmed: bool = False
    last_spoke_at: float = float("-inf")
    engaged_until: float = float("-inf")
    consecutive_ai_turns: int = 0
    inflight_discretionary_task: asyncio.Task[object] | None = None


@dataclass
class _ParticipationBudget:
    capacity: float
    refill_per_hour: float
    clock: Callable[[], float]
    balance: float = field(init=False)
    updated_at: float = field(init=False)

    def __post_init__(self) -> None:
        self.balance = self.capacity
        self.updated_at = self.clock()

    def current(self) -> float:
        now = self.clock()
        elapsed_hours = max(0.0, now - self.updated_at) / 3600
        self.balance = min(
            self.capacity, self.balance + elapsed_hours * self.refill_per_hour
        )
        self.updated_at = now
        return self.balance

    @property
    def pressure(self) -> float:
        if self.capacity <= 0:
            return 1.0
        return 1.0 - (self.current() / self.capacity)

    def spend(self, amount: float) -> bool:
        if self.current() < amount:
            return False
        self.balance -= amount
        return True


class SocialPresence:
    """Observe only noticed social rooms and fail closed around local judgment."""

    def __init__(
        self,
        *,
        companion_name: str,
        judge: AttentionJudge | None,
        ambient_enabled: bool,
        debounce_seconds: float,
        buffer_messages: int,
        buffer_chars: int,
        engagement_seconds: float,
        cooldown_seconds: float,
        budget_capacity: float,
        budget_refill_per_hour: float,
        source_capture_enabled: bool | None = None,
        direct_burst: int = 3,
        direct_refill_per_minute: float = 2.0,
        social_posture: str = "",
        ai_chain_limit: int = 4,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.companion_name = companion_name
        self.judge = judge
        self.ambient_enabled = bool(ambient_enabled and judge is not None)
        self.source_capture_enabled = (
            self.ambient_enabled
            if source_capture_enabled is None
            else bool(source_capture_enabled)
        )
        self.debounce_seconds = max(0.0, debounce_seconds)
        self.buffer_messages = max(2, buffer_messages)
        self.buffer_chars = max(200, buffer_chars)
        self.engagement_seconds = max(0.0, engagement_seconds)
        self.cooldown_seconds = max(0.0, cooldown_seconds)
        self.clock = clock
        self.sleep = sleep
        self.social_posture = social_posture.strip()[:1000]
        self.ai_chain_limit = max(2, ai_chain_limit)
        self._availability = SocialAvailability()
        self.counters = SocialCounters()
        self._channels: dict[str, _ChannelState] = {}
        self._budget = _ParticipationBudget(
            capacity=max(1.0, budget_capacity),
            refill_per_hour=max(0.01, budget_refill_per_hour),
            clock=clock,
        )
        self._direct_burst = max(1, direct_burst)
        self._direct_refill_per_hour = max(0.01, direct_refill_per_minute) * 60
        self._direct_budgets: dict[str, _ParticipationBudget] = {}

    def confirm_notice(self, channel_id: int | str) -> None:
        self._state(str(channel_id)).notice_confirmed = True

    def notice_confirmed(self, channel_id: int | str) -> bool:
        return self._state(str(channel_id)).notice_confirmed

    def allow_direct(self, author_id: int | str) -> bool:
        key = str(author_id)
        budget = self._direct_budgets.get(key)
        if budget is None:
            budget = _ParticipationBudget(
                capacity=float(self._direct_burst),
                refill_per_hour=self._direct_refill_per_hour,
                clock=self.clock,
            )
            self._direct_budgets[key] = budget
        if budget.spend(1.0):
            return True
        self.counters.direct_rate_suppressions += 1
        return False

    def set_availability(
        self, mode: str, *, duration_minutes: float = 480.0, note: str = ""
    ) -> SocialAvailability:
        normalized = mode.strip().lower()
        if normalized not in AVAILABILITY_MODES:
            raise ValueError(
                "availability must be unavailable, listening, open, or seeking"
            )
        clean_note = " ".join(note.split())[:240]
        expires_at = None
        if normalized != "open":
            bounded_minutes = max(1.0, min(1440.0, float(duration_minutes)))
            expires_at = self.clock() + bounded_minutes * 60
        self._availability = SocialAvailability(
            mode=normalized,
            note=clean_note,
            expires_at=expires_at,
        )
        return self._availability

    def availability(self) -> SocialAvailability:
        current = self._availability
        if current.expires_at is not None and self.clock() >= current.expires_at:
            current = SocialAvailability()
            self._availability = current
        return current

    async def consider(
        self, message: SocialMessage, *, direct_trigger: str | None
    ) -> SocialRoute | None:
        state = self._state(message.channel_id)
        self.counters.observed += 1
        if not message.content.strip() and direct_trigger is None:
            return None
        current_task = asyncio.current_task()
        inflight = state.inflight_discretionary_task
        if (
            inflight is not None
            and inflight is not current_task
            and not inflight.done()
        ):
            inflight.cancel()
            self.counters.inflight_cancellations += 1
            state.inflight_discretionary_task = None

        can_capture_sources = self.source_capture_enabled and state.notice_confirmed
        can_observe_ambient = self.ambient_enabled and state.notice_confirmed
        if can_capture_sources:
            state.buffer.append(
                SocialMessage(
                    guild_id=message.guild_id,
                    channel_id=message.channel_id,
                    channel_name=_label(message.channel_name),
                    message_id=message.message_id,
                    author_id=message.author_id,
                    author_name=_label(message.author_name),
                    author_kind=_author_kind(message.author_kind),
                    content=message.content,
                    occurred_at=message.occurred_at,
                )
            )
            state.generation += 1
            if message.author_kind == "ai_resident":
                state.consecutive_ai_turns += 1
            else:
                state.consecutive_ai_turns = 0

        if direct_trigger is not None:
            self.counters.direct_routes += 1
            return SocialRoute(
                trigger=direct_trigger,
                ambient_context=(
                    self._render(state, exclude_message_id=message.message_id)
                    if can_capture_sources
                    else ""
                ),
                source_author_kind=message.author_kind,
                source_messages=(
                    self._source_messages(state, exclude_message_id=message.message_id)
                    if can_capture_sources
                    else ()
                ),
            )

        if not can_observe_ambient or self.judge is None:
            return None

        current_availability = self.availability()
        if current_availability.mode == "unavailable":
            return None
        if (
            message.author_kind == "ai_resident"
            and state.consecutive_ai_turns >= self.ai_chain_limit
        ):
            self.counters.ai_chain_suppressions += 1
            return None

        generation = state.generation
        now = self.clock()
        pressure = self._budget.pressure
        effective_cooldown = self.cooldown_seconds * (1.0 + 2.0 * pressure)
        if now - state.last_spoke_at < effective_cooldown:
            self.counters.cooldown_suppressions += 1
            return None

        await self.sleep(self.debounce_seconds)
        if state.generation != generation:
            self.counters.stale_decisions += 1
            return None

        ambient = self._render(state)
        engaged = self.clock() < state.engaged_until
        self.counters.ambient_gate_calls += 1
        try:
            decision = await self.judge(
                ambient,
                engaged=engaged,
                social_posture=self.social_posture,
                availability=_format_availability(current_availability, self.clock()),
            )
        except OllamaAttentionError:
            self.counters.gate_failures += 1
            return None
        if state.generation != generation:
            self.counters.stale_decisions += 1
            return None
        self._observe_decision(decision)

        if decision.decision == "wait":
            self.counters.ambient_waits += 1
            return None
        if decision.decision != "consider":
            self.counters.ambient_ignores += 1
            return None

        if not self._budget.spend(1.0):
            self.counters.budget_suppressions += 1
            return None
        state.last_spoke_at = self.clock()
        state.inflight_discretionary_task = current_task
        self.counters.ambient_considers += 1
        return SocialRoute(
            trigger="social-attention",
            ambient_context=self._render(state, exclude_message_id=message.message_id),
            discretionary=True,
            source_author_kind=message.author_kind,
            source_messages=self._source_messages(
                state, exclude_message_id=message.message_id
            ),
        )

    def mark_response(
        self,
        channel_id: int | str,
        *,
        source_author_kind: str = "human",
        close_source_window: bool = False,
    ) -> None:
        state = self._state(str(channel_id))
        now = self.clock()
        state.last_spoke_at = now
        state.engaged_until = now + self.engagement_seconds
        if source_author_kind == "ai_resident":
            state.consecutive_ai_turns += 1
        if close_source_window:
            state.buffer.clear()
        state.inflight_discretionary_task = None

    def clear_inflight(self, channel_id: int | str) -> None:
        state = self._state(str(channel_id))
        if state.inflight_discretionary_task is asyncio.current_task():
            state.inflight_discretionary_task = None

    def status_text(self) -> str:
        c = self.counters
        balance = self._budget.current()
        availability = _format_availability(self.availability(), self.clock())
        return (
            f"Ambient model: {'enabled' if self.ambient_enabled else 'disabled'}; "
            f"noticed rooms: {sum(1 for item in self._channels.values() if item.notice_confirmed)}\n"
            f"Door sign: {availability}\n"
            f"Observed: {c.observed}; direct: {c.direct_routes}; gates: {c.ambient_gate_calls}\n"
            f"Gate results: consider {c.ambient_considers}, wait {c.ambient_waits}, "
            f"ignore {c.ambient_ignores}, failure {c.gate_failures}\n"
            f"Suppressed: stale {c.stale_decisions}, cooldown {c.cooldown_suppressions}, "
            f"budget {c.budget_suppressions}, canceled {c.inflight_cancellations}\n"
            f"Direct rate suppressions: {c.direct_rate_suppressions}; "
            f"AI-chain suppressions: {c.ai_chain_suppressions}\n"
            f"Local gate tokens: input {c.prompt_tokens}, output {c.output_tokens}; "
            f"time {c.gate_duration_ms:.0f} ms\n"
            f"Discretionary budget: {balance:.2f}/{self._budget.capacity:.2f}"
        )

    def _state(self, channel_id: str) -> _ChannelState:
        state = self._channels.get(channel_id)
        if state is None:
            state = _ChannelState(buffer=deque(maxlen=self.buffer_messages))
            self._channels[channel_id] = state
        return state

    def _render(
        self, state: _ChannelState, *, exclude_message_id: str | None = None
    ) -> str:
        lines: list[str] = []
        used = 0
        for message in reversed(state.buffer):
            if message.message_id == exclude_message_id:
                continue
            actor = "AI resident" if message.author_kind == "ai_resident" else "human"
            line = (
                f"[{actor}: {message.author_name}] "
                f"{sanitize_incoming_text(message.content)}"
            )
            if lines and used + len(line) > self.buffer_chars:
                break
            if len(line) > self.buffer_chars:
                continue
            lines.append(line)
            used += len(line)
        lines.reverse()
        return "\n".join(lines)

    @staticmethod
    def _source_messages(
        state: _ChannelState, *, exclude_message_id: str
    ) -> tuple[SocialMessage, ...]:
        return tuple(
            item for item in state.buffer if item.message_id != exclude_message_id
        )

    def _observe_decision(self, decision: AttentionDecision) -> None:
        self.counters.prompt_tokens += decision.prompt_tokens
        self.counters.output_tokens += decision.output_tokens
        self.counters.gate_duration_ms += decision.total_duration_ms


def clear_name_address(content: str, companion_name: str) -> bool:
    name = re.escape(companion_name.strip())
    if not name:
        return False
    return bool(
        re.match(
            rf"^\s*(?:hey\s+)?@?{name}\b\s*(?:$|[:,—-])",
            content,
            re.IGNORECASE,
        )
    )


def _label(value: str) -> str:
    return " ".join(str(value).replace("[", "(").replace("]", ")").split())[:80]


def _author_kind(value: str) -> str:
    return "ai_resident" if value == "ai_resident" else "human"


def _format_availability(value: SocialAvailability, now: float) -> str:
    text = value.mode
    if value.note:
        text += f" — {value.note}"
    if value.expires_at is not None:
        minutes = max(1, round((value.expires_at - now) / 60))
        text += f" (expires in about {minutes} minutes)"
    return text
