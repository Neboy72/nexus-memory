"""Die 200-Antwort von ``/write`` muss den scope nennen, der WIRKLICH im Punkt steht.

Befund (24.11.2026): ein Punkt mit ``scope: "hermes"``, über ``POST /write``
ohne scope-Feld überschrieben, antwortete mit ``"scope": "default"``. Der Punkt
behielt ``hermes`` — das Zusammenführen nimmt den alten Punkt als Basis und
lässt ``scope`` stehen. Falsch war also die Auskunft: sowohl der Rumpf als auch
die Zeile im Bewegungsprotokoll behaupteten einen anderen Geltungsbereich als
den gespeicherten.

Ohne den Fix ist der erste Test rot (Antwort ``default``, Punkt ``hermes``).
Geprüft wird an der echten ASGI-App mit echtem Agentenschlüssel und
eingebetteter Wegwerf-Sammlung, nie an ``nexus_hf``.

Der Aufbau (``welt``/``rolle``) ist bewusst eine Kopie aus
``test_write_eigentum.py``: dort gehören die Fixtures zu den Eigentums-Tests,
und ein Import eines Testmoduls in ein anderes würde dessen Tests doppelt
einsammeln.
"""

from __future__ import annotations

import json
import uuid

import pytest
from qdrant_client import models

from conftest import (
    bind_test_collection,
    embedded_qdrant,
    test_vector_name as _vector_name,
)

DIM = 2
COLLECTION = "zz_test_scope_antwort"
FAKT = "Der Verteiler liegt im Flur neben der Kellertuer und bleibt dauerhaft aus."
EIGNER = "eigner-bot"


@pytest.fixture
def welt(monkeypatch, embedded_qdrant):
    import nexus_memory.mcp_server as mcp

    client = embedded_qdrant()
    binding = bind_test_collection(client, COLLECTION, DIM)
    vector_name = _vector_name(DIM)

    monkeypatch.setattr(mcp, "COLLECTION_NAME", COLLECTION)
    monkeypatch.setattr(mcp, "_index_written_point", lambda *a, **k: None)
    monkeypatch.setattr(mcp, "_count_memory_access", lambda *a, **k: None)

    store = object.__new__(mcp.MemoryStore)
    store.client = client
    store.vector_name = vector_name
    store._hybrid_retriever = None
    store._skill_graph = None
    store._scope_centroids = None

    async def _embed(_text, is_query=True):
        return [0.1] * DIM

    store._embed = _embed
    monkeypatch.setattr(mcp, "get_store", lambda: store)
    return store, client, binding, vector_name


@pytest.fixture
def rolle(isolated_nexus_data_dir):
    from nexus_memory import agent_detect, service_auth

    def _rolle(agent_id: str, role: str = "know") -> dict:
        key = service_auth.issue_key(agent_id, agent_id)
        result = agent_detect.set_agent_role(agent_id, role)
        assert "error" not in result, result
        return {service_auth.AUTH_HEADER: key}

    return _rolle


def _seed(client, vector_name, pid, scope="hermes"):
    client.upsert(collection_name=COLLECTION, points=[models.PointStruct(
        id=pid,
        vector={vector_name: [0.25, 0.5]},
        payload={
            "id": pid,
            "content": "alte Fassung",
            "category": "fact",
            "access_level": "private",
            "lifecycle_status": "canonical",
            "source": "fremde-quelle",
            "provenance": {"source_type": "hermes-plugin", "created_by": "x"},
            "created_at": "2026-06-01T10:00:00+00:00",
            "use_count": 5,
            "salience": 0.9,
            "scope": scope,
            "agent": EIGNER,
        },
    )])


def _client():
    from starlette.testclient import TestClient

    import nexus_memory.mcp_server as mcp

    return TestClient(mcp.build_serve_app(), base_url="http://127.0.0.1:9122",
                      client=("127.0.0.1", 40000))


def _write(headers, *, pid=None, scope=None):
    rumpf = {
        "content": FAKT,
        "category": "fact",
        "access_level": "private",
        "source": "openclaw_tool",
        "source_type": "chat",
    }
    if pid is not None:
        rumpf["id"] = pid
    if scope is not None:
        rumpf["scope"] = scope
    return _client().post("/write", json=rumpf, headers=headers)


def _payload(client, pid) -> dict:
    return client.retrieve(collection_name=COLLECTION, ids=[pid],
                           with_payload=True, with_vectors=False)[0].payload or {}


def _protokoll(pid) -> list[dict]:
    from nexus_memory import service_log

    zeilen: list[str] = []
    for path in sorted(service_log.log_dir().glob("service-*.jsonl")):
        zeilen.extend(path.read_text(encoding="utf-8").splitlines())
    return [z for z in (json.loads(x) for x in zeilen if x.strip())
            if z.get("target_id") == pid]


# ── 1. Die Antwort nennt den gespeicherten Geltungsbereich ────────────


def test_antwort_nennt_den_scope_des_punktes(welt, rolle):
    _store, client, _binding, vector_name = welt
    pid = str(uuid.uuid4())
    _seed(client, vector_name, pid)

    antwort = _write(rolle(EIGNER, "know"), pid=pid)

    assert antwort.status_code == 200, antwort.text
    gespeichert = _payload(client, pid).get("scope")
    assert gespeichert == "hermes", "der Punkt muss seinen scope behalten"
    assert antwort.json().get("scope") == gespeichert, (
        f"Antwort sagt {antwort.json().get('scope')!r}, im Punkt steht "
        f"{gespeichert!r}"
    )


# ── 2. Auch die Protokollzeile nennt den gespeicherten Wert ────────────


def test_protokollzeile_nennt_den_scope_des_punktes(welt, rolle):
    _store, client, _binding, vector_name = welt
    pid = str(uuid.uuid4())
    _seed(client, vector_name, pid)

    assert _write(rolle(EIGNER, "know"), pid=pid).status_code == 200

    zeilen = [z for z in _protokoll(pid) if z.get("action") == "write"]
    assert len(zeilen) == 1, zeilen
    assert zeilen[0].get("scope") == "hermes", (
        f"Protokoll sagt {zeilen[0].get('scope')!r}, im Punkt steht "
        f"{_payload(client, pid).get('scope')!r}"
    )


# ── 3. Ein ausdruecklich gesetzter scope aendert weiterhin nichts ─────


def test_neuer_punkt_meldet_seinen_scope(welt, rolle):
    """Ein neuer Punkt ohne ``id`` ist kein Überschreiben: hier gilt die Angabe."""
    _store, client, _binding, _vector_name = welt

    antwort = _write(rolle(EIGNER, "know"), scope="kiosha")
    assert antwort.status_code == 200, antwort.text
    pid = antwort.json()["id"]
    assert _payload(client, pid).get("scope") == "kiosha"
    assert antwort.json().get("scope") == "kiosha"
