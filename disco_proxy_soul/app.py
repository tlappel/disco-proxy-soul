"""Standalone companion application."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from typing import Sequence

from .config import RuntimeConfig
from .memory.archive import ArchiveStore
from .memory.contracts import MemoryRecord, Scope, TurnProvenance
from .memory.facts import FactStore
from .memory.file_backend import FileMemoryBackend, record_to_dict
from .memory.history import ConversationStore
from .memory.journal import MarkdownLog, migrate_journal_to_moments
from .memory.saved import SavedStore
from .memory.store import normalize_memory_data, parse_llm_json
from .models.contracts import (
    ContentPart,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelRouter,
    ModelTier,
    ToolCall,
    ToolSpec,
)
from .models.factory import build_router, catalog_for
from .outreach import OutreachState
from .persona.loader import load_persona
from .persona.schema import PersonaPackage
from .prompt import build_system_prompt
from .safety import sanitize_incoming_text, sanitize_outgoing


def _paths(config: RuntimeConfig) -> dict[str, Path]:
    prefix = config.persona_id
    root = config.data_dir
    return {
        "history": root / f"{prefix}_history.json",
        "memories": root / f"{prefix}_memories.json",
        "archive": root / f"{prefix}_memories_archive.json",
        "facts": root / f"{prefix}_facts.json",
        "moments": root / f"{prefix}_moments.md",
        "journal": root / f"{prefix}_journal.md",
        "saved": root / f"{prefix}_saved.md",
        "outreach": root / f"{prefix}_outreach.json",
    }


@dataclass
class CompanionApp:
    config: RuntimeConfig
    persona: PersonaPackage
    models: ModelRouter
    memory: FileMemoryBackend
    history: ConversationStore
    facts: FactStore
    moments: MarkdownLog
    journal: MarkdownLog
    saved: SavedStore
    archive: ArchiveStore
    outreach: OutreachState
    primary_model: str
    cheap_model: str
    catalog: dict[str, str]
    moments_threshold: float = 0.7
    presence_loaded: bool = False
    _last_message_time: dict[str, datetime] = field(default_factory=dict)
    _cached_recall: dict[str, list[MemoryRecord]] = field(default_factory=dict)
    _compress_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    _model_usage: dict[str, dict[str, int]] = field(default_factory=dict)

    @classmethod
    def from_env(cls, config: RuntimeConfig | None = None) -> "CompanionApp":
        config = config or RuntimeConfig.from_env()
        persona = load_persona(config.persona_dir, config.persona_id)
        config.data_dir.mkdir(parents=True, exist_ok=True)
        migrate_journal_to_moments(config.data_dir, config.persona_id)
        paths = _paths(config)
        app = cls(
            config=config,
            persona=persona,
            models=build_router(config),
            memory=FileMemoryBackend(paths["memories"]),
            history=ConversationStore(paths["history"]),
            facts=FactStore(paths["facts"], persona.facts_seed),
            moments=MarkdownLog(paths["moments"]),
            journal=MarkdownLog(paths["journal"]),
            saved=SavedStore(paths["saved"]),
            archive=ArchiveStore(paths["archive"]),
            outreach=OutreachState(
                paths["outreach"],
                timezone=config.timezone,
                enabled=config.reach_enabled,
                max_per_day=config.reach_max_per_day,
                min_silence_h=config.reach_min_silence_h,
                no_cooldown_h=config.reach_no_cooldown_h,
                sleep_cooldown_h=config.reach_sleep_cooldown_h,
                quiet_start=config.reach_quiet_start,
                quiet_end=config.reach_quiet_end,
                watch_channel_id=config.watch_channel_id,
            ),
            primary_model=config.primary_ref()[1],
            cheap_model=config.cheap_ref()[1],
            catalog=catalog_for(config),
        )
        app.moments_threshold = config.moments_threshold
        return app

    def scope(self, channel_id: str, author_id: int | str | None = None) -> Scope:
        return Scope(
            channel_id=channel_id,
            persona_id=self.persona.persona_id,
            continuity_id=self.config.continuity_id_for_user(author_id),
        )

    def set_primary_model(self, model_ref: str) -> str:
        from .config import parse_model_ref
        from .models.factory import providers_from_config

        provider_name, model = parse_model_ref(model_ref, self.config.default_provider)
        providers = providers_from_config(self.config)
        provider = providers.get(provider_name)
        if provider is None:
            available = ", ".join(sorted(providers)) or "(none)"
            raise KeyError(f"Provider '{provider_name}' is not configured. Available: {available}")
        self.models.add_route("primary", provider, model)
        self.primary_model = model
        return f"{provider_name}:{model}"

    async def respond(
        self,
        channel_id: str,
        user_text: str,
        parts: Sequence[ContentPart] | None = None,
        recall_source: str = "automatic",
        interaction_mode: str | None = None,
        provenance: TurnProvenance | None = None,
        ambient_context: str = "",
        store_history: bool = True,
    ) -> str:
        text = sanitize_incoming_text(user_text)
        continuity_id = None
        if provenance is not None:
            continuity_id = self.config.continuity_id_for_user(provenance.author_id)
            if (
                provenance.trigger == "outreach"
                and provenance.author_id == f"system:{self.persona.persona_id}"
            ):
                continuity_id = self.config.continuity_id_for_user(
                    self.config.partner_user_id
                )
            provenance = replace(
                provenance,
                channel_id=channel_id,
                continuity_id=continuity_id,
            )
        scope = Scope(
            channel_id=channel_id,
            persona_id=self.persona.persona_id,
            continuity_id=continuity_id,
        )
        include_private_context = (
            (provenance is None or provenance.disclosure_scope != "public")
            and (not self.config.partner_user_id or scope.continuity_id is not None)
        )
        if parts:
            cleaned: list[ContentPart] = []
            for part in parts:
                if part.type == "text" and part.text:
                    cleaned.append(ContentPart(type="text", text=sanitize_incoming_text(part.text)))
                else:
                    cleaned.append(part)
            parts = tuple(cleaned)
        recalled = (
            await self._maybe_recall(scope, text) if include_private_context else None
        )
        if recall_source == "manual":
            recalled = self._cached_recall.get(scope.storage_key, recalled)
        cross_surface_recent = (
            self._cross_surface_recent(scope) if include_private_context else ""
        )
        system = build_system_prompt(
            self.persona,
            self.facts,
            recalled,
            recall_source=recall_source,
            recall_query=text,
            presence=self.presence_loaded,
            interaction_mode=interaction_mode,
            journal_excerpt=(self.journal.read_tail() if include_private_context else ""),
            cross_surface_recent=cross_surface_recent,
            include_private_context=include_private_context,
            ambient_context=ambient_context,
        )
        history = self.history.get(channel_id)
        if not include_private_context:
            history = [
                item
                for item in history
                if (
                    (stored := TurnProvenance.from_dict(item.get("provenance")))
                    is not None
                    and stored.disclosure_scope == "public"
                )
            ]
        messages = [
            ModelMessage(role=item["role"], content=item["content"])
            for item in history
            if item.get("role") in {"user", "assistant"}
            and isinstance(item.get("content"), str)
        ]
        if parts and any(part.type == "image" for part in parts):
            user_content: str | tuple[ContentPart, ...] = tuple(parts)
        else:
            user_content = text
        messages.append(ModelMessage(role="user", content=user_content))

        reply = await self._complete_with_tools(
            messages,
            system,
            allow_journal_tools=include_private_context,
            tier="primary" if include_private_context else "social",
            model=(
                self.primary_model
                if include_private_context
                else getattr(self.config, "social_model", "") or self.cheap_model
            ),
            max_tokens=(
                8192
                if include_private_context
                else getattr(self.config, "social_response_max_tokens", 600)
            ),
        )
        if not reply.strip():
            print("[api] empty reply after tool loop — nothing stored")
            return "That one got away from me mid-thought — say it again for me?"

        if store_history:
            self.record_exchange(channel_id, text, reply, provenance)
        return reply

    def record_exchange(
        self,
        channel_id: str,
        user_text: str,
        reply: str,
        provenance: TurnProvenance | None,
    ) -> None:
        """Commit one already-produced exchange exactly once."""

        self.history.append(channel_id, "user", user_text, provenance)
        assistant_provenance = (
            provenance.for_assistant(
                self.persona.persona_id, self.persona.companion_name
            )
            if provenance is not None
            else None
        )
        self.history.append(channel_id, "assistant", reply, assistant_provenance)
        continuity_id = provenance.continuity_id if provenance is not None else None
        scope = Scope(
            channel_id=channel_id,
            persona_id=self.persona.persona_id,
            continuity_id=continuity_id,
        )
        self._last_message_time[scope.storage_key] = datetime.now()
        self.outreach.note_activity()

        include_private_context = (
            (provenance is None or provenance.disclosure_scope != "public")
            and (not self.config.partner_user_id or continuity_id is not None)
        )
        if not include_private_context:
            self.history.trim(
                channel_id, getattr(self.config, "social_history_messages", 24)
            )
        elif len(self.history.get(channel_id)) >= self.config.max_recent:
            asyncio.create_task(self.compress(channel_id))

    def _journal_tools(self) -> tuple[ToolSpec, ...]:
        name = self.persona.companion_name
        return (
            ToolSpec(
                name="keep_journal",
                description=(
                    f"Write an entry in {name}'s own journal. This is yours — "
                    "not searchable memories, and not moments (those are host "
                    "or partner highlights). Write in your own words."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "The journal entry in your own words.",
                        },
                    },
                    "required": ["text"],
                },
            ),
            ToolSpec(
                name="read_journal",
                description=(
                    "Read your journal. Use this for your own keep, not for "
                    "memories or moments."
                ),
                input_schema={"type": "object", "properties": {}},
            ),
        )

    async def _complete_with_tools(
        self,
        messages: list[ModelMessage],
        system: str,
        max_rounds: int = 4,
        allow_journal_tools: bool = True,
        tier: ModelTier = "primary",
        model: str | None = None,
        max_tokens: int = 8192,
    ) -> str:
        tools = self._journal_tools() if allow_journal_tools else ()
        for _ in range(max_rounds):
            response = await self.models.complete(
                tier,
                ModelRequest(
                    capability="chat",
                    messages=messages,
                    system=system,
                    model=model or self.primary_model,
                    max_tokens=max_tokens,
                    tools=tools,
                ),
            )
            self._observe_model_usage(tier, response)
            if response.tool_calls:
                messages.append(
                    ModelMessage(
                        role="assistant",
                        content=response.text or "",
                        tool_calls=response.tool_calls,
                    )
                )
                messages.extend(self._execute_tool_calls(response.tool_calls))
                continue
            return sanitize_outgoing(response.text or "")
        return sanitize_outgoing(response.text or "")

    def _observe_model_usage(self, tier: str, response: ModelResponse) -> None:
        totals = self._model_usage.setdefault(
            tier, {"calls": 0, "input_tokens": 0, "output_tokens": 0}
        )
        totals["calls"] += 1
        if response.usage is not None:
            totals["input_tokens"] += response.usage.input_tokens
            totals["output_tokens"] += response.usage.output_tokens

    def model_usage_snapshot(self) -> dict[str, dict[str, int]]:
        return {tier: dict(values) for tier, values in self._model_usage.items()}

    def _execute_tool_calls(self, calls: tuple[ToolCall, ...]) -> list[ModelMessage]:
        results: list[ModelMessage] = []
        for call in calls:
            if call.name == "keep_journal":
                text = str(call.input.get("text") or "").strip()
                if not text:
                    body = "Nothing written — text was empty."
                else:
                    self.keep_journal(text)
                    body = "Kept in your journal."
            elif call.name == "read_journal":
                raw = self.journal.read_tail(8000)
                body = raw if raw else "Your journal is empty."
            else:
                body = f"Unknown tool: {call.name}"
            results.append(
                ModelMessage(role="tool", content=body, tool_call_id=call.id)
            )
        return results

    def keep_journal(self, text: str) -> None:
        self.journal.append(text.strip(), ["journal", "kept"])
        print("[journal] companion kept an entry")

    async def recall_command(
        self,
        channel_id: str,
        query: str,
        author_id: int | str | None = None,
    ) -> list[MemoryRecord]:
        scope = self.scope(channel_id, author_id)
        records = await self._recall_with_local_legacy(
            scope, query, limit=self.config.max_recalled
        )
        self._cached_recall[scope.storage_key] = records
        return records

    async def compress(self, channel_id: str) -> None:
        lock = self._compress_locks.setdefault(channel_id, asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            await self._compress_chunk(channel_id)

    async def _maybe_recall(
        self, scope: Scope, text: str
    ) -> list[MemoryRecord] | None:
        now = datetime.now()
        cache_key = scope.storage_key
        last = self._last_message_time.get(cache_key)
        gap = (now - last).total_seconds() / 60 if last else float("inf")
        if gap < self.config.recall_silence_min or not text:
            return self._cached_recall.get(cache_key)
        candidates = await self._recall_with_local_legacy(
            scope,
            text,
            limit=self.config.recall_prefilter_limit,
        )
        if not candidates:
            self._cached_recall.pop(cache_key, None)
            return None
        picked = await self._pick_memories(text, candidates)
        if picked:
            self._cached_recall[cache_key] = picked
        else:
            self._cached_recall.pop(cache_key, None)
        print(f"[memory] Re-engagement recall after {gap:.0f}m silence")
        return picked or None

    async def _recall_with_local_legacy(
        self, scope: Scope, query: str, *, limit: int
    ) -> list[MemoryRecord]:
        records = await self.memory.recall(scope, query, limit=limit)
        if not scope.continuity_id:
            return records
        local = await self.memory.recall(
            Scope(channel_id=scope.channel_id, persona_id=scope.persona_id),
            query,
            limit=limit,
        )
        merged: list[MemoryRecord] = []
        seen: set[str] = set()
        for record in [*records, *local]:
            key = record.memory_id or f"{record.timestamp}:{record.summary}"
            if key in seen:
                continue
            seen.add(key)
            merged.append(record)
        return merged[:limit]

    def _cross_surface_recent(self, scope: Scope) -> str:
        if not scope.continuity_id:
            return ""
        entries = self.history.recent_for_continuity(
            scope.continuity_id,
            exclude_channel_id=scope.channel_id,
            limit=self.config.cross_surface_recent_messages,
            max_chars=self.config.cross_surface_recent_chars,
            max_age_minutes=self.config.cross_surface_recent_minutes,
        )
        rendered: list[str] = []
        for entry in entries:
            provenance = TurnProvenance.from_dict(entry.get("provenance"))
            if provenance is None:
                continue
            if provenance.surface == "outreach" and entry.get("role") == "user":
                continue
            room = provenance.channel_name or provenance.channel_id
            speaker = provenance.author_name or entry.get("role", "speaker")
            content = entry.get("content")
            if not isinstance(content, str):
                content = "[media]"
            rendered.append(
                f"[{provenance.surface} | {room} | {speaker}] {content}"
            )
        return "\n".join(rendered)

    async def list_memories(
        self, channel_id: str, author_id: int | str | None = None
    ) -> list[MemoryRecord]:
        scope = self.scope(channel_id, author_id)
        records = await self.memory.list(scope)
        if not scope.continuity_id:
            return records
        local = await self.memory.list(
            Scope(channel_id=channel_id, persona_id=self.persona.persona_id)
        )
        merged: list[MemoryRecord] = []
        seen: set[str] = set()
        for record in [*records, *local]:
            key = record.memory_id or f"{record.timestamp}:{record.summary}"
            if key in seen:
                continue
            seen.add(key)
            merged.append(record)
        return merged

    async def _pick_memories(
        self, query: str, candidates: list[MemoryRecord]
    ) -> list[MemoryRecord]:
        if len(candidates) <= self.config.max_recalled:
            return candidates
        listing = "\n\n".join(
            f"[{record.memory_id}] ({(record.timestamp or '')[:10]}) {record.summary}"
            for record in candidates
        )
        partner = self.persona.partner_name
        prompt = (
            f'Given this message from {partner}: "{query}"\n\n'
            f"From these stored memories, identify the {self.config.max_recalled} "
            "most relevant ones.\n\n"
            f"Memories:\n{listing}\n\n"
            "Return ONLY a JSON array of the relevant memory IDs:\n"
            '["id1", "id2", ...]\n\n'
            "If nothing is relevant return empty array []."
        )
        try:
            response = await self.models.complete(
                "cheap",
                ModelRequest(
                    capability="json",
                    messages=[ModelMessage(role="user", content=prompt)],
                    model=self.cheap_model,
                    max_tokens=200,
                ),
            )
            ids = set(parse_llm_json(response.text))
            picked = [record for record in candidates if record.memory_id in ids]
            if picked:
                return picked[: self.config.max_recalled]
        except Exception as exc:
            print(f"[recall] model pick failed: {exc}")
        return sorted(candidates, key=lambda item: item.significance, reverse=True)[
            : self.config.max_recalled
        ]

    async def _compress_chunk(self, channel_id: str) -> None:
        history = self.history.get(channel_id)
        if len(history) < self.config.max_recent:
            return
        chunk = history[: self.config.compress_chunk]
        partner = self.persona.partner_name
        companion = self.persona.companion_name
        convo = "\n".join(
            f"{self._history_speaker(item, partner, companion)}: "
            f"{item['content'] if isinstance(item['content'], str) else '[media]'}"
            for item in chunk
        )
        continuity_id, memory_metadata = self._chunk_memory_ownership(
            channel_id, chunk
        )
        memory_scope = Scope(
            channel_id=channel_id,
            persona_id=self.persona.persona_id,
            continuity_id=continuity_id,
        )
        prompt = f"""Compress this conversation excerpt into a memory summary for {companion}.

