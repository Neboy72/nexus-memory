#!/usr/bin/env python3
"""Tests fuer den Aufsichtsbericht (``scripts/nexus-aufsicht.py``).

Geprueft werden die rechenenden Teile: Zaehlung einer Punktliste,
Etikett-Erkennung, Zuwachs gegen eine Verlaufsdatei, Alarm ja/nein und das
Verhalten bei fehlendem Stichwortindex. Der Netzwerkzugriff wird ersetzt;
kein Test spricht Qdrant an.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SKRIPT = REPO_ROOT / "scripts" / "nexus-aufsicht.py"
INDEX_MODUL = REPO_ROOT / "plugins" / "memory" / "nexus" / "keyword_index.py"


def _lade(pfad: Path, name: str):
    """Laedt eine einzelne Datei als Modul (die Skripte sind keine Pakete)."""
    spez = importlib.util.spec_from_file_location(name, pfad)
    modul = importlib.util.module_from_spec(spez)
    sys.modules[spez.name] = modul
    spez.loader.exec_module(modul)
    return modul


AUFSICHT = _lade(SKRIPT, "nexus_aufsicht")


def _punkt(kennung, **payload):
    """Qdrant-Punkt in der Form, die der Scroll liefert."""
    return {"id": kennung, "payload": payload}


# ── Zaehlung ─────────────────────────────────────────────────────────────

def test_zaehlung_aus_punktliste():
    """Kennzahlen, Verteilungen und die drei Verdachtsfaelle stimmen."""
    punkte = [
        _punkt(1, content="x" * 50, category="fact", access_level="public",
               source="hermes", provenance={"created_by": "agent-a"}),
        _punkt(2, content="kurz", category="entity", access_level="private",
               provenance={"created_by": "agent-b"}),
        _punkt(3, category="entity"),  # ohne Inhalt, ohne Herkunft
        _punkt(4, content="service: Prod 1 (port=1)", category="entity",
               access_level="public", source="plugin"),
    ]

    zahlen = AUFSICHT.zaehle(punkte)

    assert zahlen["gesamt"] == 4
    assert zahlen["kategorien"] == {"fact": 1, "entity": 3}
    assert zahlen["zugriff"] == {"public": 2, "private": 1, "ohne Angabe": 1}
    assert zahlen["schreiber"] == {
        "agent-a": 1, "agent-b": 1, "plugin": 1, "ohne Angabe": 1}
    assert zahlen["ohne_inhalt"] == 1
    assert zahlen["unter_40"] == 2        # "kurz" und das kurze Etikett
    assert zahlen["ohne_provenance"] == 2  # Punkt 3 und Punkt 4
    assert zahlen["etiketten"] == 1


def test_zaehlung_leere_liste():
    """Eine leere Sammlung ergibt Nullen, keinen Fehler."""
    zahlen = AUFSICHT.zaehle([])
    assert zahlen["gesamt"] == 0
    assert zahlen["ohne_inhalt"] == 0
    assert zahlen["kategorien"] == {}


def test_zaehlung_gibt_keine_inhalte_weiter():
    """Das Ergebnis traegt nur Zahlen und Namen, keine Inhalte."""
    zahlen = AUFSICHT.zaehle([
        _punkt(1, content="GEHEIMER-INHALT im Klartext", category="fact")])
    assert "GEHEIMER" not in json.dumps(zahlen, ensure_ascii=False)


# ── Etikett-Erkennung ────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "organization: frame.io",
    "service: Prod 17494 (port=17494, status=healthy)",
    "software: Blink-Integration (status=loaded)",
    "device: /dev/null",
    "concept: Stiller Monitor",
])
def test_etikett_erkannt(text):
    """Kurzes Typwort mit Namen ohne Satz ist ein Etikett."""
    assert AUFSICHT.ist_etikett(text) is True


@pytest.mark.parametrize("text", [
    "service: Voice (port=9443) — Die Phrase ist vereinbartes Signal.",
    "service: simplytel – Simplytel zeigt Rechnungen nicht direkt an.",
    "Datum: 22.07.2026\nLeistungszeitpunkt: heute",
    "Ein Satz ohne Typwort am Anfang.",
    "software: foo bar baz qux",  # vier Woerter: schon ein Satzteil
    "",
    "   ",
])
def test_etikett_nicht_erkannt(text):
    """Satz hinter dem Strich, Fliesstext und Leeres sind kein Etikett."""
    assert AUFSICHT.ist_etikett(text) is False


# ── Zuwachs und Verlauf ──────────────────────────────────────────────────

def test_zuwachs_gegen_verlaufsdatei(tmp_path):
    """Der Zuwachs rechnet gegen den letzten Eintrag, nicht gegen heute."""
    pfad = tmp_path / "verlauf.jsonl"
    alt = {"datum": "1999-01-01", "sammlung": "nexus_hf", "gesamt": 100,
           "ohne_inhalt": 10, "unter_40": 5, "ohne_provenance": 20,
           "etiketten": 4}
    heute_zeile = {"datum": date.today().isoformat(), "sammlung": "nexus_hf",
                   "gesamt": 1, "ohne_inhalt": 1, "unter_40": 1,
                   "ohne_provenance": 1, "etiketten": 1}
    pfad.write_text(
        json.dumps(alt) + "\n" + "kaputte zeile\n" + json.dumps(heute_zeile) + "\n",
        encoding="utf-8")

    verlauf = AUFSICHT.lies_verlauf(pfad)
    assert len(verlauf) == 2  # die kaputte Zeile faellt weg
    vorher = AUFSICHT.letzter_eintrag(verlauf, "nexus_hf")
    assert vorher["gesamt"] == 100, "der Eintrag von heute darf nicht zaehlen"

    heute = {"gesamt": 130, "ohne_inhalt": 12, "unter_40": 5,
             "ohne_provenance": 20, "etiketten": 6}
    assert AUFSICHT.zuwachs(heute, vorher) == {
        "gesamt": 30, "ohne_inhalt": 2, "unter_40": 0,
        "ohne_provenance": 0, "etiketten": 2}


def test_zuwachs_ohne_vorlauf():
    """Ohne frueheren Lauf gibt es keine Vergleichszahlen."""
    heute = {"gesamt": 5, "ohne_inhalt": 0, "unter_40": 0,
             "ohne_provenance": 0, "etiketten": 0}
    assert AUFSICHT.zuwachs(heute, None) is None
    assert AUFSICHT.alarme(heute, None) == []


def test_verlauf_ein_eintrag_je_tag(tmp_path):
    """Ein zweiter Lauf am selben Tag ersetzt die Zeile, statt sie zu doppeln."""
    pfad = tmp_path / "verlauf.jsonl"
    eintrag = {"datum": date.today().isoformat(), "sammlung": "nexus_hf",
               "gesamt": 1}
    AUFSICHT.schreibe_verlauf(pfad, eintrag)
    AUFSICHT.schreibe_verlauf(pfad, dict(eintrag, gesamt=2))

    zeilen = AUFSICHT.lies_verlauf(pfad)
    assert len(zeilen) == 1
    assert zeilen[0]["gesamt"] == 2


# ── Alarm ────────────────────────────────────────────────────────────────

def test_alarm_ja_nein():
    """Genau das Doppelte bleibt ruhig, ein Sprung darueber und ein Fall nicht."""
    vorher = {"datum": "1999-01-01", "gesamt": 1000, "ohne_inhalt": 10,
              "ohne_provenance": 20, "etiketten": 30}

    ruhig = {"gesamt": 1010, "ohne_inhalt": 20, "ohne_provenance": 40,
             "etiketten": 60}
    assert AUFSICHT.alarme(ruhig, vorher) == []

    springt = dict(ruhig, ohne_inhalt=21)
    meldungen = AUFSICHT.alarme(springt, vorher)
    assert len(meldungen) == 1
    assert "ohne Inhalt" in meldungen[0]

    faellt = {"gesamt": 999, "ohne_inhalt": 10, "ohne_provenance": 20,
              "etiketten": 30}
    assert any("Gesamtzahl" in m for m in AUFSICHT.alarme(faellt, vorher))


def test_alarm_bei_sprung_aus_null():
    """Von null auf einen Wert ist ebenfalls ein Sprung."""
    vorher = {"gesamt": 100, "ohne_inhalt": 0, "ohne_provenance": 0,
              "etiketten": 0}
    heute = {"gesamt": 100, "ohne_inhalt": 1, "ohne_provenance": 0,
             "etiketten": 0}
    assert any("ohne Inhalt" in m for m in AUFSICHT.alarme(heute, vorher))


# ── Stichwortindex ───────────────────────────────────────────────────────

def test_fehlender_stichwortindex(tmp_path):
    """Fehlt die Indexdatei, entfaellt der Vergleich — der Bericht bleibt."""
    fehlt = tmp_path / "gibts-nicht.db"
    assert AUFSICHT.stichwort_zeilen(fehlt) is None

    zahlen = AUFSICHT.zaehle([_punkt(1, content="x" * 50, category="fact")])
    bericht = AUFSICHT.bericht_text(
        "2026-11-10", "nexus_hf", zahlen, None, None, None, 1, 60000)
    assert "Kein Stichwortindex gefunden" in bericht
    assert "Aufsichtsbericht Nexus" in bericht


def test_stichwortindex_zeilen_werden_gelesen(tmp_path):
    """Ein vorhandener Index liefert seine Zeilenzahl."""
    index = _lade(INDEX_MODUL, "nexus_keyword_index_aufsicht")
    pfad = tmp_path / "nexus-fts.db"
    index.build_index(
        [{"id": "a", "payload": {"content": "hallo welt"}},
         {"id": "b", "payload": {"content": "zweiter punkt"}}],
        path=pfad, collection="nexus_hf")

    assert AUFSICHT.stichwort_zeilen(pfad) == 2


# ── Lesen und Bericht ────────────────────────────────────────────────────

def test_liest_die_sammlung_in_einem_zug(monkeypatch):
    """Genau ein Scroll, ohne Seitenzeiger und ohne Vektoren."""
    aufrufe = []

    class _Antwort:
        def __init__(self, daten):
            self._daten = daten

        def read(self):
            return self._daten

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def _urlopen(anfrage, timeout=None):
        aufrufe.append(json.loads(anfrage.data.decode()))
        return _Antwort(json.dumps(
            {"result": {"points": [_punkt(1, content="x" * 50)]}}).encode())

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    punkte = AUFSICHT.lade_punkte("http://127.0.0.1:6333", "nexus_hf", 60000)

    assert len(aufrufe) == 1
    assert aufrufe[0]["limit"] == 60000
    assert aufrufe[0]["with_vector"] is False
    assert "offset" not in aufrufe[0]
    assert punkte[0]["id"] == 1


def test_unerreichbare_sammlung_endet_mit_fehler(monkeypatch, tmp_path):
    """Ohne Sammlung kein Bericht: der Lauf endet sichtbar mit Rueckgabewert 1."""
    def _urlopen(*_args, **_kwargs):
        raise OSError("Verbindung abgelehnt")

    monkeypatch.setattr("urllib.request.urlopen", _urlopen)
    code = AUFSICHT.main(["--state-dir", str(tmp_path)])

    assert code == 1
    assert not list(tmp_path.glob("bericht-*.md"))


def test_lange_schreiberliste_wird_zusammengefasst():
    """Der Namensschwanz steht als Sammelzeile im Bericht, nicht einzeln."""
    zaehler = {f"schreiber-{i}": 1 for i in range(40)}
    zaehler["gross"] = 500
    zeilen = AUFSICHT._verteilung("Schreiber", zaehler, 540, hoechstens=15)
    text = "\n".join(zeilen)

    assert "| weitere (26) | 26 |" in text
    assert "schreiber-39" not in text


def test_bericht_enthaelt_keine_inhalte():
    """Der Bericht ist weitergebbar: nur Zahlen und Kategorien."""
    punkte = [_punkt(1, content="GEHEIMER-INHALT im Klartext " * 3,
                     category="fact", source="hermes")]
    bericht = AUFSICHT.bericht_text(
        "2026-11-10", "nexus_hf", AUFSICHT.zaehle(punkte), None, None,
        100, len(punkte), 60000)

    assert "GEHEIMER" not in bericht
    assert "| fact | 1 | 100,0 % |" in bericht
    assert "Unterschied -99" in bericht
