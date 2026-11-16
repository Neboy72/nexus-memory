"""``POST /enroll`` — nur von der eigenen Maschine, nur mit dem Installationsschluessel.

Warum: der Installationsschluessel darf seit 15.11.2026 keine Daten mehr lesen
oder schreiben. Er darf nur noch eines — einen Harness anmelden. Diese eine
Tuer muss deshalb eng sein: Loopback, kein Proxy-Header, gueltiger
Installationsschluessel.
"""
from __future__ import annotations

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


def _client(peer=("127.0.0.1", 40000)):
    """In-process client with an explicit peer address (no real port bound)."""
    from starlette.testclient import TestClient

    import nexus_memory.mcp_server as mcp

    return TestClient(mcp.build_serve_app(), base_url="http://127.0.0.1:9122",
                      client=peer)


def _enroll(peer=("127.0.0.1", 40000), key=INSTALL_KEY,
            body=None, extra_headers=None):
    from nexus_memory import service_auth

    headers = dict(extra_headers or {})
    if key:
        headers[service_auth.AUTH_HEADER] = key
    return _client(peer).post(
        "/enroll", json=body if body is not None else {"harness": "hermes"},
        headers=headers)


def test_loopback_gets_a_key(isolated_agents_registry):
    r = _enroll()

    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["key"]
    assert payload["agent_id"]
    assert payload["created"] is True
    # Der Klartext steht nirgends in der Datei — nur sein SHA-256.
    assert payload["key"] not in isolated_agents_registry.read_text(encoding="utf-8")


def test_other_address_is_refused():
    r = _enroll(peer=("192.168.1.9", 5555))

    assert r.status_code == 403
    assert r.json() == {"error": "enroll is loopback only"}


@pytest.mark.parametrize("header", [
    "x-forwarded-for", "x-forwarded-host", "x-real-ip", "forwarded",
])
def test_forwarded_header_is_refused(header, isolated_agents_registry):
    """Hinter einem Proxy sieht jede Anfrage wie Loopback aus."""
    r = _enroll(extra_headers={header: "203.0.113.7"})

    assert r.status_code == 403
    assert r.json() == {"error": "enroll is loopback only"}
    assert not isolated_agents_registry.exists()


@pytest.mark.parametrize("key", ["", "not-the-install-key"])
def test_missing_or_wrong_installation_key_gets_401(key):
    r = _enroll(key=key)

    assert r.status_code == 401
    assert r.json() == {"error": "invalid service key"}


def test_agent_key_cannot_enroll_itself(isolated_agents_registry):
    """Ein eigener Schluessel IST schon eine Identitaet — er meldet nichts an."""
    from nexus_memory import service_auth

    key = service_auth.issue_key("probe-agent", "Probe")
    r = _enroll(key=key)

    assert r.status_code == 401
    assert r.json() == {"error": "invalid service key"}


@pytest.mark.parametrize("body", [
    {}, {"harness": ""}, {"harness": "   "}, {"harness": 42}, {"harness": None},
    {"harness": "../../etc/passwd"},
])
def test_empty_or_unusable_harness_gets_400(body, isolated_agents_registry):
    r = _enroll(body=body)

    assert r.status_code == 400
    assert r.json() == {"error": "field 'harness' must be a non-empty string"}
    assert not isolated_agents_registry.exists()
