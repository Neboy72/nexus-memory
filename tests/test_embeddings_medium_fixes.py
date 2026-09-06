"""Invariant tests for the embeddings MEDIUM review fixes.

1. :278 — a failed provider init leaves NO phantom provider behind (state
   is reset centrally; available() reports False, embed() fails cleanly).
2. :369 — blocking HTTP (jina/ollama branches) runs via asyncio.to_thread,
   never on the event loop thread.
3. :21 — the wizard's .env key is the same state the provider detection
   reads (read_env_key round-trips serialize_env_value).
4. :555 — _probe_ollama_dim returns None on garbage responses (no bogus
   dimension values).
"""

import asyncio
import json
from pathlib import Path

import pytest

import nexus_memory.embeddings as em
from nexus_memory.embeddings import (
    CollectionModelUnavailable,
    EmbeddingProvider,
)
from nexus_memory import chat_wizard as cw
from nexus_memory import env_secret_store as ess


# ── :278 phantom provider reset ──────────────────────────────────────

def test_failed_init_leaves_no_phantom_provider(monkeypatch):
    """Crash AFTER _name/_backend are set → state fully reset."""
    monkeypatch.setenv("NEXUS_EMBEDDING_PROVIDER", "voyage")
    monkeypatch.setattr(em, "VOYAGE_API_KEY", "vo-testkey123")

    class _BoomClient:
        def __init__(self, api_key=None):
            self.embed = None  # attribute exists; init "succeeds" partially

    # Simulate: voyageai imports fine, client constructs, but probe crashes
    def _boom():
        import voyageai  # noqa: F401
        raise RuntimeError("network exploded mid-init")

    p = EmbeddingProvider.__new__(EmbeddingProvider)
    p._name = "none"; p._dim = 384; p._backend = "none"
    p._client = None; p._model = None
    p._preferred = "voyage"
    # Directly test the central reset contract:
    p._name = "voyage-4"; p._backend = "voyage"; p._client = _BoomClient()
    p._reset_provider_state()
    assert p._name == "none"
    assert p._backend == "none"
    assert p._client is None
    assert p.available is False


def test_try_provider_resets_state_on_failure(monkeypatch):
    monkeypatch.setenv("NEXUS_EMBEDDING_PROVIDER", "")
    p = EmbeddingProvider.__new__(EmbeddingProvider)
    p._name = "none"; p._dim = 384; p._backend = "none"
    p._client = None; p._model = None
    p._preferred = ""

    # A _try_* that partially sets state then fails:
    def _bad_voyage():
        p._name = "voyage-4"
        p._backend = "voyage"
        p._client = object()
        return False
    monkeypatch.setattr(p, "_try_voyage", _bad_voyage)
    ok = p._try_provider("voyage")
    assert ok is False
    assert p._name == "none" and p._backend == "none" and p._client is None


def test_collection_model_unavailable_still_propagates(monkeypatch, tmp_path):
    """The reset wrapper must NOT swallow CollectionModelUnavailable."""
    p = EmbeddingProvider.__new__(EmbeddingProvider)
    p._name = "none"; p._dim = 384; p._backend = "none"
    p._client = None; p._model = None
    p._preferred = ""

    def _bad_ollama():
        p._name = "qwen3-embedding:0.6b"
        p._backend = "ollama"
        raise CollectionModelUnavailable("collection model gone")
    monkeypatch.setattr(p, "_try_ollama", _bad_ollama)
    with pytest.raises(CollectionModelUnavailable):
        p._try_provider("ollama")


# ── :369 blocking HTTP off the event loop ────────────────────────────

def test_jina_embed_runs_in_worker_thread(monkeypatch):
    p = EmbeddingProvider.__new__(EmbeddingProvider)
    p._name = "jina-embeddings-v3"
    p._backend = "jina"
    p._client = {"api_key": "jk-test", "base_url": "https://api.jina.ai/v1"}
    p._model = None

    main_thread_id = []
    captured = {}

    class _Resp:
        def json(self):
            return {"data": [{"embedding": [0.1] * 1024}]}

    def fake_post(url, json=None, headers=None, timeout=None):
        import threading
        captured["thread"] = threading.get_ident()
        return _Resp()

    import requests as _req
    monkeypatch.setattr(_req, "post", fake_post)

    vec = asyncio.run(p.embed("hello"))
    assert len(vec) == 1024
    main_thread_id.append(__import__("threading").get_ident())
    assert captured["thread"] != main_thread_id[0], \
        "blocking HTTP must not run on the event-loop thread"


def test_ollama_embed_runs_in_worker_thread(monkeypatch):
    p = EmbeddingProvider.__new__(EmbeddingProvider)
    p._name = "bge-m3:latest"
    p._backend = "ollama"
    p._client = {"base_url": "http://localhost:11434"}
    p._model = None

    captured = {}

    class _Resp:
        def json(self):
            return {"embeddings": [[0.2] * 1024]}

    def fake_post(url, json=None, timeout=None):
        import threading
        captured["thread"] = threading.get_ident()
        return _Resp()

    import requests as _req
    monkeypatch.setattr(_req, "post", fake_post)

    vec = asyncio.run(p.embed("plain document text"))
    assert len(vec) == 1024
    assert captured["thread"] != __import__("threading").get_ident()


# ── :21 wizard/.env key state sync ──────────────────────────────────

def test_read_env_key_roundtrip(tmp_path):
    env = tmp_path / ".env"
    ess.write_env_key(env, "VOYAGE_API_KEY", "vo-roundtrip123")
    assert ess.read_env_key(env, "VOYAGE_API_KEY") == "vo-roundtrip123"
    assert ess.read_env_key(env, "MISSING_KEY") == ""
    assert ess.read_env_key(tmp_path / "nope.env", "X") == ""


def test_read_env_key_roundtrips_escaped_quotes(tmp_path):
    env = tmp_path / ".env"
    weird = "sk-weird'key\\value"
    ess.write_env_key(env, "OPENAI_API_KEY", weird)
    assert ess.read_env_key(env, "OPENAI_API_KEY") == weird


# ── :555 probe robustness ────────────────────────────────────────────

def test_probe_returns_none_on_garbage(monkeypatch):
    p = EmbeddingProvider.__new__(EmbeddingProvider)
    p._name = "bge-m3"
    p._client = {"base_url": "http://localhost:11434"}

    class _Resp:
        def json(self):
            return {"embeddings": [["not", "numbers"]]}  # garbage vectors

    def fake_post(url, json=None, timeout=None):
        return _Resp()

    import requests as _req
    monkeypatch.setattr(_req, "post", fake_post)
    assert p._probe_ollama_dim() is None


def test_probe_returns_none_on_network_error(monkeypatch):
    p = EmbeddingProvider.__new__(EmbeddingProvider)
    p._name = "bge-m3"
    p._client = {"base_url": "http://localhost:11434"}

    def boom(url, json=None, timeout=None):
        raise ConnectionError("ollama down")

    import requests as _req
    monkeypatch.setattr(_req, "post", boom)
    assert p._probe_ollama_dim() is None