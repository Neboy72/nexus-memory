"""Session-Fakten: keine Doppel, keine Fragmente (16.11.2026).

Gemessen: Der Bestand hatte fuenf zeichengleiche Paare, jeweils gleicher Text,
gleicher Agent, gleiche Quelle, nur vier Stunden auseinander. Ursache: Ein
Sitzungsende kann mehrfach ausgeloest werden (Gateway-Neustart, Ablauf) und
liefert dann dieselben Fakten. Entitaeten hatten dafuer laengst eine feste
Kennung — Fakten nicht.

Dazu die zweite Beobachtung des meldenden Agenten: Ein Teil der Eintraege war
keine Aussage, sondern eine Anrede an den Agenten, eine Rueckfrage oder ein
abgeschnittener Satz. Welche der gemeldeten Texte der Filter durchlaesst und
welche nicht, steht weiter unten als Liste — die Tests sind die Quelle, nicht
diese Beschreibung.
"""

import uuid

import pytest

from nexus_memory.extractor import _looks_like_fragment


# ---------- Feste Kennung fuer Session-Fakten ----------

class _Dedup:
    """Nur die Schluesselbildung und die Existenzpruefung, ohne Qdrant."""

    def __init__(self, vorhandene_ids=()):
        self.vorhanden = set(vorhandene_ids)
        self.geschrieben = []

    def key(self, text, source="hermes-plugin-session-end"):
        klar = " ".join((text or "").split()).strip().lower().strip(" .,;:!?\"'")
        return f"{source}|{klar}"

    def id_for(self, text, source="hermes-plugin-session-end"):
        return str(uuid.uuid5(uuid.NAMESPACE_DNS, self.key(text, source)))

    def write(self, text):
        pid = self.id_for(text)
        if pid in self.vorhanden:
            return "skipped"
        self.vorhanden.add(pid)
        self.geschrieben.append(pid)
        return "written"


def test_same_fact_gets_the_same_id():
    """Zwei Laeufe mit demselben Fakt muessen denselben Punkt treffen."""
    d = _Dedup()
    a = d.id_for("Der Dienst laeuft auf Port 9122.")
    b = d.id_for("der dienst   laeuft auf port 9122")
    assert a == b


def test_different_facts_get_different_ids():
    d = _Dedup()
    assert d.id_for("Der Dienst laeuft auf Port 9122.") != d.id_for("Der Dienst laeuft auf Port 9123.")


def test_second_run_writes_nothing_new():
    """Der Kern des Fixes: der zweite Durchlauf legt keine Kopie an."""
    d = _Dedup()
    fakten = [
        "Der Dienst laeuft auf Port 9122.",
        "Nexus Memory ist auf 0.22.52 aktualisiert.",
    ]
    erste = [d.write(f) for f in fakten]
    zweite = [d.write(f) for f in fakten]
    assert erste == ["written", "written"]
    assert zweite == ["skipped", "skipped"]
    assert len(d.geschrieben) == 2


def test_source_is_part_of_the_key():
    """Derselbe Wortlaut aus zwei Quellen bleibt zwei Eintraege — die Herkunft
    gehoert zur Kennung, sonst wuerde ein Zitat den Originaleintrag ersetzen."""
    d = _Dedup()
    text = "Der Dienst laeuft auf Port 9122."
    assert d.id_for(text, "hermes-plugin-session-end") != d.id_for(text, "documents")


# ---------- Gespraechsfetzen ----------

@pytest.mark.parametrize("text", [
    # die fuenf gemeldeten Eintraege: drei sind keine Fakten
    "Du hast den scheiss Kessel immer noch drin, aber immerhin ist es jetzt ordentlich",
    "Heisst das einmalig eingeschaltet ist immer da, weil eigentlich waere das ja abzufragen",
    "Warum zeigt das Dashboard 0.22.49?",
    "Kannst du das kurz erklaeren",
    "deine Antwort war falsch",
    "",
])
def test_fragments_are_rejected(text):
    assert _looks_like_fragment(text) is True


@pytest.mark.parametrize("text", [
    # die zwei belastbaren Eintraege aus demselben Lauf
    "Konkret zu deiner Frage, gerade nachgesehen: Der Harness auf dem Mac laeuft tatsaechlich nicht — kein Prozess, die Profile liegen nur da",
    "- hermes-agent: main = f65af53f28eb; Branch fix/session-authority-publication-order head 1982c29; 8 Dateien aus Branch auf main",
    # der dritte gemeldete Eintrag bleibt stehen: abgeschnittener Satz ohne
    # Punkt, aber ohne Frage- oder Anredeform. Bewusst in Kauf genommen —
    # eine Laengenregel haette echte Fakten wie die beiden folgenden verworfen.
    "Bei connected agents steht immer noch, bei Hermes Agent steht immer noch own key",
    # normale Aussagen und kurze echte Fakten
    "Der Dienst laeuft auf Port 9122.",
    "the user likes coffee",
    "Immer auf Deutsch antworten",
    "the user uses GLM-5.3-Flash",
])
def test_statements_survive(text):
    assert _looks_like_fragment(text) is False


# ---------- Die fuenf gemeldeten Originale, im Wortlaut ----------
# Damit ist belegt, was aus dem echten Lauf durchkommt und was nicht.

GEMELDETE_ECHO = "Bei connected agents steht immer noch, bei Hermes Agent steht immer noch own key"
GEMELDETE_ANREDE = "Du hast den scheiss Kessel immer noch drin, aber immerhin ist es jetzt ordentlich"
GEMELDETE_ANTWORT = (
    "Konkret zu deiner Frage, gerade nachgesehen: Der Harness auf dem Mac laeuft tatsaechlich "
    "nicht — kein Prozess, die Profile liegen nur da"
)
GEMELDETER_STAND = (
    "- hermes-agent: main = f65af53f28eb; Branch fix/session-authority-publication-order head "
    "1982c29; 8 Dateien aus Branch auf main zurueckschreiben lassen (Fix laeuft live, bewusst "
    "uncommitted)"
)
GEMELDTE_FRAGE = (
    "Heisst das einmalig eingeschaltet ist immer da, weil eigentlich waere das ja abzufragen "
    "und es muesste ja quasi aus sein oder rot sein, wenn er nicht verbunden ist, weil ich ihn "
    "gar nicht gestartet habe als Beispiel"
)


@pytest.mark.parametrize("text", [GEMELDETE_ANREDE, GEMELDTE_FRAGE])
def test_reported_fragments_are_caught(text):
    assert _looks_like_fragment(text) is True


@pytest.mark.parametrize("text", [GEMELDETE_ECHO, GEMELDETE_ANTWORT, GEMELDETER_STAND])
def test_reported_substance_survives(text):
    assert _looks_like_fragment(text) is False


def test_reported_batch_keeps_three_of_five():
    """Von den fuenf gemeldeten Eintraegen werden zwei als Fragment verworfen:
    die Anrede und die Frage. Die anderen drei bleiben — der abgeschnittene
    Satz, die Antwort mit Befund und der Repo-Stand. Genau das behauptet die
    Release-Notiz; der Test hat eine erste, zu optimistische Fassung
    ("drei von vier") widerlegt."""
    gemeldet = [GEMELDETE_ECHO, GEMELDETE_ANREDE, GEMELDETE_ANTWORT,
                GEMELDETER_STAND, GEMELDTE_FRAGE]
    verworfen = [t for t in gemeldet if _looks_like_fragment(t)]
    assert verworfen == [GEMELDETE_ANREDE, GEMELDTE_FRAGE]
    assert len(gemeldet) - len(verworfen) == 3
