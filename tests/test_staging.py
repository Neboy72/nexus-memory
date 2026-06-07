"""Tests for nexus/staging.py — Collection Bootstrap + Concurrency Guard.

Most staging functions talk to Qdrant via HTTP; these tests use mocking
to verify the logic layer (guards, filtering, bootstrap).
"""

from unittest.mock import patch, MagicMock

import pytest

from nexus.lifecycle import FactVersion, FactStatus
from nexus.staging import (
    ensure_collections,
    _collection_all,
    _collection_canonical,
)


# ─── ensure_collections() ───────────────────────────────────────────────────

class TestEnsureCollections:
    """Verify collection bootstrap logic — creation vs. skip-if-exists."""

    @patch("nexus.staging.requests.get")
    @patch("nexus.staging.requests.put")
    def test_both_exist_noop(self, mock_put: MagicMock, mock_get: MagicMock):
        """Both collections already exist → no PUT calls."""
        mock_get.return_value.status_code = 200

        result = ensure_collections("localhost", 6333)

        assert result[_collection_all()] is True
        assert result[_collection_canonical()] is True
        mock_put.assert_not_called()

    @patch("nexus.staging.requests.get")
    @patch("nexus.staging.requests.put")
    def test_creates_missing_collections(self, mock_put: MagicMock, mock_get: MagicMock):
        """Collections missing → PUT creates them with 512D Cosine."""
        mock_get.return_value.status_code = 404  # Not found
        mock_put.return_value.status_code = 201

        result = ensure_collections("localhost", 6333)

        assert result[_collection_all()] is True
        assert result[_collection_canonical()] is True
        assert mock_put.call_count == 2

        # Verify create payload has correct vector dimension
        call_data = mock_put.call_args_list[0][1]["json"]
        assert call_data["vectors"]["size"] == 512
        assert call_data["vectors"]["distance"] == "Cosine"

    @patch("nexus.staging.requests.get")
    @patch("nexus.staging.requests.put")
    def test_create_failure_reported(self, mock_put: MagicMock, mock_get: MagicMock):
        """PUT fails → collection marked as not created."""
        mock_get.return_value.status_code = 404
        mock_put.return_value.status_code = 500
        mock_put.return_value.text = "Internal error"

        result = ensure_collections("localhost", 6333)

        assert result[_collection_all()] is False
        assert result[_collection_canonical()] is False

    @patch("nexus.staging.requests.get")
    @patch("nexus.staging.requests.put")
    def test_partial_existence(self, mock_put: MagicMock, mock_get: MagicMock):
        """One collection exists, one missing → only missing is created."""
        def get_side_effect(url, **kwargs):
            resp = MagicMock()
            if _collection_canonical() in url:
                resp.status_code = 200  # canonical exists
            else:
                resp.status_code = 404  # main missing
            return resp
        mock_get.side_effect = get_side_effect
        mock_put.return_value.status_code = 201

        result = ensure_collections("localhost", 6333)

        assert result[_collection_all()] is True  # Created
        assert result[_collection_canonical()] is True  # Already existed
        assert mock_put.call_count == 1  # Only created one


# ─── Concurrency Guard Logic ────────────────────────────────────────────────

