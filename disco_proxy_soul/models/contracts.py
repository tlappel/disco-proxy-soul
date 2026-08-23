"""Provider-agnostic model contracts.

The host owns the chat/tool loop. Providers only translate to their wire
format. Anthropic cache blocks, OpenAI tool_calls, and Gemini parts do not
appear above this layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, Sequence

ModelTier = Literal[
    "primary", "cheap", "social", "medium", "json", "stt", "tts", "image", "embedding"
]
ModelCapability = Literal["chat", "json", "stt", "tts", "image", "embedding"]
StopReason = Literal["end", "tool_calls", "empty", "error"]


@dataclass(frozen=True)
class ContentPart:
    type: Literal["text", "image"]
    text: str | None = None
    mime: str | None = None
    data: str | None = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0


@dataclass(frozen=True)
class ModelMessage:
    role: Literal["system", "user", "assistant", "tool"]
    content: str | Sequence[ContentPart]
    tool_call_id: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()


@dataclass(frozen=True)
class ModelRequest:
    capability: ModelCapability
    messages: Sequence[ModelMessage]
    system: str | Sequence[ContentPart] | None = None
    model: str | None = None
    max_tokens: int | None = None
    tools: Sequence[ToolSpec] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelResponse:
    text: str
    provider: str
    model: str
    tool_calls: tuple[ToolCall, ...] = ()
    usage: Usage | None = None
    stop_reason: StopReason = "end"
    image_bytes: bytes | None = None
    audio_bytes: bytes | None = None
    raw: Any | None = None


class ModelProvider(Protocol):
    name: str

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Return a response for chat/json/media tasks this provider supports."""


class ModelRouter(Protocol):
    async def complete(self, tier: ModelTier, request: ModelRequest) -> ModelResponse:
        """Route a model request to the provider/model configured for a tier."""
