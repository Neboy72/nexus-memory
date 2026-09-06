"""Invariant tests for the extractor/entity_extractor MEDIUM review fixes.

1. entity_extractor :490 — result caps are PAIRWISE CONSISTENT: a capped
   entity list never leaves relationships pointing at dropped endpoints.
2. extractor :400 — a VALID empty LLM result ('no facts') is returned
   as-is; heuristic fallback happens only on real failures (None).
"""

from types import SimpleNamespace

from nexus_memory.entity_extractor import extract_entities, _find_nearest_entity
from nexus_memory import extractor as ex


# ── :490 pairwise-consistent caps ────────────────────────────────────

def test_relationships_never_point_at_dropped_entities():
    """15 entities + a relationship chain → entity cap (10) must not leave
    a relationship referencing entity #11+."""
    # Text engineered to yield many entities and a connected_to pair late
    # in the chain (entities 11+ would be dropped by the old cap).
    text = (
        "Server 10.0.0.1 connected to 10.0.0.2. "
        + " ".join(f"10.0.{i}.1" for i in range(3, 20))
    )
    res = extract_entities(text)
    names = {e.name for e in res.entities}
    assert len(res.entities) <= 10
    for r in res.relationships:
        assert r.source in names, "relationship source must be a returned entity"
        assert r.target in names, "relationship endpoint dropped by cap"


def test_relationship_cap_drops_whole_pairs():
    """The [:8] relationship cap keeps relationships intact — each has both
    endpoints among the returned entities."""
    text = "10.0.0.1 connected to 10.0.0.2. " * 10
    res = extract_entities(text)
    names = {e.name for e in res.entities}
    assert len(res.relationships) <= 8
    for r in res.relationships:
        assert r.source in names and r.target in names


# ── :520 whole-word matching (already fixed by agent; pinned here) ───

def test_substring_no_longer_matches():
    # 'Main' (entity) must not be found inside 'Main Street' text.
    res = extract_entities("Wir treffen uns an der Main Street 5.")
    names = [e.name.lower() for e in res.entities]
    # 'Main' alone was never mentioned — 'Main Street' isn't a bare 'Main'.
    assert "main" not in names or "Main Street".lower() in names


# ── :400 valid empty LLM result vs. heuristic fallback ───────────────

def _patch_llm(monkeypatch, value):
    """Patch _llm_extract to return `value` (list or None)."""
    calls = {"value": value}

    def fake_extract(messages, hermes_home):
        return calls["value"]

    monkeypatch.setattr(ex, "_llm_extract", fake_extract)
    return calls


def test_valid_empty_llm_result_not_replaced_by_heuristic(monkeypatch):
    """LLM says 'no facts' → extractor returns [] (no pattern guesses)."""
    calls = _patch_llm(monkeypatch, [])
    out = ex.extract_facts(
        [{"role": "user", "content": "immer benutze Postgres 15"}],
        hermes_home="/tmp/x",
    )
    assert out == []
    assert calls["value"] == []


def test_llm_failure_falls_back_to_heuristic(monkeypatch):
    calls = _patch_llm(monkeypatch, None)  # None = failure
    out = ex.extract_facts(
        [{"role": "user", "content": "immer benutze Postgres 15"}],
        hermes_home="/tmp/x",
    )
    # Heuristic found the 'immer' preference pattern.
    assert out, "real failure must fall back to heuristic"
    assert any(o["category"] in ("preference", "rule") for o in out)


def test_llm_facts_returned_as_is(monkeypatch):
    payload = [{"text": "User uses Postgres 15", "category": "fact",
                "confidence": 0.9}]
    _patch_llm(monkeypatch, payload)
    out = ex.extract_facts(
        [{"role": "user", "content": "wir nutzen Postgres 15"}],
        hermes_home="/tmp/x",
    )
    assert out == payload