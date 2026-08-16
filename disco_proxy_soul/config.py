"""Runtime configuration for the standalone v2 host."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


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
    journal_threshold: float
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
    voice_min_speech_ms: int
    voice_turn_debounce_seconds: float

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
        return cls(
            discord_token=os.getenv("DISCORD_TOKEN", "").strip(),
            data_dir=Path(os.getenv("DATA_DIR", "data")),
            persona_id=persona_id,
            persona_dir=persona_dir,
            watch_channel_id=int(os.getenv("WATCH_CHANNEL_ID", "0") or 0),
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
            journal_threshold=float(os.getenv("JOURNAL_THRESHOLD", "0.7")),
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
        )

    def require_discord(self) -> None:
        if not self.discord_token:
            raise RuntimeError("Missing DISCORD_TOKEN")

    def primary_ref(self) -> tuple[str, str]:
        return parse_model_ref(self.primary_model, self.default_provider)

    def cheap_ref(self) -> tuple[str, str]:
        return parse_model_ref(self.cheap_model, self.default_provider)
