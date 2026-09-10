"""Exercise destination privacy through parsed Discord messages and real cognition."""

import os
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from disco_proxy_soul.config import RuntimeConfig
from disco_proxy_soul.discord_app.bot import build_bot
from disco_proxy_soul.resident_runtime import RuntimeDeliveryPreparation, RuntimeOutcome
import test_continuity as continuity
from test_continuity import PublicPersona, PARTNER_ID, provenance

BOT = {"id": "400", "username": "SyntheticBot", "discriminator": "0", "avatar": None, "bot": True}


class MessagePrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = continuity.ContinuityTests().make_app(self.root)
        self.app.persona = PublicPersona()
        self.app.persona.identity = "PRIVATE_IDENTITY_SENTINEL"
        self.app.catalog = {}
        with patch.dict(os.environ, {}, clear=True):
            self.app.config = replace(
                RuntimeConfig.from_env(), partner_user_id=PARTNER_ID,
                watch_channel_id=11, active_channel_ids=(22,),
                social_channel_ids=(44,), addressed_channel_ids=(45,),
                ignored_channel_ids=(46,), max_recent=60, compress_chunk=50,
                social_direct_burst=1000,
            )
        self.app._cached_recall = {"synthetic-cache": []}
        self.cache = dict(self.app._cached_recall)
        self.client = build_bot(self.app)
        self.addAsyncCleanup(self.client.close)
        self.client._connection.user = discord.ClientUser(data=BOT, state=self.client._connection)
        self.counter = 100000000000000000
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.reply = self.stack.enter_context(patch.object(discord.Message, "reply", new_callable=AsyncMock))
        self.send = self.stack.enter_context(patch.object(discord.abc.Messageable, "send", new_callable=AsyncMock))
        self.reply.return_value = SimpleNamespace(id=900)
        self.send.return_value = SimpleNamespace(id=901)
        # Typing is transport I/O; the handler, parser and context assembly are real.
        self.typing = self.stack.enter_context(patch.object(discord.abc.Messageable, "typing"))

    def message(self, room, *, actor=PARTNER_ID, kind="server", trigger="mention"):
        self.counter += 1
        state = self.client._connection
        user = {"id": str(actor), "username": "SyntheticHuman", "discriminator": "0", "avatar": None}
        if kind in ("dm", "first-dm", "other-dm"):
            if kind == "first-dm":
                channel, _ = state._get_guild_channel({"channel_id": str(room)})
            else:
                recipient = user if kind == "dm" else dict(user, id="999")
                channel = discord.DMChannel(me=self.client.user, state=state,
                    data={"id": str(room), "recipients": [recipient]})
        elif kind == "group":
            channel = discord.GroupChannel(me=self.client.user, state=state, data={
                "id": str(room), "owner_id": str(actor), "name": "synthetic-group",
                "recipients": [user, dict(user, id="999")],
            })
        elif kind == "thread":
            guild = discord.Guild(data={"id": "1", "name": "synthetic-guild", "roles": []}, state=state)
            channel = discord.Thread(guild=guild, state=state, data={
                "id": str(room), "parent_id": "11", "owner_id": str(actor),
                "name": "synthetic-thread", "type": 11, "message_count": 0, "member_count": 1,
                "thread_metadata": {"archived": False, "archive_timestamp": "2026-09-09T00:00:00+00:00",
                                    "auto_archive_duration": 60, "locked": False},
            })
        elif kind == "cached-server":
            guild = discord.Guild(data={"id": "1", "name": "synthetic-guild", "roles": []}, state=state)
            channel = discord.TextChannel(state=state, guild=guild, data={
                "id": str(room), "name": "synthetic-room", "type": 0,
                "position": 0, "permission_overwrites": [],
            })
        elif kind == "server":
            channel, _ = state._get_guild_channel({"channel_id": str(room), "guild_id": "1"})
        else:
            channel = discord.PartialMessageable(state=state, id=room)
        payload = {
            "id": str(self.counter), "type": 0, "content": "<@400> Synthetic greeting" if trigger == "mention" else "Synthetic greeting",
            "author": user, "mentions": [BOT] if trigger == "mention" else [],
            "attachments": [], "embeds": [], "timestamp": "2026-09-09T00:00:00+00:00",
            "edited_timestamp": None, "pinned": False, "tts": False, "mention_everyone": False,
        }
        if kind in ("server", "cached-server", "thread"):
            payload["guild_id"] = "1"
        message = discord.Message(state=state, channel=channel, data=payload)
        if trigger == "reply":
            message.reference = SimpleNamespace(resolved=SimpleNamespace(author=self.client.user))
        return message

    def reset_delivery(self):
        self.reply.reset_mock()
        self.send.reset_mock()
        self.typing.reset_mock()

    async def test_public_destinations_never_load_private_material(self):
        cases = [
            (PARTNER_ID, PARTNER_ID, 48, "server", "mention"),
            (PARTNER_ID, PARTNER_ID, 48, "cached-server", "mention"),
            (PARTNER_ID, PARTNER_ID, 48, "server", "reply"),
            (PARTNER_ID, PARTNER_ID, 47, "thread", "mention"),
            (PARTNER_ID, PARTNER_ID, 44, "server", "mention"),
            (PARTNER_ID, PARTNER_ID, 45, "server", "mention"),
            (PARTNER_ID, 99, 45, "server", "mention"),
            (0, 99, 45, "server", "mention"),
            (0, 99, 48, "server", "reply"),
            (0, 99, 11, "server", "ordinary"),
            (0, 99, 48, "dm", "ordinary"),
            (PARTNER_ID, PARTNER_ID, 11, "group", "mention"),
            (PARTNER_ID, PARTNER_ID, 11, "unknown", "mention"),
            (PARTNER_ID, PARTNER_ID, 11, "other-dm", "mention"),
        ]
        with ExitStack() as stack:
            spies = [stack.enter_context(patch.object(owner, method,
                side_effect=AssertionError("public path accessed private material")))
                for owner, method in ((self.app, "_maybe_recall"), (self.app, "_cross_surface_recent"),
                                      (self.app.journal, "read_tail"), (self.app.facts, "format"))]
            for partner, actor, room, kind, trigger in cases:
                with self.subTest(partner=partner, actor=actor, room=room, kind=kind, trigger=trigger):
                    self.app.config = replace(self.app.config, partner_user_id=partner)
                    old = provenance(str(room), source_id=f"old-{self.counter}")
                    self.app.history.append(str(room), "assistant", "PRIVATE_HISTORY_SENTINEL", old)
                    self.app.models.response_text = "Public synthetic reply. " * 200
                    self.reset_delivery()
                    message = self.message(room, actor=actor, kind=kind, trigger=trigger)
                    await self.client.on_message(message)
                    tier, request = self.app.models.requests[-1]
                    self.assertEqual(tier, "social")
                    assembled = request.system + str(request.messages)
                    self.assertNotIn("PRIVATE_HISTORY_SENTINEL", assembled)
                    self.assertNotIn("Private journal line.", assembled)
                    self.assertNotIn("PRIVATE_IDENTITY_SENTINEL", assembled)
                    self.assertNotIn("marshmallow", assembled)
                    self.assertFalse(request.tools)
                    self.assertEqual(self.app.history.get(str(room))[-1]["provenance"]["disclosure_scope"], "public")
                    chunks = self.reply.await_args_list + self.send.await_args_list
                    self.assertEqual("".join(call.args[0] for call in chunks), self.app.models.response_text)
                    self.assertGreater(len(chunks), 1)
                    self.assertTrue(all(len(call.args[0]) <= 2000 for call in chunks))
            for spy in spies:
                spy.assert_not_called()
        self.assertEqual(self.app._cached_recall, self.cache)

    async def test_private_room_thread_and_bot_dm_preserve_private_context(self):
        for room, kind in ((11, "server"), (11, "cached-server"), (22, "thread"), (48, "dm"), (49, "first-dm")):
            with self.subTest(room=room, kind=kind):
                self.reset_delivery()
                message = self.message(room, kind=kind, trigger="ordinary")
                if kind == "server":
                    self.assertIsNone(message.guild)
                    self.assertEqual(message.channel.guild_id, 1)
                await self.client.on_message(message)
                tier, request = self.app.models.requests[-1]
                self.assertEqual(tier, "primary")
                self.assertIn("Private journal line.", request.system)
                self.assertTrue(request.tools)
                self.reply.assert_awaited_once()
                stored = self.app.history.get(str(room))[-1]["provenance"]
                self.assertEqual(stored["disclosure_scope"], "private")
                self.assertEqual(stored["surface"], "dm" if "dm" in kind else "thread" if kind == "thread" else "text")
                self.assertEqual(stored.get("guild_id"), None if "dm" in kind else "1")

    async def test_room_reclassification_does_not_project_private_history_publicly(self):
        self.app.models.response_text = "PRIVATE_RESPONSE_SENTINEL"
        await self.client.on_message(self.message(11, trigger="ordinary"))
        self.app.config = replace(self.app.config, watch_channel_id=0, addressed_channel_ids=(11, 45))
        self.app.models.response_text = "Public answer"
        await self.client.on_message(self.message(11))
        tier, request = self.app.models.requests[-1]
        self.assertEqual(tier, "social")
        self.assertNotIn("PRIVATE_RESPONSE_SENTINEL", str(request.messages))
        self.assertNotIn("Private journal line.", request.system)
        self.assertFalse(request.tools)
        stored = self.app.history.get("11")
        self.assertEqual([item["provenance"]["disclosure_scope"] for item in stored],
                         ["private", "private", "public", "public"])
        self.app.config = replace(self.app.config, watch_channel_id=11, addressed_channel_ids=(45,))
        await self.client.on_message(self.message(11, trigger="ordinary"))
        self.assertIn("PRIVATE_RESPONSE_SENTINEL", str(self.app.models.requests[-1][1].messages))
        self.assertIn("Private journal line.", self.app.models.requests[-1][1].system)
        self.assertEqual(self.app._cached_recall, self.cache)

    async def test_denied_messages_have_no_private_reads_or_delivery(self):
        with patch.object(self.app, "respond", side_effect=AssertionError("denied cognition")) as respond:
            for room, kind, actor, trigger in (
                (11, "server", 99, "mention"), (48, "dm", 99, "ordinary"),
                (46, "server", PARTNER_ID, "mention"), (48, "server", 99, "mention"),
                (47, "thread", PARTNER_ID, "ordinary"), (11, "group", PARTNER_ID, "ordinary"),
                (11, "unknown", PARTNER_ID, "ordinary"), (11, "other-dm", PARTNER_ID, "ordinary"),
            ):
                with self.subTest(room=room, kind=kind, actor=actor):
                    before = {p.name: p.read_bytes() for p in self.root.iterdir()}
                    self.reset_delivery()
                    await self.client.on_message(self.message(room, kind=kind, actor=actor, trigger=trigger))
                    self.reply.assert_not_awaited()
                    self.send.assert_not_awaited()
                    self.typing.assert_not_called()
                    self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})
        respond.assert_not_called()
        self.assertFalse(self.app.models.requests)

    async def test_connected_turn_uses_same_destination_scope(self):
        runtime = SimpleNamespace(
            start=AsyncMock(), close=AsyncMock(),
            complete=AsyncMock(return_value=RuntimeOutcome("outcome", "Connected answer")),
            prepare_delivery=AsyncMock(return_value=RuntimeDeliveryPreparation("send", "attempt")),
            record_delivery_result=AsyncMock(),
        )
        client = build_bot(self.app, resident_runtime=runtime)
        self.addAsyncCleanup(client.close)
        client._connection.user = self.client.user
        for partner, room, kind, expected in (
            (PARTNER_ID, 48, "server", "public"), (0, 45, "server", "public"),
            (PARTNER_ID, 47, "thread", "public"), (PARTNER_ID, 11, "server", "private"),
            (PARTNER_ID, 48, "first-dm", "private"),
        ):
            with self.subTest(partner=partner, room=room, kind=kind):
                self.app.config = replace(self.app.config, partner_user_id=partner)
                message = self.message(room, kind=kind)
                await client.on_message(message)
                turn = runtime.complete.await_args.args[0]
                self.assertEqual(turn.sources[-1].disclosure_scope, expected)
                self.assertEqual(turn.sources[-1].text, message.content)
                self.assertEqual(runtime.record_delivery_result.await_args.kwargs["status"], "confirmed")
        self.assertFalse(self.app.models.requests)
        self.assertFalse(self.app.history.get("48"))