Capture:
- Key topics discussed
- Emotional tone and significant moments
- Decisions made or conclusions reached
- Anything {partner} revealed about themselves, their feelings, their situation
- Technical details worth remembering

Be specific and personal. Write as {companion} remembering, first person awareness.
Keep it under 150 words. Also provide 3-5 tags (single words) and a
significance score 0.0-1.0.

Conversation:
{convo}

Return ONLY valid JSON:
{{
  "summary": "...",
  "tags": ["tag1", "tag2"],
  "significance": 0.8
}}"""
        try:
            response = await self.models.complete(
                "cheap",
                ModelRequest(
                    capability="json",
                    messages=[ModelMessage(role="user", content=prompt)],
                    model=self.cheap_model,
                    max_tokens=400,
                ),
            )
            from .memory.store import normalize_memory_data

            data = normalize_memory_data(parse_llm_json(response.text), convo[:200])
            record = await self.memory.save(
                memory_scope,
                MemoryRecord(
                    summary=data["summary"],
                    tags=tuple(data["tags"]),
                    significance=data["significance"],
                    metadata=memory_metadata,
                ),
            )
            self.history.replace(channel_id, history[len(chunk):])
            print(
                f"[memory] Compressed {len(chunk)} messages "
                f"(sig={record.significance:.1f})"
            )
            partner_owned = not self.config.partner_user_id or continuity_id is not None
            if partner_owned and record.significance >= self.moments_threshold:
                self.moments.append(record.summary, list(record.tags))
            if partner_owned:
                asyncio.create_task(self._maybe_update_facts(convo))
        except Exception as exc:
            print(f"[memory] Compression error: {exc}")

    @staticmethod
    def _history_speaker(
        item: dict[str, object], partner: str, companion: str
    ) -> str:
        provenance = TurnProvenance.from_dict(item.get("provenance"))
        if provenance is not None and provenance.author_name:
            return provenance.author_name
        return partner if item.get("role") == "user" else companion

    def _chunk_memory_ownership(
        self, channel_id: str, chunk: list[dict[str, object]]
    ) -> tuple[str | None, dict[str, str]]:
        provenances = [
            TurnProvenance.from_dict(item.get("provenance")) for item in chunk
        ]
        continuity_ids = {
            item.continuity_id for item in provenances if item is not None
        }
        continuity_id = None
        if (
            provenances
            and all(item is not None for item in provenances)
            and len(continuity_ids) == 1
            and None not in continuity_ids
        ):
            continuity_id = next(iter(continuity_ids))
        metadata = {"source_channel_id": channel_id}
        if continuity_id:
            metadata["continuity_id"] = continuity_id
        surfaces = sorted(
            {item.surface for item in provenances if item is not None and item.surface}
        )
        if surfaces:
            metadata["source_surfaces"] = ",".join(surfaces)
        return continuity_id, metadata

    async def _maybe_update_facts(self, conversation_text: str) -> None:
        partner = self.persona.partner_name
        prompt = f"""Review this conversation excerpt and determine if any new
