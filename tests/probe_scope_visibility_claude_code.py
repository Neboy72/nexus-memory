#!/usr/bin/env python3
"""Probe: Sichtbarkeit nach Verbund in den Claude-Code-Lesewegen.

Ausfuehren:  python3 tests/probe_scope_visibility_claude_code.py

Kein Pytest — eine ausfuehrbare Probe. Sie faehrt die ECHTEN Hook-Funktionen
(`auto_recall.search_qdrant`, `auto_recall.graph_boost`,
`session_start.search_qdrant`, `guardrail_check.load_protection_rules`) gegen
einen Qdrant-Stub und prueft:

  a) Punkt in "default"        → wird gefunden
  b) Punkt in "hermes" (fremd) → wird NICHT gefunden
  c) Punkt ohne scope-Feld     → wird gefunden (Altbestand)
  d) Punkt im eigenen Verbund  → wird gefunden

Zwei Dinge werden getrennt geprueft: der Filter, der WIRKLICH an Qdrant geht
(Auswertung des Request-Bodys), und das Ergebnis nach der zweiten, unabhaengigen
Pruefung im Hook.

Dazu das SCHREIBZIEL des Captures (15.11.2026): genau zwei Ziele — ein
Sitzungsprotokoll in den eigenen Verbund, alles andere in den gemeinsamen
Bereich. Ein aus Zentroiden geratener Bereich darf nicht mehr vorkommen.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "claude-code" / "scripts"
sys.path.insert(0, str(SCRIPTS))

# Verbund des Prueflings. NEXUS_SCOPE bleibt leer, sonst gewaenne der Override.
os.environ["NEXUS_AGENT_ID"] = "claude-code"
os.environ.pop("NEXUS_SCOPE", None)

FAILED: list[str] = []

BUNDLE = "claude-code"
FOREIGN = "hermes"
SHARED = "default"


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


def _load(name: str):
    """Das Hook-Modul aus dem scripts/-Verzeichnis, ohne das Verzeichnis zu
    installieren (der Import von `_embedding`/`scope_auto` geht ueber sys.path)."""
    spec = importlib.util.spec_from_file_location(f"_probe_{name}", SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"_probe_{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


scope_auto = _load("scope_auto")
auto_recall = _load("auto_recall")
session_start = _load("session_start")
guardrail_check = _load("guardrail_check")

# ── Fixtures: je ein Punkt pro Sichtbarkeitsfall ────────────────────────────

HITS = [
    {"id": "p-default", "score": 0.9,
     "payload": {"content": "gemeinsam", "category": "fact", "access_level": "public",
                 "scope": SHARED}},
    {"id": "p-fremd", "score": 0.9,
     "payload": {"content": "fremdes Protokoll", "category": "session",
                 "access_level": "public", "scope": FOREIGN}},
    {"id": "p-alt", "score": 0.9,
     "payload": {"content": "Altbestand", "category": "fact", "access_level": "public"}},
    {"id": "p-eigen", "score": 0.9,
     "payload": {"content": "eigenes Protokoll", "category": "session",
                 "access_level": "public", "scope": BUNDLE}},
]
ERWARTET = {"p-alt", "p-default", "p-eigen"}

BY_ID = {h["id"]: h for h in HITS}


# ── Ein Qdrant-Stub, der Requests mitschreibt ───────────────────────────────

class _Resp:
    def __init__(self, body: dict):
        self._raw = json.dumps(body).encode("utf-8")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class _QdrantStub:
    """Beantwortet GET /collections/... und POST /points/{search,scroll}."""

    def __init__(self):
        self.bodies: list[dict] = []
        self.urls: list[str] = []

    def __call__(self, req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        data = getattr(req, "data", None)
        self.urls.append(url)
        if data is None:
            return _Resp({"result": {"config": {"params": {
                "vectors": {"size": 8, "distance": "Cosine"}}}}})
        body = json.loads(data.decode())
        self.bodies.append(body)
        if url.endswith("/points/search"):
            return _Resp({"result": HITS})
        if url.endswith("/points/scroll"):
            return self._scroll(body)
        raise AssertionError(f"unerwarteter Request: {url}")

    def _scroll(self, body: dict):
        must = (body.get("filter") or {}).get("must") or []
        has_id = None
        for clause in must:
            if isinstance(clause, dict) and "has_id" in clause:
                has_id = clause["has_id"]
        if has_id is None:
            # Centroids/Regeln: leere Menge genuegt.
            return _Resp({"result": {"points": [], "next_page_offset": None}})
        punkt = BY_ID.get(has_id[0])
        return _Resp({"result": {"points": [punkt] if punkt else [],
                                 "next_page_offset": None}})


def _use_stub():
    stub = _QdrantStub()
    urllib.request.urlopen = stub      # die Hooks rufen urllib.request.urlopen
    return stub


_real_urlopen = urllib.request.urlopen


def _restore():
    urllib.request.urlopen = _real_urlopen


# ── Ein winziger Qdrant-Filter-Auswerter (must = UND, should = ODER) ────────

def _matches_clause(payload: dict, clause: dict) -> bool:
    if "should" in clause and "key" not in clause:
        # Verschachtelte Filtergruppe (must: [{should: [...]}]) — Qdrant
        # wertet sie als UND-Gruppe mit ODER darin aus.
        should = clause["should"] or []
        return not should or any(_matches_clause(payload, c) for c in should)
    if "is_empty" in clause:
        v = payload.get(clause["is_empty"]["key"])
        return v is None or v == ""
    if "has_id" in clause:
        return False                   # IDs sind hier nicht der Pruefgegenstand
    if "key" in clause and "match" in clause:
        v = payload.get(clause["key"])
        match = clause["match"]
        if "value" in match:
            return v == match["value"]
        if "any" in match:
            return v in match["any"]
        if "except" in match:
            return v is not None and v not in match["except"]
    raise AssertionError(f"unbekannte Klausel: {clause}")


def _matches(payload: dict, flt: dict) -> bool:
    must = flt.get("must") or []
    should = flt.get("should") or []
    must_ok = all(_matches_clause(payload, c) for c in must)
    should_ok = not should or any(_matches_clause(payload, c) for c in should)
    return must_ok and should_ok


def _scope_group(flt: dict) -> dict | None:
    """Die Verbund-Gruppe im Filter finden (must-Eintrag mit should-Liste)."""
    for clause in flt.get("must") or []:
        if isinstance(clause, dict) and "should" in clause and "key" not in clause:
            werte = [c.get("match", {}).get("value") for c in clause["should"]]
            if SHARED in werte:
                return clause
    return None


# ── 0) Die gemeinsame Filterform ────────────────────────────────────────────

def _form() -> None:
    should = scope_auto.scope_should(BUNDLE)
    assert len(should) == 3, f"drei Bedingungen erwartet: {should}"
    assert should[0] == {"key": "scope", "match": {"value": SHARED}}
    assert should[1] == {"key": "scope", "match": {"value": BUNDLE}}
    assert should[2] == {"is_empty": {"key": "scope"}}


def _four_cases() -> None:
    assert scope_auto.scope_visible({"scope": SHARED}, BUNDLE) is True, "gemeinsam"
    assert scope_auto.scope_visible({"scope": FOREIGN}, BUNDLE) is False, "fremd"
    assert scope_auto.scope_visible({}, BUNDLE) is True, "kein Feld = Altbestand"
    assert scope_auto.scope_visible({"scope": BUNDLE}, BUNDLE) is True, "eigener Verbund"


def _bundle() -> None:
    assert scope_auto.own_bundle() == BUNDLE, f"Vorgabe ist {BUNDLE}"
    os.environ["NEXUS_AGENT_ID"] = "dsh.harness"
    try:
        assert scope_auto.own_bundle() == "dsh-harness", "Punkt wird auf '-' abgebildet"
    finally:
        os.environ["NEXUS_AGENT_ID"] = BUNDLE


check("scope_should traegt die drei Bedingungen als should", _form)
check("scope_visible: die vier Faelle", _four_cases)
check("own_bundle: Agenten-Kennung, Vorgabe claude-code", _bundle)


# ── 0b) Schreibziel des Captures: genau zwei Ziele (15.11.2026) ────────────
# session → eigener Verbund, alles andere → gemeinsam. Ein aus Zentroiden
# geratener Themen-Bereich darf NICHT mehr vorkommen: der Punkt waere fuer
# alle unsichtbar, auch fuer den Schreiber.

auto_capture = _load("auto_capture")
import scope_auto as _sa_hooks  # noqa: E402  — genau das Modul, das der Hook zieht


def _write_targets() -> None:
    os.environ.pop("NEXUS_SCOPE", None)
    assert auto_capture._resolve_capture_scope("session") == BUNDLE, \
        "ein Sitzungsprotokoll gehoert in den eigenen Verbund"
    for category in ("fact", "rule", "belief", "preference", "temp"):
        assert auto_capture._resolve_capture_scope(category) == SHARED, category


def _write_target_manual_scope() -> None:
    os.environ["NEXUS_SCOPE"] = "voice"
    try:
        assert auto_capture._resolve_capture_scope("session") == "voice", \
            "ein gueltiges NEXUS_SCOPE ist der eigene Verbund"
        assert auto_capture._resolve_capture_scope("fact") == SHARED, \
            "ein Fakt bleibt gemeinsam, auch mit gesetztem NEXUS_SCOPE"
    finally:
        os.environ.pop("NEXUS_SCOPE", None)


def _write_target_no_guessing() -> None:
    """Klarer Zentroid-Treffer darf das Ziel NICHT mehr bestimmen."""
    alt_fetch, alt_infer = _sa_hooks.fetch_centroids, _sa_hooks.infer_scope
    _sa_hooks.fetch_centroids = lambda *_a, **_kw: {"voice": [1.0, 0.0]}
    _sa_hooks.infer_scope = lambda *_a, **_kw: "voice"
    os.environ.pop("NEXUS_SCOPE", None)
    try:
        assert auto_capture._resolve_capture_scope("fact") == SHARED, \
            "ein geratener Bereich ist kein Verbund"
        assert auto_capture._resolve_capture_scope("session") == BUNDLE, \
            "auch ein Protokoll nimmt keinen geratenen Bereich"
    finally:
        _sa_hooks.fetch_centroids, _sa_hooks.infer_scope = alt_fetch, alt_infer


check("Capture-Schreibziel: session → Verbund, alles andere → gemeinsam", _write_targets)
check("Capture-Schreibziel: gesetztes NEXUS_SCOPE ist der eigene Verbund",
      _write_target_manual_scope)
check("Capture-Schreibziel: kein geratener Bereich aus Zentroiden", _write_target_no_guessing)


# ── 1) auto_recall.search_qdrant ────────────────────────────────────────────

def _auto_recall_search():
    auto_recall._record_access = lambda **_kw: None      # kein Registry-Schreibzugriff
    stub = _use_stub()
    try:
        results = auto_recall.search_qdrant([0.1, 0.2], limit=10)
    finally:
        _restore()
    body = next(b for b in stub.bodies if "vector" in b)
    flt = body["filter"]
    return body, flt, results


def _recall_filter_has_both_axes() -> None:
    body, flt, _ = _auto_recall_search()
    group = _scope_group(flt)
    assert group is not None, f"Verbund-Gruppe fehlt im Filter: {flt}"
    assert len(group["should"]) == 3, f"drei Bedingungen erwartet: {group}"


def _recall_four_cases_on_the_wire() -> None:
    _body, flt, _results = _auto_recall_search()
    sichtbar = {h["id"] for h in HITS if _matches(h["payload"], flt)}
    assert sichtbar == ERWARTET, f"Qdrant-Filter laesst {sorted(sichtbar)} durch"


def _recall_four_cases_in_the_hook() -> None:
    _body, _flt, results = _auto_recall_search()
    ids = {r["id"] for r in results}
    assert ids == ERWARTET, f"Hook liefert {sorted(ids)}"


check("auto_recall: Filter traegt Stufe UND Verbund (zwei Achsen)", _recall_filter_has_both_axes)
check("auto_recall: die vier Faelle im Filter, der rausgeht", _recall_four_cases_on_the_wire)
check("auto_recall: die vier Faelle im Ergebnis des Hooks", _recall_four_cases_in_the_hook)


# ── 2) auto_recall.graph_boost (Graph-Nachbarn) ────────────────────────────

def _graph_boost():
    stub = _use_stub()
    try:
        items = auto_recall.graph_boost(
            [{"id": "root"}], max_boost=1, access_level="public",
            query_embedding=[0.1, 0.2],
        )
    finally:
        _restore()
    return items, stub


def _graph_four_cases() -> None:
    # Die Wurzel traegt Kanten auf alle vier Fixtures.
    BY_ID["root"] = {"id": "root", "payload": {"content": "Wurzel", "access_level": "public",
                                               "scope": SHARED,
                                               "edges": [{"target_fact_id": pid,
                                                          "relation": "uses",
                                                          "status": "active"}
                                                         for pid in ("p-default", "p-fremd",
                                                                     "p-alt", "p-eigen")]}}
    try:
        items, _stub = _graph_boost()
    finally:
        BY_ID.pop("root", None)
    block = "\n".join(items)
    assert "gemeinsam" in block, "Punkt in 'default' muss erscheinen"
    assert "fremdes Protokoll" not in block, "fremdes Protokoll darf NICHT erscheinen"
    assert "Altbestand" in block, "Punkt ohne scope-Feld muss erscheinen"
    assert "eigenes Protokoll" in block, "eigener Verbund muss erscheinen"


check("graph_boost: die vier Sichtbarkeitsfaelle", _graph_four_cases)


# ── 3) session_start.search_qdrant ─────────────────────────────────────────

def _session_search():
    stub = _use_stub()
    try:
        results = session_start.search_qdrant([0.1, 0.2], limit=10)
    finally:
        _restore()
    body = next(b for b in stub.bodies if "vector" in b)
    return body["filter"], results


def _session_filter() -> None:
    flt, _ = _session_search()
    group = _scope_group(flt)
    assert group is not None, f"Verbund-Gruppe fehlt im Filter: {flt}"
    assert len(group["should"]) == 3, f"drei Bedingungen erwartet: {group}"
    sichtbar = {h["id"] for h in HITS if _matches(h["payload"], flt)}
    assert sichtbar == ERWARTET, f"Qdrant-Filter laesst {sorted(sichtbar)} durch"


def _session_hook() -> None:
    _flt, results = _session_search()
    ids = {r["id"] for r in results}
    assert ids == ERWARTET, f"Hook liefert {sorted(ids)}"


check("session_start: Filter traegt den Verbund (drei Bedingungen)", _session_filter)
check("session_start: die vier Faelle im Ergebnis des Hooks", _session_hook)


# ── 4) guardrail_check.load_protection_rules (Regeln) ──────────────────────

def _guardrail_filter() -> None:
    stub = _use_stub()
    try:
        guardrail_check.load_protection_rules()
    finally:
        _restore()
    scroll = next(b for b in stub.bodies if "filter" in b and "limit" in b)
    flt = scroll["filter"]
    group = _scope_group(flt)
    assert group is not None, f"Verbund-Gruppe fehlt im Regel-Filter: {flt}"
    assert len(group["should"]) == 3, f"drei Bedingungen erwartet: {group}"
    # Die Regel-Bedingung bleibt daneben stehen (nicht ersetzt).
    assert any(c.get("key") == "category" for c in flt["must"] if isinstance(c, dict)), \
        f"category-Filter fehlt: {flt}"


check("guardrail_check: Regel-Filter traegt den Verbund neben category", _guardrail_filter)


# ── Ergebnis ───────────────────────────────────────────────────────────────

print()
if FAILED:
    print(f"{len(FAILED)} Probe(n) fehlgeschlagen")
    sys.exit(1)
print("alle Proben bestanden")
