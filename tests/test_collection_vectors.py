"""Regel B — collection binding in Qdrant as a named vector.

Covers the six contract cases from the 08.10.2026 design (see
``nexus_memory.collection_vectors.CollectionBinding``):

1. Collection does not exist  → created with the named vector fingerprint.
2. Fingerprint matches        → bound, ``using=<name>`` is handed out.
3. Named mismatch, EMPTY      → recreated loss-free with the new fingerprint.
4. Named mismatch, NON-EMPTY  → hard error with instructions (explicit
                                preference) / provider switch (``auto``).
5. Unnamed legacy, NON-EMPTY  → ``legacy_collections`` config decides:
                                missing → hard error, present → bound without
                                ``using`` (single-vector collection).
6. Qdrant unreachable         → hard error (nothing is guessed).

Plus the SHARP PROOF: writing with a FOREIGN vector name must be rejected by
Qdrant itself (the whole point of moving the model into the collection — a
silent mix of two vector spaces becomes structurally impossible).

All tests use an embedded Qdrant in a temp dir; no server, no network.
"""

from __future__ import annotations

import tempfile

import pytest
from qdrant_client import QdrantClient, models

from nexus_memory.collection_vectors import (
    CollectionBinding,
    named_point_vector,
    upsert_named,
    using_name,
)
from nexus_memory.embeddings import CollectionModelUnavailable, vector_fingerprint


# ---------------------------------------------------------------------------
# A minimal provider stub — carries exactly the surface the binding reads.
# ---------------------------------------------------------------------------


class _StubProvider:
    def __init__(self, backend: str = "test", model: str = "stub-model",
                 dim: int = 4, preferred: str = "") -> None:
        self.backend = backend
        self.model_name = model
        self.dim = dim
        self._preferred = preferred

    @property
    def name(self) -> str:
        return self.model_name


def _client() -> QdrantClient:
    return QdrantClient(path=tempfile.mkdtemp(prefix="regelb-"))


def _fp(backend="test", model="stub-model", dim=4) -> str:
    return vector_fingerprint(backend, model, dim)


def _seed_named(client: QdrantClient, name: str, fp: str, dim: int, n: int):
    """Create a named-vector collection with ``n`` points."""
    client.create_collection(
        collection_name=name,
        vectors_config={fp: models.VectorParams(size=dim, distance=models.Distance.COSINE)},
    )
    if n:
        client.upsert(
            collection_name=name,
            points=[
                models.PointStruct(id=i, vector={fp: [0.0] * dim}, payload={"content": f"p{i}"})
                for i in range(n)
            ],
        )
    return client


def _seed_legacy(client: QdrantClient, name: str, dim: int, n: int):
    """Create an UNNAMED (legacy) collection with ``n`` points."""
    client.create_collection(
        collection_name=name,
        vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE),
    )
    if n:
        client.upsert(
            collection_name=name,
            points=[
                models.PointStruct(id=i, vector=[0.0] * dim, payload={"content": f"p{i}"})
                for i in range(n)
            ],
        )
    return client


# ===========================================================================
# 1. Collection does not exist → created with the fingerprint
# ===========================================================================


