"""Local-only Ollama social attention adapter tests."""

from __future__ import annotations

import argparse
import unittest
from unittest.mock import AsyncMock, patch

from disco_proxy_soul.adapters.ollama_attention import (
    AttentionDecision,
    OllamaAttentionConfig,
    OllamaAttentionError,
    OllamaAttentionJudge,
    _run_probe,
)


class OllamaAttentionTests(unittest.IsolatedAsyncioTestCase):
    def test_remote_urls_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "loopback"):
            OllamaAttentionConfig(base_url="https://models.example.com")
        with self.assertRaisesRegex(ValueError, "loopback"):
            OllamaAttentionConfig(base_url="http://192.168.1.20:11434")
        self.assertEqual(
            OllamaAttentionConfig(base_url="http://localhost:11434").base_url,
            "http://localhost:11434",
        )

    async def test_structured_nonthinking_local_judgment(self) -> None:
        judge = OllamaAttentionJudge(
            OllamaAttentionConfig(model="qwen3:1.7b", threads=4, context_tokens=2048)
        )
        judge._post_chat = AsyncMock(
            return_value={
                "message": {
                    "content": (
                        '{"decision":"consider","confidence":0.91,'
                        '"reason":"an open invitation"}'
                    )
                },
                "prompt_eval_count": 100,
                "eval_count": 18,
                "total_duration": 1_250_000_000,
            }
        )

        result = await judge.judge(
            "[human: Alex] Anyone have a thought?",
            engaged=False,
            social_posture="Sociability: 0.70",
            availability="open",
        )

        self.assertEqual(result.decision, "consider")
        self.assertAlmostEqual(result.confidence, 0.91)
        self.assertEqual(result.prompt_tokens, 100)
        self.assertEqual(result.output_tokens, 18)
        self.assertEqual(result.total_duration_ms, 1250)
        payload = judge._post_chat.await_args.args[0]
        self.assertFalse(payload["think"])
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["options"]["num_thread"], 4)
        self.assertEqual(payload["options"]["num_ctx"], 2048)
        self.assertEqual(
            payload["format"]["properties"]["decision"]["enum"],
            [
                "consider",
                "wait",
                "ignore",
            ],
        )
        self.assertEqual(
            [message["role"] for message in payload["messages"]],
            [
                "system",
                "user",
                "assistant",
                "user",
                "assistant",
                "user",
                "assistant",
                "user",
            ],
        )
        instructions = payload["messages"][0]["content"]
        self.assertIn("Public participation is already invited", instructions)
        self.assertIn("not that the resident must reply", instructions)
        self.assertIn(
            '"public_social_posture":"Sociability: 0.70"',
            payload["messages"][-1]["content"],
        )

    async def test_reasonable_public_participation_is_not_phrase_vetoed(self) -> None:
        judge = OllamaAttentionJudge(OllamaAttentionConfig(companion_name="Naomi"))
        judge._post_chat = AsyncMock(
            return_value={
                "message": {
                    "content": (
                        '{"decision":"consider","confidence":0.95,'
                        '"reason":"looks conversational"}'
                    )
                }
            }
        )

        banter = await judge.judge(
            "[human: Alex] I made pasta.\n[human: Morgan] Nice, what sauce?",
            engaged=False,
            social_posture="Sociability: 0.70",
            availability="open",
        )

        self.assertEqual(banter.decision, "consider")

    async def test_invalid_response_fails_closed(self) -> None:
        judge = OllamaAttentionJudge(OllamaAttentionConfig())
        judge._post_chat = AsyncMock(return_value={"message": {"content": "nope"}})
        with self.assertRaises(OllamaAttentionError):
            await judge.judge("room", engaged=False)

    async def test_default_probe_fails_when_a_clear_social_rule_does_not_match(self):
        judge = AsyncMock()
        judge.ready.return_value = True
        judge.judge.side_effect = [
            AttentionDecision("wait", 0.85, "mismatch") for _ in range(7)
        ]
        args = argparse.Namespace(
            base_url="http://127.0.0.1:11434",
            model="qwen3:1.7b",
            companion_name="Naomi",
            timeout=30.0,
            threads=4,
            context_tokens=2048,
            keep_alive="30m",
            social_posture="Sociability: 0.70",
            availability="open",
            text=None,
            engaged=False,
        )

        with patch(
            "disco_proxy_soul.adapters.ollama_attention.OllamaAttentionJudge",
            return_value=judge,
        ):
            result = await _run_probe(args)

        self.assertEqual(result, 1)
        self.assertEqual(judge.judge.await_count, 7)

    async def test_probe_checks_rules_but_only_observes_ambiguous_cases(self):
        judge = AsyncMock()
        judge.ready.return_value = True
        judge.judge.side_effect = [
            AttentionDecision("ignore", 0.95, "safe silence"),
            AttentionDecision("wait", 0.95, "safe silence"),
            AttentionDecision("wait", 0.95, "safe silence"),
            AttentionDecision("consider", 0.95, "opening"),
            AttentionDecision("consider", 0.95, "reasonable choice"),
            AttentionDecision("ignore", 0.95, "reasonable choice"),
            AttentionDecision("consider", 0.95, "resident peer opening"),
        ]
        args = argparse.Namespace(
            base_url="http://127.0.0.1:11434",
            model="qwen3:1.7b",
            companion_name="Naomi",
            timeout=30.0,
            threads=4,
            context_tokens=2048,
            keep_alive="30m",
            social_posture="Sociability: 0.70",
            availability="open",
            text=None,
            engaged=False,
        )

        with patch(
            "disco_proxy_soul.adapters.ollama_attention.OllamaAttentionJudge",
            return_value=judge,
        ):
            result = await _run_probe(args)

        self.assertEqual(result, 0)


if __name__ == "__main__":
    unittest.main()
