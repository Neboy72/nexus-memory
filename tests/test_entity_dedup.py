"""Tests for roadmap 4.2 entity dedup detection."""

from __future__ import annotations

from nexus.sica import _detect_entity_duplicates, _synthesize_insights


def _entity_point(pid: str, etype: str, name: str, created: str = "2026-08-01T00:00:00Z") -> dict:
    return {
        "id": pid,
        "payload": {
            "category": "entity",
            "entity_type": etype,
            "entity_name": name,
            "content": f"{etype}: {name}",
            "created_at": created,
        },
    }


def _fact_point(pid: str) -> dict:
    return {"id": pid, "payload": {"category": "fact", "content": "plain fact"}}


def test_detects_case_and_whitespace_duplicates():
    points = [
        _entity_point("e1", "device", "ABL eMH3"),
        _entity_point("e2", "device", "abl  emh3", "2026-08-02T00:00:00Z"),
        _fact_point("f1"),
    ]
    issues = _detect_entity_duplicates(points)
    assert len(issues) == 1
    assert issues[0]["type"] == "entity_duplicate"
    assert issues[0]["id"] == "e2"  # newer loses
    assert issues[0]["keeper_id"] == "e1"  # oldest wins
    assert issues[0]["duplicate_ids"] == ["e2"]
    assert issues[0]["action"] == "merge_review"
    assert issues[0]["auto_fixable"] is False


def test_unique_entities_pass():
    points = [
        _entity_point("e1", "device", "ABL eMH3"),
        _entity_point("e2", "service", "ABL eMH3"),  # other type = other entity
        _entity_point("e3", "device", "Reev"),
    ]
    assert _detect_entity_duplicates(points) == []


def test_non_entity_ignored():
    assert _detect_entity_duplicates([_fact_point("f1"), _fact_point("f2")]) == []


def test_multiple_groups_each_reported():
    points = [
        _entity_point("a1", "device", "X"),
        _entity_point("a2", "device", "x"),
        _entity_point("b1", "service", "Y"),
        _entity_point("b2", "service", "y"),
    ]
    issues = _detect_entity_duplicates(points)
    assert len(issues) == 2
    keepers = {i["keeper_id"] for i in issues}
    assert keepers == {"a1", "b3" if False else "b1"}


def test_duplicate_group_yields_insight():
    points = [
        _entity_point("e1", "device", "ABL"),
        _entity_point("e2", "device", "abl"),
    ]
    issues = _detect_entity_duplicates(points)
    insights = _synthesize_insights(issues, points)
    assert len(insights) == 1
    assert insights[0]["type"] == "reflect_insight"
    assert insights[0]["focus_id"] == "e1"  # keeper = highest conf fallback order
