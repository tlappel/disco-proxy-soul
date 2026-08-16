"""Gladia Live V2 protocol boundary and real-time PCM WAV replay driver.

The reusable :class:`GladiaLiveSession` owns only Gladia's HTTP/WebSocket
protocol. Discord, turn assembly, cognition, and playback belong elsewhere.
"""

from __future__ import annotations

import argparse
import asyncio
from array import array
from collections import deque, OrderedDict
from dataclasses import dataclass, field
from enum import Enum
import json
import os
from pathlib import Path
import re
import sys
import wave
from typing import Any, AsyncIterator, Callable

import aiohttp
from dotenv import load_dotenv


GLADIA_LIVE_INIT_URL = "https://api.gladia.io/v2/live"
_LIFECYCLE_TYPES = {
    "start_session",
    "start_recording",
    "end_recording",
    "end_session",
}
_URL_WITH_QUERY_RE = re.compile(r"\b(?:https?|wss?)://[^\s\"']+", re.IGNORECASE)
_TOKEN_RE = re.compile(r"(?i)(token|api[_-]?key)(\s*[:=]\s*)([^\s,;\"']+)")
_BEARER_RE = re.compile(r"(?i)\b(Bearer)(\s+)([^\s,;\"']+)")


@dataclass(frozen=True)
class WaveFormat:
    sample_rate: int
    channels: int
    bit_depth: int
    frame_count: int = 0

    @property
    def duration_seconds(self) -> float:
        return self.frame_count / self.sample_rate


@dataclass(frozen=True)
class GladiaLiveConfig:
    model: str = "solaria-1"
    language: str = "en"
    endpointing: float = 0.1
    maximum_duration_without_endpointing: int = 15
    speech_threshold: float = 0.6
    audio_enhancer: bool = False
    frame_duration_ms: int = 20
    downmix_stereo: bool = True
    partial_state_limit: int = 32
    event_queue_limit: int = 64


@dataclass(frozen=True)
class TranscriptWord:
    word: str
    start: float
    end: float
    confidence: float


@dataclass(frozen=True)
class TranscriptUpdate:
    session_id: str
    created_at: str
    utterance_id: str
    text: str
    is_final: bool
    start: float
    end: float
    confidence: float
    channel: int
    words: tuple[TranscriptWord, ...]
    language: str
    speaker: int | None = None


@dataclass(frozen=True)
class SpeechEvent:
    session_id: str
    created_at: str
    event_type: str
    time: float
    channel: int


@dataclass(frozen=True)
class LifecycleEvent:
    session_id: str
    created_at: str
    event_type: str
    reason: str | None = None
    received_total_bytes: int | None = None
    recording_duration: float | None = None


@dataclass(frozen=True)
class AcknowledgmentEvent:
    session_id: str
    created_at: str
    action: str
    acknowledged: bool
    error: str | None = None
    byte_range: tuple[int, int] | None = None
    time_range: tuple[float, float] | None = None


@dataclass(frozen=True)
class GladiaErrorEvent:
    message: str
    session_id: str | None = None
    created_at: str | None = None
    source_type: str = "error"
    code: str | None = None


GladiaEvent = (
    TranscriptUpdate
    | SpeechEvent
    | LifecycleEvent
    | AcknowledgmentEvent
    | GladiaErrorEvent
)


class CompletionState(str, Enum):
    PENDING = "pending"
    NORMAL = "normal"
    ABNORMAL = "abnormal"


class SessionState(str, Enum):
    NEW = "new"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class GladiaEventStreamClosed(RuntimeError):
    """Raised when direct event receipt reaches the private EOS marker."""


_EVENT_STREAM_EOS = object()


class GladiaCriticalEventOverflow(RuntimeError):
    """The bounded public event stream exhausted its critical-event reserve."""


class GladiaTransportError(RuntimeError):
    """A redacted Gladia transport failure safe for public diagnostics."""


def _is_critical_event(event: GladiaEvent) -> bool:
    return (
        isinstance(event, GladiaErrorEvent)
        or isinstance(event, LifecycleEvent)
        or (isinstance(event, TranscriptUpdate) and event.is_final)
    )


def _is_routine_event(event: GladiaEvent) -> bool:
    return not _is_critical_event(event)


