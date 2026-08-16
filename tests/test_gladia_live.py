from __future__ import annotations

import asyncio
import json
from pathlib import Path
import struct
import tempfile
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

import aiohttp

from disco_proxy_soul.adapters import gladia_live as gladia_live_module
from disco_proxy_soul.adapters.gladia_live import (
    AcknowledgmentEvent,
    CompletionState,
    GladiaErrorEvent,
    GladiaCriticalEventOverflow,
    GladiaInitError,
    GladiaEventStreamClosed,
    GladiaLiveConfig,
    GladiaLiveResult,
    GladiaLiveSession,
    GladiaTransportError,
    LifecycleEvent,
    SpeechEvent,
    SessionState,
    TranscriptUpdate,
    WaveFormat,
    build_init_payload,
    inspect_pcm_wave,
    parse_gladia_message,
    parse_transcript_message,
    pcm16_stereo_to_mono,
    redact_sensitive_text,
)


def transcript_message(
    utterance_id: str = "00-00000011",
    *,
    text: str = "Hello Atlas.",
    is_final: bool = True,
) -> dict:
    return {
        "session_id": "session-1",
        "created_at": "2026-08-15T12:34:10Z",
        "type": "transcript",
        "data": {
            "id": utterance_id,
            "is_final": is_final,
            "utterance": {
                "start": 1.0,
                "end": 1.48,
                "confidence": 0.91,
                "channel": 0,
                "words": [
                    {
                        "word": "Hello",
                        "start": 1.0,
                        "end": 1.35,
                        "confidence": 0.91,
                    }
                ],
                "text": text,
                "language": "en",
                "speaker": 2,
            },
        },
    }


class FakeResponse:
    status = 422

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def text(self) -> str:
        return (
            "validation failed for api-key=gladia-secret-key at "
            "wss://api.gladia.io/v2/live?token=bearer-secret"
        )


class FakeHttpSession:
    def post(self, *args, **kwargs):
        return FakeResponse()


class FakeSuccessResponse(FakeResponse):
    status = 201

    async def json(self) -> dict:
        return {
            "id": "init-session-id",
            "url": "wss://api.gladia.io/v2/live?token=never-log-this",
        }


class FakeWebSocket:
    def __init__(
        self,
        *,
        auto_end: bool = True,
        send_error: Exception | None = None,
        cancel_close: bool = False,
        stall_send_bytes: bool = False,
        stall_send_json: bool = False,
        stop_error: Exception | None = None,
        close_error: Exception | None = None,
        websocket_error: Exception | None = None,
        close_release: asyncio.Event | None = None,
    ) -> None:
        self.closed = False
        self.close_code = 1000
        self.auto_end = auto_end
        self.send_error = send_error
        self.cancel_close = cancel_close
        self.stall_send_bytes = stall_send_bytes
        self.stall_send_json = stall_send_json
        self.stop_error = stop_error
        self.close_error = close_error
        self.websocket_error = websocket_error
        self.close_release = close_release
        self.send_started = asyncio.Event()
        self.stop_started = asyncio.Event()
        self.close_started = asyncio.Event()
        self.sent_bytes: list[bytes] = []
        self.sent_json: list[dict] = []
        self._messages: asyncio.Queue = asyncio.Queue()
        self.force_closed = False
        self._response = SimpleNamespace(close=self._force_close)

    def _force_close(self) -> None:
        self.force_closed = True
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self._messages.get()
        if message is None:
            raise StopAsyncIteration
        return message

    async def send_bytes(self, chunk: bytes) -> None:
        self.send_started.set()
        if self.stall_send_bytes:
            await asyncio.Future()
        if self.send_error is not None:
            raise self.send_error
        self.sent_bytes.append(chunk)

    async def send_json(self, payload: dict) -> None:
        self.sent_json.append(payload)
        self.stop_started.set()
        if self.stall_send_json:
            await asyncio.Future()
        if self.stop_error is not None:
            raise self.stop_error
        if payload == {"type": "stop_recording"} and self.auto_end:
            await self._messages.put(
                SimpleNamespace(
                    type=aiohttp.WSMsgType.TEXT,
                    data=json.dumps(
                        {
                            "session_id": "init-session-id",
                            "created_at": "2026-08-15T12:34:10Z",
                            "type": "end_session",
                        }
                    ),
                )
            )

    async def close(self) -> None:
        self.close_started.set()
        if self.close_release is not None:
            await self.close_release.wait()
        self.closed = True
        await self._messages.put(None)
        if self.cancel_close:
            raise asyncio.CancelledError
        if self.close_error is not None:
            raise self.close_error

    async def push(self, message_type: aiohttp.WSMsgType, data: str = "") -> None:
        await self._messages.put(SimpleNamespace(type=message_type, data=data))

    def exception(self):
        return self.websocket_error


class FakeSuccessHttpSession:
    def __init__(self, websocket: FakeWebSocket | None = None) -> None:
        self.websocket = websocket or FakeWebSocket()
        self.closed = False

    def post(self, *args, **kwargs):
        return FakeSuccessResponse()

    async def ws_connect(self, url: str, *, heartbeat: int):
        return self.websocket

    async def close(self) -> None:
        self.closed = True


class SequencedWebSocketHttpSession(FakeSuccessHttpSession):
    def __init__(self, *outcomes) -> None:
        super().__init__(None)
        self.outcomes = list(outcomes)
        self.connected_urls: list[str] = []

    async def ws_connect(self, url: str, *, heartbeat: int):
        self.connected_urls.append(url)
        if not self.outcomes:
            raise RuntimeError("no configured WebSocket outcome")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class BlockingReconnectHttpSession(FakeSuccessHttpSession):
    def __init__(self, first: FakeWebSocket) -> None:
        super().__init__(first)
        self.connect_calls = 0
        self.reconnect_started = asyncio.Event()

    async def ws_connect(self, url: str, *, heartbeat: int):
        self.connect_calls += 1
        if self.connect_calls == 1:
            return self.websocket
        self.reconnect_started.set()
        await asyncio.Future()


class BlockingRequest:
    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def __aenter__(self):
        self.entered.set()
        await asyncio.Future()

    async def __aexit__(self, exc_type, exc, traceback):
        return None


class BlockingPostHttpSession(FakeSuccessHttpSession):
    def __init__(self) -> None:
        super().__init__()
        self.request = BlockingRequest()

    def post(self, *args, **kwargs):
        return self.request


class BlockingConnectHttpSession(FakeSuccessHttpSession):
    def __init__(self) -> None:
        super().__init__()
        self.connect_started = asyncio.Event()

    async def ws_connect(self, url: str, *, heartbeat: int):
        self.connect_started.set()
        await asyncio.Future()


