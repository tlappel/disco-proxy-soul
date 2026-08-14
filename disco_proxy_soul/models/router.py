"""Simple tiered model router scaffold."""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .contracts import ModelProvider, ModelRequest, ModelResponse, ModelTier


@dataclass
class TierRoute:
    provider: ModelProvider
    model: str | None = None


@dataclass
class TieredModelRouter:
    """Route model requests by purpose/tier.

    Provider implementations are intentionally separate so Anthropic, OpenAI,
    local model servers, or resident shared model services can be swapped in.
    """

    routes: dict[ModelTier, TierRoute] = field(default_factory=dict)

    def add_route(self, tier: ModelTier, provider: ModelProvider, model: str | None = None) -> None:
        self.routes[tier] = TierRoute(provider=provider, model=model)

    async def complete(self, tier: ModelTier, request: ModelRequest) -> ModelResponse:
        route = self.routes.get(tier)
        if route is None:
            raise KeyError(f"No model route configured for tier: {tier}")
        routed_request = replace(request, model=request.model or route.model)
        return await route.provider.complete(routed_request)
