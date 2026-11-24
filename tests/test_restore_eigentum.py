"""Was ``restore`` beim Eigentum tut — geprueft, gemeldet, NICHT geaendert.

Auftrag 24.11.2026, Punkt 4: die Wiederherstellung aus einer Sicherung sollte
ebenfalls geprueft werden. Befund (siehe Bericht):

* ``restore`` ist ein MCP-Werkzeug mit Mindestrolle ``admin``
  (``roles.TOOL_MIN_ROLE["restore"]``). ``know`` und ``work`` kommen nicht
  durch — geprueft in diesem Test.
* Eine EIGENTUEMER-Pruefung je Punkt gibt es dort nicht. Das ist auch nicht
  noetig: ``roles.is_owner`` laesst ``admin`` fremde Eintraege aendern, also
  wuerde dieselbe Regel fuer jeden Punkt ``True`` liefern. Ein ``admin`` kann
  mit ``restore`` folglich jeden Punkt durch den Sicherungsstand ersetzen.

Der Test laeuft gegen eine eingebettete Wegwerf-Sammlung im Temp-Ordner; die
Live-Sammlung wird nicht angefasst.
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
COLLECTION = "zz_test_restore_eigentum"
FAKT = "Der Verteiler steht im Flur und bleibt ausgeschaltet."


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
    return client, binding, vector_name


def _seed(client, vector_name: str, pid: str, owner: str) -> None:
    client.upsert(
        collection_name=COLLECTION,
        points=[models.PointStruct(
            id=pid,
            vector={vector_name: [0.25, 0.5]},
            payload={
                "id": pid,
                "content": "alte Fassung des fremden Punktes",
                "category": "fact",
                "access_level": "private",
                "agent": owner,
                "use_count": 9,
                "scope": "hermes",
            },
        )],
    )


def _lies(client, pid: str) -> dict:
    return client.retrieve(
        collection_name=COLLECTION, ids=[pid],
        with_payload=True, with_vectors=False,
    )[0].payload or {}


def _sicherung(tmp_path, pid: str) -> str:
    path = tmp_path / "sicherung.json"
    path.write_text(json.dumps({"points": [{
        "id": pid,
        "payload": {
            "id": pid,
            "content": "Stand aus der Sicherung",
            "category": "fact",
            "access_level": "private",
            "agent": "frueherer-schreiber",
        },
        "vector": [0.3, 0.7],
    }]}), encoding="utf-8")
    return str(path)


def _protokoll() -> list[dict]:
    from nexus_memory import service_log

    zeilen: list[str] = []
    for path in sorted(service_log.log_dir().glob("service-*.jsonl")):
        zeilen.extend(path.read_text(encoding="utf-8").splitlines())
    return [json.loads(z) for z in zeilen if z.strip()]


@pytest.mark.asyncio
@pytest.mark.parametrize("rolle", ["know", "work"])
async def test_restore_ist_ein_admin_werkzeug(welt, tmp_path, caller_as, rolle):
    """``know`` und ``work`` kommen gar nicht erst bis zum Schreiben."""
    import nexus_memory.mcp_server as mcp

    client, _binding, vector_name = welt
    pid = str(uuid.uuid4())
    _seed(client, vector_name, pid, "fremder")
    vorher = _lies(client, pid)
    caller_as("probe-restore", rolle)

    roh = await mcp.handle_call_tool("restore", {"backup_path": _sicherung(tmp_path, pid)})
    antwort = json.loads(roh[0].text)

    assert antwort["status"] == "denied", antwort
    assert antwort["error"] == "not permitted: restore"
    assert _lies(client, pid) == vorher, "ohne admin darf nichts geschrieben werden"
    assert _protokoll()[-1]["action"] == "denied"


@pytest.mark.asyncio
async def test_admin_restore_ersetzt_den_fremden_punkt_durch_die_sicherung(
        welt, tmp_path, caller_as):
    """Befund zum Eigentum: ``restore`` prueft nicht je Punkt — als ``admin``
    ist das dieselbe Wirkung wie im ``update``-Weg (dort laesst ``is_owner``
    fremde Eintraege fuer ``admin`` zu)."""
    import nexus_memory.mcp_server as mcp

    client, _binding, vector_name = welt
    pid = str(uuid.uuid4())
    _seed(client, vector_name, pid, "fremder")
    caller_as("probe-admin", "admin")

    roh = await mcp.handle_call_tool("restore", {"backup_path": _sicherung(tmp_path, pid)})
    antwort = json.loads(roh[0].text)

    assert antwort["status"] == "success", antwort
    assert antwort["restored"] == 1
    nachher = _lies(client, pid)
    assert nachher["content"] == "Stand aus der Sicherung"
    assert nachher["agent"] == "frueherer-schreiber"
    assert "use_count" not in nachher, (
        "restore ersetzt bewusst (merge=False) — es fuehrt nicht zusammen")
