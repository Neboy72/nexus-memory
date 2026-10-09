"""Regel B — the raw /points/search callers must survive a NAMED collection.

The collection model lives in Qdrant as a named vector
(``<backend>__<model>__<dim>``). Qdrant's ``/points/search`` endpoint does NOT
accept the client-style ``using`` keyword: the name has to travel INSIDE the
vector object::

    {"vector": {"name": "<fingerprint>", "vector": [...]}}

A bare list against a named collection answers ``400 "Not existing vector
name"``. In ``matcher``/``confidence`` that raised; in the Claude-Code hooks the
search wraps everything in ``except Exception`` and silently returned NO
memories — the worst possible failure mode for a memory system.

These tests pin the request BODY, because that is where the bug lived: the
calls all worked perfectly against a legacy (unnamed) collection.
"""

from __future__ import annotations

import json

import pytest

from nexus.discovery import matcher
from nexus import confidence


class _Recorder:
    """Stand-in for ``requests`` that records the JSON body it was handed."""

    def __init__(self, result=None):
        self.bodies: list[dict] = []
        self._result = result if result is not None else []

    def post(self, url, json=None, timeout=None):  # noqa: A002 - mirror requests API
        self.bodies.append(json)
        return _Response(self._result)


class _Response:
    def __init__(self, result):
        self._result = result
        self.status_code = 200

    def raise_for_status(self):
        return None

    def json(self):
        return {"result": self._result}


def _named_body(body: dict, fingerprint: str) -> dict:
    """The shape Qdrant requires for a named collection."""
    return {"name": fingerprint, "vector": body.get("vector")}


FP = "ollama__qwen3-embedding_0_6b__1024"


class TestMatcherSearchBody:
    def test_named_vector_name_goes_inside_the_vector(self, monkeypatch):
        rec = _Recorder(result=[{"id": 1, "score": 0.9, "payload": {}}])
        monkeypatch.setattr(matcher, "requests", rec)
        monkeypatch.setattr(matcher, "HAS_REQUESTS", True)

        matcher.search_similar_facts(
            [0.1] * 4, collection="c", vector_name=FP
        )

        body = rec.bodies[0]
        assert body["vector"] == {"name": FP, "vector": [0.1] * 4}
        # The client-style keyword is NOT a valid HTTP body field.
        assert "using" not in body

    def test_legacy_collection_keeps_the_flat_vector(self, monkeypatch):
        rec = _Recorder(result=[])
        monkeypatch.setattr(matcher, "requests", rec)
        monkeypatch.setattr(matcher, "HAS_REQUESTS", True)

        matcher.search_similar_facts([0.2] * 4, collection="c")

        body = rec.bodies[0]
        assert body["vector"] == [0.2] * 4
        assert "using" not in body

    def test_body_is_json_serialisable(self, monkeypatch):
        """A non-serialisable body would crash before it ever reached Qdrant."""
        rec = _Recorder(result=[])
        monkeypatch.setattr(matcher, "requests", rec)
        monkeypatch.setattr(matcher, "HAS_REQUESTS", True)

        matcher.search_similar_facts([0.3] * 4, collection="c", vector_name=FP)
        json.dumps(rec.bodies[0])


class TestConfidenceSearchBody:
    def test_named_vector_name_goes_inside_the_vector(self, monkeypatch):
        rec = _Recorder(result=[])
        monkeypatch.setattr(confidence, "requests", rec)
        monkeypatch.setattr(confidence, "HAS_REQUESTS", True)

        confidence._fetch_chunks(
            [0.1] * 4, collection="c", vector_name=FP
        )

        body = rec.bodies[0]
        assert body["vector"] == {"name": FP, "vector": [0.1] * 4}
        assert "using" not in body

    def test_legacy_collection_keeps_the_flat_vector(self, monkeypatch):
        rec = _Recorder(result=[])
        monkeypatch.setattr(confidence, "requests", rec)
        monkeypatch.setattr(confidence, "HAS_REQUESTS", True)

        confidence._fetch_chunks([0.2] * 4, collection="c")

        body = rec.bodies[0]
        assert body["vector"] == [0.2] * 4
        assert "using" not in body


