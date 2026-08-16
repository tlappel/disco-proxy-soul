"""Bounded streaming PCM bridge from asyncio TTS to Discord's audio thread."""

from __future__ import annotations

import asyncio
from array import array
import queue
from typing import Any, AsyncIterator, Callable

import discord


DISCORD_PCM_FRAME_BYTES = 3_840
MONO_PCM_FRAME_BYTES = 1_920
_EOS = object()


class VoicePlaybackError(RuntimeError):
    """Safe outbound voice playback failure."""


class StreamingPcmAudioSource(discord.AudioSource):
    """Accept arbitrary mono PCM16 chunks and expose exact Discord frames."""

    def __init__(
        self,
        *,
        capacity_frames: int = 100,
        on_first_frame: Callable[[], None] | None = None,
    ) -> None:
        self._frames: queue.Queue[bytes | object] = queue.Queue(
            maxsize=max(1, int(capacity_frames))
        )
        self._mono_pending = bytearray()
        self._stereo_pending = bytearray()
        self._closed = False
        self._finished = False
        self._error: BaseException | None = None
        self._on_first_frame = on_first_frame
        self._first_frame_reported = False

    def is_opus(self) -> bool:
        return False

    async def feed_mono(self, chunk: bytes) -> None:
        if self._closed or self._finished:
            raise VoicePlaybackError("Discord playback source is closed")
        if not chunk:
            return
        self._mono_pending.extend(chunk)
        complete = len(self._mono_pending) - (len(self._mono_pending) % 2)
        if complete:
            samples = array("h")
            samples.frombytes(self._mono_pending[:complete])
            del self._mono_pending[:complete]
            stereo = array("h")
            for sample in samples:
                stereo.extend((sample, sample))
            self._stereo_pending.extend(stereo.tobytes())
        while len(self._stereo_pending) >= DISCORD_PCM_FRAME_BYTES:
            frame = bytes(self._stereo_pending[:DISCORD_PCM_FRAME_BYTES])
            del self._stereo_pending[:DISCORD_PCM_FRAME_BYTES]
            await self._put(frame)

    async def finish(self) -> None:
        if self._closed or self._finished:
            return
        if self._mono_pending:
            raise VoicePlaybackError("ElevenLabs returned an incomplete PCM16 sample")
        if self._stereo_pending:
            padding = DISCORD_PCM_FRAME_BYTES - len(self._stereo_pending)
            await self._put(bytes(self._stereo_pending) + bytes(padding))
            self._stereo_pending.clear()
        self._finished = True
        await self._put(_EOS)

    async def fail(self, error: BaseException) -> None:
        if self._closed or self._finished:
            return
        self._error = error
        self._finished = True
        await self._put(_EOS)

    async def _put(self, item: bytes | object) -> None:
        while not self._closed:
            try:
                self._frames.put_nowait(item)
                return
            except queue.Full:
                await asyncio.sleep(0.005)
        raise VoicePlaybackError("Discord playback source closed during streaming")

    def read(self) -> bytes:
        item = self._frames.get()
        if item is _EOS:
            self._closed = True
            if self._error is not None:
                raise VoicePlaybackError(
                    f"Outbound audio producer failed ({type(self._error).__name__})"
                )
            return b""
        assert isinstance(item, bytes)
        if not self._first_frame_reported:
            self._first_frame_reported = True
            if self._on_first_frame is not None:
                self._on_first_frame()
        return item

    def cleanup(self) -> None:
        self._closed = True
        self._finished = True
        self._mono_pending.clear()
        self._stereo_pending.clear()
        try:
            while True:
                self._frames.get_nowait()
        except queue.Empty:
            pass
        try:
            self._frames.put_nowait(_EOS)
        except queue.Full:
            pass


class VoicePlayback:
    """Stream one TTS response through one existing Discord voice client."""

    def __init__(self, *, capacity_frames: int = 100) -> None:
        self.capacity_frames = max(1, int(capacity_frames))

    async def play(
        self,
        voice_client: Any,
        chunks: AsyncIterator[bytes],
        *,
        on_first_frame: Callable[[], None] | None = None,
    ) -> None:
        loop = asyncio.get_running_loop()
        done: asyncio.Future[BaseException | None] = loop.create_future()
        source = StreamingPcmAudioSource(
            capacity_frames=self.capacity_frames,
            on_first_frame=on_first_frame,
        )

        def after(error: BaseException | None) -> None:
            def finish() -> None:
                if not done.done():
                    done.set_result(error)

            loop.call_soon_threadsafe(finish)

        try:
            voice_client.play(
                source,
                after=after,
                application="voip",
                signal_type="voice",
            )
            try:
                async for chunk in chunks:
                    await source.feed_mono(chunk)
                await source.finish()
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                await source.fail(exc)
            error = await done
            if error is not None:
                raise VoicePlaybackError(
                    f"Discord voice playback failed ({type(error).__name__})"
                )
        except asyncio.CancelledError:
            try:
                if voice_client.is_playing():
                    voice_client.stop_playing()
            finally:
                source.cleanup()
            raise
        finally:
            source.cleanup()
