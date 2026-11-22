"""Tests for dreaming + archive_forgetting (v0.22.0)."""
import json
import sqlite3
import time
import uuid
from pathlib import Path

import pytest

from nexus_memory import dreaming, archive_forgetting, events


@pytest.fixture(autouse=True)
def _leeres_journal(monkeypatch, tmp_path):
    """Kein Test liest das echte Ereignis-Journal des Rechners."""
    monkeypatch.setenv("NEXUS_EVENT_DIR", str(tmp_path / "events"))
    monkeypatch.delenv("NEXUS_DREAMING_SOURCES", raising=False)
    monkeypatch.delenv("NEXUS_DREAMING_JOURNAL", raising=False)


class FakeStore:
    def __init__(self, similar_score=0.0, named_vectors=False,
                 vector_count=1):
        self.collection = "nexus"
        self._similar_score = similar_score
        self.deleted = []
        self.client = self  # qdrant-style: store.client exposes the API
        # Regel B: eine Sammlung kann einen BENANNTEN Vektorraum haben. Dann
        # liefert Qdrant den Vektor als dict {name: liste} statt als Liste.
        self._named = named_vectors
        self._vector_count = vector_count

    class _Hit:
        def __init__(self, score):
            self.score = score

    def _vector(self):
        if not self._named:
            return [0.1]
        names = ["ollama__qwen3-embedding_0_6b__1024",
                 "openai__text-embedding-3-small__1536"][:self._vector_count]
        return {n: [0.1] for n in names}

    def search(self, collection_name, query_vector, limit, query_filter):
        if self._similar_score:
            return [self._Hit(self._similar_score)]
        return []

    def query_points(self, collection_name=None, query=None, limit=1, using=None, **kw):
        """Der Weg, den dreaming.py wirklich nimmt (Review-Fund 09.10.).

        Vorher hatte der Doppel nur search() — dreaming ruft aber
        query_points(). Der Aufruf lief in einen AttributeError, den der
        fail-open verschluckte, und der Dedup-Test war vakuum-gruen.
        """
        if self._similar_score:
            class _Resp:
                points = [type("H", (), {"score": self._similar_score})]
            return _Resp()
        class _Empty:
            points = []
        return _Empty()

    def scroll(self, collection_name, scroll_filter, limit,
               with_payload, with_vectors, **unknown):
        # Doppel muss sich wie der ECHTE Client verhalten: ein falscher
        # Argumentname ist ein harter Fehler, kein stiller Durchlauf.
        # (Wurzel des Fundes vom 09.10.: der Doppel nahm with_vector klaglos
        # an, deshalb blieb der Fehler wochenlang gruen im Test.)
        if unknown:
            raise AssertionError(
                f"Unknown arguments: {sorted(unknown)}")
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
                                         "vector": self._vector()})())
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


# ── 0.22.61: Lehren nur aus strukturellen Fehlschlägen ────────────
# Das alte Träumen suchte Fehlerwörter im Text und legte ein 160-Zeichen-
# Fenster als "Muster" ab. Diese Tests halten fest, was stattdessen gilt.

def _tool_zeile(exit_code=1, output="", error=None, hint=None, tool="terminal"):
    inhalt = {"output": output, "exit_code": exit_code, "error": error}
    if hint:
        inhalt["hint"] = hint
    return (tool, json.dumps(inhalt))


def _db_mit_sitzungen(tmp_path, eintraege):
    """eintraege: Liste (session_id, [ (tool, content) ... ])"""
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sessions (id TEXT, started_at REAL, "
                 "message_count INT, tool_call_count INT, ended_at REAL)")
    conn.execute("CREATE TABLE messages (session_id TEXT, role TEXT, "
                 "tool_name TEXT, content TEXT, timestamp REAL)")
    now = time.time()
    for i, (sid, werkzeuge) in enumerate(eintraege):
        conn.execute("INSERT INTO sessions VALUES (?, ?, 9, 6, ?)",
                     (sid, now - 7200 - i, now - 3600))
        for j, (tool, content) in enumerate(werkzeuge):
            conn.execute("INSERT INTO messages VALUES (?, 'tool', ?, ?, ?)",
                         (sid, tool, content, now - 3600 + j))
    conn.commit()
    conn.close()
    return db


