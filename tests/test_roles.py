"""Rollen und Eigentum (15.11.2026).

Drei Stufen ersetzen das „darf alles":

- ``know`` liest und schreibt, zieht aber nur EIGENE Eintraege zurueck.
- ``work`` darf zusaetzlich Graph fragen und fremde Eintraege aufraeumen.
- ``admin`` verwaltet (Backups, Webhooks, Guardrail-Override, Kosten).

Zwei Dinge muessen zusammen gelten, sonst ist die Tabelle eine Empfehlung:
``tools/list`` zeigt nur die erlaubten Werkzeuge, UND der Aufruf wird
serverseitig abgewiesen, auch wenn der Name bekannt ist. Beides steht hier.

Eigentum heisst: ``payload["agent"]`` traegt die Kennung des Aufrufers. Ein
FEHLENDES Feld (Altbestand) ist ausdruecklich NICHT eigen — eine Ausnahme fuer
Altdaten wuerde die Regel wertlos machen, weil jedes Aufraeumen zuerst die
aeltesten Eintraege trifft.

Anonym gilt ``know`` (fail-closed): der globale Schluessel ohne
Agenten-Zuordnung kann nichts verwalten.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import nexus_memory.mcp_server as mcp
from nexus_memory import agent_detect, roles, service_auth, service_log

ZIEL = "3f8f5c2e-6a1b-4c7d-9e2f-1a2b3c4d5e6f"
FREMD = "8e2cb680-1111-4c7d-9e2f-1a2b3c4d5e6f"

#: Alles, was der Dienst heute anbietet — die volle Liste fuer ``admin``.
ALLE_WERKZEUGE = {
    "remember", "recall", "forget", "update", "fact_history", "health",
    "report", "list_reports", "resolve_report",
    "subscribe", "unsubscribe", "list_subscriptions", "backup", "restore",
    "guardrail_check", "guardrail_override", "graph_traverse",
    "find_entities", "get_subgraph", "get_related", "cost_routing_stats",
    "cost_routing_explain",
}

KNOW_WERKZEUGE = {
    "recall", "remember", "health", "guardrail_check", "fact_history",
    "report", "forget", "update",
}

WORK_WERKZEUGE = KNOW_WERKZEUGE | {
    "graph_traverse", "find_entities", "get_subgraph", "get_related",
    "list_reports", "resolve_report",
}

ADMIN_WERKZEUGE = WORK_WERKZEUGE | {
    "subscribe", "unsubscribe", "list_subscriptions", "backup", "restore",
    "guardrail_override", "cost_routing_stats", "cost_routing_explain",
}


# ===========================================================================
# Hilfen
# ===========================================================================

def _entries() -> list[dict]:
    """Die Zeilen des Bewegungsprotokolls (conftest zeigt es auf tmp)."""
    lines: list[str] = []
    for path in sorted(service_log.log_dir().glob("service-*.jsonl")):
        lines.extend(path.read_text(encoding="utf-8").splitlines())
    return [json.loads(line) for line in lines if line.strip()]


def _body(out) -> dict:
    return json.loads(out[0].text)


def _as(monkeypatch, agent: str, role: str) -> str:
    """Der Testprozess IST dieser Agent (derselbe Weg wie stdio-MCP)."""
    monkeypatch.delenv("NEXUS_AGENT_ID", raising=False)
    monkeypatch.setenv("NEXUS_AGENT_ID", agent)
    assert "error" not in agent_detect.set_agent_role(agent, role)
    return agent


class _FakeQdrant:
    """Nur die Qdrant-Flaeche, die forget/update anfassen."""

    def __init__(self) -> None:
        self.payloads: dict[str, dict] = {}
        self.deleted: list[str] = []
        self.set_payloads: list[dict] = []

    def retrieve(self, ids=None, **_kwargs):
        return [SimpleNamespace(id=i, payload=dict(self.payloads[i]))
                for i in (ids or []) if i in self.payloads]

    def delete(self, points_selector=None, **_kwargs):
        ids = list(getattr(points_selector, "points", []) or [])
        self.deleted.extend(ids)
        for i in ids:
            self.payloads.pop(i, None)
        return SimpleNamespace(status="completed")

    def set_payload(self, payload=None, points=None, **_kwargs):
        self.set_payloads.append({"payload": payload, "points": points})
        for p in points or []:
            self.payloads.setdefault(p, {}).update(payload or {})
        return SimpleNamespace(status="completed")


@pytest.fixture
def store(monkeypatch):
    """Ein echter ``MemoryStore`` mit Attrappen fuer Qdrant und Embedder.

    ``dispatch_event`` ist stillgelegt: ein Webhook-Abonnement des
    Entwicklers darf von einem Testlauf nicht angesprochen werden.
    """
    memory_store = object.__new__(mcp.MemoryStore)
    memory_store.vector_name = None
    memory_store._hybrid_retriever = None
    memory_store._skill_graph = None
    memory_store._scope_centroids = None
    memory_store.client = _FakeQdrant()

    async def _embed(_text, is_query=True):
        return [0.1] * 4

    memory_store._embed = _embed
    monkeypatch.setattr(mcp, "get_store", lambda: memory_store)
    monkeypatch.setattr(mcp, "_index_written_point", lambda _payload: None)

    async def _no_dispatch(*_args, **_kwargs):
        return None

    monkeypatch.setattr(mcp, "dispatch_event", _no_dispatch)
    return memory_store


# ===========================================================================
# 1. tools/list zeigt nur, was die Stufe darf
# ===========================================================================

class TestWerkzeugliste:
    async def test_know_sieht_nur_seine_werkzeuge(self):
        tools = await mcp.handle_list_tools(role="know")

        assert {t.name for t in tools} == KNOW_WERKZEUGE
        assert "backup" not in {t.name for t in tools}
        assert "graph_traverse" not in {t.name for t in tools}

    async def test_work_sieht_graph_aber_keine_verwaltung(self):
        tools = await mcp.handle_list_tools(role="work")

        assert {t.name for t in tools} == WORK_WERKZEUGE

    async def test_admin_sieht_alle_werkzeuge(self):
        tools = await mcp.handle_list_tools(role="admin")

        assert {t.name for t in tools} == ALLE_WERKZEUGE
        assert len(tools) == len(ALLE_WERKZEUGE)

    def test_die_tabelle_ist_vollstaendig(self):
        """Jedes angebotene Werkzeug steht bewusst in ``TOOL_MIN_ROLE``.

        Ein Werkzeug, das dort fehlt, waere fuer alle ausser ``admin``
        unsichtbar — das soll auffallen, nicht still passieren.
        """
        assert set(roles.TOOL_MIN_ROLE) == ALLE_WERKZEUGE

    def test_unbekanntes_werkzeug_ist_nur_admin(self):
        assert roles.tool_allowed("admin", "brandneues_werkzeug") is True
        assert roles.tool_allowed("work", "brandneues_werkzeug") is False
        assert roles.tool_allowed("know", "brandneues_werkzeug") is False

    def test_unbekannte_rolle_zaehlt_als_know(self):
        assert roles.tool_allowed("gibberish", "recall") is True
        assert roles.tool_allowed("gibberish", "backup") is False


# ===========================================================================
# 2. Der Aufruf wird abgewiesen, nicht nur versteckt
# ===========================================================================

class TestAbweisung:
    async def test_know_backup_wird_abgewiesen_und_protokolliert(
            self, monkeypatch):
        _as(monkeypatch, "probe-bot", "know")

        def _kein_qdrant():
            raise AssertionError(
                "ein abgewiesener Aufruf darf Qdrant nicht anfassen")

        monkeypatch.setattr(mcp, "get_store", _kein_qdrant)

        out = await mcp.handle_call_tool("backup", {})
        body = _body(out)

        assert body["status"] == "denied"
        assert body["error"] == "not permitted: backup"
        assert body["role"] == "know"

        entry = _entries()[-1]
        assert entry["action"] == "denied"
        assert entry["result"] == "refused"
        assert entry["reason"] == "not permitted: backup"
        assert entry["actor"] == "probe-bot"

    async def test_know_graph_wird_abgewiesen(self, monkeypatch):
        _as(monkeypatch, "probe-bot", "know")

        out = await mcp.handle_call_tool(
            "graph_traverse", {"fact_id": ZIEL})

        assert _body(out)["status"] == "denied"
        assert _entries()[-1]["reason"] == "not permitted: graph_traverse"


# ===========================================================================
# 3. Eigentum bei forget / update
# ===========================================================================

class TestEigentum:
    async def test_know_darf_eigenen_eintrag_zurueckziehen(
            self, store, monkeypatch):
        _as(monkeypatch, "probe-bot", "know")
        store.client.payloads[ZIEL] = {
            "content": "Der Verteiler steht im Flur", "agent": "probe-bot",
            "category": "fact", "access_level": "trusted",
        }

        body = _body(await mcp.handle_call_tool("forget", {"memory_id": ZIEL}))

        assert body["status"] == "deleted"
        assert store.client.deleted == [ZIEL]
        assert _entries()[-1]["action"] == "forget"

    async def test_know_darf_fremden_eintrag_nicht_zurueckziehen(
            self, store, monkeypatch):
        _as(monkeypatch, "probe-bot", "know")
        store.client.payloads[FREMD] = {
            "content": "Der Verteiler steht im Keller", "agent": "hermes",
            "category": "fact", "access_level": "trusted",
        }

        body = _body(await mcp.handle_call_tool("forget", {"memory_id": FREMD}))

        assert body["status"] == "denied"
        assert body["error"] == "not owner"
        assert store.client.deleted == [], "nichts darf geloescht sein"
        entry = _entries()[-1]
        assert entry["action"] == "denied"
        assert entry["result"] == "refused"
        assert entry["reason"] == "not owner"
        assert entry["target_id"] == FREMD

    async def test_altbestand_ohne_agent_feld_ist_nicht_eigen(
            self, store, monkeypatch):
        """Ein Punkt ohne ``agent`` gehoert niemandem, den man fragen koennte."""
        _as(monkeypatch, "probe-bot", "know")
        store.client.payloads[ZIEL] = {
            "content": "Der Verteiler steht im Flur",
            "category": "fact", "access_level": "trusted",
        }

        body = _body(await mcp.handle_call_tool("forget", {"memory_id": ZIEL}))

        assert body["status"] == "denied"
        assert body["error"] == "not owner"
        assert store.client.deleted == []

    async def test_leeres_agent_feld_ist_nicht_eigen(self, store, monkeypatch):
        _as(monkeypatch, "probe-bot", "know")
        store.client.payloads[ZIEL] = {
            "content": "Der Verteiler steht im Flur", "agent": "   ",
            "category": "fact", "access_level": "trusted",
        }

        body = _body(await mcp.handle_call_tool("forget", {"memory_id": ZIEL}))

        assert body["error"] == "not owner"
        assert store.client.deleted == []

    async def test_work_darf_fremden_eintrag_zurueckziehen(
            self, store, monkeypatch):
        _as(monkeypatch, "openclaw", "work")
        store.client.payloads[FREMD] = {
            "content": "Der Verteiler steht im Keller", "agent": "hermes",
            "category": "fact", "access_level": "trusted",
        }

        body = _body(await mcp.handle_call_tool("forget", {"memory_id": FREMD}))

        assert body["status"] == "deleted"
        assert store.client.deleted == [FREMD]

    async def test_admin_darf_fremden_eintrag_zurueckziehen(
            self, store, monkeypatch):
        _as(monkeypatch, "hermes", "admin")
        store.client.payloads[FREMD] = {
            "content": "Der Verteiler steht im Keller", "agent": "probe-bot",
            "category": "fact", "access_level": "trusted",
        }

        body = _body(await mcp.handle_call_tool("forget", {"memory_id": FREMD}))

        assert body["status"] == "deleted"
        assert store.client.deleted == [FREMD]

    async def test_know_darf_fremden_eintrag_nicht_aendern(
            self, store, monkeypatch):
        _as(monkeypatch, "probe-bot", "know")
        store.client.payloads[FREMD] = {
            "content": "Der Verteiler steht im Keller", "agent": "hermes",
            "category": "fact", "access_level": "trusted",
        }

        body = _body(await mcp.handle_call_tool(
            "update", {"memory_id": FREMD, "text": "Der Verteiler steht im Flur"}))

        assert body["status"] == "denied"
        assert body["error"] == "not owner"
        assert store.client.set_payloads == [], \
            "die Ablehnung darf den Eintrag nicht vorher anfassen"
        assert store.client.payloads[FREMD]["content"] == "Der Verteiler steht im Keller"

    async def test_admin_darf_restore(self, store, monkeypatch, tmp_path):
        _as(monkeypatch, "hermes", "admin")
        import nexus_memory.collection_vectors as collection_vectors

        written: list = []
        monkeypatch.setattr(
            collection_vectors, "upsert_named",
            lambda _client, _collection, pts, _vector_name, **_kw: written.extend(pts),
        )
        backup = tmp_path / "sicherung.json"
        backup.write_text(json.dumps({"points": [{
            "id": ZIEL,
            "payload": {"content": "Der Verteiler steht im Flur",
                        "category": "fact", "access_level": "trusted"},
            "vector": [0.1, 0.2, 0.3, 0.4],
        }]}), encoding="utf-8")

        body = _body(await mcp.handle_call_tool(
            "restore", {"backup_path": str(backup)}))

        assert body["status"] == "success"
        assert body["restored"] == 1
        assert len(written) == 1


# ===========================================================================
# 4. Fehlende Rolle und anonyme Aufrufer
# ===========================================================================

class TestVorgabe:
    def test_fehlende_rolle_gilt_als_know(self):
        agent_detect.register_agent(
            "ohne-rolle", "Ohne Rolle", "🔌", "public", "unknown")

        assert agent_detect.agent_role("ohne-rolle") == "know"

    def test_unbekannter_wert_gilt_als_know(self):
        agent_detect.set_agent_role("kaputt", "root")

        assert agent_detect.agent_role("kaputt") == "know"

    def test_nicht_registrierter_aufrufer_ist_know(self):
        assert agent_detect.agent_role("gibt-es-nicht") == "know"
        assert agent_detect.agent_role(None) == "know"

    def test_rolle_wird_bei_jedem_aufruf_frisch_gelesen(self, monkeypatch):
        """Umschalten wirkt beim naechsten Aufruf — ohne Neustart."""
        _as(monkeypatch, "probe-bot", "know")
        assert mcp._caller_role() == "know"

        agent_detect.set_agent_role("probe-bot", "admin")
        assert mcp._caller_role() == "admin"

    def test_anonymer_aufruf_ist_know(self, monkeypatch):
        monkeypatch.delenv("NEXUS_AGENT_ID", raising=False)
        monkeypatch.setattr(mcp, "_request_agent_id", lambda: None)

        assert mcp._caller_role() == "know"


# ===========================================================================
# 5. Die Rolle kommt aus dem Schluessel (HTTP, live)
# ===========================================================================

def _client():
    from starlette.testclient import TestClient

    return TestClient(mcp.build_serve_app(), base_url="http://127.0.0.1:9122")


def _headers(agent: str | None = None) -> dict:
    """Ein eigener Schluessel des genannten Agenten.

    15.11.2026: der Installationsschluessel liest keine Daten mehr, und der
    Kopf ``x-nexus-agent-id`` bestimmt keine Identitaet mehr — der Aufrufer
    meldet sich mit einem Agentenschluessel an, wie ein Plugin nach /enroll.
    """
    agent = agent or "probe-bot"
    return {service_auth.AUTH_HEADER: service_auth.issue_key(agent, agent)}


def _messages(response) -> list[dict]:
    if "text/event-stream" in response.headers.get("content-type", ""):
        out = []
        for line in response.text.splitlines():
            if line.startswith("data:"):
                payload = line[len("data:"):].strip()
                if payload:
                    out.append(json.loads(payload))
        return out
    return [response.json()]


def _mcp(method: str, params: dict, agent: str | None = None) -> list[dict]:
    """initialize → notifications/initialized → ``method`` ueber POST /mcp."""
    accept = "application/json, text/event-stream"
    headers = _headers(agent)
    with _client() as client:
        init = client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "pytest", "version": "1.0"}},
        }, headers={**headers, "Accept": accept})
        session_id = init.headers.get("mcp-session-id")
        if session_id:
            headers["mcp-session-id"] = session_id
        client.post("/mcp", json={
            "jsonrpc": "2.0", "method": "notifications/initialized",
        }, headers={**headers, "Accept": accept})
        return _messages(client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 2, "method": method, "params": params,
        }, headers={**headers, "Accept": accept}))


@pytest.fixture(autouse=True)
def _service_key(monkeypatch):
    monkeypatch.setenv("NEXUS_SERVICE_AUTH_KEY", "test-service-key")
    monkeypatch.delenv("NEXUS_AGENT_ID", raising=False)


class TestEigenerSchluessel:
    def test_frischer_agent_ist_know_und_sieht_keine_verwaltung(self):
        messages = _mcp("tools/list", {})

        names = {t["name"] for t in messages[-1]["result"]["tools"]}
        assert names == KNOW_WERKZEUGE
        assert "backup" not in names

    def test_frischer_agent_darf_kein_backup(self):
        messages = _mcp("tools/call", {"name": "backup", "arguments": {}})

        body = json.loads(messages[-1]["result"]["content"][0]["text"])
        assert body["status"] == "denied"
        assert body["error"] == "not permitted: backup"
        entry = _entries()[-1]
        assert entry["action"] == "denied"
        # Der Absender ist der Schluessel-Inhaber, nicht mehr "unidentified".
        assert entry["actor"] == "probe-bot"

    def test_mit_zuordnung_gilt_die_rolle_des_agenten(self):
        agent_detect.set_agent_role("probe-bot", "know")

        messages = _mcp("tools/list", {}, agent="probe-bot")

        names = {t["name"] for t in messages[-1]["result"]["tools"]}
        assert names == KNOW_WERKZEUGE


# ===========================================================================
# 6. CLI
# ===========================================================================

class TestCLI:
    def test_setzt_eine_rolle(self, isolated_agents_registry, capsys):
        rc = service_auth.main(["--role", "--id", "probe-bot", "--set", "work"])

        assert rc == 0
        assert agent_detect.agent_role("probe-bot") == "work"
        assert "work" in capsys.readouterr().out

    def test_setzt_die_vier_bestehenden_rollen(self):
        for agent, role in (("hermes", "admin"), ("openclaw", "work"),
                            ("claude-code", "work"),
                            ("deepseek-harness", "know")):
            assert service_auth.main(
                ["--role", "--id", agent, "--set", role]) == 0
            assert agent_detect.agent_role(agent) == role

    def test_liste_zeigt_die_rolle(self, capsys):
        service_auth.main(["--role", "--id", "hermes", "--set", "admin"])

        assert service_auth.main(["--list"]) == 0

        out = capsys.readouterr().out
        assert "ROLLE" in out
        line = next(l for l in out.splitlines() if "hermes" in l)
        assert "admin" in line

    def test_unbekannte_rolle_wird_abgewiesen(self, capsys):
        with pytest.raises(SystemExit):
            service_auth.main(["--role", "--id", "probe-bot", "--set", "root"])

        assert agent_detect.agent_role("probe-bot") == "know"

    def test_rollenwechsel_steht_im_protokoll(self):
        service_auth.main(["--role", "--id", "probe-bot", "--set", "work"])

        entry = _entries()[-1]
        assert entry["action"] == "role_change"
        assert entry["result"] == "ok"
        assert entry["target_id"] == "probe-bot"
        assert entry["via"] == "cli"


# ===========================================================================
# 7. Dashboard: die Stufe ist umschaltbar
# ===========================================================================

class TestDashboard:
    def test_rolle_umschalten(self):
        pytest.importorskip("fastapi", reason="dashboard-Server braucht fastapi")
        pytest.importorskip("uvicorn", reason="dashboard-Server braucht uvicorn")
        from starlette.testclient import TestClient

        dashboard = str(Path(__file__).resolve().parents[1] / "dashboard")
        if dashboard not in sys.path:
            sys.path.insert(0, dashboard)
        import server  # noqa: PLC0415 — dashboard/server.py

        client = TestClient(server.app)
        response = client.post(
            "/api/agents/hermes/role", params={"role": "work"},
            headers={"X-Nexus-Dashboard": "1"},
        )

        assert response.status_code == 200, response.text
        assert agent_detect.agent_role("hermes") == "work"

    def test_unbekannte_rolle_wird_abgewiesen(self):
        pytest.importorskip("fastapi", reason="dashboard-Server braucht fastapi")
        pytest.importorskip("uvicorn", reason="dashboard-Server braucht uvicorn")
        from starlette.testclient import TestClient

        dashboard = str(Path(__file__).resolve().parents[1] / "dashboard")
        if dashboard not in sys.path:
            sys.path.insert(0, dashboard)
        import server  # noqa: PLC0415 — dashboard/server.py

        client = TestClient(server.app)
        response = client.post(
            "/api/agents/hermes/role", params={"role": "root"},
            headers={"X-Nexus-Dashboard": "1"},
        )

        assert response.status_code == 400
        assert agent_detect.agent_role("hermes") == "know"
