"""The local /embed endpoint: the engine's provider, for the native plugins.

Why this exists as a test: the two plugins that talk to Qdrant directly
(Claude Code, OpenClaw) used to embed on their own with their own provider
list. A machine without Ollama had no way in. The endpoint is the contract
those plugins now depend on, so its behaviour is pinned here:

1. a good request returns the vector, the model name and the dimension
2. a missing/empty/wrong-typed text is rejected (400), never embedded
3. an oversized text is rejected before it reaches the model (413)
4. no provider available is a 503 with a hint, never a silent empty vector
5. the endpoint never blocks the event loop on store setup
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


class _FakeEmbedder:
    def __init__(self, available: bool = True, dim: int = 4, fail: bool = False):
        self.available = available
        self.name = "fake-embedding-model"
        self._dim = dim
        self._fail = fail
        self.calls = []

    async def embed(self, text: str, is_query: bool = True):
        self.calls.append((text, is_query))
        if self._fail:
            raise RuntimeError("backend exploded")
        return [0.25] * self._dim


class _FakeStore:
    def __init__(self, embedder):
        self._embedder = embedder


def _client(monkeypatch, embedder):
    """A TestClient with get_store() replaced — no Qdrant, no model load."""
    from starlette.testclient import TestClient
    import nexus_memory.mcp_server as mcp

    monkeypatch.setattr(mcp, "get_store", lambda: _FakeStore(embedder))
    # No context manager: the MCP session manager is not needed for /embed and
    # starting it would only couple this test to the MCP transport.
    return TestClient(mcp.build_serve_app()), embedder


def test_returns_vector_model_and_dimension(monkeypatch):
    client, embedder = _client(monkeypatch, _FakeEmbedder(dim=6))
    r = client.post("/embed", json={"text": "remember this"})
    assert r.status_code == 200
    body = r.json()
    assert len(body["embedding"]) == 6
    assert body["model"] == "fake-embedding-model"
    assert body["dim"] == 6
    # Default is the query side: the plugins embed queries far more often.
    assert embedder.calls == [("remember this", True)]


def test_document_side_is_forwarded(monkeypatch):
    client, embedder = _client(monkeypatch, _FakeEmbedder())
    r = client.post("/embed", json={"text": "a stored fact", "is_query": False})
    assert r.status_code == 200
    assert embedder.calls == [("a stored fact", False)]


@pytest.mark.parametrize("payload", [
    {},                      # nothing
    {"text": ""},            # empty
    {"text": "   "},         # whitespace only
    {"text": 42},            # wrong type
    {"text": ["a", "b"]},    # wrong type
])
def test_rejects_requests_without_usable_text(monkeypatch, payload):
    client, embedder = _client(monkeypatch, _FakeEmbedder())
    r = client.post("/embed", json=payload)
    assert r.status_code == 400
    assert "text" in r.json()["error"]
    assert embedder.calls == [], "a rejected request must never reach the model"


def test_rejects_non_json_body(monkeypatch):
    client, _ = _client(monkeypatch, _FakeEmbedder())
    r = client.post("/embed", content=b"not json at all")
    assert r.status_code == 400


def test_oversized_text_is_rejected_before_the_model(monkeypatch):
    import nexus_memory.mcp_server as mcp

    client, embedder = _client(monkeypatch, _FakeEmbedder())
    r = client.post("/embed", json={"text": "x" * (mcp.MAX_EMBED_CHARS + 1)})
    assert r.status_code == 413
    assert str(mcp.MAX_EMBED_CHARS) in r.json()["error"]
    assert embedder.calls == [], "the bound must hold before the model runs"


def test_text_at_the_bound_is_accepted(monkeypatch):
    import nexus_memory.mcp_server as mcp

    client, _ = _client(monkeypatch, _FakeEmbedder())
    r = client.post("/embed", json={"text": "x" * mcp.MAX_EMBED_CHARS})
    assert r.status_code == 200


def test_no_provider_is_a_loud_503_with_a_hint(monkeypatch):
    client, _ = _client(monkeypatch, _FakeEmbedder(available=False))
    r = client.post("/embed", json={"text": "hello"})
    assert r.status_code == 503
    assert "embedding provider" in r.json()["error"]
    assert "hint" in r.json()


def test_store_failure_is_a_503_not_a_crash(monkeypatch):
    from starlette.testclient import TestClient
    import nexus_memory.mcp_server as mcp

    def _boom():
        raise RuntimeError("qdrant is gone")

    monkeypatch.setattr(mcp, "get_store", _boom)
    r = TestClient(mcp.build_serve_app()).post("/embed", json={"text": "hello"})
    assert r.status_code == 503


def test_embedding_failure_is_reported_not_swallowed(monkeypatch):
    client, _ = _client(monkeypatch, _FakeEmbedder(fail=True))
    r = client.post("/embed", json={"text": "hello"})
    assert r.status_code == 500
    assert "RuntimeError" in r.json()["error"], (
        "the failure class must surface — a silent empty vector is the defect "
        "(the plugins would write nothing and look fine)"
    )


def test_route_is_registered_in_the_served_app(monkeypatch):
    import nexus_memory.mcp_server as mcp

    paths = [getattr(r, "path", "") for r in mcp.build_serve_app().routes]
    assert "/embed" in paths, "the plugins depend on this route existing"
    assert "/healthz" in paths
