"""Tests for ``nexus/apply.py`` — belief resolution, apply_delta, user_override,
recompute_trust, and recompute_all.

These tests cover the *apply-API* of the belief system.  ``apply.py`` talks to
Qdrant via plain ``requests`` calls (not the Qdrant SDK), so we mock
``requests.put`` / ``requests.post`` directly.  This matches the existing
mocking style in ``test_nexus_init.py`` and ``test_staging.py``.

Important: ``apply.py`` references the **legacy** collections
``nexus_beliefs`` and ``nexus_events`` as hard-coded constants.  These do not
exist in the current default deployment (the default is ``hermes-memory``),
but we treat that as legacy behaviour and *accept / mock* the legacy names
rather than modifying the source.  See AGENTS.md — the GitHub default
collection name stays ``hermes-memory``; legacy ``nexus_beliefs`` /
``nexus_events`` are only kept for backward compatibility.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from nexus import apply as apply_mod
from nexus import events as events_mod
from nexus.apply import (
    BELIEFS_COLLECTION,
    EVENTS_COLLECTION,
    STATUS_ACTIVE,
    STATUS_CONTESTED,
    TRUST_EPSILON,
    apply_delta,
    recompute_all,
    recompute_trust,
    resolve_belief,
    user_override,
)
from nexus.events import COLLECTION as LEGACY_EVENTS_COLLECTION


# ---------------------------------------------------------------------------
# Sanity guards: confirm we are mocking the right legacy collection names.
# If someone ever changes the constants in apply.py / events.py these tests
# should be updated *consciously* (rather than silently continuing to pass).
# ---------------------------------------------------------------------------


class TestLegacyCollectionConstants:
    """Pin the legacy collection names — the tests below depend on them."""

    def test_beliefs_collection_is_legacy_name(self):
        assert BELIEFS_COLLECTION == "nexus_beliefs"

    def test_events_collection_is_legacy_name(self):
        assert EVENTS_COLLECTION == "nexus_events"

    def test_events_module_collection_is_legacy_name(self):
        assert LEGACY_EVENTS_COLLECTION == "nexus_events"


# ---------------------------------------------------------------------------
# Helpers — build a fake Qdrant point + queue of responses
# ---------------------------------------------------------------------------


def _fake_resp(status_code: int = 200, json_payload: dict | None = None):
    """Return a MagicMock that mimics a ``requests.Response``."""
    r = MagicMock()
    r.status_code = status_code
    r.text = "" if json_payload is None else str(json_payload)
    r.json.return_value = json_payload or {"result": {"points": [], "status": "ok"}}
    return r


def _point(
    belief_id: str,
    content: str = "x",
    trust: float = 0.5,
    *,
    payload: dict | None = None,
    **extras,
):
    """Build a fake Qdrant point matching the shape ``_get_belief`` returns.

    Pass keyword args to override any of the default payload fields.
    Alternatively pass a fully-formed ``payload=`` dict to start from.
    """
    base = {
        "belief_id": belief_id,
        "content": content,
        "status": STATUS_ACTIVE,
        "trust": trust,
        "source": "manual",
        "rationale": "",
        "evidences": [],
        "provenance_trail": [],
        "explicitly_set": False,
    }
    if payload is not None:
        base.update(payload)
    base.update(extras)
    return {"id": belief_id, "vector": [0.0] * 1024, "payload": base}


# ---------------------------------------------------------------------------
# 1. resolve_belief()
# ---------------------------------------------------------------------------


class TestResolveBelief:
    """``resolve_belief()`` is the create-or-find entry point."""

    @patch("nexus.apply.create_event")
    @patch("nexus.apply.requests.put")
    @patch("nexus.apply.requests.post")
    def test_creates_new_belief_when_not_found(self, mock_post, mock_put, mock_evt):
        """First call → no existing point → creates one, returns belief_id."""
        # _find_by_fact scroll returns no points
        mock_post.return_value = _fake_resp(200, {"result": {"points": []}})
        mock_put.return_value = _fake_resp(200, {"result": {"status": "ok"}})

        result = resolve_belief("The sky is blue", source="test", trust=0.7)

        assert result["created"] is True
        assert result["status"] == STATUS_ACTIVE
        assert result["trust"] == 0.7
        assert "belief_id" in result and len(result["belief_id"]) == 36

        # PUT was used to insert the new point
        assert mock_put.call_count >= 1
        # And a belief_created event was fired
        mock_evt.assert_called_once()
        assert mock_evt.call_args.kwargs["event_type"] == "belief_created"

    @patch("nexus.apply.create_event")
    @patch("nexus.apply.requests.put")
    @patch("nexus.apply.requests.post")
    def test_returns_existing_belief_when_found(self, mock_post, mock_put, mock_evt):
        """Second call with the same fact → returns the existing belief, no PUT."""
        existing_payload = {
            "belief_id": "abc-123",
            "content": "The sky is blue",
            "status": STATUS_ACTIVE,
            "trust": 0.8,
        }
        mock_post.return_value = _fake_resp(
            200, {"result": {"points": [{"payload": existing_payload}]}}
        )

        result = resolve_belief("The sky is blue")

        assert result["created"] is False
        assert result["belief_id"] == "abc-123"
        assert result["trust"] == 0.8
        # No PUT (no new write), no event
        mock_put.assert_not_called()
        mock_evt.assert_not_called()

    @patch("nexus.apply.create_event")
    @patch("nexus.apply.requests.put")
    @patch("nexus.apply.requests.post")
    def test_create_failure_returns_error(self, mock_post, mock_put, mock_evt):
        """If Qdrant rejects the PUT, return an error dict (no event)."""
        mock_post.return_value = _fake_resp(200, {"result": {"points": []}})
        mock_put.return_value = _fake_resp(500, {"error": "nope"})

        result = resolve_belief("Will fail", trust=0.5)

        assert result.get("error") is True
        assert result.get("belief_id") is None
        mock_evt.assert_not_called()

    @patch("nexus.apply.create_event")
    @patch("nexus.apply.requests.put")
    @patch("nexus.apply.requests.post")
    def test_resolve_uses_legacy_beliefs_collection(self, mock_post, mock_put, mock_evt):
        """Belt-and-braces: the URL written to must contain the legacy name."""
        mock_post.return_value = _fake_resp(200, {"result": {"points": []}})
        mock_put.return_value = _fake_resp(200, {"result": {"status": "ok"}})

        resolve_belief("A fact", source="s")

        url = mock_put.call_args.args[0]
        assert "nexus_beliefs" in url, (
            f"apply.py must hit the legacy 'nexus_beliefs' collection, got: {url}"
        )


# ---------------------------------------------------------------------------
# 2. apply_delta()
# ---------------------------------------------------------------------------


class TestApplyDelta:
    """``apply_delta()`` mutates a belief and emits an event."""

    def _setup_get(self, mock_post, payload):
        """Mock ``_get_belief`` → returns a point with the given payload."""
        mock_post.return_value = _fake_resp(
            200, {"result": {"points": [_point("bid-1", payload=payload)]}}
        )

    @patch("nexus.apply.create_event")
    @patch("nexus.apply.requests.put")
    @patch("nexus.apply.requests.post")
    def test_changes_trust_and_emits_trust_event(self, mock_post, mock_put, mock_evt):
        self._setup_get(mock_post, {"trust": 0.3, "status": STATUS_ACTIVE})
        mock_put.return_value = _fake_resp(200)

        result = apply_delta("bid-1", {"trust": 0.9})

        assert result["changed"] is True
        assert "trust" in result["fields"]
        # event_type should be "trust_changed" (only trust, not status)
        evt_kwargs = mock_evt.call_args.kwargs
        assert evt_kwargs["event_type"] == "trust_changed"

    @patch("nexus.apply.create_event")
    @patch("nexus.apply.requests.put")
    @patch("nexus.apply.requests.post")
    def test_status_change_emits_status_event(self, mock_post, mock_put, mock_evt):
        self._setup_get(mock_post, {"trust": 0.5, "status": STATUS_ACTIVE})
        mock_put.return_value = _fake_resp(200)

        result = apply_delta("bid-1", {"status": STATUS_CONTESTED})

        assert "status" in result["fields"]
        assert mock_evt.call_args.kwargs["event_type"] == "status_changed"

    @patch("nexus.apply.create_event")
    @patch("nexus.apply.requests.put")
    @patch("nexus.apply.requests.post")
    def test_explicitly_set_blocks_trust_and_status_overwrite(
        self, mock_post, mock_put, mock_evt
    ):
        """If a field was set via user_override, apply_delta must skip it."""
        self._setup_get(
            mock_post,
            {
                "trust": 0.99,
                "status": STATUS_ACTIVE,
                "explicitly_set": True,
            },
        )
        mock_put.return_value = _fake_resp(200)

        result = apply_delta("bid-1", {"trust": 0.1, "status": STATUS_CONTESTED})

        # No field actually changed
        assert result["changed"] is False
        # Both protected fields reported as overrides
        assert set(result["overrides"]) == {"trust", "status"}

    @patch("nexus.apply.create_event")
    @patch("nexus.apply.requests.put")
    @patch("nexus.apply.requests.post")
    def test_unknown_keys_are_ignored(self, mock_post, mock_put, mock_evt):
        self._setup_get(mock_post, {"trust": 0.5, "status": STATUS_ACTIVE})
        mock_put.return_value = _fake_resp(200)

        result = apply_delta("bid-1", {"random_field": "x", "trust": 0.6})

        # random_field is ignored, trust is changed
        assert result["fields"] == ["trust"]
        assert "random_field" not in result["fields"]

    @patch("nexus.apply.requests.post")
    def test_missing_belief_returns_error(self, mock_post):
        mock_post.return_value = _fake_resp(200, {"result": {"points": []}})

        result = apply_delta("does-not-exist", {"trust": 0.9})

        assert result.get("error") is True
        assert "not found" in result.get("message", "").lower()

    @patch("nexus.apply.create_event")
    @patch("nexus.apply.requests.put")
    @patch("nexus.apply.requests.post")
    def test_no_change_when_value_identical(self, mock_post, mock_put, mock_evt):
        """Setting a field to its current value → changed=False, no event."""
        self._setup_get(mock_post, {"trust": 0.5, "status": STATUS_ACTIVE})
        mock_put.return_value = _fake_resp(200)

        result = apply_delta("bid-1", {"trust": 0.5})

        assert result["changed"] is False
        mock_evt.assert_not_called()


# ---------------------------------------------------------------------------
# 3. user_override()
# ---------------------------------------------------------------------------


class TestUserOverride:
    """``user_override()`` sets a field manually and locks it (explicitly_set=True)."""

    @patch("nexus.apply.create_event")
    @patch("nexus.apply.requests.put")
    @patch("nexus.apply.requests.post")
    def test_sets_field_and_marks_explicitly_set(self, mock_post, mock_put, mock_evt):
        mock_post.return_value = _fake_resp(
            200,
            {
                "result": {
                    "points": [
                        _point(
                            "bid-1",
                            trust=0.3,
                            explicitly_set=False,
                        )
                    ]
                }
            },
        )
        mock_put.return_value = _fake_resp(200)

        result = user_override("bid-1", "trust", 0.95)

        assert result["field"] == "trust"
        assert result["old"] == 0.3
        assert result["new"] == 0.95
        # Event must be of type "user_override"
        assert mock_evt.call_args.kwargs["event_type"] == "user_override"

    @patch("nexus.apply.requests.post")
    def test_missing_belief_returns_error(self, mock_post):
        mock_post.return_value = _fake_resp(200, {"result": {"points": []}})

        result = user_override("nope", "trust", 0.5)

        assert result.get("error") is True

    @patch("nexus.apply.create_event")
    @patch("nexus.apply.requests.put")
    @patch("nexus.apply.requests.post")
    def test_override_appends_to_provenance_trail(self, mock_post, mock_put, mock_evt):
        """The provenance_trail in the persisted payload should grow."""
        mock_post.return_value = _fake_resp(
            200,
            {
                "result": {
                    "points": [
                        _point(
                            "bid-1",
                            trust=0.3,
                            provenance_trail=[],
                        )
                    ]
                }
            },
        )
        mock_put.return_value = _fake_resp(200)

        user_override("bid-1", "trust", 0.99)

        # Inspect the body that was sent to Qdrant
        sent_points = mock_put.call_args.kwargs["json"]["points"]
        trail = sent_points[0]["payload"]["provenance_trail"]
        assert any("user_override:trust:0.99" == entry for entry in trail)


# ---------------------------------------------------------------------------
# 4. recompute_trust()
# ---------------------------------------------------------------------------


class TestRecomputeTrust:
    """``recompute_trust()`` derives a new trust from evidence contributions."""

    @patch("nexus.apply.create_event")
    @patch("nexus.apply.requests.put")
    @patch("nexus.apply.requests.post")
    def test_max_aggregation_over_evidence(self, mock_post, mock_put, mock_evt):
        evidences = [
            {"trust_contribution": 0.3},
            {"trust_contribution": 0.7},
            {"trust_contribution": 0.5},
        ]
        mock_post.return_value = _fake_resp(
            200,
            {
                "result": {
                    "points": [
                        _point("bid-1", trust=0.4, evidences=evidences),
                    ]
                }
            },
        )
        mock_put.return_value = _fake_resp(200)

        result = recompute_trust("bid-1")

        # max of [0.3, 0.7, 0.5] == 0.7; old trust was 0.4
        assert result["changed"] is True
        assert result["trust"] == 0.7
        assert result["from"] == 0.4

    @patch("nexus.apply.create_event")
    @patch("nexus.apply.requests.put")
    @patch("nexus.apply.requests.post")
    def test_skips_when_within_epsilon(self, mock_post, mock_put, mock_evt):
        """If |new - old| < TRUST_EPSILON, no write happens."""
        # Use a delta *strictly smaller* than the epsilon (0.01).
        evidences = [{"trust_contribution": 0.503}]  # old=0.5, delta=0.003 < 0.01
        mock_post.return_value = _fake_resp(
            200,
            {"result": {"points": [_point("bid-1", trust=0.5, evidences=evidences)]}},
        )

        result = recompute_trust("bid-1")

        assert result["changed"] is False
        # No PUT (within eps), no event
        mock_put.assert_not_called()
        mock_evt.assert_not_called()

    @patch("nexus.apply.requests.post")
    def test_explicitly_set_blocks_recompute(self, mock_post):
        """user_override() locks the field → recompute must skip it."""
        mock_post.return_value = _fake_resp(
            200,
            {
                "result": {
                    "points": [
                        _point(
                            "bid-1",
                            trust=0.99,
                            explicitly_set=True,
                            evidences=[{"trust_contribution": 0.1}],
                        )
                    ]
                }
            },
        )

        result = recompute_trust("bid-1")

        assert result["skipped"] is True
        assert result["reason"] == "user_override"
        # Trust is preserved at the locked value
        assert result["trust"] == 0.99

    @patch("nexus.apply.requests.post")
    def test_no_evidence_skipped(self, mock_post):
        """Empty evidence list → skip, no change."""
        mock_post.return_value = _fake_resp(
            200,
            {
                "result": {
                    "points": [
                        _point("bid-1", trust=0.5, evidences=[]),
                    ]
                }
            },
        )

        result = recompute_trust("bid-1")

        assert result["skipped"] is True
        assert result["reason"] == "no_evidence"

    @patch("nexus.apply.requests.post")
    def test_missing_belief_returns_error(self, mock_post):
        mock_post.return_value = _fake_resp(200, {"result": {"points": []}})

        result = recompute_trust("ghost")

        assert result.get("error") is True


# ---------------------------------------------------------------------------
# 5. recompute_all()
# ---------------------------------------------------------------------------


class TestRecomputeAll:
    """``recompute_all()`` is a batched full-scan over all beliefs."""

    @patch("nexus.apply.requests.put")
    @patch("nexus.apply.requests.post")
    def test_counts_changed_skipped_and_overrides(self, mock_post, mock_put):
        """A page with one changed, one override, one no-evidence belief."""
        # Build a single page with three beliefs
        page_points = [
            # 1. Will change: trust 0.2, evidence contributes 0.8
            {
                "id": "b-change",
                "payload": {
                    "belief_id": "b-change",
                    "trust": 0.2,
                    "evidences": [{"trust_contribution": 0.8}],
                    "explicitly_set": False,
                },
            },
            # 2. Override-locked → counted as override, skipped
            {
                "id": "b-override",
                "payload": {
                    "belief_id": "b-override",
                    "trust": 0.99,
                    "evidences": [{"trust_contribution": 0.1}],
                    "explicitly_set": True,
                },
            },
            # 3. No evidence → skipped
            {
                "id": "b-empty",
                "payload": {
                    "belief_id": "b-empty",
                    "trust": 0.5,
                    "evidences": [],
                    "explicitly_set": False,
                },
            },
        ]

        # First scroll → returns the page, no next offset → loop exits.
        mock_post.return_value = _fake_resp(
            200,
            {"result": {"points": page_points, "next_page_offset": None}},
        )
        mock_put.return_value = _fake_resp(200)

        stats = recompute_all()

        assert stats["total"] == 3
        assert stats["changed"] == 1
        # Override counted as skipped AND as override
        assert stats["overrides"] == 1
        # override (1) + no-evidence (1) = 2 skipped
        assert stats["skipped"] == 2
        assert stats["errors"] == 0

    @patch("nexus.apply.requests.put")
    @patch("nexus.apply.requests.post")
    def test_paginates_with_next_page_offset(self, mock_post, mock_put):
        """Two pages, then a third with no next_offset → terminates."""
        page_1 = [
            {
                "id": "a",
                "payload": {
                    "belief_id": "a",
                    "trust": 0.0,
                    "evidences": [{"trust_contribution": 0.9}],
                    "explicitly_set": False,
                },
            }
        ]
        page_2 = [
            {
                "id": "b",
                "payload": {
                    "belief_id": "b",
                    "trust": 0.0,
                    "evidences": [{"trust_contribution": 0.7}],
                    "explicitly_set": False,
                },
            }
        ]
        empty = []

        # First two calls return pages with next_offset; third returns empty.
        mock_post.side_effect = [
            _fake_resp(200, {"result": {"points": page_1, "next_page_offset": "opaque1"}}),
            _fake_resp(200, {"result": {"points": page_2, "next_page_offset": "opaque2"}}),
            _fake_resp(200, {"result": {"points": empty, "next_page_offset": None}}),
        ]
        mock_put.return_value = _fake_resp(200)

        stats = recompute_all()

        assert stats["total"] == 2
        assert stats["changed"] == 2
        # Scroll was called 3 times (2 pages + terminator), and PUT twice
        assert mock_post.call_count == 3
        assert mock_put.call_count == 2

    @patch("nexus.apply.requests.post")
    def test_scroll_failure_increments_errors(self, mock_post):
        mock_post.return_value = _fake_resp(500)

        stats = recompute_all()

        assert stats["errors"] == 1
        # No PUTs attempted when the first scroll failed
        assert stats["total"] == 0

    @patch("nexus.apply.requests.put")
    @patch("nexus.apply.requests.post")
    def test_batch_update_failure_counts_errors(self, mock_post, mock_put):
        """If the batch PUT fails, every queued belief is counted as an error."""
        page_points = [
            {
                "id": "x",
                "payload": {
                    "belief_id": "x",
                    "trust": 0.0,
                    "evidences": [{"trust_contribution": 0.9}],
                    "explicitly_set": False,
                },
            },
            {
                "id": "y",
                "payload": {
                    "belief_id": "y",
                    "trust": 0.0,
                    "evidences": [{"trust_contribution": 0.6}],
                    "explicitly_set": False,
                },
            },
        ]
        mock_post.return_value = _fake_resp(
            200, {"result": {"points": page_points, "next_page_offset": None}}
        )
        mock_put.return_value = _fake_resp(500)

        stats = recompute_all()

        assert stats["total"] == 2
        assert stats["errors"] == 2


# ---------------------------------------------------------------------------
# 6. Cross-module wiring — apply.py emits events via events.create_event
# ---------------------------------------------------------------------------


class TestApplyEmitsEvents:
    """``apply.py`` must call into ``events.create_event`` for every mutation."""

    @patch("nexus.apply.create_event")
    @patch("nexus.apply.requests.put")
    @patch("nexus.apply.requests.post")
    def test_apply_delta_calls_events_module(self, mock_post, mock_put, mock_evt):
        """apply_delta must go through nexus.events.create_event (not requests)."""
        mock_post.return_value = _fake_resp(
            200,
            {
                "result": {
                    "points": [
                        _point("bid-1", trust=0.2, status=STATUS_ACTIVE),
                    ]
                }
            },
        )
        mock_put.return_value = _fake_resp(200)

        apply_delta("bid-1", {"trust": 0.8})

        # The patched symbol nexus.apply.create_event must have been called
        assert mock_evt.call_count == 1
        # And it must have been passed the belief_id we updated
        assert mock_evt.call_args.kwargs["belief_id"] == "bid-1"

    def test_apply_module_imports_events_correctly(self):
        """Smoke: apply.py binds create_event from nexus.events at import time."""
        # If apply.py did ``from nexus import create_event`` (no submodule),
        # this would fail.  We verify the *module path* is correct.
        assert hasattr(apply_mod, "create_event")
        # And the same object is reachable from nexus.events
        assert apply_mod.create_event is events_mod.create_event
