"""Stichwortindex mit Seltenheitsgewichtung (BM25) fuer die Einspielung.

Warum ein eigener Index: Die Aehnlichkeitssuche findet eine Notiz nur, wenn sie
aehnlich formuliert ist wie die Frage. Ein Fakt, der ein seltenes Wort der Frage
woertlich traegt, geht dabei unter, weil kurze Etiketten ("concept: ...") naeher
liegen. Ein BM25-Index gewichtet genau dieses seltene Wort hoch und liefert die
Kandidaten, die die Aehnlichkeit verpasst.

Der Index liegt in einer eigenen SQLite-Datei (FTS5) und wird aus einem
einmaligen Lesen der Sammlung gebaut. Er ist unabhaengig von Qdrant: Schreiben
in die Sammlung findet hier nicht statt. Ist die Datei kaputt oder fehlt sie,
faellt der Aufrufer auf die reine Aehnlichkeitssuche zurueck.

Oeffentliche Schnittstelle:
    tokenize            Wortmarken einer Zeile (Umlaute gefaltet, Versionen ganz)
    build_index         Index aus einer Punktliste bauen (atomar)
    build_from_qdrant   Index aus der Sammlung bauen (nur lesend)
    upsert_point        einen Punkt nach einem Schreiben nachziehen
    KeywordIndex        Lesen/Suchen, mit Eignungspruefung
    ensure_index        Index liefern oder im Hintergrund nachziehen
    rrf_fuse            Verschmelzung zweier Ranglisten (Reciprocal Rank Fusion)
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger("nexus.hermes_plugin.keyword_index")

# Umlaute werden gefaltet, damit "Heizöl" und "Heizoel" dasselbe Wort sind.
_UMLAUT_FOLD = str.maketrans({
    "ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
    "Ä": "ae", "Ö": "oe", "Ü": "ue",
})

# Wortmarke: Buchstaben/Ziffern, Punkte nur zwischen Ziffernbloecken. Dadurch
# bleibt "0.23.1" ein Token, waehrend "Heizöl-Lieferant" an der Fuge zerfaellt.
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:\.[a-z0-9]+)*")

# FTS5 (unicode61) behandelt den Unterstrich als Wortzeichen. Punktierte
# Versionen werden deshalb intern auf Unterstriche abgebildet; andernfalls
# zerlegt der Tokenizer "0.23.1" in "0", "23", "1" und die Frage trifft nie.
_VERSION_DOT = "_"

_FTS_TOKENIZER = "unicode61 remove_diacritics 0"

# Ab dieser Zeilenzahl wird in Bloecken geschrieben (Speicher schonen).
_INSERT_BATCH = 1000

# Ein laufender Hintergrundaufbau je Indexdatei. Der Aufbau ist teuer; zwei
# gleichzeitige Laeufe wuerden nur dieselbe Datei ueberschreiben.
_BUILD_LOCK = threading.Lock()
_BUILDING: set = set()

# Ein Nachziehen je Indexdatei; SQLite vertraegt zwar mehrere Schreiber, aber
# der Index ist klein und ein Wartender soll nicht in die Sperrfrist laufen.
_WRITE_LOCK = threading.Lock()


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    """Ganzzahl aus der Umgebung, begrenzt; ein kaputter Wert faellt zurueck."""
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return max(lo, min(hi, int(str(raw).strip())))
    except (TypeError, ValueError):
        return default


def default_db_path() -> Path:
    """Ablage des Index: ``NEXUS_FTS_DB``, sonst ``$HERMES_HOME/state/nexus-fts.db``."""
    env = os.environ.get("NEXUS_FTS_DB", "").strip()
    if env:
        return Path(os.path.expanduser(env))
    home = os.environ.get("HERMES_HOME", "").strip() or "~/.hermes"
    return Path(os.path.expanduser(home)) / "state" / "nexus-fts.db"


def _fold(text: str) -> str:
    """Kleinschreibung und Umlautfaltung in einem Schritt."""
    return (text or "").translate(_UMLAUT_FOLD).lower()


def tokenize(text: str, min_len: int = 1,
             stopwords: Optional[Iterable[str]] = None) -> List[str]:
    """Wortmarken eines Textes.

    ``min_len`` gilt fuer die gefaltete Marke; ``stopwords`` werden verworfen.
    Versionen wie ``0.23.1`` bleiben eine Marke (Punkt wird zu Unterstrich).
    """
    stop = stopwords if stopwords is not None else ()
    out: List[str] = []
    for raw in _TOKEN_RE.findall(_fold(text)):
        if len(raw) < min_len:
            continue
        if raw in stop:
            continue
        out.append(raw.replace(".", _VERSION_DOT) if "." in raw else raw)
    return out


def index_body(content: Any, search_terms: Any = "") -> str:
    """FTS-Text eines Punktes: Inhalt plus beim Schreiben erzeugte Suchvarianten.

    Die Varianten tragen Wortteile, die im Inhalt fehlen ("Oellieferant" ->
    "lieferant"); ohne sie findet die Frage "Heizöl-Lieferant" den Eintrag nicht.
    """
    return " ".join(tokenize(str(content or "")) + tokenize(str(search_terms or "")))


def query_expression(query: str, min_len: int = 2,
                     stopwords: Optional[Iterable[str]] = None) -> Optional[str]:
    """FTS5-Ausdruck aus einer Frage (ODER-Verknuepfung der Wortmarken).

    Kein Ausdruck, wenn nach Stoppwoertern und Mindestlaenge nichts uebrig
    bleibt; der Aufrufer faellt dann auf die Aehnlichkeitssuche zurueck.
    """
    terms = list(dict.fromkeys(tokenize(query, min_len=min_len, stopwords=stopwords)))
    if not terms:
        return None
    # Marken bestehen nur aus [a-z0-9_.]; Anfuehrungszeichen sind daher sicher.
    return " OR ".join(f'"{t}"' for t in terms)


def rrf_fuse(ranked_id_lists: Sequence[Sequence[Any]], k: int = 60) -> List[str]:
    """Reciprocal Rank Fusion: Ranglisten zu einer Reihenfolge verschmelzen.

    Jede Liste traegt bei Rang ``r`` (nullbasiert) den Beitrag ``1/(k+r+1)``
    bei. Seltene Stichworttreffer und aehnliche Punkte wiegen damit gleich;
    ein Punkt, der in beiden Listen steht, sammelt beide Beitraege. Bei
    Gleichstand bleibt die zuerst gesehene Kennung vorn (stabile Ausgabe).
    """
    scores: Dict[str, float] = {}
    first_seen: Dict[str, int] = {}
    for ids in ranked_id_lists:
        for rank, pid in enumerate(ids):
            key = str(pid)
            if key not in first_seen:
                first_seen[key] = len(first_seen)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
    return sorted(first_seen, key=lambda key: (-scores[key], first_seen[key]))


def payload_text(payload: Optional[Dict[str, Any]]) -> str:
    """Inhalt eines Punktes: ``content``, sonst ``text``.

    Dieselbe Regel wie in der Einspielung. Ohne den Rueckfall indizierte der
    Stichwortindex 631 Punkte mit leerem Text — sie waren ueber BM25 nicht
    auffindbar, obwohl ihr Inhalt im Payload steht.
    """
    src = payload or {}
    return str(src.get("content") or src.get("text") or "")


def _slim_payload(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Nur die Felder, die die Einspielung zum Filtern und Anzeigen braucht."""
    src = payload or {}
    keep = ("content", "text", "category", "lifecycle_status", "access_level",
            "scope", "search_terms", "state_value")
    return {key: src[key] for key in keep if key in src}


