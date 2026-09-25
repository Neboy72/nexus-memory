"""Tests for dreaming + archive_forgetting (v0.22.0)."""
import json
import sqlite3
import time
import uuid
from pathlib import Path

import pytest

from nexus_memory import dreaming, archive_forgetting


class FakeStore:
    def __init__(self, similar_score=0.0):
        self.collection = "nexus"
        self._similar_score = similar_score
        self.deleted = []
        self.client = self  # qdrant-style: store.client exposes the API

    class _Hit:
        def __init__(self, score):
            self.score = score

    def search(self, collection_name, query_vector, limit, query_filter):
        if self._similar_score:
            return [self._Hit(self._similar_score)]
        return []

    def scroll(self, collection_name, scroll_filter, limit,
               with_payload, with_vector):
        stale_ts = time.time() - (archive_forgetting.MAX_AGE_DAYS + 5) * 86400
        points = []
        # Realistic ID mix: one UUID point, one numeric-ID point, one
        # NUMERIC-STRING point (review gap v0.22.1: int(pid) path untested),
        # and one FACT point that must never be deleted.
        specs = [("session", str(uuid.uuid4())), ("session", 42_000_001),
                 ("session", "42_000_003"), ("facts", 42_000_002)]
        for cat, pid in specs:
            if scroll_filter and not any(
                    m["match"]["value"] == cat
                    for m in scroll_filter.get("should",
                                               scroll_filter.get("must", []))):
                continue
            points.append(type("P", (), {"id": pid,
                                         "payload": {"category": cat,
                                                     "created_at": stale_ts},
                                         "vector": [0.1]})())
        return (points, None)

    def delete(self, collection_name, points_selector):
        # PointIdsList may carry several ids per call — record them all
        # (review fix v0.22.1: points[0] hid multi-point selectors).
        self.deleted.extend(points_selector.points)


class _NoQdrant(FakeStore):
    def search(self, *a, **k):
        raise RuntimeError("no qdrant in unit test")

    def scroll(self, *a, **k):
        raise RuntimeError("no qdrant in unit test")

    def delete(self, *a, **k):
        raise RuntimeError("no qdrant in unit test")


# ── dreaming ──────────────────────────────────────────────────────

