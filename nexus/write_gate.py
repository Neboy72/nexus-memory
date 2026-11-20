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
6. Ein Zustandswert (Eigenschaftswort mit Zahl dahinter, etwa ``Fassung``,
   ``Port``, ``Preis``) wird gekennzeichnet, nicht umgeschrieben: das Ergebnis
   traegt ``state_value: True``, bei langen Texten zusaetzlich einen ``hint``
   auf einen eigenen Kurzfakt. Kein Fehler, kein Blockieren, kein zweiter
   Schreibvorgang.
7. Ein Gespraechsrest wird abgewiesen: ``User:``/``Assistant:`` im Text
   heisst, ein Modell hat den letzten Satz des Gespraechs mitgeschrieben.
   Bei einer Entitaet wird der Schwanz abgeschnitten und der Kopf behalten
   (dort stehen Rolle, Pfad, Preis), in allen anderen Kategorien faellt der
   Eintrag durch. ``session`` ist die Ausnahme: ein Transkript ist dort die
   Sache selbst.
8. Eine Frage ist kein Wissen: endet der ganze Text auf ``?`` (hoechstens
   zwei Saetze) oder beginnt er mit ``muss ich``/``kannst du`` ..., wird er
   abgewiesen — auch als ``rule``. Bei ``rule``/``preference`` gilt nur die
   strenge Form: ein von Hand formulierter Satz soll nicht an einer Wendung
   mitten im Satz scheitern.
9. Gruss, Zustimmung und Zustandsmeldung fallen durch: ``Ok``, ``Ja``,
   ``Danke`` am Anfang eines kurzen Textes, und ``laeuft noch``/``neu
   gestartet``/``fertig`` als ganze Meldung. Eine Zustandsmeldung ist nach
   dem naechsten Start falsch; wer sie trotzdem halten will, schreibt sie
   als ``temp`` (7 Tage), dann geht sie durch.
10. Ein Diktat-Rest wird ``session``, nicht abgewiesen (20.11.2026): was
   gesprochen klingt (Flickwoerter, Wortdopplungen, Anrede, fehlendes
   Satzzeichen, langer Schachtelsatz), ist ein Transkript, kein Wissen. Der
   Inhalt bleibt Wort fuer Wort stehen — nur die Dauerwirkung einer Regel
   entsteht nicht. Ein Mensch, der die Regel am Terminal bestaetigt, schlaegt
   die Heuristik: dann bleibt sie eine Regel.
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
# ── Diktat-Reste (20.11.2026) ────────────────────────────────────────
# Der Bestand zeigt den Fehler: 2466 Eintraege der Kategorie ``rule``, davon
# seit dem 08.11. kein neuer — das Tor haelt inzwischen. Der Ballast, der bei
# jedem Turn eingespielt wird, ist Altbestand aus August bis Oktober: diktierte
# Sprachfetzen, die als dauerhafte Regel gespeichert wurden ("dann müssten die
# alle da rein oder oder wie soll ich mir das vorstellen sagte dir nee nee").
#
# Erkannt wird an der FORM, nicht am Inhalt: ein Diktat-Rest ist gesprochene
# Rede (Flickwoerter, Wortdopplungen, Anrede, Satzzeichen fehlen, ein langer
# Schachtelsatz), eine Regel ist geschrieben (Etikett, Datum, Aussage).
# Erkannt heisst NICHT verworfen: die Kategorie wird auf ``session``
# herabgestuft — dort gehoert ein Transkript hin, und der Auto-Recall laesst
# Sitzungen draussen. Kein Inhalt geht verloren.
#
# Die Kalibrierung ist gemessen, nicht geschaetzt (Trockenlauf 20.11.2026 ueber
# den ganzen Bestand): Schwelle und Einzelgewichte stehen als Konstanten hier,
# damit die Zahl im Test nachpruefbar bleibt.
_DICTATION_SCORE_THRESHOLD = 5

#: Flickwoerter gesprochener Rede. Nur ganze Woerter — "ne" steckt sonst in
#: jedem zweiten Satz ("eine").
_DICTATION_FILLERS = frozenset({
    "nee", "ne", "nö", "äh", "ähm", "hmm", "hm", "halt", "eben",
    "irgendwie", "sozusagen", "quasi",
})

