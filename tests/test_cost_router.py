"""Tests for the Cost-Aware Routing module."""

import pytest
import os
from unittest.mock import patch, MagicMock
from nexus_memory.cost_router import (
    CostAwareRouter,
    CATEGORY_TIERS,
    PROVIDER_TIERS,
    PROVIDER_COSTS,
    TIER_PREMIUM,
    TIER_STANDARD,
    TIER_ECONOMY,
)


class TestTierDefinitions:
    """Test that tier mappings are correctly defined."""

    def test_fact_is_premium(self):
        assert CATEGORY_TIERS["fact"] == TIER_PREMIUM

    def test_rule_is_premium(self):
        assert CATEGORY_TIERS["rule"] == TIER_PREMIUM

    def test_session_is_economy(self):
        assert CATEGORY_TIERS["session"] == TIER_ECONOMY

    def test_temp_is_economy(self):
        assert CATEGORY_TIERS["temp"] == TIER_ECONOMY

    def test_preference_is_standard(self):
        assert CATEGORY_TIERS["preference"] == TIER_STANDARD

    def test_belief_is_standard(self):
        assert CATEGORY_TIERS["belief"] == TIER_STANDARD

    def test_entity_is_premium(self):
        assert CATEGORY_TIERS["entity"] == TIER_PREMIUM

    def test_procedure_is_standard(self):
        assert CATEGORY_TIERS["procedure"] == TIER_STANDARD

    def test_voyage_is_premium(self):
        assert PROVIDER_TIERS["voyage"] == TIER_PREMIUM

    def test_ollama_is_economy(self):
        assert PROVIDER_TIERS["ollama"] == TIER_ECONOMY

    def test_sentence_transformers_is_economy(self):
        assert PROVIDER_TIERS["sentence-transformers"] == TIER_ECONOMY

    def test_local_providers_have_zero_cost(self):
        assert PROVIDER_COSTS["ollama"] == 0.0
        assert PROVIDER_COSTS["sentence-transformers"] == 0.0

    def test_cloud_providers_have_nonzero_cost(self):
        assert PROVIDER_COSTS["voyage"] > 0
        assert PROVIDER_COSTS["openai"] > 0


class TestCostAwareRouterDisabled:
    """Tests for when routing is disabled (single provider)."""

    def test_routing_disabled_with_no_providers(self):
        router = CostAwareRouter()
        router._available_providers = {}
        router._routing_enabled = False
        assert router.get_provider_for_category("fact") is None

    def test_routing_disabled_with_one_provider(self):
        router = CostAwareRouter()
        router._available_providers = {"ollama": TIER_ECONOMY}
        router._routing_enabled = False
        # Single provider: routing disabled, return None (use default)
        assert router.get_provider_for_category("fact") is None

    def test_should_use_cloud_when_disabled(self):
        router = CostAwareRouter()
        router._routing_enabled = False
        # When disabled, always returns True (use whatever is configured)
        assert router.should_use_cloud("session") is True
        assert router.should_use_cloud("fact") is True


class TestCostAwareRouterEnabled:
    """Tests for when routing is enabled (2+ providers)."""

    def test_routing_enabled_with_two_providers(self):
        router = CostAwareRouter()
        router._available_providers = {
            "voyage": TIER_PREMIUM,
            "ollama": TIER_ECONOMY,
        }
        router._routing_enabled = True

        # Fact → premium tier → voyage
        provider = router.get_provider_for_category("fact")
        assert provider == "voyage"

        # Temp → economy tier → ollama
        provider = router.get_provider_for_category("temp")
        assert provider == "ollama"

    def test_routing_falls_up_when_no_economy_provider(self):
        router = CostAwareRouter()
        router._available_providers = {
            "voyage": TIER_PREMIUM,
            "google": TIER_STANDARD,
        }
        router._routing_enabled = True

        # Session → economy tier, but no economy provider → fall up to standard
        provider = router.get_provider_for_category("session")
        assert provider == "google"

    def test_routing_falls_up_to_premium(self):
        router = CostAwareRouter()
        router._available_providers = {"voyage": TIER_PREMIUM}
        router._routing_enabled = True

        # Temp → economy, no economy or standard → fall up to premium
        provider = router.get_provider_for_category("temp")
        assert provider == "voyage"

    def test_routing_standard_uses_standard_tier(self):
        router = CostAwareRouter()
        router._available_providers = {
            "voyage": TIER_PREMIUM,
            "google": TIER_STANDARD,
            "ollama": TIER_ECONOMY,
        }
        router._routing_enabled = True

        # Preference → standard tier → google
        provider = router.get_provider_for_category("preference")
        assert provider == "google"

    def test_routing_unknown_category_defaults_to_standard(self):
        router = CostAwareRouter()
        router._available_providers = {
            "voyage": TIER_PREMIUM,
            "google": TIER_STANDARD,
        }
        router._routing_enabled = True

        # Unknown category → default tier is standard
        provider = router.get_provider_for_category("unknown_cat")
        assert provider == "google"

    def test_routing_decisions_tracked(self):
        router = CostAwareRouter()
        router._available_providers = {
            "voyage": TIER_PREMIUM,
            "ollama": TIER_ECONOMY,
        }
        router._routing_enabled = True
        router._routing_decisions = {}

        router.get_provider_for_category("fact")
        router.get_provider_for_category("fact")
        router.get_provider_for_category("temp")

        assert router._routing_decisions.get("voyage") == 2
        assert router._routing_decisions.get("ollama") == 1


