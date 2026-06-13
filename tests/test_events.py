"""Tests for ``nexus/events.py`` — the bi-temporal event system.

``events.py`` exposes four user-facing functions:
- ``create_event(...)``  — append a new event to ``nexus_events``
- ``get_events(...)``    — fetch all events for one belief, chronological
- ``get_events_since(...)`` — system-wide events newer than a timestamp
- ``get_recent_events(...)`` — newest N events system-wide

Plus one internal helper (``_parse_event``) and two collection-management
functions (``ensure_collection``, ``verify_collection``).

The module talks to Qdrant via the ``requests`` library (not the Qdrant SDK),
so we mock ``requests.put`` / ``requests.post`` directly — the same pattern
used in ``test_nexus_init.py`` and ``test_staging.py``.

Note: like ``apply.py``, ``events.py`` references the **legacy** collection
name ``nexus_events`` as a hard-coded constant.  These tests accept that
legacy name and do *not* modify the source code.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from nexus import events as events_mod
from nexus.events import (
    COLLECTION,
    EVENT_TYPES,
    EventType,
    VECTOR_SIZE,
    create_event,
    ensure_collection,
    get_events,
    get_events_since,
    get_recent_events,
    verify_collection,
)


# ---------------------------------------------------------------------------
# Legacy collection-name guard
# ---------------------------------------------------------------------------


class TestLegacyEventsCollection:
    """Pin the legacy collection name — the rest of the tests rely on it."""

    def test_events_collection_is_legacy_name(self):
        assert COLLECTION == "nexus_events"

    def test_event_type_enum_has_six_values(self):
        assert len(EVENT_TYPES) == 6

    def test_event_type_enum_matches_documented_strings(self):
        assert {e.value for e in EventType} == {
            "belief_created",
            "belief_updated",
            "trust_changed",
            "status_changed",
            "belief_split",
            "user_override",
        }

    def test_vector_size_is_1024(self):
        # 1024d Cosine — must match nexus_beliefs.
        assert VECTOR_SIZE == 1024


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resp(status_code: int = 200, json_payload: dict | None = None):
    r = MagicMock()
    r.status_code = status_code
    r.text = "" if json_payload is None else json.dumps(json_payload)
    r.json.return_value = json_payload or {"result": {"points": [], "status": "ok"}}
    return r


def _event_point(
    event_id: str,
    event_type: str = "belief_created",
    belief_id: str = "bid-1",
    delta: dict | None = None,
    status: str = "ACTIVE",
    ingested_at: str = "2026-06-13T10:00:00+00:00",
    event_time: str | None = None,
):
    """Build a fake Qdrant point in the shape stored by create_event()."""
    return {
        "id": event_id,
        "vector": [0.0] * VECTOR_SIZE,
        "payload": {
            "event_id": event_id,
            "event_type": event_type,
            "belief_id": belief_id,
            "delta": json.dumps(delta or {}),
            "status": status,
            "ingested_at": ingested_at,
            "event_time": event_time or ingested_at,
        },
    }


# ---------------------------------------------------------------------------
# 1. create_event()
# ---------------------------------------------------------------------------


class TestCreateEvent:
    """``create_event()`` persists a single event to the legacy collection."""

    @patch("nexus.events.requests.put")
    def test_returns_event_id_on_success(self, mock_put):
        mock_put.return_value = _resp(200, {"result": {"status": "ok"}})

        eid = create_event("bid-1", "belief_created", delta={"fact": "x"})

        assert eid is not None
        assert isinstance(eid, str) and len(eid) == 36  # uuid4

    @patch("nexus.events.requests.put")
    def test_writes_to_legacy_events_collection(self, mock_put):
        """Belt-and-braces: the URL must hit ``nexus_events``."""
        mock_put.return_value = _resp(200, {"result": {"status": "ok"}})

        create_event("bid-1", "belief_created", delta={"fact": "x"})

        url = mock_put.call_args.args[0]
        assert "nexus_events" in url, (
            f"events.py must hit the legacy 'nexus_events' collection, got: {url}"
        )

    @patch("nexus.events.requests.put")
    def test_payload_has_required_fields(self, mock_put):
        mock_put.return_value = _resp(200)

        create_event(
            belief_id="bid-1",
            event_type="trust_changed",
            delta={"trust": {"from": 0.3, "to": 0.9}},
            status="ACTIVE",
        )

        body = mock_put.call_args.kwargs["json"]
        sent_point = body["points"][0]
        payload = sent_point["payload"]
        # Required fields per the contract
        assert payload["belief_id"] == "bid-1"
        assert payload["event_type"] == "trust_changed"
        # delta is JSON-stringified
        assert json.loads(payload["delta"]) == {"trust": {"from": 0.3, "to": 0.9}}
        assert payload["status"] == "ACTIVE"
        # Ingested_at is auto-populated to an ISO timestamp
        assert "ingested_at" in payload
        assert "T" in payload["ingested_at"]  # ISO format
        # Vector is the zero-vector (events carry no semantic content)
        assert sent_point["vector"] == [0.0] * VECTOR_SIZE

    @patch("nexus.events.requests.put")
    def test_accepts_custom_event_time(self, mock_put):
        """``event_time`` parameter overrides the default of "now"."""
        mock_put.return_value = _resp(200)
        custom_time = "2020-01-01T00:00:00+00:00"

        create_event(
            belief_id="bid-1",
            event_type="belief_updated",
            delta={"fact": "y"},
            event_time=custom_time,
        )

        payload = mock_put.call_args.kwargs["json"]["points"][0]["payload"]
        assert payload["event_time"] == custom_time

    @patch("nexus.events.requests.put")
    def test_returns_none_on_qdrant_failure(self, mock_put):
        mock_put.return_value = _resp(500, {"error": "nope"})

        eid = create_event("bid-1", "belief_created", delta={})

        assert eid is None


# ---------------------------------------------------------------------------
# 2. get_events()
# ---------------------------------------------------------------------------


class TestGetEvents:
    """``get_events()`` returns a single belief's audit trail."""

    @patch("nexus.events.requests.post")
    def test_returns_parsed_events_in_chronological_order(self, mock_post):
        # 3 events, intentionally out of order in storage → sorted by event_time.
        page = [
            _event_point("e-2", event_type="trust_changed", event_time="2026-06-13T12:00:00+00:00"),
            _event_point("e-1", event_type="belief_created", event_time="2026-06-13T10:00:00+00:00"),
            _event_point("e-3", event_type="status_changed", event_time="2026-06-13T14:00:00+00:00"),
        ]
        mock_post.return_value = _resp(200, {"result": {"points": page, "next_page_offset": None}})

        result = get_events("bid-1", limit=50)

        assert [e["event_id"] for e in result] == ["e-1", "e-2", "e-3"]

    @patch("nexus.events.requests.post")
    def test_query_filters_by_belief_id(self, mock_post):
        """The outgoing filter must include the belief_id we asked about."""
        mock_post.return_value = _resp(200, {"result": {"points": [], "next_page_offset": None}})

        get_events("bid-X", limit=10)

        body = mock_post.call_args.kwargs["json"]
        filter_block = body["filter"]
        must = filter_block["must"]
        # Exactly one must-clause matching the belief_id
        assert len(must) == 1
        assert must[0]["key"] == "belief_id"
        assert must[0]["match"]["value"] == "bid-X"

    @patch("nexus.events.requests.post")
    def test_empty_response_returns_empty_list(self, mock_post):
        mock_post.return_value = _resp(200, {"result": {"points": [], "next_page_offset": None}})

        result = get_events("nonexistent")

        assert result == []

    @patch("nexus.events.requests.post")
    def test_qdrant_failure_returns_empty_list(self, mock_post):
        mock_post.return_value = _resp(500)

        result = get_events("bid-1")

        # On error the function logs and returns whatever it has (empty here)
        assert result == []

    @patch("nexus.events.requests.post")
    def test_pagination_uses_next_page_offset(self, mock_post):
        page1 = [_event_point("a", event_time="2026-01-01T00:00:00+00:00")]
        page2 = [_event_point("b", event_time="2026-02-01T00:00:00+00:00")]
        mock_post.side_effect = [
            _resp(200, {"result": {"points": page1, "next_page_offset": "opq1"}}),
            _resp(200, {"result": {"points": page2, "next_page_offset": None}}),
        ]

        result = get_events("bid-1", limit=50, fetch_all=True)

        assert [e["event_id"] for e in result] == ["a", "b"]
        assert mock_post.call_count == 2

        # Second call should pass the offset
        second_body = mock_post.call_args_list[1].kwargs["json"]
        assert second_body.get("offset") == "opq1"


