#!/usr/bin/env python3
"""Taeglicher Aufsichtsbericht ueber die Nexus-Sammlung — nur lesend.

Warum: Zwei Fehler lagen monatelang unbemerkt im Bestand — Entity-Punkte, die
nur aus einem Etikett bestanden, und Tausende Punkte ganz ohne Inhalt. Ein
taeglicher Blick auf die Zahlen zeigt solche Kipppunkte am Tag ihres
Entstehens statt Monate spaeter.

Die Sammlung wird genau einmal gelesen: ein Scroll mit hohem Limit. Seitenweises
Blaettern scheidet aus, weil die Sammlung gemischte Kennungen traegt und der
Seitenzeiger dabei im Kreis laeuft.

Geschrieben wird ausschliesslich unter ``$HERMES_HOME/state/nexus-aufsicht/``:
der Bericht als Markdown und eine Zeile je Tag in ``verlauf.jsonl``. In den
Bericht kommen nur Zahlen und Kategorien, keine Inhalte — er muss weitergebbar
sein. Auf die Sammlung findet kein Schreibzugriff statt.

Aufruf:
    python3 scripts/nexus-aufsicht.py
    python3 scripts/nexus-aufsicht.py --sammlung nexus_hf --state-dir /tmp/probe

Konsole: eine Zusammenfassungszeile und ``ruhig``; springt eine Zahl
gegenueber dem letzten Lauf auffaellig, stattdessen ``ALARM: …``-Zeilen.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import urllib.request
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

REPO = Path(__file__).resolve().parent.parent

# Vorgaben; alles per Umgebung oder Aufruf ueberschreibbar.
QDRANT_URL = os.environ.get("NEXUS_QDRANT_URL", "http://127.0.0.1:6333")
SAMMLUNG = os.environ.get("NEXUS_COLLECTION", "nexus_hf")
LESE_LIMIT = 60000          # ein Lesezugriff, kein Blaettern
MIN_ZEICHEN = 40            # darunter gilt ein Inhalt als zu duenn
ALARM_FAKTOR = 2.0          # mehr als das Doppelte gegenueber dem letzten Lauf
SCHREIBER_ZEILEN = 15       # danach eine Sammelzeile statt Namensschwanz

# Kennzahlen, bei denen ein Sprung nach oben Alarm ausloest.
WACH_FELDER = ("ohne_inhalt", "ohne_provenance", "etiketten")
FELD_TITEL = {
    "gesamt": "Punkte gesamt",
    "ohne_inhalt": "ohne Inhalt",
    "unter_40": f"Inhalt unter {MIN_ZEICHEN} Zeichen",
    "ohne_provenance": "ohne Herkunft (provenance)",
    "etiketten": "Etikett ohne Satz",
}

# Etikett: kurzes Typwort, Doppelpunkt, Name — optional mit Klammerattributen.
# Beispiele: "organization: frame.io", "service: Prod 17494 (port=17494)".
_ETIKETT_RE = re.compile(r"^([a-zäöüß_]{2,20}):\s*(\S.*)$")
_KLAMMER_RE = re.compile(r"\([^()]*\)")
# Trennstriche, ab denen hinter dem Etikett ein Satz beginnt.
_SATZ_STRICHE = ("—", "–", " - ")
# So viele Woerter duerfen hinter dem Etikett stehen, ohne dass es ein Satz ist.
_MAX_ETIKETT_WORTE = 3


# ── Lesen ────────────────────────────────────────────────────────────────

def lade_punkte(qdrant_url: str = QDRANT_URL, sammlung: str = SAMMLUNG,
                limit: int = LESE_LIMIT) -> List[Dict[str, Any]]:
    """Liest die Sammlung in einem Zug; gibt die Punktliste zurueck.

    Bewusst ohne Seitenzeiger: die Sammlung traegt gemischte Kennungen, und
    der Zeiger laeuft dabei im Kreis. ``with_vector`` bleibt aus, damit nur
    die Nutzdaten uebertragen werden.
    """
    rumpf = json.dumps({"limit": int(limit), "with_payload": True,
                        "with_vector": False}).encode()
    anfrage = urllib.request.Request(
        f"{qdrant_url.rstrip('/')}/collections/{sammlung}/points/scroll",
        data=rumpf, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(anfrage, timeout=300) as antwort:
        daten = json.loads(antwort.read())
    return list((daten.get("result") or {}).get("points") or [])


def _payload(punkt: Dict[str, Any]) -> Dict[str, Any]:
    """Nutzdaten eines Punktes, nie None."""
    pl = punkt.get("payload") if isinstance(punkt, dict) else None
    return pl if isinstance(pl, dict) else {}


def inhalt(punkt: Dict[str, Any]) -> str:
    """Inhalt eines Punktes, getrimmt; leerer Text, wenn er keinen hat."""
    return str(_payload(punkt).get("content") or "").strip()


def schreiber(punkt: Dict[str, Any]) -> str:
    """Wer den Punkt geschrieben hat: ``provenance.created_by`` oder ``source``."""
    pl = _payload(punkt)
    herkunft = pl.get("provenance")
    if isinstance(herkunft, dict):
        wer = str(herkunft.get("created_by") or "").strip()
        if wer:
            return wer
    quelle = str(pl.get("source") or "").strip()
    return quelle or "ohne Angabe"


O_ANGABE = "ohne Angabe"


def ist_etikett(text: str) -> bool:
    """Reines Etikett (``typ: Name (…)``) ohne Satz dahinter.

    Beispiele dafuer: ``organization: frame.io``,
    ``service: Prod 17494 (port=17494, status=healthy)``.
    Beispiele dagegen: ``service: Voice (port=9443) — Die Phrase …`` (Satz
    hinter dem Strich) und jeder mehrzeilige Fliesstext.
    """
    roh = str(text or "")
    if not roh.strip() or len(roh.strip().splitlines()) > 1:
        return False
    zeile = " ".join(roh.split())
    treffer = _ETIKETT_RE.match(zeile)
    if not treffer:
        return False
    rest = _KLAMMER_RE.sub(" ", treffer.group(2))
    if any(strich in rest for strich in _SATZ_STRICHE):
        return False
    return len(rest.split()) <= _MAX_ETIKETT_WORTE


# ── Zaehlen ──────────────────────────────────────────────────────────────

def zaehle(punkte: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Kennzahlen einer Punktliste; enthaelt keine Inhalte, nur Zahlen."""
    kategorien: Counter = Counter()
    zugaenge: Counter = Counter()
    schreibern: Counter = Counter()
    gesamt = ohne_inhalt = unter_40 = ohne_provenance = etiketten = 0

    for punkt in punkte:
        gesamt += 1
        pl = _payload(punkt)
        kategorien[str(pl.get("category") or O_ANGABE)] += 1
        zugaenge[str(pl.get("access_level") or O_ANGABE)] += 1
        schreibern[schreiber(punkt)] += 1

        text = inhalt(punkt)
        if not text:
            ohne_inhalt += 1
        elif len(text) < MIN_ZEICHEN:
            unter_40 += 1

        herkunft = pl.get("provenance")
        if not isinstance(herkunft, dict) or not herkunft:
            ohne_provenance += 1

        if text and ist_etikett(text):
            etiketten += 1

    return {
        "gesamt": gesamt,
        "kategorien": dict(kategorien),
        "zugriff": dict(zugaenge),
        "schreiber": dict(schreibern),
        "ohne_inhalt": ohne_inhalt,
        "unter_40": unter_40,
        "ohne_provenance": ohne_provenance,
        "etiketten": etiketten,
    }


