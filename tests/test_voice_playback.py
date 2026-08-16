"""Tests for bounded Discord PCM framing and playback."""

from __future__ import annotations

import asyncio
from array import array
import threading
import unittest

from disco_proxy_soul.discord_app.voice_playback import (
    DISCORD_PCM_FRAME_BYTES,
    StreamingPcmAudioSource,
    VoicePlayback,
    VoicePlaybackError,
)


class ThreadedVoiceClient:
    def __init__(self) -> None:
        self.frames = []
        self.play_kwargs = None
        self._playing = False
        self.stop_calls = 0

    def play(self, source, **kwargs):
        self.play_kwargs = kwargs
        self._playing = True

        def consume():
            error = None
            try:
                while True:
                    frame = source.read()
                    if not frame:
                        break
                    self.frames.append(frame)
            except BaseException as exc:
                error = exc
            self._playing = False
            kwargs["after"](error)

        threading.Thread(target=consume, daemon=True).start()

    def is_playing(self):
        return self._playing

    def stop_playing(self):
        self.stop_calls += 1
        self._playing = False


class StreamingPcmAudioSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_odd_chunk_splits_preserve_samples_and_make_exact_stereo_frame(self):
        source = StreamingPcmAudioSource(capacity_frames=4)
        mono = array("h", range(960)).tobytes()
        await source.feed_mono(mono[:1])
        await source.feed_mono(mono[1:777])
        await source.feed_mono(mono[777:])
        await source.finish()
        frame = source.read()
        self.assertEqual(len(frame), DISCORD_PCM_FRAME_BYTES)
        stereo = array("h")
        stereo.frombytes(frame)
        self.assertEqual(stereo[0::2], array("h", range(960)))
        self.assertEqual(stereo[1::2], array("h", range(960)))
        self.assertEqual(source.read(), b"")

    async def test_final_partial_frame_is_zero_padded(self):
        source = StreamingPcmAudioSource(capacity_frames=4)
        await source.feed_mono(array("h", [321] * 10).tobytes())
        await source.finish()
        frame = source.read()
        samples = array("h")
        samples.frombytes(frame)
        self.assertEqual(samples[:20], array("h", [321, 321] * 10))
        self.assertTrue(all(sample == 0 for sample in samples[20:]))

    async def test_incomplete_final_sample_fails_closed(self):
        source = StreamingPcmAudioSource(capacity_frames=4)
        await source.feed_mono(b"x")
        with self.assertRaisesRegex(VoicePlaybackError, "incomplete PCM16"):
            await source.finish()
        source.cleanup()


class VoicePlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def test_streams_frames_with_voice_opus_controls(self):
        client = ThreadedVoiceClient()
        first_frame = threading.Event()

        async def chunks():
            mono = array("h", [500] * 960).tobytes()
            yield mono[:333]
            yield mono[333:]

        await VoicePlayback(capacity_frames=4).play(
            client,
            chunks(),
            on_first_frame=first_frame.set,
        )
        self.assertTrue(first_frame.is_set())
        self.assertEqual(len(client.frames), 1)
        self.assertEqual(len(client.frames[0]), DISCORD_PCM_FRAME_BYTES)
        self.assertEqual(client.play_kwargs["application"], "voip")
        self.assertEqual(client.play_kwargs["signal_type"], "voice")

    async def test_producer_failure_is_reported_without_unsafe_detail(self):
        client = ThreadedVoiceClient()

        async def chunks():
            yield array("h", [500] * 10).tobytes()
            raise RuntimeError("provider-secret")

        with self.assertRaisesRegex(
            VoicePlaybackError, "Discord voice playback failed"
        ) as caught:
            await VoicePlayback(capacity_frames=1).play(client, chunks())
        self.assertNotIn("provider-secret", str(caught.exception))

    async def test_cancellation_stops_playback_and_wakes_reader(self):
        client = ThreadedVoiceClient()
        producer_started = asyncio.Event()
        producer_release = asyncio.Event()

        async def chunks():
            producer_started.set()
            await producer_release.wait()
            yield b"never reached"

        task = asyncio.create_task(
            VoicePlayback(capacity_frames=1).play(client, chunks())
        )
        await producer_started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(client.stop_calls, 1)


if __name__ == "__main__":
    unittest.main()
