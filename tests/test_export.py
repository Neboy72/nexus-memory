"""Tests for nexus.export — Skill Export Engine."""

import json
import os
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from nexus.export import (
    SKILL_TEMPLATE,
    _auto_describe,
    _auto_tags,
    _format_section,
    build_skill_md,
    cli_main,
    cluster_facts,
    export_skill,
    list_topics,
    search_knowledge,
)


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def sample_facts():
    """Realistic sample facts from Nexus Memory."""
    return [
        {
            "id": "fact-1",
            "rrf_score": 0.85,
            "content": "Always use try/except around Qdrant HTTP calls to handle connection errors gracefully.",
            "category": "pattern",
            "tier": 2,
            "fact_id": "f1",
            "version_id": "v1",
        },
        {
            "id": "fact-2",
            "rrf_score": 0.72,
            "content": "Never hardcode API keys in source code — use .env files with os.getenv().",
            "category": "lesson",
            "tier": 3,
            "fact_id": "f2",
            "version_id": "v2",
        },
        {
            "id": "fact-3",
            "rrf_score": 0.65,
            "content": "Verify that Qdrant is running before calling nexus_search: curl -s http://localhost:6333/healthz",
            "category": "verification",
            "tier": 2,
            "fact_id": "f3",
            "version_id": "v3",
        },
        {
            "id": "fact-4",
            "rrf_score": 0.60,
            "content": "Python 3.11+ required. Install with: pip install hermes-nexus-memory",
            "category": "prerequisite",
            "tier": 1,
            "fact_id": "f4",
            "version_id": "v4",
        },
        {
            "id": "fact-5",
            "rrf_score": 0.55,
            "content": "Nexus Memory needs Qdrant v1.17+ running on localhost:6333",
            "category": "setup",
            "tier": 1,
            "fact_id": "f5",
            "version_id": "v5",
        },
    ]


@pytest.fixture
def mixed_facts():
    """Facts with generic categories that need content-based detection."""
    return [
        {
            "id": "fact-6",
            "rrf_score": 0.50,
            "content": "Check that the gateway is running before starting tests. Verify the health endpoint returns 200.",
            "category": "fact",
            "tier": 2,
            "fact_id": "f6",
        },
        {
            "id": "fact-7",
            "rrf_score": 0.45,
            "content": "Don't use deprecated API endpoints — always check the changelog first.",
            "category": "decision",
            "tier": 3,
            "fact_id": "f7",
        },
        {
            "id": "fact-8",
            "rrf_score": 0.40,
            "content": "You need to install bm25s for hybrid search: pip install bm25s",
            "category": "fact",
            "tier": 1,
            "fact_id": "f8",
        },
    ]


# ── cluster_facts ───────────────────────────────────────────────────────────


class TestClusterFacts:
    def test_categorizes_by_category_field(self, sample_facts):
        """Facts are sorted into correct sections by their category field."""
        clusters = cluster_facts(sample_facts)

        assert len(clusters["steps"]) == 1  # fact-1 (pattern)
        assert "try/except" in clusters["steps"][0]

        assert len(clusters["pitfalls"]) == 1  # fact-2 (lesson)
        assert "Never hardcode" in clusters["pitfalls"][0]

        assert len(clusters["verification"]) == 1  # fact-3 (verification)
        assert "Verify" in clusters["verification"][0]

        # fact-4 (prerequisite) + fact-5 (setup)
        assert len(clusters["prerequisites"]) == 2

    def test_content_based_detection(self, mixed_facts):
        """Generic-category facts get sorted by content signals."""
        clusters = cluster_facts(mixed_facts)

        # fact-6: "Verify" keyword → verification
        assert any("verify" in c.lower() for c in clusters["verification"])
        assert any("gateway is running" in c for c in clusters["verification"])

        # fact-7: "Don't" → pitfalls
        assert any("don't use deprecated" in c.lower() for c in clusters["pitfalls"])

        # fact-8: "need to install" → prerequisites
        assert any("install bm25s" in c for c in clusters["prerequisites"])

    def test_deduplicates_similar_content(self):
        """Near-identical facts are deduplicated (first 80 chars match)."""
        facts = [
            {"content": "Always validate input before processing. This text starts identically for the first many characters.", "category": "pattern", "rrf_score": 0.9},
            {"content": "Always validate input before processing. This text starts identically for the first many characters. But this one continues.", "category": "pattern", "rrf_score": 0.8},
        ]
        clusters = cluster_facts(facts)
        assert len(clusters["steps"]) == 1  # deduplicated

    def test_empty_facts(self):
        """Empty fact list produces empty clusters."""
        clusters = cluster_facts([])
        assert all(len(v) == 0 for v in clusters.values())

    def test_filters_empty_content(self):
        """Facts with empty or whitespace-only content are skipped."""
        facts = [
            {"content": "", "category": "pattern", "rrf_score": 0.5},
            {"content": "   ", "category": "pattern", "rrf_score": 0.5},
        ]
        clusters = cluster_facts(facts)
        assert len(clusters["steps"]) == 0