def test_fehlerwort_im_erfolg_ist_kein_fall():
    """Ein Fehlerwort in einer erfolgreichen Ausgabe zählt nicht.

    Genau daran scheiterte die alte Fassung: sie las "error" aus einem
    Logfile-Auszug und hielt den Fetzen für eine Lehre.
    """
    _, inhalt = _tool_zeile(exit_code=0,
                            output="grep-treffer: ModuleNotFoundError: No module named 'requests'")
    fehler, _, _ = dreaming._outcome(inhalt)
    assert fehler is False


def test_fehlerklasse_und_signatur():
    assert dreaming._signatur(
        "terminal",
        "Traceback ...\nModuleNotFoundError: No module named 'requests'") == \
        "terminal:ModuleNotFoundError:requests"
    assert dreaming._signatur("terminal", "Exit 124: timed out after 300s") == \
        "terminal:Timeout"
    # Unbekannte Fehler bekommen keinen Namen und werden nicht gelernt.
    assert dreaming._signatur("terminal", "irgendwas ist kaputt") is None


def test_regelblockade_der_umgebung_ist_keine_lehre():
    _, inhalt = _tool_zeile(error="THINK-GATE: write blockiert")
    fehler, text, _ = dreaming._outcome(inhalt)
    assert fehler is True
    assert dreaming._signatur("write_file", text) is None


def test_einmalig_wird_nicht_gelernt(monkeypatch, tmp_path):
    """Wiederkehr ist eine Zählung, kein Modellurteil."""
    db = _db_mit_sitzungen(tmp_path, [
        ("s1", [_tool_zeile(output="ModuleNotFoundError: No module named 'requests'")]),
    ])
    monkeypatch.setenv("NEXUS_DREAMING_SOURCES",
                       json.dumps([{"type": "hermes", "db": str(db)}]))
    monkeypatch.setattr(dreaming, "PLAYBOOK_DIR", tmp_path / "pb")
    monkeypatch.setattr(dreaming, "RELEARN_DAYS", 30.0)
    out = dreaming.dream_once(dry_run=True)
    assert out["incidents"] == 1 and out["recurring"] == 0
    assert out["patterns"] == 0 and not out.get("would_store")


def test_wiederkehrend_wird_als_lehre_gebaut(monkeypatch, tmp_path):
    hint = ("Python cannot import 'requests'. Activate the project venv "
            "or run the script with the venv interpreter.")
    eintrag = _tool_zeile(output="ModuleNotFoundError: No module named 'requests'",
                          hint=hint)
    db = _db_mit_sitzungen(tmp_path, [("s1", [eintrag]), ("s2", [eintrag])])
    monkeypatch.setenv("NEXUS_DREAMING_SOURCES",
                       json.dumps([{"type": "hermes", "db": str(db)}]))
    monkeypatch.setattr(dreaming, "PLAYBOOK_DIR", tmp_path / "pb")
    monkeypatch.setattr(dreaming, "RELEARN_DAYS", 30.0)
    out = dreaming.dream_once(dry_run=True)
    assert out["recurring"] == 1
    lehre = out["would_store"][0]
    assert "2 Sitzungen" in lehre
    assert "ModuleNotFoundError" in lehre and "requests" in lehre
    assert "project venv" in lehre
    # Kein Rohtext: kein Traceback, kein Dateipfad, kein Zeilenumbruch.
    assert "Traceback" not in lehre and "\n" not in lehre


