"""Cost-Aware Routing — select embedding provider based on memory category.

Different memory categories have different value. A `temp` note doesn't need
expensive cloud embeddings, while a durable `fact` warrants the best quality.

Tier mapping:
  - fact, rule → premium (Voyage 1024d / OpenAI 1536d)
  - preference, belief, procedure → standard (Google 768d / Jina 1024d)
  - session, temp → economy (Ollama 768d / sentence-transformers 384d)

If the configured provider matches the recommended tier, use it directly.
If not, route to a provider in the recommended tier (falling back gracefully).

The routing is advisory — if only one provider is available, it's used for
all categories regardless of tier. Cost-awareness kicks in when MULTIPLE
providers are available (e.g. Voyage API key + local Ollama).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ─── Tier Definitions ────────────────────────────────────────────────────────

TIER_PREMIUM = "premium"
TIER_STANDARD = "standard"
TIER_ECONOMY = "economy"

# Category → recommended tier
CATEGORY_TIERS: Dict[str, str] = {
    "fact": TIER_PREMIUM,
    "rule": TIER_PREMIUM,
    "preference": TIER_STANDARD,
    "belief": TIER_STANDARD,
    "procedure": TIER_STANDARD,
    "session": TIER_ECONOMY,
    "temp": TIER_ECONOMY,
    "entity": TIER_PREMIUM,  # entities are durable knowledge
}

# Provider → tier
PROVIDER_TIERS: Dict[str, str] = {
    "voyage": TIER_PREMIUM,
    "openai": TIER_PREMIUM,
    "google": TIER_STANDARD,
    "jina": TIER_STANDARD,
    "ollama": TIER_ECONOMY,
    "sentence-transformers": TIER_ECONOMY,
}

# Provider → estimated cost per 1M tokens (USD, approximate)
PROVIDER_COSTS: Dict[str, float] = {
    "voyage": 0.02,        # voyage-4
    "openai": 0.13,        # text-embedding-3-large
    "google": 0.025,       # text-embedding-004
    "jina": 0.018,         # jina-embeddings-v3
    "ollama": 0.0,         # local, flatrate
    "sentence-transformers": 0.0,  # local, free
}

TIER_DESCRIPTIONS: Dict[str, str] = {
    TIER_PREMIUM: "Premium cloud embeddings (Voyage/OpenAI) — best quality for durable facts and rules",
    TIER_STANDARD: "Standard cloud embeddings (Google/Jina) — balanced quality and cost for preferences and beliefs",
    TIER_ECONOMY: "Local embeddings (Ollama/sentence-transformers) — zero cost for temporary and session memories",
}


class CostAwareRouter:
    """Routes embedding requests to the appropriate provider tier.

    Usage::

        router = CostAwareRouter()
        router.initialize()

        # Get the best provider for a memory category
        provider = router.get_provider_for_category("fact")
        vector = provider.embed("some text")

        # Get routing stats
        stats = router.stats()
    """

    def __init__(self, hermes_home: str = ""):
        self._hermes_home = hermes_home
        self._available_providers: Dict[str, Any] = {}
        self._configured_provider: str = ""
        self._routing_enabled = False
        self._routing_decisions: Dict[str, int] = {}

    def initialize(self) -> None:
        """Detect available providers and read config.

        Cost-aware routing is only enabled when MULTIPLE providers are
        available (e.g. Voyage API key + local Ollama). With a single
        provider, all categories use that provider.
        """
        self._detect_available_providers()
        self._read_config()
        if len(self._available_providers) >= 2:
            self._routing_enabled = True
            logger.info(
                "CostAwareRouter: enabled (%d providers available: %s)",
                len(self._available_providers),
                ", ".join(self._available_providers.keys()),
            )
        else:
            logger.info(
                "CostAwareRouter: disabled (%d provider available, need 2+ for routing)",
                len(self._available_providers),
            )

    def _detect_available_providers(self) -> None:
        """Check which embedding providers are available without fully initializing them."""
        # Voyage
        if os.environ.get("VOYAGE_API_KEY", "").startswith(("vo-", "pa-")):
            self._available_providers["voyage"] = TIER_PREMIUM
        # OpenAI
        if os.environ.get("OPENAI_API_KEY", "").startswith("sk-"):
            self._available_providers["openai"] = TIER_PREMIUM
        # Google
        if os.environ.get("GOOGLE_API_KEY", "").startswith("AIza"):
            self._available_providers["google"] = TIER_STANDARD
        # Jina
        if os.environ.get("JINA_API_KEY", ""):
            self._available_providers["jina"] = TIER_STANDARD
        # Ollama (check if running)
        try:
            import socket
            sock = socket.create_connection(("localhost", 11434), timeout=1)
            sock.close()
            self._available_providers["ollama"] = TIER_ECONOMY
        except Exception:
            pass
        # sentence-transformers (check if installed)
        try:
            import sentence_transformers  # noqa: F401
            self._available_providers["sentence-transformers"] = TIER_ECONOMY
        except ImportError:
            pass

    def _read_config(self) -> None:
        """Read routing config from config files."""
        # Check if cost-aware routing is explicitly enabled/disabled
        config_paths = []
        if self._hermes_home:
            config_paths.append(os.path.join(self._hermes_home, "nexus", "config.json"))
        config_paths.append(os.path.expanduser("~/.nexus-memory/config.json"))

        for path in config_paths:
            try:
                if os.path.exists(path):
                    import json
                    with open(path) as f:
                        cfg = json.load(f)
                    if "cost_aware_routing" in cfg:
                        self._routing_enabled = cfg["cost_aware_routing"]
                    if "configured_embedding_provider" in cfg:
                        self._configured_provider = cfg["configured_embedding_provider"]
                    break
            except Exception:
                pass

    def get_provider_for_category(self, category: str) -> Optional[str]:
        """Get the recommended provider name for a memory category.

        Returns the provider name (e.g. "voyage", "ollama") or None if
        only one provider is available (routing disabled — use the configured one).

        Args:
            category: Memory category (fact, rule, preference, belief, session, temp, entity, procedure)

        Returns:
            Provider name string, or None if routing is disabled.
        """
        if not self._routing_enabled:
            return None

        recommended_tier = CATEGORY_TIERS.get(category, TIER_STANDARD)

        # Find providers in the recommended tier
        tier_providers = [
            name for name, tier in self._available_providers.items()
            if tier == recommended_tier
        ]

        if tier_providers:
            # Prefer the first available in the tier (priority order)
            chosen = tier_providers[0]
            self._routing_decisions[chosen] = self._routing_decisions.get(chosen, 0) + 1
            return chosen

        # No provider in the recommended tier — fall up (better quality is OK)
        if recommended_tier == TIER_ECONOMY:
            # Try standard, then premium
            for tier in [TIER_STANDARD, TIER_PREMIUM]:
                fallback = [n for n, t in self._available_providers.items() if t == tier]
                if fallback:
                    self._routing_decisions[fallback[0]] = self._routing_decisions.get(fallback[0], 0) + 1
                    return fallback[0]
        elif recommended_tier == TIER_STANDARD:
            # Try premium (fall up is OK), then economy
            for tier in [TIER_PREMIUM, TIER_ECONOMY]:
                fallback = [n for n, t in self._available_providers.items() if t == tier]
                if fallback:
                    self._routing_decisions[fallback[0]] = self._routing_decisions.get(fallback[0], 0) + 1
                    return fallback[0]

        # Shouldn't happen, but return None to use the default
        return None

    def get_tier_for_category(self, category: str) -> str:
        """Get the recommended tier for a category (advisory, no side effects)."""
        return CATEGORY_TIERS.get(category, TIER_STANDARD)

    def should_use_cloud(self, category: str) -> bool:
        """Check if a category warrants cloud (paid) embeddings.

        Economy categories (session, temp) should use local embeddings
        when available to save costs.
        """
        if not self._routing_enabled:
            return True  # Single provider: always use it
        tier = CATEGORY_TIERS.get(category, TIER_STANDARD)
        return tier != TIER_ECONOMY or not any(
            t == TIER_ECONOMY for t in self._available_providers.values()
        )

    def estimate_cost(self, text: str, category: str) -> float:
        """Estimate the embedding cost for a text in a given category.

        Returns estimated cost in USD (0.0 for local providers).
        """
        provider = self.get_provider_for_category(category)
        if not provider:
            return 0.0
        cost_per_m = PROVIDER_COSTS.get(provider, 0.0)
        # Rough token estimate: ~4 chars per token
        tokens = len(text) / 4
        return (tokens / 1_000_000) * cost_per_m

    def stats(self) -> Dict[str, Any]:
        """Return routing statistics."""
        return {
            "routing_enabled": self._routing_enabled,
            "available_providers": dict(self._available_providers),
            "routing_decisions": dict(self._routing_decisions),
            "configured_provider": self._configured_provider,
            "tier_config": {
                "premium": [c for c, t in CATEGORY_TIERS.items() if t == TIER_PREMIUM],
                "standard": [c for c, t in CATEGORY_TIERS.items() if t == TIER_STANDARD],
                "economy": [c for c, t in CATEGORY_TIERS.items() if t == TIER_ECONOMY],
            },
        }

    def explain(self, category: str) -> str:
        """Human-readable explanation of the routing decision for a category."""
        tier = self.get_tier_for_category(category)
        provider = self.get_provider_for_category(category)
        desc = TIER_DESCRIPTIONS.get(tier, "")
        if provider:
            return f"Category '{category}' → tier '{tier}' → provider '{provider}'. {desc}"
        return f"Category '{category}' → tier '{tier}' (routing disabled, using default provider). {desc}"