"""Tests for OCR review Wave 9 — HIGH findings H1–H10.

One test class per finding. Where a module has heavy import-time side effects
(the bench script connects to Qdrant at import) the test inspects the source
text and, where possible, executes the exact expression it finds so the test
stays tied to the shipped code. Pure-logic paths are exercised behaviourally.
"""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"


def _read(name: str) -> str:
    return (SCRIPTS / name).read_text()


def _load_script(name: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / name)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── H1: reembed_voyage4 .env fallback is reachable ───────────────────────────


class TestH1EnvLoadBeforeExit:
    def test_dead_exit_block_removed(self):
        src = _read("reembed_voyage4.py")
        # The premature guard ran before .env was loaded → the fallback below
        # could never execute. It must be gone.
        assert "VOYAGE_API_KEY not set in environment" not in src

    def test_env_load_precedes_only_remaining_module_guard(self):
        src = _read("reembed_voyage4.py")
        env_idx = src.index('env_file = os.path.expanduser("~/.hermes/.env")')
        guard_idx = src.index('logger.error("Could not find VOYAGE_API_KEY")')
        assert env_idx < guard_idx
        # main() keeps one exit guard for a failed voyage-4 probe.
        assert src.count("sys.exit(1)") == 2


# ── H2: reembed_voyage4 honours the API next_page_offset ─────────────────────


@pytest.fixture
def reembed_mod(monkeypatch):
    # The module exits at import when no key is present — set one first.
    monkeypatch.setenv("VOYAGE_API_KEY", "vo-test-key")
    return _load_script("reembed_voyage4.py", "wave9_reembed")


def _patch_reembed_io(monkeypatch, mod, pages):
    """Wire reembed_collection() to a canned page sequence; no network."""
    seen_offsets: list = []

    def fake_scroll(collection, offset=None):
        seen_offsets.append(offset)
        return pages[len(seen_offsets) - 1]

    monkeypatch.setattr(mod, "scroll_points", fake_scroll)
    monkeypatch.setattr(mod, "voyage_embed_batch",
                        lambda texts: [[0.0] * 4 for _ in texts])
    monkeypatch.setattr(mod, "upsert_points", lambda c, pts: {})

    class _Info:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"result": {"points_count": 0,
                               "config": {"params": {"vectors": {"size": 4}}}}}

    from types import SimpleNamespace
    monkeypatch.setattr(mod, "requests",
                        SimpleNamespace(get=lambda *a, **k: _Info()))
    return seen_offsets


class TestH2Pagination:
    def test_follows_next_page_offset(self, monkeypatch, reembed_mod):
        mod = reembed_mod
        pages = [
            {"points": [{"id": i, "payload": {"text": f"t{i}"}}
                        for i in range(mod.SCROLL_SIZE)],
             "next_page_offset": "off-2"},
            {"points": [{"id": 100, "payload": {"text": "t100"}}],
             "next_page_offset": None},
        ]
        seen = _patch_reembed_io(monkeypatch, mod, pages)

        result = mod.reembed_collection("nexus")

        assert seen == [None, "off-2"]          # API offset wins
        assert result["reembedded"] == mod.SCROLL_SIZE + 1

    def test_falls_back_to_last_point_id(self, monkeypatch, reembed_mod):
        mod = reembed_mod
        pages = [
            {"points": [{"id": i, "payload": {"text": f"t{i}"}}
                        for i in range(mod.SCROLL_SIZE)]},  # no offset keys at all
            {"points": [{"id": 100, "payload": {"text": "t100"}}]},
        ]
        seen = _patch_reembed_io(monkeypatch, mod, pages)

        mod.reembed_collection("nexus")

        assert seen == [None, mod.SCROLL_SIZE - 1]  # last point id of page 1

    def test_docstring_no_longer_claims_the_opposite(self):
        src = _read("reembed_voyage4.py")
        assert "returns next_offset=None even when more points exist" not in src


# ── H3: promote() records the staging draft as promoted_from ─────────────────


