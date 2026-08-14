"""OpenAI-compatible chat provider.

Covers xAI (https://api.x.ai/v1), OpenAI, Groq, Ollama, and anything else
that speaks this API. Conversation history stays local — we send the full
window each turn and do not use server-side response storage.
"""

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


def _text_from_parts(parts: str | list[ContentPart] | tuple[ContentPart, ...]) -> str:
    if isinstance(parts, str):
        return parts
    return "\n".join(part.text or "" for part in parts if part.type == "text")


def to_openai_messages(request: ModelRequest) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    if request.system:
        messages.append({"role": "system", "content": _text_from_parts(request.system)})

    for message in request.messages:
        if message.role == "tool":
            messages.append({
                "role": "tool",
                "tool_call_id": message.tool_call_id or "",
                "content": _text_from_parts(message.content),
            })
            continue

        payload: dict[str, Any] = {"role": message.role}
        if isinstance(message.content, str):
            payload["content"] = message.content
        else:
            blocks: list[dict[str, Any]] = []
            for part in message.content:
                if part.type == "text":
                    blocks.append({"type": "text", "text": part.text or ""})
                elif part.type == "image" and part.data:
                    mime = part.mime or "image/png"
                    blocks.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{part.data}"},
                    })
            payload["content"] = blocks or ""

        if message.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": _dump_args(call.input),
                    },
                }
                for call in message.tool_calls
            ]
        messages.append(payload)
    return messages


def _dump_args(data: dict[str, Any]) -> str:
    import json

    return json.dumps(data, ensure_ascii=False)


def _load_args(raw: str | None) -> dict[str, Any]:
    import json

    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


class OpenAICompatibleProvider:
    """Chat Completions client pointed at any OpenAI-compatible base URL."""

    def __init__(self, name: str, api_key: str, base_url: str) -> None:
        import httpx
        from openai import AsyncOpenAI

        self.name = name
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(3600.0),
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not request.model:
            raise ValueError("ModelRequest.model is required")

        kwargs: dict[str, Any] = {
            "model": request.model,
            "messages": to_openai_messages(request),
            "max_tokens": request.max_tokens or 4096,
        }
        if request.tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description,
                        "parameters": spec.input_schema,
                    },
                }
                for spec in request.tools
            ]

        response = await self._client.chat.completions.create(**kwargs)
        choice = response.choices[0] if response.choices else None
        message = choice.message if choice else None
        text = (message.content or "") if message else ""

        tool_calls: tuple[ToolCall, ...] = ()
        if message and message.tool_calls:
            tool_calls = tuple(
                ToolCall(
                    id=call.id,
                    name=call.function.name,
                    input=_load_args(call.function.arguments),
                )
                for call in message.tool_calls
            )

        finish = (choice.finish_reason if choice else None) or ""
        if tool_calls:
            stop: StopReason = "tool_calls"
        elif not text.strip():
            stop = "empty"
        elif finish in {"stop", "length", ""}:
            stop = "end"
        else:
            stop = "end"

        usage = None
        if response.usage:
            usage = Usage(
                input_tokens=getattr(response.usage, "prompt_tokens", 0) or 0,
                output_tokens=getattr(response.usage, "completion_tokens", 0) or 0,
            )

        return ModelResponse(
            text=text,
            provider=self.name,
            model=request.model,
            tool_calls=tool_calls,
            usage=usage,
            stop_reason=stop,
            raw=response,
        )
