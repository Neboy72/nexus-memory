"""Die Auswahlregel des Abrufs (20.11.2026) — Kategorien und Mindestpunktzahl.

Anlass: Der Auto-Recall lieferte je Nachricht fuenf Eintraege, davon oft zwei
bis drei ohne Bezug — Entities wie "spotify", halbe Sprachsaetze als "rule".
Die Regel dazu steht an EINER Stelle (``nexus_memory.recall_policy``), damit
Dienst und Plugins nicht wieder auseinanderlaufen.

Was hier festgehalten wird:

1. Die drei Hebel einzeln: Kategorien, Mindestpunktzahl, und die Regel, dass
   ein Eintrag ohne Kategorie als ``fact`` gilt, ein aufgezeichneter Turn
   dagegen als ``session``.
2. Die Punktzahl ist eine Aehnlichkeit (Kosinus), kein Rang: ein Eintrag mit
   hohem RRF-Rang und schlechtem Kosinus faellt durch.
3. Der Grenzfall: nichts ueber der Schwelle ergibt eine LEERE Liste, nicht
   das naechstbeste Fuellmaterial.
4. Ohne Angabe bleibt alles wie vorher (andere Aufrufer merken nichts).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from nexus_memory import recall_policy


# ── Kategorien ───────────────────────────────────────────────────────


def test_kategorienfilter_haelt_entities_und_protokolle_draussen():
    eintraege = [
        {"category": "fact", "vector_score": 0.8},
        {"category": "entity", "vector_score": 0.9},
        {"category": "session", "vector_score": 0.9},
        {"category": "rule", "vector_score": 0.7},
    ]
    gewaehlt = recall_policy.select(
        eintraege, categories=("fact", "rule", "preference"))
    assert [e["category"] for e in gewaehlt] == ["fact", "rule"]


def test_eintrag_ohne_kategorie_gilt_als_fakt():
    """Altbestand (Dokumente, alte Fakten) hat kein Kategoriefeld.

    Er wird seit jeher als ``fact`` ausgeliefert; ein Filter, der ihn
    wegwirft, wuerde den halben Bestand unsichtbar machen.
    """
    assert recall_policy.effective_category({}) == "fact"
    assert recall_policy.effective_category({"category": None}) == "fact"
    assert recall_policy.effective_category({"category": " FACT "}) == "fact"


def test_aufgezeichneter_turn_gilt_als_sitzung():
    """Ein Turn ist ein Protokoll, keine Tatsache — auch mit Kategorie.

    Gemessen am 20.11.2026: 1714 Turn-Punkte tragen die Kategorie ``fact``.
    Ohne diese Regel waeren genau die halben Sprachsaetze wieder drin, wegen
    derer die Aenderung gemacht wird.
    """
    assert recall_policy.effective_category({"type": "turn", "category": "fact"}) == "session"
    assert recall_policy.effective_category({"type": "turn"}) == "session"
    # Ein normaler Punkt mit Typ "memory" bleibt bei seiner Kategorie.
    assert recall_policy.effective_category({"type": "memory", "category": "rule"}) == "rule"


def test_unbekannte_kategorie_ist_ein_fehler_statt_kein_filter():
    """Ein Tippfehler darf nicht zu "alles durchlassen" werden."""
    with pytest.raises(recall_policy.RecallPolicyError):
        recall_policy.normalize_categories(["fac"])
    with pytest.raises(recall_policy.RecallPolicyError):
        recall_policy.select([], categories=["fakt"])


def test_kategorien_als_kommaliste():
    """Umgebungsvariablen und Kommandozeilen liefern eine Zeichenkette."""
    assert recall_policy.normalize_categories("fact, rule ,,") == ("fact", "rule")
    assert recall_policy.normalize_categories("") is None
    assert recall_policy.normalize_categories([]) is None
    assert recall_policy.normalize_categories(None) is None


# ── Mindestpunktzahl ─────────────────────────────────────────────────


def test_mindestpunktzahl_prueft_die_aehnlichkeit_nicht_den_rang():
    """RRF-Rang ist keine Kosinus-Aehnlichkeit.

    Der Spitzenreiter hat nach der Normalisierung immer den Wert 1.0 — auch
    wenn er nicht passt. Ein Eintrag mit hohem Rang und schlechtem Kosinus
    muss deshalb fallen.
    """
    eintraege = [
        {"category": "fact", "score": 1.0, "vector_score": 0.18},  # Rang 1, schlechter Kosinus
        {"category": "fact", "score": 0.6, "vector_score": 0.61},
    ]
    gewaehlt = recall_policy.select(eintraege, min_score=0.35)
    assert len(gewaehlt) == 1
    assert gewaehlt[0]["vector_score"] == 0.61


def test_eintrag_ohne_kosinus_aber_mit_worttreffer_besteht():
    """Nur woertlich gefundene Eintraege (Kennung, Zitat) sind nicht schlechter.

    Die Vektorsuche sieht nur ihre Top-Kandidaten. Ein Eintrag, der woertlich
    passt und dort nicht auftaucht, hat trotzdem die staerkste Evidenz, die es
    gibt — ihn wegen einer fehlenden Zahl zu verwerfen waere falsch.
    """
    nur_woertlich = {"category": "fact", "bm25_score": 4.2}
    assert recall_policy.passes(nur_woertlich, min_score=0.35) is True


def test_eintrag_ohne_jede_evidenz_faellt_durch():
    ohne_alles = {"category": "fact"}
    assert recall_policy.passes(ohne_alles, min_score=0.35) is False


def test_negative_kosinuswerte_zaehlen_als_kein_bezug():
    assert recall_policy.relevance({"vector_score": -0.2}) == 0.0
    assert recall_policy.passes({"vector_score": -0.2}, min_score=0.01) is False


def test_mindestpunktzahl_ungueltig_ist_ein_fehler():
    with pytest.raises(recall_policy.RecallPolicyError):
        recall_policy.normalize_min_score(1.5)
    with pytest.raises(recall_policy.RecallPolicyError):
        recall_policy.normalize_min_score("hoch")
    assert recall_policy.normalize_min_score(None) is None
    assert recall_policy.normalize_min_score(0.0) == 0.0


# ── Der Grenzfall und die Voreinstellung ─────────────────────────────


def test_kein_treffer_ueber_der_schwelle_gibt_einen_leeren_block():
    """Nichts ueber der Schwelle heisst NICHTS — kein Fuellmaterial.

    Das ist der eigentliche Zweck: lieber kein Kontext als vier falsche
    Eintraege.
    """
    eintraege = [
        {"category": "fact", "vector_score": 0.10},
        {"category": "rule", "vector_score": 0.22},
        {"category": "preference", "vector_score": 0.30},
    ]
    assert recall_policy.select(
        eintraege,
        categories=recall_policy.DEFAULT_RECALL_CATEGORIES,
        min_score=recall_policy.DEFAULT_RECALL_MIN_SCORE,
    ) == []


def test_ohne_angabe_bleibt_alles_wie_vorher():
    """Andere Aufrufer (ausdruecklicher recall, CLI) merken nichts."""
    eintraege = [
        {"category": "entity", "vector_score": 0.1},
        {"category": "session", "vector_score": 0.1},
        {"vector_score": 0.9},
    ]
    assert recall_policy.select(eintraege) == eintraege


def test_voreinstellungen_sind_die_des_auftrags():
    assert recall_policy.DEFAULT_RECALL_LIMIT == 3
    assert recall_policy.DEFAULT_RECALL_BUDGET_CHARS == 800
    assert recall_policy.DEFAULT_RECALL_CATEGORIES == ("fact", "rule", "preference")
    assert 0.0 < recall_policy.DEFAULT_RECALL_MIN_SCORE <= 1.0


def test_grenze_kuerzt_nach_der_auswahl():
    eintraege = [
        {"category": "fact", "id": "a", "vector_score": 0.9},
        {"category": "entity", "id": "b", "vector_score": 0.9},
        {"category": "fact", "id": "c", "vector_score": 0.8},
    ]
    gewaehlt = recall_policy.select(eintraege, categories=["fact"], limit=1)
    assert [e["id"] for e in gewaehlt] == ["a"]
