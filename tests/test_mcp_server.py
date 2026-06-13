"""Tests for the MCP server (``src/nexus_memory/mcp_server.py``).

The tests run **without a Qdrant server** — every network boundary is
patched. We exercise:

1. ``is_success()`` — the small helper imported from ``nexus.config``.
2. ``MemoryStore.remember()`` — category handling, provenance, access_level.
3. ``MemoryStore.recall()`` — both the hybrid and the vector-fallback paths,
   plus the legacy-entry default for ``category``.
4. The MCP tool schema (``handle_list_tools``) — category is declared required.
5. The tool dispatcher (``handle_call_tool``) — JSON envelope, category
   coercion, access-level defaults, error handling.
6. ``EmbeddingProvider`` — falls back to ``sentence-transformers`` when no
   API key is set, and refuses to import cloud clients with bogus keys.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

# Import the module under test as `mcp` for short. We use the source-layout
# import path (``nexus_memory.mcp_server``) since the package is installed
# in editable mode from ``src/``.
import nexus_memory.mcp_server as mcp
from nexus import MemoryCategory
from nexus.config import is_success


# ===========================================================================
# Helpers / shared fixtures
# ===========================================================================


class _FakeHybrid:
    """Stand-in for ``HybridRetriever`` — returns whatever the test pre-loads."""

    def __init__(self, results=None):
        self._results = list(results or [])
        self.search = MagicMock(side_effect=self._search)
        self.index_memories = MagicMock(return_value={"indexed": 0})

    def _search(self, query, query_vector=None, top_k: int = 10, **kwargs):
        return self._results[:top_k]


class _FakeEmbedder:
    """In-process replacement for the real ``EmbeddingProvider``.

    Just enough to make ``MemoryStore`` work without trying to import
    sentence-transformers or hit any network.
    """

    _name = "fake-embedder"
    _dim = 384
    _model = None
    _client = None

    def __init__(self):
        pass

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
    """Build a ``MemoryStore`` whose Qdrant client + hybrid retriever are mocks.

    - ``QdrantClient`` is patched to a MagicMock → no real network call.
    - ``EmbeddingProvider`` is replaced by ``_FakeEmbedder``.
    - ``MemoryStore._init_hybrid`` is patched to a no-op so we default to
      the vector-fallback path. Tests that want the hybrid path explicitly
      set ``store._hybrid_retriever`` afterwards.
    """
    # 1. Avoid touching real embedding providers.
    monkeypatch.setattr(mcp, "EmbeddingProvider", _FakeEmbedder)

    # 2. Disable hybrid retriever bootstrap. Tests opt in by reassigning
    #    ``store._hybrid_retriever`` to a mock.
    monkeypatch.setattr(mcp.MemoryStore, "_init_hybrid", lambda self: None)

    # 3. Make sure the collection is "missing" so we exercise the
    #    create-collection path on first instantiation.
    mock_qdrant_client.get_collections.return_value = MagicMock(collections=[])

    return mcp.MemoryStore()


# ===========================================================================
# 1. is_success() helper
# ===========================================================================


class TestIsSuccess:
    """`is_success()` is the central "did the HTTP request succeed?" check."""

    @pytest.mark.parametrize("code", [200, 201, 204, 299])
    def test_2xx_is_success(self, code):
        assert is_success(code) is True

    @pytest.mark.parametrize("code", [300, 301, 400, 404, 500, 503])
    def test_non_2xx_is_not_success(self, code):
        assert is_success(code) is False

    def test_boundaries(self):
        # 200 inclusive, 300 exclusive
        assert is_success(200) is True
        assert is_success(299) is True
        assert is_success(300) is False


# ===========================================================================
# 2. Tool schema (list_tools) — category must be required
# ===========================================================================


class TestRememberToolSchema:
    """The ``remember`` tool schema declares category as a required field."""

    async def test_remember_tool_marks_category_required(self):
        tools = await mcp.handle_list_tools()
        remember = next(t for t in tools if t.name == "remember")

        assert "category" in remember.inputSchema["required"], (
            "category must be in the `required` list so clients are forced "
            "to declare the State-Prefixing scope."
        )
        assert "text" in remember.inputSchema["required"]
        # Confidence is optional but bounded to [0, 1].
        confidence = remember.inputSchema["properties"]["confidence"]
        assert confidence["minimum"] == 0.0
        assert confidence["maximum"] == 1.0

    async def test_recall_tool_requires_query(self):
        tools = await mcp.handle_list_tools()
        recall = next(t for t in tools if t.name == "recall")
        assert "query" in recall.inputSchema["required"]


# ===========================================================================
# 3. MemoryStore.remember() — category, provenance, access_level
# ===========================================================================


class TestMemoryStoreRemember:
    """Direct unit tests on ``MemoryStore.remember()``."""

    async def test_remember_stores_payload_with_category(self, store, mock_qdrant_client):
        result = await store.remember(
            text="Voyage-3-large produces 1024-dim embeddings",
            category="fact",
        )
        assert result["status"] == "ok"
        assert result["category"] == "fact"
        # The upsert call should carry category + provenance in its payload.
        upsert_call = mock_qdrant_client.upsert.call_args
        points = upsert_call.kwargs["points"]
        assert len(points) == 1
        payload = points[0].payload
        assert payload["category"] == "fact"
        assert payload["content"] == "Voyage-3-large produces 1024-dim embeddings"
        # Provenance is a non-empty dict (default confidence 0.7).
        assert payload["provenance"]["confidence"] == 0.7

    @pytest.mark.parametrize("bad_value", ["", None, "FACTS", "random-string", 42])
    async def test_remember_coerces_invalid_category_to_fact(self, store, bad_value):
        """The schema says category is required, but old clients may send
        junk. The server must coerce to "fact" and persist that — never the
        bad value."""
        result = await store.remember(
            text="legacy fact", category=bad_value
        )
        assert result["category"] == "fact"

    @pytest.mark.parametrize("good", ["fact", "belief", "session", "rule", "preference", "temp"])
    async def test_remember_accepts_every_valid_category(self, store, good):
        result = await store.remember(text="x", category=good)
        assert result["category"] == good

    async def test_remember_records_source_url_and_confidence(self, store, mock_qdrant_client):
        result = await store.remember(
            text="The sky is blue",
            category="fact",
            source_url="https://example.com/sky",
            confidence=0.95,
            source="documentation",
        )
        assert result["status"] == "ok"
        payload = mock_qdrant_client.upsert.call_args.kwargs["points"][0].payload
        assert payload["source_url"] == "https://example.com/sky"
        assert payload["source"] == "documentation"
        # Confidence lands in the provenance dict (Level 1).
        assert payload["provenance"]["confidence"] == 0.95
        assert payload["provenance"].get("source_url") == "https://example.com/sky"

    @pytest.mark.parametrize("level", ["public", "trusted", "private"])
    async def test_remember_accepts_all_access_levels(self, store, level):
        result = await store.remember(text="x", category="fact", access_level=level)
        assert result["access_level"] == level


# ===========================================================================
# 4. MemoryStore.recall() — hybrid + vector-fallback
# ===========================================================================


def _payload(
    *,
    pid: str = "abc-123",
    content: str = "hello world",
    access_level: str = "public",
    category: str | None = "fact",
    source_url: str | None = None,
    score: float = 0.9,
) -> dict:
    """Build a Qdrant-shaped record dict for use in ``query_points`` / ``retrieve``."""
    return {
        "id": pid,
        "content": content,
        "access_level": access_level,
        # NOTE: legacy fixtures may set `category=None` to verify the
        # "fact"-fallback in the recall path.
        "category": category,
        "source": "test",
        "source_url": source_url,
        "provenance": {"confidence": 0.8},
        "created_at": "2025-01-01T00:00:00+00:00",
    }


class TestMemoryStoreRecall:
    """`MemoryStore.recall()` — exercises both retrieval paths."""

    # ---- vector-fallback path (hybrid retriever disabled) -----------------

    async def test_recall_vector_fallback_returns_fact_for_legacy(
        self, store, mock_qdrant_client
    ):
        # _hybrid_retriever is None by default → vector-only path.
        assert store._hybrid_retriever is None

        # Simulate one Qdrant hit whose payload has NO category (legacy).
        legacy_point = MagicMock()
        legacy_point.id = "legacy-id"
        legacy_point.payload = _payload(pid="legacy-id", category=None)
        legacy_point.score = 0.87

        mock_qdrant_client.query_points.return_value = MagicMock(
            points=[legacy_point]
        )

        results = await store.recall("hello", limit=5)

        assert len(results) == 1
        # Legacy entries are surfaced as "fact" so consumers always see a
        # valid enum value.
        assert results[0]["category"] == "fact"
        assert results[0]["id"] == "legacy-id"
        assert results[0]["text"] == "hello world"

    async def test_recall_vector_fallback_keeps_explicit_category(
        self, store, mock_qdrant_client
    ):
        point = MagicMock()
        point.id = "explicit-1"
        point.payload = _payload(pid="explicit-1", category="belief")
        point.score = 0.7
        mock_qdrant_client.query_points.return_value = MagicMock(points=[point])

        results = await store.recall("x", limit=5)
        assert results[0]["category"] == "belief"

    async def test_recall_respects_access_hierarchy(
        self, store, mock_qdrant_client
    ):
        # Two points: one public, one private. Agent is public-level.
        public = MagicMock()
        public.id = "pub"
        public.payload = _payload(pid="pub", access_level="public", category="fact")
        public.score = 0.9

        private = MagicMock()
        private.id = "priv"
        private.payload = _payload(pid="priv", access_level="private", category="fact")
        private.score = 0.8

        mock_qdrant_client.query_points.return_value = MagicMock(
            points=[public, private]
        )

        results = await store.recall("x", limit=5, agent_level="public")
        ids = {r["id"] for r in results}
        assert "pub" in ids
        assert "priv" not in ids, "public agent must not see private memories"

    async def test_recall_trusted_agent_sees_trusted_and_public(
        self, store, mock_qdrant_client
    ):
        trusted = MagicMock()
        trusted.id = "trusted-1"
        trusted.payload = _payload(pid="trusted-1", access_level="trusted", category="rule")
        trusted.score = 0.6
        mock_qdrant_client.query_points.return_value = MagicMock(points=[trusted])

        results = await store.recall("x", limit=5, agent_level="trusted")
        assert any(r["id"] == "trusted-1" for r in results)

    # ---- hybrid path (custom retriever injected) ---------------------------

    async def test_recall_hybrid_path_enriches_payload_from_qdrant(
        self, store, mock_qdrant_client
    ):
        # Inject a hybrid retriever that returns one match. The MCP server
        # then fetches the Qdrant point to fill in access_level / category.
        hybrid = _FakeHybrid(
            results=[
                {
                    "id": "hyb-1",
                    "text": "first",
                    "rrf_score": 0.05,
                    "score": 0.5,
                }
            ]
        )
        store._hybrid_retriever = hybrid

        # Mock the Qdrant retrieve() that the enrichment step makes.
        retrieved_point = MagicMock()
        retrieved_point.id = "hyb-1"
        retrieved_point.payload = _payload(
            pid="hyb-1",
            access_level="public",
            category="belief",
            source_url="https://example.com/belief",
        )
        mock_qdrant_client.retrieve.return_value = [retrieved_point]

        results = await store.recall("anything", limit=5)

        # Hybrid search was used.
        hybrid.search.assert_called_once()
        # Enrichment pulled the missing fields from Qdrant.
        assert results[0]["id"] == "hyb-1"
        assert results[0]["category"] == "belief"
        assert results[0]["source_url"] == "https://example.com/belief"

    async def test_recall_legacy_hybrid_entry_coerced_to_fact(
        self, store, mock_qdrant_client
    ):
        # Hybrid returns an entry whose payload (after retrieve) has no category.
        hybrid = _FakeHybrid(results=[{"id": "h-legacy", "rrf_score": 0.04}])
        store._hybrid_retriever = hybrid

        retrieved = MagicMock()
        retrieved.id = "h-legacy"
        retrieved.payload = {"id": "h-legacy", "content": "old data", "access_level": "public"}
        # NOTE: no "category" key on purpose.
        mock_qdrant_client.retrieve.return_value = [retrieved]

        results = await store.recall("anything", limit=5)
        assert results[0]["category"] == "fact"


# ===========================================================================
# 5. The MCP tool dispatcher — handle_call_tool
# ===========================================================================


@pytest.fixture
def mcp_store(monkeypatch, mock_qdrant_client, isolated_env):
    """Build a fully-mocked MCP server ``MemoryStore`` AND patch ``get_store``.

    The dispatcher calls ``get_store()`` to obtain the singleton. We patch
    the global ``_store`` so every call gets our mock-backed instance.
    """
    monkeypatch.setattr(mcp, "EmbeddingProvider", _FakeEmbedder)
    monkeypatch.setattr(mcp.MemoryStore, "_init_hybrid", lambda self: None)
    mock_qdrant_client.get_collections.return_value = MagicMock(collections=[])
    s = mcp.MemoryStore()
    mcp._store = s
    return s


def _decode(text_payload: str) -> dict:
    """Helper — unwrap the JSON envelope returned by the MCP tool handler."""
    return json.loads(text_payload)


class TestCallToolRemember:
    """End-to-end test of the MCP ``remember`` tool dispatcher."""

    async def test_category_accepted_verbatim(self, mcp_store, mock_qdrant_client):
        out = await mcp.handle_call_tool("remember", {"text": "x", "category": "rule"})
        body = _decode(out[0].text)
        assert body["status"] == "ok"
        assert body["category"] == "rule"

    async def test_missing_category_coerced_to_fact(self, mcp_store):
        # Simulate an old client that doesn't send `category` at all.
        out = await mcp.handle_call_tool("remember", {"text": "x"})
        body = _decode(out[0].text)
        assert body["category"] == "fact"

    async def test_invalid_category_coerced_to_fact(self, mcp_store):
        out = await mcp.handle_call_tool("remember", {"text": "x", "category": "GIBBERISH"})
        body = _decode(out[0].text)
        assert body["category"] == "fact"

    async def test_empty_category_coerced_to_fact(self, mcp_store):
        out = await mcp.handle_call_tool("remember", {"text": "x", "category": ""})
        body = _decode(out[0].text)
        assert body["category"] == "fact"

    async def test_source_url_and_confidence_persist(
        self, mcp_store, mock_qdrant_client
    ):
        out = await mcp.handle_call_tool(
            "remember",
            {
                "text": "verified claim",
                "category": "fact",
                "source_url": "https://docs.example.com/claim",
                "confidence": 0.99,
                "source": "documentation",
            },
        )
        body = _decode(out[0].text)
        assert body["status"] == "ok"

        payload = mock_qdrant_client.upsert.call_args.kwargs["points"][0].payload
        assert payload["source_url"] == "https://docs.example.com/claim"
        assert payload["provenance"]["confidence"] == 0.99
        assert payload["provenance"]["source_url"] == "https://docs.example.com/claim"

    async def test_access_level_passed_through(self, mcp_store, mock_qdrant_client):
        await mcp.handle_call_tool(
            "remember",
            {"text": "x", "category": "fact", "access_level": "private"},
        )
        payload = mock_qdrant_client.upsert.call_args.kwargs["points"][0].payload
        assert payload["access_level"] == "private"

    async def test_invalid_access_level_falls_back_to_public(
        self, mcp_store, mock_qdrant_client
    ):
        await mcp.handle_call_tool(
            "remember",
            {"text": "x", "category": "fact", "access_level": "GOD_MODE"},
        )
        payload = mock_qdrant_client.upsert.call_args.kwargs["points"][0].payload
        assert payload["access_level"] == "public"

    async def test_oversize_content_emits_warning(self, mcp_store):
        long_text = "a" * 6000  # exceeds 5000 char guardrail
        out = await mcp.handle_call_tool(
            "remember", {"text": long_text, "category": "fact"}
        )
        body = _decode(out[0].text)
        assert "warnings" in body
        assert any("5000" in w for w in body["warnings"])


class TestCallToolRecall:
    """End-to-end test of the MCP ``recall`` tool dispatcher."""

    async def test_recall_returns_results_envelope(
        self, mcp_store, mock_qdrant_client
    ):
        # Vector-fallback path: hybrid is None by default.
        point = MagicMock()
        point.id = "p1"
        point.payload = _payload(pid="p1", category="fact")
        point.score = 0.91
        mock_qdrant_client.query_points.return_value = MagicMock(points=[point])

        out = await mcp.handle_call_tool("recall", {"query": "hello"})
        body = _decode(out[0].text)
        assert body["count"] == 1
        assert body["results"][0]["id"] == "p1"
        assert body["results"][0]["category"] == "fact"

    async def test_recall_hybrid_path(
        self, mcp_store, mock_qdrant_client
    ):
        # Inject a hybrid retriever that returns matches.
        mcp_store._hybrid_retriever = _FakeHybrid(
            results=[{"id": "h-1", "rrf_score": 0.04, "text": "hello"}]
        )
        # Qdrant retrieve fills in the missing fields.
        retrieved = MagicMock()
        retrieved.id = "h-1"
        retrieved.payload = _payload(pid="h-1", category="belief")
        mock_qdrant_client.retrieve.return_value = [retrieved]

        out = await mcp.handle_call_tool("recall", {"query": "hello"})
        body = _decode(out[0].text)
        assert body["count"] == 1
        assert body["results"][0]["category"] == "belief"

    async def test_recall_limit_capped_at_20(self, mcp_store):
        out = await mcp.handle_call_tool("recall", {"query": "x", "limit": 9999})
        body = _decode(out[0].text)
        # No error envelope — the handler just clamps. (No payload = 0 results.)
        assert "results" in body
        assert body["count"] == 0


# ===========================================================================
# 6. EmbeddingProvider — auto-detection
# ===========================================================================


class _FakeSentenceTransformer:
    """Stand-in for ``sentence_transformers.SentenceTransformer``.

    We only need a no-op ``encode`` that returns a 384-dim list of zeros.
    """

    def __init__(self, model_name: str):
        self.model_name = model_name

    def encode(self, text: str):
        return [0.0] * 384


def _install_fake_sentence_transformers(monkeypatch):
    """Patch the ``sentence_transformers`` module to a working stub.

    The MCP server imports it inside ``EmbeddingProvider._detect()``; this
    helper makes that import succeed without needing the real model.
    """
    fake_st = MagicMock()
    fake_st.SentenceTransformer = _FakeSentenceTransformer
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st)
    return fake_st


class TestEmbeddingProviderDetection:
    """`EmbeddingProvider._detect()` priority chain.

    We only check the *minimal* contract from the task: with no API key set
    the provider must fall through to ``sentence-transformers`` (or log a
    warning if that import also fails).
    """

    def test_falls_back_to_sentence_transformers_without_api_key(
        self, isolated_env, monkeypatch
    ):
        _install_fake_sentence_transformers(monkeypatch)

        # The MCP server probes Ollama on localhost:11434 before falling
        # through to sentence-transformers. Block that probe so the test
        # is hermetic regardless of whether the developer runs Ollama.
        self._block_ollama_probe(monkeypatch)

        ep = mcp.EmbeddingProvider()
        assert ep.name == "all-MiniLM-L6-v2"
        assert ep.dim == 384
        assert ep.available is True

    def test_no_provider_when_sentence_transformers_missing(
        self, isolated_env, monkeypatch
    ):
        # No API keys, and we sabotage the sentence-transformers import path
        # by removing the cached module AND raising on import.
        monkeypatch.delitem(sys.modules, "sentence_transformers", raising=False)
        self._block_ollama_probe(monkeypatch)

        import builtins

        real_import = builtins.__import__

        def _blocked(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "sentence_transformers" or name.startswith("sentence_transformers."):
                raise ImportError("blocked for test")
            return real_import(name, globals, locals, fromlist, level)

        monkeypatch.setattr(builtins, "__import__", _blocked)

        ep = mcp.EmbeddingProvider()
        # No provider found → still a "none" instance.
        assert ep.available is False

    @staticmethod
    def _block_ollama_probe(monkeypatch):
        """Make the ``requests.get("http://localhost:11434/api/tags")`` call
        inside ``EmbeddingProvider._detect()`` raise so the Ollama branch
        is skipped. The provider then falls through to sentence-transformers.
        """
        def _explode(*args, **kwargs):
            raise ConnectionError("Ollama disabled in tests")

        # Patch the requests module the MCP server imported, in case it's
        # already cached at module load time. We patch the binding inside
        # the mcp_server module specifically.
        if hasattr(mcp, "requests"):
            monkeypatch.setattr(mcp.requests, "get", _explode)
        else:
            import requests as _r
            monkeypatch.setattr(_r, "get", _explode)

    def test_voyage_key_with_correct_prefix_picks_voyage(self, isolated_env, monkeypatch):
        # Pretend the voyageai package is installed and returning a stub client.
        fake_voyage = MagicMock()
        fake_voyage.Client = MagicMock(return_value=MagicMock(name="voyage-client"))
        monkeypatch.setitem(sys.modules, "voyageai", fake_voyage)

        # The MCP server caches the env var at import time → also patch
        # the module-level constant.
        monkeypatch.setenv("VOYAGE_API_KEY", "vo-test-1234567890")
        monkeypatch.setattr(mcp, "VOYAGE_API_KEY", "vo-test-1234567890")

        ep = mcp.EmbeddingProvider()
        assert ep.name == "voyage-3-large"
        assert ep.dim == 1024
        assert ep.available is True

    def test_voyage_key_with_wrong_prefix_does_not_pick_voyage(
        self, isolated_env, monkeypatch
    ):
        # Wrong prefix → server should skip voyage and fall through to
        # sentence-transformers.
        monkeypatch.setenv("VOYAGE_API_KEY", "not-a-voyage-key")
        monkeypatch.setattr(mcp, "VOYAGE_API_KEY", "not-a-voyage-key")
        _install_fake_sentence_transformers(monkeypatch)

        ep = mcp.EmbeddingProvider()
        assert ep.name != "voyage-3-large", (
            "Bad-prefix VOYAGE_API_KEY must not be accepted as a voyage key"
        )


# ===========================================================================
# 7. Webhook Subscriptions — store, tools, and event dispatch
# ===========================================================================


@pytest.fixture
def webhook_store(tmp_path, monkeypatch):
    """A ``WebhookStore`` pointed at a per-test temp file.

    The store is also installed as the module-level singleton so the
    dispatcher (``dispatch_event`` → ``get_webhook_store()``) and the
    tool handlers see it.
    """
    store = mcp.WebhookStore(path=tmp_path / "webhooks.json")
    monkeypatch.setattr(mcp, "_webhook_store", store)
    return store


class TestWebhookStore:
    """Direct unit tests on the ``WebhookStore`` data layer."""

    async def test_subscribe_persists_to_disk(self, webhook_store, tmp_path):
        sub = await webhook_store.subscribe(
            "memory.remember", "https://example.com/hook"
        )
        assert sub["event_type"] == "memory.remember"
        assert sub["webhook_url"] == "https://example.com/hook"
        assert sub["id"]
        assert sub["created_at"]

        # The file must actually exist on disk and be valid JSON.
        on_disk = json.loads((tmp_path / "webhooks.json").read_text())
        assert on_disk["subscriptions"][0]["id"] == sub["id"]

    async def test_unsubscribe_removes_subscription(self, webhook_store):
        sub = await webhook_store.subscribe(
            "memory.forget", "https://example.com/forget-hook"
        )
        removed = await webhook_store.unsubscribe(sub["id"])
        assert removed is True
        assert await webhook_store.list() == []

    async def test_unsubscribe_unknown_id_returns_false(self, webhook_store):
        await webhook_store.subscribe(
            "memory.update", "https://example.com/x"
        )
        removed = await webhook_store.unsubscribe("nonexistent-id")
        assert removed is False
        # The real subscription is still there.
        subs = await webhook_store.list()
        assert len(subs) == 1

    async def test_list_subscriptions_returns_all(self, webhook_store):
        await webhook_store.subscribe("memory.remember", "https://a.example/h")
        await webhook_store.subscribe("memory.update", "https://b.example/h")
        await webhook_store.subscribe("memory.forget", "https://c.example/h")
        subs = await webhook_store.list()
        assert len(subs) == 3
        event_types = {s["event_type"] for s in subs}
        assert event_types == {"memory.remember", "memory.update", "memory.forget"}

    async def test_matching_filters_by_event_type(self, webhook_store):
        await webhook_store.subscribe("memory.remember", "https://a/h")
        await webhook_store.subscribe("memory.update", "https://b/h")
        matches = await webhook_store.matching("memory.remember")
        assert len(matches) == 1
        assert matches[0]["event_type"] == "memory.remember"

    async def test_unknown_event_type_rejected(self, webhook_store):
        with pytest.raises(ValueError, match="Unknown event_type"):
            await webhook_store.subscribe("memory.bogus", "https://x/h")

    @pytest.mark.parametrize("bad_url", ["", "ftp://x/y", "not-a-url", "javascript:alert(1)"])
    async def test_invalid_webhook_url_rejected(self, webhook_store, bad_url):
        with pytest.raises(ValueError, match="http:// or https://"):
            await webhook_store.subscribe("memory.remember", bad_url)

    async def test_empty_store_file_returns_empty_list(self, tmp_path):
        # A non-existent file must NOT raise — a fresh install starts empty.
        store = mcp.WebhookStore(path=tmp_path / "does_not_exist.json")
        assert await store.list() == []

    async def test_corrupt_store_file_does_not_crash(self, tmp_path):
        # A garbage file must NOT propagate the error — the store stays empty.
        path = tmp_path / "webhooks.json"
        path.write_text("{not valid json")
        store = mcp.WebhookStore(path=path)
        assert await store.list() == []


class TestWebhookTools:
    """End-to-end tests of the three webhook MCP tools."""

    async def test_subscribe_tool_returns_subscription(self, webhook_store):
        out = await mcp.handle_call_tool(
            "subscribe",
            {"event_type": "memory.remember", "webhook_url": "https://x/h"},
        )
        body = _decode(out[0].text)
        assert body["status"] == "subscribed"
        assert body["subscription"]["id"]
        assert body["subscription"]["event_type"] == "memory.remember"
        assert body["subscription"]["webhook_url"] == "https://x/h"

    async def test_subscribe_tool_rejects_bad_event_type(self, webhook_store):
        out = await mcp.handle_call_tool(
            "subscribe",
            {"event_type": "memory.bogus", "webhook_url": "https://x/h"},
        )
        body = _decode(out[0].text)
        assert body["status"] == "error"
        assert "Unknown event_type" in body["error"]

    async def test_subscribe_tool_rejects_bad_url(self, webhook_store):
        out = await mcp.handle_call_tool(
            "subscribe",
            {"event_type": "memory.remember", "webhook_url": "not-a-url"},
        )
        body = _decode(out[0].text)
        assert body["status"] == "error"

    async def test_unsubscribe_tool_removes_subscription(self, webhook_store):
        sub = await webhook_store.subscribe(
            "memory.remember", "https://x/h"
        )
        out = await mcp.handle_call_tool(
            "unsubscribe", {"subscription_id": sub["id"]}
        )
        body = _decode(out[0].text)
        assert body["status"] == "unsubscribed"
        assert body["removed"] is True
        assert await webhook_store.list() == []

    async def test_unsubscribe_tool_unknown_id_returns_not_found(self, webhook_store):
        out = await mcp.handle_call_tool(
            "unsubscribe", {"subscription_id": "no-such-id"}
        )
        body = _decode(out[0].text)
        assert body["status"] == "not_found"
        assert body["removed"] is False

    async def test_list_subscriptions_tool_shows_all(self, webhook_store):
        await webhook_store.subscribe("memory.remember", "https://a/h")
        await webhook_store.subscribe("memory.forget", "https://b/h")
        out = await mcp.handle_call_tool("list_subscriptions", {})
        body = _decode(out[0].text)
        assert body["count"] == 2
        urls = {s["webhook_url"] for s in body["subscriptions"]}
        assert urls == {"https://a/h", "https://b/h"}

    async def test_list_subscriptions_tool_empty(self, webhook_store):
        out = await mcp.handle_call_tool("list_subscriptions", {})
        body = _decode(out[0].text)
        assert body["count"] == 0
        assert body["subscriptions"] == []

    async def test_tool_schemas_listed(self):
        tools = await mcp.handle_list_tools()
        names = {t.name for t in tools}
        assert "subscribe" in names
        assert "unsubscribe" in names
        assert "list_subscriptions" in names

    async def test_subscribe_schema_marks_fields_required(self):
        tools = await mcp.handle_list_tools()
        sub = next(t for t in tools if t.name == "subscribe")
        assert set(sub.inputSchema["required"]) == {"event_type", "webhook_url"}
        # event_type must be a closed enum of the three valid event types.
        assert set(sub.inputSchema["properties"]["event_type"]["enum"]) == {
            "memory.remember", "memory.update", "memory.forget"
        }

    async def test_unsubscribe_schema_marks_id_required(self):
        tools = await mcp.handle_list_tools()
        unsub = next(t for t in tools if t.name == "unsubscribe")
        assert unsub.inputSchema["required"] == ["subscription_id"]


class TestWebhookEventDispatch:
    """``dispatch_event`` is wired into the existing tool handlers."""

    async def test_remember_fires_webhook_when_subscription_exists(
        self, mcp_store, webhook_store
    ):
        # Patch _post_webhook so we don't actually open a socket.
        captured = []
        async def _fake_post(url, payload):
            captured.append((url, dict(payload)))
        monkeypatch = pytest.MonkeyPatch()
        try:
            monkeypatch.setattr(mcp, "_post_webhook", _fake_post)
            await webhook_store.subscribe("memory.remember", "https://hook/x")

            out = await mcp.handle_call_tool(
                "remember", {"text": "hello webhook", "category": "fact"}
            )
            body = _decode(out[0].text)
            assert body["status"] == "ok"
            mem_id = body["id"]

            # The dispatcher schedules _post_webhook as a background task;
            # give the loop a chance to run it.
            await asyncio.sleep(0)
            await asyncio.sleep(0)

            assert len(captured) == 1
            url, payload = captured[0]
            assert url == "https://hook/x"
            assert payload["event"] == "memory.remember"
            assert payload["memory_id"] == mem_id
            assert "timestamp" in payload
        finally:
            monkeypatch.undo()

    async def test_remember_does_not_fire_webhook_without_subscription(
        self, mcp_store, webhook_store, monkeypatch
    ):
        # Patch _post_webhook so we can assert it is NEVER called.
        captured = []
        async def _fake_post(url, payload):
            captured.append((url, dict(payload)))
        monkeypatch.setattr(mcp, "_post_webhook", _fake_post)

        # No subscription registered.
        assert await webhook_store.list() == []

        out = await mcp.handle_call_tool(
            "remember", {"text": "no one listening", "category": "fact"}
        )
        body = _decode(out[0].text)
        assert body["status"] == "ok"
        # Drain the event loop so any spurious background task gets to run.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert captured == []

    async def test_remember_only_fires_for_matching_event_type(
        self, mcp_store, webhook_store, monkeypatch
    ):
        captured = []
        async def _fake_post(url, payload):
            captured.append((url, dict(payload)))
        monkeypatch.setattr(mcp, "_post_webhook", _fake_post)

        # Only subscribed to memory.update — remember() must not fire.
        await webhook_store.subscribe("memory.update", "https://hook/update")

        out = await mcp.handle_call_tool(
            "remember", {"text": "x", "category": "fact"}
        )
        assert _decode(out[0].text)["status"] == "ok"
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert captured == [], (
            "A memory.remember subscription must not fire for memory.update"
        )

    async def test_dispatcher_swallows_post_errors(
        self, mcp_store, webhook_store, monkeypatch
    ):
        # The dispatcher must NOT crash the main tool call when the
        # underlying HTTP POST raises. (We use a stub that raises.)
        async def _explode(url, payload):
            raise ConnectionError("simulated network failure")
        monkeypatch.setattr(mcp, "_post_webhook", _explode)
        await webhook_store.subscribe("memory.remember", "https://hook/x")

        out = await mcp.handle_call_tool(
            "remember", {"text": "x", "category": "fact"}
        )
        body = _decode(out[0].text)
        # The tool call itself still succeeds.
        assert body["status"] == "ok"
        # Give the background task a chance to run; it must have errored
        # internally without surfacing.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    async def test_dispatch_event_unknown_event_is_silent(
        self, mcp_store, monkeypatch
    ):
        # Unknown event types are silently ignored (defense in depth).
        captured = []
        async def _fake_post(url, payload):
            captured.append((url, payload))
        monkeypatch.setattr(mcp, "_post_webhook", _fake_post)
        # No subscription, but the unknown-event guard should still no-op.
        await mcp.dispatch_event("memory.bogus", "abc")
        await asyncio.sleep(0)
        assert captured == []

    async def test_subscriptions_survive_new_webhookstore_instance(
        self, webhook_store, tmp_path
    ):
        # What we write to disk must come back when we instantiate a
        # second WebhookStore over the same file. This is the persistence
        # contract that lets subscriptions survive server restarts.
        sub = await webhook_store.subscribe(
            "memory.remember", "https://hook/x"
        )
        store2 = mcp.WebhookStore(path=tmp_path / "webhooks.json")
        subs = await store2.list()
        assert len(subs) == 1
        assert subs[0]["id"] == sub["id"]

