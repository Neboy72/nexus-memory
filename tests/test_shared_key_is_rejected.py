"""Der Installationsschluessel liest und schreibt keine Daten mehr.

Er darf nur noch eines: ueber ``POST /enroll`` einen Agenten anmelden. Auf
``/embed``, ``/write`` und ``/mcp`` antwortet der Dienst mit 401 und sagt,
warum. Ein eigener Agentenschluessel kommt durch.
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
UPDATE_ERROR = "plugin update required"


class _Embedder:
    available = True
    name = "stub"

    async def embed(self, _text, _is_query=True):
        return [0.1] * 4


class _Store:
    _embedder = _Embedder()


@pytest.fixture(autouse=True)
def _install_key(monkeypatch, isolated_agents_registry):
    monkeypatch.setenv("NEXUS_SERVICE_AUTH_KEY", INSTALL_KEY)


@pytest.fixture
def agent_key(isolated_agents_registry):
    """Ein echter Agentenschluessel — der Weg, den ein Plugin nach /enroll hat."""
    from nexus_memory import service_auth

    return service_auth.issue_key("probe-agent", "Probe")


def _client():
    from starlette.testclient import TestClient

    import nexus_memory.mcp_server as mcp

    return TestClient(mcp.build_serve_app(), base_url="http://127.0.0.1:9122",
                      client=("127.0.0.1", 40000))


def _embed(key: str, monkeypatch):
    import nexus_memory.mcp_server as mcp
    from nexus_memory import service_auth

    monkeypatch.setattr(mcp, "get_store", lambda: _Store())
    return _client().post("/embed", json={"text": "hallo"},
                          headers={service_auth.AUTH_HEADER: key})


def _mcp_initialize(key: str):
    from nexus_memory import service_auth

    with _client() as client:
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "pytest", "version": "1.0"}},
            },
            headers={service_auth.AUTH_HEADER: key,
                     "Accept": "application/json, text/event-stream"},
        )
        return response


@pytest.mark.parametrize("endpoint", ["/embed", "/mcp"])
def test_installation_key_is_rejected_on_data_endpoints(endpoint, monkeypatch):
    if endpoint == "/embed":
        r = _embed(INSTALL_KEY, monkeypatch)
    else:
        r = _mcp_initialize(INSTALL_KEY)

    assert r.status_code == 401, r.text
    body: dict = json.loads(r.text)
    assert body["error"] == UPDATE_ERROR
    assert "enrol" in body["hint"], "die Antwort muss den Weg nennen"


def test_installation_key_is_rejected_on_write():
    from nexus_memory import service_auth

    r = _client().post("/write", json={"content": "x", "category": "fact"},
                       headers={service_auth.AUTH_HEADER: INSTALL_KEY})

    assert r.status_code == 401
    assert json.loads(r.text)["error"] == UPDATE_ERROR


def test_agent_key_gets_through_embed(agent_key, monkeypatch):
    r = _embed(agent_key, monkeypatch)

    assert r.status_code == 200, r.text
    assert r.json()["dim"] == 4


def test_agent_key_gets_through_mcp(agent_key):
    r = _mcp_initialize(agent_key)

    assert r.status_code == 200, r.text
    assert r.headers.get("mcp-session-id")


def test_agent_key_gets_through_write(agent_key, monkeypatch):
    import nexus_memory.mcp_server as mcp
    from nexus_memory import service_auth

    written: list = []

    class _Writer:
        async def remember(self, content, **kwargs):
            written.append((content, kwargs))
            return {"status": "ok", "id": "probe"}

    monkeypatch.setattr(mcp, "get_store", lambda: _Writer())
    r = _client().post(
        "/write", json={"content": "Der Verteiler steht im Flur",
                        "category": "fact", "access_level": "private"},
        headers={service_auth.AUTH_HEADER: agent_key})

    assert r.status_code == 200, r.text
    assert written and written[0][1]["actor"] == "probe-agent"
