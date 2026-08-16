"""Moments (host/partner) vs journal (her keep)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from disco_proxy_soul.memory.journal import MarkdownLog, migrate_journal_to_moments
from disco_proxy_soul.models.contracts import ToolCall
from disco_proxy_soul.app import CompanionApp


class MigrationTests(unittest.TestCase):
    def test_renames_old_journal_file_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "Naomi_journal.md"
            old.write_text("💛 Travis kept this moment: coffee", encoding="utf-8")
            self.assertTrue(migrate_journal_to_moments(root, "Naomi"))
            self.assertFalse(old.exists())
            new = root / "Naomi_moments.md"
            self.assertTrue(new.exists())
            self.assertIn("coffee", new.read_text(encoding="utf-8"))
            self.assertFalse(migrate_journal_to_moments(root, "Naomi"))

    def test_leaves_existing_moments_and_journal_alone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "Naomi_journal.md").write_text("hers", encoding="utf-8")
            (root / "Naomi_moments.md").write_text("host", encoding="utf-8")
            self.assertFalse(migrate_journal_to_moments(root, "Naomi"))
            self.assertEqual((root / "Naomi_journal.md").read_text(encoding="utf-8"), "hers")
            self.assertEqual((root / "Naomi_moments.md").read_text(encoding="utf-8"), "host")


class LogIsolationTests(unittest.TestCase):
    def test_keep_paths_do_not_cross(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            moments = MarkdownLog(Path(tmp) / "moments.md")
            journal = MarkdownLog(Path(tmp) / "journal.md")
            moments.append("host highlight", ["moment", "kept"])
            journal.append("I wanted this", ["journal", "kept"])
            self.assertIn("host highlight", moments.read_tail())
            self.assertNotIn("host highlight", journal.read_tail())
            self.assertIn("I wanted this", journal.read_tail())
            self.assertNotIn("I wanted this", moments.read_tail())
            self.assertEqual(moments.entry_count(), 1)
            self.assertEqual(journal.entry_count(), 1)


class ToolDispatchTests(unittest.TestCase):
    def test_keep_journal_tool_writes_journal_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = CompanionApp.__new__(CompanionApp)
            app.moments = MarkdownLog(Path(tmp) / "moments.md")
            app.journal = MarkdownLog(Path(tmp) / "journal.md")

            class _Persona:
                companion_name = "Naomi"
                partner_name = "Travis"

            app.persona = _Persona()  # type: ignore[assignment]
            results = app._execute_tool_calls(
                (
                    ToolCall(
                        id="c1",
                        name="keep_journal",
                        input={"text": "The light in the kitchen."},
                    ),
                )
            )
            self.assertEqual(results[0].content, "Kept in your journal.")
            self.assertIn("The light in the kitchen.", app.journal.read_tail())
            self.assertEqual(app.moments.read_tail(), "")


if __name__ == "__main__":
    unittest.main()
