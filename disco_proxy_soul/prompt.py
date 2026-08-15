"""Provider-neutral system prompt. No Anthropic cache blocks."""

from __future__ import annotations

from .memory.contracts import MemoryRecord
from .memory.facts import FactStore
from .persona.schema import PersonaDocument, PersonaPackage


def build_system_prompt(
    persona: PersonaPackage,
    facts: FactStore,
    recalled: list[MemoryRecord] | None = None,
    recall_source: str = "automatic",
    recall_query: str = "",
    presence: bool = False,
) -> str:
    partner = persona.partner_name
    parts = [persona.identity.strip()]
    if persona.room_note:
        parts.append("[ROOM]\n" + persona.room_note)
    if persona.voice:
        parts.append("[VOICE]\n" + persona.voice.strip())

    card = persona.character.format_card()
    if card:
        parts.append("[CHARACTER]\n" + card)
    if persona.character.example_lines:
        examples = "\n".join(f"- {line}" for line in persona.character.example_lines)
        parts.append(
            "[HOW YOU SOUND]\n"
            "Examples of your voice — match this grain, do not recite them:\n"
            + examples
        )

    facts_text = facts.format()
    if facts_text:
        parts.append(f"[{partner.upper()} — what you know]\n{facts_text}")

    always_on = _docs_block(
        persona.documents_by_mode("always_on"),
        heading="[ALWAYS ON — who you are, and what stays with you]",
    )
    if always_on:
        parts.append(always_on)

    if presence:
        extra = _docs_block(
            persona.documents_by_mode("presence"),
            heading="[PRESENCE — the module they turned on]",
        )
        if extra:
            parts.append(extra)

    if recalled:
        summaries = "\n".join(f"• {record.summary}" for record in recalled if record.summary)
        if summaries:
            if recall_source == "manual":
                heading = f"[MEMORIES {partner.upper()} JUST SURFACED FOR YOU]"
                query_note = f' with the query "{recall_query}"' if recall_query else ""
                intro = (
                    f"{partner} used /recall{query_note} to bring these prior memories "
                    "into your active context. Treat them as real prior memory that has "
                    "just been surfaced for this moment."
                )
            else:
                heading = "[WHAT YOU REMEMBER — relevant to right now]"
                intro = "These memories were surfaced because this thread may connect to them."
            parts.append(f"{heading}\n{intro}\n\n{summaries}")

    return "\n\n".join(part for part in parts if part)


def _docs_block(
    documents: tuple[PersonaDocument, ...],
    *,
    heading: str,
) -> str:
    selected: list[str] = []
    for doc in documents:
        selected.append(f"## {doc.display_title()}\n{doc.content}")
    if not selected:
        return ""
    return heading + "\n\n" + "\n\n---\n\n".join(selected)
