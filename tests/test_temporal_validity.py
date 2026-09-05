"""Tests for Temporal Fact Validity (Unreleased).

Covers:
- valid_from/valid_to set on remember() (defaults + effective_from override)
- Auto-supersession stamps valid_to on the old fact
- recall() WITHOUT as_of behaves exactly like before (deprecated filtered —
  regression protection)
- recall(as_of=past) returns the fact that was valid then (even if deprecated)
- recall(as_of=future) returns nothing for facts with valid_from in the future
- fact_history() returns the ordered supersession chain
- Legacy points without temporal fields survive as_of=None AND as_of queries
  (backward compatibility)
"""

from __future__ import annotations

import asyncio
import json as _json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

import nexus_memory.mcp_server as mcp
from nexus_memory.mcp_server import (
    _parse_iso,
    _valid_at,
    _valid_from_of,
    _valid_to_of,
)


# ===========================================================================
# Helpers
# ===========================================================================


def _iso(**delta) -> str:
    """ISO-8601 now + given deltas (UTC)."""
    return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat()


def _payload(
    *,
    pid: str = "abc-123",
    content: str = "hello world",
    access_level: str = "public",
    category: str = "fact",
    created_at: str = "2025-01-01T00:00:00+00:00",
    lifecycle_status: str = "canonical",
    valid_until: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    superseded_at: str | None = None,
    superseded_by: str | None = None,
    supersede_reason: str | None = None,
) -> dict:
    """Qdrant-shaped record dict — with temporal fields."""
    return {
        "id": pid,
        "content": content,
        "access_level": access_level,
        "category": category,
        "source": "test",
        "source_url": None,
        "provenance": {"confidence": 0.8},
        "created_at": created_at,
        "lifecycle_status": lifecycle_status,
        "valid_until": valid_until,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "superseded_at": superseded_at,
        "superseded_by": superseded_by,
        "supersede_reason": supersede_reason,
    }


def _hit(payload: dict, score: float = 0.9):
    """Wrap a payload dict into a query_points()-shaped hit."""
    hit = MagicMock()
    hit.id = payload["id"]
    hit.payload = payload
    hit.score = score
    return hit


# ===========================================================================
# Fixture: MemoryStore with mocked Qdrant client (same pattern as
# tests/test_mcp_server.py — that module's `store` fixture is local, so we
# replicate it here to keep the file self-contained).
# ===========================================================================


class _FakeEmbedder:
    """In-process replacement for the real EmbeddingProvider (no network)."""

    _name = "fake-embedder"
    _dim = 384
    _model = None
    _client = None

    async def embed(self, text: str) -> list[float]:
        return [0.0] * 384

    @property
    def name(self) -> str:
        return self._name

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def available(self) -> bool:
        return True

    @property
    def model_name(self) -> str:
        return self._name


@pytest.fixture
def store(monkeypatch, mock_qdrant_client, isolated_env):
    """MemoryStore whose Qdrant client + hybrid retriever are mocks."""
    monkeypatch.setattr(mcp, "EmbeddingProvider", _FakeEmbedder)
    monkeypatch.setattr(mcp.MemoryStore, "_init_hybrid", lambda self: None)
    mock_qdrant_client.get_collections.return_value = MagicMock(collections=[])
    return mcp.MemoryStore()


# ===========================================================================
# Unit: temporal helpers
# ===========================================================================


class TestParseIso:
    """ISO-8601 parsing with/without timezone, Z suffix, date-only."""

    def test_parse_with_tz(self):
        dt = _parse_iso("2026-09-01T12:00:00+02:00")
        assert dt is not None
        assert dt.tzinfo is not None
        assert dt.utcoffset().total_seconds() == 0  # normalized to UTC

    def test_parse_with_z_suffix(self):
        dt = _parse_iso("2026-09-01T12:00:00Z")
        assert dt is not None
        assert dt.tzinfo is not None

    def test_parse_naive_assumed_utc(self):
        dt = _parse_iso("2026-09-01T12:00:00")
        assert dt is not None
        assert dt.utcoffset().total_seconds() == 0

    def test_parse_date_only(self):
        dt = _parse_iso("2026-09-01")
        assert dt is not None
        assert (dt.year, dt.month, dt.day) == (2026, 9, 1)

    def test_parse_none_and_garbage(self):
        assert _parse_iso(None) is None
        assert _parse_iso("") is None
        assert _parse_iso("not-a-date") is None
        assert _parse_iso("garbage", default="fallback") == "fallback"


