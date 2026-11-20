"""Die Auswahlregel des Abrufs — an EINER Stelle, fuer Dienst und Plugins.

Warum (20.11.2026): Jeder Weg entschied selbst, was er in den Kontext legt.
Das DSH-Plugin nahm fuenf Eintraege ohne jede Schwelle, Claude Code drei
ueber eine Kosinus-Schwelle, OpenClaw zehn ohne Filter, Hermes einen eigenen
Vorlauf. Ein Filter, den nur ein Weg kennt, ist kein Filter, sondern eine
Eigenheit. Diese Datei ist die gemeinsame Regel; die Plugins reichen nur noch
die Schalter durch und entscheiden nichts selbst.

Zwei Hebel, beide opt-in (ohne Angabe bleibt das Verhalten wie vorher):

* ``categories`` — erlaubte Kategorien. Ohne Angabe wird nicht gefiltert.
  ``session`` und ``entity`` draussen zu lassen ist eine Entscheidung des
  AUFRUFERS (die Plugins setzen sie als Voreinstellung), nicht des Dienstes:
  ein ausdruecklicher ``recall`` ueber MCP darf weiterhin alles finden.
* ``min_score`` — Mindestpunktzahl auf der Kosinus-Skala (0..1).

Punktzahlen, nicht Raenge: Die Fusion ist RRF, und ein RRF-Wert ist kein
Aehnlichkeitsmass — er haengt an der Listenposition und belohnt jeden Treffer,
der ueberhaupt in der Kandidatenliste steht. Nach der Normalisierung auf den
besten Treffer hat der Spitzenreiter IMMER 1.0, auch wenn er zur Frage nicht
passt. Eine Schwelle darauf waere Zierde. Die vergleichbare Zahl ist der
Kosinus des Vektorbeins; sie kommt aus der Qdrant-Suche und ist ueber Fragen
hinweg vergleichbar.

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
#: Was automatisch eingespeist wird: Wissen, keine Protokolle.
DEFAULT_RECALL_CATEGORIES = ("fact", "rule", "preference")
#: Voreinstellung der Mindestpunktzahl (Kosinus, 0..1). 0.35 ist der Wert,
#: den der Claude-Code-Hook seit jeher als ``score_threshold`` benutzt; er
#: wird hier nicht neu erfunden, sondern uebernommen.
DEFAULT_RECALL_MIN_SCORE = 0.35

#: Kategorien, die der Dienst kennt (``nexus.MemoryCategory``). Eine
#: unbekannte Angabe wird abgelehnt statt still ignoriert: ein Tippfehler
#: darf nicht zu "kein Filter" werden.
KNOWN_CATEGORIES = frozenset({
    "fact", "belief", "session", "rule", "preference", "procedure", "temp",
})


class RecallPolicyError(ValueError):
    """Ungueltige Auswahlangabe — der Aufrufer soll es erfahren, nicht raten."""


def normalize_categories(value: Any) -> Optional[tuple[str, ...]]:
    """``categories`` als Tupel erlaubter Kategorien — ``None`` heisst: alle.

    Annahme: Liste/Tupel von Zeichenketten oder eine kommagetrennte Zeichenkette
    (damit auch Umgebungsvariablen und Kommandozeilen es koennen). Leer heisst
    "kein Filter"; eine unbekannte Kategorie ist ein Fehler.
    """
    if value is None:
        return None
    if isinstance(value, str):
        roh = [teil for teil in value.split(",")]
    elif isinstance(value, Iterable):
        roh = list(value)
    else:
        raise RecallPolicyError(
            f"categories muss eine Liste oder eine Zeichenkette sein, nicht {type(value).__name__}"
        )
    sauber: list[str] = []
    for teil in roh:
        name = str(teil).strip().lower()
        if not name:
            continue
        if name not in KNOWN_CATEGORIES:
            raise RecallPolicyError(
                f"unbekannte Kategorie {name!r} — erlaubt: {sorted(KNOWN_CATEGORIES)}"
            )
        if name not in sauber:
            sauber.append(name)
    return tuple(sauber) or None


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
    gehoert nicht in die oeffentliche Form), ``bm25_score`` der der
    Retriever-Treffer.
    """
    wert = item.get("bm25_score")
    if wert is None:
        wert = item.get("_bm25")
    return isinstance(wert, (int, float)) and wert > 0


def passes(item: dict, *, categories: Optional[Iterable[str]] = None,
           min_score: Optional[float] = None) -> bool:
    """Besteht dieser Eintrag die Auswahlregel?"""
    if categories:
        if effective_category(item) not in set(categories):
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
           min_score: Optional[float] = None,
           limit: Optional[int] = None) -> list[dict]:
    """Die Auswahl in einem Schritt: filtern, dann kuerzen. Reihenfolge bleibt."""
    kategorien = normalize_categories(categories)
    schwelle = normalize_min_score(min_score)
    gewaehlt = [item for item in items if passes(item, categories=kategorien, min_score=schwelle)]
    if limit is not None:
        gewaehlt = gewaehlt[:max(0, int(limit))]
    return gewaehlt
