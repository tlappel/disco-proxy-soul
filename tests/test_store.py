"""Tests for atomic JSON store helpers."""

from __future__ import annotations

import unittest
from pathlib import Path

from disco_proxy_soul.memory.store import (
    load_json,
    normalize_memory_data,
    parse_llm_json,
    save_json,
)


class StoreTests(unittest.TestCase):
    def test_save_and_load_roundtrip(self) -> None:
        with self._tmp() as tmp:
            path = str(Path(tmp) / "facts.json")
            save_json(path, {"name": "Alex"})
            self.assertEqual(load_json(path, {}), {"name": "Alex"})

    def test_quarantines_corrupt_file(self) -> None:
        with self._tmp() as tmp:
            root = Path(tmp)
            path = root / "broken.json"
            path.write_text("{not json", encoding="utf-8")
            result = load_json(str(path), {"ok": True})
            self.assertEqual(result, {"ok": True})
            self.assertFalse(path.exists())
            quarantined = list(root.glob("broken.json.corrupt-*"))
            self.assertEqual(len(quarantined), 1)
            self.assertEqual(quarantined[0].read_text(encoding="utf-8"), "{not json")

    def test_parse_llm_json_strips_fences_and_prose(self) -> None:
        fenced = '```json\n{"a": 1}\n```'
        self.assertEqual(parse_llm_json(fenced), {"a": 1})
        wrapped = 'Sure.\n["id-1", "id-2"]\nThanks.'
        self.assertEqual(parse_llm_json(wrapped), ["id-1", "id-2"])

    def test_normalize_memory_data_guards_types(self) -> None:
        cleaned = normalize_memory_data(
            {"summary": "  hi  ", "tags": "one", "significance": "1.8"},
            "fallback",
        )
        self.assertEqual(cleaned["summary"], "hi")
        self.assertEqual(cleaned["tags"], ["one"])
        self.assertEqual(cleaned["significance"], 1.0)

        empty = normalize_memory_data("nope", "fallback")
        self.assertEqual(empty["summary"], "fallback")
        self.assertEqual(empty["tags"], [])
        self.assertEqual(empty["significance"], 0.7)

    def _tmp(self):
        import tempfile

        return tempfile.TemporaryDirectory()


if __name__ == "__main__":
    unittest.main()
