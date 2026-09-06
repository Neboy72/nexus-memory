"""
Invariant tests for the hardened health-audit dedup sweep.

These tests pin the security-relevant invariants after the review fixes:

- Lossless deletion proof: deletion decisions compare the FULL content
  (SHA-256 of the complete normalized text). The 300-char truncated key is
  only used to FIND candidates, never to prove duplication.
- Complete + atomic backup: ALL deletion candidates are written to a single
  JSON backup (payload + vector + original id type) BEFORE the first
  deletion; a failed backup aborts the sweep without touching any point.
- Security-context grouping: category + access level + owner/agent attrs are
  part of the grouping key; rules and guardrail audit entries are excluded
  from dedup; metadata merges only inside one security context.
- Opt-in only: the destructive sweep never runs by default
  (NEXUS_DEDUP_SWEEP unset => read-only), and webhook/flag messages report
  the actual behavior (deleted vs. not deleted).
- The explicitly passed collection is the collection that is audited.
"""
import json
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from nexus_memory import health_audit as ha

DIM = 4


def _mk_client():
    import tempfile
    return QdrantClient(path=tempfile.mkdtemp())


class _FakeStore:
    def __init__(self, client, collection):
        self.client = client
        self.collection_name = collection
        self.collection = collection


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc).replace(microsecond=0)
            - timedelta(days=days_ago)).isoformat()


def _upsert(client, coll, pid, text, created_at, **extra):
    payload = {"text": text, "created_at": created_at, "lifecycle_status": "canonical"}
    payload.update(extra)
    client.upsert(coll, [PointStruct(id=str(pid), vector=[0.1] * DIM, payload=payload)])


def _upsert_raw(client, coll, pid, payload):
    client.upsert(coll, [PointStruct(id=pid, vector=[0.1] * DIM, payload=payload)])


@pytest.fixture()
def temp_coll():
    client = _mk_client()
    name = "test-health-audit-invariants"
    client.create_collection(name, vectors_config=VectorParams(size=DIM, distance=Distance.COSINE))
    yield client, name
    client.delete_collection(name)


def _auditor(client, coll, tmp_path, monkeypatch, sweep="1"):
    if sweep is None:
        monkeypatch.delenv("NEXUS_DEDUP_SWEEP", raising=False)
    else:
        monkeypatch.setenv("NEXUS_DEDUP_SWEEP", sweep)
    # keep the registry cleanup off the real agent registry (mock, no network/fs side effects)
    monkeypatch.setattr("nexus_memory.agent_detect.cleanup_removed_agents",
                        lambda: {"removed": 0})
    return ha.HealthAuditor(_FakeStore(client, coll), coll, data_dir=str(tmp_path / "reports"))


class _FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ── Finding 1: lossless deletion proof ─────────────────────────────────


def test_distinct_same_prefix_not_merged(temp_coll, tmp_path, monkeypatch):
    """Same first 300 normalized chars, different full content => never merged."""
    client, coll = temp_coll
    aud = _auditor(client, coll, tmp_path, monkeypatch)
    a = "MARKER " + "A" * 300 + " tail-a-unique-2026"
    b = "MARKER " + "A" * 300 + " tail-b-unique-2026"
    ida, idb = str(uuid.uuid4()), str(uuid.uuid4())
    _upsert(client, coll, ida, a, _iso(10))
    _upsert(client, coll, idb, b, _iso(5))

    sweep = aud._dedup_sweep()

    assert sweep["merged"] == 0
    assert sweep["backup_file"] is None
    remaining = {str(p.id) for p in aud._collect_points()}
    assert remaining == {ida, idb}


def test_case_only_diff_beyond_truncation_not_merged(temp_coll, tmp_path, monkeypatch):
    """Texts identical within the 300-char window but differing later stay distinct.

    Case/whitespace differences are intentionally folded by normalization
    (same as the legacy behavior); the invariant is that any difference that
    SURVIVES normalization — here different content beyond the truncated
    window — keeps both points alive, even though their truncated candidate
    keys are equal (the lossy key must not decide deletion).
    """
    client, coll = temp_coll
    aud = _auditor(client, coll, tmp_path, monkeypatch)
    prefix = "DEPLOY RUNBOOK " + "x" * 280
    a = prefix + " first runbook variant for the alpha deploy"
    b = prefix + " second runbook variant for the beta deploy"
    ida, idb = str(uuid.uuid4()), str(uuid.uuid4())
    _upsert(client, coll, ida, a, _iso(9))
    _upsert(client, coll, idb, b, _iso(4))

    sweep = aud._dedup_sweep()

    assert sweep["merged"] == 0
    remaining = {str(p.id) for p in aud._collect_points()}
    assert remaining == {ida, idb}