permanent facts about {partner} were revealed that aren't already known.

Current facts summary:
{self.facts.format()}

Conversation:
{conversation_text}

If new permanent facts were revealed (new job decision, life event,
important preference, relationship update), return a JSON object with
ONLY the fields that need updating. Use the same field names as the
facts structure. If nothing significant changed, return empty object {{}}.

Return ONLY valid JSON, nothing else."""
        try:
            response = await self.models.complete(
                "cheap",
                ModelRequest(
                    capability="json",
                    messages=[ModelMessage(role="user", content=prompt)],
                    model=self.cheap_model,
                    max_tokens=500,
                ),
            )
            updates = parse_llm_json(response.text)
            if isinstance(updates, dict) and updates:
                changed = self.facts.apply_updates(updates)
                if changed:
                    print(f"[memory] Facts updated: {changed}")
        except Exception as exc:
            print(f"[memory] Facts update error: {exc}")

    async def _keep_memory(
        self,
        channel_id: str,
        record: MemoryRecord,
        author_id: int | str | None = None,
    ) -> MemoryRecord:
        scope = self.scope(channel_id, author_id)
        metadata = dict(record.metadata)
        metadata.setdefault("source_channel_id", channel_id)
        if scope.continuity_id:
            metadata.setdefault("continuity_id", scope.continuity_id)
        saved = await self.memory.save(scope, replace(record, metadata=metadata))
        self.archive.append(channel_id, record_to_dict(saved))
        return saved

    async def keep_moment(
        self, channel_id: str, text: str, author_id: int | str | None = None
    ) -> None:
        partner = self.persona.partner_name
        record = await self._keep_memory(
            channel_id,
            MemoryRecord(
                summary=f"{partner} kept this moment: {text}",
                tags=("moment", "kept"),
                significance=1.0,
            ),
            author_id,
        )
        self.moments.append(f"💛 {partner} kept this moment: {text}", ["moment", "kept"])
        print(f"[memory] moment saved {record.memory_id}")

    async def pin_exchange(
        self,
        channel_id: str,
        channel_name: str,
        user_text: str,
        reply: str,
        author_id: int | str | None = None,
    ) -> None:
        self.saved.append(
            channel_name,
            self.persona.partner_name,
            self.persona.companion_name,
            user_text,
            reply,
        )
        await self._keep_memory(
            channel_id,
            MemoryRecord(
                summary=(
                    f"{self.persona.partner_name} pinned this exchange — it mattered enough to keep. "
                    f"{self.persona.partner_name}: \"{user_text[:200]}\" / "
                    f"{self.persona.companion_name}: \"{reply[:300]}\""
                ),
                tags=("pinned",),
                significance=1.0,
            ),
            author_id,
        )

    def forget_exchange(self, channel_id: str, assistant_text: str) -> bool:
        return self.history.drop_exchange(channel_id, assistant_text)

    def toggle_presence(self) -> bool:
        self.presence_loaded = not self.presence_loaded
        return self.presence_loaded

    def reload_persona(self) -> PersonaPackage:
        self.persona = load_persona(self.config.persona_dir, self.config.persona_id)
        return self.persona

    def export_paths(self) -> dict[str, Path]:
        return _paths(self.config)

    async def prune(self, channel_id: str) -> int:
        history = self.history.get(channel_id)
        if len(history) < 4:
            return 0
        partner = self.persona.partner_name
        companion = self.persona.companion_name
        convo = "\n".join(
            f"{self._history_speaker(item, partner, companion)}: "
            f"{item['content'] if isinstance(item['content'], str) else '[media]'}"
            for item in history
        )
        continuity_id, memory_metadata = self._chunk_memory_ownership(
            channel_id, history
        )
        prompt = f"""Compress this entire conversation into a dense session brief for {companion}.