# ---------------------------------------------------------------------------
# 3. get_events_since()
# ---------------------------------------------------------------------------


class TestGetEventsSince:
    """``get_events_since()`` is the system-wide time-range query."""

    @patch("nexus.events.requests.post")
    def test_query_has_ingested_at_range_filter(self, mock_post):
        mock_post.return_value = _resp(200, {"result": {"points": [], "next_page_offset": None}})

        get_events_since("2026-06-01T00:00:00+00:00")

        body = mock_post.call_args.kwargs["json"]
        must = body["filter"]["must"]
        # Must have a range clause on ingested_at
        range_clauses = [c for c in must if c["key"] == "ingested_at"]
        assert len(range_clauses) == 1
        assert range_clauses[0]["range"]["gte"] == "2026-06-01T00:00:00+00:00"

    @patch("nexus.events.requests.post")
    def test_optional_event_type_filter_added(self, mock_post):
        mock_post.return_value = _resp(200, {"result": {"points": [], "next_page_offset": None}})

        get_events_since("2026-06-01T00:00:00+00:00", event_type="trust_changed")

        must = mock_post.call_args.kwargs["json"]["filter"]["must"]
        types = [c for c in must if c["key"] == "event_type"]
        assert len(types) == 1
        assert types[0]["match"]["value"] == "trust_changed"

    @patch("nexus.events.requests.post")
    def test_no_event_type_filter_when_omitted(self, mock_post):
        mock_post.return_value = _resp(200, {"result": {"points": [], "next_page_offset": None}})

        get_events_since("2026-06-01T00:00:00+00:00")

        must = mock_post.call_args.kwargs["json"]["filter"]["must"]
        # Only the ingested_at range, no event_type clause
        assert all(c["key"] != "event_type" for c in must)

    @patch("nexus.events.requests.post")
    def test_returns_parsed_events(self, mock_post):
        page = [_event_point("e1", event_type="trust_changed", belief_id="bid-1")]
        mock_post.return_value = _resp(200, {"result": {"points": page, "next_page_offset": None}})

        result = get_events_since("2026-06-01T00:00:00+00:00", limit=10)

        assert len(result) == 1
        assert result[0]["event_id"] == "e1"
        assert result[0]["event_type"] == "trust_changed"
        assert result[0]["delta"] == {}  # default empty dict

    @patch("nexus.events.requests.post")
    def test_qdrant_failure_returns_empty_list(self, mock_post):
        mock_post.return_value = _resp(500)

        result = get_events_since("2026-06-01T00:00:00+00:00")

        assert result == []