def test_programmierfehler_ohne_hint_wird_nicht_gelehrt():
    sig = "execute_code:KeyError:'output'"
    assert dreaming._ist_lehrreich(sig, "") is False
    # Mit Hinweis der Umgebung dagegen schon: der erklärt etwas über die Welt.
    assert dreaming._ist_lehrreich(sig, "Tool-Ergebnisse sind dicts, kein json.loads") is True


def test_hint_der_nur_weiterverweist_zaehlt_nicht():
    """Ein Verweis auf die Fortsetzung erklärt nichts über die Welt."""
    assert dreaming._ist_lehrreich(
        "execute_code:KeyError:'x'", "Use offset=81 to continue reading") is False
    # Timeout ohne Namen ist Rauschen, mit Namen (Paket, Code) ist es eine Lehre.
    assert dreaming._ist_lehrreich("terminal:Timeout", "") is False
    assert dreaming._ist_lehrreich("terminal:HTTPError:400", "") is True


def test_zweiter_lauf_lernt_dieselbe_stoerung_nicht_nochmal(monkeypatch, tmp_path):
    eintrag = _tool_zeile(output="ModuleNotFoundError: No module named 'websocket'")
    db = _db_mit_sitzungen(tmp_path, [("s1", [eintrag]), ("s2", [eintrag])])
    monkeypatch.setenv("NEXUS_DREAMING_SOURCES",
                       json.dumps([{"type": "hermes", "db": str(db)}]))
    monkeypatch.setattr(dreaming, "PLAYBOOK_DIR", tmp_path / "pb")
    monkeypatch.setattr(dreaming, "RELEARN_DAYS", 30.0)
    monkeypatch.setenv("NEXUS_DREAM_STORE", "0")
    erst = dreaming.dream_once(dry_run=False)
    assert erst["patterns"] == 1 and erst["stored"] == 0
    assert erst.get("store_skipped")
    (datei,) = (tmp_path / "pb").glob("dream-*.json")
    assert "websocket" in datei.read_text(encoding="utf-8")
    # Zweiter Lauf am selben Tag: dieselbe Störung nicht noch einmal.
    zweit = dreaming.dream_once(dry_run=False)
    assert zweit["recurring"] == 1 and zweit["patterns"] == 0


def test_lange_eingabe_zerfaellt_nicht_in_fenster(monkeypatch, tmp_path):
    """Der Fehler vom 20.11.: eine lange Cron-Anweisung wurde in
    Textfenster zerlegt und jedes Fenster als Lehre gespeichert."""
    lang = "fix " * 400 + " ModuleNotFoundError: No module named 'requests' " + " gotcha " * 300
    db = _db_mit_sitzungen(tmp_path, [
        ("s1", [("terminal", json.dumps({"output": lang, "exit_code": 0}))]),
        ("s2", [("terminal", json.dumps({"output": lang, "exit_code": 0}))]),
    ])
    monkeypatch.setenv("NEXUS_DREAMING_SOURCES",
                       json.dumps([{"type": "hermes", "db": str(db)}]))
    monkeypatch.setattr(dreaming, "PLAYBOOK_DIR", tmp_path / "pb")
    out = dreaming.dream_once(dry_run=True)
    assert out["incidents"] == 0 and out["patterns"] == 0


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


# ── Fund 09.10.2026: stiller Archiv-Ausfall + benannte Vektoren ─────

def test_archive_scroll_uses_real_client_kwarg(monkeypatch, tmp_path):
    """Der Doppel muss den echten Client abbilden.

    Vorher uebergab der Code `with_vector` — der echte Qdrant-Client kennt
    nur `with_vectors` und wirft, der Fehler wurde per fail-open verschluckt
    (314x im Live-Log). Dieser Test allein haette den Ausfall sofort gezeigt.
    """
    monkeypatch.setattr(archive_forgetting, "BACKUP_DIR", tmp_path / "bk")
    seen = {}

    class _Strict(FakeStore):
        def scroll(self, collection_name, scroll_filter, limit,
                   with_payload, with_vectors, **unknown):
            seen["with_vectors"] = with_vectors
            seen["unknown"] = unknown
            return super().scroll(collection_name, scroll_filter, limit,
                                  with_payload, with_vectors, **unknown)

    out = archive_forgetting.archive_once(_Strict(), "nexus", dry_run=True)
    assert seen["unknown"] == {}, f"falsche Argumente: {seen['unknown']}"
    assert seen["with_vectors"] is True
    assert out["stale"] >= 1  # Beweis: es wird tatsaechlich gefunden


