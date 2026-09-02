"""
Tests für selective_forgetting.py — in-process Selective-Forgetting-Auditor.

Deckt ab:
- parse_ts: unix (s/ms), ISO mit Z, Datum-only, Garbage -> None
- get_ts: Feld-Priorität created_at > updated_at > modified > timestamp > created
- score_point: Schutz-Zonen (canonical/ACTIVE, paperless, rule/procedure),
  no_timestamp -> None, junger Punkt -> 0, alter session-Punkt -> Kandidat,
  Category-Dämpfung (session > fact)
- SelectiveForgettingAuditor.run(): Integration gegen echte TEMP-Collection
  (Production-Collection 'nexus' wird NIE berührt)
"""
import importlib
import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

sf = importlib.import_module("nexus_memory.selective_forgetting")

NOW = time.time()
OLD_ISO = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
FRESH_ISO = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()


# ---------- parse_ts ----------

class TestParseTs:
    def test_unix_seconds(self):
        assert sf.parse_ts(1_700_000_000) == 1_700_000_000

    def test_unix_milliseconds(self):
        assert sf.parse_ts(1_700_000_000_000) == 1_700_000_000

    def test_iso_with_z(self):
        assert sf.parse_ts("2026-01-01T00:00:00Z") is not None

    def test_date_only(self):
        assert sf.parse_ts("2021-01-15") is not None

    def test_garbage_returns_none(self):
        assert sf.parse_ts(None) is None
        assert sf.parse_ts("") is None
        assert sf.parse_ts("not-a-date") is None
        assert sf.parse_ts("None") is None


# ---------- get_ts ----------

class TestGetTs:
    def test_prefers_created_at(self):
        pl = {"created_at": "2026-01-01T00:00:00Z", "modified": "2026-03-01T00:00:00Z"}
        assert sf.get_ts(pl) == sf.parse_ts("2026-01-01T00:00:00Z")

    def test_falls_back_to_modified(self):
        pl = {"modified": "2026-03-01T00:00:00Z"}
        assert sf.get_ts(pl) == sf.parse_ts("2026-03-01T00:00:00Z")

    def test_none_when_empty(self):
        assert sf.get_ts({}) is None
        assert sf.get_ts({"created_at": None}) is None


# ---------- score_point / Schutz-Zonen ----------

class TestScorePoint:
    def setup_method(self):
        self.aud = sf.SelectiveForgettingAuditor(store=None, collection="x", data_dir="/tmp/nexus-sf-test")

    def test_canonical_protected(self):
        assert self.aud.score_point({"lifecycle_status": "canonical", "created_at": OLD_ISO}, NOW) is None

    def test_active_protected(self):
        assert self.aud.score_point({"status": "ACTIVE", "created_at": OLD_ISO}, NOW) is None

    def test_superseded_protected(self):
        assert self.aud.score_point({"lifecycle_status": "superseded", "created_at": OLD_ISO}, NOW) is None

    def test_paperless_protected(self):
        assert self.aud.score_point({"source": "paperless", "created_at": OLD_ISO}, NOW) is None

    def test_rule_protected(self):
        assert self.aud.score_point({"category": "rule", "created_at": OLD_ISO}, NOW) is None

    def test_no_timestamp_not_scored(self):
        assert self.aud.score_point({"category": "session"}, NOW) is None

    def test_fresh_session_below_threshold(self):
        s = self.aud.score_point({"category": "session", "created_at": FRESH_ISO}, NOW)
        assert s is not None and s < sf.CANDIDATE_THRESHOLD  # kein Kandidat, Punkt bleibt

    def test_old_session_is_candidate(self):
        s = self.aud.score_point({"category": "session", "created_at": OLD_ISO}, NOW)
        assert s is not None and s >= sf.CANDIDATE_THRESHOLD

    def test_category_damping(self):
        old_session = self.aud.score_point({"category": "session", "created_at": OLD_ISO}, NOW)
        old_fact = self.aud.score_point({"category": "fact", "created_at": OLD_ISO}, NOW)
        assert old_session > old_fact  # session darf eher weg als fact


