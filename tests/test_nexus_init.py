"""Tests for ``nexus/__init__.py`` — focus on ``nexus_remember()`` category coercion.

The library API does its own validation/coercion of the ``category``
field independently of the MCP server wrapper. We exercise:

- All six valid categories are accepted verbatim.
- An unknown / invalid / empty / non-string category triggers a
  ``WARNING`` log message AND is coerced to ``"fact"`` in **both** the
  Python ``category`` variable and the outbound ``payload["category"]``.
- The HTTP PUT to Qdrant carries the corrected ``category`` value.

All tests use mocks — no real Qdrant server is required.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

import nexus
from nexus import MemoryCategory, nexus_remember


# ===========================================================================
# Helpers
# ===========================================================================


@pytest.fixture
def mocked_qdrant_put(monkeypatch):
    """Mock the ``requests.put`` call that ``nexus_remember`` makes.

    Returns a MagicMock whose ``json()`` returns a fake Qdrant response so
    ``nexus_remember`` doesn't try to deserialize ``None``. Tests assert
    on ``mocked_qdrant_put.call_args`` to verify the payload.
    """
    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {"result": {"status": "ok"}}
    fake_response.text = '{"result": {"status": "ok"}}'

    mock_put = MagicMock(return_value=fake_response)
    monkeypatch.setattr("requests.put", mock_put)
    return mock_put


# ===========================================================================
# 1. nexus_remember() category handling
# ===========================================================================


class TestNexusRememberCategory:
    """`nexus_remember()` validates the `category` argument and coerces
    unknown values to ``"fact"``."""

    # ---- Valid categories pass through unchanged ----------------------------

    @pytest.mark.parametrize(
        "cat", ["fact", "belief", "session", "rule", "preference", "temp"]
    )
    def test_valid_category_passes_through(self, cat, mocked_qdrant_put, caplog):
        with caplog.at_level(logging.WARNING, logger="nexus"):
            nexus_remember(
                content="hello",
                category=cat,
                qdrant_host="localhost",
                qdrant_port=6333,
                collection_name="test-collection",
            )

        # The outgoing payload to Qdrant must carry the literal category.
        sent_payload = mocked_qdrant_put.call_args.kwargs["json"]["points"][0]["payload"]
        assert sent_payload["category"] == cat, (
            f"category={cat!r} should be persisted unchanged, "
            f"got {sent_payload['category']!r}"
        )
        # No warning should be raised for a valid category.
        assert "coercing" not in caplog.text.lower()

    def test_default_category_is_fact(self, mocked_qdrant_put):
        """Omitting `category` is fine — it defaults to "fact"."""
        nexus_remember(
            content="hello",
            qdrant_host="localhost",
            qdrant_port=6333,
            collection_name="test-collection",
        )
        sent_payload = mocked_qdrant_put.call_args.kwargs["json"]["points"][0]["payload"]
        assert sent_payload["category"] == "fact"

    # ---- Invalid categories trigger warning + coercion ---------------------

    @pytest.mark.parametrize(
        "bad_cat", ["", "FACTS", "ruleS", "factoid", "unknown", "default"]
    )
    def test_invalid_category_emits_warning(self, bad_cat, mocked_qdrant_put, caplog):
        """A non-empty but unknown category is rejected, logged, and
        coerced to "fact"."""
        with caplog.at_level(logging.WARNING, logger="nexus"):
            nexus_remember(
                content="hello",
                category=bad_cat,
                qdrant_host="localhost",
                qdrant_port=6333,
                collection_name="test-collection",
            )

        # 1. A WARNING was logged mentioning the bad value + 'coercing'
        warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any(
            f"Unknown category '{bad_cat}'" in r.getMessage()
            and "coercing to 'fact'" in r.getMessage()
            for r in warning_records
        ), (
            f"Expected a WARNING for category={bad_cat!r}; "
            f"got: {[r.getMessage() for r in warning_records]}"
        )

        # 2. The payload sent to Qdrant was updated to "fact"
        sent_payload = mocked_qdrant_put.call_args.kwargs["json"]["points"][0]["payload"]
        assert sent_payload["category"] == "fact", (
            f"category={bad_cat!r} should be coerced to 'fact' in the payload"
        )

    def test_payload_category_updated_after_coercion(self, mocked_qdrant_put, caplog):
        """Specifically verify the contract from the task:
        ``payload["category"]`` is updated **after** coercion. The code
        path is:

            payload["category"] = category  # initial
            if invalid:
                category = "fact"
                payload["category"] = category  # ← reassign
        """
        with caplog.at_level(logging.WARNING, logger="nexus"):
            nexus_remember(
                content="hello",
                category="garbage-value",
                qdrant_host="localhost",
                qdrant_port=6333,
                collection_name="test-collection",
            )

        # Capture the request data.
        call_kwargs = mocked_qdrant_put.call_args.kwargs
        body = call_kwargs["json"]
        sent_point = body["points"][0]
        assert sent_point["payload"]["category"] == "fact"

        # The original (bad) value should NOT leak into the payload.
        assert "garbage-value" not in str(sent_point["payload"]["category"])

    # ---- None / non-string categories --------------------------------------

    def test_none_category_coerced_to_fact(self, mocked_qdrant_put, caplog):
        with caplog.at_level(logging.WARNING, logger="nexus"):
            nexus_remember(
                content="hello",
                category=None,
                qdrant_host="localhost",
                qdrant_port=6333,
                collection_name="test-collection",
            )
        sent_payload = mocked_qdrant_put.call_args.kwargs["json"]["points"][0]["payload"]
        assert sent_payload["category"] == "fact"

    def test_int_category_coerced_to_fact(self, mocked_qdrant_put, caplog):
        """Defensive: a non-string category (int, bool) should not crash —
        it should be coerced to "fact"."""
        with caplog.at_level(logging.WARNING, logger="nexus"):
            nexus_remember(
                content="hello",
                category=42,  # type: ignore[arg-type]
                qdrant_host="localhost",
                qdrant_port=6333,
                collection_name="test-collection",
            )
        sent_payload = mocked_qdrant_put.call_args.kwargs["json"]["points"][0]["payload"]
        assert sent_payload["category"] == "fact"


# ===========================================================================
# 2. MemoryCategory enum sanity
# ===========================================================================


class TestMemoryCategoryEnum:
    """The enum that backs the State-Prefixing pattern."""

    def test_all_seven_values_present(self):
        values = {c.value for c in MemoryCategory}
        assert values == {"fact", "belief", "session", "rule", "preference", "procedure", "temp"}

    def test_member_lookup_by_value(self):
        # The coercion check uses ``category not in MemoryCategory._value2member_map_``
        # which means string lookup via the value-→-member map.
        m = MemoryCategory._value2member_map_.get("fact")
        assert m is MemoryCategory.FACT
        assert MemoryCategory._value2member_map_.get("nonsense") is None


# ===========================================================================
# 3. nexus_remember() basic happy-path (smoke test)
# ===========================================================================


class TestNexusRememberSmoke:
    """A few smoke tests that don't focus on category — they pin down the
    HTTP envelope so a future refactor of nexus_remember() can't silently
    change the wire format without breaking these."""

    def test_put_targets_configured_collection(self, mocked_qdrant_put):
        nexus_remember(
            content="hello",
            category="fact",
            qdrant_host="myhost",
            qdrant_port=9999,
            collection_name="custom-coll",
        )
        url = mocked_qdrant_put.call_args.args[0]
        assert url == "http://myhost:9999/collections/custom-coll/points"

    def test_point_id_is_uuid(self, mocked_qdrant_put):
        nexus_remember(
            content="hello",
            category="fact",
            collection_name="test-collection",
        )
        sent = mocked_qdrant_put.call_args.kwargs["json"]["points"][0]
        # 36-char UUID (8-4-4-4-12 hex with hyphens).
        assert isinstance(sent["id"], str)
        assert len(sent["id"]) == 36
        assert sent["id"].count("-") == 4

    def test_valid_from_defaults_to_today(self, mocked_qdrant_put):
        from datetime import date
        nexus_remember(
            content="hello",
            category="fact",
            collection_name="test-collection",
        )
        sent_payload = mocked_qdrant_put.call_args.kwargs["json"]["points"][0]["payload"]
        assert sent_payload["valid_from"] == date.today().isoformat()