# ---------------------------------------------------------------------------
# 4. get_recent_events() — bonus coverage
# ---------------------------------------------------------------------------


class TestGetRecentEvents:
    """``get_recent_events()`` is the simple "newest N system-wide" query."""

    @patch("nexus.events.requests.post")
    def test_returns_events_sorted_by_ingested_at_desc(self, mock_post):
        # 3 events, out of order
        page = [
            _event_point("e-1", ingested_at="2026-06-13T10:00:00+00:00"),
            _event_point("e-3", ingested_at="2026-06-13T14:00:00+00:00"),
            _event_point("e-2", ingested_at="2026-06-13T12:00:00+00:00"),
        ]
        mock_post.return_value = _resp(200, {"result": {"points": page, "next_page_offset": None}})

        result = get_recent_events(limit=5)

        # Newest first
        assert [e["event_id"] for e in result] == ["e-3", "e-2", "e-1"]

    @patch("nexus.events.requests.post")
    def test_empty_when_qdrant_fails(self, mock_post):
        mock_post.return_value = _resp(500)

        result = get_recent_events(limit=10)

        assert result == []


# ---------------------------------------------------------------------------
# 5. ensure_collection() & verify_collection() — collection bootstrap
# ---------------------------------------------------------------------------


class TestEnsureCollection:
    """``ensure_collection()`` is idempotent: no PUT if the collection exists."""

    @patch("nexus.events.requests.put")
    @patch("nexus.events.requests.get")
    def test_noop_when_collection_exists(self, mock_get, mock_put):
        mock_get.return_value = _resp(200, {"result": {"name": "nexus_events"}})

        result = ensure_collection()

        assert result is True
        # No PUT should be issued if the collection is already there
        mock_put.assert_not_called()

    @patch("nexus.events.requests.put")
    @patch("nexus.events.requests.get")
    def test_creates_collection_when_missing(self, mock_get, mock_put):
        # First GET: collection missing.  Subsequent PUTs (create + indexes).
        mock_get.return_value = _resp(404)
        mock_put.return_value = _resp(201, {"result": {"status": "ok"}})

        result = ensure_collection()

        assert result is True
        # At least one PUT — the collection creation itself.
        assert mock_put.call_count >= 1
        # The creation PUT should specify 1024d Cosine.
        create_call = mock_put.call_args_list[0]
        body = create_call.kwargs["json"]
        assert body["vectors"]["size"] == 1024
        assert body["vectors"]["distance"] == "Cosine"
        assert body["name"] == "nexus_events"

    @patch("nexus.events.requests.put")
    @patch("nexus.events.requests.get")
    def test_returns_false_on_create_failure(self, mock_get, mock_put):
        mock_get.return_value = _resp(404)
        mock_put.return_value = _resp(500, {"error": "boom"})

        result = ensure_collection()

        assert result is False


