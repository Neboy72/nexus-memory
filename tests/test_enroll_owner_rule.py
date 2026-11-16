"""Die Eigentuemer-Regel beim Selbsteinschreiben (15.11.2026).

Der ERSTE Eintrag im Register ist der Eigentuemer: ``admin`` + ``private``.
Jeder weitere startet als ``know`` + ``trusted``.

Geaendert am 16.11.2026: ein erneutes Einschreiben desselben Harness legt
jetzt einen NEUEN Eintrag an, weil der bestehende schon einen Schluessel hat
und nicht mehr uebernommen wird. Vorher wurde nur der Schluessel neu
ausgestellt und der alte verlor sofort seine Gueltigkeit — eine zweite
Installation (Windows neben dem Mac) sperrte damit die erste aus. Der
Eigentuemer-Platz bleibt beim ersten Eintrag; ``enroll_count`` zaehlt nur
noch die Anmeldungen desselben Eintrags.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

INSTALL_KEY = "test-install-key"


@pytest.fixture(autouse=True)
def _install_key(monkeypatch, isolated_agents_registry):
    monkeypatch.setenv("NEXUS_SERVICE_AUTH_KEY", INSTALL_KEY)


def _client():
    from starlette.testclient import TestClient

    import nexus_memory.mcp_server as mcp

    return TestClient(mcp.build_serve_app(), base_url="http://127.0.0.1:9122",
                      client=("127.0.0.1", 40000))


def _enroll(harness: str, label: str = "") -> dict:
    from nexus_memory import service_auth

    r = _client().post(
        "/enroll", json={"harness": harness, "label": label},
        headers={service_auth.AUTH_HEADER: INSTALL_KEY})
    assert r.status_code == 200, r.text
    return r.json()


def _agents(registry: Path) -> list[dict]:
    if not registry.exists():
        return []
    return json.loads(registry.read_text(encoding="utf-8")).get("agents", [])


def test_first_enrol_is_the_owner(isolated_agents_registry):
    payload = _enroll("hermes", "Hermes Agent")

    assert payload["is_owner"] is True
    agents = _agents(isolated_agents_registry)
    assert len(agents) == 1
    assert agents[0]["id"] == payload["agent_id"]
    assert agents[0]["role"] == "admin"
    assert agents[0]["trust_level"] == "private"
    assert agents[0]["harness"] == "hermes"
    assert agents[0]["name"] == "Hermes Agent"


def test_second_enrol_is_not_the_owner(isolated_agents_registry):
    first = _enroll("hermes")
    second = _enroll("openclaw")

    assert first["is_owner"] is True
    assert second["is_owner"] is False
    assert second["created"] is True
    agents = {a["id"]: a for a in _agents(isolated_agents_registry)}
    assert agents[second["agent_id"]]["role"] == "know"
    assert agents[second["agent_id"]]["trust_level"] == "trusted"
    assert len(agents) == 2


def test_re_enrol_creates_a_new_entry_and_keeps_the_first_key(
        isolated_agents_registry):
    """Eine zweite Anmeldung verdraengt die erste Installation nicht mehr."""
    from nexus_memory import service_auth

    first = _enroll("hermes")
    second = _enroll("hermes")

    assert second["created"] is True, "der bestehende Eintrag hat einen Schluessel"
    assert second["agent_id"] != first["agent_id"]
    assert second["is_owner"] is False, "der Eigentuemer-Platz ist vergeben"

    agents = {a["id"]: a for a in _agents(isolated_agents_registry)}
    assert len(agents) == 2, "je Anmeldung ein Eintrag, keiner wird ueberschrieben"
    assert agents[first["agent_id"]]["role"] == "admin"
    assert agents[first["agent_id"]]["enroll_count"] == 1
    assert agents[second["agent_id"]]["role"] == "know"
    assert agents[second["agent_id"]]["trust_level"] == "trusted"

    # Beide Schluessel gelten — auf der Datenebene geprueft (dieselbe Stelle,
    # die die Middleware fragt).
    assert service_auth.verify_key(first["key"]) == (first["agent_id"], False)
    assert service_auth.verify_key(second["key"]) == (second["agent_id"], False)


def test_dashboard_never_takes_the_owner_slot(isolated_agents_registry):
    """Das Dashboard ist das Fenster auf die Agenten, kein Agent.

    Es meldet sich an (es bettet ueber den Dienst ein), darf aber den
    Eigentuemer-Platz nicht besetzen — der gehoert dem ersten echten Agenten.
    """
    dashboard = _enroll("dashboard")
    hermes = _enroll("hermes")

    assert dashboard["is_owner"] is False
    assert hermes["is_owner"] is True
    agents = {a["id"]: a for a in _agents(isolated_agents_registry)}
    assert agents[dashboard["agent_id"]]["trust_level"] == "trusted"
    assert agents[hermes["agent_id"]]["role"] == "admin"


def test_owner_rule_ignores_the_anonymous_entry(isolated_agents_registry):
    """``unidentified`` ist kein Agent und macht niemanden zum Nicht-Eigentuemer."""
    isolated_agents_registry.write_text(json.dumps({"agents": [{
        "id": "unidentified", "name": "Unidentified", "icon": "x",
        "trust_level": "public", "install_type": "unknown", "config_dir": None,
        "connected_at": "2026-01-01T00:00:00+00:00",
        "last_seen": "2026-01-01T00:00:00+00:00", "reads": 0, "writes": 0,
    }]}), encoding="utf-8")

    payload = _enroll("hermes")

    assert payload["is_owner"] is True
