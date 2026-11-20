"""Neutrale Ereignis-Aufnahme (v0.22.61).

Warum es das gibt
-----------------
Das nächtliche Lernen (``dreaming``) braucht Vorfälle: Werkzeugaufrufe, die
gescheitert sind, mit dem Hinweis der Umgebung. Bis 0.22.60 las es dafür die
Sitzungsdatenbank von Hermes. Damit hing das Langzeitgedächtnis an einem
bestimmten Harness — genau die Kopplung, die es nicht geben soll: Nexus
Memory muss ohne Hermes, OpenClaw, Claude Code oder DSH laufen.

Deshalb dreht die Richtung jetzt um: **Nexus nimmt an, was ein Agent ihm
schickt.** Jeder Harness, der etwas gelernt haben will, meldet seinen Vorfall
hier an (``POST /events`` oder ``record()`` in-process). Nexus klassifiziert
selbst, prüft die Wiederkehr und entscheidet, was ins Gedächtnis geht. Kein
Harness wird gelesen, keiner wird gebraucht; wer nichts schickt, dessen
Vorfälle fehlen einfach.

Format einer Zeile (JSONL, eine Zeile je Vorfall)::

    {"v": 1, "ts": 1795191353.5, "agent": "hermes", "tool": "terminal",
     "ok": false, "error": "ModuleNotFoundError: No module named 'requests'",
     "hint": "Wrong interpreter: use the project venv.", "session": "local-abc"}

``ok`` false heißt: der Aufruf ist gescheitert. Die Klassifikation passiert
beim Lesen (``nexus.dreaming``), nicht hier — so lässt sie sich später
verbessern, ohne dass alte Zeilen falsch etikettiert bleiben.

Ablage: ``~/.nexus-memory/events/ereignisse.jsonl`` (``NEXUS_EVENT_DIR``).
Kein Qdrant, kein Schema-Umbau: das Journal ist Text, von Hand lesbar und
im Zweifel löschbar ohne Datenverlust im Gedächtnis.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

log = logging.getLogger("nexus.events")

EVENT_DIR = Path(os.environ.get(
    "NEXUS_EVENT_DIR", str(Path.home() / ".nexus-memory" / "events")))
JOURNAL = EVENT_DIR / "ereignisse.jsonl"

# Grenzen, damit eine fehlerhafte Meldung das Journal nicht flutet.
MAX_ZEILENLAENGE = 8000
MAX_FEHLERTEXT = 2000
MAX_HINT = 600
MAX_ROTATE_BYTES = 20 * 1024 * 1024

_SCHREIB_SPERRE = threading.Lock()


def _kuerzen(wert: Any, grenze: int) -> str:
    text = " ".join(str(wert or "").split())
    return text[:grenze]


def normalisieren(eintrag: Any) -> Optional[Dict[str, Any]]:
    """Macht aus einer Meldung eine gültige Journal-Zeile oder ``None``.

    Pflicht ist nur ``tool``: ohne Werkzeug gibt es nichts zu lernen. Alles
    andere wird gekürzt und, wenn es fehlt, leer gelassen.
    """
    if not isinstance(eintrag, dict):
        return None
    tool = _kuerzen(eintrag.get("tool"), 64)
    if not tool:
        return None
    try:
        ts = float(eintrag.get("ts"))
    except (TypeError, ValueError):
        ts = time.time()
    if ts <= 0 or ts > time.time() + 300:  # Zukunft ist kein Beleg
        ts = time.time()
    return {
        "v": 1,
        "ts": round(ts, 3),
        "agent": _kuerzen(eintrag.get("agent"), 64) or "unbekannt",
        "tool": tool,
        "ok": bool(eintrag.get("ok", True)),
        "error": _kuerzen(eintrag.get("error"), MAX_FEHLERTEXT),
        "hint": _kuerzen(eintrag.get("hint"), MAX_HINT),
        "session": _kuerzen(eintrag.get("session"), 128),
    }


def journal_path() -> Path:
    """Pfad des Journals, bei jedem Aufruf aus der Umgebung gelesen.

    Damit lässt sich ein anderer Ablageort (Tests, zweite Installation)
    setzen, ohne das Modul neu zu importieren.
    """
    env = os.environ.get("NEXUS_EVENT_DIR")
    if env:
        return Path(env) / "ereignisse.jsonl"
    return JOURNAL


def _datei() -> Path:
    pfad = journal_path()
    pfad.parent.mkdir(parents=True, exist_ok=True)
    return pfad


def _rotieren(pfad: Path) -> None:
    """Ab 20 MB: weg mit dem alten, das neue Journal beginnt leer.

    Gelerntes steht im Gedächtnis, das Journal ist nur Beleg. Eine Rotation
    kostet höchstens, dass eine sehr alte Störung erneut zählt.
    """
    try:
        if pfad.exists() and pfad.stat().st_size > MAX_ROTATE_BYTES:
            ziel = pfad.with_suffix(".1.jsonl")
            if ziel.exists():
                ziel.unlink()
            pfad.rename(ziel)
            log.info("events: Journal rotiert nach %s", ziel)
    except Exception as exc:
        log.warning("events: Rotation fehlgeschlagen (%s)", exc)


def record(agent: str, tool: str, ok: bool, error: str = "",
           hint: str = "", session: str = "",
           ts: Optional[float] = None) -> bool:
    """Ein Vorfall ins Journal. Gibt ``True`` bei Erfolg, wirft nie."""
    eintrag = normalisieren({"agent": agent, "tool": tool, "ok": ok,
                             "error": error, "hint": hint,
                             "session": session, "ts": ts})
    if eintrag is None:
        return False
    return write_many([eintrag]) == 1


def write_many(eintraege: Iterable[Any]) -> int:
    """Mehrere Vorfälle anhängen. Gibt die Zahl der geschriebenen Zeilen."""
    zeilen: List[str] = []
    for roh in eintraege:
        eintrag = roh if isinstance(roh, dict) and "v" in roh else normalisieren(roh)
        if eintrag is None:
            continue
        zeile = json.dumps(eintrag, ensure_ascii=False)
        if len(zeile) > MAX_ZEILENLAENGE:
            continue
        zeilen.append(zeile)
    if not zeilen:
        return 0
    with _SCHREIB_SPERRE:
        try:
            pfad = _datei()
            _rotieren(pfad)
            with open(pfad, "a", encoding="utf-8") as fh:
                try:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                except Exception:
                    pass  # Windows oder exotisches Dateisystem
                fh.write("\n".join(zeilen) + "\n")
                fh.flush()
                try:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass
            return len(zeilen)
        except Exception as exc:
            log.warning("events: Journal nicht schreibbar (%s)", exc)
            return 0


def iter_events(since_ts: float = 0.0,
                path: Optional[Path] = None) -> Iterator[Dict[str, Any]]:
    """Alle Vorfälle ab ``since_ts``. Kaputte Zeilen werden übersprungen."""
    pfad = path or journal_path()
    if not pfad.exists():
        return
    try:
        with open(pfad, "r", encoding="utf-8", errors="replace") as fh:
            for zeile in fh:
                zeile = zeile.strip()
                if not zeile:
                    continue
                try:
                    eintrag = json.loads(zeile)
                except Exception:
                    continue
                if not isinstance(eintrag, dict):
                    continue
                try:
                    ts = float(eintrag.get("ts") or 0)
                except (TypeError, ValueError):
                    continue
                if ts < since_ts:
                    continue
                yield eintrag
    except Exception as exc:
        log.warning("events: Journal nicht lesbar (%s)", exc)


def stats() -> Dict[str, Any]:
    """Kleine Auskunft für Health und Diagnose."""
    pfad = journal_path()
    if not pfad.exists():
        return {"path": str(pfad), "exists": False, "lines": 0, "bytes": 0}
    zeilen = 0
    try:
        with open(pfad, "r", encoding="utf-8", errors="replace") as fh:
            for _ in fh:
                zeilen += 1
    except Exception:
        pass
    return {"path": str(pfad), "exists": True, "lines": zeilen,
            "bytes": pfad.stat().st_size}
