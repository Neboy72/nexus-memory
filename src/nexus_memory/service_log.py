"""Bewegungsprotokoll des Gedaechtnisses — wer hat wann was getan.

Warum (15.11.2026): Es gab nur ein technisches Zugriffslog
(``logs/serve.log``, 44.686 Zeilen "POST /embed 200") und keine Spur von
Aenderungen. Wer 628 Eintraege nachgetragen hat, war nirgends festgehalten —
bei einem fehlenden, falschen oder verschwundenen Eintrag liess sich nicht in
Sekunden nachsehen, wer ihn angelegt, geaendert oder geloescht hat.

Das Protokoll haelt deshalb die BEWEGUNG fest, nicht das Wissen: eine Zeile
JSON je Vorgang, angehaengt an ``service-YYYY-MM-DD.jsonl`` im Ordner aus
``NEXUS_LOG_DIR`` (Vorgabe ``~/.nexus-memory/logs``).

Kein Inhalt, nie:

* der volle Text steht nicht drin — nur ein kurzes Thema (max. 40 Zeichen),
* Zahlenwerte aus dem Inhalt werden durch ``…`` ersetzt (eine Zahl ist oft
  genau die Angabe, die geschuetzt werden soll: Tuer-Code, Betrag, Stand),
* bei ``private`` bleibt auch das Thema leer.

Ein Fehler beim Schreiben darf den eigentlichen Vorgang nie verhindern:
``record`` schluckt jede Ausnahme und meldet sie auf stderr. Die Hooks laufen
parallel, deshalb ist das Anhaengen gesperrt (thread-sicher).

Pruefsummen statt Inhalt (15.11.2026): ``before_hash`` und ``after_hash``
tragen die ersten 16 Zeichen des SHA-256 von Inhalt vor und nach der
Aenderung. Damit ist spaeter beweisbar, dass sich ein Eintrag geaendert hat
und ob zwei Staende gleich sind, ohne den Text zu speichern. ``write`` traegt
nur ``after_hash``, ``update`` beide, ``forget`` nur ``before_hash`` (der
Inhalt ist danach weg), ``denied`` keins von beiden.

Nachtrag (15.11.2026): die letzten Wege, die einen Punkt anlegten oder
ueberschrieben, ohne eine Zeile zu hinterlassen — Konsolidierung (``via:
consolidation``), SICA (``sica``), Staging (``staging``), der Restore-Weg des
Dienstes (``action: restore``) und der Guardrail-Override (``action:
guardrail_override``, ``via: guard``) — schreiben jetzt ebenfalls genau eine
Zeile je Vorgang.

Urheber (15.11.2026): ein Systemvorgang hat keinen Agenten, aber einen
Urheber — er traegt ``system:<bereich>`` (``system:consolidation``,
``system:sica``, ``system:staging``, ``system:guard``, ``system:restore``).
``unidentified`` bleibt fuer echte Fremdzugriffe ohne Absender; dort ist es
eine ehrliche Aussage und kein Fehler.

Schluesselart (15.11.2026): eine Zeile aus dem HTTP-Dienst traegt ``key`` —
``own``, wenn der Aufrufer den eigenen Schluessel eines Agenten vorlegte,
``shared`` beim globalen. Damit ist spaeter ablesbar, wer schon umgestellt ist.
Ausserhalb des Dienstes (Hooks, CLI, Bibliothek) fehlt das Feld: dort wurde
kein Schluessel vorgelegt.

Kette (15.11.2026): jede Zeile traegt ``prev`` — die Pruefsumme der vorherigen
Zeile der Datei, in der ersten Zeile ``"genesis"``. Eine geaenderte, geloeschte
oder eingeschobene Zeile bricht die Kette an genau dieser Stelle;
``verify_chain`` nennt die Zeilennummer, ``--verify`` zeigt sie an.

Kennung und Label (15.11.2026): ``actor`` ist die Kennung, ``actor_label`` das
Label, das der Agent ZUM ZEITPUNKT der Handlung trug. Ein spaeter geaendertes
Label schreibt die Historie nicht um — die alte Zeile bleibt lesbar, wie sie
gelesen wurde. Zeilen ohne ``actor_label`` (Bestand, Systemvorgaenge ohne
Registereintrag) bleiben unveraendert.

Lesen: ``read_events`` (juengste zuerst), ``verify_chain`` und die CLI
``python -m nexus_memory.service_log --tail 20 [--action forget] [--actor ayosha]``
sowie ``python -m nexus_memory.service_log --verify [--date YYYY-MM-DD]``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from nexus_memory import service_auth

#: Ordner des Protokolls (Umgebungsvariable, sonst ``~/.nexus-memory/logs``).
LOG_DIR_ENV = "NEXUS_LOG_DIR"
FILE_PREFIX = "service-"
FILE_SUFFIX = ".jsonl"

#: Eine Zeile darf so lang werden — mehr ist kein Protokolleintrag mehr.
MAX_LINE_CHARS = 400
#: Laenge des Themas. Kurz genug, um nichts wiederzugeben.
TOPIC_MAX_CHARS = 40
#: Ausnahmetyp und erste Meldungszeile bei ``error``.
DETAIL_MAX_CHARS = 200
#: So lange bleiben Tagesdateien liegen. Neunzig Tage, weil dreissig fuer
#: "was war vor zwei Monaten" nicht reichen.
RETENTION_DAYS = 90
#: Laenge einer Pruefsumme (erste Zeichen des SHA-256).
HASH_CHARS = 16
#: ``prev`` der ersten Zeile einer Datei.
GENESIS = "genesis"

ACTIONS = frozenset({
    "write", "update", "forget", "backup", "restore",
    "role_change", "key_change", "denied", "error",
    # Nachtrag 15.11.2026: der Guardrail-Override legt einen Punkt an und war
    # damit der letzte Schreibweg ohne eigene Zeile.
    "guardrail_override",
    # Melden statt Ueberschreiben (15.11.2026): die Meldung haengt am Eintrag,
    # die Entscheidung darueber ebenfalls — beides bekommt eine Zeile.
    "report", "resolve_report",
})
RESULTS = frozenset({"ok", "refused", "failed"})
VIAS = frozenset({
    "mcp", "http", "library", "cli", "plugin", "hook",
    # Nachtrag 15.11.2026: die Wege, die bisher ohne Zeile schrieben.
    "consolidation", "guard", "sica", "staging",
})

#: Der Urheber eines Systemvorgangs (Nachtrag 15.11.2026). Ein Systemvorgang
#: hat keinen Agenten, aber einen Bereich, der ihn ausgeloest hat: der
#: Aufraeumlauf ist der Aufraeumlauf. ``unidentified`` war dort die falsche
#: Aussage — der Urheber war bekannt, er war nur kein Agent. ``restore``
#: nimmt die Kennung des Aufrufers und faellt erst ohne Aufrufer hierher.
SYSTEM_ACTORS = {
    "consolidation": "system:consolidation",
    "sica": "system:sica",
    "staging": "system:staging",
    "guard": "system:guard",
    "restore": "system:restore",
}

#: Zahlenwerte aus dem Inhalt — Betrag, Stand, Tuer-Code, Datum. Die Ziffern
#: samt Trennern dazwischen sind EIN Wert: "15.11.2026" wird zu einem "…",
#: nicht zu drei Stuecken. Ein Punkt am Satzende gehoert nicht dazu.
_NUMBER = re.compile(r"\d(?:[\d.,:/\-]*\d)?")
_WHITESPACE = re.compile(r"\s+")

#: Die Hooks laufen parallel; eine Zeile muss in einem Stueck ankommen.
_lock = threading.Lock()
#: Tag, an dem dieser Prozess zuletzt rotiert hat (Rotation einmal je Tag).
_rotated_on: Optional[date] = None


# ── Ort und Tag ──────────────────────────────────────────────────────


def log_dir() -> Path:
    """Der Protokollordner — bei jedem Aufruf frisch aus der Umgebung.

    Nicht beim Import gebunden: Tests und Dienstprofile setzen
    ``NEXUS_LOG_DIR`` zur Laufzeit.
    """
    raw = os.environ.get(LOG_DIR_ENV, "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".nexus-memory" / "logs"


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _file_day(path: Path) -> Optional[date]:
    """Tag einer Tagesdatei — aus dem Namen, sonst aus der Aenderungszeit."""
    name = path.name
    if name.startswith(FILE_PREFIX) and name.endswith(FILE_SUFFIX):
        stem = name[len(FILE_PREFIX):-len(FILE_SUFFIX)]
        try:
            return datetime.strptime(stem, "%Y-%m-%d").date()
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).date()
    except OSError:
        return None


# ── Pruefsummen ──────────────────────────────────────────────────────


def _hash16(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:HASH_CHARS]


def content_hash(value: Any) -> str:
    """Pruefsumme eines Inhalts (erste 16 Zeichen) — ``""`` bei leerem Inhalt.

    Der Aufrufer haelt den Text ohnehin in der Hand; gespeichert wird nur die
    Pruefsumme. Sie belegt spaeter, DASS sich ein Eintrag geaendert hat und ob
    zwei Staende gleich sind, ohne den Text abzuschreiben.
    """
    if not isinstance(value, str) or not value:
        return ""
    return _hash16(value)


def _line_hash(entry: dict) -> str:
    """Pruefsumme einer Zeile ueber ihre Felder OHNE ``prev``.

    ``sort_keys`` und feste Trenner machen die Kette reproduzierbar: dieselben
    Felder ergeben immer dieselbe Pruefsumme, unabhaengig von der Reihenfolge,
    in der sie gebaut wurden. Genau so prueft ``verify_chain`` sie nach.
    """
    body = {key: value for key, value in entry.items() if key != "prev"}
    return _hash16(json.dumps(body, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":")))


# ── Inhalt, der nicht ins Protokoll gehoert ──────────────────────────


def _topic(text: Any, access_level: Any) -> str:
    """Kurzes Thema aus dem Text — leer bei ``private``.

    Die Datenschutz-Zusicherung dieses Moduls: ein privater Eintrag hinterlaesst
    im Protokoll kein Thema. Eine unbekannte oder fehlende Stufe zaehlt wie
    ``private`` (im Zweifel nichts preisgeben). Zahlenwerte werden durch ``…``
    ersetzt, weil eine Zahl im Text oft die Angabe selbst ist.
    """
    level = str(access_level or "").strip().lower()
    if not level or level == "private":
        return ""
    topic = _NUMBER.sub("…", _WHITESPACE.sub(" ", str(text or "")).strip())
    if len(topic) > TOPIC_MAX_CHARS:
        topic = topic[: TOPIC_MAX_CHARS - 1].rstrip() + "…"
    return topic.strip()


def _detail(exc: BaseException) -> str:
    """Ausnahmetyp und erste Meldungszeile — gekuerzt, nie ein Stacktrace."""
    message = str(exc).strip()
    first = message.splitlines()[0].strip() if message else ""
    text = f"{type(exc).__name__}: {first}" if first else type(exc).__name__
    return text[:DETAIL_MAX_CHARS]


def _actor(explicit: Any, via: str = "", action: str = "") -> str:
    """Wer den Vorgang ausgeloest hat — nie leer.

    Reihenfolge: die ausdrueckliche Angabe des Aufrufers, dann der Bereich
    eines Systemvorgangs (``system:<bereich>`` aus ``SYSTEM_ACTORS``), dann
    ``NEXUS_AGENT_ID`` (wie ``resolve_agent_id``), zuletzt ``unidentified``.

    Ein Systemvorgang hat keinen Agenten: der Aufraeumlauf ist der
    Aufraeumlauf, auch wenn in seinem Prozess zufaellig eine Agentenkennung
    gesetzt waere. ``unidentified`` bleibt fuer echte Fremdzugriffe ohne
    Absender — das ist dann eine ehrliche Aussage und kein Fehler.
    """
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    area = SYSTEM_ACTORS.get(via)
    if area:
        return area
    try:
        from nexus_memory.agent_detect import UNIDENTIFIED_AGENT_ID, resolve_agent_id

        agent = resolve_agent_id(None)
    except Exception:
        UNIDENTIFIED_AGENT_ID = "unidentified"
        agent = os.environ.get("NEXUS_AGENT_ID", "").strip() or UNIDENTIFIED_AGENT_ID
    if agent != UNIDENTIFIED_AGENT_ID:
        return agent
    # Erst hier: ein Restore ohne Aufrufer ist ein Vorgang des Dienstes.
    return SYSTEM_ACTORS.get(action, agent)


def _actor_label(actor_id: str) -> str:
    """Das Label des Agenten ``actor_id`` — Snapshot, nie erfunden.

    Gelesen wird direkt aus ``agents.json`` (ohne Sperre: die Datei wird
    atomar ersetzt, ein Leser sieht also immer einen vollstaendigen Stand).
    Eine Sperre hier waere sogar gefaehrlich — wer protokolliert, waehrend er
    die Registersperre haelt, wartete auf sich selbst.

    Kein Eintrag, kein Label, kaputtes Verzeichnis: das Feld bleibt weg. Eine
    leere Zeile ist ehrlicher als ein geratener Name.
    """
    if not actor_id:
        return ""
    try:
        from nexus_memory import agent_detect

        registry = agent_detect._load_registry_unlocked()
    except Exception:  # noqa: BLE001 — fail-open, das Protokoll laeuft weiter
        return ""
    agents = registry.get("agents") if isinstance(registry, dict) else None
    for entry in agents or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("id") or "").strip() != actor_id:
            continue
        label = entry.get("name")
        if isinstance(label, str) and label.strip():
            return label.strip()[:64]
        return ""
    return ""


# ── Eine Zeile bauen ─────────────────────────────────────────────────


def _build(action: str, result: str, text: Any, access_level: Any,
           target_id: Any, category: Any, scope: Any, reason: Any,
           detail: Any, via: str, actor: Any,
           before_hash: Any = None, after_hash: Any = None) -> dict:
    """Der Eintrag in der Feldreihenfolge der Spezifikation.

    ``ts``, ``actor``, ``action`` und ``result`` stehen immer; ``target_id``
    steht immer (``null``, wenn unbekannt); die uebrigen Felder nur, wenn sie
    etwas sagen — eine kurze Zeile ist im Protokoll mehr wert als ein Feld
    voller leerer Werte. ``prev`` setzt ``_append`` beim Schreiben (die
    Pruefsumme der vorherigen Zeile).
    """
    actor_id = _actor(actor, via, action)[:64]
    entry: dict = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "actor": actor_id,
    }
    # Das Label steht neben der Kennung, nicht statt ihr: die Kennung bleibt
    # der Schluessel, das Label die lesbare Beschriftung von damals.
    label = _actor_label(actor_id)
    if label:
        entry["actor_label"] = label
    entry["action"] = action
    entry["result"] = result
    entry["target_id"] = str(target_id) if target_id else None
    # Nur echter Text ergibt ein Thema — ein Dict oder eine Zahl aus einem
    # fehlerhaften Aufruf wird nicht abgeschrieben.
    topic = _topic(text if isinstance(text, str) else "", access_level)
    for key, value in (
        ("category", category),
        ("scope", scope),
        ("topic", topic),
        ("reason", reason),
        ("detail", detail),
        ("before_hash", before_hash),
        ("after_hash", after_hash),
    ):
        if isinstance(value, str):
            value = value.strip()
        if value:
            entry[key] = str(value)
    entry["via"] = via
    # Welche Art Schluessel der HTTP-Aufruf vorlegte (``own`` = eigener
    # Agentenschluessel, ``shared`` = globaler). Ausserhalb des Dienstes wurde
    # keiner vorgelegt — dann bleibt das Feld weg, wie ``via`` es auch tut.
    key_kind = service_auth.request_key_kind()
    if key_kind:
        entry["key"] = key_kind
    return entry


def _dumps(entry: dict) -> str:
    return json.dumps(entry, ensure_ascii=False, separators=(",", ":"))


def _fit(entry: dict) -> str:
    """Eine Zeile, die ``MAX_LINE_CHARS`` nicht ueberschreitet.

    Gekuerzt wird in dieser Reihenfolge: ``detail``, ``reason``, ``topic``,
    ``scope``, ``target_id``, ``actor`` — das Nebensaechlichste zuerst. Die
    Pflichtfelder bleiben unangetastet, damit die Zeile lesbar und gueltiges
    JSON bleibt.
    """
    line = _dumps(entry)
    for key in ("detail", "reason", "topic", "scope", "target_id", "actor_label",
                "actor"):
        if len(line) <= MAX_LINE_CHARS:
            break
        value = entry.get(key)
        if not value:
            continue
        excess = len(line) - MAX_LINE_CHARS
        keep = max(0, len(str(value)) - excess)
        entry[key] = str(value)[:keep] or None
        line = _dumps(entry)
    if len(line) > MAX_LINE_CHARS:  # Pflichtfelder allein sind weit darunter
        # ``prev`` und die Pruefsummen gehoeren zur Pflicht: ohne ``prev``
        # waere die Zeile nicht mehr Teil der Kette.
        entry = {key: entry.get(key) for key in
                 ("ts", "actor", "action", "result", "via", "key", "prev",
                  "before_hash", "after_hash")
                 if entry.get(key) is not None}
        line = _dumps(entry)
    return line


# ── Rotation ─────────────────────────────────────────────────────────


def _rotate(directory: Path, today: Optional[date] = None) -> int:
    """Tagesdateien aelter als ``RETENTION_DAYS`` loeschen; Anzahl zurueck."""
    day = today or _today()
    cutoff = day - timedelta(days=RETENTION_DAYS)
    removed = 0
    try:
        candidates = list(directory.glob(f"{FILE_PREFIX}*{FILE_SUFFIX}"))
    except OSError as exc:
        print(f"nexus service_log: Rotation uebersprungen: {exc}", file=sys.stderr)
        return 0
    for path in candidates:
        when = _file_day(path)
        if when is None or when >= cutoff:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError as exc:
            # Eine Datei, die gerade jemand liest, darf das Protokoll nicht
            # anhalten — beim naechsten Tag wird es erneut versucht.
            print(f"nexus service_log: {path.name} nicht entfernt: {exc}",
                  file=sys.stderr)
    return removed


def _rotate_once_per_day(directory: Path) -> None:
    """Rotieren beim ERSTEN Schreiben eines Tages, nicht bei jeder Zeile."""
    global _rotated_on
    today = _today()
    if _rotated_on == today:
        return
    # Vor dem Lauf setzen: eine kaputte Rotation soll nicht bei jeder Zeile
    # erneut teuer scheitern.
    _rotated_on = today
    _rotate(directory, today)


# ── Schreiben ────────────────────────────────────────────────────────


def _last_prev(path: Path) -> str:
    """Pruefsumme der letzten Zeile der Datei — ``"genesis"``, wenn es keine gibt.

    Gelesen wird bewusst aus der DATEI, nicht aus einem Zwischenspeicher: die
    Hooks, das Plugin und der Dienst sind getrennte Prozesse. Ein Hash im
    Speicher waere nach dem ersten fremden Anhang falsch und wuerde die Kette
    an dieser Stelle spalten. Eine Tagesdatei von wenigen kB zu lesen kostet
    weniger als eine Millisekunde — einmal je Vorgang.
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return GENESIS
    for line in reversed(raw.splitlines()):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            return _line_hash(entry)
    return GENESIS


