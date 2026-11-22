"""Dreaming: nächtliches Lernen aus Sitzungsprotokollen (v0.22.61).

Was hier gelernt wird
---------------------
Nicht "Muster" im Sinn eines Modellurteils, sondern **wiederkehrende
Störungen mit Beleg**. Eine Störung zählt nur, wenn

1. sie strukturell als Fehlschlag vorliegt (``exit_code != 0`` oder ein
   gesetzter ``error``-Text in einem Werkzeug-Ergebnis) — niemals, weil ein
   Fehlerwort irgendwo im Text steht, und
2. sie in mindestens ``NEXUS_DREAMING_MIN_SESSIONS`` (Standard 2)
   verschiedenen Sitzungen auftritt, und
3. sie eine belegte Abhilfe trägt: den ``hint`` der Umgebung, oder eine
   Umgebungsursache (fehlendes Paket, fehlendes Programm, Zeitlimit,
   HTTP-Fehler). Reine Programmierfehler des Agenten (TypeError, KeyError,
   SyntaxError) sind kein Wissen und werden nicht gespeichert.

Was das alte Träumen falsch machte
----------------------------------
Bis 0.22.60 nahm es jedes Vorkommen von "fix", "fehler", "timeout" im Text,
schnitt ein Fenster von 160 Zeichen heraus (auch mitten im Satz einer langen
Cron-Anweisung) und fragte ein Modell, ob das ein "wiederkehrendes Muster"
sei. Gespeichert wurde danach der Rohtext. Ergebnis: Textfetzen im
Langzeitgedächtnis. Der Wiederkehr-Nachweis ist jetzt eine Zählung über
Sitzungen, kein Modellurteil; gespeichert wird ein Satz, der aus den
strukturierten Feldern gebaut ist.

Grenzen (ehrlich benannt)
-------------------------
Erkannt werden nur Störungen, die das Werkzeug-Ergebnis klar als Fehler
ausweist. Ein Fehler, der nur im Fließtext einer erfolgreichen Ausgabe
steht (etwa in einem Logfile-Auszug), zählt nicht — das ist die Grenze, an
der die alte Fassung gescheitert ist. Abhilfen, die der Agent selbst
gefunden hat, ohne dass die Umgebung sie als ``hint`` mitliefert, werden
nicht behauptet.

Env-Knöpfe:
- NEXUS_DREAMING (Standard "1")            Hauptschalter
- NEXUS_DREAM_STORE (Standard "0")         Gelerntes in den Speicher
  schreiben. Aus, bis die Erkennung an echten Daten gemessen ist; die
  JSON-Datei unter learned-playbooks/ entsteht immer.
- NEXUS_DREAMING_JOURNAL (Standard "1")   liest das eigene Ereignis-Journal
  (~/.nexus-memory/events/ereignisse.jsonl), das jeder Harness über
  POST /events füllt. Das ist die Quelle: Nexus hängt an keinem Agenten.
- NEXUS_DREAMING_SOURCES                   optionale Adapter, die eine fremde
  Sitzungsablage selbst lesen, z. B.
  [{"type": "hermes", "db": "/abs/pfad/state.db"},
   {"type": "jsonl", "dir": "/abs/verzeichnis"}]
  Nicht der Standard: wer eine fremde Ablage anschließt, tut das bewusst
  und außerhalb des Dienstes.
- NEXUS_DREAMING_LOOKBACK_DAYS (14)        Zeitfenster der Zählung
- NEXUS_DREAMING_LOOKBACK_HOURS            älterer Name, hat Vorrang
- NEXUS_DREAMING_MIN_SESSIONS (2)          wie oft es wiederkehren muss
- NEXUS_DREAMING_MIN_TOOLCALLS (5)         Sitzungen mit so viel Werkzeug
- NEXUS_DREAMING_RELEARN_DAYS (30)         dieselbe Störung erst danach neu
- NEXUS_DREAMING_PLAYBOOK_DIR (~/.nexus-memory/learned-playbooks)
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("nexus.dreaming")

NEXUS_DREAMING_ENABLED = os.environ.get("NEXUS_DREAMING", "1") == "1"
MIN_TOOLCALLS = int(os.environ.get("NEXUS_DREAMING_MIN_TOOLCALLS", "5"))
MIN_SESSIONS = int(os.environ.get("NEXUS_DREAMING_MIN_SESSIONS", "2"))
RELEARN_DAYS = float(os.environ.get("NEXUS_DREAMING_RELEARN_DAYS", "30"))
MAX_LESSONS_PER_RUN = 5
PLAYBOOK_DIR = Path(os.environ.get(
    "NEXUS_DREAMING_PLAYBOOK_DIR",
    str(Path.home() / ".nexus-memory" / "learned-playbooks")))

_JA_NEIN = re.compile(r"(?i)\b(ja|nein|yes|no)\b")


def _lookback_hours() -> float:
    """Zeitfenster in Stunden: der ältere Name LOOKBACK_HOURS hat Vorrang."""
    stunden = os.environ.get("NEXUS_DREAMING_LOOKBACK_HOURS")
    if stunden:
        try:
            return float(stunden)
        except ValueError:
            pass
    try:
        return float(os.environ.get("NEXUS_DREAMING_LOOKBACK_DAYS", "14")) * 24.0
    except ValueError:
        return 14 * 24.0


def _verdict_ist_nein(verdict: str) -> bool:
    """Liest das erste klare Ja oder Nein aus einem Modellurteil.

    Seit 0.22.61 entscheidet kein Modell mehr darüber, ob etwas wiederkehrt
    (das macht die Zählung über Sitzungen). Die Funktion bleibt, weil
    Aufrufer und Tests sie noch kennen.
    """
    text = str(verdict or "").lower()
    treffer = _JA_NEIN.search(text)
    if not treffer:
        return True
    return treffer.group(1) in ("nein", "no")


def _store_collection(store) -> Optional[str]:
    """Name der Sammlung, egal wie das Speicherobjekt ihn traegt.

    ``MemoryStore`` hat kein Feld ``collection`` — es haelt den Namen in
    ``_binding.collection_name``. Das alte ``self._store.collection`` warf
    deshalb immer einen AttributeError, der in ``_similar_known`` still
    gefangen wurde: die Dublettenpruefung lief nie.
    """
    for feld in ("collection", "collection_name"):
        wert = getattr(store, feld, None)
        if isinstance(wert, str) and wert:
            return wert
    binding = getattr(store, "_binding", None)
    if binding is not None:
        wert = getattr(binding, "collection_name", None)
        if isinstance(wert, str) and wert:
            return wert
    return None


def _embed_sync(store, text: str) -> list:
    """Einbettung aus dem Daemon-Thread (dort laeuft keine Schleife)."""
    import asyncio
    return asyncio.run(store._embedder.embed(text, is_query=False))


# ── Fehlerklassen ────────────────────────────────────────────────────
# Nur was hier steht, bekommt einen Namen. Alles andere gilt als
# "unbekannt" und wird nicht gelernt: ein Namenloser Fehler taugt nicht
# als Merksatz, und ein geratenes Etikett waere schlimmer als keines.
_KLASSEN: List[Tuple[str, "re.Pattern[str]"]] = [
    ("ModuleNotFoundError",
     re.compile(r"ModuleNotFoundError: No module named '([^']+)'")),
    ("ImportError", re.compile(r"(?:ImportError|ImportWarning): ([^\n]{0,60})")),
    ("CommandNotFound", re.compile(
        r"(?:command not found|No such file or directory: '?([\w.+-]+)'?)")),
    ("PermissionError", re.compile(r"PermissionError: [^\n]{0,70}")),
    ("FileNotFoundError", re.compile(r"FileNotFoundError: [^\n]{0,80}")),
    ("Timeout", re.compile(r"(?:timed out after|Exit 124\b|killed after)")),
    ("HTTPError", re.compile(r"HTTP(?: Error|Error ) ?(\d{3})")),
    ("DiskFull", re.compile(r"(?:No space left on device|disk full)", re.I)),
    ("PortInUse", re.compile(r"(?:Address already in use|port already in use)", re.I)),
    ("AuthError", re.compile(
        r"(?:401 Unauthorized|403 Forbidden|invalid api key|not owner"
        r"|permission denied)", re.I)),
]

# Diese Klassen sind Umgebungsursachen: auch ohne hint ein Merksatz wert,
# weil sie sagen, was fehlt. Programmierfehler des Agenten stehen bewusst
# nicht darin.
_UMWELT_KLASSEN = {
    "ModuleNotFoundError", "ImportError", "CommandNotFound", "PermissionError",
    "FileNotFoundError", "Timeout", "HTTPError", "DiskFull", "PortInUse",
    "AuthError",
}

# Regelblockaden der Laufzeitumgebung sind keine Lehre über die Welt:
# sie sagen, was dieser Gateway gerade nicht erlaubt. Sie wechseln mit
# der Konfiguration und wären in einem halben Jahr falsch.
_NICHT_LEHRE = re.compile(
    r"(THINK-GATE|BLOCKED:|restricted inside a supervised gateway"
    r"|Background review denied|requires approval)", re.I)

# Ein hint, der nur auf eine Fortsetzung verweist, ist kein Wissen.
_HINT_UNBRAUCHBAR = re.compile(
    r"(?i)^(use offset|use limit|showing \d+|truncated|mehr zeichen|"
    r"continue reading|call again)")

_PATH_LIKE = re.compile(r"/[\w.@+-]+(?:/[\w.@+-]+)*")
_NUM = re.compile(r"\b\d{2,}\b")


def _normalisieren(text: str) -> str:
    """Pfade und lange Zahlen raus: sonst zählt jeder Tag als eigener Fall."""
    text = _PATH_LIKE.sub("<pfad>", str(text or ""))
    return _NUM.sub("<n>", text)


def _outcome(content: Any) -> Tuple[bool, str, str]:
    """Strukturierter Ausgang eines Werkzeug-Ergebnisses.

    Rückgabe: (fehlgeschlagen, Fehlertext, hint). "Fehlgeschlagen" heißt
    ``exit_code != 0`` oder ein nicht leerer ``error``-Text oder
    ``status == "error"`` — nicht, dass irgendwo ein Fehlerwort steht.
    """
    if isinstance(content, (bytes, bytearray)):
        content = content.decode("utf-8", "replace")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except Exception:
            return False, "", ""
    if isinstance(content, list):
        for eintrag in content:
            fehler, text, hint = _outcome(json.dumps(eintrag))
            if fehler:
                return fehler, text, hint
        return False, "", ""
    if not isinstance(content, dict):
        return False, "", ""
    hint = str(content.get("hint") or "").strip()
    fehler_text = content.get("error")
    ausgabe = content.get("output")
    if ausgabe is None:
        ausgabe = content.get("content")
    exit_code = content.get("exit_code")
    if isinstance(exit_code, int) and exit_code != 0:
        return True, str(ausgabe or fehler_text or ""), hint
    if isinstance(fehler_text, str) and fehler_text.strip():
        return True, str(fehler_text), hint
    if str(content.get("status") or "").lower() == "error":
        return True, str(ausgabe or ""), hint
    return False, "", hint


def _signatur(tool: str, text: str) -> Optional[str]:
    """Name der Störung: Werkzeug, Fehlerklasse, betroffenes Ding.

    Ohne bekannte Klasse gibt es keine Signatur — unbekannte Fehler werden
    bewusst nicht gelernt. Regelblockaden der Umgebung fallen ebenfalls raus.
    """
    if not tool:
        return None
    roh = str(text or "")
    if _NICHT_LEHRE.search(roh):
        return None
    normal = _normalisieren(roh)
    for klasse, muster in _KLASSEN:
        treffer = muster.search(normal)
        if not treffer:
            continue
        token = ""
        if treffer.groups():
            token = str(treffer.group(1)).strip()
            if token.startswith("<pfad>"):
                token = ""
            token = token.replace("\n", " ")[:40]
        return f"{tool}:{klasse}" + (f":{token}" if token else "")
    return None


def _ist_lehrreich(signatur: str, hint: str) -> bool:
    """Speicherwürdig? Belegter Hinweis, oder Umgebungsursache mit Namen.

    Ohne hint braucht es ein benennbares Ding: "Timeout" allein sagt nichts,
    "ModuleNotFoundError: requests" sagt alles. Ein Merksatz, der nur
    "irgendwas läuft manchmal in ein Zeitlimit" behauptet, ist Rauschen und
    gehört nicht ins Langzeitgedächtnis.
    """
    if hint and not _HINT_UNBRAUCHBAR.match(hint):
        return True
    teile = signatur.split(":")
    klasse = teile[1] if len(teile) > 1 else ""
    token = ":".join(teile[2:]).strip()
    return klasse in _UMWELT_KLASSEN and bool(token)


# ── Sitzungsquellen ──────────────────────────────────────────────────

def _session_sources() -> List[Dict[str, Any]]:
    """Optionale Adapter aus NEXUS_DREAMING_SOURCES.

    Ohne Einstellung gibt es keine: das Träumen liest keine fremde
    Sitzungsablage, auch nicht die von Hermes, nur weil sie zufällig
    existiert. Wer sie anschließen will, sagt es ausdrücklich.
    """
    raw = os.environ.get("NEXUS_DREAMING_SOURCES")
    if not raw:
        return []
    try:
        srcs = json.loads(raw)
        if isinstance(srcs, list):
            return [s for s in srcs if isinstance(s, dict)]
        log.warning("dreaming: NEXUS_DREAMING_SOURCES ist keine Liste")
    except Exception:
        log.warning("dreaming: fehlerhaftes NEXUS_DREAMING_SOURCES JSON")
    return []


def _journal_incidents(cutoff: float) -> Tuple[List[Dict[str, Any]], int]:
    """Vorfälle aus dem eigenen Ereignis-Journal (neue Quelle)."""
    from nexus_memory import events as ereignisse
    vorfaelle: List[Dict[str, Any]] = []
    gesehen = 0
    for eintrag in ereignisse.iter_events(since_ts=cutoff):
        gesehen += 1
        if eintrag.get("ok", True):
            continue
        text = str(eintrag.get("error") or "")
        if not text:
            continue
        hint = str(eintrag.get("hint") or "")
        signatur = _signatur(str(eintrag.get("tool") or ""), text + " " + hint)
        if not signatur:
            continue
        # Ohne Sitzungskennung zählt alles als EINE Sitzung: lieber nichts
        # lernen als Wiederkehr erfinden.
        sitzung = str(eintrag.get("session") or "")
        if not sitzung:
            sitzung = f"ohne-sitzung:{eintrag.get('agent') or 'unbekannt'}"
        vorfaelle.append({"signatur": signatur,
                          "tool": str(eintrag.get("tool") or ""),
                          "hint": hint,
                          "ts": float(eintrag.get("ts") or 0),
                          "session_id": sitzung,
                          "klasse": signatur.split(":")[1]})
    return vorfaelle, gesehen


def _iter_hermes_sessions(db_path: str, cutoff: float) -> List[Dict[str, Any]]:
    """Beendete Sitzungen mit Werkzeuggebrauch, neuer als cutoff."""
    out: List[Dict[str, Any]] = []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT id, started_at, message_count, tool_call_count "
            "FROM sessions WHERE ended_at IS NOT NULL AND tool_call_count >= ? "
            "AND started_at > ? ORDER BY started_at DESC LIMIT ?",
            (MIN_TOOLCALLS, cutoff, 5000)).fetchall()
        conn.close()
    except Exception as exc:
        log.warning("dreaming: Hermes-Datenbank nicht lesbar (%s) — fail-open",
                    exc)
        return out
    for sid, started, msgs, tools in rows:
        out.append({"id": sid, "started_at": started,
                    "message_count": msgs, "tool_calls": tools,
                    "type": "hermes", "db": db_path})
    return out


def _iter_jsonl_sessions(dir_path: str, cutoff: float) -> List[Dict[str, Any]]:
    """Generische JSONL-Quelle: die zuletzt geänderten Dateien als Sitzungen."""
    out: List[Dict[str, Any]] = []
    try:
        d = Path(dir_path)
        if not d.is_dir():
            return out
        for f in sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime,
                        reverse=True)[:500]:
            if f.stat().st_mtime >= cutoff:
                out.append({"id": f.stem, "path": str(f),
                            "started_at": f.stat().st_mtime,
                            "tool_calls": MIN_TOOLCALLS, "type": "jsonl"})
    except Exception as exc:
        log.warning("dreaming: JSONL-Quelle nicht lesbar (%s) — fail-open", exc)
    return out


# ── Marker und gelernte Störungen ────────────────────────────────────

def _marker_path(base: Path) -> Path:
    return base / ".dreaming-last-run"


def _read_marker(base: Path) -> set:
    try:
        return set(_marker_path(base).read_text(encoding="utf-8").splitlines())
    except Exception:
        return set()


def _write_marker(ids: List[str], base: Path) -> None:
    try:
        marker = _marker_path(base)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("\n".join(ids), encoding="utf-8")
    except Exception as exc:
        log.warning("dreaming: Marker nicht geschrieben (%s)", exc)


def _new_session_ids(ids: List[str], base: Path) -> List[str]:
    """Reine Prüfung ohne Nebenwirkung: ids, die im Marker fehlen.

    Der Marker wird erst nach einem erfolgreichen Lauf geschrieben
    (Review-Fund v0.22.1: ein Fehlversuch oder ein dry_run verbrannte die
    Sitzungen sonst dauerhaft).
    """
    seen = _read_marker(base)
    return [i for i in ids if i not in seen]


def _learned_path(base: Path) -> Path:
    return base / ".dreaming-learned.json"


def _read_learned(base: Path) -> Dict[str, Dict[str, float]]:
    """Lernstand je Senke: {"file": {...}, "store": {...}}.

    Getrennt, weil die Datei bei abgeschaltetem Speicher trotzdem
    geschrieben wird: eine heute nur abgelegte Störung muss am Tag nach dem
    Einschalten noch in den Speicher dürfen.
    """
    leer: Dict[str, Dict[str, float]] = {"file": {}, "store": {}}
    try:
        roh = json.loads(_learned_path(base).read_text(encoding="utf-8"))
    except Exception:
        return leer
    if not isinstance(roh, dict):
        return leer
    if all(isinstance(v, (int, float)) for v in roh.values()):
        # Alter Stand: eine flache Landkarte = Speicher-Senke.
        return {"file": {}, "store": {str(k): float(v) for k, v in roh.items()}}
    for senke in ("file", "store"):
        werte = roh.get(senke) or {}
        if isinstance(werte, dict):
            leer[senke] = {str(k): float(v) for k, v in werte.items()
                           if isinstance(v, (int, float))}
    return leer


def _write_learned(learned: Dict[str, Dict[str, float]], base: Path) -> None:
    try:
        pfad = _learned_path(base)
        pfad.parent.mkdir(parents=True, exist_ok=True)
        pfad.write_text(json.dumps(learned, ensure_ascii=False, indent=1),
                        encoding="utf-8")
    except Exception as exc:
        log.warning("dreaming: Lernstand nicht geschrieben (%s)", exc)


# ── Vorfälle aus einer Sitzung ───────────────────────────────────────

def _session_incidents(session: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Vorfälle einer Sitzung: nur strukturelle Fehlschläge mit Signatur."""
    vorfaelle: List[Dict[str, Any]] = []
    try:
        if session["type"] == "hermes":
            conn = sqlite3.connect(f"file:{session['db']}?mode=ro", uri=True)
            zeilen = conn.execute(
                "SELECT tool_name, content, timestamp FROM messages "
                "WHERE session_id = ? AND role = 'tool' ORDER BY timestamp",
                (session["id"],)).fetchall()
            conn.close()
        else:
            zeilen = []
            pfad = Path(session["path"])
            for nummer, zeile in enumerate(
                    pfad.read_text(encoding="utf-8", errors="replace")
                    .splitlines()):
                try:
                    eintrag = json.loads(zeile)
                except Exception:
                    continue
                if not isinstance(eintrag, dict):
                    continue
                zeilen.append((eintrag.get("tool_name") or eintrag.get("tool"),
                               eintrag.get("content"),
                               eintrag.get("timestamp") or nummer))
    except Exception as exc:
        log.info("dreaming: Sitzung %s nicht lesbar (%s)", session["id"], exc)
        return vorfaelle

    for tool, content, ts in zeilen:
        fehler, text, hint = _outcome(content)
        if not fehler:
            continue
        signatur = _signatur(str(tool or ""), text)
        if not signatur:
            continue
        vorfaelle.append({"signatur": signatur, "tool": str(tool or ""),
                          "hint": hint, "ts": float(ts or 0),
                          "session_id": session["id"],
                          "klasse": signatur.split(":")[1]})
    return vorfaelle