class TestConcurrencyGuard:
    """Verify promote() rejects stale / conflicting versions.

    These tests mock _get_current_canonical to control what the
    guard sees without an actual Qdrant.
    """

    @patch("nexus.staging._get_current_canonical")
    @patch("nexus.staging._auto_ensure_collections")
    @patch("nexus.staging._upsert_point")
    @patch("nexus.staging._write_canonical")
    def test_promote_update_matches(
        self, _mock_write, _mock_upsert,
        _mock_ensure, mock_get_canonical,
    ):
        """Update pending matches current canonical → promote succeeds."""
        # Setup: canonical A exists
        canonical_a = FactVersion.new_pending(
            content={"text": "v1"},
            fact_id="test-fact",
        )
        canonical_a = FactVersion.promote(canonical_a)
        mock_get_canonical.return_value = canonical_a

        # Pending built against A — supersedes matches
        pending = FactVersion.new_pending(
            content={"text": "v2"},
            fact_id="test-fact",
            supersedes=canonical_a.version_id,
        )

        from nexus.staging import promote as staging_promote
        result = staging_promote(pending, reason="test")
        assert result.status == FactStatus.CANONICAL.value

    @patch("nexus.staging._get_current_canonical")
    @patch("nexus.staging._auto_ensure_collections")
    def test_promote_update_stale_rejected(
        self, _mock_ensure, mock_get_canonical,
    ):
        """Pending built against A, but canonical is now B → reject."""
        # Canonical A (old)
        canonical_a = FactVersion.new_pending(
            content={"text": "v1"},
            fact_id="test-fact",
        )
        canonical_a = FactVersion.promote(canonical_a)

        # Canonical B (current)
        canonical_b = FactVersion.new_pending(
            content={"text": "v2"},
            fact_id="test-fact",
        )
        canonical_b = FactVersion.promote(canonical_b)
        mock_get_canonical.return_value = canonical_b  # Current is B

        # Pending built against A — STALE!
        pending = FactVersion.new_pending(
            content={"text": "v3"},
            fact_id="test-fact",
            supersedes=canonical_a.version_id,  # Built against A
        )

        from nexus.staging import promote as staging_promote
        with pytest.raises(ValueError, match="Concurrency conflict"):
            staging_promote(pending, reason="test")

    @patch("nexus.staging._get_current_canonical")
    @patch("nexus.staging._auto_ensure_collections")
    def test_promote_new_fact_with_existing_rejected(
        self, _mock_ensure, mock_get_canonical,
    ):
        """New fact (no supersedes) but canonical already exists → reject."""
        canonical = FactVersion.new_pending(
            content={"text": "existing"},
            fact_id="test-fact",
        )
        canonical = FactVersion.promote(canonical)
        mock_get_canonical.return_value = canonical

        # New pending without supersedes — but canonical already exists!
        pending = FactVersion.new_pending(
            content={"text": "also-existing"},
            fact_id="test-fact",
        )

        from nexus.staging import promote as staging_promote
        with pytest.raises(ValueError, match="already exists"):
            staging_promote(pending, reason="test")

    @patch("nexus.staging._get_current_canonical")
    @patch("nexus.staging._auto_ensure_collections")
    def test_promote_update_with_no_canonical_rejected(
        self, _mock_ensure, mock_get_canonical,
    ):
        """Update pending says it supersedes X, but no canonical exists."""
        mock_get_canonical.return_value = None

        pending = FactVersion.new_pending(
            content={"text": "orphan"},
            fact_id="test-fact",
            supersedes="nonexistent-version",
        )

        from nexus.staging import promote as staging_promote
        with pytest.raises(ValueError, match="no canonical exists"):
            staging_promote(pending, reason="test")


# ─── Lazy Collection Bootstrap ──────────────────────────────────────────────

class TestAutoEnsureCollections:
    """Verify _auto_ensure_collections runs once, raises on failure."""

    def _reset_ensured_flag(self):
        """Reset the module-level flag between tests."""
        import nexus.staging
        nexus.staging._collections_ensured = False

    @patch("nexus.staging.ensure_collections")
    def test_lazy_one_shot(self, mock_ensure):
        """_auto_ensure_collections calls ensure_collections exactly once."""
        self._reset_ensured_flag()
        from nexus.staging import _auto_ensure_collections

        mock_ensure.return_value = {_collection_all(): True, _collection_canonical(): True}
        _auto_ensure_collections("localhost", 6333)
        _auto_ensure_collections("localhost", 6333)  # Second call
        _auto_ensure_collections("localhost", 6334)  # Different port

        assert mock_ensure.call_count == 1  # Only called once

    @patch("nexus.staging.ensure_collections")
    def test_raises_on_failure(self, mock_ensure):
        """If a collection can't be created, raise RuntimeError."""
        self._reset_ensured_flag()
        from nexus.staging import _auto_ensure_collections

        mock_ensure.return_value = {_collection_all(): False, _collection_canonical(): True}

        with pytest.raises(RuntimeError, match="Failed to ensure"):
            _auto_ensure_collections("localhost", 6333)
