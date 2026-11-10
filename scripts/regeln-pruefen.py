#!/usr/bin/env python3
"""Bestandsdurchsicht der Regeln — nur lesend.

Warum: Bis 10.11.2026 genuegten ``source_type`` und ``explicit`` als
Selbstauskunft des Modells, damit ein Text als Regel gespeichert wurde. Ein
Dokument, das "merke dir als Regel ..." enthaelt, kam so als Dauerregel in den
Bestand; die Zitatmerkmale greifen nicht, sobald das Modell umformuliert.
Seitdem braucht eine Regel einen menschlichen Nachweis (``rule-confirm``).
Diese Durchsicht zeigt, was vorher entstanden ist.

Markiert wird eine Regel, wenn sie nicht aus einer Handeingabe stammt
(``source_type != "manual"``) oder wenn sie Zitatmerkmale traegt. In den
Bericht kommen nur Punktkennung, Quelle und die ersten 60 Zeichen — keine
vollen Inhalte. Auf die Sammlung findet kein Schreibzugriff statt.

Aufruf:
    python3 scripts/regeln-pruefen.py
    python3 scripts/regeln-pruefen.py --sammlung nexus_hf --qdrant http://127.0.0.1:6333

Konsole: eine Tabelle je Regel und eine Summenzeile.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from nexus.write_gate import looks_document_quoted  # noqa: E402

QDRANT_URL = os.environ.get("NEXUS_QDRANT_URL", "http://127.0.0.1:6333")
SAMMLUNG = os.environ.get("NEXUS_COLLECTION", "nexus")
LESE_LIMIT = 60000          # ein Lesezugriff, kein Blaettern
ANFANG_ZEICHEN = 60         # so viel Inhalt steht im Bericht


def lade_punkte(qdrant_url: str = QDRANT_URL, sammlung: str = SAMMLUNG,
                limit: int = LESE_LIMIT) -> List[Dict[str, Any]]:
    """Liest die Sammlung in einem Zug; gibt die Punktliste zurueck.

    Wie in ``nexus-aufsicht.py`` bewusst ohne Seitenzeiger: die Sammlung traegt
    gemischte Kennungen, und der Zeiger laeuft dabei im Kreis.
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
    pl = punkt.get("payload") if isinstance(punkt, dict) else None
    return pl if isinstance(pl, dict) else {}


def ist_regel(punkt: Dict[str, Any]) -> bool:
    return str(_payload(punkt).get("category") or "").strip().lower() == "rule"


def quelle(punkt: Dict[str, Any]) -> str:
    """Herkunftsart: ``provenance.source_type``, sonst ``source_type``/``source``."""
    pl = _payload(punkt)
    herkunft = pl.get("provenance")
    if isinstance(herkunft, dict):
        art = str(herkunft.get("source_type") or "").strip()
        if art:
            return art
    for feld in ("source_type", "source"):
        wert = str(pl.get(feld) or "").strip()
        if wert:
            return wert
    return "ohne Angabe"


def menschlich_bestaetigt(punkt: Dict[str, Any]) -> bool:
    """Neuere Regeln tragen den Nachweis in der Herkunft."""
    herkunft = _payload(punkt).get("provenance")
    return isinstance(herkunft, dict) and herkunft.get("rule_confirmed") is True


def anfang(text: str, laenge: int = ANFANG_ZEICHEN) -> str:
    """Erste Zeichen des Inhalts, einzeilig und ohne Spaltenbrecher."""
    einzeilig = " ".join(str(text or "").split())
    return einzeilig[:laenge] + ("…" if len(einzeilig) > laenge else "")


def markierung(punkt: Dict[str, Any]) -> str:
    """Was an dieser Regel auffaellig ist; leer, wenn nichts."""
    gruende: List[str] = []
    if quelle(punkt) != "manual":
        gruende.append("keine Handeingabe")
    if looks_document_quoted(str(_payload(punkt).get("content") or "")):
        gruende.append("Zitatmerkmale")
    return ", ".join(gruende)


def zeilen(punkte: List[Dict[str, Any]]) -> Tuple[List[str], Dict[str, int]]:
    """Tabellenzeilen und Summen fuer alle Punkte mit Kategorie ``rule``."""
    regeln = [p for p in punkte if ist_regel(p)]
    kopf = ("| Punktkennung                         | Quelle          "
            "| Anfang (60 Zeichen)                                          | Markierung |")
    zeilen = [kopf, "|" + "-" * 38 + "|" + "-" * 17 + "|" + "-" * 62 + "|" + "-" * 12 + "|"]
    zahlen = {"gesamt": len(regeln), "markiert": 0, "handeingabe": 0,
              "zitat": 0, "bestaetigt": 0}
    for punkt in regeln:
        kennung = str(punkt.get("id") or "")
        herkunft = quelle(punkt)
        text = str(_payload(punkt).get("content") or "")
        marke = markierung(punkt)
        if marke:
            zahlen["markiert"] += 1
        if herkunft != "manual":
            zahlen["handeingabe"] += 1
        if looks_document_quoted(text):
            zahlen["zitat"] += 1
        if menschlich_bestaetigt(punkt):
            zahlen["bestaetigt"] += 1
        zeilen.append(
            f"| {kennung:<36} | {herkunft:<15} | {anfang(text):<60} | {marke:<10} |")
    return zeilen, zahlen


def summenzeile(zahlen: Dict[str, int]) -> str:
    return (f"Gesamt: {zahlen['gesamt']} Regeln, markiert: {zahlen['markiert']} "
            f"(keine Handeingabe: {zahlen['handeingabe']}, "
            f"Zitatmerkmale: {zahlen['zitat']}) — "
            f"davon menschlich bestaetigt: {zahlen['bestaetigt']}")


def main(argv: Optional[List[str]] = None) -> int:
    zerleger = argparse.ArgumentParser(
        description="Listet alle Regeln der Sammlung und markiert verdaechtige "
                    "(nur lesend).")
    zerleger.add_argument("--qdrant", default=QDRANT_URL)
    zerleger.add_argument("--sammlung", default=SAMMLUNG)
    zerleger.add_argument("--limit", type=int, default=LESE_LIMIT)
    args = zerleger.parse_args(argv)

    try:
        punkte = lade_punkte(args.qdrant, args.sammlung, args.limit)
    except Exception as fehler:
        print(f"Qdrant nicht lesbar ({args.qdrant}): {fehler}", file=sys.stderr)
        return 1

    tabelle, zahlen = zeilen(punkte)
    for zeile in tabelle:
        print(zeile)
    print()
    print(summenzeile(zahlen))
    return 0


if __name__ == "__main__":
    sys.exit(main())
