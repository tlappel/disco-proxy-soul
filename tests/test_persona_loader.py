"""Tests for the v2 persona loader."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from disco_proxy_soul.persona.loader import PersonaLoadError, load_persona


class PersonaLoaderTests(unittest.TestCase):
    def test_loads_persona_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "example"
            docs = root / "docs"
            docs.mkdir(parents=True)
            (root / "persona.md").write_text("Identity", encoding="utf-8")
            (root / "voice.md").write_text("Voice", encoding="utf-8")
            (root / "persona.json").write_text(
                json.dumps({
                    "companion_name": "Example",
                    "partner_name": "Alex",
                    "always_on_docs": ["shared.md"],
                }),
                encoding="utf-8",
            )
            (root / "facts.seed.json").write_text(json.dumps({"name": "T"}), encoding="utf-8")
            (root / "memory_policy.json").write_text(json.dumps({"journal_threshold": 0.7}), encoding="utf-8")
            (docs / "shared.md").write_text("Shared", encoding="utf-8")

            persona = load_persona(root)

        self.assertEqual(persona.persona_id, "example")
        self.assertEqual(persona.identity, "Identity")
        self.assertEqual(persona.companion_name, "Example")
        self.assertEqual(persona.partner_name, "Alex")
        self.assertEqual(persona.voice, "Voice")
        self.assertEqual(persona.facts_seed["name"], "T")
        self.assertEqual(persona.memory_policy["journal_threshold"], 0.7)
        self.assertEqual(persona.always_on_docs, ("shared.md",))
        self.assertEqual(len(persona.documents), 1)

    def test_requires_persona_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "missing"
            root.mkdir()
            with self.assertRaises(PersonaLoadError):
                load_persona(root)


if __name__ == "__main__":
    unittest.main()