class _BoundedEventBuffer:
    """Non-blocking bounded delivery with partial coalescing and critical reserve."""

    def __init__(self, maxsize: int) -> None:
        if maxsize < 4:
            raise ValueError("event_queue_limit must be at least 4")
        self.maxsize = maxsize
        self._regular_limit = maxsize - 2  # fatal-overflow marker + EOS reserve
        self._routine_limit = max(1, self._regular_limit - 2)
        self._items: deque[GladiaEvent | object] = deque()
        self._ready = asyncio.Event()
        self.coalesced_count = 0
        self.dropped_count = 0
        self.critical_overflow_count = 0
        self._eos_published = False
        self._fatal_overflow = False

    def qsize(self) -> int:
        return len(self._items)

    def publish(self, event: GladiaEvent) -> None:
        if self._eos_published:
            if _is_critical_event(event):
                raise GladiaCriticalEventOverflow(
                    "A critical Gladia event arrived after event-stream EOS"
                )
            self.dropped_count += 1
            return
        if isinstance(event, TranscriptUpdate) and not event.is_final:
            identity = (event.session_id, event.utterance_id)
            for index, queued in enumerate(self._items):
                if (
                    isinstance(queued, TranscriptUpdate)
                    and not queued.is_final
                    and (queued.session_id, queued.utterance_id) == identity
                ):
                    self._items[index] = event
                    self.coalesced_count += 1
                    self._ready.set()
                    return

        if _is_routine_event(event) and len(self._items) >= self._routine_limit:
            self.dropped_count += 1
            return
        if len(self._items) >= self._regular_limit:
            for index, queued in enumerate(self._items):
                if queued is not _EVENT_STREAM_EOS and isinstance(queued, (
                    AcknowledgmentEvent,
                    SpeechEvent,
                    TranscriptUpdate,
                )) and not _is_critical_event(queued):
                    del self._items[index]
                    self.dropped_count += 1
                    break
            else:
                if _is_critical_event(event):
                    raise GladiaCriticalEventOverflow(
                        "Gladia critical event buffer capacity was exhausted"
                    )
                self.dropped_count += 1
                return
        self._items.append(event)
        self._ready.set()

    def publish_fatal_overflow(self, event: GladiaErrorEvent) -> None:
        if self._fatal_overflow:
            return
        self._fatal_overflow = True
        self.critical_overflow_count += 1
        try:
            eos_index = self._items.index(_EVENT_STREAM_EOS)
        except ValueError:
            self._items.append(event)
        else:
            self._items.insert(eos_index, event)
        self.publish_eos()

    def publish_eos(self) -> None:
        if self._eos_published:
            return
        self._eos_published = True
        self._items.append(_EVENT_STREAM_EOS)
        self._ready.set()

    async def get(self) -> GladiaEvent | object:
        while True:
            if self._items:
                item = self._items.popleft()
                if not self._items:
                    self._ready.clear()
                return item
            if self._eos_published:
                return _EVENT_STREAM_EOS
            self._ready.clear()
            if self._items:
                continue
            await self._ready.wait()


@dataclass
class GladiaLiveResult:
    partials: list[str] = field(default_factory=list)
    finals: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    session_id: str | None = None
    completion: CompletionState = CompletionState.PENDING
    completion_reason: str | None = None

    @property
    def transcript(self) -> str:
        return " ".join(text.strip() for text in self.finals if text.strip())


class GladiaInitError(RuntimeError):
    """A redacted Gladia session-init failure safe to show in diagnostics."""

    def __init__(self, status: int | None, detail: str) -> None:
        prefix = f"Gladia live initialization failed (HTTP {status})" if status else (
            "Gladia live initialization failed"
        )
        super().__init__(f"{prefix}: {detail}")
        self.status = status


def inspect_pcm_wave(path: str | Path) -> WaveFormat:
    """Validate a WAV file and return the format Gladia needs."""

    wave_path = Path(path)
    with wave.open(str(wave_path), "rb") as reader:
        if reader.getcomptype() != "NONE":
            raise ValueError("Gladia replay requires an uncompressed PCM WAV file")
        if reader.getsampwidth() != 2:
            raise ValueError("Gladia replay currently requires 16-bit PCM audio")
        if reader.getnchannels() not in (1, 2):
            raise ValueError("Gladia replay supports mono or stereo WAV files")
        if reader.getframerate() <= 0:
            raise ValueError("WAV sample rate must be positive")
        return WaveFormat(
            sample_rate=reader.getframerate(),
            channels=reader.getnchannels(),
            bit_depth=reader.getsampwidth() * 8,
            frame_count=reader.getnframes(),
        )


def build_init_payload(
    audio_format: WaveFormat,
    config: GladiaLiveConfig,
) -> dict[str, Any]:
    """Build a conservative Gladia Live V2 session configuration."""

    return {
        "encoding": "wav/pcm",
        "bit_depth": audio_format.bit_depth,
        "sample_rate": audio_format.sample_rate,
        "channels": audio_format.channels,
        "model": config.model,
        "endpointing": config.endpointing,
        "maximum_duration_without_endpointing": (
            config.maximum_duration_without_endpointing
        ),
        "language_config": {
            "languages": [config.language],
            "code_switching": False,
        },
        "pre_processing": {
            "audio_enhancer": config.audio_enhancer,
            "speech_threshold": config.speech_threshold,
        },
        "messages_config": {
            "receive_partial_transcripts": True,
            "receive_final_transcripts": True,
            "receive_speech_events": True,
            "receive_acknowledgments": True,
            "receive_errors": True,
            "receive_lifecycle_events": True,
        },
    }


def pcm16_stereo_to_mono(chunk: bytes) -> bytes:
    """Downmix little-endian interleaved stereo PCM16 into mono PCM16."""

    if len(chunk) % 4:
        raise ValueError("Stereo PCM16 data must contain complete sample frames")
    samples = array("h")
    samples.frombytes(chunk)
    if sys.byteorder != "little":
        samples.byteswap()
    mono = array(
        "h",
        (
            (int(samples[index]) + int(samples[index + 1])) // 2
            for index in range(0, len(samples), 2)
        ),
    )
    if sys.byteorder != "little":
        mono.byteswap()
    return mono.tobytes()


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"missing or invalid {key}")
    return value


def _number(payload: dict[str, Any], key: str) -> float:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"missing or invalid {key}")
    return float(value)


def _integer(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"missing or invalid {key}")
    return value


def _error_message(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("message", "error", "description", "detail"):
            if value.get(key):
                return str(value[key])
        return "Gladia reported an unspecified error"
    if value:
        return str(value)
    return "Gladia reported an unspecified error"


def _parse_pair(value: Any, integer: bool) -> tuple[Any, Any] | None:
    if not isinstance(value, list) or len(value) != 2:
        return None
    if integer:
        if any(isinstance(item, bool) or not isinstance(item, int) for item in value):
            return None
        return value[0], value[1]
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value):
        return None
    return float(value[0]), float(value[1])


