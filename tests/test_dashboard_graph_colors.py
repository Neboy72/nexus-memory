"""Wache: jede Kategorie im Bestand hat eine eigene Farbe — und der Ersatzton
ist NICHT die Temp-Farbe.

Der Defekt: die Farbtabelle kannte nur sechs Kategorien. Die uebrigen neun
(6.024 Eintraege) fielen auf den Ersatzton — und der war exakt die Temp-Farbe.
In der Anzeige sahen damit 227 Knoten aus wie "Temp", obwohl es genau EINEN
temp-Eintrag gab. Zusaetzlich stand die Legende fest im HTML und behauptete
dieselbe Falschaussage.
"""

import os
import re

STATIC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "dashboard", "static",
)
COLORS_JS = os.path.join(STATIC, "webui-js", "colors.js")
GRAPH_JS = os.path.join(STATIC, "webui-js", "graph.js")
GRAPH_HTML = os.path.join(STATIC, "graph.html")

# Die Kategorien, die im Bestand wirklich vorkommen (gemessen 12.10.2026:
# 38.686 Punkte, 15 Kategorien).
BESTANDSKATEGORIEN = [
    "fact", "entity", "rule", "uncategorized", "session", "preference", "belief",
    "pattern", "procedure", "lesson", "decision", "test", "idea", "sica_session", "temp",
]


def _palette():
    """Nur der Kategorie-Block — die Drift-Farben haben eigene Namen und
    duerfen hier nicht mitgezaehlt werden."""
    src = open(COLORS_JS, encoding="utf-8").read()
    block = src.split("NEXUS_CATEGORY_COLORS = {", 1)[1].split("};", 1)[0]
    return dict(re.findall(r"^\s*([a-z_]+):\s*'(#[0-9a-fA-F]{6})'", block, re.M))


def test_every_category_in_the_store_has_its_own_colour():
    palette = _palette()
    fehlend = [c for c in BESTANDSKATEGORIEN if c not in palette]
    assert not fehlend, f"ohne eigene Farbe (fallen auf den Ersatzton): {fehlend}"


def test_no_two_categories_share_a_colour():
    palette = _palette()
    gesehen = {}
    for cat, col in palette.items():
        if col in gesehen:
            raise AssertionError(f"'{cat}' und '{gesehen[col]}' teilen sich {col}")
        gesehen[col] = cat


def test_fallback_is_not_the_temp_colour():
    src = open(COLORS_JS, encoding="utf-8").read()
    fallback = re.search(r"NEXUS_CATEGORY_FALLBACK_COLOR\s*=\s*'(#[0-9a-fA-F]{6})'", src)
    assert fallback, "der Ersatzton muss benannt sein"
    assert fallback.group(1) != _palette()["temp"], (
        "ein unbekannter Eintrag darf nicht wie ein temporaerer aussehen"
    )


def test_legend_is_built_from_the_data_not_hardcoded():
    """Die Legende stand fest im HTML und behauptete 'grau = Temp'."""
    html = open(GRAPH_HTML, encoding="utf-8").read()
    js = open(GRAPH_JS, encoding="utf-8").read()
    assert "_renderLegend" in js, "die Legende muss aus dem Bestand gebaut werden"
    hartkodiert = re.findall(r'legend-dot--(fact|belief|session|rule|preference|temp)"', html)
    assert not hartkodiert, f"feste Legendeneintraege im HTML: {hartkodiert}"