class TestShouldUseCloud:
    """Tests for should_use_cloud() method."""

    def test_economy_with_local_available(self):
        router = CostAwareRouter()
        router._available_providers = {
            "voyage": TIER_PREMIUM,
            "ollama": TIER_ECONOMY,
        }
        router._routing_enabled = True

        # Session → economy → local available → should NOT use cloud
        assert router.should_use_cloud("session") is False

    def test_economy_without_local(self):
        router = CostAwareRouter()
        router._available_providers = {"voyage": TIER_PREMIUM}
        router._routing_enabled = True

        # Session → economy, but no local → must use cloud
        assert router.should_use_cloud("session") is True

    def test_premium_always_cloud(self):
        router = CostAwareRouter()
        router._available_providers = {
            "voyage": TIER_PREMIUM,
            "ollama": TIER_ECONOMY,
        }
        router._routing_enabled = True

        assert router.should_use_cloud("fact") is True


class TestEstimateCost:
    """Tests for estimate_cost() method."""

    def test_local_provider_zero_cost(self):
        router = CostAwareRouter()
        router._available_providers = {"ollama": TIER_ECONOMY}
        router._routing_enabled = True

        cost = router.estimate_cost("some text", "temp")
        assert cost == 0.0

    def test_cloud_provider_nonzero_cost(self):
        router = CostAwareRouter()
        router._available_providers = {"voyage": TIER_PREMIUM}
        router._routing_enabled = True

        cost = router.estimate_cost("some text", "fact")
        assert cost > 0.0

    def test_disabled_routing_zero_cost(self):
        router = CostAwareRouter()
        router._routing_enabled = False

        cost = router.estimate_cost("some text", "fact")
        assert cost == 0.0

    def test_longer_text_costs_more(self):
        router = CostAwareRouter()
        router._available_providers = {"voyage": TIER_PREMIUM}
        router._routing_enabled = True

        short_cost = router.estimate_cost("short", "fact")
        long_cost = router.estimate_cost("x" * 10000, "fact")
        assert long_cost > short_cost


class TestStatsAndExplain:
    """Tests for stats() and explain() methods."""

    def test_stats_structure(self):
        router = CostAwareRouter()
        router._available_providers = {"voyage": TIER_PREMIUM, "ollama": TIER_ECONOMY}
        router._routing_enabled = True

        stats = router.stats()
        assert "routing_enabled" in stats
        assert "available_providers" in stats
        assert "routing_decisions" in stats
        assert "tier_config" in stats
        assert stats["routing_enabled"] is True

    def test_stats_tier_config(self):
        router = CostAwareRouter()
        stats = router.stats()
        assert "fact" in stats["tier_config"]["premium"]
        assert "session" in stats["tier_config"]["economy"]
        assert "preference" in stats["tier_config"]["standard"]

    def test_explain_returns_string(self):
        router = CostAwareRouter()
        router._available_providers = {"voyage": TIER_PREMIUM, "ollama": TIER_ECONOMY}
        router._routing_enabled = True

        explanation = router.explain("fact")
        assert isinstance(explanation, str)
        assert "fact" in explanation
        assert "premium" in explanation
        assert "voyage" in explanation

    def test_explain_disabled_routing(self):
        router = CostAwareRouter()
        router._routing_enabled = False

        explanation = router.explain("fact")
        assert "routing disabled" in explanation

    def test_get_tier_for_category(self):
        router = CostAwareRouter()
        assert router.get_tier_for_category("fact") == TIER_PREMIUM
        assert router.get_tier_for_category("session") == TIER_ECONOMY
        assert router.get_tier_for_category("unknown") == TIER_STANDARD