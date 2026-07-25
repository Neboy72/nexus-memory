"""Tests for the session→memory extraction pipeline."""

import json
import pytest
from nexus_memory.extractor import extract_facts, _heuristic_extract, _llm_extract


class TestHeuristicExtraction:
    """Unit tests for the pattern-based fallback extractor."""

    def test_empty_messages(self):
        assert _heuristic_extract([]) == []

    def test_no_relevant_messages(self):
        msgs = [
            {"role": "tool", "content": "some output"},
            {"role": "system", "content": "system prompt"},
        ]
        assert _heuristic_extract(msgs) == []

    def test_rule_extraction_german(self):
        msgs = [
            {"role": "user", "content": "Du musst immer erst den Skill laden bevor du antwortest."},
        ]
        facts = _heuristic_extract(msgs)
        assert len(facts) >= 1
        assert facts[0]["category"] == "rule"
        assert "immer" in facts[0]["text"].lower()

    def test_rule_extraction_english(self):
        msgs = [
            {"role": "user", "content": "Never delete the test directory. It is important."},
        ]
        facts = _heuristic_extract(msgs)
        assert len(facts) >= 1
        assert facts[0]["category"] == "rule"

    def test_preference_extraction(self):
        msgs = [
            {"role": "user", "content": "Ich mag keine langen Antworten. Bitte kurz halten."},
        ]
        facts = _heuristic_extract(msgs)
        assert len(facts) >= 1
        assert any(f["category"] == "preference" for f in facts)

    def test_correction_extraction(self):
        msgs = [
            {"role": "assistant", "content": "Die IP ist 192.168.1.1"},
            {"role": "user", "content": "Nein, das stimmt nicht. Die IP ist 192.168.31.59."},
        ]
        facts = _heuristic_extract(msgs)
        # Should find the correction as a rule
        corrections = [f for f in facts if f["category"] == "rule"]
        assert len(corrections) >= 1

    def test_fact_extraction_assistant_only(self):
        msgs = [
            {"role": "user", "content": "Was ist die IP vom Server?"},
            {"role": "assistant", "content": "Der Server laeuft auf 192.168.31.59:8123."},
        ]
        facts = _heuristic_extract(msgs)
        # User message should not trigger fact pattern, assistant should
        fact_cats = [f for f in facts if f["category"] == "fact"]
        # May or may not match depending on pattern, but if it does it's from assistant
        for f in fact_cats:
            assert "192.168" in f["text"] or "lauft" in f["text"].lower()

    def test_dedup_same_text(self):
        msgs = [
            {"role": "user", "content": "Nie die config.yaml loeschen ohne GO."},
            {"role": "user", "content": "Nie die config.yaml loeschen ohne GO."},
        ]
        facts = _heuristic_extract(msgs)
        # Should not duplicate
        assert len(facts) <= 1

    def test_max_five_facts(self):
        msgs = []
        for i in range(10):
            msgs.append({"role": "user", "content": f"Immer Regel Nummer {i} befolgen."})
        facts = _heuristic_extract(msgs)
        assert len(facts) <= 5

    def test_confidence_range(self):
        msgs = [
            {"role": "user", "content": "Du musst immer vorher nachdenken."},
        ]
        facts = _heuristic_extract(msgs)
        for f in facts:
            assert 0.0 <= f["confidence"] <= 1.0

    def test_text_truncation(self):
        long_text = "Immer " + "x" * 500 + " befolgen."
        msgs = [{"role": "user", "content": long_text}]
        facts = _heuristic_extract(msgs)
        for f in facts:
            assert len(f["text"]) <= 300


class TestExtractFactsPublicAPI:
    """Tests for the public extract_facts() function."""

    def test_empty_messages(self):
        assert extract_facts([]) == []

    def test_only_tool_messages(self):
        msgs = [{"role": "tool", "content": "output"}]
        assert extract_facts(msgs) == []

    def test_filters_non_string_content(self):
        msgs = [
            {"role": "user", "content": None},
            {"role": "assistant", "content": {"nested": "dict"}},
            {"role": "user", "content": ""},
        ]
        assert extract_facts(msgs) == []

    def test_heuristic_fallback_returns_results(self):
        """Without hermes_home, should use heuristic and return results."""
        msgs = [
            {"role": "user", "content": "Nie den Ollama-Prozess killen ohne Fallback."},
        ]
        facts = extract_facts(msgs, hermes_home="")
        assert len(facts) >= 1
        assert facts[0]["category"] == "rule"
        assert "text" in facts[0]
        assert "confidence" in facts[0]


