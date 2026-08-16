"""Single-speaker Discord-to-Gladia live transcription lifecycle.

This module transports audio and stable transcript text only.  It deliberately
does not call ``CompanionApp.respond()``, mutate history, save raw PCM, or own
outbound speech.
"""

from __future__ import annotations

import asyncio
from array import array
from bisect import bisect_left
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
import logging
import math
from typing import Any, Callable, Protocol

from discord.ext import voice_recv

from ..adapters.gladia_live import (
    CompletionState,
    GladiaErrorEvent,
    GladiaLiveConfig,
    GladiaLiveSession,
    TranscriptUpdate,
    WaveFormat,
    redact_sensitive_text,
)
from ..config import RuntimeConfig
from .voice_capture import CaptureSummary, WaveCaptureSession
from .voice_compat import install_voice_receive_compatibility
from .voice_sink import DiagnosticWaveSink, LivePCMFrame, LivePCMSink


log = logging.getLogger(__name__)

# A disconnect coroutine that ignores cancellation cannot be awaited without
# surrendering the shutdown wall clock. It remains owned by its FAILED session
# and is mirrored here solely so its eventual outcome is retrieved. The manager
# retains that guild's session, preventing restart or another disconnect task,
# until the exact coroutine resolves and an explicit stop retry finalizes it.
_QUARANTINED_DISCONNECT_TASKS: set[asyncio.Task[Any]] = set()


def _observe_quarantined_disconnect(task: asyncio.Task[Any]) -> None:
    _QUARANTINED_DISCONNECT_TASKS.discard(task)
    try:
        error = task.exception()
    except asyncio.CancelledError:
        return
    if error is not None:
        log.error(
            "Quarantined Discord disconnect finished with %s", type(error).__name__
        )


def _quarantine_disconnect(task: asyncio.Task[Any]) -> None:
    if getattr(task, "_disco_proxy_observed", False):
        return
    setattr(task, "_disco_proxy_observed", True)
    _QUARANTINED_DISCONNECT_TASKS.add(task)
    task.add_done_callback(_observe_quarantined_disconnect)


def _install_cache_safe_cleanup(
    voice_client: Any,
    current_voice_client: Callable[[], Any | None],
) -> tuple[Callable[[], None] | None, bool]:
    """Prevent an old client's late cleanup from evicting its replacement."""

    cleanup = getattr(voice_client, "cleanup", None)
    if not callable(cleanup):
        return None, False
    if getattr(voice_client, "_disco_proxy_cleanup_guarded", False):
        return cleanup, True

    original_cleanup = cleanup

    def guarded_cleanup() -> None:
        current = current_voice_client()
        if current is not None and current is not voice_client:
            return
        original_cleanup()

    try:
        setattr(voice_client, "cleanup", guarded_cleanup)
        setattr(voice_client, "_disco_proxy_cleanup_guarded", True)
    except Exception:
        return original_cleanup, False
    return guarded_cleanup, True

SAMPLE_RATE = 48_000
FRAME_SAMPLES = 960
FRAME_SECONDS = FRAME_SAMPLES / SAMPLE_RATE
MONO_FRAME_BYTES = FRAME_SAMPLES * 2
RTP_MODULUS = 2**32
RTP_HALF_RANGE = 2**31
MAX_CREDIBLE_RTP_JUMP_SAMPLES = SAMPLE_RATE * 10
DEFAULT_PRE_ROLL_SECONDS = 0.1
DEFAULT_SHUTDOWN_STEP_SECONDS = 2.0
DEFAULT_GLADIA_STOP_SECONDS = 15.0
REPORT_QUEUE_LIMIT = 8


class VoiceSessionError(RuntimeError):
    """A safe, user-facing live voice lifecycle error."""


class VoiceSessionState(str, Enum):
    NEW = "new"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass
class VoiceSessionCounters:
    received_packets: int = 0
    enqueued_packets: int = 0
    ingress_drops: int = 0
    queue_drops: int = 0
    sent_frames: int = 0
    inserted_silence_samples: int = 0
    rtp_gap_samples: int = 0
    late_audio_samples: int = 0
    overlap_samples: int = 0
    missing_rtp_packets: int = 0
    out_of_order_packets: int = 0
    rtp_discontinuities: int = 0
    playout_reanchors: int = 0
    clock_dropped_packets: int = 0
    pending_drops: int = 0
    sender_late_ticks: int = 0
    partial_transcripts: int = 0
    final_transcripts: int = 0
    report_drops: int = 0


@dataclass(frozen=True)
class VoiceSessionStatus:
    guild_id: int
    channel_id: int
    starter_user_id: int
    starter_name: str
    state: VoiceSessionState
    queue_size: int
    queue_capacity: int
    ingress_pending: int
    counters: VoiceSessionCounters
    last_error: str | None
    gladia_completion: str


@dataclass
class _TimedMono:
    start: int
    pcm: bytes

    @property
    def samples(self) -> int:
        return len(self.pcm) // 2

    @property
    def end(self) -> int:
        return self.start + self.samples


def stereo_pcm16_to_mono(pcm: bytes) -> bytes:
    """Downmix little-endian interleaved stereo PCM16 without overflow."""

    if len(pcm) % 4:
        raise ValueError("Discord PCM must contain complete stereo PCM16 frames")
    samples = array("h")
    samples.frombytes(pcm)
    mono = array(
        "h",
        (
            (int(samples[i]) + int(samples[i + 1])) // 2
            for i in range(0, len(samples), 2)
        ),
    )
    return mono.tobytes()