def parse_gladia_message(payload: str | bytes | dict[str, Any]) -> GladiaEvent | None:
    """Parse a documented Gladia message without letting malformed input escape."""

    try:
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            raise ValueError("message must be a JSON object")

        message_type = payload.get("type")
        session_id = payload.get("session_id")
        created_at = payload.get("created_at")

        # Gladia also puts errors at the envelope's top level for messages whose
        # type is not literally ``error`` (notably acknowledgments).
        if payload.get("error") not in (None, False, ""):
            return GladiaErrorEvent(
                message=_error_message(payload["error"]),
                session_id=session_id if isinstance(session_id, str) else None,
                created_at=created_at if isinstance(created_at, str) else None,
                source_type=message_type if isinstance(message_type, str) else "error",
                code=str(payload.get("error_code")) if payload.get("error_code") else None,
            )

        if message_type == "error":
            data = payload.get("data")
            code = str(data["code"]) if isinstance(data, dict) and data.get("code") else None
            return GladiaErrorEvent(
                message=_error_message(data),
                session_id=session_id if isinstance(session_id, str) else None,
                created_at=created_at if isinstance(created_at, str) else None,
                code=code,
            )

        if message_type == "transcript":
            data = payload.get("data")
            if not isinstance(data, dict):
                raise ValueError("missing or invalid data")
            utterance = data.get("utterance")
            if not isinstance(utterance, dict):
                raise ValueError("missing or invalid utterance")
            raw_words = utterance.get("words")
            if not isinstance(raw_words, list):
                raise ValueError("missing or invalid words")
            words = tuple(
                TranscriptWord(
                    word=_required_text(word, "word"),
                    start=_number(word, "start"),
                    end=_number(word, "end"),
                    confidence=_number(word, "confidence"),
                )
                for word in raw_words
                if isinstance(word, dict)
            )
            if len(words) != len(raw_words):
                raise ValueError("invalid transcript word")
            is_final = data.get("is_final")
            if not isinstance(is_final, bool):
                raise ValueError("missing or invalid is_final")
            speaker = utterance.get("speaker")
            if speaker is not None and (
                isinstance(speaker, bool) or not isinstance(speaker, int)
            ):
                raise ValueError("invalid speaker")
            return TranscriptUpdate(
                session_id=_required_text(payload, "session_id"),
                created_at=_required_text(payload, "created_at"),
                utterance_id=_required_text(data, "id"),
                text=_required_text(utterance, "text"),
                is_final=is_final,
                start=_number(utterance, "start"),
                end=_number(utterance, "end"),
                confidence=_number(utterance, "confidence"),
                channel=_integer(utterance, "channel"),
                words=words,
                language=_required_text(utterance, "language"),
                speaker=speaker,
            )

        if message_type in ("speech_start", "speech_end"):
            data = payload.get("data")
            if not isinstance(data, dict):
                raise ValueError("missing or invalid data")
            return SpeechEvent(
                session_id=_required_text(payload, "session_id"),
                created_at=_required_text(payload, "created_at"),
                event_type=message_type,
                time=_number(data, "time"),
                channel=_integer(data, "channel"),
            )

        if message_type in _LIFECYCLE_TYPES:
            data = payload.get("data") or {}
            if not isinstance(data, dict):
                raise ValueError("invalid lifecycle data")
            total_bytes = data.get("received_total_bytes")
            if total_bytes is not None and (
                isinstance(total_bytes, bool) or not isinstance(total_bytes, int)
            ):
                raise ValueError("invalid received_total_bytes")
            duration = data.get("recording_duration")
            if duration is not None and (
                isinstance(duration, bool) or not isinstance(duration, (int, float))
            ):
                raise ValueError("invalid recording_duration")
            return LifecycleEvent(
                session_id=_required_text(payload, "session_id"),
                created_at=_required_text(payload, "created_at"),
                event_type=message_type,
                reason=str(data["reason"]) if data.get("reason") is not None else None,
                received_total_bytes=total_bytes,
                recording_duration=float(duration) if duration is not None else None,
            )

        if message_type in ("audio_chunk", "stop_recording") and isinstance(
            payload.get("acknowledged"), bool
        ):
            data = payload.get("data") or {}
            if not isinstance(data, dict):
                raise ValueError("invalid acknowledgment data")
            return AcknowledgmentEvent(
                session_id=_required_text(payload, "session_id"),
                created_at=_required_text(payload, "created_at"),
                action=message_type,
                acknowledged=payload["acknowledged"],
                byte_range=_parse_pair(data.get("byte_range"), True),
                time_range=_parse_pair(data.get("time_range"), False),
            )
        return None
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        return GladiaErrorEvent(
            message=f"Invalid Gladia message: {exc}",
            source_type="malformed_message",
        )


def parse_transcript_message(
    payload: str | bytes | dict[str, Any],
) -> TranscriptUpdate | None:
    """Compatibility helper returning only well-formed transcript messages."""

    event = parse_gladia_message(payload)
    return event if isinstance(event, TranscriptUpdate) else None


def redact_sensitive_text(text: str, *secrets: str) -> str:
    """Remove API keys, token values, and tokenized URLs from diagnostics."""

    redacted = text
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "<redacted>")
    redacted = _URL_WITH_QUERY_RE.sub("<redacted-url>", redacted)
    redacted = _TOKEN_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}<redacted>", redacted
    )
    redacted = _BEARER_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}<redacted>", redacted
    )
    return redacted[:2_000]