# ── build_skill_md ──────────────────────────────────────────────────────────


class TestBuildSkillMd:
    def test_generates_yaml_frontmatter(self, sample_facts):
        """Output starts with valid YAML frontmatter."""
        clusters = cluster_facts(sample_facts)
        md = build_skill_md("test-skill", clusters, sample_facts)

        assert md.startswith("---")
        assert "name: test-skill" in md
        assert "description:" in md
        assert "tags:" in md

    def test_contains_all_sections(self, sample_facts):
        """All four sections are present in output."""
        clusters = cluster_facts(sample_facts)
        md = build_skill_md("test-skill", clusters, sample_facts)

        assert "## Prerequisites" in md
        assert "## Steps" in md
        assert "## Pitfalls" in md
        assert "## Verification" in md

    def test_default_text_for_empty_sections(self):
        """Empty sections get 'None yet documented.' placeholder."""
        clusters = {"steps": [], "pitfalls": [], "prerequisites": [], "verification": []}
        md = build_skill_md("empty-skill", clusters, [])

        assert "None yet documented." in md

    def test_auto_generated_tags(self, sample_facts):
        """Tags are auto-extracted from fact categories."""
        clusters = cluster_facts(sample_facts)
        md = build_skill_md("test-skill", clusters, sample_facts)

        # Should have tags from categories: pattern, lesson, verification, prerequisite
        assert "tags:" in md
        # No '[]' empty bracket
        assert "tags: []" not in md

    def test_title_from_name(self, sample_facts):
        """Title is derived from name with hyphens converted to spaces."""
        clusters = cluster_facts(sample_facts)
        md = build_skill_md("test-skill-name", clusters, sample_facts)

        assert "# Test Skill Name" in md

    def test_preserves_ordered_bullets(self, sample_facts):
        """Steps section outputs as markdown bullet list."""
        clusters = cluster_facts(sample_facts)
        md = build_skill_md("test-skill", clusters, sample_facts)

        # Steps should be bullet points
        steps_section = md.split("## Steps")[1].split("## Pitfalls")[0]
        assert "- " in steps_section


# ── _format_section ─────────────────────────────────────────────────────────


class TestFormatSection:
    def test_formats_bullets(self):
        result = _format_section(["Step 1", "Step 2"])
        assert result == "- Step 1\n- Step 2"

    def test_default_for_empty(self):
        result = _format_section([])
        assert result == "None yet documented."


# ── _auto_describe ──────────────────────────────────────────────────────────


class TestAutoDescribe:
    def test_describes_with_counts(self, sample_facts):
        clusters = cluster_facts(sample_facts)
        desc = _auto_describe(clusters)
        assert "steps" in desc.lower()
        assert "pitfalls" in desc.lower()
        assert "prerequisites" in desc.lower()

    def test_fallback_for_empty(self):
        desc = _auto_describe({"steps": [], "pitfalls": [], "prerequisites": [], "verification": []})
        assert "Auto-exported" in desc


