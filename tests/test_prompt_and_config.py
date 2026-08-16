"""Tests for prompt assembly and model-ref parsing."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from disco_proxy_soul.config import parse_model_ref
from disco_proxy_soul.memory.contracts import MemoryRecord
from disco_proxy_soul.memory.facts import FactStore
from disco_proxy_soul.persona.loader import load_persona
from disco_proxy_soul.prompt import build_system_prompt


class ConfigTests(unittest.TestCase):
    def test_parse_model_ref(self) -> None:
        self.assertEqual(parse_model_ref("grok-4.6", "xai"), ("xai", "grok-4.6"))
        self.assertEqual(parse_model_ref("openai:gpt-4.1", "xai"), ("openai", "gpt-4.1"))


class PromptTests(unittest.TestCase):
    def test_voice_interaction_context_is_optional_system_only_guidance(self) -> None:
        root = Path("personas/example")
        if not (root / "persona.md").exists():
            self.skipTest("personas/example not present")
        persona = load_persona(root)
        with tempfile.TemporaryDirectory() as tmp:
            facts = FactStore(Path(tmp) / "facts.json", persona.facts_seed)
            normal = build_system_prompt(persona, facts)
            voice = build_system_prompt(persona, facts, interaction_mode="voice")
        self.assertNotIn("[LIVE VOICE CONTEXT]", normal)
        self.assertIn("[LIVE VOICE CONTEXT]", voice)

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
            )
        self.assertIn("example persona", prompt.lower())
        self.assertIn("Coffee was sacred this morning", prompt)


if __name__ == "__main__":
    unittest.main()