class MonoRtpClock:
    """Render a bounded, ordered 20 ms mono RTP timeline."""

    def __init__(
        self,
        counters: VoiceSessionCounters,
        *,
        max_pending_packets: int = 100,
        max_credible_jump_samples: int = MAX_CREDIBLE_RTP_JUMP_SAMPLES,
    ) -> None:
        if max_pending_packets < 1:
            raise ValueError("max_pending_packets must be positive")
        if max_credible_jump_samples < FRAME_SAMPLES:
            raise ValueError("max_credible_jump_samples must be at least one frame")
        self._counters = counters
        self._max_pending_packets = max_pending_packets
        self._max_credible_jump = max_credible_jump_samples
        self._anchor_rtp: int | None = None
        self._anchor_sample = 0
        self._cursor = 0
        self._timeline_started = False
        self._last_valid_start: int | None = None
        self._pending: list[_TimedMono] = []

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def _pending_end(self) -> int:
        return max((segment.end for segment in self._pending), default=self._cursor)

    def push(self, frame: LivePCMFrame) -> None:
        mono = stereo_pcm16_to_mono(frame.pcm)
        if not mono:
            return
        timestamp = frame.rtp_timestamp
        forward_progressing = False
        if timestamp is None:
            self._counters.missing_rtp_packets += 1
            start = max(self._cursor, self._pending_end())
        elif self._anchor_rtp is None:
            # Missing-timestamp packets form a provisional timeline. The first
            # valid timestamp anchors after them instead of overlapping them.
            self._anchor_rtp = int(timestamp) % RTP_MODULUS
            self._anchor_sample = max(self._cursor, self._pending_end())
            start = self._anchor_sample
            self._last_valid_start = start
            forward_progressing = True
        else:
            delta = (int(timestamp) - self._anchor_rtp) % RTP_MODULUS
            signed_delta = delta if delta < RTP_HALF_RANGE else delta - RTP_MODULUS
            start = self._anchor_sample + signed_delta
            reference = max(self._cursor, self._pending_end())
            if abs(start - reference) > self._max_credible_jump:
                self._reset_for_discontinuity(int(timestamp), mono)
                return
            previous_valid_start = self._last_valid_start
            if previous_valid_start is not None and start < previous_valid_start:
                self._counters.out_of_order_packets += 1
            forward_progressing = (
                previous_valid_start is None or start > previous_valid_start
            )
            previous_floor = (
                previous_valid_start
                if previous_valid_start is not None
                else start
            )
            self._last_valid_start = max(previous_floor, start)
        self._timeline_started = True
        self._insert_segment(
            _TimedMono(start=start, pcm=mono),
            allow_late_reanchor=timestamp is not None and forward_progressing,
        )

    def _reset_for_discontinuity(self, timestamp: int, pcm: bytes) -> None:
        dropped = len(self._pending)
        self._counters.rtp_discontinuities += 1
        self._counters.pending_drops += dropped
        self._counters.clock_dropped_packets += dropped
        self._pending.clear()
        self._anchor_rtp = timestamp % RTP_MODULUS
        self._anchor_sample = self._cursor
        self._last_valid_start = self._cursor
        self._timeline_started = True
        self._insert_segment(_TimedMono(start=self._cursor, pcm=pcm))

    def _insert_segment(
        self,
        segment: _TimedMono,
        *,
        allow_late_reanchor: bool = False,
    ) -> None:
        if segment.start < self._cursor:
            late = min(self._cursor - segment.start, segment.samples)
            if late >= segment.samples:
                if allow_late_reanchor and not self._pending:
                    # The pinned jitter buffer may intentionally release one
                    # forward packet per tick while remaining at bounded full
                    # depth after a sequence gap. Re-anchor that monotonic
                    # stream once instead of dropping every later packet.
                    shift = self._cursor - segment.start
                    self._anchor_sample += shift
                    segment.start = self._cursor
                    self._last_valid_start = self._cursor
                    self._counters.playout_reanchors += 1
                    self._counters.rtp_discontinuities += 1
                else:
                    self._counters.late_audio_samples += late
                    self._counters.clock_dropped_packets += 1
                    return
            else:
                self._counters.late_audio_samples += late
                segment.pcm = segment.pcm[late * 2:]
                segment.start += late

        starts = [item.start for item in self._pending]
        index = bisect_left(starts, segment.start)
        if index:
            previous = self._pending[index - 1]
            overlap = min(max(0, previous.end - segment.start), segment.samples)
            if overlap:
                self._counters.overlap_samples += overlap
                if overlap >= segment.samples:
                    self._counters.clock_dropped_packets += 1
                    return
                segment.pcm = segment.pcm[overlap * 2:]
                segment.start += overlap

        if index < len(self._pending):
            following = self._pending[index]
            if segment.end > following.start:
                keep = max(0, following.start - segment.start)
                self._counters.overlap_samples += segment.samples - keep
                if keep == 0:
                    self._counters.clock_dropped_packets += 1
                    return
                segment.pcm = segment.pcm[:keep * 2]

        starts = [item.start for item in self._pending]
        index = bisect_left(starts, segment.start)
        if len(self._pending) >= self._max_pending_packets:
            self._counters.pending_drops += 1
            self._counters.clock_dropped_packets += 1
            if index >= len(self._pending):
                return
            self._pending.pop()
        self._pending.insert(index, segment)

    def render(self) -> bytes:
        """Return exactly one frame with non-negative silence metrics."""

        if not self._timeline_started and not self._pending:
            self._counters.inserted_silence_samples += FRAME_SAMPLES
            return bytes(MONO_FRAME_BYTES)

        frame_start = self._cursor
        frame_end = frame_start + FRAME_SAMPLES
        output = bytearray(MONO_FRAME_BYTES)
        copied = 0
        future_audio = False
        while self._pending:
            segment = self._pending[0]
            if segment.end <= frame_start:
                self._counters.late_audio_samples += segment.samples
                self._counters.clock_dropped_packets += 1
                self._pending.pop(0)
                continue
            if segment.start >= frame_end:
                future_audio = True
                break
            source_start = max(frame_start, segment.start)
            source_end = min(frame_end, segment.end)
            sample_count = max(0, source_end - source_start)
            if sample_count:
                source_offset = (source_start - segment.start) * 2
                target_offset = (source_start - frame_start) * 2
                output[target_offset:target_offset + sample_count * 2] = segment.pcm[
                    source_offset:source_offset + sample_count * 2
                ]
                copied += sample_count
            if segment.end <= frame_end:
                self._pending.pop(0)
            else:
                consumed = max(0, frame_end - segment.start)
                segment.pcm = segment.pcm[consumed * 2:]
                segment.start += consumed
                break

        silent = max(0, FRAME_SAMPLES - min(copied, FRAME_SAMPLES))
        self._counters.inserted_silence_samples += silent
        if silent and future_audio:
            self._counters.rtp_gap_samples += silent
        self._cursor = frame_end
        return bytes(output)


class _TextChannel(Protocol):
    async def send(self, content: str) -> Any: ...


GladiaFactory = Callable[..., GladiaLiveSession]
TerminalCallback = Callable[["VoiceSession"], None]