def _lesson_text(signatur: str, anzahl: int, letzte: float,
                 hint: str) -> Optional[str]:
    """Der Satz, der ins Gedächtnis geht — gebaut aus Belegen, ohne Modell."""
    if not _ist_lehrreich(signatur, hint):
        return None
    teile = signatur.split(":")
    tool = teile[0]
    klasse = teile[1] if len(teile) > 1 else "Fehler"
    token = ":".join(teile[2:]) if len(teile) > 2 else ""
    datum = time.strftime("%d.%m.%Y", time.localtime(letzte)) if letzte else "?"
    was = f"{tool} scheitert mit {klasse}" + (f" ({token})" if token else "")
    satz = (f"Wiederkehrende Störung, {anzahl} Sitzungen bis {datum}: {was}.")
    if hint:
        kurz = " ".join(hint.split())[:300]
        satz += f" Abhilfe laut Umgebung: {kurz}"
        if not satz.endswith("."):
            satz += "."
    else:
        satz += " Bisher ohne belegte Abhilfe."
    return " ".join(satz.split())


class Dreamer:
    """Ein Traumdurchlauf: wiederkehrende Störungen mit Beleg lernen."""

    def __init__(self, store=None, llm_fn: Optional[Callable[[str], str]] = None):
        self._store = store
        # llm_fn wird nicht mehr befragt. Es entscheidet nicht mehr, was ein
        # Muster ist, und formuliert keine Lehre: ein Modell, das aus einem
        # Textfenster einen Merksatz baut, hat genau den Müll erzeugt, den
        # wir 20.11.2026 zurückziehen mussten. Der Parameter bleibt, damit
        # bestehende Aufrufer nicht brechen.
        self._llm_fn = llm_fn

    def _similar_known(self, text: str) -> bool:
        """Dublettenprüfung: ein Vector-Treffer über der Schwelle = bekannt."""
        if self._store is None:
            return False
        try:
            # 20.11.2026: hier stand ``self._store._embed([text])[0]``.
            # ``MemoryStore._embed`` ist async und nimmt EINEN Text, kein
            # Liste — der Aufruf lieferte ein Coroutine-Objekt, warf im
            # nächsten Schritt und wurde vom except still gefangen. Die
            # Dublettenprüfung lief damit nie; im Dienst stand nur eine
            # RuntimeWarning im Log. Jetzt derselbe Weg wie beim Schreiben.
            vec = _embed_sync(self._store, text)
            qp_kwargs = {
                "collection_name": _store_collection(self._store),
                "query": vec,
                "limit": 1,
            }
            vector_name = getattr(self._store, "vector_name", None)
            if vector_name:
                qp_kwargs["using"] = vector_name
            resp = self._store.client.query_points(**qp_kwargs)
            hits = resp.points
            if hits and hits[0].score >= 0.92:
                return True
        except Exception as exc:
            log.info("dreaming: Dublettenprüfung übersprungen (%s)", exc)
        return False

    def _store_playbook(self, playbook: Dict[str, Any]) -> bool:
        """Schreibt eine gelernte Lehre als Eintrag in den Speicher.

        Kategorie ``procedure``, weil es Handlungswissen ist, und durch
        dasselbe Schreibtor wie jeder Agent (20.11.2026).
        """
        if self._store is None:
            return False
        import uuid
        from qdrant_client.models import PointStruct
        from nexus_memory.collection_vectors import upsert_named

        muster = str(playbook.get("lesson") or playbook.get("pattern") or "")
        muster = " ".join(muster.split()).strip()
        zusatz = str(playbook.get("verdict") or "").strip()
        if not muster:
            return False
        text = muster + (f" — {zusatz}" if zusatz else "")
        text = text[:900]

        from nexus.write_gate import prepare as _gate, last_reason as _gate_reason
        _prepared = _gate(
            content=text, category="procedure", access_level="private",
            source="nexus-dreaming", source_type="dreaming",
            provenance={"source_type": "dreaming",
                        "created_by": "dreaming-v2"},
        )
        if _prepared is None:
            log.info("dreaming: Schreibtor weist ab (%s) — Lehre nicht gespeichert",
                     _gate_reason() or "refused")
            return False
        text = _prepared["content"]

        vec = _embed_sync(self._store, text)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        point = PointStruct(
            id=str(uuid.uuid4()), vector=vec,
            payload={"content": text, "text": text, "category": "procedure",
                     "access_level": "private", "scope": "default",
                     "source": "nexus-dreaming", "created_at": now,
                     # Verfahren statt Verfasser: der Traum fasst Sitzungen
                     # mehrerer Agenten zusammen (siehe consolidation.py).
                     "writer": "dreaming",
                     "lifecycle_status": "canonical", "confidence": 0.6,
                     "provenance": {"source_type": "dreaming",
                                    "created_by": "dreaming-v2",
                                    "signature": playbook.get("signature"),
                                    "sessions": playbook.get("sessions"),
                                    "session_id": playbook.get("session_id"),
                                    "timestamp": now}},
        )
        sammlung = _store_collection(self._store)
        if not sammlung:
            log.warning("dreaming: Sammlungsname unbekannt — Eintrag nicht geschrieben")
            return False
        upsert_named(self._store.client, sammlung, [point],
                     getattr(self._store, "vector_name", None))
        return True

    def dream(self, dry_run: bool = False) -> Dict[str, Any]:
        """Ein Durchlauf. Gibt eine Zusammenfassung zurück, wirft nie."""
        result: Dict[str, Any] = {"sessions_scanned": 0, "incidents": 0,
                                  "recurring": 0, "patterns": 0,
                                  "playbooks": 0, "dry_run": dry_run}
        if not NEXUS_DREAMING_ENABLED or os.environ.get("NEXUS_DREAMING", "1") != "1":
            result["skipped"] = "disabled"
            return result
        try:
            cutoff = time.time() - _lookback_hours() * 3600
            sessions: List[Dict[str, Any]] = []
            for src in _session_sources():
                if src.get("type") == "hermes":
                    sessions.extend(_iter_hermes_sessions(src["db"], cutoff))
                elif src.get("type") == "jsonl":
                    sessions.extend(_iter_jsonl_sessions(src["dir"], cutoff))
            result["sessions_scanned"] = len(sessions)
            journal_vorfaelle: List[Dict[str, Any]] = []
            if str(os.environ.get("NEXUS_DREAMING_JOURNAL", "1")).strip().lower() \
                    not in ("0", "false", "no", "off"):
                journal_vorfaelle, gesehen = _journal_incidents(cutoff)
                result["events_scanned"] = gesehen
            if not sessions and not journal_vorfaelle:
                return result
            base = PLAYBOOK_DIR
            result["new_sessions"] = len(
                _new_session_ids([s["id"] for s in sessions], base))

            # Zählung über alle Sitzungen im Fenster: Wiederkehr ist eine
            # Tatsache über die Zeit, nicht über einen einzelnen Lauf.
            je_signatur: Dict[str, Dict[str, Any]] = {}
            alle_vorfaelle: List[Dict[str, Any]] = list(journal_vorfaelle)
            for s in sessions:
                alle_vorfaelle.extend(_session_incidents(s))
            for vorfall in alle_vorfaelle:
                eintrag = je_signatur.setdefault(
                    vorfall["signatur"],
                    {"sessions": set(), "hint": "", "last_ts": 0.0,
                     "last_session": None})
                eintrag["sessions"].add(vorfall["session_id"])
                if vorfall["hint"] and not eintrag["hint"]:
                    eintrag["hint"] = vorfall["hint"]
                if vorfall["ts"] >= eintrag["last_ts"]:
                    eintrag["last_ts"] = vorfall["ts"]
                    eintrag["last_session"] = vorfall["session_id"]
            result["incidents"] = sum(len(v["sessions"])
                                      for v in je_signatur.values())
            wiederkehrend = {sig: v for sig, v in je_signatur.items()
                             if len(v["sessions"]) >= max(2, MIN_SESSIONS)}
            result["recurring"] = len(wiederkehrend)

            lernstand = _read_learned(base)
            speicher_an = _store_enabled()
            # Bei eingeschaltetem Speicher zählt, was schon im Speicher
            # steht; sonst zählt, was schon als Datei vorliegt.
            letzte_senke = lernstand["store" if speicher_an else "file"]
            jetzt = time.time()
            lehren: List[Dict[str, Any]] = []
            for signatur, info in sorted(
                    wiederkehrend.items(),
                    key=lambda kv: -len(kv[1]["sessions"])):
                letzte = letzte_senke.get(signatur)
                if letzte and (jetzt - letzte) < RELEARN_DAYS * 86400:
                    continue
                text = _lesson_text(signatur, len(info["sessions"]),
                                    info["last_ts"], info["hint"])
                if not text:
                    continue
                if not dry_run and self._similar_known(text):
                    continue
                lehren.append({"lesson": text, "signature": signatur,
                               "sessions": len(info["sessions"]),
                               "session_id": info["last_session"],
                               "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})
                if len(lehren) >= MAX_LESSONS_PER_RUN:
                    break

            if lehren and not dry_run:
                geschrieben = 0
                erledigt: List[str] = []
                if speicher_an:
                    for lehre in lehren:
                        try:
                            if self._store_playbook(lehre):
                                geschrieben += 1
                                erledigt.append(lehre["signature"])
                        except Exception as exc:
                            log.info("dreaming: Lehre nicht geschrieben (%s)", exc)
                else:
                    result["store_skipped"] = (
                        "NEXUS_DREAM_STORE=0 — Lehren nur als Datei")
                    erledigt = [l["signature"] for l in lehren]
                result["stored"] = geschrieben
                base.mkdir(parents=True, exist_ok=True)
                aus = base / f"dream-{time.strftime('%Y%m%d-%H%M%S')}.json"
                aus.write_text(json.dumps(lehren, ensure_ascii=False, indent=1),
                               encoding="utf-8")
                result["file"] = str(aus)
                for signatur in erledigt:
                    lernstand["store" if speicher_an else "file"][signatur] = jetzt
                _write_learned(lernstand, base)
            elif lehren:
                result["would_store"] = [l["lesson"] for l in lehren]
            if not dry_run:
                _write_marker([s["id"] for s in sessions], base)
            result["patterns"] = len(lehren)
            result["playbooks"] = 1 if lehren and not dry_run else 0
        except Exception as exc:
            log.warning("dreaming: Durchlauf fehlgeschlagen (%s) — fail-open", exc)
            result["error"] = str(exc)[:200]
        return result


def _store_enabled() -> bool:
    """Dürfen gelernte Lehren in den Speicher?

    Standard aus (NEXUS_DREAM_STORE=0), bis die Erkennung an echten Daten
    gemessen ist. Die JSON-Datei entsteht immer.
    """
    return str(os.environ.get("NEXUS_DREAM_STORE", "0")).strip().lower() in (
        "1", "true", "yes", "on")


def dream_once(store=None, llm_fn=None, dry_run: bool = False) -> Dict[str, Any]:
    """Einstiegspunkt für die Daemon-Schleife oder den Handaufruf."""
    return Dreamer(store=store, llm_fn=llm_fn).dream(dry_run=dry_run)