#: Wendungen, die ein Mensch beim Sprechen benutzt und beim Schreiben nicht.
_DICTATION_PHRASES = (
    "oder oder", "sagte dir", "sag ich", "sag mal", "weisst du", "weißt du",
    "ja ne", "ne ne", "oder so", "und so", "nicht wahr",
)

#: Wortdopplung ("oder oder", "nee nee") — beim Diktieren passiert das.
#: GROSS/KLEIN wird unterschieden: "eine Eine-Zeilen-Warnung" ist eine
#: Zusammenfuehrung, keine Dopplung — mit IGNORECASE traf sie 15 echte Regeln
#: (im Trockenlauf 20.11.2026 gesehen und korrigiert).
_DOUBLED_WORD_RE = re.compile(r"\b(\w{3,})\s+\1\b")
#: Ein Satzende irgendwo im Text (nicht am Rand): dann ist es kein Schachtelsatz.
_INNER_SENTENCE_RE = re.compile(r"[.!?]\s")
#: Zweite Person / Icherzaehlung: der Agent redet, statt eine Regel zu notieren.
_ADDRESS_RE = re.compile(
    r"\b(du|dir|dich|dein|deine|deinen|ihr|euch|ich|mich|wir|uns)\b",
    re.IGNORECASE,
)
#: Ab hier gilt ein satzzeichenloser Text als Schachtelsatz.
_RUNON_CHARS = 160
#: Ein Etikett am Anfang ("MORGENRAPPORT-REGEL (Nebo, 21.09.): ...") ist das
#: Gegenteil eines Diktats: ein geschriebener Kopf ueber einer Aussage.
_LABEL_HEAD_RE = re.compile(r"^[^\n]{0,80}:\s", re.UNICODE)

#: Satzzeichen, das einen Satz beendet — ein Punkt in "Skill.md" nicht.
_SENTENCE_PUNCT = re.compile(r"[!?;]|\.(?=\s|$)")
_DIGIT = re.compile(r"\d")
#: Kurze Kennung ohne Zahl, etwa eine Blutgruppe (``A+``).
_SHORT_CODE = re.compile(r"^[A-ZÄÖÜ][A-ZÄÖÜ0-9+\-/.]{0,7}$")
#: Finite Verbform im Wert — dann ist der Wert ein Satz, kein Etikett.
_FINITE_VERB = re.compile(r"\b\w{3,}(?:t|en|iert)\b")

#: Laenge, ab der ein Text als lang gilt. Der eingespielte Block fasst 2400
#: Zeichen und zeigt je Punkt ein Fenster von 400 — ein Zustandswert in einem
#: langen Text steht dort auf Rang 17 bis 26 statt auf 1 bis 3.
_LONG_TEXT_CHARS = 400

#: Eigenschaftswoerter, hinter denen ein Zustandswert stehen kann.
_STATE_WORDS = ("fassung", "version", "stand", "anzahl", "betrag", "preis",
                "kostet", "port", "adresse")
#: Eigenschaftswort und Wert duerfen hoechstens so viele Zeichen auseinander
#: stehen; ein Wert am Satzende gehoert nicht mehr zu einem Wort am Anfang.
_STATE_PAIR_RE = re.compile(
    r"\b(?:" + "|".join(_STATE_WORDS) + r")\b[^\n]{0,24}?(\d[\d.,]*)",
    re.IGNORECASE,
)

#: Satz fuer den Aufrufer, wenn der Zustandswert in einem langen Text steht.
_STATE_VALUE_HINT = (
    "Bitte zusätzlich einen eigenen Kurzfakt anlegen: Ding, Wert, Herkunft "
    "und Datum — in einem langen Text geht der Wert im eingespielten Block unter."
)

#: Namen mit Kategorie-Praefix sind Etiketten, keine Fakten.
_CATEGORY_PREFIXES = frozenset({
    "concept", "entity", "entitaet", "entität", "software", "person", "ort",
    "organization", "organisation", "project", "projekt", "tool", "geraet",
    "gerät", "event", "ereignis", "fakt", "fact",
})