class GladiaLiveSession:
    """One reusable Gladia Live V2 session with structured event delivery."""

    def __init__(
        self,
        api_key: str,
        audio_format: WaveFormat,
        *,
        config: GladiaLiveConfig | None = None,
        init_url: str = GLADIA_LIVE_INIT_URL,
        http_session: aiohttp.ClientSession | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("A Gladia API key is required")
        live_config = config or GladiaLiveConfig()
        if live_config.partial_state_limit < 1:
            raise ValueError("partial_state_limit must be positive")
        if live_config.event_queue_limit < 4:
            raise ValueError("event_queue_limit must be at least 4")
        self._api_key = api_key
        self.audio_format = audio_format
        self.config = live_config
        self._init_url = init_url
        self._http = http_session
        self._owns_http = http_session is None
        self._websocket: aiohttp.ClientWebSocketResponse | None = None
        self._handshake_websocket: aiohttp.ClientWebSocketResponse | None = None
        self._receiver: asyncio.Task[None] | None = None
        self._events = _BoundedEventBuffer(live_config.event_queue_limit)
        self._latest_partials: OrderedDict[
            tuple[str, str], TranscriptUpdate
        ] = OrderedDict()
        self._seen_finals: set[tuple[str, str]] = set()
        self._stop_sent = False
        self._state = SessionState.NEW
        self._state_lock = asyncio.Lock()
        self._stop_lock = asyncio.Lock()
        self._connect_task: asyncio.Task[Any] | None = None
        self._shutdown_deadline: float | None = None
        self._active_sends: set[asyncio.Task[Any]] = set()
        self.session_id: str | None = None
        self.result = GladiaLiveResult()

    @property
    def completion(self) -> CompletionState:
        return self.result.completion

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def event_queue_size(self) -> int:
        return self._events.qsize()

    @property
    def coalesced_event_count(self) -> int:
        return self._events.coalesced_count

    @property
    def dropped_event_count(self) -> int:
        return self._events.dropped_count

    @property
    def critical_overflow_count(self) -> int:
        return self._events.critical_overflow_count

    @property
    def latest_partials(self) -> tuple[TranscriptUpdate, ...]:
        return tuple(self._latest_partials.values())

    @property
    def connected(self) -> bool:
        return (
            self._state is SessionState.CONNECTED
            and self._websocket is not None
            and not self._websocket.closed
        )

    async def __aenter__(self) -> "GladiaLiveSession":
        return await self.connect()

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.stop()

    async def connect(self) -> "GladiaLiveSession":
        current = asyncio.current_task()
        if current is None:
            raise RuntimeError("connect requires an asyncio task")
        async with self._state_lock:
            if self._state is not SessionState.NEW:
                raise RuntimeError(f"Cannot connect Gladia session from {self._state.value}")
            self._state = SessionState.CONNECTING
            self._connect_task = current
            if self._http is None:
                timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=60)
                self._http = aiohttp.ClientSession(timeout=timeout)

        websocket_url: str | None = None
        try:
            assert self._http is not None
            async with self._http.post(
                self._init_url,
                headers={"x-gladia-key": self._api_key},
                json=build_init_payload(self.audio_format, self.config),
            ) as response:
                if response.status < 200 or response.status >= 300:
                    body = await response.text()
                    detail = redact_sensitive_text(body, self._api_key)
                    raise GladiaInitError(
                        response.status, detail or "empty response"
                    ) from None
                try:
                    session_info = await response.json()
                except (aiohttp.ContentTypeError, json.JSONDecodeError, ValueError):
                    raise GladiaInitError(
                        response.status, "invalid JSON response"
                    ) from None

            if not isinstance(session_info, dict):
                raise GladiaInitError(None, "response was not a JSON object") from None
            session_id = session_info.get("id")
            websocket_url = session_info.get("url")
            if not isinstance(session_id, str) or not session_id:
                raise GladiaInitError(
                    None, "response did not include a session ID"
                ) from None
            if not isinstance(websocket_url, str) or not websocket_url:
                raise GladiaInitError(
                    None, "response did not include a WebSocket URL"
                ) from None

            websocket = await self._http.ws_connect(websocket_url, heartbeat=20)
            # Record local ownership before the next await. If stop cancels us
            # while waiting for the state lock, cleanup can still find/close it.
            self._handshake_websocket = websocket
            async with self._state_lock:
                if self._state is not SessionState.CONNECTING:
                    shutdown_started = True
                else:
                    shutdown_started = False
                    self.session_id = session_id
                    self.result.session_id = session_id
                    self._websocket = websocket
                    self._handshake_websocket = None
                    self._state = SessionState.CONNECTED
                    self._connect_task = None
                    self._receiver = asyncio.create_task(
                        self._receive_messages(),
                        name=f"gladia-live-receiver-{session_id}",
                    )
            if shutdown_started:
                raise GladiaTransportError(
                    "Gladia connect completed after shutdown began"
                ) from None
            return self
        except BaseException as exc:
            async with self._state_lock:
                self._connect_task = None
                if self._state is SessionState.CONNECTING:
                    self._state = SessionState.FAILED
            if isinstance(exc, asyncio.CancelledError):
                self._mark_abnormal("Gladia connect was cancelled")
            elif isinstance(exc, (GladiaInitError, GladiaTransportError)):
                self._mark_abnormal(str(exc))
            else:
                safe_detail = redact_sensitive_text(
                    str(exc), self._api_key, websocket_url or ""
                )
                self._mark_abnormal(
                    f"Gladia live initialization failed: "
                    f"{safe_detail or type(exc).__name__}"
                )
            try:
                # A concurrent stop owns the wall-clock shutdown budget. Its
                # deadline must also bound cleanup of a just-acquired socket.
                await self._cleanup_transport_shielded(self._shutdown_deadline)
            except (asyncio.CancelledError, GladiaTransportError):
                pass
            self._events.publish_eos()
            if isinstance(exc, asyncio.CancelledError):
                raise
            if isinstance(exc, GladiaInitError):
                raise exc from None
            if isinstance(exc, GladiaTransportError):
                raise exc from None
            safe_detail = redact_sensitive_text(
                str(exc), self._api_key, websocket_url or ""
            )
            raise GladiaInitError(
                None, safe_detail or type(exc).__name__
            ) from None

    async def send_pcm(self, chunk: bytes) -> None:
        if not isinstance(chunk, bytes):
            raise TypeError("PCM chunk must be bytes")
        if not chunk:
            return
        current = asyncio.current_task()
        if current is None:
            raise RuntimeError("send_pcm requires an asyncio task")
        async with self._state_lock:
            if self._state is not SessionState.CONNECTED or self._websocket is None:
                raise RuntimeError(f"Cannot send PCM while session is {self._state.value}")
            websocket = self._websocket
            self._active_sends.add(current)

        public_error: GladiaTransportError | None = None
        cancelled = False
        try:
            await websocket.send_bytes(chunk)
        except asyncio.CancelledError:
            cancelled = True
        except Exception as exc:
            safe = redact_sensitive_text(str(exc), self._api_key)
            public_error = GladiaTransportError(
                f"Gladia PCM send failed: {safe or type(exc).__name__}"
            )
        finally:
            async with self._state_lock:
                self._active_sends.discard(current)
                if (
                    (cancelled or public_error is not None)
                    and self._state is SessionState.CONNECTED
                ):
                    self._state = SessionState.FAILED
                    self._mark_abnormal(
                        "Gladia PCM send was cancelled"
                        if cancelled
                        else str(public_error)
                    )

        if cancelled:
            raise asyncio.CancelledError
        if public_error is not None:
            raise public_error from None

    async def receive_event(self) -> GladiaEvent:
        """Wait for the next structured protocol event."""

        event = await self._events.get()
        if event is _EVENT_STREAM_EOS:
            raise GladiaEventStreamClosed("Gladia event stream has ended")
        return event

    async def iter_events(self) -> AsyncIterator[GladiaEvent]:
        """Yield public events until the receiver's private EOS marker."""

        while True:
            event = await self._events.get()
            if event is _EVENT_STREAM_EOS:
                return
            yield event

    async def iter_final_transcripts(self) -> AsyncIterator[TranscriptUpdate]:
        """Final-only integration surface; partials never cross this boundary."""

        async for event in self.iter_events():
            if isinstance(event, TranscriptUpdate) and event.is_final:
                yield event

    def process_message(self, payload: str | bytes | dict[str, Any]) -> GladiaEvent | None:
        """Parse and publish one message; public for deterministic protocol tests."""

        event = parse_gladia_message(payload)
        if event is None:
            return None
        if isinstance(event, TranscriptUpdate):
            identity = (event.session_id, event.utterance_id)
            if event.is_final:
                self._latest_partials.pop(identity, None)
                self.result.partials = [item.text for item in self._latest_partials.values()]
                if identity in self._seen_finals:
                    return None
                self._seen_finals.add(identity)
                self.result.finals.append(event.text)
            else:
                if identity in self._seen_finals:
                    return None
                self._latest_partials.pop(identity, None)
                self._latest_partials[identity] = event
                while len(self._latest_partials) > self.config.partial_state_limit:
                    self._latest_partials.popitem(last=False)
                self.result.partials = [item.text for item in self._latest_partials.values()]
        elif isinstance(event, GladiaErrorEvent):
            safe_message = redact_sensitive_text(event.message, self._api_key)
            if safe_message != event.message:
                event = GladiaErrorEvent(
                    message=safe_message,
                    session_id=event.session_id,
                    created_at=event.created_at,
                    source_type=event.source_type,
                    code=event.code,
                )
            self.result.errors.append(event.message)
        self._publish_event(event)
        if isinstance(event, LifecycleEvent) and event.event_type == "end_session":
            self._set_completion(CompletionState.NORMAL, "Gladia sent end_session")
        return event

    def _publish_event(self, event: GladiaEvent) -> None:
        if self._events._fatal_overflow:
            raise GladiaCriticalEventOverflow(
                "Gladia event stream already failed from critical overflow"
            ) from None
        try:
            self._events.publish(event)
        except GladiaCriticalEventOverflow:
            message = (
                "Gladia critical event delivery overflow; session stopped before "
                "silently losing a final, error, or lifecycle event"
            )
            self._set_completion(CompletionState.ABNORMAL, message)
            if message not in self.result.errors:
                self.result.errors.append(message)
            self._state = SessionState.FAILED
            self._events.publish_fatal_overflow(
                GladiaErrorEvent(
                    message=message,
                    session_id=self.session_id,
                    source_type="critical_event_overflow",
                    code="critical_event_overflow",
                )
            )
            receiver = self._receiver
            try:
                current = asyncio.current_task()
            except RuntimeError:
                current = None
            if receiver is not None and receiver is not current and not receiver.done():
                receiver.cancel()
            raise GladiaCriticalEventOverflow(message) from None

    async def stop(self, *, timeout: float = 60) -> GladiaLiveResult:
        """Stop within one wall-clock deadline and release all transport work."""

        if timeout <= 0:
            raise ValueError("stop timeout must be positive")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        cleanup_reserve = min(0.1, max(0.01, timeout * 0.25))
        io_deadline = deadline - cleanup_reserve
        stop_acquired = False
        cancelled = False
        public_error: GladiaTransportError | None = None
        receiver: asyncio.Task[None] | None = None

        try:
            await self._acquire_before(self._stop_lock, deadline)
            stop_acquired = True
            await self._acquire_before(self._state_lock, deadline)
            try:
                if self._state is SessionState.NEW:
                    self._state = SessionState.STOPPED
                    self._mark_abnormal("Gladia session stopped before connect")
                    self._events.publish_eos()
                    return self.result
                self._shutdown_deadline = deadline
                self._state = SessionState.STOPPING
                websocket = self._websocket
                receiver = self._receiver
                active_sends = [
                    task
                    for task in self._active_sends
                    if task is not asyncio.current_task() and not task.done()
                ]
                connect_task = self._connect_task
            finally:
                self._state_lock.release()

            for task in active_sends:
                task.cancel()
            if active_sends:
                try:
                    await self._wait_tasks_before(active_sends, io_deadline)
                except asyncio.TimeoutError:
                    public_error = GladiaTransportError(
                        "Gladia stop timed out cancelling active PCM sends"
                    )

            if (
                connect_task is not None
                and connect_task is not asyncio.current_task()
                and not connect_task.done()
            ):
                connect_task.cancel()
                try:
                    await self._wait_tasks_before([connect_task], io_deadline)
                except asyncio.TimeoutError:
                    public_error = public_error or GladiaTransportError(
                        "Gladia stop timed out cancelling session connect"
                    )

            if (
                public_error is None
                and websocket is not None
                and not websocket.closed
                and not self._stop_sent
                and self.result.completion is not CompletionState.NORMAL
            ):
                self._stop_sent = True
                try:
                    await self._await_before(
                        websocket.send_json({"type": "stop_recording"}), io_deadline
                    )
                except asyncio.TimeoutError:
                    public_error = GladiaTransportError(
                        "Gladia stop timed out sending stop_recording"
                    )
                except asyncio.CancelledError:
                    cancelled = True
                except Exception as exc:
                    safe = redact_sensitive_text(str(exc), self._api_key)
                    public_error = GladiaTransportError(
                        f"Could not send stop_recording: {safe or type(exc).__name__}"
                    )

            if (
                not cancelled
                and public_error is None
                and receiver is not None
            ):
                try:
                    if not receiver.done():
                        await self._await_before(
                            asyncio.shield(receiver), io_deadline
                        )
                    # Always retrieve an already-done task's failure. Awaiting a
                    # shield may have done so too; result() is safely repeatable.
                    receiver.result()
                except asyncio.TimeoutError:
                    public_error = GladiaTransportError(
                        "Gladia stop timed out waiting for end_session"
                    )
                except asyncio.CancelledError:
                    if receiver.cancelled():
                        public_error = GladiaTransportError(
                            "Gladia receiver ended by cancellation"
                        )
                    else:
                        cancelled = True
                except GladiaTransportError as exc:
                    public_error = exc
                except Exception as exc:
                    safe = redact_sensitive_text(str(exc), self._api_key)
                    public_error = GladiaTransportError(
                        f"Gladia receiver failed: {safe or type(exc).__name__}"
                    )
        except asyncio.TimeoutError:
            public_error = GladiaTransportError(
                "Gladia stop timed out before transport shutdown could start"
            )
        except asyncio.CancelledError:
            cancelled = True
        finally:
            if receiver is not None:
                if not receiver.done():
                    receiver.cancel()
                try:
                    await self._wait_tasks_before([receiver], deadline)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    cancelled = cancelled or asyncio.current_task().cancelling() > 0
            try:
                await self._cleanup_transport_shielded(deadline)
            except asyncio.CancelledError:
                cancelled = True
            except GladiaTransportError as exc:
                public_error = public_error or exc
            if public_error is not None:
                self._mark_abnormal(str(public_error))
            if cancelled:
                self._mark_abnormal("Gladia stop was cancelled")
            self._events.publish_eos()
            async with self._state_lock:
                self._state = SessionState.STOPPED
            if stop_acquired:
                self._stop_lock.release()

        if cancelled:
            raise asyncio.CancelledError
        if public_error is not None:
            raise public_error from None
        return self.result

    @staticmethod
    async def _acquire_before(lock: asyncio.Lock, deadline: float) -> None:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError
        await asyncio.wait_for(lock.acquire(), timeout=remaining)

    @staticmethod
    async def _await_before(awaitable: Any, deadline: float) -> Any:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            if hasattr(awaitable, "close"):
                awaitable.close()
            raise asyncio.TimeoutError
        future = asyncio.ensure_future(awaitable)
        try:
            done, _ = await asyncio.wait({future}, timeout=remaining)
        except asyncio.CancelledError:
            future.cancel()
            future.add_done_callback(GladiaLiveSession._consume_future_outcome)
            raise
        if not done:
            future.cancel()
            future.add_done_callback(GladiaLiveSession._consume_future_outcome)
            raise asyncio.TimeoutError
        return future.result()

    @staticmethod
    async def _wait_tasks_before(
        tasks: list[asyncio.Task[Any]], deadline: float
    ) -> None:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise asyncio.TimeoutError
        try:
            done, pending = await asyncio.wait(tasks, timeout=remaining)
        except asyncio.CancelledError:
            for task in tasks:
                if not task.done():
                    task.cancel()
                    task.add_done_callback(GladiaLiveSession._consume_future_outcome)
                else:
                    GladiaLiveSession._consume_future_outcome(task)
            raise
        for task in done:
            GladiaLiveSession._consume_future_outcome(task)
        if pending:
            for task in pending:
                task.cancel()
                task.add_done_callback(GladiaLiveSession._consume_future_outcome)
            raise asyncio.TimeoutError

    @staticmethod
    def _consume_future_outcome(future: asyncio.Future[Any]) -> None:
        """Retrieve detached timeout/cancellation outcomes without waiting."""

        try:
            future.exception()
        except (asyncio.CancelledError, Exception):
            pass

    async def _cleanup_transport_shielded(self, deadline: float | None = None) -> None:
        cleanup = asyncio.create_task(self._cleanup_transport(deadline))
        try:
            await self._wait_cleanup_task(cleanup, deadline)
        except asyncio.CancelledError:
            # Caller cancellation must not tear down the cleanup coroutine.
            # Continue waiting for it, but never beyond the original deadline.
            try:
                await self._wait_cleanup_task(cleanup, deadline)
            except asyncio.TimeoutError:
                cleanup.cancel()
                self._record_cleanup_error("Gladia transport close timed out")
            except (asyncio.CancelledError, GladiaTransportError):
                pass
            if cleanup.done():
                self._consume_future_outcome(cleanup)
            else:
                cleanup.add_done_callback(self._consume_future_outcome)
            raise
        except asyncio.TimeoutError:
            cleanup.cancel()
            if cleanup.done():
                self._consume_future_outcome(cleanup)
            else:
                cleanup.add_done_callback(self._consume_future_outcome)
            error = GladiaTransportError("Gladia transport close timed out")
            self._record_cleanup_error(str(error))
            raise error from None
        finally:
            if cleanup.done():
                # Explicitly consume every terminal outcome even when a shield
                # future was cancelled by its caller.
                self._consume_future_outcome(cleanup)

    @staticmethod
    async def _wait_cleanup_task(
        cleanup: asyncio.Task[None], deadline: float | None
    ) -> None:
        """Passively wait without propagating caller cancellation to cleanup."""

        timeout = None
        if deadline is not None:
            timeout = max(0.0, deadline - asyncio.get_running_loop().time())
        done, _ = await asyncio.wait({cleanup}, timeout=timeout)
        if not done:
            raise asyncio.TimeoutError
        cleanup.result()

    async def _cleanup_transport(self, deadline: float | None) -> None:
        errors: list[GladiaTransportError] = []
        websockets: list[aiohttp.ClientWebSocketResponse] = []
        for websocket in (self._websocket, self._handshake_websocket):
            if websocket is not None and all(
                websocket is not existing for existing in websockets
            ):
                websockets.append(websocket)
        self._websocket = None
        self._handshake_websocket = None
        for websocket in websockets:
            if websocket.closed:
                continue
            try:
                close = websocket.close()
                if deadline is None:
                    await close
                else:
                    await self._await_before(close, deadline)
            except asyncio.TimeoutError:
                self._force_close_websocket(websocket)
                errors.append(
                    GladiaTransportError("Gladia WebSocket close timed out")
                )
            except asyncio.CancelledError:
                self._force_close_websocket(websocket)
                errors.append(
                    GladiaTransportError("Gladia WebSocket close was cancelled")
                )
            except Exception as exc:
                self._force_close_websocket(websocket)
                safe = redact_sensitive_text(str(exc), self._api_key)
                errors.append(
                    GladiaTransportError(
                        f"Could not close Gladia WebSocket: "
                        f"{safe or type(exc).__name__}"
                    )
                )
        if self._owns_http and self._http is not None:
            http = self._http
            self._http = None
            try:
                close = http.close()
                if deadline is None:
                    await close
                else:
                    await self._await_before(close, deadline)
            except asyncio.TimeoutError:
                errors.append(
                    GladiaTransportError("Gladia HTTP session close timed out")
                )
            except asyncio.CancelledError:
                errors.append(
                    GladiaTransportError("Gladia HTTP session close was cancelled")
                )
            except Exception as exc:
                safe = redact_sensitive_text(str(exc), self._api_key)
                errors.append(
                    GladiaTransportError(
                        f"Could not close Gladia HTTP session: "
                        f"{safe or type(exc).__name__}"
                    )
                )
        for error in errors:
            self._record_cleanup_error(str(error))
        if errors:
            raise errors[0] from None

    @staticmethod
    def _force_close_websocket(websocket: Any) -> None:
        """Best-effort physical close after the async close budget expires.

        aiohttp exposes no public synchronous WebSocket abort. Its underlying
        ClientResponse does expose synchronous close(), so use that last-resort
        release without ever inspecting or reporting the bearer URL.
        """

        response = getattr(websocket, "_response", None)
        close = getattr(response, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def _record_cleanup_error(self, reason: str) -> None:
        """Record cleanup health without rewriting a normal protocol outcome."""

        safe_reason = redact_sensitive_text(reason, self._api_key)
        self._set_completion(CompletionState.ABNORMAL, safe_reason)
        if safe_reason not in self.result.errors:
            self.result.errors.append(safe_reason)

    def _set_completion(self, state: CompletionState, reason: str) -> bool:
        if self.result.completion is not CompletionState.PENDING:
            return False
        self.result.completion = state
        self.result.completion_reason = reason
        return True

    def _mark_abnormal(self, reason: str) -> None:
        if self.result.completion is CompletionState.NORMAL:
            return
        safe_reason = redact_sensitive_text(reason, self._api_key)
        self._set_completion(CompletionState.ABNORMAL, safe_reason)
        if safe_reason not in self.result.errors:
            self.result.errors.append(safe_reason)
            try:
                self._publish_event(
                    GladiaErrorEvent(message=safe_reason, session_id=self.session_id)
                )
            except GladiaCriticalEventOverflow:
                pass

    async def _receive_messages(self) -> None:
        assert self._websocket is not None
        websocket = self._websocket
        try:
            async for message in websocket:
                if message.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                    event = self.process_message(message.data)
                    if isinstance(event, LifecycleEvent) and event.event_type == "end_session":
                        return
                elif message.type == aiohttp.WSMsgType.ERROR:
                    safe = redact_sensitive_text(
                        str(websocket.exception()), self._api_key
                    )
                    error = GladiaTransportError(
                        f"Gladia WebSocket error: {safe or 'unknown transport error'}"
                    )
                    self._mark_abnormal(str(error))
                    raise error from None
                elif message.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                ):
                    break
        except asyncio.CancelledError:
            self._mark_abnormal("Gladia receiver was cancelled")
            raise
        except GladiaCriticalEventOverflow:
            return
        except GladiaTransportError:
            raise
        except Exception as exc:
            safe = redact_sensitive_text(str(exc), self._api_key)
            error = GladiaTransportError(
                f"Gladia receive failure: {safe or type(exc).__name__}"
            )
            self._mark_abnormal(str(error))
            raise error from None
        finally:
            if self.result.completion is CompletionState.PENDING:
                self._mark_abnormal(
                    f"Gladia WebSocket ended before end_session (close code {websocket.close_code})"
                )
            async with self._state_lock:
                if self.result.completion is CompletionState.NORMAL:
                    self._state = SessionState.STOPPED
                elif self._state is SessionState.CONNECTED:
                    self._state = SessionState.FAILED
            self._events.publish_eos()


