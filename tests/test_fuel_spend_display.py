"""Wache: die Kostenzeile der Fuel-Kachel muss den ECHTEN Verbrauch zeigen.

Defekt vom 12.10.2026 (vom Bau-Agenten gemeldet, dann selbst nachgemessen): die Kachel
las `~/.nexus-memory/fuel_spend.json` und suchte darin einen Schluessel "2026-10" oder
"total". Die Engine schreibt dort aber `{"year_month": ..., "spent_usd": ...}` — der
gesuchte Schluessel existiert also nie. Der Verbrauch stand deshalb dauerhaft auf
$0.00, auch wenn die Engine Geld ausgegeben hatte. Und der Pfad war fest verdrahtet,
obwohl die Engine `NEXUS_FUEL_STATE` respektiert.

Dieselbe Familie wie die Fuel-Kachel selbst: die Anzeige sagt nicht die Wahrheit.
"""

import json
import os
import sys
import time

import pytest

# dashboard/server.py beendet beim Import den Prozess, wenn FastAPI fehlt.
pytest.importorskip("fastapi", reason="dashboard-Server braucht fastapi")
pytest.importorskip("uvicorn", reason="dashboard-Server braucht uvicorn")

DASH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard"
)
if DASH not in sys.path:
    sys.path.insert(0, DASH)

import server  # noqa: E402

TOTE_ADRESSE = "http://127.0.0.1:59999"


def _keine_station():
    """Der Nulllfall, damit die Stationen die Kostenpruefung nicht stoeren."""
    return server._fuel_status(ollama_base=TOTE_ADRESSE, probe=lambda name: False)


def test_kostenzeile_zeigt_den_verbrauch_aus_dem_ledger(monkeypatch, tmp_path):
    """Das Konto der Engine (eigenes Format, eigener Pfad) muss in der Kachel ankommen.

    Der Pfad wird bewusst am Attribut gesetzt, nicht per Umgebungsvariable: die
    Engine loest `FUEL_STATE` schon beim Import auf, eine spaetere Variable kaeme
    zu spaet — genau daran ist ein erster Anlauf dieser Wache gescheitert.
    """
    from nexus_memory import fuel_chain

    ledger = tmp_path / "fuel_spend.json"
    ledger.write_text(
        json.dumps({"year_month": time.strftime("%Y-%m"), "spent_usd": 1.234}),
        encoding="utf-8",
    )
    monkeypatch.setattr(fuel_chain, "FUEL_STATE", ledger)

    st = _keine_station()

    assert st["spent_usd"] == 1.23, (
        "der Verbrauch muss aus dem Ledger der Engine kommen (hier 1.234 -> 1.23), "
        f"nicht aus einer eigenen Datei-Lesung mit dem falschen Schluessel: {st}"
    )
    assert st["budget_usd"] > 0


def test_verbrauch_eines_fremden_monats_ist_null(monkeypatch, tmp_path):
    """Die Regel des Monatswechsels gilt — sie kommt aus der Engine, nicht aus hier."""
    from nexus_memory import fuel_chain

    ledger = tmp_path / "fuel_spend.json"
    ledger.write_text(json.dumps({"year_month": "2020-01", "spent_usd": 9.99}),
                      encoding="utf-8")
    monkeypatch.setattr(fuel_chain, "FUEL_STATE", ledger)

    st = _keine_station()

    assert st["spent_usd"] == 0.0, st
