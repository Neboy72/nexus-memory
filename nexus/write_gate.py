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
2. Ein ``fact``, der nur ein Etikett ist, wird abgewiesen: die Form
   ``wort: wert`` ohne Satzzeichen und ohne Verb, oder ein Name mit
   Kategorie-Praefix (``concept: Heizoel``). Kurze Fakten mit Zahl oder
   Kennung (``Tuer-Code: 4711``, ``Blutgruppe: A+``) sind keine Etiketten
   und gehen durch. Die Schwellen stehen hier als Konstanten, nicht in der
   Umgebung des jeweiligen Aufrufers.
3. Die Herkunft wird nie leer: fehlt sie, wird sie aus Quelle und Quellentyp
   gebaut (``unbekannt``, wenn beides fehlt).
4. Die Zugriffsstufe ist die strengste aus Quelle und Angabe. Paperless und
   Wiki sind ``private``, Sitzungen ``trusted``. Eine ausdrueckliche Angabe
   bleibt, wird aber nie lockerer als die Quelle — privater Inhalt aus einem
   Paperless-Dokument wird also nicht oeffentlich, nur weil der Aufrufer
   ``public`` sagt. ``public`` vergibt das Tor nie von sich aus: es entsteht
   nur aus einer ausdruecklich oeffentlichen Quelle (als ganzes Wort, nie als
   Pfadbestandteil) oder aus einer ausdruecklichen Angabe des Aufrufers.
5. Kategorie ``rule`` nur ausdruecklich: bei ``source_type="manual"`` oder
   wenn der Aufrufer den Schreibvorgang als bewusst markiert (``explicit``).
   Automatische Extraktion nach einem Turn darf nie Regeln schreiben, und
   Text mit Zitatmerkmalen (Anfuehrungszeichen am Anfang, "laut Dokument",
   "steht im Schreiben", mehr als zwei Saetze) nie — auch nicht ausdruecklich.
   Regeln werden bei jedem Turn eingespielt und wirken damit dauerhaft.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger("nexus.write_gate")

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
# Post, Kalender und Gesundheitsdaten (10.11.2026): sie fehlten — eine Mail
# ueber einen Arzttermin kam als "trusted" durch.
_PRIVATE_MARKERS = (
    "paperless", "wiki", "document", "dokument", "pdf", "scan",
    "mail", "email", "postfach", "kalender", "termin",
    "gesundheit", "arzt", "befund", "diagnose", "blutdruck", "medikament",
    "konto", "iban", "versicherung", "steuer", "rechnung",
)

# Sitzungen: Inhalt einer Unterhaltung, nicht zur Verbreitung bestimmt.
_TRUSTED_MARKERS = (
    "session", "sitzung", "transcript", "protokoll",
)

# Ausdruecklich oeffentliche Quellen. Ohne einen dieser Marker entsteht
# ``public`` nie aus der Quelle — nur aus einer ausdruecklichen Angabe.
_PUBLIC_MARKERS = ("public", "oeffentlich", "öffentlich", "open", "open-data")

# Woher Text stammen darf, damit er als Regel gespeichert wird: nur
# ``source_type == "manual"``. Alles andere braucht ``explicit``.
_RULE_MANUAL_SOURCE = "manual"

# Text aus diesen Quellen ist nie eine Regel (Dauer-Injektion).
_RULE_DENIED = (
    "paperless", "wiki", "document", "dokument", "pdf", "scan",
    "web", "website", "webseite", "http", "url", "mail", "email",
    "import", "crawl", "crawler", "scrape", "rss", "cron", "protokoll",
)

# Etikett statt Aussage ("Farbe: blau"). Nur bei ``fact`` geprueft.
_LABEL_RE = re.compile(r"^\s*([^:\n]{1,40}?)\s*:\s*(\S.*?)\s*$")
#: Satzzeichen, das einen Satz beendet — ein Punkt in "Skill.md" nicht.
_SENTENCE_PUNCT = re.compile(r"[!?;]|\.(?=\s|$)")
_DIGIT = re.compile(r"\d")
#: Kurze Kennung ohne Zahl, etwa eine Blutgruppe (``A+``).
_SHORT_CODE = re.compile(r"^[A-ZÄÖÜ][A-ZÄÖÜ0-9+\-/.]{0,7}$")
#: Finite Verbform im Wert — dann ist der Wert ein Satz, kein Etikett.
_FINITE_VERB = re.compile(r"\b\w{3,}(?:t|en|iert)\b")

#: Namen mit Kategorie-Praefix sind Etiketten, keine Fakten.
_CATEGORY_PREFIXES = frozenset({
    "concept", "entity", "entitaet", "entität", "software", "person", "ort",
    "organization", "organisation", "project", "projekt", "tool", "geraet",
    "gerät", "event", "ereignis", "fakt", "fact",
})

#: Zitatmerkmale im Inhalt einer behaupteten Regel.
_QUOTE_STARTS = ('"', "„", "“", "”", "»", "«", "‘", "‚")
_QUOTE_PHRASES = (
    "laut dokument", "laut dem dokument", "laut unterlage", "laut unterlagen",
    "laut anleitung", "laut vertrag", "steht im schreiben",
    "im schreiben steht", "heisst es", "heißt es", "zitat",
)
#: Mehr als so viele Saetze gelten als abgeschriebener Dokumenttext.
_MAX_RULE_SENTENCES = 2

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


