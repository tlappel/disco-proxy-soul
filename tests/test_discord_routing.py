"""Pure Discord participation-policy tests."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from disco_proxy_soul.discord_app.bot import (
    _CompanionCommandTree,
    _author_allowed,
    _response_trigger,
)


class DiscordRoutingTests(unittest.TestCase):
    def test_configured_partner_fails_closed_for_other_authors(self) -> None:
        self.assertTrue(_author_allowed(7, 7))
        self.assertFalse(_author_allowed(7, 99))
        self.assertTrue(_author_allowed(0, 99))

    def test_active_channel_allows_unaddressed_message(self) -> None:
        self.assertEqual(
            _response_trigger(
                mentioned=False, is_dm=False, in_active=True, is_reply=False
            ),
            "active-channel",
        )


class CommandPrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def test_partner_commands_allowed_and_guest_commands_fail_privately(self):
        client = discord.Client(intents=discord.Intents.none())
        app = SimpleNamespace(
            config=SimpleNamespace(partner_user_id=7),
            persona=SimpleNamespace(companion_name="Naomi"),
        )
        tree = _CompanionCommandTree(client, app)

        partner = SimpleNamespace(
            user=SimpleNamespace(id=7),
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        self.assertTrue(await tree.interaction_check(partner))
        partner.response.send_message.assert_not_awaited()

        guest = SimpleNamespace(
            user=SimpleNamespace(id=99),
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        self.assertFalse(await tree.interaction_check(guest))
        guest.response.send_message.assert_awaited_once()
        self.assertTrue(guest.response.send_message.await_args.kwargs["ephemeral"])
        await client.close()

    def test_unlisted_unaddressed_message_is_ignored(self) -> None:
        self.assertIsNone(
            _response_trigger(
                mentioned=False, is_dm=False, in_active=False, is_reply=False
            )
        )

    def test_direct_routes_take_precedence_over_active_channel(self) -> None:
        self.assertEqual(
            _response_trigger(
                mentioned=True, is_dm=False, in_active=True, is_reply=True
            ),
            "mention",
        )
        self.assertEqual(
            _response_trigger(
                mentioned=False, is_dm=True, in_active=True, is_reply=True
            ),
            "dm",
        )


if __name__ == "__main__":
    unittest.main()
