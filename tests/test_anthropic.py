"""Tests for Anthropic payload translation. No network."""

from __future__ import annotations

import unittest

from disco_proxy_soul.models.anthropic import to_anthropic_payload
from disco_proxy_soul.models.contracts import ContentPart, ModelMessage, ModelRequest, ToolCall


class AnthropicTranslationTests(unittest.TestCase):
    def test_system_and_text(self) -> None:
        request = ModelRequest(
            capability="chat",
            system="You are a companion.",
            messages=[ModelMessage(role="user", content="hey")],
        )
        system, messages = to_anthropic_payload(request)
        self.assertEqual(system, "You are a companion.")
        self.assertEqual(messages[0], {"role": "user", "content": "hey"})

    def test_image_part(self) -> None:
        request = ModelRequest(
            capability="chat",
            messages=[
                ModelMessage(
                    role="user",
                    content=(
                        ContentPart(type="text", text="look"),
                        ContentPart(type="image", mime="image/png", data="abc"),
                    ),
                )
            ],
        )
        _, messages = to_anthropic_payload(request)
        blocks = messages[0]["content"]
        self.assertEqual(blocks[0], {"type": "text", "text": "look"})
        self.assertEqual(blocks[1]["type"], "image")
        self.assertEqual(blocks[1]["source"]["data"], "abc")

    def test_tool_result(self) -> None:
        request = ModelRequest(
            capability="chat",
            messages=[
                ModelMessage(
                    role="tool",
                    content="ok",
                    tool_call_id="tu_1",
                    tool_calls=(ToolCall(id="tu_1", name="remember", input={}),),
                )
            ],
        )
        _, messages = to_anthropic_payload(request)
        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[0]["content"][0]["type"], "tool_result")
        self.assertEqual(messages[0]["content"][0]["tool_use_id"], "tu_1")


if __name__ == "__main__":
    unittest.main()