class GatedConnectHttpSession(FakeSuccessHttpSession):
    def __init__(self, websocket: FakeWebSocket | None = None) -> None:
        super().__init__(websocket)
        self.connect_started = asyncio.Event()
        self.connect_release = asyncio.Event()

    async def ws_connect(self, url: str, *, heartbeat: int):
        self.connect_started.set()
        await self.connect_release.wait()
        return self.websocket


class FailingConnectHttpSession(FakeSuccessHttpSession):
    async def ws_connect(self, url: str, *, heartbeat: int):
        raise RuntimeError(
            "connect failed key=gladia-secret-key "
            "wss://api.gladia.io/v2/live?token=never-log-this"
        )


class FakeReplaySession:
    def __init__(self, events: list[TranscriptUpdate] | None = None) -> None:
        self.events = events or []
        self.sent: list[bytes] = []
        self.stop_calls = 0
        self.result = GladiaLiveResult(
            finals=[event.text for event in self.events if event.is_final],
            completion=CompletionState.NORMAL,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        await self.stop()

    async def send_pcm(self, chunk: bytes) -> None:
        self.sent.append(chunk)
        await asyncio.sleep(0)

    async def stop(self):
        self.stop_calls += 1
        return self.result

    async def iter_events(self):
        for event in self.events:
            yield event


class GladiaLiveTests(unittest.TestCase):
    def test_inspect_pcm_wave(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.wav"
            with wave.open(str(path), "wb") as writer:
                writer.setnchannels(2)
                writer.setsampwidth(2)
                writer.setframerate(48_000)
                writer.writeframes(b"\x00\x00\x00\x00" * 960)

            audio_format = inspect_pcm_wave(path)

        self.assertEqual(audio_format.sample_rate, 48_000)
        self.assertEqual(audio_format.channels, 2)
        self.assertEqual(audio_format.bit_depth, 16)
        self.assertEqual(audio_format.frame_count, 960)
        self.assertAlmostEqual(audio_format.duration_seconds, 0.02)

    def test_build_init_payload_matches_wave_format(self) -> None:
        payload = build_init_payload(
            WaveFormat(48_000, 2, 16, 960),
            GladiaLiveConfig(audio_enhancer=True, speech_threshold=0.7),
        )

        self.assertEqual(payload["encoding"], "wav/pcm")
        self.assertEqual(payload["sample_rate"], 48_000)
        self.assertEqual(payload["channels"], 2)
        self.assertTrue(payload["pre_processing"]["audio_enhancer"])
        self.assertEqual(payload["pre_processing"]["speech_threshold"], 0.7)
        self.assertEqual(payload["endpointing"], 0.1)
        self.assertTrue(payload["messages_config"]["receive_acknowledgments"])

    def test_parse_complete_partial_and_final_transcripts(self) -> None:
        partial_payload = transcript_message(text="Hello Atl", is_final=False)
        partial = parse_transcript_message(json.dumps(partial_payload))
        final = parse_transcript_message(transcript_message())

        self.assertIsNotNone(partial)
        self.assertEqual(partial.text, "Hello Atl")
        self.assertFalse(partial.is_final)
        self.assertIsNotNone(final)
        self.assertEqual(final.text, "Hello Atlas.")
        self.assertTrue(final.is_final)
        self.assertEqual(final.session_id, "session-1")
        self.assertEqual(final.utterance_id, "00-00000011")
        self.assertEqual(final.start, 1.0)
        self.assertEqual(final.end, 1.48)
        self.assertEqual(final.confidence, 0.91)
        self.assertEqual(final.language, "en")
        self.assertEqual(final.channel, 0)
        self.assertEqual(final.speaker, 2)
        self.assertEqual(final.words[0].word, "Hello")
        self.assertEqual(final.words[0].start, 1.0)
        self.assertEqual(final.words[0].end, 1.35)
        self.assertEqual(final.words[0].confidence, 0.91)

    def test_ignores_non_transcript_messages(self) -> None:
        self.assertIsNone(parse_transcript_message({"type": "speech_start"}))

    def test_parses_speech_lifecycle_and_acknowledgment(self) -> None:
        envelope = {
            "session_id": "session-1",
            "created_at": "2026-08-15T12:34:10Z",
        }
        speech = parse_gladia_message(
            {**envelope, "type": "speech_start", "data": {"time": 1.24, "channel": 0}}
        )
        speech_end = parse_gladia_message(
            {**envelope, "type": "speech_end", "data": {"time": 3.1, "channel": 0}}
        )
        start_session = parse_gladia_message({**envelope, "type": "start_session"})
        start_recording = parse_gladia_message({**envelope, "type": "start_recording"})
        lifecycle = parse_gladia_message(
            {
                **envelope,
                "type": "end_recording",
                "data": {"reason": "user_request", "received_total_bytes": 4096},
            }
        )
        acknowledgment = parse_gladia_message(
            {
                **envelope,
                "type": "audio_chunk",
                "acknowledged": True,
                "error": None,
                "data": {"byte_range": [0, 4095], "time_range": [0, 0.256]},
            }
        )

        self.assertIsInstance(speech, SpeechEvent)
        self.assertEqual(speech.time, 1.24)
        self.assertIsInstance(speech_end, SpeechEvent)
        self.assertEqual(speech_end.event_type, "speech_end")
        self.assertIsInstance(start_session, LifecycleEvent)
        self.assertEqual(start_session.event_type, "start_session")
        self.assertIsInstance(start_recording, LifecycleEvent)
        self.assertEqual(start_recording.event_type, "start_recording")
        self.assertIsInstance(lifecycle, LifecycleEvent)
        self.assertEqual(lifecycle.reason, "user_request")
        self.assertEqual(lifecycle.received_total_bytes, 4096)
        self.assertIsInstance(acknowledgment, AcknowledgmentEvent)
        self.assertEqual(acknowledgment.byte_range, (0, 4095))
        self.assertEqual(acknowledgment.time_range, (0.0, 0.256))

    def test_parses_stop_acknowledgment_and_top_level_error(self) -> None:
        envelope = {
            "session_id": "session-1",
            "created_at": "2026-08-15T12:34:10Z",
        }
        acknowledgment = parse_gladia_message(
            {
                **envelope,
                "type": "stop_recording",
                "acknowledged": True,
                "error": None,
                "data": {},
            }
        )
        error = parse_gladia_message(
            {
                **envelope,
                "type": "audio_chunk",
                "acknowledged": False,
                "error": {"message": "invalid chunk"},
                "data": {},
            }
        )

        self.assertIsInstance(acknowledgment, AcknowledgmentEvent)
        self.assertEqual(acknowledgment.action, "stop_recording")
        self.assertIsInstance(error, GladiaErrorEvent)
        self.assertEqual(error.message, "invalid chunk")
        self.assertEqual(error.source_type, "audio_chunk")

    def test_parses_error_type_and_malformed_messages(self) -> None:
        error = parse_gladia_message(
            {"type": "error", "data": {"code": "bad_audio", "message": "Nope"}}
        )
        invalid_json = parse_gladia_message("{definitely-not-json")
        invalid_transcript = parse_gladia_message(
            {"type": "transcript", "data": {"is_final": True}}
        )

        self.assertIsInstance(error, GladiaErrorEvent)
        self.assertEqual(error.code, "bad_audio")
        self.assertEqual(error.message, "Nope")
        self.assertIsInstance(invalid_json, GladiaErrorEvent)
        self.assertEqual(invalid_json.source_type, "malformed_message")
        self.assertIsInstance(invalid_transcript, GladiaErrorEvent)
        self.assertIn("Invalid Gladia message", invalid_transcript.message)

    def test_session_deduplicates_finals_by_session_and_utterance(self) -> None:
        session = GladiaLiveSession("secret", WaveFormat(48_000, 1, 16))

        first = session.process_message(transcript_message())
        duplicate = session.process_message(transcript_message())
        other_session = transcript_message()
        other_session["session_id"] = "session-2"
        second = session.process_message(other_session)

        self.assertIsInstance(first, TranscriptUpdate)
        self.assertIsNone(duplicate)
        self.assertIsInstance(second, TranscriptUpdate)
        self.assertEqual(session.result.finals, ["Hello Atlas.", "Hello Atlas."])

    def test_session_keeps_bounded_latest_partial_state(self) -> None:
        session = GladiaLiveSession(
            "secret",
            WaveFormat(48_000, 1, 16),
            config=GladiaLiveConfig(partial_state_limit=2),
        )
        session.process_message(transcript_message("one", text="one", is_final=False))
        session.process_message(transcript_message("two", text="two", is_final=False))
        session.process_message(transcript_message("one", text="one updated", is_final=False))
        session.process_message(transcript_message("three", text="three", is_final=False))

        self.assertEqual(
            [update.utterance_id for update in session.latest_partials],
            ["one", "three"],
        )
        self.assertEqual(session.result.partials, ["one updated", "three"])

    def test_session_marks_end_session_normal_and_other_close_abnormal(self) -> None:
        normal = GladiaLiveSession("secret", WaveFormat(48_000, 1, 16))
        event = normal.process_message(
            {
                "session_id": "session-1",
                "created_at": "2026-08-15T12:34:10Z",
                "type": "end_session",
            }
        )
        abnormal = GladiaLiveSession("secret", WaveFormat(48_000, 1, 16))
        abnormal._mark_abnormal("transport closed")

        self.assertIsInstance(event, LifecycleEvent)
        self.assertEqual(normal.completion, CompletionState.NORMAL)
        self.assertEqual(abnormal.completion, CompletionState.ABNORMAL)
        self.assertIn("transport closed", abnormal.result.errors)

    def test_completion_is_monotonic_in_both_sequences(self) -> None:
        abnormal_first = GladiaLiveSession("secret", WaveFormat(48_000, 1, 16))
        abnormal_first._mark_abnormal("transport failed")
        abnormal_first.process_message(
            {
                "session_id": "session-1",
                "created_at": "2026-08-15T12:34:10Z",
                "type": "end_session",
            }
        )
        normal_first = GladiaLiveSession("secret", WaveFormat(48_000, 1, 16))
        normal_first.process_message(
            {
                "session_id": "session-1",
                "created_at": "2026-08-15T12:34:10Z",
                "type": "end_session",
            }
        )
        normal_first._mark_abnormal("late close noise")

        self.assertEqual(abnormal_first.completion, CompletionState.ABNORMAL)
        self.assertEqual(abnormal_first.result.completion_reason, "transport failed")
        self.assertEqual(normal_first.completion, CompletionState.NORMAL)
        self.assertNotIn("late close noise", normal_first.result.errors)

    def test_event_delivery_is_bounded_and_preserves_critical_events(self) -> None:
        session = GladiaLiveSession(
            "secret",
            WaveFormat(48_000, 1, 16),
            config=GladiaLiveConfig(event_queue_limit=8),
        )
        envelope = {
            "session_id": "session-1",
            "created_at": "2026-08-15T12:34:10Z",
        }
        for index in range(100):
            session.process_message(
                transcript_message("partial", text=f"partial {index}", is_final=False)
            )
            session.process_message(
                {
                    **envelope,
                    "type": "audio_chunk",
                    "acknowledged": True,
                    "error": None,
                    "data": {"byte_range": [index, index + 1]},
                }
            )
        session.process_message(transcript_message("final", text="stable", is_final=True))
        session.process_message(
            {**envelope, "type": "error", "data": {"message": "critical error"}}
        )
        session.process_message({**envelope, "type": "end_session"})

        self.assertLessEqual(session.event_queue_size, 8)
        self.assertGreater(session.coalesced_event_count, 0)
        self.assertGreater(session.dropped_event_count, 0)
        queued = list(session._events._items)
        self.assertTrue(
            any(isinstance(event, TranscriptUpdate) and event.is_final for event in queued)
        )
        self.assertTrue(any(isinstance(event, GladiaErrorEvent) for event in queued))
        self.assertTrue(
            any(
                isinstance(event, LifecycleEvent) and event.event_type == "end_session"
                for event in queued
            )
        )

    def test_unique_final_overflow_fails_explicitly_without_silent_eviction(self) -> None:
        session = GladiaLiveSession(
            "secret",
            WaveFormat(48_000, 1, 16),
            config=GladiaLiveConfig(event_queue_limit=4),
        )
        session.process_message(transcript_message("one", text="one", is_final=True))
        session.process_message(transcript_message("two", text="two", is_final=True))

        with self.assertRaises(GladiaCriticalEventOverflow):
            session.process_message(
                transcript_message("three", text="three", is_final=True)
            )

        queued = list(session._events._items)
        delivered_finals = [
            event.text
            for event in queued
            if isinstance(event, TranscriptUpdate) and event.is_final
        ]
        overflow_errors = [
            event
            for event in queued
            if isinstance(event, GladiaErrorEvent)
            and event.code == "critical_event_overflow"
        ]
        self.assertEqual(delivered_finals, ["one", "two"])
        self.assertEqual(session.result.finals, ["one", "two", "three"])
        self.assertEqual(len(overflow_errors), 1)
        self.assertEqual(session.critical_overflow_count, 1)
        self.assertEqual(session.completion, CompletionState.ABNORMAL)
        self.assertLessEqual(session.event_queue_size, 4)
        with self.assertRaises(GladiaCriticalEventOverflow):
            session.process_message(
                transcript_message("four", text="four", is_final=True)
            )

    def test_error_lifecycle_and_eos_pressure_has_explicit_overflow(self) -> None:
        session = GladiaLiveSession(
            "secret",
            WaveFormat(48_000, 1, 16),
            config=GladiaLiveConfig(event_queue_limit=6),
        )
        envelope = {
            "session_id": "session-1",
            "created_at": "2026-08-15T12:34:10Z",
        }
        session.process_message({**envelope, "type": "start_session"})
        session.process_message({**envelope, "type": "start_recording"})
        session.process_message(
            {**envelope, "type": "error", "data": {"message": "first"}}
        )
        session.process_message(
            {**envelope, "type": "error", "data": {"message": "second"}}
        )

        with self.assertRaises(GladiaCriticalEventOverflow):
            session.process_message(
                {**envelope, "type": "end_recording", "data": {"reason": "limit"}}
            )
        session._events.publish_eos()
        session._events.publish_eos()

        queued = list(session._events._items)
        self.assertEqual(session.critical_overflow_count, 1)
        self.assertEqual(
            sum(
                isinstance(event, GladiaErrorEvent)
                and event.code == "critical_event_overflow"
                for event in queued
            ),
            1,
        )
        self.assertEqual(
            sum(event is gladia_live_module._EVENT_STREAM_EOS for event in queued), 1
        )
        self.assertLessEqual(len(queued), 6)

    def test_redacts_keys_token_values_and_tokenized_urls(self) -> None:
        api_key = "gladia-secret-key"
        websocket_url = "wss://api.gladia.io/v2/live?token=bearer-secret"
        diagnostic = redact_sensitive_text(
            f"key={api_key} url={websocket_url} token: another-secret",
            api_key,
        )

        self.assertNotIn(api_key, diagnostic)
        self.assertNotIn("bearer-secret", diagnostic)
        self.assertNotIn("another-secret", diagnostic)
        self.assertNotIn(websocket_url, diagnostic)

    def test_redacts_authorization_and_standalone_bearer_case_insensitively(self) -> None:
        secret = "credential-value"
        diagnostic = redact_sensitive_text(
            f"upstream Authorization: Bearer {secret}; retry bEaReR {secret} now"
        )

        self.assertEqual(
            diagnostic,
            "upstream Authorization: Bearer <redacted>; retry bEaReR <redacted> now",
        )
        self.assertNotIn(secret, diagnostic)

    def test_gladia_error_event_and_result_store_only_redacted_bearer(self) -> None:
        secret = "credential-value"
        session = GladiaLiveSession(
            "api-key",
            WaveFormat(48_000, 1, 16),
            http_session=FakeSuccessHttpSession(),
        )
        event = session.process_message(
            {
                "type": "error",
                "data": {
                    "message": f"Authorization: Bearer {secret}",
                    "code": "auth",
                },
            }
        )

        self.assertIsInstance(event, GladiaErrorEvent)
        self.assertNotIn(secret, event.message)
        self.assertNotIn(secret, "\n".join(session.result.errors))
        self.assertIn("Bearer <redacted>", event.message)

    def test_downmixes_stereo_pcm16_without_overflow(self) -> None:
        stereo = struct.pack(
            "<hhhhhhhh",
            1_000,
            3_000,
            -1_000,
            -3_000,
            32_767,
            32_767,
            -32_768,
            -32_768,
        )

        mono = struct.unpack("<hhhh", pcm16_stereo_to_mono(stereo))

        self.assertEqual(mono, (2_000, -2_000, 32_767, -32_768))


class GladiaLiveAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        await asyncio.sleep(0)
        leaked = [
            task.get_name()
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and not task.done()
            and (
                task.get_name().startswith("gladia-live-receiver-")
                or task.get_name() == "gladia-wave-transcript-consumer"
            )
        ]
        self.assertEqual(leaked, [], f"Gladia tasks leaked: {leaked}")

    async def test_reusable_session_connect_send_receive_and_stop(self) -> None:
        http = FakeSuccessHttpSession()
        session = GladiaLiveSession(
            "gladia-secret-key",
            WaveFormat(48_000, 1, 16),
            http_session=http,
        )

        connected = await session.connect()
        await session.send_pcm(b"\x01\x02")
        result = await session.stop(timeout=1)
        event = await session.receive_event()

        self.assertIs(connected, session)
        self.assertEqual(session.session_id, "init-session-id")
        self.assertEqual(result.session_id, "init-session-id")
        self.assertEqual(http.websocket.sent_bytes, [b"\x01\x02"])
        self.assertEqual(http.websocket.sent_json, [{"type": "stop_recording"}])
        self.assertIsInstance(event, LifecycleEvent)
        self.assertEqual(event.event_type, "end_session")
        self.assertEqual(result.completion, CompletionState.NORMAL)
        self.assertFalse(http.closed, "an injected HTTP session must not be closed")
        self.assertEqual(session.state, SessionState.STOPPED)

    async def test_connect_surfaces_useful_redacted_init_error(self) -> None:
        session = GladiaLiveSession(
            "gladia-secret-key",
            WaveFormat(48_000, 1, 16),
            http_session=FakeHttpSession(),
        )

        with self.assertRaises(GladiaInitError) as raised:
            await session.connect()

        diagnostic = str(raised.exception)
        formatted = "".join(traceback.format_exception(raised.exception))
        self.assertIn("HTTP 422", diagnostic)
        self.assertIn("validation failed", diagnostic)
        self.assertIsNone(raised.exception.__cause__)
        for secret in ("gladia-secret-key", "bearer-secret", "wss://"):
            self.assertNotIn(secret, diagnostic)
            self.assertNotIn(secret, formatted)

    async def test_connect_redacts_websocket_exception_chain(self) -> None:
        session = GladiaLiveSession(
            "gladia-secret-key",
            WaveFormat(48_000, 1, 16),
            http_session=FailingConnectHttpSession(),
        )

        with self.assertRaises(GladiaInitError) as raised:
            await session.connect()

        formatted = "".join(traceback.format_exception(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertNotIn("gladia-secret-key", formatted)
        self.assertNotIn("never-log-this", formatted)
        self.assertNotIn("wss://", formatted)

    async def test_event_iteration_terminates_after_public_event_already_drained(self) -> None:
        session = GladiaLiveSession(
            "secret",
            WaveFormat(48_000, 1, 16),
            http_session=FakeSuccessHttpSession(),
        )
        await session.connect()
        await session.stop(timeout=1)
        drained = await session.receive_event()

        remaining = await asyncio.wait_for(
            self._collect_events(session.iter_events()), timeout=1
        )

        self.assertIsInstance(drained, LifecycleEvent)
        self.assertEqual(remaining, [])
        self.assertTrue(session._events._eos_published)

    async def test_abnormal_close_publishes_error_then_eos(self) -> None:
        websocket = FakeWebSocket(auto_end=False)
        session = GladiaLiveSession(
            "secret",
            WaveFormat(48_000, 1, 16),
            http_session=FakeSuccessHttpSession(websocket),
        )
        await session.connect()
        await websocket.push(aiohttp.WSMsgType.CLOSE)
        await asyncio.wait_for(session._receiver, timeout=1)

        events = await asyncio.wait_for(
            self._collect_events(session.iter_events()), timeout=1
        )

        self.assertEqual(session.completion, CompletionState.ABNORMAL)
        self.assertEqual(session.state, SessionState.FAILED)
        self.assertTrue(any(isinstance(event, GladiaErrorEvent) for event in events))
        await session.stop(timeout=1)

    async def test_cancelled_iterator_does_not_consume_future_eos(self) -> None:
        session = GladiaLiveSession("secret", WaveFormat(48_000, 1, 16))
        iterator = session.iter_events()
        pending = asyncio.create_task(anext(iterator))
        await asyncio.sleep(0)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        session._events.publish_eos()
        session._events.publish_eos()

        events = await asyncio.wait_for(
            self._collect_events(session.iter_events()), timeout=1
        )

        self.assertEqual(events, [])
        self.assertEqual(
            sum(item is gladia_live_module._EVENT_STREAM_EOS for item in session._events._items),
            0,
        )

    async def test_owned_http_cleanup_on_post_and_connect_cancellation(self) -> None:
        post_http = BlockingPostHttpSession()
        with patch.object(gladia_live_module.aiohttp, "ClientSession", return_value=post_http):
            post_session = GladiaLiveSession("secret", WaveFormat(48_000, 1, 16))
            post_task = asyncio.create_task(post_session.connect())
            await post_http.request.entered.wait()
            post_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await post_task
        connect_http = BlockingConnectHttpSession()
        with patch.object(
            gladia_live_module.aiohttp, "ClientSession", return_value=connect_http
        ):
            connect_session = GladiaLiveSession("secret", WaveFormat(48_000, 1, 16))
            connect_task = asyncio.create_task(connect_session.connect())
            await connect_http.connect_started.wait()
            connect_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await connect_task

        self.assertTrue(post_http.closed)
        self.assertTrue(connect_http.closed)
        self.assertEqual(post_session.state, SessionState.FAILED)
        self.assertEqual(connect_session.state, SessionState.FAILED)

    async def test_stop_preempts_stalled_connect_without_lock_starvation(self) -> None:
        http = BlockingPostHttpSession()
        with patch.object(gladia_live_module.aiohttp, "ClientSession", return_value=http):
            session = GladiaLiveSession("secret", WaveFormat(48_000, 1, 16))
            connect_task = asyncio.create_task(session.connect())
            await http.request.entered.wait()
            started = asyncio.get_running_loop().time()

            result = await session.stop(timeout=0.2)
            elapsed = asyncio.get_running_loop().time() - started

        self.assertLess(elapsed, 0.3)
        self.assertTrue(connect_task.cancelled())
        self.assertTrue(http.closed)
        self.assertEqual(session.state, SessionState.STOPPED)
        self.assertEqual(result.completion, CompletionState.ABNORMAL)

    async def test_stop_closes_websocket_acquired_before_connect_state_transfer(self) -> None:
        http = GatedConnectHttpSession()
        session = GladiaLiveSession(
            "secret", WaveFormat(48_000, 1, 16), http_session=http
        )
        connect_task = asyncio.create_task(session.connect())
        await http.connect_started.wait()

        # Hold the transition lock so stop queues first, then let ws_connect
        # return. connect now owns a bearer socket locally but cannot publish it.
        await session._state_lock.acquire()
        stop_task = asyncio.create_task(session.stop(timeout=1))
        await asyncio.sleep(0)
        http.connect_release.set()
        await asyncio.sleep(0)
        self.assertIs(session._handshake_websocket, http.websocket)
        session._state_lock.release()

        result = await asyncio.wait_for(stop_task, timeout=1)
        with self.assertRaises(asyncio.CancelledError):
            await connect_task

        self.assertTrue(http.websocket.closed)
        self.assertIsNone(session._handshake_websocket)
        self.assertFalse(http.closed, "an injected HTTP session must not be closed")
        self.assertEqual(session.state, SessionState.STOPPED)
        self.assertEqual(result.completion, CompletionState.ABNORMAL)

    async def test_stop_deadline_bounds_stalled_post_handshake_close(self) -> None:
        close_release = asyncio.Event()
        websocket = FakeWebSocket(auto_end=False, close_release=close_release)
        http = GatedConnectHttpSession(websocket)
        session = GladiaLiveSession(
            "secret", WaveFormat(48_000, 1, 16), http_session=http
        )
        connect_task = asyncio.create_task(session.connect())
        await http.connect_started.wait()

        await session._state_lock.acquire()
        started = asyncio.get_running_loop().time()
        stop_task = asyncio.create_task(session.stop(timeout=0.15))
        await asyncio.sleep(0)
        http.connect_release.set()
        await asyncio.sleep(0)
        self.assertIs(session._handshake_websocket, websocket)
        session._state_lock.release()

        with self.assertRaises(GladiaTransportError):
            await asyncio.wait_for(stop_task, timeout=0.3)
        elapsed = asyncio.get_running_loop().time() - started
        self.assertLess(elapsed, 0.25)
        self.assertEqual(session.state, SessionState.STOPPED)

        # The stop call can detach connect cleanup at its I/O reserve, but that
        # cleanup remains governed by the same original deadline. At expiry it
        # cancels async close and synchronously closes aiohttp's response.
        done, _ = await asyncio.wait(
            {connect_task}, timeout=max(0.01, 0.25 - elapsed)
        )
        self.assertEqual(done, {connect_task})
        await asyncio.gather(connect_task, return_exceptions=True)
        self.assertTrue(websocket.close_started.is_set())
        self.assertTrue(websocket.force_closed)
        self.assertTrue(websocket.closed)
        self.assertIsNone(session._handshake_websocket)
        self.assertFalse(http.closed)

    async def test_stop_cancellation_cleans_receiver_and_transport(self) -> None:
        websocket = FakeWebSocket(auto_end=False)
        http = FakeSuccessHttpSession(websocket)
        session = GladiaLiveSession(
            "secret", WaveFormat(48_000, 1, 16), http_session=http
        )
        await session.connect()
        stop_task = asyncio.create_task(session.stop(timeout=30))
        while not websocket.sent_json:
            await asyncio.sleep(0)
        stop_task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await stop_task

        self.assertTrue(websocket.closed)
        self.assertFalse(http.closed)
        self.assertTrue(session._receiver.done())
        self.assertEqual(session.state, SessionState.STOPPED)
        self.assertEqual(session.completion, CompletionState.ABNORMAL)

    async def test_close_cancellation_still_closes_owned_http(self) -> None:
        websocket = FakeWebSocket(cancel_close=True)
        http = FakeSuccessHttpSession(websocket)
        with patch.object(gladia_live_module.aiohttp, "ClientSession", return_value=http):
            session = GladiaLiveSession("secret", WaveFormat(48_000, 1, 16))
            await session.connect()
            with self.assertRaises(GladiaTransportError):
                await session.stop(timeout=1)

        self.assertTrue(http.closed)
        self.assertEqual(session.state, SessionState.STOPPED)

    async def test_cancelling_stop_during_close_waits_for_physical_cleanup(self) -> None:
        close_release = asyncio.Event()
        websocket = FakeWebSocket(close_release=close_release)
        http = FakeSuccessHttpSession(websocket)
        session = GladiaLiveSession(
            "secret", WaveFormat(48_000, 1, 16), http_session=http
        )
        await session.connect()
        stop_task = asyncio.create_task(session.stop(timeout=1))
        await websocket.close_started.wait()

        stop_task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(
            stop_task.done(), "caller cancellation must not abandon physical close"
        )
        self.assertFalse(websocket.closed)
        close_release.set()

        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(stop_task, timeout=1)
        self.assertTrue(websocket.closed)
        self.assertFalse(http.closed)
        self.assertEqual(session.state, SessionState.STOPPED)

    async def test_stop_before_connect_repeated_and_concurrent_stop(self) -> None:
        never_connected = GladiaLiveSession("secret", WaveFormat(48_000, 1, 16))
        first = await never_connected.stop()
        second = await never_connected.stop()
        self.assertIs(first, second)
        self.assertEqual(never_connected.state, SessionState.STOPPED)
        self.assertEqual(never_connected.completion, CompletionState.ABNORMAL)
        with self.assertRaises(RuntimeError):
            await never_connected.connect()

        websocket = FakeWebSocket()
        session = GladiaLiveSession(
            "secret",
            WaveFormat(48_000, 1, 16),
            http_session=FakeSuccessHttpSession(websocket),
        )
        await session.connect()
        results = await asyncio.gather(
            session.stop(timeout=1), session.stop(timeout=1), session.stop(timeout=1)
        )
        self.assertTrue(all(result is results[0] for result in results))
        self.assertEqual(websocket.sent_json, [{"type": "stop_recording"}])

    async def test_send_failure_transitions_and_stop_recovers(self) -> None:
        websocket = FakeWebSocket(
            send_error=RuntimeError(
                "send broke key=gladia-secret-key "
                "wss://api.gladia.io/v2/live?token=never-log-this"
            )
        )
        session = GladiaLiveSession(
            "gladia-secret-key",
            WaveFormat(48_000, 1, 16),
            http_session=FakeSuccessHttpSession(websocket),
        )
        await session.connect()

        with self.assertRaises(GladiaTransportError) as raised:
            await session.send_pcm(b"\x00\x00")
        formatted = "".join(traceback.format_exception(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertNotIn("gladia-secret-key", formatted)
        self.assertNotIn("never-log-this", formatted)
        self.assertNotIn("wss://", formatted)
        self.assertEqual(session.state, SessionState.FAILED)
        with self.assertRaises(RuntimeError):
            await session.send_pcm(b"\x00\x00")
        await session.stop(timeout=1)
        self.assertEqual(session.state, SessionState.STOPPED)
        self.assertTrue(websocket.closed)

    async def test_abnormal_close_reconnects_same_session_url(self) -> None:
        first = FakeWebSocket(auto_end=False)
        second = FakeWebSocket()
        http = SequencedWebSocketHttpSession(first, second)
        session = GladiaLiveSession(
            "secret",
            WaveFormat(48_000, 1, 16),
            config=GladiaLiveConfig(
                reconnect_attempts=2,
                reconnect_initial_delay=0,
                reconnect_max_delay=0,
            ),
            http_session=http,
        )
        await session.connect()

        await first.push(aiohttp.WSMsgType.CLOSED)
        for _ in range(100):
            if session.result.reconnects == 1:
                break
            await asyncio.sleep(0)

        self.assertEqual(session.result.reconnects, 1)
        self.assertEqual(session.state, SessionState.CONNECTED)
        self.assertFalse(session._receiver.done())
        self.assertEqual(len(set(http.connected_urls)), 1)
        self.assertTrue(await session.send_pcm(b"\x01\x02"))
        self.assertEqual(second.sent_bytes, [b"\x01\x02"])
        await session.stop(timeout=1)
        self.assertEqual(session.completion, CompletionState.NORMAL)

    async def test_ambiguous_send_frame_is_dropped_not_replayed(self) -> None:
        first = FakeWebSocket(
            auto_end=False,
            send_error=RuntimeError("delivery unknown token=secret"),
        )
        second = FakeWebSocket()
        http = SequencedWebSocketHttpSession(first, second)
        session = GladiaLiveSession(
            "secret",
            WaveFormat(48_000, 1, 16),
            config=GladiaLiveConfig(
                reconnect_attempts=1,
                reconnect_initial_delay=0,
                reconnect_max_delay=0,
            ),
            http_session=http,
        )
        await session.connect()

        self.assertFalse(await session.send_pcm(b"ambiguous"))
        self.assertEqual(second.sent_bytes, [])
        self.assertEqual(session.result.ambiguous_frames_dropped, 1)
        self.assertTrue(await session.send_pcm(b"next"))
        self.assertEqual(second.sent_bytes, [b"next"])
        await session.stop(timeout=1)

    async def test_reconnect_exhaustion_is_bounded_and_redacted(self) -> None:
        first = FakeWebSocket(auto_end=False)
        failure = RuntimeError(
            "connect key=secret wss://api.gladia.io/v2/live?token=never-log"
        )
        http = SequencedWebSocketHttpSession(first, failure, failure)
        session = GladiaLiveSession(
            "secret",
            WaveFormat(48_000, 1, 16),
            config=GladiaLiveConfig(
                reconnect_attempts=2,
                reconnect_initial_delay=0,
                reconnect_max_delay=0,
                reconnect_connect_timeout=0.1,
            ),
            http_session=http,
        )
        await session.connect()
        await first.push(aiohttp.WSMsgType.CLOSED)

        await session._receiver
        rendered = "\n".join(session.result.errors)
        self.assertEqual(session.result.reconnect_failures, 2)
        self.assertEqual(session.state, SessionState.FAILED)
        self.assertEqual(session.completion, CompletionState.ABNORMAL)
        self.assertNotIn("secret", rendered)
        self.assertNotIn("never-log", rendered)
        self.assertNotIn("wss://", rendered)
        try:
            await session.stop(timeout=0.5)
        except GladiaTransportError:
            pass

    async def test_stop_during_reconnect_is_bounded_and_terminal(self) -> None:
        first = FakeWebSocket(auto_end=False)
        http = BlockingReconnectHttpSession(first)
        session = GladiaLiveSession(
            "secret",
            WaveFormat(48_000, 1, 16),
            config=GladiaLiveConfig(
                reconnect_attempts=1,
                reconnect_initial_delay=0,
                reconnect_max_delay=0,
                reconnect_connect_timeout=10,
            ),
            http_session=http,
        )
        await session.connect()
        await first.push(aiohttp.WSMsgType.CLOSED)
        await http.reconnect_started.wait()

        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            await session.stop(timeout=0.1)
        except GladiaTransportError:
            pass
        self.assertLess(loop.time() - started, 0.5)
        self.assertEqual(session.state, SessionState.STOPPED)
        self.assertTrue(session._receiver.done())

    async def test_stop_recording_receive_and_close_errors_are_redacted(self) -> None:
        secret_error = RuntimeError(
            "transport key=gladia-secret-key "
            "wss://api.gladia.io/v2/live?token=never-log-this"
        )

        stop_websocket = FakeWebSocket(auto_end=False, stop_error=secret_error)
        stop_session = GladiaLiveSession(
            "gladia-secret-key",
            WaveFormat(48_000, 1, 16),
            http_session=FakeSuccessHttpSession(stop_websocket),
        )
        await stop_session.connect()
        with self.assertRaises(GladiaTransportError) as stop_raised:
            await stop_session.stop(timeout=0.5)

        receive_websocket = FakeWebSocket(
            auto_end=False, websocket_error=secret_error
        )
        receive_session = GladiaLiveSession(
            "gladia-secret-key",
            WaveFormat(48_000, 1, 16),
            http_session=FakeSuccessHttpSession(receive_websocket),
        )
        await receive_session.connect()
        await receive_websocket.push(aiohttp.WSMsgType.ERROR)
        with self.assertRaises(GladiaTransportError) as receive_raised:
            await receive_session._receiver
        try:
            await receive_session.stop(timeout=0.5)
        except GladiaTransportError:
            pass

        close_websocket = FakeWebSocket(close_error=secret_error)
        close_session = GladiaLiveSession(
            "gladia-secret-key",
            WaveFormat(48_000, 1, 16),
            http_session=FakeSuccessHttpSession(close_websocket),
        )
        await close_session.connect()
        with self.assertRaises(GladiaTransportError) as close_raised:
            await close_session.stop(timeout=0.5)

        for raised in (stop_raised, receive_raised, close_raised):
            formatted = "".join(traceback.format_exception(raised.exception))
            self.assertIsNone(raised.exception.__cause__)
            self.assertNotIn("gladia-secret-key", formatted)
            self.assertNotIn("never-log-this", formatted)
            self.assertNotIn("wss://", formatted)

    async def test_stop_retrieves_already_failed_receiver_and_surfaces_error(self) -> None:
        secret_error = RuntimeError(
            "receive key=gladia-secret-key "
            "wss://api.gladia.io/v2/live?token=never-log-this"
        )
        websocket = FakeWebSocket(auto_end=False, websocket_error=secret_error)
        session = GladiaLiveSession(
            "gladia-secret-key",
            WaveFormat(48_000, 1, 16),
            http_session=FakeSuccessHttpSession(websocket),
        )
        await session.connect()
        await websocket.push(aiohttp.WSMsgType.ERROR)
        while not session._receiver.done():
            await asyncio.sleep(0)

        # Deliberately do not await _receiver: stop owns retrieval and reporting.
        with self.assertRaises(GladiaTransportError) as raised:
            await session.stop(timeout=0.5)

        formatted = "".join(traceback.format_exception(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertIn("Gladia WebSocket error", str(raised.exception))
        self.assertNotIn("gladia-secret-key", formatted)
        self.assertNotIn("never-log-this", formatted)
        self.assertNotIn("wss://", formatted)
        self.assertTrue(websocket.closed)
        self.assertEqual(session.state, SessionState.STOPPED)
        self.assertEqual(session.completion, CompletionState.ABNORMAL)

    async def test_normal_protocol_completion_retains_cleanup_failure_health(self) -> None:
        secret_error = RuntimeError(
            "close key=gladia-secret-key "
            "wss://api.gladia.io/v2/live?token=never-log-this"
        )
        websocket = FakeWebSocket(close_error=secret_error)
        session = GladiaLiveSession(
            "gladia-secret-key",
            WaveFormat(48_000, 1, 16),
            http_session=FakeSuccessHttpSession(websocket),
        )
        await session.connect()

        with self.assertRaises(GladiaTransportError) as raised:
            await session.stop(timeout=0.5)

        self.assertEqual(session.completion, CompletionState.NORMAL)
        self.assertEqual(session.result.completion_reason, "Gladia sent end_session")
        self.assertTrue(
            any("Could not close Gladia WebSocket" in item for item in session.result.errors)
        )
        rendered = "\n".join([str(raised.exception), *session.result.errors])
        self.assertNotIn("gladia-secret-key", rendered)
        self.assertNotIn("never-log-this", rendered)
        self.assertNotIn("wss://", rendered)
        self.assertEqual(session.state, SessionState.STOPPED)

    async def test_precompleted_normal_session_still_surfaces_cleanup_failure(self) -> None:
        secret_error = RuntimeError(
            "close key=gladia-secret-key "
            "wss://api.gladia.io/v2/live?token=never-log-this"
        )
        websocket = FakeWebSocket(auto_end=False, close_error=secret_error)
        session = GladiaLiveSession(
            "gladia-secret-key",
            WaveFormat(48_000, 1, 16),
            http_session=FakeSuccessHttpSession(websocket),
        )
        await session.connect()
        await websocket.push(
            aiohttp.WSMsgType.TEXT,
            json.dumps(
                {
                    "session_id": "init-session-id",
                    "created_at": "2026-08-15T12:34:10Z",
                    "type": "end_session",
                }
            ),
        )
        while not session._receiver.done():
            await asyncio.sleep(0)
        self.assertEqual(session.completion, CompletionState.NORMAL)
        self.assertEqual(session.state, SessionState.STOPPED)

        with self.assertRaises(GladiaTransportError) as raised:
            await session.stop(timeout=0.5)

        formatted = "".join(traceback.format_exception(raised.exception))
        self.assertEqual(websocket.sent_json, [])
        self.assertEqual(session.completion, CompletionState.NORMAL)
        self.assertTrue(
            any("Could not close Gladia WebSocket" in item for item in session.result.errors)
        )
        self.assertIsNone(raised.exception.__cause__)
        for secret in ("gladia-secret-key", "never-log-this", "wss://"):
            self.assertNotIn(secret, formatted)
            self.assertNotIn(secret, "\n".join(session.result.errors))

    async def test_stalled_send_bytes_cannot_block_stop_deadline(self) -> None:
        websocket = FakeWebSocket(stall_send_bytes=True)
        session = GladiaLiveSession(
            "secret",
            WaveFormat(48_000, 1, 16),
            http_session=FakeSuccessHttpSession(websocket),
        )
        await session.connect()
        send_task = asyncio.create_task(session.send_pcm(b"\x00\x00"))
        await websocket.send_started.wait()
        started = asyncio.get_running_loop().time()

        result = await session.stop(timeout=0.2)
        elapsed = asyncio.get_running_loop().time() - started

        self.assertLess(elapsed, 0.3)
        self.assertEqual(result.completion, CompletionState.NORMAL)
        self.assertTrue(send_task.done())
        self.assertTrue(send_task.cancelled())
        self.assertTrue(websocket.closed)
        self.assertEqual(session.state, SessionState.STOPPED)

    async def test_stalled_stop_recording_is_bounded_and_rejects_new_sends(self) -> None:
        websocket = FakeWebSocket(auto_end=False, stall_send_json=True)
        session = GladiaLiveSession(
            "secret",
            WaveFormat(48_000, 1, 16),
            http_session=FakeSuccessHttpSession(websocket),
        )
        await session.connect()
        started = asyncio.get_running_loop().time()
        stop_task = asyncio.create_task(session.stop(timeout=0.2))
        await websocket.stop_started.wait()

        self.assertEqual(session.state, SessionState.STOPPING)
        with self.assertRaises(RuntimeError):
            await session.send_pcm(b"\x00\x00")
        with self.assertRaises(GladiaTransportError) as raised:
            await stop_task
        elapsed = asyncio.get_running_loop().time() - started

        self.assertLess(elapsed, 0.3)
        self.assertIn("timed out sending stop_recording", str(raised.exception))
        self.assertTrue(websocket.closed)
        self.assertTrue(session._receiver.done())
        self.assertEqual(session.state, SessionState.STOPPED)

    async def test_async_context_manager_owns_protocol_not_injected_http(self) -> None:
        http = FakeSuccessHttpSession()
        session = GladiaLiveSession(
            "secret", WaveFormat(48_000, 1, 16), http_session=http
        )

        async with session as connected:
            self.assertIs(connected, session)
            self.assertEqual(session.state, SessionState.CONNECTED)

        self.assertEqual(session.state, SessionState.STOPPED)
        self.assertTrue(http.websocket.closed)
        self.assertFalse(http.closed)

    async def test_final_only_surface_filters_partial(self) -> None:
        session = GladiaLiveSession("secret", WaveFormat(48_000, 1, 16))
        session.process_message(
            transcript_message("turn", text="This is trash", is_final=False)
        )
        session.process_message(
            transcript_message("turn", text="This is Travis", is_final=True)
        )
        session._events.publish_eos()

        finals = [event async for event in session.iter_final_transcripts()]

        self.assertEqual([event.text for event in finals], ["This is Travis"])

    async def test_mocked_replay_paces_downmixes_and_uses_replay_default(self) -> None:
        fake = FakeReplaySession()
        captured: dict = {}
        delays: list[float] = []
        real_sleep = asyncio.sleep

        def make_session(*args, **kwargs):
            captured.update(kwargs)
            captured["audio_format"] = args[1]
            return fake

        async def capture_sleep(delay: float) -> None:
            delays.append(delay)
            await real_sleep(0)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stereo.wav"
            with wave.open(str(path), "wb") as writer:
                writer.setnchannels(2)
                writer.setsampwidth(2)
                writer.setframerate(48_000)
                writer.writeframes(struct.pack("<hh", 1_000, 3_000) * 1_920)
            with (
                patch.object(
                    gladia_live_module, "GladiaLiveSession", side_effect=make_session
                ),
                patch.object(
                    gladia_live_module.asyncio, "sleep", side_effect=capture_sleep
                ),
            ):
                result = await gladia_live_module.transcribe_wave_live(path, "secret")

        self.assertIs(result, fake.result)
        self.assertEqual(captured["config"].endpointing, 0.5)
        self.assertEqual(captured["audio_format"].channels, 1)
        self.assertEqual(len(fake.sent), 2)
        self.assertTrue(all(len(chunk) == 1_920 for chunk in fake.sent))
        self.assertEqual(struct.unpack("<h", fake.sent[0][:2])[0], 2_000)
        paced_delays = [delay for delay in delays if delay > 0]
        self.assertEqual(len(paced_delays), 2)
        self.assertTrue(all(abs(delay - 0.02) < 0.005 for delay in paced_delays))
        self.assertGreaterEqual(fake.stop_calls, 1)

    async def test_final_callback_never_receives_partial_and_failure_propagates(self) -> None:
        partial = parse_gladia_message(
            transcript_message("turn", text="partial", is_final=False)
        )
        final = parse_gladia_message(transcript_message("turn", text="final", is_final=True))
        self.assertIsInstance(partial, TranscriptUpdate)
        self.assertIsInstance(final, TranscriptUpdate)
        fake = FakeReplaySession([partial, final])
        received: list[str] = []

        def failing_final(update: TranscriptUpdate) -> None:
            received.append(update.text)
            raise RuntimeError("callback failed")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mono.wav"
            with wave.open(str(path), "wb") as writer:
                writer.setnchannels(1)
                writer.setsampwidth(2)
                writer.setframerate(48_000)
                writer.writeframes(b"\x00\x00" * 960)
            with patch.object(gladia_live_module, "GladiaLiveSession", return_value=fake):
                with self.assertRaisesRegex(RuntimeError, "callback failed"):
                    await gladia_live_module.transcribe_wave_live(
                        path, "secret", on_final=failing_final
                    )

        self.assertEqual(received, ["final"])
        self.assertFalse(
            any(
                task.get_name() == "gladia-wave-transcript-consumer" and not task.done()
                for task in asyncio.all_tasks()
            )
        )

    @staticmethod
    async def _collect_events(iterator) -> list:
        return [event async for event in iterator]


if __name__ == "__main__":
    unittest.main()