# ── Verlauf ──────────────────────────────────────────────────────────────

def lies_verlauf(pfad: Path) -> List[Dict[str, Any]]:
    """Verlaufszeilen; kaputte Zeilen werden uebersprungen."""
    try:
        roh = Path(pfad).read_text(encoding="utf-8")
    except OSError:
        return []
    eintraege: List[Dict[str, Any]] = []
    for zeile in roh.splitlines():
        zeile = zeile.strip()
        if not zeile:
            continue
        try:
            eintrag = json.loads(zeile)
        except ValueError:
            continue
        if isinstance(eintrag, dict):
            eintraege.append(eintrag)
    return eintraege


def letzter_eintrag(verlauf: List[Dict[str, Any]],
                    sammlung: str) -> Optional[Dict[str, Any]]:
    """Juengster Eintrag derselben Sammlung (nicht der von heute)."""
    heute = date.today().isoformat()
    for eintrag in reversed(verlauf):
        if eintrag.get("sammlung") not in (None, sammlung):
            continue
        if eintrag.get("datum") != heute:
            return eintrag
    return None


def zuwachs(heute: Dict[str, Any],
            vorher: Optional[Dict[str, Any]]) -> Optional[Dict[str, int]]:
    """Differenz je Kennzahl gegenueber dem letzten Lauf; None ohne Vorlauf."""
    if not vorher:
        return None
    felder = ("gesamt", "ohne_inhalt", "unter_40", "ohne_provenance", "etiketten")
    delta: Dict[str, int] = {}
    for feld in felder:
        alt = vorher.get(feld)
        if isinstance(alt, bool) or not isinstance(alt, (int, float)):
            continue
        delta[feld] = int(heute.get(feld, 0)) - int(alt)
    return delta


