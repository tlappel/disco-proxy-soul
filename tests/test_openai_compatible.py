"""Tests for OpenAI-compatible message translation. No network."""

from __future__ import annotations

import unittest

from disco_proxy_soul.models.contracts import ContentPart, ModelMessage, ModelRequest, ToolCall
from disco_proxy_soul.models.openai_compatible import to_openai_messages


class OpenAICompatibleTests(unittest.TestCase):
    def test_system_and_text_messages(self) -> None:
        request = ModelRequest(
            capability="chat",
            system="You are a companion.",
            messages=[
                ModelMessage(role="user", content="hey"),
                ModelMessage(role="assistant", content="hi"),
            ],
        )
        messages = to_openai_messages(request)
        self.assertEqual(messages[0], {"role": "system", "content": "You are a companion."})
        self.assertEqual(messages[1], {"role": "user", "content": "hey"})
        self.assertEqual(messages[2], {"role": "assistant", "content": "hi"})

    def test_image_part_becomes_data_url(self) -> None:
        request = ModelRequest(
            capability="chat",
            messages=[
                ModelMessage(
                    role="user",
                    content=(
                        ContentPart(type="text", text="what is this"),
                        ContentPart(type="image", mime="image/png", data="abc123"),
                    ),
                )
            ],
        )
        messages = to_openai_messages(request)
        blocks = messages[0]["content"]
        self.assertEqual(blocks[0], {"type": "text", "text": "what is this"})
        self.assertEqual(blocks[1]["type"], "image_url")
        self.assertIn("data:image/png;base64,abc123", blocks[1]["image_url"]["url"])

    def test_tool_result_role(self) -> None:
        request = ModelRequest(
            capability="chat",
            messages=[
                ModelMessage(
                    role="tool",
                    content="ok",
                    tool_call_id="call_1",
                    tool_calls=(ToolCall(id="call_1", name="remember", input={}),),
                )
            ],
        )
        messages = to_openai_messages(request)
        self.assertEqual(messages[0]["role"], "tool")
        self.assertEqual(messages[0]["tool_call_id"], "call_1")
        self.assertEqual(messages[0]["content"], "ok")


if __name__ == "__main__":
    unittest.main()