# ── _auto_tags ──────────────────────────────────────────────────────────────


class TestAutoTags:
    def test_extracts_from_categories(self, sample_facts):
        clusters = cluster_facts(sample_facts)
        tags = _auto_tags(clusters, sample_facts)
        assert "pattern" in tags
        assert "prerequisite" in tags
        assert "verification" in tags

    def test_content_based_tags(self):
        facts = [{"content": "Never use deprecated APIs — security risk!", "category": "lesson"}]
        clusters = cluster_facts(facts)
        tags = _auto_tags(clusters, facts)
        assert "deprecation" in tags


# ── search_knowledge (mocked) ──────────────────────────────────────────────


class TestSearchKnowledge:
    @patch("nexus.nexus_search_hybrid")
    def test_filters_canonical_only(self, mock_search):
        """Only status=canonical facts are returned."""
        mock_search.return_value = [
            {
                "id": "a1",
                "rrf_score": 0.9,
                "payload": {
                    "content": "Canonical fact",
                    "category": "pattern",
                    "status": "canonical",
                    "fact_id": "f1",
                },
            },
            {
                "id": "a2",
                "rrf_score": 0.8,
                "payload": {
                    "content": "Deprecated fact",
                    "category": "fact",
                    "status": "deprecated",
                    "fact_id": "f2",
                },
            },
            {
                "id": "a3",
                "rrf_score": 0.7,
                "payload": {
                    "content": "Pending fact",
                    "category": "fact",
                    "status": "pending",
                    "fact_id": "f3",
                },
            },
        ]

        results = search_knowledge("test", limit=10)
        assert len(results) == 1
        assert results[0]["id"] == "a1"

    @patch("nexus.nexus_search_hybrid")
    def test_empty_result(self, mock_search):
        mock_search.return_value = []
        results = search_knowledge("nothing", limit=10)
        assert results == []

    @patch("nexus.nexus_search_hybrid")
    def test_filters_short_content(self, mock_search):
        """Facts with content < 10 chars are filtered out."""
        mock_search.return_value = [
            {
                "id": "a1",
                "rrf_score": 0.5,
                "payload": {
                    "content": "Hi",
                    "category": "fact",
                    "status": "canonical",
                },
            },
        ]
        results = search_knowledge("test", limit=10)
        assert len(results) == 0


# ── export_skill (mocked) ─────────────────────────────────────────────────


class TestExportSkill:
    @patch("nexus.export.search_knowledge")
    def test_basic_export(self, mock_search, sample_facts):
        """export_skill returns correct structure."""
        mock_search.return_value = sample_facts

        with tempfile.TemporaryDirectory() as tmpdir:
            result = export_skill("test-skill", output_dir=tmpdir)

            assert result["name"] == "test-skill"
            assert result["facts_found"] == 5
            assert result["clusters"]["steps"] >= 1
            assert result["clusters"]["pitfalls"] >= 1
            assert result["output_path"] == os.path.join(tmpdir, "test-skill.md")
            assert os.path.exists(result["output_path"])

    @patch("nexus.export.search_knowledge")
    def test_deploy_mode(self, mock_search, sample_facts):
        """--deploy writes to ~/.hermes/skills/<name>/SKILL.md."""
        mock_search.return_value = sample_facts

        with patch("nexus.export.HERMES_SKILLS_DIR", new=tempfile.mkdtemp()) as skills_dir:
            result = export_skill("deployed-skill", deploy=True)

            expected = os.path.join(skills_dir, "deployed-skill", "SKILL.md")
            assert result["output_path"] == expected
            assert result["deployed"] is True
            assert os.path.exists(expected)

    @patch("nexus.export.search_knowledge")
    def test_topic_override(self, mock_search, sample_facts):
        """--topic overrides the search query independently of skill name."""
        mock_search.return_value = sample_facts

        with tempfile.TemporaryDirectory() as tmpdir:
            result = export_skill("my-skill", topic="custom search topic", output_dir=tmpdir)

            assert result["topic"] == "custom search topic"
            mock_search.assert_called_with("custom search topic", limit=20)

    @patch("nexus.export.search_knowledge")
    def test_empty_export(self, mock_search):
        """Export with no facts still produces valid SKILL.md."""
        mock_search.return_value = []

        with tempfile.TemporaryDirectory() as tmpdir:
            result = export_skill("empty-skill", output_dir=tmpdir)
            assert result["facts_found"] == 0
            assert os.path.exists(result["output_path"])


