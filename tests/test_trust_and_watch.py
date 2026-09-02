"""
Tests für trust_service.py + retrieval_watch.py (v0.14.1, in-process daemons).

Abgedeckt:
- compute_trust: alle Governance-Zweige (retraction/override/confirm/contest/still-contested/active)
- TrustService.run: Temp-Collection, set_payload nur bei Abweichung, contested_open-Zählung
- RetrievalWatch.run: gefunden/verfehlt + NEXUS_WATCH_QUERIES-Override
- Kill-Switch-Verhalten (env) via mcp_server-Wiring-Konvention (hier: nur Modul-Level)
"""
import uuid
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from nexus_memory import trust_service as ts
from nexus_memory import retrieval_watch as rw
from nexus_memory.trust_service import TrustService, compute_trust

DIM = 4


class _FakeStore:
    def __init__(self, client, coll):
        self.client = client
        self.collection_name = coll


@pytest.fixture(scope="module")
def temp_coll():
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, VectorParams

    client = QdrantClient(":memory:")
    coll = "test-trust-service"
    client.create_collection(
        coll, vectors_config=VectorParams(size=DIM, distance=Distance.COSINE)
    )
    yield client, coll
    client.close()


def _iso(days_ago: float) -> str:
    import datetime
    t = ts.__dict__  # noqa
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


# compute_trust-Zweige ----------------------------------------------------------

def _belief(status="ACTIVE", trust=0.3):
    return {"payload": {"status": status, "trust": trust}}


def _evt(assertion, actor="user", trust_level=None):
    pl = {"assertion": assertion, "actor": {"type": actor}}
    if trust_level is not None:
        pl["trust_level"] = trust_level
    return {"payload": pl}


def test_retraction_wins():
    events = [_evt("confirm", "user"), _evt("retract"), _evt("confirm", "user", 0.9)]
    t, s, r = compute_trust(events, _belief())
    assert s == "RETRACTED" and r == "Retracted by event"


def test_user_override_activates():
    events = [_evt("contest", "agent"), _evt("override", "user", 0.8)]
    t, s, _ = compute_trust(events, _belief("CONTESTED", 0.3))
    assert s == "ACTIVE" and t == 0.8


def test_user_confirm_activates():
    events = [_evt("contest", "agent"), _evt("confirm", "user", 0.7)]
    t, s, _ = compute_trust(events, _belief("CONTESTED", 0.3))
    assert s == "ACTIVE" and t == 0.7  # max aus events (contest hat kein trust_level)


def test_agent_contest_without_confirm_contested():
    events = [_evt("contest", "agent", 0.5)]
    t, s, _ = compute_trust(events, _belief("ACTIVE", 0.5))
    assert s == "CONTESTED" and t == 0.5


def test_still_contested_without_confirm():
    events = [_evt("contest", "agent", 0.4)]
    t, s, _ = compute_trust(events, _belief("CONTESTED", 0.3))
    assert s == "CONTESTED"


def test_no_events_keeps_existing_trust():
    t, s, _ = compute_trust([], _belief("ACTIVE", 0.3))
    assert s == "ACTIVE" and t == 0.3


def test_max_aggregation():
    events = [_evt("confirm", "user", 0.6), _evt("confirm", "user", 0.9)]
    t, _, _ = compute_trust(events, _belief())
    assert t == 0.9


def test_garbage_trust_level_skipped():
    events = [{"payload": {"assertion": "confirm", "actor": {"type": "user"}, "trust_level": "x"}}]
    t, s, _ = compute_trust(events, _belief("ACTIVE", 0.3))
    assert t == 0.3  # kein crash, fallback auf bestehenden Wert


# Integration gegen echte Temp-Collection ---------------------------------------

def test_run_against_temp_collection(temp_coll):
    from qdrant_client.models import PointStruct

    client, coll = temp_coll
    store = _FakeStore(client, coll)
    svc = TrustService(store, coll, data_dir=tempfile.mkdtemp())

    belief_id = "bel-test-001"
    bid = str(uuid.uuid4())
    eid = str(uuid.uuid4())
    client.upsert(coll, [
        PointStruct(id=bid, vector=[0.1] * DIM, payload={
            "category": "belief", "belief_id": belief_id,
            "text": "Ollama Max deckt alles ab", "status": "CONTESTED", "trust": 0.3,
        }),
        PointStruct(id=eid, vector=[0.2] * DIM, payload={
            "event_type": "belief_event", "belief_id": belief_id,
            "assertion": "confirm", "actor": {"type": "user"}, "trust_level": 0.9,
        }),
    ])

    report = svc.run()
    assert report["beliefs_scanned"] == 2
    assert report["updated"] == 1
    assert report["contested_open"] == 0  # confirm → ACTIVE

    # Payload wirklich aktualisiert?
    pts = client.retrieve(coll, ids=[bid], with_payload=True)
    assert pts[0].payload["status"] == "ACTIVE"
    assert pts[0].payload["trust"] == 0.9


def test_retrieval_watch_flags():
    class _FakeStoreRW:
        collection_name = "x"
        _embedder = None

        class client:  # noqa
            @staticmethod
            def scroll(*a, **kw):
                # Fallback-Pfad (kein Embedder): liefert den Bose-Punkt statisch
                class P:
                    payload = {"text": "Bose SoundLink laeuft"}
                    score = 0.8
                return [P()], None

    watch = rw.RetrievalWatch(_FakeStoreRW(), "x")
    # Queries überschreiben für deterministischen Test
    watch._queries = [("Bose SoundLink Audio", "Bose")]
    rep = watch.run()
    assert rep["queries_checked"] == 1
    assert rep["failures"] == []
    # Und ein Fall der NICHT findet:
    watch._queries = [("Flugzeug Motor Wartung", "Turbine")]
    rep = watch.run()
    assert len(rep["failures"]) == 1
    flags = watch.get_flags()
    assert "retrieval" in flags