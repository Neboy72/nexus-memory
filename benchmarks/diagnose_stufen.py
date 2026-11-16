"""Wo verliert der Abruf die Paraphrase? Rang je Stufe (17.11.2026).

Aus dem Befund: Zitate treffen zu 92 Prozent auf Platz 1, Paraphrasen nur zu 25 —
aber zwei Drittel liegen in den Top 5. Das ist ein Sortierproblem, kein
Findeproblem. Bevor daran etwas gebaut wird, muss klar sein, WELCHE Stufe den
Eintrag nach hinten schiebt.

Dieses Skript nimmt bekannte Faelle (Frage + Ziel-Eintrag) und zeigt den Rang des
Ziels auf jeder Stufe der Kette:

    BM25 · Vektor · nach RRF · nach Tier-Boost · nach Zeitverfall
    · nach Abdeckungsregel · nach der Sortierung des Dienstes

Die letzte Stufe ist die, die der Dienst nach der Suche anwendet: sortiert nach
Nutzungswert, mit dem Volltreffer der Abdeckungsregel zuerst.

Aufruf:
    env -u PYTHONPATH .venv/bin/python benchmarks/diagnose_stufen.py [DATEI]

DATEI ist eine JSON-Liste mit {"frage": ..., "id": ...}. Ohne Angabe wird
benchmarks/paraphrase-faelle.json genommen, sonst die Faelle aus dem letzten
Messlauf unter /tmp/messlatte-ergebnis.json.
"""

import asyncio
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from nexus.retrieval import HybridRetriever  # noqa: E402

COLLECTION = "nexus_hf"
POOL = 100


def rang(liste, ziel):
    for nummer, treffer in enumerate(liste, 1):
        if str(treffer.get("id")) == str(ziel):
            return nummer
    return None


async def main():
    quelle = sys.argv[1] if len(sys.argv) > 1 else None
    if quelle:
        faelle = json.loads(pathlib.Path(quelle).read_text())
    else:
        kandidaten = [
            pathlib.Path(__file__).resolve().parent / "paraphrase-faelle.json",
            pathlib.Path("/tmp/messlatte-ergebnis.json"),
        ]
        for weg in kandidaten:
            if weg.exists():
                daten = json.loads(weg.read_text())
                if isinstance(daten, dict):
                    faelle = [{"frage": e["frage"], "id": e["ziel"]}
                              for e in daten["ergebnisse"] if e["art"] == "paraphrase"]
                else:
                    faelle = daten
                break
        else:
            raise SystemExit("Keine Falldatei gefunden.")

    # Die Sammlung traegt einen BENANNTEN Vektor. Ohne den Namen antwortet
    # Qdrant mit 400 — der Name kommt aus der Collection-Konfiguration, nicht
    # aus einer Annahme.
    import urllib.request

    with urllib.request.urlopen(f"http://127.0.0.1:6333/collections/{COLLECTION}", timeout=20) as antwort:
        vektoren = json.loads(antwort.read())["result"]["config"]["params"]["vectors"]
    vektor_name = list(vektoren.keys())[0] if isinstance(vektoren, dict) else None

    r = HybridRetriever(collection_name=COLLECTION, vector_name=vektor_name)
    if r._bm25 is None:
        r.index_memories()
    print(f"Vektor: {vektor_name}")

    from nexus_memory.embeddings import EmbeddingProvider
    einbetter = EmbeddingProvider()

    print(f"{len(faelle)} Faelle | Pool {POOL}")
    print(f"{'Frage':38s} {'BM25':>5s} {'Vektor':>6s} {'RRF':>5s} {'Tier':>5s} {'Zeit':>5s} {'Regel':>6s} {'Dienst':>6s}")
    statistik = {"bm25": 0, "vektor": 0, "rrf": 0, "tier": 0, "zeit": 0, "regel": 0, "dienst": 0, "gesamt": 0}

    for fall in faelle[:40]:
        frage, ziel = fall["frage"], fall["id"]
        vektor = await einbetter.embed(frage)
        bm25 = r.search_bm25(frage, top_k=POOL)
        vec = r.search_vector(vektor, top_k=POOL)
        fused = r._rrf(bm25, vec)
        tier = r._tier_boost(list(fused))
        zeit = r._time_decay(list(tier))
        regel = r._apply_coverage(list(zeit), frage)

        # Die Schluss-Sortierung des Dienstes nachstellen: Nutzungswert zuerst,
        # Volltreffer der Abdeckungsregel vor allem anderen.
        dienst = sorted(
            regel,
            key=lambda x: (1 if x.get("coverage_win") else 0,
                           x.get("dynamic_score", x.get("rrf_score", 0)),
                           x.get("rrf_score", 0)),
            reverse=True,
        )

        werte = {
            "bm25": rang(bm25, ziel),
            "vektor": rang(vec, ziel),
            "rrf": rang(fused, ziel),
            "tier": rang(tier, ziel),
            "zeit": rang(zeit, ziel),
            "regel": rang(regel, ziel),
            "dienst": rang(dienst, ziel),
        }
        statistik["gesamt"] += 1
        for stufe, wert in werte.items():
            if wert == 1:
                statistik[stufe] += 1

        def zeig(wert):
            return "-" if wert is None else str(wert)

        print(f"{frage[:36]:38s} {zeig(werte['bm25']):>5s} {zeig(werte['vektor']):>6s} "
              f"{zeig(werte['rrf']):>5s} {zeig(werte['tier']):>5s} {zeig(werte['zeit']):>5s} "
              f"{zeig(werte['regel']):>6s} {zeig(werte['dienst']):>6s}")

    print()
    print("Faelle auf Platz 1 je Stufe:")
    for stufe in ("bm25", "vektor", "rrf", "tier", "zeit", "regel", "dienst"):
        print(f"  {stufe:8s} {statistik[stufe]:3d} von {statistik['gesamt']}")


if __name__ == "__main__":
    asyncio.run(main())