class TestValidAt:
    """_valid_at(): (valid_from|created_at) <= as_of AND (valid_to|superseded_at|∞) > as_of."""

    def test_open_interval_implicitly_valid(self):
        pl = _payload(valid_from=None, valid_to=None)
        assert _valid_at(pl, _parse_iso("2026-01-15")) is True

    def test_valid_inside_window(self):
        pl = _payload(valid_from="2026-01-01T00:00:00+00:00",
                      valid_to="2026-06-01T00:00:00+00:00")
        assert _valid_at(pl, _parse_iso("2026-03-15")) is True

    def test_invalid_before_valid_from(self):
        pl = _payload(valid_from="2026-01-01T00:00:00+00:00", valid_to=None)
        assert _valid_at(pl, _parse_iso("2025-12-31")) is False

    def test_invalid_at_or_after_valid_to(self):
        pl = _payload(valid_from="2026-01-01T00:00:00+00:00",
                      valid_to="2026-06-01T00:00:00+00:00")
        # valid_to is exclusive: at exactly valid_to the fact is no longer valid
        assert _valid_at(pl, _parse_iso("2026-06-01")) is False
        assert _valid_at(pl, _parse_iso("2026-06-02")) is False

    def test_superseded_at_fallback(self):
        # No valid_to but superseded_at → supersession ended validity
        pl = _payload(valid_from="2026-01-01T00:00:00+00:00",
                      valid_to=None,
                      superseded_at="2026-05-01T00:00:00+00:00")
        assert _valid_at(pl, _parse_iso("2026-04-30")) is True
        assert _valid_at(pl, _parse_iso("2026-05-01")) is False

    def test_created_at_fallback(self):
        # No valid_from → created_at is the start
        pl = _payload(valid_from=None, created_at="2026-02-01T00:00:00+00:00")
        assert _valid_at(pl, _parse_iso("2026-01-31")) is False
        assert _valid_at(pl, _parse_iso("2026-02-01")) is True


# ===========================================================================
# (a) remember() sets valid_from / valid_to
# ===========================================================================


class TestRememberSetsTemporalFields:
    async def test_remember_sets_valid_from_and_to(self, store, mock_qdrant_client):
        result = await store.remember(text="Sky is blue", category="fact")
        assert result["status"] == "ok"

        payload = mock_qdrant_client.upsert.call_args.kwargs["points"][0].payload
        assert "valid_from" in payload
        assert "valid_to" in payload
        # valid_from defaults to created_at
        assert payload["valid_from"] == payload["created_at"]
        # valid_to=None → open interval / still valid
        assert payload["valid_to"] is None
        # sanity: parseable ISO
        assert _parse_iso(payload["valid_from"]) is not None

    async def test_effective_from_overrides_valid_from(self, store, mock_qdrant_client):
        result = await store.remember(
            text="Imported mail fact",
            category="fact",
            effective_from="2026-08-15T09:30:00+00:00",
        )
        assert result["status"] == "ok"
        payload = mock_qdrant_client.upsert.call_args.kwargs["points"][0].payload
        assert payload["valid_from"] == "2026-08-15T09:30:00+00:00"
        # created_at stays at "now" (not overridden by retro date)
        assert _parse_iso(payload["created_at"]) > _parse_iso(payload["valid_from"])
        assert payload["valid_to"] is None

    async def test_effective_from_date_only_accepted(self, store, mock_qdrant_client):
        result = await store.remember(
            text="Retro import", category="fact", effective_from="2026-08-01"
        )
        assert result["status"] == "ok"
        payload = mock_qdrant_client.upsert.call_args.kwargs["points"][0].payload
        assert payload["valid_from"].startswith("2026-08-01")

    async def test_invalid_effective_from_falls_back_to_created_at(
        self, store, mock_qdrant_client
    ):
        result = await store.remember(
            text="x", category="fact", effective_from="garbage-date"
        )
        assert result["status"] == "ok"
        payload = mock_qdrant_client.upsert.call_args.kwargs["points"][0].payload
        assert payload["valid_from"] == payload["created_at"]