async def transcribe_wave_live(
    path: str | Path,
    api_key: str,
    *,
    config: GladiaLiveConfig | None = None,
    on_final: Callable[[TranscriptUpdate], None] | None = None,
    on_transcript_update: Callable[[TranscriptUpdate], None] | None = None,
    init_url: str = GLADIA_LIVE_INIT_URL,
) -> GladiaLiveResult:
    """Replay a PCM WAV in real time through :class:`GladiaLiveSession`."""

    wave_path = Path(path)
    audio_format = inspect_pcm_wave(wave_path)
    # Replay retains its proven 0.5s behavior; live callers get the 0.1s config
    # default unless they deliberately provide another value.
    live_config = config or GladiaLiveConfig(endpointing=0.5)
    stream_format = audio_format
    if audio_format.channels == 2 and live_config.downmix_stereo:
        stream_format = WaveFormat(
            sample_rate=audio_format.sample_rate,
            channels=1,
            bit_depth=audio_format.bit_depth,
            frame_count=audio_format.frame_count,
        )

    session = GladiaLiveSession(
        api_key,
        stream_format,
        config=live_config,
        init_url=init_url,
    )
    async with session:
        consumer = asyncio.create_task(
            _consume_transcripts(session, on_final, on_transcript_update),
            name="gladia-wave-transcript-consumer",
        )
        try:
            frames_per_chunk = max(
                1,
                audio_format.sample_rate * live_config.frame_duration_ms // 1000,
            )
            with wave.open(str(wave_path), "rb") as reader:
                while source_chunk := reader.readframes(frames_per_chunk):
                    started = asyncio.get_running_loop().time()
                    chunk = source_chunk
                    if audio_format.channels == 2 and live_config.downmix_stereo:
                        chunk = pcm16_stereo_to_mono(source_chunk)
                    await session.send_pcm(chunk)
                    if consumer.done():
                        consumer.result()
                    frame_count = len(source_chunk) / (
                        audio_format.channels * (audio_format.bit_depth // 8)
                    )
                    target_delay = frame_count / audio_format.sample_rate
                    elapsed = asyncio.get_running_loop().time() - started
                    await asyncio.sleep(max(0.0, target_delay - elapsed))
                    if consumer.done():
                        consumer.result()
            if consumer.done():
                consumer.result()
            result = await session.stop()
            await consumer
            return result
        finally:
            if not consumer.done():
                consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)


