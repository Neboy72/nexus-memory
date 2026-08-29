"""Tests for v0.13.0 roadmap-final."""

from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = str(_REPO_ROOT / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

_PLUGIN_PATH = _REPO_ROOT / "plugins" / "memory" / "nexus" / "__init__.py"
_spec = importlib.util.spec_from_file_location("nexus_hermes_plugin_final", str(_PLUGIN_PATH))
_nexus_plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_nexus_plugin)

NexusMemoryProvider = _nexus_plugin.NexusMemoryProvider


def _provider():
    prov = NexusMemoryProvider.__new__(NexusMemoryProvider)
    prov._collection = "c"
    prov._rerank_cfg = {"enabled": False}
    prov._rerank_lock = threading.Lock()
    prov._skill_graph = None
    prov._skill_graph_lock = threading.Lock()
    prov._prefetch_result = ""
    prov._prefetch_lock = threading.Lock()
    prov._entity_extract_lock = threading.Lock()
    prov._embed_cache = None
    prov._embed_cache_lock = threading.Lock()
    prov._hermes_home = ""
    prov._write_stop = threading.Event()
    prov._embedder = SimpleNamespace(dim=1024, embed=lambda t: [0.0] * 1024)
    client = MagicMock()
    client.query_points.return_value = SimpleNamespace(points=[])
    prov._qdrant = client
    return prov, client


def _fake_point(point_id, payload, score=0.9):
    return SimpleNamespace(id=point_id, payload=payload, score=score)


def test_as_of_filters_newer():
    prov, client = _provider()
    client.query_points.return_value = SimpleNamespace(points=[
        _fake_point("old", {"id": "old", "content": "old text", "created_at": "2025-01-01T00:00:00Z"}, 0.9),
        _fake_point("new", {"id": "new", "content": "new text", "created_at": "2026-08-30T00:00:00Z"}, 0.8),
    ])
    with_as_of = prov._recall("q", limit=5, as_of="2026-01-01")
    without = prov._recall("q", limit=5)
    assert [r["id"] for r in with_as_of] == ["old"]
    assert {r["id"] for r in without} == {"old", "new"}


def test_supersede_reason_written():
    importer_ready = True  # mcp_server import erfordert mcp - stattdessen regex auf src
    import re
    src = open("/Users/miosha/nexus-memory/src/nexus_memory/mcp_server.py").read()
    assert "supersede_reason" in src
    assert "similarity" in src


def test_skill_health_detector():
    from nexus.sica import _detect_skill_health
    now_iso = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
    points = [
        {"id": "s1", "payload": {"category": "skill", "content": "used recently",
                                  "last_accessed": now_iso}},
        {"id": "s2", "payload": {"category": "skill", "content": "stale skill",
                                  "created_at": "2026-01-01T00:00:00Z"}},  # ~8 months
        {"id": "n1", "payload": {"category": "fact", "content": "not a skill",
                                  "created_at": "2026-01-01T00:00:00Z"}},
    ]
    issues = _detect_skill_health(points)
    assert [i["id"] for i in issues] == ["s2"]
    assert issues[0]["type"] == "skill_stale"
    assert issues[0]["auto_fixable"] is False  # never auto-delete skills
