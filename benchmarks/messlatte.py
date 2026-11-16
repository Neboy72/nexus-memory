"""Messlatte fuer den Abruf: viele Fragen statt einer (17.11.2026).

Eine einzelne Abfrage kann immer zufaellig stimmen. Diese Messung zieht eine
Stichprobe aus dem Bestand, baut daraus vier Arten von Fragen und prueft fuer
jede, auf welchem Platz der Eintrag zurueckkommt, aus dem die Frage stammt.

Kategorien, weil sie unterschiedlich schwer sind:
  zitat      — der Text des Eintrags, woertlich
  stichwort  — zwei bis vier Woerter aus dem Eintrag
  kennung    — eine Zahl, Adresse oder ein Punktname aus dem Eintrag
  paraphrase — eine umformulierte Frage, die inhaltlich auf den Eintrag zielt

Aufruf ueber den laufenden Dienst (der echte Weg), nicht am Dienst vorbei.
Ausgabe: JSON unter /tmp/messlatte-ergebnis.json und eine Tabelle auf stdout.
"""

import json
import os
import pathlib
import random
import re
import sys
import time
import urllib.request

QD = "http://127.0.0.1:6333"
COLLECTION = "nexus_hf"
STICHPROBE = 2000          # Rohstichprobe aus Qdrant
AUSWAHL = 120              # daraus zufaellig, damit die Messung nicht an einer Ecke haengt
RANDOM_SEED = 16
TOP_K = 10
PRO_ART = 40


def rpc_factory():
    key = open(os.path.expanduser("~/.nexus-memory/agent_keys/ds-bot.key")).read().strip()
    zustand = {"sid": None}

    def rpc(method, params):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "x-nexus-service-key": key,
        }
        if zustand["sid"]:
            headers["Mcp-Session-Id"] = zustand["sid"]
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
        req = urllib.request.Request("http://127.0.0.1:9122/mcp", data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=180) as antwort:
            zustand["sid"] = antwort.headers.get("Mcp-Session-Id") or zustand["sid"]
            roh = antwort.read().decode()
        zeilen = [z[5:] for z in roh.splitlines() if z.startswith("data: ")]
        return json.loads(zeilen[-1]) if zeilen else json.loads(roh)

    return rpc


def stichprobe():
    """Eintraege aus dem Suchkorpus des Retrievers — genau die, die auffindbar sind.

    Aus Qdrant zu ziehen war der falsche Weg: Dort heisst das Textfeld meist
    ``content``, und in der Sammlung liegen auch gescannte Dokumente
    (Rechnungen, Arztbriefe) mit ``doc_id``. Die kommen hier nicht vor — der
    Korpus enthaelt nur, was der Dienst selbst durchsucht, und Punkte mit
    ``doc_id`` werden ausdruecklich ausgeschlossen, damit keine
    Dokumentinhalte in Messung oder Bericht auftauchen.
    """
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from nexus.retrieval import HybridRetriever

    retriever = HybridRetriever(collection_name=COLLECTION)
    if retriever._bm25 is None:
        retriever.index_memories()
    # Zurueckgezogene Eintraege ausschliessen: Der Abruf blendet sie absichtlich
    # aus, also sind sie kein Suchfehler. Ohne diesen Filter zaehlt die Messung
    # Fehler, die keine sind (erster Lauf: 8 von 40 Zitaten "nicht dabei").
    ausgeschlossen = retriever._suppressed_ids
    return [{"id": pid, "text": txt}
            for pid, txt in zip(retriever._ids, retriever._texts)
            if str(pid) not in ausgeschlossen]


def brauchbar(punkt):
    kennung = str(punkt.get("id") or "")
    text = (punkt.get("text") or "").strip()
    if len(text) < 40 or len(text) > 300:
        return False
    if text.startswith("[") or text.startswith("User: [IMPORTANT"):
        return False
    if "::" in kennung:  # zusammengefasste Sitzungsfenster, nicht der Originaleintrag
        return False
    return True


