"""Regressionstest: Das Claude-Code-Plugin muss ins Gedaechtnis SCHREIBEN koennen.

Fund 09.10.2026: ``auto_capture.store_memory`` baute einen ``urllib.request.Request``
ohne ``method=``. urllib nimmt dann POST, Qdrant verlangt fuer das Anlegen von Punkten
aber PUT. Jeder Schreibversuch endete mit HTTP 400 "missing field `ids`", der
umgebende ``except`` schluckte den Fehler, und der Hook meldete nur nach stderr.
Folge: Claude Code las Erinnerungen, schrieb aber nie welche.

Dieser Test faehrt den echten Weg: schreiben, mit dem offiziellen Client zuruecklesen,
aufraeumen. Er prueft ausdruecklich die HTTP-Methode, damit derselbe Fehler nicht
unbemerkt zurueckkommt.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import urllib.request
import uuid

import pytest

WURZEL = pathlib.Path(__file__).resolve().parents[1]
SKRIPTE = WURZEL / "plugins" / "claude-code" / "scripts"

# Die Sammlung NIE hartkodieren. ``tests/conftest.py`` setzt fuer die Suite
# ``NEXUS_COLLECTION=test-collection``; der Hook-Layer liest denselben Wert aus
# der Umgebung. Ein Hardcode auf "nexus_qwen" liess Schreiben und Lesen auf
# VERSCHIEDENE Sammlungen zeigen: der Punkt landete in test-collection, die
# Suche lief gegen nexus_qwen, der Test schlug fehl — und der Aufraeum- Schritt
# im ``finally`` loeschte aus der falschen Sammlung, sodass die Testpunkte als
# Dauerleichen liegen blieben.
SAMMLUNG = os.environ.get("NEXUS_COLLECTION", "nexus_qwen")
QDRANT = os.environ.get("NEXUS_QDRANT_URL", "http://127.0.0.1:6333")


def _qdrant_erreichbar() -> bool:
    try:
        with urllib.request.urlopen(f"{QDRANT}/collections", timeout=4):
            return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _qdrant_erreichbar(), reason="Qdrant nicht erreichbar"
)


@pytest.fixture(scope="module")
def capture_modul():
    if not (SKRIPTE / "auto_capture.py").exists():
        pytest.skip("Claude-Code-Plugin nicht vorhanden")
    sys.path.insert(0, str(SKRIPTE))
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_cc_auto_capture_under_test", SKRIPTE / "auto_capture.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _abgefangene_anfragen(aufruf) -> list[tuple[str, str]]:
    """Ruft ``aufruf()`` auf und liefert die tatsaechlich gebauten HTTP-Anfragen.

    Das ist der entscheidende Unterschied zu jeder Textpruefung: Hier wird das
    VERHALTEN gemessen. Egal wie eine Adresse zusammengesetzt wird — ueber eine
    Funktion, eine Verkettung, einen Alias-Import oder ``getattr`` —, die Anfrage
    entsteht im Speicher und wird hier abgegriffen. Fuenf Textmuster-Fassungen
    waren umgehbar (Review deleg_91cbd798, 09.10.2026); diese Pruefung nicht.

    Der Netzaufruf wird unterbunden, sobald die Anfrage gebaut ist: wir brauchen
    nur die Methode und die Adresse.
    """
    import urllib.request

    abgefangen: list[tuple[str, str]] = []
    echter_request = urllib.request.Request
    echter_urlopen = urllib.request.urlopen

    # Der Abgriff muss den Zustand der geprueften Module UNVERSEHRT lassen.
    # ``scope_auto.vector_field`` legt sein Ergebnis in ``_LAYOUT_CACHE`` ab.
    # Wird der Aufruf mittendrin abgebrochen (unser ``_Stop``), landet dort ein
    # falsches ``None`` — und danach baut ``_point_vector`` fuer eine Sammlung mit
    # benanntem Vektor eine flache Liste, die Qdrant mit 400 ablehnt. Der Test
    # vergiftete damit seine eigene Umgebung und liess die folgenden Tests
    # scheitern, obwohl das Plugin in Ordnung war (Review deleg_7312e7a0).
    zwischengespeichert: list = []
    try:
        sys.path.insert(0, str(SKRIPTE))
        import scope_auto as _sa

        zwischengespeichert.append((_sa, dict(_sa._LAYOUT_CACHE)))
    except Exception:
        pass

    class _Stop(Exception):
        pass

    def _request(url, *args, **kwargs):
        methode = kwargs.get("method")
        if methode is None:
            # urllib nimmt POST, sobald Daten mitgehen, sonst GET.
            methode = "POST" if kwargs.get("data") is not None else "GET"
        abgefangen.append((str(url), str(methode).upper()))
        return echter_request(url, *args, **kwargs)

    def _urlopen(*args, **kwargs):
        raise _Stop

    urllib.request.Request = _request
    urllib.request.urlopen = _urlopen
    try:
        aufruf()
    except _Stop:
        pass
    except Exception:
        # Fehlende Sammlung, kein Netz, fehlende Umgebung — gleichgueltig: die
        # Anfragen wurden bereits gebaut und abgefangen.
        pass
    finally:
        urllib.request.Request = echter_request
        urllib.request.urlopen = echter_urlopen
        # Zwischenspeicher zuruecksetzen, damit kein falscher Eintrag stehen bleibt.
        for modul, sicherung in zwischengespeichert:
            modul._LAYOUT_CACHE.clear()
            modul._LAYOUT_CACHE.update(sicherung)
    return abgefangen


def test_lesewege_senden_POST(capture_modul):
    """Der Haertetest: Jede echte Anfrage an search/scroll muss POST sein.

    Gemessen wird das Verhalten, nicht der Quelltext. Damit sind alle fuenf
    Umgehungswege der Textpruefung wirkungslos: Adresse aus Funktionsrueckgabe,
    annotierte Konstante, String-Verkettung, Alias-Import, ``getattr``.
    Grundlage: Qdrant erwartet auf ``/points/search`` und ``/points/scroll`` POST.
    """
    sys.path.insert(0, str(SKRIPTE))
    import importlib.util

    def _lade(name):
        spec = importlib.util.spec_from_file_location(
            f"_cc_{name}_under_test", SKRIPTE / f"{name}.py"
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod

    vektor = [0.0] * 1024
    module_und_aufrufe = []

    # (Modulname, was aufzurufen ist) — jede Datei mit einem Leseweg.
    module_und_aufrufe.append(("auto_recall", lambda m: m.search_qdrant(vektor, limit=1)))
    module_und_aufrufe.append(("session_start", lambda m: m.search_qdrant(vektor, limit=1)))
    module_und_aufrufe.append(
        ("scope_auto", lambda m: m.fetch_centroids(m.QDRANT_URL, m.COLLECTION))
    )
    module_und_aufrufe.append(("guardrail_check", lambda m: m.load_protection_rules()))

    gepruefte_lesewege = 0
    for name, aufruf in module_und_aufrufe:
        if not (SKRIPTE / f"{name}.py").exists():
            continue
        modul = _lade(name)
        anfragen = _abgefangene_anfragen(lambda: aufruf(modul))
        for url, methode in anfragen:
            if "/points/search" in url or "/points/scroll" in url:
                gepruefte_lesewege += 1
                assert methode != "PUT", (
                    f"{name}.py: eine echte Anfrage an einen Leseweg geht als PUT "
                    f"hinaus ({url!r}). Qdrant erwartet dort POST — das Lesen ist "
                    "damit kaputt."
                )

    assert gepruefte_lesewege >= 3, (
        f"Nur {gepruefte_lesewege} echte Leseweg-Anfragen gemessen — der Test hat "
        "die Lesewege nicht erreicht (kein Aufruf gelungen?). Er wuerde ins Leere laufen."
    )


def test_schreibweg_nutzt_PUT():
    """Qdrant lehnt POST auf diesem Endpunkt mit 400 ab. Ohne PUT schreibt
    das Plugin nichts — und es sagt es nicht.

    Gemessen am echten Verhalten: Die Einbettung wird durch einen Platzhalter
    ersetzt (sonst bricht ``store_memory`` VOR dem Aufbau der Anfrage ab und es
    gaebe nichts zu messen), dann wird die tatsaechlich abgeschickte Anfrage
    geprueft — kein Textmuster.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_cc_capture_write_check", SKRIPTE / "auto_capture.py"
    )
    modul = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = modul
    spec.loader.exec_module(modul)

    echter_embedder = modul.get_embedding
    modul.get_embedding = lambda *a, **k: [0.0] * 1024
    try:
        anfragen = _abgefangene_anfragen(
            lambda: modul.store_memory("Methodenpruefung", category="temp")
        )
    finally:
        modul.get_embedding = echter_embedder

    schreibwege = [
        (u, m)
        for u, m in anfragen
        # Abfrageparameter abschneiden: die Adresse heisst jetzt
        # ``.../points?wait=true``. Ein Vergleich auf ``endswith("/points")``
        # scheiterte daran und meldete "kein Schreibaufruf gemessen", obwohl der
        # PUT korrekt dastand (Review deleg_7312e7a0).
        if u.split("?")[0].rstrip("/").endswith("/points")
    ]
    assert schreibwege, (
        f"Kein Schreibaufruf auf /points gemessen (abgefangen: {anfragen}) — "
        "der Test erreicht die Stelle nicht."
    )
    for url, methode in schreibwege:
        assert methode == "PUT", (
            f"Der Schreibaufruf auf /points sendet {methode}, nicht PUT ({url!r}). "
            "Qdrant antwortet darauf mit 400 und der Schreibvorgang verschwindet still."
        )
        # ``wait=true`` ist ebenso wichtig wie die Methode: ohne den Parameter
        # antwortet Qdrant mit 200 "acknowledged" und wendet den Auftrag im
        # HINTERGRUND an. Ein ABGELEHNTER Schreibvorgang saehe dann wie Erfolg aus.
        # Die Ruecklese-Tests koennen das nicht bemerken, weil sie auf die
        # Anwendung warten (Review-Fund F1 vom 09.10.2026) — deshalb wird es hier
        # am echten Aufruf geprueft.
        assert "wait=true" in url, (
            f"Der Schreibaufruf auf /points traegt kein wait=true ({url!r}). Qdrant "
            "bestaetigt dann nur ('acknowledged') und ein abgelehnter Schreibvorgang "
            "bliebe unbemerkt — genau der stille Fehler vom 09.10.2026."
        )


