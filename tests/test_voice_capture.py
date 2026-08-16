"""Tests for the decoded-PCM diagnostic recorder."""

from __future__ import annotations

import tempfile
import unittest
import wave
from datetime import datetime, timezone
from pathlib import Path

from disco_proxy_soul.discord_app.voice_capture import (
    CHANNELS,
    FRAME_WIDTH,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    WaveCaptureSession,
)


class WaveCaptureTests(unittest.TestCase):
    def test_writes_valid_per_speaker_wav_and_preserves_rtp_gap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            capture = WaveCaptureSession(
                Path(tmp),
                guild_id=12,
                channel_id=34,
                started_at=datetime(2026, 8, 15, tzinfo=timezone.utc),
            )
            packet = b"\x01\x00\x02\x00" * 960
            capture.write(7, "Travis / test", packet, rtp_timestamp=1000)
            capture.write(7, "Travis / test", packet, rtp_timestamp=2920)
            summaries = capture.close()

            self.assertEqual(len(summaries), 1)
            summary = summaries[0]
            self.assertEqual(summary.path.name, "user-7-Travis-test.wav")
            self.assertEqual(summary.frames, 2880)
            self.assertAlmostEqual(summary.duration_seconds, 0.06)

            with wave.open(str(summary.path), "rb") as recorded:
                self.assertEqual(recorded.getnchannels(), CHANNELS)
                self.assertEqual(recorded.getsampwidth(), SAMPLE_WIDTH)
                self.assertEqual(recorded.getframerate(), SAMPLE_RATE)
                self.assertEqual(recorded.getnframes(), 2880)
                audio = recorded.readframes(2880)
                gap = audio[960 * FRAME_WIDTH:1920 * FRAME_WIDTH]
                self.assertEqual(gap, b"\x00" * (960 * FRAME_WIDTH))

    def test_close_is_idempotent_and_ignores_incomplete_frame(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            capture = WaveCaptureSession(Path(tmp), guild_id=1, channel_id=2)
            capture.write(9, "Speaker", b"\x01\x02\x03")
            self.assertEqual(capture.close(), ())
            self.assertEqual(capture.close(), ())


if __name__ == "__main__":
    unittest.main()
