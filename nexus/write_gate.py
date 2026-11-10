"""Gemeinsames Schreibtor fuer Nexus Memory.

Warum (gemessen 10.11.2026): Drei Wege schreiben in die Sammlung — das
Hermes-Plugin (nach jedem Turn), der MCP-Server (auf Zuruf) und Cron-Laeufe
(Paperless, Wiki, Sitzungsende). Am Eingang prueft keiner: 632 Punkte ohne
Inhalt, 5146 ohne Herkunft, 1890 Etiketten ohne Satz. Und die Zugriffsstufe
hing an der Quelle statt am Inhalt: eine Unterhaltung ueber ein privates
Dokument erzeugte "trusted"-Punkte mit privatem Inhalt.

Dies ist eine Bibliothek, kein Dienst. ``prepare`` bekommt einen Schreibvorgang,
prueft ihn und gibt das bereinigte Payload-Dict zurueck — oder ``None``, wenn
der Eintrag abgewiesen wird. Der Grund steht im Protokoll und ist nach dem
Aufruf ueber ``last_reason()`` abholbar.

Die Regeln:

1. Leerer Inhalt wird abgewiesen.
2. Unter ``MIN_FACT_CHARS`` Zeichen und Kategorie ``fact`` wird abgewiesen
   (Etikett, kein Fakt). Die Grenze ist ueber
   ``NEXUS_WRITE_GATE_MIN_FACT_CHARS`` einstellbar; ``0`` schaltet sie ab.
3. Die Herkunft wird nie leer: fehlt sie, wird sie aus Quelle und Quellentyp
   gebaut (``unbekannt``, wenn beides fehlt).
4. Die Zugriffsstufe ist die strengste aus Quelle und Angabe. Paperless und
   Wiki sind ``private``, Sitzungen ``trusted``. Eine ausdrueckliche Angabe
   bleibt, wird aber nie lockerer als die Quelle — privater Inhalt aus einem
   Paperless-Dokument wird also nicht oeffentlich, nur weil der Aufrufer
   ``public`` sagt. ``public`` vergibt das Tor nie von sich aus: es entsteht
   nur aus einer ausdruecklich oeffentlichen Quelle oder aus einer
   ausdruecklichen Angabe des Aufrufers.
5. Kategorie ``rule`` nur aus einem Gespraech oder einer Handeingabe. Text aus
   Dokument oder Webseite darf nie Regel werden: Regeln werden bei jedem Turn
   eingespielt und wirken damit dauerhaft.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger("nexus.write_gate")

#: Ab dieser Laenge ist ein ``fact`` ein Satz und kein Etikett.
MIN_FACT_CHARS = 20

#: Umgebungsvariable fuer die Laengengrenze; ``0`` schaltet sie ab (Tests,
#: Messlaeufe). Der Vorgabewert gilt, wenn sie fehlt oder unlesbar ist.
MIN_FACT_CHARS_ENV = "NEXUS_WRITE_GATE_MIN_FACT_CHARS"

#: Stufe, wenn weder Quelle noch Angabe etwas anderes sagen.
DEFAULT_LEVEL = "private"

#: Strenger wird es von links nach rechts.
_LEVEL_ORDER = {"public": 0, "trusted": 1, "private": 2}
_LEVEL_BY_ORDER = {order: name for name, order in _LEVEL_ORDER.items()}

_KNOWN_CATEGORIES = frozenset({
    "fact", "belief", "session", "rule", "preference", "procedure", "temp",
    # Entities werden ebenfalls ueber ``_upsert_entity`` geschrieben und laufen
    # durch dasselbe Tor.
    "entity",
})

# Quellen, deren Inhalt nie oeffentlich oder geteilt sein darf.
_PRIVATE_MARKERS = (
    "paperless", "wiki", "document", "dokument", "pdf", "scan",
)

# Sitzungen: Inhalt einer Unterhaltung, nicht zur Verbreitung bestimmt.
_TRUSTED_MARKERS = (
    "session", "sitzung", "transcript", "protokoll",
)

# Ausdruecklich oeffentliche Quellen. Ohne einen dieser Marker entsteht
# ``public`` nie aus der Quelle — nur aus einer ausdruecklichen Angabe.
_PUBLIC_MARKERS = ("public", "oeffentlich", "öffentlich", "open-data")

# Woher Text stammen darf, damit er als Regel gespeichert wird.
_RULE_ALLOWED = frozenset({
    "chat", "conversation", "unterhaltung", "gespraech", "gespräch",
    "manual", "manuell", "user", "human",
})

# Text aus diesen Quellen ist nie eine Regel (Dauer-Injektion).
_RULE_DENIED = (
    "paperless", "wiki", "document", "dokument", "pdf", "scan",
    "web", "website", "webseite", "http", "url", "mail", "email",
    "import", "crawl", "crawler", "scrape", "rss", "cron", "protokoll",
)

_local = threading.local()


def last_reason() -> str:
    """Grund der letzten Abweisung (leer, wenn der Aufruf angenommen wurde).

    Der Grund gehoert in die Antwort an den Aufrufer: ein abgewiesener
    Schreibvorgang ohne Begruendung sieht aus wie ein Fehler.
    """
    return getattr(_local, "reason", "")


def _refuse(reason: str, category: str, source: Any) -> None:
    """Merkt den Grund, protokolliert ihn und weist ab (``None``)."""
    _local.reason = reason
    logger.warning(
        "Schreibtor weist ab: %s (Kategorie %s, Quelle %r)", reason, category, source
    )
    return None


def _has(text: str, markers: tuple) -> bool:
    return any(marker in text for marker in markers)


def _min_fact_chars() -> int:
    """Laengengrenze fuer ``fact`` (0 = aus); ein kaputter Wert faellt zurueck."""
    raw = str(os.environ.get(MIN_FACT_CHARS_ENV, "") or "").strip()
    if not raw:
        return MIN_FACT_CHARS
    try:
        return max(0, int(raw))
    except ValueError:
        return MIN_FACT_CHARS


def _origin_text(source: Any, source_type: Any,
                 provenance: Dict[str, Any]) -> str:
    """Alles, was ueber die Herkunft bekannt ist, klein geschrieben.

    Quelle, Quellentyp und die entsprechenden Provenance-Felder zusammen —
    eine Angabe irgendwo reicht, damit die Quelle erkannt wird.
    """
    parts = [str(source or ""), str(source_type or "")]
    for key in ("source_type", "source", "origin"):
        value = provenance.get(key)
        if isinstance(value, str):
            parts.append(value)
    return " ".join(parts).lower()


def _normalize_category(category: Any) -> str:
    value = str(category or "").strip().lower()
    return value if value in _KNOWN_CATEGORIES else "fact"


def _normalize_level(level: Any) -> Optional[str]:
    value = str(level or "").strip().lower()
    return value if value in _LEVEL_ORDER else None


def _level_from_origin(origin_text: str) -> Optional[str]:
    """Stufe, die die Quelle verlangt; ``None``, wenn sie nichts verlangt.

    ``Sitzung`` heisst: die Quelle benennt sich so (``session``, ``sitzung``,
    ``transcript``, ``protokoll``) — nicht die Kategorie. Eine Kategorie
    ``session`` allein sagt nichts darueber, wer den Text lesen darf.
    """
    if _has(origin_text, _PRIVATE_MARKERS):
        return "private"
    if _has(origin_text, _TRUSTED_MARKERS):
        return "trusted"
    if _has(origin_text, _PUBLIC_MARKERS):
        return "public"
    return None


def _resolve_level(requested: Any, origin_text: str) -> str:
    """Die strengste aus Quelle und Angabe; ``public`` nur ausdruecklich."""
    wanted = _normalize_level(requested)
    forced = _level_from_origin(origin_text)
    if wanted is None:
        return forced or DEFAULT_LEVEL
    if forced is None:
        return wanted
    return _LEVEL_BY_ORDER[max(_LEVEL_ORDER[wanted], _LEVEL_ORDER[forced])]


def _build_provenance(provenance: Optional[Dict[str, Any]], source: str,
                      source_type: str) -> Dict[str, Any]:
    """Herkunft nie leer lassen: vorhandene Angaben ergaenzen, nicht ersetzen."""
    built: Dict[str, Any] = dict(provenance) if isinstance(provenance, dict) else {}
    origin = str(source_type or "").strip()
    if not str(built.get("source_type") or "").strip():
        built["source_type"] = origin or source or "unbekannt"
    if not str(built.get("source") or "").strip():
        built["source"] = source or origin or "unbekannt"
    return built


def _rule_origin_allowed(origin_text: str) -> bool:
    """Regel nur aus Gespraech oder Handeingabe — nie aus Dokument oder Web."""
    if _has(origin_text, _RULE_DENIED):
        return False
    tokens = origin_text.replace(",", " ").replace(";", " ").split()
    return bool(_RULE_ALLOWED.intersection(tokens))


def prepare(content: Any, category: str = "fact", access_level: Any = "",
            source: Any = "", provenance: Optional[Dict[str, Any]] = None,
            source_type: Any = "") -> Optional[Dict[str, Any]]:
    """Prueft einen Schreibvorgang und liefert das bereinigte Payload-Dict.

    ``source_type`` ist die Herkunftsart (``chat``, ``manual``, ``paperless``
    ...); ein bereits vorhandener ``provenance["source_type"]`` (etwa
    ``hermes-plugin``) bleibt unangetastet — er benennt den Schreiber, nicht
    die Herkunft des Textes.

    Rueckgabe ``None`` heisst abgewiesen; ``last_reason()`` nennt den Grund.
    Sonst ein Dict mit ``content``, ``category``, ``access_level``, ``source``
    und ``provenance`` — fertig zum Schreiben.
    """
    _local.reason = ""

    text = "" if content is None else str(content).strip()
    if not text:
        return _refuse("leerer Inhalt", category, source)

    cat = _normalize_category(category)
    min_fact_chars = _min_fact_chars()
    if cat == "fact" and min_fact_chars and len(text) < min_fact_chars:
        return _refuse(
            f"fact kuerzer als {min_fact_chars} Zeichen ({len(text)})", cat, source
        )

    prov_input = provenance if isinstance(provenance, dict) else {}
    origin_text = _origin_text(source, source_type, prov_input)

    if cat == "rule" and not _rule_origin_allowed(origin_text):
        return _refuse("Regel aus einer Dokument- oder Webquelle", cat, source)

    src = str(source or "").strip()
    return {
        "content": text,
        "category": cat,
        "access_level": _resolve_level(access_level, origin_text),
        "source": src,
        "provenance": _build_provenance(prov_input, src, str(source_type or "")),
    }
