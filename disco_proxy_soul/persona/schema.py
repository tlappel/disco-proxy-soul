"""Persona package data structures."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PersonaDocument:
    name: str
    content: str
    path: Path


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
    memory_policy: dict[str, Any] = field(default_factory=dict)
    documents: tuple[PersonaDocument, ...] = ()
    always_on_docs: tuple[str, ...] = ()

    def format_facts_seed(self) -> str:
        return format_facts(self.facts_seed)

    def document_map(self) -> dict[str, str]:
        return {doc.name: doc.content for doc in self.documents}


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
