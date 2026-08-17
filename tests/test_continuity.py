"""Cross-surface provenance and continuity isolation regressions."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from disco_proxy_soul.app import CompanionApp
from disco_proxy_soul.memory.contracts import MemoryRecord, Scope, TurnProvenance
from disco_proxy_soul.memory.facts import FactStore
from disco_proxy_soul.memory.file_backend import FileMemoryBackend
from disco_proxy_soul.memory.history import ConversationStore
from disco_proxy_soul.memory.journal import MarkdownLog
from disco_proxy_soul.models.contracts import ModelResponse


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


class RecordingModels:
    def __init__(self) -> None:
        self.requests = []

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
        return ModelResponse(text="I remember the thread.", provider="fake", model="fake")


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
        return app

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


if __name__ == "__main__":
    unittest.main()