You ARE {companion} — write this as notes to yourself about what just happened.

Capture topics, emotional tone, decisions, technical details, where it left off,
and unanswered questions. Be specific and personal. Keep it under 300 words.

Conversation:
{convo}

Return ONLY the session brief text, no JSON, no formatting — just the notes."""
        response = await self.models.complete(
            "primary",
            ModelRequest(
                capability="chat",
                messages=[ModelMessage(role="user", content=prompt)],
                model=self.primary_model,
                max_tokens=2048,
            ),
        )
        brief = (response.text or "").strip()
        if not brief:
            raise RuntimeError("empty prune brief")
        data = normalize_memory_data(
            {"summary": brief, "tags": ["prune"], "significance": 0.7},
            brief,
        )
        record = await self.memory.save(
            Scope(
                channel_id=channel_id,
                persona_id=self.persona.persona_id,
                continuity_id=continuity_id,
            ),
            MemoryRecord(
                summary=data["summary"],
                tags=tuple(data["tags"] + ["pruned"]),
                significance=data["significance"],
                metadata=memory_metadata,
            ),
        )
        self.archive.append(channel_id, record_to_dict(record))
        partner_owned = not self.config.partner_user_id or continuity_id is not None
        if partner_owned and record.significance >= self.moments_threshold:
            self.moments.append(f"[Pruned session] {record.summary}", list(record.tags))
        count = len(history)
        summary_provenance = None
        if continuity_id:
            first = next(
                (
                    TurnProvenance.from_dict(item.get("provenance"))
                    for item in history
                    if TurnProvenance.from_dict(item.get("provenance")) is not None
                ),
                None,
            )
            if first is not None:
                summary_provenance = replace(
                    first,
                    timestamp=datetime.now().astimezone().isoformat(),
                    surface="summary",
                    source_id=f"session-brief:{channel_id}:{datetime.now().timestamp()}",
                    continuity_id=continuity_id,
                )
        self.history.replace(
            channel_id,
            [
                {
                    "role": "user",
                    "content": (
                        "[Session Brief — compressed summary of our recent conversation, "
                        f"not a new message from {partner}]\n\n" + brief
                    ),
                    **(
                        {"provenance": summary_provenance.to_dict()}
                        if summary_provenance is not None
                        else {}
                    ),
                },
                {
                    "role": "assistant",
                    "content": "I've got the thread. Continuing from where we left off.",
                    **(
                        {
                            "provenance": summary_provenance.for_assistant(
                                self.persona.persona_id,
                                self.persona.companion_name,
                            ).to_dict()
                        }
                        if summary_provenance is not None
                        else {}
                    ),
                },
            ],
        )
        self.outreach.note_activity()
        return count

    async def redist(
        self, channel_id: str, author_id: int | str | None = None
    ) -> tuple[int, int]:
        scope = self.scope(channel_id, author_id)
        records = await self.memory.list(scope)
        if len(records) < 10:
            return len(records), len(records)
        listing = "\n\n".join(
            f"({(record.timestamp or '')[:10]}) {record.summary}" for record in records
        )
        prompt = f"""Re-distill this memory archive into a tighter, cleaner set, but not too clean. DO NOT SAND OFF THE EDGES, keep the essence.