def _point_parts(point: Any) -> Tuple[str, Dict[str, Any]]:
    """Kennung und Payload eines Punktes (Objekt oder Dict)."""
    if isinstance(point, dict):
        pid = point.get("id")
        payload = point.get("payload") or {}
    else:
        pid = getattr(point, "id", None)
        payload = getattr(point, "payload", None) or {}
    return str(pid), _slim_payload(payload)


def build_index(points: Iterable[Any], path: Optional[os.PathLike] = None,
                collection: str = "") -> int:
    """Baut den Index aus einer Punktliste und ersetzt die Datei atomar.

    Geschrieben wird in eine Nachbardatei und erst nach erfolgreichem Schluss
    umbenannt; ein abgebrochener Aufbau hinterlaesst so nie einen halben Index.
    Rueckgabe: Anzahl der aufgenommenen Punkte.
    """
    db_path = Path(path) if path is not None else default_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = db_path.with_name(db_path.name + ".tmp")
    try:
        tmp_path.unlink()
    except FileNotFoundError:
        pass
    con = sqlite3.connect(str(tmp_path))
    count = 0
    try:
        # Der Aufbau ist einmalig und darf schnell sein; Haltbarkeit ist hier
        # nicht wichtig, weil die Datei danach nur gelesen wird.
        con.execute("PRAGMA journal_mode=OFF")
        con.execute("PRAGMA synchronous=OFF")
        con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        con.execute("CREATE TABLE docs (doc_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        con.execute(
            "CREATE VIRTUAL TABLE docs_fts USING fts5("
            f"doc_id UNINDEXED, body, tokenize='{_FTS_TOKENIZER}')"
        )
        docs_batch: List[Tuple[str, str]] = []
        fts_batch: List[Tuple[str, str]] = []
        for point in points:
            pid, payload = _point_parts(point)
            body = index_body(payload_text(payload), payload.get("search_terms"))
            docs_batch.append((pid, json.dumps(payload, ensure_ascii=False)))
            fts_batch.append((pid, body))
            count += 1
            if len(docs_batch) >= _INSERT_BATCH:
                con.executemany("INSERT OR REPLACE INTO docs VALUES (?, ?)", docs_batch)
                con.executemany("INSERT INTO docs_fts (doc_id, body) VALUES (?, ?)", fts_batch)
                docs_batch.clear()
                fts_batch.clear()
        if docs_batch:
            con.executemany("INSERT OR REPLACE INTO docs VALUES (?, ?)", docs_batch)
            con.executemany("INSERT INTO docs_fts (doc_id, body) VALUES (?, ?)", fts_batch)
        con.executemany("INSERT INTO meta (key, value) VALUES (?, ?)",
                        [("collection", str(collection or "")), ("points", str(count))])
        con.commit()
    except Exception:
        con.close()
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
    else:
        con.close()
    os.replace(tmp_path, db_path)
    return count