class TestVerifyCollection:
    """``verify_collection()`` reports existence + point count + index count."""

    @patch("nexus.events.requests.get")
    def test_returns_exists_false_on_404(self, mock_get):
        mock_get.return_value = _resp(404)

        info = verify_collection()

        assert info == {"exists": False, "points": 0, "indexes": 0}

    @patch("nexus.events.requests.get")
    def test_returns_metadata_when_present(self, mock_get):
        mock_get.return_value = _resp(
            200,
            {
                "result": {
                    "points_count": 42,
                    "payload_schema": {
                        "event_id": {"type": "keyword"},
                        "event_type": {"type": "keyword"},
                    },
                }
            },
        )

        info = verify_collection()

        assert info["exists"] is True
        assert info["points"] == 42
        assert info["indexes"] == 2


# ---------------------------------------------------------------------------
# 6. _parse_event() — defensive parsing of stored payload
# ---------------------------------------------------------------------------


class TestParseEvent:
    """The internal parser must handle corrupt JSON gracefully."""

    def test_parses_stringified_delta(self):
        p = _event_point("e1", delta={"trust": 0.9})
        result = events_mod._parse_event(p)
        assert result["delta"] == {"trust": 0.9}
        assert result["event_id"] == "e1"
        assert result["event_type"] == "belief_created"
        assert result["belief_id"] == "bid-1"
        assert result["status"] == "ACTIVE"

    def test_handles_corrupt_delta_gracefully(self):
        """If the JSON in ``delta`` is malformed, fall back to ``{}``."""
        p = {
            "id": "e1",
            "payload": {
                "event_id": "e1",
                "event_type": "x",
                "belief_id": "b",
                "delta": "{not valid json",
                "status": "",
                "ingested_at": "t",
                "event_time": "t",
            },
        }
        result = events_mod._parse_event(p)
        assert result["delta"] == {}

    def test_handles_dict_delta_passthrough(self):
        """If delta is already a dict (not stringified), use it as-is."""
        p = {
            "id": "e1",
            "payload": {
                "event_id": "e1",
                "event_type": "x",
                "belief_id": "b",
                "delta": {"trust": 0.5},
                "status": "ACTIVE",
                "ingested_at": "t",
                "event_time": "t",
            },
        }
        result = events_mod._parse_event(p)
        assert result["delta"] == {"trust": 0.5}
