"""Social attention, throttling, and cancellation regressions."""

from __future__ import annotations

import asyncio
import unittest

from disco_proxy_soul.adapters.ollama_attention import AttentionDecision
from disco_proxy_soul.discord_app.social_presence import (
    SocialMessage,
    SocialPresence,
    clear_name_address,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def message(number: int, content: str = "What do people think?") -> SocialMessage:
    return SocialMessage(
        guild_id="1",
        channel_id="22",
        channel_name="community",
        message_id=str(number),
        author_id=str(100 + number),
        author_name=f"Human {number}",
        content=content,
    )


class SocialPresenceTests(unittest.IsolatedAsyncioTestCase):
    def make_presence(self, *, judge=None, ambient=True, sleep=None, capacity=6):
        clock = FakeClock()
        presence = SocialPresence(
            companion_name="Naomi",
            judge=judge,
            ambient_enabled=ambient,
            debounce_seconds=0.01,
            buffer_messages=12,
            buffer_chars=4000,
            attention_threshold=0.82,
            engagement_seconds=120,
            cooldown_seconds=0,
            budget_capacity=capacity,
            budget_refill_per_hour=0.1,
            clock=clock,
            sleep=sleep or asyncio.sleep,
        )
        return presence, clock

    async def test_default_deterministic_mode_never_buffers_or_calls_model(self):
        calls = 0

        async def judge(*args, **kwargs):
            nonlocal calls
            calls += 1
            return AttentionDecision("speak", 1.0, "yes")

        presence, _ = self.make_presence(judge=judge, ambient=False)
        self.assertIsNone(await presence.consider(message(1), direct_trigger=None))
        direct = await presence.consider(message(2), direct_trigger="mention")
        self.assertEqual(direct.trigger, "mention")
        self.assertEqual(direct.ambient_context, "")
        self.assertEqual(calls, 0)

    async def test_notice_must_succeed_before_local_ambient_processing(self):
        calls = 0

        async def judge(*args, **kwargs):
            nonlocal calls
            calls += 1
            return AttentionDecision("speak", 1.0, "yes")

        presence, _ = self.make_presence(judge=judge)
        self.assertIsNone(await presence.consider(message(1), direct_trigger=None))
        self.assertEqual(calls, 0)

        presence.confirm_notice("22")
        route = await presence.consider(message(2), direct_trigger=None)
        self.assertTrue(route.discretionary)
        self.assertEqual(calls, 1)

    async def test_burst_only_latest_message_reaches_gate(self):
        release = asyncio.Event()
        calls = 0

        async def sleep(_seconds):
            await release.wait()

        async def judge(*args, **kwargs):
            nonlocal calls
            calls += 1
            return AttentionDecision("ignore", 1.0, "quiet")

        presence, _ = self.make_presence(judge=judge, sleep=sleep)
        presence.confirm_notice("22")
        first = asyncio.create_task(
            presence.consider(message(1, "first"), direct_trigger=None)
        )
        await asyncio.sleep(0)
        second = asyncio.create_task(
            presence.consider(message(2, "second"), direct_trigger=None)
        )
        await asyncio.sleep(0)
        release.set()
        self.assertIsNone(await first)
        self.assertIsNone(await second)
        self.assertEqual(calls, 1)
        self.assertEqual(presence.counters.stale_decisions, 1)

    async def test_soft_budget_preserves_direct_routes_after_discretionary_depletion(self):
        async def judge(*args, **kwargs):
            return AttentionDecision("speak", 1.0, "opening")

        presence, _ = self.make_presence(judge=judge, capacity=1)
        presence.confirm_notice("22")
        first = await presence.consider(message(1), direct_trigger=None)
        self.assertTrue(first.discretionary)
        presence.mark_response("22")
        second = await presence.consider(message(2), direct_trigger=None)
        self.assertIsNone(second)
        self.assertEqual(presence.counters.budget_suppressions, 1)

        direct = await presence.consider(message(3), direct_trigger="reply")
        self.assertEqual(direct.trigger, "reply")

    async def test_new_human_message_cancels_inflight_discretionary_work(self):
        routed = asyncio.Event()
        hold = asyncio.Event()

        async def judge(*args, **kwargs):
            return AttentionDecision("speak", 1.0, "opening")

        presence, _ = self.make_presence(judge=judge)
        presence.confirm_notice("22")

        async def first_flow():
            route = await presence.consider(message(1), direct_trigger=None)
            self.assertTrue(route.discretionary)
            routed.set()
            try:
                await hold.wait()
            finally:
                presence.clear_inflight("22")

        first = asyncio.create_task(first_flow())
        await routed.wait()
        direct = await presence.consider(message(2), direct_trigger="mention")
        self.assertEqual(direct.trigger, "mention")
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertEqual(presence.counters.inflight_cancellations, 1)

    async def test_direct_rate_limit_refills_without_touching_discretionary_budget(self):
        presence, clock = self.make_presence(ambient=False)
        self.assertTrue(presence.allow_direct("guest"))
        self.assertTrue(presence.allow_direct("guest"))
        self.assertTrue(presence.allow_direct("guest"))
        self.assertFalse(presence.allow_direct("guest"))
        self.assertEqual(presence.counters.direct_rate_suppressions, 1)
        clock.advance(30)
        self.assertTrue(presence.allow_direct("guest"))

    def test_clear_name_address_requires_a_real_vocative(self):
        self.assertTrue(clear_name_address("Naomi, what do you think?", "Naomi"))
        self.assertTrue(clear_name_address("hey Naomi: come look", "Naomi"))
        self.assertFalse(clear_name_address("Naomi would probably agree", "Naomi"))
        self.assertFalse(clear_name_address("I spoke with Naomi yesterday", "Naomi"))


if __name__ == "__main__":
    unittest.main()