def build_from_qdrant(qdrant: Any, collection: str,
                      path: Optional[os.PathLike] = None) -> int:
    """Liest die Sammlung einmal vollstaendig und baut daraus den Index.

    Bewusst ohne seitenweises Blaettern: die Sammlung traegt gemischte
    Kennungen, und der Seitenzeiger laeuft dabei im Kreis. Ein einzelner
    Scroll mit hohem Limit liefert alle Punkte.
    """
    limit = _env_int("NEXUS_FTS_LIMIT", 60000, 1, 1_000_000)
    records, _offset = qdrant.scroll(
        collection_name=collection, limit=limit,
        with_payload=True, with_vectors=False,
    )
    return build_index(records or [], path=path, collection=collection)


class KeywordIndex:
    """Lesender Zugriff auf den FTS5-Index einer Sammlung."""

    def __init__(self, path: Optional[os.PathLike] = None, collection: str = "") -> None:
        self.path = Path(path) if path is not None else default_db_path()
        self.collection = str(collection or "")

    def _connect(self) -> sqlite3.Connection:
        # Nur lesen: ein fehlender Index wird nicht versehentlich angelegt.
        uri = f"file:{self.path}?mode=ro"
        return sqlite3.connect(uri, uri=True)

    def meta(self) -> Dict[str, str]:
        """Gespeicherte Kopfdaten (Sammlung, Zeilenzahl); leer bei Fehlern."""
        try:
            con = self._connect()
            try:
                rows = con.execute("SELECT key, value FROM meta").fetchall()
            finally:
                con.close()
            return {str(k): str(v) for k, v in rows}
        except (sqlite3.Error, OSError):
            return {}

    def count(self) -> int:
        """Zeilenzahl des Index, -1 wenn er nicht lesbar ist."""
        try:
            con = self._connect()
            try:
                return int(con.execute("SELECT count(*) FROM docs").fetchone()[0])
            finally:
                con.close()
        except (sqlite3.Error, OSError, TypeError, IndexError):
            return -1

    def is_current(self, expected_count: Optional[int] = None) -> bool:
        """Passt der Index zur Sammlung (Name und, falls bekannt, Zeilenzahl)?"""
        info = self.meta()
        if info.get("collection") != self.collection:
            return False
        if expected_count is None:
            return True
        try:
            stored = int(info.get("points", "-1"))
        except (TypeError, ValueError):
            return False
        return stored == int(expected_count)

    def search(self, query: str, limit: int = 32, min_len: int = 2,
               stopwords: Optional[Iterable[str]] = None
               ) -> List[Tuple[str, Dict[str, Any]]]:
        """BM25-Suche; seltene Woerter wiegen mehr als haeufige.

        Rueckgabe: ``(kennung, payload)`` in Rangfolge, bester Treffer zuerst.
        """
        expr = query_expression(query, min_len=min_len, stopwords=stopwords)
        if not expr:
            return []
        try:
            con = self._connect()
            try:
                rows = con.execute(
                    "SELECT d.doc_id, d.payload, bm25(docs_fts) AS score "
                    "FROM docs_fts JOIN docs d ON d.doc_id = docs_fts.doc_id "
                    "WHERE docs_fts MATCH ? ORDER BY score LIMIT ?",
                    (expr, max(1, int(limit))),
                ).fetchall()
            finally:
                con.close()
        except (sqlite3.Error, OSError) as exc:
            logger.debug("Stichwortsuche nicht moeglich: %s", exc)
            return []
        hits: List[Tuple[str, Dict[str, Any]]] = []
        for doc_id, payload_text, _score in rows:
            try:
                payload = json.loads(payload_text or "{}")
            except (TypeError, ValueError):
                payload = {}
            hits.append((str(doc_id), payload if isinstance(payload, dict) else {}))
        return hits


