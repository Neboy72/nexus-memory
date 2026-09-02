"""Tests für memory_dynamics (Nexus v0.15 Memory-Dynamik)."""
import math
from datetime import datetime, timedelta, timezone

import pytest

from nexus_memory.memory_dynamics import (
    DECAY_FLOOR,
    SALIENCE_IMMUNE,
    access_update_payload,
    decay_factor,
    effective_score,
    is_salient,
    months_since,
    parse_ts,
    reinforcement_factor,
)

NOW = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)


# ---------- F1: Reinforcement ----------

def test_access_update_increments():
    p = {"use_count": 5}
    upd = access_update_payload(p, now=NOW)
    assert upd["use_count"] == 6
    assert parse_ts(upd["last_accessed"]) == NOW


def test_access_update_from_missing_field():
    upd = access_update_payload({}, now=NOW)
    assert upd["use_count"] == 1


def test_access_update_garbage_value():
    upd = access_update_payload({"use_count": "kaputt"}, now=NOW)
    assert upd["use_count"] == 1


# ---------- F3: Decay ----------

def test_fresh_memory_no_decay():
    p = {"last_accessed": NOW.isoformat()}
    assert decay_factor(p, now=NOW) == 1.0


def test_decay_after_six_months():
    old = NOW - timedelta(days=180)
    p = {"last_accessed": old.isoformat()}
    # 6 Monate * 5% = 0.7
    assert abs(decay_factor(p, now=NOW) - 0.70) < 0.01


def test_decay_floor():
    old = NOW - timedelta(days=3650)  # 10 Jahre
    p = {"last_accessed": old.isoformat()}
    assert decay_factor(p, now=NOW) == DECAY_FLOOR


def test_salience_immune():
    old = NOW - timedelta(days=365)
    p = {"last_accessed": old.isoformat(), "salience": 0.95}
    assert decay_factor(p, now=NOW) == 1.0
    assert is_salient(p) is True


def test_high_salience_boundary():
    p = {"last_accessed": NOW.isoformat(), "salience": SALIENCE_IMMUNE}
    assert decay_factor(p, now=NOW) == 1.0


def test_missing_timestamp_no_decay():
    # Kein last_accessed/created_at → nicht verfallen (sicherer Default)
    assert decay_factor({}, now=NOW) == 1.0


# ---------- F1 x F3: effective_score ----------

def test_effective_score_oft_genutzt_rankt_hoeher():
    """Kern-Test: gleicher base, einer oft genutzt → höherer Score."""
    base = 0.8
    selten = effective_score(base, {"use_count": 0, "last_accessed": NOW.isoformat()}, NOW)
    oft = effective_score(base, {"use_count": 50, "last_accessed": NOW.isoformat()}, NOW)
    assert oft > selten  # oft genutzt = Verstärkungs-Boost schlägt Faktor 1.0


def test_effective_score_vergessen_sinkt():
    base = 0.9
    frisch = effective_score(base, {"last_accessed": NOW.isoformat()}, NOW)
    alt = effective_score(base, {"last_accessed": (NOW - timedelta(days=365)).isoformat()}, NOW)
    assert frisch > alt
    assert alt >= base * DECAY_FLOOR - 0.001


def test_effective_score_gold_fakt_beat_vergessen():
    """Ein oft-genutzter, alter Fakt schlägt einen vergessenen gleich-guten."""
    base = 0.8
    gold = effective_score(base, {"use_count": 100, "last_accessed": (NOW - timedelta(days=30)).isoformat()}, NOW)
    vergess = effective_score(base, {"use_count": 0, "last_accessed": (NOW - timedelta(days=365)).isoformat()}, NOW)
    assert gold > vergess


# ---------- Backward compatibility ----------

def test_alte_memories_ohne_felder_funktionieren():
    base = 0.8
    s = effective_score(base, {}, NOW)
    assert 0 < s <= base  # use=0 → 1.0 Boost, last_accessed fehlt → kein Decay


# ---------- Robustheit ----------

def test_parse_ts_variants():
    assert parse_ts("2026-09-03T12:00:00Z") is not None
    assert parse_ts("2026-09-03T12:00:00+00:00") is not None
    assert parse_ts(1756900000) is None  # int → None (kein Crash)
    assert parse_ts(None) is None
    assert parse_ts("Müll") is None


def test_reinforcement_log_cap():
    p = {"use_count": 10**9}
    f = reinforcement_factor(p)
    assert f <= 1.0 + 3.0  # gedeckelt


# ---------- Review-Fixes (03.09.2026, Claude-Review + Auto-Review) ----------

def test_access_update_accepts_z_suffix_string():
    """Review-Fix: 'Z'-Strings crashten vorher via bare fromisoformat (Py<3.11)."""
    upd = access_update_payload({"use_count": 2}, now="2026-09-03T12:00:00Z")
    assert upd["use_count"] == 3
    assert parse_ts(upd["last_accessed"]) == NOW


def test_access_update_invalid_string_falls_back():
    """Ungültiger String → jetzt()-Fallback statt Crash."""
    upd = access_update_payload({"use_count": 1}, now="kein-timestamp")
    assert upd["use_count"] == 2
    assert parse_ts(upd["last_accessed"]) is not None


def test_normalize_salience_clamps():
    from nexus_memory.memory_dynamics import normalize_salience
    assert normalize_salience(1.7) == 1.0
    assert normalize_salience(-0.5) == 0.0
    assert normalize_salience(None, "rule") == 0.8
    assert normalize_salience(None, "temp") == 0.1
    assert normalize_salience(None, "fact") == 0.5
    assert normalize_salience("kaputt", "fact") == 0.5
    assert normalize_salience(0.9, "fact") == 0.9


def test_reinforcement_uses_math_module():
    """Review-Fix: kein __import__('math') mehr — direkter Import."""
    import inspect
    from nexus_memory import memory_dynamics as md
    src = inspect.getsource(md.reinforcement_factor)
    assert "__import__" not in src