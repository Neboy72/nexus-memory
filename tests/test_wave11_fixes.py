"""Tests for OCR review Wave 11 — HIGH findings H1–H10.

One test class per finding (>= 2 tests each). Python paths are exercised
behaviourally with fakes/monkeypatch; the shell findings (#105–#107) are
checked with ``bash -n`` plus real invocations against temp directories.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from nexus_memory import selective_forgetting as SF
from nexus_memory import retrieval_watch as RW
from nexus_memory import scope_auto as SA
from nexus_memory import trust_service as TS

REPO_ROOT = Path(__file__).resolve().parents[1]
HERMES_INSTALLER = REPO_ROOT / "scripts" / "install_hermes_plugin.sh"
OPENCLAW_INSTALLER = REPO_ROOT / "plugins" / "openclaw" / "scripts" / "install_openclaw_plugin.sh"
RELEASE_GATE = REPO_ROOT / "scripts" / "release_gate.sh"


def _iso_days_ago(days: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


class _FakePoint:
    def __init__(self, pid, payload):
        self.id = pid
        self.payload = payload


class _FakeSFStore:
    def __init__(self, points):
        self._points = points
        self.client = SimpleNamespace(
            scroll=lambda collection, limit=500, offset=None,
            with_payload=True, with_vectors=False:
                (list(self._points) if offset is None else [], None),
        )


# ── H1: selective_forgetting filters on the EXACT score (#98) ────────────────


class TestH1ExactScore:
    def _auditor(self, points, tmp_path):
        return SF.SelectiveForgettingAuditor(
            _FakeSFStore(points), "c", data_dir=str(tmp_path)
        )

    def test_scored_entries_expose_exact_score(self, tmp_path):
        points = [_FakePoint("old-1", {"category": "session",
                                       "created_at": _iso_days_ago(400)})]
        rep = self._auditor(points, tmp_path).run()
        assert len(rep["candidates_score_ge_060"]) == 1
        cand = rep["candidates_score_ge_060"][0]
        assert "exact_score" in cand
        assert cand["exact_score"] >= SF.CANDIDATE_THRESHOLD
        # display value stays the rounded one
        assert cand["score"] == round(cand["exact_score"], 3)

    def test_rounded_up_score_is_not_a_candidate(self, tmp_path, monkeypatch):
        # exact 0.5996 rounds (display) to 0.6 but is below the threshold —
        # the candidate filter must use exact_score, so this is no candidate.
        auditor = self._auditor(
            [_FakePoint("x", {"category": "session",
                              "created_at": _iso_days_ago(400)})], tmp_path
        )
        monkeypatch.setattr(auditor, "score_point", lambda payload, now: 0.5996)
        rep = auditor.run()
        assert rep["scored"] == 1
        assert round(0.5996, 3) == 0.6  # the rounded value WOULD have passed
        assert rep["candidates_score_ge_060"] == []


# ── H2: CATEGORY_DEFAULT_WEIGHT must exceed the threshold (#99) ──────────────


class TestH2DefaultWeight:
    def test_default_weight_exceeds_threshold(self):
        assert SF.CATEGORY_DEFAULT_WEIGHT > SF.CANDIDATE_THRESHOLD
        assert SF.CATEGORY_DEFAULT_WEIGHT == 1.0

    def test_old_default_category_point_is_reachable(self, tmp_path):
        # A "fact" uses the default weight; before the fix its score was
        # capped below the threshold and could never become a candidate.
        points = [_FakePoint("fact-old", {"category": "fact",
                                          "created_at": _iso_days_ago(400)})]
        auditor = SF.SelectiveForgettingAuditor(
            _FakeSFStore(points), "c", data_dir=str(tmp_path)
        )
        score = auditor.score_point(points[0].payload, __import__("time").time())
        assert score is not None and score >= SF.CANDIDATE_THRESHOLD
        rep = auditor.run()
        assert [c["id"] for c in rep["candidates_score_ge_060"]] == ["fact-old"]


# ── H3: scope_auto refreshes discovered scopes on cache refresh (#100) ───────


class _ProbeClient:
    """Scrolls scope-probe points; centroid fetches return nothing."""

    def __init__(self, scopes):
        self.scopes = list(scopes)
        self.probe_calls = 0

    def scroll(self, **kwargs):
        flt = json.dumps(kwargs.get("scroll_filter") or {})
        if "except" in flt:  # probe query (scope != default)
            self.probe_calls += 1
            return [SimpleNamespace(payload={"scope": s}) for s in self.scopes], None
        return [], None  # centroid fetch: no scoped canonical points


class TestH3ScopeRefresh:
    def test_known_scopes_refreshed_on_cache_expiry(self):
        client = _ProbeClient(["voice"])
        centroids = SA.ScopeCentroids(client, "c")
        assert centroids._known_scopes == ["voice"]
        assert client.probe_calls == 1  # __init__ probe

        client.scopes = ["voice", "openclaw-maint"]
        centroids.get()  # cache is None → refresh
        assert centroids._known_scopes == sorted(["voice", "openclaw-maint"])
        assert client.probe_calls == 2

    def test_no_probe_while_cache_is_fresh(self):
        client = _ProbeClient(["voice"])
        centroids = SA.ScopeCentroids(client, "c")
        centroids.get()
        calls = client.probe_calls
        centroids.get()  # within CENTROID_TTL_SECONDS → no refresh
        assert client.probe_calls == calls


# ── H4: trust_service filters events server-side + ceiling (#101) ────────────


class _ScrollStore:
    def __init__(self, batches, collection_name="c"):
        self.batches = list(batches)
        self.calls = []
        self.collection_name = collection_name
        self.client = self

    def scroll(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.batches.pop(0) if self.batches else ([], None)


class TestH4EventsFor:
    def _svc(self, store, tmp_path):
        return TS.TrustService(store, "c", data_dir=str(tmp_path))

    def test_scroll_filter_is_pushed_down(self, tmp_path):
        point = SimpleNamespace(payload={
            "event_type": "belief_event", "belief_id": "b1", "assertion": "confirm",
        })
        store = _ScrollStore([([point], None)])
        events = self._svc(store, tmp_path)._events_for("b1")

        assert events == [point.payload]
        _args, kwargs = store.calls[0]
        flt = kwargs["scroll_filter"]
        # server-side filter carries BOTH predicates
        keys = sorted(c.key for c in flt.must)
        assert keys == ["belief_id", "event_type"]
        rendered = json.dumps(flt.model_dump(mode="json", exclude_none=True))
        assert "belief_event" in rendered and "b1" in rendered

    def test_matching_is_still_enforced_client_side(self, tmp_path):
        # A point that slips through the (fake) filter must not be counted.
        good = SimpleNamespace(payload={"event_type": "belief_event", "belief_id": "b1"})
        other = SimpleNamespace(payload={"event_type": "belief_event", "belief_id": "b2"})
        store = _ScrollStore([([good, other], None)])
        events = self._svc(store, tmp_path)._events_for("b1")
        assert events == [good.payload]

    def test_event_ceiling_truncates_runaway_fanout(self, tmp_path, monkeypatch):
        monkeypatch.setattr(TS, "_MAX_EVENTS", 3)
        batch = [
            (SimpleNamespace(payload={"event_type": "belief_event", "belief_id": "b1"}), "next")
        ]
        # Every scroll returns 2 matching events and a non-None offset.
        two = [
            SimpleNamespace(payload={"event_type": "belief_event", "belief_id": "b1"}),
            SimpleNamespace(payload={"event_type": "belief_event", "belief_id": "b1"}),
        ]
        store = _ScrollStore([(two, "next"), (two, "next"), (two, "next")])
        events = self._svc(store, tmp_path)._events_for("b1")
        assert len(events) == 3  # truncated at the ceiling
        assert len(store.calls) == 2  # stopped looping


# ── H5: retrieval_watch score gate only on real vector hits (#102) ───────────


class _RWHit:
    def __init__(self, text, score):
        self.payload = {"text": text}
        self.score = score


class _RWVectors:
    """Store with a working embedder → vector search path (embed_ok=True)."""

    def __init__(self, hits):
        self.client = SimpleNamespace(query_points=lambda **kw: SimpleNamespace(points=hits))
        self._embedder = _RWVectors._E()

    class _E:
        def embed(self, _q):
            return [0.1, 0.2, 0.3, 0.4]


class _RWScroll:
    """Store without an embedder → scroll fallback (embed_ok=False)."""

    def __init__(self, hits):
        self.client = SimpleNamespace(
            scroll=lambda *a, **kw: (hits, None), query_points=None
        )
        self._embedder = None


def _watch(store, monkeypatch, min_score=0.5):
    monkeypatch.setattr(RW, "MIN_SCORE", min_score)
    w = RW.RetrievalWatch(store, "c")
    w._queries = [("Bose SoundLink Audio", "Bose")]
    return w


class TestH5ScoreGate:
    def test_keyword_hit_below_min_is_score_failure(self, monkeypatch):
        store = _RWVectors([_RWHit("Bose SoundLink laeuft", 0.2)])
        rep = _watch(store, monkeypatch).run()
        assert rep["failures"][0]["reason"] == "score_below_min"

    def test_unrelated_strong_hit_does_not_mask_weak_keyword_hit(self, monkeypatch):
        store = _RWVectors([
            _RWHit("Razer USB Mikrofon", 0.99),   # strong but wrong keyword
            _RWHit("Bose SoundLink leise", 0.2),  # weak, has the keyword
        ])
        rep = _watch(store, monkeypatch).run()
        assert rep["failures"][0]["reason"] == "score_below_min"
        assert rep["failures"][0]["top_score"] == 0.2

    def test_no_keyword_hit_is_not_found(self, monkeypatch):
        store = _RWVectors([_RWHit("Razer USB Mikrofon", 0.99)])
        rep = _watch(store, monkeypatch).run()
        assert rep["failures"][0]["reason"] == "not_found"

    def test_scroll_fallback_has_no_score_gate(self, monkeypatch):
        # Embedding failed → scores are meaningless; a keyword hit counts.
        store = _RWScroll([_RWHit("Bose SoundLink laeuft", 0.01)])
        rep = _watch(store, monkeypatch).run()
        assert rep["failures"] == []
        assert rep["failed_embeddings"] == 1


# ── H6: setup.install_agent validates before installing (#103) ───────────────


class TestH6InstallValidation:
    @pytest.fixture
    def setup_mod(self):
        from nexus_memory import setup
        return setup

    def _detection(self, monkeypatch, setup_mod):
        info = {"id": "synth", "name": "Synth", "icon": "X",
                "plugin_available": False, "mcp_available": True, "config_dir": ""}
        monkeypatch.setattr(setup_mod, "detect_all_agents",
                            lambda: {"detected_agents": [info], "not_detected": []})

    def test_invalid_trust_level_aborts_before_install(self, monkeypatch, setup_mod):
        self._detection(monkeypatch, setup_mod)
        called = []
        monkeypatch.setattr(setup_mod, "_install_mcp", lambda a: called.append(a))
        monkeypatch.setattr(setup_mod, "register_agent", lambda **kw: called.append(kw))
        result = setup_mod.install_agent("synth", "godmode")
        assert "error" in result and "trust" in result["error"].lower()
        assert called == []

    def test_register_error_is_surfaced_not_reported_installed(self, monkeypatch, setup_mod):
        self._detection(monkeypatch, setup_mod)
        monkeypatch.setattr(setup_mod, "_install_mcp", lambda a: {
            "agent_id": a, "install_type": "mcp", "status": "installed", "message": "ok",
        })
        monkeypatch.setattr(setup_mod, "register_agent",
                            lambda **kw: {"error": "remote host collision"})
        result = setup_mod.install_agent("synth", "trusted")
        assert "error" in result
        assert result.get("status") != "installed"
        assert "collision" in result["error"]


# ── H7: mcp_server fails closed on unknown levels (#104) ─────────────────────


class TestH7FailClosed:
    @pytest.fixture
    def mcp(self, monkeypatch):
        from nexus_memory import mcp_server
        monkeypatch.delenv("NEXUS_AGENT_ID", raising=False)
        return mcp_server

    async def test_remember_invalid_access_level_errors(self, mcp, monkeypatch):
        monkeypatch.setattr(mcp, "_store", SimpleNamespace())
        out = await mcp.handle_call_tool(
            "remember", {"text": "x", "category": "fact", "access_level": "GOD_MODE"}
        )
        body = json.loads(out[0].text)
        assert body["status"] == "error"
        assert "access_level" in body["error"]

    async def test_recall_invalid_filter_level_errors(self, mcp, monkeypatch):
        calls = []

        class _Store:
            async def recall(self, *a, **kw):
                calls.append(kw)
                return []

        monkeypatch.setattr(mcp, "_store", _Store())
        out = await mcp.handle_call_tool(
            "recall", {"query": "q", "filter_level": "GOD_MODE"}
        )
        body = json.loads(out[0].text)
        assert body["status"] == "error"
        assert "filter_level" in body["error"]
        assert calls == []  # must not reach the store

    async def test_recall_valid_filter_level_passes_through(self, mcp, monkeypatch):
        seen = {}

        class _Store:
            async def recall(self, query, agent_level=None, limit=5, as_of=None):
                seen["level"] = agent_level
                return []

        monkeypatch.setattr(mcp, "_store", _Store())
        out = await mcp.handle_call_tool(
            "recall", {"query": "q", "filter_level": "private"}
        )
        body = json.loads(out[0].text)
        assert body["count"] == 0
        assert seen["level"] == "private"


# ── H8: hermes installer backs up a regular file at the target (#105) ────────


def _run_installer(script: Path, home: Path):
    env = dict(os.environ)
    env["HOME"] = str(home)
    # Minimal PATH: keeps date/ln/mv/mkdir but drops the 'hermes' CLI so the
    # config step is skipped.
    env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    return subprocess.run(["bash", str(script)], capture_output=True, text=True,
                          env=env, timeout=60)


def _hermes_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    plugin_src = home / "nexus-memory" / "plugins" / "memory" / "nexus"
    plugin_src.mkdir(parents=True)
    (plugin_src / "MARKER").write_text("src")
    (home / ".hermes" / "hermes-agent" / "plugins" / "memory").mkdir(parents=True)
    return home


class TestH8HermesBackup:
    def _target(self, home: Path) -> Path:
        return home / ".hermes" / "hermes-agent" / "plugins" / "memory" / "nexus"

    def test_regular_file_is_backed_up_and_replaced(self, tmp_path):
        home = _hermes_home(tmp_path)
        target = self._target(home)
        target.write_text("stale-content")

        r = _run_installer(HERMES_INSTALLER, home)
        assert r.returncode == 0, r.stdout + r.stderr
        assert target.is_symlink()
        assert (target / "MARKER").read_text() == "src"
        assert target.parent.joinpath("nexus.bak").read_text() == "stale-content"

    def test_existing_bak_gets_timestamped_backup(self, tmp_path):
        home = _hermes_home(tmp_path)
        target = self._target(home)
        target.write_text("stale-content")
        (target.parent / "nexus.bak").write_text("previous-backup")

        r = _run_installer(HERMES_INSTALLER, home)
        assert r.returncode == 0, r.stdout + r.stderr
        # the old .bak is never overwritten
        assert (target.parent / "nexus.bak").read_text() == "previous-backup"
        backups = sorted(p for p in target.parent.glob("nexus.bak.*"))
        assert len(backups) == 1
        assert backups[0].read_text() == "stale-content"

    def test_syntax_ok(self):
        assert subprocess.run(["bash", "-n", str(HERMES_INSTALLER)]).returncode == 0


# ── H9: openclaw installer guards the destructive rm -rf (#106) ──────────────


def _run_openclaw(state_dir: str):
    env = dict(os.environ)
    env["OPENCLAW_STATE_DIR"] = state_dir
    return subprocess.run(["bash", str(OPENCLAW_INSTALLER)], capture_output=True,
                          text=True, env=env, timeout=60)


class TestH9OpenclawGuard:
    def test_refuses_root_plugins_dir(self):
        r = _run_openclaw("/")
        assert r.returncode != 0
        assert "refusing" in (r.stdout + r.stderr).lower()

    def test_install_into_temp_state_dir(self, tmp_path):
        state = tmp_path / "oc"
        r = _run_openclaw(str(state))
        assert r.returncode == 0, r.stdout + r.stderr
        target = state / "plugins" / "nexus-memory"
        assert target.is_symlink()

    def test_guards_precede_the_rm_rf(self):
        src = OPENCLAW_INSTALLER.read_text()
        rm_cmd = 'rm -rf "$TARGET_DIR"'
        assert rm_cmd in src
        # Both guards sit above the destructive command.
        assert src.index('case "$PLUGINS_DIR"') < src.index(rm_cmd)
        assert src.index('basename "$TARGET_DIR"') < src.index(rm_cmd)

    def test_syntax_ok(self):
        assert subprocess.run(["bash", "-n", str(OPENCLAW_INSTALLER)]).returncode == 0


# ── H10: release_gate diagnostics (#107) ─────────────────────────────────────


def _stub_gh(bin_dir: Path, output: str | None, exit_code: int = 0) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "gh"
    body = "#!/bin/bash\n"
    if output is not None:
        body += f'printf "%s\\n" "{output}"\n'
    body += f"exit {exit_code}\n"
    script.write_text(body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _pyproject(tmp_path: Path, version: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "pyproject.toml").write_text(f'[project]\nname = "nexus-memory"\nversion = "{version}"\n')
    return repo


def _run_gate(repo_dir: Path, bin_dir: Path | None = None):
    env = dict(os.environ)
    env["NEXUS_REPO_DIR"] = str(repo_dir)
    # A restricted PATH (no /opt/homebrew, no ~/.local/bin) so a real `gh`
    # installed for the user cannot leak into the test.
    if bin_dir is not None:
        env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
    return subprocess.run(["bash", str(RELEASE_GATE)], capture_output=True,
                          text=True, env=env, timeout=60)


class TestH10ReleaseGate:
    def test_missing_pyproject_is_a_clear_error(self, tmp_path):
        r = _run_gate(tmp_path / "does-not-exist")
        assert r.returncode == 1
        assert "GATE-ERROR" in r.stdout and "pyproject" in r.stdout

    def test_missing_gh_is_a_clear_error(self, tmp_path):
        repo = _pyproject(tmp_path, "1.2.3")
        empty_bin = tmp_path / "emptybin"
        empty_bin.mkdir()
        r = _run_gate(repo, bin_dir=empty_bin)
        assert r.returncode == 1
        assert "GATE-ERROR" in r.stdout and "gh" in r.stdout

    def test_no_tags_reports_no_tags(self, tmp_path):
        repo = _pyproject(tmp_path, "1.2.3")
        bin_dir = tmp_path / "bin"
        _stub_gh(bin_dir, output=None, exit_code=0)
        r = _run_gate(repo, bin_dir)
        assert r.returncode == 1
        assert "keine Tags" in r.stdout

    def test_gh_api_failure_is_a_clear_error(self, tmp_path):
        # gh present but the API call fails (e.g. unauthenticated on CI):
        # that is an infrastructure error, never "no tags".
        repo = _pyproject(tmp_path, "1.2.3")
        bin_dir = tmp_path / "bin"
        _stub_gh(bin_dir, output=None, exit_code=1)
        r = _run_gate(repo, bin_dir)
        assert r.returncode == 1
        assert "GATE-ERROR" in r.stdout and "gh api" in r.stdout
        assert "Release fehlt" not in r.stdout

    def test_matching_tag_is_green(self, tmp_path):
        repo = _pyproject(tmp_path, "1.2.3")
        bin_dir = tmp_path / "bin"
        _stub_gh(bin_dir, output="v1.2.3")
        r = _run_gate(repo, bin_dir)
        assert r.returncode == 0
        assert "GATE-GRUEN" in r.stdout

    def test_mismatched_tag_is_rot(self, tmp_path):
        repo = _pyproject(tmp_path, "1.2.3")
        bin_dir = tmp_path / "bin"
        _stub_gh(bin_dir, output="v1.1.0")
        r = _run_gate(repo, bin_dir)
        assert r.returncode == 1
        assert "GATE-ROT" in r.stdout and "Release fehlt" in r.stdout

    def test_unparsable_version_is_rejected(self, tmp_path):
        repo = _pyproject(tmp_path, "not-a-version")
        bin_dir = tmp_path / "bin"
        _stub_gh(bin_dir, output="v1.2.3")
        r = _run_gate(repo, bin_dir)
        assert r.returncode == 1
        assert "GATE-ROT" in r.stdout and "not-a-version" in r.stdout

    def test_syntax_ok(self):
        assert subprocess.run(["bash", "-n", str(RELEASE_GATE)]).returncode == 0
