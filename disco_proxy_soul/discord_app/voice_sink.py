"""Small synchronous Discord PCM sinks.

Sink callbacks run on the receive extension's packet-router thread.  They must
never await, perform network or disk I/O, or invoke companion cognition.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import threading
from typing import Callable

from discord.ext import voice_recv

from .voice_capture import WaveCaptureSession


DISCORD_PCM_FRAME_BYTES = 3_840
DEFAULT_DRAIN_BATCH_FRAMES = 32


@dataclass(frozen=True)
class LivePCMFrame:
    """One copied Discord PCM packet ready to cross into the event loop."""

    pcm: bytes
    rtp_timestamp: int | None


class LivePCMSink(voice_recv.AudioSink):
    """Bounded thread-to-loop bridge for one selected human speaker.

    The packet-router thread copies each accepted frame once into a bounded
    ring.  At most one event-loop drain callback can be outstanding, even if
    the loop is stalled while thousands of packets arrive.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        starter_user_id: int,
        enqueue: Callable[[LivePCMFrame], None],
        *,
        capacity_frames: int,
        drain_batch_frames: int = DEFAULT_DRAIN_BATCH_FRAMES,
        on_drops: Callable[[int], None] | None = None,
    ) -> None:
        super().__init__()
        if capacity_frames < 1:
            raise ValueError("capacity_frames must be positive")
        if drain_batch_frames < 1:
            raise ValueError("drain_batch_frames must be positive")
        self._loop = loop
        self._starter_user_id = int(starter_user_id)
        self._enqueue = enqueue
        self._on_drops = on_drops
        self._capacity_frames = int(capacity_frames)
        self._drain_batch_frames = min(int(drain_batch_frames), self._capacity_frames)
        self._pending: deque[LivePCMFrame] = deque()
        self._lock = threading.Lock()
        self._closed = False
        self._drain_scheduled = False
        self._dropped_frames = 0
        self._reported_drops = 0

    @property
    def pending_frames(self) -> int:
        with self._lock:
            return len(self._pending)

    @property
    def dropped_frames(self) -> int:
        with self._lock:
            return self._dropped_frames

    @property
    def drain_scheduled(self) -> bool:
        with self._lock:
            return self._drain_scheduled

    def wants_opus(self) -> bool:
        return False

    def write(self, user, data: voice_recv.VoiceData) -> None:
        if user is None or getattr(user, "bot", False):
            return
        if int(getattr(user, "id", -1)) != self._starter_user_id:
            return
        pcm = getattr(data, "pcm", None)
        if not pcm or len(pcm) != DISCORD_PCM_FRAME_BYTES:
            return
        packet = getattr(data, "packet", None)
        frame = LivePCMFrame(
            pcm=memoryview(pcm).tobytes(),
            rtp_timestamp=getattr(packet, "timestamp", None),
        )
        schedule = False
        with self._lock:
            if self._closed:
                return
            if len(self._pending) >= self._capacity_frames:
                self._dropped_frames += 1
            else:
                self._pending.append(frame)
            if not self._drain_scheduled and self._pending:
                self._drain_scheduled = True
                schedule = True
        if schedule:
            self._loop.call_soon_threadsafe(self._drain_on_loop)

    def _drain_on_loop(self) -> None:
        """Drain one fair batch and yield before any continuation."""

        with self._lock:
            count = min(self._drain_batch_frames, len(self._pending))
            frames = tuple(self._pending.popleft() for _ in range(count))
        for frame in frames:
            self._enqueue(frame)

        with self._lock:
            dropped = self._dropped_frames
            report_drops = dropped != self._reported_drops
            self._reported_drops = dropped
            continuation = bool(self._pending) and not self._closed
            if not continuation:
                self._drain_scheduled = False
        if report_drops and self._on_drops is not None:
            self._on_drops(dropped)
        if continuation:
            self._loop.call_soon(self._drain_on_loop)

    def cleanup(self) -> None:
        lock = getattr(self, "_lock", None)
        if lock is None:
            return
        with lock:
            self._closed = True
            self._pending.clear()


class DiagnosticWaveSink(voice_recv.AudioSink):
    """Record decoded Discord PCM into one WAV file per human speaker."""

    def __init__(self, capture: WaveCaptureSession) -> None:
        super().__init__()
        self.capture = capture

    def wants_opus(self) -> bool:
        return False

    def write(self, user, data: voice_recv.VoiceData) -> None:
        if user is None or getattr(user, "bot", False) or not data.pcm:
            return
        packet = getattr(data, "packet", None)
        display_name = str(
            getattr(user, "display_name", None) or getattr(user, "name", user.id)
        )
        self.capture.write(
            int(user.id),
            display_name,
            data.pcm,
            rtp_timestamp=getattr(packet, "timestamp", None),
        )

    def cleanup(self) -> None:
        self.capture.close()