# Nur echte Kennungen: Adresse mit Port, Hex-Gruppe ab acht Zeichen, Domainname,
# Pfad mit Schraegstrich. Jahreszahlen und Versionsnummern sind KEINE Kennungen —
# "2026" steht in tausenden Eintraegen und sagt nichts ueber den gesuchten aus.
KENNUNG = re.compile(
    r"\b(?:\d{1,3}\.){3}\d{1,3}:\d+\b"      # IP mit Port
    r"|\b[0-9a-f]{8,}\b"                      # Hex-Gruppe
    r"|\b[a-z0-9-]+(?:\.[a-z0-9-]+){1,}\.[a-z]{2,}\b"   # Domain
    r"|\b(?:/[\w.-]+){2,}"                    # Pfad
)


def fragen_aus(punkt):
    """Baut bis zu vier Fragen aus einem Eintrag — nur wo es sinnvoll ist."""
    text = (punkt.get("text") or "").strip()
    fragen = []

    if len(text) <= 160:
        fragen.append(("zitat", text))

    woerter = [w.strip(",.;:!?()\"'") for w in text.split()]
    woerter = [w for w in woerter if len(w) > 3]
    if len(woerter) >= 4:
        fragen.append(("stichwort", " ".join(woerter[1:4])))

    treffer = KENNUNG.search(text)
    if treffer:
        fragen.append(("kennung", treffer.group(0)))

    if len(woerter) >= 5:
        fragen.append(("paraphrase", "was steht zu " + " ".join(woerter[:3]) + " im bestand"))

    return fragen


def nur_sichtbare(faelle):
    """Faelle behalten, deren Zieleintrag ein public-Aufrufer sehen darf."""
    ids = [str(f.get("ziel") or f.get("id")) for f in faelle]
    if not ids:
        return faelle
    try:
        body = {"ids": ids, "with_payload": True, "with_vector": False}
        req = urllib.request.Request(f"{QD}/collections/{COLLECTION}/points",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=60) as antwort:
            punkte = json.loads(antwort.read())["result"]
    except Exception as fehler:  # noqa: BLE001 — ohne Payloads lieber ungefiltert messen
        print(f"  WARNUNG: Sichtbarkeit nicht pruefbar ({fehler}) — messe ungefiltert.")
        return faelle

    payloads = {str(p["id"]): (p.get("payload") or {}) for p in punkte}
    behalten, verworfen = [], {"privat": 0, "zurueckgezogen": 0, "fremder Bereich": 0, "fehlt": 0}
    for fall in faelle:
        pl = payloads.get(str(fall.get("ziel") or fall.get("id")), {})
        if not pl:
            verworfen["fehlt"] += 1
            continue
        if pl.get("lifecycle_status") in ("deprecated", "rolled_back"):
            verworfen["zurueckgezogen"] += 1
            continue
        if (pl.get("access_level") or "private") != "public":
            verworfen["privat"] += 1
            continue
        if (pl.get("scope") or "default") != "default":
            verworfen["fremder Bereich"] += 1
            continue
        behalten.append(fall)
    if any(verworfen.values()):
        print(f"  Ausgeschlossen (fuer diesen Aufruf unsichtbar): "
              f"{', '.join(f'{k}={v}' for k, v in verworfen.items() if v)}")
    return behalten


