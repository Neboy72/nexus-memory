"""Tests for nexus/health/__init__.py — DriftDetector + Memory Expiry."""

from datetime import datetime, timedelta, timezone
from nexus.health import (
    ExpiryPolicy,
    compute_expires_at,
    DriftDetector,
    DriftReport,
)


class TestExpiryPolicy:
    def test_static_never_expires(self):
        expires, expired = compute_expires_at(datetime.now(timezone.utc), None, "static")
        assert expires is None
        assert not expired

    def test_normal_expires_after_90_days(self):
        old = datetime.now(timezone.utc) - timedelta(days=100)
        expires, expired = compute_expires_at(old, None, "normal")
        assert expired
        assert expires is not None

    def test_volatile_expires_after_7_days(self):
        old = datetime.now(timezone.utc) - timedelta(days=14)
        expires, expired = compute_expires_at(old, None, "volatile")
        assert expired

    def test_recent_normal_not_expired(self):
        recent = datetime.now(timezone.utc) - timedelta(days=1)
        expires, expired = compute_expires_at(recent, None, "normal")
        assert not expired

    def test_no_anchor_treated_as_expired(self):
        expires, expired = compute_expires_at(None, None, "normal")
        assert expired

    def test_last_confirmed_extends_life(self):
        now = datetime.now(timezone.utc)
        old_created = now - timedelta(days=200)
        recent_confirmed = now - timedelta(days=10)
        expires, expired = compute_expires_at(old_created, recent_confirmed, "normal")
        assert not expired

    def test_none_policy_defaults_to_normal(self):
        old = datetime.now(timezone.utc) - timedelta(days=100)
        expires, expired = compute_expires_at(old, None, None)
        assert expired


class TestDriftReport:
    def test_expired_field_present(self):
        r = DriftReport()
        assert hasattr(r, "expired")
        assert r.expired == []

    def test_json_includes_expired_count(self):
        r = DriftReport()
        j = r.json()
        assert '"expired_count"' in j


class TestCheckExpiry:
    def test_check_expiry_parses_payload(self):
        now = datetime.now(timezone.utc)
        old = now - timedelta(days=100)
        payload = {
            "timestamp": old.isoformat(),
            "expiry_policy": "volatile",
        }
        detector = DriftDetector()
        expires, expired = detector._check_expiry(payload)
        assert expired

    def test_valid_until_override_future(self):
        now = datetime.now(timezone.utc)
        future = now + timedelta(days=30)
        payload = {
            "timestamp": (now - timedelta(days=200)).isoformat(),
            "expiry_policy": "volatile",
            "valid_until": future.isoformat(),
        }
        detector = DriftDetector()
        expires, expired = detector._check_expiry(payload)
        assert not expired, "valid_until in future should prevent expiry"

    def test_valid_until_override_past(self):
        now = datetime.now(timezone.utc)
        past = now - timedelta(days=10)
        payload = {
            "timestamp": (now - timedelta(days=200)).isoformat(),
            "expiry_policy": "static",
            "valid_until": past.isoformat(),
        }
        detector = DriftDetector()
        expires, expired = detector._check_expiry(payload)
        assert expired, "valid_until in past should expire regardless of policy"


class TestRunFromTexts:
    def test_expiry_integration(self):
        now = datetime.now(timezone.utc)
        detector = DriftDetector()
        entries = [
            {
                "id": "1",
                "content": "Config: API key",
                "timestamp": (now - timedelta(days=200)).isoformat(),
                "payload": {"expiry_policy": "static"},
            },
            {
                "id": "2",
                "content": "Temp token abc",
                "timestamp": (now - timedelta(days=30)).isoformat(),
                "payload": {"expiry_policy": "volatile"},
            },
            {
                "id": "3",
                "content": "Project deadline Friday",
                "timestamp": now.isoformat(),
                "payload": {},
            },
            {
                "id": "4",
                "content": "Old normal fact",
                "timestamp": (now - timedelta(days=200)).isoformat(),
                "payload": {"expiry_policy": "normal"},
            },
        ]
        report = detector.run_from_texts(entries)
        expired_ids = {e["id"] for e in report.expired}
        assert "2" in expired_ids, "volatile 30d should be expired"
        assert "4" in expired_ids, "normal 200d should be expired"
        assert "1" not in expired_ids, "static should NOT be expired"
        assert "3" not in expired_ids, "recent entry should NOT be expired"
