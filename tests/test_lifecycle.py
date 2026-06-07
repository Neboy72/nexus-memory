"""Tests for nexus/lifecycle.py — FactVersion State Machine + DecisionEvent.

All tests are Qdrant-free — they test the data model and state
transitions in memory only.
"""

import hashlib
import json
from datetime import datetime, timezone

from nexus.lifecycle import (
    FactStatus,
    FactVersion,
    CanonicalView,
    DecisionEvent,
    DECISION_PROMOTE,
    DECISION_DEPRECATE,
    DECISION_ROLLBACK,
)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _make_pending(content: dict = None, **kwargs) -> FactVersion:
    if content is None:
        content = {"text": "test-fact"}
    return FactVersion.new_pending(content=content, **kwargs)


def _hash_of(content: dict) -> str:
    return hashlib.sha256(
        json.dumps(content, sort_keys=True, default=str).encode()
    ).hexdigest()


# ─── FactVersion.new_pending() ──────────────────────────────────────────────

class TestNewPending:
    def test_creates_pending_status(self):
        v = _make_pending()
        assert v.status == FactStatus.PENDING.value

    def test_auto_generates_ids(self):
        v = _make_pending()
        assert v.fact_id
        assert v.version_id
        assert v.fact_id != v.version_id

    def test_content_hash_locked_at_creation(self):
        v = _make_pending({"name": "hello"})
        expected = _hash_of({"name": "hello"})
        assert v.content_hash == expected

    def test_no_decision_event_on_pending(self):
        v = _make_pending()
        assert v.decision_event is None

    def test_custom_fact_id(self):
        fid = "custom-fact-001"
        v = _make_pending(fact_id=fid)
        assert v.fact_id == fid

    def test_supersedes_stored(self):
        v = _make_pending(supersedes="abc-123")
        assert v.supersedes == "abc-123"

    def test_default_supersedes_is_none(self):
        v = _make_pending()
        assert v.supersedes is None

    def test_ttl_stored(self):
        v = _make_pending(ttl=30)
        assert v.ttl == 30

    def test_ttl_default_none(self):
        v = _make_pending()
        assert v.ttl is None

    def test_timestamps_set(self):
        v = _make_pending()
        assert v.created_at
        assert v.updated_at
        assert v.created_at == v.updated_at


# ─── FactVersion.promote() ──────────────────────────────────────────────────

class TestPromote:
    def test_promote_creates_canonical(self):
        p = _make_pending({"text": "hello"})
        c = FactVersion.promote(p)
        assert c.status == FactStatus.CANONICAL.value

    def test_promote_frozen_content(self):
        p = _make_pending({"text": "hello"})
        c = FactVersion.promote(p)
        assert c.content == p.content
        assert c.content_hash == p.content_hash

    def test_promote_has_decision_event(self):
        p = _make_pending({"text": "hello"})
        c = FactVersion.promote(p, reason="verified test")
        assert c.decision_event is not None
        assert c.decision_event.type == DECISION_PROMOTE
        assert c.decision_event.reason == "verified test"

    def test_promote_default_supersedes_pending(self):
        p = _make_pending({"text": "hello"})
        c = FactVersion.promote(p)
        assert c.supersedes == p.version_id

    def test_promote_custom_supersedes(self):
        p = _make_pending({"text": "hello"})
        c = FactVersion.promote(p, supersedes="prev-version")
        assert c.supersedes == "prev-version"

    def test_promote_new_version_id(self):
        p = _make_pending({"text": "hello"})
        c = FactVersion.promote(p)
        assert c.version_id != p.version_id
        assert c.fact_id == p.fact_id

    def test_promote_keeps_ttl(self):
        p = _make_pending({"text": "hello"}, ttl=45)
        c = FactVersion.promote(p)
        assert c.ttl == 45

    def test_promote_rejects_non_pending(self):
        p = _make_pending({"text": "hello"})
        c = FactVersion.promote(p)
        try:
            FactVersion.promote(c)
            assert False, "Should reject promote of canonical"
        except AssertionError:
            pass

    def test_promote_rejects_content_drift(self):
        p = _make_pending({"text": "original"})
        p.content = {"text": "tampered"}  # Simulate payload drift
        try:
            FactVersion.promote(p)
            assert False, "Should reject promote with drifted content"
        except AssertionError:
            pass

    def test_promote_rejects_content_drift_when_explicit(self):
        p = _make_pending({"text": "hello"})
        p.content["extra"] = "should not be here"  # Mutate after creation
        try:
            FactVersion.promote(p)
            assert False, "Should reject promote with mutated content"
        except AssertionError:
            pass


