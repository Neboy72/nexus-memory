"""
Tests für den in-process Dedup-Sweep (health_audit.HealthAuditor._dedup_sweep).

Nebo-Grundentscheidung (02.09.): Nexus Memory ist unabhängig — Wartung läuft
in-process, kein externer Scheduler. Der Sweep ist die erste schreibende
Selbstpflege-Funktion und braucht deshalb harte Tests:

- merge: exakt-normalisierte Duplikate werden zusammengeführt (Keeper = ältester)
- attribute rescue: entity_attributes fehlender Keys wandern auf den Keeper
- unique content bleibt unberührt
- Backup-Datei wird VOR dem Delete geschrieben und enthält gelöschte Punkte
- Kill switch: NEXUS_DEDUP_SWEEP=0 -> kein Sweep (Report ohne dedup_sweep)
- superseded/deleted Lifecycle wird nicht angefasst
"""
import json
import os
import time
import uuid
from datetime import datetime, timezone

import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from nexus_memory import health_audit as ha

DIM = 4


def _mk_client():
    import tempfile
    from qdrant_client import QdrantClient
    return QdrantClient(path=tempfile.mkdtemp())


class _FakeStore:
    def __init__(self, client, collection):
        self.client = client
        self.collection_name = collection
        self.collection = collection


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc).replace(microsecond=0)
            - __import__("datetime").timedelta(days=days_ago)).isoformat()


def _upsert(client, coll, pid, text, created_at, attrs=None, lifecycle=None):
    client.upsert(coll, [PointStruct(
        id=str(pid),
        vector=[0.1, 0.2, 0.3, 0.4],
        payload={
            "text": text,
            "created_at": created_at,
            "lifecycle_status": lifecycle or "canonical",
            **({"entity_attributes": attrs} if attrs else {}),
        },
    )])


@pytest.fixture()
def temp_coll():
    client = _mk_client()
    name = "test-dedup-sweep"
    client.create_collection(name, vectors_config=VectorParams(size=DIM, distance=Distance.COSINE))
    yield client, name
    client.delete_collection(name)


from qdrant_client.models import VectorParams  # noqa: E402  (nach fixture-Import ok)