class TestRetrievalSearchBody:
    """``nexus/retrieval`` is the hybrid retriever's vector leg."""

    def _retriever(self, monkeypatch, vector_name):
        from nexus.retrieval import HybridRetriever

        rec = _Recorder(result=[])
        monkeypatch.setattr("nexus.retrieval.requests", rec)
        monkeypatch.setattr("nexus.retrieval.HAS_REQUESTS", True)
        r = HybridRetriever(collection_name="c", vector_name=vector_name)
        # Skip the BM25 corpus load — only the vector leg is under test.
        r._collection_dim = 4
        return r, rec

    def test_named_vector_name_goes_inside_the_vector(self, monkeypatch):
        r, rec = self._retriever(monkeypatch, FP)
        r.search_vector([0.1] * 4, top_k=3)

        body = rec.bodies[0]
        assert body["vector"] == {"name": FP, "vector": [0.1] * 4}
        assert "using" not in body

    def test_legacy_collection_keeps_the_flat_vector(self, monkeypatch):
        r, rec = self._retriever(monkeypatch, None)
        r.search_vector([0.2] * 4, top_k=3)

        body = rec.bodies[0]
        assert body["vector"] == [0.2] * 4
        assert "using" not in body

    def test_dimension_guard_reads_a_named_collection(self, monkeypatch):
        """The guard read ``vectors["size"]`` and swallowed the KeyError for
        named collections — i.e. it was off exactly where Regel B applied."""
        from nexus.retrieval import HybridRetriever

        seen = {}

        class _DimResponse:
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return {"result": {"config": {"params": {"vectors": {FP: {"size": 1024}}}}}}

        class _DimRecorder(_Recorder):
            def get(self, url, timeout=None):
                seen["url"] = url
                return _DimResponse()

        rec = _DimRecorder(result=[])
        monkeypatch.setattr("nexus.retrieval.requests", rec)
        monkeypatch.setattr("nexus.retrieval.HAS_REQUESTS", True)
        r = HybridRetriever(collection_name="c", vector_name=FP)

        # 4d vector against a 1024d NAMED collection must now raise.
        with pytest.raises(ValueError):
            r.search_vector([0.1] * 4, top_k=3)


class TestClaudeCodeHookHelper:
    """The hooks resolve the named/legacy layout through ``scope_auto``.

    An earlier revision of this file probed a standalone ``qdrant_helpers.py``
    that no shipped hook ever imported — the tests passed while the helpers the
    hooks actually call were untested. The cases below pin the functions the
    hooks really use.
    """

    def _scope_auto(self):
        import importlib.util
        import pathlib

        path = (
            pathlib.Path(__file__).resolve().parent.parent
            / "plugins" / "claude-code" / "scripts" / "scope_auto.py"
        )
        spec = importlib.util.spec_from_file_location("scope_auto_probe", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _with_override(self, mod, name):
        """Force a layout without needing a live Qdrant."""
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(mod, "_LAYOUT_CACHE", {(mod.QDRANT_URL, mod.COLLECTION): name})
        return monkeypatch

    def test_anonymous_collection_keeps_the_flat_vector(self):
        mod = self._scope_auto()
        mp = self._with_override(mod, None)
        try:
            assert mod.search_vector_body([0.1] * 4) == [0.1] * 4
            assert mod.point_vector_body([0.1] * 4) == [0.1] * 4
        finally:
            mp.undo()

    def test_named_collection_puts_the_name_inside_the_object(self):
        mod = self._scope_auto()
        mp = self._with_override(mod, FP)
        try:
            # Search: {"name": …, "vector": […]} — NOT a top-level "using".
            assert mod.search_vector_body([0.1] * 4) == {"name": FP, "vector": [0.1] * 4}
            # Write: {name: […]}
            assert mod.point_vector_body([0.1] * 4) == {FP: [0.1] * 4}
        finally:
            mp.undo()

    def test_layout_helper_never_raises_on_a_dead_store(self):
        mod = self._scope_auto()
        mod._LAYOUT_CACHE.clear()
        # Unreachable port -> fail-open to the legacy (flat) protocol.
        assert mod.vector_field("http://127.0.0.1:1", "nope") is None
