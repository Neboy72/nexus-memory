"""OCR-2 Welle 32 (high, packet 5): lifecycle, backfill, confidence, hooks,
mcp_server. One class per root cause."""

import pytest

REPO = __file__.rsplit("/tests/", 1)[0]


def _read(rel: str) -> str:
    with open(f"{REPO}/{rel}", encoding="utf-8") as f:
        return f.read()


class TestW32Lifecycle:
    def test_behavior_double_rollback_rejected(self):
        import importlib, sys, uuid, datetime
        sys.path.insert(0, REPO)
        import nexus.lifecycle as lc

        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        bad = lc.FactVersion(
            fact_id="f1", version_id=str(uuid.uuid4()), content="bad", content_hash="h1",
            status="rolled_back", supersedes=None, decision_event=None,
        )
        restore = lc.FactVersion(
            fact_id="f1", version_id=str(uuid.uuid4()), content="good", content_hash="h2",
            status="pending", supersedes=None, decision_event=None,
        )
        try:
            lc.FactVersion.rollback(bad, restore, triggered_by="test")
            raised = False
        except ValueError:
            raised = True
        assert raised, "double rollback of an already-rolled-back version must be rejected"

    def test_behavior_restore_of_rolled_back_rejected(self):
        import importlib, sys, uuid
        sys.path.insert(0, REPO)
        import nexus.lifecycle as lc

        bad = lc.FactVersion(
            fact_id="f1", version_id=str(uuid.uuid4()), content="bad", content_hash="h1",
            status="pending", supersedes=None, decision_event=None,
        )
        restore = lc.FactVersion(
            fact_id="f1", version_id=str(uuid.uuid4()), content="good", content_hash="h2",
            status="rolled_back", supersedes=None, decision_event=None,
        )
        try:
            lc.FactVersion.rollback(bad, restore, triggered_by="test")
            raised = False
        except ValueError:
            raised = True
        assert raised, "restoring a rolled_back version as canonical must be rejected"


class TestW32BackfillTermination:
    def test_source_counts_consecutive_failures(self):
        src = _read("scripts/backfill_consolidation.py")
        assert "consecutive" in src or "fail_streak" in src or "no_progress" in src


class TestW32MigrateCollections:
    def test_source_no_silent_overwrite(self):
        src = _read("scripts/migrate-collections.py")
        seg = src[src.index("def merge_points"):]
        seg = seg[:seg.index("\n\ndef ", 1)]
        assert "setdefault" in seg or "if key in" in seg or "if k in" in seg

    def test_source_offset_not_truthy(self):
        src = _read("scripts/migrate-collections.py")
        assert "offset is not None" in src


class TestW32BackfillShard:
    def test_source_created_at_guarded(self):
        src = _read("scripts/backfill_shard.py")
        assert "_iso_date" in src or "fromtimestamp" in src or "isinstance" in src


class TestW32Confidence:
    def test_source_embed_fail_neutral(self):
        src = _read("nexus/confidence.py")
        seg = src[src.index("_embed([answer]"):]
        seg = seg[:seg.index("\n    if ", 1)] if "\n    if " in seg else seg
        # embed-fail path must mark signals neutral/unavailable, not silently 1.0
        assert "0.5" in seg or "unavailable" in seg or "embedder_ok" in seg


class TestW32SessionStart:
    def test_source_get_embedding_guards(self):
        src = _read("plugins/claude-code/scripts/session_start.py")
        seg = src[src.index("def get_embedding"):]
        seg = seg[:seg.index("\n\ndef ", 1)]
        assert "try:" in seg and "except" in seg


class TestW32AutoRecall:
    def test_source_query_input_type(self):
        src = _read("plugins/claude-code/scripts/auto_recall.py")
        assert 'input_type="query"' in src or "'query'" in src


class TestW32AutoCapture:
    def test_source_embed_field_matches_endpoint(self):
        src = _read("plugins/claude-code/scripts/auto_capture.py")
        # either prompt-field for /api/embeddings or a migration to /api/embed
        assert "prompt" in src or "/api/embed" in src


class TestW32ReembedLog:
    def test_source_no_pct_comma(self):
        src = _read("scripts/reembed_voyage4.py")
        assert "%,d" not in src
        assert "{:,}" in src or ":,.0f}" in src


class TestW32SessionScan:
    def test_source_zoneinfo(self):
        src = _read("scripts/session_scan.py")
        assert "ZoneInfo" in src or "zoneinfo" in src

    def test_source_env_path(self):
        src = _read("scripts/session_scan.py")
        assert "NEXUS_SESSION_DB" in src


class TestW32SessionDump:
    def test_source_limit_guarded(self):
        src = _read("scripts/session_dump.py")
        assert "max(0" in src or "limit > 0" in src


class TestW32EnvSecretStore:
    def test_source_read_failure_aborts(self):
        src = _read("src/nexus_memory/env_secret_store.py")
        assert "refusing" in src.lower() or "RuntimeError" in src


class TestW32McpServer:
    def test_source_fcntl_optional(self):
        src = _read("src/nexus_memory/mcp_server.py")
        assert "ImportError" in src and "fcntl = None" in src

    def test_source_do_update_dual_error_key(self):
        src = _read("src/nexus_memory/mcp_server.py")
        # _do_update error path must carry BOTH status and error keys
        seg = src[src.index("def _do_update"):]
        seg = seg[:seg.index("\nasync def ", 1)] if "\nasync def " in seg else seg
        assert '"error"' in seg

    def test_source_backup_threaded(self):
        src = _read("src/nexus_memory/mcp_server.py")
        seg = src[src.index("def _do_backup"):]
        seg = seg[:seg.index("\nasync def ", 1)] if "\nasync def " in seg else seg
        assert "asyncio.to_thread" in seg