def test_full_content_duplicates_merge_despite_long_text(temp_coll, tmp_path, monkeypatch):
    """Real duplicates well beyond 300 chars still merge (full-content hash works)."""
    client, coll = temp_coll
    aud = _auditor(client, coll, tmp_path, monkeypatch)
    content = "Kiosha deployt den Nexus Server auf dem Mac Mini. " * 14  # ~660 chars
    old_id, new_id = str(uuid.uuid4()), str(uuid.uuid4())
    _upsert(client, coll, old_id, content, _iso(90))
    _upsert(client, coll, new_id, "  " + content.replace(" ", "   ") + "  ", _iso(2))

    sweep = aud._dedup_sweep()

    assert sweep["merged"] == 1
    remaining = {str(p.id) for p in aud._collect_points()}
    assert remaining == {old_id}


# ── Finding 2: complete + atomic backup before first deletion ──────────


def test_backup_contains_all_groups_vectors_and_id_types(temp_coll, tmp_path, monkeypatch):
    """Multiple duplicate groups: one backup holds EVERY deleted row, incl. vector + id type."""
    client, coll = temp_coll
    aud = _auditor(client, coll, tmp_path, monkeypatch)
    t1 = "Group one shared content for backup completeness"
    t2 = "Group two shared content for backup completeness"
    t3 = "Group three shared content for backup completeness"
    a1, a2, a3 = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    b1, b2 = 7001, str(uuid.uuid4())  # mixed original id types (int id is the deletion candidate)
    c1, c2 = str(uuid.uuid4()), str(uuid.uuid4())
    _upsert(client, coll, a1, t1, _iso(30))
    _upsert(client, coll, a2, t1, _iso(20))
    _upsert(client, coll, a3, t1, _iso(10))
    _upsert_raw(client, coll, b2, {"text": t2, "created_at": _iso(30)})
    _upsert_raw(client, coll, b1, {"text": t2, "created_at": _iso(5)})
    _upsert(client, coll, c1, t3, _iso(30))
    _upsert(client, coll, c2, t3, _iso(5))

    sweep = aud._dedup_sweep()

    assert sweep["merged"] == 4
    assert sweep["backup_file"]
    data = json.load(open(sweep["backup_file"]))
    rows = data["deleted"]
    assert len(rows) == 4
    by_id = {row["id"]: row for row in rows}
    # original id types preserved
    assert str(a2) in by_id and by_id[str(a2)]["id_type"] == "str"
    assert by_id[7001]["id_type"] == "int"
    # every row has full payload AND the (stored, normalized) vector
    for row in rows:
        assert row["payload"].get("text") in (t1, t2, t3)
        assert row["payload"].get("created_at")
        vec = row["vector"]
        assert isinstance(vec, list) and len(vec) == DIM
        assert all(isinstance(v, float) and v == v and abs(v) != float("inf") for v in vec)
        assert row["collection"] == coll
        assert row["keeper_id"]
    assert data["collection"] == coll
    assert data["keeper_strategy"] == "oldest_created_at"
    # atomic write: no temp file leftovers
    leftovers = [p.name for p in (tmp_path / "reports").glob("*.tmp")]
    assert leftovers == []


def test_no_deletion_when_backup_write_fails(temp_coll, tmp_path, monkeypatch):
    """If the atomic backup cannot be created, nothing is deleted at all."""
    client, coll = temp_coll
    aud = _auditor(client, coll, tmp_path, monkeypatch)
    t = "Backup failure must abort the dedup sweep completely"
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    _upsert(client, coll, a, t, _iso(30))
    _upsert(client, coll, b, t, _iso(5))

    def _boom(src, dst):
        raise OSError("simulated atomic-write failure")
    monkeypatch.setattr("os.replace", _boom)

    import pytest as _pytest
    with _pytest.raises(OSError):
        aud._dedup_sweep()

    # nothing was deleted, no backup file exists at the target path
    assert len({str(p.id) for p in aud._collect_points()}) == 2
    assert not list((tmp_path / "reports").glob("dedup-sweep-backup-*.json"))


