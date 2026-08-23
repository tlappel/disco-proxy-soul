"""Pure Discord participation-policy tests."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from disco_proxy_soul.discord_app.bot import (
    _CompanionCommandTree,
    _author_allowed,
    _message_policy,
    _response_trigger,
    _social_author_kind,
    social_ambient_notice,
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

    def test_room_policy_separates_partner_identity_from_public_disclosure(self):
        self.assertIsNone(
            _message_policy(
                mode="private",
                partner_configured=True,
                is_partner=False,
                direct_trigger="mention",
                private_active=True,
            )
        )
        social_partner = _message_policy(
            mode="social",
            partner_configured=True,
            is_partner=True,
            direct_trigger=None,
            private_active=False,
        )
        self.assertEqual(social_partner.route_kind, "social")
        self.assertEqual(social_partner.disclosure_scope, "public")
        addressed_guest = _message_policy(
            mode="addressed",
            partner_configured=True,
            is_partner=False,
            direct_trigger="mention",
            private_active=False,
        )
        self.assertEqual(addressed_guest.route_kind, "immediate")
        self.assertEqual(addressed_guest.disclosure_scope, "public")

    def test_social_notice_discloses_local_gate_and_selected_cloud_context(self):
        notice = social_ambient_notice(
            "Naomi", attention_model="qwen3:1.7b", response_provider="xai"
        )
        self.assertIn("local Ollama model `qwen3:1.7b`", notice)
        self.assertIn("configured external `xai` response provider", notice)
        self.assertIn("RAM", notice)
        self.assertIn("Attachments", notice)
        self.assertIn("approved AI residents", notice)

    def test_social_author_kind_admits_people_and_approved_residents_only(self):
        residents = (700,)
        self.assertEqual(
            _social_author_kind(
                author_id=12,
                is_bot=False,
                is_self=False,
                resident_user_ids=residents,
            ),
            "human",
        )
        self.assertEqual(
            _social_author_kind(
                author_id=700,
                is_bot=True,
                is_self=False,
                resident_user_ids=residents,
            ),
            "ai_resident",
        )
        self.assertIsNone(
            _social_author_kind(
                author_id=800,
                is_bot=True,
                is_self=False,
                resident_user_ids=residents,
            )
        )
        self.assertIsNone(
            _social_author_kind(
                author_id=700,
                is_bot=True,
                is_self=True,
                resident_user_ids=residents,
            )
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