def alarme(heute: Dict[str, Any], vorher: Optional[Dict[str, Any]]) -> List[str]:
    """Alarmzeilen ohne Praefix; leer heisst ruhig.

    Ausgeloest wird, wenn eine Wach-Kennzahl mehr als das Doppelte des letzten
    Laufs erreicht oder wenn die Gesamtzahl faellt. Ohne Vorlauf gibt es
    nichts zu vergleichen — dann bleibt es ruhig.
    """
    if not vorher:
        return []
    meldungen: List[str] = []
    for feld in WACH_FELDER:
        alt = vorher.get(feld)
        if isinstance(alt, bool) or not isinstance(alt, (int, float)):
            continue
        neu = int(heute.get(feld, 0))
        if neu > ALARM_FAKTOR * int(alt):
            meldungen.append(
                f"{FELD_TITEL[feld]}: {int(alt)} -> {neu} "
                f"(mehr als das Doppelte)")
    alt_gesamt = vorher.get("gesamt")
    if not isinstance(alt_gesamt, bool) and isinstance(alt_gesamt, (int, float)):
        if int(heute.get("gesamt", 0)) < int(alt_gesamt):
            meldungen.append(
                f"Gesamtzahl faellt: {int(alt_gesamt)} -> "
                f"{int(heute.get('gesamt', 0))}")
    return meldungen


def schreibe_verlauf(pfad: Path, eintrag: Dict[str, Any]) -> None:
    """Haengt den Tageseintrag an und ersetzt einen frueheren desselben Tages."""
    pfad = Path(pfad)
    pfad.parent.mkdir(parents=True, exist_ok=True)
    behalten = [
        zeile for zeile in lies_verlauf(pfad)
        if not (zeile.get("datum") == eintrag.get("datum")
                and zeile.get("sammlung") == eintrag.get("sammlung"))
    ]
    behalten.append(eintrag)
    zwischendatei = pfad.with_name(pfad.name + ".tmp")
    zwischendatei.write_text(
        "".join(json.dumps(zeile, ensure_ascii=False, sort_keys=True) + "\n"
                for zeile in behalten),
        encoding="utf-8")
    os.replace(zwischendatei, pfad)


# ── Stichwortindex ───────────────────────────────────────────────────────

def stichwort_zeilen(pfad: Optional[Path] = None) -> Optional[int]:
    """Zeilenzahl des Stichwortindex; None, wenn er fehlt oder unlesbar ist.

    Der Index wird ueber das Plugin-Modul gelesen (gleiche Ablage- und
    Leselogik wie die Einspielung). Fehlt die Datei, ist das kein Fehler:
    der Vergleich entfaellt dann.
    """
    try:
        modul_pfad = REPO / "plugins" / "memory" / "nexus" / "keyword_index.py"
        spez = importlib.util.spec_from_file_location(
            "nexus_keyword_index", modul_pfad)
        if spez is None or spez.loader is None:
            return None
        modul = importlib.util.module_from_spec(spez)
        spez.loader.exec_module(modul)
        ablage = Path(pfad) if pfad is not None else modul.default_db_path()
        if not Path(ablage).exists():
            return None
        anzahl = modul.KeywordIndex(ablage).count()
        return anzahl if isinstance(anzahl, int) and anzahl >= 0 else None
    except Exception:
        return None


