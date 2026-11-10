"""Suchvarianten fuer deutsche Komposita und Umlaute — die Ablage-Seite.

Warum es das gibt (gemessen 10.11.2026): Die Einspielung holt 32 Kandidaten ueber
Aehnlichkeit und zieht danach Eintraege nach vorn, in denen ein Wort der Frage
woertlich steht. Ein Etikett wie ``organization: Marsolek (type=Oellieferant)``
enthaelt weder "Heizöl" noch "Lieferant" als eigenes Wort: Frage "Heizöl-Lieferant"
ergab 0.579, damit ausserhalb der 32 Kandidaten, obwohl der Eintrag die Antwort
traegt. "Lieferant" steckt aber in "Oellieferant" — nur zusammen geschrieben.

Die Varianten entstehen beim Schreiben und landen in zwei Toepfen:

* ``payload["search_terms"]`` — damit die Umsortierung sie sieht, ohne dass der
  sichtbare Inhalt voller Stichwoerter steht.
* zusaetzlich im Einbettungstext — damit der Punkt ueberhaupt in die Kandidaten
  kommt (der Vektor wird aus Inhalt + Varianten gebildet, gespeichert wird der
  reine Inhalt).

Bewusste Grenze: zerlegt wird nur an einer Fuge, deren hinterer Teil ein
bekanntes Grundwort ist (Lieferant, Haendler, Anlage, Rechnung ...). Ohne
Woerterbuch ist jede andere Zerlegung geraten — "heizoellieferant" hat ein
Dutzend moeglicher Schnitte und nur einen richtigen. Lieber wenige richtige
Varianten als viele, die den Vektor verwaschen.
"""

from __future__ import annotations

import re

_MAX_TERMS = 8
_MIN_LEFT = 3        # "oel|lieferant" muss zaehlen
_MIN_HEAD = 5        # ein Grundwort ist nie kuerzer
_MAX_TERMS_PER_WORD = 3

_FOLD = {
    "ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
    "Ä": "Ae", "Ö": "Oe", "Ü": "Ue",
}

# Grundwoerter: der Teil eines Kompositums, der die Bedeutung traegt. Wer hier
# etwas ergaenzt, ergaenzt eine geprueften Grundwort, keine Vermutung.
_HEADS = frozenset({
    "lieferant", "haendler", "handlung", "anlage", "geraet", "maschine",
    "dienst", "rechnung", "vertrag", "pflege", "plan", "preis", "termin",
    "nummer", "stelle", "schalter", "speicher", "netzwerk", "leitung",
    "kasse", "amt", "werk", "haus", "platz", "kammer", "ordnung", "system",
    "pruefung", "wartung", "bericht", "liste", "meldung", "antrag",
})

_WORD_RE = re.compile(r"[A-Za-zÄÖÜäöüß]{4,}")

# Wortenden, an denen im Deutschen ein Praefix anfaengt. Steht der linke Teil
# eines Schnitts so da ("datenschutzver|ordnung"), ist der Schnitt an der
# falschen Stelle: den linken Teil weglassen, das Grundwort bleibt.
_PREFIX_TAILS = ("ver", "ge", "be", "ent", "zer", "miss")



def fold(text: str) -> str:
    """Umlaute und Eszett in die verbreitete ASCII-Schreibweise bringen."""
    return "".join(_FOLD.get(ch, ch) for ch in (text or ""))


def compound_parts(word: str, limit: int = _MAX_TERMS_PER_WORD) -> list[str]:
    """Tragbare Teile eines zusammengeschriebenen deutschen Wortes.

    Zerlegt nur, wo der hintere Teil ein bekanntes Grundwort ist. Gibt den
    hinteren Teil immer zurueck, den vorderen nur, wenn er fuer sich stehen
    kann (>= 5 Zeichen, sonst bleibt "Oel" in "Oellieferant" aussen vor).
    """
    w = (word or "").strip()
    if len(w) < _MIN_LEFT + _MIN_HEAD:
        return []
    parts: list[str] = []
    for i in range(_MIN_LEFT, len(w) - _MIN_HEAD + 1):
        left, right = w[:i], w[i:]
        if len(right) < _MIN_HEAD or len(left) < _MIN_LEFT:
            continue
        if fold(right).lower() not in _HEADS:
            continue
        if (len(left) >= _MIN_HEAD
                and not left.lower().endswith(_PREFIX_TAILS)
                and left.lower() not in parts):
            parts.append(left.lower())
        if right.lower() not in parts:
            parts.append(right.lower())
        if len(parts) >= limit:
            break
    return parts[:limit]


def search_variants(*texts: str, max_terms: int = _MAX_TERMS) -> str:
    """Komma-getrennte Suchvarianten fuer die uebergebenen Texte.

    Erzeugt (a) die ASCII-Schreibweise von Woertern mit Umlauten und (b) die
    tragbaren Teile zusammengeschriebener Woerter. Reihenfolge ist stabil,
    Doppel fallen weg, das Ergebnis ist auf ``max_terms`` begrenzt.
    """
    seen: list[str] = []

    def _add(term: str) -> None:
        t = (term or "").strip().lower()
        if len(t) < 4 or t in seen:
            return
        if len(seen) < max_terms:
            seen.append(t)

    for text in texts:
        for word in _WORD_RE.findall(text or ""):
            if fold(word) != word:
                folded = fold(word).lower()
                _add(folded)
                for part in compound_parts(folded):
                    _add(part)
            else:
                for part in compound_parts(word):
                    _add(part)
    return ", ".join(seen)


def embed_text(content: str, terms: str) -> str:
    """Text, der eingebettet wird: Inhalt plus Varianten (Inhalt bleibt gespeichert)."""
    content = content or ""
    terms = (terms or "").strip()
    return f"{content} {terms}" if terms else content