def main():
    rpc = rpc_factory()
    rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "messlatte", "version": "1"}})

    # Die Faelle werden EINMAL gezogen und dann festgehalten (17.11.2026).
    # Vorher zog jeder Lauf eine neue Stichprobe — die Zahl der Kennungs-Faelle
    # schwankte zwischen 8 und 36, und zwei Laeufe waren nicht vergleichbar.
    # Mit fester Liste ist jeder Eingriff paarweise messbar.
    fall_datei = pathlib.Path(__file__).resolve().parent / "messlatte-faelle.json"
    fremd = next((a for a in sys.argv[1:] if a.endswith(".json")), None)
    if fremd:
        auswahl = json.loads(pathlib.Path(fremd).read_text())
        print(f"Fremde Fallliste: {fremd} ({len(auswahl)})")
    elif fall_datei.exists():
        auswahl = json.loads(fall_datei.read_text())
        print(f"Faelle aus {fall_datei.name}: {len(auswahl)}")
    else:
        punkte = [p for p in stichprobe() if brauchbar(p)]
        random.Random(RANDOM_SEED).shuffle(punkte)
        punkte = punkte[:AUSWAHL]

        faelle = []
        for punkt in punkte:
            for art, frage in fragen_aus(punkt):
                faelle.append({"art": art, "frage": frage, "ziel": str(punkt["id"])})

        pro_art = {}
        for fall in faelle:
            pro_art.setdefault(fall["art"], []).append(fall)
        auswahl = []
        for art in sorted(pro_art):
            auswahl.extend(pro_art[art][:PRO_ART])
        fall_datei.write_text(json.dumps(auswahl, indent=2, ensure_ascii=False))
        print(f"Faelle gezogen und festgehalten: {fall_datei.name} ({len(auswahl)})")

    for fall in auswahl:
        fall.setdefault("art", "paraphrase")

    # Sichtbarkeit pruefen (17.11.2026): Ein Fall, dessen Zieleintrag der Aufrufer
    # nicht sehen darf, ist kein Suchfehler — er wird ausgeblendet, weil er
    # privat, zurueckgezogen oder aus einem fremden Geltungsbereich ist. Ohne
    # diesen Schritt misst die Messlatte die Filter statt der Suche.
    auswahl = nur_sichtbare(auswahl)
    verteilung = {}
    for fall in auswahl:
        verteilung[fall["art"]] = verteilung.get(fall["art"], 0) + 1
    print(f"Fragen: {len(auswahl)} "
          f"({', '.join(f'{a}={n}' for a, n in sorted(verteilung.items()))})")

    ergebnisse = []
    start = time.time()
    for i, fall in enumerate(auswahl, 1):
        antwort = rpc("tools/call", {"name": "recall",
                                     "arguments": {"query": fall["frage"], "limit": TOP_K}})
        treffer = json.loads(antwort["result"]["content"][0]["text"])["results"]
        ids = [str(m["id"]) for m in treffer]
        ziel = fall.get("ziel") or fall.get("id")
        rang = next((n for n, kennung in enumerate(ids, 1) if kennung == ziel), None)
        ergebnisse.append({**fall, "rang": rang, "anzahl": len(ids)})
        if i % 20 == 0:
            print(f"  ... {i}/{len(auswahl)} ({time.time()-start:.0f}s)")

    print()
    print(f"{'Art':12s} {'Faelle':>6s} {'Platz 1':>8s} {'Top 5':>7s} {'nicht dabei':>12s}")
    je_art = {}
    for art in sorted({e["art"] for e in ergebnisse}):
        teil = [e for e in ergebnisse if e["art"] == art]
        p1 = sum(1 for e in teil if e["rang"] == 1)
        p5 = sum(1 for e in teil if e["rang"] and e["rang"] <= 5)
        fehlt = sum(1 for e in teil if e["rang"] is None)
        je_art[art] = {"faelle": len(teil), "platz1": p1, "top5": p5, "fehlt": fehlt}
        print(f"{art:12s} {len(teil):6d} {p1:8d} {p5:7d} {fehlt:12d}")

    p1 = sum(1 for e in ergebnisse if e["rang"] == 1)
    p5 = sum(1 for e in ergebnisse if e["rang"] and e["rang"] <= 5)
    fehlt = sum(1 for e in ergebnisse if e["rang"] is None)
    print(f"{'GESAMT':12s} {len(ergebnisse):6d} {p1:8d} {p5:7d} {fehlt:12d}")
    print(f"Laufzeit {time.time()-start:.0f}s")

    with open("/tmp/messlatte-ergebnis.json", "w") as f:
        json.dump({"gesamt": {"faelle": len(ergebnisse), "platz1": p1, "top5": p5, "fehlt": fehlt},
                   "je_art": je_art, "ergebnisse": ergebnisse}, f, indent=2, ensure_ascii=False)
    print("Details: /tmp/messlatte-ergebnis.json")

    schlecht = [e for e in ergebnisse if e["rang"] is None or e["rang"] > 5]
    if schlecht:
        print(f"\nAuffaellig ({len(schlecht)}):")
        for e in schlecht[:15]:
            print(f"  [{e['art']}] {e['frage'][:52]!r} -> {e['rang']}")


if __name__ == "__main__":
    sys.exit(main())