class TestH3PromotedFrom:
    def test_factversion_promote_sets_promoted_from(self):
        from nexus.lifecycle import FactVersion

        pending = FactVersion.new_pending({"text": "hi"}, fact_id="f1")
        canonical = FactVersion.promote(pending)

        assert canonical.promoted_from == pending.version_id
        assert canonical.to_dict()["promoted_from"] == pending.version_id

    def test_update_promotion_records_draft_in_payload(self, monkeypatch):
        import nexus.staging as st
        from nexus.lifecycle import FactVersion

        canonical_a = FactVersion.promote(
            FactVersion.new_pending({"text": "v1"}, fact_id="f1"))
        pending = FactVersion.new_pending(
            {"text": "v2"}, fact_id="f1", supersedes=canonical_a.version_id)

        captured: dict = {}
        monkeypatch.setattr(st, "_auto_ensure_collections", lambda *a, **k: None)
        monkeypatch.setattr(st, "_get_current_canonical", lambda *a, **k: canonical_a)
        monkeypatch.setattr(st, "_write_canonical", lambda v, *a, **k: None)

        def cap_upsert(v, *a, **k):
            captured["payload"] = v.to_dict()
            return v.version_id

        monkeypatch.setattr(st, "_upsert_point", cap_upsert)

        st.promote(pending, reason="test")

        # supersedes points at the previous canonical, promoted_from at the draft
        assert captured["payload"]["supersedes"] == canonical_a.version_id
        assert captured["payload"]["promoted_from"] == pending.version_id

    def test_supersedes_set_unions_promoted_from(self, monkeypatch):
        import nexus.staging as st

        class _Resp:
            def json(self):
                return {"result": {"points": [
                    {"payload": {"supersedes": "S1"}},
                    {"payload": {"promoted_from": "P1"}},
                    {"payload": {"supersedes": "S2", "promoted_from": "P2"}},
                    {"payload": {}},
                ]}}

        from types import SimpleNamespace
        monkeypatch.setattr(st, "requests",
                            SimpleNamespace(post=lambda *a, **k: _Resp()))

        # Nr 270 (W18): the scroll response is now checked via raise_for_status
        # and paged; the mock response must speak that API.
        def _raise(self):
            pass
        _Resp.raise_for_status = _raise

        assert st._get_canonical_supersedes_set() == {"S1", "P1", "S2", "P2"}


# ── H4: ollama dimension is probed, not hardcoded ────────────────────────────


class TestH4OllamaDimProbe:
    @staticmethod
    def _force_ollama(monkeypatch, probed):
        import nexus.staging as st

        monkeypatch.setattr(st, "_EMBED_DIM_CACHE", None)
        monkeypatch.setattr(st, "_EMBED_PROVIDER", None)
        monkeypatch.setattr(st, "_read_preferred_provider", lambda: "ollama")
        monkeypatch.setattr(st, "_try_init_provider", lambda p: p == "ollama")
        monkeypatch.setattr(st, "_probe_ollama_dim", lambda: probed)
        return st

    def test_probed_dimension_is_used(self, monkeypatch):
        st = self._force_ollama(monkeypatch, 1024)
        assert st._detect_vector_size() == 1024

    def test_probe_failure_falls_back_to_map_value(self, monkeypatch):
        st = self._force_ollama(monkeypatch, None)
        assert st._detect_vector_size() == 768

    def test_probe_hits_embed_endpoint_with_model(self, monkeypatch):
        import nexus.staging as st

        monkeypatch.setattr(st, "_OLLAMA_MODEL", "bge-m3")
        bodies: list = []

        class _Resp:
            def json(self):
                return {"embeddings": [[0.0] * 1024]}

        def fake_post(url, json=None, timeout=None):
            bodies.append((url, json))
            return _Resp()

        from types import SimpleNamespace
        monkeypatch.setattr(st, "requests", SimpleNamespace(post=fake_post))

        assert st._probe_ollama_dim() == 1024
        assert bodies[0][0].endswith("/api/embed")
        assert bodies[0][1]["model"] == "bge-m3"

    def test_probe_without_model_returns_none(self, monkeypatch):
        import nexus.staging as st

        monkeypatch.setattr(st, "_OLLAMA_MODEL", None)
        assert st._probe_ollama_dim() is None


# ── H5: bench p95 index (ceiling, clamped) ───────────────────────────────────


