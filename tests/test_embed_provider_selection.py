"""Tests for qwen3-embedding priority + Instruct prefix + collection drift guard."""
import json
import pytest

from nexus_memory import embeddings as emb_mod
from nexus_memory.embeddings import (
    EmbeddingProvider,
    _same_local_model,
    _read_existing_collection_model,
)


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


def test_ollama_prefers_qwen3_over_bge(monkeypatch):
    _patch_tags(monkeypatch, ["bge-m3:latest", "qwen3-embedding:0.6b", "nomic-embed-text:latest"])
    _patch_probe(monkeypatch, 1024)
    _clear_env(monkeypatch)
    p = EmbeddingProvider.__new__(EmbeddingProvider)
    p._name = "none"; p._dim = 384; p._client = None; p._model = None
    p._preferred = ""
    assert p._try_ollama() is True
    assert p.name == "qwen3-embedding:0.6b"
    assert p.dim == 1024


def test_ollama_falls_back_to_bge(monkeypatch):
    _patch_tags(monkeypatch, ["bge-m3:latest", "nomic-embed-text:latest"])
    _patch_probe(monkeypatch, 1024)
    _clear_env(monkeypatch)
    p = EmbeddingProvider.__new__(EmbeddingProvider)
    p._name = "none"; p._dim = 384; p._client = None; p._model = None
    p._preferred = ""
    assert p._try_ollama() is True
    assert p.name == "bge-m3:latest"


def test_drift_guard_keeps_existing_model(monkeypatch, tmp_path):
    """Collection already uses bge-m3 → must stay on bge-m3 even though qwen3 exists."""
    cfg_dir = tmp_path / "nexus"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(json.dumps({"embedding_model": "bge-m3:latest"}))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(emb_mod2 := emb_mod2, "", None) if False else None
    _patch_tags(monkeypatch, ["bge-m3:latest", "qwen3-embedding:0.6b"])
    _patch_probe(monkeypatch, 1024)
    _clear_env(monkeypatch)
    p = EmbeddingProvider.__new__(EmbeddingProvider)
    p._name = "none"; p._dim = 384; p._client = None; p._model = None
    p._preferred = ""
    assert p._try_ollama() is True
    assert p.name == "bge-m3:latest"


def test_same_local_model_tolerates_tags():
    assert _same_local_model("bge-m3", "bge-m3:latest") is True
    assert _same_local_model("qwen3-embedding:0.6b", "qwen3-embedding") is True
    assert _same_local_model("bge-m3", "qwen3-embedding:0.6b") is False
    assert _same_local_model("", "bge-m3") is False


def test_qwen3_query_gets_instruct_prefix(monkeypatch):
    """Query embed calls must carry the Instruct prefix; doc-style calls (same method) too —
    prefix lives in embed(), so a call always shows the prefix for qwen3 models."""
    captured = {}

    class _PostResp:
        def json(self):
            return {"embeddings": [[0.1] * 1024]}

    import requests as _requests
    def fake_post(url, json=None, timeout=None):
        captured["json"] = json
        return _RespJson()
    class _RespJson:
        def json(self):
            return {"embeddings": [[0.1] * 1024]}

    monkeypatch.setattr("requests.post", fake_post)
    p = EmbeddingProvider.__new__(EmbeddingProvider)
    p._name = "qwen3-embedding:0.6b"
    p._dim = 1024
    p._model = None
    p._client = {"base_url": "http://localhost:11434"}
    import asyncio
    vec = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        p.embed("wo ist Bleki geboren"))
    assert len(vec) == 1024
    sent = captured["json"]
    assert sent["model"] == "qwen3-embedding:0.6b"
    assert sent["input"][0].startswith("Instruct: retrieve the relevant memory for the user query. Query: ")
    assert "wo ist Bleki geboren" in sent["input"][0]


def test_bge_no_instruct_prefix(monkeypatch):
    captured = {}
    class _RespJson:
        def json(self):
            return {"embeddings": [[0.1] * 1024]}
    def fake_post(url, json=None, timeout=None):
        captured["json"] = json
        return _RespJson()
    monkeypatch.setattr("requests.post", fake_post)
    p = EmbeddingProvider.__new__(EmbeddingProvider)
    p._name = "bge-m3"
    p._dim = 1024
    p._model = None
    p._client = {"base_url": "http://localhost:11434"}
    import asyncio
    vec = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        p.embed("irgendein text"))
    assert captured["json"]["input"][0] == "irgendein text"
