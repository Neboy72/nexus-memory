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

    def __init__(self, *, indexed=False, snapshot=None, snapshot_error=None,
                 put_error=None, counts=(5, 5), becomes_keyword=True):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.indexed = indexed
        self.snapshot = ({"name": "snap-2026-11-27", "size": 124_000_000,
                          "checksum": "deadbeef"} if snapshot is None
                         else snapshot)
        self.snapshot_error = snapshot_error
        self.put_error = put_error
        self.counts = list(counts)
        self.becomes_keyword = becomes_keyword

    def _info(self) -> dict:
        schema = ({"scope": {"data_type": "keyword"}} if self.indexed else {})
        return {"result": {
            "points_count": 68602,
            "config": {"params": {"on_disk_payload": True,
                                  "payload_schema": schema},
                       "hnsw_config": {"full_scan_threshold": 10000}},
        }}

    def __call__(self, method, url, body=None, timeout=600.0):
        self.calls.append((method, url, body))
        if method == "GET":
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
            self.indexed = self.becomes_keyword
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


def test_apply_aborts_when_the_schema_stays_not_keyword(fake, capsys):
    f = fake(becomes_keyword=False)
    assert script.main(_argv("--apply")) == 1
    assert "does not name 'scope' as keyword" in capsys.readouterr().out


def test_existing_keyword_index_is_a_no_op(fake, capsys):
    f = fake(indexed=True)
    assert script.main(_argv("--apply")) == 0
    assert "PUT" not in f.methods()
    assert "nothing to do" in capsys.readouterr().out


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