# ── list_topics (mocked Qdrant) ──────────────────────────────────────────


class TestListTopics:
    @patch("requests.post")
    def test_groups_by_category(self, mock_post):
        """Facts are grouped by category with correct counts."""
        # Simulate Qdrant scroll response
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "result": {
                "points": [
                    {
                        "payload": {
                            "status": "canonical",
                            "category": "pattern",
                            "content": "Step 1: Always validate input",
                        }
                    },
                    {
                        "payload": {
                            "status": "canonical",
                            "category": "lesson",
                            "content": "Never hardcode API keys",
                        }
                    },
                    {
                        "payload": {
                            "status": "canonical",
                            "category": "pattern",
                            "content": "Step 2: Use try/except blocks",
                        }
                    },
                ]
            }
        }
        mock_post.return_value = mock_response

        topics = list_topics(min_facts=2)

        # pattern has 2 facts (≥ 2), lesson has 1 (< 2)
        assert len(topics) == 1
        assert topics[0]["category"] == "pattern"
        assert topics[0]["fact_count"] == 2

    @patch("requests.post")
    def test_empty_result(self, mock_post):
        """No categories returned when no points match."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "result": {"points": []}
        }
        mock_post.return_value = mock_response

        topics = list_topics(min_facts=1)
        assert len(topics) == 0

    @patch("requests.post")
    def test_legacy_facts_included(self, mock_post):
        """Pre-v1.8.0 facts (no status) are treated as canonical."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "result": {
                "points": [
                    {
                        "payload": {
                            "category": "pattern",
                            "content": "Legacy fact without status field",
                        }
                    },
                ]
            }
        }
        mock_post.return_value = mock_response

        topics = list_topics(min_facts=1)
        assert len(topics) == 1
        assert "Legacy" in topics[0]["sample"]

    @patch("requests.post")
    def test_short_content_filtered(self, mock_post):
        """Facts with content < 10 chars are excluded."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "result": {
                "points": [
                    {
                        "payload": {
                            "status": "canonical",
                            "category": "pattern",
                            "content": "Hi",
                        }
                    },
                    {
                        "payload": {
                            "status": "canonical",
                            "category": "pattern",
                            "content": "   ",
                        }
                    },
                ]
            }
        }
        mock_post.return_value = mock_response

        topics = list_topics(min_facts=1)
        assert len(topics) == 0


# ── CLI ─────────────────────────────────────────────────────────────────


class TestCli:
    def test_help(self):
        """CLI accepts --help without error."""
        with pytest.raises(SystemExit) as exc:
            try:
                cli_main()
            except SystemExit:
                raise
        # argparse exits with 0 on --help

    @patch("nexus.export.list_topics")
    def test_list_flag_calls_list_topics(self, mock_list):
        """--list flag triggers list_topics."""
        mock_list.return_value = [{"topic": "pattern", "fact_count": 5, "sample": "Step 1"}]

        with patch.object(sys, "argv", ["nexus-export", "--list"]):
            try:
                cli_main()
            except SystemExit:
                pass
        mock_list.assert_called_once()

    @patch("nexus.export.export_skill")
    def test_skill_flag_calls_export(self, mock_export):
        """--skill flag triggers export_skill."""
        mock_export.return_value = {"name": "test", "topic": "test", "facts_found": 5, "clusters": {}, "output_path": "/tmp/test.md", "deployed": False}

        with patch.object(sys, "argv", ["nexus-export", "--skill", "test-skill"]):
            try:
                cli_main()
            except SystemExit:
                pass
        mock_export.assert_called_once()
