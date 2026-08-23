"""Runtime configuration for the standalone v2 host."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _moments_threshold() -> float:
    raw = os.getenv("MOMENTS_THRESHOLD")
    if raw is None or raw.strip() == "":
        raw = os.getenv("JOURNAL_THRESHOLD", "0.7")
    return float(raw)


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _bounded_float(
    name: str,
    raw: str | None,
    default: float,
    low: float,
    high: float,
) -> float:
    try:
        value = float(raw) if raw not in (None, "") else default
    except ValueError:
        raise ValueError(f"{name} must be a number") from None
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low:g} and {high:g}")
    return value


def _bounded_int(
    name: str,
    raw: str | None,
    default: int,
    low: int,
    high: int,
) -> int:
    try:
        value = int(raw) if raw not in (None, "") else default
    except ValueError:
        raise ValueError(f"{name} must be a whole number") from None
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return value


def _discord_ids(name: str, raw: str | None) -> tuple[int, ...]:
    values: list[int] = []
    for item in (raw or "").split(","):
        text = item.strip()
        if not text:
            continue
        try:
            value = int(text)
        except ValueError:
            raise ValueError(f"{name} must contain comma-separated Discord IDs") from None
        if value <= 0:
            raise ValueError(f"{name} must contain positive Discord IDs")
        if value not in values:
            values.append(value)
    return tuple(values)


def _validate_channel_modes(groups: dict[str, tuple[int, ...]]) -> None:
    owners: dict[int, str] = {}
    for name, channel_ids in groups.items():
        for channel_id in channel_ids:
            prior = owners.get(channel_id)
            if prior is not None:
                raise ValueError(
                    f"Discord channel {channel_id} appears in both {prior} and {name}"
                )
            owners[channel_id] = name


def parse_model_ref(raw: str, default_provider: str) -> tuple[str, str]:
    """Split 'provider:model' or bare 'model' into (provider, model)."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("Model reference is empty")
    if ":" in text:
        provider, model = text.split(":", 1)
        provider = provider.strip()
        model = model.strip()
        if provider and model:
            return provider, model
    return default_provider, text


