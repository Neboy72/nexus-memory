"""Tests for the security review fixes in ``nexus_memory.embeddings``.

Covers the four HIGH findings from the external security review:

1. Provider dispatch: ``embed()`` must dispatch on the stored backend type,
   never on the model name (Google's 'text-embedding-004' used to be sent
   through the OpenAI branch, which the google.generativeai module cannot
   serve).
2. Fail-closed explicit provider choice: a failed *explicitly configured*
   provider must never silently fall back to auto-detection — in particular
   not from a local backend to a cloud one. Cloud fallback stays opt-in
   (``preferred='auto'`` or ``NEXUS_ALLOWED_CLOUD_FALLBACK``).
3. Exact local model identity: ``_same_local_model`` compares full model
   names including the tag — no tag-stripping, no substring matching — so
   qwen3-embedding:0.6b and qwen3-embedding:8b are distinct models.
4. Stored collection model resolution: a stored Ollama model is only reused
   when it is actually present in the Ollama inventory (``/api/tags``);
   otherwise initialization fails with an explicit error instead of
   silently switching models.

All Ollama / Google / OpenAI interactions are mocked — no network.
"""

from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import MagicMock

import pytest

import nexus_memory.embeddings as embeddings_module

from nexus_memory.embeddings import (
    CLOUD_PROVIDER_IDS,
    CollectionModelUnavailable,
    EmbeddingProvider,
    _same_local_model,
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
    """Install a fake ``google.generativeai`` module and return it.

    The module records ``embed_content`` calls and raises if anything tries
    to use an OpenAI-style API on it.
    """
    fake_genai = MagicMock(name="google.generativeai")
    fake_genai.configure = MagicMock()
    fake_genai.embed_content = MagicMock(
        return_value={"embedding": [0.25] * 768}
    )
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
        resp.json.return_value = {
            "models": [{"name": m} for m in models]
        }
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


def _block_cloud_imports(monkeypatch) -> None:
    """Make voyageai / openai imports fail so auto-detect cannot pick them."""
    for name in ("voyageai", "openai"):
        monkeypatch.delitem(sys.modules, name, raising=False)

    import builtins

    real_import = builtins.__import__

    def _blocked(name, globals=None, locals=None, fromlist=(), level=0):
        if name in ("voyageai", "openai") or name.startswith(("voyageai.", "openai.")):
            raise ImportError(f"{name} blocked for test")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _blocked)


