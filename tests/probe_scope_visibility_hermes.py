#!/usr/bin/env python3
"""Probe: Sichtbarkeit nach Verbund im Graph-Boost des Hermes-Plugins.

Ausfuehren:  python3 tests/probe_scope_visibility_hermes.py

Kein Pytest — eine ausfuehrbare Probe. Geprueft wird der Weg, der die Regel
bisher umging: ``_graph_boost`` zog Nachbarn aus dem Wissensgraphen nach, ohne
den Sichtbarkeitsfilter. Ein fremdes Sitzungsprotokoll landete damit im
eingespielten Block, obwohl der Vektorweg es ausschliesst.

Vier Faelle:
  a) Punkt in "default"      → sichtbar
  b) Punkt in "hermes"       → von einem FREMDEN Verbund nicht sichtbar
  c) Punkt ohne scope-Feld   → sichtbar (Altbestand)
  d) eigener Verbund         → sichtbar
"""
from __future__ import annotations

import importlib.util
import os
import sys
import threading
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FAILED: list[str] = []


def check(name: str, fn) -> None:
    try:
        fn()
    except AssertionError as exc:
        FAILED.append(f"{name}: {exc}")
        print(f"FAIL  {name} — {exc}")
    except Exception as exc:                      # noqa: BLE001 - Probe
        FAILED.append(f"{name}: {type(exc).__name__}: {exc}")
        print(f"FAIL  {name} — {type(exc).__name__}: {exc}")
    else:
        print(f"PASS  {name}")