def test_archive_named_vector_keeps_plain_list(monkeypatch, tmp_path):
    """Regel B: benannter Vektorraum liefert dict — gesichert wird die Liste."""
    monkeypatch.setattr(archive_forgetting, "BACKUP_DIR", tmp_path / "bk")
    store = FakeStore(named_vectors=True)
    out = archive_forgetting.archive_once(store, "nexus", dry_run=False)
    assert out["deleted"] == out["backed_up"] >= 1
    line = (tmp_path / "bk").glob("session-points-*.jsonl")
    first = json.loads(next(iter(line)).read_text(encoding="utf-8").splitlines()[0])
    assert isinstance(first["vector"], list), f"dict gesichert: {first['vector']}"
    assert first["vector"] == [0.1]


def test_archive_multivector_is_skipped_not_guessed(monkeypatch, tmp_path):
    """Mehrere Vektoren = mehrdeutig: ueberspringen statt raten."""
    monkeypatch.setattr(archive_forgetting, "BACKUP_DIR", tmp_path / "bk")
    store = FakeStore(named_vectors=True, vector_count=2)
    out = archive_forgetting.archive_once(store, "nexus", dry_run=False)
    assert out["stale"] == 0 and out["deleted"] == 0
    assert store.deleted == []


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
    """Der echte Lauf: Datei entsteht, Marker wird erst danach gesetzt."""
    eintrag = ("terminal",
               json.dumps({"output": "ModuleNotFoundError: No module named 'requests'",
                           "exit_code": 1, "error": None,
                           "hint": "Wrong interpreter: use the project venv."}))
    db = _db_mit_sitzungen(tmp_path, [("s1", [eintrag]), ("s2", [eintrag])])
    monkeypatch.setenv("NEXUS_DREAMING_SOURCES",
                       json.dumps([{"type": "hermes", "db": str(db)}]))
    monkeypatch.setattr(dreaming, "PLAYBOOK_DIR", tmp_path / "pb")
    monkeypatch.setattr(dreaming, "RELEARN_DAYS", 30.0)
    monkeypatch.setenv("NEXUS_DREAM_STORE", "0")

    out = dreaming.dream_once(store=None, dry_run=False)
    assert out["patterns"] == 1 and out["stored"] == 0
    (datei,) = (tmp_path / "pb").glob("dream-*.json")
    koerper = json.loads(datei.read_text(encoding="utf-8"))
    assert koerper[0]["signature"] == "terminal:ModuleNotFoundError:requests"
    assert koerper[0]["sessions"] == 2
    # Marker nach erfolgreichem Lauf → zweiter Lauf sieht keine neue Sitzung.
    marker = (tmp_path / "pb" / ".dreaming-last-run").read_text(encoding="utf-8")
    assert "s1" in marker and "s2" in marker
    out2 = dreaming.dream_once(store=None, dry_run=False)
    assert out2["new_sessions"] == 0


def test_dream_ohne_messages_tabelle_bricht_nicht_ab(monkeypatch, tmp_path):
    """Eine Sitzung ohne lesbare Nachrichten darf den Lauf nicht kippen."""
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE sessions (id TEXT, started_at REAL, "
                 "message_count INT, tool_call_count INT, ended_at REAL)")
    now = time.time()
    conn.execute("INSERT INTO sessions VALUES ('s1', ?, 9, 6, ?)",
                 (now - 7200, now - 3600))
    conn.commit()
    conn.close()
    monkeypatch.setenv("NEXUS_DREAMING_SOURCES",
                       json.dumps([{"type": "hermes", "db": str(db)}]))
    monkeypatch.setattr(dreaming, "PLAYBOOK_DIR", tmp_path / "pb")
    out = dreaming.dream_once(dry_run=True)
    assert out["sessions_scanned"] == 1 and out["patterns"] == 0
    assert "error" not in out


