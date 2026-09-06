#!/usr/bin/env python3
"""tests for fuel_chain — the daemon's multi-station fuel discovery."""

import json
from unittest.mock import patch

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from nexus_memory import fuel_chain
from nexus_memory.fuel_chain import (Station, budget_exhausted, build_chain,
                                     get_fuel, _budget_add, FUEL_STATE)


def _clean_state(tmp_path, monkeypatch):
    monkeypatch.setattr(fuel_chain, "FUEL_STATE", tmp_path / "fuel.json")
    monkeypatch.setattr(fuel_chain, "FUEL_BUDGET_USD", 1.00)


def test_ollama_first_when_open(tmp_path, monkeypatch):
    """Free local station wins when its probe answers."""
    _clean_state(tmp_path, monkeypatch)
    calls = []
    fake = lambda p: calls.append(1) or '{"facts": []}'
    fn = get_fuel("http://ollama-fake:1", "m", ollama_generate=fake)
    assert fn is not None
    fn("hi")
    assert calls == [1]  # ollama was used, no paid station touched


def test_falls_through_to_openrouter(tmp_path, monkeypatch):
    """Ollama probe fails -> openrouter station (with key) serves."""
    _clean_state(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setattr(fuel_chain, "_openai_compat_generate",
                        lambda *a, **k: '{"facts": ["x"]}')
    def dead_probe():
        return False
    with patch("urllib.request.urlopen", side_effect=Exception("no ollama here")):
        fn = get_fuel("http://127.0.0.1:1", "m")
    assert fn is not None
    out = fn("prompt")
    assert "facts" in out


def test_all_closed_returns_none(tmp_path, monkeypatch):
    """Every station closed -> None (daemon sleeps), never raises."""
    _clean_state(tmp_path, monkeypatch)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("NOUS_API_KEY", raising=False)
    monkeypatch.delenv("NEXUS_FUEL_BASE", raising=False)
    with patch("urllib.request.urlopen", side_effect=Exception("dead")):
        fn = get_fuel("http://127.0.0.1:1", "m")
    assert fn is None


def test_budget_cap_pauses_paid_only(tmp_path, monkeypatch):
    """Budget exhausted: paid stations skipped, free ollama still serves."""
    _clean_state(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    _budget_add(0.99)  # just under cap... then push over
    _budget_add(0.50)  # now over the 1.00 default
    assert budget_exhausted() is True
    with patch("urllib.request.urlopen", side_effect=Exception("dead")):
        fn = get_fuel("http://127.0.0.1:1", "m")
    assert fn is None  # ollama dead + paid paused -> None
    # free station still allowed even when budget spent:
    fake = lambda p: '{"facts": []}'
    fn2 = get_fuel("http://x", "m", ollama_generate=fake)
    assert fn2 is not None


def test_budget_add_monthly_reset(tmp_path, monkeypatch):
    """Counter resets when the month turns."""
    _clean_state(tmp_path, monkeypatch)
    _budget_add(5.0)
    assert budget_exhausted() is True
    # simulate month turn
    s = json.loads(fuel_chain.FUEL_STATE.read_text())
    s["year_month"] = "2000-01"
    fuel_chain.FUEL_STATE.write_text(json.dumps(s))
    assert budget_exhausted() is False


def test_explicit_station_ranked_after_ollama(tmp_path, monkeypatch):
    """NEXUS_FUEL_BASE+KEY is inserted right after free ollama."""
    _clean_state(tmp_path, monkeypatch)
    monkeypatch.setenv("NEXUS_FUEL_BASE", "https://custom.example/v1")
    monkeypatch.setenv("NEXUS_FUEL_KEY", "k")
    chain = build_chain("http://127.0.0.1:1", "m")
    assert [s.name for s in chain][:2] == ["ollama", "custom"]


def test_cheap_estimate_present_on_paid(tmp_path, monkeypatch):
    """Every paid station carries a cost estimator (budget accounting)."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    chain = build_chain("http://127.0.0.1:1", "m")
    paid = [s for s in chain if s.paid]
    assert paid and all(s.estimate is not None for s in paid)


def test_estimate_math_sane(tmp_path, monkeypatch):
    """~4 chars/token: 4000-char prompt on luna-tier ~= $0.0002 — cent range."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    chain = [s for s in build_chain("http://x", "m") if s.name == "openai"][0]
    est = chain.estimate("x" * 4000, "y" * 400)
    assert 0.0 < est < 0.01
