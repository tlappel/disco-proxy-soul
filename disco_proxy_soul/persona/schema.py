"""Persona package data structures."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DOC_MODES = ("always_on", "presence", "public", "author")

CARD_FIELD_ORDER = (
    "age",
    "occupation",
    "appearance",
    "cat",
    "personality",
    "speech",
    "relationship_style",
    "intimate_style",
)


@dataclass(frozen=True)
class PersonaDocument:
    name: str
    content: str
    path: Path
    mode: str = "author"
    tags: tuple[str, ...] = ()
    title: str = ""
    origin: str = "user"

    def display_title(self) -> str:
        if self.title:
            return self.title
        return self.name.replace(".md", "").replace("-", " ").replace("_", " ").title()


@dataclass(frozen=True)
class PersonaCharacter:
    """Structured self-knowledge. Compact card, not a second identity essay."""

    fields: dict[str, str] = field(default_factory=dict)
    example_lines: tuple[str, ...] = ()

    def format_card(self) -> str:
        if not self.fields:
            return ""
        lines: list[str] = []
        seen: set[str] = set()
        for key in CARD_FIELD_ORDER:
            value = self.fields.get(key)
            if value:
                lines.append(f"{key.replace('_', ' ').title()}: {value}")
                seen.add(key)
        for key, value in self.fields.items():
            if key not in seen and value:
                lines.append(f"{key.replace('_', ' ').title()}: {value}")
        return "\n".join(lines)


@dataclass(frozen=True)
class PersonaPackage:
    """External persona material loaded by the reusable host app."""

    persona_id: str
    root: Path
    identity: str
    companion_name: str
    partner_name: str
    voice: str = ""
    facts_seed: dict[str, Any] = field(default_factory=dict)
    documents: tuple[PersonaDocument, ...] = ()
    always_on_docs: tuple[str, ...] = ()
    character: PersonaCharacter = field(default_factory=PersonaCharacter)
    room_note: str = ""
    voice_is_default: bool = False
    uses_default_presence: bool = False

    def format_facts_seed(self) -> str:
        return format_facts(self.facts_seed)

    def document_map(self) -> dict[str, str]:
        return {doc.name: doc.content for doc in self.documents}

    def documents_by_mode(self, *modes: str) -> tuple[PersonaDocument, ...]:
        wanted = set(modes)
        return tuple(doc for doc in self.documents if doc.mode in wanted)

    def find_document(self, name: str) -> PersonaDocument | None:
        key = name if name.endswith(".md") else f"{name}.md"
        for doc in self.documents:
            if doc.name == key or doc.name == name:
                return doc
        return None

    def mode_counts(self) -> dict[str, int]:
        counts = {mode: 0 for mode in DOC_MODES}
        for doc in self.documents:
            counts[doc.mode] = counts.get(doc.mode, 0) + 1
        return counts


def format_facts(facts: dict[str, Any]) -> str:
    """Render a facts object as compact prompt text."""
    if not facts:
        return ""

    def join_list(value: Any, sep: str = " | ") -> str:
        if isinstance(value, list):
            return sep.join(str(item) for item in value)
        return str(value) if value else ""

    def format_map(value: Any) -> str:
        if isinstance(value, dict):
            return ", ".join(f"{key} ({item})" for key, item in value.items())
        return str(value) if value else ""

    lines = [
        f"Name: {facts.get('name', '')}",
        f"Background: {join_list(facts.get('background', []))}",
        f"Current situation: {join_list(facts.get('current_situation', []))}",
    ]
    people = facts.get("important_people", {})
    if people:
        lines.append(f"Important people: {format_map(people)}")
    prefs = facts.get("preferences", {})
    if prefs:
        if isinstance(prefs, dict):
            lines.append(
                "Preferences: " + " | ".join(f"{key}: {item}" for key, item in prefs.items())
            )
        else:
            lines.append(f"Preferences: {prefs}")
    needs = facts.get("emotional_needs", [])
    if needs:
        lines.append(f"Emotional needs: {join_list(needs)}")
    return "\n".join(line for line in lines if not line.endswith(": "))
