"""Tests for roadmap 4.6: superseded-by recall skip.

Deprecated/rolled_back facts stay in Qdrant for audit but never surface
in plugin recall or prefetch. Legacy points without lifecycle_status
stay visible.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import importlib.util
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

_PLUGIN_PATH = _REPO_ROOT / "plugins" / "memory" / "nexus" / "__init__.py"
_spec = importlib.util.spec_from_file_location("nexus_hermes_plugin_ssl", str(_PLUGIN_PATH))
_nexus_plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_nexus_plugin)

NexusMemoryProvider = _nexus_plugin.NexusMemoryProvider


class _FakePoint:
    def __init__(self, point_id: str, payload: dict, score: float = 0.9):
        self.id = point_id
        self.payload = payload
        self.score = score


def _provider():
    prov = NexusMemoryProvider.__new__(NexusMemoryProvider)
    prov._collection = "c"
    prov._rerank_cfg = {"enabled": False}
    prov._rerank_lock = threading.Lock()
    prov._skill_graph = None
    prov._skill_graph_lock = threading.Lock()
    prov._prefetch_result = ""
    prov._prefetch_lock = threading.Lock()
    prov._embedder = SimpleNamespace(dim=1024, embed=lambda t: [0.0] * 2)
    return prov


def _setup_client(prov, points):
    client = MagicMock()
    client.query_points.return_value = SimpleNamespace(points=points)
    prov._qdrant = client
    return client


def test_recall_skips_deprecated_and_rolledback():
    prov = _provider()
    pts = [
        _FakePoint("a", {"id": "a", "content": "fresh", "lifecycle_status": "canonical"}),
        _FakePoint("b", {"id": "b", "content": "old truth", "lifecycle_status": "deprecated"}),
        _FakePoint("c", {"id": "c", "content": "reverted", "lifecycle_status": "rolled_back"}),
        _FakePoint("d", {"id": "d", "content": "legacy", "lifecycle_status": None}),
    ]
    client = _setup_client(prov, pts)
    out = prov._recall("anything", limit=10)
    ids = [r["id"] for r in out]
    assert ids == ["a", "d"]


def test_recall_missing_lifecycle_stays_visible():
    prov = _provider()
    pts = [_FakePoint("x", {"id": "x", "content": "old world"})]
    _setup_client(prov, pts)
    out = prov._recall("q", limit=5)
    assert [r["id"] for r in out] == ["x"]


def test_prefetch_skips_deprecated():
    prov = _provider()
    pts = [
        _FakePoint("a", {"id": "a", "category": "fact", "content": "fresh", "lifecycle_status": "canonical", "score": 0.9}),
        _FakePoint("b", {"id": "b", "category": "fact", "content": "deprecated", "lifecycle_status": "deprecated", "score": 0.9}),
    ]
    _setup_client(prov, pts)
    prov._do_prefetch("q")
    with prov._prefetch_lock:
        text = prov._prefetch_result
    assert "fresh" in text and "deprecated" not in text
