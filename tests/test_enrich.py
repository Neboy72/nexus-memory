"""Tests for nexus/enrich.py — Tiered Enrichment."""

from nexus.enrich import (
    EnrichmentTier,
    decide_tier,
    enrich,
    KNOWN_CATEGORIES,
)


class TestEnrichmentTier:
    def test_from_str_valid_lower(self):
        assert EnrichmentTier.from_str("raw") == EnrichmentTier.RAW

    def test_from_str_valid_upper(self):
        assert EnrichmentTier.from_str("TAGGED") == EnrichmentTier.TAGGED

    def test_from_str_int(self):
        assert EnrichmentTier.from_str(2) == EnrichmentTier.TAGGED
        assert EnrichmentTier.from_str(3) == EnrichmentTier.LINKED

    def test_from_str_invalid_fallback(self):
        assert EnrichmentTier.from_str("bogus") == EnrichmentTier.RAW
        assert EnrichmentTier.from_str(None) == EnrichmentTier.RAW
        assert EnrichmentTier.from_str(0) == EnrichmentTier.RAW


class TestDecideTier:
    def test_importance_high_linked(self):
        tier = decide_tier(
            content="The agent uses DeepSeek V4 Flash as primary model",
            category="general",
            importance="high",
        )
        assert tier == EnrichmentTier.LINKED, f"expected LINKED, got {tier}"

    def test_importance_medium_tagged(self):
        tier = decide_tier(
            content="Project update: deployment finished",
            category="general",
            importance="medium",
        )
        assert tier == EnrichmentTier.TAGGED, f"expected TAGGED, got {tier}"

    def test_importance_low_raw(self):
        tier = decide_tier(
            content="User said hello at 14:30",
            category="general",
            importance="low",
        )
        assert tier == EnrichmentTier.RAW, f"expected RAW, got {tier}"

    def test_category_config_linked(self):
        tier = decide_tier(
            content="Set logging level to debug in settings.yaml",
            category="config",
        )
        assert tier == EnrichmentTier.LINKED, f"expected LINKED, got {tier}"

    def test_category_decision_tagged(self):
        tier = decide_tier(
            content="We decided to use PostgreSQL for the new service",
            category="decision",
        )
        assert tier == EnrichmentTier.TAGGED, f"expected TAGGED, got {tier}"

    def test_content_signal_linked(self):
        tier = decide_tier(
            content="Production deployment requires the secret key",
            category="note",
        )
        assert tier == EnrichmentTier.LINKED, f"expected LINKED, got {tier}"

    def test_short_content_raw(self):
        tier = decide_tier(content="Ok.", category="general")
        assert tier == EnrichmentTier.RAW, f"expected RAW, got {tier}"

    def test_long_content_tagged(self):
        long = "The pipeline uses three-stage validation before deploying. " * 10
        tier = decide_tier(content=long, category="note")
        assert tier == EnrichmentTier.TAGGED, f"expected TAGGED, got {tier}"


class TestEnrich:
    def test_t1_raw_noop(self):
        payload = {"content": "hello world", "category": "log"}
        result = enrich(EnrichmentTier.RAW, payload)
        assert result is payload  # in-place
        assert result.get("_enrichment_tier") == 1

    def test_t2_tagged_adds_keywords(self):
        payload = {
            "content": "The Hermes Agent uses DeepSeek V4 Flash on Mac Mini M4",
            "category": "config",
        }
        result = enrich(EnrichmentTier.TAGGED, payload)
        assert result.get("_enrichment_tier") == 2
        assert "_keywords" in result
        assert isinstance(result["_keywords"], list)
        # Check that proper nouns and version-like tokens are extracted
        assert any("Mac Mini" in kw or "M4" in kw for kw in result["_keywords"])

    def test_t3_linked_placeholder(self):
        payload = {
            "content": "Architecture: three-stage validation pipeline",
            "category": "architecture",
        }
        result = enrich(EnrichmentTier.LINKED, payload)
        assert result.get("_enrichment_tier") == 3
        assert result.get("_linking_attempted") is True
        assert "_keywords" in result

    def test_unknown_category_warning(self):
        payload = {"content": "Some random note", "category": "bogus"}
        result = enrich(EnrichmentTier.TAGGED, payload)
        assert "_enrichment_warnings" in result
        assert any("unknown_category" in w for w in result["_enrichment_warnings"])
