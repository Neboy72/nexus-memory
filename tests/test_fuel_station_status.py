#!/usr/bin/env python3
"""Wachen fuer die Engine-Wahrheit der Fuel-Kette — ``station_status``.

Die Kachel (``dashboard/server.py::_fuel_status``) ist nur noch ein duenner
Wrapper; die Wahrheit ueber die Kette entsteht in der Engine. Vorher lebte sie
nur in der Oberflaeche, und die Einrichtung baute sie sich selbst nach — genau
daraus entstand der Defekt vom 12.10.2026 (ein Modellname, den der User nie
erreichen konnte). Hier stehen die Regeln, die vorher nur als Verhalten der
Kachel existierten — plus die Reihenfolge der Tueren, damit Doku und Kette
nicht auseinanderlaufen.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from nexus_memory import fuel_chain  # noqa: E402

TOTE_ADRESSE = "http://127.0.0.1:59999"   # kein Ollama dahinter
# Jede Umgebungsvariable, die eine Tuer oeffnen koennte (siehe build_chain).
FUEL_KEYS = ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "NOUS_API_KEY",
             "NEXUS_FUEL_BASE", "NEXUS_FUEL_KEY", "NEXUS_FUEL_MODEL",
             "NEXUS_FUEL_MODEL_OPENROUTER", "NEXUS_FUEL_MODEL_OPENAI",
             "NEXUS_FUEL_MODEL_NOUS")


@pytest.fixture
def ohne_tueren(monkeypatch, tmp_path):
    """Hermeisch: keine Schluessel, kein bezahlter Schalter, kein geteilter
    Budget-Ledger auf dieser Maschine."""
    for key in FUEL_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("NEXUS_FUEL_PAID", "0")
    monkeypatch.setattr(fuel_chain, "FUEL_TOGGLE_PATH", tmp_path / "paid_enabled")
    monkeypatch.setattr(fuel_chain, "FUEL_STATE", tmp_path / "fuel_spend.json")
    return monkeypatch


def _probe_nur(name: str) -> bool:
    """Test-Probe: nur Ollama ist 'offen', alles andere nicht."""
    return name == "ollama"


def test_nullfall_keine_station_kein_modellname(ohne_tueren):
    """Tote Ollama-Adresse, keine Schluessel -> kein Modellname, keine Station."""
    st = fuel_chain.station_status(ollama_base=TOTE_ADRESSE)

    assert st["open_station"] is None, st
    assert st["next_station"] is None, st
    assert st["model"] == "", st
    assert st["provider"] == ""
    assert st["verified"] is False
    assert [s["name"] for s in st["stations"]] == ["ollama"], st["stations"]
    assert st["stations"][0]["state"] == "zu"


def test_ein_schluessel_ist_erwartet_nicht_geprueft(ohne_tueren):
    """Ein Schluessel allein oeffnet keine gepruefte Tuer: 'bereit', nicht 'offen'."""
    ohne_tueren.setenv("OPENAI_API_KEY", "probe-key")

    st = fuel_chain.station_status(ollama_base=TOTE_ADRESSE)

    assert st["open_station"] is None, st
    assert st["next_station"] == "openai", st
    assert st["provider"] == "openai"
    assert st["model"] == "gpt-5.6-luna", st
    assert st["verified"] is False, "ein Schluessel ist eine Erwartung, keine Gewissheit"


def test_ollama_geprueft_offen_gilt_als_verifiziert(ohne_tueren):
    """Die freie Station zuerst — und sie ist die einzige, die geprueft sein kann."""
    ohne_tueren.setenv("OPENAI_API_KEY", "probe-key")
    # Der Modellname der freien Station ist der EINE Standard der Engine.
    ohne_tueren.setenv("NEXUS_CONSOLIDATION_MODEL", "qwen3-embedding:0.6b")

    st = fuel_chain.station_status(ollama_base=TOTE_ADRESSE, probe=_probe_nur)

    assert st["open_station"] == "ollama", st
    assert st["next_station"] == "ollama", st
    assert st["provider"] == "ollama"
    assert st["model"] == "qwen3-embedding:0.6b"
    assert st["verified"] is True


def test_reihenfolge_custom_vor_den_schluessel_stationen(ohne_tueren):
    """NEXUS_FUEL_BASE+KEY schiebt die eigene Station VOR OpenRouter/OpenAI/Nous
    — aber hinter Ollama. Die Doku nennt dieselbe Reihenfolge; so baut sie
    build_chain, und genau das muss die Liste zeigen."""
    ohne_tueren.setenv("OPENROUTER_API_KEY", "probe-key")
    ohne_tueren.setenv("OPENAI_API_KEY", "probe-key")
    ohne_tueren.setenv("NOUS_API_KEY", "probe-key")
    ohne_tueren.setenv("NEXUS_FUEL_BASE", "https://meins.example/v1")
    ohne_tueren.setenv("NEXUS_FUEL_KEY", "probe-key")

    st = fuel_chain.station_status(ollama_base=TOTE_ADRESSE)

    assert [s["name"] for s in st["stations"]] == \
        ["ollama", "custom", "openrouter", "openai", "nous"], st["stations"]
    # Die eigene Station nennt ihren Standardnamen, nicht den eines Fremdanbieters.
    custom = st["stations"][1]
    assert custom["model"] == "gpt-5.6-luna", custom
    assert custom["paid"] is True

    kette = fuel_chain.build_chain(TOTE_ADRESSE, "irgendein-modell")
    assert [s["name"] for s in st["stations"]] == [s.name for s in kette]


def test_custom_station_bleibt_hinter_ollama(ohne_tueren):
    """Die freie Station bleibt die guenstigste Tuer, auch wenn eine eigene
    Basis gesetzt ist: ein kostenlos erreichbares Ollama darf nie uebersprungen
    werden."""
    ohne_tueren.setenv("NEXUS_FUEL_BASE", "https://meins.example/v1")
    ohne_tueren.setenv("NEXUS_FUEL_KEY", "probe-key")

    st = fuel_chain.station_status(ollama_base=TOTE_ADRESSE, probe=_probe_nur)

    assert [s["name"] for s in st["stations"]] == ["ollama", "custom"]
    assert st["next_station"] == "ollama"
    assert st["verified"] is True


def test_leerer_schluessel_ist_keine_tuer(ohne_tueren):
    """OPENAI_API_KEY="" setzt einen Namen in die Umgebung, oeffnet aber nichts:
    build_chain prueft auf Wahrheitswert, nicht auf 'ist gesetzt'. Ein leerer
    Schluessel darf darum keine Station bauen — sonst stuende in der Kachel
    wieder ein Modell, den niemand erreichen kann."""
    ohne_tueren.setenv("OPENAI_API_KEY", "")
    ohne_tueren.setenv("NEXUS_FUEL_BASE", "https://meins.example/v1")
    ohne_tueren.setenv("NEXUS_FUEL_KEY", "")   # halbe custom-Tuer: Basis ohne Schluessel

    kette = fuel_chain.build_chain(TOTE_ADRESSE, "irgendein-modell")
    assert [s.name for s in kette] == ["ollama"], [s.name for s in kette]

    st = fuel_chain.station_status(ollama_base=TOTE_ADRESSE, probe=_probe_nur)
    assert st["next_station"] == "ollama", st
    assert [s["name"] for s in st["stations"]] == ["ollama"], st["stations"]


def test_jede_station_traegt_name_modell_bezahlt_zustand(ohne_tueren):
    """Die Liste ist vollstaendig: ohne Zustand je Tuer kann der User nicht
    sehen, welche Tueren es gibt und wie sie stehen."""
    ohne_tueren.setenv("OPENROUTER_API_KEY", "probe-key")
    ohne_tueren.setenv("NEXUS_FUEL_MODEL_OPENROUTER", "mein/guenstiges-modell")

    st = fuel_chain.station_status(ollama_base=TOTE_ADRESSE)

    assert [s["state"] for s in st["stations"]] == ["zu", "bereit"]
    assert st["stations"][1]["model"] == "mein/guenstiges-modell"
    assert st["stations"][1]["paid"] is True
    assert st["stations"][0]["paid"] is False