def test_dream_disabled_by_env(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXUS_DREAMING", "0")
    out = dreaming.dream_once(dry_run=True)
    assert out["skipped"] == "disabled"


def test_dream_no_sources_is_silent(monkeypatch, tmp_path):
    monkeypatch.delenv("NEXUS_DREAMING_SOURCES", raising=False)
    monkeypatch.setattr(dreaming, "_session_sources", lambda: [])
    out = dreaming.dream_once(dry_run=True)
    assert out["sessions_scanned"] == 0


def test_dream_hermes_source_fresh_and_idempotent(monkeypatch, tmp_path):
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sessions (id TEXT, started_at REAL, "
                 "message_count INT, tool_call_count INT, ended_at REAL)")
    now = time.time()
    conn.execute("INSERT INTO sessions VALUES ('20260925_120000_abc123', ?, 12, 7, ?)",
                 (now - 3600, now - 1800))
    conn.commit()
    conn.close()
    monkeypatch.setenv("NEXUS_DREAMING_SOURCES",
                       json.dumps([{"type": "hermes", "db": str(db)}]))
    monkeypatch.setattr(dreaming, "PLAYBOOK_DIR", tmp_path / "pb")
    out = dreaming.dream_once(dry_run=True)
    assert out["sessions_scanned"] == 1
    # v0.22.1 review fix: dry_run must NOT commit the marker — the second
    # dry run still sees the session as fresh. (Old test burned the session
    # on the first dry run: known bug, now fixed.)
    assert out["new_sessions"] == 1
    out2 = dreaming.dream_once(dry_run=True)
    assert out2.get("new_sessions", 0) == 1


def test_dream_jsonl_source(monkeypatch, tmp_path):
    d = tmp_path / "sessions"
    d.mkdir()
    (d / "20260925_120000_abc123.jsonl").write_text(
        "irgendwo ein fix und ein gotcha im text", encoding="utf-8")
    import os
    os.utime(d / "20260925_120000_abc123.jsonl", (time.time(), time.time()))
    monkeypatch.setenv("NEXUS_DREAMING_SOURCES",
                       json.dumps([{"type": "jsonl", "dir": str(d)}]))
    monkeypatch.setattr(dreaming, "PLAYBOOK_DIR", tmp_path / "pb")
    out = dreaming.dream_once(dry_run=True)
    assert out["sessions_scanned"] == 1


def test_dream_no_delete_invariant(monkeypatch, tmp_path):
    # Dreaming must never delete: even with a fake store, no delete call.
    store = _NoQdrant()
    monkeypatch.setattr(dreaming, "_session_sources", lambda: [])
    out = dreaming.dream_once(store=store, dry_run=True)
    assert store.deleted == []
    assert "error" not in out or out["error"] is None


def test_hint_regex_finds_lehre():
    m = dreaming._HINT_RE.search("Das war der Fix: erst Replik, dann Push")
    assert m is not None


# ── archive_forgetting ────────────────────────────────────────────

def test_archive_disabled_by_env(monkeypatch):
    monkeypatch.setenv("NEXUS_ARCHIVE_ENABLED", "0")
    store = _NoQdrant()
    out = archive_forgetting.archive_once(store, "nexus", dry_run=True)
    assert out["skipped"] == "disabled"


def test_archive_dry_run_no_backup_no_delete(monkeypatch, tmp_path):
    monkeypatch.setattr(archive_forgetting, "BACKUP_DIR", tmp_path / "bk")
    store = FakeStore()
    out = archive_forgetting.archive_once(store, "nexus", dry_run=True)
    assert out["stale"] >= 1
    assert out["deleted"] == 0
    assert store.deleted == []
    assert not (tmp_path / "bk").exists()


def test_archive_backup_before_delete(monkeypatch, tmp_path):
    monkeypatch.setattr(archive_forgetting, "BACKUP_DIR", tmp_path / "bk")
    store = FakeStore()
    out = archive_forgetting.archive_once(store, "nexus", dry_run=False)
    assert out["deleted"] == out["stale"] == out["backed_up"]
    assert out["deleted"] >= 1
    backups = list((tmp_path / "bk").glob("session-points-*.jsonl"))
    assert len(backups) == 1
    lines = [json.loads(l) for l in backups[0].read_text(encoding="utf-8").splitlines()]
    assert all("id" in p and "vector" in p and "payload" in p for p in lines)
    assert len(store.deleted) == out["deleted"]


def test_archive_backup_failure_blocks_delete(monkeypatch, tmp_path):
    monkeypatch.setattr(archive_forgetting, "BACKUP_DIR", tmp_path / "bk")
    monkeypatch.setattr(archive_forgetting, "_backup_points",
                        lambda points, d: None)  # backup broken
    store = FakeStore()
    out = archive_forgetting.archive_once(store, "nexus", dry_run=False)
    assert out["deleted"] == 0
    assert store.deleted == []


def test_parse_ts_both_schemas():
    iso = "2026-08-01T10:00:00+00:00"
    assert archive_forgetting._parse_ts({"created_at": iso}) > 1_000_000_000
    assert archive_forgetting._parse_ts({"created": 1_700_000_000}) == 1_700_000_000
    assert archive_forgetting._parse_ts({}) == 0.0


def test_archive_never_touches_facts(monkeypatch, tmp_path):
    # Only session/temp categories are selected; no fact point is ever deleted.
    monkeypatch.setattr(archive_forgetting, "BACKUP_DIR", tmp_path / "bk")
    store = FakeStore()
    archive_forgetting.archive_once(store, "nexus", dry_run=False)
    assert all(d != 42_000_002 for d in store.deleted)  # fact-ID untouched
    assert len(store.deleted) == 3  # uuid + numeric + numeric-string sessions


def test_archive_mixed_id_types_deleted(monkeypatch, tmp_path):
    # UUID string and numeric int IDs both survive the delete path.
    monkeypatch.setattr(archive_forgetting, "BACKUP_DIR", tmp_path / "bk")
    store = FakeStore()
    out = archive_forgetting.archive_once(store, "nexus", dry_run=False)
    assert out["deleted"] == 3 and out["errors"] == 0
    kinds = {type(d).__name__ for d in store.deleted}
    assert "str" in kinds and "int" in kinds


# ── v0.22.1 review fixes: closing the gaps ────────────────────────

def test_archive_numeric_string_id_becomes_int(monkeypatch, tmp_path):
    # Review gap: "42000003" (numeric string) must be sent to Qdrant as int,
    # not as string — regression here makes the delete silently miss.
    monkeypatch.setattr(archive_forgetting, "BACKUP_DIR", tmp_path / "bk")
    store = FakeStore()
    archive_forgetting.archive_once(store, "nexus", dry_run=False)
    assert 42_000_003 in store.deleted
    assert isinstance([d for d in store.deleted if d == 42_000_003][0], int)


def test_archive_real_backup_check_blocks_delete(monkeypatch, tmp_path):
    # Review gap: the row-count completeness check must actually run — a
    # non-JSON-serializable payload must abort BEFORE any delete.
    monkeypatch.setattr(archive_forgetting, "BACKUP_DIR", tmp_path / "bk")
    store = FakeStore()
    out = archive_forgetting.archive_once(store, "nexus", dry_run=False)
    # Poison one candidate payload AFTER selection, via a wrapped backup:
    orig = archive_forgetting._backup_points

    def poison(points, d):
        broken = [dict(p) for p in points]
        if broken:
            broken[0] = dict(broken[0], payload={"x": object()})
        return orig(broken, d)

    monkeypatch.setattr(archive_forgetting, "_backup_points", poison)
    store2 = FakeStore()
    out2 = archive_forgetting.archive_once(store2, "nexus", dry_run=False)
    assert out2["deleted"] == 0 and store2.deleted == []
    assert out2["backed_up"] == 0


def test_dream_writes_playbook_and_commits_marker(monkeypatch, tmp_path):
    # Review gap: the real product (dream-*.json + marker) was never
    # exercised — every old test ran dry_run=True.
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sessions (id TEXT, started_at REAL, "
                 "message_count INT, tool_call_count INT, ended_at REAL)")
    now = time.time()
    conn.execute("INSERT INTO sessions VALUES ('20260925_150000_fix99', ?, 9, 6, ?)",
                 (now - 7200, now - 3600))
    conn.execute("CREATE TABLE messages (session_id TEXT, content TEXT)")
    conn.execute("INSERT INTO messages VALUES ('20260925_150000_fix99', 'Beim nightly deploy gab es wieder denselben timeout im health-check, fix war ein retry und ein gotcha im cron.')")
    conn.commit()
    conn.close()
    monkeypatch.setenv("NEXUS_DREAMING_SOURCES",
                       json.dumps([{"type": "hermes", "db": str(db)}]))
    monkeypatch.setattr(dreaming, "PLAYBOOK_DIR", tmp_path / "pb")

    def fake_llm(prompt):
        return "ja: Nachts immer derselbe Fehler, dann Neustart. Aktion: wachen."

    out = dreaming.dream_once(store=None, llm_fn=fake_llm, dry_run=False)
    # The snippet regex yields two candidates from the message text.
    assert out["patterns"] == 2 and out["playbooks"] == 1
    files = list((tmp_path / "pb").glob("dream-*.json"))
    assert len(files) == 1
    body = json.loads(files[0].read_text(encoding="utf-8"))
    assert body[0]["verdict"].startswith("ja:")
    assert body[0]["session_id"] == "20260925_150000_fix99"
    # Marker committed after success → second run is silent.
    marker = (tmp_path / "pb" / ".dreaming-last-run").read_text(encoding="utf-8")
    assert "20260925_150000_fix99" in marker
    out2 = dreaming.dream_once(store=None, llm_fn=fake_llm, dry_run=False)
    assert out2.get("new_sessions", 0) == 0


def test_dream_llm_failure_keeps_sessions_learnable(monkeypatch, tmp_path):
    # Review fix v0.22.1: LLM down mid-pass → abort WITHOUT marker commit;
    # sessions must stay learnable for the next pass.
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sessions (id TEXT, started_at REAL, "
                 "message_count INT, tool_call_count INT, ended_at REAL)")
    now = time.time()
    conn.execute("INSERT INTO sessions VALUES ('20260925_160000_blast', ?, 5, 6, ?)",
                 (now - 7200, now - 3600))
    conn.execute("CREATE TABLE messages (session_id TEXT, content TEXT)")
    conn.execute("INSERT INTO messages VALUES ('20260925_160000_blast', 'Beim nightly deploy gab es wieder denselben timeout im health-check, fix war ein retry und ein gotcha im cron.')")
    conn.commit()
    conn.close()
    monkeypatch.setenv("NEXUS_DREAMING_SOURCES",
                       json.dumps([{"type": "hermes", "db": str(db)}]))
    monkeypatch.setattr(dreaming, "PLAYBOOK_DIR", tmp_path / "pb")

    def boom(prompt):
        raise RuntimeError("fuel station closed")

    out = dreaming.dream_once(store=None, llm_fn=boom, dry_run=False)
    assert "error" in out and out["patterns"] == 0
    assert not list((tmp_path / "pb").glob("dream-*.json"))
    marker = tmp_path / "pb" / ".dreaming-last-run"
    assert not marker.exists()
    # Next pass (LLM back) must see the session as fresh again.
    out2 = dreaming.dream_once(store=None, llm_fn=lambda p: "ja: Muster. Aktion: wachen.",
                               dry_run=False)
    assert out2.get("new_sessions", 0) == 1 and out2["patterns"] == 2


def test_dream_llm_verdict_nein_blocks_pattern(monkeypatch, tmp_path):
    # Review gap: the ja/nein verdict gate had zero coverage — a typo in the
    # string check would let everything through unnoticed.
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sessions (id TEXT, started_at REAL, "
                 "message_count INT, tool_call_count INT, ended_at REAL)")
    now = time.time()
    conn.execute("INSERT INTO sessions VALUES ('20260925_170000_nein', ?, 3, 6, ?)",
                 (now - 7200, now - 3600))
    conn.execute("CREATE TABLE messages (session_id TEXT, content TEXT)")
    conn.execute("INSERT INTO messages VALUES ('20260925_170000_nein', 'Beim nightly deploy gab es wieder denselben timeout im health-check, fix war ein retry und ein gotcha im cron.')")
    conn.commit()
    conn.close()
    monkeypatch.setenv("NEXUS_DREAMING_SOURCES",
                       json.dumps([{"type": "hermes", "db": str(db)}]))
    monkeypatch.setattr(dreaming, "PLAYBOOK_DIR", tmp_path / "pb")
    out = dreaming.dream_once(
        store=None, llm_fn=lambda p: "nein, kein Muster erkennbar", dry_run=False)
    assert out["patterns"] == 0
    assert not list((tmp_path / "pb").glob("dream-*.json"))


def test_archive_never_touches_facts_with_real_fact_present(monkeypatch, tmp_path):
    # Review gap: old test had NO fact point in the store (tautology).
    # FakeStore now carries one; the category filter must protect it.
    monkeypatch.setattr(archive_forgetting, "BACKUP_DIR", tmp_path / "bk")
    store = FakeStore()
    archive_forgetting.archive_once(store, "nexus", dry_run=False)
    assert all(d != 42_000_002 for d in store.deleted)
    assert 42_000_003 in store.deleted  # numeric-string session WAS deleted