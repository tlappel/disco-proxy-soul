"""The optional connected-runtime doorway stays neutral and bounded."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from disco_proxy_soul.discord_app.bot import _runtime_turn, build_bot
from disco_proxy_soul.discord_app.social_presence import SocialMessage, SocialRoute
from disco_proxy_soul.resident_runtime import (
    EverthreadRuntimeAdapter,
    RuntimeDeliveryPreparation,
    RuntimeOutcome,
    RuntimeSource,
    RuntimeTurn,
)

NOW = datetime(2026, 8, 23, 18, 0, tzinfo=UTC)


def test_public_runtime_turn_keeps_exact_ordered_sources_and_neutral_labels() -> None:
    app = SimpleNamespace(
        persona=SimpleNamespace(persona_id="naomi", companion_name="Naomi")
    )
    message = SimpleNamespace(
        id=30,
        created_at=NOW + timedelta(seconds=2),
        channel=SimpleNamespace(id=20, name="synthetic-public-room"),
        author=SimpleNamespace(id=300, display_name="Hannah"),
    )
    route = SocialRoute(
        trigger="social-attention",
        discretionary=True,
        source_messages=(
            SocialMessage(
                guild_id="10",
                channel_id="20",
                channel_name="synthetic-public-room",
                message_id="28",
                author_id="100",
                author_name="Travis",
                author_kind="human",
                content="Hannah, what makes dark wave work?",
                occurred_at=NOW,
            ),
            SocialMessage(
                guild_id="10",
                channel_id="20",
                channel_name="synthetic-public-room",
                message_id="29",
                author_id="200",
                author_name="Morgan",
                author_kind="human",
                content="I want to hear this too.",
                occurred_at=NOW + timedelta(seconds=1),
            ),
        ),
    )

    turn = _runtime_turn(
        app,
        message,
        route,
        canonical_text="Dark wave makes melancholy feel architectural.",
        surface="text",
        disclosure_scope="public",
    )

    assert turn.resident_id == "naomi"
    assert turn.response_source_id == "30"
    assert [source.source_id for source in turn.sources] == ["28", "29", "30"]
    assert [source.actor_label for source in turn.sources] == [
        "Travis",
        "Morgan",
        "Hannah",
    ]
    assert [source.source_group_position for source in turn.sources] == [0, 1, 2]
    assert {source.source_group_id for source in turn.sources} == {"discord-turn:30"}
    assert turn.sources[-1].text == "Dark wave makes melancholy feel architectural."
    assert "Hannah:" not in turn.sources[-1].text


def test_private_runtime_turn_admits_only_current_source() -> None:
    app = SimpleNamespace(
        persona=SimpleNamespace(persona_id="naomi", companion_name="Naomi")
    )
    message = SimpleNamespace(
        id=31,
        created_at=NOW,
        channel=SimpleNamespace(id=21, name="naomi-home"),
        author=SimpleNamespace(id=100, display_name="Travis"),
    )
    turn = _runtime_turn(
        app,
        message,
        SocialRoute(trigger="addressed"),
        canonical_text="Hannah nailed that dark wave description.",
        surface="text",
        disclosure_scope="private",
    )

    assert len(turn.sources) == 1
    assert turn.sources[0].source_group_id is None
    assert turn.sources[0].actor_label == "Travis"


def test_real_everthread_adapter_round_trip_is_one_writer_and_delivery_gated(
    tmp_path,
) -> None:
    pytest.importorskip("everthread")
    from everthread.application.connected_runtime import ConnectedResidentRuntime
    from everthread.application.conversation import (
        ConversationService,
        ConversationSourceRecorder,
    )
    from everthread.application.delivery import DeliveryService
    from everthread.application.recall import ConversationRecallDeriver
    from everthread.persistence import SQLiteEventArchive
    from everthread.vessels import VesselResponse

    class Vessel:
        vessel_id = "synthetic-disco-bridge"

        def generate(self, turn):
            return VesselResponse("The Disco door reached Everthread exactly once.")

    database = tmp_path / "disco-everthread.sqlite3"

    def factory():
        archive = SQLiteEventArchive(database)
        runtime = ConnectedResidentRuntime(
            archive,
            ConversationSourceRecorder(archive),
            ConversationService(archive, Vessel()),
            DeliveryService(archive),
            ConversationRecallDeriver(archive, archive),
        )
        return runtime, archive.close

    async def scenario() -> None:
        adapter = EverthreadRuntimeAdapter(factory)
        turn = RuntimeTurn(
            resident_id="naomi",
            person_label="Hannah",
            resident_label="Naomi",
            sources=(
                RuntimeSource(
                    source_id="topic",
                    text="What makes dark wave work?",
                    occurred_at=NOW,
                    conversation_id="discord-channel:synthetic",
                    conversation_label="Synthetic public room",
                    surface="discord.text",
                    actor_id="person:travis",
                    actor_label="Travis",
                    disclosure_scope="public",
                    interaction_mode="text",
                    source_group_id="discord-turn:answer",
                    source_group_position=0,
                ),
                RuntimeSource(
                    source_id="answer",
                    text="It makes melancholy feel architectural.",
                    occurred_at=NOW + timedelta(seconds=1),
                    conversation_id="discord-channel:synthetic",
                    conversation_label="Synthetic public room",
                    surface="discord.text",
                    actor_id="resident:hannah",
                    actor_label="Hannah",
                    disclosure_scope="public",
                    interaction_mode="text",
                    source_group_id="discord-turn:answer",
                    source_group_position=1,
                ),
            ),
            response_source_id="answer",
        )
        outcome = await adapter.complete(turn)
        prepared = await adapter.prepare_delivery(
            resident_id="naomi",
            outcome_id=outcome.outcome_id,
            logical_delivery_id="discord-message:answer",
            target="discord.channel:synthetic",
        )
        assert prepared.disposition == "send"
        assert prepared.attempt_id is not None
        await adapter.record_delivery_result(
            resident_id="naomi",
            attempt_id=prepared.attempt_id,
            status="confirmed",
            external_ids=("discord-message:resident-answer",),
        )
        await adapter.close()

    asyncio.run(scenario())

    with SQLiteEventArchive(database) as archive:
        recall = archive.list_source_recall("naomi")
    assert len(recall) == 1
    assert [part.actor_label for part in recall[0].content] == [
        "Travis",
        "Hannah",
        "Naomi",
    ]


def test_discord_text_path_uses_injected_runtime_and_reports_delivery() -> None:
    class Runtime:
        def __init__(self) -> None:
            self.turns = []
            self.results = []
            self.closed = False

        async def start(self) -> None:
            pass

        async def complete(self, turn):
            self.turns.append(turn)
            return RuntimeOutcome("outcome-1", "One accepted answer.")

        async def prepare_delivery(self, **values):
            self.prepared = values
            return RuntimeDeliveryPreparation("send", "attempt-1")

        async def record_delivery_result(self, **values):
            self.results.append(values)

        async def close(self) -> None:
            self.closed = True

    class Typing:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

    class Channel:
        id = 20
        name = "naomi-home"

        def typing(self):
            return Typing()

        async def send(self, content):
            return SimpleNamespace(id=902, content=content)

    class Message:
        id = 30
        content = "Exact private words."
        created_at = NOW
        guild = None
        reference = None
        mentions = ()
        attachments = ()
        channel = Channel()
        author = SimpleNamespace(id=100, bot=False, display_name="Travis")

        async def reply(self, content):
            self.reply_text = content
            return SimpleNamespace(id=901, content=content)

    config = MagicMock()
    config.partner_user_id = 100
    config.automatic_response_channel_ids = frozenset()
    config.social_resident_user_ids = ()
    config.social_ambient_enabled = False
    config.social_channel_ids = ()
    config.ollama_base_url = "http://127.0.0.1:11434"
    config.social_attention_model = "qwen3:4b"
    config.social_attention_timeout_seconds = 30.0
    config.social_attention_threads = 4
    config.social_attention_context_tokens = 2048
    config.social_attention_keep_alive = "-1"
    config.social_debounce_seconds = 3.0
    config.social_buffer_messages = 12
    config.social_buffer_chars = 4000
    config.social_engagement_seconds = 120.0
    config.social_cooldown_seconds = 30.0
    config.social_budget_capacity = 6.0
    config.social_budget_refill_per_hour = 2.0
    config.social_direct_burst = 3
    config.social_direct_refill_per_minute = 2.0
    config.social_ai_chain_limit = 4
    app = MagicMock()
    app.config = config
    app.persona = SimpleNamespace(
        persona_id="naomi",
        companion_name="Naomi",
        partner_name="Travis",
        social_posture=None,
    )
    runtime = Runtime()

    async def scenario() -> None:
        client = build_bot(app, resident_runtime=runtime)
        client._connection.user = SimpleNamespace(id=400)
        incoming = Message()
        try:
            await client.on_message(incoming)
            assert incoming.reply_text == "One accepted answer."
            assert app.respond.call_count == 0
            assert len(runtime.turns) == 1
            assert runtime.prepared["outcome_id"] == "outcome-1"
            assert runtime.results[0]["status"] == "confirmed"
            assert runtime.results[0]["external_ids"] == ("901",)
        finally:
            await client.close()
        assert runtime.closed is True

    asyncio.run(scenario())
