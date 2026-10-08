"""Tests for the security-review fixes in ``nexus_memory.embeddings``.

Covers the findings that are STILL part of the contract after the 08.10.2026
switch to named-vector collections (Regel A/B):

1. Provider dispatch: ``embed()`` dispatches on the stored backend type, never
   on the model name (Google's 'text-embedding-004' used to be sent through the
   OpenAI branch, which the google.generativeai module cannot serve).
2. Fail-closed explicit provider choice: a failed *explicitly configured*
   provider must never silently fall back to auto-detection — in particular not
   from a local backend to a cloud one.
3. ``auto`` stays local-first: plain detection never reaches the cloud, even
   with a cloud key present (Regel A.3).
4. The missing-backend case is reported at ERROR level, not silently swallowed
   (plugin contract: report, do not kill).

REMOVED with the old design — and therefore no longer tested here:
``_same_local_model`` (exact local-model identity), the collection drift guard
(``_read_existing_collection_model``) and ``NEXUS_ALLOWED_CLOUD_FALLBACK``. The
collection model now lives in Qdrant as a named vector; creation/mismatch/
legacy behaviour is covered by the Regel-B tests in ``test_collection_vectors``.

All Ollama / Google / OpenAI interactions are mocked — no network.
"""

from __future__ import annotations

import logging

import asyncio
import sys
import types
from unittest.mock import MagicMock

import pytest

import nexus_memory.embeddings as embeddings_module

from nexus_memory.embeddings import (
    CLOUD_PROVIDER_IDS,
    EmbeddingProvider,
)


# ---------------------------------------------------------------------------
# Helpers — construct providers without running auto-detection
# ---------------------------------------------------------------------------


def _make_provider(**attrs) -> EmbeddingProvider:
    """Build an EmbeddingProvider bypassing __init__ (no detection, no network)."""
    ep = EmbeddingProvider.__new__(EmbeddingProvider)
    ep._name = "none"
    ep._dim = 384
    ep._backend = "none"
    ep._client = None
    ep._model = None
    ep._preferred = ""
    for key, value in attrs.items():
        setattr(ep, key, value)
    return ep


def _fake_genai_module(monkeypatch) -> MagicMock:
    """Install a fake ``google.generativeai`` module and return it."""
    fake_genai = MagicMock(name="google.generativeai")
    fake_genai.configure = MagicMock()
    fake_genai.embed_content = MagicMock(return_value={"embedding": [0.25] * 768})
    fake_google = types.ModuleType("google")
    fake_google.generativeai = fake_genai
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.generativeai", fake_genai)
    return fake_genai


def _fake_ollama_tags(monkeypatch, models: list[str]) -> dict:
    """Patch ``requests.get`` for the Ollama /api/tags inventory call."""
    calls = {"get": []}

    def fake_get(url, timeout=None, **kwargs):
        calls["get"].append(url)
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"models": [{"name": m} for m in models]}
        return resp

    monkeypatch.setattr("requests.get", fake_get)
    return calls


def _fake_ollama_embed(monkeypatch, dim: int) -> list[tuple]:
    """Patch ``requests.post`` for the Ollama /api/embed probe."""
    posts = []

    def fake_post(url, json=None, timeout=None, **kwargs):
        posts.append((url, json))
        resp = MagicMock()
        resp.json.return_value = {"embeddings": [[0.0] * dim]}
        return resp

    monkeypatch.setattr("requests.post", fake_post)
    return posts


def _block_ollama(monkeypatch) -> None:
    """Make every Ollama HTTP call raise so detection skips the backend."""
    monkeypatch.setattr(
        "requests.get",
        lambda *a, **k: (_ for _ in ()).throw(ConnectionError("no ollama")),
    )
    monkeypatch.setattr(
        "requests.post",
        lambda *a, **k: (_ for _ in ()).throw(ConnectionError("no ollama")),
    )


def _fake_local_hf(monkeypatch, dim: int = 1024) -> None:
    """Install a fake SentenceTransformer so the local route needs no download."""

    class _FakeModel:
        def __init__(self, name):  # noqa: D107
            self.name = name

        def encode(self, _text):  # noqa: D102
            return [0.0] * dim

    fake_st = types.ModuleType("sentence_transformers")
    fake_st.SentenceTransformer = _FakeModel
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_st)


