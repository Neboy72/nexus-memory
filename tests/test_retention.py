"""Tests for per-category retention policies (roadmap 2.2).

Covers: _detect_retention policies per category, default retention,
never-expire (None), the legacy _detect_stale_temp bridge, config
policies parsing, and the run_sica auto-patch of retention_expired
issues (delete).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from nexus.sica import (
    _detect_retention,
    _detect_stale_temp,
    _get_config,
    _apply_auto_patch,
    run_sica,
)


def _ts(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _pool() -> list:
    """5 real + 1 empty-content point (mirror of test_plugin_rerank._pool)."""
    from types import SimpleNamespace as NS

    def pt(pid, content, score=0.9):
        return NS(id=pid, payload={"id": pid, "content": content}, score=score)

    return [
        pt("p1", "completely unrelated text about gardening", 0.92),
        pt("p2", "fallback routing config for providers", 0.88),
        pt("p3", "routing: how to route fallback providers", 0.85),
        pt("p4", "cat pictures collection", 0.80),
        pt("p5", "deepseek routing table for fallback", 0.77),
        pt("p6", "", 0.75),
    ]


def _mk(pid: str, category: str, days_ago: float, confidence: float = 0.7) -> dict:
    ts = _ts(days_ago)
    return {
        "id": pid,
        "payload": {
            "category": category,
            "created_at": ts,
            "provenance": {"confidence": confidence},
        },
    }


# ---------------------------------------------------------------------------
# _get_config policies
# ---------------------------------------------------------------------------


class TestConfigPolicies:
    def test_default_policies(self, monkeypatch):
        for var in ("SICA_RETENTION_TEMP", "SICA_RETENTION_SESSION",
                    "SICA_DEFAULT_RETENTION_DAYS"):
            monkeypatch.delenv(var, raising=False)
        cfg = _get_config()
        # Legacy knob (SICA_STALE_TEMP_DAYS default 7) feeds temp by default.
        assert cfg["retention_policies"]["temp"] == 7
        assert cfg["retention_policies"]["session"] == 7
        assert cfg["default_retention_days"] is None

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("SICA_RETENTION_TEMP", "3")
        monkeypatch.setenv("SICA_RETENTION_SESSION", "14")
        monkeypatch.setenv("SICA_DEFAULT_RETENTION_DAYS", "365")
        cfg = _get_config()
        assert cfg["retention_policies"]["temp"] == 3
        assert cfg["retention_policies"]["session"] == 14
        assert cfg["default_retention_days"] == 365

    def test_bad_default_retention_falls_to_none(self, monkeypatch):
        monkeypatch.setenv("SICA_DEFAULT_RETENTION_DAYS", "not-a-number")
        cfg = _get_config()
        assert cfg["default_retention_days"] is None


# ---------------------------------------------------------------------------
# _detect_retention
# ---------------------------------------------------------------------------


class TestDetectRetention:
    def test_temp_expired_under_default_policy(self):
        points = [_mk("t1", "temp", days_ago=3), _mk("t2", "temp", days_ago=0.2)]
        issues = _detect_retention(points, policies={"temp": 1})
        ids = [i["id"] for i in issues]
        assert ids == ["t1"]
        assert issues[0]["type"] == "retention_expired"
        assert issues[0]["action"] == "delete"
        assert issues[0]["auto_fixable"] is True

    def test_session_policy_boundary(self):
        # 6.9 days: inside the 7-day session window. 7.1: expired.
        points = [
            _mk("s1", "session", days_ago=6.9),
            _mk("s2", "session", days_ago=7.5),
        ]
        issues = _detect_retention(points, policies={"session": 7})
        assert [i["id"] for i in issues] == ["s2"]

    def test_unlisted_category_with_no_default_never_expires(self):
        points = [_mk("f1", "fact", days_ago=4000)]
        issues = _detect_retention(points, policies={"temp": 1})
        assert issues == []

    def test_default_days_applies_to_unlisted(self):
        points = [
            _mk("p1", "preference", days_ago=100),
            _mk("p2", "preference", days_ago=10),
        ]
        issues = _detect_retention(
            points, policies={"temp": 1}, default_days=30
        )
        assert [i["id"] for i in issues] == ["p1"]

    def test_missing_timestamp_never_deleted(self):
        points = [
            {"id": "x1", "payload": {"category": "temp", "created_at": ""}},
            {"id": "x2", "payload": {"category": "temp"}},
        ]
        issues = _detect_retention(points, policies={"temp": 1})
        assert issues == []

    def test_unparseable_timestamp_skipped_not_deleted(self):
        points = [
            {"id": "x1", "payload": {"category": "temp", "created_at": "garbage"}},
        ]
        issues = _detect_retention(points, policies={"temp": 1})
        assert issues == []

    def test_future_timestamp_not_expired(self):
        # Clock-skew protection: future timestamps age as 0, never negative.
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        points = [{"id": "t1", "payload": {"category": "temp", "created_at": future}}]
        issues = _detect_retention(points, policies={"temp": 1})
        assert issues == []

    def test_naive_timestamp_treated_as_utc(self):
        # Naive datetime (no tz) 10 days old — must use UTC, not local.
        naive = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        points = [{"id": "t1", "payload": {"category": "temp", "created_at": naive}}]
        issues = _detect_retention(points, policies={"temp": 7})
        assert len(issues) == 1


# ---------------------------------------------------------------------------
# legacy bridge
# ---------------------------------------------------------------------------


class TestLegacyStaleTemp:
    def test_detect_stale_temp_uses_retention(self):
        points = [_mk("t1", "temp", days_ago=10), _mk("f1", "fact", days_ago=400)]
        issues = _detect_stale_temp(points, stale_temp_days=7)
        assert [i["id"] for i in issues] == ["t1"]
        assert issues[0]["type"] == "retention_expired"

    def test_detect_stale_temp_fresh_temp_passes(self):
        points = [_mk("t1", "temp", days_ago=1)]
        assert _detect_stale_temp(points, stale_temp_days=7) == []


# ---------------------------------------------------------------------------
# auto-patch + run_sica integration
# ---------------------------------------------------------------------------


class TestAutoPatchRetention:
    def test_apply_auto_patch_deletes_retention_expired(self):
        client = MagicMock()
        issue = {
            "id": "t1", "type": "retention_expired", "action": "delete",
            "category": "temp",
        }
        patch = _apply_auto_patch(client, "coll-x", issue)
        assert patch == {"id": "t1", "action": "deleted", "type": "retention_expired"}
        client.delete.assert_called_once()

    def test_apply_auto_patch_ignores_review_issues(self):
        client = MagicMock()
        issue = {"id": "p1", "type": "low_confidence", "action": "review"}
        assert _apply_auto_patch(client, "coll-x", issue) is None
        client.delete.assert_not_called()


class TestRunSicaRetention:
    def test_run_sica_purges_expired_and_keeps_fresh(self, monkeypatch):
        """E2E: run_sica with policies deletes only expired points."""
        from nexus.sica import run_sica

        monkeypatch.setattr(
            "nexus.sica._get_config",
            lambda: {
                "collection": "c",
                "qdrant_url": "http://localhost:6333",
                "low_confidence_threshold": 0.5,
                "stale_temp_days": 7,
                "retention_policies": {"temp": 1},
                "default_retention_days": None,
                "max_suggestions": 10,
            },
        )
        now_iso = datetime.now(timezone.utc).isoformat()
        points = [
            {"id": "old", "payload": {"category": "temp", "created_at": _ts(5)}},
            {"id": "fresh", "payload": {"category": "temp", "created_at": now_iso}},
        ]
        client = MagicMock()
        # _scroll_all reads .id/.payload off each result point.
        scrolled = [SimpleNamespace(id=pt["id"], payload=pt["payload"]) for pt in points]
        client.scroll.side_effect = lambda *a, **k: (scrolled, None)
        result = run_sica(client=client, collection="c", auto_patch=True)
        deleted_ids = [
            c.kwargs["points_selector"].points[0]
            for c in client.delete.call_args_list
        ]
        assert deleted_ids == ["old"]
        assert len(result.auto_patches) == 1
        assert result.auto_patches[0]["type"] == "retention_expired"

# ---------------------------------------------------------------------------
# Review-fix tests (round 2: blocker legacy-knob + edge cases)
# ---------------------------------------------------------------------------


class TestLegacyKnobFallback:
    """SICA_STALE_TEMP_DAYS must stay effective (blocker from review)."""

    def test_legacy_knob_feeds_temp_policy(self, monkeypatch):
        monkeypatch.delenv("SICA_RETENTION_TEMP", raising=False)
        monkeypatch.setenv("SICA_STALE_TEMP_DAYS", "30")
        cfg = _get_config()
        assert cfg["retention_policies"]["temp"] == 30
        # run_sica inherits the policy: 5-day temp must survive.
        import nexus.sica as sica_mod

        monkeypatch.setattr(
            "nexus.sica._get_config",
            lambda: {
                "collection": "c",
                "qdrant_url": "http://localhost:6333",
                "low_confidence_threshold": 0.5,
                "stale_temp_days": 30,
                "retention_policies": {"temp": 30},
                "default_retention_days": None,
                "max_suggestions": 10,
            },
        )
        client = MagicMock()
        scrolled = [SimpleNamespace(id="t1", payload=_mk("t1", "temp", 5)["payload"])]
        client.scroll.side_effect = lambda *a, **k: (scrolled, None)
        result = run_sica(client=client, collection="c", auto_patch=True)
        client.delete.assert_not_called()  # 5 days < 30 → nothing expired

    def test_new_env_beats_legacy_knob(self, monkeypatch):
        monkeypatch.setenv("SICA_STALE_TEMP_DAYS", "30")
        monkeypatch.setenv("SICA_RETENTION_TEMP", "2")
        cfg = _get_config()
        assert cfg["retention_policies"]["temp"] == 2


class TestGenericCategoryEnv:
    def test_extra_category_env_parsed(self, monkeypatch):
        monkeypatch.setenv("SICA_RETENTION_PREFERENCE", "90")
        cfg = _get_config()
        assert cfg["retention_policies"]["preference"] == 90

    def test_builtin_not_overridden_by_generic_loop(self, monkeypatch):
        # SICA_RETENTION_TEMP is parsed by its dedicated key; the generic
        # loop skips temp/session so priorities stay unambiguous.
        monkeypatch.setenv("SICA_RETENTION_TEMP", "2")
        cfg = _get_config()
        assert cfg["retention_policies"]["temp"] == 2
        # and the generic loop did not add a duplicate key
        assert list(cfg["retention_policies"].keys()).count("temp") == 1


class TestConfigFailOpen:
    def test_garbage_env_values_do_not_crash(self, monkeypatch):
        monkeypatch.setenv("SICA_LOW_CONFIDENCE", "abc")
        monkeypatch.setenv("SICA_MAX_SUGGESTIONS", "xyz")
        monkeypatch.setenv("SICA_RETENTION_SESSION", "junk")
        cfg = _get_config()
        assert cfg["low_confidence_threshold"] == 0.5
        assert cfg["max_suggestions"] == 10
        assert cfg["retention_policies"]["session"] == 7


class TestRerankerEdgeCases:
    def test_empty_content_inside_pool_survives(self, monkeypatch):
        """A reranker that drops empty-content points must not lose them."""
        import nexus_memory.reranker as rr

        monkeypatch.setattr(
            rr, "_rerank_local", lambda q, results: sorted(results, key=lambda r: r["_idx"], reverse=True)
        )
        pts = _pool()
        out = rr.rerank_points(
            "q", pts, reranker="cross-encoder", pool_k=6  # p6 (empty) INSIDE pool
        )
        ids = [p.id for p in out]
        assert set(ids) == {p.id for p in pts}  # nothing lost
        assert out[-1].id == "p6"  # dropped pool candidates appended last

    def test_voyage_without_key_falls_back(self):
        from nexus_memory.reranker import rerank_points

        pts = _pool()
        out = rerank_points("q", pts, reranker="voyage", voyage_api_key=None)
        assert [p.id for p in out] == [p.id for p in pts]

    def test_voyage_http_error_raises_then_fail_open(self, monkeypatch):
        import nexus_memory.reranker as rr

        class FakeResp:
            status_code = 500
            def json(self):
                return {"data": []}

        fake = MagicMock()
        fake.post.return_value = FakeResp()
        monkeypatch.setattr(rr, "_requests", fake)
        pts = _pool()[:3]
        out = rr.rerank_points("q", pts, reranker="voyage", voyage_api_key="k")
        assert [p.id for p in out] == [p.id for p in pts]  # fail-open order