# ===========================================================================
# (b) Auto-supersession stamps valid_to on the old fact
# ===========================================================================


class TestSupersessionSetsValidTo:
    async def test_supersession_sets_valid_to_on_old_fact(self):
        from nexus_memory.mcp_server import MemoryStore

        old_id = "old-fact-123"
        existing = [_hit(_payload(
            pid=old_id,
            content="Project uses Stripe for payments",
            category="fact",
            lifecycle_status="canonical",
        ), score=0.95)]

        with patch.object(MemoryStore, "_embed") as mock_embed:
            mock_embed.return_value = [0.1] * 1024
            with patch.object(MemoryStore, "_ensure_collection"):
                real_store = MemoryStore.__new__(MemoryStore)
                real_store.client = MagicMock()
                real_store._embedder = MagicMock(dim=1024)
                real_store._skill_graph = None
                real_store.client.query_points.return_value = MagicMock(
                    points=existing
                )

                result = await MemoryStore.remember(
                    real_store,
                    text="Project uses Paddle for payments",
                    category="fact",
                )

                assert result["status"] == "ok"
                assert result["superseded"] == [old_id]

                set_payload_call = real_store.client.set_payload.call_args
                payload = set_payload_call.kwargs["payload"]
                assert payload["lifecycle_status"] == "deprecated"
                assert payload["superseded_by"] == result["id"]
                assert payload["superseded_at"]
                # THE NEW PART: valid_to stamped at supersession time
                assert payload["valid_to"] == payload["superseded_at"]
                assert _parse_iso(payload["valid_to"]) is not None


# ===========================================================================
# (c) + (d) + (e) recall() as_of behavior
# ===========================================================================