def _ohne_kommentare(quelle: str) -> str:
    """Quelltext mit ausgeblendeten Kommentaren und Docstrings.

    Die ausgeblendeten Stuecke werden durch Leerzeichen ERSETZT, nicht entfernt:
    so bleiben die Zeichenpositionen (und damit die Zeilennummern in Meldungen)
    erhalten. Ein blosses Zusammenfuegen der Tokens wuerde Leerzeichen zwischen
    alle Bezeichner setzen und jedes Suchmuster zerstoeren.

    Noetig, weil ``auto_capture.py`` die Lesewege nur in einem Kommentar nennt.
    Ohne das Ausblenden gaelte die Datei faelschlich als Leseweg-Datei
    (Review-Fund 3, 09.10.2026).
    """
    import io
    import tokenize

    zeilen = quelle.splitlines(keepends=True)
    # Zeichenposition jeder Zeile vorab berechnen.
    starts: list[int] = []
    pos = 0
    for z in zeilen:
        starts.append(pos)
        pos += len(z)
    zeichen = list(quelle)

    try:
        for tok in tokenize.generate_tokens(io.StringIO(quelle).readline):
            ist_docstring = tok.type == tokenize.STRING and tok.string.startswith(
                ('"""', "'''")
            )
            if tok.type != tokenize.COMMENT and not ist_docstring:
                continue
            (z0, s0), (z1, s1) = tok.start, tok.end
            for nr in range(z0, z1 + 1):
                if nr > len(zeilen):
                    continue
                anfang = starts[nr - 1] + (s0 if nr == z0 else 0)
                ende = anfang + (
                    len(zeilen[nr - 1].rstrip("\n")) - (s0 if nr == z0 else 0)
                    if nr < z1
                    else (s1 - s0 if nr == z0 else s1)
                )
                for i in range(anfang, min(ende, len(zeichen))):
                    if zeichen[i] not in "\n\r":
                        zeichen[i] = " "
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return quelle
    return "".join(zeichen)