@dataclass(frozen=True)
class RuntimeConfig:
    discord_token: str
    data_dir: Path
    persona_id: str
    persona_dir: Path
    watch_channel_id: int
    timezone: str
    default_provider: str
    primary_model: str
    cheap_model: str
    xai_api_key: str
    openai_api_key: str
    openai_base_url: str
    anthropic_api_key: str
    max_recent: int
    compress_chunk: int
    max_recalled: int
    recall_prefilter_limit: int
    recall_silence_min: int
    moments_threshold: float
    reach_enabled: bool
    reach_max_per_day: int
    reach_min_silence_h: float
    reach_check_minutes: int
    reach_no_cooldown_h: float
    reach_sleep_cooldown_h: float
    reach_quiet_start: int
    reach_quiet_end: int
    gladia_api_key: str
    voice_enabled: bool
    voice_endpointing_seconds: float
    voice_queue_seconds: float
    voice_gladia_stop_seconds: float
    voice_gladia_reconnect_attempts: int
    voice_gladia_reconnect_initial_delay_seconds: float
    voice_gladia_reconnect_max_delay_seconds: float
    voice_gladia_reconnect_connect_timeout_seconds: float
    voice_gladia_rotate_seconds: float
    voice_min_speech_ms: int
    voice_turn_debounce_seconds: float
    voice_tts_enabled: bool
    elevenlabs_api_key: str
    elevenlabs_voice_id: str
    elevenlabs_model_id: str
    elevenlabs_stability: float
    elevenlabs_similarity_boost: float
    elevenlabs_style: float
    elevenlabs_speaker_boost: bool
    elevenlabs_speed: float
    voice_playback_queue_seconds: float
    voice_barge_in_enabled: bool
    voice_barge_in_min_speech_ms: int
    active_channel_ids: tuple[int, ...] = ()
    partner_user_id: int = 0
    cross_surface_recent_messages: int = 12
    cross_surface_recent_chars: int = 4000
    cross_surface_recent_minutes: int = 120
    social_model: str = ""
    social_channel_ids: tuple[int, ...] = ()
    social_resident_user_ids: tuple[int, ...] = ()
    addressed_channel_ids: tuple[int, ...] = ()
    ignored_channel_ids: tuple[int, ...] = ()
    social_debounce_seconds: float = 3.0
    social_buffer_messages: int = 12
    social_buffer_chars: int = 4000
    social_engagement_seconds: float = 120.0
    social_cooldown_seconds: float = 30.0
    social_budget_capacity: float = 6.0
    social_budget_refill_per_hour: float = 2.0
    social_history_messages: int = 24
    social_response_max_tokens: int = 600
    social_ambient_enabled: bool = False
    social_attention_model: str = "qwen3:4b"
    social_attention_timeout_seconds: float = 30.0
    social_attention_threads: int = 4
    social_attention_context_tokens: int = 2048
    social_attention_keep_alive: str = "-1"
    ollama_base_url: str = "http://127.0.0.1:11434"
    social_direct_burst: int = 3
    social_direct_refill_per_minute: float = 2.0
    social_ai_chain_limit: int = 4

    @property
    def automatic_response_channel_ids(self) -> frozenset[int]:
        values = set(self.active_channel_ids)
        if self.watch_channel_id:
            values.add(self.watch_channel_id)
        return frozenset(values)

    def continuity_id_for_user(self, user_id: int | str | None) -> str | None:
        if not self.partner_user_id or user_id in (None, ""):
            return None
        try:
            candidate = int(user_id)
        except (TypeError, ValueError):
            return None
        if candidate != self.partner_user_id:
            return None
        return f"discord-user:{candidate}"

    def channel_mode(self, channel_id: int) -> str:
        if channel_id in self.ignored_channel_ids:
            return "ignored"
        if channel_id in self.social_channel_ids:
            return "social"
        if channel_id in self.automatic_response_channel_ids:
            return "private"
        return "addressed"

    @classmethod
    def from_env(cls) -> "RuntimeConfig":
        persona_id = os.getenv("PERSONA_ID", "example")
        persona_dir = Path(os.getenv("PERSONA_DIR", f"personas/{persona_id}"))
        has_xai = bool(os.getenv("XAI_API_KEY", "").strip())
        has_anthropic = bool(os.getenv("ANTHROPIC_API_KEY", "").strip())
        has_openai = bool(os.getenv("OPENAI_API_KEY", "").strip())
        if os.getenv("MODEL_PROVIDER"):
            default_provider = os.getenv("MODEL_PROVIDER", "xai")
        elif has_xai:
            default_provider = "xai"
        elif has_anthropic:
            default_provider = "anthropic"
        elif has_openai:
            default_provider = "openai"
        else:
            default_provider = "xai"
        if default_provider == "anthropic":
            primary_default = "claude-opus-4-6"
            cheap_default = "claude-haiku-4-5"
        elif default_provider == "openai":
            primary_default = "gpt-4.1"
            cheap_default = "gpt-4.1-mini"
        else:
            primary_default = "grok-4.6"
            cheap_default = "grok-4.3"
        active_channel_ids = _discord_ids(
            "ACTIVE_CHANNEL_IDS", os.getenv("ACTIVE_CHANNEL_IDS")
        )
        social_channel_ids = _discord_ids(
            "SOCIAL_CHANNEL_IDS", os.getenv("SOCIAL_CHANNEL_IDS")
        )
        social_resident_user_ids = _discord_ids(
            "SOCIAL_RESIDENT_USER_IDS", os.getenv("SOCIAL_RESIDENT_USER_IDS")
        )
        addressed_channel_ids = _discord_ids(
            "ADDRESSED_CHANNEL_IDS", os.getenv("ADDRESSED_CHANNEL_IDS")
        )
        ignored_channel_ids = _discord_ids(
            "IGNORED_CHANNEL_IDS", os.getenv("IGNORED_CHANNEL_IDS")
        )
        watch_channel_id = int(os.getenv("WATCH_CHANNEL_ID", "0") or 0)
        private_channel_ids = tuple(
            dict.fromkeys(
                ([watch_channel_id] if watch_channel_id else [])
                + list(active_channel_ids)
            )
        )
        _validate_channel_modes(
            {
                "private channels": private_channel_ids,
                "SOCIAL_CHANNEL_IDS": social_channel_ids,
                "ADDRESSED_CHANNEL_IDS": addressed_channel_ids,
                "IGNORED_CHANNEL_IDS": ignored_channel_ids,
            }
        )
        return cls(
            discord_token=os.getenv("DISCORD_TOKEN", "").strip(),
            data_dir=Path(os.getenv("DATA_DIR", "data")),
            persona_id=persona_id,
            persona_dir=persona_dir,
            watch_channel_id=watch_channel_id,
            timezone=os.getenv("COMPANION_TZ") or os.getenv("LILA_TZ") or "America/Chicago",
            default_provider=default_provider,
            primary_model=os.getenv("MODEL_PRIMARY", primary_default),
            cheap_model=os.getenv("MODEL_CHEAP", cheap_default),
            xai_api_key=os.getenv("XAI_API_KEY", "").strip(),
            openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
            openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip(),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
            max_recent=int(os.getenv("MAX_RECENT", "60")),
            compress_chunk=int(os.getenv("COMPRESS_CHUNK", "10")),
            max_recalled=int(os.getenv("MAX_RECALLED", "5")),
            recall_prefilter_limit=int(os.getenv("RECALL_PREFILTER_LIMIT", "20")),
            recall_silence_min=int(os.getenv("RECALL_SILENCE_MIN", "30")),
            moments_threshold=_moments_threshold(),
            reach_enabled=_as_bool(os.getenv("REACH_ENABLED"), True),
            reach_max_per_day=int(os.getenv("REACH_MAX_PER_DAY", "2")),
            reach_min_silence_h=float(os.getenv("REACH_MIN_SILENCE_HOURS", "2")),
            reach_check_minutes=int(os.getenv("REACH_CHECK_MINUTES", "30")),
            reach_no_cooldown_h=float(os.getenv("REACH_NO_COOLDOWN_HOURS", "2")),
            reach_sleep_cooldown_h=float(os.getenv("REACH_SLEEP_COOLDOWN_HOURS", "2")),
            reach_quiet_start=int(os.getenv("REACH_QUIET_START", "22")),
            reach_quiet_end=int(os.getenv("REACH_QUIET_END", "5")),
            gladia_api_key=os.getenv("GLADIA_API_KEY", "").strip(),
            voice_enabled=_as_bool(os.getenv("VOICE_ENABLED"), False),
            voice_endpointing_seconds=_bounded_float(
                "VOICE_ENDPOINTING_SECONDS",
                os.getenv("VOICE_ENDPOINTING_SECONDS"),
                0.1,
                0.05,
                10.0,
            ),
            voice_queue_seconds=_bounded_float(
                "VOICE_QUEUE_SECONDS",
                os.getenv("VOICE_QUEUE_SECONDS"),
                2.0,
                0.2,
                30.0,
            ),
            voice_gladia_stop_seconds=_bounded_float(
                "VOICE_GLADIA_STOP_SECONDS",
                os.getenv("VOICE_GLADIA_STOP_SECONDS"),
                15.0,
                1.0,
                120.0,
            ),
            voice_gladia_reconnect_attempts=_bounded_int(
                "VOICE_GLADIA_RECONNECT_ATTEMPTS",
                os.getenv("VOICE_GLADIA_RECONNECT_ATTEMPTS"),
                3,
                0,
                10,
            ),
            voice_gladia_reconnect_initial_delay_seconds=_bounded_float(
                "VOICE_GLADIA_RECONNECT_INITIAL_DELAY_SECONDS",
                os.getenv("VOICE_GLADIA_RECONNECT_INITIAL_DELAY_SECONDS"),
                0.5,
                0.05,
                30.0,
            ),
            voice_gladia_reconnect_max_delay_seconds=_bounded_float(
                "VOICE_GLADIA_RECONNECT_MAX_DELAY_SECONDS",
                os.getenv("VOICE_GLADIA_RECONNECT_MAX_DELAY_SECONDS"),
                5.0,
                0.05,
                60.0,
            ),
            voice_gladia_reconnect_connect_timeout_seconds=_bounded_float(
                "VOICE_GLADIA_RECONNECT_CONNECT_TIMEOUT_SECONDS",
                os.getenv("VOICE_GLADIA_RECONNECT_CONNECT_TIMEOUT_SECONDS"),
                10.0,
                0.5,
                60.0,
            ),
            voice_gladia_rotate_seconds=_bounded_float(
                "VOICE_GLADIA_ROTATE_SECONDS",
                os.getenv("VOICE_GLADIA_ROTATE_SECONDS"),
                10_200.0,
                60.0,
                10_740.0,
            ),
            voice_min_speech_ms=_bounded_int(
                "VOICE_MIN_SPEECH_MS",
                os.getenv("VOICE_MIN_SPEECH_MS"),
                120,
                0,
                10_000,
            ),
            voice_turn_debounce_seconds=_bounded_float(
                "VOICE_TURN_DEBOUNCE_SECONDS",
                os.getenv("VOICE_TURN_DEBOUNCE_SECONDS"),
                1.5,
                0.1,
                5.0,
            ),
            voice_tts_enabled=_as_bool(os.getenv("VOICE_TTS_ENABLED"), False),
            elevenlabs_api_key=os.getenv("ELEVENLABS_API_KEY", "").strip(),
            elevenlabs_voice_id=os.getenv("ELEVENLABS_VOICE_ID", "").strip(),
            elevenlabs_model_id=os.getenv(
                "ELEVENLABS_MODEL_ID", "eleven_flash_v2_5"
            ).strip(),
            elevenlabs_stability=_bounded_float(
                "ELEVENLABS_STABILITY",
                os.getenv("ELEVENLABS_STABILITY"),
                0.5,
                0.0,
                1.0,
            ),
            elevenlabs_similarity_boost=_bounded_float(
                "ELEVENLABS_SIMILARITY_BOOST",
                os.getenv("ELEVENLABS_SIMILARITY_BOOST"),
                0.75,
                0.0,
                1.0,
            ),
            elevenlabs_style=_bounded_float(
                "ELEVENLABS_STYLE",
                os.getenv("ELEVENLABS_STYLE"),
                0.0,
                0.0,
                1.0,
            ),
            elevenlabs_speaker_boost=_as_bool(
                os.getenv("ELEVENLABS_SPEAKER_BOOST"), False
            ),
            elevenlabs_speed=_bounded_float(
                "ELEVENLABS_SPEED",
                os.getenv("ELEVENLABS_SPEED"),
                1.0,
                0.7,
                1.2,
            ),
            voice_playback_queue_seconds=_bounded_float(
                "VOICE_PLAYBACK_QUEUE_SECONDS",
                os.getenv("VOICE_PLAYBACK_QUEUE_SECONDS"),
                2.0,
                0.2,
                10.0,
            ),
            voice_barge_in_enabled=_as_bool(
                os.getenv("VOICE_BARGE_IN_ENABLED"), False
            ),
            voice_barge_in_min_speech_ms=_bounded_int(
                "VOICE_BARGE_IN_MIN_SPEECH_MS",
                os.getenv("VOICE_BARGE_IN_MIN_SPEECH_MS"),
                160,
                40,
                2_000,
            ),
            active_channel_ids=active_channel_ids,
            partner_user_id=_bounded_int(
                "PARTNER_USER_ID",
                os.getenv("PARTNER_USER_ID"),
                0,
                0,
                2**63 - 1,
            ),
            cross_surface_recent_messages=_bounded_int(
                "CROSS_SURFACE_RECENT_MESSAGES",
                os.getenv("CROSS_SURFACE_RECENT_MESSAGES"),
                12,
                0,
                50,
            ),
            cross_surface_recent_chars=_bounded_int(
                "CROSS_SURFACE_RECENT_CHARS",
                os.getenv("CROSS_SURFACE_RECENT_CHARS"),
                4_000,
                0,
                20_000,
            ),
            cross_surface_recent_minutes=_bounded_int(
                "CROSS_SURFACE_RECENT_MINUTES",
                os.getenv("CROSS_SURFACE_RECENT_MINUTES"),
                120,
                1,
                1440,
            ),
            social_model=(os.getenv("MODEL_SOCIAL") or primary_default).strip(),
            social_channel_ids=social_channel_ids,
            social_resident_user_ids=social_resident_user_ids,
            addressed_channel_ids=addressed_channel_ids,
            ignored_channel_ids=ignored_channel_ids,
            social_debounce_seconds=_bounded_float(
                "SOCIAL_DEBOUNCE_SECONDS",
                os.getenv("SOCIAL_DEBOUNCE_SECONDS"),
                3.0,
                0.1,
                30.0,
            ),
            social_buffer_messages=_bounded_int(
                "SOCIAL_BUFFER_MESSAGES",
                os.getenv("SOCIAL_BUFFER_MESSAGES"),
                12,
                2,
                50,
            ),
            social_buffer_chars=_bounded_int(
                "SOCIAL_BUFFER_CHARS",
                os.getenv("SOCIAL_BUFFER_CHARS"),
                4_000,
                200,
                20_000,
            ),
            social_engagement_seconds=_bounded_float(
                "SOCIAL_ENGAGEMENT_SECONDS",
                os.getenv("SOCIAL_ENGAGEMENT_SECONDS"),
                120.0,
                5.0,
                1800.0,
            ),
            social_cooldown_seconds=_bounded_float(
                "SOCIAL_COOLDOWN_SECONDS",
                os.getenv("SOCIAL_COOLDOWN_SECONDS"),
                30.0,
                1.0,
                3600.0,
            ),
            social_budget_capacity=_bounded_float(
                "SOCIAL_BUDGET_CAPACITY",
                os.getenv("SOCIAL_BUDGET_CAPACITY"),
                6.0,
                1.0,
                100.0,
            ),
            social_budget_refill_per_hour=_bounded_float(
                "SOCIAL_BUDGET_REFILL_PER_HOUR",
                os.getenv("SOCIAL_BUDGET_REFILL_PER_HOUR"),
                2.0,
                0.1,
                100.0,
            ),
            social_history_messages=_bounded_int(
                "SOCIAL_HISTORY_MESSAGES",
                os.getenv("SOCIAL_HISTORY_MESSAGES"),
                24,
                2,
                100,
            ),
            social_response_max_tokens=_bounded_int(
                "SOCIAL_RESPONSE_MAX_TOKENS",
                os.getenv("SOCIAL_RESPONSE_MAX_TOKENS"),
                600,
                64,
                4000,
            ),
            social_ambient_enabled=_as_bool(
                os.getenv("SOCIAL_AMBIENT_ENABLED"), False
            ),
            social_attention_model=os.getenv(
                "SOCIAL_ATTENTION_MODEL", "qwen3:4b"
            ).strip(),
            social_attention_timeout_seconds=_bounded_float(
                "SOCIAL_ATTENTION_TIMEOUT_SECONDS",
                os.getenv("SOCIAL_ATTENTION_TIMEOUT_SECONDS"),
                30.0,
                1.0,
                120.0,
            ),
            social_attention_threads=_bounded_int(
                "SOCIAL_ATTENTION_THREADS",
                os.getenv("SOCIAL_ATTENTION_THREADS"),
                4,
                1,
                32,
            ),
            social_attention_context_tokens=_bounded_int(
                "SOCIAL_ATTENTION_CONTEXT_TOKENS",
                os.getenv("SOCIAL_ATTENTION_CONTEXT_TOKENS"),
                2048,
                512,
                8192,
            ),
            social_attention_keep_alive=os.getenv(
                "SOCIAL_ATTENTION_KEEP_ALIVE", "-1"
            ).strip(),
            ollama_base_url=os.getenv(
                "OLLAMA_BASE_URL", "http://127.0.0.1:11434"
            ).strip(),
            social_direct_burst=_bounded_int(
                "SOCIAL_DIRECT_BURST",
                os.getenv("SOCIAL_DIRECT_BURST"),
                3,
                1,
                20,
            ),
            social_direct_refill_per_minute=_bounded_float(
                "SOCIAL_DIRECT_REFILL_PER_MINUTE",
                os.getenv("SOCIAL_DIRECT_REFILL_PER_MINUTE"),
                2.0,
                0.1,
                60.0,
            ),
            social_ai_chain_limit=_bounded_int(
                "SOCIAL_AI_CHAIN_LIMIT",
                os.getenv("SOCIAL_AI_CHAIN_LIMIT"),
                4,
                2,
                20,
            ),
        )

    def require_discord(self) -> None:
        if not self.discord_token:
            raise RuntimeError("Missing DISCORD_TOKEN")

    def primary_ref(self) -> tuple[str, str]:
        return parse_model_ref(self.primary_model, self.default_provider)

    def cheap_ref(self) -> tuple[str, str]:
        return parse_model_ref(self.cheap_model, self.default_provider)

    def social_ref(self) -> tuple[str, str]:
        return parse_model_ref(
            self.social_model or self.cheap_model, self.default_provider
        )
