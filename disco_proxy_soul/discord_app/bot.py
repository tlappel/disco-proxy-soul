"""Discord client for the companion host."""

from __future__ import annotations

import asyncio
import logging
import os
from collections import defaultdict

import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv

from ..app import CompanionApp
from ..adapters.gladia_live import redact_sensitive_text
from ..config import RuntimeConfig
from ..safety import sanitize_outgoing
from .attachments import build_user_parts
from .commands import register_commands
from .voice_session import VoiceSessionManager


APPLICATION_LOGGER_NAME = "disco_proxy_soul"


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


def build_bot(app: CompanionApp) -> discord.Client:
    intents = discord.Intents.default()
    intents.message_content = True
    intents.reactions = True

    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)
    voice_sessions = VoiceSessionManager(app.config, app=app)
    register_commands(tree, app, voice_sessions)
    companion = app.persona.companion_name
    partner = app.persona.partner_name
    message_index: dict[str, dict[str, tuple[str, str]]] = defaultdict(dict)

    @client.event
    async def on_ready() -> None:
        await tree.sync()
        print(f"{companion} v2 online as {client.user}")
        print(f"Persona: {app.persona.persona_id} ({app.persona.root})")
        print(f"Primary model: {app.primary_model}")
        print(f"Data dir: {app.config.data_dir.resolve()}")
        print(f"Watch channel: {app.config.watch_channel_id or 'not set (DM/mention/reply only)'}")
        print(f"Outreach: {'enabled' if app.outreach.enabled else 'disabled'}")
        print("Do not run two processes with the same Discord token.")
        if not getattr(client, "_outreach_task", None):
            client._outreach_task = asyncio.create_task(_outreach_loop(client, app))

    @client.event
    async def on_message(message: discord.Message) -> None:
        if message.author == client.user or message.author.bot:
            return
        mentioned = client.user is not None and client.user in message.mentions
        is_dm = message.guild is None
        in_watch = (
            app.config.watch_channel_id
            and message.channel.id == app.config.watch_channel_id
        )
        is_reply = (
            message.reference
            and message.reference.resolved
            and getattr(message.reference.resolved, "author", None) == client.user
        )
        if not any([mentioned, is_dm, in_watch, is_reply]):
            return

        text = _clean_mentions(message.content, client.user.id) if client.user else message.content
        display = f"[{message.author.display_name}]: {text}" if (not in_watch and text) else text

        print(
            f"[{'DM' if is_dm else '#' + getattr(message.channel, 'name', '?')}] "
            f"{message.author.display_name}: {display or '[image]'}"
        )

        typing = None
        try:
            typing = message.channel.typing()
            await typing.__aenter__()
        except Exception:
            typing = None
        try:
            async with aiohttp.ClientSession() as session:
                parts = await build_user_parts(message, display or "", session)
            store_text = display or "[image]"
            reply = await app.respond(str(message.channel.id), store_text, parts=parts)
        finally:
            if typing is not None:
                try:
                    await typing.__aexit__(None, None, None)
                except Exception:
                    pass

        reply = sanitize_outgoing(reply)
        sent = None
        if len(reply) <= 2000:
            sent = await message.reply(reply)
        else:
            chunks = [reply[i:i + 1900] for i in range(0, len(reply), 1900)]
            for index, chunk in enumerate(chunks):
                sent = await message.reply(chunk) if index == 0 else await message.channel.send(chunk)
        if sent:
            message_index[str(message.channel.id)][str(sent.id)] = (store_text, reply)

    @client.event
    async def on_reaction_add(reaction: discord.Reaction, user: discord.User) -> None:
        if user == client.user:
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
                ckey, getattr(msg.channel, "name", "dm"), user_text, reply_text
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
            reply = await app.respond(ckey, prompt)
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
        if not load_dotenv(env_file):
            raise SystemExit(f"ENV_FILE could not be loaded: {env_file}")
        print(f"Runtime environment: {env_file}")
    else:
        load_dotenv()
    config = RuntimeConfig.from_env()
    configure_application_logging(config.gladia_api_key)
    if not config.discord_token:
        raise SystemExit(
            "Missing DISCORD_TOKEN.\n"
            "Copy .env.v2.example to .env and fill DISCORD_TOKEN plus a provider key "
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
