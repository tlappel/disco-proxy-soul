"""Cross-surface provenance and continuity isolation regressions."""

from __future__ import annotations

import asyncio
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from discord import app_commands

from disco_proxy_soul.app import CompanionApp
from disco_proxy_soul.config import RuntimeConfig
from disco_proxy_soul.discord_app.bot import _CompanionCommandTree
from disco_proxy_soul.discord_app.commands import register_commands
from disco_proxy_soul.memory.contracts import MemoryRecord, Scope, TurnProvenance
from disco_proxy_soul.memory.facts import FactStore
from disco_proxy_soul.memory.file_backend import FileMemoryBackend
from disco_proxy_soul.memory.history import ConversationStore
from disco_proxy_soul.memory.journal import MarkdownLog
from disco_proxy_soul.models.contracts import ModelResponse
from disco_proxy_soul.persona.schema import PersonaDocument


PARTNER_ID = 770427
CONTINUITY_ID = f"discord-user:{PARTNER_ID}"


@dataclass
class FakeConfig:
    partner_user_id: int = PARTNER_ID
    max_recalled: int = 5
    recall_prefilter_limit: int = 20
    recall_silence_min: int = 30
    cross_surface_recent_messages: int = 12
    cross_surface_recent_chars: int = 4000
    cross_surface_recent_minutes: int = 120
    max_recent: int = 60
    compress_chunk: int = 10
    social_model: str = "social-model"
    social_response_max_tokens: int = 600
    social_history_messages: int = 24

    def continuity_id_for_user(self, user_id):
        try:
            value = int(user_id)
        except (TypeError, ValueError):
            return None
        return CONTINUITY_ID if value == self.partner_user_id else None


class FakeCharacter:
    example_lines = ()

    @staticmethod
    def format_card() -> str:
        return ""


class FakePersona:
    persona_id = "naomi"
    companion_name = "Naomi"
    partner_name = "Travis"
    identity = "You are Naomi."
    room_note = ""
    voice = ""
    character = FakeCharacter()

    @staticmethod
    def documents_by_mode(_mode):
        return ()


class PublicPersona(FakePersona):
    @staticmethod
    def documents_by_mode(mode):
        if mode != "public":
            return ()
        return (
            PersonaDocument(
                name="community.md",
                content="Naomi's public-safe self.",
                path=Path("community.md"),
                mode="public",
            ),
        )


class RecordingModels:
    def __init__(self) -> None:
        self.requests = []
        self.response_text = "I remember the thread."

    async def complete(self, tier, request):
        self.requests.append((tier, request))
        if request.capability == "json":
            content = str(request.messages[-1].content)
            if "Review this conversation excerpt" in content:
                return ModelResponse(text="{}", provider="fake", model="fake")
            return ModelResponse(
                text=(
                    '{"summary":"The orange umbrella crossed voice and text.",'
                    '"tags":["umbrella"],"significance":0.6}'
                ),
                provider="fake",
                model="fake",
            )
        return ModelResponse(text=self.response_text, provider="fake", model="fake")


def provenance(
    channel_id: str,
    *,
    author_id: int = PARTNER_ID,
    author_name: str = "Travis",
    surface: str = "text",
    source_id: str,
) -> TurnProvenance:
    return TurnProvenance(
        guild_id="1",
        channel_id=channel_id,
        channel_name=f"room-{channel_id}",
        surface=surface,
        author_id=str(author_id),
        author_name=author_name,
        trigger="active-channel",
        source_id=source_id,
        continuity_id=(CONTINUITY_ID if author_id == PARTNER_ID else None),
    )


