"""Network-free tests for streaming ElevenLabs PCM transport."""

from __future__ import annotations

import asyncio
import unittest

from disco_proxy_soul.adapters.elevenlabs_tts import (
    ElevenLabsTTS,
    ElevenLabsTTSConfig,
    ElevenLabsTTSError,
)


class FakeContent:
    def __init__(self, chunks):
        self.chunks = chunks
        self.requested_size = None

    async def iter_chunked(self, size):
        self.requested_size = size
        for chunk in self.chunks:
            yield chunk


class FakeResponse:
    def __init__(self, status=200, chunks=()):
        self.status = status
        self.content = FakeContent(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class FakeSession:
    def __init__(self, response=None, error=None):
        self.response = response or FakeResponse()
        self.error = error
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


class ElevenLabsTTSTests(unittest.IsolatedAsyncioTestCase):
    async def collect(self, tts, text="Hello"):
        return [chunk async for chunk in tts.stream_pcm(text)]

    async def test_stream_request_and_arbitrary_pcm_chunks(self) -> None:
        response = FakeResponse(chunks=[b"a", b"bc", b"", b"def"])
        session = FakeSession(response)
        config = ElevenLabsTTSConfig(
            model_id="eleven_flash_v2_5",
            chunk_bytes=777,
            stability=0.4,
            similarity_boost=0.8,
            speed=1.1,
        )
        tts = ElevenLabsTTS(
            "private-key", "voice/id", config=config, http_session=session
        )
        self.assertEqual(await self.collect(tts), [b"a", b"bc", b"def"])
        url, request = session.calls[0]
        self.assertTrue(url.endswith("/text-to-speech/voice%2Fid/stream"))
        self.assertEqual(request["params"], {"output_format": "pcm_48000"})
        self.assertEqual(request["headers"]["xi-api-key"], "private-key")
        self.assertEqual(request["json"]["text"], "Hello")
        self.assertEqual(request["json"]["model_id"], "eleven_flash_v2_5")
        self.assertEqual(request["json"]["voice_settings"]["speed"], 1.1)
        self.assertEqual(response.content.requested_size, 777)

    async def test_account_format_failure_is_clear_and_redacted(self) -> None:
        session = FakeSession(FakeResponse(status=422))
        tts = ElevenLabsTTS("secret-key", "voice", http_session=session)
        with self.assertRaisesRegex(ElevenLabsTTSError, "raw 48 kHz PCM") as caught:
            await self.collect(tts)
        self.assertNotIn("secret-key", str(caught.exception))

    async def test_transport_error_never_echoes_credentials(self) -> None:
        session = FakeSession(error=RuntimeError("secret-key tokenized URL"))
        tts = ElevenLabsTTS("secret-key", "voice", http_session=session)
        with self.assertRaisesRegex(ElevenLabsTTSError, "RuntimeError") as caught:
            await self.collect(tts)
        self.assertNotIn("secret-key", str(caught.exception))

    async def test_empty_text_makes_no_request(self) -> None:
        session = FakeSession()
        tts = ElevenLabsTTS("key", "voice", http_session=session)
        self.assertEqual(await self.collect(tts, "  "), [])
        self.assertEqual(session.calls, [])

    async def test_cancellation_propagates_without_wrapping(self) -> None:
        class BlockingContent:
            async def iter_chunked(self, size):
                await asyncio.Event().wait()
                yield b"never"

        response = FakeResponse()
        response.content = BlockingContent()
        task = asyncio.create_task(
            self.collect(
                ElevenLabsTTS("key", "voice", http_session=FakeSession(response))
            )
        )
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


if __name__ == "__main__":
    unittest.main()