def test_delete_failure_leaves_backup_complete(temp_coll, tmp_path, monkeypatch):
    """If the first deletion fails, all candidates are already safely backed up."""
    client, coll = temp_coll
    aud = _auditor(client, coll, tmp_path, monkeypatch)
    t = "Delete failure must still have produced the full backup"
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    _upsert(client, coll, a, t, _iso(30))
    _upsert(client, coll, b, t, _iso(5))

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated qdrant delete failure")
    monkeypatch.setattr(client, "delete", _boom)

    import pytest as _pytest
    with _pytest.raises(RuntimeError):
        aud._dedup_sweep()

    backups = list((tmp_path / "reports").glob("dedup-sweep-backup-*.json"))
    assert len(backups) == 1
    data = json.load(open(backups[0]))
    assert [row["id"] for row in data["deleted"]] == [b]
    stored_vec = data["deleted"][0]["vector"]
    assert isinstance(stored_vec, list) and len(stored_vec) == DIM
    # both points are still in the collection
    assert len({str(p.id) for p in aud._collect_points()}) == 2


# ── Finding 3: security-context grouping + protected exclusions ────────


def test_rule_not_deleted_in_favor_of_identical_fact(temp_coll, tmp_path, monkeypatch):
    client, coll = temp_coll
    aud = _auditor(client, coll, tmp_path, monkeypatch)
    t = "Never deploy on Friday afternoon without explicit rollback plan"
    rule_id, fact_id = str(uuid.uuid4()), str(uuid.uuid4())
    _upsert_raw(client, coll, rule_id,
                {"text": t, "created_at": _iso(30), "category": "rule", "access_level": "public"})
    _upsert_raw(client, coll, fact_id,
                {"text": t, "created_at": _iso(5), "category": "fact", "access_level": "public"})

    sweep = aud._dedup_sweep()

    assert sweep["merged"] == 0
    assert sweep["skipped_protected"] == 1  # the rule
    remaining = {str(p.id) for p in aud._collect_points()}
    assert remaining == {rule_id, fact_id}


def test_private_and_public_same_text_not_merged(temp_coll, tmp_path, monkeypatch):
    client, coll = temp_coll
    aud = _auditor(client, coll, tmp_path, monkeypatch)
    t = "SSH login for the Synology backup user uses port 2222"
    pub_id, priv_id = str(uuid.uuid4()), str(uuid.uuid4())
    _upsert_raw(client, coll, pub_id,
                {"text": t, "created_at": _iso(30), "category": "fact", "access_level": "public"})
    _upsert_raw(client, coll, priv_id,
                {"text": t, "created_at": _iso(5), "category": "fact",
                 "access_level": "private", "entity_attributes": {"email": "k@nebo.dev"}})

    sweep = aud._dedup_sweep()

    assert sweep["merged"] == 0
    remaining = {str(p.id) for p in aud._collect_points()}
    assert remaining == {pub_id, priv_id}
    for p in aud._collect_points():
        if str(p.id) == priv_id:
            assert (p.payload.get("entity_attributes") or {}).get("email") == "k@nebo.dev"


def test_owner_context_prevents_cross_agent_merge(temp_coll, tmp_path, monkeypatch):
    client, coll = temp_coll
    aud = _auditor(client, coll, tmp_path, monkeypatch)
    t = "Shared infrastructure note about the reverse proxy setup"
    a_id, b_id = str(uuid.uuid4()), str(uuid.uuid4())
    _upsert_raw(client, coll, a_id,
                {"text": t, "created_at": _iso(30), "category": "fact",
                 "access_level": "public", "agent_id": "agent-a"})
    _upsert_raw(client, coll, b_id,
                {"text": t, "created_at": _iso(5), "category": "fact",
                 "access_level": "public", "agent_id": "agent-b"})

    sweep = aud._dedup_sweep()

    assert sweep["merged"] == 0
    remaining = {str(p.id) for p in aud._collect_points()}
    assert remaining == {a_id, b_id}


