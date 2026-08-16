"""Tests for the persona loader and extra-file contract."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from disco_proxy_soul.memory.facts import FactStore
from disco_proxy_soul.persona.loader import PersonaLoadError, load_persona
from disco_proxy_soul.prompt import build_system_prompt


def _write_package(root: Path, *, extra_json: dict | None = None) -> Path:
    docs = root / "docs"
    docs.mkdir(parents=True)
    (root / "persona.md").write_text("Identity", encoding="utf-8")
    (root / "voice.md").write_text("Voice", encoding="utf-8")
    meta = {
        "companion_name": "Example",
        "partner_name": "Alex",
        "always_on_docs": ["shared.md"],
    }
    if extra_json:
        meta.update(extra_json)
    (root / "persona.json").write_text(json.dumps(meta), encoding="utf-8")
    (root / "facts.seed.json").write_text(json.dumps({"name": "T"}), encoding="utf-8")
    (docs / "shared.md").write_text("Shared", encoding="utf-8")
    return root


class PersonaLoaderTests(unittest.TestCase):
    def test_loads_persona_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _write_package(Path(tmp) / "example")
            persona = load_persona(root)

        self.assertEqual(persona.persona_id, "example")
        self.assertEqual(persona.identity, "Identity")
        self.assertEqual(persona.companion_name, "Example")
        self.assertEqual(persona.partner_name, "Alex")
        self.assertEqual(persona.voice, "Voice")
        self.assertFalse(persona.voice_is_default)
        self.assertEqual(persona.facts_seed["name"], "T")
        self.assertEqual(persona.always_on_docs, ("shared.md",))
        modes = {doc.name: doc.mode for doc in persona.documents}
        self.assertEqual(modes["shared.md"], "always_on")
        self.assertTrue(any(doc.mode == "presence" for doc in persona.documents))
        self.assertTrue(persona.uses_default_presence)
        self.assertTrue(persona.room_note)

    def test_requires_identity_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "missing"
            root.mkdir()
            with self.assertRaises(PersonaLoadError):
                load_persona(root)

    def test_identity_and_manifest_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "nova"
            root.mkdir()
            (root / "identity.md").write_text("I am Nova", encoding="utf-8")
            (root / "manifest.json").write_text(
                json.dumps({"companion_name": "Nova", "partner_name": "Alex"}),
                encoding="utf-8",
            )
            persona = load_persona(root)
        self.assertEqual(persona.identity, "I am Nova")
        self.assertEqual(persona.companion_name, "Nova")
        self.assertTrue(persona.voice_is_default)

    def test_presence_is_a_toggle_not_a_junk_drawer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _write_package(Path(tmp) / "example")
            (root / "docs" / "intimate-presence.md").write_text(
                "Stay present", encoding="utf-8"
            )
            (root / "docs" / "extra-notes.md").write_text("Loose extra", encoding="utf-8")
            persona = load_persona(root)

        modes = {doc.name: doc.mode for doc in persona.documents}
        self.assertEqual(modes["shared.md"], "always_on")
        self.assertEqual(modes["intimate-presence.md"], "presence")
        self.assertEqual(modes["extra-notes.md"], "author")
        self.assertFalse(persona.uses_default_presence)

    def test_folder_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _write_package(Path(tmp) / "example")
            (root / "docs" / "always").mkdir()
            (root / "docs" / "presence").mkdir()
            (root / "docs" / "always" / "practice.md").write_text(
                "The practice", encoding="utf-8"
            )
            (root / "docs" / "presence" / "focus.md").write_text(
                "Situational module", encoding="utf-8"
            )
            persona = load_persona(root)

        self.assertEqual(persona.find_document("practice.md").mode, "always_on")
        self.assertEqual(persona.find_document("focus.md").mode, "presence")

    def test_root_markdown_defaults_to_library(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _write_package(Path(tmp) / "example")
            (root / "notes.md").write_text("Author only", encoding="utf-8")
            persona = load_persona(root)

        notes = persona.find_document("notes.md")
        assert notes is not None
        self.assertEqual(notes.mode, "author")

    def test_character_card_from_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _write_package(
                Path(tmp) / "example",
                extra_json={
                    "occupation": "night shift",
                    "example_lines": ["slow and deep. yes."],
                },
            )
            persona = load_persona(root)

        self.assertEqual(persona.character.fields["occupation"], "night shift")
        self.assertEqual(persona.character.example_lines, ("slow and deep. yes.",))

    def test_missing_layer_path_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _write_package(
                Path(tmp) / "example",
                extra_json={"layers": {"docs/missing.md": {"mode": "always_on"}}},
            )
            with self.assertRaises(PersonaLoadError):
                load_persona(root)

    def test_prompt_presence_and_library(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = _write_package(
                Path(tmp) / "example",
                extra_json={
                    "character": {"speech": "mostly lowercase"},
                    "example_lines": ["i noticed."],
                    "layers": {
                        "docs/intimate.md": {"mode": "presence"},
                        "notes.md": {"mode": "author"},
                    },
                },
            )
            (root / "docs" / "intimate.md").write_text("Stay in the room", encoding="utf-8")
            (root / "notes.md").write_text("Lilith draft notes", encoding="utf-8")
            persona = load_persona(root)
            facts = FactStore(root / "facts.json", persona.facts_seed)
            closed = build_system_prompt(persona, facts)
            open_presence = build_system_prompt(persona, facts, presence=True)

        self.assertIn("[CHARACTER]", closed)
        self.assertIn("[ROOM]", closed)
        self.assertIn("mostly lowercase", closed)
        self.assertIn("i noticed.", closed)
        self.assertIn("Shared", closed)
        self.assertNotIn("Stay in the room", closed)
        self.assertNotIn("Lilith draft notes", closed)
        self.assertNotIn("Lilith draft notes", open_presence)
        self.assertIn("Stay in the room", open_presence)


if __name__ == "__main__":
    unittest.main()
