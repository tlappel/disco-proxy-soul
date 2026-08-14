"""Tests for local recall ranking."""

from __future__ import annotations

import unittest

from disco_proxy_soul.memory.recall import (
    memory_recall_score,
    prefilter_recall_candidates,
    recall_terms,
)


class RecallTests(unittest.TestCase):
    def test_recall_terms_drops_stopwords(self) -> None:
        terms = recall_terms("Remember the guitar and the coffee")
        self.assertIn("guitar", terms)
        self.assertIn("coffee", terms)
        self.assertNotIn("the", terms)
        self.assertNotIn("remember", terms)

    def test_tag_overlap_outranks_unrelated_recent(self) -> None:
        memories = [
            {"summary": "Talked about weather", "tags": ["smalltalk"], "significance": 0.9},
            {"summary": "He picked up a left-handed guitar", "tags": ["guitar"], "significance": 0.4},
        ]
        query = "guitar"
        low = memory_recall_score(memories[0], recall_terms(query), query, 1, 2)
        high = memory_recall_score(memories[1], recall_terms(query), query, 0, 2)
        self.assertGreater(high, low)

    def test_prefilter_keeps_best_and_passthroughs_short_lists(self) -> None:
        memories = [
            {"summary": f"note {i}", "tags": [], "significance": 0.1}
            for i in range(5)
        ]
        memories.append(
            {"summary": "Nadia found a scent article", "tags": ["dogs"], "significance": 0.8}
        )
        short = prefilter_recall_candidates(memories[:3], "dogs", limit=20)
        self.assertEqual(short, memories[:3])

        picked = prefilter_recall_candidates(memories, "Nadia scent dogs", limit=2)
        self.assertEqual(len(picked), 2)
        self.assertEqual(picked[0]["tags"], ["dogs"])


if __name__ == "__main__":
    unittest.main()