def test_lesewege_bleiben_POST():
    """Auf /points/search und /points/scroll erwartet Qdrant POST. Wer dort PUT
    setzt, macht das Lesen kaputt.

    Vier Review-Funde vom 09.10.2026 stecken in dieser Fassung:

    1. Die erste Fassung sah nur ``auto_capture.py`` an — dort gibt es gar keine
       Lesewege, die Schleife lief ueber null Treffer und der Test war
       wirkungslos.
    2. Ein festes Zeichenfenster rechts vom Aufruf uebersah ``guardrail_check.py``,
       wo die Adresse zehn Zeilen VOR dem Aufruf in eine Variable gelegt wird.
    3. Eine Ausnahme fuer Dateien mit Schreibweg machte sie blind, weil
       ``auto_capture.py`` die Lesewege nur in einem KOMMENTAR erwaehnt.
    4. Auch die Syntaxbaum-Fassung hatte Luecken (Adresse aus Funktionsrueckgabe,
       zusammengesetzte Konstanten).

    Deshalb der robuste Anker: Die ADRESSE selbst (``/points/search`` oder
    ``/points/scroll``) ist der Bezugspunkt, und in ihrer weiteren Umgebung darf
    kein ``method="PUT"`` stehen — gleichgueltig, wie die Adresse gebaut wurde.
    Zusaetzlich deckt ``test_leseweg_funktioniert`` den Pfad funktional ab.
    """
    import re

    verboten = ('method="PUT"', "method='PUT'", 'method = "PUT"', "method = 'PUT'")
    ankermuster = r"/points/(?:search|scroll)"
    dateien_mit_leseweg: list[str] = []

    for datei in sorted(SKRIPTE.glob("*.py")):
        quelle = datei.read_text()
        # Kommentare und Docstrings ausblenden, damit blosse Erwaehnungen nicht
        # als Leseweg zaehlen (auto_capture.py nennt sie im Kommentar).
        ohne_kommentar = _ohne_kommentare(quelle)
        treffer = list(re.finditer(ankermuster, ohne_kommentar))
        if not treffer:
            continue
        dateien_mit_leseweg.append(datei.name)
        for m in treffer:
            umfeld = ohne_kommentar[max(0, m.start() - 700) : m.end() + 700]
            for v in verboten:
                assert v not in umfeld, (
                    f"{datei.name}: Leseweg {m.group(0)!r} bei Zeichen {m.start()} "
                    f"hat ein {v!r} in der Umgebung. Qdrant erwartet dort POST — "
                    "PUT gehoert nicht hin."
                )

    for erwartet in ("auto_recall.py", "scope_auto.py", "session_start.py", "guardrail_check.py"):
        assert erwartet in dateien_mit_leseweg, (
            f"{erwartet} nicht als Datei mit Leseweg erfasst "
            f"(gefunden: {dateien_mit_leseweg}) — die Pruefung uebersieht eine Stelle."
        )
    assert "auto_capture.py" not in dateien_mit_leseweg, (
        "auto_capture.py wurde faelschlich als Datei mit Leseweg gezaehlt — dort "
        "steht der Pfad nur in einem Kommentar."
    )


