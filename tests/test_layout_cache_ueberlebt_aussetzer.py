"""Ein Qdrant-Aussetzer darf das Schreiben NICHT dauerhaft zerstören.

Anlass (09.10.2026, gemessen): ``scope_auto.vector_field`` legte auch ein
FEHLGESCHLAGENES Ergebnis dauerhaft in ``_LAYOUT_CACHE`` ab. Ein einziger
Netzwerk-Hänger genügte damit, um für den ganzen Prozess jeden späteren
Schreibvorgang zu zerstören: Die Auflösung lieferte fortan ``None`` (die anonyme
Form), eine Sammlung mit benanntem Vektor lehnte das mit ``400 Not existing
vector name`` ab, und die Erinnerung wurde nicht gespeichert — ohne ein Wort.

Nebenwirkung, die den Fehler auffliegen liess: In der Testsuite fiel eine Datei
in einer Reihenfolge durch und in der anderen nicht, weil der eine Test den
vergifteten Zwischenspeicher hinterliess.
"""
from __future__ import annotations

import importlib.util
import sys
import urllib.request
import pathlib

import pytest

WURZEL = pathlib.Path(__file__).resolve().parents[1]
SKRIPTE = WURZEL / "plugins" / "claude-code" / "scripts"


def _scope_auto():
    if not (SKRIPTE / "scope_auto.py").exists():
        pytest.skip("Claude-Code-Plugin nicht vorhanden")
    spec = importlib.util.spec_from_file_location(
        "_scope_auto_cache_test", SKRIPTE / "scope_auto.py"
    )
    assert spec and spec.loader
    modul = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = modul
    spec.loader.exec_module(modul)
    return modul


def _qdrant_erreichbar() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:6333/collections", timeout=4):
            return True
    except Exception:
        return False


def test_fehlgeschlagener_blick_wird_nicht_gecacht():
    """Nach einem Aussetzer muss der NAECHSTE Aufruf es erneut versuchen."""
    sa = _scope_auto()
    sa._LAYOUT_CACHE.clear()

    echter = urllib.request.urlopen
    urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(OSError("Qdrant weg"))
    try:
        erste = sa.vector_field("http://localhost:6333", "test-collection")
    finally:
        urllib.request.urlopen = echter

    assert erste is None, (
        "Ein unlesbares Layout muss fuer DIESEN Aufruf die anonyme Form liefern"
    )
    assert ("http://localhost:6333", "test-collection") not in sa._LAYOUT_CACHE, (
        "Ein FEHLGESCHLAGENER Layout-Blick wurde zwischengespeichert. Ein einziger "
        "Netzwerk-Haenger zerstört damit jeden spaeteren Schreibvorgang des Prozesses: "
        "die Aufloesung liefert fortan None, Qdrant lehnt die flache Form mit 400 ab, "
        "und die Erinnerung geht still verloren."
    )


def test_erholung_nach_aussetzer():
    """Und die Gegenprobe: ist Qdrant wieder da, muss es wieder funktionieren."""
    if not _qdrant_erreichbar():
        pytest.skip("Qdrant nicht erreichbar")
    sa = _scope_auto()
    sa._LAYOUT_CACHE.clear()

    echter = urllib.request.urlopen
    urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(OSError("weg"))
    try:
        sa.vector_field("http://localhost:6333", "test-collection")
    finally:
        urllib.request.urlopen = echter

    # Jetzt ist Qdrant wieder da — der naechste Aufruf muss den Namen finden.
    name = sa.vector_field("http://localhost:6333", "test-collection")
    try:
        assert name, (
            "Nach der Erholung liefert die Aufloesung immer noch None — der "
            "Zwischenspeicher haelt den Fehlschlag fest und das Schreiben bleibt kaputt."
        )
    finally:
        sa._LAYOUT_CACHE.clear()


def test_erfolgreicher_blick_wird_gecacht():
    """Die Ersparnis muss bleiben: ein erfolgreicher Blick wird gemerkt."""
    if not _qdrant_erreichbar():
        pytest.skip("Qdrant nicht erreichbar")
    sa = _scope_auto()
    sa._LAYOUT_CACHE.clear()

    sa.vector_field("http://localhost:6333", "test-collection")
    try:
        assert ("http://localhost:6333", "test-collection") in sa._LAYOUT_CACHE, (
            "Ein erfolgreicher Layout-Blick wird nicht mehr zwischengespeichert — "
            "damit fragt jeder Schreibvorgang Qdrant erneut, was die Latenz treibt."
        )
    finally:
        sa._LAYOUT_CACHE.clear()
