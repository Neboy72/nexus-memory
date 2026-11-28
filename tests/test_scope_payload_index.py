"""Guards for ``scripts/scope_payload_index.py`` (payload index on ``scope``).

Why this file exists at all: the script touches a live collection, and its one
safety promise is "no snapshot, no build". That promise is worth a test, not a
comment — the script is not run by CI, so nothing else would notice if it broke.

Also pinned here: the script's filter is the SAME three visibility conditions
the plugin applies (``_scope_should``). If the visibility rule changes there,
this test must fail here.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT_PATH = (Path(__file__).resolve().parents[1]
                / "scripts" / "scope_payload_index.py")


def _load_script():
    spec = importlib.util.spec_from_file_location("scope_payload_index",
                                                  _SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script = _load_script()

_COL = "nexus_hf"


class _FakeQdrant:
    """Stands in for the HTTP layer; records every call in order."""

    def __init__(self, *, indexed=False, initial_type="keyword", snapshot=None,
                 snapshot_error=None, put_error=None, counts=(5, 5),
                 becomes_keyword=True, delay_gets=None):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.indexed = indexed
        # The type of the index that already exists (keyword unless a test
        # simulates the wrong one, e.g. ``text``). A successful PUT rebuilds
        # it as keyword.
        self.current_type = initial_type
        self.snapshot = ({"name": "snap-2026-11-27", "size": 124_000_000,
                          "checksum": "deadbeef"} if snapshot is None
                         else snapshot)
        self.snapshot_error = snapshot_error
        self.put_error = put_error
        self.counts = list(counts)
        self.becomes_keyword = becomes_keyword
        # Qdrant fills payload_schema from the optimiser side: on 27.11.2026
        # the index was built and working while the collection info still said
        # nothing. This many GETs AFTER the PUT are needed before it shows up.
        self.delay_gets = delay_gets
        self._put_done = False
        self._gets_after_put = 0

    def _info(self) -> dict:
        schema = ({"scope": {"data_type": self.current_type}}
                  if self.indexed else {})
        return {"result": {
            "points_count": 68602,
            "config": {"params": {"on_disk_payload": True,
                                  "payload_schema": schema},
                       "hnsw_config": {"full_scan_threshold": 10000}},
        }}

    def __call__(self, method, url, body=None, timeout=600.0):
        self.calls.append((method, url, body))
        if method == "GET":
            if self.delay_gets is not None and self._put_done:
                self._gets_after_put += 1
                if self._gets_after_put >= self.delay_gets:
                    self.indexed = self.becomes_keyword
            return self._info()
        if url.endswith("/points/count"):
            count = self.counts.pop(0) if len(self.counts) > 1 else self.counts[0]
            return {"result": {"count": count}}
        if url.endswith("/snapshots"):
            if self.snapshot_error:
                raise RuntimeError(self.snapshot_error)
            return {"result": self.snapshot}
        if "/index/scope" in url and method == "DELETE":
            self.indexed = False
            return {"result": True}
        if url.endswith("/index?wait=true") and method == "PUT":
            if self.put_error:
                raise RuntimeError(self.put_error)
            self._put_done = True
            if self.becomes_keyword:
                self.current_type = "keyword"
            self.indexed = (False if self.delay_gets is not None
                            else self.becomes_keyword)
            return {"result": True}
        raise AssertionError(f"unexpected call: {method} {url}")

    # helpers for assertions
    def methods(self):
        return [c[0] for c in self.calls]

    def put_body(self):
        for method, url, body in self.calls:
            if method == "PUT":
                return body
        return None


@pytest.fixture()
def fake(monkeypatch):
    def install(**kwargs):
        f = _FakeQdrant(**kwargs)
        monkeypatch.setattr(script, "_request", f)
        return f
    return install


@pytest.fixture()
def clock(monkeypatch):
    """Fake clock: the bounded wait costs no real seconds in tests."""
    ticks = {"t": 1000.0}

    def _sleep(seconds):
        ticks["t"] += seconds

    monkeypatch.setattr(script, "_now", lambda: ticks["t"])
    monkeypatch.setattr(script, "_sleep", _sleep)
    return ticks


def _argv(*extra):
    return ["--url", "http://localhost:6333", "--collection", _COL,
            "--bundle", "hermes", *extra]


def test_dry_run_only_reads(fake, capsys):
    """Without --apply the script reports and stops — nothing is written."""
    f = fake()
    assert script.main(_argv()) == 0
    assert "PUT" not in f.methods()
    assert "DELETE" not in f.methods()
    assert "POST" in f.methods()  # the count query is read-only
    assert "dry run" in capsys.readouterr().out


def test_snapshot_without_checksum_aborts_before_any_build(fake, capsys):
    """Name and size are not enough: without a checksum there is no restore
    point, so nothing may be built."""
    f = fake(snapshot={"name": "snap-1", "size": 124})
    assert script.main(_argv("--apply")) == 1
    assert "PUT" not in f.methods()
    assert "nothing is built" in capsys.readouterr().out


def test_failed_snapshot_aborts_before_any_build(fake, capsys):
    f = fake(snapshot_error="disk full")
    assert script.main(_argv("--apply")) == 1
    assert "PUT" not in f.methods()
    assert "disk full" in capsys.readouterr().out


def test_apply_snapshots_then_builds_keyword_index(fake):
    """The happy path, in order: snapshot first, then the keyword index."""
    f = fake()
    assert script.main(_argv("--apply")) == 0

    methods = [m for m, url, _ in f.calls
               if url.endswith("/snapshots") or url.endswith("/index?wait=true")]
    assert methods == ["POST", "PUT"], methods
    assert f.put_body() == {"field_name": "scope", "field_schema": "keyword"}


def test_apply_refuses_when_the_visible_set_changes(fake, capsys):
    """The verification count is not a formality: a changed set means stop."""
    f = fake(counts=(5, 6))
    assert script.main(_argv("--apply")) == 1
    out = capsys.readouterr().out
    assert "filtered set changed" in out


def test_schema_that_shows_up_late_is_waited_for(fake, clock, capsys):
    """The 27.11.2026 field failure: the index was there, the schema lagged.

    The build worked (index in place, filtered search 2-3 ms) and the script
    still shouted "does not name 'scope' as keyword". It must poll the
    finished state instead of reading it once.
    """
    f = fake(delay_gets=3)
    assert script.main(_argv("--apply")) == 0
    out = capsys.readouterr().out
    assert "payload_schema     : appeared after" in out, out
    assert "done: index on 'scope' is in place" in out, out
    # the build happened exactly once — waiting must not rebuild
    assert [m for m, url, _ in f.calls if url.endswith("/index?wait=true")] == ["PUT"]


def test_schema_that_never_shows_up_is_a_real_error(fake, clock, capsys):
    """Bounded wait, then a message that says what to do — no false alarm."""
    fake(becomes_keyword=False)
    assert script.main(_argv("--apply")) == 1
    out = capsys.readouterr().out
    assert "ERROR: after waiting" in out, out
    assert "still does not report" in out, out
    assert "idempotent" in out, out


def test_apply_aborts_when_the_schema_stays_not_keyword(fake, clock, capsys):
    f = fake(becomes_keyword=False)
    assert script.main(_argv("--apply")) == 1
    assert "still does not report" in capsys.readouterr().out


def test_existing_keyword_index_is_a_no_op(fake, capsys):
    f = fake(indexed=True)
    assert script.main(_argv("--apply")) == 0
    assert "PUT" not in f.methods()
    assert "nothing to do" in capsys.readouterr().out


def test_wrong_index_type_fails_closed_without_recreate(fake, capsys):
    """A ``text`` index on ``scope`` is a failure, not a WARNING + exit 0.

    Red before 28.11.2026: the script printed "WARNING: it is not a keyword
    index" and returned 0, so a human, a cron job and CI all read it as
    "everything is fine" while every filtered read kept paying for the wrong
    type. The message must name the found type, the needed type and the fix.
    """
    f = fake(indexed=True, initial_type="text")
    assert script.main(_argv("--apply")) == 1

    out = capsys.readouterr().out
    assert "ERROR" in out, out
    assert "'text'" in out, out
    assert "keyword" in out, out
    assert "--recreate" in out, out
    assert "PUT" not in f.methods()
    assert "DELETE" not in f.methods()


def test_wrong_index_type_fails_closed_on_a_dry_run_too(fake, capsys):
    """The read-only mode must not hide a wrong index either."""
    f = fake(indexed=True, initial_type="text")
    assert script.main(_argv()) == 1
    out = capsys.readouterr().out
    assert "'text'" in out and "--recreate" in out, out
    assert "PUT" not in f.methods()


def test_recreate_rebuilds_a_wrong_index_as_keyword(fake):
    """``--recreate`` is the offered repair, and it must actually repair."""
    f = fake(indexed=True, initial_type="text")
    assert script.main(_argv("--apply", "--recreate")) == 0

    index_calls = [m for m, url, _ in f.calls if "index" in url]
    assert index_calls == ["DELETE", "PUT"], f.calls
    assert f.put_body() == {"field_name": "scope", "field_schema": "keyword"}
    state = script.scope_index_state("http://localhost:6333", _COL)
    assert state is not None and script._index_is_keyword(state), state


def test_recreate_drops_then_rebuilds(fake):
    f = fake(indexed=True)
    assert script.main(_argv("--apply", "--recreate")) == 0
    methods = [m for m, url, _ in f.calls if "index" in url]
    assert methods == ["DELETE", "PUT"], f.calls


def test_failed_build_names_the_snapshot_to_restore_from(fake, capsys):
    f = fake(put_error="boom")
    assert script.main(_argv("--apply", "--recreate")) == 1
    out = capsys.readouterr().out
    assert "boom" in out
    assert "snap-2026-11-27" in out, (
        "nach einem misslungenen Rebuild muss das Skript sagen, womit man "
        "zurueckkommt: " + out)


def test_scope_filter_is_the_plugin_visibility_rule():
    """Same three conditions as ``_scope_should`` — one rule, two places."""
    import plugins.memory.nexus as nx

    conditions = nx._scope_should("hermes")
    if not all(hasattr(c, "model_dump") for c in conditions):
        pytest.skip("qdrant models without model_dump")
    expected = [c.model_dump(exclude_none=True) for c in conditions]
    assert script.scope_filter("hermes") == {"should": expected}
