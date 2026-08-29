"""Tests for roadmap 2.1 reflect insights: _synthesize_insights.

SICA's Reflect phase synthesizes ONE deterministic insight per
contradiction group instead of only emitting review suggestions.
"""

from __future__ import annotations

from nexus.sica import SICAResult, _synthesize_insights


def _issue(pid: str, target_id: str, confidence: float = 0.5) -> dict:
    return {
        "id": pid,
        "type": "contradiction",
        "detail": f"Contradicts {target_id[:8]}",
        "auto_fixable": False,
        "action": "review",
        "category": "fact",
        "confidence": confidence,
        "target_id": target_id,
    }


def _point(pid: str, content: str, category: str = "fact") -> dict:
    return {
        "id": pid,
        "payload": {
            "category": category,
            "content": content,
            "created_at": "2026-08-01T00:00:00Z",
        },
    }


def test_synthesize_one_insight_per_contradiction_group():
    issues = [
        _issue("a1", "tgt-1", 0.8),
        _issue("a2", "tgt-1", 0.3),
        _issue("a3", "tgt-2", 0.9),
    ]
    points = [
        _point("a1", "Kiosha uses voyage-4"),
        _point("a2", "Kiosha uses voyage-3"),
        _point("a3", "Mac Mini has 32GB"),
    ]
    insights = _synthesize_insights(issues, points)
    # 2 target groups -> 2 insights (one per group)
    assert len(insights) == 2
    targets = {i["target_id"] for i in insights}
    assert targets == {"tgt-1", "tgt-2"}


def test_winner_is_highest_confidence():
    issues = [_issue("low", "t", 0.2), _issue("high", "t", 0.9)]
    points = [_point("low", "old claim"), _point("high", "new claim")]
    insights = _synthesize_insights(issues, points)
    assert insights[0]["focus_id"] == "high"
    assert insights[0]["winner_confidence"] == 0.9
    assert set(insights[0]["involved_ids"]) == {"low", "high"}


def test_non_contradiction_issues_are_ignored():
    issues = [
        {"id": "x", "type": "retention_expired", "detail": "old"},
        _issue("c1", "t", 0.5),
    ]
    points = [_point("c1", "claim"), _point("x", "stale")]
    insights = _synthesize_insights(issues, points)
    assert len(insights) == 1


def test_empty_issues_return_empty():
    assert _synthesize_insights([], []) == []


def _empty_result() -> SICAResult:
    return SICAResult()