def _append(entry: dict) -> str:
    """Eine fertige Zeile anhaengen — mit ``prev`` aus der Datei davor."""
    directory = log_dir()
    with _lock:
        directory.mkdir(parents=True, exist_ok=True)
        _rotate_once_per_day(directory)
        path = directory / f"{FILE_PREFIX}{_today().isoformat()}{FILE_SUFFIX}"
        entry["prev"] = _last_prev(path)
        line = _fit(entry)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()
        return line


def record(action: str, result: str = "ok", *, text: Any = "",
           access_level: Any = "", target_id: Any = None, category: Any = None,
           scope: Any = None, reason: Any = None, detail: Any = None,
           before_hash: Any = None, after_hash: Any = None,
           via: str = "library", actor: Any = None) -> Optional[dict]:
    """Einen Vorgang protokollieren — fail-open, wirft nie.

    ``text`` und ``access_level`` bestimmen allein das Thema: bei ``private``
    (und bei unbekannter Stufe) bleibt es leer, und Zahlenwerte aus dem Inhalt
    werden ersetzt. Alles andere am Text wird nicht gespeichert.

    ``before_hash``/``after_hash`` sind Pruefsummen aus
    ``content_hash`` — ``write`` setzt nur ``after_hash``, ``update`` beide,
    ``forget`` nur ``before_hash``, ``denied`` keins.

    Rueckgabe ist der geschriebene Eintrag (mit ``prev``) — oder ``None``, wenn
    das Protokoll nicht geschrieben werden konnte. Der aufrufende Vorgang
    laeuft in beiden Faellen weiter.
    """
    try:
        if action not in ACTIONS:
            raise ValueError(f"unbekannte Aktion {action!r}")
        if result not in RESULTS:
            raise ValueError(f"unbekanntes Ergebnis {result!r}")
        entry = _build(
            action=action,
            result=result,
            text=text,
            access_level=access_level,
            target_id=target_id,
            category=category,
            scope=scope,
            reason=reason,
            detail=detail,
            via=via if via in VIAS else "library",
            actor=actor,
            before_hash=before_hash,
            after_hash=after_hash,
        )
        _append(entry)
        return entry
    except Exception as exc:  # noqa: BLE001 — fail-open ist hier die Regel
        print(f"nexus service_log: Protokoll nicht geschrieben "
              f"({type(exc).__name__}: {exc})", file=sys.stderr)
        return None