def _scrolle_mit_geduld(sammlung: str, marke: str, versuche: int = 15):
    """Findet einen Testpunkt und gibt Qdrant Zeit, ihn anzuwenden.

    Zwei Fallen stecken hier, beide am 09.10.2026 gemessen und behoben:

    1. ``store_memory`` schreibt OHNE ``wait=true`` — Qdrant bestaetigt den
       Auftrag (HTTP 200), wendet ihn aber asynchron an. Ein sofortiges
       Zuruecklesen findet den Punkt daher manchmal nicht.
    2. Die Suche darf NICHT ueber ``MatchText`` filtern. Das setzt einen
       Text-Index auf dem Feld voraus; die Testsammlung hat keinen, also liefert
       der Filter dort grundsaetzlich nichts — unabhaengig davon, ob der Punkt
       existiert. Deshalb wird hier seitenweise gelesen und in Python verglichen.
    """
    import time

    from qdrant_client import QdrantClient

    C = QdrantClient(url=QDRANT)
    for _ in range(versuche):
        treffer = []
        offset = None
        while True:
            seite, offset = C.scroll(
                sammlung, limit=500, offset=offset, with_payload=True
            )
            for punkt in seite:
                if marke in str((punkt.payload or {}).get("text", "")):
                    treffer.append(punkt)
            if offset is None or treffer:
                break
        if treffer:
            return treffer
        time.sleep(0.2)
    return []


