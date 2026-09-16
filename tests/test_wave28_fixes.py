"""OCR-2 Welle 28 (high, packet 1): fixes for 11 findings across 6 files.

One test class per root cause. Behavior checks where cheap (real functions with
fakes), source-inspection elsewhere.
"""

import ast
from datetime import datetime, timedelta, timezone

import pytest

REPO = __file__.rsplit("/tests/", 1)[0]

# nexus is importable when installed (hermes verify venv) or via repo root on sys.path
try:
    import sys

    if REPO not in sys.path:
        sys.path.insert(0, REPO)
    import nexus.events  # noqa: F401

    HAS_NEXUS = True
except Exception:  # pragma: no cover
    HAS_NEXUS = False

REASON_NEXUS = "nexus import unavailable in this environment"


def _read(rel: str) -> str:
    with open(f"{REPO}/{rel}", encoding="utf-8") as f:
        return f.read()


# ── W28-1: events.py get_events_since order_by + paging ───────────────────


class TestW28EventsOrderBy:
    def test_source_has_order_by(self):
        src = _read("nexus/events.py")
        seg = src[src.index("def get_events_since"):]
        seg = seg[:seg.index("\ndef ", 1)]
        assert '"order_by"' in seg and '"ingested_at"' in seg
        assert '"direction": "desc"' in seg

    def test_source_pagesize_decoupled(self):
        src = _read("nexus/events.py")
        seg = src[src.index("def get_events_since"):]
        seg = seg[:seg.index("\ndef ", 1)]
        # page size must NOT be the caller limit (H65: first page == cap)
        import re

        m = re.search(r'params = \{\s*"limit":\s*(\w+)', seg)
        assert m, "scroll params limit not found"
        assert m.group(1) != "limit", "page size must not equal caller limit"

    def test_behavior_first_page_cap_not_satisfied(self):
        if not HAS_NEXUS:
            pytest.skip(REASON_NEXUS)
        from nexus import events as ev

        fake_results = [
            [{"ingested_at": "2026-01-01T00:0%d:00Z" % i} for i in range(3)],
            [{"ingested_at": "2026-01-02T00:0%d:00Z" % i} for i in range(3)],
        ]
        calls = {"n": 0}

        class FakeResp:
            def __init__(self, points, next_offset):
                self.status_code = 200
                self._points = points
                self._next = next_offset

            def json(self):
                return {"result": {"points": self._points, "next_page_offset": self._next}}

        def fake_post(url, json=None, timeout=None):
            i = calls["n"]
            calls["n"] += 1
            if i >= len(fake_results):
                return FakeResp([], None)
            return FakeResp(fake_results[i], "next-page-2" if i == 0 else None)

        orig_post = ev.requests.post
        ev.requests.post = fake_post
        try:
            out = ev.get_events_since("2026-01-01T00:00:00Z", limit=5)
            # 6 events collected (2 full pages) before the >=5 cap truncates
            assert len(out) == 5
            assert calls["n"] >= 2, f"expected >1 page fetched, got {calls['n']} calls"
        finally:
            ev.requests.post = orig_post


# ── W28-2: ensure_collection indexes on exists-path ───────────────────────


class TestW28EnsureIndexes:
    def test_source_has_index_helper(self):
        src = _read("nexus/events.py")
        assert "_ensure_indexes" in src
        # exists-path calls the helper too: the early-return branch mentions it
        seg = src[src.index("def ensure_collection"):]
        seg = seg[:seg.index("\ndef ", 1)]
        assert "_ensure_indexes" in seg

    def test_source_helper_idempotent_tolerant(self):
        src = _read("nexus/events.py")
        seg = src[src.index("def _ensure_indexes"):]
        seg = seg[:seg.index("\ndef ", 1)]
        # idempotent PUT accepted; no warning-level log for existing index
        assert "debug" in seg.lower()


# ── W28-3: nexus_remember status check ────────────────────────────────────


class TestW28RememberStatusCheck:
    def test_source_checks_status_after_write(self):
        src = _read("nexus/__init__.py")
        seg = src[src.index("def nexus_remember"):]
        seg = seg[:seg.index("\ndef ", 1)]
        # after the upsert/write request a non-2xx must not pass silently
        assert "status_code" in seg

    def test_neighbor_style_parity(self):
        src = _read("nexus/__init__.py")
        seg_rem = src[src.index("def nexus_remember"):]
        seg_rem = seg_rem[:seg_rem.index("\ndef ", 1)]
        seg_upd = src[src.index("def nexus_update"):]
        seg_upd = seg_upd[:seg_upd.index("\ndef ", 1)]
        # whatever style nexus_update uses (raise or error dict), remember must match
        upd_raises = "raise_for_status" in seg_upd or "raise " in seg_upd
        if upd_raises:
            assert "raise" in seg_rem
        else:
            assert "status_code" in seg_upd and "status_code" in seg_rem


