"""Anthropic provider. Translates the host contract; never leaks upward."""

from __future__ import annotations

from typing import Any

from .contracts import (
    ContentPart,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    StopReason,
    ToolCall,
    Usage,
)


def _text_from_parts(parts: str | list[ContentPart] | tuple[ContentPart, ...] | None) -> str:
    if parts is None:
        return ""
    if isinstance(parts, str):
        return parts
    return "\n".join(part.text or "" for part in parts if part.type == "text")


def to_anthropic_payload(request: ModelRequest) -> tuple[str | None, list[dict[str, Any]]]:
    system = _text_from_parts(request.system) or None
    messages: list[dict[str, Any]] = []
    for message in request.messages:
        if message.role == "system":
            continue
        if message.role == "tool":
            messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id or "",
                    "content": _text_from_parts(message.content),
                }],
            })
            continue

        if isinstance(message.content, str):
            content: Any = message.content
        else:
            content = []
            for part in message.content:
                if part.type == "text":
                    content.append({"type": "text", "text": part.text or ""})
                elif part.type == "image" and part.data:
                    content.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": part.mime or "image/png",
                            "data": part.data,
                        },
                    })

        if message.tool_calls:
            blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
            for call in message.tool_calls:
                blocks.append({
                    "type": "tool_use",
                    "id": call.id,
                    "name": call.name,
                    "input": call.input,
                })
            content = blocks

        messages.append({"role": message.role, "content": content})
    return system, messages


class AnthropicProvider:
    def __init__(self, api_key: str) -> None:
        import anthropic

        self.name = "anthropic"
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not request.model:
            raise ValueError("ModelRequest.model is required")
        system, messages = to_anthropic_payload(request)
        kwargs: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens or 4096,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system
        if request.tools:
            kwargs["tools"] = [
                {
                    "name": spec.name,
                    "description": spec.description,
                    "input_schema": spec.input_schema,
                }
                for spec in request.tools
            ]

        response = await self._client.messages.create(**kwargs)
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in response.content:
            kind = getattr(block, "type", None)
            if kind == "text":
                text_parts.append(getattr(block, "text", "") or "")
            elif kind == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        input=dict(block.input or {}),
                    )
                )

        text = "".join(text_parts)
        stop_raw = getattr(response, "stop_reason", "") or ""
        if tool_calls or stop_raw == "tool_use":
            stop: StopReason = "tool_calls"
        elif not text.strip():
            stop = "empty"
        else:
            stop = "end"

        usage = None
        raw_usage = getattr(response, "usage", None)
        if raw_usage:
            usage = Usage(
                input_tokens=getattr(raw_usage, "input_tokens", 0) or 0,
                output_tokens=getattr(raw_usage, "output_tokens", 0) or 0,
                cache_creation_tokens=getattr(raw_usage, "cache_creation_input_tokens", 0) or 0,
                cache_read_tokens=getattr(raw_usage, "cache_read_input_tokens", 0) or 0,
            )

        return ModelResponse(
            text=text,
            provider=self.name,
            model=request.model,
            tool_calls=tuple(tool_calls),
            usage=usage,
            stop_reason=stop,
            raw=response,
        )