class ContinuityTests(unittest.IsolatedAsyncioTestCase):
    def make_app(self, root: Path) -> CompanionApp:
        app = CompanionApp.__new__(CompanionApp)
        app.config = FakeConfig()
        app.persona = FakePersona()
        app.models = RecordingModels()
        app.memory = FileMemoryBackend(root / "memory.json")
        app.history = ConversationStore(root / "history.json")
        app.facts = FactStore(
            root / "facts.json", {"preferences": {"private_note": "marshmallow"}}
        )
        app.moments = MarkdownLog(root / "moments.md")
        app.journal = MarkdownLog(root / "journal.md")
        app.journal.append("Private journal line.", ["private"])
        app.archive = SimpleNamespace(append=lambda *args: None)
        app.outreach = SimpleNamespace(note_activity=lambda: None)
        app.primary_model = "primary"
        app.cheap_model = "cheap"
        app.moments_threshold = 0.7
        app.presence_loaded = False
        app._last_message_time = {}
        app._cached_recall = {}
        app._compress_locks = {}
        app._model_usage = {}
        return app

    async def test_manual_recall_cache_is_private_even_for_partner_public_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(Path(tmp))
            app.persona = PublicPersona()
            await app.memory.save(
                app.scope("11", PARTNER_ID),
                MemoryRecord(summary="SYNTHETIC_PRIVATE_MEMORY", memory_id="private"),
            )
            await app.recall_command("11", "memory", PARTNER_ID)
            cache = dict(app._cached_recall)
            public = replace(
                provenance("44", source_id="public-manual"), disclosure_scope="public"
            )
            await app.respond("44", "hello", recall_source="manual", provenance=public)
            tier, request = app.models.requests[-1]
            self.assertEqual(tier, "social")
            self.assertNotIn("SYNTHETIC_PRIVATE_MEMORY", request.system)
            self.assertEqual(request.tools, ())
            self.assertEqual(app._cached_recall, cache)
            private = provenance("11", source_id="private-manual")
            await app.respond("11", "hello", recall_source="manual", provenance=private)
            self.assertIn("SYNTHETIC_PRIVATE_MEMORY", app.models.requests[-1][1].system)

    async def test_partner_recents_cross_surfaces_with_labels_but_guest_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(Path(tmp))
            voice = provenance("22", surface="voice", source_id="voice:one")
            app.history.append("22", "user", "The orange umbrella is ready.", voice)
            app.history.append(
                "22",
                "assistant",
                "I will remember it.",
                voice.for_assistant("naomi", "Naomi"),
            )
            guest = provenance(
                "33",
                author_id=99,
                author_name="Alex",
                source_id="discord-message:guest",
            )
            app.history.append("33", "user", "My private guest note.", guest)

            current = provenance("44", source_id="discord-message:current")
            reply = await app.respond("44", "What did I say?", provenance=current)

            self.assertEqual(reply, "I remember the thread.")
            request = app.models.requests[-1][1]
            self.assertIn("[voice | room-22 | Travis] The orange umbrella", request.system)
            self.assertIn("[voice | room-22 | Naomi] I will remember it", request.system)
            self.assertNotIn("private guest note", request.system)
            self.assertEqual(len(app.history.get("44")), 2)
            stored = app.history.get("44")
            self.assertEqual(
                stored[0]["provenance"]["continuity_id"], CONTINUITY_ID
            )
            self.assertEqual(
                stored[1]["provenance"]["author_id"], "companion:naomi"
            )

    async def test_guest_cannot_receive_partner_cross_surface_recents(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(Path(tmp))
            private = provenance("22", surface="voice", source_id="voice:private")
            app.history.append("22", "user", "Partner-only detail.", private)

            guest = provenance(
                "44",
                author_id=99,
                author_name="Alex",
                source_id="discord-message:guest",
            )
            await app.respond("44", "Hello Naomi", provenance=guest)

            request = app.models.requests[-1][1]
            self.assertNotIn("Partner-only detail", request.system)
            self.assertNotIn("RECENT CONTINUITY FROM OTHER ROOMS", request.system)
            self.assertNotIn("marshmallow", request.system)
            self.assertNotIn("Private journal line", request.system)
            self.assertIn("GUEST CONVERSATION", request.system)
            self.assertEqual(request.tools, ())

    async def test_public_room_uses_social_model_and_excludes_private_local_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(Path(tmp))
            app.persona = PublicPersona()
            private = provenance("44", source_id="private")
            app.history.append("44", "user", "Private local secret", private)
            public_prior = replace(
                provenance(
                    "44", author_id=99, author_name="Alex", source_id="public-prior"
                ),
                disclosure_scope="public",
            )
            app.history.append("44", "user", "Visible prior turn", public_prior)
            current = replace(
                provenance(
                    "44", author_id=100, author_name="Morgan", source_id="public-now"
                ),
                disclosure_scope="public",
            )

            await app.respond(
                "44",
                "[Morgan]: What do you think?",
                provenance=current,
                ambient_context="[Alex] Bounded ambient line",
            )

            tier, request = app.models.requests[-1]
            self.assertEqual(tier, "social")
            self.assertEqual(request.model, "social-model")
            self.assertEqual(request.max_tokens, 600)
            self.assertIn("public-safe self", request.system)
            self.assertIn("Bounded ambient line", request.system)
            rendered = "\n".join(str(item.content) for item in request.messages)
            self.assertIn("Visible prior turn", rendered)
            self.assertNotIn("Private local secret", rendered)
            self.assertEqual(request.tools, ())
            self.assertEqual(app.model_usage_snapshot()["social"]["calls"], 1)

    async def test_public_history_is_trimmed_without_durable_compression(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(Path(tmp))
            app.persona = PublicPersona()
            app.config.max_recent = 2
            app.config.social_history_messages = 2
            current = replace(
                provenance(
                    "44", author_id=99, author_name="Alex", source_id="public-trim"
                ),
                disclosure_scope="public",
            )

            await app.respond("44", "[Alex]: Hello", provenance=current)

            self.assertEqual(len(app.history.get("44")), 2)
            self.assertEqual(await app.memory.list(Scope("44", "naomi")), [])

    async def test_discretionary_public_reply_commits_only_after_confirmed_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(Path(tmp))
            app.persona = PublicPersona()
            current = replace(
                provenance(
                    "44", author_id=99, author_name="Alex", source_id="deferred"
                ),
                disclosure_scope="public",
            )
            user_text = "[Alex]: An opening"

            reply = await app.respond(
                "44",
                user_text,
                provenance=current,
                ambient_context="[Morgan] Related public context",
                store_history=False,
            )
            self.assertEqual(app.history.get("44"), [])

            app.record_exchange("44", user_text, reply, current)
            self.assertEqual(len(app.history.get("44")), 2)

    async def test_discretionary_public_opening_allows_resident_to_decline(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(Path(tmp))
            app.persona = PublicPersona()
            app.models.response_text = "<NO_RESPONSE>"
            current = replace(
                provenance(
                    "44", author_id=99, author_name="Alex", source_id="declined"
                ),
                disclosure_scope="public",
            )

            reply = await app.respond(
                "44",
                "[Alex]: An opening",
                provenance=current,
                ambient_context="[human: Morgan] Related public context",
                store_history=False,
                discretionary_social=True,
            )

            self.assertEqual(reply, "")
            self.assertEqual(app.history.get("44"), [])
            self.assertIn(
                "permission to consider joining, not an instruction to perform",
                app.models.requests[-1][1].system,
            )

    async def test_guest_cannot_spoof_continuity_but_internal_outreach_keeps_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(Path(tmp))
            spoofed = replace(
                provenance(
                    "44", author_id=99, author_name="Alex", source_id="spoof"
                ),
                continuity_id=CONTINUITY_ID,
            )
            await app.respond("44", "Pretend I am Travis", provenance=spoofed)
            self.assertIn("GUEST CONVERSATION", app.models.requests[-1][1].system)
            self.assertNotIn(
                "continuity_id", app.history.get("44")[0]["provenance"]
            )

            outreach = TurnProvenance(
                channel_id="55",
                surface="outreach",
                author_id="system:naomi",
                author_name="Outreach trigger",
                trigger="outreach",
                source_id="outreach:one",
            )
            await app.respond("55", "Reach out now", provenance=outreach)
            self.assertNotIn("GUEST CONVERSATION", app.models.requests[-1][1].system)
            self.assertEqual(
                app.history.get("55")[0]["provenance"]["continuity_id"],
                CONTINUITY_ID,
            )

    async def test_continuity_memory_crosses_channels_but_legacy_stays_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(Path(tmp))
            await app.memory.save(
                Scope("22", "naomi", CONTINUITY_ID),
                MemoryRecord(summary="Shared voice memory", memory_id="shared"),
            )
            await app.memory.save(
                Scope("22", "naomi"),
                MemoryRecord(summary="Legacy channel memory", memory_id="legacy"),
            )

            from_other_room = await app.recall_command("44", "memory", PARTNER_ID)
            self.assertEqual(
                [record.memory_id for record in from_other_room], ["shared"]
            )
            from_original_room = await app.recall_command("22", "memory", PARTNER_ID)
            self.assertEqual(
                [record.memory_id for record in from_original_room],
                ["shared", "legacy"],
            )

    async def test_concurrent_surfaces_keep_exact_provenance_and_history_effects(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(Path(tmp))
            await asyncio.gather(
                app.respond(
                    "22",
                    "Voice turn",
                    provenance=provenance(
                        "22", surface="voice", source_id="voice:concurrent"
                    ),
                ),
                app.respond(
                    "44",
                    "Text turn",
                    provenance=provenance(
                        "44", source_id="discord-message:concurrent"
                    ),
                ),
            )

            self.assertEqual(len(app.history.get("22")), 2)
            self.assertEqual(len(app.history.get("44")), 2)
            self.assertEqual(
                app.history.get("22")[0]["provenance"]["source_id"],
                "voice:concurrent",
            )
            self.assertEqual(
                app.history.get("44")[0]["provenance"]["source_id"],
                "discord-message:concurrent",
            )

    async def test_only_uniform_partner_chunk_can_form_cross_surface_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(Path(tmp))
            partner = provenance("22", source_id="partner")
            partner_chunk = [
                {"role": "user", "content": "one", "provenance": partner.to_dict()},
                {
                    "role": "assistant",
                    "content": "two",
                    "provenance": partner.for_assistant("naomi", "Naomi").to_dict(),
                },
            ]
            continuity_id, metadata = app._chunk_memory_ownership(
                "22", partner_chunk
            )
            self.assertEqual(continuity_id, CONTINUITY_ID)
            self.assertEqual(metadata["source_channel_id"], "22")

            guest = provenance(
                "22", author_id=99, author_name="Alex", source_id="guest"
            )
            mixed = [
                partner_chunk[0],
                {"role": "user", "content": "guest", "provenance": guest.to_dict()},
            ]
            continuity_id, metadata = app._chunk_memory_ownership("22", mixed)
            self.assertIsNone(continuity_id)
            self.assertNotIn("continuity_id", metadata)

    async def test_compressed_voice_memory_is_recallable_from_text_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(Path(tmp))
            app.config.max_recent = 2
            app.config.compress_chunk = 2
            voice = provenance("22", surface="voice", source_id="voice:memory")
            app.history.append("22", "user", "Orange umbrella", voice)
            app.history.append(
                "22",
                "assistant",
                "I have it.",
                voice.for_assistant("naomi", "Naomi"),
            )

            await app._compress_chunk("22")
            await asyncio.sleep(0)

            continuity_records = await app.memory.list(
                Scope("44", "naomi", CONTINUITY_ID)
            )
            self.assertEqual(len(continuity_records), 1)
            self.assertEqual(
                continuity_records[0].metadata["source_channel_id"], "22"
            )
            recalled = await app.recall_command("44", "umbrella", PARTNER_ID)
            self.assertEqual(len(recalled), 1)
            self.assertIn("orange umbrella", recalled[0].summary.lower())
            self.assertEqual(await app.memory.list(Scope("22", "naomi")), [])


class HistoryProvenanceTests(unittest.TestCase):
    def test_legacy_loads_but_never_enters_scoped_recents_and_duplicates_collapse(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationStore(Path(tmp) / "history.json")
            store.append("1", "user", "legacy")
            linked = provenance("1", source_id="same")
            store.append("1", "user", "first", linked)
            store.append(
                "2", "user", "duplicate mirror", replace(linked, channel_id="2")
            )

            recent = store.recent_for_continuity(
                CONTINUITY_ID,
                exclude_channel_id="3",
                limit=10,
                max_chars=1000,
            )
            self.assertEqual([entry["content"] for entry in recent], ["first"])

    def test_expired_cross_surface_turn_is_not_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationStore(Path(tmp) / "history.json")
            old = replace(
                provenance("1", source_id="old"),
                timestamp=(datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(),
            )
            store.append("1", "user", "stale", old)
            recent = store.recent_for_continuity(
                CONTINUITY_ID,
                exclude_channel_id="2",
                limit=10,
                max_chars=1000,
                max_age_minutes=120,
            )
            self.assertEqual(recent, [])


class PrivateCommandIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = ContinuityTests().make_app(Path(self.temp.name))
        # Use the real room policy with synthetic fixtures, no providers or tokens.
        self.app.config.channel_mode = lambda channel: RuntimeConfig.channel_mode(
            self.app.config, channel
        )
        self.app.config.ignored_channel_ids = (46,)
        self.app.config.social_channel_ids = (44,)
        self.app.config.addressed_channel_ids = (45,)
        self.app.config.automatic_response_channel_ids = frozenset({11, 22})
        self.app.catalog = {}
        self.client = discord.Client(intents=discord.Intents.none())
        self.addAsyncCleanup(self.client.close)
        self.tree = _CompanionCommandTree(self.client, self.app)
        register_commands(self.tree, self.app, SimpleNamespace())

    def interaction(self, channel_id, user_id=PARTNER_ID, *, dm=False):
        channel = SimpleNamespace(id=channel_id, name="synthetic-room", send=AsyncMock())
        if channel_id in (22, 47):
            # An explicitly private thread is allowed; an unlisted child of a
            # private channel must not inherit that channel's authorization.
            channel = MagicMock(spec=discord.Thread)
            channel.id = channel_id
            channel.name = "synthetic-thread"
            channel.parent_id = 11
        return SimpleNamespace(
            id=12345,
            guild_id=None if dm else 1,
            context=app_commands.AppCommandContext(dm_channel=dm, guild=not dm),
            guild=None if dm else SimpleNamespace(id=1),
            channel_id=channel_id,
            channel=channel,
            user=SimpleNamespace(id=user_id, name="Synthetic", display_name="Synthetic"),
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

    async def invoke(self, name, interaction, *args):
        # Exercise the tree authorization followed by its registered callback.
        if await self.tree.interaction_check(interaction):
            await self.tree.get_command(name).callback(interaction, *args)

    def guildless_interaction(self, name, argument, *, context, channel_type, channel_id=48):
        # Parse actual Discord payloads: DMChannel alone cannot distinguish a
        # bot DM from a user-installed command in a DM with someone else.
        user = {"id": str(PARTNER_ID), "username": "SyntheticPartner",
                "discriminator": "0", "avatar": None}
        other = {"id": "99", "username": "SyntheticOther",
                 "discriminator": "0", "avatar": None}
        bot = {"id": "400", "username": "SyntheticBot",
               "discriminator": "0", "avatar": None, "bot": True}
        self.client._connection.user = discord.ClientUser(
            state=self.client._connection, data=bot
        )
        self.client.dispatch = MagicMock()
        raw = {
            "id": "123456789012345678", "application_id": "400",
            "token": "synthetic-not-a-token", "type": 2, "version": 1,
            "attachment_size_limit": 1000000,
            "authorizing_integration_owners": {"1": str(PARTNER_ID)},
            "user": user,
            "channel": {
                "id": str(channel_id), "type": channel_type,
                "name": "synthetic-destination", "owner_id": str(PARTNER_ID),
                "recipients": [user, other] if channel_type == 3 else
                              [bot if context == 1 else other],
            },
            "data": {"name": name, "type": 1, "options": [
                {"name": {"recall": "query", "reflect": "topic", "export": "data"}[name],
                 "type": 3, "value": argument},
            ]},
        }
        if context is not None:
            raw["context"] = context
        interaction = discord.Interaction(data=raw, state=self.client._connection)
        interaction._cs_response = SimpleNamespace(
            defer=AsyncMock(), send_message=AsyncMock()
        )
        interaction._cs_followup = SimpleNamespace(send=AsyncMock())
        return interaction

    async def test_shared_and_unknown_guildless_contexts_deny_before_access(self):
        requests = [("recall", "synthetic")]
        requests.extend(("reflect", topic) for topic in
                        ("facts", "journal", "moments", "memories", "docs"))
        original_files = {
            p.name: p.read_bytes() for p in Path(self.temp.name).iterdir()
        }
        with ExitStack() as stack:
            channel_send = stack.enter_context(patch.object(
                discord.abc.Messageable, "send", new_callable=AsyncMock
            ))
            for owner, attribute in (
                (self.app, "recall_command"), (self.app, "list_memories"),
                (self.app, "respond"), (self.app.facts, "format"),
                (self.app.journal, "read_tail"), (self.app.moments, "read_tail"),
                (self.app.persona, "documents_by_mode"),
            ):
                stack.enter_context(patch.object(
                    owner, attribute, side_effect=AssertionError("private access before denial")
                ))
            for context, channel_type in ((2, 3), (2, 1), (None, 1), (99, 1), (0, 1)):
                # An allowlisted ID must not bypass the guild-less context gate.
                for channel_id in (48, 11):
                    for name, argument in requests:
                        with self.subTest(context=context, channel_type=channel_type,
                                          channel=channel_id, command=name, argument=argument):
                            interaction = self.guildless_interaction(
                                name, argument, context=context,
                                channel_type=channel_type, channel_id=channel_id,
                            )
                            self.assertIsInstance(interaction.channel,
                                discord.GroupChannel if channel_type == 3 else discord.DMChannel)
                            await self.tree._call(interaction)
                            interaction.response.send_message.assert_awaited_once()
                            self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])
                            interaction.response.defer.assert_not_awaited()
                            interaction.followup.send.assert_not_awaited()
            channel_send.assert_not_awaited()
        self.assertEqual(self.app.models.requests, [])
        self.assertEqual(self.app._cached_recall, {})
        self.assertEqual(original_files, {
            p.name: p.read_bytes() for p in Path(self.temp.name).iterdir()
        })

    async def test_real_bot_dm_dispatch_preserves_private_context_and_split_replies(self):
        await self.app.memory.save(self.app.scope("11", PARTNER_ID), MemoryRecord(
            summary="SYNTHETIC_PRIVATE_MEMORY", memory_id="private"
        ))
        reply = "Synthetic private response. " * 200
        self.app.models.response_text = reply
        for name, argument in (("reflect", "journal"), ("recall", "synthetic")):
            with self.subTest(command=name):
                interaction = self.guildless_interaction(
                    name, argument, context=1, channel_type=1
                )
                with patch.object(discord.abc.Messageable, "send", new_callable=AsyncMock) as send:
                    await self.tree._call(interaction)
                interaction.response.send_message.assert_not_awaited()
                tier, request = self.app.models.requests[-1]
                self.assertEqual(tier, "primary")
                if name == "reflect":
                    self.assertIn("Private journal line.", request.messages[-1].content)
                    sends = interaction.followup.send.await_args_list
                else:
                    self.assertIn("SYNTHETIC_PRIVATE_MEMORY", request.system)
                    self.assertTrue(interaction.followup.send.await_args.kwargs["ephemeral"])
                    sends = send.await_args_list
                self.assertGreater(len(sends), 1)
                self.assertEqual("".join(call.args[0] for call in sends), reply)
                self.assertTrue(all(len(call.args[0]) <= 2000 for call in sends))
                for entry in self.app.history.get("48")[-2:]:
                    self.assertEqual(entry["provenance"]["surface"], "dm")
                    self.assertEqual(entry["provenance"]["disclosure_scope"], "private")

    async def test_user_installed_shared_dm_exports_remain_ephemeral(self):
        export = Path(self.temp.name) / "synthetic-export.json"
        export.write_text('{"synthetic":"private export"}')
        for channel_type in (1, 3):
            with self.subTest(channel_type=channel_type):
                interaction = self.guildless_interaction(
                    "export", "facts", context=2, channel_type=channel_type
                )
                with patch.object(self.app, "export_paths", return_value={"facts": export}), \
                     patch.object(discord.abc.Messageable, "send", new_callable=AsyncMock) as send:
                    await self.tree._call(interaction)
                self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])
                sent = interaction.followup.send.await_args.kwargs
                try:
                    self.assertTrue(sent["ephemeral"])
                    self.assertIn(b"private export", sent["file"].fp.read())
                finally:
                    sent["file"].close()
                send.assert_not_awaited()

    async def test_denied_cognition_commands_never_read_write_or_call_model(self):
        requests = [("recall", "synthetic")]
        requests.extend(
            ("reflect", app_commands.Choice(name=topic, value=topic))
            for topic in ("facts", "journal", "moments", "memories", "docs")
        )
        original_files = {
            p.name: p.read_bytes() for p in Path(self.temp.name).iterdir()
        }
        with ExitStack() as stack:
            for owner, attribute in (
                (self.app, "recall_command"), (self.app, "list_memories"),
                (self.app, "respond"), (self.app.facts, "format"),
                (self.app.journal, "read_tail"), (self.app.moments, "read_tail"),
                (self.app.persona, "documents_by_mode"),
            ):
                stack.enter_context(patch.object(
                    owner, attribute, side_effect=AssertionError("private access before denial")
                ))
            for partner_id, actor in ((PARTNER_ID, PARTNER_ID), (PARTNER_ID, 99), (0, 99)):
                self.app.config.partner_user_id = partner_id
                for channel_id, dm in ((11, False), (22, False), (44, False),
                                       (45, False), (46, False), (47, False), (48, True)):
                    if partner_id == actor and (dm or channel_id in (11, 22)):
                        continue
                    for name, argument in requests:
                        with self.subTest(partner=partner_id, actor=actor, room=channel_id, command=name):
                            interaction = self.interaction(channel_id, actor, dm=dm)
                            await self.invoke(name, interaction, argument)
                            interaction.response.send_message.assert_awaited_once()
                            self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])
                            interaction.response.defer.assert_not_awaited()
                            interaction.followup.send.assert_not_awaited()
                            interaction.channel.send.assert_not_awaited()
        self.assertEqual(self.app.models.requests, [])
        self.assertEqual(self.app._cached_recall, {})
        self.assertEqual(original_files, {
            p.name: p.read_bytes() for p in Path(self.temp.name).iterdir()
        })

    async def test_private_reflect_and_recall_preserve_context_and_all_reply_chunks(self):
        record = MemoryRecord(summary="SYNTHETIC_PRIVATE_MEMORY", memory_id="private")
        await self.app.memory.save(self.app.scope("11", PARTNER_ID), record)
        for channel_id, dm in ((11, False), (22, False), (48, True)):
            for name in ("reflect", "recall"):
                for reply in ("Synthetic short reply", "Synthetic long reply. " * 200):
                    with self.subTest(room=channel_id, command=name, length=len(reply)):
                        self.app.models.response_text = reply
                        interaction = self.interaction(channel_id, dm=dm)
                        argument = (
                            app_commands.Choice(name="journal", value="journal")
                            if name == "reflect" else "synthetic"
                        )
                        await self.invoke(name, interaction, argument)
                        tier, request = self.app.models.requests[-1]
                        self.assertEqual(tier, "primary")
                        self.assertIn("Private journal line.", request.system)
                        self.assertTrue(request.tools)
                        if name == "reflect":
                            self.assertIn("Private journal line.", request.messages[-1].content)
                            sends = interaction.followup.send.await_args_list
                        else:
                            self.assertIn("SYNTHETIC_PRIVATE_MEMORY", request.system)
                            self.assertTrue(interaction.response.defer.await_args.kwargs["ephemeral"])
                            self.assertTrue(interaction.followup.send.await_args.kwargs["ephemeral"])
                            sends = interaction.channel.send.await_args_list
                        self.assertEqual("".join(call.args[0] for call in sends), reply)
                        self.assertTrue(all(len(call.args[0]) <= 2000 for call in sends))
                        stored = self.app.history.get(str(channel_id))[-2:]
                        self.assertEqual(len(stored), 2)
                        self.assertTrue(all(item["provenance"]["disclosure_scope"] == "private" for item in stored))

    async def test_guest_and_unconfigured_exports_and_mutations_stop_before_access(self):
        for partner_id in (0, PARTNER_ID):
            self.app.config.partner_user_id = partner_id
            with patch.object(self.app, "export_paths", side_effect=AssertionError("export access")), \
                 patch.object(self.app.history, "clear", side_effect=AssertionError("history mutation")):
                for name in ("export", "clear"):
                    interaction = self.interaction(44, 99)
                    arguments = (app_commands.Choice(name="facts", value="facts"),) if name == "export" else ()
                    await self.invoke(name, interaction, *arguments)
                    self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])
                    interaction.followup.send.assert_not_awaited()

    async def test_partner_can_export_ephemerally_from_shared_room(self):
        interaction = self.interaction(44)
        export = Path(self.temp.name) / "synthetic-export.json"
        export.write_text('{"synthetic":"private export"}')
        with patch.object(self.app, "export_paths", return_value={"facts": export}):
            await self.invoke("export", interaction, app_commands.Choice(name="facts", value="facts"))
        self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])
        sent = interaction.followup.send.await_args.kwargs
        try:
            self.assertTrue(sent["ephemeral"])
            self.assertIn(b"private export", sent["file"].fp.read())
        finally:
            sent["file"].close()
        interaction.channel.send.assert_not_awaited()

    async def test_guild_id_controls_destination_and_provenance_when_cache_missing(self):
        interaction = self.interaction(44)
        interaction.guild = None
        with patch.object(self.app, "recall_command", side_effect=AssertionError("private access")):
            await self.invoke("recall", interaction, "synthetic")
        self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])
        interaction.channel.send.assert_not_awaited()
        private = self.interaction(11)
        private.guild = None
        await self.invoke("reflect", private, app_commands.Choice(name="journal", value="journal"))
        stored = self.app.history.get("11")[-2]["provenance"]
        self.assertEqual(stored["guild_id"], "1")
        self.assertEqual(stored["surface"], "text")


if __name__ == "__main__":
    unittest.main()