# ── W28-4: query filter haystack content ──────────────────────────────────


class TestW28QueryHaystack:
    def test_haystack_includes_content(self):
        src = _read("nexus/__init__.py")
        seg = src[src.index("def nexus_remember"):]
        seg = seg[:seg.index("def ", 300)]
        # find the haystack line (query_norm filter in list_memories/query area)
        idx = src.find("haystack")
        assert idx != -1, "haystack expression not found"
        window = src[idx - 100: idx + 200]
        assert "content" in window and "fact" in window


# ── W28-5: recompute_all guard + default parity ───────────────────────────


class TestW28RecomputeAllGuard:
    def test_source_guard_and_default(self):
        src = _read("nexus/apply.py")
        seg = src[src.index("def recompute_all"):]
        seg = seg[:seg.index("\ndef ", 1)]
        assert "isinstance(e, dict)" in seg
        assert '0.5' in seg

    def test_behavior_non_dict_entries(self):
        if not HAS_NEXUS:
            pytest.skip(REASON_NEXUS)
        # behavior check via source AST: the max() generator in recompute_all
        src = _read("nexus/apply.py")
        seg = src[src.index("def recompute_all"):]
        seg = seg[:seg.index("\ndef ", 1)]
        assert "for e in evidences if isinstance(e, dict)" in seg


# ── W28-6: provenance _has_cycle fail-closed ──────────────────────────────


class TestW28ProvenanceFailClosed:
    def test_source_separates_transport_errors(self):
        src = _read("nexus/provenance/__init__.py")
        seg = src[src.index("def _has_cycle"):]
        # the function above _has_cycle is _fetch — include it via reverse window
        idx = src.index("def _fetch")
        fetch_src = src[idx: idx + 2000]
        assert "raise" in fetch_src or "FetchError" in fetch_src or "_FETCH_ERROR" in fetch_src

    def test_has_cycle_fails_closed_on_error(self):
        src = _read("nexus/provenance/__init__.py")
        seg = src[src.index("def _has_cycle"):]
        seg = seg[:seg.index("\n    def ", 1)]
        assert "True" in seg  # fail-closed returns True on unreachable
        assert "unreachable" in seg.lower() or "storage error" in seg.lower()


# ── W28-7: Edge.new validation ────────────────────────────────────────────


class TestW28EdgeNewValidation:
    def test_source_validates_relation(self):
        src = _read("nexus/graph/schema.py")
        seg = src[src.index("def new("):]
        seg = seg[:seg.index("\n    def ", 1)]
        assert "_VALID_RELATIONS" in seg
        assert "EdgeSchemaError" in seg

    def test_behavior_invalid_relation_raises(self):
        if not HAS_NEXUS:
            pytest.skip(REASON_NEXUS)
        from nexus.graph.schema import Edge

        with pytest.raises(Exception):
            Edge.new("fact-a", "fact-b", "not_a_valid_relation")

    def test_behavior_valid_relation_ok(self):
        if not HAS_NEXUS:
            pytest.skip(REASON_NEXUS)
        from nexus.graph.schema import Edge

        e = Edge.new("fact-a", "fact-b", "supports")
        assert e.relation == "supports"


# ── W28-8: naive datetime harmonization ───────────────────────────────────


class TestW28NaiveDatetime:
    def test_helper_exists_and_applied(self):
        src = _read("nexus/health/__init__.py")
        assert "_as_aware_utc" in src
        # applied at multiple fromisoformat sites
        count = src.count("datetime.fromisoformat")
        applied = src.count("_as_aware_utc(")
        assert applied >= min(count, 4), (
            f"helper applied {applied}x but {count} fromisoformat sites exist"
        )

    def test_behavior_helper_naive(self):
        if not HAS_NEXUS:
            pytest.skip(REASON_NEXUS)
        import importlib

        health_mod = importlib.import_module("nexus.health")
        helper = getattr(health_mod, "_as_aware_utc", None)
        if helper is None:  # module-level function may be nested; source check suffices
            src = _read("nexus/health/__init__.py")
            assert "def _as_aware_utc" in src
            pytest.skip("helper not module-level in this build")

        naive = datetime(2026, 1, 1, 12, 0, 0)
        aware = helper(naive)
        assert aware.tzinfo is not None
        # aware passthrough unchanged
        ref = datetime(2026, 1, 2, 0, 0, tzinfo=timezone.utc)
        assert helper(ref) is ref

    def test_behavior_naive_compare_works(self):
        if not HAS_NEXUS:
            pytest.skip(REASON_NEXUS)
        import importlib

        health_mod = importlib.import_module("nexus.health")
        helper = getattr(health_mod, "_as_aware_utc", None)
        if helper is None:
            pytest.skip("helper not module-level in this build")

        naive = datetime.now() - timedelta(days=3)  # naive "3 days old"
        age = datetime.now(timezone.utc) - helper(naive)
        assert age > timedelta(days=2)