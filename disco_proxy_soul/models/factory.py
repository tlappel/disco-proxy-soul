"""Build a tiered router from runtime config and available API keys."""

from __future__ import annotations

from .anthropic import AnthropicProvider
from .contracts import ModelProvider, ModelRouter
from .openai_compatible import OpenAICompatibleProvider
from .router import TieredModelRouter
from ..config import RuntimeConfig

XAI_BASE_URL = "https://api.x.ai/v1"


class ProviderConfigError(RuntimeError):
    """Raised when a configured provider has no credentials."""


def providers_from_config(config: RuntimeConfig) -> dict[str, ModelProvider]:
    providers: dict[str, ModelProvider] = {}
    if config.xai_api_key:
        providers["xai"] = OpenAICompatibleProvider(
            "xai", config.xai_api_key, XAI_BASE_URL
        )
    if config.openai_api_key:
        providers["openai"] = OpenAICompatibleProvider(
            "openai", config.openai_api_key, config.openai_base_url
        )
    if config.anthropic_api_key:
        providers["anthropic"] = AnthropicProvider(config.anthropic_api_key)
    return providers


def build_router(config: RuntimeConfig) -> ModelRouter:
    providers = providers_from_config(config)
    if not providers:
        raise ProviderConfigError(
            "No model provider configured. Set XAI_API_KEY, ANTHROPIC_API_KEY, or OPENAI_API_KEY."
        )

    router = TieredModelRouter()
    for tier, (provider_name, model) in (
        ("primary", config.primary_ref()),
        ("cheap", config.cheap_ref()),
        ("social", config.social_ref()),
        ("medium", config.cheap_ref()),
    ):
        provider = providers.get(provider_name)
        if provider is None:
            available = ", ".join(sorted(providers)) or "(none)"
            raise ProviderConfigError(
                f"Provider '{provider_name}' is not configured for {tier}. "
                f"Available: {available}"
            )
        router.add_route(tier, provider, model)
    return router


def catalog_for(config: RuntimeConfig) -> dict[str, str]:
    """Display name -> model id for /model. Only lists reachable providers."""
    catalog: dict[str, str] = {}
    if config.xai_api_key:
        catalog.update({
            "Grok 4.6": "xai:grok-4.6",
            "Grok 4.5": "xai:grok-4.5",
            "Grok 4.3": "xai:grok-4.3",
        })
    if config.anthropic_api_key:
        catalog.update({
            "Opus 4.6": "anthropic:claude-opus-4-6",
            "Opus 4.8": "anthropic:claude-opus-4-8",
            "Sonnet 4.6": "anthropic:claude-sonnet-4-6",
            "Haiku 4.5": "anthropic:claude-haiku-4-5",
        })
    if config.openai_api_key:
        catalog.update({
            "GPT-4.1": "openai:gpt-4.1",
            "GPT-4.1 mini": "openai:gpt-4.1-mini",
        })
    provider, model = config.primary_ref()
    label = f"{provider}:{model}"
    if label not in catalog.values():
        catalog[label] = label
    return catalog
