"""Local recall ranking — no model, no I/O.

Python prefilters memory candidates before a cheap model pick. Used by the
file backend; an external backend may ignore this and rank on its own.
"""

from __future__ import annotations

import re
from typing import Any

RECALL_STOPWORDS = {
    "the", "and", "for", "that", "this", "with", "you", "your", "about", "what",
    "when", "where", "why", "how", "was", "were", "are", "have", "has", "had",
    "did", "does", "can", "could", "would", "should", "just", "like", "from",
    "into", "thing", "things", "something", "anything", "remember", "recall",
}


def recall_terms(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9']+", text.lower())
        if len(token) > 2 and token not in RECALL_STOPWORDS
    }


def memory_recall_score(
    memory: dict[str, Any],
    query_terms: set[str],
    query_text: str,
    index: int,
    total: int,
) -> float:
    summary = str(memory.get("summary", ""))
    tags = [str(tag) for tag in memory.get("tags", []) if tag]
    searchable = " ".join([summary, " ".join(tags)]).lower()
    memory_terms = recall_terms(searchable)
    tag_terms = {term for tag in tags for term in recall_terms(tag)}

    overlap = query_terms & memory_terms
    tag_overlap = query_terms & tag_terms
    exact_bonus = 5.0 if query_text and query_text.lower() in searchable else 0.0
    significance = float(memory.get("significance", 0.5) or 0.5)
    recency = (index + 1) / total if total else 0.0

    return (
        len(overlap) * 3.0
        + len(tag_overlap) * 4.0
        + exact_bonus
        + significance * 1.5
        + recency * 0.75
    )


def prefilter_recall_candidates(
    memories: list[dict[str, Any]],
    query: str,
    limit: int,
) -> list[dict[str, Any]]:
    if limit <= 0 or len(memories) <= limit:
        return memories

    query_terms = recall_terms(query)
    if not query_terms:
        ranked = sorted(
            enumerate(memories),
            key=lambda item: (
                float(item[1].get("significance", 0.5) or 0.5),
                item[0],
            ),
            reverse=True,
        )
    else:
        total = len(memories)
        ranked = sorted(
            enumerate(memories),
            key=lambda item: memory_recall_score(
                item[1], query_terms, query, item[0], total
            ),
            reverse=True,
        )

    return [memory for _, memory in ranked[:limit]]