# ─── FactVersion.deprecate() ────────────────────────────────────────────────

class TestDeprecate:
    def test_deprecate_from_canonical(self):
        p = _make_pending({"text": "hello"})
        c = FactVersion.promote(p)
        d = FactVersion.deprecate(c)
        assert d.status == FactStatus.DEPRECATED.value

    def test_deprecate_supersedes_original(self):
        p = _make_pending({"text": "hello"})
        c = FactVersion.promote(p)
        d = FactVersion.deprecate(c)
        assert d.supersedes == c.version_id

    def test_deprecate_ttl_is_none(self):
        """Deprecated facts must survive as history — no TTL."""
        p = _make_pending({"text": "hello"})
        c = FactVersion.promote(p)
        d = FactVersion.deprecate(c)
        assert d.ttl is None

    def test_deprecate_has_decision_event(self):
        p = _make_pending({"text": "hello"})
        c = FactVersion.promote(p)
        d = FactVersion.deprecate(c, reason="outdated")
        assert d.decision_event.type == DECISION_DEPRECATE
        assert d.decision_event.reason == "outdated"

    def test_deprecate_new_version_id(self):
        p = _make_pending({"text": "hello"})
        c = FactVersion.promote(p)
        d = FactVersion.deprecate(c)
        assert d.version_id != c.version_id

    def test_deprecate_from_pending(self):
        p = _make_pending({"text": "hello"})
        d = FactVersion.deprecate(p)
        assert d.status == FactStatus.DEPRECATED.value

    def test_deprecate_rejects_deprecated(self):
        p = _make_pending({"text": "hello"})
        c = FactVersion.promote(p)
        d = FactVersion.deprecate(c)
        try:
            FactVersion.deprecate(d)
            assert False, "Should reject deprecate of already deprecated"
        except AssertionError:
            pass

    def test_deprecate_rejects_rolled_back(self):
        p = _make_pending({"text": "hello"})
        c = FactVersion.promote(p)
        rb, _rv = FactVersion.rollback(c, p)
        try:
            FactVersion.deprecate(rb)
            assert False, "Should reject deprecate of rolled_back"
        except AssertionError:
            pass


# ─── FactVersion.rollback() ─────────────────────────────────────────────────

