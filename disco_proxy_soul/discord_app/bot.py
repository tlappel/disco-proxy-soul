"""Discord client for the companion host."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import os
from collections import defaultdict

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

from ..app import CompanionApp
from ..adapters.gladia_live import redact_sensitive_text
from ..adapters.ollama_attention import OllamaAttentionConfig, OllamaAttentionJudge
from ..config import RuntimeConfig
from ..memory.contracts import TurnProvenance
from ..safety import sanitize_outgoing
from .attachments import build_user_parts
from .commands import register_commands
from .social_presence import (
    SocialMessage,
    SocialPresence,
    SocialRoute,
    clear_name_address,
)
from .voice_session import VoiceSessionManager


APPLICATION_LOGGER_NAME = "disco_proxy_soul"


class _CompanionCommandTree(app_commands.CommandTree):
    """Keep private control and memory commands with the configured partner."""

    def __init__(self, client: discord.Client, app: CompanionApp) -> None:
        super().__init__(client)
        self._companion_app = app

    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        partner_user_id = self._companion_app.config.partner_user_id
        if not partner_user_id or int(interaction.user.id) == partner_user_id:
            return True
        companion = self._companion_app.persona.companion_name
        await interaction.response.send_message(
            f"{companion}'s control and memory commands are restricted to "
            "the configured partner.",
            ephemeral=True,
        )
        return False


class _RedactingApplicationFormatter(logging.Formatter):
    def __init__(self, api_key: str) -> None:
        super().__init__("%(levelname)s %(name)s: %(message)s")
        self._api_key = api_key

    def format(self, record: logging.LogRecord) -> str:
        return redact_sensitive_text(super().format(record), self._api_key)


def configure_application_logging(
    api_key: str = "", *, stream: object | None = None
) -> logging.Logger:
    """Expose this app's INFO diagnostics without changing Discord logging."""

    app_logger = logging.getLogger(APPLICATION_LOGGER_NAME)
    app_logger.setLevel(logging.INFO)
    app_logger.propagate = False
    formatter = _RedactingApplicationFormatter(api_key)
    for handler in app_logger.handlers:
        if getattr(handler, "_dps_application_handler", False):
            handler.setLevel(logging.INFO)
            handler.setFormatter(formatter)
            return app_logger
    handler = logging.StreamHandler(stream)  # type: ignore[arg-type]
    handler.setLevel(logging.INFO)
    handler.setFormatter(formatter)
    handler._dps_application_handler = True  # type: ignore[attr-defined]
    app_logger.addHandler(handler)
    return app_logger


def _clean_mentions(content: str, user_id: int) -> str:
    return content.replace(f"<@{user_id}>", "").replace(f"<@!{user_id}>", "").strip()


def _response_trigger(
    *, mentioned: bool, is_dm: bool, in_active: bool, is_reply: bool
) -> str | None:
    if is_dm:
        return "dm"
    if mentioned:
        return "mention"
    if is_reply:
        return "reply"
    if in_active:
        return "active-channel"
    return None


def _author_allowed(partner_user_id: int, author_id: int) -> bool:
    return not partner_user_id or int(author_id) == partner_user_id


def _social_author_kind(
    *,
    author_id: int,
    is_bot: bool,
    is_self: bool,
    resident_user_ids: tuple[int, ...],
) -> str | None:
    if is_self:
        return None
    if not is_bot:
        return "human"
    if author_id in resident_user_ids:
        return "ai_resident"
    return None


@dataclass(frozen=True)
class _MessagePolicy:
    route_kind: str
    disclosure_scope: str
    trigger: str | None = None


def _message_policy(
    *,
    mode: str,
    partner_configured: bool,
    is_partner: bool,
    direct_trigger: str | None,
    private_active: bool,
) -> _MessagePolicy | None:
    if mode == "ignored":
        return None
    if mode == "private":
        if partner_configured and not is_partner:
            return None
        trigger = direct_trigger or ("active-channel" if private_active else None)
        if trigger is None:
            return None
        return _MessagePolicy("immediate", "private", trigger)
    if mode == "addressed":
        if direct_trigger is None:
            return None
        return _MessagePolicy(
            "immediate",
            "public" if partner_configured else "private",
            direct_trigger,
        )
    if mode == "social":
        return _MessagePolicy("social", "public", direct_trigger)
    return None


def social_ambient_notice(
    companion_name: str, *, attention_model: str, response_provider: str
) -> str:
    return (
        f"**{companion_name} social presence is active in this channel.** "
        f"Visible human text and messages from explicitly approved AI residents may "
        f"be examined transiently on this host by local "
        f"Ollama model `{attention_model}` to decide whether an opening is worth "
        f"bringing to {companion_name}'s attention. The ambient buffer stays in "
        "RAM and is not written "
        "to conversation history or durable memory. If the local gate chooses to "
        f"consider joining, a bounded public excerpt is sent to the configured external "
        f"`{response_provider}` response provider. Attachments, unapproved utility-bot "
        "messages, DMs, and private channels are not included in ambient processing."
    )


