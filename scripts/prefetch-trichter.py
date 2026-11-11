#!/usr/bin/env python3
"""Trichter-Messung der Einspielung: vier Stufen je Frage, nur lesend.

Gemessen wird, wo der tragende Punkt auf dem Weg in den eingespielten Block
verloren geht:

  a) Kandidatenkreis — steht der Punkt im verschmolzenen Kreis (Aehnlichkeit
     plus Stichwortindex)?
  b) Aehnlichkeit   — sein Rang in der reinen Aehnlichkeitsliste.
  c) Stichwort      — sein Rang in der BM25-Liste (seltene Woerter zuerst).
  d) Block          — steht das erwartete Stichwort im fertigen Block
     (Budget 2400 Zeichen, Fenster um die Fundstelle)?

Auf die Sammlung wird ausschliesslich lesend zugegriffen. Geschrieben wird nur
der Stichwortindex unter ``~/.hermes/state/nexus-fts.db`` (bzw. ``NEXUS_FTS_DB``),
und zwar nur, wenn er fehlt oder nicht zur Sammlung passt.

Aufruf:
    python3 scripts/prefetch-trichter.py
    python3 scripts/prefetch-trichter.py --rerank      # Cross-Encoder testen
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import urllib.request
from types import SimpleNamespace

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "src"))
os.environ.setdefault("NEXUS_PREFETCH_MIN_CHARS", "40")

# Fragen und erwartete Stichworte: uebernommen aus
# ~/.hermes/cache/scratch/nexus-messlatte.py (20 echte Fragen).
FRAGEN = [
    ("Welche Telegram Chat ID hat Nebo", "5763330319"),
    ("Welche IP hat der Windows-PC", "192.168.31.206"),  # guard-ok: Testfrage, IP aus dem Bestand
    ("Wie viele Punkte liegen in der Nexus-Sammlung", "51.7"),
    ("Welche Claude-Code-Fassung läuft", "2.1.322"),  # guard-ok: Testfrage, Fassungsnummer aus dem Bestand
    ("Welches macOS läuft auf dem Mac mini", "27.2.1"),
    ("Welches Notebook nutzt Ina", "ideapad"),
    ("Wie groß ist der Heizöltank", "5.800"),
    ("Wer liefert unser Heizöl", "marsolek"),
    ("Heizöl-Lieferant", "marsolek"),
    ("Welcher Stromanbieter kommt im November", "savin"),
    ("Welche Fassung hat Hermes", "0.23.1"),
    ("Welcher Port gehört zum Einbetter", "9122"),
    ("Wie heißt der Schornsteinfeger", "berger"),
    ("Welche Qdrant-Fassung läuft", "1.20"),
    ("Welche IP hat Inas Laptop", "192.168.31.115"),  # guard-ok: Testfrage, IP aus dem Bestand
    ("Was kostet der Energieausweis", "99"),
    ("Welcher Kessel steht im Keller", "buderus"),
    ("Wie viel Heizöl verbrauchen wir im Jahr", "4.300"),
    ("Wann kommt die Müllabfuhr", "abfuhr"),
    ("Welche Wärmepumpe ist geplant", "vitocal"),
]

QDRANT = os.environ.get("NEXUS_QDRANT_URL", "http://127.0.0.1:6333")
COLLECTION = os.environ.get("NEXUS_COLLECTION", "nexus_hf")
EMBED = os.environ.get("NEXUS_EMBED_URL", "http://127.0.0.1:9122/embed")
VECTOR_NAME = os.environ.get(
    "NEXUS_VECTOR_NAME", "sentence-transformers__qwen_qwen3-embedding-0_6b__1024")  # guard-ok: Name der Sammlung, per NEXUS_VECTOR_NAME ueberschreibbar
BUDGET = int(os.environ.get("NEXUS_PREFETCH_CHARS", "2400"))
MIN_CHARS = int(os.environ.get("NEXUS_PREFETCH_MIN_CHARS", "40"))
VECTOR_LIMIT = int(os.environ.get("NEXUS_PREFETCH_VECTOR_LIMIT", "32"))


def _plugin():
    """Laedt das Hermes-Plugin als einzelne Datei (wie die Messlatte)."""
    spec = importlib.util.spec_from_file_location(
        "nexusplug", os.path.join(REPO, "plugins", "memory", "nexus", "__init__.py"))
    plug = importlib.util.module_from_spec(spec)
    sys.modules["nexusplug"] = plug
    spec.loader.exec_module(plug)
    return plug


def _http(url, payload, headers=None):
    head = {"Content-Type": "application/json"}
    head.update(headers or {})
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=head)
    return json.loads(urllib.request.urlopen(req, timeout=120).read())


def _embed(text):
    from nexus_memory import service_auth
    head = {getattr(service_auth, "AUTH_HEADER", "x-nexus-service-key"):
            service_auth.load_service_key()}
    return _http(EMBED, {"text": text}, head)["embedding"]


class HttpQdrant:
    """Nur die beiden Lesezugriffe, die der Stichwortindex braucht."""

    def count(self, collection_name, exact=True):
        result = _http(f"{QDRANT}/collections/{collection_name}/points/count",
                       {"exact": bool(exact)})
        return SimpleNamespace(count=result.get("result", {}).get("count", 0))

    def scroll(self, collection_name, limit, with_payload=True, with_vectors=False,
               **_kwargs):
        result = _http(f"{QDRANT}/collections/{collection_name}/points/scroll",
                       {"limit": limit, "with_payload": bool(with_payload),
                        "with_vector": bool(with_vectors)})
        records = [SimpleNamespace(id=p["id"], payload=p.get("payload") or {})
                   for p in result.get("result", {}).get("points", [])]
        return records, None


class AehnlichkeitsPunkt:
    def __init__(self, data):
        self.id = data["id"]
        self.score = float(data.get("score") or 0.0)
        self.payload = data.get("payload") or {}


def _text(point):
    return str((point.payload or {}).get("content") or "")


def _enthaelt(point, stichwort):
    return stichwort.lower() in _text(point).lower()


def _erster_rang(punkte, stichwort):
    """1-basierter Rang des ersten Punktes mit dem Stichwort, sonst None."""
    for rang, punkt in enumerate(punkte, 1):
        if _enthaelt(punkt, stichwort):
            return rang
    return None


def _block(plug, punkte, frage):
    """Baut den Block wie ``_do_prefetch_inner`` (ohne Graph-Zugabe)."""
    toks = plug._query_tokens(frage)
    gesamt, zeilen = 0, []
    for punkt in punkte:
        if gesamt >= BUDGET:
            break
        payload = punkt.payload or {}
        # Inhalt und Grenze kommen aus dem Plugin, nicht aus einer Abschrift:
        # sonst misst die Sonde eine aeltere Regel als die, die laeuft.
        text = plug._point_text(payload)
        if len(text.strip()) < plug._point_min_chars(payload):
            continue
        if (payload.get("lifecycle_status") or "canonical") in ("deprecated", "rolled_back"):
            continue
        if not text:
            continue
        item = (f"[{payload.get('category', 'fact')}] score={punkt.score or 0:.2f}: "
                f"{plug._fenster(text, toks, 400)}")
        if gesamt + len(item) > BUDGET:
            if BUDGET - gesamt < 80:
                break
            item = item[: BUDGET - gesamt].rstrip() + " …"
        zeilen.append(item)
        gesamt += len(item)
    return "\n".join(zeilen)


def _index_bauen(plug, qdrant):
    """Stichwortindex bereitstellen; baut ihn synchron, falls er fehlt."""
    mod = plug._keyword_index_mod
    if mod is None:
        return None
    pfad = mod.default_db_path()
    index = mod.KeywordIndex(pfad, COLLECTION)
    anzahl = mod.collection_count(qdrant, COLLECTION)
    if index.is_current(anzahl):
        print(f"Index: {pfad} ({index.count()} Punkte, aktuell)", file=sys.stderr)
        return index
    print(f"Index: {pfad} wird gebaut ...", file=sys.stderr)
    gebaut = mod.build_from_qdrant(qdrant, COLLECTION, pfad)
    print(f"Index: {gebaut} Punkte geschrieben", file=sys.stderr)
    return mod.KeywordIndex(pfad, COLLECTION)


def main() -> int:
    plug = _plugin()
    qdrant = HttpQdrant()
    index = _index_bauen(plug, qdrant)
    use_rerank = "--rerank" in sys.argv[1:]
    if use_rerank:
        from nexus_memory.reranker import rerank_points

    summe = {"kreis": 0, "aehnlich": 0, "stichwort": 0, "block": 0}
    print(f"{'a':>2} {'b':>4} {'c':>4} {'d':>2}  Frage / Stichwort")
    for frage, stichwort in FRAGEN:
        vektor = _embed(frage)
        roh = _http(f"{QDRANT}/collections/{COLLECTION}/points/query",
                    {"query": vektor, "limit": VECTOR_LIMIT,
                     "with_payload": True, "using": VECTOR_NAME})
        aehnlich = [AehnlichkeitsPunkt(p)
                    for p in roh.get("result", {}).get("points", [])]

        stichwort_liste = []
        if index is not None:
            treffer = index.search(frage, limit=plug._FTS_CANDIDATE_LIMIT,
                                   min_len=plug._FTS_MIN_TERM_LEN,
                                   stopwords=plug._KEYWORD_STOPWORDS)
            stichwort_liste = [plug._KeywordPoint(pid, payload)
                               for pid, payload in treffer]

        kreis = plug._rrf_order(list(aehnlich), stichwort_liste)
        if use_rerank and kreis:
            try:
                kreis = rerank_points(frage, kreis, reranker="cross-encoder",
                                      pool_k=len(kreis))
            except Exception as exc:  # Modell fehlt: Aussage bleibt vergleichbar
                print(f"Reranker nicht verfuegbar: {exc}", file=sys.stderr)

        im_kreis = any(_enthaelt(p, stichwort) for p in kreis)
        rang_a = _erster_rang(aehnlich, stichwort)
        rang_c = _erster_rang(stichwort_liste, stichwort)
        block = _block(plug, kreis, frage)
        im_block = stichwort.lower() in block.lower()

        summe["kreis"] += int(im_kreis)
        summe["aehnlich"] += int(rang_a is not None)
        summe["stichwort"] += int(rang_c is not None)
        summe["block"] += int(im_block)
        print(f"{'ja' if im_kreis else '--':>2} {rang_a if rang_a else '--':>4} "
              f"{rang_c if rang_c else '--':>4} {'ja' if im_block else '--':>2}  "
              f"{frage} / {stichwort}")

    n = len(FRAGEN)
    print()
    print(f"Summe von {n}: Kandidatenkreis {summe['kreis']}, "
          f"Aehnlichkeit {summe['aehnlich']}, Stichwort {summe['stichwort']}, "
          f"Block {summe['block']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