# ── Gespraechsreste (20.11.2026) ─────────────────────────────────────
# Befund aus der Bestandsdurchsicht: von 303 belegten Muell-Eintraegen waren
# 99 reine Zustandsmeldungen ("laeuft noch", "neu gestartet"), 157 ein
# abgeschnittener Gespraechsrest ("User: das Problem ist ...") und 25
# als Regel gespeicherte Rohsaetze ("Okay, was muss ich jetzt machen"). Ein
# Modell schreibt beim Sitzungsende gern den letzten Satz mit; das ist keine
# Aussage ueber die Welt, sondern ein Rest des Gespraechs.

#: Der ganze Text ist eine Frage — dann ist er kein Wissen.
_QUESTION_END = re.compile(r"\?\s*$")
#: Hoechstens so viele Saetze. Ein Protokoll, das mit einer Frage endet, ist
#: keine reine Frage; ein Zweizeiler schon.
_MAX_QUESTION_SENTENCES = 2

#: Wendungen, mit denen ein Gespraechsrest beginnt (erste/zweite Person).
_ASK_STARTS = (
    "muss ich", "soll ich", "sollte ich", "kann ich", "kannst du",
    "machst du", "mache ich", "hast du", "bist du", "weisst du", "weißt du",
    "darf ich", "brauche ich", "was muss ich", "wie mache ich",
    "wo finde ich", "wo muss ich", "wie kann ich", "sag mir",
    "verstehst du", "meinst du",
)
#: Dieselben Wendungen mitten im Satz. Nur fuer Kategorien, die ein Modell
#: automatisch fuellt: eine von Hand formulierte Regel ("Wenn etwas fehlt,
#: soll ich nachfragen") ist keine Frage und darf nicht daran scheitern.
_ASK_MARKERS = ("muss ich", "soll ich", "machst du", "kann ich", "kannst du")
#: Laenger als das ist es kein abgeschnittener Satz mehr, sondern ein Text.
_MAX_ASK_CHARS = 120
#: Ein Satz, der mit einer Konjunktion beginnt, ist gefuegt und kein
#: abgeschnittener Gedanke: "Wenn etwas fehlt, soll ich nachfragen" ist eine
#: Aussage ueber das Verhalten, keine Frage.
_SUBORDINATE_STARTS = (
    "wenn", "falls", "sobald", "solange", "bevor", "damit", "weil", "obwohl",
    "nachdem", "seitdem", "während", "waehrend", "sofern",
)

#: Erste Woerter eines Grusses oder einer Zustimmung — kein Wissen.
_FRAGMENT_STARTS = frozenset({
    "ok", "okay", "okey", "ja", "nein", "doch", "danke", "genau", "passt",
    "super", "perfekt", "hallo", "hi", "moin", "servus", "guten",
    "alles", "vielen", "gerne", "bitte",
})
_MAX_FRAGMENT_CHARS = 80

#: Zustandsmeldungen: sagen, dass etwas laeuft, startet oder fertig ist.
_STATUS_PHRASES = (
    "läuft noch", "laeuft noch", "läuft weiter", "laeuft weiter",
    "läuft wieder", "laeuft wieder", "läuft jetzt", "laeuft jetzt",
    "noch am laufen", "neu gestartet", "gerade gestartet", "neu starten",
    "ist fertig", "jetzt fertig", "bin fertig", "schon fertig",
    # Offene Punkte (20.11.2026, Gegentest): "steht noch aus", "noch offen",
    # "ausstehend" sind Zustand und waren der zweitgroesste Rest im Altbestand.
    "steht noch aus", "steht noch offen", "steht aus", "ausstehend",
    "noch offen", "ist noch offen", "nicht erledigt",
    "bleibt offen", "noch ausstehend", "offener punkt", "steht noch",
)
#: Erste Woerter einer Erledigt-Meldung — auch in einem langen Satz.
#: "Erledigt 👍 Sauber. Läuft auf dem neuesten Stand durch. <Signatur>" war
#: 78 Zeichen lang und rutschte deshalb an der Laengengrenze vorbei.
_COMPLETION_WORDS = frozenset({
    "erledigt", "fertig", "geschafft", "done", "abgeschlossen", "erledigt.",
})
#: Ein einziges Wort als ganze Meldung.
_STATUS_WORDS = frozenset({
    "ok", "okay", "fertig", "erledigt", "abgeschlossen", "done", "passt",
    "läuft", "laeuft", "alles gut", "super", "danke", "ja", "nein",
})
#: Laenger als das ist eine Meldung kein Schnipsel mehr.
_MAX_STATUS_CHARS = 140