def test_archive_never_touches_facts_with_real_fact_present(monkeypatch, tmp_path):
    # Review gap: old test had NO fact point in the store (tautology).
    # FakeStore now carries one; the category filter must protect it.
    monkeypatch.setattr(archive_forgetting, "BACKUP_DIR", tmp_path / "bk")
    store = FakeStore()
    archive_forgetting.archive_once(store, "nexus", dry_run=False)
    assert all(d != 42_000_002 for d in store.deleted)
    assert 42_000_003 in store.deleted  # numeric-string session WAS deleted

def test_store_playbook_goes_through_the_write_gate(monkeypatch):
    """Das Traeumen schrieb als eigener Weg direkt in die Sammlung.

    Seit 20.11.2026 gilt dasselbe Tor wie fuer jeden Agenten: ein Muster ohne
    Aussage ("laeuft noch") wird abgewiesen und nichts geschrieben."""
    from unittest.mock import MagicMock

    store = MagicMock()
    store.client = MagicMock()
    dreamer = dreaming.Dreamer(store=store)

    aufgerufen = {"n": 0}

    def kein_schreiben(*a, **k):
        aufgerufen["n"] += 1
        return MagicMock()

    monkeypatch.setattr(dreaming, "upsert_named", kein_schreiben, raising=False)

    assert dreamer._store_playbook({"pattern": "laeuft noch", "verdict": ""}) is False
    assert aufgerufen["n"] == 0, "ein abgewiesenes Muster darf die Sammlung nicht erreichen"


def test_store_playbook_marks_the_writer(monkeypatch):
    """Ein getraeumter Punkt nennt das Verfahren (``writer``), keinen Agenten.

    Wie bei der Konsolidierung: der Traum fasst Sitzungen mehrerer Agenten
    zusammen, ``agent`` bleibt deshalb leer.
    """
    from unittest.mock import MagicMock

    class _Embedder:
        async def embed(self, text, is_query=False):
            return [0.1] * 8

    class _Store:
        def __init__(self):
            self.client = MagicMock()
            self.vector_name = None
            self.collection = "test-collection"
            self._embedder = _Embedder()

    dreamer = dreaming.Dreamer(store=_Store())
    punkte: list = []
    # ``_store_playbook`` importiert ``upsert_named`` lokal aus
    # ``collection_vectors`` — dort wird es ersetzt, nicht im Modul.
    import nexus_memory.collection_vectors as collection_vectors

    monkeypatch.setattr(
        collection_vectors, "upsert_named",
        lambda _client, _collection, pts, _vector: punkte.extend(pts),
    )

    assert dreamer._store_playbook({
        "pattern": "Nach einem Import-Fehler zuerst den Interpreter pruefen",
        "verdict": "hat geholfen",
    }) is True

    payload = punkte[0].payload
    assert payload["writer"] == "dreaming"
    assert "agent" not in payload, "kein geratener Verfasser an einem Traumpunkt"


# ── 0.22.61: neutrale Quelle statt Hermes-Datenbank ───────────────

