"""Guards for ``scripts/dedupe_content_groups.py``.

The tool deletes points from a live collection, and its whole point is that it
can do that without damaging the graph. So the tests here are not about pretty
output; they pin the four things that must never be wrong:

  1. A dry run writes NOTHING (not even a snapshot).
  2. Without a snapshot with name+size+checksum nothing is written at all.
  3. Every reference to a removed copy — on a foreign point, in either edge
     shape — is moved to the keeper BEFORE anything is deleted.
  4. If a reference is still there afterwards, the run stops before the
     deletion instead of leaving a dangling edge behind.

The HTTP layer is an in-memory Qdrant: the real code runs, only the network is
replaced.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT_PATH = (Path(__file__).resolve().parents[1]
                / "scripts" / "dedupe_content_groups.py")


def _load_script():
    spec = importlib.util.spec_from_file_location("dedupe_content_groups",
                                                  _SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script = _load_script()

KEEPER = "1001"
COPY_B = "1002"
COPY_C = "1003"
CONTENT = "dieselbe Seite, byte-gleich"


def _edge(edge_id, target, relation="references", source=KEEPER):
    return {"edge_id": edge_id, "target_fact_id": target, "relation": relation,
            "status": "active",
            "metadata": {"confidence": 0.9, "source_memory_id": source}}


def _point(edges=(), content=CONTENT, created="2026-07-22", **extra):
    payload = {"content": content, "created": created, "edges": list(edges)}
    payload.update(extra)
    return payload


def _point_id(raw):
    text = str(raw)
    return int(text) if text.isdigit() else text


class FakeQdrant:
    """In-memory Qdrant for the endpoints the tool uses."""

    def __init__(self, points):
        self.points = {str(k): json.loads(json.dumps(v)) for k, v in points.items()}
        self.calls: list[tuple[str, str, dict | None]] = []
        self.snapshot_error = None
        self.snapshot = {"name": "nexus_hf-2026-11-27.snapshot",
                         "size": 124_000_000, "checksum": "abc123"}
        self.ignore_payload_writes = False

    # helpers for assertions
    def methods_used(self):
        return [m for m, _u, _b in self.calls]

    def wrote_payload(self):
        return any(u.endswith("/points/payload?wait=true") for _m, u, _b in self.calls)

    def deleted(self):
        return any("/points/delete" in u for _m, u, _b in self.calls)

    def __call__(self, method, url, body=None, timeout=600.0):
        self.calls.append((method, url, body))
        body = body or {}
        if url.endswith("/snapshots"):
            if self.snapshot_error:
                raise RuntimeError(self.snapshot_error)
            return {"result": self.snapshot}
        if url.endswith("/points/scroll"):
            ids = sorted(self.points,
                         key=lambda p: (0, int(p)) if p.isdigit() else (1, p))
            offset = int(body.get("offset") or 0)
            limit = int(body.get("limit") or 1000)
            page = ids[offset:offset + limit]
            nxt = offset + limit if offset + limit < len(ids) else None
            return {"result": {
                "points": [{"id": _point_id(p),
                            "payload": {"edges": self.points[p].get("edges") or []}}
                           for p in page],
                "next_page_offset": nxt}}
        if url.endswith("/points/count"):
            return {"result": {"count": len(self.points)}}
        if url.endswith("/points") and method == "POST":
            return {"result": [{"id": _point_id(i), "payload": self.points[str(i)]}
                               for i in body.get("ids") or []
                               if str(i) in self.points]}
        if url.endswith("/points/payload?wait=true"):
            if not self.ignore_payload_writes:
                for i in body.get("points") or []:
                    self.points[str(i)].update(body.get("payload") or {})
            return {"result": True}
        if "/points/delete" in url:
            for i in body.get("points") or []:
                self.points.pop(str(i), None)
            return {"result": True}
        raise AssertionError(f"unexpected call: {method} {url}")


@pytest.fixture()
def qdrant(monkeypatch):
    def install(points):
        fake = FakeQdrant(points)
        monkeypatch.setattr(script, "_request", fake)
        return fake
    return install


@pytest.fixture()
def report(tmp_path):
    def write(groups):
        path = tmp_path / "health-2026-11-27.json"
        path.write_text(json.dumps({
            "collection": "nexus_hf",
            "total_points": 4,
            "duplicate_summary": {"groups": len(groups), "excess_copies": 0},
            "duplicate_groups": [{"count": len(g), "ids": list(g)} for g in groups],
        }), encoding="utf-8")
        return path
    return write


def _argv(report_path, *extra):
    return ["--report", str(report_path), "--url", "http://test",
            "--collection", "nexus_hf", *extra]


def _base_points():
    """Three copies of one page, plus points that reference them.

    The reference counts are deliberate: copy 1001 is referenced twice, copy
    1002 once, copy 1003 not at all — so the keeper rule has a clear answer and
    the test does not silently depend on a tie-break.
    """
    return {
        KEEPER: _point(edges=[_edge("k1", "uuid-1")], doc_id=4050),
        COPY_B: _point(edges=[_edge("b1", "uuid-2", "mentions", source=COPY_B)],
                       doc_id=4148),
        COPY_C: _point(edges=[_edge("c1", "uuid-2", "mentions", source=COPY_C),
                              _edge("c2", KEEPER, "references", source=COPY_C)],
                       doc_id=4188),
        "2001": {"content": "ein ganz anderer Punkt",
                 "edges": [_edge("e1", "uuid-9", "references", source=COPY_B)]},
        "2002": {"content": "noch ein Punkt",
                 "edges": [_edge("e2", "uuid-8", "references", source=KEEPER)]},
        "2003": {"content": "und einer",
                 "edges": [_edge("e3", KEEPER, "references", source="2003")]},
    }


# ── 1. dry run ────────────────────────────────────────────────────────────

def test_dry_run_writes_nothing(qdrant, report, capsys):
    fake = qdrant(_base_points())
    assert script.main(_argv(report([[KEEPER, COPY_B, COPY_C]]))) == 0
    assert fake.wrote_payload() is False
    assert fake.deleted() is False
    assert not any(u.endswith("/snapshots") for _m, u, _b in fake.calls)
    out = capsys.readouterr().out
    assert "dry run: nothing written" in out
    # and it says what it WOULD do
    assert "points to delete   : 2" in out, out
    assert "edges rerouted     : 1" in out, out


def test_dry_run_shows_the_keeper_and_the_rerouted_edges(qdrant, report, capsys):
    qdrant(_base_points())
    assert script.main(_argv(report([[KEEPER, COPY_B, COPY_C]]))) == 0
    out = capsys.readouterr().out
    assert "keeper=%s" % KEEPER in out, out


# ── 2. content check ──────────────────────────────────────────────────────

def test_group_with_different_content_is_skipped(qdrant, report, capsys):
    points = _base_points()
    points[COPY_C]["content"] = CONTENT + " (anders)"
    fake = qdrant(points)

    assert script.main(_argv(report([[KEEPER, COPY_B, COPY_C]]), "--apply")) == 0
    out = capsys.readouterr().out
    assert "SKIP group" in out, out
    assert "not byte-identical" in out, out
    assert fake.deleted() is False
    assert fake.wrote_payload() is False


def test_group_with_a_missing_point_is_skipped(qdrant, report, capsys):
    fake = qdrant(_base_points())
    assert script.main(_argv(report([[KEEPER, COPY_B, "404404"]]), "--apply")) == 0
    assert "SKIP group" in capsys.readouterr().out
    assert fake.deleted() is False


# ── 3. the snapshot gate ──────────────────────────────────────────────────

def test_no_snapshot_no_write(qdrant, report, capsys):
    fake = qdrant(_base_points())
    fake.snapshot_error = "disk full"
    assert script.main(_argv(report([[KEEPER, COPY_B, COPY_C]]), "--apply")) == 1
    out = capsys.readouterr().out
    assert "snapshot failed, nothing is written" in out, out
    assert "disk full" in out, out
    assert fake.wrote_payload() is False
    assert fake.deleted() is False


def test_snapshot_without_checksum_writes_nothing(qdrant, report, capsys):
    fake = qdrant(_base_points())
    fake.snapshot = {"name": "snap", "size": 1}
    assert script.main(_argv(report([[KEEPER, COPY_B, COPY_C]]), "--apply")) == 1
    assert "without name/size/checksum" in capsys.readouterr().out
    assert fake.wrote_payload() is False


# ── 4. the edges move before the point goes ───────────────────────────────

def test_incoming_edge_on_a_foreign_point_is_rerouted(qdrant, report):
    fake = qdrant(_base_points())
    assert script.main(_argv(report([[KEEPER, COPY_B, COPY_C]]), "--apply")) == 0

    foreign = fake.points["2001"]["edges"][0]
    assert foreign["metadata"]["source_memory_id"] == KEEPER, foreign
    assert foreign["target_fact_id"] == "uuid-9", "das Ziel bleibt unberührt"
    assert foreign["edge_id"] == "e1", "die Kante bleibt dieselbe"


def test_outgoing_edges_of_the_copies_are_carried_to_the_keeper(qdrant, report):
    fake = qdrant(_base_points())
    assert script.main(_argv(report([[KEEPER, COPY_B, COPY_C]]), "--apply")) == 0

    keeper_edges = fake.points[KEEPER]["edges"]
    ids = [e["edge_id"] for e in keeper_edges]
    assert "k1" in ids and "b1" in ids, ids
    carried = [e for e in keeper_edges if e["edge_id"] == "b1"][0]
    assert carried["metadata"]["source_memory_id"] == KEEPER, (
        "eine mitgenommene Kante gehoert danach dem Keeper: %s" % carried)
    # c1 is the same statement (mentions uuid-2) that b1 already makes
    assert "c1" not in ids, ids


def test_self_edge_is_dropped_instead_of_carried(qdrant, report):
    """c2 targets the keeper and belongs to a copy: keeper -> keeper is nothing."""
    fake = qdrant(_base_points())
    assert script.main(_argv(report([[KEEPER, COPY_B, COPY_C]]), "--apply")) == 0
    ids = [e["edge_id"] for e in fake.points[KEEPER]["edges"]]
    assert "c2" not in ids, ids


def test_kept_points_lose_no_other_field(qdrant, report):
    fake = qdrant(_base_points())
    assert script.main(_argv(report([[KEEPER, COPY_B, COPY_C]]), "--apply")) == 0
    assert fake.points[KEEPER]["doc_id"] == 4050
    assert fake.points[KEEPER]["content"] == CONTENT


def test_points_are_deleted_only_after_the_reroute(qdrant, report):
    fake = qdrant(_base_points())
    assert script.main(_argv(report([[KEEPER, COPY_B, COPY_C]]), "--apply")) == 0
    urls = [u for _m, u, _b in fake.calls]
    first_payload = min(i for i, u in enumerate(urls) if "/points/payload" in u)
    first_delete = min(i for i, u in enumerate(urls) if "/points/delete" in u)
    assert first_payload < first_delete, urls


def test_a_remaining_reference_stops_the_deletion(qdrant, report, capsys):
    """The re-scan is the safety net: if a write did not land, do not delete."""
    fake = qdrant(_base_points())
    fake.ignore_payload_writes = True
    assert script.main(_argv(report([[KEEPER, COPY_B, COPY_C]]), "--apply")) == 1
    out = capsys.readouterr().out
    assert "still point at removed points" in out, out
    assert "nothing deleted" in out, out
    assert fake.deleted() is False


# ── 5. the keeper rule ────────────────────────────────────────────────────

def test_keeper_is_the_most_referenced_member():
    payloads = {p: {"content": "x", "created": "2026-07-22"}
                for p in (KEEPER, COPY_B, COPY_C)}
    incoming = {KEEPER: 1, COPY_B: 9, COPY_C: 2}
    assert script.choose_keeper([KEEPER, COPY_B, COPY_C], incoming, payloads) == COPY_B


def test_keeper_tie_goes_to_the_oldest_created():
    payloads = {
        KEEPER: {"content": "x", "created": "2026-09-01"},
        COPY_B: {"content": "x", "created": "2026-07-22"},
        COPY_C: {"content": "x", "created": "2026-08-01"},
    }
    incoming = {KEEPER: 0, COPY_B: 0, COPY_C: 0}
    assert script.choose_keeper([KEEPER, COPY_B, COPY_C], incoming, payloads) == COPY_B


def test_keeper_tie_on_created_goes_to_the_lowest_id():
    payloads = {p: {"content": "x", "created": "2026-07-22"}
                for p in ("1003", "1001", "1002")}
    incoming = {"1001": 0, "1002": 0, "1003": 0}
    assert script.choose_keeper(["1003", "1001", "1002"], incoming, payloads) == "1001"


# ── 6. what the run reports afterwards ────────────────────────────────────

def test_apply_reports_the_point_count_before_and_after(qdrant, report, capsys):
    fake = qdrant(_base_points())
    assert script.main(_argv(report([[KEEPER, COPY_B, COPY_C]]), "--apply")) == 0
    out = capsys.readouterr().out
    assert "points before      : 6" in out, out
    assert "points after       : 4 (expected 4)" in out, out
    assert sorted(fake.points) == [KEEPER, "2001", "2002", "2003"]
    assert "done: 2 copies removed" in out, out


def test_dangling_target_inside_the_group_is_moved_to_the_keeper():
    """Defensive: a target inside the group must not survive as a dead link."""
    payloads = {
        KEEPER: {"content": "x", "created": "2026-07-22", "edges": []},
        COPY_B: {"content": "x", "created": "2026-07-22", "edges": []},
    }
    edges = {
        "3001": [_edge("z1", COPY_B, "references", source="3001")],
        "3002": [_edge("z2", KEEPER, "references", source="3002")],
    }
    plan = script.plan_group([KEEPER, COPY_B], payloads, edges)
    assert plan.ok
    assert plan.keeper == KEEPER          # tie on references -> lowest id
    assert plan.removed == [COPY_B]
    assert plan.rewrites["3001"][0]["target_fact_id"] == KEEPER
    assert plan.rewrites["3001"][0]["metadata"]["source_memory_id"] == "3001"
    assert "3002" not in plan.rewrites, "ein Ziel auf dem Keeper bleibt"


def test_latest_report_picks_the_newest_file(tmp_path, monkeypatch):
    (tmp_path / "health-2026-11-26.json").write_text("{}", encoding="utf-8")
    newest = tmp_path / "health-2026-11-27.json"
    newest.write_text("{}", encoding="utf-8")
    assert script.latest_report(str(tmp_path)) == str(newest)


def test_missing_report_is_a_clear_error(tmp_path, capsys):
    assert script.main(["--report", str(tmp_path / "nope.json"),
                        "--url", "http://test"]) == 1
    assert "no health report found" in capsys.readouterr().out


def test_one_point_referencing_two_groups_is_written_once_with_both(qdrant, report):
    """Ten points in the live collection reference two groups at once.

    Written group by group, the second write would come from the same old
    snapshot and undo the first. One write per point, every mapping at once.
    """
    points = {
        "1001": _point(content="Gruppe A", edges=[]),
        "1002": _point(content="Gruppe A", edges=[]),
        "2001": _point(content="Gruppe B", edges=[]),
        "2002": _point(content="Gruppe B", edges=[]),
        # two references each make 1001 and 2001 the keepers, so 1002 and 2002
        # are the ones that get deleted
        "3002": {"content": "verweist auf A", "edges": [
            _edge("x3", "uuid-m", "references", source="1001"),
            _edge("x4", "uuid-n", "references", source="1001")]},
        "3003": {"content": "verweist auf B", "edges": [
            _edge("x5", "uuid-o", "references", source="2001"),
            _edge("x6", "uuid-p", "references", source="2001")]},
        "3001": {"content": "fremder Punkt", "edges": [
            _edge("x1", "uuid-x", "references", source="1002"),
            _edge("x2", "uuid-y", "references", source="2002")]},
    }
    fake = qdrant(points)
    assert script.main(_argv(report([["1001", "1002"], ["2001", "2002"]]),
                             "--apply")) == 0

    edges = fake.points["3001"]["edges"]
    assert [e["metadata"]["source_memory_id"] for e in edges] == ["1001", "2001"]
    writes = [b for _m, url, b in fake.calls
              if url.endswith("/points/payload?wait=true")
              and 3001 in (b or {}).get("points", [])]
    assert len(writes) == 1, f"ein Punkt, ein Schreibvorgang: {len(writes)}"


def test_a_point_in_two_groups_stops_the_run(qdrant, report, capsys):
    """A contradictory report must not produce a plan that keeps and deletes."""
    fake = qdrant(_base_points())
    assert script.main(_argv(report([[KEEPER, COPY_B], [KEEPER, COPY_C]]))) == 1
    assert "more than one group" in capsys.readouterr().out
    assert fake.wrote_payload() is False