class TestRecallAsOf:
    def _setup_store(self, store, mock_qdrant_client, hits):
        mock_qdrant_client.query_points.return_value = MagicMock(points=hits)
        return store

    async def test_recall_without_as_of_filters_deprecated_regression(
        self, store, mock_qdrant_client
    ):
        """(c) Regression protection: as_of=None → deprecated filtered out exactly as before."""
        store._hybrid_retriever = None
        old = _hit(_payload(
            pid="old-fact",
            content="Project uses Stripe",
            lifecycle_status="deprecated",
            superseded_at="2026-05-01T00:00:00+00:00",
            superseded_by="new-fact",
            valid_to="2026-05-01T00:00:00+00:00",
        ))
        self._setup_store(store, mock_qdrant_client, [old])

        results = await store.recall("stripe", limit=5)  # as_of defaults to None
        assert len(results) == 0, "deprecated fact must be filtered without as_of"

    async def test_recall_without_as_of_keeps_canonical(
        self, store, mock_qdrant_client
    ):
        store._hybrid_retriever = None
        cur = _hit(_payload(pid="new-fact", content="Project uses Paddle"))
        self._setup_store(store, mock_qdrant_client, [cur])

        results = await store.recall("paddle", limit=5)
        assert len(results) == 1
        assert results[0]["id"] == "new-fact"

    async def test_recall_as_of_past_returns_deprecated_fact_valid_then(
        self, store, mock_qdrant_client
    ):
        """(d) Point-in-time: the deprecated fact was valid at as_of → returned."""
        store._hybrid_retriever = None
        old = _hit(_payload(
            pid="old-fact",
            content="Project uses Stripe",
            lifecycle_status="deprecated",
            created_at="2026-01-01T00:00:00+00:00",
            superseded_at="2026-09-01T12:00:00+00:00",
            superseded_by="new-fact",
            valid_from="2026-01-01T00:00:00+00:00",
            valid_to="2026-09-01T12:00:00+00:00",
        ))
        self._setup_store(store, mock_qdrant_client, [old])

        # A month before supersession the old fact was the valid truth
        results = await store.recall("stripe", limit=5, as_of="2026-08-01")
        assert len(results) == 1
        assert results[0]["id"] == "old-fact"

    async def test_recall_as_of_after_supersession_excludes_old_fact(
        self, store, mock_qdrant_client
    ):
        """as_of after valid_to → old fact no longer valid even in point-in-time mode."""
        store._hybrid_retriever = None
        old = _hit(_payload(
            pid="old-fact",
            content="Project uses Stripe",
            lifecycle_status="deprecated",
            created_at="2026-01-01T00:00:00+00:00",
            superseded_at="2026-09-01T12:00:00+00:00",
            valid_from="2026-01-01T00:00:00+00:00",
            valid_to="2026-09-01T12:00:00+00:00",
        ))
        self._setup_store(store, mock_qdrant_client, [old])

        results = await store.recall("stripe", limit=5, as_of="2026-09-02")
        assert len(results) == 0

    async def test_recall_as_of_future_excludes_future_valid_from(
        self, store, mock_qdrant_client
    ):
        """(e) effective_from in the future → not valid at earlier as_of."""
        store._hybrid_retriever = None
        future = _hit(_payload(
            pid="future-fact",
            content="Go-live starts October",
            created_at="2026-09-01T00:00:00+00:00",
            valid_from="2026-10-01T00:00:00+00:00",  # future
            valid_to=None,
        ))
        self._setup_store(store, mock_qdrant_client, [future])

        results = await store.recall("go-live", limit=5, as_of="2026-09-02")
        assert len(results) == 0, "fact with valid_from in the future must not match"

    async def test_recall_as_of_at_future_valid_from_is_included(
        self, store, mock_qdrant_client
    ):
        """Boundary: as_of == valid_from (inclusive) → valid."""
        store._hybrid_retriever = None
        future = _hit(_payload(
            pid="future-fact",
            content="Go-live starts October",
            created_at="2026-09-01T00:00:00+00:00",
            valid_from="2026-10-01T00:00:00+00:00",
            valid_to=None,
        ))
        self._setup_store(store, mock_qdrant_client, [future])

        results = await store.recall("go-live", limit=5, as_of="2026-10-01")
        assert len(results) == 1

    async def test_recall_as_of_respects_valid_until_ttl_cutoff(
        self, store, mock_qdrant_client
    ):
        """TTL: valid_until compared against the as_of cutoff, not 'now'."""
        store._hybrid_retriever = None
        # A belief valid until 2026-09-10 — already 'expired' from today's
        # perspective would only apply after that date.
        belief = _hit(_payload(
            pid="belief-1",
            content="Team believes migration finishes in Q3",
            category="belief",
            created_at="2026-08-01T00:00:00+00:00",
            valid_until="2026-09-10T00:00:00+00:00",
        ))
        self._setup_store(store, mock_qdrant_client, [belief])

        # Before TTL expiry (in point-in-time terms) → returned
        results = await store.recall("migration", limit=5, as_of="2026-09-05")
        assert len(results) == 1
        # After TTL expiry relative to as_of → filtered
        results = await store.recall("migration", limit=5, as_of="2026-09-11")
        assert len(results) == 0

    async def test_recall_bad_as_of_falls_back_to_default_behavior(
        self, store, mock_qdrant_client
    ):
        """Garbage as_of must not crash and must behave like as_of=None."""
        store._hybrid_retriever = None
        old = _hit(_payload(
            pid="old-fact",
            content="Project uses Stripe",
            lifecycle_status="deprecated",
        ))
        self._setup_store(store, mock_qdrant_client, [old])

        results = await store.recall("stripe", limit=5, as_of="not-a-date")
        assert len(results) == 0, "unparseable as_of = default mode = deprecated filtered"

    async def test_recall_as_of_returns_canonical_fact(
        self, store, mock_qdrant_client
    ):
        """Canonical facts also pass the point-in-time window."""
        store._hybrid_retriever = None
        cur = _hit(_payload(
            pid="new-fact",
            content="Project uses Paddle",
            created_at="2026-06-01T00:00:00+00:00",
            valid_from="2026-06-01T00:00:00+00:00",
            valid_to=None,
        ))
        self._setup_store(store, mock_qdrant_client, [cur])

        results = await store.recall("paddle", limit=5, as_of="2026-07-01")
        assert len(results) == 1
        assert results[0]["id"] == "new-fact"

    async def test_recall_as_of_timezone_aware_string(self, store, mock_qdrant_client):
        """as_of with an explicit timezone (Berlin +02:00) is handled."""
        store._hybrid_retriever = None
        cur = _hit(_payload(
            pid="tz-fact",
            content="Berlin fact",
            created_at="2026-06-01T00:00:00+00:00",
            valid_from="2026-06-01T00:00:00+00:00",
            valid_to=None,
        ))
        self._setup_store(store, mock_qdrant_client, [cur])

        # 2026-07-01T12:00:00+02:00 == 2026-07-01T10:00:00+00:00 — after valid_from
        results = await store.recall("berlin", limit=5, as_of="2026-07-01T12:00:00+02:00")
        assert len(results) == 1

        # Earlier in UTC terms: 2026-05-31T12:00:00+02:00 == 2026-05-31T10:00:00+00:00
        results = await store.recall("berlin", limit=5, as_of="2026-05-31T12:00:00+02:00")
        assert len(results) == 0


