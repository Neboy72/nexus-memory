"""Tests for the server-side agent self-report watchdog (self_report.py).

Cover the fixed contract with the Hermes plugin: it writes
``agent-selfcheck[-<id>].json`` once per process; the server must notice a
broken plugin and suspicious registry silence, alert via configured channels,
and dedup repeated alerts — all fail-open, all silent by default.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from nexus_memory import self_report


def _iso(hours_ago: float = 0.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def _write(path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def _report(agent_id: str, ok: bool, hours_ago: float = 1.0, **extra) -> dict:
    data = {
        "agent_id": agent_id,
        "ok": ok,
        "reason": "" if ok else "qdrant_client missing",
        "fix": "" if ok else "uv pip install -e /repo",
        "interpreter": "/usr/bin/python3",
        "plugin_version": "0.0.0",
        "ts": _iso(hours_ago),
    }
    data.update(extra)
    return data


# ---------------------------------------------------------------------------
# 1. read_agent_reports
# ---------------------------------------------------------------------------

def test_read_reports_per_agent_and_legacy_and_corrupt(tmp_path):
    _write(tmp_path / "agent-selfcheck-hermes.json", _report("hermes", False))
    _write(tmp_path / "agent-selfcheck-legacy.json", _report("legacy", True))
    (tmp_path / "agent-selfcheck.json").write_text("{not valid json", encoding="utf-8")
    (tmp_path / "selfreport-state.json").write_text("{}", encoding="utf-8")

    reports = self_report.read_agent_reports(tmp_path)

    ids = sorted(r["agent_id"] for r in reports)
    assert ids == ["hermes", "legacy"]  # corrupt file skipped, state file ignored
    for r in reports:
        assert isinstance(r["age_hours"], float)
        assert r["age_hours"] < 2  # fresh-ish


def test_read_reports_missing_ts_is_infinite_age(tmp_path):
    _write(tmp_path / "agent-selfcheck-hermes.json",
           {"agent_id": "hermes", "ok": False})
    (reports,) = self_report.read_agent_reports(tmp_path)
    assert reports["age_hours"] == float("inf")


def test_read_reports_never_raises_on_unreadable_dir(tmp_path):
    (tmp_path / "not-a-dir").write_text("x", encoding="utf-8")
    assert self_report.read_agent_reports(tmp_path / "not-a-dir" / "nope") == []


# ---------------------------------------------------------------------------
# 2. evaluate — fresh vs. stale broken reports
# ---------------------------------------------------------------------------

def test_evaluate_warning_for_fresh_broken_report():
    now = time.time()
    result = self_report.evaluate(
        reports=[_report("hermes", False, hours_ago=1.0)],
        registry={"agents": []}, now=now,
    )
    assert result["status"] == "warning"
    assert result["broken_agents"] == ["hermes"]
    assert result["silent_agents"] == []
    assert result["reports"][0]["fresh"] is True
    assert result["reports"][0]["age_hours"] < 2


def test_evaluate_ok_when_only_broken_report_is_stale():
    now = time.time()
    result = self_report.evaluate(
        reports=[_report("hermes", False, hours_ago=200.0)],
        registry={"agents": []}, now=now,
    )
    assert result["status"] == "ok"
    assert result["broken_agents"] == []
    assert result["reports"][0]["fresh"] is False  # history, still listed
    assert result["reports"][0]["ok"] is False


# ---------------------------------------------------------------------------
# 3. silence heuristic
# ---------------------------------------------------------------------------

def _agent(agent_id, install_type, reads, writes, hours_ago):
    return {
        "id": agent_id,
        "install_type": install_type,
        "reads": reads,
        "writes": writes,
        "last_seen": _iso(hours_ago),
    }


def test_silence_heuristic_is_conservative():
    now = time.time()
    registry = {"agents": [
        _agent("plugin-quiet", "plugin+mcp", 3, 0, 5 * 24),      # silent
        _agent("plugin-dormant", "plugin+mcp", 3, 0, 20 * 24),   # too long: dormant
        _agent("mcp-only", "mcp", 3, 0, 5 * 24),                 # expected quiet
        _agent("plugin-unused", "plugin+mcp", 0, 0, 5 * 24),     # never used memory
    ]}
    result = self_report.evaluate(reports=[], registry=registry, now=now)

    silent_ids = [s["agent_id"] for s in result["silent_agents"]]
    assert silent_ids == ["plugin-quiet"]
    assert result["silent_agents"][0]["silent_hours"] == pytest.approx(5 * 24, abs=0.1)
    assert result["status"] == "warning"


# ---------------------------------------------------------------------------
# 4. alert_messages
# ---------------------------------------------------------------------------

def test_alert_messages_keys_and_dedup_windows():
    evaluation = {
        "reports": [_report("hermes", False)],
        "broken_agents": ["hermes"],
        "silent_agents": [{"agent_id": "openclaw", "silent_hours": 120.0,
                           "last_seen": _iso(120)}],
    }
    alerts = self_report.alert_messages(evaluation)
    by_key = {a["key"]: a for a in alerts}
    assert set(by_key) == {"broken:hermes", "silent:openclaw"}
    assert by_key["broken:hermes"]["dedup_hours"] == 48
    assert by_key["silent:openclaw"]["dedup_hours"] == 168

    broken_text = by_key["broken:hermes"]["text"]
    assert "qdrant_client missing" in broken_text
    assert "uv pip install -e /repo" in broken_text
    assert "safe and not lost" in broken_text
    assert "restores memory" in broken_text

    silent_text = by_key["silent:openclaw"]["text"]
    # Honest wording: presents BOTH possibilities, commits to neither.
    assert "not being used" in silent_text
    assert "plugin" in silent_text


# ---------------------------------------------------------------------------
# 5. notify
# ---------------------------------------------------------------------------

class _FakeResponse:
    status = 200

    def close(self) -> None:
        pass


def test_notify_silent_without_channel(monkeypatch):
    monkeypatch.delenv("NEXUS_ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("NEXUS_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("NEXUS_ALERT_MACOS", raising=False)
    assert self_report.notify("hello", "title") is False  # never raises


def test_notify_webhook_posts_content(monkeypatch):
    monkeypatch.delenv("NEXUS_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("NEXUS_ALERT_MACOS", raising=False)
    monkeypatch.setenv("NEXUS_ALERT_WEBHOOK_URL", "https://example.invalid/hook")
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        captured["payload"] = json.loads(req.data)
        return _FakeResponse()

    monkeypatch.setattr(self_report.urllib.request, "urlopen", fake_urlopen)
    assert self_report.notify("body text", "TITLE") is True
    assert captured["url"] == "https://example.invalid/hook"
    assert captured["timeout"] == 5
    assert "content" in captured["payload"]
    assert "body text" in captured["payload"]["content"]


def test_notify_webhook_failure_is_fail_open(monkeypatch):
    monkeypatch.delenv("NEXUS_ALERT_MACOS", raising=False)
    monkeypatch.setenv("NEXUS_ALERT_WEBHOOK_URL", "https://example.invalid/hook")

    def boom(req, timeout=None):
        raise OSError("network down")

    monkeypatch.setattr(self_report.urllib.request, "urlopen", boom)
    assert self_report.notify("x") is False  # no exception


# ---------------------------------------------------------------------------
# 6. dedup state across passes
# ---------------------------------------------------------------------------

def test_dedup_delivers_once_per_window(tmp_path, monkeypatch):
    delivered: list = []
    monkeypatch.setattr(
        self_report, "notify",
        lambda text, title="": (delivered.append(title), True)[1],
    )
    _write(tmp_path / "agent-selfcheck-hermes.json", _report("hermes", False))

    daemon = self_report.SelfReportDaemon(data_dir=tmp_path)
    t = time.time()

    assert daemon.run_pass(now=t)["delivered"] == ["broken:hermes"]
    assert daemon.run_pass(now=t)["delivered"] == []  # suppressed by state file
    state = json.loads((tmp_path / "selfreport-state.json").read_text())
    assert "broken:hermes" in state

    # Healthy pass: the report disappears -> no issue, state unchanged.
    (tmp_path / "agent-selfcheck-hermes.json").unlink()
    assert daemon.run_pass(now=t + 3600)["delivered"] == []

    # Same issue returns: still within the 48 h window -> suppressed ...
    _write(tmp_path / "agent-selfcheck-hermes.json", _report("hermes", False))
    assert daemon.run_pass(now=t + 47 * 3600)["delivered"] == []
    # ... and re-alerts once the window has passed.
    assert daemon.run_pass(now=t + 49 * 3600)["delivered"] == ["broken:hermes"]
    assert len(delivered) == 2


# ---------------------------------------------------------------------------
# 7. kill-switch
# ---------------------------------------------------------------------------

def test_kill_switch_leaves_no_thread(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_SELFREPORT", "0")
    daemon = self_report.SelfReportDaemon(data_dir=tmp_path)
    daemon.start()
    assert daemon._thread is None


# ---------------------------------------------------------------------------
# 8. defensive env parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["abc", "-5", ""])
def test_malformed_interval_falls_back_to_default(tmp_path, monkeypatch, bad):
    monkeypatch.setenv("NEXUS_SELFREPORT_INTERVAL_SEC", bad)
    daemon = self_report.SelfReportDaemon(data_dir=tmp_path)
    assert daemon._interval == self_report.DEFAULT_INTERVAL


# ---------------------------------------------------------------------------
# 9. failed delivery must not lose the alert (dedup only on success)
# ---------------------------------------------------------------------------

def test_failed_delivery_is_not_deduped_and_retried(tmp_path, monkeypatch):
    seen: list = []
    monkeypatch.setattr(
        self_report, "notify",
        lambda text, title="": (seen.append(title), False)[1],
    )
    _write(tmp_path / "agent-selfcheck-hermes.json", _report("hermes", False))
    daemon = self_report.SelfReportDaemon(data_dir=tmp_path)
    t = time.time()

    assert daemon.run_pass(now=t)["delivered"] == []
    state_path = tmp_path / "selfreport-state.json"
    assert not state_path.exists() or "broken:hermes" not in json.loads(
        state_path.read_text())
    # Not recorded -> the next pass offers the alert again.
    assert daemon.run_pass(now=t + 60)["delivered"] == []
    assert len(seen) == 2


def test_successful_delivery_is_deduped(tmp_path, monkeypatch):
    monkeypatch.setattr(self_report, "notify", lambda text, title="": True)
    _write(tmp_path / "agent-selfcheck-hermes.json", _report("hermes", False))
    daemon = self_report.SelfReportDaemon(data_dir=tmp_path)
    t = time.time()

    assert daemon.run_pass(now=t)["delivered"] == ["broken:hermes"]
    assert daemon.run_pass(now=t + 60)["delivered"] == []
    state = json.loads((tmp_path / "selfreport-state.json").read_text())
    assert "broken:hermes" in state


# ---------------------------------------------------------------------------
# 10. hostile self-check values must not blind the watchdog
# ---------------------------------------------------------------------------

def test_infinite_reads_do_not_break_evaluate():
    now = time.time()
    registry = {"agents": [{
        "id": "weird", "install_type": "plugin+mcp",
        "reads": float("inf"), "writes": float("inf"),
        "last_seen": _iso(5 * 24),
    }]}
    result = self_report.evaluate(reports=[], registry=registry, now=now)
    # int(inf) raises OverflowError; the agent is simply not reported silent.
    assert result["silent_agents"] == []
    assert result["status"] == "ok"


def test_future_ts_is_not_fresh():
    now = time.time()
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    result = self_report.evaluate(
        reports=[{"agent_id": "x", "ok": False, "reason": "r",
                  "fix": "f", "ts": future}],
        registry={"agents": []}, now=now,
    )
    assert result["reports"][0]["fresh"] is False
    assert result["broken_agents"] == []
    assert result["status"] == "ok"


def test_long_reason_and_fix_truncated_in_alert():
    evaluation = {
        "reports": [{"agent_id": "x", "reason": "A" * 100_000,
                     "fix": "B" * 100_000}],
        "broken_agents": ["x"],
        "silent_agents": [],
    }
    (alert,) = self_report.alert_messages(evaluation)
    text = alert["text"]
    assert text.count("A") <= 500
    assert text.count("B") <= 500
    assert "…" in text
    assert len(text) < 2_000


def test_overlong_agent_id_truncated():
    evaluation = {
        "reports": [], "broken_agents": [], "silent_agents": [
            {"agent_id": "z" * 500, "silent_hours": 120.0},
        ],
    }
    (alert,) = self_report.alert_messages(evaluation)
    assert len(alert["title"]) < 300
    assert "z" * 121 not in alert["title"]


def test_oversized_selfcheck_file_skipped(tmp_path):
    big = tmp_path / "agent-selfcheck-big.json"
    big.write_text(
        json.dumps({"agent_id": "big", "ok": False,
                    "reason": "x" * (2 * 1024 * 1024), "ts": _iso(1)}),
        encoding="utf-8",
    )
    assert self_report.read_agent_reports(tmp_path) == []


# ---------------------------------------------------------------------------
# 11. env parsing rejects non-finite values
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["inf", "nan", "abc"])
def test_non_finite_start_delay_falls_back(tmp_path, monkeypatch, bad):
    monkeypatch.setenv("NEXUS_SELFREPORT_START_DELAY", bad)
    daemon = self_report.SelfReportDaemon(data_dir=tmp_path)
    assert daemon._start_delay == self_report.DEFAULT_START_DELAY


# ---------------------------------------------------------------------------
# 12. dedup state with a non-finite value must load clean and never raise
# ---------------------------------------------------------------------------

def test_state_with_infinity_loads_empty(tmp_path):
    (tmp_path / "selfreport-state.json").write_text(
        '{"broken:hermes": Infinity}', encoding="utf-8")
    daemon = self_report.SelfReportDaemon(data_dir=tmp_path)
    assert daemon._load_state() == {}


# ---------------------------------------------------------------------------
# 13. AppleScript payload stays a valid single line
# ---------------------------------------------------------------------------

def test_applescript_payload_is_single_line(monkeypatch):
    class _Proc:
        returncode = 0

    captured: dict = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return _Proc()

    monkeypatch.setenv("NEXUS_ALERT_MACOS", "1")
    monkeypatch.delenv("NEXUS_ALERT_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("NEXUS_WEBHOOK_URL", raising=False)
    monkeypatch.setattr(self_report.sys, "platform", "darwin")
    monkeypatch.setattr(self_report.subprocess, "run", fake_run)

    assert self_report.notify("line1\nline2\ttab\r", "TITLE\nX") is True
    script = captured["args"][-1]
    assert "\n" not in script
    assert "\r" not in script
    assert "\t" not in script
    assert "line1 line2 tab" in script


# ---------------------------------------------------------------------------
# 14. webhook SSRF guard + no redirects
# ---------------------------------------------------------------------------

def test_notify_rejects_unsafe_webhook_url(monkeypatch):
    monkeypatch.setenv("NEXUS_ALERT_WEBHOOK_URL", "file:///etc/passwd")
    monkeypatch.delenv("NEXUS_ALERT_MACOS", raising=False)

    def boom(*args, **kwargs):
        raise AssertionError("network must not be attempted for a rejected URL")

    monkeypatch.setattr(self_report.urllib.request, "urlopen", boom)
    assert self_report.notify("x", "t") is False


class _RedirectResponse:
    status = 302

    def close(self) -> None:
        pass


class _RedirectOpener:
    def __init__(self) -> None:
        self.calls = 0

    def open(self, req, timeout=None):
        self.calls += 1
        return _RedirectResponse()


def test_no_redirect_handler_refuses_redirects():
    handler = self_report._NoRedirect()
    assert handler.redirect_request(
        None, None, 302, "Found", {}, "http://evil.invalid/") is None


def test_notify_does_not_follow_redirect(monkeypatch):
    monkeypatch.setenv("NEXUS_ALERT_WEBHOOK_URL", "https://example.invalid/hook")
    monkeypatch.delenv("NEXUS_ALERT_MACOS", raising=False)
    opener = _RedirectOpener()
    monkeypatch.setattr(self_report, "_redirect_safe_opener", lambda: opener)
    monkeypatch.setattr(self_report, "_ORIGINAL_URLOPEN",
                        self_report.urllib.request.urlopen)

    # A 302 is returned by the opener instead of being followed -> not delivered.
    assert self_report.notify("x", "t") is False
    assert opener.calls == 1


# ---------------------------------------------------------------------------
# 15. registry reader reuses agent_detect only on the default path
# ---------------------------------------------------------------------------

def test_read_registry_delegates_to_agent_detect_on_default(monkeypatch):
    monkeypatch.delenv("NEXUS_DATA_DIR", raising=False)
    from nexus_memory import agent_detect
    monkeypatch.setattr(agent_detect, "load_agents_registry",
                        lambda: {"agents": [{"id": "sentinel"}]})
    assert self_report.read_registry()["agents"][0]["id"] == "sentinel"


def test_read_registry_local_reader_for_redirected_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_DATA_DIR", str(tmp_path))
    _write(tmp_path / "agents.json", {"agents": [{"id": "local"}]})
    assert self_report.read_registry()["agents"][0]["id"] == "local"
