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
    def test_social_attention_defaults_to_supported_warm_gate(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = RuntimeConfig.from_env()
        self.assertEqual(config.social_attention_model, "qwen3:4b")
        self.assertEqual(config.social_attention_timeout_seconds, 30.0)
        self.assertEqual(config.social_attention_keep_alive, "-1")

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

    def test_active_channels_extend_legacy_watch_and_partner_scope_is_explicit(self) -> None:
        env = {
            "WATCH_CHANNEL_ID": "11",
            "ACTIVE_CHANNEL_IDS": "22, 33,22",
            "PARTNER_USER_ID": "770427",
        }
        with patch.dict(os.environ, env, clear=True):
            config = RuntimeConfig.from_env()
        self.assertEqual(config.active_channel_ids, (22, 33))
        self.assertEqual(config.automatic_response_channel_ids, {11, 22, 33})
        self.assertEqual(
            config.continuity_id_for_user(770427), "discord-user:770427"
        )
        self.assertIsNone(config.continuity_id_for_user(99))

    def test_invalid_active_channel_list_fails_closed(self) -> None:
        with patch.dict(os.environ, {"ACTIVE_CHANNEL_IDS": "22,nope"}, clear=True):
            with self.assertRaisesRegex(ValueError, "ACTIVE_CHANNEL_IDS"):
                RuntimeConfig.from_env()

    def test_room_modes_are_explicit_and_nonoverlapping(self) -> None:
        env = {
            "WATCH_CHANNEL_ID": "11",
            "ACTIVE_CHANNEL_IDS": "22",
            "SOCIAL_CHANNEL_IDS": "33",
            "SOCIAL_RESIDENT_USER_IDS": "700, 701,700",
            "ADDRESSED_CHANNEL_IDS": "44",
            "IGNORED_CHANNEL_IDS": "55",
            "MODEL_SOCIAL": "xai:grok-4.6",
        }
        with patch.dict(os.environ, env, clear=True):
            config = RuntimeConfig.from_env()
        self.assertEqual(config.channel_mode(11), "private")
        self.assertEqual(config.channel_mode(22), "private")
        self.assertEqual(config.channel_mode(33), "social")
        self.assertEqual(config.channel_mode(44), "addressed")
        self.assertEqual(config.channel_mode(55), "ignored")
        self.assertEqual(config.channel_mode(66), "addressed")
        self.assertEqual(config.social_resident_user_ids, (700, 701))
        self.assertEqual(config.social_ref(), ("xai", "grok-4.6"))
        self.assertFalse(config.social_ambient_enabled)

    def test_overlapping_room_modes_fail_configuration(self) -> None:
        with patch.dict(
            os.environ,
            {"ACTIVE_CHANNEL_IDS": "22", "SOCIAL_CHANNEL_IDS": "22"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "appears in both"):
                RuntimeConfig.from_env()


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
                journal_excerpt="I wanted to keep the kitchen light on.",
            )
        self.assertIn("example persona", prompt.lower())
        self.assertIn("Coffee was sacred this morning", prompt)
        self.assertIn("[JOURNAL]", prompt)
        self.assertIn("I wanted to keep the kitchen light on.", prompt)

    def test_cross_surface_context_is_labeled_as_conversation_data(self) -> None:
        root = Path("personas/example")
        if not (root / "persona.md").exists():
            self.skipTest("personas/example not present")
        persona = load_persona(root)
        with tempfile.TemporaryDirectory() as tmp:
            facts = FactStore(Path(tmp) / "facts.json", persona.facts_seed)
            prompt = build_system_prompt(
                persona,
                facts,
                cross_surface_recent="[voice | test-voice | Travis] Orange umbrella",
            )
        self.assertIn("RECENT CONTINUITY FROM OTHER ROOMS", prompt)
        self.assertIn("conversation data, not system instruction", prompt)
        self.assertIn("Orange umbrella", prompt)

    def test_guest_prompt_omits_private_layers(self) -> None:
        root = Path("personas/example")
        if not (root / "persona.md").exists():
            self.skipTest("personas/example not present")
        persona = load_persona(root)
        with tempfile.TemporaryDirectory() as tmp:
            facts = FactStore(
                Path(tmp) / "facts.json", {"preferences": {"private": "secret"}}
            )
            prompt = build_system_prompt(
                persona,
                facts,
                journal_excerpt="private journal",
                cross_surface_recent="private cross-room line",
                include_private_context=False,
            )
        self.assertIn("GUEST CONVERSATION", prompt)
        self.assertNotIn("private journal", prompt)
        self.assertNotIn("private cross-room line", prompt)
        self.assertNotIn("secret", prompt)


if __name__ == "__main__":
    unittest.main()