class TestH5P95Index:
    def test_uses_clamped_ceiling_index(self):
        src = _read("bench_latency.py")
        expr = "lat[min(len(lat) - 1, max(0, math.ceil(0.95 * len(lat)) - 1))]"
        assert f"p95 = {expr}" in src
        # the old off-by-one form must be gone
        assert "lat[int(len(lat) * 0.95) - 1]" not in src

    def test_p95_value_for_known_list(self):
        src = _read("bench_latency.py")
        expr = "lat[min(len(lat) - 1, max(0, math.ceil(0.95 * len(lat)) - 1))]"
        assert expr in src  # the value below is computed from the shipped form

        lat = sorted([10, 20, 30, 40, 50, 60, 70, 80, 90, 100])
        assert eval(expr, {"math": math, "lat": lat}) == 100  # was 90 before

        # single-sample and tiny lists must not index out of range
        assert eval(expr, {"math": math, "lat": [5]}) == 5
        assert eval(expr, {"math": math, "lat": [5, 9]}) == 9


# ── H6: bench does not mutate live SICA/flywheel state ───────────────────────


class TestH6BenchIsSideEffectFree:
    def test_state_mutators_are_shadowed(self):
        src = _read("bench_latency.py")
        assert "prov._bump_agent_stats = lambda *a, **k: None" in src
        assert "prov._flywheel_bump = lambda *a, **k: None" in src

    def test_shadowing_happens_before_recall(self):
        src = _read("bench_latency.py")
        patch_idx = src.index("prov._bump_agent_stats = lambda")
        assert patch_idx < src.index("prov._recall(")
        # both overrides live in the same setup block
        assert patch_idx < src.index("prov._flywheel_bump = lambda")


# ── H7: backfill_shard uses NEXUS_COLLECTION ─────────────────────────────────


class TestH7CollectionFromEnv:
    def test_coll_resolved_from_env(self):
        src = _read("backfill_shard.py")
        line = 'coll = os.environ.get("NEXUS_COLLECTION", "nexus")'
        assert line in src

        ns: dict = {}
        _os = __import__("os")
        exec(line, {"os": _os}, ns)
        assert ns["coll"] == _os.environ.get("NEXUS_COLLECTION", "nexus")

    def test_env_override_and_default(self, monkeypatch):
        src = _read("backfill_shard.py")
        line = 'coll = os.environ.get("NEXUS_COLLECTION", "nexus")'
        assert line in src

        monkeypatch.setenv("NEXUS_COLLECTION", "my-shard-coll")
        ns: dict = {}
        exec(line, {"os": __import__("os")}, ns)
        assert ns["coll"] == "my-shard-coll"

        monkeypatch.delenv("NEXUS_COLLECTION")
        ns = {}
        exec(line, {"os": __import__("os")}, ns)
        assert ns["coll"] == "nexus"

    def test_scroll_call_uses_coll(self):
        src = _read("backfill_shard.py")
        assert 'client.scroll("nexus"' not in src
        # Nr 277 (W18): the dead first scroll loop is gone — exactly ONE pass
        assert src.count("client.scroll(coll,") == 1


# ── H8: store-fact tuple unpacking (moved to Consolidator.consolidate_point) ─


class TestH8StoreFactTuple:
    def test_tuple_assignment(self):
        # Nr 279 (W18): backfill_shard no longer reaches into underscored
        # internals — the tuple-unpacking contract moved into the public
        # consolidate_point() entry point on Consolidator.
        src = _read("backfill_shard.py")
        assert "c._store_fact" not in src
        assert "c.consolidate_point(" in src

        cons_src = (REPO_ROOT / "src" / "nexus_memory" / "consolidation.py").read_text()
        assert "new_id, _fact_scope = self._store_fact(" in cons_src


# ── H9: consolidation marks partial failures ─────────────────────────────────