def test_journal_ist_die_quelle_ohne_fremde_datenbank(monkeypatch, tmp_path):
    """Zwei Meldungen aus zwei Sitzungen = eine Lehre, ohne Hermes."""
    from nexus_memory import events as journal
    journal.record(agent="hermes", tool="terminal", ok=False,
                   error="ModuleNotFoundError: No module named 'requests'",
                   hint="Falscher Interpreter: venv nutzen.", session="s1")
    journal.record(agent="dsh", tool="terminal", ok=False,
                   error="ModuleNotFoundError: No module named 'requests'",
                   hint="Falscher Interpreter: venv nutzen.", session="s2")
    monkeypatch.setattr(dreaming, "PLAYBOOK_DIR", tmp_path / "pb")
    out = dreaming.dream_once(dry_run=True)
    assert out["events_scanned"] == 2
    assert out["recurring"] == 1
    assert "2 Sitzungen" in out["would_store"][0]


def test_gleiche_sitzung_zweimal_ist_keine_wiederkehr(monkeypatch, tmp_path):
    from nexus_memory import events as journal
    for _ in range(3):
        journal.record(agent="hermes", tool="terminal", ok=False,
                       error="Exit 124: timed out after 300s", session="s1")
    monkeypatch.setattr(dreaming, "PLAYBOOK_DIR", tmp_path / "pb")
    out = dreaming.dream_once(dry_run=True)
    assert out["events_scanned"] == 3 and out["recurring"] == 0


def test_meldung_ohne_sitzung_zaehlt_nur_einmal(monkeypatch, tmp_path):
    """Ohne Sitzungskennung lieber nichts lernen als Wiederkehr erfinden."""
    from nexus_memory import events as journal
    for _ in range(2):
        journal.record(agent="openclaw", tool="terminal", ok=False,
                       error="ModuleNotFoundError: No module named 'websocket'")
    monkeypatch.setattr(dreaming, "PLAYBOOK_DIR", tmp_path / "pb")
    out = dreaming.dream_once(dry_run=True)
    assert out["events_scanned"] == 2 and out["recurring"] == 0


def test_journal_kappt_lange_texte():
    from nexus_memory import events as journal
    eintrag = journal.normalisieren({"tool": "terminal", "error": "x" * 9000,
                                     "hint": "y" * 2000})
    assert len(eintrag["error"]) == journal.MAX_FEHLERTEXT
    assert len(eintrag["hint"]) == journal.MAX_HINT


def test_journal_ohne_werkzeug_wird_nicht_geschrieben():
    from nexus_memory import events as journal
    assert journal.normalisieren({"error": "irgendwas"}) is None
    assert journal.write_many([{"tool": ""}]) == 0


def test_kaputte_journalzeile_bricht_das_lesen_nicht():
    from nexus_memory import events as journal
    journal.record(agent="hermes", tool="terminal", ok=False,
                   error="Exit 124: timed out after 300s")
    pfad = journal.journal_path()
    with open(pfad, "a", encoding="utf-8") as fh:
        fh.write("kein json\n")
    assert len(list(journal.iter_events())) == 1


def test_dublettenpruefung_laeuft_wirklich(monkeypatch):
    """Der Dedup-Aufruf war jahrelang kaputt: ``_embed`` ist async und nimmt
    einen Text, nicht eine Liste. Der Coroutine-Aufruf flog im except, die
    Prüfung lief nie. Dieser Test fährt den echten Pfad."""
    import asyncio

    class _Embedder:
        def __init__(self):
            self.gesehen = []

        async def embed(self, text, is_query=False):
            self.gesehen.append(text)
            return [0.1, 0.2]

    class _Store:
        def __init__(self, score):
            self.collection = "nexus"
            self._embedder = _Embedder()
            self.client = self
            self._score = score

        def query_points(self, collection_name=None, query=None, limit=1,
                         using=None, **kw):
            assert query == [0.1, 0.2], f"Vektor falsch: {query!r}"
            punkte = [type("H", (), {"score": self._score})]
            return type("R", (), {"points": punkte})

    store = _Store(0.95)
    dreamer = dreaming.Dreamer(store=store)
    assert dreamer._similar_known("irgendein Merksatz") is True
    assert store._embedder.gesehen == ["irgendein Merksatz"]
    assert dreaming.Dreamer(store=_Store(0.5))._similar_known("neu") is False