#: Reste eines Gespraechs im Text. ``session`` ist die Ausnahme: dort ist
#: ein Transkript die Sache selbst.
_PLACEHOLDER_RE = re.compile(
    # Kontextfenster-Reste und Prompt-Boilerplate. Sie sind lang und damit
    # unsichtbar fuer die Laengengrenzen; im Altbestand waren 12 Eintraege
    # woertlich "Last conversation: [System: Your previous response was
    # truncated by the output length limit. Continue exactly where you ...]".
    r"^\s*(?:last conversation|review the conversation above)\b"
    r"|your previous response was truncated"
    r"|continue exactly where you left off",
    re.IGNORECASE,
)
_CONVERSATION_RE = re.compile(
    # Nur echte Rollenmarken: am Textanfang oder hinter einem Gedankenstrich,
    # gross geschrieben und mit Leerzeichen nach dem Doppelpunkt. Gemessen am
    # 20.11.2026 an sechs Fehlalarmen im Altbestand: "assistant:write" ist ein
    # OAuth-Scope, "`user:`" ein Beispiel fuer ein Praefix, "(user: paperless)"
    # ein Datenbankbenutzer und "DB-User:" ein Kontoname.
    r"^[ \t]*(?:User|Assistant|Human)\s*:\s"
    r"|[—–][ \t]*(?:User|Assistant|Human)\s*:\s"
    r"|\[role:\s*user\]"
    r"|\[user:end\]"
    r"|\[assistant:end\]"
)
#: Trennzeichen, die nach dem Abschneiden hinten uebrig bleiben.
_TRAILING_SEPARATORS = " \t-–—,;:·|"

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


def _value_counts(value: str) -> bool:
    """Zahl, Ziffernfolge mit Punkt oder Versionsnummer — keine bloße Ziffer.

    ``Port 8`` ist keine Angabe, ``Port 8080`` und ``Fassung 0.23.1`` sind es.
    """
    return len(re.sub(r"\D", "", value)) >= 2 or "." in value


def has_state_value(text: str) -> bool:
    """Steckt ein Zustandswert im Text — ein Eigenschaftswort mit Wert dahinter?

    Ein Zustandswert ist eine Angabe, die sich aendert, ohne dass der Text
    sich aendert: Fassung, Version, Stand, Anzahl, Betrag, Preis, Port,
    Adresse. Solche Werte gehoeren als eigener Kurzfakt in den Speicher; in
    einem Gespraechsprotokoll stehen sie auf Rang 17 bis 26 und fallen aus dem
    eingespielten Block (2400 Zeichen, Fenster 400 je Punkt).

    Eng gehalten: nur diese Woerter, hoechstens 24 Zeichen bis zum Wert, und
    der Wert muss mehr als eine Ziffer sein. Ein Datum oder eine Uhrzeit
    allein ist kein Zustandswert.
    """
    for match in _STATE_PAIR_RE.finditer(text or ""):
        if _value_counts(match.group(1)):
            return True
    return False


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


def _first_word(text: str) -> str:
    match = re.match(r"\s*([^\s,.;:!?]+)", text or "")
    return match.group(1).lower() if match else ""


def is_question(text: str, category: str = "") -> bool:
    """Ist der ganze Text eine Frage?

    Zwei Formen, beide eng: der Text endet auf ``?`` und hat hoechstens zwei
    Saetze, oder er beginnt mit einer Wendung der ersten/zweiten Person
    (``muss ich``, ``kannst du`` ...). Die Wendungen mitten im Satz gelten nur
    fuer Kategorien, die ein Modell automatisch fuellt — eine von Hand
    formulierte ``rule``/``preference`` wird nicht daran gemessen.

    Ein laengeres Protokoll, das mit einer Frage endet, ist keine Frage: mehr
    als zwei Saetze heisst, es steht etwas davor.
    """
    value = (text or "").strip()
    if not value:
        return False
    if _QUESTION_END.search(value) and _sentence_count(value) <= _MAX_QUESTION_SENTENCES:
        return True
    lowered = value.lower()
    if lowered.startswith(_ASK_STARTS):
        return True
    if category in ("rule", "preference"):
        return False
    if len(value) > _MAX_ASK_CHARS or _sentence_count(value) > 1:
        return False
    if lowered.startswith(_SUBORDINATE_STARTS):
        return False
    return any(marker in lowered for marker in _ASK_MARKERS)