def _is_label(text: str) -> bool:
    """Reines Etikett statt Aussage ("Farbe: blau", "concept: Heizoel").

    Kurze Fakten mit Zahl oder Kennung sind keine Etiketten: "Tuer-Code: 4711"
    und "Blutgruppe: A+" gehen durch. Satzzeichen im Text heisst: es ist ein
    Satz. Im Zweifel wird angenommen, dass es kein Etikett ist — ein Etikett,
    das durchrutscht, ist harmloser als ein echter Fakt, der abgewiesen wird.
    """
    if _SENTENCE_PUNCT.search(text):
        return False
    match = _LABEL_RE.match(text)
    if not match:
        return False
    head, value = match.group(1).strip(), match.group(2).strip()
    if not head or not value:
        return False
    if _DIGIT.search(value) or _SHORT_CODE.match(value):
        return False
    words = head.split()
    if len(words) == 1 and head.lower() in _CATEGORY_PREFIXES:
        return True
    if len(words) > 3:
        return False
    return not _FINITE_VERB.search(value)


def _sentence_count(text: str) -> int:
    return len([part for part in re.split(r"[.!?]+", text) if part.strip()])


def _looks_document_quoted(text: str) -> bool:
    """Zitatmerkmale: Anfuehrungszeichen am Anfang, woertliches Zitat, Absatz.

    Eine Regel ist ein Satz, den jemand formuliert — kein Absatz aus einer
    Anleitung. Mehr als ``_MAX_RULE_SENTENCES`` Saetze gelten deshalb als
    abgeschriebener Dokumenttext.
    """
    if text.lstrip()[:1] in _QUOTE_STARTS:
        return True
    lowered = text.lower()
    if any(phrase in lowered for phrase in _QUOTE_PHRASES):
        return True
    return _sentence_count(text) > _MAX_RULE_SENTENCES


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


def _public_in_origin(origin_text: str) -> bool:
    """Oeffentlich nur als ganzes Wort und nie als Pfadbestandteil.

    ``nicht-oeffentlich`` verneint, ``/public/`` ist ein Verzeichnisname —
    beides ist keine oeffentliche Quelle. Die Zeichen links und rechts des
    Treffers duerfen deshalb weder Wortzeichen noch Bindestrich noch
    Pfadtrenner sein.
    """
    for marker in _PUBLIC_MARKERS:
        pattern = r"(?<![\w\-/\\])" + re.escape(marker) + r"(?![\w\-/\\])"
        if re.search(pattern, origin_text):
            return True
    return False


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
    if _public_in_origin(origin_text):
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


def _rule_origin_allowed(source_type: Any, explicit: bool,
                         origin_text: str) -> bool:
    """Regel nur ausdruecklich — nie automatisch aus einem Turn.

    Zwei Wege: ``source_type == "manual"`` (Handeingabe) oder ein bewusster
    Aufruf (``explicit``, etwa das Werkzeug ``nexus_remember``). Die blosse
    Behauptung "chat" genuegt nicht mehr: ein im Gespraech zitiertes Dokument
    kam bisher als ``chat`` an und durfte Regel werden. Dokument- und
    Webquellen bleiben in jedem Fall gesperrt.
    """
    if _has(origin_text, _RULE_DENIED):
        return False
    if str(source_type or "").strip().lower() == _RULE_MANUAL_SOURCE:
        return True
    return bool(explicit)


def prepare(content: Any, category: str = "fact", access_level: Any = "",
            source: Any = "", provenance: Optional[Dict[str, Any]] = None,
            source_type: Any = "", explicit: bool = False) -> Optional[Dict[str, Any]]:
    """Prueft einen Schreibvorgang und liefert das bereinigte Payload-Dict.

    ``source_type`` ist die Herkunftsart (``chat``, ``manual``, ``paperless``
    ...); ein bereits vorhandener ``provenance["source_type"]`` (etwa
    ``hermes-plugin``) bleibt unangetastet — er benennt den Schreiber, nicht
    die Herkunft des Textes.

    ``explicit`` markiert einen bewussten Schreibvorgang (Werkzeugaufruf,
    Handeingabe). Automatische Extraktion laesst es weg und darf damit nie
    eine Regel schreiben.

    Rueckgabe ``None`` heisst abgewiesen; ``last_reason()`` nennt den Grund.
    Sonst ein Dict mit ``content``, ``category``, ``access_level``, ``source``
    und ``provenance`` — fertig zum Schreiben.
    """
    _local.reason = ""

    text = "" if content is None else str(content).strip()
    if not text:
        return _refuse("leerer Inhalt", category, source)

    cat = _normalize_category(category)
    if cat == "fact" and _is_label(text):
        return _refuse("Etikett statt Aussage (kein Satz)", cat, source)

    prov_input = provenance if isinstance(provenance, dict) else {}
    origin_text = _origin_text(source, source_type, prov_input)

    if cat == "rule":
        if not _rule_origin_allowed(source_type, explicit, origin_text):
            return _refuse(
                "Regel nur aus Handeingabe oder ausdruecklichem Aufruf", cat, source
            )
        if _looks_document_quoted(text):
            return _refuse("Regel mit Zitatmerkmalen (Dokumenttext)", cat, source)

    src = str(source or "").strip()
    return {
        "content": text,
        "category": cat,
        "access_level": _resolve_level(access_level, origin_text),
        "source": src,
        "provenance": _build_provenance(prov_input, src, str(source_type or "")),
    }
