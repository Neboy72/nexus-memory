"""Paraphrase-Fragen bauen: ein Modell formuliert, nicht ich (17.11.2026).

Der Befund vom 16.11. sagt: Zitate treffen zu 92 Prozent, Paraphrasen nur zu 25.
Eine Aenderung an der Bedeutungssuche ohne belastbare Paraphrase-Fragen waere
geraten — die automatischen Fragen der Messlatte ("was steht zu X im bestand")
taugen dafuer nicht, weil sie den Wortlaut wiederholen.

Dieses Skript nimmt Eintraege aus dem Suchkorpus und laesst ein lokales Modell
je eine Frage formulieren, die dasselbe meint, aber andere Woerter benutzt. Die
Fragen werden mit dem Ziel-Eintrag in eine Datei geschrieben und sind damit vor
und nach jedem Eingriff dieselben.

Aufruf:
    env -u PYTHONPATH .venv/bin/python benchmarks/fragen_bauen.py [ANZAHL]

Ergebnis: benchmarks/paraphrase-faelle.json
"""

import json
import os
import pathlib
import random
import re
import sys
import urllib.request

OLLAMA = "http://localhost:11434/api/chat"


def modell_aus_konfiguration():
    """Modellname aus der Konfiguration des Nutzers — kein Standard im Code.

    Reihenfolge: Umgebungsvariable MODELL, dann der Hauptmodell-Eintrag in
    ~/.hermes/config.yaml. Ist beides leer, wird NICHTS aufgerufen und das
    Skript sagt es laut (Regel: kein fest verdrahteter Modellname).
    """
    aus_umgebung = os.environ.get("MODELL", "").strip()
    if aus_umgebung:
        return aus_umgebung
    config = pathlib.Path.home() / ".hermes" / "config.yaml"
    if config.exists():
        for zeile in config.read_text().splitlines():
            if zeile.startswith("  model:") or zeile.startswith("model:"):
                wert = zeile.split(":", 1)[1].strip().strip("'\"")
                if wert:
                    return wert
    raise SystemExit(
        "Kein Modell gesetzt. Entweder MODELL=<name> in die Umgebung, "
        "oder ein Hauptmodell in ~/.hermes/config.yaml. Es wird nichts aufgerufen."
    )


MODELL = modell_aus_konfiguration()
COLLECTION = "nexus_hf"
ANZAHL = int(sys.argv[1]) if len(sys.argv) > 1 else 60
ZIEL = pathlib.Path(__file__).resolve().parent / "paraphrase-faelle.json"
SEED = 23

SYSTEM = (
    "Du formulierst Suchfragen fuer einen Wissensspeicher. Aus einem Eintrag baust du EINE "
    "Frage, die dasselbe wissen will. Regeln: hoechstens 12 Woerter, keine Woerter aus dem "
    "Eintrag woertlich uebernehmen, ausser Eigennamen und Kennungen. Kein Praefix wie "
    "'was steht zu'. Antworte nur mit der Frage, ohne Anfuehrungszeichen."
)


def korpus():
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from nexus.retrieval import HybridRetriever

    r = HybridRetriever(collection_name=COLLECTION)
    if r._bm25 is None:
        r.index_memories()
    ausgeschlossen = r._suppressed_ids
    return [{"id": pid, "text": txt} for pid, txt in zip(r._ids, r._texts)
            if str(pid) not in ausgeschlossen]


def brauchbar(eintrag):
    text = eintrag["text"]
    if len(text) < 60 or len(text) > 400:
        return False
    if text.startswith("[") or text.startswith("User: [IMPORTANT"):
        return False
    if "::" in str(eintrag["id"]):
        return False
    inhalt = re.findall(r"\w{4,}", text)
    return len(inhalt) >= 4


def frage_bauen(text):
    body = {
        "model": MODELL,
        "stream": False,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"Eintrag:\n{text[:400]}\n\nDeine Frage:"},
        ],
    }
    req = urllib.request.Request(OLLAMA, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=180) as antwort:
        daten = json.loads(antwort.read())
    frage = (daten.get("message", {}).get("content") or "").strip().strip('"').strip()
    return frage.splitlines()[0] if frage else ""


def woerter(text):
    return {w.lower() for w in re.findall(r"\w{4,}", text)}


def main():
    eintraege = [e for e in korpus() if brauchbar(e)]
    random.Random(SEED).shuffle(eintraege)
    auswahl = eintraege[:ANZAHL]
    print(f"Korpus brauchbar: {len(eintraege)} | nehme {len(auswahl)} | Modell {MODELL}")

    faelle = []
    for i, eintrag in enumerate(auswahl, 1):
        try:
            frage = frage_bauen(eintrag["text"])
        except Exception as fehler:
            print(f"  {i}: Fehler {fehler}")
            continue
        if not frage:
            continue
        # Nachweis, dass es eine Umformulierung ist und kein Zitat mit Fragezeichen
        gemeinsam = len(woerter(frage) & woerter(eintrag["text"]))
        anteil = gemeinsam / max(1, len(woerter(frage)))
        faelle.append({
            "id": eintrag["id"],
            "frage": frage,
            "wortgleich": round(anteil, 2),
            "text": eintrag["text"][:200],
        })
        if i % 10 == 0:
            print(f"  {i}/{len(auswahl)} | letzte: {frage[:60]!r} ({anteil:.0%} wortgleich)")

    ZIEL.write_text(json.dumps(faelle, indent=2, ensure_ascii=False))
    echte = [f for f in faelle if f["wortgleich"] < 0.5]
    print(f"\n{len(faelle)} Fragen geschrieben, davon {len(echte)} mit weniger als "
          f"50 Prozent wortgleich: {ZIEL}")


if __name__ == "__main__":
    sys.exit(main())
