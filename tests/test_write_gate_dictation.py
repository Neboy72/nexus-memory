"""Diktat-Reste im Schreibtor (20.11.2026).

Anlass: Bei Nebo landeten diktierte Sprachfetzen als dauerhafte ``rule`` im
Gedaechtnis und wurden bei jedem Turn eingespielt ("dann müssten die alle da
rein oder oder wie soll ich mir das vorstellen sagte dir nee nee ...").

Die Regel erkennt sie an der FORM — Flickwoerter, Wortdopplungen, Anrede,
fehlendes Satzzeichen, langer Schachtelsatz — und VERWIRFT sie nicht: die
Kategorie wird auf ``session`` herabgestuft. Dort gehoert ein Transkript hin,
und der Auto-Recall laesst Sitzungen draussen.

Was hier festgehalten wird:

1. Ein Diktat-Rest wird ``session``, der Inhalt bleibt Wort fuer Wort stehen.
2. Eine geschriebene Regel bleibt eine Regel (mit Nachweis) — die Heuristik
   schlaegt keine menschliche Bestaetigung.
3. Ein knapper, aber echter Satz wird NICHT erkannt: die einzelnen Merkmale
   (kein Punkt am Ende, "du", lang) reichen allein nicht.
4. Die Schwelle ist eine gemessene Zahl, keine Stimmung: der Trockenlauf ueber
   den ganzen Bestand ist im Benchmark dokumentiert.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import nexus.write_gate as write_gate  # noqa: E402

#: Ein echter Diktat-Rest aus dem Bestand (11 Punkte).
DIKTAT = (
    "dann müssten die alle da rein oder oder wie soll ich mir das vorstellen "
    "sagte dir nee nee hast du gedacht bevor du was gemacht hast oder wie "
    "funktioniert das keine ahnung das das habe ich noch nicht verstanden"
)
#: Geschriebene Regel: Etikett, Datum, Aussage — kein Diktat.
REGEL = (
    "REGEL (Nebo, 25.10.2026, nach dem Router-Debakel): Vor jedem Eingriff an "
    "Geraeten/Firmware die Anleitung VOLLSTAENDIG lesen, nicht nur den Titel."
)


# ── Der Diktat-Rest ──────────────────────────────────────────────────


def test_diktat_rest_wird_session_statt_regel():
    ergebnis = write_gate.prepare(
        DIKTAT, category="rule", source="chat", source_type="chat")
    assert ergebnis is not None, "nichts verwerfen: der Inhalt bleibt"
    assert ergebnis["category"] == "session"
    assert ergebnis["content"] == DIKTAT, "Wort fuer Wort unveraendert"


def test_diktat_rest_traegt_den_grund_im_provenance():
    ergebnis = write_gate.prepare(DIKTAT, category="rule", source="chat")
    grund = ergebnis["provenance"]["dictated_residue"]
    assert "Punkte" in grund
    assert "Wortdopplung" in grund


def test_diktat_rest_als_fact_wird_auch_session():
    """Auch ein Fakt, der diktiert wurde, ist ein Transkript — kein Wissen."""
    ergebnis = write_gate.prepare(DIKTAT, category="fact", source="chat")
    assert ergebnis["category"] == "session"


def test_session_bleibt_session():
    ergebnis = write_gate.prepare(DIKTAT, category="session", source="chat")
    assert ergebnis["category"] == "session"
    assert "dictated_residue" not in ergebnis["provenance"], (
        "ein Transkript braucht keine zweite Herabstufung")


# ── Was NICHT erkannt werden darf ────────────────────────────────────


def test_geschriebene_regel_bleibt_regel():
    from nexus.rule_confirm import issue

    ergebnis = write_gate.prepare(
        REGEL, category="rule", source="manual", source_type="manual",
        rule_proof=issue(REGEL))
    assert ergebnis["category"] == "rule"


def test_knapper_satz_wird_nicht_erkannt():
    """Kein Punkt am Ende allein ist kein Diktat — sonst faellt der halbe Bestand."""
    for text in (
        "Der Slice-Fehler muss woanders liegen",
        "Es muss passen",
        "Per Regel: [SILENT]",
        "Alle Checks gruen, keine neuen Fehler",
    ):
        punkte, _ = write_gate.dictation_reasons(text)
        assert punkte < write_gate._DICTATION_SCORE_THRESHOLD, text


def test_wortdopplung_in_zusammensetzung_ist_keine_dopplung():
    """"eine Eine-Zeilen-Warnung" ist eine Fuegung, keine Diktat-Dopplung.

    Im Trockenlauf traf die Gross/Klein-blind Fassung 15 echte Regeln.
    """
    punkte, gruende = write_gate.dictation_reasons(
        "Bei Alarm gibt der Agent eine Eine-Zeilen-Warnung mit Handlungsempfehlung.")
    assert not any("Wortdopplung" in g for g in gruende)


def test_wendung_als_ganzes_wort():
    """"ne ne" steckt sonst in "eine neue" — Wortgrenzen sind Pflicht."""
    punkte, gruende = write_gate.dictation_reasons(
        "Direkt nach dem Frontmatter eine neue Sektion einfuegen.")
    assert not any("Wendung" in g for g in gruende)


# ── Bestaetigung schlaegt Heuristik ──────────────────────────────────


def test_bestaetigte_regel_schlaegt_die_heuristik():
    """Ein Mensch hat am Terminal bestaetigt — dann bleibt es eine Regel.

    Die Formheuristik darf eine bewusste Entscheidung nicht umdrehen.
    """
    from nexus.rule_confirm import issue

    ergebnis = write_gate.prepare(
        DIKTAT, category="rule", source="manual", source_type="manual",
        rule_proof=issue(DIKTAT))
    assert ergebnis["category"] == "rule"
    assert ergebnis["provenance"]["rule_confirmed"] is True


def test_frage_als_diktat_wird_nicht_abgewiesen():
    """"nichts verwerfen": ein gesprochener Fragesatz wird session, nicht verworfen."""
    text = "kannst du mir bitte sagen warum das gestern nicht funktioniert hat oder oder"
    punkte, _ = write_gate.dictation_reasons(text)
    if punkte >= write_gate._DICTATION_SCORE_THRESHOLD:
        ergebnis = write_gate.prepare(text, category="fact", source="chat")
        assert ergebnis is not None
        assert ergebnis["category"] == "session"
    else:  # pragma: no cover - Sicherheitsnetz fuer kuenftige Gewichte
        pytest.skip("Beispiel unterschreitet die Schwelle")