def is_fragment(text: str, strict: bool = False) -> bool:
    """Gruss, Zustimmung oder Flickwort statt einer Aussage.

    Nur kurz und nur am Anfang: "Ja, der Verteiler steht im Flur" ist ein
    Satz und geht durch, "Okay, ich verstehe nur Bahnhof" nicht.

    ``strict`` hebt die Laengengrenze auf — fuer ``rule`` und ``preference``.
    Eine von Hand formulierte Regel beginnt nicht mit "Ja, aber ..."; ein
    Satz, der so anfaengt, ist ein zitiertes Gespraech (Gegentest 20.11.2026:
    sechs der 83 Reste waren genau das).
    """
    value = (text or "").strip()
    if not value:
        return False
    if not strict and len(value) > _MAX_FRAGMENT_CHARS:
        return False
    return _first_word(value) in _FRAGMENT_STARTS


def _lowercase_scrap(text: str, category: str) -> bool:
    """Ein langer Satz, der klein anfaengt, ist ein abgetippter Gespraechssatz.

    Nur bei ``rule``/``preference``: ein Mensch formuliert eine Regel gross,
    ein Modell schreibt beim Mitschneiden ab, was im Chat stand ("es kam nur
    kurz deine stimme abgehackt dann habe ich dir nochmal gesprochen").
    Ausgenommen bleibt, was nach Konfiguration aussieht (Doppelpunkt, Backtick).
    """
    if category not in ("rule", "preference"):
        return False
    value = (text or "").strip()
    if len(value) <= 25 or ":" in value or value.startswith(("`", "~", "/")):
        return False
    # Ein Dateiname am Anfang ist kein abgetippter Satz: "hermes-config.md
    # enthaelt eine Summary-Tabelle" beginnt klein, ist aber Wissen.
    erstes = value.split(" ", 1)[0]
    if "." in erstes or "_" in erstes or "/" in erstes:
        return False
    return value[:1].islower()


def dictation_reasons(text: str) -> tuple[int, list[str]]:
    """Punktzahl und Gruende fuer "das ist ein Diktat-Rest".

    Reine Formpruefung, kein Inhalt und keine Kategorie: gesprochene Rede hat
    Merkmale, die geschriebene Saetze nicht haben. Die Gewichte sind klein und
    lesbar, damit ein einzelnes Merkmal nie reicht — erkannt wird nur, was
    mehrere traegt.
    """
    roh = str(text or "").strip()
    if not roh:
        return 0, []
    punkte = 0
    gruende: list[str] = []

    if not _SENTENCE_PUNCT.search(roh[-1:]) and not roh.endswith(("…", '"', "'")):
        punkte += 1
        gruende.append("kein Satzzeichen am Ende")

    doppelt = _DOUBLED_WORD_RE.search(roh)
    if doppelt:
        punkte += 2
        gruende.append(f"Wortdopplung ({doppelt.group(0)})")

    woerter = re.findall(r"[A-Za-zÄÖÜäöüß]+", roh.lower())
    treffer = [w for w in woerter if w in _DICTATION_FILLERS]
    if treffer:
        punkte += min(2, len(treffer))
        gruende.append("Flickwoerter (" + ", ".join(sorted(set(treffer))[:3]) + ")")

    # Wendungen als GANZE Woerter: "ne ne" steckt sonst in "eine neue" und
    # traefe jede zweite Regel (im Trockenlauf so passiert und korrigiert).
    klein = roh.lower()
    for wendung in _DICTATION_PHRASES:
        if re.search(r"\b" + re.escape(wendung) + r"\b", klein):
            punkte += 2
            gruende.append(f"Wendung ({wendung})")
            break

    if roh[:1].islower():
        punkte += 1
        gruende.append("beginnt klein")

    if len(roh) >= _RUNON_CHARS and not _INNER_SENTENCE_RE.search(roh):
        punkte += 2
        gruende.append(f"Schachtelsatz ({len(roh)} Zeichen ohne Satzbruch)")

    if _ADDRESS_RE.search(roh):
        punkte += 1
        gruende.append("Anrede/Icherzaehlung")

    if _LABEL_HEAD_RE.match(roh):
        # Ein geschriebener Kopf ueber einer Aussage ist das Gegenteil.
        punkte -= 3
        gruende.append("Etikett-Kopf (Abzug)")

    return punkte, gruende


