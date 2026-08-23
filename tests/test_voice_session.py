"""Network-free tests for the Phase 2 live voice transport."""

from __future__ import annotations

import asyncio
from array import array
import io
import logging
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import traceback
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from discord import app_commands
from discord.ext.voice_recv.router import MultiDataEvent, PacketDecoder

from disco_proxy_soul.adapters.gladia_live import CompletionState, TranscriptUpdate
from disco_proxy_soul.config import RuntimeConfig
from disco_proxy_soul.discord_app.commands import register_commands
from disco_proxy_soul.discord_app.bot import build_bot, configure_application_logging
from disco_proxy_soul.discord_app import voice_session as voice_session_module
from disco_proxy_soul.discord_app.voice_session import (
    FinalTurnCoordinator,
    FRAME_SAMPLES,
    MONO_FRAME_BYTES,
    MonoRtpClock,
    SpeechEvidenceTimeline,
    VoiceSession,
    VoiceSessionCounters,
    VoiceSessionError,
    VoiceSessionManager,
    VoiceSessionState,
    is_intentional_barge_in,
)
from disco_proxy_soul.discord_app.voice_sink import LivePCMFrame, LivePCMSink


def config(**changes):
    values = {
        "voice_enabled": True,
        "gladia_api_key": "test-key",
        "voice_queue_seconds": 0.2,
        "voice_endpointing_seconds": 0.1,
        "voice_gladia_stop_seconds": 0.05,
        "voice_min_speech_ms": 120,
        "data_dir": Path("unused-test-data"),
        "ollama_base_url": "http://127.0.0.1:11434",
        "social_attention_model": "qwen3:4b",
        "social_attention_timeout_seconds": 30.0,
        "social_attention_threads": 4,
        "social_attention_context_tokens": 2048,
        "social_attention_keep_alive": "-1",
        "social_ambient_enabled": False,
        "social_debounce_seconds": 3.0,
        "social_buffer_messages": 12,
        "social_buffer_chars": 4000,
        "social_engagement_seconds": 120.0,
        "social_cooldown_seconds": 30.0,
        "social_budget_capacity": 6.0,
        "social_budget_refill_per_hour": 2.0,
        "social_direct_burst": 3,
        "social_direct_refill_per_minute": 2.0,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def stereo_frame(value: int = 1000) -> bytes:
    return array("h", [value, value] * FRAME_SAMPLES).tobytes()


def mono_frame(value: int = 1000) -> bytes:
    return array("h", [value] * FRAME_SAMPLES).tobytes()


def transcript(
    text: str,
    *,
    final: bool,
    utterance_id: str = "utterance",
    start: float = 0,
    end: float = 1,
    confidence: float = 0.9,
) -> TranscriptUpdate:
    return TranscriptUpdate(
        session_id="session",
        created_at="now",
        utterance_id=utterance_id,
        text=text,
        is_final=final,
        start=start,
        end=end,
        confidence=confidence,
        channel=0,
        words=(),
        language="en",
    )


class FakeTextChannel:
    def __init__(self, *, send_gate=None, send_error=None) -> None:
        self.messages: list[str] = []
        self.send_gate = send_gate
        self.send_error = send_error
        self.send_entered = asyncio.Event() if send_gate is not None else None

    async def send(self, content: str):
        if self.send_entered is not None:
            self.send_entered.set()
            await self.send_gate.wait()
        if self.send_error is not None:
            raise self.send_error
        self.messages.append(content)


class FakeCompanion:
    def __init__(self, *, gate=None, reply="Naomi heard you.") -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self.provenances = []
        self.history: list[tuple[str, str]] = []
        self.gate = gate
        self.entered = asyncio.Event() if gate is not None else None
        self.reply = reply
        self.persona = SimpleNamespace(companion_name="Naomi")

    async def respond(
        self, channel_id, user_text, *, interaction_mode=None, provenance=None
    ):
        self.calls.append((channel_id, user_text, interaction_mode))
        self.provenances.append(provenance)
        if self.entered is not None:
            self.entered.set()
            await self.gate.wait()
        self.history.extend((("user", user_text), ("assistant", self.reply)))
        return self.reply


class FakeTTS:
    def __init__(self, chunks=(b"pcm",)) -> None:
        self.chunks = tuple(chunks)
        self.texts = []

    async def stream_pcm(self, text):
        self.texts.append(text)
        for chunk in self.chunks:
            yield chunk


class FakePlayback:
    def __init__(self, *, capacity_frames=100) -> None:
        self.capacity_frames = capacity_frames
        self.calls = []

    async def play(self, voice_client, chunks, *, on_first_frame=None):
        rendered = [chunk async for chunk in chunks]
        self.calls.append((voice_client, rendered))
        if rendered and on_first_frame is not None:
            on_first_frame()


class BlockingPlayback(FakePlayback):
    def __init__(self, *, capacity_frames=100) -> None:
        super().__init__(capacity_frames=capacity_frames)
        self.release = asyncio.Event()
        self.entered = asyncio.Event()
        self.cancelled = 0
        self.active = 0
        self.max_active = 0

    async def play(self, voice_client, chunks, *, on_first_frame=None):
        rendered = [chunk async for chunk in chunks]
        self.calls.append((voice_client, rendered))
        if rendered and on_first_frame is not None:
            on_first_frame()
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.active -= 1


class FakeGladia:
    def __init__(
        self,
        log: list[str] | None = None,
        *,
        connect_error=None,
        connect_gate=None,
        send_error=None,
        stop_gate=None,
        stop_delay=0.0,
        suppress_stop_cancel=False,
        session_id=None,
    ) -> None:
        self.log = log if log is not None else []
        self.connect_error = connect_error
        self.connect_gate = connect_gate
        self.connect_entered = asyncio.Event() if connect_gate is not None else None
        self.send_error = send_error
        self.stop_gate = stop_gate
        self.stop_delay = stop_delay
        self.suppress_stop_cancel = suppress_stop_cancel
        self.stop_entered = asyncio.Event() if stop_gate is not None else None
        self.sent: list[bytes] = []
        self.events: asyncio.Queue[object] = asyncio.Queue()
        self.completion = CompletionState.PENDING
        self.session_id = session_id
        self.result = SimpleNamespace(
            completion_reason=None,
            reconnects=0,
            reconnect_failures=0,
            ambiguous_frames_dropped=0,
        )
        self.connected = False
        self.stop_calls = 0
        self.stop_timeouts: list[float] = []
        self.stop_cancel_seen = asyncio.Event()

    async def connect(self):
        self.log.append("gladia.connect")
        if self.connect_entered is not None:
            self.connect_entered.set()
            await self.connect_gate.wait()
        if self.connect_error:
            raise self.connect_error
        self.connected = True
        return self

    async def send_pcm(self, chunk: bytes):
        if self.send_error:
            raise self.send_error
        self.sent.append(chunk)

    async def iter_events(self):
        while True:
            event = await self.events.get()
            if event is None:
                return
            if isinstance(event, BaseException):
                raise event
            yield event

    async def stop(self, *, timeout: float = 60):
        self.log.append("gladia.stop")
        self.stop_calls += 1
        self.stop_timeouts.append(timeout)
        if self.stop_entered is not None:
            self.stop_entered.set()
            if self.suppress_stop_cancel:
                while not self.stop_gate.is_set():
                    try:
                        await self.stop_gate.wait()
                    except asyncio.CancelledError:
                        self.stop_cancel_seen.set()
            else:
                await self.stop_gate.wait()
        if self.stop_delay:
            await asyncio.sleep(self.stop_delay)
        self.completion = CompletionState.NORMAL
        await self.events.put(None)


class FakeVoiceClient:
    def __init__(
        self,
        channel,
        log: list[str],
        *,
        listen_error=None,
        is_listening_error=None,
        defer_after=False,
        disconnect_gate=None,
        disconnect_error=None,
        cleanup_error=None,
        move_gate=None,
        move_error=None,
        suppress_disconnect_cancel=False,
        disconnect_release=None,
        late_cleanup_on_disconnect=False,
        late_disconnect_error=None,
    ) -> None:
        self.channel = channel
        self.log = log
        self.listen_error = listen_error
        self.is_listening_error = is_listening_error
        self.defer_after = defer_after
        self.disconnect_gate = disconnect_gate
        self.disconnect_error = disconnect_error
        self.cleanup_error = cleanup_error
        self.move_gate = move_gate
        self.move_error = move_error
        self.suppress_disconnect_cancel = suppress_disconnect_cancel
        self.disconnect_release = disconnect_release
        self.late_cleanup_on_disconnect = late_cleanup_on_disconnect
        self.late_disconnect_error = late_disconnect_error
        self.disconnect_cancel_seen = asyncio.Event()
        self.disconnect_entered = (
            asyncio.Event() if disconnect_gate is not None else None
        )
        self.move_entered = asyncio.Event() if move_gate is not None else None
        self._listening = False
        self.after = None
        self.sink = None
        self.disconnect_calls = 0
        self.cleanup_calls = 0
        self.cache_owned = True

    def is_listening(self):
        if self.is_listening_error:
            raise self.is_listening_error
        return self._listening

    def listen(self, sink, *, after=None):
        self.log.append("discord.listen")
        if self.listen_error:
            raise self.listen_error
        self.sink = sink
        self.after = after
        self._listening = True

    def stop_listening(self):
        self.log.append("discord.stop_listening")
        self._listening = False
        if self.after and not self.defer_after:
            self.after(None)
        if self.sink:
            self.sink.cleanup()

    def fail_receiver(self, error):
        self.log.append("discord.receiver_failed")
        self._listening = False
        if self.after:
            self.after(error)

    async def disconnect(self, *, force=False):
        self.log.append("discord.disconnect")
        self.disconnect_calls += 1
        if self.suppress_disconnect_cancel:
            while not self.disconnect_release.is_set():
                try:
                    await self.disconnect_release.wait()
                except asyncio.CancelledError:
                    self.disconnect_cancel_seen.set()
            if self.late_cleanup_on_disconnect:
                self.cleanup()
            if self.late_disconnect_error is not None:
                raise self.late_disconnect_error
            return
        if self.disconnect_entered is not None:
            self.disconnect_entered.set()
            await self.disconnect_gate.wait()
        if self.disconnect_error:
            raise self.disconnect_error
        self.cleanup()

    def cleanup(self):
        self.log.append("discord.cleanup")
        self.cleanup_calls += 1
        if self.cleanup_error:
            raise self.cleanup_error
        self.cache_owned = False
        cache = getattr(self.channel, "voice_cache", None)
        if cache is not None:
            cache.pop(getattr(self.channel, "id", None), None)
        guild = getattr(self.channel, "guild", None)
        if guild is not None and guild.voice_client is self:
            guild.voice_client = None

    async def move_to(self, channel):
        if self.move_entered is not None:
            self.move_entered.set()
            await self.move_gate.wait()
        if self.move_error:
            raise self.move_error
        self.channel = channel


class FakeVoiceChannel:
    def __init__(
        self,
        channel_id: int = 22,
        *,
        listen_error=None,
        is_listening_error=None,
        defer_after=False,
        disconnect_gate=None,
        disconnect_error=None,
        cleanup_error=None,
        move_gate=None,
        move_error=None,
        connect_gate=None,
        connect_error=None,
        cache_before_handshake=False,
        suppress_disconnect_cancel=False,
        disconnect_release=None,
        late_cleanup_on_disconnect=False,
        late_disconnect_error=None,
    ) -> None:
        self.id = channel_id
        self.log: list[str] = []
        self.client = FakeVoiceClient(
            self,
            self.log,
            listen_error=listen_error,
            is_listening_error=is_listening_error,
            defer_after=defer_after,
            disconnect_gate=disconnect_gate,
            disconnect_error=disconnect_error,
            cleanup_error=cleanup_error,
            move_gate=move_gate,
            move_error=move_error,
            suppress_disconnect_cancel=suppress_disconnect_cancel,
            disconnect_release=disconnect_release,
            late_cleanup_on_disconnect=late_cleanup_on_disconnect,
            late_disconnect_error=late_disconnect_error,
        )
        self.connect_kwargs = None
        self.connect_gate = connect_gate
        self.connect_error = connect_error
        self.cache_before_handshake = cache_before_handshake
        self.voice_cache = {}
        self.guild = SimpleNamespace(voice_client=None)
        self.connect_entered = asyncio.Event() if connect_gate is not None else None
        self.client.cache_owned = False

    async def connect(self, **kwargs):
        self.connect_kwargs = kwargs
        self.log.append("discord.connect")
        factory_owner = getattr(kwargs.get("cls"), "__self__", None)
        if factory_owner is not None:
            factory_owner._connect_candidate = self.client
        if self.cache_before_handshake:
            self.voice_cache[self.id] = self.client
            self.guild.voice_client = self.client
            self.client.cache_owned = True
        if self.connect_entered is not None:
            self.connect_entered.set()
            await self.connect_gate.wait()
        if self.connect_error:
            raise self.connect_error
        if not self.cache_before_handshake:
            self.voice_cache[self.id] = self.client
            self.guild.voice_client = self.client
        self.client.cache_owned = True
        return self.client


class SinkBridgeTests(unittest.TestCase):
    def test_thread_callback_only_copies_and_schedules_starter_pcm(self) -> None:
        calls = []

        class Loop:
            def call_soon_threadsafe(self, callback):
                calls.append((callback, threading.get_ident()))

        delivered = []
        sink = LivePCMSink(Loop(), 7, delivered.append, capacity_frames=4)
        source = bytearray(stereo_frame())
        data = SimpleNamespace(pcm=source, packet=SimpleNamespace(timestamp=123))

        worker = threading.Thread(
            target=sink.write,
            args=(SimpleNamespace(id=7, bot=False), data),
        )
        worker.start()
        worker.join()

        self.assertEqual(delivered, [])
        self.assertEqual(len(calls), 1)
        callback, _ = calls[0]
        source[0] = 0
        callback()
        self.assertEqual(len(delivered), 1)
        self.assertNotEqual(delivered[0].pcm[0], 0)

        sink.write(SimpleNamespace(id=8, bot=False), data)
        sink.write(SimpleNamespace(id=7, bot=True), data)
        sink.write(None, data)
        sink.write(SimpleNamespace(id=7, bot=False), SimpleNamespace(pcm=b"x"))
        self.assertEqual(len(calls), 1)

    def test_stalled_loop_stress_is_bounded_with_one_drain_callback(self) -> None:
        callbacks = []
        drops = []

        class StalledLoop:
            def call_soon_threadsafe(self, callback):
                callbacks.append(callback)

        delivered = []
        sink = LivePCMSink(
            StalledLoop(),
            7,
            delivered.append,
            capacity_frames=32,
            on_drops=drops.append,
        )
        data = SimpleNamespace(
            pcm=stereo_frame(), packet=SimpleNamespace(timestamp=123)
        )
        user = SimpleNamespace(id=7, bot=False)
        def hammer():
            for _ in range(2_500):
                sink.write(user, data)

        workers = [threading.Thread(target=hammer) for _ in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()

        self.assertEqual(sink.pending_frames, 32)
        self.assertEqual(sink.dropped_frames, 9_968)
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        self.assertEqual(len(delivered), 32)
        self.assertEqual(drops, [9_968])

    def test_drain_batches_yield_and_keep_one_continuation_under_refill(self) -> None:
        callbacks = []

        class ManualLoop:
            def call_soon_threadsafe(self, callback):
                callbacks.append(callback)

            def call_soon(self, callback):
                callbacks.append(callback)

        delivered = []
        sink = LivePCMSink(
            ManualLoop(),
            7,
            delivered.append,
            capacity_frames=64,
            drain_batch_frames=8,
        )
        data = SimpleNamespace(
            pcm=stereo_frame(), packet=SimpleNamespace(timestamp=123)
        )
        user = SimpleNamespace(id=7, bot=False)
        for _ in range(50_000):
            sink.write(user, data)

        self.assertEqual(sink.pending_frames, 64)
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(sink.dropped_frames, 49_936)

        # Refill while callbacks run: each turn delivers at most one batch and
        # leaves exactly one continuation, so the event loop gets a fair turn.
        for _ in range(20):
            callback = callbacks.pop(0)
            before = len(delivered)
            callback()
            self.assertLessEqual(len(delivered) - before, 8)
            self.assertLessEqual(sink.pending_frames, 64)
            self.assertLessEqual(len(callbacks), 1)
            for _ in range(100):
                sink.write(user, data)
            self.assertLessEqual(sink.pending_frames, 64)
            self.assertLessEqual(len(callbacks), 1)

        while callbacks:
            callback = callbacks.pop(0)
            before = len(delivered)
            callback()
            self.assertLessEqual(len(delivered) - before, 8)
            self.assertLessEqual(len(callbacks), 1)
        self.assertEqual(sink.pending_frames, 0)
        self.assertGreater(sink.dropped_frames, 49_936)
        self.assertFalse(sink.drain_scheduled)


class VoiceConfigTests(unittest.TestCase):
    def test_voice_defaults_and_canonical_environment_fields(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "GLADIA_API_KEY": "gladia-secret",
                "VOICE_ENABLED": "true",
                "VOICE_ENDPOINTING_SECONDS": "0.2",
                "VOICE_QUEUE_SECONDS": "3.5",
                "VOICE_GLADIA_STOP_SECONDS": "17.5",
                "VOICE_GLADIA_RECONNECT_ATTEMPTS": "4",
                "VOICE_GLADIA_RECONNECT_INITIAL_DELAY_SECONDS": "0.75",
                "VOICE_GLADIA_RECONNECT_MAX_DELAY_SECONDS": "6.0",
                "VOICE_GLADIA_RECONNECT_CONNECT_TIMEOUT_SECONDS": "8.0",
                "VOICE_GLADIA_ROTATE_SECONDS": "9000",
                "VOICE_MIN_SPEECH_MS": "200",
                "VOICE_TURN_DEBOUNCE_SECONDS": "0.8",
                "VOICE_TTS_ENABLED": "true",
                "ELEVENLABS_API_KEY": "eleven-secret",
                "ELEVENLABS_VOICE_ID": "naomi-voice",
                "ELEVENLABS_MODEL_ID": "eleven_flash_v2_5",
                "ELEVENLABS_STABILITY": "0.4",
                "ELEVENLABS_SIMILARITY_BOOST": "0.8",
                "ELEVENLABS_STYLE": "0.1",
                "ELEVENLABS_SPEAKER_BOOST": "true",
                "ELEVENLABS_SPEED": "1.1",
                "VOICE_PLAYBACK_QUEUE_SECONDS": "2.5",
                "VOICE_BARGE_IN_ENABLED": "true",
                "VOICE_BARGE_IN_MIN_SPEECH_MS": "240",
            },
            clear=True,
        ):
            loaded = RuntimeConfig.from_env()
        self.assertTrue(loaded.voice_enabled)
        self.assertEqual(loaded.gladia_api_key, "gladia-secret")
        self.assertEqual(loaded.voice_endpointing_seconds, 0.2)
        self.assertEqual(loaded.voice_queue_seconds, 3.5)
        self.assertEqual(loaded.voice_gladia_stop_seconds, 17.5)
        self.assertEqual(loaded.voice_gladia_reconnect_attempts, 4)
        self.assertEqual(
            loaded.voice_gladia_reconnect_initial_delay_seconds, 0.75
        )
        self.assertEqual(loaded.voice_gladia_reconnect_max_delay_seconds, 6.0)
        self.assertEqual(
            loaded.voice_gladia_reconnect_connect_timeout_seconds, 8.0
        )
        self.assertEqual(loaded.voice_gladia_rotate_seconds, 9000.0)
        self.assertEqual(loaded.voice_min_speech_ms, 200)
        self.assertEqual(loaded.voice_turn_debounce_seconds, 0.8)
        self.assertTrue(loaded.voice_tts_enabled)
        self.assertEqual(loaded.elevenlabs_api_key, "eleven-secret")
        self.assertEqual(loaded.elevenlabs_voice_id, "naomi-voice")
        self.assertEqual(loaded.elevenlabs_stability, 0.4)
        self.assertEqual(loaded.elevenlabs_similarity_boost, 0.8)
        self.assertEqual(loaded.elevenlabs_style, 0.1)
        self.assertTrue(loaded.elevenlabs_speaker_boost)
        self.assertEqual(loaded.elevenlabs_speed, 1.1)
        self.assertEqual(loaded.voice_playback_queue_seconds, 2.5)
        self.assertTrue(loaded.voice_barge_in_enabled)
        self.assertEqual(loaded.voice_barge_in_min_speech_ms, 240)

    def test_voice_config_ranges_are_validated_without_echoing_values(self) -> None:
        with patch.dict(
            "os.environ", {"VOICE_QUEUE_SECONDS": "999-secret"}, clear=True
        ):
            with self.assertRaises(ValueError) as caught:
                RuntimeConfig.from_env()
        self.assertNotIn("999-secret", str(caught.exception))

        with patch.dict(
            "os.environ", {"VOICE_GLADIA_STOP_SECONDS": "999-secret"}, clear=True
        ):
            with self.assertRaises(ValueError) as caught:
                RuntimeConfig.from_env()
        self.assertNotIn("999-secret", str(caught.exception))


class RtpClockTests(unittest.TestCase):
    class _Packet:
        def __init__(self, sequence: int, timestamp: int) -> None:
            self.ssrc = 42
            self.sequence = sequence
            self.timestamp = timestamp

        def __lt__(self, other) -> bool:
            return (
                self.sequence < other.sequence
                and self.timestamp < other.timestamp
            )

    def test_mono_frames_and_rtp_gap_are_exactly_twenty_ms(self) -> None:
        counters = VoiceSessionCounters()
        clock = MonoRtpClock(counters)
        clock.push(LivePCMFrame(stereo_frame(1200), 1000))
        first = clock.render()
        clock.push(LivePCMFrame(stereo_frame(2400), 1000 + FRAME_SAMPLES * 2))
        gap = clock.render()
        third = clock.render()

        self.assertEqual(len(first), MONO_FRAME_BYTES)
        self.assertEqual(array("h", first)[0], 1200)
        self.assertEqual(gap, bytes(MONO_FRAME_BYTES))
        self.assertEqual(array("h", third)[0], 2400)
        self.assertEqual(counters.rtp_gap_samples, FRAME_SAMPLES)

    def test_initial_timed_silence_does_not_discard_first_rtp_packet(self) -> None:
        counters = VoiceSessionCounters()
        clock = MonoRtpClock(counters)
        self.assertEqual(clock.render(), bytes(MONO_FRAME_BYTES))
        clock.push(LivePCMFrame(stereo_frame(321), 90_000))
        self.assertEqual(array("h", clock.render())[0], 321)

    def test_missing_timestamp_then_valid_uses_provisional_anchor_offset(self) -> None:
        counters = VoiceSessionCounters()
        clock = MonoRtpClock(counters)
        clock.push(LivePCMFrame(stereo_frame(111), None))
        clock.push(LivePCMFrame(stereo_frame(222), 50_000))
        self.assertEqual(array("h", clock.render())[0], 111)
        self.assertEqual(array("h", clock.render())[0], 222)
        self.assertEqual(counters.late_audio_samples, 0)

    def test_partial_late_and_overlap_are_counted(self) -> None:
        counters = VoiceSessionCounters()
        clock = MonoRtpClock(counters)
        clock.push(LivePCMFrame(stereo_frame(100), 1_000))
        clock.render()
        clock.push(LivePCMFrame(stereo_frame(200), 1_000 + FRAME_SAMPLES // 2))
        partial = array("h", clock.render())
        self.assertEqual(counters.late_audio_samples, FRAME_SAMPLES // 2)
        self.assertEqual(partial[0], 200)
        clock.push(LivePCMFrame(stereo_frame(300), 1_000 + FRAME_SAMPLES * 2))
        clock.push(LivePCMFrame(stereo_frame(400), 1_000 + FRAME_SAMPLES * 2))
        self.assertGreaterEqual(counters.overlap_samples, FRAME_SAMPLES)

    def test_out_of_order_packets_are_sorted(self) -> None:
        counters = VoiceSessionCounters()
        clock = MonoRtpClock(counters)
        clock.push(LivePCMFrame(stereo_frame(10), 10_000))
        clock.push(LivePCMFrame(stereo_frame(30), 10_000 + FRAME_SAMPLES * 2))
        clock.push(LivePCMFrame(stereo_frame(20), 10_000 + FRAME_SAMPLES))
        self.assertEqual([array("h", clock.render())[0] for _ in range(3)], [10, 20, 30])
        self.assertEqual(counters.out_of_order_packets, 1)

    def test_huge_jump_reanchors_and_wrap_is_contiguous(self) -> None:
        counters = VoiceSessionCounters()
        clock = MonoRtpClock(counters, max_credible_jump_samples=FRAME_SAMPLES * 4)
        clock.push(LivePCMFrame(stereo_frame(1), 100))
        self.assertEqual(array("h", clock.render())[0], 1)
        clock.push(LivePCMFrame(stereo_frame(2), 100 + FRAME_SAMPLES * 100))
        self.assertEqual(array("h", clock.render())[0], 2)
        self.assertEqual(counters.rtp_discontinuities, 1)
        clock.push(LivePCMFrame(stereo_frame(3), 100 + FRAME_SAMPLES * 2))
        self.assertEqual(array("h", clock.render())[0], 3)
        self.assertEqual(counters.rtp_discontinuities, 2)

        wrapped = MonoRtpClock(VoiceSessionCounters())
        wrapped.push(LivePCMFrame(stereo_frame(7), 2**32 - FRAME_SAMPLES))
        wrapped.push(LivePCMFrame(stereo_frame(8), 0))
        self.assertEqual([array("h", wrapped.render())[0] for _ in range(2)], [7, 8])

    def test_pending_clock_stress_is_bounded(self) -> None:
        counters = VoiceSessionCounters()
        clock = MonoRtpClock(counters, max_pending_packets=8)
        for index in range(1_000):
            clock.push(
                LivePCMFrame(stereo_frame(index % 100), 1_000 + index * FRAME_SAMPLES)
            )
        self.assertLessEqual(clock.pending_count, 8)
        self.assertGreaterEqual(counters.pending_drops, 992)
        self.assertGreaterEqual(counters.inserted_silence_samples, 0)

    def test_installed_jitter_gap_reanchors_playout_without_wholesale_loss(self) -> None:
        voice_session_module.install_voice_receive_compatibility()
        waiter = MultiDataEvent()
        router = SimpleNamespace(
            waiter=waiter,
            sink=SimpleNamespace(wants_opus=lambda: True),
        )
        decoder = PacketDecoder(router, 42)
        counters = VoiceSessionCounters()
        clock = MonoRtpClock(counters, max_pending_packets=32)
        produced = 0

        for sequence in range(1, 1_201):
            if sequence != 6:
                decoder.push_packet(
                    self._Packet(sequence, sequence * FRAME_SAMPLES)
                )
            if decoder in waiter.items:
                packet = decoder._get_next_packet(0)
                decoder._flag_ready_state()
                if packet is not None:
                    produced += 1
                    clock.push(
                        LivePCMFrame(
                            stereo_frame(sequence % 100 + 1),
                            packet.timestamp,
                        )
                    )
            clock.render()

        self.assertGreaterEqual(produced, 1_185)
        self.assertEqual(len(decoder._buffer._buffer), 9)
        self.assertGreaterEqual(
            getattr(router, "_dps_compat_forced_full_releases", 0), 1_180
        )
        self.assertLessEqual(counters.clock_dropped_packets, 2)
        self.assertLessEqual(counters.late_audio_samples, FRAME_SAMPLES * 2)
        self.assertGreaterEqual(counters.playout_reanchors, 1)

        drops = counters.clock_dropped_packets
        reanchors = counters.playout_reanchors
        clock.push(
            LivePCMFrame(stereo_frame(77), 1_000 * FRAME_SAMPLES)
        )
        self.assertEqual(counters.clock_dropped_packets, drops + 1)
        self.assertEqual(counters.playout_reanchors, reanchors)


class SpeechEvidenceTests(unittest.TestCase):
    def test_intentional_barge_in_requires_name_and_narrow_cue(self) -> None:
        self.assertTrue(is_intentional_barge_in("Naomi, wait", "Naomi"))
        self.assertTrue(is_intentional_barge_in("Hey Naomi, please hold on", "Naomi"))
        self.assertTrue(is_intentional_barge_in("Okay Naomi, can you stop?", "Naomi"))
        self.assertFalse(is_intentional_barge_in("wait", "Naomi"))
        self.assertFalse(is_intentional_barge_in("Naomi, I have a thought", "Naomi"))
        self.assertFalse(is_intentional_barge_in("Lila, wait", "Naomi"))

    def test_low_energy_artifact_is_rejected_but_short_speech_is_preserved(self) -> None:
        evidence = SpeechEvidenceTimeline()
        for _ in range(10):
            evidence.observe_sent_frame(mono_frame(0))
        artifact = transcript("Thank you.", final=True, start=0, end=0.2)
        self.assertFalse(evidence.corroborates(artifact, min_speech_ms=120))

        start = evidence.duration
        for _ in range(6):
            evidence.observe_sent_frame(mono_frame(1000))
        concise = transcript("Yes.", final=True, start=start, end=evidence.duration)
        self.assertTrue(evidence.corroborates(concise, min_speech_ms=120))

    def test_loud_high_frequency_artifact_is_not_speech_evidence(self) -> None:
        evidence = SpeechEvidenceTimeline()
        artifact_frame = array(
            "h", (1000 if index % 2 == 0 else -1000 for index in range(FRAME_SAMPLES))
        ).tobytes()
        for _ in range(10):
            evidence.observe_sent_frame(artifact_frame)
        artifact = transcript(
            "Cicada words", final=True, start=0, end=evidence.duration
        )
        self.assertFalse(evidence.corroborates(artifact, min_speech_ms=120))


class FinalTurnCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def make_evidence(self) -> tuple[SpeechEvidenceTimeline, float, float]:
        evidence = SpeechEvidenceTimeline()
        start = evidence.duration
        for _ in range(40):
            evidence.observe_sent_frame(mono_frame(1000))
        return evidence, start, evidence.duration

    async def test_split_finals_assemble_into_one_natural_turn(self) -> None:
        evidence, start, end = self.make_evidence()
        turns = []

        async def accept(text):
            turns.append(text)

        coordinator = FinalTurnCoordinator(
            evidence, accept, min_speech_ms=120, debounce_seconds=0.01
        )
        await coordinator.offer(
            transcript(
                "This is the repaired live voice",
                final=True,
                utterance_id="one",
                start=start,
                end=start + 0.4,
            )
        )
        await coordinator.offer(
            transcript(
                "connection.",
                final=True,
                utterance_id="two",
                start=start + 0.42,
                end=end,
            )
        )
        async with asyncio.timeout(0.5):
            while not turns:
                await asyncio.sleep(0)
        self.assertEqual(turns, ["This is the repaired live voice connection."])
        await coordinator.close()

    async def test_late_second_final_still_joins_before_debounce(self) -> None:
        evidence, start, end = self.make_evidence()
        turns = []

        async def accept(text):
            turns.append(text)

        coordinator = FinalTurnCoordinator(
            evidence, accept, min_speech_ms=120, debounce_seconds=0.05
        )
        await coordinator.offer(
            transcript(
                "This is the repaired live voice",
                final=True,
                utterance_id="late-one",
                start=start,
                end=start + 0.4,
            )
        )
        await asyncio.sleep(0.03)
        self.assertEqual(turns, [])
        await coordinator.offer(
            transcript(
                "connection.",
                final=True,
                utterance_id="late-two",
                start=start + 0.42,
                end=end,
            )
        )
        async with asyncio.timeout(0.5):
            while not turns:
                await asyncio.sleep(0)
        self.assertEqual(turns, ["This is the repaired live voice connection."])
        await coordinator.close()

    async def test_rtp_distorted_timestamps_do_not_override_arrival_debounce(self) -> None:
        evidence = SpeechEvidenceTimeline()
        for _ in range(150):
            evidence.observe_sent_frame(mono_frame(1000))
        turns = []

        async def accept(text):
            turns.append(text)

        coordinator = FinalTurnCoordinator(
            evidence, accept, min_speech_ms=120, debounce_seconds=0.01
        )
        await coordinator.offer(
            transcript(
                "This is the repaired live voice",
                final=True,
                utterance_id="rtp-one",
                start=0.1,
                end=0.5,
            )
        )
        await coordinator.offer(
            transcript(
                "connection.",
                final=True,
                utterance_id="rtp-two",
                start=2.2,
                end=2.5,
            )
        )
        async with asyncio.timeout(0.5):
            while not turns:
                await asyncio.sleep(0)
        self.assertEqual(turns, ["This is the repaired live voice connection."])
        await coordinator.close()

    async def test_duplicate_and_concurrent_finals_dispatch_once(self) -> None:
        evidence, start, end = self.make_evidence()
        turns = []

        async def accept(text):
            turns.append(text)

        coordinator = FinalTurnCoordinator(
            evidence, accept, min_speech_ms=120, debounce_seconds=0.01
        )
        duplicate = transcript(
            "Yes.", final=True, utterance_id="same", start=start, end=end
        )
        accepted = await asyncio.gather(
            coordinator.offer(duplicate), coordinator.offer(duplicate)
        )
        async with asyncio.timeout(0.5):
            while not turns:
                await asyncio.sleep(0)
        self.assertEqual(accepted.count(True), 1)
        self.assertEqual(turns, ["Yes."])
        await coordinator.close()

    async def test_new_final_does_not_cancel_inflight_cognition(self) -> None:
        evidence, start, end = self.make_evidence()
        gate = asyncio.Event()
        entered = asyncio.Event()
        turns = []

        async def accept(text):
            turns.append(text)
            if len(turns) == 1:
                entered.set()
                await gate.wait()

        coordinator = FinalTurnCoordinator(
            evidence, accept, min_speech_ms=120, debounce_seconds=0
        )
        await coordinator.offer(
            transcript(
                "First turn",
                final=True,
                utterance_id="first",
                start=start,
                end=start + 0.3,
            )
        )
        await entered.wait()
        await coordinator.offer(
            transcript(
                "Second turn",
                final=True,
                utterance_id="second",
                start=start + 0.4,
                end=end,
            )
        )
        await asyncio.sleep(0)
        self.assertEqual(turns, ["First turn"])
        gate.set()
        async with asyncio.timeout(0.5):
            while len(turns) < 2:
                await asyncio.sleep(0)
        self.assertEqual(turns, ["First turn", "Second turn"])
        await coordinator.close()


class VoiceSessionAsyncTests(unittest.IsolatedAsyncioTestCase):
    def make_session(
        self,
        *,
        gladia=None,
        gladia_sequence=None,
        channel=None,
        text_channel=None,
        app=None,
        session_config=None,
        tts=None,
        playback=None,
        pre_roll=0,
        turn_debounce=0,
        shutdown_step=0.05,
    ):
        channel = channel or FakeVoiceChannel()
        sequence = list(gladia_sequence or [])
        gladia = gladia or (sequence[0] if sequence else FakeGladia(channel.log))
        factory_calls = []

        def factory(*args, **kwargs):
            factory_calls.append((args, kwargs))
            if sequence:
                return sequence.pop(0)
            return gladia

        def tts_factory(*args, **kwargs):
            return tts or FakeTTS()

        def playback_factory(**kwargs):
            if playback is not None:
                return playback
            return FakePlayback(**kwargs)

        session = VoiceSession(
            guild_id=1,
            voice_channel=channel,
            text_channel=text_channel or FakeTextChannel(),
            starter_user_id=7,
            starter_name="Travis",
            config=session_config or config(),
            app=app,
            gladia_factory=factory,
            tts_factory=tts_factory,
            playback_factory=playback_factory,
            pre_roll_seconds=pre_roll,
            turn_debounce_seconds=turn_debounce,
            shutdown_step_seconds=shutdown_step,
        )
        return session, channel, gladia, factory_calls

    async def wait_for(self, predicate, *, timeout=0.5):
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0)
        await asyncio.sleep(0)

    def assert_owned_tasks_terminal(self, session):
        for name in (
            "_sender_task",
            "_consumer_task",
            "_rotation_task",
            "_reporter_task",
            "_failure_task",
            "_shutdown_task",
            "_connect_task",
            "_disconnect_task",
            "_gladia_stop_task",
        ):
            task = getattr(session, name)
            self.assertTrue(task is None or task.done(), f"{name} leaked")
        leaked = [
            task
            for task in asyncio.all_tasks()
            if not task.done()
            and f"g{session.guild_id}" in task.get_name()
            and task is not asyncio.current_task()
        ]
        self.assertEqual(leaked, [])

    async def test_queue_is_bounded_and_drops_are_visible(self) -> None:
        session, _, _, _ = self.make_session()
        session.state = VoiceSessionState.RUNNING
        frame = LivePCMFrame(stereo_frame(), 1)
        for _ in range(session.queue_capacity + 1):
            session.enqueue_from_loop(frame)
        self.assertEqual(session.queue.qsize(), session.queue_capacity)
        self.assertEqual(session.counters.queue_drops, 1)

    async def test_sender_drains_current_burst_before_each_render(self) -> None:
        session, _, gladia, _ = self.make_session(pre_roll=0.02)
        await session.start()
        session.enqueue_from_loop(LivePCMFrame(stereo_frame(10), 1_000))
        session.enqueue_from_loop(
            LivePCMFrame(stereo_frame(30), 1_000 + FRAME_SAMPLES * 2)
        )
        session.enqueue_from_loop(
            LivePCMFrame(stereo_frame(20), 1_000 + FRAME_SAMPLES)
        )
        await self.wait_for(lambda: len(gladia.sent) >= 2)
        self.assertEqual(
            [array("h", chunk)[0] for chunk in gladia.sent[:2]],
            [10, 20],
        )
        await session.stop()

    async def test_start_connects_receive_with_self_deaf_false_and_one_gladia(self) -> None:
        session, channel, gladia, calls = self.make_session()
        status = await session.start()
        self.assertEqual(status.state, VoiceSessionState.RUNNING)
        self.assertFalse(channel.connect_kwargs["self_deaf"])
        self.assertEqual(len(calls), 1)
        self.assertLess(channel.log.index("gladia.connect"), channel.log.index("discord.listen"))
        await asyncio.sleep(0.025)
        self.assertTrue(gladia.sent)
        self.assertEqual(gladia.sent[0], bytes(MONO_FRAME_BYTES))
        await session.stop()

    async def test_partials_never_trigger_cognition_and_final_has_exactly_one_history_effect(self) -> None:
        app = FakeCompanion()
        session, _, gladia, _ = self.make_session(app=app)
        await session.start()
        start = session._speech_evidence.duration
        for _ in range(10):
            session._speech_evidence.observe_sent_frame(mono_frame(1000))
        end = session._speech_evidence.duration
        await gladia.events.put(transcript("This is trash", final=False))
        await gladia.events.put(
            transcript("This is Travis", final=True, start=start, end=end)
        )
        await self.wait_for(lambda: len(app.calls) == 1)
        self.assertEqual(
            session.text_channel.messages,
            [
                "**Travis (voice transcript):** This is Travis",
                "Naomi heard you.",
            ],
        )
        self.assertEqual(app.calls, [("22", "[Travis]: This is Travis", "voice")])
        self.assertEqual(app.provenances[0].surface, "voice")
        self.assertEqual(app.provenances[0].author_id, "7")
        self.assertEqual(app.provenances[0].channel_id, "22")
        self.assertEqual(app.provenances[0].trigger, "live-voice")
        self.assertTrue(app.provenances[0].source_id.startswith("voice:"))
        self.assertEqual(
            app.history,
            [
                ("user", "[Travis]: This is Travis"),
                ("assistant", "Naomi heard you."),
            ],
        )
        await session.stop()

    async def test_artifact_and_duplicate_finals_do_not_reach_cognition(self) -> None:
        app = FakeCompanion()
        session, _, gladia, _ = self.make_session(app=app)
        await session.start()
        quiet_start = session._speech_evidence.duration
        for _ in range(10):
            session._speech_evidence.observe_sent_frame(mono_frame(0))
        quiet_end = session._speech_evidence.duration
        await gladia.events.put(
            transcript(
                "Thank you.",
                final=True,
                utterance_id="artifact",
                start=quiet_start,
                end=quiet_end,
            )
        )
        speech_start = session._speech_evidence.duration
        for _ in range(10):
            session._speech_evidence.observe_sent_frame(mono_frame(1000))
        speech_end = session._speech_evidence.duration
        accepted = transcript(
            "Yes.",
            final=True,
            utterance_id="real",
            start=speech_start,
            end=speech_end,
        )
        await gladia.events.put(accepted)
        await gladia.events.put(accepted)
        await self.wait_for(lambda: len(app.calls) == 1)
        self.assertEqual(app.calls[0][1], "[Travis]: Yes.")
        self.assertEqual(len(app.history), 2)
        self.assertEqual(session.counters.rejected_finals, 2)
        await session.stop()

    async def test_split_finals_make_one_cognition_call_and_one_exchange(self) -> None:
        app = FakeCompanion()
        session, _, gladia, _ = self.make_session(
            app=app, turn_debounce=0.01
        )
        await session.start()
        start = session._speech_evidence.duration
        for _ in range(30):
            session._speech_evidence.observe_sent_frame(mono_frame(1000))
        end = session._speech_evidence.duration
        await gladia.events.put(
            transcript(
                "This is the repaired live voice",
                final=True,
                utterance_id="split-one",
                start=start,
                end=start + 0.3,
            )
        )
        await gladia.events.put(
            transcript(
                "connection.",
                final=True,
                utterance_id="split-two",
                start=start + 0.32,
                end=end,
            )
        )
        await self.wait_for(lambda: len(app.calls) == 1)
        self.assertEqual(
            app.calls[0][1],
            "[Travis]: This is the repaired live voice connection.",
        )
        self.assertEqual(len(app.history), 2)
        self.assertEqual(session.counters.accepted_turns, 1)
        self.assertEqual(session.counters.companion_responses, 1)
        await session.stop()

    async def test_canonical_text_reply_is_spoken_once(self) -> None:
        app = FakeCompanion(reply="The same reply, exactly.")
        tts = FakeTTS(chunks=(b"one", b"two"))
        playback = FakePlayback()
        tts_config = config(
            voice_tts_enabled=True,
            elevenlabs_api_key="tts-key",
            elevenlabs_voice_id="voice-id",
            elevenlabs_model_id="eleven_flash_v2_5",
            elevenlabs_stability=0.5,
            elevenlabs_similarity_boost=0.75,
            elevenlabs_style=0.0,
            elevenlabs_speaker_boost=False,
            elevenlabs_speed=1.0,
            voice_playback_queue_seconds=2.0,
        )
        session, channel, gladia, _ = self.make_session(
            app=app,
            tts=tts,
            playback=playback,
            session_config=tts_config,
        )
        await session.start()
        start = session._speech_evidence.duration
        for _ in range(10):
            session._speech_evidence.observe_sent_frame(mono_frame(1000))
        await gladia.events.put(
            transcript(
                "Speak back",
                final=True,
                start=start,
                end=session._speech_evidence.duration - 0.04,
            )
        )
        await self.wait_for(lambda: len(playback.calls) == 1)
        await self.wait_for(
            lambda: session.counters.playback_start_latency.samples == 1
        )
        self.assertEqual(tts.texts, ["The same reply, exactly."])
        self.assertEqual(playback.calls, [(channel.client, [b"one", b"two"])])
        self.assertEqual(
            session.text_channel.messages,
            [
                "**Travis (voice transcript):** Speak back",
                "The same reply, exactly.",
            ],
        )
        self.assertEqual(session.counters.companion_responses, 1)
        self.assertEqual(session.counters.spoken_responses, 1)
        self.assertEqual(session.counters.stt_final_latency.samples, 1)
        self.assertGreaterEqual(session.counters.stt_final_latency.last_ms, 40.0)
        self.assertEqual(session.counters.cognition_latency.samples, 1)
        self.assertEqual(session.counters.tts_first_frame_latency.samples, 1)
        self.assertEqual(session.counters.playback_start_latency.samples, 1)
        await session.stop()

    async def test_planned_rotation_preserves_timeline_and_cognition(self) -> None:
        log: list[str] = []
        first = FakeGladia(log, session_id="gladia-one")
        second = FakeGladia(log, session_id="gladia-two")
        app = FakeCompanion(reply="Still with you.")
        session, _, _, factory_calls = self.make_session(
            gladia_sequence=[first, second],
            app=app,
        )
        await session.start()
        for _ in range(10):
            session._speech_evidence.observe_sent_frame(mono_frame(1000))

        await session._rotate_gladia()
        offset = session._gladia_audio_offset
        self.assertIs(session.gladia, second)
        self.assertEqual(session._gladia_session_ids, ["gladia-one", "gladia-two"])
        self.assertEqual(session.counters.gladia_rotations, 1)
        self.assertEqual(session.status().counters.gladia_sessions_started, 2)
        self.assertEqual(len(factory_calls), 2)
        self.assertLess(log.index("gladia.stop"), log.index("gladia.connect", 1))

        for _ in range(10):
            session._speech_evidence.observe_sent_frame(mono_frame(1000))
        await second.events.put(
            transcript(
                "Across the rotation",
                final=True,
                utterance_id="rotated-turn",
                start=0.0,
                end=0.2,
            )
        )
        await self.wait_for(lambda: len(app.calls) == 1)
        self.assertEqual(app.calls[0][1], "[Travis]: Across the rotation")
        self.assertGreater(offset, 0.0)
        await session.stop()

    async def test_rotation_timer_is_accelerated_without_wall_clock_hours(self) -> None:
        first = FakeGladia(session_id="timer-one")
        second = FakeGladia(session_id="timer-two")
        session, _, _, _ = self.make_session(
            gladia_sequence=[first, second],
            session_config=config(voice_gladia_rotate_seconds=0.02),
        )
        await session.start()
        await self.wait_for(
            lambda: session.counters.gladia_rotations == 1,
            timeout=0.5,
        )
        self.assertIs(session.gladia, second)
        await session.stop()
        self.assert_owned_tasks_terminal(session)

    async def test_multiple_rotations_preserve_ids_and_cumulative_health(self) -> None:
        first = FakeGladia(session_id="long-one")
        second = FakeGladia(session_id="long-two")
        third = FakeGladia(session_id="long-three")
        first.result.reconnects = 1
        first.result.ambiguous_frames_dropped = 1
        second.result.reconnect_failures = 2
        session, _, _, _ = self.make_session(
            gladia_sequence=[first, second, third],
        )
        await session.start()

        await session._rotate_gladia()
        await session._rotate_gladia()
        counters = session.status().counters
        self.assertEqual(
            session._gladia_session_ids,
            ["long-one", "long-two", "long-three"],
        )
        self.assertEqual(counters.gladia_rotations, 2)
        self.assertEqual(counters.gladia_sessions_started, 3)
        self.assertEqual(counters.gladia_reconnects, 1)
        self.assertEqual(counters.gladia_reconnect_failures, 2)
        self.assertEqual(counters.gladia_ambiguous_frames_dropped, 1)
        await session.stop()

    async def test_turn_during_playback_waits_without_overlapping_response(self) -> None:
        app = FakeCompanion(reply="One reply at a time.")
        playback = BlockingPlayback()
        tts_config = config(
            voice_tts_enabled=True,
            elevenlabs_api_key="tts-key",
            elevenlabs_voice_id="voice-id",
        )
        session, _, gladia, _ = self.make_session(
            app=app,
            playback=playback,
            session_config=tts_config,
        )
        await session.start()

        first_start = session._speech_evidence.duration
        for _ in range(10):
            session._speech_evidence.observe_sent_frame(mono_frame(1000))
        await gladia.events.put(
            transcript(
                "First turn",
                final=True,
                utterance_id="half-duplex-one",
                start=first_start,
                end=session._speech_evidence.duration,
            )
        )
        await playback.entered.wait()
        self.assertTrue(session.status().playback_active)

        second_start = session._speech_evidence.duration
        for _ in range(10):
            session._speech_evidence.observe_sent_frame(mono_frame(1000))
        await gladia.events.put(
            transcript(
                "Second turn",
                final=True,
                utterance_id="half-duplex-two",
                start=second_start,
                end=session._speech_evidence.duration,
            )
        )
        await self.wait_for(
            lambda: session.counters.finals_spoken_during_playback == 1
        )
        self.assertEqual(len(app.calls), 1)
        self.assertEqual(playback.max_active, 1)

        playback.release.set()
        await self.wait_for(lambda: len(app.calls) == 2)
        await self.wait_for(lambda: session.counters.spoken_responses == 2)
        self.assertEqual(playback.max_active, 1)
        self.assertFalse(session.status().playback_active)
        self.assertEqual(
            [call[1] for call in app.calls],
            ["[Travis]: First turn", "[Travis]: Second turn"],
        )
        self.assertEqual(len(app.history), 4)
        await session.stop()

    async def test_stop_cancels_playback_and_discards_queued_turn(self) -> None:
        app = FakeCompanion(reply="Current reply.")
        playback = BlockingPlayback()
        tts_config = config(
            voice_tts_enabled=True,
            elevenlabs_api_key="tts-key",
            elevenlabs_voice_id="voice-id",
        )
        session, _, gladia, _ = self.make_session(
            app=app,
            playback=playback,
            session_config=tts_config,
        )
        await session.start()

        first_start = session._speech_evidence.duration
        for _ in range(10):
            session._speech_evidence.observe_sent_frame(mono_frame(1000))
        await gladia.events.put(
            transcript(
                "Playing now",
                final=True,
                utterance_id="stop-playback-one",
                start=first_start,
                end=session._speech_evidence.duration,
            )
        )
        await playback.entered.wait()

        queued_start = session._speech_evidence.duration
        for _ in range(10):
            session._speech_evidence.observe_sent_frame(mono_frame(1000))
        await gladia.events.put(
            transcript(
                "Do not dispatch",
                final=True,
                utterance_id="stop-playback-two",
                start=queued_start,
                end=session._speech_evidence.duration,
            )
        )
        await self.wait_for(
            lambda: session.counters.finals_spoken_during_playback == 1
        )

        status = await session.stop()
        self.assertEqual(status.state, VoiceSessionState.STOPPED)
        self.assertFalse(status.playback_active)
        self.assertEqual(playback.cancelled, 1)
        self.assertEqual(len(app.calls), 1)
        self.assertEqual(len(app.history), 2)
        self.assertEqual(session.counters.spoken_responses, 0)

    async def test_delayed_final_correlates_to_completed_playback_window(self) -> None:
        app = FakeCompanion(reply="Clock-aligned reply.")
        playback = BlockingPlayback()
        tts_config = config(
            voice_tts_enabled=True,
            elevenlabs_api_key="tts-key",
            elevenlabs_voice_id="voice-id",
        )
        session, _, gladia, _ = self.make_session(
            app=app,
            playback=playback,
            session_config=tts_config,
        )
        await session.start()

        first_start = session._speech_evidence.duration
        for _ in range(10):
            session._speech_evidence.observe_sent_frame(mono_frame(1000))
        await gladia.events.put(
            transcript(
                "Begin playback",
                final=True,
                utterance_id="clock-window-one",
                start=first_start,
                end=session._speech_evidence.duration,
            )
        )
        await playback.entered.wait()

        overlap_start = session._speech_evidence.duration
        for _ in range(10):
            session._speech_evidence.observe_sent_frame(mono_frame(1000))
        overlap_end = session._speech_evidence.duration
        playback.release.set()
        await self.wait_for(lambda: session.counters.spoken_responses == 1)
        self.assertFalse(session.status().playback_active)

        await gladia.events.put(
            transcript(
                "Final arrived later",
                final=True,
                utterance_id="clock-window-two",
                start=overlap_start,
                end=overlap_end,
            )
        )
        await self.wait_for(
            lambda: session.counters.finals_spoken_during_playback == 1
        )
        await self.wait_for(lambda: len(app.calls) == 2)
        self.assertEqual(
            app.calls[1][1],
            "[Travis]: Final arrived later",
        )
        await session.stop()

    async def test_corroborated_named_partial_intentionally_interrupts_playback(self) -> None:
        app = FakeCompanion(reply="I heard the interruption.")
        playback = BlockingPlayback()
        tts_config = config(
            voice_tts_enabled=True,
            elevenlabs_api_key="tts-key",
            elevenlabs_voice_id="voice-id",
            voice_barge_in_enabled=True,
            voice_barge_in_min_speech_ms=120,
        )
        session, _, gladia, _ = self.make_session(
            app=app,
            playback=playback,
            session_config=tts_config,
        )
        await session.start()

        first_start = session._speech_evidence.duration
        for _ in range(10):
            session._speech_evidence.observe_sent_frame(mono_frame(1000))
        await gladia.events.put(
            transcript(
                "Give me a long answer",
                final=True,
                utterance_id="barge-first",
                start=first_start,
                end=session._speech_evidence.duration,
            )
        )
        await playback.entered.wait()

        cue_start = session._speech_evidence.duration
        for _ in range(10):
            session._speech_evidence.observe_sent_frame(mono_frame(1000))
        cue_end = session._speech_evidence.duration
        await gladia.events.put(
            transcript(
                "Naomi, wait",
                final=False,
                utterance_id="barge-cue",
                start=cue_start,
                end=cue_end,
            )
        )
        await self.wait_for(lambda: session.counters.interrupted_playbacks == 1)
        self.assertEqual(session.counters.barge_in_cues, 1)
        self.assertEqual(playback.cancelled, 1)
        self.assertFalse(session.status().playback_active)
        self.assertEqual(len(app.calls), 1)
        self.assertEqual(len(app.history), 2)
        self.assertEqual(session.counters.spoken_responses, 0)

        playback.release.set()
        await gladia.events.put(
            transcript(
                "Naomi, wait. I need to add something.",
                final=True,
                utterance_id="barge-cue",
                start=cue_start,
                end=cue_end,
            )
        )
        await self.wait_for(lambda: len(app.calls) == 2)
        await self.wait_for(lambda: session.counters.spoken_responses == 1)
        self.assertEqual(
            app.calls[1][1],
            "[Travis]: Naomi, wait. I need to add something.",
        )
        self.assertEqual(len(app.history), 4)
        self.assertNotIn(
            "**Travis (voice transcript):** Naomi, wait",
            session.text_channel.messages,
        )
        await session.stop()

    async def test_ordinary_overlap_does_not_trigger_intentional_barge_in(self) -> None:
        app = FakeCompanion(reply="I keep speaking.")
        playback = BlockingPlayback()
        tts_config = config(
            voice_tts_enabled=True,
            elevenlabs_api_key="tts-key",
            elevenlabs_voice_id="voice-id",
            voice_barge_in_enabled=True,
            voice_barge_in_min_speech_ms=120,
        )
        session, _, gladia, _ = self.make_session(
            app=app,
            playback=playback,
            session_config=tts_config,
        )
        await session.start()

        first_start = session._speech_evidence.duration
        for _ in range(10):
            session._speech_evidence.observe_sent_frame(mono_frame(1000))
        await gladia.events.put(
            transcript(
                "Start speaking",
                final=True,
                utterance_id="ordinary-first",
                start=first_start,
                end=session._speech_evidence.duration,
            )
        )
        await playback.entered.wait()

        overlap_start = session._speech_evidence.duration
        for _ in range(10):
            session._speech_evidence.observe_sent_frame(mono_frame(1000))
        await gladia.events.put(
            transcript(
                "Orange umbrella",
                final=False,
                utterance_id="ordinary-overlap",
                start=overlap_start,
                end=session._speech_evidence.duration,
            )
        )
        await asyncio.sleep(0)
        self.assertEqual(session.counters.barge_in_cues, 0)
        self.assertEqual(session.counters.interrupted_playbacks, 0)
        self.assertTrue(session.status().playback_active)
        self.assertEqual(playback.cancelled, 0)

        playback.release.set()
        await self.wait_for(lambda: session.counters.spoken_responses == 1)
        await session.stop()

    async def test_stop_cancels_inflight_cognition_without_history_effect(self) -> None:
        gate = asyncio.Event()
        app = FakeCompanion(gate=gate)
        session, _, gladia, _ = self.make_session(app=app)
        await session.start()
        start = session._speech_evidence.duration
        for _ in range(10):
            session._speech_evidence.observe_sent_frame(mono_frame(1000))
        await gladia.events.put(
            transcript(
                "Stop while thinking",
                final=True,
                start=start,
                end=session._speech_evidence.duration,
            )
        )
        await app.entered.wait()
        status = await session.stop()
        self.assertEqual(status.state, VoiceSessionState.STOPPED)
        self.assertEqual(len(app.calls), 1)
        self.assertEqual(app.history, [])
        self.assertEqual(
            session.text_channel.messages,
            ["**Travis (voice transcript):** Stop while thinking"],
        )

    async def test_gladia_start_failure_disconnects_receiver(self) -> None:
        channel = FakeVoiceChannel()
        gladia = FakeGladia(channel.log, connect_error=RuntimeError("secret detail"))
        session, _, _, _ = self.make_session(gladia=gladia, channel=channel)
        with self.assertRaisesRegex(VoiceSessionError, "RuntimeError"):
            await session.start()
        self.assertEqual(session.state, VoiceSessionState.STOPPED)
        self.assertEqual(channel.client.disconnect_calls, 1)
        self.assertNotIn("secret detail", str(session.last_error))

    async def test_receiver_start_failure_stops_gladia_and_disconnects(self) -> None:
        channel = FakeVoiceChannel(listen_error=RuntimeError("receiver broke"))
        session, _, gladia, _ = self.make_session(channel=channel)
        with self.assertRaises(VoiceSessionError):
            await session.start()
        self.assertEqual(gladia.stop_calls, 1)
        self.assertEqual(channel.client.disconnect_calls, 1)

    async def test_connected_client_is_owned_before_is_listening_failure(self) -> None:
        channel = FakeVoiceChannel(
            is_listening_error=RuntimeError("receiver state unavailable")
        )
        session, _, _, _ = self.make_session(channel=channel)
        with self.assertRaises(VoiceSessionError):
            await session.start()
        self.assertEqual(channel.client.disconnect_calls, 1)
        self.assertFalse(channel.client.cache_owned)
        self.assertIsNone(session.voice_client)
        self.assertEqual(session.state, VoiceSessionState.STOPPED)

    async def test_existing_client_move_error_and_cancellation_roll_back(self) -> None:
        target = FakeVoiceChannel()
        old_channel = SimpleNamespace(id=99)
        move_error_client = FakeVoiceClient(
            old_channel,
            target.log,
            move_error=RuntimeError("move failed"),
        )
        session, _, _, _ = self.make_session(channel=target)
        with self.assertRaises(VoiceSessionError):
            await session.start(move_error_client)
        self.assertTrue(session._voice_client_borrowed)
        self.assertEqual(move_error_client.disconnect_calls, 1)
        self.assertFalse(move_error_client.cache_owned)

        move_gate = asyncio.Event()
        move_client = FakeVoiceClient(
            old_channel,
            target.log,
            move_gate=move_gate,
        )
        session, _, _, _ = self.make_session(channel=target)
        starting = asyncio.create_task(session.start(move_client))
        await move_client.move_entered.wait()
        starting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await starting
        self.assertEqual(move_client.disconnect_calls, 1)
        self.assertFalse(move_client.cache_owned)
        self.assertIsNone(session.voice_client)
        self.assertEqual(session.state, VoiceSessionState.STOPPED)

    async def test_active_borrowed_receiver_is_preserved_on_rejection(self) -> None:
        channel = FakeVoiceChannel()
        channel.client._listening = True
        channel.client.cache_owned = True
        channel.voice_cache[channel.id] = channel.client
        session, _, gladia, calls = self.make_session(channel=channel)

        with self.assertRaisesRegex(VoiceSessionError, "already in use"):
            await session.start(channel.client)

        self.assertEqual(session.state, VoiceSessionState.STOPPED)
        self.assertTrue(channel.client.is_listening())
        self.assertTrue(channel.client.cache_owned)
        self.assertIs(channel.voice_cache[channel.id], channel.client)
        self.assertEqual(channel.client.disconnect_calls, 0)
        self.assertNotIn("discord.stop_listening", channel.log)
        self.assertEqual(gladia.stop_calls, 0)
        self.assertEqual(calls, [])

    async def test_connect_error_does_not_claim_preexisting_cache_identity(self) -> None:
        channel = FakeVoiceChannel(connect_error=RuntimeError("already cached"))
        unrelated = FakeVoiceClient(channel, channel.log)
        unrelated._listening = True
        unrelated.cache_owned = True
        channel.voice_cache[channel.id] = unrelated
        channel.guild.voice_client = unrelated
        session, _, _, _ = self.make_session(channel=channel)

        with self.assertRaises(VoiceSessionError):
            await session.start(None)

        self.assertEqual(session.state, VoiceSessionState.STOPPED)
        self.assertTrue(unrelated.is_listening())
        self.assertTrue(unrelated.cache_owned)
        self.assertIs(channel.guild.voice_client, unrelated)
        self.assertEqual(unrelated.disconnect_calls, 0)
        self.assertNotIn("discord.stop_listening", channel.log)

    async def test_receiver_runtime_failure_is_reported_and_stop_still_cleans_up(self) -> None:
        session, channel, gladia, _ = self.make_session()
        await session.start()
        channel.client.fail_receiver(RuntimeError("decoder internals"))
        await self.wait_for(lambda: session.state is VoiceSessionState.STOPPED)
        self.assertIn("Discord receiver stopped: RuntimeError", session.last_error)
        self.assertTrue(any("transport warning" in item for item in session.text_channel.messages))
        self.assertEqual(gladia.stop_calls, 1)
        self.assertEqual(channel.client.disconnect_calls, 1)
        self.assert_owned_tasks_terminal(session)

    async def test_stop_is_idempotent_and_cleanup_ordered(self) -> None:
        session, channel, gladia, _ = self.make_session()
        await session.start()
        first = await session.stop()
        second = await session.stop()
        self.assertEqual(first.state, VoiceSessionState.STOPPED)
        self.assertEqual(second.state, VoiceSessionState.STOPPED)
        self.assertEqual(gladia.stop_calls, 1)
        self.assertEqual(channel.client.disconnect_calls, 1)
        self.assertLess(
            channel.log.index("discord.stop_listening"),
            channel.log.index("gladia.stop"),
        )
        self.assertLess(
            channel.log.index("gladia.stop"), channel.log.index("discord.disconnect")
        )
        self.assert_owned_tasks_terminal(session)

    async def test_concurrent_stop_callers_share_one_cleanup(self) -> None:
        session, channel, gladia, _ = self.make_session()
        await session.start()
        one, two = await asyncio.gather(session.stop(), session.stop())
        self.assertEqual(one.state, VoiceSessionState.STOPPED)
        self.assertEqual(two.state, VoiceSessionState.STOPPED)
        self.assertEqual(gladia.stop_calls, 1)
        self.assertEqual(channel.client.disconnect_calls, 1)
        self.assert_owned_tasks_terminal(session)

    async def test_stop_cancellation_waits_for_gladia_cleanup_then_reraises(self) -> None:
        stop_gate = asyncio.Event()
        channel = FakeVoiceChannel()
        gladia = FakeGladia(channel.log, stop_gate=stop_gate)
        session, _, _, _ = self.make_session(gladia=gladia, channel=channel)
        await session.start()
        stopping = asyncio.create_task(session.stop())
        await gladia.stop_entered.wait()
        stopping.cancel()
        await asyncio.sleep(0)
        self.assertFalse(stopping.done())
        self.assertIsNot(session.state, VoiceSessionState.STOPPED)
        stop_gate.set()
        with self.assertRaises(asyncio.CancelledError):
            await stopping
        self.assertEqual(session.state, VoiceSessionState.STOPPED)
        retry = await session.stop()
        self.assertEqual(retry.state, VoiceSessionState.STOPPED)
        self.assert_owned_tasks_terminal(session)

    async def test_gladia_stop_uses_independent_drain_deadline(self) -> None:
        channel = FakeVoiceChannel()
        gladia = FakeGladia(channel.log, stop_delay=0.03)
        session, _, _, _ = self.make_session(
            gladia=gladia, channel=channel, shutdown_step=0.005
        )
        session._gladia_stop_seconds = 0.08
        await session.start()
        loop = asyncio.get_running_loop()
        started = loop.time()
        status = await session.stop()
        self.assertGreaterEqual(loop.time() - started, 0.025)
        self.assertEqual(status.state, VoiceSessionState.STOPPED)
        self.assertEqual(gladia.stop_timeouts, [0.08])
        self.assertNotIn("deadline", session.last_error or "")
        self.assert_owned_tasks_terminal(session)

    async def test_stubborn_gladia_stop_is_hard_bounded_and_retryable(self) -> None:
        gate = asyncio.Event()
        channel = FakeVoiceChannel()
        gladia = FakeGladia(
            channel.log,
            stop_gate=gate,
            suppress_stop_cancel=True,
        )
        session, _, _, _ = self.make_session(
            gladia=gladia, channel=channel, shutdown_step=0.005
        )
        session._gladia_stop_seconds = 0.02
        await session.start()
        loop = asyncio.get_running_loop()
        started = loop.time()
        with self.assertRaisesRegex(VoiceSessionError, "retry stop"):
            await session.stop()
        self.assertLess(loop.time() - started, 0.1)
        self.assertEqual(session.state, VoiceSessionState.FAILED)
        self.assertIsNotNone(session._gladia_stop_task)
        self.assertFalse(session._gladia_stop_task.done())
        self.assertTrue(gladia.stop_cancel_seen.is_set())
        self.assertEqual(gladia.stop_calls, 1)
        gate.set()
        async with asyncio.timeout(0.5):
            while not session._gladia_stop_task.done():
                await asyncio.sleep(0)
        status = await session.stop()
        self.assertEqual(status.state, VoiceSessionState.STOPPED)
        self.assertEqual(gladia.stop_calls, 1)
        self.assert_owned_tasks_terminal(session)

    async def test_terminal_log_preserves_transport_counters(self) -> None:
        session, _, _, _ = self.make_session()
        await session.start()
        session.counters.received_packets = 11
        session.counters.enqueued_packets = 10
        session.counters.ingress_drops = 1
        session.counters.queue_drops = 2
        session.counters.rtp_gap_samples = 960
        session.counters.rtp_discontinuities = 3
        session.counters.playout_reanchors = 2
        session.counters.clock_dropped_packets = 4
        session.counters.late_audio_samples = 5
        session.gladia.result.reconnects = 1
        session.gladia.result.reconnect_failures = 2
        session.gladia.result.ambiguous_frames_dropped = 3
        session.counters.partial_transcripts = 6
        session.counters.final_transcripts = 7
        with self.assertLogs(
            "disco_proxy_soul.discord_app.voice_session", level="INFO"
        ) as captured:
            await session.stop()
        terminal = "\n".join(captured.output)
        for expected in (
            "received=11",
            "enqueued=10",
            "queue_drops=2",
            "rtp_gap_samples=960",
            "rtp_discontinuities=3",
            "playout_reanchors=2",
            "clock_dropped=4",
            "late_samples=5",
            "gladia_reconnects=1",
            "gladia_reconnect_failures=2",
            "gladia_ambiguous_frames=3",
            "partials=6",
            "finals=7",
        ):
            self.assertIn(expected, terminal)
        self.assertNotIn("test-key", terminal)

    async def test_production_application_logger_emits_terminal_once(self) -> None:
        app_logger = logging.getLogger("disco_proxy_soul")
        saved_handlers = list(app_logger.handlers)
        saved_level = app_logger.level
        saved_propagate = app_logger.propagate
        root_level = logging.getLogger().level
        discord_level = logging.getLogger("discord").level
        output = io.StringIO()
        app_logger.handlers.clear()
        try:
            configure_application_logging("console-secret", stream=output)
            configure_application_logging("console-secret", stream=output)
            self.assertEqual(logging.getLogger().level, root_level)
            self.assertEqual(logging.getLogger("discord").level, discord_level)
            session, _, _, _ = self.make_session()
            await session.start()
            session.counters.received_packets = 17
            await session.stop()
            logging.getLogger(
                "disco_proxy_soul.discord_app.voice_session"
            ).error("Authorization: bEaReR console-secret")
            console = output.getvalue()
            self.assertEqual(console.count("Live voice terminal"), 1)
            self.assertIn("received=17", console)
            self.assertNotIn("console-secret", console)
        finally:
            for handler in app_logger.handlers:
                handler.close()
            app_logger.handlers[:] = saved_handlers
            app_logger.setLevel(saved_level)
            app_logger.propagate = saved_propagate

    async def test_stop_cancellation_during_receive_after_is_bounded(self) -> None:
        channel = FakeVoiceChannel(defer_after=True)
        session, _, _, _ = self.make_session(channel=channel, shutdown_step=0.02)
        await session.start()
        stopping = asyncio.create_task(session.stop())
        await self.wait_for(lambda: "discord.stop_listening" in channel.log)
        stopping.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await stopping
        self.assertEqual(session.state, VoiceSessionState.STOPPED)
        self.assertTrue(session._listener_done.done())
        self.assert_owned_tasks_terminal(session)

    async def test_stop_cancellation_during_reporter_and_disconnect_is_safe(self) -> None:
        send_gate = asyncio.Event()
        text = FakeTextChannel(send_gate=send_gate)
        channel = FakeVoiceChannel()
        session, _, _, _ = self.make_session(
            channel=channel,
            text_channel=text,
            shutdown_step=0.02,
        )
        await session.start()
        session._queue_report("test", "bounded warning")
        await text.send_entered.wait()
        stopping = asyncio.create_task(session.stop())
        await self.wait_for(lambda: session._report_stop)
        stopping.cancel()
        send_gate.set()
        with self.assertRaises(asyncio.CancelledError):
            await stopping
        self.assertEqual(session.state, VoiceSessionState.STOPPED)
        self.assert_owned_tasks_terminal(session)

    async def test_stop_cancellation_during_disconnect_is_safe(self) -> None:
        disconnect_gate = asyncio.Event()
        channel = FakeVoiceChannel(disconnect_gate=disconnect_gate)
        session, _, _, _ = self.make_session(channel=channel)
        await session.start()
        stopping = asyncio.create_task(session.stop())
        await channel.client.disconnect_entered.wait()
        stopping.cancel()
        await asyncio.sleep(0)
        self.assertFalse(stopping.done())
        disconnect_gate.set()
        with self.assertRaises(asyncio.CancelledError):
            await stopping
        self.assertEqual(session.state, VoiceSessionState.STOPPED)
        self.assert_owned_tasks_terminal(session)

    async def test_disconnect_timeout_uses_public_cleanup_fallback(self) -> None:
        disconnect_gate = asyncio.Event()
        channel = FakeVoiceChannel(disconnect_gate=disconnect_gate)
        session, _, _, _ = self.make_session(channel=channel, shutdown_step=0.01)
        await session.start()
        result = await session.stop()
        self.assertEqual(result.state, VoiceSessionState.STOPPED)
        self.assertEqual(channel.client.cleanup_calls, 1)
        self.assertFalse(channel.client.cache_owned)
        self.assertIsNone(session.voice_client)

    async def test_disconnect_and_cleanup_failure_stays_failed_until_retry(self) -> None:
        channel = FakeVoiceChannel(
            disconnect_error=RuntimeError("disconnect failed"),
            cleanup_error=RuntimeError("cleanup failed"),
        )
        session, _, _, _ = self.make_session(channel=channel, shutdown_step=0.01)
        await session.start()
        with self.assertRaisesRegex(VoiceSessionError, "non-terminal"):
            await session.stop()
        self.assertEqual(session.state, VoiceSessionState.FAILED)
        self.assertIs(session.voice_client, channel.client)
        self.assertTrue(channel.client.cache_owned)

        channel.client.cleanup_error = None
        result = await session.stop()
        self.assertEqual(result.state, VoiceSessionState.STOPPED)
        self.assertFalse(channel.client.cache_owned)
        self.assertIsNone(session.voice_client)

    async def test_stop_cancellation_during_sender_and_consumer_task_awaits(self) -> None:
        for owned_name in ("_sender_task", "_consumer_task"):
            with self.subTest(task=owned_name):
                session, _, _, _ = self.make_session(shutdown_step=0.01)
                await session.start()
                original = getattr(session, owned_name)
                original.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await original

                entered = asyncio.Event()
                release = asyncio.Event()

                async def stubborn_owned_task():
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        entered.set()
                        await release.wait()

                replacement = asyncio.create_task(stubborn_owned_task())
                await asyncio.sleep(0)
                setattr(session, owned_name, replacement)
                stopping = asyncio.create_task(session.stop())
                await entered.wait()
                stopping.cancel()
                await asyncio.sleep(0)
                self.assertFalse(stopping.done())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await stopping
                self.assertEqual(session.state, VoiceSessionState.STOPPED)
                self.assert_owned_tasks_terminal(session)

    async def test_nonterminal_owned_task_keeps_failed_state_until_stop_retry(self) -> None:
        session, _, _, _ = self.make_session(shutdown_step=0.01)
        await session.start()
        original = session._sender_task
        original.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await original

        release = asyncio.Event()

        async def twice_stubborn():
            cancellations = 0
            while cancellations < 2:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancellations += 1
            await release.wait()

        stubborn = asyncio.create_task(twice_stubborn())
        await asyncio.sleep(0)
        session._sender_task = stubborn
        with self.assertRaisesRegex(VoiceSessionError, "non-terminal"):
            await session.stop()
        self.assertEqual(session.state, VoiceSessionState.FAILED)
        self.assertFalse(stubborn.done())

        release.set()
        await stubborn
        result = await session.stop()
        self.assertEqual(result.state, VoiceSessionState.STOPPED)
        self.assert_owned_tasks_terminal(session)

    async def test_sender_consumer_and_abnormal_failures_auto_shutdown(self) -> None:
        cases = []

        sender_channel = FakeVoiceChannel()
        sender = FakeGladia(sender_channel.log, send_error=RuntimeError("secret"))
        cases.append((sender_channel, sender, None))

        consumer_channel = FakeVoiceChannel()
        consumer = FakeGladia(consumer_channel.log)
        cases.append((consumer_channel, consumer, RuntimeError("consumer secret")))

        abnormal_channel = FakeVoiceChannel()
        abnormal = FakeGladia(abnormal_channel.log)
        abnormal.completion = CompletionState.ABNORMAL
        abnormal.result.completion_reason = "socket closed"
        cases.append((abnormal_channel, abnormal, None))

        for index, (channel, gladia, event) in enumerate(cases):
            with self.subTest(index=index):
                session, _, _, _ = self.make_session(gladia=gladia, channel=channel)
                await session.start()
                if index == 1:
                    await gladia.events.put(event)
                elif index == 2:
                    await gladia.events.put(None)
                await self.wait_for(lambda: session.state is VoiceSessionState.STOPPED)
                self.assertEqual(gladia.stop_calls, 1)
                self.assertEqual(channel.client.disconnect_calls, 1)
                sent_after_stop = len(gladia.sent)
                await asyncio.sleep(0.03)
                self.assertEqual(len(gladia.sent), sent_after_stop)
                self.assert_owned_tasks_terminal(session)

    async def test_reporter_is_bounded_coalesced_redacted_and_terminal(self) -> None:
        gate = asyncio.Event()
        text = FakeTextChannel(send_gate=gate)
        session, _, _, _ = self.make_session(text_channel=text)
        await session.start()
        for index in range(100):
            session._queue_report(
                "same-key",
                f"warning {index} test-key wss://example.invalid/?token=bearer",
            )
        for index in range(100):
            session._queue_report(f"bounded-{index}", f"bounded {index} test-key")
        self.assertLessEqual(len(session._reports), 8)
        self.assertGreater(session.counters.report_drops, 0)
        await text.send_entered.wait()
        stopping = asyncio.create_task(session.stop())
        gate.set()
        await stopping
        rendered = " ".join(text.messages)
        self.assertNotIn("test-key", rendered)
        self.assertNotIn("bearer", rendered)
        self.assert_owned_tasks_terminal(session)

    async def test_reporter_send_failure_logs_redacted_message_without_traceback(self) -> None:
        secret = "reporter-credential"
        text = FakeTextChannel(
            send_error=RuntimeError(f"Authorization: bEaReR {secret}")
        )
        session, _, _, _ = self.make_session(text_channel=text)
        with self.assertLogs(
            "disco_proxy_soul.discord_app.voice_session", level="ERROR"
        ) as captured:
            await session.start()
            session._queue_report("report", "safe warning")
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            await session.stop()
        rendered = "\n".join(captured.output)
        self.assertNotIn(secret, rendered)
        self.assertIn("bEaReR <redacted>", rendered)
        self.assertNotIn("Traceback", rendered)

    async def test_bearer_credentials_are_redacted_from_reports_and_public_error(self) -> None:
        secret = "super-secret-credential"
        text = FakeTextChannel()
        session, _, _, _ = self.make_session(text_channel=text)
        await session.start()
        session._trigger_failure(
            "auth",
            f"Authorization: Bearer {secret}; standalone bEaReR {secret}",
        )
        await self.wait_for(lambda: session.state is VoiceSessionState.STOPPED)
        rendered = "\n".join(text.messages)
        self.assertNotIn(secret, rendered)
        self.assertIn("Bearer <redacted>", rendered)

        channel = FakeVoiceChannel()
        gladia = FakeGladia(
            channel.log,
            connect_error=RuntimeError(f"Authorization: Bearer {secret}"),
        )
        failed, _, _, _ = self.make_session(gladia=gladia, channel=channel)
        try:
            await failed.start()
        except VoiceSessionError as exc:
            public = "".join(traceback.format_exception(exc))
        else:
            self.fail("startup unexpectedly succeeded")
        self.assertNotIn(secret, public)

    async def test_live_session_creates_no_raw_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before = set(root.rglob("*"))
            session, _, _, _ = self.make_session()
            await session.start()
            await session.stop()
            self.assertEqual(before, set(root.rglob("*")))


class VoiceManagerAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_bot_voice_state_event_is_wired_to_canonical_manager(self) -> None:
        app = MagicMock()
        app.config = config()
        app.persona = SimpleNamespace(companion_name="Naomi", partner_name="Travis")
        app.catalog = {}
        client = build_bot(app)
        try:
            manager = client.voice_sessions
            manager.handle_voice_state_update = MagicMock(return_value=False)
            member = SimpleNamespace(id=7)
            before = SimpleNamespace(channel=None)
            after = SimpleNamespace(channel=None)
            await client.on_voice_state_update(member, before, after)
            manager.handle_voice_state_update.assert_called_once_with(
                member, before, after
            )
        finally:
            await client.close()

    async def test_starter_leave_during_connect_aborts_before_gladia(self) -> None:
        connect_gate = asyncio.Event()
        channel = FakeVoiceChannel(connect_gate=connect_gate)
        created = []

        def gladia_factory(*args, **kwargs):
            gladia = FakeGladia(channel.log)
            created.append(gladia)
            return gladia

        manager = VoiceSessionManager(config(), gladia_factory=gladia_factory)
        guild = SimpleNamespace(id=1, voice_client=None)
        starter = SimpleNamespace(
            id=7,
            name="travis",
            display_name="Travis",
            voice=SimpleNamespace(channel=channel),
            guild=guild,
        )
        starting = asyncio.create_task(
            manager.start(
                guild=guild,
                voice_channel=channel,
                text_channel=FakeTextChannel(),
                starter=starter,
            )
        )
        await channel.connect_entered.wait()
        starter.voice.channel = None
        leave = (
            starter,
            SimpleNamespace(channel=channel),
            SimpleNamespace(channel=None),
        )
        self.assertTrue(manager.handle_voice_state_update(*leave))
        self.assertFalse(manager.handle_voice_state_update(*leave))
        self.assertEqual(manager._starter_leave_tasks, {})
        connect_gate.set()
        with self.assertRaisesRegex(VoiceSessionError, "starter left"):
            await starting
        self.assertEqual(created, [])
        self.assertEqual(channel.client.disconnect_calls, 1)
        self.assertFalse(manager.has_live(1))

    async def test_starter_move_during_gladia_connect_never_starts_sender(self) -> None:
        connect_gate = asyncio.Event()
        channel = FakeVoiceChannel()
        other = SimpleNamespace(id=23)
        gladia = FakeGladia(channel.log, connect_gate=connect_gate)
        manager = VoiceSessionManager(
            config(), gladia_factory=lambda *a, **k: gladia
        )
        guild = SimpleNamespace(id=1, voice_client=None)
        starter = SimpleNamespace(
            id=7,
            name="travis",
            display_name="Travis",
            voice=SimpleNamespace(channel=channel),
            guild=guild,
        )
        starting = asyncio.create_task(
            manager.start(
                guild=guild,
                voice_channel=channel,
                text_channel=FakeTextChannel(),
                starter=starter,
            )
        )
        await gladia.connect_entered.wait()
        starter.voice.channel = other
        move = (
            starter,
            SimpleNamespace(channel=channel),
            SimpleNamespace(channel=other),
        )
        self.assertTrue(manager.handle_voice_state_update(*move))
        self.assertFalse(manager.handle_voice_state_update(*move))
        connect_gate.set()
        with self.assertRaisesRegex(VoiceSessionError, "starter left"):
            await starting
        self.assertEqual(gladia.stop_calls, 1)
        self.assertEqual(gladia.sent, [])
        self.assertNotIn("discord.listen", channel.log)
        self.assertEqual(channel.client.disconnect_calls, 1)
        self.assertFalse(manager.has_live(1))
        self.assertEqual(manager._starter_leave_tasks, {})

    async def test_starter_leave_and_move_each_auto_stop_once(self) -> None:
        for moved_to in (None, SimpleNamespace(id=23)):
            with self.subTest(moved_to=getattr(moved_to, "id", None)):
                channel = FakeVoiceChannel()
                text = FakeTextChannel()
                gladia = FakeGladia(channel.log)
                manager = VoiceSessionManager(
                    config(), gladia_factory=lambda *a, **k: gladia
                )
                guild = SimpleNamespace(id=1, voice_client=None)
                starter = SimpleNamespace(
                    id=7,
                    name="travis",
                    display_name="Travis",
                    voice=SimpleNamespace(channel=channel),
                    guild=guild,
                )
                await manager.start(
                    guild=guild,
                    voice_channel=channel,
                    text_channel=text,
                    starter=starter,
                )

                self.assertTrue(
                    manager.handle_voice_state_update(
                        starter,
                        SimpleNamespace(channel=channel),
                        SimpleNamespace(channel=moved_to),
                    )
                )
                self.assertFalse(
                    manager.handle_voice_state_update(
                        starter,
                        SimpleNamespace(channel=channel),
                        SimpleNamespace(channel=moved_to),
                    )
                )
                task = next(iter(manager._starter_leave_tasks.values()))
                await task
                await asyncio.sleep(0)
                self.assertFalse(manager.has_live(1))
                self.assertEqual(manager._starter_leave_tasks, {})
                self.assertEqual(gladia.stop_calls, 1)
                self.assertEqual(channel.client.disconnect_calls, 1)
                self.assertTrue(
                    any("starter left" in message.lower() for message in text.messages)
                )

    async def test_voice_state_update_ignores_irrelevant_changes_and_diagnostics(self) -> None:
        channel = FakeVoiceChannel()
        text = FakeTextChannel()
        manager = VoiceSessionManager(
            config(), gladia_factory=lambda *a, **k: FakeGladia(channel.log)
        )
        guild = SimpleNamespace(id=1, voice_client=None)
        starter = SimpleNamespace(
            id=7,
            name="travis",
            display_name="Travis",
            voice=SimpleNamespace(channel=channel),
            guild=guild,
        )
        await manager.start(
            guild=guild,
            voice_channel=channel,
            text_channel=text,
            starter=starter,
        )
        other = SimpleNamespace(id=8, guild=guild)
        same = SimpleNamespace(channel=channel)
        self.assertFalse(
            manager.handle_voice_state_update(
                other, same, SimpleNamespace(channel=None)
            )
        )
        self.assertFalse(
            manager.handle_voice_state_update(
                starter,
                SimpleNamespace(channel=None),
                same,
            )
        )
        self.assertFalse(manager.handle_voice_state_update(starter, same, same))
        self.assertEqual(manager._starter_leave_tasks, {})
        self.assertTrue(manager.has_live(1))
        await manager.stop(1)

        manager._diagnostics[1] = SimpleNamespace()
        self.assertFalse(
            manager.handle_voice_state_update(
                starter, same, SimpleNamespace(channel=None)
            )
        )
        self.assertEqual(manager._starter_leave_tasks, {})

    async def test_starter_leave_and_manual_stop_share_one_shutdown(self) -> None:
        gate = asyncio.Event()
        channel = FakeVoiceChannel()
        text = FakeTextChannel()
        gladia = FakeGladia(channel.log, stop_gate=gate)
        manager = VoiceSessionManager(
            config(voice_gladia_stop_seconds=0.2),
            gladia_factory=lambda *a, **k: gladia,
        )
        guild = SimpleNamespace(id=1, voice_client=None)
        starter = SimpleNamespace(
            id=7,
            name="travis",
            display_name="Travis",
            voice=SimpleNamespace(channel=channel),
            guild=guild,
        )
        await manager.start(
            guild=guild,
            voice_channel=channel,
            text_channel=text,
            starter=starter,
        )
        self.assertTrue(
            manager.handle_voice_state_update(
                starter,
                SimpleNamespace(channel=channel),
                SimpleNamespace(channel=None),
            )
        )
        auto = next(iter(manager._starter_leave_tasks.values()))
        await gladia.stop_entered.wait()
        manual = asyncio.create_task(manager.stop(1))
        self.assertFalse(
            manager.handle_voice_state_update(
                starter,
                SimpleNamespace(channel=channel),
                SimpleNamespace(channel=None),
            )
        )
        gate.set()
        manual_result = await manual
        await auto
        await asyncio.sleep(0)
        self.assertEqual(manual_result.state, VoiceSessionState.STOPPED)
        self.assertEqual(gladia.stop_calls, 1)
        self.assertEqual(channel.client.disconnect_calls, 1)
        self.assertFalse(manager.has_live(1))
        self.assertEqual(manager._starter_leave_tasks, {})
        self.assertFalse(
            any(
                task.get_name() == "voice-starter-left-g1" and not task.done()
                for task in asyncio.all_tasks()
            )
        )

    async def test_stale_blocked_leave_notice_does_not_suppress_new_session(self) -> None:
        class OutcomeGateText(FakeTextChannel):
            def __init__(self):
                super().__init__()
                self.release = asyncio.Event()
                self.outcome_entered = asyncio.Event()

            async def send(self, content: str):
                if content.startswith("Live voice transcription stopped because"):
                    self.outcome_entered.set()
                    await self.release.wait()
                self.messages.append(content)

        channels = [FakeVoiceChannel(11), FakeVoiceChannel(12)]
        gladias = [FakeGladia(channels[0].log), FakeGladia(channels[1].log)]
        manager = VoiceSessionManager(
            config(), gladia_factory=lambda *a, **k: gladias.pop(0)
        )
        guild = SimpleNamespace(id=1, voice_client=None)
        starter = SimpleNamespace(
            id=7,
            name="travis",
            display_name="Travis",
            voice=SimpleNamespace(channel=channels[0]),
            guild=guild,
        )
        old_text = OutcomeGateText()
        await manager.start(
            guild=guild,
            voice_channel=channels[0],
            text_channel=old_text,
            starter=starter,
        )
        self.assertTrue(
            manager.handle_voice_state_update(
                starter,
                SimpleNamespace(channel=channels[0]),
                SimpleNamespace(channel=None),
            )
        )
        old_task = next(iter(manager._starter_leave_tasks.values()))
        await old_text.outcome_entered.wait()
        self.assertFalse(manager.has_live(1))

        starter.voice.channel = channels[1]
        new_text = FakeTextChannel()
        await manager.start(
            guild=guild,
            voice_channel=channels[1],
            text_channel=new_text,
            starter=starter,
        )
        self.assertTrue(
            manager.handle_voice_state_update(
                starter,
                SimpleNamespace(channel=channels[1]),
                SimpleNamespace(channel=None),
            )
        )
        self.assertEqual(len(manager._starter_leave_tasks), 2)
        new_task = next(
            task
            for task in manager._starter_leave_tasks.values()
            if task is not old_task
        )
        await new_task
        await asyncio.sleep(0)
        self.assertFalse(manager.has_live(1))
        old_text.release.set()
        await old_task
        await asyncio.sleep(0)
        self.assertEqual(manager._starter_leave_tasks, {})

    async def test_wrong_channel_duplicate_and_diagnostic_conflicts(self) -> None:
        manager = VoiceSessionManager(config())
        channel = FakeVoiceChannel()
        other = FakeVoiceChannel(23)
        starter = SimpleNamespace(
            id=7,
            name="travis",
            display_name="Travis",
            voice=SimpleNamespace(channel=other),
        )
        guild = SimpleNamespace(id=1, voice_client=None)
        with self.assertRaisesRegex(VoiceSessionError, "starter"):
            await manager.start(
                guild=guild,
                voice_channel=channel,
                text_channel=FakeTextChannel(),
                starter=starter,
            )

        self.assertTrue(manager.begin_diagnostic(1))
        with self.assertRaisesRegex(VoiceSessionError, "diagnostic"):
            manager.validate_start(1)
        manager.end_diagnostic(1)

        fake_session = SimpleNamespace(
            start=lambda existing: asyncio.sleep(0),
            status=lambda: None,
        )
        manager._sessions[1] = fake_session
        with self.assertRaisesRegex(VoiceSessionError, "already"):
            manager.validate_start(1)

    async def test_fatal_shutdown_releases_registry_and_allows_restart(self) -> None:
        channel = FakeVoiceChannel()
        created = []

        def gladia_factory(*args, **kwargs):
            gladia = FakeGladia(channel.log)
            created.append(gladia)
            return gladia

        manager = VoiceSessionManager(config(), gladia_factory=gladia_factory)
        starter = SimpleNamespace(
            id=7,
            name="travis",
            display_name="Travis",
            voice=SimpleNamespace(channel=channel),
        )
        guild = SimpleNamespace(id=1, voice_client=None)
        await manager.start(
            guild=guild,
            voice_channel=channel,
            text_channel=FakeTextChannel(),
            starter=starter,
        )
        channel.client.fail_receiver(RuntimeError("receiver"))
        async with asyncio.timeout(0.5):
            while manager.has_live(1):
                await asyncio.sleep(0)
        self.assertEqual(created[0].stop_calls, 1)

        await manager.start(
            guild=guild,
            voice_channel=channel,
            text_channel=FakeTextChannel(),
            starter=starter,
        )
        self.assertTrue(manager.has_live(1))
        await manager.stop(1)
        self.assertFalse(manager.has_live(1))

    async def test_manager_keeps_registry_until_cancelled_stop_cleanup_finishes(self) -> None:
        gate = asyncio.Event()
        channel = FakeVoiceChannel()
        gladia = FakeGladia(channel.log, stop_gate=gate)
        manager = VoiceSessionManager(config(), gladia_factory=lambda *a, **k: gladia)
        starter = SimpleNamespace(
            id=7,
            name="travis",
            display_name="Travis",
            voice=SimpleNamespace(channel=channel),
        )
        guild = SimpleNamespace(id=1, voice_client=None)
        await manager.start(
            guild=guild,
            voice_channel=channel,
            text_channel=FakeTextChannel(),
            starter=starter,
        )
        stopping = asyncio.create_task(manager.stop(1))
        await gladia.stop_entered.wait()
        stopping.cancel()
        await asyncio.sleep(0)
        self.assertTrue(manager.has_live(1))
        gate.set()
        with self.assertRaises(asyncio.CancelledError):
            await stopping
        self.assertFalse(manager.has_live(1))

    async def test_failed_disconnect_retains_registry_and_auto_failure_is_observed(self) -> None:
        secret = "auto-fatal-bearer-secret"
        channel = FakeVoiceChannel(
            disconnect_error=RuntimeError(f"Bearer {secret}"),
            cleanup_error=RuntimeError(f"Authorization: Bearer {secret}"),
        )
        text = FakeTextChannel()
        manager = VoiceSessionManager(
            config(), gladia_factory=lambda *a, **k: FakeGladia(channel.log)
        )
        starter = SimpleNamespace(
            id=7,
            name="travis",
            display_name="Travis",
            voice=SimpleNamespace(channel=channel),
        )
        guild = SimpleNamespace(id=1, voice_client=None)
        loop_contexts = []
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: loop_contexts.append(context))
        try:
            await manager.start(
                guild=guild,
                voice_channel=channel,
                text_channel=text,
                starter=starter,
            )
            channel.client.fail_receiver(
                RuntimeError(f"Authorization: Bearer {secret}")
            )
            async with asyncio.timeout(0.5):
                while not (
                    manager._sessions[1]._shutdown_task is not None
                    and manager._sessions[1]._shutdown_task.done()
                ):
                    await asyncio.sleep(0)
            # Do not inspect task.exception(): the production callback must do it.
            await asyncio.sleep(0)
            self.assertTrue(manager.has_live(1))
            self.assertEqual(manager._sessions[1].state, VoiceSessionState.FAILED)
            self.assertEqual(loop_contexts, [])
            self.assertNotIn(secret, "\n".join(text.messages))

            channel.client.cleanup_error = None
            result = await manager.stop(1)
            self.assertEqual(result.state, VoiceSessionState.STOPPED)
            self.assertFalse(manager.has_live(1))
        finally:
            loop.set_exception_handler(previous_handler)

    async def test_cancelled_live_connect_cache_is_retained_failed_until_retry_cleanup(self) -> None:
        connect_gate = asyncio.Event()
        channel = FakeVoiceChannel(
            connect_gate=connect_gate,
            cache_before_handshake=True,
        )

        def session_factory(**kwargs):
            return VoiceSession(**kwargs, shutdown_step_seconds=0.01)

        manager = VoiceSessionManager(
            config(),
            gladia_factory=lambda *a, **k: FakeGladia(channel.log),
            session_factory=session_factory,
        )
        starter = SimpleNamespace(
            id=7,
            name="travis",
            display_name="Travis",
            voice=SimpleNamespace(channel=channel),
        )
        guild = SimpleNamespace(id=1, voice_client=None)
        starting = asyncio.create_task(
            manager.start(
                guild=guild,
                voice_channel=channel,
                text_channel=FakeTextChannel(),
                starter=starter,
            )
        )
        await channel.connect_entered.wait()
        self.assertIs(channel.voice_cache[channel.id], channel.client)
        starting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await starting
        self.assertTrue(manager.has_live(1))
        self.assertEqual(manager._sessions[1].state, VoiceSessionState.FAILED)
        self.assertTrue(channel.client.cache_owned)

        connect_gate.set()
        async with asyncio.timeout(0.5):
            while not manager._sessions[1]._connect_task.done():
                await asyncio.sleep(0)
        stopped = await manager.stop(1)
        self.assertEqual(stopped.state, VoiceSessionState.STOPPED)
        self.assertFalse(manager.has_live(1))
        self.assertEqual(channel.voice_cache, {})
        self.assertFalse(channel.client.cache_owned)

    async def test_live_connect_error_reclaims_client_cached_before_handshake(self) -> None:
        channel = FakeVoiceChannel(
            cache_before_handshake=True,
            connect_error=RuntimeError("handshake failed"),
        )
        manager = VoiceSessionManager(
            config(), gladia_factory=lambda *a, **k: FakeGladia(channel.log)
        )
        starter = SimpleNamespace(
            id=7,
            name="travis",
            display_name="Travis",
            voice=SimpleNamespace(channel=channel),
        )
        guild = SimpleNamespace(id=1, voice_client=None)

        with self.assertRaisesRegex(VoiceSessionError, "RuntimeError"):
            await manager.start(
                guild=guild,
                voice_channel=channel,
                text_channel=FakeTextChannel(),
                starter=starter,
            )
        self.assertFalse(manager.has_live(1))
        self.assertEqual(channel.voice_cache, {})
        self.assertIsNone(channel.guild.voice_client)
        self.assertEqual(channel.client.disconnect_calls, 1)

    async def test_live_failed_connect_does_not_claim_concurrent_replacement(self) -> None:
        gate = asyncio.Event()
        channel = FakeVoiceChannel(
            connect_gate=gate,
            connect_error=RuntimeError("handshake failed"),
        )
        manager = VoiceSessionManager(
            config(), gladia_factory=lambda *a, **k: FakeGladia(channel.log)
        )
        starter = SimpleNamespace(
            id=7,
            name="travis",
            display_name="Travis",
            voice=SimpleNamespace(channel=channel),
        )
        guild = SimpleNamespace(id=1, voice_client=None)
        starting = asyncio.create_task(
            manager.start(
                guild=guild,
                voice_channel=channel,
                text_channel=FakeTextChannel(),
                starter=starter,
            )
        )
        await channel.connect_entered.wait()
        unrelated = FakeVoiceClient(channel, channel.log)
        unrelated.cache_owned = True
        channel.guild.voice_client = unrelated
        channel.voice_cache[channel.id] = unrelated
        gate.set()

        with self.assertRaises(VoiceSessionError):
            await starting
        self.assertFalse(manager.has_live(1))
        self.assertIs(channel.guild.voice_client, unrelated)
        self.assertIs(channel.voice_cache[channel.id], unrelated)
        self.assertTrue(unrelated.cache_owned)
        self.assertEqual(unrelated.disconnect_calls, 0)
        self.assertEqual(channel.client.disconnect_calls, 1)

    async def test_diagnostic_start_cancellation_at_connect_and_move_cleans_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            connect_gate = asyncio.Event()
            channel = FakeVoiceChannel(connect_gate=connect_gate)
            manager = VoiceSessionManager(config(data_dir=Path(tmp)))
            guild = SimpleNamespace(id=1, voice_client=None)
            starting = asyncio.create_task(
                manager.start_diagnostic(guild=guild, voice_channel=channel)
            )
            await channel.connect_entered.wait()
            self.assertTrue(manager.has_diagnostic(1))
            starting.cancel()
            connect_gate.set()
            with self.assertRaises(asyncio.CancelledError):
                await starting
            self.assertFalse(manager.has_diagnostic(1))
            self.assertFalse(channel.client.cache_owned)

            move_gate = asyncio.Event()
            target = FakeVoiceChannel(23)
            existing = FakeVoiceClient(
                SimpleNamespace(id=99), target.log, move_gate=move_gate
            )
            guild.voice_client = existing
            starting = asyncio.create_task(
                manager.start_diagnostic(guild=guild, voice_channel=target)
            )
            await existing.move_entered.wait()
            self.assertTrue(manager.has_diagnostic(1))
            starting.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await starting
            self.assertFalse(manager.has_diagnostic(1))
            self.assertEqual(existing.disconnect_calls, 1)
            self.assertFalse(existing.cache_owned)

    async def test_cancelled_diagnostic_connect_waits_for_cached_client_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            connect_gate = asyncio.Event()
            channel = FakeVoiceChannel(
                connect_gate=connect_gate,
                cache_before_handshake=True,
            )
            manager = VoiceSessionManager(config(data_dir=Path(tmp)))
            guild = SimpleNamespace(id=1, voice_client=None)
            starting = asyncio.create_task(
                manager.start_diagnostic(guild=guild, voice_channel=channel)
            )
            await channel.connect_entered.wait()
            manager._diagnostics[1]._shutdown_step_seconds = 0.1
            self.assertIs(channel.voice_cache[channel.id], channel.client)

            starting.cancel()
            connect_gate.set()
            with self.assertRaises(asyncio.CancelledError):
                await starting
            self.assertFalse(manager.has_diagnostic(1))
            self.assertEqual(channel.voice_cache, {})
            self.assertEqual(channel.client.disconnect_calls, 1)
            self.assertFalse(channel.client.cache_owned)

    async def test_diagnostic_connect_error_reclaims_cached_client(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            channel = FakeVoiceChannel(
                cache_before_handshake=True,
                connect_error=RuntimeError("handshake failed"),
            )
            manager = VoiceSessionManager(config(data_dir=Path(tmp)))
            guild = SimpleNamespace(id=1, voice_client=None)

            with self.assertRaisesRegex(VoiceSessionError, "RuntimeError"):
                await manager.start_diagnostic(guild=guild, voice_channel=channel)
            self.assertFalse(manager.has_diagnostic(1))
            self.assertEqual(channel.voice_cache, {})
            self.assertIsNone(channel.guild.voice_client)
            self.assertEqual(channel.client.disconnect_calls, 1)
            self.assertFalse((Path(tmp) / "voice-captures").exists())

    async def test_cancelled_diagnostic_connect_preserves_concurrent_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            gate = asyncio.Event()
            channel = FakeVoiceChannel(
                connect_gate=gate,
                cache_before_handshake=True,
            )
            manager = VoiceSessionManager(config(data_dir=Path(tmp)))
            guild = SimpleNamespace(id=1, voice_client=None)
            starting = asyncio.create_task(
                manager.start_diagnostic(guild=guild, voice_channel=channel)
            )
            await channel.connect_entered.wait()
            unrelated = FakeVoiceClient(channel, channel.log)
            unrelated.cache_owned = True
            channel.guild.voice_client = unrelated
            channel.voice_cache[channel.id] = unrelated
            starting.cancel()
            gate.set()

            with self.assertRaises(asyncio.CancelledError):
                await starting
            self.assertFalse(manager.has_diagnostic(1))
            self.assertIs(channel.guild.voice_client, unrelated)
            self.assertIs(channel.voice_cache[channel.id], unrelated)
            self.assertTrue(unrelated.cache_owned)
            self.assertEqual(unrelated.disconnect_calls, 0)
            self.assertEqual(channel.client.disconnect_calls, 1)

    async def test_active_borrowed_receiver_is_preserved_for_diagnostic_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            channel = FakeVoiceChannel()
            channel.client._listening = True
            channel.client.cache_owned = True
            channel.voice_cache[channel.id] = channel.client
            manager = VoiceSessionManager(config(data_dir=Path(tmp)))
            guild = SimpleNamespace(id=1, voice_client=channel.client)

            with self.assertRaisesRegex(VoiceSessionError, "already in use"):
                await manager.start_diagnostic(guild=guild, voice_channel=channel)

            self.assertFalse(manager.has_diagnostic(1))
            self.assertTrue(channel.client.is_listening())
            self.assertTrue(channel.client.cache_owned)
            self.assertIs(channel.voice_cache[channel.id], channel.client)
            self.assertEqual(channel.client.disconnect_calls, 0)
            self.assertNotIn("discord.stop_listening", channel.log)
            self.assertFalse((Path(tmp) / "voice-captures").exists())

    async def test_diagnostic_stop_cancellation_finishes_before_registry_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            disconnect_gate = asyncio.Event()
            channel = FakeVoiceChannel(disconnect_gate=disconnect_gate)
            manager = VoiceSessionManager(config(data_dir=Path(tmp)))
            guild = SimpleNamespace(id=1, voice_client=None)
            await manager.start_diagnostic(guild=guild, voice_channel=channel)
            self.assertTrue(manager.has_diagnostic(1))

            stopping = asyncio.create_task(manager.stop_diagnostic(1))
            await channel.client.disconnect_entered.wait()
            stopping.cancel()
            await asyncio.sleep(0)
            self.assertTrue(manager.has_diagnostic(1))
            self.assertFalse(stopping.done())
            disconnect_gate.set()
            with self.assertRaises(asyncio.CancelledError):
                await stopping
            self.assertFalse(manager.has_diagnostic(1))
            self.assertFalse(channel.client.cache_owned)

    async def test_diagnostic_stop_cancellation_during_after_wait_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            channel = FakeVoiceChannel(defer_after=True)
            manager = VoiceSessionManager(config(data_dir=Path(tmp)))
            guild = SimpleNamespace(id=1, voice_client=None)
            session = await manager.start_diagnostic(
                guild=guild, voice_channel=channel
            )
            session._shutdown_step_seconds = 0.01

            stopping = asyncio.create_task(manager.stop_diagnostic(1))
            async with asyncio.timeout(0.5):
                while "discord.stop_listening" not in channel.log:
                    await asyncio.sleep(0)
            stopping.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await stopping
            self.assertFalse(manager.has_diagnostic(1))
            self.assertTrue(session._listener_done.done())

    async def test_stalled_disconnect_cleanup_releases_manager_for_restart(self) -> None:
        disconnect_gate = asyncio.Event()
        channel = FakeVoiceChannel(disconnect_gate=disconnect_gate)

        def session_factory(**kwargs):
            return VoiceSession(**kwargs, shutdown_step_seconds=0.01)

        manager = VoiceSessionManager(
            config(),
            gladia_factory=lambda *a, **k: FakeGladia(channel.log),
            session_factory=session_factory,
        )
        starter = SimpleNamespace(
            id=7,
            name="travis",
            display_name="Travis",
            voice=SimpleNamespace(channel=channel),
        )
        guild = SimpleNamespace(id=1, voice_client=None)
        for _ in range(2):
            await manager.start(
                guild=guild,
                voice_channel=channel,
                text_channel=FakeTextChannel(),
                starter=starter,
            )
            stopped = await manager.stop(1)
            self.assertEqual(stopped.state, VoiceSessionState.STOPPED)
            self.assertFalse(manager.has_live(1))
        self.assertEqual(channel.client.cleanup_calls, 2)

    async def test_stubborn_live_disconnect_is_hard_bounded_and_late_safe(self) -> None:
        release = asyncio.Event()
        channel = FakeVoiceChannel(
            suppress_disconnect_cancel=True,
            disconnect_release=release,
            late_cleanup_on_disconnect=True,
        )

        def session_factory(**kwargs):
            return VoiceSession(**kwargs, shutdown_step_seconds=0.01)

        manager = VoiceSessionManager(
            config(),
            gladia_factory=lambda *a, **k: FakeGladia(channel.log),
            session_factory=session_factory,
        )
        starter = SimpleNamespace(
            id=7,
            name="travis",
            display_name="Travis",
            voice=SimpleNamespace(channel=channel),
        )
        guild = SimpleNamespace(id=1, voice_client=None)
        await manager.start(
            guild=guild,
            voice_channel=channel,
            text_channel=FakeTextChannel(),
            starter=starter,
        )
        quarantined_before = set(
            voice_session_module._QUARANTINED_DISCONNECT_TASKS
        )
        loop = asyncio.get_running_loop()
        started = loop.time()
        with self.assertRaisesRegex(VoiceSessionError, "non-terminal"):
            await manager.stop(1)

        self.assertLess(loop.time() - started, 0.1)
        self.assertTrue(manager.has_live(1))
        self.assertEqual(manager._sessions[1].state, VoiceSessionState.FAILED)
        self.assertEqual(channel.client.cleanup_calls, 1)
        self.assertEqual(channel.voice_cache, {})
        quarantined = (
            set(voice_session_module._QUARANTINED_DISCONNECT_TASKS)
            - quarantined_before
        )
        self.assertEqual(len(quarantined), 1)

        for _ in range(25):
            with self.assertRaisesRegex(VoiceSessionError, "already"):
                await manager.start(
                    guild=guild,
                    voice_channel=channel,
                    text_channel=FakeTextChannel(),
                    starter=starter,
                )
            with self.assertRaisesRegex(VoiceSessionError, "non-terminal"):
                await manager.stop(1)
            self.assertEqual(
                set(voice_session_module._QUARANTINED_DISCONNECT_TASKS)
                - quarantined_before,
                quarantined,
            )
            self.assertEqual(channel.client.disconnect_calls, 1)

        replacement = FakeVoiceClient(channel, channel.log)
        replacement.cache_owned = True
        channel.guild.voice_client = replacement
        channel.voice_cache[channel.id] = replacement
        release.set()
        async with asyncio.timeout(0.5):
            while not all(task.done() for task in quarantined):
                await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertIs(channel.guild.voice_client, replacement)
        self.assertIs(channel.voice_cache[channel.id], replacement)
        self.assertEqual(channel.client.cleanup_calls, 1)
        stopped = await manager.stop(1)
        self.assertEqual(stopped.state, VoiceSessionState.STOPPED)
        self.assertFalse(manager.has_live(1))
        manager.validate_start(1)

    async def test_stubborn_disconnect_cleanup_failure_retains_manager_for_retry(self) -> None:
        release = asyncio.Event()
        channel = FakeVoiceChannel(
            suppress_disconnect_cancel=True,
            disconnect_release=release,
            late_cleanup_on_disconnect=True,
            cleanup_error=RuntimeError("cleanup failed"),
        )

        def session_factory(**kwargs):
            return VoiceSession(**kwargs, shutdown_step_seconds=0.01)

        manager = VoiceSessionManager(
            config(),
            gladia_factory=lambda *a, **k: FakeGladia(channel.log),
            session_factory=session_factory,
        )
        starter = SimpleNamespace(
            id=7,
            name="travis",
            display_name="Travis",
            voice=SimpleNamespace(channel=channel),
        )
        guild = SimpleNamespace(id=1, voice_client=None)
        await manager.start(
            guild=guild,
            voice_channel=channel,
            text_channel=FakeTextChannel(),
            starter=starter,
        )
        loop = asyncio.get_running_loop()
        started = loop.time()
        with self.assertRaisesRegex(VoiceSessionError, "non-terminal"):
            await manager.stop(1)
        self.assertLess(loop.time() - started, 0.1)
        self.assertTrue(manager.has_live(1))
        self.assertEqual(manager._sessions[1].state, VoiceSessionState.FAILED)

        channel.client.cleanup_error = None
        started = loop.time()
        with self.assertRaisesRegex(VoiceSessionError, "non-terminal"):
            await manager.stop(1)
        self.assertLess(loop.time() - started, 0.1)
        self.assertTrue(manager.has_live(1))
        replacement = FakeVoiceClient(channel, channel.log)
        replacement.cache_owned = True
        channel.guild.voice_client = replacement
        channel.voice_cache[channel.id] = replacement
        release.set()
        async with asyncio.timeout(0.5):
            while voice_session_module._QUARANTINED_DISCONNECT_TASKS:
                await asyncio.sleep(0)
        self.assertIs(channel.guild.voice_client, replacement)
        self.assertIs(channel.voice_cache[channel.id], replacement)
        stopped = await manager.stop(1)
        self.assertEqual(stopped.state, VoiceSessionState.STOPPED)
        self.assertFalse(manager.has_live(1))

    async def test_quarantined_disconnect_late_error_is_observed_and_redacted(self) -> None:
        secret = "late-disconnect-secret"
        release = asyncio.Event()
        channel = FakeVoiceChannel(
            suppress_disconnect_cancel=True,
            disconnect_release=release,
            late_disconnect_error=RuntimeError(
                f"Authorization: bEaReR {secret}"
            ),
        )

        def session_factory(**kwargs):
            return VoiceSession(**kwargs, shutdown_step_seconds=0.01)

        manager = VoiceSessionManager(
            config(),
            gladia_factory=lambda *a, **k: FakeGladia(channel.log),
            session_factory=session_factory,
        )
        starter = SimpleNamespace(
            id=7,
            name="travis",
            display_name="Travis",
            voice=SimpleNamespace(channel=channel),
        )
        guild = SimpleNamespace(id=1, voice_client=None)
        await manager.start(
            guild=guild,
            voice_channel=channel,
            text_channel=FakeTextChannel(),
            starter=starter,
        )
        with self.assertRaises(VoiceSessionError):
            await manager.stop(1)
        self.assertTrue(manager.has_live(1))

        with self.assertLogs(
            "disco_proxy_soul.discord_app.voice_session", level="ERROR"
        ) as captured:
            release.set()
            async with asyncio.timeout(0.5):
                while voice_session_module._QUARANTINED_DISCONNECT_TASKS:
                    await asyncio.sleep(0)
            await asyncio.sleep(0)
        rendered = "\n".join(captured.output)
        self.assertNotIn(secret, rendered)
        self.assertIn("RuntimeError", rendered)
        stopped = await manager.stop(1)
        self.assertEqual(stopped.state, VoiceSessionState.STOPPED)
        self.assertFalse(manager.has_live(1))

    async def test_stubborn_diagnostic_disconnect_is_hard_bounded_and_late_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            release = asyncio.Event()
            channel = FakeVoiceChannel(
                suppress_disconnect_cancel=True,
                disconnect_release=release,
                late_cleanup_on_disconnect=True,
            )
            manager = VoiceSessionManager(config(data_dir=Path(tmp)))
            guild = SimpleNamespace(id=1, voice_client=None)
            session = await manager.start_diagnostic(
                guild=guild, voice_channel=channel
            )
            session._shutdown_step_seconds = 0.01
            quarantined_before = set(
                voice_session_module._QUARANTINED_DISCONNECT_TASKS
            )
            loop = asyncio.get_running_loop()
            started = loop.time()
            with self.assertRaisesRegex(VoiceSessionError, "retry stop"):
                await manager.stop_diagnostic(1)

            self.assertLess(loop.time() - started, 0.1)
            self.assertTrue(manager.has_diagnostic(1))
            self.assertEqual(channel.client.cleanup_calls, 1)
            self.assertEqual(channel.voice_cache, {})
            quarantined = (
                set(voice_session_module._QUARANTINED_DISCONNECT_TASKS)
                - quarantined_before
            )
            self.assertEqual(len(quarantined), 1)

            for _ in range(5):
                with self.assertRaisesRegex(VoiceSessionError, "already"):
                    await manager.start_diagnostic(
                        guild=guild, voice_channel=channel
                    )
                with self.assertRaisesRegex(VoiceSessionError, "retry stop"):
                    await manager.stop_diagnostic(1)
                self.assertEqual(
                    set(voice_session_module._QUARANTINED_DISCONNECT_TASKS)
                    - quarantined_before,
                    quarantined,
                )
                self.assertEqual(channel.client.disconnect_calls, 1)

            replacement = FakeVoiceClient(channel, channel.log)
            replacement.cache_owned = True
            channel.guild.voice_client = replacement
            channel.voice_cache[channel.id] = replacement
            release.set()
            async with asyncio.timeout(0.5):
                while not all(task.done() for task in quarantined):
                    await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.assertIs(channel.guild.voice_client, replacement)
            self.assertIs(channel.voice_cache[channel.id], replacement)
            self.assertEqual(channel.client.cleanup_calls, 1)
            stopped = await manager.stop_diagnostic(1)
            self.assertIsNotNone(stopped)
            self.assertFalse(manager.has_diagnostic(1))
            manager.validate_diagnostic_start(1)

    async def test_diagnostic_mutual_exclusion_and_cleanup_failure_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            channel = FakeVoiceChannel(
                disconnect_error=RuntimeError("disconnect failed"),
                cleanup_error=RuntimeError("cleanup failed"),
            )
            manager = VoiceSessionManager(config(data_dir=Path(tmp)))
            guild = SimpleNamespace(id=1, voice_client=None)
            await manager.start_diagnostic(guild=guild, voice_channel=channel)
            with self.assertRaisesRegex(VoiceSessionError, "diagnostic"):
                manager.validate_start(1)
            with self.assertRaisesRegex(VoiceSessionError, "already"):
                manager.validate_diagnostic_start(1)

            with self.assertRaisesRegex(VoiceSessionError, "retry stop"):
                await manager.stop_diagnostic(1)
            self.assertTrue(manager.has_diagnostic(1))
            channel.client.cleanup_error = None
            stopped = await manager.stop_diagnostic(1)
            self.assertIsNotNone(stopped)
            self.assertFalse(manager.has_diagnostic(1))
            manager.validate_start(1)


class DiagnosticCommandAsyncTests(unittest.IsolatedAsyncioTestCase):
    def command_tree(self, manager):
        client = discord.Client(intents=discord.Intents.none())
        tree = app_commands.CommandTree(client)
        app = MagicMock()
        app.persona = SimpleNamespace(companion_name="Naomi", partner_name="Travis")
        app.catalog = {}
        register_commands(tree, app, manager)
        return tree

    async def test_start_defer_failure_never_reserves_diagnostic(self) -> None:
        manager = MagicMock(spec=VoiceSessionManager)
        manager.validate_diagnostic_start.return_value = None
        manager.start_diagnostic = AsyncMock()
        tree = self.command_tree(manager)
        response = SimpleNamespace(
            defer=AsyncMock(side_effect=RuntimeError("ack failed")),
            send_message=AsyncMock(),
        )
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            user=SimpleNamespace(voice=SimpleNamespace(channel=SimpleNamespace(id=22))),
            response=response,
            followup=SimpleNamespace(send=AsyncMock()),
        )

        with self.assertRaisesRegex(RuntimeError, "ack failed"):
            await tree.get_command("voice-record").callback(interaction)
        manager.start_diagnostic.assert_not_awaited()

    async def test_start_notice_cancellation_has_no_diagnostic_side_effects(self) -> None:
        manager = MagicMock(spec=VoiceSessionManager)
        manager.validate_diagnostic_start.return_value = None
        manager.start_diagnostic = AsyncMock()
        manager.has_diagnostic.return_value = False
        manager.stop_diagnostic = AsyncMock()
        tree = self.command_tree(manager)
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            user=SimpleNamespace(voice=SimpleNamespace(channel=SimpleNamespace(id=22))),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(
                send=AsyncMock(side_effect=asyncio.CancelledError)
            ),
        )

        with self.assertRaises(asyncio.CancelledError):
            await tree.get_command("voice-record").callback(interaction)
        manager.start_diagnostic.assert_not_awaited()
        manager.stop_diagnostic.assert_not_awaited()

    async def test_post_start_confirmation_cancellation_closes_diagnostic(self) -> None:
        manager = MagicMock(spec=VoiceSessionManager)
        manager.validate_diagnostic_start.return_value = None
        manager.start_diagnostic = AsyncMock()
        manager.has_diagnostic.return_value = True
        manager.stop_diagnostic = AsyncMock()
        tree = self.command_tree(manager)
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            user=SimpleNamespace(voice=SimpleNamespace(channel=SimpleNamespace(id=22))),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(
                send=AsyncMock(side_effect=[None, asyncio.CancelledError()])
            ),
        )

        with self.assertRaises(asyncio.CancelledError):
            await tree.get_command("voice-record").callback(interaction)
        manager.start_diagnostic.assert_awaited_once()
        manager.stop_diagnostic.assert_awaited_once_with(1)

    async def test_failed_privacy_notice_creates_no_client_registry_or_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = VoiceSessionManager(config(data_dir=Path(tmp)))
            tree = self.command_tree(manager)
            channel = FakeVoiceChannel()
            interaction = SimpleNamespace(
                guild=SimpleNamespace(id=1, voice_client=None),
                user=SimpleNamespace(voice=SimpleNamespace(channel=channel)),
                response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
                followup=SimpleNamespace(
                    send=AsyncMock(side_effect=RuntimeError("notice failed"))
                ),
            )

            with self.assertRaisesRegex(RuntimeError, "notice failed"):
                await tree.get_command("voice-record").callback(interaction)
            self.assertFalse(manager.has_diagnostic(1))
            self.assertIsNone(channel.connect_kwargs)
            self.assertFalse((Path(tmp) / "voice-captures").exists())

    async def test_stalled_privacy_notice_is_inert_until_visible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = VoiceSessionManager(config(data_dir=Path(tmp)))
            tree = self.command_tree(manager)
            channel = FakeVoiceChannel()
            entered = asyncio.Event()
            gate = asyncio.Event()

            async def stalled_send(*args, **kwargs):
                entered.set()
                await gate.wait()

            interaction = SimpleNamespace(
                guild=SimpleNamespace(id=1, voice_client=None),
                user=SimpleNamespace(voice=SimpleNamespace(channel=channel)),
                response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
                followup=SimpleNamespace(send=stalled_send),
            )
            command = asyncio.create_task(
                tree.get_command("voice-record").callback(interaction)
            )
            await entered.wait()
            self.assertFalse(manager.has_diagnostic(1))
            self.assertIsNone(channel.connect_kwargs)
            self.assertFalse((Path(tmp) / "voice-captures").exists())
            command.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await command

    async def test_stop_defer_failure_keeps_reachable_diagnostic_handle(self) -> None:
        manager = MagicMock(spec=VoiceSessionManager)
        manager.has_diagnostic.return_value = True
        manager.stop_diagnostic = AsyncMock()
        tree = self.command_tree(manager)
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            response=SimpleNamespace(
                defer=AsyncMock(side_effect=RuntimeError("ack failed")),
                send_message=AsyncMock(),
            ),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        with self.assertRaisesRegex(RuntimeError, "ack failed"):
            await tree.get_command("voice-stop").callback(interaction)
        manager.stop_diagnostic.assert_not_awaited()
        self.assertTrue(manager.has_diagnostic(1))


if __name__ == "__main__":
    unittest.main()