class TestCreateWhenMissing:
    def test_creates_collection_with_named_fingerprint(self):
        c = _client()
        b = CollectionBinding(c, "fresh", _StubProvider())
        b.ensure()

        assert b.vector_name() == _fp()
        assert b.is_named() is True
        assert b.is_legacy() is False
        vectors = c.get_collection("fresh").config.params.vectors
        assert list(vectors.keys()) == [_fp()]

    def test_create_is_idempotent(self):
        c = _client()
        b = CollectionBinding(c, "fresh", _StubProvider())
        b.ensure()
        again = b.ensure()  # second call must not raise or re-create
        assert again.vector_name() == _fp()

    def test_ensure_does_not_write_any_config(self, tmp_path, monkeypatch):
        """The binding never writes a JSON config — the model lives in Qdrant.

        Stronger than a glob: HOME/HERMES_HOME are redirected into ``tmp_path``
        and we snapshot the whole tree before/after, so a write to ANY config
        path (including ~/.nexus-memory) would show up as a new/changed file.
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))

        def _snapshot() -> dict:
            return {
                str(p): p.stat().st_mtime_ns
                for p in tmp_path.glob("**/*")
                if p.is_file()
            }

        before = _snapshot()
        c = _client()
        CollectionBinding(c, "fresh", _StubProvider()).ensure()
        after = _snapshot()

        assert after == before, (
            f"binding must not write any file; changed: "
            f"{set(after.items()) ^ set(before.items())}"
        )


# ===========================================================================
# 2. Fingerprint matches → bound, `using` handed out
# ===========================================================================


class TestBindOnMatch:
    def test_matching_fingerprint_binds(self):
        c = _client()
        _seed_named(c, "match", _fp(), 4, n=3)

        b = CollectionBinding(c, "match", _StubProvider())
        b.ensure()

        assert b.vector_name() == _fp()
        assert b.is_named() is True

    def test_using_name_returns_kwarg_only_when_named(self):
        c = _client()
        _seed_named(c, "match", _fp(), 4, n=1)
        b = CollectionBinding(c, "match", _StubProvider())
        b.ensure()

        assert using_name(b) == {"using": _fp()}
        assert using_name(None) == {}
        assert using_name("") == {}


# ===========================================================================
# 3. Named mismatch, EMPTY → recreated loss-free
# ===========================================================================


class TestMismatchEmptyRecreates:
    def test_empty_mismatched_collection_is_recreated(self):
        c = _client()
        other_fp = _fp(model="old-model")
        _seed_named(c, "swap", other_fp, 4, n=0)

        b = CollectionBinding(c, "swap", _StubProvider())
        b.ensure()

        assert b.vector_name() == _fp()
        vectors = c.get_collection("swap").config.params.vectors
        assert list(vectors.keys()) == [_fp()], "collection must carry the new name"


# ===========================================================================
# 4. Named mismatch, NON-EMPTY → hard error (explicit) / switch (auto)
# ===========================================================================


class TestMismatchNonEmpty:
    def test_explicit_preference_fails_hard(self):
        c = _client()
        other_fp = _fp(model="other")
        _seed_named(c, "occupied", other_fp, 4, n=2)

        b = CollectionBinding(c, "occupied", _StubProvider(preferred="test"))
        with pytest.raises(CollectionModelUnavailable) as excinfo:
            b.ensure()

        msg = str(excinfo.value)
        assert "occupied" in msg and other_fp in msg
        assert "NEXUS_COLLECTION" in msg or "preference" in msg

    def test_auto_without_local_match_also_fails_hard(self):
        """`auto` may only switch to a LOCAL provider that matches the stored
        name; an unknown/mismatching stored name still fails closed."""
        c = _client()
        other_fp = _fp(backend="weird", model="unknown", dim=4)
        _seed_named(c, "occupied", other_fp, 4, n=1)

        b = CollectionBinding(c, "occupied", _StubProvider(preferred="auto"))
        with pytest.raises(CollectionModelUnavailable):
            b.ensure()

    def test_auto_fallthrough_message_warns_against_mapping_active_provider(self):
        """P1: the auto-mode fallthrough must not lead the user into
        'update legacy_collections' (same-dim/different-model would mix)."""
        c = _client()
        # A stored name whose backend cannot be resolved to a local provider,
        # so auto mode falls through to the instructive error.
        other_fp = _fp(backend="somecloud", model="unknown", dim=4)
        _seed_named(c, "occupied-msg", other_fp, 4, n=1)

        b = CollectionBinding(c, "occupied-msg", _StubProvider(preferred="auto"))
        with pytest.raises(CollectionModelUnavailable) as excinfo:
            b.ensure()

        msg = str(excinfo.value)
        assert "BUILT" in msg
        assert "Do NOT" in msg
        assert "silently mixes" in msg
        assert "update legacy_collections" not in msg.lower()

    def test_auto_is_not_a_free_pass(self):
        """Regression guard: `auto` must never silently accept a foreign space."""
        c = _client()
        other_fp = _fp(backend="somecloud", model="x", dim=4)
        _seed_named(c, "occupied", other_fp, 4, n=1)

        b = CollectionBinding(c, "occupied", _StubProvider(preferred="auto"))
        with pytest.raises(CollectionModelUnavailable):
            b.ensure()


# ===========================================================================
# 5. Unnamed legacy, NON-EMPTY → legacy_collections decides
# ===========================================================================


class TestLegacyUnnamed:
    def test_legacy_without_mapping_fails_hard_with_instruction(self):
        c = _client()
        _seed_legacy(c, "old", 4, n=2)

        b = CollectionBinding(c, "old", _StubProvider())
        with pytest.raises(CollectionModelUnavailable) as excinfo:
            b.ensure()

        assert "legacy_collections" in str(excinfo.value)
        assert "old" in str(excinfo.value)

    def test_legacy_with_matching_mapping_binds_without_using(self):
        c = _client()
        _seed_legacy(c, "old", 4, n=2)

        b = CollectionBinding(
            c, "old", _StubProvider(),
            config_legacy_collections={"old": _fp()},
        )
        b.ensure()

        assert b.is_legacy() is True
        assert b.vector_name() is None, "legacy collections must not get `using`"
        assert using_name(b) == {}

    def test_legacy_mapping_mismatch_fails_hard(self):
        c = _client()
        _seed_legacy(c, "old", 4, n=1)

        b = CollectionBinding(
            c, "old", _StubProvider(),
            config_legacy_collections={"old": _fp(model="different")},
        )
        with pytest.raises(CollectionModelUnavailable):
            b.ensure()

    def test_empty_legacy_is_recreated_as_named(self):
        """An EMPTY unnamed collection carries no data — safe to recreate."""
        c = _client()
        _seed_legacy(c, "old", 4, n=0)

        b = CollectionBinding(c, "old", _StubProvider())
        b.ensure()

        assert b.vector_name() == _fp()
        vectors = c.get_collection("old").config.params.vectors
        assert list(vectors.keys()) == [_fp()]


# ===========================================================================
# 6. Qdrant unreachable → hard error
# ===========================================================================


class TestQdrantUnreachable:
    def test_unreachable_client_raises_runtime_error(self):
        class _Dead:
            def get_collections(self):
                raise ConnectionError("connection refused")

        b = CollectionBinding(_Dead(), "any", _StubProvider())
        with pytest.raises(RuntimeError) as excinfo:
            b.ensure()

        assert "unreachable" in str(excinfo.value).lower()


# ===========================================================================
# SHARP PROOF — a foreign vector name is rejected by Qdrant itself
# ===========================================================================


class TestSharpProofForeignVectorName:
    """The core promise: Qdrant enforces the vector space, not just our code."""

    def test_write_with_foreign_vector_name_is_rejected(self):
        c = _client()
        mine = _fp()
        _seed_named(c, "sharp", mine, 4, n=0)

        foreign = {"someone__else__4": [0.1, 0.2, 0.3, 0.4]}
        with pytest.raises(Exception) as excinfo:
            c.upsert(
                collection_name="sharp",
                points=[models.PointStruct(id=1, vector=foreign, payload={})],
            )
        assert "vector" in str(excinfo.value).lower()

    def test_write_with_raw_vector_is_rejected_on_named_collection(self):
        """A plain list against a named collection is refused by Qdrant too."""
        c = _client()
        _seed_named(c, "sharp2", _fp(), 4, n=0)

        with pytest.raises(Exception) as excinfo:
            c.upsert(
                collection_name="sharp2",
                points=[models.PointStruct(id=1, vector=[0.1, 0.2, 0.3, 0.4], payload={})],
            )
        assert "unnamed" in str(excinfo.value).lower()

    def test_using_kwarg_on_upsert_is_rejected(self):
        """`using` is a READ-side argument — passing it to upsert must fail.

        This is the exact bug the worker introduced: `upsert(**{'using': n})`
        raises ``Unknown arguments`` and (when swallowed) silently writes
        nothing. The helper avoids it; this test pins the API behaviour.
        """
        c = _client()
        _seed_named(c, "sharp3", _fp(), 4, n=0)
        with pytest.raises(Exception) as excinfo:
            c.upsert(
                collection_name="sharp3",
                points=[models.PointStruct(id=1, vector={_fp(): [0.0] * 4}, payload={})],
                using=_fp(),
            )
        assert "unknown arguments" in str(excinfo.value).lower()

    def test_upsert_named_wraps_the_vector_correctly(self):
        """The helper turns a raw vector into the named-vector dict."""
        c = _client()
        b = CollectionBinding(c, "helped", _StubProvider())
        b.ensure()

        upsert_named(
            c, "helped",
            [models.PointStruct(id=7, vector=[0.5] * 4, payload={"content": "x"})],
            b.vector_name(),
        )

        rec = c.retrieve("helped", ids=[7], with_vectors=True)[0]
        assert isinstance(rec.vector, dict)
        assert list(rec.vector.keys()) == [_fp()]

    def test_upsert_named_keeps_legacy_writes_plain(self):
        """Legacy collection (no name): the vector stays a plain list."""
        c = _client()
        _seed_legacy(c, "legacy", 4, n=0)
        upsert_named(
            c, "legacy",
            [models.PointStruct(id=1, vector=[0.1] * 4, payload={})],
            None,
        )
        rec = c.retrieve("legacy", ids=[1], with_vectors=True)[0]
        assert isinstance(rec.vector, list)

    def test_named_point_vector_helper(self):
        assert named_point_vector("a__b__4", [1, 2]) == {"a__b__4": [1, 2]}
        assert named_point_vector(None, [1, 2]) == [1, 2]


# ===========================================================================
# P0 regressions (from the K3 review, 09.10.2026)
# ===========================================================================


class TestAutoSwitchAdoptsProvider:
    """P0-1: the binding may switch the provider — the caller must adopt it.

    Before the fix the binding swapped ``self.provider`` but the caller kept
    its old ``_embedder``: same name, same dimension, WRONG vector space — the
    silent mix this design exists to prevent.
    """

    def _provider_for(self, backend: str, model: str, dim: int):
        p = _StubProvider(backend=backend, model=model, dim=dim, preferred="auto")
        p.available = True
        return p

    def test_auto_switches_to_matching_local_provider(self):
        c = _client()
        # Collection was built by ollama/stub-model (fingerprint below).
        stored_fp = vector_fingerprint("ollama", "stub-model", 4)
        _seed_named(c, "auto-coll", stored_fp, 4, n=2)

        # Active provider wants something DIFFERENT, preference auto.
        active = _StubProvider(backend="huggingface", model="other", dim=4,
                               preferred="auto")
        binding = CollectionBinding(
            c, "auto-coll", active,
            provider_factory=lambda backend: self._provider_for(
                "ollama", "stub-model", 4),
        )
        binding.ensure()

        # The switch happened AND the binding exposes the NEW provider, which
        # is what every caller must now use.
        assert binding.vector_name() == stored_fp
        assert binding.provider is not active
        assert binding.provider.backend == "ollama"

    def test_auto_without_matching_provider_fails_hard(self):
        c = _client()
        stored_fp = vector_fingerprint("ollama", "gone-model", 4)
        _seed_named(c, "auto-coll2", stored_fp, 4, n=1)

        active = _StubProvider(backend="huggingface", model="other", dim=4,
                               preferred="auto")
        binding = CollectionBinding(
            c, "auto-coll2", active,
            provider_factory=lambda backend: None,  # nothing available
        )
        with pytest.raises(CollectionModelUnavailable):
            binding.ensure()

    def test_explicit_preference_never_switches(self):
        c = _client()
        stored_fp = vector_fingerprint("ollama", "stub-model", 4)
        _seed_named(c, "auto-coll3", stored_fp, 4, n=1)

        active = _StubProvider(backend="huggingface", model="other", dim=4,
                               preferred="huggingface")
        binding = CollectionBinding(c, "auto-coll3", active)
        with pytest.raises(CollectionModelUnavailable):
            binding.ensure()
        assert binding.provider is active, "an explicit choice must never switch"


class TestLegacyMessageNeverSuggestsActiveFingerprint:
    """P0-2: the error must not hand the user a snippet that mixes spaces.

    The old message suggested ``{"<coll>": "<active fingerprint>"}`` — a user
    who switched providers (same dim) would copy exactly that into an unnamed
    collection, where Qdrant cannot enforce anything.
    """

    def test_message_does_not_contain_active_fingerprint(self):
        c = _client()
        _seed_legacy(c, "legacy-old", 4, n=2)

        active_fp = _fp()  # the fingerprint the ACTIVE provider would produce
        binding = CollectionBinding(c, "legacy-old", _StubProvider())

        with pytest.raises(CollectionModelUnavailable) as excinfo:
            binding.ensure()

        msg = str(excinfo.value)
        assert active_fp not in msg, (
            "the message must not suggest the active provider's fingerprint"
        )
        assert "<backend>__<model>__<dim>" in msg
        assert "BUILT" in msg or "built" in msg

    def test_dimension_mismatch_in_mapping_fails_before_use(self):
        """A mapping whose __<dim> contradicts the collection must fail."""
        c = _client()
        _seed_legacy(c, "legacy-dim", 4, n=1)

        # Mapping declares 1024 dims although the collection holds 4.
        wrong = vector_fingerprint("test", "stub-model", 1024)
        binding = CollectionBinding(
            c, "legacy-dim", _StubProvider(),
            config_legacy_collections={"legacy-dim": wrong},
        )
        with pytest.raises(CollectionModelUnavailable) as excinfo:
            binding.ensure()
        assert "dimensional" in str(excinfo.value) or "dimensions" in str(excinfo.value)

    def test_correct_mapping_still_binds(self):
        c = _client()
        _seed_legacy(c, "legacy-ok", 4, n=1)
        binding = CollectionBinding(
            c, "legacy-ok", _StubProvider(),
            config_legacy_collections={"legacy-ok": _fp()},
        )
        binding.ensure()
        assert binding.is_legacy() is True


class TestNoProviderFailsClosed:
    """P2 (adopted as a guard): never materialise ``none__none__384``."""

    def test_unavailable_provider_blocks_recreate_of_empty_collection(self):
        """The guard sits at the top of ensure(), so the recreate path is too."""
        c = _client()
        # Existing EMPTY named collection with a foreign fingerprint -> the
        # normal path would recreate it. With no provider it must refuse.
        _seed_named(c, "recreate-me", _fp(model="old"), 4, n=0)

        p = _StubProvider(backend="none", model="none", dim=384)
        p.available = False
        with pytest.raises(CollectionModelUnavailable):
            CollectionBinding(c, "recreate-me", p).ensure()

        # The old collection must still be there, untouched.
        vectors = c.get_collection("recreate-me").config.params.vectors
        assert list(vectors.keys()) == [_fp(model="old")]

    def test_unavailable_provider_blocks_creation(self):
        c = _client()
        p = _StubProvider(backend="none", model="none", dim=384)
        p.available = False
        binding = CollectionBinding(c, "must-not-exist", p)

        with pytest.raises(CollectionModelUnavailable):
            binding.ensure()

        names = [x.name for x in c.get_collections().collections]
        assert "must-not-exist" not in names, (
            "a bogus vector space must not be created"
        )


class TestEntityWritePathRuns:
    """P0 regression (review round 2): the entity write path must RUN.

    The dead ``upsert_kwargs`` frame had been removed but its ``del`` stayed —
    a guaranteed ``NameError`` AFTER a successful write in the Hermes plugin's
    ``_upsert_entity`` (retry risk, confusing logs). This drives the real
    method and asserts the named vector lands in the point.
    """

    def test_upsert_entity_writes_named_vector_without_error(self):
        import importlib.util
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        repo = __import__("pathlib").Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location(
            "hp_entity_test", str(repo / "integrations" / "hermes-plugin" / "__init__.py"))
        mod = importlib.util.module_from_spec(spec)

        class _FakeQ:
            def __init__(self):
                self.last = None

            def upsert(self, **kw):
                self.last = kw

        spec.loader.exec_module(mod)
        p = mod.NexusMemoryProvider.__new__(mod.NexusMemoryProvider)
        p._qdrant = _FakeQ()
        p._collection = "smoke"
        p.vector_name = "ollama__qwen3-embedding_0_6b__1024"
        p._embedder = MagicMock()
        p._embed_cache = None

        ent = SimpleNamespace(name="X", entity_type="concept", confidence=0.9,
                              attributes={})
        result = p._upsert_entity(ent)  # must not raise NameError

        assert result["status"] == "ok"
        vec = p._qdrant.last["points"][0].vector
        assert isinstance(vec, dict)
        assert list(vec.keys()) == ["ollama__qwen3-embedding_0_6b__1024"]


class TestCallerAdoptsSwitchedProvider:
    """P1: the CALLER must read the provider back after ``ensure()``.

    ``TestAutoSwitchAdoptsProvider`` proves the BINDING exposes the new
    provider. The actual P0-1 fix is one line in each caller
    (``self._embedder = self._binding.provider``) — this test drives the real
    ``MemoryStore.__init__`` so that deleting that line turns THIS test red.
    """

    def test_memory_store_uses_the_binding_provider(self, monkeypatch):
        import nexus_memory.mcp_server as mcp

        switched = _StubProvider(backend="ollama", model="stub-model", dim=4,
                                 preferred="auto")

        class _FakeBinding:
            def __init__(self, client, name, provider, **kw):
                # The provider built by the caller, then the switch the binding
                # would perform in auto mode.
                self.provider = switched

            def ensure(self):
                return self

            def vector_name(self):
                return vector_fingerprint("ollama", "stub-model", 4)

        class _FakeClient:
            def __init__(self, *a, **kw):
                pass

        monkeypatch.setattr(mcp, "QdrantClient", _FakeClient)
        monkeypatch.setattr(mcp, "CollectionBinding", _FakeBinding)
        # Hermetic: never let the real provider try sentence-transformers
        # (up to ~600 MB download / network probe in auto mode).
        monkeypatch.setattr(mcp, "EmbeddingProvider", lambda *a, **k: switched)
        monkeypatch.setattr(mcp.MemoryStore, "_init_hybrid", lambda self: None)
        monkeypatch.setattr(mcp.MemoryStore, "_init_skill_graph", lambda self: None)

        store = mcp.MemoryStore()

        assert store._embedder is switched, (
            "MemoryStore must adopt the provider the binding resolved — "
            "otherwise it embeds with the old model under the new vector name"
        )
        assert store.vector_name == vector_fingerprint("ollama", "stub-model", 4)


# ===========================================================================
# Fingerprint derivation — deterministic, legal characters, separators kept
# ===========================================================================


class TestFingerprint:
    def test_pattern_and_characters(self):
        import re
        name = vector_fingerprint("huggingface", "Qwen/Qwen3-Embedding-0.6B", 1024)
        assert re.fullmatch(r"[a-z0-9_-]+", name), name
        parts = name.split("__")
        assert len(parts) == 3, f"separators must survive: {name}"
        assert parts[0] == "huggingface"
        assert parts[2] == "1024"

    def test_expected_examples(self):
        assert vector_fingerprint("voyage", "voyage-4", 1024) == "voyage__voyage-4__1024"
        assert vector_fingerprint("ollama", "qwen3-embedding:0.6b", 1024) == \
            "ollama__qwen3-embedding_0_6b__1024"

    def test_deterministic(self):
        a = vector_fingerprint("test", "m", 4)
        b = vector_fingerprint("test", "m", 4)
        assert a == b

    def test_no_double_underscore_inside_a_part(self):
        name = vector_fingerprint("a--b", "m__n", 8)
        assert name.count("__") == 2, name