def upsert_point(point: Any, path: Optional[os.PathLike] = None,
                 collection: str = "") -> bool:
    """Zieht einen Punkt nach einem erfolgreichen Schreiben in den Index nach.

    Der Index wird sonst nur aus einem vollstaendigen Lesen der Sammlung
    gebaut; ein frisch geschriebener Punkt fehlte darin bis zum naechsten
    Aufbau und war damit ueber seltene Stichwoerter nicht auffindbar.

    Geschrieben wird nur in eine vorhandene, lesbare Indexdatei derselben
    Sammlung. Fehlt sie, laeuft gerade ein Neuaufbau, gehoert sie zu einer
    anderen Sammlung oder ist sie gesperrt, bleibt der Index unveraendert
    (Rueckgabe ``False``). Der Aufrufer darf das Schreiben davon nicht
    abhaengig machen: der Index ist eine Zugabe, sonst faellt die Einspielung
    auf die Aehnlichkeitssuche zurueck.
    """
    db_path = Path(path) if path is not None else default_db_path()
    key = str(db_path)
    if not db_path.exists():
        return False
    with _WRITE_LOCK:
        with _BUILD_LOCK:
            if key in _BUILDING:
                # Der Aufbau schreibt die Datei am Ende neu (os.replace);
                # ein Eintrag jetzt ginge dabei verloren.
                return False
        try:
            con = sqlite3.connect(str(db_path), timeout=2.0)
        except sqlite3.Error as exc:
            logger.debug("Stichwortindex nicht schreibbar: %s", exc)
            return False
        try:
            row = con.execute(
                "SELECT value FROM meta WHERE key = 'collection'"
            ).fetchone()
            stored_collection = str(row[0]) if row else ""
            if collection and stored_collection != collection:
                # Eine andere Sammlung teilt sich die Datei nicht.
                return False
            pid, payload = _point_parts(point)
            body = index_body(payload_text(payload), payload.get("search_terms"))
            with con:
                # Alte Zeile zuerst entfernen: ein FTS5-Eintrag wird nicht
                # durch INSERT OR REPLACE ersetzt, sondern ein zweites Mal
                # angelegt.
                con.execute("DELETE FROM docs_fts WHERE doc_id = ?", (pid,))
                con.execute(
                    "INSERT OR REPLACE INTO docs VALUES (?, ?)",
                    (pid, json.dumps(payload, ensure_ascii=False)),
                )
                con.execute(
                    "INSERT INTO docs_fts (doc_id, body) VALUES (?, ?)", (pid, body)
                )
                total = con.execute("SELECT count(*) FROM docs").fetchone()
                con.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('points', ?)",
                    (str(int(total[0])),),
                )
            return True
        except (sqlite3.Error, OSError, TypeError, ValueError) as exc:
            logger.debug("Stichwortindex nicht nachgezogen: %s", exc)
            return False
        finally:
            con.close()