# ===========================================================================
# (f) fact_history() — ordered supersession chain
# ===========================================================================


class TestFactHistory:
    def _make_store_with_points(self, points_by_id: dict):
        """Build a MemoryStore whose client serves retrieve() + scroll() from a dict."""
        from nexus_memory.mcp_server import MemoryStore

        real_store = MemoryStore.__new__(MemoryStore)
        real_store.client = MagicMock()
        real_store._embedder = MagicMock(dim=1024)
        real_store._skill_graph = None

        def _retrieve(collection_name, ids, with_payload=True, with_vectors=False):
            records = []
            for pid in ids:
                key = str(pid)
                if key in points_by_id:
                    rec = MagicMock()
                    rec.id = key
                    rec.payload = points_by_id[key]
                    records.append(rec)
            return records

        def _scroll(collection_name, scroll_filter=None, with_payload=True, limit=100):
            # Extract the superseded_by match value from the filter
            target = None
            try:
                must = scroll_filter.must
                for cond in must:
                    if getattr(cond, "key", None) == "superseded_by":
                        target = cond.match.value
            except Exception:
                pass
            records = []
            if target is not None:
                for pid, pl in points_by_id.items():
                    if str(pl.get("superseded_by")) == str(target):
                        rec = MagicMock()
                        rec.id = pid
                        rec.payload = pl
                        records.append(rec)
            return (records, None)  # Qdrant scroll returns (points, next_offset)

        real_store.client.retrieve.side_effect = _retrieve
        real_store.client.scroll.side_effect = _scroll
        return real_store

    async def test_fact_history_forward_and_backward_chain(self):
        """Chain a → b → c; starting from b must find a and c."""
        pl_a = _payload(
            pid="a", content="Uses Stripe", created_at="2026-01-01T00:00:00+00:00",
            lifecycle_status="deprecated", superseded_by="b",
            superseded_at="2026-02-01T00:00:00+00:00",
            valid_from="2026-01-01T00:00:00+00:00",
            valid_to="2026-02-01T00:00:00+00:00",
            supersede_reason="replaced by fact b",
        )
        pl_b = _payload(
            pid="b", content="Uses Adyen", created_at="2026-02-01T00:00:00+00:00",
            lifecycle_status="deprecated", superseded_by="c",
            superseded_at="2026-03-01T00:00:00+00:00",
            valid_from="2026-02-01T00:00:00+00:00",
            valid_to="2026-03-01T00:00:00+00:00",
            supersede_reason="replaced by fact c",
        )
        pl_c = _payload(
            pid="c", content="Uses Paddle", created_at="2026-03-01T00:00:00+00:00",
            lifecycle_status="canonical",
            valid_from="2026-03-01T00:00:00+00:00",
            valid_to=None,
        )

        store = self._make_store_with_points({"a": pl_a, "b": pl_b, "c": pl_c})
        chain = await store.fact_history("b")

        assert [e["memory_id"] for e in chain] == ["a", "b", "c"], (
            "chain must be ordered by valid_from, oldest first"
        )
        # Entry shape
        entry = chain[1]
        assert set(entry.keys()) == {
            "memory_id", "text", "valid_from", "valid_to", "supersede_reason"
        }
        assert chain[0]["supersede_reason"] == "replaced by fact b"
        assert chain[2]["valid_to"] is None  # current fact: open interval
        assert chain[0]["text"].startswith("Uses Stripe")

    async def test_fact_history_single_point_no_chain(self):
        """A point without supersession links → chain of exactly itself."""
        pl = _payload(pid="solo", content="Lone fact",
                      created_at="2026-01-01T00:00:00+00:00")
        store = self._make_store_with_points({"solo": pl})
        chain = await store.fact_history("solo")
        assert len(chain) == 1
        assert chain[0]["memory_id"] == "solo"
        assert chain[0]["valid_to"] is None

    async def test_fact_history_unknown_id_empty_chain(self):
        """Unknown memory_id → empty chain (no crash)."""
        store = self._make_store_with_points({})
        chain = await store.fact_history("does-not-exist")
        assert chain == []

    async def test_fact_history_text_truncated(self):
        long_text = "x" * 500
        pl = _payload(pid="long", content=long_text,
                      created_at="2026-01-01T00:00:00+00:00")
        store = self._make_store_with_points({"long": pl})
        chain = await store.fact_history("long")
        assert len(chain[0]["text"]) <= 121  # 120 chars + ellipsis

    async def test_fact_history_mcp_tool_envelope(self):
        """Dispatcher: fact_history returns JSON envelope with chain + count."""
        response = await mcp.handle_call_tool(
            "fact_history", {"memory_id": "abc-123"}
        )
        data = _json.loads(response[0].text)
        # The mocked client returns no records → empty chain, but envelope is well-typed
        assert "chain" in data
        assert "count" in data
        assert data["memory_id"] == "abc-123"
        assert data["count"] == len(data["chain"])

    def test_fact_history_tool_declared_in_schema(self):
        tools = asyncio.run(mcp.handle_list_tools())
        fact_history = next(t for t in tools if t.name == "fact_history")
        assert "memory_id" in fact_history.input_schema["required"]