class TestRollback:
    def test_creates_two_versions(self):
        p = _make_pending({"text": "v1"})
        v1 = FactVersion.promote(p)
        v2p = _make_pending({"text": "v2"}, fact_id=v1.fact_id,
                            supersedes=v1.version_id)
        v2 = FactVersion.promote(v2p)
        rb, rv = FactVersion.rollback(v2, v1)
        assert rb.status == FactStatus.ROLLED_BACK.value
        assert rv.status == FactStatus.CANONICAL.value

    def test_rolled_back_supersedes_bad_version(self):
        p = _make_pending({"text": "v1"})
        v1 = FactVersion.promote(p)
        v2p = _make_pending({"text": "v2"}, fact_id=v1.fact_id,
                            supersedes=v1.version_id)
        v2 = FactVersion.promote(v2p)
        rb, _rv = FactVersion.rollback(v2, v1)
        assert rb.supersedes == v2.version_id
        assert rb.fact_id == v2.fact_id

    def test_restored_supersedes_restore_target(self):
        p = _make_pending({"text": "v1"})
        v1 = FactVersion.promote(p)
        v2p = _make_pending({"text": "v2"}, fact_id=v1.fact_id,
                            supersedes=v1.version_id)
        v2 = FactVersion.promote(v2p)
        _rb, rv = FactVersion.rollback(v2, v1)
        assert rv.supersedes == v1.version_id

    def test_restored_content_equals_restore_target(self):
        p = _make_pending({"text": "v1-content"})
        v1 = FactVersion.promote(p)
        v2p = _make_pending({"text": "v2-content"}, fact_id=v1.fact_id,
                            supersedes=v1.version_id)
        v2 = FactVersion.promote(v2p)
        _rb, rv = FactVersion.rollback(v2, v1)
        assert rv.content == v1.content
        assert rv.content_hash == v1.content_hash

    def test_rollback_ttl_none_for_rolled_back(self):
        p = _make_pending({"text": "v1"})
        v1 = FactVersion.promote(p)
        v2p = _make_pending({"text": "v2"}, fact_id=v1.fact_id,
                            supersedes=v1.version_id)
        v2 = FactVersion.promote(v2p)
        rb, _rv = FactVersion.rollback(v2, v1)
        assert rb.ttl is None  # History must survive

    def test_restored_keeps_original_ttl(self):
        p = _make_pending({"text": "v1"}, ttl=60)
        v1 = FactVersion.promote(p)
        v2p = _make_pending({"text": "v2"}, fact_id=v1.fact_id,
                            supersedes=v1.version_id)
        v2 = FactVersion.promote(v2p)
        _rb, rv = FactVersion.rollback(v2, v1)
        assert rv.ttl == 60

    def test_rollback_decision_events(self):
        p = _make_pending({"text": "v1"})
        v1 = FactVersion.promote(p)
        v2p = _make_pending({"text": "v2"}, fact_id=v1.fact_id,
                            supersedes=v1.version_id)
        v2 = FactVersion.promote(v2p)
        rb, rv = FactVersion.rollback(v2, v1, reason="bad content")
        assert rb.decision_event.type == DECISION_ROLLBACK
        assert rb.decision_event.reason == "bad content"
        assert rv.decision_event.type == DECISION_ROLLBACK
        assert "Restored after rollback" in rv.decision_event.reason


# ─── FactStatus.valid_transitions() ─────────────────────────────────────────

class TestValidTransitions:
    def test_pending_to_canonical(self):
        assert FactStatus.valid_transitions("pending", "canonical")

    def test_pending_to_deprecated(self):
        assert FactStatus.valid_transitions("pending", "deprecated")

    def test_pending_to_rolled_back(self):
        assert FactStatus.valid_transitions("pending", "rolled_back")

    def test_canonical_to_deprecated(self):
        assert FactStatus.valid_transitions("canonical", "deprecated")

    def test_canonical_to_rolled_back(self):
        assert FactStatus.valid_transitions("canonical", "rolled_back")

    def test_deprecated_is_terminal(self):
        assert not FactStatus.valid_transitions("deprecated", "canonical")
        assert not FactStatus.valid_transitions("deprecated", "pending")
        assert not FactStatus.valid_transitions("deprecated", "rolled_back")

    def test_rolled_back_is_terminal(self):
        assert not FactStatus.valid_transitions("rolled_back", "canonical")
        assert not FactStatus.valid_transitions("rolled_back", "pending")
        assert not FactStatus.valid_transitions("rolled_back", "deprecated")

    def test_invalid_source_rejected(self):
        assert not FactStatus.valid_transitions("unknown", "canonical")


# ─── is_queryable / is_history ──────────────────────────────────────────────

class TestQueryable:
    def test_canonical_is_queryable(self):
        p = _make_pending()
        c = FactVersion.promote(p)
        assert c.is_queryable()

    def test_pending_not_queryable(self):
        v = _make_pending()
        assert not v.is_queryable()

    def test_deprecated_not_queryable(self):
        v = _make_pending()
        c = FactVersion.promote(v)
        d = FactVersion.deprecate(c)
        assert not d.is_queryable()

    def test_rolled_back_not_queryable(self):
        p = _make_pending({"text": "v1"})
        v1 = FactVersion.promote(p)
        rb, _rv = FactVersion.rollback(v1, p)
        assert not rb.is_queryable()

    def test_deprecated_is_history(self):
        v = _make_pending()
        c = FactVersion.promote(v)
        d = FactVersion.deprecate(c)
        assert d.is_history()

    def test_rolled_back_is_history(self):
        p = _make_pending({"text": "v1"})
        v1 = FactVersion.promote(p)
        rb, _rv = FactVersion.rollback(v1, p)
        assert rb.is_history()

    def test_canonical_not_history(self):
        p = _make_pending()
        c = FactVersion.promote(p)
        assert not c.is_history()


