"""Regression guard for the OpenClaw named-vector handling (09.10.2026).

The engine now stores its embedding fingerprint as a NAMED Qdrant vector
(coll ``vectors = {"ollama__qwen3-embedding_0_6b__1024": {...}}``). The OpenClaw
plugin sent a bare array, which a named space rejects:

* search → 400 "Not existing vector name error"  (silently swallowed into an
  empty result list, so the agent simply stopped remembering)
* upsert → 400 "data did not match any variant of untagged enum VectorStruct"

Both are fixed, and the two request shapes are genuinely DIFFERENT — search
takes ``{"name":…, "vector":…}``, an upsert point takes ``{name: […]}}``. A
single helper would fail on one of the two, so both are pinned here.

These tests are static (source parsing) plus one live probe that skips when
Qdrant is unreachable, so the suite still runs offline.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import urllib.error
import urllib.request

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
CLIENT = REPO / "plugins" / "openclaw" / "lib" / "qdrant-client.ts"
QD = "http://127.0.0.1:6333"


def _src() -> str:
    return CLIENT.read_text(encoding="utf-8")


# ── the shape of the two request bodies ──────────────────────────────────

def test_reads_the_vector_name_from_the_collection():
    """The name must be discovered, never guessed from the model id."""
    src = _src()
    assert "readVectorLayout" in src, "no layout reader — the name would be guessed"
    assert "vectorName" in src


def test_search_body_carries_the_name():
    src = _src()
    # `vector: { name, vector }` inside a search body.
    assert "vector: this.vectorField(queryVector)" in src, (
        "search still sends a bare vector — broken on a named collection"
    )


def test_upsert_point_carries_the_name_in_the_map_shape():
    src = _src()
    assert "pointVector(vector)" in src, (
        "upsert still sends a bare vector — broken on a named collection"
    )
    assert "pointVector" in src and "vectorField" in src
    # They must NOT be interchangeable: one is {name, vector}, the other {name: [...]}.
    body = src[src.index("private pointVector"):]
    assert "[this.vectorName]: vector" in body, (
        "pointVector does not build the map shape Qdrant's upsert expects"
    )


def test_a_read_waits_for_the_layout_before_building_its_body():
    """ensureCollection is fire-and-forget, so an early recall could race it."""
    src = _src()
    assert "resolveLayout" in src
    # Both search entry points must await it.
    for fn in ("async search(", "async searchByVector("):
        chunk = src[src.index(fn):src.index(fn) + 260]
        assert "await this.resolveLayout()" in chunk, (
            f"{fn} can still send a bare vector to a named collection"
        )


def test_multi_vector_space_is_not_guessed():
    """More than one vector with no dimension match must refuse, not pick."""
    src = _src()
    assert "refusing to guess a vector name" in src


# ── live probe against BOTH layouts (skips without Qdrant) ───────────────

def _qdrant_up() -> bool:
    try:
        urllib.request.urlopen(f"{QD}/collections", timeout=3)
        return True
    except (urllib.error.URLError, OSError):
        return False


@pytest.mark.skipif(not _qdrant_up(), reason="Qdrant not reachable")
def test_live_named_and_anonymous_collections():
    """The real proof: the built client reads and writes both layouts."""
    if not shutil.which("npx"):
        pytest.skip("npx not available")
    script = REPO / "plugins" / "openclaw" / "scripts" / "named-vector-proof.ts"
    if not script.exists():
        pytest.skip("proof script missing")
    result = subprocess.run(
        ["npx", "tsx", str(script)],
        capture_output=True, text=True, timeout=300,
        cwd=str(REPO / "plugins" / "openclaw"),
    )
    assert "ALLES GRUEN" in result.stdout, (
        f"named/anonymous proof failed:\n{result.stdout[-1500:]}\n{result.stderr[-800:]}"
    )
