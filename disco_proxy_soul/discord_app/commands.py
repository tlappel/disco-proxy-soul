"""Standalone slash commands — the v1 control panel, persona-labeled."""

from __future__ import annotations

import asyncio

import discord
from discord import app_commands

from ..app import CompanionApp
from .voice_session import VoiceSessionError, VoiceSessionManager, VoiceSessionStatus


def live_start_notice(
    display_name: str,
    *,
    tts_enabled: bool,
    barge_in_name: str | None = None,
) -> str:
    tts_disclosure = (
        " Naomi's response text will be sent to ElevenLabs for voice synthesis "
        "and played in the voice channel."
        if tts_enabled
        else " Naomi's response will appear as Discord text."
    )
    barge_in_note = (
        f' During playback, say "{barge_in_name}, wait" to interrupt intentionally.'
        if barge_in_name
        else ""
    )
    return (
        f"🔴 **Live voice chat is starting for {display_name}.** Their voice "
        "audio is sent to Gladia for transcription. Accepted final turns reach "
        "companion cognition and appear in this channel."
        f"{tts_disclosure}{barge_in_note} "
        "Live mode saves no raw WAV, PCM, or generated audio. "
        "Use `/voice-chat stop` to end it."
    )


def register_commands(
    tree: app_commands.CommandTree,
    app: CompanionApp,
    voice_sessions: VoiceSessionManager,
) -> None:
    companion = app.persona.companion_name
    partner = app.persona.partner_name
    voice_chat = app_commands.Group(
        name="voice-chat",
        description="Control experimental single-speaker live voice chat",
    )
    tree.add_command(voice_chat)

    def live_status_text(status: VoiceSessionStatus) -> str:
        counters = status.counters

        def latency(metric) -> str:
            if not metric.samples:
                return "n/a"
            return (
                f"{metric.last_ms:.0f}/{metric.average_ms:.0f}/"
                f"{metric.maximum_ms:.0f}"
            )

        return (
            f"**Live Voice Status — {status.state.value}**\n"
            f"Starter: **{status.starter_name}**\n"
            f"Voice channel: <#{status.channel_id}>\n"
            f"Audio queues: loop {status.queue_size}/{status.queue_capacity}, "
            f"thread ring {status.ingress_pending}/{status.queue_capacity} packets\n"
            f"Drops: thread {counters.ingress_drops}, loop {counters.queue_drops}, "
            f"clock {counters.clock_dropped_packets}\n"
            f"Gladia frames sent: {counters.sent_frames}; "
            f"finals: {counters.final_transcripts}; "
            f"partials: {counters.partial_transcripts}\n"
            f"Gladia reconnects: {counters.gladia_reconnects}; "
            f"failed attempts: {counters.gladia_reconnect_failures}; "
            "ambiguous frames dropped: "
            f"{counters.gladia_ambiguous_frames_dropped}\n"
            f"Gladia sessions: {counters.gladia_sessions_started}; "
            f"planned rotations: {counters.gladia_rotations}\n"
            f"Turns accepted: {counters.accepted_turns}; "
            f"text replies: {counters.companion_responses}; "
            f"spoken replies: {counters.spoken_responses}\n"
            f"Playback active: {'yes' if status.playback_active else 'no'}; "
            f"finals spoken during playback: "
            f"{counters.finals_spoken_during_playback}\n"
            f"Intentional barge-ins: {counters.barge_in_cues}; "
            f"playbacks interrupted: {counters.interrupted_playbacks}\n"
            "Latency ms (last/avg/max): "
            f"STT {latency(counters.stt_final_latency)}; "
            f"model {latency(counters.cognition_latency)}; "
            f"TTS {latency(counters.tts_first_frame_latency)}; "
            f"play {latency(counters.playback_start_latency)}\n"
            f"Inserted silence: {counters.inserted_silence_samples / 48_000:.2f}s; "
            f"RTP gaps: {counters.rtp_gap_samples / 48_000:.2f}s\n"
            f"RTP discontinuities: {counters.rtp_discontinuities}; "
            f"playout reanchors: {counters.playout_reanchors}; "
            f"late samples: {counters.late_audio_samples}\n"
            f"Transport completion: {status.gladia_completion}\n"
            f"Last warning: {status.last_error or 'none'}"
        )

    @voice_chat.command(name="start", description="Start live voice chat for your voice")
    async def slash_voice_chat_start(interaction: discord.Interaction) -> None:
        guild = interaction.guild
        voice_state = getattr(interaction.user, "voice", None)
        if guild is None or voice_state is None or voice_state.channel is None:
            await interaction.response.send_message(
                "Join a server voice channel first.", ephemeral=True
            )
            return
        if interaction.channel is None:
            await interaction.response.send_message(
                "Run this command from a server text channel.", ephemeral=True
            )
            return
        try:
            voice_sessions.validate_start(guild.id)
        except VoiceSessionError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.defer(thinking=True)
        await interaction.followup.send(
            live_start_notice(
                interaction.user.display_name,
                tts_enabled=bool(voice_sessions.config.voice_tts_enabled),
                barge_in_name=(
                    companion
                    if bool(voice_sessions.config.voice_barge_in_enabled)
                    else None
                ),
            )
        )
        try:
            status = await voice_sessions.start(
                guild=guild,
                voice_channel=voice_state.channel,
                text_channel=interaction.channel,
                starter=interaction.user,
            )
        except VoiceSessionError as exc:
            await interaction.followup.send(f"Live voice could not start: {exc}")
            return
        await interaction.followup.send(
            f"Listening to **{status.starter_name}** in <#{status.channel_id}>. "
            "Only the starter is transcribed in this single-human mode."
        )

    @voice_chat.command(name="stop", description="Stop this server's live voice chat")
    async def slash_voice_chat_stop(interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return
        await interaction.response.defer(thinking=True)
        status = await voice_sessions.stop(guild.id)
        if status is None:
            await interaction.followup.send(
                "No live voice chat session is running.", ephemeral=True
            )
            return
        await interaction.followup.send(
            "Live voice chat stopped. No raw audio was saved.\n"
            + live_status_text(status)
        )

    @voice_chat.command(name="status", description="Show live voice chat health")
    async def slash_voice_chat_status(interaction: discord.Interaction) -> None:
        guild = interaction.guild
        status = voice_sessions.status(guild.id) if guild is not None else None
        if status is None:
            await interaction.response.send_message(
                "No live voice chat session is running.", ephemeral=True
            )
            return
        await interaction.response.send_message(live_status_text(status), ephemeral=True)

    @tree.command(name="status", description=f"Show {companion}'s memory status")
    async def slash_status(interaction: discord.Interaction) -> None:
        ckey = str(interaction.channel_id)
        history = app.history.get(ckey)
        memories = await app.memory.list(app.scope(ckey))
        facts = app.facts.raw()
        counts = app.persona.mode_counts()
        presence = "loaded" if app.presence_loaded else "off"
        msg = (
            f"**{companion} Memory Status**\n"
            f"Model: `{app.primary_model}`\n"
            f"Recent messages: {len(history)}/{app.config.max_recent}\n"
            f"Memory chunks: {len(memories)}\n"
            f"Moments: {app.moments.entry_count()}\n"
            f"Journal entries: {app.journal.entry_count()}\n"
            f"Facts last updated: {str(facts.get('last_updated', 'never'))[:10]}\n"
            f"Persona extras: {counts['always_on']} always-on, "
            f"{counts['presence']} presence, {counts['author']} docs\n"
            f"Presence module: {presence}"
        )
        await interaction.response.send_message(msg, ephemeral=True)

    @tree.command(name="clear", description="Clear recent history for this channel")
    async def slash_clear(interaction: discord.Interaction) -> None:
        app.history.clear(str(interaction.channel_id))
        await interaction.response.send_message(
            "Recent history cleared. Memories are still intact.", ephemeral=True
        )

    @tree.command(name="history-status", description="Show stored rolling history counts")
    async def slash_history_status(interaction: discord.Interaction) -> None:
        stats = app.history.stats(str(interaction.channel_id))
        largest = "\n".join(
            f"- `{channel}`: {count} messages" for channel, count in stats["largest"]
        ) or "- none"
        msg = (
            f"**History Store Status**\n"
            f"Current channel: {stats['current']}/{app.config.max_recent} messages\n"
            f"Stored channels: {stats['channels']} non-empty"
            f" ({stats['empty_channels']} empty records)\n"
            f"Total uncompressed messages: {stats['total']}\n"
            f"Largest channel windows:\n{largest}\n\n"
            f"`/export history` downloads all channel windows, not just this channel."
        )
        await interaction.response.send_message(msg, ephemeral=True)

    model_choices = [
        app_commands.Choice(name=name[:100], value=value[:100])
        for name, value in list(app.catalog.items())[:25]
    ]

    @tree.command(name="model", description=f"Switch {companion}'s response model")
    @app_commands.describe(choice="Which model to use")
    @app_commands.choices(choice=model_choices)
    async def slash_model(
        interaction: discord.Interaction, choice: app_commands.Choice[str]
    ) -> None:
        try:
            resolved = app.set_primary_model(choice.value)
        except KeyError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            f"Switched to **{choice.name}** (`{resolved}`). Lasts until restart.",
            ephemeral=True,
        )

    @tree.command(name="presence", description="Toggle the presence module")
    async def slash_presence(interaction: discord.Interaction) -> None:
        loaded = app.toggle_presence()
        names = ", ".join(
            doc.name for doc in app.persona.documents_by_mode("presence")
        ) or "none configured"
        if loaded:
            await interaction.response.send_message(
                f"Presence loaded. ({names})", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "Presence unloaded — saving tokens.", ephemeral=True
            )

    @tree.command(name="recall", description=f"Search {companion}'s memories")
    @app_commands.describe(query="What to search for")
    async def slash_recall(interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer(ephemeral=True)
        records = await app.recall_command(str(interaction.channel_id), query)
        if not records:
            await interaction.followup.send(
                f"**Recall success:** no\n**Query:** *{query}*\n**Memories loaded:** 0",
                ephemeral=True,
            )
            return
        words = sum(len(record.summary.split()) for record in records)
        chars = sum(len(record.summary) for record in records)
        await interaction.followup.send(
            f"**Recall success:** yes\n**Query:** *{query}*\n"
            f"**Memories loaded:** {len(records)}\n"
            f"**Loaded size:** {words} words / {chars} chars\n"
            f"**Active context:** updated for {companion}'s next response",
            ephemeral=True,
        )
        channel = interaction.channel
        if channel:
            prompt = (
                f'[Recall event — {partner} used /recall with query "{query}". '
                f"{len(records)} memories were just surfaced into your active context. "
                "Acknowledge this naturally. Do not dump or recite the memories mechanically.]"
            )
            reply = await app.respond(str(interaction.channel_id), prompt, recall_source="manual")
            if len(reply) <= 2000:
                await channel.send(reply)
            else:
                for index in range(0, len(reply), 1900):
                    await channel.send(reply[index:index + 1900])

    @tree.command(name="memories", description="Show stored memory chunks")
    async def slash_memories(interaction: discord.Interaction) -> None:
        records = await app.memory.list(app.scope(str(interaction.channel_id)))
        if not records:
            await interaction.response.send_message("No memories stored yet.", ephemeral=True)
            return
        lines = [f"**Stored Memories ({len(records)} chunks)**\n"]
        for record in records[-10:]:
            stamp = (record.timestamp or "")[:10]
            tags = ", ".join(record.tags)
            lines.append(f"📅 {stamp} | ⭐{record.significance:.1f} | {tags}")
            lines.append(f"   {record.summary[:120]}...\n")
        await interaction.response.send_message("\n".join(lines)[:2000], ephemeral=True)

    @tree.command(name="moments", description="Show host and partner highlights")
    async def slash_moments(interaction: discord.Interaction) -> None:
        excerpt = app.moments.read_tail()
        if not excerpt:
            await interaction.response.send_message(
                "No moments yet. /moment and significant compressions land here.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(f"**Moments (recent)**\n{excerpt}", ephemeral=True)

    @tree.command(name="journal", description=f"Show {companion}'s own journal")
    async def slash_journal(interaction: discord.Interaction) -> None:
        excerpt = app.journal.read_tail()
        if not excerpt:
            await interaction.response.send_message(
                f"No journal entries yet. This file is {companion}'s — they write it.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(f"**Journal (recent)**\n{excerpt}", ephemeral=True)

    @tree.command(name="saved", description="Show saved exchanges")
    async def slash_saved(interaction: discord.Interaction) -> None:
        excerpt = app.saved.read_tail()
        await interaction.response.send_message(excerpt or "Nothing saved yet.", ephemeral=True)

    @tree.command(name="reflect", description=f"Let {companion} read and respond to memory data")
    @app_commands.describe(topic="What to reflect on")
    @app_commands.choices(topic=[
        app_commands.Choice(name="facts", value="facts"),
        app_commands.Choice(name="journal", value="journal"),
        app_commands.Choice(name="moments", value="moments"),
        app_commands.Choice(name="memories", value="memories"),
        app_commands.Choice(name="docs", value="docs"),
    ])
    async def slash_reflect(
        interaction: discord.Interaction, topic: app_commands.Choice[str]
    ) -> None:
        await interaction.response.defer()
        ckey = str(interaction.channel_id)
        if topic.value == "facts":
            data = f"Here are the facts you currently hold about {partner}:\n\n{app.facts.format()}"
        elif topic.value == "journal":
            raw = app.journal.read_tail(8000)
            data = (
                f"Here are your own journal entries:\n\n{raw}"
                if raw
                else "Your journal is empty. You write it with keep_journal."
            )
        elif topic.value == "moments":
            raw = app.moments.read_tail(8000)
            data = (
                f"Here are the recent moments (host or {partner} highlights, not your journal):\n\n{raw}"
                if raw
                else "No moments yet. Those are host or partner highlights, not your journal."
            )
        elif topic.value == "memories":
            mems = await app.memory.list(app.scope(ckey))
            if mems:
                summaries = "\n".join(
                    f"- [{(m.timestamp or '')[:10]}] {m.summary}" for m in mems[-10:]
                )
                data = f"Here are your most recent memory chunks for this channel:\n\n{summaries}"
            else:
                data = "You don't have any stored memories for this channel yet."
        else:
            visible = list(app.persona.documents_by_mode("always_on"))
            if app.presence_loaded:
                visible.extend(app.persona.documents_by_mode("presence"))
            library = app.persona.documents_by_mode("author")
            if visible:
                pieces = []
                for doc in visible:
                    body = doc.content[:500] + "..." if len(doc.content) > 500 else doc.content
                    pieces.append(f"**{doc.name}** [{doc.mode}]\n{body}")
                data = "Here are the persona docs currently in your context:\n\n" + "\n\n---\n\n".join(pieces)
            else:
                data = "No persona docs are in active context right now."
            if library:
                names = ", ".join(doc.name for doc in library)
                data += f"\n\nOther docs (library, not in the prompt): {names}"
        prompt = (
            f"{partner} used /reflect to let you see your own {topic.value}. "
            "Read through this and share what stands out, what feels right, "
            f"what might be missing or outdated. Be yourself.\n\n{data}"
        )
        reply = await app.respond(ckey, prompt)
        if len(reply) <= 2000:
            await interaction.followup.send(reply)
            return
        for index in range(0, len(reply), 1900):
            await interaction.followup.send(reply[index:index + 1900])

    @tree.command(name="docs", description="Show loaded persona layers")
    @app_commands.describe(name="Optional: view a specific doc by filename")
    async def slash_docs(interaction: discord.Interaction, name: str | None = None) -> None:
        if not app.persona.documents:
            await interaction.response.send_message("No persona layers loaded.", ephemeral=True)
            return
        if name:
            doc = app.persona.find_document(name)
            if doc is None:
                names = ", ".join(item.name for item in app.persona.documents)
                await interaction.response.send_message(
                    f"No doc named `{name}`. Loaded: {names}", ephemeral=True
                )
                return
            excerpt = (
                doc.content[:1800] + "\n\n*(truncated)*"
                if len(doc.content) > 1800
                else doc.content
            )
            tags = f" tags: {', '.join(doc.tags)}" if doc.tags else ""
            await interaction.response.send_message(
                f"**{doc.name}** [{doc.mode}{tags}]\n{excerpt}",
                ephemeral=True,
            )
            return
        lines = [f"**Persona layers ({len(app.persona.documents)})**\n"]
        for doc in app.persona.documents:
            if doc.mode == "always_on":
                state = "always-on"
            elif doc.mode == "presence":
                state = (
                    "presence — loaded"
                    if app.presence_loaded
                    else "presence — /presence to load"
                )
            else:
                state = "docs — library, not in prompt"
            tags = f" · {', '.join(doc.tags)}" if doc.tags else ""
            lines.append(f"📄 `{doc.name}` — {len(doc.content)} chars [{state}]{tags}")
        lines.append("\nUse `/docs name:<filename>` to view one.")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @tree.command(name="reload-docs", description="Reload persona package from disk")
    async def slash_reload_docs(interaction: discord.Interaction) -> None:
        persona = app.reload_persona()
        if persona.documents:
            counts = persona.mode_counts()
            await interaction.response.send_message(
                f"Reloaded {len(persona.documents)} layers "
                f"({counts['always_on']} always-on, {counts['presence']} presence, "
                f"{counts['author']} docs).",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"No extra layers found in {persona.root}", ephemeral=True
            )

    @tree.command(name="moment", description="Save a moment that matters — in your own words")
    @app_commands.describe(text="What happened and why it mattered")
    async def slash_moment(interaction: discord.Interaction, text: str) -> None:
        await app.keep_moment(str(interaction.channel_id), text)
        await interaction.response.send_message("Kept. 💛", ephemeral=True)

    @tree.command(name="prune", description="Compress conversation into a summary")
    async def slash_prune(interaction: discord.Interaction) -> None:
        ckey = str(interaction.channel_id)
        if len(app.history.get(ckey)) < 4:
            await interaction.response.send_message("Not enough history to prune.", ephemeral=True)
            return
        count = len(app.history.get(ckey))
        await interaction.response.send_message(
            f"Pruning {count} messages into a summary...", ephemeral=True
        )
        try:
            pruned = await app.prune(ckey)
            await interaction.followup.send(
                f"Pruned {pruned} messages → session brief + memory. "
                "Window is open, context is intact.",
                ephemeral=True,
            )
        except Exception as exc:
            print(f"[prune] Error: {exc}")
            await interaction.followup.send(f"Prune failed: {exc}", ephemeral=True)

    @tree.command(name="redist", description="Re-distill the memory archive")
    async def slash_redist(interaction: discord.Interaction) -> None:
        await interaction.response.send_message("Re-distilling memories...", ephemeral=True)
        before, after = await app.redist(str(interaction.channel_id))
        await interaction.followup.send(
            f"Done. {before} → {after} memory chunks remaining.", ephemeral=True
        )

    @tree.command(name="export", description="Download data files")
    @app_commands.describe(data="Which data to export")
    @app_commands.choices(data=[
        app_commands.Choice(name="memories — working set", value="memories"),
        app_commands.Choice(name="archive — permanent record", value="archive"),
        app_commands.Choice(name="facts — what I know", value="facts"),
        app_commands.Choice(name="moments — host and partner highlights", value="moments"),
        app_commands.Choice(name="journal — companion keep", value="journal"),
        app_commands.Choice(name="saved — pinned exchanges", value="saved"),
        app_commands.Choice(name="history — all channel windows", value="history"),
    ])
    async def slash_export(
        interaction: discord.Interaction, data: app_commands.Choice[str]
    ) -> None:
        paths = app.export_paths()
        path = paths.get(data.value)
        if path is None or not path.exists():
            await interaction.response.send_message(
                f"No {data.value} data exists yet.", ephemeral=True
            )
            return
        size = path.stat().st_size
        if size == 0:
            await interaction.response.send_message(f"{data.value} file is empty.", ephemeral=True)
            return
        if size > 25 * 1024 * 1024:
            await interaction.response.send_message(
                f"{data.value} is too large to send ({size // 1024 // 1024}MB).",
                ephemeral=True,
            )
            return
        await interaction.response.send_message("Here you go.", ephemeral=True)
        await interaction.followup.send(
            file=discord.File(path, filename=path.name),
            ephemeral=True,
        )

    @tree.command(name="reach", description=f"Show {companion}'s outreach status")
    async def slash_reach(interaction: discord.Interaction) -> None:
        gate = app.outreach.status()
        data = app.outreach.data
        msg = (
            f"**Outreach Status**\n"
            f"Enabled: {app.outreach.enabled}\n"
            f"Gate status: {gate['status']} — {gate['reason']}\n"
            f"Next eligible: {gate['next_time']}\n"
            f"Loop started: {app.outreach.format_dt(str(data.get('loop_started') or ''))}\n"
            f"Last loop tick: {app.outreach.format_dt(str(data.get('last_loop_tick') or ''))}\n"
            f"Last loop error: {data.get('last_loop_error') or 'none'}\n"
            f"Today: {gate['count']}/{app.outreach.max_per_day} used\n"
            f"Last outreach: {app.outreach.format_dt(str(data.get('last_outreach') or ''))}\n"
            f"Last activity: {app.outreach.format_dt(str(data.get('last_activity') or ''))}\n"
            f"Last gate: {data.get('last_gate_result') or 'none'}"
            f"{' — ' + str(data.get('last_gate_reason') or '') if data.get('last_gate_reason') else ''}\n"
            f"Next gate check: {app.outreach.format_dt(str(data.get('next_gate_check') or ''))}\n"
            f"Silence gate: {app.outreach.min_silence_h}h (currently {gate['silence_h']}h quiet)\n"
            f"No cooldown: {app.outreach.no_cooldown_h}h | Sleep cooldown: {app.outreach.sleep_cooldown_h}h\n"
            f"Quiet hours: {app.outreach.quiet_start}:00-{app.outreach.quiet_end}:00 {app.outreach.tz.key}"
        )
        await interaction.response.send_message(msg, ephemeral=True)

    @tree.command(name="reach-reset", description="Reset outreach counters and timers")
    async def slash_reach_reset(interaction: discord.Interaction) -> None:
        app.outreach.reset_day()
        await interaction.response.send_message(
            "Outreach reset. Count is 0 and silence timer is clear.",
            ephemeral=True,
        )
    @tree.command(name="voice-record", description="Record decoded Discord audio for diagnosis")
    async def slash_voice_record(interaction: discord.Interaction) -> None:
        guild = interaction.guild
        voice_state = getattr(interaction.user, "voice", None)
        if guild is None or voice_state is None or voice_state.channel is None:
            await interaction.response.send_message(
                "Join a server voice channel first.", ephemeral=True
            )
            return
        try:
            voice_sessions.validate_diagnostic_start(guild.id)
        except VoiceSessionError as exc:
            await interaction.response.send_message(
                str(exc),
                ephemeral=True,
            )
            return

        # A failed/cancelled acknowledgement must not leave a manager reservation.
        await interaction.response.defer(thinking=True)
        channel = voice_state.channel
        try:
            # Privacy/consent notice must be visible before connection, listen,
            # capture construction, or any WAV file can begin.
            await interaction.followup.send(
                "🔴 **Diagnostic voice recording is about to start.** Everyone in "
                "the channel: decoded audio will be saved locally as per-speaker "
                "WAV files until `/voice-stop` is used."
            )
            await voice_sessions.start_diagnostic(
                guild=guild,
                voice_channel=channel,
            )
            await interaction.followup.send(
                "Diagnostic recording started. Use `/voice-stop` when the test phrase "
                "is finished."
            )
            return
        except BaseException as exc:
            # This is idempotent and cancellation-latched. If the public notice
            # failed after listen began, capture and Discord ownership still close.
            if voice_sessions.has_diagnostic(guild.id):
                try:
                    await voice_sessions.stop_diagnostic(guild.id)
                except VoiceSessionError:
                    pass
            if isinstance(exc, asyncio.CancelledError):
                raise
            print(
                "[voice] Could not start diagnostic capture "
                f"({type(exc).__name__})"
            )
            await interaction.followup.send(
                "Voice recording could not start. Check the bot terminal for the "
                f"transport error (`{type(exc).__name__}`).",
                ephemeral=True,
            )
            return

    @tree.command(name="voice-stop", description="Stop diagnostic voice recording")
    async def slash_voice_stop(interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command only works in a server.", ephemeral=True
            )
            return
        if not voice_sessions.has_diagnostic(guild.id):
            await interaction.response.send_message(
                "No diagnostic voice recording is running.", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)
        try:
            stopped = await voice_sessions.stop_diagnostic(guild.id)
        except VoiceSessionError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        if stopped is None:
            await interaction.followup.send(
                "No diagnostic voice recording is running.", ephemeral=True
            )
            return

        summaries = stopped.summaries
        if not summaries:
            result = "Recording stopped, but no human PCM frames were captured."
        else:
            lines = [
                "Recording stopped. Local diagnostic files:",
                f"`{stopped.output_dir}`",
            ]
            for summary in summaries[:10]:
                lines.append(
                    f"- `{summary.path.name}` — {summary.display_name}, "
                    f"{summary.duration_seconds:.2f}s"
                )
            if len(summaries) > 10:
                lines.append(f"- …and {len(summaries) - 10} more speaker files")
            result = "\n".join(lines)
        await interaction.followup.send(result)