# ===========================================================================
# (g) Backward compatibility — legacy points without temporal fields
# ===========================================================================


class TestLegacyBackwardCompat:
    async def test_legacy_point_survives_as_of_none(self, store, mock_qdrant_client):
        """Old point without valid_from/valid_to → passes default recall."""
        store._hybrid_retriever = None
        legacy = _hit(_payload(
            pid="legacy-1",
            content="Old point without temporal fields",
        ))
        # Strip temporal fields entirely
        legacy.payload.pop("valid_from", None)
        legacy.payload.pop("valid_to", None)
        mock_qdrant_client.query_points.return_value = MagicMock(points=[legacy])

        results = await store.recall("legacy", limit=5)
        assert len(results) == 1
        assert results[0]["id"] == "legacy-1"

    async def test_legacy_point_survives_as_of_query(self, store, mock_qdrant_client):
        """Old point (no temporal fields) → implicitly valid in point-in-time mode."""
        store._hybrid_retriever = None
        legacy = _hit(_payload(
            pid="legacy-1",
            content="Old point without temporal fields",
            created_at="2025-01-01T00:00:00+00:00",
        ))
        legacy.payload.pop("valid_from", None)
        legacy.payload.pop("valid_to", None)
        mock_qdrant_client.query_points.return_value = MagicMock(points=[legacy])

        results = await store.recall("legacy", limit=5, as_of="2026-06-01")
        assert len(results) == 1
        assert results[0]["id"] == "legacy-1"

    async def test_legacy_point_without_created_at_survives_both_modes(
        self, store, mock_qdrant_client
    ):
        """Even created_at missing → fully implicitly valid (no crash)."""
        store._hybrid_retriever = None
        legacy = _hit(_payload(pid="legacy-2", content="Ancient point"))
        legacy.payload.pop("valid_from", None)
        legacy.payload.pop("valid_to", None)
        legacy.payload.pop("created_at", None)
        mock_qdrant_client.query_points.return_value = MagicMock(points=[legacy])

        results_none = await store.recall("ancient", limit=5)
        assert len(results_none) == 1
        results_as_of = await store.recall("ancient", limit=5, as_of="2020-01-01")
        assert len(results_as_of) == 1

    async def test_deprecated_legacy_point_filtered_without_as_of(
        self, store, mock_qdrant_client
    ):
        """Legacy + deprecated (superseded_at only, no valid_to) → filtered in default mode."""
        store._hybrid_retriever = None
        legacy = _hit(_payload(
            pid="legacy-dep",
            content="Old deprecated point",
            lifecycle_status="deprecated",
            superseded_at="2026-01-01T00:00:00+00:00",
        ))
        legacy.payload.pop("valid_from", None)
        legacy.payload.pop("valid_to", None)
        mock_qdrant_client.query_points.return_value = MagicMock(points=[legacy])

        results = await store.recall("old", limit=5)
        assert len(results) == 0