class VoiceSession:
    """Own one guild's single-speaker live transcription session."""

    def __init__(
        self,
        *,
        guild_id: int,
        voice_channel: Any,
        text_channel: _TextChannel,
        starter_user_id: int,
        starter_name: str,
        config: RuntimeConfig,
        gladia_factory: GladiaFactory = GladiaLiveSession,
        pre_roll_seconds: float = DEFAULT_PRE_ROLL_SECONDS,
        shutdown_step_seconds: float = DEFAULT_SHUTDOWN_STEP_SECONDS,
        on_terminal: TerminalCallback | None = None,
    ) -> None:
        self.guild_id = int(guild_id)
        self.voice_channel = voice_channel
        self.text_channel = text_channel
        self.starter_user_id = int(starter_user_id)
        self.starter_name = starter_name
        self.config = config
        self.queue_capacity = max(1, math.ceil(config.voice_queue_seconds / FRAME_SECONDS))
        self.queue: asyncio.Queue[LivePCMFrame] = asyncio.Queue(self.queue_capacity)
        self.counters = VoiceSessionCounters()
        self.state = VoiceSessionState.NEW
        self.last_error: str | None = None
        self.voice_client: Any | None = None
        self._voice_client_borrowed = False
        self._preserve_borrowed_receiver = False
        self._voice_cache_clean = True
        self._connect_task: asyncio.Task[Any] | None = None
        self._connect_candidate: Any | None = None
        self._disconnect_task: asyncio.Task[Any] | None = None
        self._disconnect_cleanup_done = False
        self.gladia: GladiaLiveSession | None = None
        self._gladia_stop_task: asyncio.Task[Any] | None = None
        self.sink: LivePCMSink | None = None
        self._gladia_factory = gladia_factory
        self._pre_roll_seconds = max(0.0, pre_roll_seconds)
        self._shutdown_step_seconds = max(0.01, shutdown_step_seconds)
        self._gladia_stop_seconds = max(
            0.01,
            float(
                getattr(
                    config,
                    "voice_gladia_stop_seconds",
                    DEFAULT_GLADIA_STOP_SECONDS,
                )
            ),
        )
        self._on_terminal = on_terminal
        self._loop: asyncio.AbstractEventLoop | None = None
        self._listener_done: asyncio.Future[Exception | None] | None = None
        self._listen_started = False
        self._stop_event = asyncio.Event()
        self._sender_task: asyncio.Task[None] | None = None
        self._consumer_task: asyncio.Task[None] | None = None
        self._reporter_task: asyncio.Task[None] | None = None
        self._shutdown_task: asyncio.Task[VoiceSessionStatus] | None = None
        self._failure_task: asyncio.Task[VoiceSessionStatus] | None = None
        self._reports: OrderedDict[str, str] = OrderedDict()
        self._report_ready = asyncio.Event()
        self._report_stop = False
        self._starter_left_during_start = False

    def status(self) -> VoiceSessionStatus:
        if self.sink is not None:
            self.counters.ingress_drops = self.sink.dropped_frames
        completion = (
            self.gladia.completion.value
            if self.gladia is not None
            else CompletionState.PENDING.value
        )
        return VoiceSessionStatus(
            guild_id=self.guild_id,
            channel_id=int(self.voice_channel.id),
            starter_user_id=self.starter_user_id,
            starter_name=self.starter_name,
            state=self.state,
            queue_size=self.queue.qsize(),
            queue_capacity=self.queue_capacity,
            ingress_pending=self.sink.pending_frames if self.sink is not None else 0,
            counters=self.counters,
            last_error=self.last_error,
            gladia_completion=completion,
        )

    def enqueue_from_loop(self, frame: LivePCMFrame) -> None:
        self.counters.received_packets += 1
        if self.state is not VoiceSessionState.RUNNING:
            return
        try:
            self.queue.put_nowait(frame)
            self.counters.enqueued_packets += 1
        except asyncio.QueueFull:
            self.counters.queue_drops += 1
            self._queue_report(
                "event_queue_drop",
                f"Discord event-loop audio queue dropped {self.counters.queue_drops} "
                "packet(s); transcription may have a gap",
            )

    def _ingress_drops(self, total: int) -> None:
        self.counters.ingress_drops = total
        self._queue_report(
            "thread_ingress_drop",
            f"Discord receive-thread audio ring dropped {total} packet(s); "
            "transcription may have a gap",
        )

    async def start(self, existing_voice_client: Any | None = None) -> VoiceSessionStatus:
        if self.state is not VoiceSessionState.NEW:
            raise VoiceSessionError(f"Voice session is already {self.state.value}")
        self.state = VoiceSessionState.STARTING
        self._loop = asyncio.get_running_loop()
        self._listener_done = self._loop.create_future()
        original_error: BaseException | None = None
        try:
            install_voice_receive_compatibility()
            voice_client = existing_voice_client
            if voice_client is not None:
                self.voice_client = voice_client
                self._voice_client_borrowed = True
                self._preserve_borrowed_receiver = True
                self._voice_cache_clean = False
            if voice_client is not None and not self._is_receive_client(voice_client):
                if not await self._disconnect_voice_client(voice_client):
                    raise VoiceSessionError(
                        "Existing Discord voice client could not be released; retry"
                    )
                voice_client = None
                self._preserve_borrowed_receiver = False
                self._raise_if_starter_left_during_start()
            elif voice_client is not None:
                # An already-active borrowed receiver belongs to another
                # subsystem. Rejection must leave it completely untouched.
                if voice_client.is_listening():
                    raise VoiceSessionError(
                        "Discord voice receive is already in use in this server"
                    )
                # An idle borrowed connection is now accepted for this session;
                # movement or later listening transfers cleanup responsibility.
                self._preserve_borrowed_receiver = False
            if voice_client is None:
                self._connect_task = asyncio.create_task(
                    self.voice_channel.connect(
                        cls=self._make_voice_recv_client,
                        self_deaf=False,
                    ),
                    name=f"voice-connect-g{self.guild_id}",
                )
                self._connect_task.add_done_callback(self._observe_connect_task)
                voice_client = await asyncio.shield(self._connect_task)
                # Ownership is established immediately after connect returns;
                # every later failure/cancellation can now find and clean it.
                self._adopt_connected_client(voice_client)
                self._connect_task = None
                self._raise_if_starter_left_during_start()
            elif getattr(voice_client, "channel", None) != self.voice_channel:
                await voice_client.move_to(self.voice_channel)
                self._raise_if_starter_left_during_start()
            if not self._voice_client_borrowed and voice_client.is_listening():
                raise VoiceSessionError("Discord voice receive is already in use in this server")
            self._raise_if_starter_left_during_start()
            self.gladia = self._gladia_factory(
                self.config.gladia_api_key,
                WaveFormat(SAMPLE_RATE, 1, 16),
                config=GladiaLiveConfig(endpointing=self.config.voice_endpointing_seconds),
            )
            await self.gladia.connect()
            self._raise_if_starter_left_during_start()
            self.sink = LivePCMSink(
                self._loop,
                self.starter_user_id,
                self.enqueue_from_loop,
                capacity_frames=self.queue_capacity,
                on_drops=self._ingress_drops,
            )
            self._reporter_task = asyncio.create_task(
                self._reporter_loop(), name=f"voice-reporter-g{self.guild_id}"
            )
            self._sender_task = asyncio.create_task(
                self._sender_loop(), name=f"voice-pcm-sender-g{self.guild_id}"
            )
            self._consumer_task = asyncio.create_task(
                self._consume_transcripts(), name=f"voice-transcripts-g{self.guild_id}"
            )
            self.state = VoiceSessionState.RUNNING
            voice_client.listen(self.sink, after=self._listener_after)
            self._listen_started = True
            return self.status()
        except BaseException as exc:
            original_error = exc
            self.last_error = self._safe_error(exc)
            self.state = VoiceSessionState.FAILED
        cleanup_error: VoiceSessionError | None = None
        try:
            await self.stop()
        except asyncio.CancelledError:
            if not isinstance(original_error, asyncio.CancelledError):
                raise
        except VoiceSessionError as exc:
            cleanup_error = exc
        assert original_error is not None
        if isinstance(original_error, asyncio.CancelledError):
            raise original_error
        if cleanup_error is not None:
            raise cleanup_error
        if isinstance(original_error, VoiceSessionError):
            raise original_error
        raise VoiceSessionError(
            f"Live voice startup failed ({type(original_error).__name__})"
        ) from None

    def note_starter_left_during_start(self) -> bool:
        """Latch one departure intent while the canonical session is STARTING."""

        if self.state is not VoiceSessionState.STARTING:
            return False
        if self._starter_left_during_start:
            return False
        self._starter_left_during_start = True
        return True

    def _raise_if_starter_left_during_start(self) -> None:
        if self._starter_left_during_start:
            raise VoiceSessionError(
                "The session starter left the voice channel while live voice was starting"
            )

    @staticmethod
    def _is_receive_client(voice_client: Any) -> bool:
        return isinstance(voice_client, voice_recv.VoiceRecvClient) or all(
            hasattr(voice_client, name)
            for name in ("listen", "stop_listening", "is_listening")
        )

    def _adopt_connected_client(self, voice_client: Any) -> None:
        self.voice_client = voice_client
        self._voice_client_borrowed = False
        self._preserve_borrowed_receiver = False
        self._voice_cache_clean = False

    def _make_voice_recv_client(self, client: Any, channel: Any) -> Any:
        voice_client = voice_recv.VoiceRecvClient(client, channel)
        # Connectable.connect invokes cls before it installs the guild cache.
        # This exact object is the only cache entry recovery may ever claim.
        self._connect_candidate = voice_client
        return voice_client

    def _observe_connect_task(self, task: asyncio.Task[Any]) -> None:
        """Retrieve late handshake outcomes and retain any cached client handle."""

        try:
            voice_client = task.result()
        except (asyncio.CancelledError, Exception):
            self._recover_new_cached_voice_client()
            return
        if self.voice_client is None:
            self._adopt_connected_client(voice_client)

    async def _finish_connect_acquisition(self) -> bool:
        task = self._connect_task
        if task is None:
            return True
        if not task.done():
            try:
                await asyncio.wait_for(
                    asyncio.shield(task), timeout=self._shutdown_step_seconds
                )
            except asyncio.TimeoutError:
                self.last_error = (
                    "Discord voice ownership acquisition did not finish; retry stop"
                )
                return False
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
        try:
            voice_client = task.result()
        except (asyncio.CancelledError, Exception):
            self._recover_new_cached_voice_client()
            self._connect_task = None
            return True
        self._adopt_connected_client(voice_client)
        self._connect_task = None
        return True

    def _current_cached_voice_client(self) -> Any | None:
        guild = getattr(self.voice_channel, "guild", None)
        return getattr(guild, "voice_client", None) if guild is not None else None

    def _recover_new_cached_voice_client(self) -> Any | None:
        """Claim only the exact client constructed for this connect attempt."""

        if self.voice_client is not None or self._voice_client_borrowed:
            return self.voice_client
        candidate = self._connect_candidate
        if candidate is None:
            return None
        self._adopt_connected_client(candidate)
        return candidate

    def _listener_after(self, error: Exception | None) -> None:
        loop = self._loop
        if loop is None:
            return

        def finish() -> None:
            if self._listener_done is not None and not self._listener_done.done():
                self._listener_done.set_result(error)
            if self.state in {VoiceSessionState.STOPPING, VoiceSessionState.STOPPED}:
                return
            message = (
                "Discord receiver ended unexpectedly"
                if error is None
                else f"Discord receiver stopped: {type(error).__name__}"
            )
            self._trigger_failure("discord_receiver", message)

        loop.call_soon_threadsafe(finish)

    async def _sender_loop(self) -> None:
        assert self.gladia is not None
        clock = MonoRtpClock(self.counters, max_pending_packets=self.queue_capacity)
        try:
            if self._pre_roll_seconds:
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=self._pre_roll_seconds
                    )
                    return
                except asyncio.TimeoutError:
                    pass
            loop = asyncio.get_running_loop()
            deadline = loop.time()
            while not self._stop_event.is_set():
                # Drain the bounded event-loop queue before rendering so a
                # decoder burst is ordered by RTP rather than artificially
                # admitted one frame per playout tick.
                for _ in range(self.queue_capacity):
                    try:
                        clock.push(self.queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                await self.gladia.send_pcm(clock.render())
                self.counters.sent_frames += 1
                deadline += FRAME_SECONDS
                now = loop.time()
                if deadline < now - (FRAME_SECONDS * 5):
                    self.counters.sender_late_ticks += 1
                    deadline = now
                delay = max(0.0, deadline - loop.time())
                if delay:
                    try:
                        await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                    except asyncio.TimeoutError:
                        pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._trigger_failure(
                "gladia_sender",
                self._safe_error(exc, prefix="Gladia audio sender stopped"),
            )

    async def _consume_transcripts(self) -> None:
        assert self.gladia is not None
        try:
            async for event in self.gladia.iter_events():
                if isinstance(event, TranscriptUpdate):
                    text = event.text.strip()
                    if not text:
                        continue
                    if event.is_final:
                        self.counters.final_transcripts += 1
                        print(f"[voice:{self.guild_id}] [final] {self.starter_name}: {text}")
                        await self.text_channel.send(
                            f"**{self.starter_name} (voice transcript):** {text}"
                        )
                    else:
                        self.counters.partial_transcripts += 1
                        print(f"[voice:{self.guild_id}] [partial] {text}")
                elif isinstance(event, GladiaErrorEvent):
                    self._trigger_failure("gladia_error", f"Gladia reported: {event.message}")
                    return
            if self.state is VoiceSessionState.RUNNING:
                if self.gladia.completion is CompletionState.ABNORMAL:
                    reason = self.gladia.result.completion_reason or "unknown transport ending"
                    self._trigger_failure(
                        "gladia_completion",
                        f"Gladia session ended abnormally: {reason}",
                    )
                else:
                    self._trigger_failure(
                        "gladia_completion", "Gladia event stream ended unexpectedly"
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._trigger_failure(
                "gladia_consumer",
                self._safe_error(exc, prefix="Gladia event receiver stopped"),
            )

    def _trigger_failure(self, key: str, message: str) -> None:
        if self.state in {VoiceSessionState.STOPPING, VoiceSessionState.STOPPED}:
            return
        safe = redact_sensitive_text(message, self.config.gladia_api_key)
        self.last_error = safe
        self.state = VoiceSessionState.FAILED
        self._queue_report(key, safe)
        if self._shutdown_task is None:
            self._shutdown_task = asyncio.create_task(
                self._shutdown(), name=f"voice-failure-shutdown-g{self.guild_id}"
            )
            self._failure_task = self._shutdown_task
            self._shutdown_task.add_done_callback(self._observe_failure_shutdown)

    def _observe_failure_shutdown(self, task: asyncio.Task[VoiceSessionStatus]) -> None:
        """Retrieve every automatic shutdown outcome and log only redacted text."""

        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is None:
            return
        safe = redact_sensitive_text(str(error), self.config.gladia_api_key)
        log.error("Automatic live voice shutdown failed: %s", safe)

    def _queue_report(self, key: str, message: str) -> None:
        safe = redact_sensitive_text(message, self.config.gladia_api_key)
        if key in self._reports:
            self._reports.pop(key)
        elif len(self._reports) >= REPORT_QUEUE_LIMIT:
            self._reports.popitem(last=False)
            self.counters.report_drops += 1
        self._reports[key] = safe
        self._report_ready.set()

    async def _reporter_loop(self) -> None:
        while True:
            await self._report_ready.wait()
            while self._reports:
                _, message = self._reports.popitem(last=False)
                log.warning("Live voice session g%s: %s", self.guild_id, message)
                try:
                    await self.text_channel.send(
                        f"⚠️ Live voice transport warning: {message}"
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    safe = redact_sensitive_text(
                        str(exc), self.config.gladia_api_key
                    )
                    log.error(
                        "Could not report live voice transport warning: %s",
                        safe or type(exc).__name__,
                    )
            self._report_ready.clear()
            if self._report_stop and not self._reports:
                return

    async def stop(self) -> VoiceSessionStatus:
        """Latch caller cancellation, finish bounded cleanup, then re-raise."""

        if (
            self._shutdown_task is not None
            and self._shutdown_task.done()
            and self.state is not VoiceSessionState.STOPPED
        ):
            self._consume_task_result(self._shutdown_task)
            if self._failure_task is self._shutdown_task:
                self._failure_task = None
            self._shutdown_task = None
        if self._shutdown_task is None:
            self._shutdown_task = asyncio.create_task(
                self._shutdown(), name=f"voice-shutdown-g{self.guild_id}"
            )
        shutdown = self._shutdown_task
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(shutdown)
                break
            except asyncio.CancelledError:
                cancelled = True
                if shutdown.done():
                    result = shutdown.result()
                    break
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def _shutdown(self) -> VoiceSessionStatus:
        if self.state is VoiceSessionState.STOPPED:
            return self.status()
        self.state = VoiceSessionState.STOPPING
        self._stop_event.set()
        connect_terminal = await self._finish_connect_acquisition()
        voice_client = self.voice_client
        if self.sink is not None:
            self.sink.cleanup()
        preserve_borrowed = self._preserve_borrowed_receiver
        if voice_client is not None and not preserve_borrowed:
            try:
                if voice_client.is_listening():
                    voice_client.stop_listening()
            except Exception as exc:
                self.last_error = self._safe_error(exc, prefix="Discord receive stop failed")
        if (
            self._listen_started
            and self._listener_done is not None
            and not self._listener_done.done()
        ):
            await self._bounded_wait(
                asyncio.shield(self._listener_done),
                "Discord receiver shutdown timed out",
            )
            if not self._listener_done.done():
                self._listener_done.cancel()
        sender_terminal = await self._cancel_task(self._sender_task, "audio sender")
        gladia_terminal = await self._finish_gladia()
        consumer_terminal = await self._finish_consumer()
        if preserve_borrowed:
            # Drop only our reference. The active borrowed receiver and its
            # Discord cache entry remain owned by the pre-existing subsystem.
            self.voice_client = None
            self._voice_cache_clean = True
            disconnect_terminal = True
        else:
            disconnect_terminal = await self._disconnect_voice_client(voice_client)
        resources_terminal = all(
            (
                connect_terminal,
                sender_terminal,
                gladia_terminal,
                consumer_terminal,
                disconnect_terminal,
            )
        )
        if not resources_terminal:
            self.state = VoiceSessionState.FAILED
            self.last_error = (
                "Live voice shutdown left an owned resource non-terminal; retry stop"
            )
            self._queue_report("shutdown_nonterminal", self.last_error)
        reporter_terminal = await self._finish_reporter()
        if not resources_terminal or not reporter_terminal:
            self.state = VoiceSessionState.FAILED
            self._log_terminal_status()
            raise VoiceSessionError(
                "Live voice shutdown left an owned resource non-terminal; retry stop"
            )
        self.state = VoiceSessionState.STOPPED
        self._log_terminal_status()
        if self._on_terminal is not None:
            try:
                self._on_terminal(self)
            except Exception:
                log.exception("Voice session terminal callback failed")
        return self.status()

    async def _finish_gladia(self) -> bool:
        """Give Live V2 its own drain deadline without surrendering our wall clock."""

        if self.gladia is None:
            return True
        task = self._gladia_stop_task
        if task is None:
            task = asyncio.create_task(
                self.gladia.stop(timeout=self._gladia_stop_seconds),
                name=f"voice-gladia-stop-g{self.guild_id}",
            )
            self._gladia_stop_task = task
            task.add_done_callback(self._observe_gladia_stop)
        if not task.done():
            # Passive waiting is important: unlike wait_for(), this deadline
            # never cancels Gladia while it is draining final transcripts.
            done, _ = await asyncio.wait(
                {task}, timeout=self._gladia_stop_seconds
            )
            if task not in done:
                # If Gladia's own timeout fired on the same loop tick, let its
                # owned cleanup publish that terminal result before forcing it.
                await asyncio.sleep(0)
            if not task.done():
                self.last_error = "Gladia shutdown exceeded its drain deadline"
                self._queue_report("gladia_shutdown_timeout", self.last_error)
                task.cancel()
                await asyncio.sleep(0)
                if not task.done():
                    return False
        try:
            task.result()
        except asyncio.CancelledError:
            self.last_error = "Gladia shutdown was cancelled"
            self._queue_report("gladia_shutdown_cancelled", self.last_error)
        except Exception as exc:
            self.last_error = self._safe_error(
                exc, prefix="Gladia shutdown failed"
            )
            self._queue_report("gladia_shutdown_error", self.last_error)
        self._gladia_stop_task = None
        return True

    def _observe_gladia_stop(self, task: asyncio.Task[Any]) -> None:
        """Retrieve every late stop failure without exposing transport details."""

        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            log.error("Gladia stop task finished with %s", type(error).__name__)

    def _log_terminal_status(self) -> None:
        status = self.status()
        counters = status.counters
        log.info(
            "Live voice terminal guild=%s channel=%s starter=%s state=%s "
            "received=%s enqueued=%s ingress_drops=%s queue_drops=%s "
            "sent_frames=%s rtp_gap_samples=%s rtp_discontinuities=%s "
            "playout_reanchors=%s "
            "clock_dropped=%s pending_drops=%s late_samples=%s "
            "partials=%s finals=%s",
            status.guild_id,
            status.channel_id,
            status.starter_user_id,
            status.state.value,
            counters.received_packets,
            counters.enqueued_packets,
            counters.ingress_drops,
            counters.queue_drops,
            counters.sent_frames,
            counters.rtp_gap_samples,
            counters.rtp_discontinuities,
            counters.playout_reanchors,
            counters.clock_dropped_packets,
            counters.pending_drops,
            counters.late_audio_samples,
            counters.partial_transcripts,
            counters.final_transcripts,
        )

    async def _disconnect_voice_client(self, voice_client: Any | None) -> bool:
        if voice_client is None and self._disconnect_task is None:
            self._voice_cache_clean = True
            return True
        if voice_client is None:
            self._voice_cache_clean = False
            return False

        cleanup, late_cleanup_guarded = _install_cache_safe_cleanup(
            voice_client, self._current_cached_voice_client
        )
        task = self._disconnect_task
        if task is None:
            task = asyncio.create_task(
                voice_client.disconnect(force=True),
                name=f"voice-disconnect-g{self.guild_id}",
            )
            self._disconnect_task = task
        try:
            done, _ = await asyncio.wait(
                {task}, timeout=self._shutdown_step_seconds
            )
        except asyncio.CancelledError:
            raise
        if task in done:
            self._disconnect_task = None
            try:
                task.result()
            except asyncio.CancelledError:
                disconnect_error = "Discord disconnect was cancelled"
            except Exception as exc:
                disconnect_error = self._safe_error(
                    exc, prefix="Discord disconnect failed"
                )
            else:
                self._voice_cache_clean = True
                self.voice_client = None
                return True
        else:
            disconnect_error = "Discord disconnect timed out"
            # Do not await cancellation: this coroutine may suppress it forever.
            # Public cleanup is synchronous and releases cache ownership now.
            task.cancel()
            _quarantine_disconnect(task)

        self.last_error = disconnect_error
        self._queue_report("discord_disconnect", disconnect_error)
        if self._disconnect_cleanup_done:
            if not task.done():
                return False
            self._disconnect_task = None
            self._voice_cache_clean = True
            self.voice_client = None
            return True
        if not callable(cleanup):
            self._voice_cache_clean = False
            return False
        try:
            cleanup()
        except Exception as exc:
            self.last_error = self._safe_error(
                exc, prefix="Discord voice cache cleanup failed"
            )
            self._queue_report("discord_cleanup", self.last_error)
            self._voice_cache_clean = False
            return False
        self._disconnect_cleanup_done = True
        if not task.done():
            # Give a cancellation-cooperative disconnect exactly one loop turn;
            # never await its completion or extend the wall-clock deadline.
            await asyncio.sleep(0)
        if not task.done() and not late_cleanup_guarded:
            self.last_error = (
                "Discord cleanup succeeded but late disconnect could not be guarded"
            )
            self._voice_cache_clean = False
            return False
        if not task.done():
            self.last_error = (
                "Discord cache was cleaned but disconnect is still running; retry stop"
            )
            self._voice_cache_clean = True
            return False
        if task is self._disconnect_task:
            self._disconnect_task = None
        self._voice_cache_clean = True
        self.voice_client = None
        return True

    async def _bounded_wait(self, awaitable: Any, timeout_message: str) -> None:
        try:
            await asyncio.wait_for(awaitable, timeout=self._shutdown_step_seconds)
        except asyncio.TimeoutError:
            self.last_error = timeout_message
            self._queue_report("shutdown_timeout", timeout_message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = self._safe_error(exc, prefix=timeout_message)
            self._queue_report("shutdown_error", self.last_error)

    async def _finish_consumer(self) -> bool:
        task = self._consumer_task
        if task is None or task.done():
            self._consume_task_result(task)
            return True
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=self._shutdown_step_seconds)
        except asyncio.TimeoutError:
            return await self._cancel_task(task, "transcript consumer")
        except asyncio.CancelledError:
            raise
        except Exception:
            self._consume_task_result(task)
        return task.done()

    async def _finish_reporter(self) -> bool:
        self._report_stop = True
        self._report_ready.set()
        task = self._reporter_task
        if task is None or task.done():
            self._consume_task_result(task)
            self._reports.clear()
            return True
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=self._shutdown_step_seconds)
        except asyncio.TimeoutError:
            terminal = await self._cancel_task(task, "reporter")
        except asyncio.CancelledError:
            raise
        except Exception:
            self._consume_task_result(task)
            terminal = task.done()
        else:
            terminal = task.done()
        self._reports.clear()
        return terminal

    async def _cancel_task(self, task: asyncio.Task[Any] | None, label: str) -> bool:
        if task is None or task is asyncio.current_task():
            return True
        for _ in range(2):
            if task.done():
                break
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=self._shutdown_step_seconds)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            except Exception:
                pass
        self._consume_task_result(task)
        if not task.done():
            self.last_error = f"{label} did not terminate during shutdown"
            return False
        return True

    @staticmethod
    def _consume_task_result(task: asyncio.Task[Any] | None) -> None:
        if task is None or not task.done():
            return
        try:
            task.result()
        except (asyncio.CancelledError, Exception):
            pass

    @staticmethod
    def _safe_error(exc: BaseException, *, prefix: str | None = None) -> str:
        detail = type(exc).__name__
        return f"{prefix}: {detail}" if prefix else detail


@dataclass(frozen=True)
class DiagnosticStopResult:
    output_dir: Any | None
    summaries: tuple[CaptureSummary, ...]


class DiagnosticSession:
    """Cancellation-safe owner for one local diagnostic recording."""

    def __init__(
        self,
        *,
        guild_id: int,
        voice_channel: Any,
        output_root: Any,
        on_terminal: Callable[["DiagnosticSession"], None] | None = None,
        shutdown_step_seconds: float = DEFAULT_SHUTDOWN_STEP_SECONDS,
    ) -> None:
        self.guild_id = int(guild_id)
        self.voice_channel = voice_channel
        self.output_root = output_root
        self.state = VoiceSessionState.NEW
        self.voice_client: Any | None = None
        self._voice_client_borrowed = False
        self._preserve_borrowed_receiver = False
        self._connect_task: asyncio.Task[Any] | None = None
        self._connect_candidate: Any | None = None
        self._disconnect_task: asyncio.Task[Any] | None = None
        self._disconnect_cleanup_done = False
        self.capture: WaveCaptureSession | None = None
        self.sink: DiagnosticWaveSink | None = None
        self._on_terminal = on_terminal
        self._shutdown_step_seconds = max(0.01, shutdown_step_seconds)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._listener_done: asyncio.Future[Exception | None] | None = None
        self._listen_started = False
        self._shutdown_task: asyncio.Task[DiagnosticStopResult] | None = None

    async def start(self, existing_voice_client: Any | None = None) -> None:
        if self.state is not VoiceSessionState.NEW:
            raise VoiceSessionError(f"Diagnostic session is already {self.state.value}")
        self.state = VoiceSessionState.STARTING
        self._loop = asyncio.get_running_loop()
        self._listener_done = self._loop.create_future()
        original_error: BaseException | None = None
        try:
            install_voice_receive_compatibility()
            voice_client = existing_voice_client
            if voice_client is not None:
                self.voice_client = voice_client
                self._voice_client_borrowed = True
                self._preserve_borrowed_receiver = True
            if voice_client is not None and not VoiceSession._is_receive_client(voice_client):
                if not await self._disconnect(voice_client):
                    raise VoiceSessionError(
                        "Existing Discord voice client could not be released; retry"
                    )
                voice_client = None
                self._preserve_borrowed_receiver = False
            elif voice_client is not None:
                if voice_client.is_listening():
                    raise VoiceSessionError(
                        "Discord voice receive is already in use in this server"
                    )
                self._preserve_borrowed_receiver = False
            if voice_client is None:
                self._connect_task = asyncio.create_task(
                    self.voice_channel.connect(
                        cls=self._make_voice_recv_client,
                        self_deaf=False,
                    ),
                    name=f"voice-diagnostic-connect-g{self.guild_id}",
                )
                self._connect_task.add_done_callback(self._observe_connect_task)
                voice_client = await asyncio.shield(self._connect_task)
                self._adopt_connected_client(voice_client)
                self._connect_task = None
            elif getattr(voice_client, "channel", None) != self.voice_channel:
                await voice_client.move_to(self.voice_channel)
            if not self._voice_client_borrowed and voice_client.is_listening():
                raise VoiceSessionError("Discord voice receive is already in use in this server")
            self.capture = WaveCaptureSession(
                self.output_root,
                guild_id=self.guild_id,
                channel_id=int(self.voice_channel.id),
            )
            self.sink = DiagnosticWaveSink(self.capture)
            self.state = VoiceSessionState.RUNNING
            voice_client.listen(self.sink, after=self._listener_after)
            self._listen_started = True
            return
        except BaseException as exc:
            original_error = exc
            self.state = VoiceSessionState.FAILED
        cleanup_error: VoiceSessionError | None = None
        try:
            await self.stop()
        except asyncio.CancelledError:
            if not isinstance(original_error, asyncio.CancelledError):
                raise
        except VoiceSessionError as exc:
            cleanup_error = exc
        assert original_error is not None
        if isinstance(original_error, asyncio.CancelledError):
            raise original_error
        if cleanup_error is not None:
            raise cleanup_error
        if isinstance(original_error, VoiceSessionError):
            raise original_error
        raise VoiceSessionError(
            f"Diagnostic voice startup failed ({type(original_error).__name__})"
        ) from None

    def _adopt_connected_client(self, voice_client: Any) -> None:
        self.voice_client = voice_client
        self._voice_client_borrowed = False
        self._preserve_borrowed_receiver = False

    def _make_voice_recv_client(self, client: Any, channel: Any) -> Any:
        voice_client = voice_recv.VoiceRecvClient(client, channel)
        self._connect_candidate = voice_client
        return voice_client

    def _observe_connect_task(self, task: asyncio.Task[Any]) -> None:
        try:
            voice_client = task.result()
        except (asyncio.CancelledError, Exception):
            self._recover_new_cached_voice_client()
            return
        if self.voice_client is None:
            self._adopt_connected_client(voice_client)

    async def _finish_connect_acquisition(self) -> bool:
        task = self._connect_task
        if task is None:
            return True
        if not task.done():
            try:
                await asyncio.wait_for(
                    asyncio.shield(task), timeout=self._shutdown_step_seconds
                )
            except asyncio.TimeoutError:
                return False
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
        try:
            voice_client = task.result()
        except (asyncio.CancelledError, Exception):
            self._recover_new_cached_voice_client()
            self._connect_task = None
            return True
        self._adopt_connected_client(voice_client)
        self._connect_task = None
        return True

    def _current_cached_voice_client(self) -> Any | None:
        guild = getattr(self.voice_channel, "guild", None)
        return getattr(guild, "voice_client", None) if guild is not None else None

    def _recover_new_cached_voice_client(self) -> Any | None:
        if self.voice_client is not None or self._voice_client_borrowed:
            return self.voice_client
        candidate = self._connect_candidate
        if candidate is None:
            return None
        self._adopt_connected_client(candidate)
        return candidate

    def _listener_after(self, error: Exception | None) -> None:
        if self._loop is None:
            return

        def finish() -> None:
            if self._listener_done is not None and not self._listener_done.done():
                self._listener_done.set_result(error)

        self._loop.call_soon_threadsafe(finish)

    async def stop(self) -> DiagnosticStopResult:
        if (
            self._shutdown_task is not None
            and self._shutdown_task.done()
            and self.state is not VoiceSessionState.STOPPED
        ):
            VoiceSession._consume_task_result(self._shutdown_task)
            self._shutdown_task = None
        if self._shutdown_task is None:
            self._shutdown_task = asyncio.create_task(
                self._shutdown(), name=f"voice-diagnostic-shutdown-g{self.guild_id}"
            )
        shutdown = self._shutdown_task
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(shutdown)
                break
            except asyncio.CancelledError:
                cancelled = True
                if shutdown.done():
                    result = shutdown.result()
                    break
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def _shutdown(self) -> DiagnosticStopResult:
        if self.state is VoiceSessionState.STOPPED:
            return self._result()
        self.state = VoiceSessionState.STOPPING
        connect_terminal = await self._finish_connect_acquisition()
        voice_client = self.voice_client
        preserve_borrowed = self._preserve_borrowed_receiver
        if voice_client is not None and not preserve_borrowed:
            try:
                if voice_client.is_listening():
                    voice_client.stop_listening()
            except Exception:
                pass
        if (
            self._listen_started
            and self._listener_done is not None
            and not self._listener_done.done()
        ):
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._listener_done),
                    timeout=self._shutdown_step_seconds,
                )
            except asyncio.TimeoutError:
                self._listener_done.cancel()
        if self.sink is not None:
            self.sink.cleanup()
        elif self.capture is not None:
            self.capture.close()

        if preserve_borrowed:
            self.voice_client = None
            cache_clean = True
        else:
            cache_clean = await self._disconnect(voice_client)
        if not connect_terminal or not cache_clean:
            self.state = VoiceSessionState.FAILED
            raise VoiceSessionError(
                "Diagnostic voice cleanup could not release Discord ownership; retry stop"
            )
        self.state = VoiceSessionState.STOPPED
        if self._on_terminal is not None:
            self._on_terminal(self)
        return self._result()

    async def _disconnect(self, voice_client: Any | None) -> bool:
        if voice_client is None and self._disconnect_task is None:
            return True
        if voice_client is None:
            return False
        cleanup, late_cleanup_guarded = _install_cache_safe_cleanup(
            voice_client, self._current_cached_voice_client
        )
        task = self._disconnect_task
        if task is None:
            task = asyncio.create_task(
                voice_client.disconnect(force=True),
                name=f"voice-diagnostic-disconnect-g{self.guild_id}",
            )
            self._disconnect_task = task
        done, _ = await asyncio.wait({task}, timeout=self._shutdown_step_seconds)
        if task in done:
            self._disconnect_task = None
            try:
                task.result()
            except (asyncio.CancelledError, Exception):
                pass
            else:
                self.voice_client = None
                return True
        else:
            task.cancel()
            _quarantine_disconnect(task)
        if self._disconnect_cleanup_done:
            if not task.done():
                return False
            self._disconnect_task = None
            self.voice_client = None
            return True
        if not callable(cleanup):
            return False
        try:
            cleanup()
        except Exception:
            return False
        self._disconnect_cleanup_done = True
        if not task.done():
            await asyncio.sleep(0)
        if not task.done() and not late_cleanup_guarded:
            return False
        if not task.done():
            return False
        if task is self._disconnect_task:
            self._disconnect_task = None
        self.voice_client = None
        return True

    def _result(self) -> DiagnosticStopResult:
        if self.capture is None:
            return DiagnosticStopResult(None, ())
        return DiagnosticStopResult(
            self.capture.output_dir,
            self.capture.close(),
        )


