"""Eigene Schluessel je Agent (Punkt 2).

Warum: mit EINEM Schluessel fuer den Dienst durfte jeder alles, und der
Absender war nur der Kopf ``x-nexus-agent-id`` — also eine Behauptung. Ein
Agent kann jetzt einen eigenen Schluessel halten; gespeichert wird nur sein
SHA-256 in ``agents.json``. Der globale Schluessel muss sich weiterhin genau
wie bisher verhalten (nichts umstellen, bevor Punkt 4 die Agenten umzieht).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

SHARED_KEY = "test-global-key"


@pytest.fixture(autouse=True)
def _shared_key(monkeypatch):
    """Der globale Schluessel — wie heute in jeder bestehenden Verbindung."""
    monkeypatch.setenv("NEXUS_SERVICE_AUTH_KEY", SHARED_KEY)


@pytest.fixture
def registry(isolated_agents_registry):
    """The temp agents.json (autouse fixture in conftest redirects it)."""
    return isolated_agents_registry


def _read_registry(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ── verify_key ───────────────────────────────────────────────────────────


def test_own_key_wins_over_lying_header(registry):
    """Ein eigener Schluessel bestimmt den Absender — der Kopf zaehlt nicht."""
    import nexus_memory.service_auth as service_auth

    key = service_auth.issue_key("probe-agent", "Probe")

    agent_id, is_legacy = service_auth.verify_key(key, declared="hermes")

    assert (agent_id, is_legacy) == ("probe-agent", False)


def test_shared_key_keeps_todays_behaviour(registry):
    """Der globale Schluessel nimmt den Absender weiter aus dem Kopf."""
    import nexus_memory.service_auth as service_auth

    assert service_auth.verify_key(SHARED_KEY, declared="hermes") == ("hermes", True)
    # Ohne Kopf bleibt es beim bisherigen Rueckfall.
    assert service_auth.verify_key(SHARED_KEY) == ("unidentified", True)


def test_unknown_key_is_refused(registry):
    """Kein Treffer = abweisen (fail closed)."""
    import nexus_memory.service_auth as service_auth

    service_auth.issue_key("probe-agent", "Probe")

    with pytest.raises(service_auth.ServiceAuthError):
        service_auth.verify_key("not-a-known-key", declared="hermes")
    with pytest.raises(service_auth.ServiceAuthError):
        service_auth.verify_key("", declared="hermes")


def test_unknown_key_gets_401_over_http(registry):
    """Der Dienst antwortet einem unbekannten Schluessel mit 401."""
    from starlette.testclient import TestClient

    import nexus_memory.mcp_server as mcp
    from nexus_memory import service_auth

    client = TestClient(mcp.build_serve_app(), base_url="http://127.0.0.1:9122")
    response = client.post(
        "/write", json={"content": "x", "category": "fact"},
        headers={service_auth.AUTH_HEADER: "not-a-known-key"},
    )

    assert response.status_code == 401


# ── Ausstellen / Auflisten / Zuruecknehmen ────────────────────────


def test_issue_stores_only_the_hash(registry):
    """Der Klartext steht nirgends in agents.json — nur sein SHA-256."""
    import hashlib

    import nexus_memory.service_auth as service_auth

    key = service_auth.issue_key("probe-agent", "Probe")

    raw = registry.read_text(encoding="utf-8")
    assert key not in raw
    stored = _read_registry(registry)["agents"][0]
    expected = "sha256:" + hashlib.sha256(key.encode("utf-8")).hexdigest()
    assert stored["key_hash"] == expected
    assert stored["key_issued_at"]
    assert stored["name"] == "Probe"
    assert stored["trust_level"] == "public"  # unbekannter Agent: kein Zugewinn


def test_issue_keeps_an_existing_entry_intact(registry):
    """Ein vorhandener Agent behaelt Name, Vertrauensstufe und Zaehler."""
    import nexus_memory.service_auth as service_auth

    registry.write_text(json.dumps({"agents": [{
        "id": "hermes", "name": "Hermes Agent", "icon": "x",
        "trust_level": "private", "install_type": "plugin+mcp",
        "config_dir": "/tmp/hermes", "connected_at": "2026-01-01T00:00:00+00:00",
        "last_seen": "2026-01-01T00:00:00+00:00", "reads": 7, "writes": 9,
    }]}), encoding="utf-8")

    service_auth.issue_key("hermes", "Anderer Name")

    stored = _read_registry(registry)["agents"][0]
    assert stored["name"] == "Hermes Agent"
    assert stored["trust_level"] == "private"
    assert (stored["reads"], stored["writes"]) == (7, 9)
    assert stored["key_hash"].startswith("sha256:")


def test_revoke_puts_the_key_out_of_service(registry):
    """Nach ``--revoke`` gilt der eigene Schluessel nicht mehr, global schon."""
    import nexus_memory.service_auth as service_auth

    key = service_auth.issue_key("probe-agent", "Probe")
    assert service_auth.verify_key(key, declared="hermes")[0] == "probe-agent"

    assert service_auth.revoke_key("probe-agent") is True

    with pytest.raises(service_auth.ServiceAuthError):
        service_auth.verify_key(key, declared="hermes")
    # Der Agent faellt auf den globalen Schluessel zurueck.
    assert service_auth.verify_key(SHARED_KEY, declared="probe-agent") == (
        "probe-agent", True)
    assert "key_hash" not in _read_registry(registry)["agents"][0]
    assert service_auth.revoke_key("probe-agent") is False


def test_two_agents_do_not_interfere(registry):
    """Zwei eigene Schluessel stehen nebeneinander, ohne sich zu stoeren."""
    import nexus_memory.service_auth as service_auth

    first = service_auth.issue_key("probe-agent", "Probe")
    second = service_auth.issue_key("zweiter-agent", "Zweiter")

    assert first != second
    assert service_auth.verify_key(first, declared="zweiter-agent")[0] == "probe-agent"
    assert service_auth.verify_key(second, declared="probe-agent")[0] == "zweiter-agent"

    service_auth.revoke_key("probe-agent")

    assert service_auth.verify_key(second, declared="probe-agent")[0] == "zweiter-agent"
    rows = {row["id"]: row for row in service_auth.list_agent_keys()}
    assert rows["probe-agent"]["has_key"] is False
    assert rows["zweiter-agent"]["has_key"] is True


def test_cli_issue_and_list_and_revoke(registry, capsys):
    """Die CLI zeigt den Schluessel genau einmal und fuehrt die Liste."""
    import nexus_memory.service_auth as service_auth

    assert service_auth.main(["--issue", "--id", "probe-agent", "--name", "Probe"]) == 0
    out = capsys.readouterr().out
    # Die CLI gibt den Schluessel als letzte Zeile aus — genau einmal.
    issued = [line for line in out.splitlines() if line.strip()][-1].strip()
    assert len(issued) >= 40, out
    assert issued not in registry.read_text(encoding="utf-8")
    # Einmal heisst einmal: ein zweiter Lauf gibt einen ANDEREN Schluessel.
    assert service_auth.main(["--issue", "--id", "probe-agent"]) == 0
    assert capsys.readouterr().out.count(issued) == 0

    assert service_auth.main(["--list"]) == 0
    listing = capsys.readouterr().out
    assert "probe-agent" in listing and "ja" in listing

    assert service_auth.main(["--revoke", "--id", "probe-agent"]) == 0
    assert "zurueck" in capsys.readouterr().out
    assert service_auth.main(["--list"]) == 0
    assert "nein" in capsys.readouterr().out


def test_issue_rejects_an_unusable_id(registry):
    """Eine Kennung mit Leerzeichen wuerde Protokoll und Verzeichnis zerlegen."""
    import nexus_memory.service_auth as service_auth

    with pytest.raises(ValueError):
        service_auth.issue_key("Probe Agent")
    with pytest.raises(ValueError):
        service_auth.issue_key("x" * 65)


# ── Protokoll ────────────────────────────────────────────────────


def _denied_line(tmp_path, monkeypatch, headers) -> dict:
    """Eine ``denied``-Zeile aus einem echten HTTP-Aufruf an ``/write``."""
    from starlette.testclient import TestClient

    import nexus_memory.mcp_server as mcp
    from nexus_memory import service_log

    monkeypatch.setenv(service_log.LOG_DIR_ENV, str(tmp_path / "logs"))
    client = TestClient(mcp.build_serve_app(), base_url="http://127.0.0.1:9122")
    # Kein JSON-Koerper: der Weg lehnt ab, bevor irgendetwas Qdrant beruehrt,
    # und schreibt genau deshalb eine Zeile.
    response = client.post("/write", content=b"not json", headers=headers)
    assert response.status_code == 400
    events = service_log.read_events(limit=1)
    assert events, "der abgewiesene Aufruf muss im Protokoll stehen"
    return events[0]


def test_log_line_marks_an_own_key(tmp_path, monkeypatch, registry):
    """``key: own`` — und der Absender ist der Agent, nicht der Kopf."""
    import nexus_memory.service_auth as service_auth

    key = service_auth.issue_key("probe-agent", "Probe")
    entry = _denied_line(tmp_path, monkeypatch, {
        service_auth.AUTH_HEADER: key,
        "x-nexus-agent-id": "hermes",
    })

    assert entry["key"] == "own"
    assert entry["actor"] == "probe-agent"
    assert entry["via"] == "http"


def test_shared_key_writes_no_line_because_it_is_refused(tmp_path, monkeypatch,
                                                          registry):
    """Der Installationsschluessel schreibt keine Daten mehr — also auch keine Zeile.

    15.11.2026 (Abloese des alten ``key: shared``-Falls): der gemeinsame
    Schluessel meldet nur noch an (``/enroll``); auf ``/write`` antwortet der
    Dienst mit 401, bevor irgendetwas protokolliert oder geschrieben wird.
    """
    from starlette.testclient import TestClient

    import nexus_memory.mcp_server as mcp
    from nexus_memory import service_auth, service_log

    monkeypatch.setenv(service_log.LOG_DIR_ENV, str(tmp_path / "logs"))
    client = TestClient(mcp.build_serve_app(), base_url="http://127.0.0.1:9122")
    response = client.post(
        "/write", content=b"not json",
        headers={service_auth.AUTH_HEADER: SHARED_KEY,
                 "x-nexus-agent-id": "hermes"},
    )

    assert response.status_code == 401
    assert json.loads(response.text)["error"] == "plugin update required"
    assert service_log.read_events(limit=1) == []
