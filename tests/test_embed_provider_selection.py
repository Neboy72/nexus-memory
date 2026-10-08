"""Tests for qwen3-embedding priority + Instruct prefix (Regel A, local-first).

Design-Entscheidung 08.10.2026: Ollama serves exactly ONE embedding model
(``qwen3-embedding``, 1024d). The old multi-model fallback (bge-m3 etc.) and
the collection drift-guard were removed together with the recorded-provider
machinery — the collection model now lives in Qdrant as a named vector
(rule B, ``nexus_memory.collection_vectors``). Tests exercising those removed
paths were deleted with them.
"""
import asyncio

import pytest

from nexus_memory import embeddings as emb_mod
from nexus_memory.embeddings import EmbeddingProvider


class _Resp:
    def __init__(self, status_code=200, models=None):
        self.status_code = status_code
        self._models = models or []

    def json(self):
        return {"models": [{"name": m} for m in self._models]}


def _patch_tags(monkeypatch, models):
    """Patch requests.get so /api/tags returns the given model list."""
    import requests as _requests
    monkeypatch.setattr(_requests, "get", lambda *a, **k: _Resp(models=models))


def _patch_probe(monkeypatch, dim=1024):
    """Patch _probe_ollama_dim so no real Ollama call happens."""
    monkeypatch.setattr(EmbeddingProvider, "_probe_ollama_dim", lambda self: dim)


def _clear_env(monkeypatch):
    for var in ("VOYAGE_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY",
                "JINA_API_KEY", "NEXUS_EMBEDDING_PROVIDER"):
        monkeypatch.delenv(var, raising=False)
    # neutralize config-based preference
    monkeypatch.setattr(emb_mod, "_read_preferred_provider", lambda: "")


def _bare_provider() -> EmbeddingProvider:
    p = EmbeddingProvider.__new__(EmbeddingProvider)
    p._name = "none"; p._dim = 384; p._client = None; p._model = None
    p._backend = "none"; p._preferred = ""
    return p


def test_ollama_uses_qwen3_embedding(monkeypatch):
    """Ollama route binds qwen3-embedding — the single supported model."""
    _patch_tags(monkeypatch, ["qwen3-embedding:0.6b", "llama3.2:latest"])
    _patch_probe(monkeypatch, 1024)
    _clear_env(monkeypatch)
    p = _bare_provider()
    assert p._try_ollama() is True
    assert p.name.startswith("qwen3-embedding")
    assert p.dim == 1024
    assert p.backend == "ollama"


def test_ollama_without_qwen3_is_unavailable(monkeypatch):
    """No qwen3-embedding in the inventory → the local route is unavailable.

    The old code silently fell back to bge-m3/nomic; that fallback was
    removed on purpose (one collection, one model — never a quiet mix).
    """
    _patch_tags(monkeypatch, ["bge-m3:latest", "nomic-embed-text:latest"])
    _patch_probe(monkeypatch, 1024)
    _clear_env(monkeypatch)
    p = _bare_provider()
    assert p._try_ollama() is False
    assert p.name == "none"


def test_qwen3_query_gets_instruct_prefix(monkeypatch):
    """Query embed calls (is_query defaults to True) carry the Instruct prefix.

    Document embeddings are produced with is_query=False and stay plain — the
    prefix is instruction-aware and applies to queries only.
    """
    captured = {}

    class _RespJson:
        def json(self):
            return {"embeddings": [[0.1] * 1024]}

    def fake_post(url, json=None, timeout=None):
        captured["json"] = json
        return _RespJson()

    monkeypatch.setattr("requests.post", fake_post)
    p = _bare_provider()
    p._name = "qwen3-embedding:0.6b"
    p._dim = 1024
    p._backend = "ollama"
    p._client = {"base_url": "http://localhost:11434"}
    vec = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        p.embed("wo ist Bleki geboren"))
    assert len(vec) == 1024
    sent = captured["json"]
    assert sent["model"] == "qwen3-embedding:0.6b"
    assert sent["input"][0].startswith(
        "Instruct: retrieve the relevant memory for the user query. Query: ")
    assert "wo ist Bleki geboren" in sent["input"][0]


def test_non_qwen3_model_gets_no_instruct_prefix(monkeypatch):
    """Only qwen3 query embeddings get the Instruct prefix."""
    captured = {}

    class _RespJson:
        def json(self):
            return {"embeddings": [[0.1] * 1024]}

    def fake_post(url, json=None, timeout=None):
        captured["json"] = json
        return _RespJson()

    monkeypatch.setattr("requests.post", fake_post)
    p = _bare_provider()
    p._name = "some-other-model"
    p._dim = 1024
    p._backend = "ollama"
    p._client = {"base_url": "http://localhost:11434"}
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        p.embed("irgendein text"))
    assert captured["json"]["input"][0] == "irgendein text"
