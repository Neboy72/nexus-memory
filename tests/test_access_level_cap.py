"""Die im Dashboard gesetzte Stufe ist die OBERGRENZE beim Lesen.

Vorher nahm ``recall`` die Stufe aus dem Aufruf (Default ``public``), ein Agent
konnte sich also selbst hochstufen — der Schalter im Dashboard war reine
Anzeige. Diese Tests halten die Grenze fest: kleiner ja, groesser nein.

Nebo, 15.11.2026: „Es muss funktionieren. Wir machen uns ja total unglaubwuerdig."
"""

from __future__ import annotations

import json

import pytest

from nexus_memory import agent_detect
from nexus_memory import mcp_server


@pytest.fixture()
def registry(tmp_path, monkeypatch):
    """Ein Register mit genau drei Faellen: Eigentuemer, Vertrauter, Fremder."""
    path = tmp_path / "agents.json"
    path.write_text(json.dumps({"agents": [
        {"id": "eigentuemer", "name": "Eigentuemer", "trust_level": "private", "role": "admin"},
        {"id": "vertrauter", "name": "Vertrauter", "trust_level": "trusted", "role": "know"},
    ]}))
    monkeypatch.setattr(agent_detect, "REGISTRY_PATH", str(path), raising=False)
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    return path


@pytest.mark.parametrize("agent,erwartet", [
    ("eigentuemer", "private"),
    ("vertrauter", "trusted"),
    ("gar-nicht-da", "public"),
])
def test_maximum_kommt_aus_dem_register(registry, agent, erwartet):
    token = mcp_server._REQUEST_AGENT_ID.set(agent)
    try:
        assert mcp_server._caller_max_level() == erwartet
    finally:
        mcp_server._REQUEST_AGENT_ID.reset(token)


def test_vertrauter_kann_sich_nicht_hochstufen(registry):
    token = mcp_server._REQUEST_AGENT_ID.set("vertrauter")
    try:
        assert mcp_server._cap_to_caller_level("private") == "trusted", (
            "ein vertrauenswuerdiger Agent darf sich nicht selbst Privates geben"
        )
        assert mcp_server._cap_to_caller_level("trusted") == "trusted"
        assert mcp_server._cap_to_caller_level("public") == "public", (
            "kleiner darf er immer"
        )
    finally:
        mcp_server._REQUEST_AGENT_ID.reset(token)


def test_fremder_bekommt_nur_oeffentliches(registry):
    token = mcp_server._REQUEST_AGENT_ID.set("gar-nicht-da")
    try:
        assert mcp_server._cap_to_caller_level("private") == "public"
        assert mcp_server._cap_to_caller_level("trusted") == "public"
    finally:
        mcp_server._REQUEST_AGENT_ID.reset(token)


def test_eigentuemer_darf_alles(registry):
    token = mcp_server._REQUEST_AGENT_ID.set("eigentuemer")
    try:
        assert mcp_server._cap_to_caller_level("private") == "private"
    finally:
        mcp_server._REQUEST_AGENT_ID.reset(token)