def _loesche(sammlung: str, ids: list) -> None:
    """Entfernt Testpunkte — mit ``wait=True``, damit das Loeschen greift.

    Ohne die Wartezeit konnte ein Loeschauftrag unbemerkt liegen bleiben; genau
    so sind am 09.10.2026 Testleichen in der Sammlung zurueckgeblieben.
    """
    if not ids:
        return
    from qdrant_client import QdrantClient
    from qdrant_client.http import models as qm

    QdrantClient(url=QDRANT).delete(
        sammlung, points_selector=qm.PointIdsList(points=list(ids)), wait=True
    )


def test_leseweg_funktioniert(capture_modul):
    """Der funktionale Gegentest: Der LESE-Pfad des Plugins muss Daten liefern.

    Gemessen wird ueber ``auto_recall.search_qdrant`` — den Weg, den der Hook
    wirklich geht —, NICHT ueber die Qdrant-Bibliothek. Eine fruehere Fassung las
    mit ``QdrantClient.scroll`` und blieb gruen, selbst wenn der Leseweg des
    Plugins auf PUT umgestellt war: sie testete damit nur den Schreibweg und gab
    falsche Sicherheit (Review deleg_7312e7a0, experimentell belegt).
    """
    import importlib.util

    sammlung = _erwartete_sammlung(capture_modul)
    marke = f"lesetest-{uuid.uuid4().hex[:10]}"
    inhalt = f"Lesepfad-Pruefung {marke}: dieser Text muss auffindbar sein."

    assert capture_modul.store_memory(inhalt, category="temp") is True, (
        "Der Schreibweg funktioniert nicht — ohne ihn ist der Lesetest sinnlos."
    )

    # Der echte Plugin-Lesepfad. Er braucht ein Embedding; das ersetzen wir durch
    # einen Platzhalter, damit der Test nicht vom Embedding-Dienst abhaengt.
    spec = importlib.util.spec_from_file_location(
        "_cc_auto_recall_under_test", SKRIPTE / "auto_recall.py"
    )
    assert spec and spec.loader, "auto_recall.py nicht ladbar"
    recall = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = recall
    spec.loader.exec_module(recall)

    # ECHTES Embedding, kein Nullvektor: ein Nullvektor hat keine Richtung, die
    # Kosinus-Aehnlichkeit ist dann 0 und die Suche findet grundsaetzlich nichts.
    # Genau daran scheiterte eine fruehere Fassung dieses Tests (gemessen
    # 09.10.2026) — sie schlug im Normalfall fehl, ohne dass etwas kaputt war.
    vektor = recall.get_embedding("Lesepfad-Pruefung")
    if not vektor:
        pytest.skip("Kein Embedding verfuegbar (Embedding-Dienst nicht erreichbar)")

    treffer: list = []
    try:
        treffer = recall.search_qdrant(vektor, limit=10) or []
    finally:
        # Der Lesepfad legt sein Layout zwischen; das darf nicht stehen bleiben.
        if hasattr(recall, "_LAYOUT_CACHE"):
            recall._LAYOUT_CACHE.clear()

    try:
        assert treffer, (
            f"Der Lesepfad des Plugins lieferte keine Treffer (gesucht: {marke}). "
            "Qdrant erwartet auf /points/search POST — ein PUT dort bricht das Lesen."
        )
    finally:
        _loesche(
            sammlung,
            [p.id for p in _scrolle_mit_geduld(sammlung, marke)],
        )


