"""Bounded, opt-in social attention for shared Discord text rooms."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
import re
import time
from typing import Awaitable, Callable

from ..adapters.ollama_attention import AttentionDecision, OllamaAttentionError
from ..safety import sanitize_incoming_text


AttentionJudge = Callable[..., Awaitable[AttentionDecision]]


@dataclass(frozen=True)
class SocialMessage:
    guild_id: str
    channel_id: str
    channel_name: str
    message_id: str
    author_id: str
    author_name: str
    content: str


@dataclass(frozen=True)
class SocialRoute:
    trigger: str
    ambient_context: str = ""
    discretionary: bool = False


@dataclass
class SocialCounters:
    observed: int = 0
    direct_routes: int = 0
    ambient_gate_calls: int = 0
    ambient_speaks: int = 0
    ambient_waits: int = 0
    ambient_ignores: int = 0
    stale_decisions: int = 0
    cooldown_suppressions: int = 0
    budget_suppressions: int = 0
    gate_failures: int = 0
    inflight_cancellations: int = 0
    direct_rate_suppressions: int = 0
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
        attention_threshold: float,
        engagement_seconds: float,
        cooldown_seconds: float,
        budget_capacity: float,
        budget_refill_per_hour: float,
        direct_burst: int = 3,
        direct_refill_per_minute: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.companion_name = companion_name
        self.judge = judge
        self.ambient_enabled = bool(ambient_enabled and judge is not None)
        self.debounce_seconds = max(0.0, debounce_seconds)
        self.buffer_messages = max(2, buffer_messages)
        self.buffer_chars = max(200, buffer_chars)
        self.attention_threshold = max(0.5, min(0.99, attention_threshold))
        self.engagement_seconds = max(0.0, engagement_seconds)
        self.cooldown_seconds = max(0.0, cooldown_seconds)
        self.clock = clock
        self.sleep = sleep
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

        can_observe_ambient = self.ambient_enabled and state.notice_confirmed
        if can_observe_ambient:
            state.buffer.append(
                SocialMessage(
                    guild_id=message.guild_id,
                    channel_id=message.channel_id,
                    channel_name=_label(message.channel_name),
                    message_id=message.message_id,
                    author_id=message.author_id,
                    author_name=_label(message.author_name),
                    content=sanitize_incoming_text(message.content),
                )
            )
            state.generation += 1

        if direct_trigger is not None:
            self.counters.direct_routes += 1
            return SocialRoute(
                trigger=direct_trigger,
                ambient_context=(
                    self._render(state, exclude_message_id=message.message_id)
                    if can_observe_ambient
                    else ""
                ),
            )

        if not can_observe_ambient or self.judge is None:
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
            decision = await self.judge(ambient, engaged=engaged)
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
        if decision.decision != "speak":
            self.counters.ambient_ignores += 1
            return None

        threshold = min(
            0.99,
            self.attention_threshold
            + (self._budget.pressure * 0.15)
            - (0.08 if engaged else 0.0),
        )
        if decision.confidence < threshold:
            self.counters.ambient_ignores += 1
            return None
        if not self._budget.spend(1.0):
            self.counters.budget_suppressions += 1
            return None
        state.last_spoke_at = self.clock()
        state.inflight_discretionary_task = current_task
        self.counters.ambient_speaks += 1
        return SocialRoute(
            trigger="social-attention",
            ambient_context=self._render(
                state, exclude_message_id=message.message_id
            ),
            discretionary=True,
        )

    def mark_response(self, channel_id: int | str) -> None:
        state = self._state(str(channel_id))
        now = self.clock()
        state.last_spoke_at = now
        state.engaged_until = now + self.engagement_seconds
        state.inflight_discretionary_task = None

    def clear_inflight(self, channel_id: int | str) -> None:
        state = self._state(str(channel_id))
        if state.inflight_discretionary_task is asyncio.current_task():
            state.inflight_discretionary_task = None

    def status_text(self) -> str:
        c = self.counters
        balance = self._budget.current()
        return (
            f"Ambient model: {'enabled' if self.ambient_enabled else 'disabled'}; "
            f"noticed rooms: {sum(1 for item in self._channels.values() if item.notice_confirmed)}\n"
            f"Observed: {c.observed}; direct: {c.direct_routes}; gates: {c.ambient_gate_calls}\n"
            f"Gate results: speak {c.ambient_speaks}, wait {c.ambient_waits}, "
            f"ignore {c.ambient_ignores}, failure {c.gate_failures}\n"
            f"Suppressed: stale {c.stale_decisions}, cooldown {c.cooldown_suppressions}, "
            f"budget {c.budget_suppressions}, canceled {c.inflight_cancellations}\n"
            f"Direct rate suppressions: {c.direct_rate_suppressions}\n"
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
            line = f"[{message.author_name}] {message.content}"
            if lines and used + len(line) > self.buffer_chars:
                break
            if len(line) > self.buffer_chars:
                continue
            lines.append(line)
            used += len(line)
        lines.reverse()
        return "\n".join(lines)

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
