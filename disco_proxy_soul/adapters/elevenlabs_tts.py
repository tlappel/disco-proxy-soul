"""Streaming ElevenLabs text-to-speech transport with redacted failures."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, AsyncIterator
from urllib.parse import quote

import aiohttp

ELEVENLABS_API_ROOT = "https://api.elevenlabs.io/v1"


class ElevenLabsTTSError(RuntimeError):
    """Safe provider error that never exposes API credentials."""


@dataclass(frozen=True)
class ElevenLabsTTSConfig:
    model_id: str = "eleven_flash_v2_5"
    output_format: str = "pcm_48000"
    stability: float = 0.5
    similarity_boost: float = 0.75
    style: float = 0.0
    use_speaker_boost: bool = False
    speed: float = 1.0
    request_timeout_seconds: float = 60.0
    chunk_bytes: int = 4096


class ElevenLabsTTS:
    """Yield provider PCM chunks without buffering a complete audio file."""

    def __init__(
        self,
        api_key: str,
        voice_id: str,
        *,
        config: ElevenLabsTTSConfig | None = None,
        http_session: Any | None = None,
    ) -> None:
        self.api_key = api_key.strip()
        self.voice_id = voice_id.strip()
        self.config = config or ElevenLabsTTSConfig()
        self._http_session = http_session

    async def stream_pcm(self, text: str) -> AsyncIterator[bytes]:
        clean_text = text.strip()
        if not clean_text:
            return
        if not self.api_key:
            raise ElevenLabsTTSError("ElevenLabs TTS needs ELEVENLABS_API_KEY")
        if not self.voice_id:
            raise ElevenLabsTTSError("ElevenLabs TTS needs ELEVENLABS_VOICE_ID")

        owned_session = self._http_session is None
        session = self._http_session or aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=self.config.request_timeout_seconds)
        )
        url = (
            f"{ELEVENLABS_API_ROOT}/text-to-speech/"
            f"{quote(self.voice_id, safe='')}/stream"
        )
        payload = {
            "text": clean_text,
            "model_id": self.config.model_id,
            "voice_settings": {
                "stability": self.config.stability,
                "similarity_boost": self.config.similarity_boost,
                "style": self.config.style,
                "use_speaker_boost": self.config.use_speaker_boost,
                "speed": self.config.speed,
            },
        }
        try:
            async with session.post(
                url,
                params={"output_format": self.config.output_format},
                headers={
                    "xi-api-key": self.api_key,
                    "Content-Type": "application/json",
                    "Accept": "audio/pcm",
                },
                json=payload,
            ) as response:
                if response.status != 200:
                    if response.status in {400, 422}:
                        detail = (
                            "; verify that the account supports raw 48 kHz PCM"
                            if self.config.output_format == "pcm_48000"
                            else ""
                        )
                    else:
                        detail = ""
                    raise ElevenLabsTTSError(
                        f"ElevenLabs TTS request failed (HTTP {response.status}){detail}"
                    )
                async for chunk in response.content.iter_chunked(
                    max(1, self.config.chunk_bytes)
                ):
                    if chunk:
                        yield bytes(chunk)
        except ElevenLabsTTSError:
            raise
        except BaseException as exc:
            if isinstance(exc, (GeneratorExit, KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise ElevenLabsTTSError(
                f"ElevenLabs TTS transport failed ({type(exc).__name__})"
            ) from None
        finally:
            if owned_session:
                await session.close()
