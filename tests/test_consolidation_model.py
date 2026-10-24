#!/usr/bin/env python3
"""The consolidation model is the user's, never a built-in default name.

Regression guard for 24.10.2026: the module carried a foreign model name as
its default, so every user who never set ``NEXUS_CONSOLIDATION_MODEL`` would
call a model they do not have (same failure family as the reranker default).

Contract:
  resolve_consolidation_model(config_path=None) -> str
    env NEXUS_CONSOLIDATION_MODEL  >  config nexus-memory.consolidation_model
    >  config model.default  >  ""  (nothing configured = no name, no call)

All tests run without network: the HTTP opener is patched wherever a call
could happen, and the home directory is redirected into ``tmp_path`` so the
machine's real ``~/.hermes/config.yaml`` never leaks into the result.
"""

import logging
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from nexus_memory import consolidation, fuel_chain  # noqa: E402
from nexus_memory.consolidation import (get_default_fuel,  # noqa: E402
                                        resolve_consolidation_model)

FUEL_KEYS = ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "NOUS_API_KEY",
             "NEXUS_FUEL_BASE", "NEXUS_FUEL_KEY", "NEXUS_FUEL_MODEL")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """No env model, no real home, no fuel keys — every test starts at zero."""
    monkeypatch.delenv("NEXUS_CONSOLIDATION_MODEL", raising=False)
    for key in FUEL_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(consolidation, "_NO_MODEL_LOGGED", False)


def _write_config(text: str) -> Path:
    cfg = Path(os.path.expanduser("~")) / ".hermes" / "config.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(text, encoding="utf-8")
    return cfg


def test_env_wins_over_config(monkeypatch):
    """Source 1: NEXUS_CONSOLIDATION_MODEL beats both config keys."""
    _write_config(
        "nexus-memory:\n"
        "  consolidation_model: from-block\n"
        "model:\n"
        "  default: from-model-default\n"
    )
    monkeypatch.setenv("NEXUS_CONSOLIDATION_MODEL", "from-env")

    assert resolve_consolidation_model() == "from-env"


def test_consolidation_model_key_from_config():
    """Source 2a: nexus-memory.consolidation_model is used."""
    _write_config(
        "nexus-memory:\n"
        "  consolidation_model: from-block\n"
        "model:\n"
        "  default: from-model-default\n"
    )

    assert resolve_consolidation_model() == "from-block"


def test_model_default_from_config():
    """Source 2b: model.default is the last configured fallback."""
    _write_config("model:\n  default: from-model-default\n")

    assert resolve_consolidation_model() == "from-model-default"


def test_nothing_configured_is_empty_and_calls_no_model():
    """Nothing configured -> "" and the call path makes NO HTTP request."""
    assert resolve_consolidation_model() == ""

    with patch("urllib.request.urlopen") as opener:
        assert get_default_fuel() is None
        with pytest.raises(RuntimeError):
            consolidation._ollama_generate("prompt")
    opener.assert_not_called()


def test_nothing_configured_warns_once_per_process(caplog):
    """The idle state is announced exactly once, not on every tick."""
    caplog.set_level(logging.WARNING, logger="nexus.consolidation")
    with patch("urllib.request.urlopen"):
        assert get_default_fuel() is None
        assert get_default_fuel() is None

    warnings = [r for r in caplog.records if "no model configured" in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in caplog.records]


def test_broken_yaml_is_swallowed():
    """A corrupt config is 'not configured', never an exception."""
    _write_config("nexus-memory: [unclosed\n  - :\n")

    assert resolve_consolidation_model() == ""


def test_station_status_with_empty_model_shows_no_name():
    """The tile reports model "" when nothing is configured — no guessed name."""
    st = fuel_chain.station_status(
        ollama_base="http://127.0.0.1:59999", probe=lambda _name: False)

    assert st["model"] == ""
    assert st["provider"] == ""
    assert st["next_station"] is None
    # Even a PROVEN-open Ollama must not invent a name: the station itself
    # carries the empty model that the call would use.
    st_open = fuel_chain.station_status(
        ollama_base="http://127.0.0.1:59999", probe=lambda name: name == "ollama")
    assert st_open["open_station"] == "ollama"
    assert st_open["model"] == ""