def _auditor(client, coll, tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DEDUP_SWEEP", "1")
    return ha.HealthAuditor(_FakeStore(client, coll), coll, data_dir=str(tmp_path / "reports"))


class _FakeStore:
    def __init__(self, client, collection):
        self.client = client
        self.collection_name = collection
        self.collection = collection


def test_dedup_sweep_merges_exact_duplicates(temp_coll, tmp_path, monkeypatch):
    client, coll = temp_coll
    aud = _auditor(client, coll, tmp_path, monkeypatch)
    t1 = "Kiosha nutzt glm-5.3-flash als Main-Modell"
    t2 = "  kiosha   nutzt glm-5.3-flash als main-modell  "  # gleicher normalized key (Whitespace+Case)
    unique = "Bleki ist ein Puli und wohnt bei Nebo"
    client.upsert(coll, [PointStruct(id=str(uuid.uuid4()), vector=[0.1]*DIM, payload={"text": t1, "created_at": _iso(90)})])
    client.upsert(coll, [PointStruct(id=str(uuid.uuid4()), vector=[0.2]*DIM, payload={"text": t2, "created_at": _iso(10)})])
    client.upsert(coll, [PointStruct(id=str(uuid.uuid4()), vector=[0.2]*DIM, payload={"text": unique, "created_at": _iso(5)})])

    sweep = aud._dedup_sweep()

    assert sweep["merged"] == 1
    remaining = {str(p.id) for p in aud._collect_points()}
    # 3 upserts -> nach Merge 2 Punkte
    assert len(remaining) == 2
    # Der eindeutige Punkt überlebt
    texts = {p.payload["text"] for p in aud._collect_points()}
    assert unique in texts


def test_keeper_is_oldest(temp_coll, tmp_path, monkeypatch):
    client, coll = temp_coll
    aud = _auditor(client, coll, tmp_path, monkeypatch)
    text = "Deploy-Doku liegt unter /srv/paperless"
    old_id, new_id = str(uuid.uuid4()), str(uuid.uuid4())
    client.upsert(coll, [PointStruct(id=old_id, vector=[0.1]*DIM, payload={"text": text, "created_at": _iso(200)})])
    client.upsert(coll, [PointStruct(id=new_id, vector=[0.1]*DIM, payload={"text": text, "created_at": _iso(2)})])

    sweep = aud._dedup_sweep()
    assert sweep["merged"] == 1
    remaining = {str(p.id) for p in aud._collect_points()}
    assert old_id in remaining and new_id not in remaining


def test_attribute_rescue(temp_coll, tmp_path, monkeypatch):
    client, coll = temp_coll
    aud = _auditor(client, coll, tmp_path, monkeypatch)
    text = "Synology DSM Login per SSH kiosha"
    old_id = str(uuid.uuid4())
    new_id = str(uuid.uuid4())
    client.upsert(coll, [PointStruct(id=old_id, vector=[0.1]*DIM, payload={
        "text": text, "created_at": _iso(100), "entity_attributes": {"ip": "192.168.31.40"}})])
    client.upsert(coll, [PointStruct(id=new_id, vector=[0.1]*DIM, payload={
        "text": text, "created_at": _iso(1), "entity_attributes": {"ssh_user": "kiosha", "ip": "192.168.31.40"}})])

    sweep = aud._dedup_sweep()
    assert sweep["merged"] == 1
    assert sweep["rescued_attributes"] >= 1
    keeper = [p for p in aud._collect_points() if str(p.id) == old_id][0]
    attrs = keeper.payload.get("entity_attributes") or {}
    assert attrs.get("ssh_user") == "kiosha" and attrs.get("ip") == "192.168.31.40"


def test_backup_written_before_delete(temp_coll, tmp_path, monkeypatch):
    client, coll = temp_coll
    aud = _auditor(client, coll, tmp_path, monkeypatch)
    text = "Router-Gateway ist 192.168.31.1"
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    client.upsert(coll, [PointStruct(id=a, vector=[0.1]*DIM, payload={"text": text, "created_at": _iso(30)})])
    client.upsert(coll, [PointStruct(id=b, vector=[0.1]*DIM, payload={"text": text, "created_at": _iso(5)})])

    sweep = aud._dedup_sweep()
    assert sweep["merged"] == 1 and sweep["backup_file"]
    data = json.load(open(sweep["backup_file"]))
    deleted_ids = {row["id"] for row in data["deleted"]}
    assert deleted_ids == {b}
    assert data["keeper_strategy"] == "oldest_created_at"


def test_kill_switch(temp_coll, tmp_path, monkeypatch):
    client, coll = temp_coll
    # Achtung: _auditor erzwingt Sweep=1 — hier manuell instanziieren statt fixen
    monkeypatch.setenv("NEXUS_DEDUP_SWEEP", "0")
    aud = ha.HealthAuditor(_FakeStore(client, coll), coll, data_dir=str(tmp_path / "reports"))
    text = "Backup-Regel 3-2-1 gilt fuer den Mac Mini"
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    client.upsert(coll, [PointStruct(id=a, vector=[0.1]*DIM, payload={"text": text, "created_at": _iso(30)})])
    client.upsert(coll, [PointStruct(id=b, vector=[0.1]*DIM, payload={"text": text, "created_at": _iso(5)})])

    report = aud.run_audit()
    assert "dedup_sweep" not in report
    assert len({str(p.id) for p in aud._collect_points()}) == 2  # nichts passiert


def test_superseded_not_touched(temp_coll, tmp_path, monkeypatch):
    client, coll = temp_coll
    aud = _auditor(client, coll, tmp_path, monkeypatch)
    text = "Alter Workflow fuer Expense-Import"
    a = str(uuid.uuid4()); b = str(uuid.uuid4())
    client.upsert(coll, [PointStruct(id=a, vector=[0.1]*DIM, payload={"text": text, "created_at": _iso(30)})])
    client.upsert(coll, [PointStruct(id=b, vector=[0.1]*DIM, payload={"text": text, "created_at": _iso(5), "lifecycle_status": "superseded"})])

    sweep = aud._dedup_sweep()
    assert sweep["merged"] == 0  # superseded zählt nicht als Duplikat-Quelle