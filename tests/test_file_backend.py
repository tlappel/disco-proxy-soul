"""Tests for file memory, facts, and history stores."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from disco_proxy_soul.memory.contracts import MemoryRecord, Scope
from disco_proxy_soul.memory.facts import FactStore
from disco_proxy_soul.memory.file_backend import FileMemoryBackend
from disco_proxy_soul.memory.history import ConversationStore


class FileBackendTests(unittest.TestCase):
    def test_save_list_recall_and_replace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = FileMemoryBackend(Path(tmp) / "mem.json")
            scope = Scope(channel_id="1", persona_id="lila")

            async def run() -> None:
                saved = await backend.save(
                    scope,
                    MemoryRecord(summary="He picked up a guitar", tags=("guitar",), significance=0.8),
                )
                self.assertTrue(saved.memory_id)
                listed = await backend.list(scope)
                self.assertEqual(len(listed), 1)
                recalled = await backend.recall(scope, "guitar", limit=5)
                self.assertEqual(recalled[0].summary, "He picked up a guitar")
                await backend.replace_all(scope, [])
                self.assertEqual(await backend.list(scope), [])

            asyncio.run(run())

    def test_facts_seed_then_type_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "facts.json"
            store = FactStore(path, {"name": "Alex", "background": ["engineer"]})
            self.assertIn("Alex", store.format())
            changed = store.apply_updates({"background": "not a list"})
            self.assertEqual(changed, [])
            changed = store.apply_updates({"background": ["engineer", "teacher"]})
            self.assertEqual(changed, ["background"])
            again = FactStore(path, {"name": "ignored"})
            self.assertEqual(again.raw()["name"], "Alex")

    def test_history_refuses_empty_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = ConversationStore(Path(tmp) / "hist.json")
            store.append("9", "user", "")
            self.assertEqual(store.get("9"), [])
            store.append("9", "user", "hey")
            store.append("9", "assistant", "hi")
            reloaded = ConversationStore(Path(tmp) / "hist.json")
            self.assertEqual(len(reloaded.get("9")), 2)
            reloaded.clear("9")
            self.assertEqual(reloaded.get("9"), [])


if __name__ == "__main__":
    unittest.main()