def record_error(action: str, exc: BaseException, *, text: Any = "",
                 access_level: Any = "", target_id: Any = None,
                 category: Any = None, scope: Any = None,
                 via: str = "library", actor: Any = None) -> Optional[dict]:
    """Unerwartete Ausnahme als ``error``/``failed`` protokollieren.

    ``action`` ist die Aktion, die gescheitert ist (``write``, ``update``,
    ``forget``); im Protokoll steht sie im Feld ``reason`` (``update
    fehlgeschlagen``), denn ``action`` ist bei einer Ausnahme ``error``.
    ``detail`` traegt Ausnahmetyp und erste Meldungszeile (max. 200 Zeichen) —
    genug, um den Fehler wiederzuerkennen, ohne Inhalte abzuschreiben.
    """
    return record(
        "error", "failed", text=text, access_level=access_level,
        target_id=target_id, category=category, scope=scope,
        reason=f"{action} failed", detail=_detail(exc),
        via=via, actor=actor,
    )


# ── Lesen ────────────────────────────────────────────────────────────


def _parse_since(since: Any) -> Optional[datetime]:
    """``since`` als Zeitpunkt in UTC — ISO-Datum oder -Zeitpunkt."""
    if since is None or since == "":
        return None
    if isinstance(since, datetime):
        value = since
    elif isinstance(since, date):
        value = datetime(since.year, since.month, since.day, tzinfo=timezone.utc)
    else:
        raw = str(since).strip().replace("Z", "+00:00")
        try:
            value = datetime.fromisoformat(raw)
        except ValueError:
            return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _entry_ts(entry: dict) -> Optional[datetime]:
    raw = entry.get("ts")
    if not isinstance(raw, str):
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _parse_day(day: Any) -> Optional[str]:
    """``day`` als ``YYYY-MM-DD`` — ``None``, wenn nicht angegeben.

    Ein unbrauchbarer Wert ist ein Fehler, kein "dann eben alle Tage": ein
    stillschweigend ignorierter Tagesfilter zeigt mehr, als der Aufrufer
    sehen wollte (dieselbe Begruendung wie bei ``--since``).
    """
    if day is None or day == "":
        return None
    if isinstance(day, datetime):
        return day.date().isoformat()
    if isinstance(day, date):
        return day.isoformat()
    try:
        return datetime.strptime(str(day).strip(), "%Y-%m-%d").date().isoformat()
    except ValueError:
        raise ValueError(f"{day!r} ist kein Datum (YYYY-MM-DD)") from None