def _plugin():
    """Das Hermes-Plugin als Modul, ohne Hermes-Installation."""
    spec = importlib.util.spec_from_file_location(
        "nexus_plugin_scope_probe", str(ROOT / "plugins" / "memory" / "nexus" / "__init__.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("nexus_plugin_scope_probe", mod)
    spec.loader.exec_module(mod)
    return mod


plug = _plugin()

# Der Verbund des Prueflings: NICHT der fremde Verbund aus den Fixtures.
BUNDLE = "hermes"
FOREIGN = "openclaw"
os.environ["NEXUS_AGENT_ID"] = BUNDLE

SHARED = "default"


class _FakeTraversal:
    """GraphTraversal-Ersatz: feste Nachbarn, keine Qdrant-Aufrufe."""

    def __init__(self, _graph, access_level="public"):
        self.access_level = access_level

    def get_related(self, _pid):
        # Dieselbe Form wie GraphTraversal.get_related: Liste von dicts.
        return [{"fact_id": nid, "relation": "uses", "depth": 1}
                for nid in ("n-default", "n-fremd", "n-alt", "n-eigen")]

    def traverse(self, _pid, max_depth=2):
        return self.get_related(_pid)


class _SkillGraph:
    def __init__(self, payloads):
        self._payloads = payloads

    def get_point(self, nid):
        payload = self._payloads.get(nid)
        return {"payload": payload} if payload is not None else None


def _provider(payloads):
    prov = object.__new__(plug.NexusMemoryProvider)
    prov._qdrant = object()                     # nur "vorhanden" sein
    prov._scope_centroids = None
    prov._prefetch_lock = threading.Lock()
    prov._collection = "nexus"
    prov._skill_graph = _SkillGraph(payloads)
    prov._skill_graph_lock = threading.Lock()
    prov._visible_levels = lambda: ("public",)
    prov._agent_level = lambda: "public"
    return prov


# ── 0) Die gemeinsame Filterform ────────────────────────────────────────────

def _filter_form() -> None:
    should = plug._scope_should(BUNDLE)
    assert len(should) == 3, f"drei Bedingungen erwartet, nicht {len(should)}"
    werte = [c.match.value for c in should if getattr(c, "match", None) is not None]
    assert werte == [SHARED, BUNDLE], f"falsche Werte: {werte}"
    leer = [c for c in should if getattr(c, "is_empty", None) is not None]
    assert len(leer) == 1 and leer[0].is_empty.key == "scope", "is_empty(scope) fehlt"


def _four_cases_python() -> None:
    assert plug._scope_visible({"scope": SHARED}, BUNDLE) is True, "gemeinsam"
    assert plug._scope_visible({"scope": FOREIGN}, BUNDLE) is False, "fremder Verbund"
    assert plug._scope_visible({}, BUNDLE) is True, "kein Feld = Altbestand"
    assert plug._scope_visible({"scope": BUNDLE}, BUNDLE) is True, "eigener Verbund"


check("_scope_should traegt die drei Bedingungen als should", _filter_form)
check("_scope_visible: die vier Faelle", _four_cases_python)


# ── 1) Graph-Boost: der Weg, der die Regel umging ───────────────────────────

def _boosted(payloads):
    prov = _provider(payloads)
    import nexus.graph.traversal as traversal
    original = traversal.GraphTraversal
    traversal.GraphTraversal = _FakeTraversal
    try:
        return prov._graph_boost([types.SimpleNamespace(id="root")], max_boost=1, max_depth=1)
    finally:
        traversal.GraphTraversal = original


def _boost_four_cases() -> None:
    items = _boosted({
        "n-default": {"content": "NACHBAR-GEMEINSAM", "access_level": "public",
                      "scope": SHARED},
        "n-fremd": {"content": "NACHBAR-FREMD-OPENCLAW", "access_level": "public",
                    "scope": FOREIGN},
        "n-alt": {"content": "NACHBAR-ALTBESTAND", "access_level": "public"},
        "n-eigen": {"content": "NACHBAR-EIGEN", "access_level": "public", "scope": BUNDLE},
    })
    block = "\n".join(items)
    assert "NACHBAR-GEMEINSAM" in block, "Punkt in 'default' muss erscheinen"
    assert "NACHBAR-FREMD-OPENCLAW" not in block, "fremdes Protokoll darf NICHT erscheinen"
    assert "NACHBAR-ALTBESTAND" in block, "Punkt ohne scope-Feld muss erscheinen"
    assert "NACHBAR-EIGEN" in block, "eigener Verbund muss erscheinen"


check("_graph_boost: die vier Sichtbarkeitsfaelle", _boost_four_cases)


def _boost_uses_payload_without_extra_call() -> None:
    """Kein zusaetzlicher Qdrant-Aufruf: der Payload kommt aus dem Skill-Graph."""
    prov = _provider({"n-fremd": {"content": "X", "access_level": "public", "scope": FOREIGN}})

    class _NoQdrant:
        def __getattr__(self, name):
            raise AssertionError(f"Qdrant darf hier nicht angefasst werden ({name})")

    prov._qdrant = _NoQdrant()
    import nexus.graph.traversal as traversal
    original = traversal.GraphTraversal
    traversal.GraphTraversal = _FakeTraversal
    try:
        items = prov._graph_boost([types.SimpleNamespace(id="root")], max_boost=1, max_depth=1)
    finally:
        traversal.GraphTraversal = original
    assert "NACHBAR-FREMD-OPENCLAW" not in "\n".join(items)
    assert items == [], f"nur fremde Nachbarn → nichts darf uebrig bleiben: {items}"


check("_graph_boost filtert ohne zusaetzlichen Qdrant-Aufruf", _boost_uses_payload_without_extra_call)


# ── 2) Der ausdrueckliche Abruf (nexus_recall des Plugins) ─────────────────

class _P:
    def __init__(self, pid, payload):
        self.id = pid
        self.payload = payload
        self.score = 0.9


RECALL_POINTS = [
    _P("p-default", {"id": "p-default", "content": "gemeinsam", "category": "fact",
                     "access_level": "public", "scope": SHARED}),
    _P("p-fremd", {"id": "p-fremd", "content": "fremdes Protokoll", "category": "session",
                   "access_level": "public", "scope": FOREIGN}),
    _P("p-alt", {"id": "p-alt", "content": "Altbestand", "category": "fact",
                 "access_level": "public"}),
    _P("p-eigen", {"id": "p-eigen", "content": "eigenes Protokoll", "category": "session",
                   "access_level": "public", "scope": BUNDLE}),
]
ERWARTET = {"p-alt", "p-default", "p-eigen"}


class _QueryQdrant:
    def __init__(self, points):
        self._points = points
        self.filters = []

    def query_points(self, **kwargs):
        self.filters.append(kwargs.get("query_filter"))
        return types.SimpleNamespace(points=list(self._points))

    def set_payload(self, **_kw):
        pass

    def retrieve(self, **_kw):
        return []


def _recall_provider():
    prov = _provider({})
    prov._embedder = object()
    prov._qdrant = _QueryQdrant(RECALL_POINTS)
    prov._rerank_cfg = {"enabled": False}
    prov._embed_cached = lambda _q: [0.0] * 8
    prov._embedder_available = lambda: True
    prov._bump_agent_stats = lambda **_kw: None
    prov._flywheel_bump = lambda *_a, **_kw: None
    prov._can_read = lambda _pl: True
    prov._apply_dynamics_tiebreak = lambda pts, _eff: pts
    prov._graph_boost = lambda *a, **k: []
    prov._agent_context = "primary"
    return prov


def _recall_four_cases() -> None:
    prov = _recall_provider()
    rows = prov._recall("worum geht es", limit=10)
    ids = {r["id"] for r in rows}
    assert ids == ERWARTET, f"Plugin-Recall liefert {sorted(ids)}"
    flt = prov._qdrant.filters[0]
    assert flt is not None, "recall muss Qdrant einen Sichtbarkeitsfilter mitgeben"
    assert len(flt.should) == 3, f"drei Bedingungen erwartet: {flt.should}"


check("_recall: die vier Sichtbarkeitsfaelle + Qdrant-Filter", _recall_four_cases)


print()
if FAILED:
    print(f"{len(FAILED)} Probe(n) fehlgeschlagen")
    sys.exit(1)
print("alle Proben bestanden")
