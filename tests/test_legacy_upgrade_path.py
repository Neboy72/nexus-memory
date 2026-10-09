"""Regression guard for the legacy-collection upgrade path (09.10.2026).

The review of the pre-push diff raised the real question: the engine now stores
the embedding model as a NAMED vector. What happens to an existing install whose
collection was created by an older version and is therefore UNNAMED with data
in it? A foreign user has no ``legacy_collections`` mapping.

Answer, verified here: the engine stops with a clear, actionable error instead
of guessing (correct — a wrong guess silently mixes vector spaces), and
``scripts/detect-legacy-collection.py`` prints the exact entry that resolves it.
That tool and its three outcomes (empty / unnamed with data / named) are pinned
here so the escape hatch cannot rot.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request
import uuid

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
TOOL = REPO / "scripts" / "detect-legacy-collection.py"
QD = "http://127.0.0.1:6333"

sys.path.insert(0, str(REPO / "src"))


def _qdrant_up() -> bool:
    try:
        urllib.request.urlopen(f"{QD}/collections", timeout=3)
        return True
    except (urllib.error.URLError, OSError):
        return False


needs_qdrant = pytest.mark.skipif(not _qdrant_up(), reason="Qdrant not reachable")


# ── the tool exists and is safe by construction ──────────────────────────

def test_tool_is_shipped_and_documented():
    src = TOOL.read_text(encoding="utf-8")
    assert "legacy_collections" in src
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "detect-legacy-collection.py" in readme, (
        "the upgrade path is not documented — a user hitting the stop has no path"
    )
    assert "legacy_collections" in readme


def test_tool_writes_nothing():
    """It may read Qdrant and print; it must never write to the collection."""
    src = TOOL.read_text(encoding="utf-8")
    # Writing helpers and mutating HTTP verbs must not appear. `open(...,"r")` is
    # the config READ and is fine — only a write mode would be a problem.
    for forbidden in ("write_text", "json.dump(", "\"PUT\"", "\"POST\"",
                      "\"DELETE\"", "open(path, \"w\")", "open(path, 'w')"):
        assert forbidden not in src, (
            f"the detector contains {forbidden!r} — it must stay read-only"
        )
    assert 'method="PUT"' not in src and "method='PUT'" not in src


def test_the_error_message_points_at_the_tool():
    """The stop must not be a dead end: the library error names the way out."""
    src = (REPO / "src" / "nexus_memory" / "collection_vectors.py").read_text(encoding="utf-8")
    assert "not empty, so its vector space is unknown" in src
    # The message asks for the BUILDER, which is the safe question.
    assert "BUILT it (not who should use it now)" in src


# ── the three real outcomes, against live throwaway collections ─────────

def _make(name: str, named: str | None, with_data: bool) -> None:
    from qdrant_client import QdrantClient, models
    c = QdrantClient(url=QD, timeout=15)
    if name in [x.name for x in c.get_collections().collections]:
        c.delete_collection(name)
    cfg = ({named: models.VectorParams(size=1024, distance=models.Distance.COSINE)}
           if named else models.VectorParams(size=1024, distance=models.Distance.COSINE))
    c.create_collection(name, vectors_config=cfg)
    if with_data:
        vec = {named: [0.1] * 1024} if named else [0.1] * 1024
        c.upsert(name, points=[models.PointStruct(
            id=str(uuid.uuid4()), vector=vec, payload={"content": "x"})])


def _run_tool(collection: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOL), "--collection", collection,
         "--backend", "ollama", "--model", "qwen3-embedding:0.6b"],
        capture_output=True, text=True, timeout=120,
    )


@needs_qdrant
def test_empty_unnamed_collection_needs_no_entry():
    _make("zz_test_empty", None, False)
    try:
        out = _run_tool("zz_test_empty").stdout
        assert "LEER" in out and "Nichts zu tun" in out
        assert "legacy_collections" not in out
    finally:
        _drop("zz_test_empty")


@needs_qdrant
def test_unnamed_collection_with_data_gets_the_exact_entry():
    _make("zz_test_legacy", None, True)
    try:
        out = _run_tool("zz_test_legacy").stdout
        assert "anonym, 1024" in out
        # The printed JSON must equal the library's own fingerprint — an
        # approximation here would hand the user a silent vector-space mix.
        from nexus_memory.embeddings import vector_fingerprint
        start = out.index("{")
        entry = json.loads(out[start:out.rindex("}") + 1])
        assert entry["legacy_collections"]["zz_test_legacy"] == (
            vector_fingerprint("ollama", "qwen3-embedding:0.6b", 1024)
        ), entry
    finally:
        _drop("zz_test_legacy")


@needs_qdrant
def test_named_collection_needs_nothing():
    _make("zz_test_named", "ollama__qwen3-embedding_0_6b__1024", True)
    try:
        out = _run_tool("zz_test_named").stdout
        assert "BENANNT" in out and "Nichts zu tun" in out
    finally:
        _drop("zz_test_named")


def _drop(name: str) -> None:
    from qdrant_client import QdrantClient
    QdrantClient(url=QD, timeout=15).delete_collection(name)


# ── the engine really does refuse an unmapped legacy collection ─────────

@needs_qdrant
def test_engine_refuses_to_guess_an_unmapped_legacy_collection():
    from nexus_memory.collection_vectors import (CollectionBinding,
                                                 CollectionModelUnavailable)
    from nexus_memory.embeddings import EmbeddingProvider
    from qdrant_client import QdrantClient

    _make("zz_test_guard", None, True)
    try:
        binding = CollectionBinding(QdrantClient(url=QD, timeout=15),
                                     "zz_test_guard", EmbeddingProvider(),
                                     config_legacy_collections=None)
        with pytest.raises(CollectionModelUnavailable):
            binding.ensure()
    finally:
        _drop("zz_test_guard")