def collection_count(qdrant: Any, collection: str) -> Optional[int]:
    """Punktzahl der Sammlung; ``None``, wenn sie sich nicht ermitteln laesst."""
    try:
        result = qdrant.count(collection_name=collection, exact=True)
        value = getattr(result, "count", result)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return int(value)
    except Exception:
        return None


def _rebuild_in_background(qdrant: Any, collection: str, path: Path) -> None:
    """Startet einen Aufbau, sofern nicht bereits einer laeuft."""
    key = str(path)
    with _BUILD_LOCK:
        if key in _BUILDING:
            return
        _BUILDING.add(key)

    def _run() -> None:
        try:
            build_from_qdrant(qdrant, collection, path=path)
        except Exception as exc:
            logger.debug("Stichwortindex-Aufbau abgebrochen: %s", exc)
        finally:
            with _BUILD_LOCK:
                _BUILDING.discard(key)

    threading.Thread(target=_run, name="nexus-fts-build", daemon=True).start()


def ensure_index(qdrant: Any, collection: str,
                 path: Optional[os.PathLike] = None
                 ) -> Optional[KeywordIndex]:
    """Liefert einen nutzbaren Index; zieht ihn sonst im Hintergrund nach.

    Der Aufruf blockiert nicht auf dem Aufbau: fehlt der Index oder ist er
    veraltet, wird er in einem Hintergrundfaden gebaut und diese Runde laeuft
    mit reiner Aehnlichkeit. Ist die Datei defekt oder gehoert sie zu einer
    anderen Sammlung, wird sie nicht ueberschrieben; dann gibt es ``None``.
    """
    db_path = Path(path) if path is not None else default_db_path()
    index = KeywordIndex(db_path, collection)
    expected = collection_count(qdrant, collection)
    try:
        if index.is_current(expected):
            return index
        info = index.meta()
        if not info:
            # Kein lesbarer Index. Nur bauen, wenn die Sammlung bekannt ist —
            # sonst legte ein Testlauf einen Index aus einer Attrappe an.
            if expected is not None and expected > 0:
                _rebuild_in_background(qdrant, collection, db_path)
            return None
        if info.get("collection") != collection:
            # Eine andere Sammlung teilt sich die Datei nicht; nicht ueberschreiben.
            return None
        if expected is not None:
            _rebuild_in_background(qdrant, collection, db_path)
        # Veralteter, aber lesbarer Index: diese Runde noch verwenden.
        return index
    except Exception as exc:
        logger.debug("Stichwortindex nicht verfuegbar: %s", exc)
        return None