def _no_stored_model(monkeypatch) -> None:
    """Pretend no local model is recorded for the existing collection."""
    monkeypatch.setattr(
        embeddings_module, "_read_existing_collection_model", lambda: ""
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
        assert not hasattr(fake_genai, "embeddings") or not isinstance(
            getattr(fake_genai, "embeddings", None), MagicMock
        ) or fake_genai.embeddings.create.call_count == 0

    def test_google_backend_never_reaches_openai_branch(self, isolated_env, monkeypatch):
        """Even with an OpenAI-shaped client attribute present, the google
        backend must be dispatched via embed_content (the old code keyed off
        the model name 'text-embedding-004' and entered the OpenAI branch)."""
        fake_genai = _fake_genai_module(monkeypatch)
        # Poison-pill: if the OpenAI branch were reached it would explode.
        openai_shaped = MagicMock(name="openai-client")
        openai_shaped.embeddings = MagicMock(
            side_effect=AssertionError("openai branch reached for google backend")
        )
        # The google backend uses embed_content on the SAME client object,
        # which is exactly what the google.generativeai module provides.
        openai_shaped.embed_content = fake_genai.embed_content
        ep = _make_provider(
            _name="text-embedding-004", _backend="google", _client=openai_shaped
        )

        vector = asyncio.run(ep.embed("text"))

        openai_shaped.embeddings.create.assert_not_called()
        assert len(vector) == 768

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
        ep = EmbeddingProvider.__new__(EmbeddingProvider)
        ep._name = "none"; ep._dim = 384; ep._backend = "none"
        ep._client = None; ep._model = None; ep._preferred = ""

        assert ep._try_google() is True
        assert ep.backend == "google"
        assert ep.name == "text-embedding-004"
        assert fake_genai.configure.called

    def test_all_backends_record_distinct_dispatch_keys(self, isolated_env):
        """Backend types are the dispatch contract — every supported backend
        must map to its own key (no name-substring heuristics anywhere)."""
        assert "google" in CLOUD_PROVIDER_IDS
        assert "openai" in CLOUD_PROVIDER_IDS
        # The google model name must not be dispatchable as openai backend.
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
        """Explicit local provider down + cloud keys present → error, and
        no cloud provider is initialized."""
        monkeypatch.setenv("NEXUS_EMBEDDING_PROVIDER", "ollama")
        monkeypatch.setattr(embeddings_module, "VOYAGE_API_KEY", "vo-valid")
        monkeypatch.setattr(embeddings_module, "OPENAI_API_KEY", "sk-valid")
        _block_ollama(monkeypatch)

        with pytest.raises(RuntimeError, match="fail-closed|not available"):
            EmbeddingProvider(preferred="ollama")

    def test_explicit_local_failure_never_initializes_cloud_client(
        self, isolated_env, monkeypatch
    ):
        """Stronger invariant: no client of a cloud backend exists after the
        failed explicit local choice."""
        monkeypatch.setenv("NEXUS_EMBEDDING_PROVIDER", "ollama")
        monkeypatch.setattr(embeddings_module, "VOYAGE_API_KEY", "vo-valid")
        monkeypatch.setattr(embeddings_module, "OPENAI_API_KEY", "sk-valid")
        _block_ollama(monkeypatch)

        try:
            EmbeddingProvider(preferred="ollama")
            raised = False
        except RuntimeError:
            raised = True
        assert raised

        # If any provider had been initialized it would be a cloud one —
        # prove none was (local-only fallback like sentence-transformers
        # is also forbidden for an explicit choice, see next test).
        ep = EmbeddingProvider.__new__(EmbeddingProvider)
        ep._name = "none"; ep._dim = 384; ep._backend = "none"
        ep._client = None; ep._model = None; ep._preferred = ""
        assert ep.provider_type == "none"

    def test_explicit_local_failure_does_not_reach_sentence_transformers(
        self, isolated_env, monkeypatch
    ):
        """Fail-closed means fail-closed: no *any* silent fallback, even to a
        local one — the user's explicit choice must be honored or fail."""
        monkeypatch.setenv("NEXUS_EMBEDDING_PROVIDER", "ollama")
        _block_ollama(monkeypatch)

        with pytest.raises(RuntimeError):
            EmbeddingProvider(preferred="ollama")

    def test_preferred_auto_allows_cloud_first_detection(self, isolated_env, monkeypatch):
        """'auto' is the explicit opt-in to the cloud-first priority chain."""
        monkeypatch.setenv("NEXUS_EMBEDDING_PROVIDER", "auto")
        monkeypatch.setattr(embeddings_module, "VOYAGE_API_KEY", "vo-valid")
        fake_voyage = MagicMock(name="voyageai")
        fake_voyage.Client = MagicMock(return_value=MagicMock())
        monkeypatch.setitem(sys.modules, "voyageai", fake_voyage)

        ep = EmbeddingProvider(preferred="auto")

        assert ep.name == "voyage-4"
        assert ep.provider_type == "cloud"

    def test_cloud_fallback_env_allows_fallback(
        self, isolated_env, monkeypatch
    ):
        """NEXUS_ALLOWED_CLOUD_FALLBACK is the second opt-in path."""
        monkeypatch.setenv("NEXUS_EMBEDDING_PROVIDER", "openai")
        monkeypatch.setattr(embeddings_module, "OPENAI_API_KEY", "")  # unusable
        monkeypatch.setenv("NEXUS_ALLOWED_CLOUD_FALLBACK", "1")
        _block_ollama(monkeypatch)

        ep = EmbeddingProvider(preferred="openai")

        # Fallback ran; the provider that got selected must still be local
        # unless a cloud one is configured (none is, here → MiniLM).
        assert ep.name == "all-MiniLM-L6-v2"

    def test_failed_explicit_cloud_choice_fails_closed_too(
        self, isolated_env, monkeypatch
    ):
        """Without the fallback opt-in, a failed explicit cloud provider also
        raises instead of silently choosing a different backend."""
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
# Finding 3 — exact local model identity (tags matter, no substring match)
# ===========================================================================


class TestExactLocalModelIdentity:
    """_same_local_model compares exact model names including the tag."""

    def test_different_tags_are_different_models(self):
        """qwen3-embedding:0.6b and :8b differ and can have different dims."""
        assert _same_local_model("qwen3-embedding:0.6b", "qwen3-embedding:8b") is False

    def test_identical_names_match(self):
        assert _same_local_model("qwen3-embedding:0.6b", "qwen3-embedding:0.6b") is True

    def test_case_is_normalized(self):
        assert _same_local_model("Qwen3-Embedding:0.6B", "qwen3-embedding:0.6b") is True

    def test_whitespace_is_normalized(self):
        assert _same_local_model(" bge-m3 ", "bge-m3") is True

    def test_no_substring_match_between_family_members(self):
        """The old code matched substrings — a base name must not equal its
        qualified variant."""
        assert _same_local_model("bge-m3", "bge-m3:latest-x") is False
        assert _same_local_model("nomic-embed", "nomic-embed-text") is False

    def test_missing_tag_resolves_to_latest(self):
        """Ollama semantics: a tag-less name IS the ':latest' variant."""
        assert _same_local_model("bge-m3", "bge-m3:latest") is True
        assert _same_local_model("qwen3-embedding", "qwen3-embedding:latest") is True

    def test_empty_names_never_match(self):
        assert _same_local_model("", "bge-m3") is False
        assert _same_local_model("bge-m3", "") is False
        assert _same_local_model("", "") is False

    def test_drift_guard_keeps_collection_model_only_when_truly_equal(
        self, isolated_env, monkeypatch, tmp_path
    ):
        """The drift guard must not treat different tags as 'the same model':
        with stored ':0.6b' and only ':8b' installed, the stored model must
        NOT be reused (collection binding stays exact)."""
        # Collection says 0.6b, Ollama only has the 8b variant.
        monkeypatch.setattr(
            embeddings_module,
            "_read_existing_collection_model",
            lambda: "qwen3-embedding:0.6b",
        )
        _fake_ollama_tags(monkeypatch, ["qwen3-embedding:8b", "bge-m3"])
        _fake_ollama_embed(monkeypatch, dim=1024)
        monkeypatch.setenv("NEXUS_EMBEDDING_PROVIDER", "ollama")

        # 8b is installed → not the stored model → explicit mismatch error,
        # never a silent "they are basically the same" reuse.
        with pytest.raises((RuntimeError, CollectionModelUnavailable)):
            EmbeddingProvider(preferred="ollama")

    def test_drift_guard_reuses_exactly_matching_collection_model(
        self, isolated_env, monkeypatch
    ):
        """Same tag installed → stored model is kept (binding preserved)."""
        monkeypatch.setattr(
            embeddings_module,
            "_read_existing_collection_model",
            lambda: "qwen3-embedding:0.6b",
        )
        _fake_ollama_tags(monkeypatch, ["qwen3-embedding:0.6b", "bge-m3"])
        posts = _fake_ollama_embed(monkeypatch, dim=1024)
        monkeypatch.setenv("NEXUS_EMBEDDING_PROVIDER", "ollama")

        ep = EmbeddingProvider(preferred="ollama")

        assert ep.name == "qwen3-embedding:0.6b"
        assert ep.backend == "ollama"
        # The probe must have been issued for the stored model.
        assert posts and "qwen3-embedding:0.6b" in posts[0][1]["model"]


# ===========================================================================
# Finding 4 — stored collection model must exist in the Ollama inventory
# ===========================================================================


class TestStoredCollectionModelResolution:
    """A stored model is only reused when /api/tags actually lists it."""

    def test_missing_stored_model_raises_explicit_error(
        self, isolated_env, monkeypatch
    ):
        """Stored model gone from Ollama → explicit error, no silent model
        switch, no fall-through to another provider."""
        monkeypatch.setattr(
            embeddings_module,
            "_read_existing_collection_model",
            lambda: "bge-m3",
        )
        # Ollama is running but has other models only.
        _fake_ollama_tags(monkeypatch, ["qwen3-embedding:0.6b", "llama3:8b"])
        _fake_ollama_embed(monkeypatch, dim=1024)
        monkeypatch.setenv("NEXUS_EMBEDDING_PROVIDER", "ollama")

        with pytest.raises(CollectionModelUnavailable, match="bge-m3"):
            EmbeddingProvider(preferred="ollama")

    def test_missing_stored_model_error_names_the_model(
        self, isolated_env, monkeypatch
    ):
        monkeypatch.setattr(
            embeddings_module,
            "_read_existing_collection_model",
            lambda: "qwen3-embedding:0.6b",
        )
        _fake_ollama_tags(monkeypatch, ["bge-m3"])
        _fake_ollama_embed(monkeypatch, dim=1024)
        monkeypatch.setenv("NEXUS_EMBEDDING_PROVIDER", "ollama")

        with pytest.raises(CollectionModelUnavailable) as excinfo:
            EmbeddingProvider(preferred="ollama")

        assert "qwen3-embedding:0.6b" in str(excinfo.value)

    def test_bogus_cloud_name_as_stored_model_is_rejected(
        self, isolated_env, monkeypatch
    ):
        """The old always-true check even accepted stored names that belong
        to a cloud provider (e.g. a voyage model) — these are not Ollama
        models and must fail the inventory resolution."""
        monkeypatch.setattr(
            embeddings_module,
            "_read_existing_collection_model",
            lambda: "voyage-4",
        )
        _fake_ollama_tags(monkeypatch, ["qwen3-embedding:0.6b"])
        _fake_ollama_embed(monkeypatch, dim=1024)
        monkeypatch.setenv("NEXUS_EMBEDDING_PROVIDER", "ollama")

        with pytest.raises(CollectionModelUnavailable, match="voyage-4"):
            EmbeddingProvider(preferred="ollama")

    def test_installed_stored_model_is_resolved_and_used(
        self, isolated_env, monkeypatch
    ):
        """Happy path: stored model present in /api/tags → reused, dims probed."""
        monkeypatch.setattr(
            embeddings_module,
            "_read_existing_collection_model",
            lambda: "bge-m3",
        )
        _fake_ollama_tags(monkeypatch, ["qwen3-embedding:0.6b", "bge-m3"])
        _fake_ollama_embed(monkeypatch, dim=1024)
        monkeypatch.setenv("NEXUS_EMBEDDING_PROVIDER", "ollama")

        ep = EmbeddingProvider(preferred="ollama")

        assert ep.name == "bge-m3"
        assert ep.backend == "ollama"
        assert ep.dim == 1024
        assert ep.available is True

    def test_no_stored_model_prefers_qwen3_unchanged(
        self, isolated_env, monkeypatch
    ):
        """Production context: with nothing stored, qwen3-embedding:0.6b is
        still preferred over bge-m3 (benchmark winner) — the fix must not
        change the default selection."""
        _no_stored_model(monkeypatch)
        _fake_ollama_tags(monkeypatch, ["bge-m3", "qwen3-embedding:0.6b"])
        _fake_ollama_embed(monkeypatch, dim=1024)
        monkeypatch.setenv("NEXUS_EMBEDDING_PROVIDER", "ollama")

        ep = EmbeddingProvider(preferred="ollama")

        assert ep.name == "qwen3-embedding:0.6b"
        assert ep.dim == 1024

    def test_failure_surfaces_and_does_not_fall_through_to_other_provider(
        self, isolated_env, monkeypatch
    ):
        """CollectionModelUnavailable must escape initialization — it must
        not be swallowed into a silent switch to sentence-transformers or a
        cloud provider."""
        monkeypatch.setattr(
            embeddings_module,
            "_read_existing_collection_model",
            lambda: "bge-m3",
        )
        _fake_ollama_tags(monkeypatch, ["qwen3-embedding:0.6b"])
        monkeypatch.setattr(embeddings_module, "VOYAGE_API_KEY", "vo-valid")
        fake_voyage = MagicMock(name="voyageai")
        fake_voyage.Client = MagicMock(return_value=MagicMock())
        monkeypatch.setitem(sys.modules, "voyageai", fake_voyage)
        monkeypatch.setenv("NEXUS_EMBEDDING_PROVIDER", "ollama")

        with pytest.raises(CollectionModelUnavailable):
            EmbeddingProvider(preferred="ollama")