def _read_file(path: Path) -> list[dict]:
    """Alle brauchbaren Zeilen einer Tagesdatei (kaputte Zeilen uebersprungen)."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    entries = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def read_events(limit: int = 50, action: Optional[str] = None,
                actor: Optional[str] = None,
                since: Any = None, day: Any = None) -> list[dict]:
    """Die juengsten Vorgaenge, neueste zuerst.

    ``action`` und ``actor`` filtern genau, ``since`` (ISO-Datum oder
    -Zeitpunkt) schneidet ab diesem Zeitpunkt ab. Gelesen wird von der
    neuesten Tagesdatei rueckwaerts, bis ``limit`` erreicht ist — ein ``limit``
    von ``None`` oder ``0`` liest alles.

    ``day`` (``YYYY-MM-DD``) liest NUR die Tagesdatei dieses Tages (fehlt sie:
    leere Liste). Das Dashboard fragt einen Tag ab; ohne diesen Filter muesste
    es alle neueren Tagesdateien lesen und koennte bei einem alten Tag hinter
    lauter juengeren Zeilen nichts finden.
    """
    directory = log_dir()
    since_dt = _parse_since(since)
    wanted_day = _parse_day(day)
    if wanted_day is not None:
        only = directory / f"{FILE_PREFIX}{wanted_day}{FILE_SUFFIX}"
        files = [only] if only.is_file() else []
    else:
        try:
            files = sorted(directory.glob(f"{FILE_PREFIX}*{FILE_SUFFIX}"),
                           key=lambda path: path.name, reverse=True)
        except OSError:
            return []

    events: list[dict] = []
    for path in files:
        file_day = _file_day(path)
        if since_dt is not None and file_day is not None and file_day < since_dt.date():
            break  # aeltere Tage koennen nichts Juengeres enthalten
        for entry in reversed(_read_file(path)):
            if action and entry.get("action") != action:
                continue
            if actor and entry.get("actor") != actor:
                continue
            if since_dt is not None:
                ts = _entry_ts(entry)
                if ts is not None and ts < since_dt:
                    continue
            events.append(entry)
            if limit and len(events) >= limit:
                return events
    return events


def verify_chain(path: Any) -> dict:
    """Die Kette einer Tagesdatei von vorn pruefen.

    Jede Zeile traegt ``prev`` — die Pruefsumme der vorherigen Zeile, in der
    ersten Zeile ``"genesis"``. Geprueft wird Zeile fuer Zeile: stimmt
    ``prev`` nicht mit der Pruefsumme der Vorgaengerzeile ueberein, wurde eine
    Zeile geaendert, geloescht, eingeschoben oder umgestellt. Genau dann steht
    die Zeilennummer in ``broken_at`` (1-basiert, wie ein Editor zaehlt).

    Rueckgabe: ``{"ok": bool, "lines": n, "broken_at": <Zeilennummer|None>}``.

    Altbestand: Zeilen aus der Zeit VOR der Kette (heute schon geschrieben)
    haben kein ``prev``. Sie duerfen nur als zusammenhaengender Anfang
    vorkommen; ihre Pruefsumme ist der Anker, an dem die erste verkettete
    Zeile haengt. Ein fehlendes ``prev`` MITTEN in der Kette ist ein Bruch —
    sonst liesse sich die Kette durch Entfernen des Feldes umgehen.
    """
    target = Path(path)
    try:
        raw = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        # Keine Datei heisst: nichts ist pruefbar. ``ok`` bleibt False, damit
        # ein fehlendes Protokoll nie wie ein intaktes aussieht.
        return {"ok": False, "lines": 0, "broken_at": None}

    numbered = list(enumerate(raw.splitlines(), start=1))
    total = sum(1 for _, line in numbered if line.strip())
    expected = GENESIS
    seen_chained = False
    for number, line in numbered:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            return {"ok": False, "lines": total, "broken_at": number}
        if not isinstance(entry, dict):
            return {"ok": False, "lines": total, "broken_at": number}
        prev = entry.get("prev")
        if prev is None:
            if seen_chained:
                return {"ok": False, "lines": total, "broken_at": number}
            expected = _line_hash(entry)
            continue
        seen_chained = True
        if prev != expected:
            return {"ok": False, "lines": total, "broken_at": number}
        expected = _line_hash(entry)
    return {"ok": True, "lines": total, "broken_at": None}


# ── CLI ──────────────────────────────────────────────────────────────


def main(argv: Optional[list] = None) -> int:
    """``--tail 20 [--action …]`` oder ``--verify [--date YYYY-MM-DD]``."""
    parser = argparse.ArgumentParser(
        prog="python -m nexus_memory.service_log",
        description="Bewegungsprotokoll des Gedaechtnisses ansehen.",
    )
    parser.add_argument("--tail", type=int, default=50,
                        help="so viele Vorgaenge, von den juengsten an (0 = alle)")
    parser.add_argument("--action", default=None, help="nur diese Aktion")
    parser.add_argument("--actor", default=None, help="nur dieser Ausloeser")
    parser.add_argument("--since", default=None,
                        help="ab diesem ISO-Zeitpunkt (z.B. 2026-11-15)")
    parser.add_argument("--verify", action="store_true",
                        help="die Kette der Tagesdatei pruefen")
    parser.add_argument("--date", default=None,
                        help="Tag der zu pruefenden Datei (YYYY-MM-DD)")
    args = parser.parse_args(argv)
    if args.since and _parse_since(args.since) is None:
        # Ein stillschweigend ignorierter Filter zeigt mehr, als der Aufrufer
        # sehen wollte — das muss ein Fehler sein, keine leere Antwort.
        parser.error(f"--since {args.since!r} ist kein ISO-Datum/-Zeitpunkt")

    if args.verify:
        day = args.date or _today().isoformat()
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            parser.error(f"--date {day!r} ist kein Datum (YYYY-MM-DD)")
        path = log_dir() / f"{FILE_PREFIX}{day}{FILE_SUFFIX}"
        if not path.is_file():
            print(f"kein Protokoll fuer {day} unter {log_dir()}", file=sys.stderr)
            return 2
        result = verify_chain(path)
        print(_dumps(result))
        return 0 if result["ok"] else 1

    events = read_events(limit=args.tail, action=args.action,
                         actor=args.actor, since=args.since)
    if not events and not log_dir().is_dir():
        print(f"kein Protokoll unter {log_dir()}", file=sys.stderr)
    for entry in reversed(events):  # aelteste zuerst, wie ein Log aussieht
        print(_dumps(entry))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