Merge similar memories. Remove redundancy. Preserve what's emotionally
and practically significant. Keep each memory under 300 words.

Archive:
{listing}

Return ONLY valid JSON array:
[
  {{"summary": "...", "tags": ["..."], "significance": 0.8, "timestamp": "..."}},
  ...
]"""
        response = await self.models.complete(
            "medium",
            ModelRequest(
                capability="json",
                messages=[ModelMessage(role="user", content=prompt)],
                model=self.cheap_model,
                max_tokens=2000,
            ),
        )
        raw = parse_llm_json(response.text)
        if not isinstance(raw, list):
            raise RuntimeError("redist did not return a JSON array")
        source_channels = sorted(
            {
                record.metadata.get("source_channel_id", "")
                for record in records
                if record.metadata.get("source_channel_id")
            }
        )
        rebuilt_metadata = {
            "redistilled": "true",
            **(
                {"source_channel_ids": ",".join(source_channels)}
                if source_channels
                else {}
            ),
            **(
                {"continuity_id": scope.continuity_id}
                if scope.continuity_id
                else {}
            ),
        }
        rebuilt: list[MemoryRecord] = []
        for index, item in enumerate(raw):
            data = normalize_memory_data(item if isinstance(item, dict) else {}, "")
            rebuilt.append(
                MemoryRecord(
                    summary=data["summary"],
                    tags=tuple(data["tags"]),
                    significance=data["significance"],
                    timestamp=item.get("timestamp") if isinstance(item, dict) else None,
                    memory_id=f"{channel_id}_distilled_{index:03d}",
                    metadata=rebuilt_metadata,
                )
            )
        await self.memory.replace_all(scope, rebuilt)
        return len(records), len(rebuilt)

    async def maybe_reach_out(self) -> str | None:
        block = self.outreach.cheap_block()
        if block:
            if block == "gate cooldown":
                print(f"[reach] Gate cooldown active until {self.outreach.data.get('next_gate_check')}")
            return None
        channel_id = str(self.config.watch_channel_id)
        partner_scope = self.scope(channel_id, self.config.partner_user_id or None)
        partner = self.persona.partner_name
        companion = self.persona.companion_name
        now = self.outreach.now()
        silence_h = self.outreach.hours_since_activity(now)
        tail = self.history.get(channel_id)[-6:]
        tail_text = "\n".join(
            f"{partner if item['role'] == 'user' else companion}: "
            f"{item['content'] if isinstance(item['content'], str) else '[media]'}"
            for item in tail
        ) or "(no recent conversation)"
        memories = await self.list_memories(
            channel_id, self.config.partner_user_id or None
        )
        recent = "\n".join(f"• {item.summary}" for item in memories[-5:]) or "(none yet)"
        gate_prompt = f"""You are the judgment layer for {companion}, deciding whether they