def is_dictated_residue(text: str) -> Optional[str]:
    """Grund, warum der Text ein Diktat-Rest ist — ``None``, wenn nicht.

    Die Rueckgabe ist der Text fuer die Protokollzeile und den Trockenlauf;
    ``None`` heisst "sieht aus wie geschriebene Rede".
    """
    punkte, gruende = dictation_reasons(text)
    if punkte >= _DICTATION_SCORE_THRESHOLD:
        return f"{punkte} Punkte: " + ", ".join(gruende)
    return None


def is_status_snippet(text: str) -> bool:
    """Reine Zustandsmeldung: laeuft noch, neu gestartet, fertig.

    Sagt, wie etwas gerade steht, nicht was gilt. Eine solche Meldung ist
    nach dem naechsten Start falsch — sie gehoert, wenn ueberhaupt, in
    ``temp`` (7 Tage), nie in einen dauerhaften ``fact``.
    """
    value = (text or "").strip().lower()
    if not value:
        return False
    if _first_word(value) in _COMPLETION_WORDS:
        # Ohne Laengengrenze: eine Erledigt-Meldung mit Modellnamen und
        # Fuchs-Signatur ist 80 bis 130 Zeichen lang und war damit bisher
        # unsichtbar fuer die Laengengrenze.
        return True
    if len(value) > _MAX_STATUS_CHARS:
        return False
    if value.strip(" .!:") in _STATUS_WORDS:
        return True
    # Wortgrenzen sind Pflicht: "besteht aus" enthaelt "steht aus" und
    # "versteht noch" enthaelt "steht noch" — ein Substring-Vergleich hat am
    # 20.11.2026 einen echten Fakt abgewiesen ("Das System besteht aus den
    # Komponenten ...").
    return any(
        re.search(r"(?<!\w)" + re.escape(p) + r"(?!\w)", value)
        for p in _STATUS_PHRASES
    )