# ── Bericht ──────────────────────────────────────────────────────────────

def _prozent(teil: int, ganz: int) -> str:
    """Anteil in Prozent mit deutschem Dezimalkomma."""
    if not ganz:
        return "0,0 %"
    return f"{teil * 100.0 / ganz:.1f} %".replace(".", ",")


def _verteilung(titel: str, zaehler: Dict[str, Any], gesamt: int,
                hoechstens: Optional[int] = None) -> List[str]:
    """Markdown-Abschnitt fuer eine Verteilung, groesste Gruppe zuerst.

    Mit ``hoechstens`` werden die kleinsten Gruppen zu einer Sammelzeile
    zusammengefasst: bei freien Namen (Schreiber) steht sonst ein Schwanz aus
    lauter Einzelfaellen im Bericht und verdeckt die tragenden Zahlen.
    """
    eintraege = sorted(zaehler.items(), key=lambda e: (-e[1], str(e[0])))
    rest: List[Any] = []
    if hoechstens is not None and len(eintraege) > hoechstens:
        eintraege, rest = eintraege[:hoechstens], eintraege[hoechstens:]

    zeilen = [f"## {titel}", "", f"| {titel} | Punkte | Anteil |",
              "|---|---:|---:|"]
    for name, anzahl in eintraege:
        zeilen.append(f"| {name} | {anzahl} | {_prozent(anzahl, gesamt)} |")
    if rest:
        summe = sum(anzahl for _, anzahl in rest)
        zeilen.append(f"| weitere ({len(rest)}) | {summe} | "
                      f"{_prozent(summe, gesamt)} |")
    return zeilen + [""]


def bericht_text(datum: str, sammlung: str, zahlen: Dict[str, Any],
                 vorher: Optional[Dict[str, Any]],
                 delta: Optional[Dict[str, int]],
                 index_zeilen: Optional[int],
                 gelesen: int, limit: int) -> str:
    """Bericht als Markdown — nur Zahlen und Kategorien, keine Inhalte."""
    gesamt = int(zahlen.get("gesamt", 0))
    wort = "Punkt" if gelesen == 1 else "Punkte"
    zeilen: List[str] = [
        f"# Aufsichtsbericht Nexus — {datum}",
        "",
        f"Sammlung `{sammlung}`. Ein Lesevorgang, {gelesen} {wort} gelesen, "
        f"kein Schreibzugriff.",
        "",
    ]
    if gelesen >= limit:
        zeilen += [f"Hinweis: das Limit von {limit} Punkten wurde erreicht — "
                   f"der Bericht kann unvollstaendig sein.", ""]

    zeilen += ["## Kennzahlen", "", "| Kennzahl | Punkte | Anteil |", "|---|---:|---:|"]
    for feld in ("gesamt", "ohne_inhalt", "unter_40", "ohne_provenance", "etiketten"):
        wert = int(zahlen.get(feld, 0))
        zeilen.append(f"| {FELD_TITEL[feld]} | {wert} | {_prozent(wert, gesamt)} |")
    zeilen.append("")

    zeilen += _verteilung("Kategorie", zahlen.get("kategorien") or {}, gesamt)
    zeilen += _verteilung("Zugriffsstufe", zahlen.get("zugriff") or {}, gesamt)
    zeilen += _verteilung("Schreiber", zahlen.get("schreiber") or {}, gesamt,
                          hoechstens=SCHREIBER_ZEILEN)

    zeilen += ["## Zuwachs seit dem letzten Lauf", ""]
    if vorher is None:
        zeilen += ["Kein frueherer Lauf im Verlauf — keine Vergleichszahlen.", ""]
    else:
        zeilen += [f"Letzter Lauf: {vorher.get('datum', 'unbekannt')}.", "",
                   "| Kennzahl | letzter Lauf | heute | Zuwachs |",
                   "|---|---:|---:|---:|"]
        for feld in ("gesamt", "ohne_inhalt", "unter_40", "ohne_provenance",
                     "etiketten"):
            alt = vorher.get(feld)
            if not isinstance(alt, (int, float)) or isinstance(alt, bool):
                continue
            unterschied = int((delta or {}).get(feld, 0))
            zeilen.append(f"| {FELD_TITEL[feld]} | {int(alt)} | "
                          f"{int(zahlen.get(feld, 0))} | {unterschied:+d} |")
        zeilen.append("")

    zeilen += ["## Stichwortindex", ""]
    if index_zeilen is None:
        zeilen += ["Kein Stichwortindex gefunden — Vergleich uebersprungen.", ""]
    else:
        zeilen += [f"Qdrant {gesamt} Punkte, Stichwortindex {index_zeilen} "
                   f"Zeilen, Unterschied {gesamt - index_zeilen:+d}.", ""]
    return "\n".join(zeilen)