async def _consume_transcripts(
    session: GladiaLiveSession,
    on_final: Callable[[TranscriptUpdate], None] | None,
    on_transcript_update: Callable[[TranscriptUpdate], None] | None,
) -> None:
    async for event in session.iter_events():
        if not isinstance(event, TranscriptUpdate):
            continue
        if on_transcript_update is not None:
            on_transcript_update(event)
        if event.is_final and on_final is not None:
            on_final(event)


def _print_update(update: TranscriptUpdate) -> None:
    label = "final" if update.is_final else "partial"
    print(f"[{label}] {update.text}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay a PCM WAV through Gladia Live V2",
    )
    parser.add_argument("wave_file", type=Path)
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="dotenv file containing GLADIA_API_KEY",
    )
    parser.add_argument("--language", default="en")
    parser.add_argument("--endpointing", type=float, default=0.5)
    parser.add_argument("--speech-threshold", type=float, default=0.6)
    parser.add_argument("--audio-enhancer", action="store_true")
    parser.add_argument(
        "--preserve-stereo",
        action="store_true",
        help="Send both input channels instead of downmixing to mono",
    )
    return parser.parse_args()


async def _main() -> int:
    args = _parse_args()
    env_file = args.env_file or (
        Path(os.environ["ENV_FILE"]) if os.getenv("ENV_FILE") else None
    )
    if env_file is not None:
        if not load_dotenv(env_file):
            raise SystemExit(f"Could not load environment file: {env_file}")
    else:
        load_dotenv()

    api_key = os.getenv("GLADIA_API_KEY", "")
    if not api_key:
        raise SystemExit(
            "GLADIA_API_KEY is missing. Add it to the selected environment file."
        )

    audio_format = inspect_pcm_wave(args.wave_file)
    print(
        "Replaying "
        f"{audio_format.duration_seconds:.2f}s, "
        f"{audio_format.sample_rate} Hz, "
        f"{audio_format.channels} input channel(s)"
    )
    result = await transcribe_wave_live(
        args.wave_file,
        api_key,
        config=GladiaLiveConfig(
            language=args.language,
            endpointing=args.endpointing,
            speech_threshold=args.speech_threshold,
            audio_enhancer=args.audio_enhancer,
            downmix_stereo=not args.preserve_stereo,
        ),
        on_transcript_update=_print_update,
    )
    print(f"Transcript: {result.transcript or '(no final transcript)'}")
    if result.errors:
        print("Errors:")
        for error in result.errors:
            print(f"- {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
