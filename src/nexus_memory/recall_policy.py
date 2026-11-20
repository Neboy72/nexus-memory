"""Die Auswahlregel des Abrufs — an EINER Stelle, fuer Dienst und Plugins.

Warum (20.11.2026): Jeder Weg entschied selbst, was er in den Kontext legt.
Das DSH-Plugin nahm fuenf Eintraege ohne jede Schwelle, Claude Code drei
ueber eine Kosinus-Schwelle, OpenClaw zehn ohne Filter, Hermes einen eigenen
Vorlauf. Ein Filter, den nur ein Weg kennt, ist kein Filter, sondern eine
Eigenheit. Diese Datei ist die gemeinsame Regel; die Plugins reichen nur noch
die Schalter durch und entscheiden nichts selbst.

Zwei Hebel, beide opt-in (ohne Angabe bleibt das Verhalten wie vorher):

* ``exclude`` — Kategorien, die NICHT eingespeist werden. Das ist der Weg des
  Auto-Recall: ``session`` (Sitzungsprotokolle) und ``entity`` (Etiketten wie
  "software: x.com") sind fuer den Kontext Ballast; alles, was Wissen ist —
  ``fact``, ``rule``, ``preference``, ``belief``, ``procedure``, ``lesson``,
  ``pattern`` — bleibt drin. Eine Ausschlussliste statt einer Erlaubnisliste,
  weil eine Erlaubnisliste jede neue Wissenskategorie still mit aussperrt
  (gemessen 20.11.2026: sie kostete drei passende Eintraege in 25 Fragen).
* ``categories`` — die umgekehrte Form (nur diese). Bleibt fuer Aufrufer, die
  genau eine Kategorie suchen; im Auto-Recall nicht benutzt.
* ``min_score`` — Mindestpunktzahl auf der Kosinus-Skala (0..1), Standard AUS.

Punktzahlen, nicht Raenge: Die Fusion ist RRF, und ein RRF-Wert ist kein
Aehnlichkeitsmass — er haengt an der Listenposition und belohnt jeden Treffer,
der ueberhaupt in der Kandidatenliste steht. Nach der Normalisierung auf den
besten Treffer hat der Spitzenreiter IMMER 1.0, auch wenn er zur Frage nicht
passt. Eine Schwelle darauf waere Zierde. Die vergleichbare Zahl ist der
Kosinus des Vektorbeins; sie kommt aus der Qdrant-Suche und ist ueber Fragen
hinweg vergleichbar.

Zur Schwelle, gemessen (25 echte Fragen, 20.11.2026): 0.30 bis 0.40 filtern
NICHTS — die Stichprobe liefert dort dieselben Treffer. Ab 0.45 fallen zuerst
die passenden Eintraege weg, waehrend der Ballastanteil steigt (0.45 → 60 %,
0.60 → 73 %). Eine Schwelle unter 0.45 ist also ohne Wirkung; ueber 0.45
schadet sie. Deshalb ist sie nicht voreingestellt, sondern eine bewusst zu
setzende Option.

Eintraege ohne Kosinus (nur ueber BM25 gefunden) bestehen die Schwelle, wenn
sie mindestens einen echten Worttreffer haben. Begruendung: die Vektorsuche
sieht nur ihre Top-Kandidaten; ein Eintrag, der woertlich passt und dort nicht
auftaucht, ist damit nicht schlechter — bei Kennungen (Adresse, Punktname)
sucht der Dienst sogar bewusst nur woertlich. Ihn wegen einer fehlenden Zahl
wegzuwerfen hiesse, die staerkste Evidenz zu verwerfen, die es gibt.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

#: Voreinstellung fuer Auto-Recall (die Plugins setzen sie, nicht der Dienst).
DEFAULT_RECALL_LIMIT = 3
DEFAULT_RECALL_BUDGET_CHARS = 800
#: Was der Auto-Recall NICHT einspeist: Protokolle und Etiketten.
DEFAULT_RECALL_EXCLUDE = ("session", "entity")
#: Mindestpunktzahl: nicht voreingestellt. Siehe Modul-Docstring zur Messung —
#: unter 0.45 ohne Wirkung, darueber schaedlich. Wer sie setzt, setzt sie
#: bewusst (0.45 ist die erste Schwelle mit messbarer Wirkung).
DEFAULT_RECALL_MIN_SCORE: Optional[float] = None
#: Die erste Schwelle, ab der sich das Ergebnis messbar aendert.
RECALL_MIN_SCORE_HAS_EFFECT_FROM = 0.45

#: Kategorien, die der Dienst kennt (``nexus.MemoryCategory``) plus die
#: Etikett- und Wissenskategorien, die im Bestand tatsaechlich vorkommen.
#: Eine unbekannte Angabe wird abgelehnt statt still ignoriert: ein Tippfehler
#: darf nicht zu "kein Filter" werden.
KNOWN_CATEGORIES = frozenset({
    "fact", "belief", "session", "rule", "preference", "procedure", "temp",
    "entity", "lesson", "pattern", "decision", "idea",
})


class RecallPolicyError(ValueError):
    """Ungueltige Auswahlangabe — der Aufrufer soll es erfahren, nicht raten."""


def _normalize_category_list(value: Any, feld: str) -> Optional[tuple[str, ...]]:
    if value is None:
        return None
    if isinstance(value, str):
        roh = list(value.split(","))
    elif isinstance(value, Iterable):
        roh = list(value)
    else:
        raise RecallPolicyError(
            f"{feld} muss eine Liste oder eine Zeichenkette sein, nicht {type(value).__name__}"
        )
    sauber: list[str] = []
    for teil in roh:
        name = str(teil).strip().lower()
        if not name:
            continue
        if name not in KNOWN_CATEGORIES:
            raise RecallPolicyError(
                f"unbekannte Kategorie {name!r} in {feld} — erlaubt: {sorted(KNOWN_CATEGORIES)}"
            )
        if name not in sauber:
            sauber.append(name)
    return tuple(sauber) or None


def normalize_categories(value: Any) -> Optional[tuple[str, ...]]:
    """``categories`` als Tupel erlaubter Kategorien — ``None`` heisst: alle.

    Annahme: Liste/Tupel von Zeichenketten oder eine kommagetrennte Zeichenkette
    (damit auch Umgebungsvariablen und Kommandozeilen es koennen). Leer heisst
    "kein Filter"; eine unbekannte Kategorie ist ein Fehler.
    """
    return _normalize_category_list(value, "categories")


def normalize_exclude(value: Any) -> Optional[tuple[str, ...]]:
    """``exclude`` als Tupel ausgeschlossener Kategorien — ``None``: keins."""
    return _normalize_category_list(value, "exclude")


def normalize_min_score(value: Any) -> Optional[float]:
    """``min_score`` als Zahl in 0..1 — ``None`` heisst: keine Schwelle."""
    if value is None or value == "":
        return None
    try:
        zahl = float(value)
    except (TypeError, ValueError):
        raise RecallPolicyError(f"min_score muss eine Zahl sein, nicht {value!r}") from None
    if zahl != zahl:  # NaN
        raise RecallPolicyError("min_score darf nicht NaN sein")
    if not 0.0 <= zahl <= 1.0:
        raise RecallPolicyError(f"min_score liegt in 0..1, nicht {zahl}")
    return zahl


def effective_category(item: dict) -> str:
    """Die Kategorie, nach der gefiltert wird — wie sie der Leser sieht.

    Altbestand ohne Kategorie gilt als ``fact`` (dieselbe Regel, die der
    Abruf seit jeher beim Ausliefern anwendet). Ein aufgezeichneter Turn ohne
    eigene Kategorie ist dagegen KEIN Fakt, sondern ein Sitzungsprotokoll —
    sonst bliebe genau der Ballast drin, der hier raus soll.
    """
    typ = str(item.get("type") or "").strip().lower()
    if typ == "turn":
        return "session"
    kategorie = str(item.get("category") or "").strip().lower()
    return kategorie or "fact"


def relevance(item: dict) -> Optional[float]:
    """Die vergleichbare Punktzahl eines Eintrags — ``None``, wenn es keine gibt.

    Kosinus aus dem Vektorbein, auf 0..1 geklemmt (eine Kosinus-Aehnlichkeit
    kann rechnerisch negativ sein; unter null ist "kein Bezug").
    """
    for schluessel in ("vector_score", "relevance"):
        wert = item.get(schluessel)
        if isinstance(wert, (int, float)) and wert == wert:
            return max(0.0, min(1.0, float(wert)))
    return None


def has_lexical_evidence(item: dict) -> bool:
    """Gab es einen echten Worttreffer (BM25 > 0)?

    ``_bm25`` ist der interne Schluessel der Abruf-Ausgabe (der rohe BM25-Wert
    gehoert nicht in die oeffentliche Form), ``bm25_score`` der des
    Retriever-Treffers.
    """
    wert = item.get("bm25_score")
    if wert is None:
        wert = item.get("_bm25")
    return isinstance(wert, (int, float)) and wert > 0


def passes(item: dict, *, categories: Optional[Iterable[str]] = None,
           exclude: Optional[Iterable[str]] = None,
           min_score: Optional[float] = None) -> bool:
    """Besteht dieser Eintrag die Auswahlregel?

    ``exclude`` schlaegt ``categories``: eine Kategorie, die in beiden Listen
    steht, ist draussen — der Ausschluss ist die strengere Aussage.
    """
    kategorie = effective_category(item)
    if categories:
        if kategorie not in set(categories):
            return False
    if exclude and kategorie in set(exclude):
        return False
    if min_score is not None:
        wert = relevance(item)
        if wert is not None:
            if wert < min_score:
                return False
        elif not has_lexical_evidence(item):
            # Keine vergleichbare Zahl und kein Worttreffer: nichts, was fuer
            # den Eintrag spricht.
            return False
    return True


def select(items: Iterable[dict], *, categories: Optional[Iterable[str]] = None,
           exclude: Optional[Iterable[str]] = None,
           min_score: Optional[float] = None,
           limit: Optional[int] = None) -> list[dict]:
    """Die Auswahl in einem Schritt: filtern, dann kuerzen. Reihenfolge bleibt."""
    erlaubte = normalize_categories(categories)
    verboten = normalize_exclude(exclude)
    schwelle = normalize_min_score(min_score)
    gewaehlt = [
        item for item in items
        if passes(item, categories=erlaubte, exclude=verboten, min_score=schwelle)
    ]
    if limit is not None:
        gewaehlt = gewaehlt[:max(0, int(limit))]
    return gewaehlt
