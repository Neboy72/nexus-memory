"""The maintenance daemons stay quiet when there is nothing to work on.

Two in-process daemons (trust recompute, retrieval watch) start with every
``MemoryStore``. Against a collection that does not exist yet — a fresh
install, or the test suite itself — they used to produce a stacktrace and a
"3/3 queries failed" alarm. Both are false alarms: they hide real errors and
make a clean installation look broken.

Covered here:
- the kill-switches in ``MemoryStore.__init__`` (tests/conftest.py sets them),
- a missing collection → one INFO line, empty result, no exception,
- an unusable ``scroll`` reply → one WARNING, no ``ValueError``/stacktrace.
"""

import logging
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import nexus_memory.mcp_server as mcp
from nexus_memory import retrieval_watch as rw
from nexus_memory import trust_service as ts
from nexus_memory.retrieval_watch import RetrievalWatch
from nexus_memory.trust_service import TrustService


# ── helpers ──────────────────────────────────────────────────────────────────

class _MissingCollectionClient:
    """Qdrant client stand-in whose collection list is empty."""

    def __init__(self):
        self.scroll_calls = 0

    def get_collections(self):
        return SimpleNamespace(collections=[])

    def scroll(self, *args, **kwargs):  # pragma: no cover - must not be called
        self.scroll_calls += 1
        raise AssertionError("scroll must not run when the collection is absent")


class _MalformedScrollClient:
    """Client that claims the collection exists but answers scroll unusably."""

    def __init__(self):
        self.scroll_calls = 0

    def get_collections(self):
        return SimpleNamespace(collections=[SimpleNamespace(name="c-quiet-malformed")])

    def scroll(self, *args, **kwargs):
        self.scroll_calls += 1
        return MagicMock()  # not a (points, offset) pair → used to raise ValueError


def _store(client):
    return SimpleNamespace(client=client, collection_name="c", _embedder=None)


# ── A) kill-switches ─────────────────────────────────────────────────────────

class _FakeEmbedder:
    """Just enough EmbeddingProvider for ``MemoryStore.__init__``."""

    backend = "local"
    model_name = "fake-embedder"
    name = "fake-embedder"
    dim = 2
    _preferred = ""

    def __init__(self):
        pass

    async def embed(self, text, is_query=True):
        return [0.0] * 2


@pytest.fixture
def build_store(monkeypatch, mock_qdrant_client):
    """Factory: a ``MemoryStore`` with a mocked Qdrant client and no hybrid.

    A factory (not a ready-made store) because the switches are read inside
    ``MemoryStore.__init__`` — each test sets the environment *first*.
    """
    def _build():
        monkeypatch.setattr(mcp, "EmbeddingProvider", _FakeEmbedder)
        monkeypatch.setattr(mcp.MemoryStore, "_init_hybrid", lambda self: None)
        mock_qdrant_client.get_collections.return_value = MagicMock(collections=[])
        return mcp.MemoryStore()

    return _build


def test_switches_off_start_neither_daemon(monkeypatch, build_store):
    """With both kill-switches at 0 no service is constructed or started."""
    started = []
    monkeypatch.setattr(TrustService, "start", lambda self: started.append("trust"))
    monkeypatch.setattr(RetrievalWatch, "start", lambda self: started.append("watch"))

    assert os.environ["NEXUS_TRUST_SWEEP"] == "0"
    assert os.environ["NEXUS_RETRIEVAL_WATCH"] == "0"

    store = build_store()
    assert started == []
    assert store._trust_service is None
    assert store._retrieval_watch is None


def test_switches_on_start_both_daemons(monkeypatch, tmp_path, build_store):
    """Positive control: the switches are what gates the daemons."""
    started = []
    monkeypatch.setattr(TrustService, "start", lambda self: started.append("trust"))
    monkeypatch.setattr(RetrievalWatch, "start", lambda self: started.append("watch"))
    monkeypatch.setenv("NEXUS_TRUST_SWEEP", "1")
    monkeypatch.setenv("NEXUS_RETRIEVAL_WATCH", "1")
    # TrustService writes its report dir under $HOME; keep that in the test.
    monkeypatch.setenv("HOME", str(tmp_path))

    store = build_store()
    assert started == ["trust", "watch"]
    assert store._trust_service is not None
    assert store._retrieval_watch is not None


# ── B) collection missing ────────────────────────────────────────────────────

def test_trust_skips_missing_collection(tmp_path, caplog):
    client = _MissingCollectionClient()
    svc = TrustService(_store(client), "c-quiet-trust-missing", data_dir=str(tmp_path))

    with caplog.at_level(logging.INFO, logger="nexus.trust"):
        points = svc._scroll_all()
        svc._scroll_all()  # second pass must not repeat the line

    assert points == []
    assert client.scroll_calls == 0
    infos = [r for r in caplog.records if "not present yet" in r.getMessage()]
    assert len(infos) == 1, "exactly one INFO line per process"
    assert infos[0].levelno == logging.INFO
    assert "c-quiet-trust-missing" in infos[0].getMessage()
    # no stacktrace anywhere
    assert all(r.exc_info is None for r in caplog.records)

    report = svc.run()
    assert report["beliefs_scanned"] == 0
    assert report["updated"] == 0


def test_retrieval_watch_skips_missing_collection(tmp_path, caplog):
    client = _MissingCollectionClient()
    watch = RetrievalWatch(_store(client), "c-quiet-watch-missing")
    watch._queries = [("Bose SoundLink", "Bose"), ("Razer Mikrofon", "Razer")]

    with caplog.at_level(logging.INFO, logger="nexus.retrieval_watch"):
        report = watch.run()

    assert client.scroll_calls == 0
    assert report["queries_checked"] == 0
    assert report["failures"] == []
    assert report["skipped"]
    assert watch.get_flags() == {}, "a missing collection is not a quality alarm"

    infos = [r for r in caplog.records if "not present yet" in r.getMessage()]
    assert len(infos) == 1
    assert all(r.exc_info is None for r in caplog.records)
    assert not any("queries failed" in r.getMessage() for r in caplog.records)


# ── C) unusable scroll reply ─────────────────────────────────────────────────

def test_trust_malformed_scroll_warns_instead_of_raising(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(ts, "_malformed_scroll_noticed", False)
    client = _MalformedScrollClient()
    svc = TrustService(_store(client), "c-quiet-malformed", data_dir=str(tmp_path))

    with caplog.at_level(logging.WARNING, logger="nexus.trust"):
        assert svc._scroll_all() == []  # no ValueError
        assert svc._scroll_all() == []

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, "one clear warning per process"
    assert "unusable page" in warnings[0].getMessage()
    assert all(r.exc_info is None for r in caplog.records)