class VoiceSessionManager:
    """Canonical one-live-session-per-guild registry."""

    def __init__(
        self,
        config: RuntimeConfig,
        *,
        gladia_factory: GladiaFactory = GladiaLiveSession,
        session_factory: Callable[..., VoiceSession] = VoiceSession,
    ) -> None:
        self.config = config
        self._gladia_factory = gladia_factory
        self._session_factory = session_factory
        self._sessions: dict[int, VoiceSession] = {}
        self._diagnostics: dict[int, DiagnosticSession] = {}
        self._diagnostic_guilds: set[int] = set()
        self._starter_leave_tasks: dict[tuple[int, int], asyncio.Task[None]] = {}

    def has_live(self, guild_id: int) -> bool:
        return int(guild_id) in self._sessions

    def has_diagnostic(self, guild_id: int) -> bool:
        guild_id = int(guild_id)
        return guild_id in self._diagnostics or guild_id in self._diagnostic_guilds

    def begin_diagnostic(self, guild_id: int) -> bool:
        guild_id = int(guild_id)
        if (
            guild_id in self._sessions
            or guild_id in self._diagnostics
            or guild_id in self._diagnostic_guilds
        ):
            return False
        self._diagnostic_guilds.add(guild_id)
        return True

    def end_diagnostic(self, guild_id: int) -> None:
        self._diagnostic_guilds.discard(int(guild_id))

    def validate_start(self, guild_id: int) -> None:
        guild_id = int(guild_id)
        if not self.config.voice_enabled:
            raise VoiceSessionError("Live voice chat is disabled (VOICE_ENABLED=false)")
        if not self.config.gladia_api_key:
            raise VoiceSessionError("Live voice chat needs GLADIA_API_KEY")
        if guild_id in self._diagnostics or guild_id in self._diagnostic_guilds:
            raise VoiceSessionError(
                "Stop the diagnostic /voice-record session before starting live voice chat"
            )
        if guild_id in self._sessions:
            raise VoiceSessionError("A live voice session is already running in this server")

    def validate_diagnostic_start(self, guild_id: int) -> None:
        guild_id = int(guild_id)
        if guild_id in self._sessions:
            raise VoiceSessionError(
                "Stop the live /voice-chat session before starting a diagnostic recording"
            )
        if guild_id in self._diagnostics or guild_id in self._diagnostic_guilds:
            raise VoiceSessionError(
                "A diagnostic voice recording is already running in this server"
            )

    async def start_diagnostic(
        self,
        *,
        guild: Any,
        voice_channel: Any,
    ) -> DiagnosticSession:
        guild_id = int(guild.id)
        self.validate_diagnostic_start(guild_id)

        def terminal(session: DiagnosticSession) -> None:
            if self._diagnostics.get(guild_id) is session:
                self._diagnostics.pop(guild_id, None)

        session = DiagnosticSession(
            guild_id=guild_id,
            voice_channel=voice_channel,
            output_root=self.config.data_dir / "voice-captures",
            on_terminal=terminal,
        )
        # Publish ownership before the first await so cancellation and rollback
        # always have a reachable handle.
        self._diagnostics[guild_id] = session
        try:
            await session.start(getattr(guild, "voice_client", None))
        except BaseException:
            if session.state is VoiceSessionState.STOPPED:
                self._diagnostics.pop(guild_id, None)
            raise
        return session

    async def stop_diagnostic(self, guild_id: int) -> DiagnosticStopResult | None:
        guild_id = int(guild_id)
        session = self._diagnostics.get(guild_id)
        if session is None:
            return None
        try:
            return await session.stop()
        finally:
            if session.state is VoiceSessionState.STOPPED:
                self._diagnostics.pop(guild_id, None)

    async def start(
        self,
        *,
        guild: Any,
        voice_channel: Any,
        text_channel: _TextChannel,
        starter: Any,
    ) -> VoiceSessionStatus:
        guild_id = int(guild.id)
        self.validate_start(guild_id)
        starter_voice = getattr(starter, "voice", None)
        if starter_voice is None or getattr(starter_voice, "channel", None) != voice_channel:
            raise VoiceSessionError("The session starter must be in that voice channel")

        def terminal(session: VoiceSession) -> None:
            if self._sessions.get(guild_id) is session:
                self._sessions.pop(guild_id, None)

        session = self._session_factory(
            guild_id=guild_id,
            voice_channel=voice_channel,
            text_channel=text_channel,
            starter_user_id=int(starter.id),
            starter_name=str(getattr(starter, "display_name", None) or starter.name),
            config=self.config,
            gladia_factory=self._gladia_factory,
            on_terminal=terminal,
        )
        self._sessions[guild_id] = session
        try:
            status = await session.start(getattr(guild, "voice_client", None))
            current_voice = getattr(getattr(starter, "voice", None), "channel", None)
            if current_voice is None or int(current_voice.id) != int(voice_channel.id):
                await session.stop()
                raise VoiceSessionError(
                    "The session starter left the voice channel while live voice was starting"
                )
            return status
        except BaseException:
            if session.state is VoiceSessionState.STOPPED:
                self._sessions.pop(guild_id, None)
            raise

    async def stop(self, guild_id: int) -> VoiceSessionStatus | None:
        guild_id = int(guild_id)
        session = self._sessions.get(guild_id)
        if session is None:
            return None
        try:
            return await session.stop()
        finally:
            if session.state is VoiceSessionState.STOPPED:
                self._sessions.pop(guild_id, None)

    def status(self, guild_id: int) -> VoiceSessionStatus | None:
        session = self._sessions.get(int(guild_id))
        return session.status() if session is not None else None

    def handle_voice_state_update(self, member: Any, before: Any, after: Any) -> bool:
        """Schedule exactly one owned auto-stop when the live starter leaves."""

        guild = getattr(member, "guild", None)
        if guild is None:
            return False
        guild_id = int(guild.id)
        session = self._sessions.get(guild_id)
        if session is None or session.state not in {
            VoiceSessionState.STARTING,
            VoiceSessionState.RUNNING,
        }:
            return False
        if int(getattr(member, "id", -1)) != session.starter_user_id:
            return False
        session_channel_id = int(session.voice_channel.id)
        before_channel = getattr(before, "channel", None)
        after_channel = getattr(after, "channel", None)
        if before_channel is None or int(before_channel.id) != session_channel_id:
            return False
        if after_channel is not None and int(after_channel.id) == session_channel_id:
            return False
        if session.state is VoiceSessionState.STARTING:
            return session.note_starter_left_during_start()
        task_key = (guild_id, id(session))
        existing = self._starter_leave_tasks.get(task_key)
        if existing is not None and not existing.done():
            return False
        session._queue_report(
            "starter_left",
            "The session starter left the live voice channel; stopping transcription",
        )
        task = asyncio.create_task(
            self._stop_after_starter_left(guild_id, session),
            name=f"voice-starter-left-g{guild_id}",
        )
        self._starter_leave_tasks[task_key] = task
        task.add_done_callback(
            lambda completed, key=task_key: self._observe_starter_leave_stop(
                key, completed
            )
        )
        return True

    async def _stop_after_starter_left(
        self, guild_id: int, session: VoiceSession
    ) -> None:
        outcome = "Live voice transcription stopped because the starter left the channel."
        try:
            await session.stop()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error(
                "Starter-leave live voice stop failed for guild %s with %s",
                guild_id,
                type(exc).__name__,
            )
            outcome = (
                "Live voice transcription is stopping because the starter left, "
                "but cleanup still needs attention. Try `/voice-chat stop`."
            )
        try:
            await session.text_channel.send(outcome)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error(
                "Could not report starter-leave stop for guild %s: %s",
                guild_id,
                type(exc).__name__,
            )

    def _observe_starter_leave_stop(
        self, task_key: tuple[int, int], task: asyncio.Task[None]
    ) -> None:
        guild_id, _ = task_key
        if self._starter_leave_tasks.get(task_key) is task:
            self._starter_leave_tasks.pop(task_key, None)
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            log.error(
                "Starter-leave stop task failed for guild %s with %s",
                guild_id,
                type(error).__name__,
            )
