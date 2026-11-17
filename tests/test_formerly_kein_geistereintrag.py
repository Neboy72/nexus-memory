"""Kein Geistereintrag nach dem Umbenennen (Punkt 4c).

Nach ``--rename hermes hx7m2p`` trug der Eintrag ``formerly: ["hermes"]``.
Trotzdem erschien im Dashboard ein ZWEITER Agent ``hermes`` mit 0 Zugriffen —
also nicht durch einen Zugriff, sondern durch eine Stelle, die einen Eintrag
anlegt, ohne den frueheren Namen aufzuloesen.

Diese Tests decken jeden Weg ab, der ``agents.json`` anlegen kann:

* ``agent_detect.register_agent``          (Dashboard "Connect", Setup)
* ``agent_detect.update_agent_stats``      (Telemetrie-Hot-Path)
* ``agent_detect.record_agent_access``     (fail-open Zaehler)
* ``agent_detect.set_agent_role``          (Dashboard "Role")
* ``agent_detect.register_remote_agent``   (Remote-Vorregistrierung)
* ``service_auth.issue_key``               (eigener Schluessel)
* ``mcp_server._count_memory_access``      (Kern-Zaehler)
* ``GET /api/detect``                      (Dashboard "Detect Agents")
* ``POST /api/agents/{id}/connect``        (Dashboard "Connect")
* ``auto_recall._record_access``           (Claude-Code-Hook, direkter Writer)
* ``openclaw recordAgentAccess``           (eigener Node-Test:

  ``plugins/openclaw/test-agent-formerly.mjs``)

Regel: ein Name aus irgendeinem ``formerly`` zeigt auf die heutige Kennung und
erzeugt NIE einen neuen Eintrag. Keine Datenmigration — die Tests beruehren nur
``agents.json``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

ALT = "hermes"
NEU = "hx7m2p"
ISO = "2026-01-01T00:00:00+00:00"


def _entry(agent_id: str, **extra) -> dict:
    entry = {
        "id": agent_id, "name": agent_id, "icon": "x",
        "trust_level": "private", "install_type": "plugin+mcp",
        "config_dir": None, "connected_at": ISO, "last_seen": ISO,
        "reads": 0, "writes": 0,
    }
    entry.update(extra)
    return entry


def _registry_file(path: Path, *entries: dict) -> None:
    path.write_text(json.dumps({"agents": list(entries)}, indent=2) + "\n",
                    encoding="utf-8")


def _registry(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _ids(path: Path) -> list[str]:
    return [a["id"] for a in _registry(path)["agents"]]


@pytest.fixture(autouse=True)
def _kein_env_agent(monkeypatch):
    monkeypatch.delenv("NEXUS_AGENT_ID", raising=False)
    monkeypatch.delenv("NEXUS_AGENT_SELFTEST", raising=False)


@pytest.fixture
def umbenannt(isolated_agents_registry):
    """Der umbenannte Agent: Kennung NEU, alter Name als ``formerly``."""
    _registry_file(
        isolated_agents_registry,
        _entry(NEU, name="Hermes Buero", role="work", formerly=[ALT],
               reads=4, writes=2),
    )
    return isolated_agents_registry


# ===========================================================================
# 1. register_agent — Dashboard "Connect" / Setup-Wizard
# ===========================================================================


def test_register_agent_loest_formerly_auf(umbenannt):
    from nexus_memory.agent_detect import register_agent

    result = register_agent(ALT, "Hermes", "H", "trusted", "plugin+mcp")

    assert "error" not in result
    assert _ids(umbenannt) == [NEU], "kein zweiter Eintrag unter dem alten Namen"
    entry = _registry(umbenannt)["agents"][0]
    # Der Alias darf den umbenannten Eintrag nicht umetikettieren.
    assert entry["name"] == "Hermes Buero"
    assert entry["trust_level"] == "private"
    assert entry["install_type"] == "plugin+mcp"


# ===========================================================================
# 2. update_agent_stats — Telemetrie-Hot-Path
# ===========================================================================


def test_update_agent_stats_loest_formerly_auf(umbenannt):
    from nexus_memory.agent_detect import update_agent_stats

    update_agent_stats(ALT, write=True)

    assert _ids(umbenannt) == [NEU]
    entry = _registry(umbenannt)["agents"][0]
    assert entry["writes"] == 3
    assert entry["reads"] == 4


# ===========================================================================
# 3. record_agent_access — fail-open Zaehler
# ===========================================================================


def test_record_agent_access_loest_formerly_auf(umbenannt):
    from nexus_memory.agent_detect import record_agent_access

    record_agent_access(ALT, read=True)

    assert _ids(umbenannt) == [NEU]
    assert _registry(umbenannt)["agents"][0]["reads"] == 5


# ===========================================================================
# 4. set_agent_role — Dashboard "Role"
# ===========================================================================


def test_set_agent_role_loest_formerly_auf(umbenannt):
    from nexus_memory.agent_detect import set_agent_role

    result = set_agent_role(ALT, "admin")

    assert result.get("status") == "updated"
    assert result.get("agent_id") == NEU
    assert _ids(umbenannt) == [NEU]
    assert _registry(umbenannt)["agents"][0]["role"] == "admin"


# ===========================================================================
# 5. register_remote_agent — Remote-Vorregistrierung
# ===========================================================================


def test_register_remote_agent_weist_formerly_namen_ab(isolated_agents_registry):
    """``hermes`` ist ein lokaler Detektor-Name — hier ein freier Name, damit
    wirklich die ``formerly``-Pruefung greift und nicht die Lokal-Sperre."""
    from nexus_memory.agent_detect import register_remote_agent

    _registry_file(
        isolated_agents_registry,
        _entry("vps-neu", formerly=["vps-alt"]),
    )

    result = register_remote_agent("vps-alt", "Alter VPS")

    assert "error" in result
    assert "former name" in result["error"]
    assert _ids(isolated_agents_registry) == ["vps-neu"]


# ===========================================================================
# 6. issue_key — eigener Schluessel
# ===========================================================================


def test_issue_key_loest_formerly_auf(umbenannt):
    from nexus_memory import service_auth

    key = service_auth.issue_key(ALT, "Hermes")

    assert _ids(umbenannt) == [NEU]
    entry = _registry(umbenannt)["agents"][0]
    assert entry.get(service_auth.KEY_HASH_FIELD) == service_auth.hash_key(key)
    # Der Schluessel gehoert dem umbenannten Agenten, nicht dem alten Namen.
    assert service_auth.verify_key(key, "") == (NEU, False)


# ===========================================================================
# 7. mcp_server._count_memory_access — Kern-Zaehler
# ===========================================================================


def test_count_memory_access_loest_formerly_auf(umbenannt, monkeypatch):
    import nexus_memory.mcp_server as mcp

    monkeypatch.setenv("NEXUS_AGENT_ID", ALT)
    mcp._count_memory_access(write=True)

    assert _ids(umbenannt) == [NEU]
    assert _registry(umbenannt)["agents"][0]["writes"] == 3


# ===========================================================================
# 8. Dashboard "Detect Agents" — legt grundsaetzlich nichts an
# ===========================================================================


def test_dashboard_detect_legt_keine_eintraege_an(umbenannt):
    pytest.importorskip("fastapi", reason="dashboard-Server braucht fastapi")
    pytest.importorskip("uvicorn", reason="dashboard-Server braucht uvicorn")

    import os

    dash = os.path.join(str(Path(__file__).resolve().parents[1]), "dashboard")
    if dash not in sys.path:
        sys.path.insert(0, dash)
    import server

    before = umbenannt.read_text(encoding="utf-8")
    from starlette.testclient import TestClient

    response = TestClient(server.app).get("/api/detect")

    assert response.status_code == 200
    assert umbenannt.read_text(encoding="utf-8") == before


def test_health_audit_daemon_startet_im_test_nicht():
    """Der Audit-Daemon sweepet das Register aus einem Hintergrund-Thread.

    45 s nach dem Start ruft er ``cleanup_removed_agents()`` gegen den DANN
    aktuellen Pfad — unter pytest also gegen das Register des naechsten Tests.
    Genau das hat ``test_dashboard_detect_legt_keine_eintraege_an`` auf dem
    Windows-Runner rot gemacht: der Daemon leerte die Datei, die der Test
    gerade geschrieben hatte. Die Sperre sitzt in ``conftest.py``; hier steht,
    dass sie greift.
    """
    import threading

    daemon = threading.Thread(target=lambda: None,
                              name="nexus-health-audit", daemon=True)
    daemon.start()
    daemon.join(1)

    assert daemon.ident is None, "der Audit-Daemon darf unter pytest nicht starten"


# ===========================================================================
# 9. Dashboard "Connect" — der Routen-Weg zu register_agent
# ===========================================================================


def test_dashboard_connect_legt_keinen_geistereintrag_an(
        isolated_agents_registry, monkeypatch, tmp_path):
    pytest.importorskip("fastapi", reason="dashboard-Server braucht fastapi")
    pytest.importorskip("uvicorn", reason="dashboard-Server braucht uvicorn")

    import os

    dash = os.path.join(str(Path(__file__).resolve().parents[1]), "dashboard")
    if dash not in sys.path:
        sys.path.insert(0, dash)
    import server

    # Die Route kennt nur windsurf/pi als Ziel — also wird windsurf umbenannt.
    _registry_file(
        isolated_agents_registry,
        _entry("w9x2k4", name="Windsurf Buero", formerly=["windsurf"]),
    )
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(server.Path, "home", staticmethod(lambda: fake_home))

    from starlette.testclient import TestClient

    response = TestClient(server.app).post(
        "/api/agents/windsurf/connect",
        headers={"X-Nexus-Dashboard": "1"},
    )

    assert response.status_code == 200
    assert _ids(isolated_agents_registry) == ["w9x2k4"]
    assert _registry(isolated_agents_registry)["agents"][0]["name"] == "Windsurf Buero"


# ===========================================================================
# 10. Claude-Code-Hook — direkter Writer ohne nexus_memory-Import
# ===========================================================================


def test_auto_recall_hook_loest_formerly_auf(umbenannt, monkeypatch, tmp_path):
    import importlib.util

    monkeypatch.setenv("NEXUS_ENV_FILE", str(tmp_path / "keine.env"))
    scripts = Path(__file__).resolve().parents[1] / "plugins" / "claude-code" / "scripts"
    spec = importlib.util.spec_from_file_location(
        "_probe_auto_recall_formerly", scripts / "auto_recall.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["_probe_auto_recall_formerly"] = module
    spec.loader.exec_module(module)

    monkeypatch.setattr(module, "AGENTS_FILE", umbenannt)
    monkeypatch.setattr(module, "AGENT_ID", ALT)
    monkeypatch.setattr(module, "_ACCESS_COUNTED", False)
    module._record_access(read=True)

    assert _ids(umbenannt) == [NEU]
    assert _registry(umbenannt)["agents"][0]["reads"] == 5


# ===========================================================================
# 11. OpenClaw-Plugin — eigener Node-Writer, hier gestartet wie im Repo ueblich
# ===========================================================================


def test_openclaw_writer_loest_formerly_auf():
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    script = (Path(__file__).resolve().parents[1]
              / "plugins" / "openclaw" / "test-agent-formerly.mjs")
    result = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, (result.stdout[-1000:] + result.stderr[-1000:])
    assert "PASS" in result.stdout
