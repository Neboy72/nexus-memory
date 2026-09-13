#!/usr/bin/env python3
"""Wiring-Tests: query_rewrite im Recall- und Prefetch-Pfad (env-gated).

Kein Netz: _embedder/_qdrant sind gemockt, generate_fn immer gepatcht.
Deckt die 6 Faelle der WIRING_SPEC ab.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = str(_REPO_ROOT / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import nexus_memory.query_rewrite as qr  # noqa: E402
import nexus_memory.fuel_chain as fc  # noqa: E402

# Plugin direkt von der Datei laden (Kollision mit top-level 'nexus' vermeiden).
_PLUGIN_PATH = _REPO_ROOT / "plugins" / "memory" / "nexus" / "__init__.py"
_spec = importlib.util.spec_from_file_location(
    "nexus_hermes_plugin_wiring", str(_PLUGIN_PATH)
)
nexus_plugin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nexus_plugin)
NexusMemoryProvider = nexus_plugin.NexusMemoryProvider

ORIGINAL = "was war das mit dem ding furs auto"
REWRITTEN = "wallbox ladekabel rfid ruckgabe"


def _make_provider(captured, points=None):
    """Provider mit gemocktem Embedder/Qdrant; `captured` sammelt Embed-Queries."""
    p = NexusMemoryProvider()
    p._embedder = MagicMock()
    p._qdrant = MagicMock()
    p._qdrant.query_points.return_value = SimpleNamespace(points=points or [])
    p._rerank_cfg = {"enabled": False}
    p._scope_centroids = MagicMock()
    p._embed_cached = lambda q: (captured.append(q), [0.0] * 4)[1]
    return p


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("NEXUS_REWRITE", raising=False)


def test_recall_rewrites_query(monkeypatch):
    """_recall rewritet die Query VOR dem Embedding."""
    monkeypatch.setenv("NEXUS_REWRITE", "1")
    monkeypatch.setattr(qr, "enabled", lambda: True)
    seen = []
    monkeypatch.setattr(qr, "rewrite_query",
                        lambda q, fn: (seen.append(q), REWRITTEN)[1])
    monkeypatch.setattr(fc, "get_fuel", lambda *a, **k: (lambda prompt: "x"))
    cap = []
    _make_provider(cap)._recall(ORIGINAL)
    assert seen == [ORIGINAL]
    assert cap == [REWRITTEN]


def test_prefetch_rewrites_query(monkeypatch):
    """_do_prefetch rewritet die Auto-Prefetch-Query identisch."""
    monkeypatch.setenv("NEXUS_REWRITE", "1")
    monkeypatch.setattr(qr, "enabled", lambda: True)
    monkeypatch.setattr(qr, "rewrite_query",
                        lambda q, fn: (REWRITTEN if q == ORIGINAL else q))
    monkeypatch.setattr(fc, "get_fuel", lambda *a, **k: (lambda prompt: "x"))
    cap = []
    _make_provider(cap)._do_prefetch(ORIGINAL)
    assert cap == [REWRITTEN]


def test_disabled_passthrough(monkeypatch):
    """Disabled: Query unveraendert, rewrite_query wird nicht aufgerufen."""
    monkeypatch.setattr(qr, "enabled", lambda: False)
    spy = []
    monkeypatch.setattr(qr, "rewrite_query", lambda q, fn: spy.append(q) or "X")
    cap = []
    _make_provider(cap)._recall(ORIGINAL)
    assert cap == [ORIGINAL]
    assert spy == []


def test_no_fuel_call_when_disabled(monkeypatch):
    """Disabled: fuel-chain wird gar nicht erst aufgebaut (Kosten = 0)."""
    monkeypatch.setattr(qr, "enabled", lambda: False)
    counter = []
    monkeypatch.setattr(fc, "get_fuel",
                        lambda *a, **k: counter.append(a) or None)
    cap = []
    _make_provider(cap)._recall(ORIGINAL)
    assert counter == []
    assert cap == [ORIGINAL]


def test_fuel_fail_open(monkeypatch):
    """Enabled, aber get_fuel -> None: fail-open, Original-Query."""
    monkeypatch.setenv("NEXUS_REWRITE", "1")
    monkeypatch.setattr(qr, "enabled", lambda: True)
    monkeypatch.setattr(fc, "get_fuel", lambda *a, **k: None)
    cap = []
    _make_provider(cap)._recall(ORIGINAL)
    assert cap == [ORIGINAL]


def test_as_of_intact(monkeypatch):
    """Rewrite laesst as_of unangetastet: Point-in-time-Filter greift weiter."""
    monkeypatch.setenv("NEXUS_REWRITE", "1")
    monkeypatch.setattr(qr, "enabled", lambda: True)
    monkeypatch.setattr(qr, "rewrite_query", lambda q, fn: REWRITTEN)
    monkeypatch.setattr(fc, "get_fuel", lambda *a, **k: None)
    pt = SimpleNamespace(
        id="p1", score=0.9,
        payload={"id": "p1", "content": "neu",
                 "created_at": "2025-06-01T00:00:00Z",
                 "lifecycle_status": "canonical"},
    )
    cap = []
    out = _make_provider(cap, points=[pt])._recall(ORIGINAL, as_of="2024-01-01")
    assert cap == [REWRITTEN]  # rewrite applied
    assert out == []           # as_of filter still active