# ─── DecisionEvent ──────────────────────────────────────────────────────────

class TestDecisionEvent:
    def test_auto_timestamp(self):
        e = DecisionEvent(type=DECISION_PROMOTE, reason="test")
        assert e.timestamp
        assert "T" in e.timestamp  # ISO format

    def test_roundtrip_dict(self):
        e = DecisionEvent(
            type=DECISION_PROMOTE,
            reason="verified",
            triggered_by="manual",
        )
        d = e.to_dict()
        e2 = DecisionEvent.from_dict(d)
        assert e2.type == e.type
        assert e2.reason == e.reason
        assert e2.triggered_by == e.triggered_by

    def test_default_triggered_by(self):
        e = DecisionEvent(type=DECISION_PROMOTE, reason="test")
        assert e.triggered_by == "manual"


# ─── FactVersion Serialization ──────────────────────────────────────────────

class TestSerialization:
    def test_to_dict_roundtrip(self):
        p = _make_pending({"text": "hello"}, ttl=30)
        c = FactVersion.promote(p, reason="test")
        d = c.to_dict()
        restored = FactVersion.from_dict(d)
        assert restored.fact_id == c.fact_id
        assert restored.version_id == c.version_id
        assert restored.content == c.content
        assert restored.content_hash == c.content_hash
        assert restored.status == c.status
        assert restored.supersedes == c.supersedes
        assert restored.ttl == c.ttl
        assert restored.decision_event.type == c.decision_event.type

    def test_pending_serialization(self):
        p = _make_pending({"text": "hello"}, supersedes="prev")
        d = p.to_dict()
        restored = FactVersion.from_dict(d)
        assert restored.status == FactStatus.PENDING.value
        assert restored.supersedes == "prev"
        assert restored.decision_event is None


# ─── CanonicalView ──────────────────────────────────────────────────────────

class TestCanonicalView:
    def test_set_and_get(self):
        view = CanonicalView()
        p = _make_pending()
        c = FactVersion.promote(p)
        view.set(c)
        assert view.get(c.fact_id) == c

    def test_set_non_canonical_not_in_get(self):
        view = CanonicalView()
        p = _make_pending()
        view.set(p)
        assert view.get(p.fact_id) is None

    def test_deprecate_removes_from_canonical(self):
        view = CanonicalView()
        p = _make_pending()
        c = FactVersion.promote(p)
        view.set(c)
        assert view.get(c.fact_id) is not None
        d = FactVersion.deprecate(c)
        view.set(d)
        assert view.get(c.fact_id) is None

    def test_chain_tracks_all_versions(self):
        view = CanonicalView()
        p = _make_pending()
        c = FactVersion.promote(p)
        view.set(c)
        d = FactVersion.deprecate(c)
        view.set(d)
        chain = view.chain(c.fact_id)
        assert len(chain) == 2
        assert chain[0] == d.version_id  # newest first

    def test_all_canonical(self):
        view = CanonicalView()
        p1 = _make_pending({"text": "fact1"})
        p2 = _make_pending({"text": "fact2"})
        c1 = FactVersion.promote(p1)
        c2 = FactVersion.promote(p2)
        view.set(c1)
        view.set(c2)
        assert len(view.all_canonical()) == 2

    def test_fact_ids_with_status(self):
        view = CanonicalView()
        p = _make_pending()
        c = FactVersion.promote(p)
        view.set(c)
        ids = view.fact_ids_with_status("canonical")
        assert c.fact_id in ids
        ids = view.fact_ids_with_status("deprecated")
        assert c.fact_id not in ids
