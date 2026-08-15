"""Tests for prompt assembly and model-ref parsing."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import os
from unittest.mock import patch

from disco_proxy_soul.config import parse_model_ref, RuntimeConfig
from disco_proxy_soul.memory.contracts import MemoryRecord
from disco_proxy_soul.memory.facts import FactStore
from disco_proxy_soul.persona.loader import load_persona
from disco_proxy_soul.prompt import build_system_prompt


class ConfigTests(unittest.TestCase):
    def test_parse_model_ref(self) -> None:
        self.assertEqual(parse_model_ref("grok-4.6", "xai"), ("xai", "grok-4.6"))
        self.assertEqual(parse_model_ref("openai:gpt-4.1", "xai"), ("openai", "gpt-4.1"))

    def test_moments_threshold_prefers_new_name(self) -> None:
        env = {
            "MOMENTS_THRESHOLD": "0.4",
            "JOURNAL_THRESHOLD": "0.9",
        }
        with patch.dict(os.environ, env, clear=False):
            config = RuntimeConfig.from_env()
        self.assertAlmostEqual(config.moments_threshold, 0.4)

    def test_moments_threshold_accepts_old_journal_env(self) -> None:
        env = {"JOURNAL_THRESHOLD": "0.55", "MOMENTS_THRESHOLD": ""}
        with patch.dict(os.environ, env, clear=False):
            config = RuntimeConfig.from_env()
        self.assertAlmostEqual(config.moments_threshold, 0.55)


class PromptTests(unittest.TestCase):
    def test_prompt_includes_identity_facts_and_recall(self) -> None:
        root = Path("personas/example")
        if not (root / "persona.md").exists():
            self.skipTest("personas/example not present")
        persona = load_persona(root)
        with tempfile.TemporaryDirectory() as tmp:
            facts = FactStore(Path(tmp) / "facts.json", persona.facts_seed)
            prompt = build_system_prompt(
                persona,
                facts,
                recalled=[MemoryRecord(summary="Coffee was sacred this morning")],
                recall_source="automatic",
                journal_excerpt="I wanted to keep the kitchen light on.",
            )
        self.assertIn("example persona", prompt.lower())
        self.assertIn("Coffee was sacred this morning", prompt)
        self.assertIn("[JOURNAL]", prompt)
        self.assertIn("I wanted to keep the kitchen light on.", prompt)


if __name__ == "__main__":
    unittest.main()