class TestH9PartialFailureMark:
    @staticmethod
    def _build(monkeypatch, llm_json, resolve_fn, store_fn, dry_run=False):
        from types import SimpleNamespace

        from nexus_memory.consolidation import Consolidator

        raw = SimpleNamespace(
            id="raw-1",
            payload={"content": "some session text", "category": "session",
                     "lifecycle_status": "canonical"})

        c = Consolidator(SimpleNamespace(client=SimpleNamespace()), "test-coll")
        monkeypatch.setattr(c, "_load_pending_supersedes", lambda: [])
        monkeypatch.setattr(c, "_retry_pending_supersedes", lambda p: (0, []))
        monkeypatch.setattr(c, "_next_raw_batch", lambda limit: [raw])
        monkeypatch.setattr(c, "_llm", lambda prompt: llm_json)
        monkeypatch.setattr(c, "_resolve_conflicts", resolve_fn)
        monkeypatch.setattr(c, "_store_fact", store_fn)

        marks: list = []
        monkeypatch.setattr(c, "_mark_consolidated",
                            lambda pid, n: marks.append((pid, n)))
        return c, marks

    def test_partial_failure_still_marks(self, monkeypatch):
        calls = {"n": 0}

        def store(fact, pid, source_payload=None):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise RuntimeError("boom")
            return ("new-id", "default")

        c, marks = self._build(
            monkeypatch, '{"facts": ["fact one", "fact two"]}',
            lambda fact, access_level="private": ("ok", []), store)

        c.run()

        # one fact was stored before the blow-up → the source must be marked
        # so it is not re-distilled (and duplicated) on the next tick.
        assert marks == [("raw-1", 1)]

    def test_failure_before_any_store_does_not_mark(self, monkeypatch):
        def store(fact, pid, source_payload=None):
            raise RuntimeError("boom")

        c, marks = self._build(
            monkeypatch, '{"facts": ["fact one"]}',
            lambda fact, access_level="private": ("ok", []), store)

        c.run()

        assert marks == []

    def test_dry_run_never_marks(self, monkeypatch):
        def resolve(fact, access_level="private"):
            if fact == "fact two":
                raise RuntimeError("boom")
            return ("ok", [])

        c, marks = self._build(
            monkeypatch, '{"facts": ["fact one", "fact two"]}', resolve,
            lambda *a, **k: ("new-id", "default"))

        c.run(dry_run=True)

        assert marks == []


# ── H10: chat_wizard.apply_choice with key_env=None ──────────────────────────


class TestH10KeyEnvNone:
    @staticmethod
    def _provider(key_env):
        return {"id": "fake", "name": "Fake", "dims": 384, "quality": "basic",
                "type": "local", "key_url": "", "key_env": key_env,
                "icon": "x", "pip_package": None}

    def _patch_env_io(self, monkeypatch):
        import nexus_memory.chat_wizard as cw

        monkeypatch.setattr(cw, "_load_config", lambda: {})
        saved: dict = {}
        monkeypatch.setattr(cw, "_save_config", lambda cfg: saved.update(cfg))

        calls: list = []
        import nexus_memory.env_secret_store as ess
        monkeypatch.setattr(ess, "read_env_key",
                            lambda path, key: calls.append(key) or "")
        return cw, saved, calls

    def test_key_env_none_never_reads_or_writes_environ(self, monkeypatch):
        cw, saved, calls = self._patch_env_io(monkeypatch)
        monkeypatch.setattr(cw, "PROVIDERS", [self._provider(None)])

        result = cw.apply_choice("fake")

        assert "error" not in result
        assert saved.get("embedding_provider") == "fake"
        # the backfill branch must be skipped entirely for keyless providers
        assert calls == []

    def test_key_env_present_backfills_from_dotenv(self, monkeypatch):
        cw, _saved, calls = self._patch_env_io(monkeypatch)
        monkeypatch.setattr(cw, "PROVIDERS", [self._provider("NEXUS_WAVE9_KEY")])
        monkeypatch.delenv("NEXUS_WAVE9_KEY", raising=False)

        import nexus_memory.env_secret_store as ess

        def fake_read(path, key):
            calls.append(key)
            return "sekret"

        monkeypatch.setattr(ess, "read_env_key", fake_read)

        cw.apply_choice("fake")

        assert calls == ["NEXUS_WAVE9_KEY"]
        assert __import__("os").environ["NEXUS_WAVE9_KEY"] == "sekret"