def test_guardrail_audit_entries_excluded_from_dedup(temp_coll, tmp_path, monkeypatch):
    """Identical guardrail-override audit entries are never deleted, normal dups are."""
    client, coll = temp_coll
    aud = _auditor(client, coll, tmp_path, monkeypatch)
    audit_text = "GUARDRAIL OVERRIDE by kiosha at 2026-09-01\nCommand: rm -rf /tmp/x\n"
    audit1, audit2 = str(uuid.uuid4()), str(uuid.uuid4())
    _upsert_raw(client, coll, audit1,
                {"content": audit_text, "created_at": _iso(9), "category": "session",
                 "access_level": "private", "guardrail_override": True, "agent_id": "kiosha"})
    _upsert_raw(client, coll, audit2,
                {"content": audit_text, "created_at": _iso(1), "category": "session",
                 "access_level": "private", "guardrail_override": True, "agent_id": "kiosha"})
    dup_id, dup2 = str(uuid.uuid4()), str(uuid.uuid4())
    _upsert_raw(client, coll, dup_id,
                {"text": "Plain duplicate memory that may be merged", "created_at": _iso(30),
                 "category": "fact", "access_level": "public"})
    _upsert_raw(client, coll, dup2,
                {"text": "plain duplicate memory that may be merged", "created_at": _iso(2),
                 "category": "fact", "access_level": "public"})

    sweep = aud._dedup_sweep()

    # only the plain duplicate pair is merged; both audit entries survive
    assert sweep["merged"] == 1
    assert sweep["skipped_protected"] == 2
    remaining = {str(p.id) for p in aud._collect_points()}
    assert remaining == {audit1, audit2, dup_id}


def test_metadata_rescue_only_within_same_security_context(temp_coll, tmp_path, monkeypatch):
    """Attribute rescue works inside one context; public keeper never adopts private attrs."""
    client, coll = temp_coll
    aud = _auditor(client, coll, tmp_path, monkeypatch)
    t_private = "Router admin panel credentials are documented in the vault"
    keeper_priv, cand_priv = str(uuid.uuid4()), str(uuid.uuid4())
    _upsert_raw(client, coll, keeper_priv,
                {"text": t_private, "created_at": _iso(50), "category": "fact",
                 "access_level": "private", "entity_attributes": {"ip": "192.168.31.1"}})
    _upsert_raw(client, coll, cand_priv,
                {"text": t_private, "created_at": _iso(3), "category": "fact",
                 "access_level": "private", "entity_attributes": {"user": "admin", "ip": "192.168.31.1"}})

    sweep = aud._dedup_sweep()

    # same context (both private facts): merge + attribute rescue allowed
    assert sweep["merged"] == 1
    assert sweep["rescued_attributes"] == 1
    keeper = [p for p in aud._collect_points() if str(p.id) == keeper_priv][0]
    attrs = keeper.payload.get("entity_attributes") or {}
    assert attrs == {"ip": "192.168.31.1", "user": "admin"}

    # different context (public keeper vs private candidate): no merge at all
    t_public = "Backup script runs nightly at 03:00 on the file server"
    keeper_pub, cand_pub = str(uuid.uuid4()), str(uuid.uuid4())
    _upsert_raw(client, coll, keeper_pub,
                {"text": t_public, "created_at": _iso(50), "category": "fact",
                 "access_level": "public"})
    _upsert_raw(client, coll, cand_pub,
                {"text": t_public, "created_at": _iso(3), "category": "fact",
                 "access_level": "private", "entity_attributes": {"secret_path": "/srv/keys"}})

    sweep2 = aud._dedup_sweep()
    assert sweep2["merged"] == 0
    assert sweep2["rescued_attributes"] == 0
    remaining = {str(p.id) for p in aud._collect_points()}
    assert remaining == {keeper_priv, keeper_pub, cand_pub}
    # the public keeper never adopted the private attribute
    pub = [p for p in aud._collect_points() if str(p.id) == keeper_pub][0]
    assert not (pub.payload.get("entity_attributes") or {})
    # the private candidate keeps its own attribute untouched
    priv = [p for p in aud._collect_points() if str(p.id) == cand_pub][0]
    assert (priv.payload.get("entity_attributes") or {}).get("secret_path") == "/srv/keys"


# ── Finding 4: opt-in only + truthful reporting ────────────────────────


def test_default_no_destructive_sweep(temp_coll, tmp_path, monkeypatch):
    """Default: NEXUS_DEDUP_SWEEP unset => read-only, no dedup_sweep in report."""
    client, coll = temp_coll
    aud = _auditor(client, coll, tmp_path, monkeypatch, sweep=None)
    t = "Default-off duplicates must survive the audit run"
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    _upsert(client, coll, a, t, _iso(30))
    _upsert(client, coll, b, t, _iso(5))

    report = aud.run_audit()

    assert "dedup_sweep" not in report
    assert ha._sweep_enabled() is False
    assert len({str(p.id) for p in aud._collect_points()}) == 2


def test_explicit_optin_env_runs_sweep(temp_coll, tmp_path, monkeypatch):
    client, coll = temp_coll
    aud = _auditor(client, coll, tmp_path, monkeypatch, sweep="true")
    t = "Explicit opt-in enables the destructive dedup sweep"
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    _upsert(client, coll, a, t, _iso(30))
    _upsert(client, coll, b, t, _iso(5))

    report = aud.run_audit()

    assert report["dedup_sweep"]["merged"] == 1
    assert report["dedup_sweep"]["backup_file"]
    assert len({str(p.id) for p in aud._collect_points()}) == 1