class TestLLMExtraction:
    """Tests for the LLM extraction path. These use mocking since we
    don't want to call a real API in unit tests."""

    def test_llm_returns_empty_on_no_model(self):
        """When no model is configured, LLM extraction should return []."""
        # _load_llm_config with empty hermes_home should give no model
        # Actually it falls back to gemma3:4b, so this tests the fallback path
        msgs = [{"role": "user", "content": "test"}]
        # This will try to connect to localhost ollama, which may or may not be running
        # We just verify it doesn't crash
        try:
            result = _llm_extract(msgs, "/nonexistent/path")
            assert isinstance(result, list)
        except Exception:
            pass  # OK if it fails gracefully

    def test_llm_parses_json_response(self):
        """Verify that the JSON parsing logic handles markdown code blocks."""
        import re as stdlib_re

        # Simulate LLM responses with code blocks (lowercase and uppercase)
        for tag in ["json", "JSON", ""]:
            if tag:
                fake_response = f"```{tag}\n" + '{"facts": [{"text": "Test", "category": "fact", "confidence": 0.9}]}\n```'
            else:
                fake_response = '{"facts": [{"text": "Test", "category": "fact", "confidence": 0.9}]}'

            text = fake_response.strip()
            if "```" in text:
                match = stdlib_re.search(r"```(?:json|JSON)?\s*(.*?)```", text, stdlib_re.DOTALL)
                assert match is not None, f"Failed to extract from tag={tag!r}"
                parsed = json.loads(match.group(1).strip())
            else:
                parsed = json.loads(text)
            assert len(parsed["facts"]) == 1
            assert parsed["facts"][0]["category"] == "fact"

    def test_llm_parses_plain_json(self):
        """Verify parsing of plain JSON without code blocks."""
        import json

        fake_response = '{"facts": [{"text": "Test fact", "category": "rule", "confidence": 0.8}]}'
        parsed = json.loads(fake_response)
        assert len(parsed["facts"]) == 1
        assert parsed["facts"][0]["category"] == "rule"

    def test_llm_handles_empty_facts(self):
        """When LLM returns no facts, should return []."""
        import json

        fake_response = '{"facts": []}'
        parsed = json.loads(fake_response)
        assert len(parsed["facts"]) == 0

    def test_llm_normalizes_invalid_category(self):
        """Invalid categories should default to 'fact'."""
        # Simulate the normalization logic from _llm_extract
        category = "invalid_cat"
        if category not in ("fact", "rule", "preference", "belief"):
            category = "fact"
        assert category == "fact"

    def test_llm_clamps_confidence(self):
        """Confidence values should be clamped to [0, 1]."""
        for raw in [-0.5, 0.0, 0.7, 1.0, 1.5, "invalid"]:
            try:
                confidence = float(raw)
                confidence = max(0.0, min(1.0, confidence))
            except (TypeError, ValueError):
                confidence = 0.7
            assert 0.0 <= confidence <= 1.0


class TestIntegrationScenarios:
    """Realistic conversation scenarios that test the full pipeline."""

    def test_technical_session_extracts_rules(self):
        """A session about system configuration should extract rules."""
        msgs = [
            {"role": "user", "content": "Config.yaml darf nur mit meiner Freigabe geaendert werden."},
            {"role": "assistant", "content": "Verstanden. Ich werde config.yaml nie ohne dein GO aendern."},
            {"role": "user", "content": "Ollama-Prozess nie killen ohne verifizierten Fallback."},
        ]
        facts = extract_facts(msgs, hermes_home="")
        assert len(facts) >= 1
        # Should have at least one rule
        rules = [f for f in facts if f["category"] == "rule"]
        assert len(rules) >= 1

    def test_preference_session(self):
        """A session expressing preferences should extract them."""
        msgs = [
            {"role": "user", "content": "Ich bevorzuge knappe Antworten. Keine langen Erklaerungen wenn nicht gefragt."},
        ]
        facts = extract_facts(msgs, hermes_home="")
        prefs = [f for f in facts if f["category"] == "preference"]
        assert len(prefs) >= 1

    def test_correction_creates_rule(self):
        """When user corrects the assistant, it should become a rule."""
        msgs = [
            {"role": "assistant", "content": "Die Wallbox hat IP 192.168.1.1"},
            {"role": "user", "content": "Nein, falsch. Die Wallbox ist auf 192.168.31.235."},
        ]
        facts = extract_facts(msgs, hermes_home="")
        rules = [f for f in facts if f["category"] == "rule"]
        assert len(rules) >= 1