# ---------- Integration: run() gegen TEMP-Collection ----------

class TestRunIntegration:
    def test_run_against_temp_collection(self, tmp_path):
        """Kompletter run() mit echtem Qdrant-Client gegen TEMP-Collection."""
        from qdrant_client import QdrantClient, models

        client = QdrantClient(host="localhost", port=6333)
        coll = "nexus-sf-test-tmp"
        if client.collection_exists(coll):
            client.delete_collection(coll)
        client.create_collection(coll, vectors_config=models.VectorParams(size=4, distance=models.Distance.COSINE))

        class FakeStore:  # minimales Store-Interface wie im MCP-Server
            def __init__(self, c, name):
                self.client = c
                self.collection_name = name

        pts = [
            models.PointStruct(id="11111111-1111-1111-1111-111111111111",
                               vector=[0.1] * 4,
                               payload={"text": "alte session notiz", "category": "session", "created_at": OLD_ISO}),
            models.PointStruct(id="22222222-2222-2222-2222-222222222222",
                               vector=[0.5] * 4,
                               payload={"text": "canonical fact", "category": "fact",
                                        "lifecycle_status": "canonical", "created_at": OLD_ISO}),
            models.PointStruct(id="33333333-3333-3333-3333-333333333333",
                               vector=[0.2] * 4,
                               payload={"text": "frische notiz", "category": "session", "created_at": FRESH_ISO}),
            models.PointStruct(id="44444444-4444-4444-4444-444444444444",
                               vector=[0.3] * 4,
                               payload={"text": "dokument", "category": "fact",
                                        "source": "paperless", "created_at": OLD_ISO}),
            models.PointStruct(id="55555555-5555-5555-5555-555555555555",
                               vector=[0.9] * 4,
                               payload={"text": "kein ts", "category": "session"}),
        ]
        client.upsert(coll, points=pts)
        try:
            aud = sf.SelectiveForgettingAuditor(FakeStore(client, coll), coll, data_dir=str(tmp_path))
            report = aud.run()
        finally:
            client.delete_collection(coll)

        assert report["total_points"] == len(pts)
        cand_ids = [c["id"] for c in report["candidates_score_ge_060"]]
        assert cand_ids == ["11111111-1111-1111-1111-111111111111"]
        assert report["protected"].get("protected_lifecycle", 0) >= 1
        assert report["protected"].get("protected_source", 0) >= 1
        assert report["protected"].get("no_timestamp", 0) >= 1
        assert report["scored"] == 2  # alt+frisch session
        # Report auf Platte
        assert os.path.exists(report["report_file"])
        reloaded = json.loads(open(report["report_file"]).read())
        assert reloaded["candidates_score_ge_060"] == report["candidates_score_ge_060"]

    def test_run_never_deletes(self, tmp_path):
        """Härte-Garantie: nach run() existieren alle Punkte noch (READ-ONLY)."""
        from qdrant_client import QdrantClient, models

        client = QdrantClient(host="localhost", port=6333)
        coll = "nexus-sf-test-tmp2"
        if client.collection_exists(coll):
            client.delete_collection(coll)
        client.create_collection(coll, vectors_config=models.VectorParams(size=4, distance=models.Distance.COSINE))

        class FakeStore:
            def __init__(self, c, name):
                self.client = c
                self.collection_name = name

        pts = [
            models.PointStruct(id=f"{i:08d}-0000-0000-0000-000000000000",
                               vector=[0.1] * 4,
                               payload={"text": f"alte notiz {i}", "category": "session", "created_at": OLD_ISO})
            for i in range(5)
        ]
        client.upsert(coll, points=pts)

        aud = sf.SelectiveForgettingAuditor(FakeStore(client, coll), coll, data_dir=str(tmp_path))
        aud.run()
        after = client.count(coll, exact=True).count
        assert after == 5, "READ-ONLY verletzt: Punkte wurden gelöscht!"
        client.delete_collection(coll)