# ===========================================================================
# MCP tool schema — as_of / effective_from declared
# ===========================================================================


class TestTemporalToolSchemas:
    def test_recall_schema_declares_as_of(self):
        tools = asyncio.run(mcp.handle_list_tools())
        recall = next(t for t in tools if t.name == "recall")
        assert "as_of" in recall.input_schema["properties"]
        assert recall.input_schema["properties"]["as_of"]["type"] == "string"

    def test_remember_schema_declares_effective_from(self):
        tools = asyncio.run(mcp.handle_list_tools())
        remember = next(t for t in tools if t.name == "remember")
        assert "effective_from" in remember.input_schema["properties"]
        assert "effective_from" not in remember.input_schema.get("required", [])

    def test_update_schema_declares_effective_from(self):
        tools = asyncio.run(mcp.handle_list_tools())
        update = next(t for t in tools if t.name == "update")
        assert "effective_from" in update.input_schema["properties"]

    def test_remember_dispatcher_passes_effective_from(
        self, store, mock_qdrant_client, monkeypatch
    ):
        """handle_call_tool('remember', {..., 'effective_from': ...}) → valid_from."""
        monkeypatch.setattr(mcp, "get_store", lambda: store)
        monkeypatch.setattr(mcp.handle_call_tool, "_skip_stats", True, raising=False)

        # handle_call_tool is async → drive it with asyncio.run
        response = asyncio.run(mcp.handle_call_tool(
            "remember",
            {"text": "Retro mail fact", "category": "fact",
             "effective_from": "2026-08-15T09:30:00+00:00"},
        ))
        payload = mock_qdrant_client.upsert.call_args.kwargs["points"][0].payload
        assert payload["valid_from"] == "2026-08-15T09:30:00+00:00"