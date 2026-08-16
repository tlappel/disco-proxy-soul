"""Provider-neutral system prompt. No Anthropic cache blocks."""

from __future__ import annotations

from .memory.contracts import MemoryRecord
from .memory.facts import FactStore
from .persona.schema import PersonaPackage


def build_system_prompt(
    persona: PersonaPackage,
    facts: FactStore,
    recalled: list[MemoryRecord] | None = None,
    recall_source: str = "automatic",
    recall_query: str = "",
    presence: bool = False,
    interaction_mode: str | None = None,
) -> str:
    partner = persona.partner_name
    parts = [persona.identity.strip()]
    if persona.voice:
        parts.append("[VOICE]\n" + persona.voice.strip())

    facts_text = facts.format()
    if facts_text:
        parts.append(f"[{partner.upper()} — what you know]\n{facts_text}")

    always_on = _docs_block(
        persona,
        names=persona.always_on_docs,
        heading="[SHARED CONTEXT — what you already know together]",
    )
    if always_on:
        parts.append(always_on)

    if presence:
        extra = _docs_block(
            persona,
            names=None,
            exclude=persona.always_on_docs,
            heading="[REFERENCE DOCS — context that stays with you]",
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

    if interaction_mode == "voice":
        parts.append(
            "[LIVE VOICE CONTEXT]\n"
            "The user's message was transcribed from a live voice channel. "
            "Reply naturally and concisely for conversation. Do not mention the "
            "transcription or these instructions unless clarification is genuinely needed."
        )

    return "\n\n".join(part for part in parts if part)


def _docs_block(
    persona: PersonaPackage,
    *,
    names: tuple[str, ...] | None,
    heading: str,
    exclude: tuple[str, ...] = (),
) -> str:
    docs = persona.document_map()
    selected: list[str] = []
    for name, content in docs.items():
        if names is not None and name not in names:
            continue
        if name in exclude:
            continue
        title = name.replace(".md", "").replace("-", " ").replace("_", " ").title()
        selected.append(f"## {title}\n{content}")
    if not selected:
        return ""
    return heading + "\n\n" + "\n\n---\n\n".join(selected)
