"""Per-speaker PCM capture for diagnosing Discord voice receive.

This module deliberately knows nothing about Gladia, the companion, or TTS.
Its one job is to turn decoded Discord PCM frames into WAV files that a human
can inspect before any higher layer is connected.
"""

from __future__ import annotations

import re
import threading
import wave
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


SAMPLE_RATE = 48_000
CHANNELS = 2
SAMPLE_WIDTH = 2
FRAME_WIDTH = CHANNELS * SAMPLE_WIDTH
RTP_TIMESTAMP_MODULUS = 2**32
MAX_INSERTED_GAP_SECONDS = 300


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", value.strip()).strip("-")
    return cleaned[:48] or "speaker"


@dataclass(frozen=True)
class CaptureSummary:
    user_id: int
    display_name: str
    path: Path
    frames: int
    sample_rate: int = SAMPLE_RATE

    @property
    def duration_seconds(self) -> float:
        return self.frames / self.sample_rate


@dataclass
class _SpeakerWriter:
    user_id: int
    display_name: str
    path: Path
    wav: wave.Wave_write
    first_rtp_timestamp: int | None = None
    frames_written: int = 0


class WaveCaptureSession:
    """Thread-safe collection of one WAV writer per Discord speaker."""

    def __init__(
        self,
        output_root: Path,
        guild_id: int,
        channel_id: int,
        *,
        started_at: datetime | None = None,
    ) -> None:
        timestamp = (started_at or datetime.now(timezone.utc)).strftime(
            "%Y%m%d-%H%M%S-%fZ"
        )
        self.output_dir = output_root / f"{timestamp}-g{guild_id}-c{channel_id}"
        self.output_dir.mkdir(parents=True, exist_ok=False)
        self._writers: dict[int, _SpeakerWriter] = {}
        self._summaries: tuple[CaptureSummary, ...] | None = None
        self._lock = threading.Lock()

    def write(
        self,
        user_id: int,
        display_name: str,
        pcm: bytes,
        *,
        rtp_timestamp: int | None = None,
    ) -> None:
        usable_bytes = len(pcm) - (len(pcm) % FRAME_WIDTH)
        if usable_bytes <= 0:
            return
        pcm = pcm[:usable_bytes]

        with self._lock:
            if self._summaries is not None:
                return
            writer = self._writers.get(user_id)
            if writer is None:
                path = self.output_dir / f"user-{user_id}-{_safe_name(display_name)}.wav"
                wav = wave.open(str(path), "wb")
                wav.setnchannels(CHANNELS)
                wav.setsampwidth(SAMPLE_WIDTH)
                wav.setframerate(SAMPLE_RATE)
                writer = _SpeakerWriter(user_id, display_name, path, wav)
                self._writers[user_id] = writer

            self._preserve_rtp_gap(writer, rtp_timestamp)
            writer.wav.writeframesraw(pcm)
            writer.frames_written += len(pcm) // FRAME_WIDTH

    def _preserve_rtp_gap(
        self, writer: _SpeakerWriter, rtp_timestamp: int | None
    ) -> None:
        if rtp_timestamp is None:
            return
        if writer.first_rtp_timestamp is None:
            writer.first_rtp_timestamp = rtp_timestamp
            return

        target_frame = (
            rtp_timestamp - writer.first_rtp_timestamp
        ) % RTP_TIMESTAMP_MODULUS
        gap_frames = target_frame - writer.frames_written
        if gap_frames <= 0:
            return
        if gap_frames > SAMPLE_RATE * MAX_INSERTED_GAP_SECONDS:
            return

        silence_frame = b"\x00" * FRAME_WIDTH
        while gap_frames:
            chunk_frames = min(gap_frames, SAMPLE_RATE)
            writer.wav.writeframesraw(silence_frame * chunk_frames)
            writer.frames_written += chunk_frames
            gap_frames -= chunk_frames

    def close(self) -> tuple[CaptureSummary, ...]:
        with self._lock:
            if self._summaries is not None:
                return self._summaries
            summaries: list[CaptureSummary] = []
            for writer in self._writers.values():
                writer.wav.close()
                summaries.append(
                    CaptureSummary(
                        user_id=writer.user_id,
                        display_name=writer.display_name,
                        path=writer.path,
                        frames=writer.frames_written,
                    )
                )
            self._summaries = tuple(sorted(summaries, key=lambda item: item.user_id))
            return self._summaries
