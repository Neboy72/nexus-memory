#!/usr/bin/env python3
"""Schattenmessung fuer das Schreibtor: welche Regel faengt was, und was kostet sie?

Warum (20.11.2026): Ein Vorschlag von aussen klang gut — "Anredeform ich+du",
"Signatur mit Modellname am Ende" — und traf in der Messung 875 von 13.912
behaltenen Eintraegen. Beim Nachsehen waren das Rechnungen, Vertraege und
Behoerdenbriefe aus dem Papierarchiv: deutsche Dokumente enthalten "du" und
"ich". Ohne diese Messung waere das Tor zum Schloss fuer das eigene Archiv
geworden.

Das Werkzeug vergleicht zwei Mengen aus derselben Sammlung:

* **Muell** — zurueckgezogene Eintraege, deren Grund mit "Aufraeumen" beginnt
  (also die, die von Hand als Muell eingestuft wurden). Sie sind die Wahrheit
  fuer "haette abgewiesen werden muessen".
* **Bestand** — alles ohne Statusfeld, also die behaltenen Eintraege. Sie sind
  die Wahrheit fuer "darf nicht abgewiesen werden".

Ausgegeben wird je Regel: wie viele Muell-Eintraege sie faengt (Trefferquote)
und wie viele behaltene sie trifft (Preis der Regel). Eine Regel, die viel
faengt und wenig trifft, ist brauchbar; eine, die beides tut, nicht.

    python3 benchmarks/tor-schatten.py                 # nur die Regeln im Tor
    python3 benchmarks/tor-schatten.py --menge 300     # schneller, Stichprobe
    python3 benchmarks/tor-schatten.py --beispiele 3   # Treffer auf dem Bestand zeigen

Rein lesend: keine Schreibvorgaenge, keine Aenderung an der Sammlung.
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Tuple

REPO = Path(__file__).resolve().parents[1]
for pfad in (REPO, REPO / "src"):
    if str(pfad) not in sys.path:
        sys.path.insert(0, str(pfad))

QDRANT = "http://localhost:6333"
SAMMLUNG = "nexus_hf"

#: Ein Muell-Eintrag ist einer, den ein Mensch mit einem dieser
#: Grundanfaenge zurueckgezogen hat. Alles andere Zurueckgezogene (Widerspruch, Dublette,
#: abgeloest) zaehlt nicht als Muell — es war einmal richtig.
MUELL_GRUENDE = ("aufraeumen", "altbestand")


def _scroll(filter_must: List[Dict[str, Any]] | None, limit: int) -> List[dict]:
    import json
    import urllib.request

    punkte: List[dict] = []
    cursor = None
    while True:
        body: Dict[str, Any] = {"limit": 500, "with_payload": True, "with_vector": False}
        if filter_must:
            body["filter"] = {"must": filter_must}
        if cursor is not None:
            body["offset"] = cursor
        req = urllib.request.Request(
            f"{QDRANT}/collections/{SAMMLUNG}/points/scroll",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as antwort:
            ergebnis = json.loads(antwort.read())["result"]
        seite = ergebnis.get("points") or []
        punkte.extend(seite)
        if not seite or len(punkte) >= limit:
            return punkte[:limit]
        cursor = ergebnis.get("next_page_offset")
        if cursor is None:
            return punkte


def _text(payload: Dict[str, Any]) -> str:
    return (payload.get("content") or payload.get("text") or "").strip()


def _kategorie(payload: Dict[str, Any]) -> str:
    return str(payload.get("category") or "fact")


def _quelle(payload: Dict[str, Any]) -> str:
    prov = payload.get("provenance") or {}
    return str(prov.get("source_type") or payload.get("source") or "")


def lade_korpora(menge: int, anteil_muell: float, rng: random.Random) -> Tuple[List[Tuple[str, str, str]], List[Tuple[str, str, str]]]:
    """Liefert (muell, bestand) als Liste von (Kategorie, Quelle, Text)."""
    alle = _scroll(None, 10 ** 9)
    muell: List[Tuple[str, str, str]] = []
    bestand: List[Tuple[str, str, str]] = []
    for punkt in alle:
        payload = punkt.get("payload") or {}
        text = _text(payload)
        if not text:
            continue
        status = payload.get("lifecycle_status")
        grund = str(payload.get("supersede_reason") or "").lower()
        eintrag = (_kategorie(payload), _quelle(payload), text)
        if status == "deprecated":
            if grund.startswith(MUELL_GRUENDE):
                muell.append(eintrag)
        else:
            bestand.append(eintrag)
    if menge and len(bestand) > menge:
        bestand = rng.sample(bestand, menge)
    return muell, bestand


# ── Die Regeln, einzeln pruefbar ────────────────────────────────────
#
# Jede Regel ist absichtlich EINZELN messbar: das Tor entscheidet in seiner
# Reihenfolge, hier soll sichtbar werden, was jede Regel allein kostet.

def regeln() -> List[Tuple[str, Callable[[str, str, str], bool]]]:
    from nexus.write_gate import (
        _CONVERSATION_RE,
        _PLACEHOLDER_RE,
        _is_label,
        _lowercase_scrap,
        is_fragment,
        is_question,
        is_status_snippet,
    )

    return [
        ("Frage statt Aussage", lambda k, q, t: k != "session" and is_question(t, k)),
        ("Gespraechsrest (Rollenmarke)", lambda k, q, t: k != "session" and bool(_CONVERSATION_RE.search(t))),
        ("Zustandsmeldung", lambda k, q, t: k != "temp" and is_status_snippet(t)),
        ("Platzhalter aus dem Kontext", lambda k, q, t: bool(_PLACEHOLDER_RE.search(t))),
        ("Gruss/Zustimmung", lambda k, q, t: k != "session" and is_fragment(t, strict=k in ("rule", "preference"))),
        ("Kleingeschriebener Satz (rule)", lambda k, q, t: _lowercase_scrap(t, k)),
        ("Etikett statt Aussage", lambda k, q, t: k == "fact" and _is_label(t)),
    ]


# ── Zur Diskussion stehende Regeln, NICHT im Tor ────────────────────
#
# Diese sind Vorschlaege von aussen (20.11.2026) und waren in der Messung
# untragbar. Sie bleiben hier stehen, damit niemand sie spaeter ungemessen
# einbaut.

def vorschlaege() -> List[Tuple[str, Callable[[str, str, str], bool]]]:
    import re

    # guard-ok: Messmuster fuer einen Fremdvorschlag "Signatur mit Modellname".
    # Es wird nichts aufgerufen und nichts konfiguriert, nur Text erkannt.
    modell = re.compile(r"\b(deepseek-v[\d.]+|kimi-k[\d.]+|glm-[\d.]+|minimax-m[\d.]+)\b", re.I)  # guard-ok: Messmuster fuer einen Fremdvorschlag, es wird nichts aufgerufen
    anrede = re.compile(r"\b(du|dir|dich|dein\w*)\b", re.I)
    ich = re.compile(r"\b(ich|wir)\b", re.I)
    uhr = re.compile(r"\b\d{1,2}:\d{2}\b")
    passiv = re.compile(r"wurde[n]?\s+\w*(gestartet|beendet|getauscht|geändert|ersetzt)|ist\s+\w*\s*(ausgefallen|abgestürzt)", re.I)
    funktionswort = re.compile(r"\b(und|oder|der|die|das|mit|für|zu|dass|weil|aber|denn|wenn|als|auf|von|im|am|ist|sind|war|hat|habe|nicht|noch|auch|dann)\s*$", re.I)

    return [
        ("V: Anredeform ich+du", lambda k, q, t: k in ("fact", "entity", "belief", "procedure") and bool(anrede.search(t)) and bool(ich.search(t))),
        ("V: Signatur mit Modellname", lambda k, q, t: bool(modell.search(t))),
        ("V: Fuchs UND Modellname", lambda k, q, t: "🦊" in t and bool(modell.search(t))),
        ("V: abgeschnittenes Ende", lambda k, q, t: bool(funktionswort.search(t.strip()))),
        ("V: Uhrzeit + Vorgangspassiv", lambda k, q, t: bool(uhr.search(t)) and bool(passiv.search(t))),
    ]


def messe(name: str, regel: Callable[[str, str, str], bool],
          muell: List[Tuple[str, str, str]], bestand: List[Tuple[str, str, str]],
          beispiele: int) -> Dict[str, Any]:
    treffer = sum(1 for k, q, t in muell if regel(k, q, t))
    fehl = [(k, t) for k, q, t in bestand if regel(k, q, t)]
    return {
        "name": name,
        "muell": treffer,
        "muell_gesamt": len(muell),
        "bestand": len(fehl),
        "bestand_gesamt": len(bestand),
        "beispiele": fehl[:beispiele],
    }


def main() -> int:
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description="Schattenmessung fuer das Schreibtor")
    parser.add_argument("--menge", type=int, default=0, help="Stichprobe des Bestands (0 = alles)")
    parser.add_argument("--beispiele", type=int, default=0, help="Treffer auf dem Bestand anzeigen")
    parser.add_argument("--nur-vorschlaege", action="store_true", help="nur die nicht eingebauten Vorschlaege messen")
    args = parser.parse_args()

    rng = random.Random(20261120)
    muell, bestand = lade_korpora(args.menge, 0.2, rng)
    if not muell:
        print("Keine Muell-Eintraege gefunden (Grund beginnt nicht mit 'Aufraeumen').")
        return 1

    print(f"Muell (Wahrheit): {len(muell)} | Bestand (darf nicht getroffen werden): {len(bestand)}")
    print()
    kopf = f"{'Regel':34s} {'faengt':>8s} {'trifft':>8s}"
    print(kopf)
    print("-" * len(kopf))

    kandidaten = vorschlaege() if args.nur_vorschlaege else regeln() + vorschlaege()
    for name, regel in kandidaten:
        m = messe(name, regel, muell, bestand, args.beispiele)
        quote = 100 * m["muell"] / max(m["muell_gesamt"], 1)
        preis = 100 * m["bestand"] / max(m["bestand_gesamt"], 1)
        print(f"{name:34s} {m['muell']:4d} ({quote:3.0f}%) {m['bestand']:6d} ({preis:4.2f}%)")
        for k, t in m["beispiele"]:
            print(f"      [{k}] {t[:110]}")

    print()
    print("Lesart: 'faengt' ist die Trefferquote auf Muell, 'trifft' der Preis auf")
    print("behaltenem Wissen. Die Vorschlaege (V:) sind NICHT im Tor.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
