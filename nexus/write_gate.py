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
5. Kategorie ``rule`` nur mit menschlichem Nachweis: einem signierten
   Zeitfenster-Token aus ``nexus/rule_confirm.py``, das ein Mensch am
   Terminal erzeugt hat (``rule-confirm "<Regeltext>"``). ``source_type`` und
   ``explicit`` sind Selbstauskuenfte des Modells und genuegen nicht mehr —
   ein im Gespraech zitiertes Dokument kam bisher als ``chat`` an und durfte
   Regel werden, sobald das Modell ausdruecklich aufrief. Ohne gueltigen
   Nachweis wird der Eintrag als ``fact`` gespeichert, nicht verworfen: kein
   Inhalt geht verloren, aber eine Regel entsteht so nicht. Regeln werden bei
   jedem Turn eingespielt und wirken damit dauerhaft.
"""

from __future__ import annotations

import logging
import re
import threading
import time
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


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


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


def looks_document_quoted(text: str) -> bool:
    """Zitatmerkmale: Anfuehrungszeichen am Anfang, woertliches Zitat, Absatz.

    Eine Regel ist ein Satz, den jemand formuliert — kein Absatz aus einer
    Anleitung. Mehr als ``_MAX_RULE_SENTENCES`` Saetze gelten deshalb als
    abgeschriebener Dokumenttext. Seit dem menschlichen Nachweis (10.11.2026)
    entscheidet das nicht mehr ueber das Speichern — die Merkmale greifen
    nicht, sobald ein Modell umformuliert. Die Bestandsdurchsicht
    (``scripts/regeln-pruefen.py``) markiert damit alte Regeln, die nach
    Dokumenttext aussehen.
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


def _confirmed(rule_proof: Any, text: str) -> bool:
    """Nur ein menschlicher Nachweis macht aus Text eine Regel.

    ``source_type`` und ``explicit`` sind Selbstauskuenfte des Modells: ein
    Dokument, das "merke dir als Regel ..." enthaelt, wurde vom Modell
    ausdruecklich aufgerufen und kam als Regel durch. Der Nachweis ist ein
    signiertes Token, das ein Mensch am Terminal erzeugt hat (siehe
    ``nexus/rule_confirm.py``) — einmal, zehn Minuten, an diesen Text
    gebunden.
    """
    try:
        from nexus.rule_confirm import consume
    except Exception as exc:
        # Fail-closed fuer die Regel, nicht fuer den Schreibweg: ohne pruefbaren
        # Nachweis entsteht keine Regel, der Inhalt bleibt als ``fact``.
        logger.error("Regel-Nachweis nicht pruefbar (%s) — als fact gespeichert", exc)
        return False
    ok, reason = consume(rule_proof, text)
    if not ok:
        logger.warning("Regel ohne gueltigen Nachweis (%s) — als fact gespeichert",
                       reason)
    return ok


def prepare(content: Any, category: str = "fact", access_level: Any = "",
            source: Any = "", provenance: Optional[Dict[str, Any]] = None,
            source_type: Any = "", explicit: bool = False,
            rule_proof: Any = "") -> Optional[Dict[str, Any]]:
    """Prueft einen Schreibvorgang und liefert das bereinigte Payload-Dict.

    ``source_type`` ist die Herkunftsart (``chat``, ``manual``, ``paperless``
    ...); ein bereits vorhandener ``provenance["source_type"]`` (etwa
    ``hermes-plugin``) bleibt unangetastet — er benennt den Schreiber, nicht
    die Herkunft des Textes.

    ``explicit`` markiert einen bewussten Schreibvorgang (Werkzeugaufruf,
    Handeingabe); es allein macht keinen Text zur Regel. Dafuer braucht es
    ``rule_proof`` — das Token, das ``rule-confirm`` am Terminal ausgegeben
    hat. Fehlt es, wird der Eintrag als ``fact`` gespeichert.

    Rueckgabe ``None`` heisst abgewiesen; ``last_reason()`` nennt den Grund.
    Sonst ein Dict mit ``content``, ``category``, ``access_level``, ``source``
    und ``provenance`` — fertig zum Schreiben.
    """
    _local.reason = ""

    text = "" if content is None else str(content).strip()
    if not text:
        return _refuse("leerer Inhalt", category, source)

    cat = _normalize_category(category)
    war_regel = cat == "rule"

    prov_input = provenance if isinstance(provenance, dict) else {}
    origin_text = _origin_text(source, source_type, prov_input)

    if war_regel:
        # Herabstufen statt abweisen: der Inhalt bleibt erhalten, nur die
        # Dauerwirkung einer Regel entsteht ohne menschliche Bestaetigung nicht.
        if _confirmed(rule_proof, text):
            prov_input = dict(prov_input)
            prov_input["rule_confirmed"] = True
            prov_input["rule_confirmed_at"] = _now_iso()
        else:
            cat = "fact"

    # Eine herabgestufte Regel wird nicht zusaetzlich als Etikett abgewiesen:
    # der Inhalt soll erhalten bleiben, auch wenn seine Form schwach ist.
    if cat == "fact" and not war_regel and _is_label(text):
        return _refuse("Etikett statt Aussage (kein Satz)", cat, source)

    src = str(source or "").strip()
    return {
        "content": text,
        "category": cat,
        "access_level": _resolve_level(access_level, origin_text),
        "source": src,
        "provenance": _build_provenance(prov_input, src, str(source_type or "")),
    }