def _no_cloud_keys(monkeypatch) -> None:
    """Remove every cloud key from the environment and the module constants."""
    for var in ("VOYAGE_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY", "JINA_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    for const in ("VOYAGE_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        if hasattr(embeddings_module, const):
            monkeypatch.setattr(embeddings_module, const, "")


def _block_local_hf(monkeypatch) -> None:
    """Make the local HuggingFace route unavailable so detection moves on."""
    monkeypatch.setattr(
        embeddings_module.EmbeddingProvider,
        "_try_sentence_transformers",
        lambda self: False,
    )


# ===========================================================================
# Finding 1 — backend-type dispatch (Google must not go through OpenAI API)
# ===========================================================================


class TestBackendDispatch:
    """embed() dispatches on the stored backend type, not the model name."""

    def test_google_model_dispatches_to_embed_content(self, isolated_env, monkeypatch):
        """'text-embedding-004' must call google.generativeai.embed_content."""
        fake_genai = _fake_genai_module(monkeypatch)
        ep = _make_provider(
            _name="text-embedding-004", _backend="google", _client=fake_genai
        )

        vector = asyncio.run(ep.embed("private memory text"))

        fake_genai.embed_content.assert_called_once_with(
            model="text-embedding-004", content="private memory text"
        )
        assert vector == [0.25] * 768
        # The OpenAI-style API must never be touched on the google module.
        assert (
            not hasattr(fake_genai, "embeddings")
            or not isinstance(getattr(fake_genai, "embeddings", None), MagicMock)
            or fake_genai.embeddings.create.call_count == 0
        )

    def test_openai_backend_still_uses_embeddings_create(self, isolated_env):
        """Regression guard: the OpenAI branch keeps its API shape."""
        openai_client = MagicMock(name="openai-client")
        openai_client.embeddings.create.return_value = MagicMock(
            data=[MagicMock(embedding=[0.5] * 1536)]
        )
        ep = _make_provider(
            _name="text-embedding-3-small", _backend="openai",
            _client=openai_client,
        )

        vector = asyncio.run(ep.embed("text"))

        openai_client.embeddings.create.assert_called_once_with(
            model="text-embedding-3-small", input=["text"]
        )
        assert len(vector) == 1536

    def test_try_google_records_backend_type(self, isolated_env, monkeypatch):
        """_try_google must store the backend type alongside the model name."""
        fake_genai = _fake_genai_module(monkeypatch)
        monkeypatch.setattr(embeddings_module, "GOOGLE_API_KEY", "AIza-test")
        ep = _make_provider()

        assert ep._try_google() is True
        assert ep.backend == "google"
        assert ep.name == "text-embedding-004"
        assert fake_genai.configure.called

    def test_all_backends_record_distinct_dispatch_keys(self, isolated_env):
        """Backend types are the dispatch contract — no name-substring heuristics."""
        assert "google" in CLOUD_PROVIDER_IDS
        assert "openai" in CLOUD_PROVIDER_IDS
        ep = _make_provider(_name="text-embedding-004", _backend="google")
        assert ep.backend == "google"
        assert ep.backend != "openai"

    def test_provider_type_cloud_vs_local(self, isolated_env):
        assert _make_provider(_backend="google").provider_type == "cloud"
        assert _make_provider(_backend="ollama").provider_type == "local"
        assert _make_provider(_backend="none").provider_type == "none"


# ===========================================================================
# Finding 2 — fail-closed explicit provider choice (no silent cloud fallback)
# ===========================================================================


class TestFailClosedExplicitProvider:
    """A failed explicitly chosen provider must not leak texts to the cloud."""

    def test_explicit_ollama_failure_raises_no_cloud_fallback(
        self, isolated_env, monkeypatch
    ):
        """Explicit local provider down + cloud keys present → error."""
        monkeypatch.setenv("NEXUS_EMBEDDING_PROVIDER", "ollama")
        monkeypatch.setattr(embeddings_module, "VOYAGE_API_KEY", "vo-valid")
        monkeypatch.setattr(embeddings_module, "OPENAI_API_KEY", "sk-valid")
        _block_ollama(monkeypatch)

        with pytest.raises(RuntimeError, match="fail-closed|not available"):
            EmbeddingProvider(preferred="ollama")

    def test_explicit_local_failure_does_not_reach_sentence_transformers(
        self, isolated_env, monkeypatch
    ):
        """Fail-closed means fail-closed: no silent fallback, even to a local one."""
        monkeypatch.setenv("NEXUS_EMBEDDING_PROVIDER", "ollama")
        _block_ollama(monkeypatch)

        with pytest.raises(RuntimeError):
            EmbeddingProvider(preferred="ollama")

    def test_failed_explicit_cloud_choice_fails_closed_too(
        self, isolated_env, monkeypatch
    ):
        """A failed explicit cloud provider also raises instead of switching."""
        monkeypatch.setenv("NEXUS_EMBEDDING_PROVIDER", "voyage")
        monkeypatch.setattr(embeddings_module, "VOYAGE_API_KEY", "")  # unusable
        monkeypatch.setattr(embeddings_module, "OPENAI_API_KEY", "sk-valid")
        _block_ollama(monkeypatch)

        with pytest.raises(RuntimeError):
            EmbeddingProvider(preferred="voyage")

    def test_successful_explicit_provider_still_works(
        self, isolated_env, monkeypatch
    ):
        """The happy path is untouched: explicit voyage + working client."""
        monkeypatch.setattr(embeddings_module, "VOYAGE_API_KEY", "vo-valid")
        fake_voyage = MagicMock(name="voyageai")
        fake_voyage.Client = MagicMock(return_value=MagicMock())
        monkeypatch.setitem(sys.modules, "voyageai", fake_voyage)

        ep = EmbeddingProvider(preferred="voyage")

        assert ep.name == "voyage-4"
        assert ep.available is True


# ===========================================================================
# Finding 3 (still valid) — `auto` stays local, cloud needs an explicit choice
# ===========================================================================


class TestAutoStaysLocal:
    def test_preferred_auto_stays_local_even_with_cloud_key(
        self, isolated_env, monkeypatch
    ):
        """'auto' must NOT send a user to the cloud (Regel A.3)."""
        monkeypatch.setenv("NEXUS_EMBEDDING_PROVIDER", "auto")
        monkeypatch.setattr(embeddings_module, "VOYAGE_API_KEY", "vo-valid")
        fake_voyage = MagicMock(name="voyageai")
        fake_voyage.Client = MagicMock(return_value=MagicMock())
        monkeypatch.setitem(sys.modules, "voyageai", fake_voyage)
        _block_ollama(monkeypatch)
        _fake_local_hf(monkeypatch)

        ep = EmbeddingProvider(preferred="auto")

        assert ep.name == "Qwen/Qwen3-Embedding-0.6B"
        assert ep.dim == 1024
        assert ep.provider_type == "local"
        fake_voyage.Client.assert_not_called()

    def test_auto_with_cloud_key_and_local_down_never_reaches_cloud(
        self, isolated_env, monkeypatch
    ):
        """Plain 'auto' must never leave the machine, even with a cloud key.

        The cloud client must not even be CONSTRUCTED — that is the exact
        finding the public scanner reported twice.
        """
        monkeypatch.setenv("NEXUS_EMBEDDING_PROVIDER", "auto")
        monkeypatch.setenv("VOYAGE_API_KEY", "vo-test-1234567890")
        monkeypatch.setattr(embeddings_module, "VOYAGE_API_KEY", "vo-test-1234567890")
        fake_voyage = MagicMock(name="voyageai")
        fake_voyage.Client = MagicMock(return_value=MagicMock())
        monkeypatch.setitem(sys.modules, "voyageai", fake_voyage)
        _block_ollama(monkeypatch)
        _block_local_hf(monkeypatch)

        ep = EmbeddingProvider(preferred="auto")

        fake_voyage.Client.assert_not_called()
        assert ep.provider_type == "none", (
            "with no local backend and no explicit cloud opt-in the provider "
            f"must stay unset, not fall into the cloud (got {ep.provider_type})"
        )

    def test_auto_uses_local_hf_when_ollama_is_down(self, isolated_env, monkeypatch):
        """auto prefers the local HF default and never touches the cloud."""
        monkeypatch.setenv("NEXUS_EMBEDDING_PROVIDER", "auto")
        monkeypatch.setattr(embeddings_module, "VOYAGE_API_KEY", "vo-valid")
        fake_voyage = MagicMock(name="voyageai")
        fake_voyage.Client = MagicMock(return_value=MagicMock())
        monkeypatch.setitem(sys.modules, "voyageai", fake_voyage)
        _block_ollama(monkeypatch)
        _fake_local_hf(monkeypatch, dim=1024)

        ep = EmbeddingProvider(preferred="auto")

        assert ep.backend == "sentence-transformers"
        assert ep.provider_type == "local"
        fake_voyage.Client.assert_not_called()


# ===========================================================================
# Missing backend must be reported loudly (plugin contract: report, don't kill)
# ===========================================================================


class TestMissingBackendReportsLoudly:
    def test_auto_without_any_backend_reports_loudly(
        self, isolated_env, monkeypatch, caplog
    ):
        """A fresh install with nothing available must SAY so, not go quiet."""
        monkeypatch.setenv("NEXUS_EMBEDDING_PROVIDER", "auto")
        _block_ollama(monkeypatch)
        _block_local_hf(monkeypatch)
        _no_cloud_keys(monkeypatch)

        with caplog.at_level(logging.ERROR):
            ep = EmbeddingProvider(preferred="auto")

        assert ep.available is False
        assert ep.provider_type == "none"
        assert any(
            rec.levelno >= logging.ERROR and "No embedding provider" in rec.getMessage()
            for rec in caplog.records
        ), "the missing backend must be reported at ERROR level"

    def test_default_path_without_any_backend_reports_loudly(
        self, isolated_env, monkeypatch, caplog
    ):
        """Same guarantee for plain construction (no explicit preference)."""
        monkeypatch.setattr(embeddings_module, "_read_preferred_provider", lambda: "")
        _block_ollama(monkeypatch)
        _block_local_hf(monkeypatch)
        _no_cloud_keys(monkeypatch)

        with caplog.at_level(logging.ERROR):
            ep = EmbeddingProvider()

        assert ep.available is False
        assert any(
            rec.levelno >= logging.ERROR and "No embedding provider" in rec.getMessage()
            for rec in caplog.records
        )