def _state_verzeichnis() -> Path:
    """Ablage des Berichts: ``$HERMES_HOME/state/nexus-aufsicht``."""
    heim = Path(os.path.expanduser(
        os.environ.get("HERMES_HOME", "").strip() or "~/.hermes"))
    return heim / "state" / "nexus-aufsicht"


def main(argv: Optional[List[str]] = None) -> int:
    """Liest die Sammlung, schreibt Bericht und Verlaufszeile, meldet Alarm."""
    zerleger = argparse.ArgumentParser(
        description="Taeglicher Aufsichtsbericht ueber die Nexus-Sammlung.")
    zerleger.add_argument("--qdrant", default=QDRANT_URL)
    zerleger.add_argument("--sammlung", default=SAMMLUNG)
    zerleger.add_argument("--state-dir", default=None,
                          help="Zielverzeichnis (Vorgabe: $HERMES_HOME/state/nexus-aufsicht)")
    zerleger.add_argument("--limit", type=int, default=LESE_LIMIT)
    args = zerleger.parse_args(argv)

    try:
        punkte = lade_punkte(args.qdrant, args.sammlung, args.limit)
    except Exception as fehler:
        # Ohne Sammlung kein Bericht: laut scheitern statt still schweigen.
        print(f"Qdrant nicht lesbar ({args.qdrant}): {fehler}", file=sys.stderr)
        return 1
    zahlen = zaehle(punkte)

    verzeichnis = (Path(os.path.expanduser(args.state_dir)) if args.state_dir
                   else _state_verzeichnis())
    verlauf_pfad = verzeichnis / "verlauf.jsonl"
    vorher = letzter_eintrag(lies_verlauf(verlauf_pfad), args.sammlung)
    delta = zuwachs(zahlen, vorher)
    index_zeilen = stichwort_zeilen()

    heute = date.today().isoformat()
    bericht = bericht_text(heute, args.sammlung, zahlen, vorher, delta,
                           index_zeilen, len(punkte), args.limit)
    verzeichnis.mkdir(parents=True, exist_ok=True)
    bericht_pfad = verzeichnis / f"bericht-{heute}.md"
    bericht_pfad.write_text(bericht, encoding="utf-8")

    eintrag = {"datum": heute, "sammlung": args.sammlung}
    eintrag.update({feld: zahlen[feld] for feld in
                    ("gesamt", "ohne_inhalt", "unter_40", "ohne_provenance",
                     "etiketten")})
    eintrag["index_zeilen"] = index_zeilen
    schreibe_verlauf(verlauf_pfad, eintrag)

    print(f"Nexus-Aufsicht {heute}: {zahlen['gesamt']} Punkte, "
          f"ohne Inhalt {zahlen['ohne_inhalt']}, "
          f"ohne Herkunft {zahlen['ohne_provenance']}, "
          f"Etiketten {zahlen['etiketten']} — Bericht {bericht_pfad}")
    meldungen = alarme(zahlen, vorher)
    if meldungen:
        for meldung in meldungen:
            print(f"ALARM: {meldung}")
    else:
        print("ruhig")
    return 0


if __name__ == "__main__":
    sys.exit(main())