def conversation_rest(text: str) -> Optional[str]:
    """Schneidet einen Gespraechsrest ab; ``None``, wenn keiner drin ist.

    ``concept: helper (role=Monitoring-Agent) — User: das Problem ist
    ...`` wird zu ``concept: helper (role=Monitoring-Agent)``. Der Kopf ist
    Wissen (Rolle, Pfad, Preis), der Schwanz ist der letzte Satz eines
    Gespraechs. Bei einem Fakt gibt es keinen Kopf, der bleiben kann — dort
    weist das Tor ab (siehe ``prepare``).
    """
    match = _CONVERSATION_RE.search(text or "")
    if not match:
        return None
    head = (text or "")[:match.start()].rstrip(_TRAILING_SEPARATORS)
    return head


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
        return _refuse("empty content", category, source)

    cat = _normalize_category(category)

    # Diktat-Rest (20.11.2026): gesprochene Rede ist ein Transkript, kein
    # Wissen. EINMAL hier bestimmt und unten zur Herabstufung benutzt — die
    # Formpruefungen (Frage, Gruss, klein anfangend) gelten fuer sie nicht:
    # ein Transkript darf klein anfangen und Stocken enthalten.
    dictation = is_dictated_residue(text) if cat != "session" else None

    # ── Gespraechsrest (20.11.2026) ────────────────────────────────────
    # Steckt ein abgeschnittener Satz aus dem Gespraech im Text, bleibt bei
    # einer Entitaet nur der Kopf stehen (dort steht das Wissen: Rolle, Pfad,
    # Preis); ein Fakt hat keinen Kopf, der ohne den Rest noch etwas sagt.
    if _CONVERSATION_RE.search(text):
        rest = conversation_rest(text)
        if cat == "entity":
            if not rest or len(rest) < 8:
                return _refuse("conversation rest without a usable head", cat, source)
            text = rest
        elif cat != "session":
            # ``session`` ist ein Transkript — dort gehoert der Rest hin.
            return _refuse("conversation rest (User:/Assistant: in the text)", cat, source)

    # ── Frage statt Aussage ────────────────────────────────────────────
    # Eine Frage ist kein Wissen. Ein Transkript (``session``) darf Fragen
    # enthalten: dort ist der Verlauf die Sache selbst.
    if cat != "session" and not dictation:
        if is_question(text, cat):
            if cat in ("rule", "preference"):
                return _refuse("a rule/preference must be a statement, not a question", cat, source)
            return _refuse("question, not a statement", cat, source)
        if is_fragment(text, strict=cat in ("rule", "preference")):
            return _refuse("chat fragment (greeting/acknowledgement), not a statement", cat, source)
        if _lowercase_scrap(text, cat):
            return _refuse("dictated chat line (starts lowercase), not a statement", cat, source)

    # ── Zustandsmeldung ────────────────────────────────────────────────
    # "laeuft noch", "neu gestartet", "fertig" sagt, wie etwas gerade steht.
    # In ``temp`` (7 Tage) ist das richtig aufgehoben, als ``fact`` wird es
    # nie wahr — deshalb dort abweisen und den Weg nach ``temp`` nennen.
    if cat != "temp" and is_status_snippet(text):
        return _refuse(
            "status message, not knowledge (use category 'temp' if it must be kept)",
            cat, source,
        )

    # ── Reste des Kontextfensters ──────────────────────────────────────
    # "Last conversation: [System: ... truncated ...]" ist kein Inhalt, egal
    # in welcher Kategorie und egal wie lang.
    if _PLACEHOLDER_RE.search(text):
        return _refuse("placeholder from the context window, not knowledge", cat, source)

    war_regel = cat == "rule"

    prov_input = provenance if isinstance(provenance, dict) else {}
    origin_text = _origin_text(source, source_type, prov_input)

    if war_regel and _confirmed(rule_proof, text):
        # Ein Mensch hat die Regel am Terminal bestaetigt — dann bleibt sie
        # eine Regel. Die Formheuristik schlaegt keine Bestaetigung.
        prov_input = dict(prov_input)
        prov_input["rule_confirmed"] = True
        prov_input["rule_confirmed_at"] = _now_iso()
    elif dictation:
        # Nicht verwerfen: der Inhalt bleibt, nur die Dauerwirkung entfaellt.
        cat = "session"
        prov_input = dict(prov_input)
        prov_input["dictated_residue"] = dictation
        logger.info("write_gate: Diktat-Rest als session abgelegt (%s)", dictation)
    elif war_regel:
        # Herabstufen statt abweisen: der Inhalt bleibt erhalten, nur die
        # Dauerwirkung einer Regel entsteht ohne menschliche Bestaetigung nicht.
        cat = "fact"

    # Wendungen mitten im Satz ("Jetzt muss ich das Muster verstehen") gelten
    # erst hier: eine Regel, die ein Mensch am Terminal bestaetigt hat, wird
    # nicht an einer Formulierung gemessen. Eine herabgestufte Regel ist ein
    # ``fact`` und faellt damit doch durch — sie ist ein abgeschnittener
    # Gedanke, den niemand bestaetigt hat.
    if cat not in ("rule", "preference", "session") and is_question(text, "fact"):
        return _refuse("question, not a statement", cat, source)

    # Eine herabgestufte Regel wird nicht zusaetzlich als Etikett abgewiesen:
    # der Inhalt soll erhalten bleiben, auch wenn seine Form schwach ist.
    if cat == "fact" and not war_regel and _is_label(text):
        return _refuse("label instead of a statement (not a sentence)", cat, source)

    src = str(source or "").strip()
    prepared = {
        "content": text,
        "category": cat,
        "access_level": _resolve_level(access_level, origin_text),
        "source": src,
        "provenance": _build_provenance(prov_input, src, str(source_type or "")),
    }
    if has_state_value(text):
        # Nur ein Kennzeichen, kein neuer Inhalt: der Aufrufer schreibt es in
        # den Payload, damit die Einspielung den Punkt am Laengenfilter
        # vorbeilaesst (12 statt 40 Zeichen). Lange Texte bekommen zusaetzlich
        # einen Hinweis auf den eigenen Kurzfakt — kein Fehler, keine Abweisung.
        prepared["state_value"] = True
        if len(text) >= _LONG_TEXT_CHARS:
            prepared["hint"] = _STATE_VALUE_HINT
    return prepared
