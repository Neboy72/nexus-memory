"""Trockenlauf: Diktat-Erkennung ueber alle gespeicherten Regeln (20.11.2026).

Was hier passiert: NICHTS wird geschrieben. Der Lauf liest die Eintraege der
Kategorie ``rule`` aus der Sammlung, wendet die Erkennung aus
``nexus/write_gate.py`` an und legt das Ergebnis als Datei ab — die Treffer
mit Grund, dazu eine Stichprobe der NICHT erkannten Regeln, damit sich die
Fehlalarme am echten Bestand ansehen lassen.

Aufruf (im Klon):

    ./.venv/bin/python benchmarks/diktat_trockenlauf.py \\
        --aus "/Users/miosha/DeepSeek Bot/work/regel-trockenlauf.json"

Die Ausgabedatei enthaelt Textanfaenge aus dem Gedaechtnis und gehoert
deshalb NICHT ins Repo (``.messdaten`` bzw. der angegebene Ort).
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HIER = Path(__file__).resolve().parent
sys.path.insert(0, str(HIER.parent / "src"))
sys.path.insert(0, str(HIER.parent))

import nexus.write_gate as write_gate  # noqa: E402

QDRANT = "http://127.0.0.1:6333"
SAMMLUNG = "nexus_hf"
STICHPROBE = 100
SEED = 20261120
TEXTANFANG = 160


def _post(pfad: str, body: dict) -> dict:
    req = urllib.request.Request(
        QDRANT + pfad, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    return json.loads(urllib.request.urlopen(req, timeout=60).read())


def lade_regeln() -> list[dict]:
    """Alle Eintraege der Kategorie ``rule`` — nur lesend."""
    zeilen: list[dict] = []
    offset = None
    while True:
        body = {
            "limit": 1000,
            "with_payload": ["text", "content", "created_at", "category",
                             "source", "type"],
            "with_vector": False,
            "filter": {"must": [{"key": "category", "match": {"value": "rule"}}]},
        }
        if offset is not None:
            body["offset"] = offset
        seite = _post(f"/collections/{SAMMLUNG}/points/scroll", body)["result"]
        for punkt in seite["points"]:
            payload = punkt["payload"] or {}
            zeilen.append({
                "id": str(punkt["id"]),
                "datum": (payload.get("created_at") or "")[:10],
                "text": payload.get("content") or payload.get("text") or "",
                "quelle": payload.get("source") or "",
            })
        offset = seite.get("next_page_offset")
        if not offset:
            break
    return zeilen


def _anfang(text: str) -> str:
    return " ".join(str(text).split())[:TEXTANFANG]


def lauf(regeln: list[dict]) -> dict:
    erkannt = []
    nicht_erkannt = []
    for regel in regeln:
        punkte, gruende = write_gate.dictation_reasons(regel["text"])
        eintrag = {
            "id": regel["id"],
            "datum": regel["datum"],
            "textanfang": _anfang(regel["text"]),
            "punkte": punkte,
        }
        if punkte >= write_gate._DICTATION_SCORE_THRESHOLD:
            eintrag["warum"] = ", ".join(gruende)
            erkannt.append(eintrag)
        else:
            nicht_erkannt.append(eintrag)

    zufall = random.Random(SEED)
    stichprobe = zufall.sample(nicht_erkannt, min(STICHPROBE, len(nicht_erkannt)))
    stichprobe.sort(key=lambda e: e["datum"])

    return {
        "stand": datetime.now(timezone.utc).isoformat(),
        "quelle": f"{QDRANT}/collections/{SAMMLUNG}",
        "kategorie": "rule",
        "regeln_gesamt": len(regeln),
        "schwelle": write_gate._DICTATION_SCORE_THRESHOLD,
        "erkannt": {
            "anzahl": len(erkannt),
            "anteil": round(len(erkannt) / max(1, len(regeln)), 4),
            "eintraege": erkannt,
        },
        "nicht_erkannt_stichprobe": {
            "anzahl": len(stichprobe),
            "seed": SEED,
            "eintraege": stichprobe,
        },
        "hinweis": (
            "Trockenlauf: nichts geschrieben, nichts geaendert. Bewertung der "
            "Treffer (Fehlalarm ja/nein) steht in 'bewertung' und ist von Hand "
            "gelesen, nicht gerechnet."
        ),
    }


def vorschlag(ergebnis: dict) -> dict:
    """Die Treffer als Vorschlagsliste: id, alt, neu. Nichts wird angewendet.

    Der Vorschlag ist eine Liste zum Vorlegen, kein Auftrag: die Sammlung
    bleibt unberuehrt, bis ein Mensch zustimmt.
    """
    eintraege = []
    for treffer in ergebnis["erkannt"]["eintraege"]:
        eintraege.append({
            "id": treffer["id"],
            "alt": "rule",
            "neu": "session",
            "datum": treffer["datum"],
            "textanfang": treffer["textanfang"],
            "warum": treffer.get("warum", ""),
            "bewertung": treffer.get("bewertung", "unbewertet"),
        })
    return {
        "titel": "Vorschlag: Diktat-Reste von 'rule' auf 'session' herabstufen",
        "stand": ergebnis["stand"],
        "grundlage": {
            "schwelle": ergebnis["schwelle"],
            "regeln_geprueft": ergebnis["regeln_gesamt"],
            "treffer": len(eintraege),
            "fehlalarm": ergebnis.get("fehlalarm", {}),
        },
        "hinweis": (
            "VORSCHLAG — nichts angewendet. Die Sammlung ist unveraendert; "
            "die Herabstufung erfolgt erst nach ausdruecklichem Go."
        ),
        "eintraege": eintraege,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--aus", required=False)
    ap.add_argument("--vorschlag-aus",
                    help="aus dem Trockenlauf-Ergebnis eine Vorschlagsliste schreiben")
    ap.add_argument("--bewertung", default=argparse.SUPPRESS,
                    help="Pfad einer JSON-Datei mit {'<id>': 'fehlalarm'|'diktat'}")
    args = ap.parse_args()

    if args.vorschlag_aus:
        quelle = json.loads(Path(args.aus).read_text())
        Ziel = Path(args.vorschlag_aus)
        Ziel.parent.mkdir(parents=True, exist_ok=True)
        Ziel.write_text(json.dumps(vorschlag(quelle), ensure_ascii=False, indent=2))
        print(f"Vorschlag: {len(quelle['erkannt']['eintraege'])} Eintraege -> {Ziel}")
        return 0

    if not args.aus:
        ap.error("--aus fehlt")
    regeln = lade_regeln()
    ergebnis = lauf(regeln)
    if getattr(args, "bewertung", ""):
        urteil = json.loads(Path(args.bewertung).read_text())
        for eintrag in ergebnis["erkannt"]["eintraege"]:
            eintrag["bewertung"] = urteil.get(eintrag["id"], "unbewertet")
        # Fehlalarm = als Diktat erkannt, ist aber eine geschriebene Regel.
        bewertet = [e for e in ergebnis["erkannt"]["eintraege"]
                    if e.get("bewertung") in ("diktat", "fehlalarm")]
        fehlalarm = sum(1 for e in bewertet if e["bewertung"] == "fehlalarm")
        ergebnis["fehlalarm"] = {
            "bewertete_treffer": len(bewertet),
            "fehlalarm": fehlalarm,
            "quote_der_treffer": round(fehlalarm / max(1, len(bewertet)), 4),
            "anteil_aller_regeln": round(fehlalarm / max(1, len(regeln)), 4),
            "gelesen_von": "Hand (Treffer einzeln gelesen)",
        }

    Ziel = Path(args.aus)
    Ziel.parent.mkdir(parents=True, exist_ok=True)
    Ziel.write_text(json.dumps(ergebnis, ensure_ascii=False, indent=2))
    print(f"Regeln: {len(regeln)}  Treffer: {ergebnis['erkannt']['anzahl']} "
          f"({ergebnis['erkannt']['anteil']:.2%})  Stichprobe: "
          f"{ergebnis['nicht_erkannt_stichprobe']['anzahl']}")
    print(f"geschrieben: {Ziel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