def test_webhook_reports_deletions_truthfully(temp_coll, tmp_path, monkeypatch):
    """When the sweep deleted points, the webhook must NOT claim read-only."""
    client, coll = temp_coll
    aud = _auditor(client, coll, tmp_path, monkeypatch, sweep="1")
    monkeypatch.setenv("NEXUS_WEBHOOK_URL", "http://webhook.example/notify")
    captured = []

    def _fake_urlopen(req, timeout=15):
        captured.append(json.loads(req.data.decode("utf-8")))
        return _FakeResponse()
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    t = "Webhook truthfulness test memory for deleted-copies reporting"
    a, b, c = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    _upsert(client, coll, a, t, _iso(30))
    _upsert(client, coll, b, t, _iso(20))
    _upsert(client, coll, c, t, _iso(5))

    report = aud.run_audit()

    assert report["dedup_sweep"]["merged"] == 2
    assert len(captured) == 1
    body = captured[0]["content"]
    assert "DELETED 2 duplicate copies" in body
    assert "destructive" in body
    assert "read-only" not in body.lower()


def test_webhook_reports_readonly_when_nothing_deleted(temp_coll, tmp_path, monkeypatch):
    client, coll = temp_coll
    aud = _auditor(client, coll, tmp_path, monkeypatch, sweep=None)
    monkeypatch.setenv("NEXUS_WEBHOOK_URL", "http://webhook.example/notify")
    captured = []

    def _fake_urlopen(req, timeout=15):
        captured.append(json.loads(req.data.decode("utf-8")))
        return _FakeResponse()
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    t = "Read-only reporting test memory with an exact duplicate copy"
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    _upsert(client, coll, a, t, _iso(30))
    _upsert(client, coll, b, t, _iso(5))

    report = aud.run_audit()

    assert "dedup_sweep" not in report
    assert len(captured) == 1
    body = captured[0]["content"]
    assert "read-only" in body
    assert "No memories were deleted" in body
    assert "DELETED" not in body


def test_flags_message_matches_actual_behavior(temp_coll, tmp_path, monkeypatch):
    client, coll = temp_coll
    aud = _auditor(client, coll, tmp_path, monkeypatch, sweep=None)
    t = "Health flags must reflect whether points were actually deleted"
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    _upsert(client, coll, a, t, _iso(30))
    _upsert(client, coll, b, t, _iso(5))

    aud.run_audit()
    flags = aud.get_flags()
    msg_readonly = flags["dedup"]["message"]
    assert "READ-ONLY" in msg_readonly
    assert "DELETED" not in msg_readonly

    # now the destructive opt-in run
    aud2 = _auditor(client, coll, tmp_path, monkeypatch, sweep="1")
    aud2.run_audit()
    flags2 = aud2.get_flags()
    msg_deleted = flags2["dedup"]["message"]
    assert "DELETED" in msg_deleted
    assert "READ-ONLY" not in msg_deleted


# ── Finding 5 (MEDIUM): explicit collection is respected ───────────────


def test_uses_explicit_collection_argument(tmp_path, monkeypatch):
    client = _mk_client()
    try:
        for name in ("coll-main", "coll-other"):
            client.create_collection(name, vectors_config=VectorParams(size=DIM, distance=Distance.COSINE))
        _upsert(client, "coll-main", str(uuid.uuid4()),
                "Only the explicitly passed collection may be touched", _iso(30))
        _upsert(client, "coll-main", str(uuid.uuid4()),
                "only the explicitly passed collection may be touched", _iso(5))
        other_id = str(uuid.uuid4())
        _upsert(client, "coll-other", other_id,
                "Only the explicitly passed collection may be touched", _iso(1))

        aud = _auditor(client, "coll-main", tmp_path, monkeypatch, sweep="1")
        report = aud.run_audit()

        assert report["collection"] == "coll-main"
        assert report["dedup_sweep"]["merged"] == 1
        # the other collection is untouched
        other_points = list(client.scroll("coll-other", limit=100, with_payload=True)[0])
        assert [str(p.id) for p in other_points] == [other_id]
        main_points = list(client.scroll("coll-main", limit=100, with_payload=True)[0])
        assert len(main_points) == 1
    finally:
        client.delete_collection("coll-main")
        client.delete_collection("coll-other")