def build_bot(app: CompanionApp) -> discord.Client:
    intents = discord.Intents.default()
    intents.message_content = True
    intents.reactions = True

    client = discord.Client(intents=intents)
    tree = _CompanionCommandTree(client, app)
    voice_sessions = VoiceSessionManager(app.config, app=app)
    companion = app.persona.companion_name
    partner = app.persona.partner_name
    posture = getattr(app.persona, "social_posture", None)
    format_posture = getattr(posture, "format_for_attention", None)
    social_posture = format_posture() if callable(format_posture) else ""
    attention = OllamaAttentionJudge(
        OllamaAttentionConfig(
            base_url=app.config.ollama_base_url,
            model=app.config.social_attention_model,
            companion_name=companion,
            timeout_seconds=app.config.social_attention_timeout_seconds,
            threads=app.config.social_attention_threads,
            context_tokens=app.config.social_attention_context_tokens,
        )
    )
    social_presence = SocialPresence(
        companion_name=companion,
        judge=attention.judge if app.config.social_ambient_enabled else None,
        ambient_enabled=app.config.social_ambient_enabled,
        debounce_seconds=app.config.social_debounce_seconds,
        buffer_messages=app.config.social_buffer_messages,
        buffer_chars=app.config.social_buffer_chars,
        engagement_seconds=app.config.social_engagement_seconds,
        cooldown_seconds=app.config.social_cooldown_seconds,
        budget_capacity=app.config.social_budget_capacity,
        budget_refill_per_hour=app.config.social_budget_refill_per_hour,
        direct_burst=app.config.social_direct_burst,
        direct_refill_per_minute=app.config.social_direct_refill_per_minute,
        social_posture=social_posture,
        ai_chain_limit=getattr(app.config, "social_ai_chain_limit", 4),
    )
    register_commands(tree, app, voice_sessions, social_presence=social_presence)
    message_index: dict[str, dict[str, tuple[str, str]]] = defaultdict(dict)
    channel_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    @client.event
    async def on_ready() -> None:
        await tree.sync()
        print(f"{companion} v2 online as {client.user}")
        print(f"Persona: {app.persona.persona_id} ({app.persona.root})")
        print(f"Primary model: {app.primary_model}")
        print(f"Data dir: {app.config.data_dir.resolve()}")
        active = sorted(app.config.automatic_response_channel_ids)
        print(f"Active channels: {active or 'none (DM/mention/reply only)'}")
        print(f"Outreach: {'enabled' if app.outreach.enabled else 'disabled'}")
        print(
            f"Social channels: {sorted(app.config.social_channel_ids) or 'none'}; "
            f"ambient local attention: "
            f"{'enabled' if app.config.social_ambient_enabled else 'disabled'}"
        )
        print("Do not run two processes with the same Discord token.")
        if app.config.social_ambient_enabled and app.config.social_channel_ids:
            if not app.persona.documents_by_mode("public"):
                print(
                    "[social] Ambient attention remains disabled: persona has no "
                    "public document layer"
                )
            elif not await attention.ready():
                print(
                    "[social] Ambient attention remains disabled: local Ollama "
                    f"model '{app.config.social_attention_model}' is unavailable"
                )
            else:
                provider_name, _ = app.config.social_ref()
                notice = social_ambient_notice(
                    companion,
                    attention_model=app.config.social_attention_model,
                    response_provider=provider_name,
                )
                for channel_id in app.config.social_channel_ids:
                    if social_presence.notice_confirmed(channel_id):
                        continue
                    try:
                        channel = client.get_channel(channel_id)
                        if channel is None:
                            channel = await client.fetch_channel(channel_id)
                        if not hasattr(channel, "send"):
                            raise TypeError("configured social channel cannot send")
                        await channel.send(notice)  # type: ignore[union-attr]
                    except Exception as exc:
                        print(
                            f"[social] Notice failed for channel {channel_id}; "
                            f"ambient attention stays off there ({type(exc).__name__})"
                        )
                    else:
                        social_presence.confirm_notice(channel_id)
        if not getattr(client, "_outreach_task", None):
            client._outreach_task = asyncio.create_task(_outreach_loop(client, app))

    @client.event
    async def on_message(message: discord.Message) -> None:
        author_kind = _social_author_kind(
            author_id=int(message.author.id),
            is_bot=bool(message.author.bot),
            is_self=message.author == client.user,
            resident_user_ids=getattr(app.config, "social_resident_user_ids", ()),
        )
        if author_kind is None:
            return
        mentioned = client.user is not None and client.user in message.mentions
        is_dm = message.guild is None
        is_reply = (
            message.reference
            and message.reference.resolved
            and getattr(message.reference.resolved, "author", None) == client.user
        )
        direct_trigger = _response_trigger(
            mentioned=mentioned,
            is_dm=is_dm,
            in_active=False,
            is_reply=bool(is_reply),
        )
        if direct_trigger is None and clear_name_address(message.content, companion):
            direct_trigger = "name-address"

        if is_dm:
            mode = "private"
        else:
            mode = app.config.channel_mode(int(message.channel.id))
        if author_kind == "ai_resident" and mode != "social":
            return
        if author_kind == "ai_resident":
            # Resident peers use the ambient gate and its loop protection even
            # when they mention this resident directly.
            direct_trigger = None
        is_partner = bool(
            app.config.partner_user_id
            and int(message.author.id) == app.config.partner_user_id
        )
        policy = _message_policy(
            mode=mode,
            partner_configured=bool(app.config.partner_user_id),
            is_partner=is_partner,
            direct_trigger=direct_trigger,
            private_active=(
                is_dm
                or message.channel.id in app.config.automatic_response_channel_ids
            ),
        )
        if policy is None:
            return
        if (
            policy.disclosure_scope == "public"
            and direct_trigger is not None
            and not social_presence.allow_direct(message.author.id)
        ):
            try:
                await message.add_reaction("⏳")
            except Exception:
                pass
            return
        if policy.route_kind == "social":
            route = await social_presence.consider(
                SocialMessage(
                    guild_id=str(message.guild.id) if message.guild else "",
                    channel_id=str(message.channel.id),
                    channel_name=str(getattr(message.channel, "name", "?")),
                    message_id=str(message.id),
                    author_id=str(message.author.id),
                    author_name=str(message.author.display_name),
                    author_kind=author_kind,
                    content=message.content,
                ),
                direct_trigger=direct_trigger,
            )
            if route is None:
                return
        else:
            route = SocialRoute(trigger=policy.trigger or "addressed")
        disclosure_scope = policy.disclosure_scope

        text = _clean_mentions(message.content, client.user.id) if client.user else message.content
        identify_author = disclosure_scope == "public" or (
            message.channel.id not in app.config.automatic_response_channel_ids
            or (bool(app.config.partner_user_id) and not is_partner)
        )
        display = (
            f"[{'AI resident ' if author_kind == 'ai_resident' else ''}"
            f"{message.author.display_name}]: {text}"
            if identify_author and text
            else text
        )

        print(
            f"[{'DM' if is_dm else '#' + getattr(message.channel, 'name', '?')}] "
            f"{message.author.display_name}: {display or '[image]'}"
        )

        channel_key = str(message.channel.id)
        try:
            async with channel_locks[channel_key]:
                typing = None
                try:
                    typing = message.channel.typing()
                    await typing.__aenter__()
                except Exception:
                    typing = None
                try:
                    if route.discretionary:
                        parts = ()
                    else:
                        async with aiohttp.ClientSession() as session:
                            parts = await build_user_parts(
                                message, display or "", session
                            )
                    store_text = display or "[image]"
                    if is_dm:
                        surface = "dm"
                    elif isinstance(message.channel, discord.Thread):
                        surface = "thread"
                    else:
                        surface = "text"
                    provenance = TurnProvenance(
                        guild_id=str(message.guild.id) if message.guild else None,
                        channel_id=channel_key,
                        channel_name=getattr(message.channel, "name", None),
                        surface=surface,
                        author_id=str(message.author.id),
                        author_name=str(message.author.display_name),
                        author_kind=author_kind,
                        trigger=route.trigger,
                        source_id=f"discord-message:{message.id}",
                        disclosure_scope=disclosure_scope,
                    )
                    reply = await app.respond(
                        channel_key,
                        store_text,
                        parts=parts,
                        provenance=provenance,
                        ambient_context=route.ambient_context,
                        store_history=not route.discretionary,
                        discretionary_social=route.discretionary,
                    )
                finally:
                    if typing is not None:
                        try:
                            await typing.__aexit__(None, None, None)
                        except Exception:
                            pass

                reply = sanitize_outgoing(reply)
                if route.discretionary:
                    social_presence.clear_inflight(channel_key)
                    if not reply.strip():
                        return
                sent = None
                if len(reply) <= 2000:
                    sent = await message.reply(reply)
                else:
                    chunks = [reply[i:i + 1900] for i in range(0, len(reply), 1900)]
                    for index, chunk in enumerate(chunks):
                        sent = (
                            await message.reply(chunk)
                            if index == 0
                            else await message.channel.send(chunk)
                        )
                if sent:
                    if route.discretionary:
                        app.record_exchange(
                            channel_key, store_text, reply, provenance
                        )
                    message_index[channel_key][str(sent.id)] = (store_text, reply)
                    if disclosure_scope == "public":
                        social_presence.mark_response(
                            channel_key,
                            source_author_kind=route.source_author_kind,
                        )
        finally:
            if route.discretionary:
                social_presence.clear_inflight(channel_key)

    @client.event
    async def on_reaction_add(reaction: discord.Reaction, user: discord.User) -> None:
        if user == client.user:
            return
        if not _author_allowed(app.config.partner_user_id, int(user.id)):
            return
        msg = reaction.message
        if msg.author != client.user:
            return
        ckey = str(msg.channel.id)
        emoji = str(reaction.emoji)
        pair = message_index[ckey].get(str(msg.id))
        user_text, reply_text = pair if pair else ("", msg.content)

        if emoji == "📌":
            await app.pin_exchange(
                ckey,
                getattr(msg.channel, "name", "dm"),
                user_text,
                reply_text,
                user.id,
            )
            await msg.add_reaction("✅")
            return
        if emoji == "❌":
            if app.forget_exchange(ckey, reply_text):
                await msg.add_reaction("🗑️")
            else:
                await msg.add_reaction("🤷")
            return
        if emoji == "👍":
            prompt = (
                f"{partner} reacted 👍 to your last message — "
                "expand on it or go deeper."
            )
        else:
            prompt = (
                f"{partner} reacted with {emoji} to your message: \"{msg.content[:100]}\""
            )
        async with msg.channel.typing():
            reply = await app.respond(
                ckey,
                prompt,
                provenance=TurnProvenance(
                    guild_id=str(msg.guild.id) if msg.guild else None,
                    channel_id=ckey,
                    channel_name=getattr(msg.channel, "name", None),
                    surface="dm" if msg.guild is None else "text",
                    author_id=str(user.id),
                    author_name=str(getattr(user, "display_name", user.name)),
                    trigger="reaction",
                    source_id=f"discord-reaction:{msg.id}:{user.id}:{emoji}",
                ),
            )
        sent = await msg.reply(reply)
        if sent:
            message_index[ckey][str(sent.id)] = (user_text, reply)

    @client.event
    async def on_voice_state_update(
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        voice_sessions.handle_voice_state_update(member, before, after)

    client.tree = tree  # type: ignore[attr-defined]
    client.voice_sessions = voice_sessions  # type: ignore[attr-defined]
    client.social_presence = social_presence  # type: ignore[attr-defined]
    return client


async def _outreach_loop(client: discord.Client, app: CompanionApp) -> None:
    await client.wait_until_ready()
    app.outreach.record_loop_started()
    print(
        f"[reach] Outreach loop running — max {app.outreach.max_per_day}/day, "
        f"{app.outreach.min_silence_h}h silence, quiet "
        f"{app.outreach.quiet_start}:00-{app.outreach.quiet_end}:00, "
        f"every {app.config.reach_check_minutes}m"
    )
    while not client.is_closed():
        app.outreach.record_loop_tick()
        try:
            reply = await app.maybe_reach_out()
            if reply and app.config.watch_channel_id:
                channel = client.get_channel(app.config.watch_channel_id)
                if channel is None:
                    channel = await client.fetch_channel(app.config.watch_channel_id)
                if hasattr(channel, "send"):
                    await channel.send(reply)  # type: ignore[union-attr]
        except Exception as exc:
            app.outreach.record_loop_error(exc)
            print(f"[reach] Loop error: {exc}")
        await asyncio.sleep(app.config.reach_check_minutes * 60)


def run() -> None:
    env_file = os.getenv("ENV_FILE", "").strip()
    if env_file:
        if not load_dotenv(env_file, override=False):
            raise SystemExit(f"ENV_FILE could not be loaded: {env_file}")
        print(f"Runtime environment: {env_file}")
    load_dotenv(override=False)
    config = RuntimeConfig.from_env()
    configure_application_logging(config.gladia_api_key)
    if not config.discord_token:
        raise SystemExit(
            "Missing DISCORD_TOKEN.\n"
            "Set ENV_FILE to your private .env, or copy .env.example to .env "
            "and fill DISCORD_TOKEN plus a provider key "
            "(XAI_API_KEY, ANTHROPIC_API_KEY, or OPENAI_API_KEY)."
        )
    if not (config.xai_api_key or config.anthropic_api_key or config.openai_api_key):
        raise SystemExit(
            "No model provider key set. Add XAI_API_KEY (Grok), "
            "ANTHROPIC_API_KEY (Claude), or OPENAI_API_KEY."
        )
    app = CompanionApp.from_env(config)
    bot = build_bot(app)
    bot.run(config.discord_token)