def _erwartete_sammlung(capture_modul) -> str:
    """Die Sammlung, in die das Plugin WIRKLICH schreibt.

    Nicht aus der Umgebung raten: ``auto_capture`` loest die Sammlung selbst auf
    (Umgebungsvariable, dann die zentrale ``.env``, dann Standard). Nimmt der Test
    einen anderen Wert an, zeigt Schreiben und Lesen auf verschiedene Sammlungen —
    und der Aufraeum-Schritt loescht aus der falschen. Genau so blieb am
    09.10.2026 ein Testpunkt in der echten Sammlung liegen.
    """
    return getattr(capture_modul, "COLLECTION", None) or os.environ.get(
        "NEXUS_COLLECTION", "nexus_qwen"
    )


def test_schreiben_und_zuruecklesen(capture_modul):
    """Der scharfe Beweis: ein Punkt faellt an, ist auffindbar und laesst
    sich wieder entfernen."""
    sammlung = _erwartete_sammlung(capture_modul)
    marke = f"regression-{uuid.uuid4().hex[:10]}"
    text = f"Regressionstest {marke}: der Schreibweg des Claude-Plugins funktioniert."

    gespeichert = capture_modul.store_memory(text, category="temp")
    assert gespeichert is True, (
        "store_memory() meldete einen Fehler — der Schreibweg ist kaputt "
        "(haeufigste Ursache: fehlendes method=\"PUT\")"
    )

    treffer = _scrolle_mit_geduld(sammlung, marke)
    try:
        assert treffer, (
            f"Geschriebener Punkt {marke} ist nicht auffindbar. Der Punkt wurde "
            "gemeldet, aber nicht gefunden — Schreib- und Lesesammlung stimmen "
            "moeglicherweise nicht ueberein."
        )
    finally:
        _loesche(sammlung, [p.id for p in treffer])


def test_schreibt_trusted_nicht_private(capture_modul):
    """Claude Code ist Stufe 'trusted' und darf NICHTS als 'private' ablegen
    (Nebo-Entscheid 02.09.2026)."""
    sammlung = _erwartete_sammlung(capture_modul)
    marke = f"trustcheck-{uuid.uuid4().hex[:10]}"
    capture_modul.store_memory(f"Trustpruefung {marke}", category="temp")

    punkte = _scrolle_mit_geduld(sammlung, marke)
    try:
        assert punkte, (
            f"Trustpruefung {marke} konnte nicht zurueckgelesen werden — ohne "
            "gelesenen Punkt ist die Stufe nicht pruefbar."
        )
        stufe = (punkte[0].payload or {}).get("access_level")
        assert stufe == "trusted", (
            f"Claude Code muss 'trusted' schreiben, schrieb aber {stufe!r}. "
            "Es darf keine privaten Erinnerungen sehen oder anlegen."
        )
    finally:
        _loesche(sammlung, [p.id for p in punkte])