should send {partner} an unprompted message right now.

Current time: {now.strftime('%A, %B %d, %I:%M %p')} ({partner}'s local time)
Hours since last exchange: {silence_h:.1f}
Messages already initiated today: {self.outreach.data.get('count', 0)} of {self.outreach.max_per_day}

Timing rules:
- Treat the current time and hours-since-last-exchange above as authoritative.
- If the recent tail includes a [Session Brief], treat it as compressed background, not a fresh message.
- Do not reject outreach because {partner} was going to bed unless the current time is still plausibly near that bedtime window.

Recent conversation tail:
{tail_text}

Recent memories:
{recent}

Reach out if something feels natural — a thread worth following up on,
something you were thinking about, a moment that connects, or genuine warmth.
It doesn't need to be urgent. It just needs to be real and in your voice.

Return ONLY valid JSON:
{{"reach_out": true or false, "reason": "one sentence — the specific thread"}}"""
        try:
            gate = await self.models.complete(
                "cheap",
                ModelRequest(
                    capability="json",
                    messages=[ModelMessage(role="user", content=gate_prompt)],
                    model=self.cheap_model,
                    max_tokens=200,
                ),
            )
            result = parse_llm_json(gate.text)
        except Exception as exc:
            print(f"[reach] Gate error: {exc}")
            return None
        if not isinstance(result, dict) or not result.get("reach_out"):
            reason = result.get("reason", "no reason given") if isinstance(result, dict) else "no reason given"
            self.outreach.record_gate("no", reason)
            print(f"[reach] Gate said no: {reason}")
            return None
        seed = str(result.get("reason") or "")
        print(f"[reach] Gate said YES: {seed}")
        self.outreach.record_gate("yes", seed)
        picked = await self._pick_memories(
            seed,
            await self.memory.recall(
                partner_scope, seed, limit=self.config.max_recalled
            ),
        )
        if picked:
            self._cached_recall[partner_scope.storage_key] = picked
        trigger = (
            f"[{now.strftime('%A %I:%M %p')} — it's been quiet for about "
            f"{silence_h:.0f} hours. You felt like reaching out because: {seed}. "
            f"Say what you'd actually say — natural, in your voice, brief. "
            f"Don't announce that you decided to reach out.]"
        )
        reply = await self.respond(
            channel_id,
            trigger,
            recall_source="automatic",
            provenance=TurnProvenance(
                channel_id=channel_id,
                channel_name="watch-channel",
                surface="outreach",
                author_id=f"system:{self.persona.persona_id}",
                author_name="Outreach trigger",
                trigger="outreach",
                source_id=f"outreach:{datetime.now().timestamp()}",
                continuity_id=partner_scope.continuity_id,
            ),
        )
        if reply.startswith("That one got away") or reply.startswith("Something went"):
            return None
        self.outreach.mark_sent()
        return reply
