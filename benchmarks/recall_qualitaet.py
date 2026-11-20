"""Messung der Auto-Recall-Qualitaet: dieselben Fragen, zwei Auswahlregeln.

Warum (20.11.2026): Nebo bekam fuenf Eintraege je Nachricht, davon oft zwei
bis drei ohne Bezug. Ohne Zahl waere jede Aenderung eine Meinung. Diese
Messung haelt deshalb beide Regeln auf DEMSELBEN Kandidatenpool fest — alt
(Grenze 5, kein Filter, keine Schwelle) und neu (Grenze 3, Kategorien,
Mindestpunktzahl) — und schreibt je Frage die gelieferten Eintraege weg.

Der Lauf geht NICHT ueber den laufenden Dienst: der traegt den alten Stand und
wuerde beim Vergleich mit dem neuen Stand zwei verschiedene Zufaelle
vergleichen. Stattdessen wird derselbe Speicher im Prozess gebaut, die
Dienste-Schreibwege (Zaehler, Nutzungsstaerke, Quellpruefung) sind abgeschaltet,
und der BM25-Index kommt aus dem vorhandenen Zwischenspeicher (nur gelesen).

Aufruf (im Klon, Reihenfolge zaehlt):

    ./.venv/bin/python benchmarks/recall_qualitaet.py --modus alt --aus alt.json
    ./.venv/bin/python benchmarks/recall_qualitaet.py --modus neu --aus neu.json

Die Fragen stehen in ``benchmarks/.messdaten/fragen.json`` (echte Fragen aus
den Sitzungsaufzeichnungen der letzten Wochen, nicht im Repo). Danach bewertet
ein Mensch je Frage, welche Eintraege passen (``bewertung.json``); erst
``--auswerten`` rechnet Trefferquote und Ballastanteil.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

HIER = Path(__file__).resolve().parent
DATEN = HIER / ".messdaten"

# Der Bestand, den der Dienst liest (siehe ~/.hermes/.env).
os.environ.setdefault("NEXUS_COLLECTION", "nexus_hf")
os.environ.setdefault("NEXUS_QDRANT_HOST", "localhost")
os.environ.setdefault("NEXUS_QDRANT_PORT", "6333")
os.environ.setdefault("NEXUS_EMBEDDING_PROVIDER", "auto")
os.environ.setdefault("NEXUS_EMBEDDING_MODEL", "Qwen/Qwen3-Embedding-0.6B")
os.environ.setdefault("NEXUS_AGENT_ID", "benchmark")
# Ein Messlauf ist kein Dienstbetrieb: das Lese-Protokoll dieser Laeufe gehoert
# in den Messordner, nicht in das Protokoll des Rechners.
os.environ.setdefault("NEXUS_LOG_DIR", str(HIER / ".messdaten" / "logs"))

sys.path.insert(0, str(HIER.parent / "src"))   # nexus_memory
sys.path.insert(0, str(HIER.parent))            # nexus (Top-Level-Paket im Repo)


def _baue_store():
    """MemoryStore ohne Dienste: kein Backup, kein Trust-Sweep, kein Watch.

    ``__new__`` statt ``__init__`` ist das dokumentierte Muster fuer Messungen
    und Tests; ``__init__`` startet vier Hintergrundlaeufe, die in den Bestand
    schreiben. Genau das darf eine Messung nicht.
    """
    from nexus_memory import mcp_server as ms

    store = ms.MemoryStore.__new__(ms.MemoryStore)
    store.client = ms.QdrantClient(host=ms.QDRANT_HOST, port=ms.QDRANT_PORT)
    store._embedder = ms.EmbeddingProvider()
    store._binding = ms.CollectionBinding(
        store.client, ms.COLLECTION_NAME, store._embedder,
        config_legacy_collections=ms._read_legacy_collections(),
    )
    store._binding.ensure()
    store._embedder = store._binding.provider
    store.vector_name = store._binding.vector_name()
    store._hybrid_retriever = None
    store._skill_graph = None
    store._scope_centroids = None
    store._update_check_result = None
    store._update_check_time = 0
    store._update_nudged_at = 0

    # BM25: vorhandenen Zwischenspeicher lesen, neue Staende NICHT in den
    # gemeinsamen Ordner schreiben (sonst ueberschreibt die Messung den Index
    # des laufenden Dienstes).
    from nexus.retrieval import HybridRetriever

    retriever = HybridRetriever(
        qdrant_host=ms.QDRANT_HOST,
        qdrant_port=ms.QDRANT_PORT,
        vector_name=store.vector_name,
    )
    retriever._index_dir = DATEN / "bm25-cache"
    if not retriever._ids:
        retriever.index_memories()
    store._hybrid_retriever = retriever

    # Schreibwege des Dienstes abschalten: Zaehler, Nutzungsstaerke, Quellen-HEAD.
    ms._count_memory_access = lambda *a, **k: None

    async def _keine_quellen(self, urls):
        return {}

    store._check_sources = _keine_quellen.__get__(store, type(store))
    try:
        from nexus_memory import memory_dynamics

        memory_dynamics.access_update_payload = None
    except Exception:
        pass
    return store, ms


async def _lauf(modus: str, fragen: list[dict]) -> list[dict]:
    store, ms = _baue_store()
    aus = []
    for nr, fall in enumerate(fragen, 1):
        frage = fall["frage"]
        if modus == "roh":
            # Der rohe Kandidatenkreis: kein Filter, grosse Grenze. Aus IHM
            # werden alt und neu offline gerechnet, damit beide Regeln
            # dieselben Kandidaten sehen (Paarvergleich, keine Drift).
            treffer = await store.recall(frage, agent_level="private", limit=20)
        elif modus == "alt":
            # Der heutige Weg: das DSH-Plugin fragt limit=5, sonst nichts.
            treffer = await store.recall(frage, agent_level="private", limit=5)
        else:
            treffer = await store.recall(
                frage,
                agent_level="private",
                limit=3,
                categories=["fact", "rule", "preference"],
                min_score=0.35,
            )
        aus.append({
            "nr": nr,
            "frage": frage,
            "treffer": [
                {
                    "doc_id": t.get("doc_id"),
                    "id": t.get("id"),
                    "category": t.get("category"),
                    "type": t.get("type"),
                    "score": t.get("score"),
                    "relevance": t.get("relevance"),
                    # Woertliche Evidenz: ohne sie kann die Regel offline
                    # nicht dieselbe Entscheidung treffen wie der Dienst.
                    "bm25": t.get("_bm25"),
                    "text": (t.get("text") or "")[:300],
                }
                for t in treffer
            ],
        })
        print(f"[{nr:02d}/{len(fragen)}] {len(aus[-1]['treffer'])} Treffer  {frage[:60]}", flush=True)
    return aus


def _auswerten(alt: list[dict], neu: list[dict], bewertung: dict) -> dict:
    """Trefferquote in den ersten 3 und Ballastanteil, alt gegen neu.

    ``bewertung`` bildet Frage-Nummer -> Menge der passenden doc_ids. Ein
    Eintrag ohne Bewertung zaehlt als unpassend (der strenge Fall).
    """
    def zeile(lauf, top_n):
        treffer = 0
        geliefert = 0
        passend = 0
        leer = 0
        for fall in lauf:
            passt = set(bewertung.get(str(fall["nr"]), []))
            genommen = fall["treffer"][:top_n]
            geliefert += len(genommen)
            passend += sum(1 for t in genommen if t["doc_id"] in passt or t["id"] in passt)
            if any(t["doc_id"] in passt or t["id"] in passt for t in genommen):
                treffer += 1
            if not genommen:
                leer += 1
        return {
            "fragen": len(lauf),
            "fragen_mit_treffer": treffer,
            "trefferquote": round(treffer / max(1, len(lauf)), 3),
            "geliefert": geliefert,
            "passend": passend,
            "ballast_anteil": round(1 - passend / max(1, geliefert), 3),
            "leere_bloecke": leer,
        }

    return {"alt_top5": zeile(alt, 5), "neu_top3": zeile(neu, 3)}


def _kurve(roh: list[dict], bewertung: dict, top_n: int = 3) -> list[dict]:
    """Ballastanteil und Trefferquote ueber eine Reihe von Schwellen.

    Die Regel wird HIER angewendet (auf denselben rohen Kandidatenkreis), damit
    die Zahl zur Schwelle gehoert und nicht zu einem zweiten Messlauf. Genau
    das war der Fehler beim ersten Versuch: zwei Laeufe, zwei Zufaelle.
    """
    from nexus_memory import recall_policy as rp

    kategorien = rp.DEFAULT_RECALL_CATEGORIES
    zeilen = []
    for schwelle in [0.0, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70]:
        geliefert = 0
        passend = 0
        mit_treffer = 0
        leer = 0
        for fall in roh:
            passt = set(bewertung.get(str(fall["nr"]), []))
            gewaehlt = [
                e for e in fall["treffer"]
                if rp.passes({**e, "bm25_score": e.get("bm25")},
                             categories=kategorien,
                             min_score=(schwelle or None))
            ][:top_n]
            geliefert += len(gewaehlt)
            passend += sum(1 for e in gewaehlt if e["doc_id"] in passt)
            if any(e["doc_id"] in passt for e in gewaehlt):
                mit_treffer += 1
            if not gewaehlt:
                leer += 1
        zeilen.append({
            "schwelle": schwelle,
            "fragen_mit_treffer": mit_treffer,
            "trefferquote_top3": round(mit_treffer / max(1, len(roh)), 3),
            "geliefert": geliefert,
            "passend": passend,
            "ballast_anteil": round(1 - passend / max(1, geliefert), 3),
            "leere_bloecke": leer,
        })
    return zeilen


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--modus", choices=["alt", "neu", "roh"], help="welche Regel gemessen wird")
    ap.add_argument("--aus", help="Zieldatei der Rohdaten")
    ap.add_argument("--auswerten", action="store_true", help="alt.json gegen neu.json rechnen")
    ap.add_argument("--kurve", action="store_true", help="Schwellenkurve aus roh.json")
    args = ap.parse_args()

    DATEN.mkdir(parents=True, exist_ok=True)
    if args.auswerten:
        alt = json.loads((DATEN / "alt.json").read_text())
        neu = json.loads((DATEN / "neu.json").read_text())
        bewertung = json.loads((DATEN / "bewertung.json").read_text())
        print(json.dumps(_auswerten(alt, neu, bewertung), indent=2, ensure_ascii=False))
        return 0

    if args.kurve:
        roh = json.loads((DATEN / "roh.json").read_text())
        bewertung = json.loads((DATEN / "bewertung.json").read_text())
        # Praefix-Invariante pruefen: die ersten fuenf des rohen Laufs muessen
        # der alte Lauf sein, sonst ist der Vergleich nicht paarweise.
        alt = json.loads((DATEN / "alt.json").read_text())
        abweichung = [
            f["nr"] for f, a in zip(roh, alt)
            if [t["doc_id"] for t in f["treffer"][:5]] != [t["doc_id"] for t in a["treffer"]]
        ]
        print(f"Praefix-Abweichungen roh[:5] vs alt: {len(abweichung)} {abweichung}")
        print(json.dumps(_kurve(roh, bewertung), indent=2, ensure_ascii=False))
        return 0

    fragen = json.loads((DATEN / "fragen.json").read_text())
    ergebnis = asyncio.run(_lauf(args.modus, fragen))
    ziel = DATEN / (args.aus or f"{args.modus}.json")
    ziel.write_text(json.dumps(ergebnis, indent=2, ensure_ascii=False))
    print(f"geschrieben: {ziel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
