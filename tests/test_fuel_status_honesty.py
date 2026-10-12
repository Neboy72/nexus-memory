"""Wachen fuer die Fuel-Kachel — sie darf nur nennen, was wirklich offen ist.

Der Fall, der uns gefehlt hat (12.10.2026): ein User ohne Ollama und ohne
Schluessel in der Umgebung. Beleg vom selben Tag, gefahren mit der ECHTEN Kette
und einer toten Ollama-Adresse:

    kette (1 station(en)):
      - ollama  modell=glm-5.3-flash:cloud  bezahlt=False  offen=False
    get_fuel() liefert: None
    fuel: all stations closed — daemon sleeps until next tick
    KACHEL BEHAUPTET:  modell='glm-5.3-flash:cloud'  anbieter='ollama-cloud'
    WIRKLICH OFFEN:    KEINE STATION

Die Kachel nannte also ein Modell, das dieser User nie erreichen kann — dieselbe
Fehlerfamilie wie die Embedding-Kachel (ein Name, der nicht die Wahrheit ist).

VERTRAG, den diese Wachen festnageln — `server._fuel_status(ollama_base=None, probe=None)`:

  probe: optionales callable(name: str) -> bool; ersetzt die echte Ollama-Probe
         (Tests). Ohne Angabe wird `{ollama_base}/api/tags` angetastet.
  Rueckgabe:
    enabled     bool    — Nutzer-Schalter (bestehendes _paid_enabled())
    open_station str|None — erste Station, die GEPRUEFT offen ist (nur Ollama ist
                            pruefbar; Key-Stationen sind erst beim Aufruf verifiziert)
    next_station str|None — die Station, die die Kette jetzt nehmen WUERDE
                            (offen oder nur Schluessel vorhanden)
    model       str     — Modell von next_station, "" wenn keine da ist
    provider    str     — Name von next_station, "" wenn keine da ist
    verified    bool    — True nur, wenn next_station == open_station,
                            also der Name geprueft und nicht nur erwartet ist
    stations    list    — je {name, model, paid, state}
                          state: "offen" | "bereit" | "zu" | "kein_schluessel"

Damit ist der Nulllfall ehrlich: keine Station -> model "" -> die Kachel darf
KEINEN Modellnamen zeigen.
"""

import os
import sys

import pytest

# dashboard/server.py beendet beim Import den Prozess, wenn FastAPI fehlt
# (`sys.exit(1)`), und die Testumgebung der CI bringt FastAPI nicht mit.
pytest.importorskip("fastapi", reason="dashboard-Server braucht fastapi")
pytest.importorskip("uvicorn", reason="dashboard-Server braucht uvicorn")

DASH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard"
)
if DASH not in sys.path:
    sys.path.insert(0, DASH)

import server  # noqa: E402

TOTE_ADRESSE = "http://127.0.0.1:59999"   # kein Ollama dahinter
FUEL_KEYS = ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "NOUS_API_KEY",
             "NEXUS_FUEL_BASE", "NEXUS_FUEL_KEY", "NEXUS_FUEL_MODEL")


def _ohne_schluessel(monkeypatch):
    for k in FUEL_KEYS:
        monkeypatch.delenv(k, raising=False)


def _status(**kw):
    assert hasattr(server, "_fuel_status"), (
        "server._fuel_status() fehlt: die Kachel baut ihren Wert noch aus einem "
        "fest eingebauten Standardnamen, statt die echte Kette zu fragen"
    )
    return server._fuel_status(**kw)


def test_ohne_ollama_und_ohne_schluessel_bleibt_kein_modellname(monkeypatch):
    """Der Nulllfall: nichts offen -> kein Name. Genau hier log die Kachel."""
    _ohne_schluessel(monkeypatch)

    st = _status(ollama_base=TOTE_ADRESSE)

    assert st["open_station"] is None, st
    assert st["next_station"] is None, st
    assert st["model"] == "", (
        "ohne Station darf kein Modellname stehen (die Kachel behauptete "
        f"'glm-5.3-flash:cloud'): {st}"
    )
    assert st["provider"] == ""
    assert st["verified"] is False
    assert st["stations"] and st["stations"][0]["state"] == "zu"


def test_offenes_lokales_ollama_gilt_als_geprueft(monkeypatch):
    """Ollama geprueft offen -> der Name kommt aus NEXUS_CONSOLIDATION_MODEL und
    gilt als geprueft (nicht nur erwartet)."""
    _ohne_schluessel(monkeypatch)
    monkeypatch.setenv("NEXUS_CONSOLIDATION_MODEL", "qwen3-embedding:0.6b")

    st = _status(ollama_base=TOTE_ADRESSE, probe=lambda name: name == "ollama")

    assert st["open_station"] == "ollama", st
    assert st["next_station"] == "ollama", st
    assert st["model"] == "qwen3-embedding:0.6b"
    assert st["provider"] == "ollama"
    assert st["verified"] is True


def test_ein_schluessel_allein_ist_erwartet_nicht_geprueft(monkeypatch):
    """Nur ein API-Schluessel, kein Ollama: die Kette nennt DIESE Station — aber
    ehrlich als 'erwartet', nicht als geprueft."""
    _ohne_schluessel(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "probe-key")

    st = _status(ollama_base=TOTE_ADRESSE)

    assert st["open_station"] is None, st
    assert st["next_station"] == "openai", st
    assert st["model"] == "gpt-5.6-luna", st
    assert st["provider"] == "openai"
    assert st["verified"] is False


def test_stationen_liste_zeigt_jede_tuer_mit_ihrem_zustand(monkeypatch):
    """Der Nutzer soll sehen, welche Tueren es gibt und wie sie stehen."""
    _ohne_schluessel(monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "probe-key")

    st = _status(ollama_base=TOTE_ADRESSE)

    assert [s["name"] for s in st["stations"]] == ["ollama", "openrouter"]
    assert [s["state"] for s in st["stations"]] == ["zu", "bereit"]
    assert all("paid" in s for s in st["stations"])
    assert st["stations"][1]["model"] == "openai/gpt-5.6-luna"


def test_die_abfrage_laeuft_nicht_auf_dem_event_loop():
    """Der 12.10.-Defekt: eine Netz-Abfrage im Event-Loop blockiert JEDEN Endpunkt
    (danach antwortete die ganze Oberflaeche nicht mehr). Die Fuel-Abfrage gehoert
    in einen Thread — strukturell geprueft, weil ein Timing-Beweis in der CI
    unzuverlaessig waere.

    Absichtlich OHNE Funktionsnamen der Route: ein Waechter, der einen Namen
    festnagelt, erzwingt Umbenennungen, die mit dem Defekt nichts zu tun haben."""
    import re

    quelle = open(os.path.join(DASH, "server.py"), encoding="utf-8").read()
    # Ohne Klammer gesucht: `asyncio.to_thread(_fuel_status)` uebergibt die
    # Funktion ALS Objekt, ein Muster mit "(" faende nur die Definition.
    rufe = [m.start() for m in re.finditer(r"_fuel_status", quelle)]
    assert rufe, "server.py kennt _fuel_status nicht"
    assert any("to_thread" in quelle[max(0, i - 120):i + 40] for i in rufe), (
        "die Fuel-Abfrage muss ueber asyncio.to_thread laufen, sonst blockiert sie "
        "den Event-Loop (Ausfall vom 12.10.)"
    )
