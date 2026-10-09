"""Prueft, welche Zugriffsstufe jedes Plugin WIRKLICH sendet.

Nebo-Auftrag 09.10.2026: "Die accept-Bloecke moechte ich auch, dass du ueberpruefst
... lieber einmal mehr pruefen ... Nicht, dass sich bei 90 Stueck noch irgendwo
1, 2 oder mehr falsch uebermitteln und wir denken, alles ist in Ordnung."

Dieser Test misst am echten Verhalten: Er faengt die HTTP-Anfragen ab, die die
Plugin-Skripte bauen, und liest die Zugriffsstufe aus der Nutzlast. Geprueft wird
gegen die Rangfolge public < trusted < private und gegen die festgelegten Rollen:
  - claude-code = trusted (Nebo-Entscheid 02.09.2026)
  - unbekannter Agent = public (der sichere Rueckfall)
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys
import urllib.request
import uuid

import pytest

WURZEL = pathlib.Path(__file__).resolve().parents[1]
SKRIPTE = WURZEL / "plugins" / "claude-code" / "scripts"
RANG = ("public", "trusted", "private")


def _lade(pfad: pathlib.Path, name: str):
    spec = importlib.util.spec_from_file_location(name, pfad)
    assert spec and spec.loader, f"{pfad} nicht ladbar"
    modul = importlib.util.module_from_spec(spec)
    sys.modules[name] = modul
    spec.loader.exec_module(modul)
    return modul


def _abgefangene_nutzlast(aufruf) -> list[dict]:
    """Fuehrt ``aufruf()`` aus und liefert die Nutzlasten der gebauten Anfragen."""
    gesammelt: list[dict] = []
    echter_request = urllib.request.Request
    echter_urlopen = urllib.request.urlopen

    class _Stop(Exception):
        pass

    def _request(url, *args, **kwargs):
        daten = kwargs.get("data")
        if daten:
            try:
                gesammelt.append(
                    {"url": str(url), "koerper": json.loads(daten.decode())}
                )
            except Exception:
                pass
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
        pass
    finally:
        urllib.request.Request = echter_request
        urllib.request.urlopen = echter_urlopen
    return gesammelt


def test_rangfolge_ist_aufsteigend():
    """public < trusted < private — die Grundlage jeder Sichtbarkeitspruefung."""
    assert RANG == ("public", "trusted", "private")
    for i, stufe in enumerate(RANG):
        # Jede Stufe sieht sich selbst und alle darunter.
        sichtbar = RANG[: i + 1]
        assert stufe in sichtbar
        for hoeher in RANG[i + 1 :]:
            assert hoeher not in sichtbar, f"{stufe} darf {hoeher} nicht sehen"


def test_claude_code_schreibt_trusted():
    """Claude Code ist 'trusted' — es darf NICHTS als 'private' ablegen.

    Nebo-Entscheid 02.09.2026: Claude Code sieht keine privaten Erinnerungen.
    Ein 'private' aus diesem Plugin waere ein Datenschutzverstoss.
    """
    modul = _lade(SKRIPTE / "auto_capture.py", "_zugriff_capture")
    echter = modul.get_embedding
    modul.get_embedding = lambda *a, **k: [0.0] * 1024
    marke = f"zugriff-{uuid.uuid4().hex[:10]}"
    # NEXUS_AGENT_ID ausdruecklich setzen UND danach wiederherstellen. Ohne das
    # erbte dieser Test die Umgebung des Aufrufers, und ein vorheriger Test, der
    # die Variable gesetzt hatte, verfaelschte das Ergebnis — bzw. dieser Test
    # hinterliess einen Zustand, an dem spaetere Tests scheiterten (gemessen
    # 09.10.2026: die Testdatei fiel in einer Reihenfolge durch, in der anderen nicht).
    alt_id = os.environ.get("NEXUS_AGENT_ID")
    os.environ["NEXUS_AGENT_ID"] = "claude-code"
    try:
        anfragen = _abgefangene_nutzlast(
            lambda: modul.store_memory(f"Zugriffspruefung {marke}", category="temp")
        )
    finally:
        modul.get_embedding = echter
        if alt_id is None:
            os.environ.pop("NEXUS_AGENT_ID", None)
        else:
            os.environ["NEXUS_AGENT_ID"] = alt_id

    stufen = []
    for a in anfragen:
        for punkt in a["koerper"].get("points", []):
            stufe = (punkt.get("payload") or {}).get("access_level")
            if stufe:
                stufen.append(stufe)

    assert stufen, f"keine Zugriffsstufe in der Nutzlast gefunden ({anfragen})"
    for stufe in stufen:
        assert stufe in RANG, f"ungueltige Zugriffsstufe gesendet: {stufe!r}"
        assert stufe == "trusted", (
            f"Claude Code sendet access_level={stufe!r}, erwartet 'trusted'. "
            "'private' waere ein Datenschutzverstoss, 'public' eine stille Herabstufung."
        )


def test_unbekannter_agent_faellt_auf_public():
    """Ohne Eintrag in der Registry gilt die SICHERSTE Stufe, nicht die freundlichste.

    Der Rueckfall darf nie 'private' sein (dann saehe ein Fremder alles) und nie
    stillschweigend passieren (dann merkt es niemand).

    Zwei Rueckfaelle sind zu pruefen, nicht einer: der leere Agentenname und der
    unbekannte Agent. Ein frueherer Gegentest traf nur den ersten und blieb bei
    manipuliertem zweitem gruen (gemessen 09.10.2026) — genau die Stelle, die
    zaehlt, war ungeprueft.
    """
    modul = _lade(SKRIPTE / "auto_capture.py", "_zugriff_fallback")
    alt = os.environ.get("NEXUS_AGENT_ID")
    try:
        for beschreibung, wert in (
            ("leerer Agentenname", ""),
            ("unbekannter Agent", "gibt-es-nicht-xyz"),
            ("Agent mit ungueltiger Stufe", "agent-mit-quatsch"),
        ):
            os.environ["NEXUS_AGENT_ID"] = wert
            stufe = modul._resolve_trust_level()
            assert stufe in RANG, f"{beschreibung}: ungueltige Stufe {stufe!r}"
            assert stufe == "public", (
                f"{beschreibung} bekam {stufe!r}. Der Rueckfall muss die SICHERSTE "
                "Stufe sein, sonst sieht ein Fremder fremde Erinnerungen."
            )
    finally:
        if alt is None:
            os.environ.pop("NEXUS_AGENT_ID", None)
        else:
            os.environ["NEXUS_AGENT_ID"] = alt


def test_ungueltige_stufe_in_der_registry_faellt_auf_public():
    """Steht in der Registry eine erfundene Stufe, gilt die sichere.

    Eine Registry-Datei ist eine gewoehnliche Datei — sie kann von Hand bearbeitet
    oder beschädigt worden sein. Ein Wert wie ``"admin"`` oder ``"Private"`` darf
    nicht durchgereicht werden.
    """
    modul = _lade(SKRIPTE / "auto_capture.py", "_zugriff_ungueltig")
    datei = pathlib.Path(modul.AGENTS_FILE)
    if not datei.exists():
        pytest.skip("agents.json nicht vorhanden")

    import tempfile

    original = datei.read_text()
    kaputt = json.loads(original)
    if not kaputt.get("agents"):
        pytest.skip("keine Agenten eingetragen")
    kaputt["agents"][0]["trust_level"] = "admin"  # erfundene, hoehere Stufe

    ziel = pathlib.Path(tempfile.mkdtemp()) / "agents.json"
    ziel.write_text(json.dumps(kaputt))
    alt_datei = modul.AGENTS_FILE
    alt_id = os.environ.get("NEXUS_AGENT_ID")
    modul.AGENTS_FILE = str(ziel)
    os.environ["NEXUS_AGENT_ID"] = kaputt["agents"][0]["id"]
    try:
        stufe = modul._resolve_trust_level()
    finally:
        modul.AGENTS_FILE = alt_datei
        if alt_id is None:
            os.environ.pop("NEXUS_AGENT_ID", None)
        else:
            os.environ["NEXUS_AGENT_ID"] = alt_id

    assert stufe == "public", (
        f"Eine erfundene Stufe 'admin' wurde als {stufe!r} durchgereicht. "
        "Ungueltige Werte muessen auf die SICHERSTE Stufe fallen, nie auf eine hoehere."
    )


def test_gemeldeter_agent_bekommt_seine_stufe():
    """Und die Gegenprobe: ein eingetragener Agent bekommt genau seine Stufe.

    Ohne diesen Test wuerde ein Fehler in der Aufloesung nur auffallen, wenn er
    zufaellig auf einen unbekannten Agenten faellt.
    """
    modul = _lade(SKRIPTE / "auto_capture.py", "_zugriff_bekannt")
    datei = pathlib.Path(modul.AGENTS_FILE)
    if not datei.exists():
        pytest.skip("agents.json nicht vorhanden")

    registry = json.loads(datei.read_text())
    eintraege = registry.get("agents", [])
    if not eintraege:
        pytest.skip("keine Agenten eingetragen")

    alt = os.environ.get("NEXUS_AGENT_ID")
    geprueft = 0
    try:
        for eintrag in eintraege:
            erwartet = eintrag.get("trust_level")
            if erwartet not in RANG:
                continue
            os.environ["NEXUS_AGENT_ID"] = eintrag["id"]
            tatsaechlich = modul._resolve_trust_level()
            assert tatsaechlich == erwartet, (
                f"Agent {eintrag['id']!r}: Registry sagt {erwartet!r}, "
                f"das Plugin liest {tatsaechlich!r}"
            )
            geprueft += 1
    finally:
        if alt is None:
            os.environ.pop("NEXUS_AGENT_ID", None)
        else:
            os.environ["NEXUS_AGENT_ID"] = alt

    assert geprueft >= 2, (
        f"nur {geprueft} Agenten pruefbar — die Registry wirkt unerwartet leer"
    )
