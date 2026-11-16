"""``x-nexus-agent-id`` bestimmt keine Identitaet mehr.

Es gibt genau EINE Identitaetsquelle: den Agentenschluessel. Ein Aufruf mit
dem Schluessel von A und dem Kopf ``x-nexus-agent-id: B`` zaehlt als A — im
Eintrag, im Protokoll und in der Telemetrie. Der Kopf wird vollstaendig
ignoriert; er steht nur noch da, weil alte Plugins ihn senden.
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
FAKT = "Der Verteiler liegt im Flur neben der Kellertuer"
A = "probe-agent-a"
B = "probe-agent-b"


class _FakeQdrant:
    """Nur da, damit ``remember`` ein Attribut vorfindet (Kategorie session)."""

    def query_points(self, **_kwargs):
        raise AssertionError("diese Kategorie darf keine Aehnlichkeitssuche starten")


@pytest.fixture(autouse=True)
def _install_key(monkeypatch, isolated_agents_registry):
    monkeypatch.setenv("NEXUS_SERVICE_AUTH_KEY", INSTALL_KEY)
    monkeypatch.delenv("NEXUS_AGENT_ID", raising=False)


class _Record:
    """The captured write points and the request identity seen in the thread."""

    def __init__(self) -> None:
        self.points: list = []
        self.seen: dict = {}


@pytest.fixture
def written(monkeypatch):
    """Haengt sich in den ECHTEN Schreibvorgang und haelt die Punkte fest."""
    import nexus_memory.collection_vectors as collection_vectors
    import nexus_memory.mcp_server as mcp

    record = _Record()
    monkeypatch.setattr(
        collection_vectors, "upsert_named",
        lambda _client, _collection, pts, _vector_name: record.points.extend(pts))
    monkeypatch.setattr(mcp, "_index_written_point", lambda _payload: None)

    store = object.__new__(mcp.MemoryStore)
    store.vector_name = None
    store._hybrid_retriever = None
    store._skill_graph = None
    store._scope_centroids = None
    store.client = _FakeQdrant()

    async def _embed(_text, is_query=True):
        return [0.1] * 4

    store._embed = _embed

    def _get_store():
        # Der Kontext wird in den Thread kopiert — hier steht genau das,
        # was die Anfrage als Absender gesetzt hat.
        record.seen["request_agent_id"] = mcp._request_agent_id()
        return store

    monkeypatch.setattr(mcp, "get_store", _get_store)
    return record


def _write(key: str, header: str | None = None):
    from starlette.testclient import TestClient

    import nexus_memory.mcp_server as mcp
    from nexus_memory import service_auth

    headers = {service_auth.AUTH_HEADER: key}
    if header is not None:
        headers[mcp.AGENT_ID_HEADER] = header
    with TestClient(mcp.build_serve_app(), base_url="http://127.0.0.1:9122",
                    client=("127.0.0.1", 40000)) as client:
        return client.post("/write", json={
            "content": FAKT, "category": "session", "access_level": "private",
            "source": "dsh-plugin", "source_type": "session",
        }, headers=headers)


def _agents(registry: Path) -> dict:
    if not registry.exists():
        return {}
    data = json.loads(registry.read_text(encoding="utf-8"))
    return {a.get("id"): a for a in data.get("agents", [])}


def _messages(response) -> list[dict]:
    """JSON-RPC messages from a JSON or SSE body (Streamable HTTP)."""
    if "text/event-stream" in response.headers.get("content-type", ""):
        out = []
        for line in response.text.splitlines():
            if line.startswith("data:"):
                payload = line[len("data:"):].strip()
                if payload:
                    out.append(json.loads(payload))
        return out
    return [response.json()]


def test_header_cannot_impersonate_another_agent(isolated_agents_registry,
                                                 written):
    from nexus_memory import service_auth

    key_a = service_auth.issue_key(A, "A")
    service_auth.issue_key(B, "B")

    r = _write(key_a, header=B)

    assert r.status_code == 200, r.text
    assert len(written.points) == 1
    assert written.points[0].payload["agent"] == A, "der Schluessel nennt den Absender"
    assert written.seen["request_agent_id"] == A

    agents = _agents(isolated_agents_registry)
    assert agents[A]["writes"] == 1
    assert agents[B]["writes"] == 0, "B darf nichts gutgeschrieben bekommen"


def test_installation_key_plus_header_names_nobody(written,
                                                   isolated_agents_registry):
    """Der alte Weg war Installationsschluessel + Kopf. Der Kopf nennt heute nichts.

    Frueher zaehlte so ein Aufruf als B — heute wird er abgewiesen, weil der
    Installationsschluessel keine Daten mehr schreibt.
    """
    r = _write(INSTALL_KEY, header=B)

    assert r.status_code == 401
    assert written.points == []
    assert _agents(isolated_agents_registry) == {}


def test_header_without_a_key_is_still_401(written):
    from starlette.testclient import TestClient

    import nexus_memory.mcp_server as mcp

    with TestClient(mcp.build_serve_app(), base_url="http://127.0.0.1:9122",
                    client=("127.0.0.1", 40000)) as client:
        r = client.post("/write", json={"content": FAKT, "category": "session"},
                        headers={mcp.AGENT_ID_HEADER: B})

    assert r.status_code == 401
    assert written.points == []


def test_movement_log_names_the_key_holder(isolated_agents_registry, written):
    from nexus_memory import service_auth, service_log

    key_a = service_auth.issue_key(A, "A")
    _write(key_a, header=B)

    lines = service_log.read_events(limit=1, action="write")
    assert lines and lines[0]["actor"] == A


def test_mcp_tools_see_the_key_holder(isolated_agents_registry):
    """Derselbe Beweis auf dem MCP-Weg: ``tools/list`` fragt die Rolle des Aufrufers."""
    from starlette.testclient import TestClient

    import nexus_memory.mcp_server as mcp
    from nexus_memory import service_auth

    key_a = service_auth.issue_key(A, "A")
    service_auth.issue_key(B, "B")
    # B ist Admin, A nicht: nur wenn der Kopf zaehlen wuerde, saehe der Aufruf
    # die Verwaltungswerkzeuge.
    from nexus_memory.agent_detect import set_agent_role

    assert "error" not in set_agent_role(B, "admin")

    accept = "application/json, text/event-stream"
    headers = {service_auth.AUTH_HEADER: key_a, mcp.AGENT_ID_HEADER: B}

    with TestClient(mcp.build_serve_app(), base_url="http://127.0.0.1:9122",
                    client=("127.0.0.1", 40000)) as client:
        init = client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "pytest", "version": "1.0"}},
        }, headers={**headers, "Accept": accept})
        session = init.headers.get("mcp-session-id")
        if session:
            headers["mcp-session-id"] = session
        client.post("/mcp", json={"jsonrpc": "2.0",
                                  "method": "notifications/initialized"},
                    headers={**headers, "Accept": accept})
        r = client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers={**headers, "Accept": accept})

    assert r.status_code == 200, r.text
    names = {t["name"] for t in _messages(r)[-1]["result"]["tools"]}
    assert "backup" not in names, "A ist kein Admin — der Kopf B aendert das nicht"
