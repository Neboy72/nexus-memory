"""OCR-2 Welle 30 (high, packet 3): dashboard async/auth, graph multi-edges,
plugin privacy, staging fail-loud. One class per root cause."""

import pytest

REPO = __file__.rsplit("/tests/", 1)[0]


def _read(rel: str) -> str:
    with open(f"{REPO}/{rel}", encoding="utf-8") as f:
        return f.read()


# ── W30-1: blocking I/O off the event loop ────────────────────────────────


class TestW30AsyncQdrant:
    def test_source_async_wrapper(self):
        src = _read("dashboard/server.py")
        assert "to_thread" in src
        # direct calls from async handlers replaced
        import re
        # every `resp = _qdrant_request(` inside an async def must be gone
        for m in re.finditer(r"async def (\w+)\(", src):
            start = m.end()
            nxt = src.find("\ndef ", start)
            nxt2 = src.find("\nasync def ", start)
            end = min(x for x in (nxt, nxt2, len(src)) if x != -1)
            seg = src[start:end]
            assert "_qdrant_request(" not in seg or "to_thread" in seg, (
                f"handler {m.group(1)} still calls blocking _qdrant_request"
            )


# ── W30-2: confidence None guard ──────────────────────────────────────────


class TestW30ConfidenceGuard:
    def test_source_filters_non_numeric(self):
        src = _read("dashboard/server.py")
        seg = src[src.index("confidences = []"):]
        seg = seg[:seg.index("\n    avg_conf")]
        assert "isinstance" in seg


# ── W30-3: local-origin guard on mutating routes ──────────────────────────


class TestW30MutationGuard:
    def test_source_dependency_exists(self):
        src = _read("dashboard/server.py")
        assert "verify_local_mutation" in src or "local_origin_guard" in src
        # at least one mutating route wired
        assert src.count("verify_local_mutation") >= 3

    def test_source_frontend_header(self):
        # dashboard static JS must send the header
        import glob
        js = ""
        for p in glob.glob(f"{REPO}/dashboard/static/**/*.js", recursive=True):
            js += open(p, encoding="utf-8", errors="ignore").read()
        assert "X-Nexus-Dashboard" in js


# ── W30-4: MultiDiGraph ───────────────────────────────────────────────────


class TestW30MultiDiGraph:
    def test_source_multi(self):
        src = _read("nexus/graph/graph.py")
        assert "MultiDiGraph" in src
        # edge keys carried
        assert "key=" in src


# ── W30-5: sync_turn access level ─────────────────────────────────────────


class TestW30SyncTurnPrivacy:
    def test_source_no_hardcoded_public(self):
        src = _read("integrations/hermes-plugin/__init__.py")
        seg = src[src.index("def sync_turn"):]
        seg = seg[:seg.index("\n    def ", 1)]
        assert 'self._default_access_level' in seg
        assert '"access_level": "public"' not in seg


# ── W30-6: graph-boost privacy filter ─────────────────────────────────────


class TestW30GraphBoostPrivacy:
    def test_source_filters_access_level(self):
        src = _read("integrations/hermes-plugin/__init__.py")
        seg = src[src.index("def _graph_boost"):]
        seg = seg[:seg.index("\n    def ", 1)]
        assert "access_level" in seg
        # no blanket public label anymore
        assert '"access_level": "public"' not in seg or "default_access_level" in seg


# ── W30-7: backup permissions ─────────────────────────────────────────────


class TestW30BackupPerms:
    def test_source_restricted_modes(self):
        src = _read("integrations/hermes-plugin/__init__.py")
        seg = src[src.index("def _do_backup"):]
        seg = seg[:seg.index("\n    def ", 1)]
        assert "0o700" in seg
        assert "0o600" in seg


# ── W30-8: EdgeStore scheme ───────────────────────────────────────────────


class TestW30EdgeStoreUrl:
    def test_source_scheme(self):
        src = _read("integrations/hermes-plugin/__init__.py")
        assert 'EdgeStore(qdrant_url=f"{_HOST}:{_PORT}"' not in src
        assert 'EdgeStore(qdrant_url=f"http://{_HOST}:{_PORT}"' in src


# ── W30-9: staging _get_version fail-loud ─────────────────────────────────


class TestW30StagingGetVersion:
    def test_source_raises_on_transport(self):
        src = _read("nexus/staging.py")
        seg = src[src.index("def _get_version"):]
        seg = seg[:seg.index("\n\n\ndef ", 1)]
        assert "RuntimeError" in seg
        assert "except requests.RequestException:\n        pass" not in seg