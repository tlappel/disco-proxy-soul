"""Turn Discord attachments into provider-neutral content parts."""

from __future__ import annotations

import base64
from pathlib import Path

import aiohttp
import discord

from ..models.contracts import ContentPart

IMAGE_TYPES = {
    "image/jpeg": "image/jpeg",
    "image/png": "image/png",
    "image/gif": "image/gif",
    "image/webp": "image/webp",
}
TEXT_EXTENSIONS = {".txt", ".md", ".py", ".json", ".yaml", ".yml", ".log"}


async def build_user_parts(
    message: discord.Message,
    text: str,
    session: aiohttp.ClientSession,
) -> list[ContentPart]:
    parts: list[ContentPart] = []
    for attachment in message.attachments:
        ext = Path(attachment.filename).suffix.lower()
        if ext in TEXT_EXTENSIONS:
            try:
                async with session.get(attachment.url) as resp:
                    if resp.status == 200:
                        body = await resp.text(encoding="utf-8", errors="replace")
                        parts.append(
                            ContentPart(type="text", text=f"[File: {attachment.filename}]\n{body}")
                        )
            except Exception as exc:
                print(f"[attachment] text read error: {exc}")
            continue
        fetched = await _fetch_image(attachment.url, session)
        if fetched:
            parts.append(fetched)
    if text:
        parts.append(ContentPart(type="text", text=text))
    return parts or [ContentPart(type="text", text="Hey")]


async def _fetch_image(url: str, session: aiohttp.ClientSession) -> ContentPart | None:
    try:
        async with session.get(url) as resp:
            if resp.status != 200:
                return None
            content_type = (resp.content_type or "").split(";")[0].strip()
            mime = IMAGE_TYPES.get(content_type)
            if not mime:
                return None
            data = await resp.read()
            return ContentPart(
                type="image",
                mime=mime,
                data=base64.b64encode(data).decode("utf-8"),
            )
    except Exception as exc:
        print(f"[image] fetch error: {exc}")
        return None
