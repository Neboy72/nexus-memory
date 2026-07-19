"""Tests for Auto-Supersession - automatic deprecation of similar canonical facts.

When a new fact is stored that is semantically similar (>0.90 similarity) to an
existing canonical fact of the same category, the old fact is automatically
deprecated and the new one marked as canonical with a 'supersedes' reference.
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from dataclasses import dataclass


@dataclass
class MockScoredPoint:
    """Mock Qdrant ScoredPoint for query results."""
    id: str
    score: float
    payload: dict


class TestAutoSupersession:
    """Test the auto-supersession logic in MemoryStore.remember()."""

    def _make_mock_store(self, existing_points=None):
        """Create a mock MemoryStore with controlled query results."""
        from nexus_memory.mcp_server import MemoryStore, COLLECTION_NAME
        store = MagicMock(spec=MemoryStore)
        store.client = MagicMock()
        store._embed = AsyncMock(return_value=[0.1] * 1024)
        store._skill_graph = None

        # Configure query_points to return existing similar facts
        if existing_points:
            store.client.query_points.return_value = MagicMock(
                points=existing_points
            )
        else:
            store.client.query_points.return_value = MagicMock(points=[])

        return store

    @pytest.mark.asyncio
    async def test_new_fact_no_existing_no_supersession(self):
        """When no similar canonical fact exists, no supersession happens."""
        store = self._make_mock_store(existing_points=[])

        # Call remember directly on the mock
        # We need to call the real method with the mock client
        from nexus_memory.mcp_server import MemoryStore
        with patch.object(MemoryStore, '_embed', new_callable=AsyncMock) as mock_embed:
            mock_embed.return_value = [0.1] * 1024
            with patch.object(MemoryStore, '_ensure_collection'):
                real_store = MemoryStore.__new__(MemoryStore)
                real_store.client = store.client
                real_store._embedder = MagicMock(dim=1024)
                real_store._skill_graph = None
                store.client.query_points.return_value = MagicMock(points=[])

                result = await MemoryStore.remember(
                    real_store,
                    text="Project uses Paddle for payments",
                    category="fact",
                )

                assert result["status"] == "ok"
                assert result.get("superseded") is None
                # query_points was called to check for similar facts
                assert store.client.query_points.called

    @pytest.mark.asyncio
    async def test_similar_canonical_fact_is_deprecated(self):
        """When a similar canonical fact exists, it gets deprecated."""
        from nexus_memory.mcp_server import MemoryStore

        old_id = "old-fact-123"
        existing = [MockScoredPoint(id=old_id, score=0.95, payload={
            "content": "Project uses Stripe for payments",
            "category": "fact",
            "lifecycle_status": "canonical",
        })]

        with patch.object(MemoryStore, '_embed', new_callable=AsyncMock) as mock_embed:
            mock_embed.return_value = [0.1] * 1024
            with patch.object(MemoryStore, '_ensure_collection'):
                real_store = MemoryStore.__new__(MemoryStore)
                real_store.client = MagicMock()
                real_store._embedder = MagicMock(dim=1024)
                real_store._skill_graph = None
                real_store.client.query_points.return_value = MagicMock(points=existing)

                result = await MemoryStore.remember(
                    real_store,
                    text="Project uses Paddle for payments",
                    category="fact",
                )

                assert result["status"] == "ok"
                assert result.get("superseded") is not None
                assert old_id in result["superseded"]
                # set_payload was called to deprecate the old fact
                real_store.client.set_payload.assert_called_once()
                call_args = real_store.client.set_payload.call_args
                payload = call_args.kwargs.get("payload", {})
                assert payload.get("lifecycle_status") == "deprecated"
                assert payload.get("superseded_by") == result["id"]

    @pytest.mark.asyncio
    async def test_low_similarity_no_supersession(self):
        """Facts below 0.90 similarity should not be superseded."""
        from nexus_memory.mcp_server import MemoryStore

        existing = [MockScoredPoint(id="old-fact", score=0.75, payload={
            "content": "Something unrelated",
            "category": "fact",
            "lifecycle_status": "canonical",
        })]

        with patch.object(MemoryStore, '_embed', new_callable=AsyncMock) as mock_embed:
            mock_embed.return_value = [0.1] * 1024
            with patch.object(MemoryStore, '_ensure_collection'):
                real_store = MemoryStore.__new__(MemoryStore)
                real_store.client = MagicMock()
                real_store._embedder = MagicMock(dim=1024)
                real_store._skill_graph = None
                real_store.client.query_points.return_value = MagicMock(points=existing)

                result = await MemoryStore.remember(
                    real_store,
                    text="Project uses Paddle for payments",
                    category="fact",
                )

                assert result["status"] == "ok"
                # Score 0.75 < 0.90, no supersession
                assert result.get("superseded") is None
                real_store.client.set_payload.assert_not_called()

    @pytest.mark.asyncio
    async def test_different_category_no_supersession(self):
        """Facts of different categories should not supersede each other."""
        from nexus_memory.mcp_server import MemoryStore

        # The query filter already restricts to same category, but even if
        # a result leaks through, the category check in the filter should prevent it
        existing = []  # No results because filter restricts to same category

        with patch.object(MemoryStore, '_embed', new_callable=AsyncMock) as mock_embed:
            mock_embed.return_value = [0.1] * 1024
            with patch.object(MemoryStore, '_ensure_collection'):
                real_store = MemoryStore.__new__(MemoryStore)
                real_store.client = MagicMock()
                real_store._embedder = MagicMock(dim=1024)
                real_store._skill_graph = None
                real_store.client.query_points.return_value = MagicMock(points=existing)

                result = await MemoryStore.remember(
                    real_store,
                    text="Project uses Paddle for payments",
                    category="session",  # Session category - no auto-supersession
                )

                assert result["status"] == "ok"
                assert result.get("superseded") is None
                # query_points should NOT be called for session/belief/temp categories
                real_store.client.query_points.assert_not_called()

    @pytest.mark.asyncio
    async def test_multiple_similar_facts_all_deprecated(self):
        """When multiple similar canonical facts exist, all get deprecated."""
        from nexus_memory.mcp_server import MemoryStore

        old1 = MockScoredPoint(id="old-1", score=0.93, payload={
            "content": "Project uses Stripe", "category": "fact",
            "lifecycle_status": "canonical",
        })
        old2 = MockScoredPoint(id="old-2", score=0.91, payload={
            "content": "Payment via Stripe", "category": "fact",
            "lifecycle_status": "canonical",
        })

        with patch.object(MemoryStore, '_embed', new_callable=AsyncMock) as mock_embed:
            mock_embed.return_value = [0.1] * 1024
            with patch.object(MemoryStore, '_ensure_collection'):
                real_store = MemoryStore.__new__(MemoryStore)
                real_store.client = MagicMock()
                real_store._embedder = MagicMock(dim=1024)
                real_store._skill_graph = None
                real_store.client.query_points.return_value = MagicMock(points=[old1, old2])

                result = await MemoryStore.remember(
                    real_store,
                    text="Project uses Paddle for payments",
                    category="fact",
                )

                assert result["status"] == "ok"
                assert len(result["superseded"]) == 2
                assert "old-1" in result["superseded"]
                assert "old-2" in result["superseded"]
                # set_payload called twice (once per old fact)
                assert real_store.client.set_payload.call_count == 2

    @pytest.mark.asyncio
    async def test_supersession_failure_is_non_blocking(self):
        """If the supersession check fails, the fact should still be stored."""
        from nexus_memory.mcp_server import MemoryStore

        with patch.object(MemoryStore, '_embed', new_callable=AsyncMock) as mock_embed:
            mock_embed.return_value = [0.1] * 1024
            with patch.object(MemoryStore, '_ensure_collection'):
                real_store = MemoryStore.__new__(MemoryStore)
                real_store.client = MagicMock()
                real_store._embedder = MagicMock(dim=1024)
                real_store._skill_graph = None
                # query_points raises an exception
                real_store.client.query_points.side_effect = Exception("Qdrant error")

                result = await MemoryStore.remember(
                    real_store,
                    text="Project uses Paddle for payments",
                    category="fact",
                )

                # Fact is still stored despite supersession check failure
                assert result["status"] == "ok"
                assert result.get("superseded") is None
                # upsert was still called
                assert real_store.client.upsert.called

    @pytest.mark.asyncio
    async def test_new_fact_has_supersedes_field(self):
        """The new fact's payload should contain a 'supersedes' field."""
        from nexus_memory.mcp_server import MemoryStore

        old_id = "old-fact-456"
        existing = [MockScoredPoint(id=old_id, score=0.95, payload={
            "content": "Project uses Stripe", "category": "fact",
            "lifecycle_status": "canonical",
        })]

        with patch.object(MemoryStore, '_embed', new_callable=AsyncMock) as mock_embed:
            mock_embed.return_value = [0.1] * 1024
            with patch.object(MemoryStore, '_ensure_collection'):
                real_store = MemoryStore.__new__(MemoryStore)
                real_store.client = MagicMock()
                real_store._embedder = MagicMock(dim=1024)
                real_store._skill_graph = None
                real_store.client.query_points.return_value = MagicMock(points=existing)

                result = await MemoryStore.remember(
                    real_store,
                    text="Project uses Paddle for payments",
                    category="fact",
                )

                # Check the upsert payload contains 'supersedes'
                upsert_call = real_store.client.upsert.call_args
                points = upsert_call.kwargs.get("points", [])
                if points:
                    payload = points[0].payload if hasattr(points[0], 'payload') else points[0].get("payload", {})
                    assert "supersedes" in payload
                    assert old_id in payload["supersedes"]