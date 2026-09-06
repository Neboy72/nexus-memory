"""Tests for Active Guardrails - memory-driven prevention of destructive actions.

Tests cover:
- Action classification (pattern matching for rm, kill, drop, etc.)
- Target extraction (paths, collections from commands)
- Path matching (exact, subpath, wildcard)
- GuardrailEngine with mocked Qdrant client
- Override recording with audit trail
- Quick check (pattern-only mode without Qdrant)
"""

import pytest
from unittest.mock import MagicMock, patch
from dataclasses import dataclass

from nexus_memory.guardrails import (
    GuardrailAction,
    GuardrailVerdict,
    GuardrailResult,
    GuardrailEngine,
    classify_action,
    extract_targets,
    quick_check,
)


# ---------------------------------------------------------------------------
# classify_action tests
# ---------------------------------------------------------------------------

class TestClassifyAction:
    """Test destructive command pattern matching."""

    def test_rm_rf_is_delete(self):
        assert classify_action("rm -rf ~/nexus-memory-test/") == GuardrailAction.DELETE

    def test_rm_f_is_delete(self):
        assert classify_action("rm -f somefile.txt") == GuardrailAction.DELETE

    def test_rmdir_is_delete(self):
        assert classify_action("rmdir old_dir") == GuardrailAction.DELETE

    def test_drop_is_delete(self):
        assert classify_action("DROP TABLE users") == GuardrailAction.DELETE

    def test_truncate_is_delete(self):
        assert classify_action("truncate -s 0 logfile.txt") == GuardrailAction.DELETE

    def test_kill_9_is_kill(self):
        assert classify_action("kill -9 12345") == GuardrailAction.KILL

    def test_pkill_is_kill(self):
        assert classify_action("pkill -f ollama") == GuardrailAction.KILL

    def test_killall_is_kill(self):
        assert classify_action("killall python") == GuardrailAction.KILL

    def test_pip_install_is_install(self):
        assert classify_action("pip install requests") == GuardrailAction.INSTALL

    def test_npm_install_is_install(self):
        assert classify_action("npm install express") == GuardrailAction.INSTALL

    def test_brew_install_is_install(self):
        assert classify_action("brew install wget") == GuardrailAction.INSTALL

    def test_write_file_is_overwrite(self):
        assert classify_action("write_file /config.yaml") == GuardrailAction.OVERWRITE

    def test_recreate_collection_is_recreate(self):
        assert classify_action("recreate_collection nexus") == GuardrailAction.RECREATE

    def test_ls_is_not_destructive(self):
        assert classify_action("ls -la /home") is None

    def test_cat_is_not_destructive(self):
        assert classify_action("cat /etc/hosts") is None

    def test_echo_is_not_destructive(self):
        assert classify_action("echo hello") is None

    def test_git_status_is_not_destructive(self):
        assert classify_action("git status") is None

    def test_empty_command_is_none(self):
        assert classify_action("") is None

    def test_uninstall_is_delete(self):
        assert classify_action("pip uninstall requests") == GuardrailAction.DELETE

    def test_find_delete_is_delete(self):
        assert classify_action("find . -delete") == GuardrailAction.DELETE

    def test_git_clean_is_delete(self):
        assert classify_action("git clean -fdx") == GuardrailAction.DELETE

    def test_dd_of_is_delete(self):
        assert classify_action("dd if=/dev/zero of=/dev/sda") == GuardrailAction.DELETE

    def test_del_f_is_delete(self):
        assert classify_action("del /f /s /q somefile") == GuardrailAction.DELETE

    def test_rm_bare_is_delete(self):
        """rm without -r/-f is still destructive."""
        assert classify_action("rm /protected/file") == GuardrailAction.DELETE

    def test_rm_with_other_flags_is_delete(self):
        assert classify_action("rm -i somefile") == GuardrailAction.DELETE

    def test_kill_without_9_is_kill(self):
        """kill with any signal, not only -9, is destructive."""
        assert classify_action("kill 12345") == GuardrailAction.KILL

    def test_kill_term_is_kill(self):
        assert classify_action("kill -TERM 12345") == GuardrailAction.KILL


# ---------------------------------------------------------------------------
# extract_targets tests
# ---------------------------------------------------------------------------

class TestExtractTargets:
    """Test path/collection extraction from commands."""

    def test_extract_unix_path(self):
        targets = extract_targets("rm -rf ~/nexus-memory-test/")
        assert any("nexus-memory-test" in t for t in targets)

    def test_extract_absolute_path(self):
        targets = extract_targets("rm -rf /home/user/data")
        assert any("/home/user/data" in t for t in targets)

    def test_extract_no_path(self):
        targets = extract_targets("kill -9 12345")
        # The PID is captured as an (unresolvable) operand so destructive
        # actions on unknown targets can be blocked instead of passing
        assert any("12345" in t for t in targets)

    def test_extract_collection_name(self):
        targets = extract_targets("DELETE collection=nexus")
        assert "nexus" in targets

    def test_extract_relative_path_operand(self):
        """Bare relative operands of destructive commands are captured."""
        targets = extract_targets("rm -rf protected")
        assert any("protected" in t for t in targets)

    def test_extract_variable_reference(self):
        targets = extract_targets("rm -rf $BACKUP_DIR")
        assert any("BACKUP_DIR" in t for t in targets)

    def test_extract_variable_reference_braces(self):
        targets = extract_targets("rm -rf ${BACKUP_DIR}")
        assert any("BACKUP_DIR" in t for t in targets)


# ---------------------------------------------------------------------------
# Path matching tests
# ---------------------------------------------------------------------------

class TestPathMatching:
    """Test path normalization and matching logic."""

    def test_exact_match(self):
        assert GuardrailEngine._path_matches("/foo/bar", "/foo/bar")

    def test_no_match_different_paths(self):
        assert not GuardrailEngine._path_matches("/foo/bar", "/foo/baz")

    def test_subpath_matches(self):
        assert GuardrailEngine._path_matches("/foo/bar/baz", "/foo/bar")

    def test_parent_does_not_match_child(self):
        assert not GuardrailEngine._path_matches("/foo", "/foo/bar")

    def test_wildcard_matches_subpath(self):
        assert GuardrailEngine._path_matches("/foo/anything", "/foo/*")

    def test_wildcard_does_not_match_parent(self):
        assert not GuardrailEngine._path_matches("/foo", "/foo/*")

    def test_normalized_paths_case_insensitive(self):
        assert GuardrailEngine._path_matches("/Foo/Bar", "/foo/bar")

    def test_normalized_trailing_slash(self):
        assert GuardrailEngine._path_matches("/foo/bar/", "/foo/bar")

    def test_recursive_delete_blocks_protected_child_inside_target(self):
        """Deleting a parent subtree containing the protected path matches."""
        assert GuardrailEngine._path_matches("/data", "/data/protected", recursive=True)

    def test_parent_dir_bypass_blocked_end_to_end(self):
        """rm -rf /data blocks when /data/protected is protected."""
        points = [MockPoint(
            id="rule1",
            payload={"content": "NIEMALS /data/protected loeschen - kritisch", "category": "rule"},
        )]
        client = MagicMock()
        client.scroll.return_value = (points, None)
        engine = GuardrailEngine(client)
        result = engine.check_action("rm -rf /data")
        assert result.verdict == GuardrailVerdict.BLOCK

    def test_nonrecursive_parent_still_allows(self):
        """Without recursion a parent target does not match (unchanged)."""
        assert not GuardrailEngine._path_matches("/data", "/data/protected")


# ---------------------------------------------------------------------------
# Symlink resolution tests
# ---------------------------------------------------------------------------

class TestSymlinkResolution:
    """Symlink aliases must compare equal to the real protected path."""

    def test_symlink_normalization_matches_real_path(self, tmp_path):
        import os
        real = tmp_path / "real_secret.txt"
        real.write_text("secret")
        link = tmp_path / "alias.txt"
        os.symlink(str(real), str(link))
        engine = GuardrailEngine.__new__(GuardrailEngine)
        assert engine._normalize_path(str(link)) == engine._normalize_path(str(real))

    def test_symlink_alias_write_blocks(self, tmp_path):
        """Writing through a symlink to a protected file is blocked."""
        import os
        real = tmp_path / "config.yaml"
        real.write_text("cfg")
        link = tmp_path / "alias.yaml"
        os.symlink(str(real), str(link))
        client = MagicMock()
        client.scroll.return_value = ([MockPoint(
            id="rule1",
            payload={"content": f"NIEMALS {real} ueberschreiben", "category": "rule"},
        )], None)
        engine = GuardrailEngine(client)
        result = engine.check_action(
            "write_file",
            tool_name="write_file",
            tool_input={"path": str(link), "content": "overwrite"},
        )
        assert result.verdict == GuardrailVerdict.BLOCK


# ---------------------------------------------------------------------------
# GuardrailEngine tests with mocked Qdrant
# ---------------------------------------------------------------------------

@dataclass
class MockPoint:
    """Mock Qdrant point for testing."""
    id: str
    payload: dict


class TestGuardrailEngine:
    """Test the guardrail engine with a mocked Qdrant client."""

    def _make_mock_client(self, rules: list[dict]) -> MagicMock:
        """Create a mock Qdrant client that returns the given rules."""
        client = MagicMock()
        points = []
        for rule in rules:
            points.append(MockPoint(
                id=rule.get("id", "test-id"),
                payload={
                    "content": rule["text"],
                    "category": "rule",
                    "access_level": "private",
                },
            ))
        client.scroll.return_value = (points, None)
        return client

    def test_non_destructive_action_allowed(self):
        client = self._make_mock_client([])
        engine = GuardrailEngine(client)
        result = engine.check_action("ls -la /home")
        assert result.verdict == GuardrailVerdict.ALLOW
        assert result.allowed

    def test_destructive_no_target_allowed(self):
        """A PID-only kill passes when the rule store was fully loaded."""
        client = self._make_mock_client([])
        engine = GuardrailEngine(client)
        result = engine.check_action("kill -9 12345")
        # The PID operand is now extracted; with a complete rule load and no
        # match this resolves to a normal ALLOW on an unprotected target.
        assert result.verdict == GuardrailVerdict.ALLOW
        assert "unprotected target" in result.reason.lower()

    def test_destructive_unprotected_target_allowed(self):
        client = self._make_mock_client([
            {"text": "NIEMALS ~/nexus-memory-test/ löschen - Testgelände", "id": "rule1"}
        ])
        engine = GuardrailEngine(client)
        result = engine.check_action("rm -rf /tmp/some_other_dir")
        assert result.verdict == GuardrailVerdict.ALLOW
        assert "unprotected" in result.reason.lower()

    def test_destructive_protected_target_blocked(self):
        client = self._make_mock_client([
            {"text": "NIEMALS ~/nexus-memory-test/ löschen - Testgelände", "id": "rule1"}
        ])
        engine = GuardrailEngine(client)
        result = engine.check_action("rm -rf ~/nexus-memory-test/")
        assert result.verdict == GuardrailVerdict.BLOCK
        assert not result.allowed
        assert len(result.matched_rules) > 0

    def test_block_includes_matched_rule_details(self):
        client = self._make_mock_client([
            {"text": "NIEMALS ~/nexus-memory-test/ löschen - Testgelände", "id": "rule1"}
        ])
        engine = GuardrailEngine(client)
        result = engine.check_action("rm -rf ~/nexus-memory-test/")
        assert result.verdict == GuardrailVerdict.BLOCK
        assert result.matched_rules[0]["source_memory_id"] == "rule1"
        assert "nexus-memory-test" in result.matched_rules[0]["protected_path"]

    def test_non_protection_rule_ignored(self):
        """Rules without protection keywords should not block."""
        client = self._make_mock_client([
            {"text": "Git commits should use conventional format", "id": "rule2"}
        ])
        engine = GuardrailEngine(client)
        result = engine.check_action("rm -rf ~/some-path/")
        assert result.verdict == GuardrailVerdict.ALLOW

    def test_rules_paginated_until_end(self):
        """Scroll offset is followed until the end of the rule set."""
        def make_points(start, n):
            return [MockPoint(
                id=f"rule{start + i}",
                payload={
                    "content": f"NIEMALS /data/dir{start + i} loeschen",
                    "category": "rule",
                },
            ) for i in range(n)]
        client = MagicMock()
        client.scroll.side_effect = [
            (make_points(1, 200), "offset-page-2"),
            (make_points(201, 150), None),
        ]
        engine = GuardrailEngine(client)
        rules = engine._load_protected_rules(force_refresh=True)
        assert len(rules) == 350
        assert client.scroll.call_count == 2
        second_call = client.scroll.call_args_list[1]
        assert second_call.kwargs.get("offset") == "offset-page-2"

    def test_cache_preserved_on_load_failure(self):
        """A failed reload must not wipe a complete cache."""
        points = [MockPoint(
            id="rule1",
            payload={"content": "NIEMALS /data/protected loeschen", "category": "rule"},
        )]
        client = MagicMock()
        client.scroll.return_value = (points, None)
        engine = GuardrailEngine(client)
        rules = engine._load_protected_rules(force_refresh=True)
        assert len(rules) == 1
        assert engine._last_load_state == "fresh"
        client.scroll.side_effect = Exception("Qdrant down")
        rules2 = engine._load_protected_rules(force_refresh=True)
        # Stale cache served as additional layer, not replaced while broken
        assert len(rules2) == 1
        assert engine._last_load_state == "degraded"

    def test_collection_rule_loaded_without_path(self):
        """Path-less collection protection rules are loaded too."""
        points = [MockPoint(
            id="rule-coll",
            payload={"content": "NIEMALS delete collection=prod-memories", "category": "rule"},
        )]
        client = MagicMock()
        client.scroll.return_value = (points, None)
        engine = GuardrailEngine(client)
        rules = engine._load_protected_rules(force_refresh=True)
        assert any(r.get("collection") == "prod-memories" for r in rules)
        client = self._make_mock_client([])
        engine = GuardrailEngine(client)
        engine._load_protected_rules()
        assert len(engine._cache) > 0 or client.scroll.call_count >= 1
        engine.clear_cache()
        assert len(engine._cache) == 0

    def test_write_file_path_check(self):
        """write_file on protected config should block."""
        client = self._make_mock_client([
            {"text": "NIEMALS ~/.hermes/config.yaml überschreiben", "id": "rule3"}
        ])
        engine = GuardrailEngine(client)
        result = engine.check_action(
            "write_file",
            tool_name="write_file",
            tool_input={"path": "~/.hermes/config.yaml", "content": "new config"},
        )
        assert result.verdict == GuardrailVerdict.BLOCK

    def test_qdrant_failure_blocks_destructive(self):
        """Fail-closed: unreachable Qdrant blocks destructive actions."""
        client = MagicMock()
        client.scroll.side_effect = Exception("Qdrant unreachable")
        engine = GuardrailEngine(client)
        result = engine.check_action("rm -rf ~/nexus-memory-test/")
        assert result.verdict == GuardrailVerdict.BLOCK
        assert "not fully loaded" in result.reason

    def test_qdrant_failure_allows_nondestructive(self):
        """Fail-closed only applies to destructive actions, never to reads."""
        client = MagicMock()
        client.scroll.side_effect = Exception("Qdrant unreachable")
        engine = GuardrailEngine(client)
        result = engine.check_action("ls -la /home")
        assert result.verdict == GuardrailVerdict.ALLOW

    def test_cache_serves_as_additional_layer_on_failure(self):
        """On load failure, a complete cached rule set still blocks matches."""
        points = [MockPoint(
            id="rule1",
            payload={"content": "NIEMALS ~/nexus-memory-test/ loeschen", "category": "rule"},
        )]
        client = MagicMock()
        client.scroll.return_value = (points, None)
        engine = GuardrailEngine(client)
        engine._load_protected_rules()
        assert engine._last_load_state == "fresh"
        # Fresh engine sharing the populated cache, Qdrant now unreachable:
        # the cache still blocks the protected target as an additional layer.
        import time as _time
        client.scroll.side_effect = Exception("Qdrant down")
        engine2 = GuardrailEngine(client)
        engine2._cache = list(engine._cache)
        engine2._cache_time = _time.time() - 999.0  # TTL expired -> real reload
        result = engine2.check_action("rm -rf ~/nexus-memory-test/")
        assert result.verdict == GuardrailVerdict.BLOCK
        assert engine2._last_load_state == "degraded"


# ---------------------------------------------------------------------------
# Override recording tests
# ---------------------------------------------------------------------------

class TestOverrideRecording:
    """Test guardrail override with audit trail."""

    def test_override_recorded_to_qdrant(self):
        client = MagicMock()
        engine = GuardrailEngine(client)
        override_id = engine.record_override(
            command="rm -rf ~/nexus-memory-test/",
            matched_rules=[{
                "target": "~/nexus-memory-test/",
                "protected_path": "~/nexus-memory-test/",
                "rule_text": "NIEMALS löschen",
                "source_memory_id": "rule1",
                "action": "delete",
            }],
            reasoning="User explicitly authorized cleanup of sandbox after backup",
            agent_id="kiosha",
        )
        assert override_id is not None
        # Verify upsert was called with audit entry
        assert client.upsert.called
        call_args = client.upsert.call_args
        points = call_args.kwargs.get("points", []) or call_args.args[0] if call_args.args else []
        if hasattr(points, '__iter__') and not isinstance(points, dict):
            points = list(points)
            if points:
                payload = points[0].payload if hasattr(points[0], 'payload') else points[0].get("payload", {})
                assert payload.get("guardrail_override") is True
                assert payload.get("agent_id") == "kiosha"

    def test_override_returns_uuid(self):
        client = MagicMock()
        engine = GuardrailEngine(client)
        override_id = engine.record_override(
            command="rm -rf test",
        matched_rules=[],
            reasoning="test reasoning here",
        )
        # Should be a valid UUID string
        import uuid
        uuid.UUID(override_id)  # Raises if invalid

    def test_override_rejects_empty_reasoning(self):
        """Empty reasoning must raise, not return an override id."""
        client = MagicMock()
        engine = GuardrailEngine(client)
        with pytest.raises(ValueError):
            engine.record_override(
                command="rm -rf test",
                matched_rules=[],
                reasoning="",
            )

    def test_override_rejects_whitespace_reasoning(self):
        client = MagicMock()
        engine = GuardrailEngine(client)
        with pytest.raises(ValueError):
            engine.record_override(
                command="rm -rf test",
                matched_rules=[],
                reasoning="   ",
            )

    def test_override_rejects_short_reasoning(self):
        client = MagicMock()
        engine = GuardrailEngine(client)
        with pytest.raises(ValueError):
            engine.record_override(
                command="rm -rf test",
                matched_rules=[],
                reasoning="short",
            )

    def test_override_raises_when_upsert_fails(self):
        """Failed persistence must raise instead of returning an id."""
        client = MagicMock()
        client.upsert.side_effect = Exception("upsert failed")
        engine = GuardrailEngine(client)
        with pytest.raises(RuntimeError):
            engine.record_override(
                command="rm -rf test",
                matched_rules=[],
                reasoning="valid reasoning here",
            )

    def test_override_reasoning_validated_before_upsert(self):
        """Reasoning validation happens before any store access."""
        client = MagicMock()
        engine = GuardrailEngine(client)
        with pytest.raises(ValueError):
            engine.record_override(
                command="rm -rf test",
                matched_rules=[],
                reasoning="ab",
            )
        assert not client.upsert.called


# ---------------------------------------------------------------------------
# quick_check tests (pattern-only mode)
# ---------------------------------------------------------------------------

class TestQuickCheck:
    """Test the convenience quick_check function."""

    def test_quick_check_non_destructive(self):
        result = quick_check("ls -la", qdrant_client=None)
        assert result.verdict == GuardrailVerdict.ALLOW

    def test_quick_check_destructive_no_qdrant(self):
        result = quick_check("rm -rf ~/test/", qdrant_client=None)
        # Pattern-only mode: classifies but can't check memory
        assert result.verdict == GuardrailVerdict.ALLOW
        assert "pattern-only" in result.reason

    def test_quick_check_with_qdrant_client(self):
        client = MagicMock()
        client.scroll.return_value = ([], None)
        result = quick_check("ls -la", qdrant_client=client)
        assert result.verdict == GuardrailVerdict.ALLOW


# ---------------------------------------------------------------------------
# GuardrailResult tests
# ---------------------------------------------------------------------------

class TestGuardrailResult:
    """Test the result dataclass."""

    def test_allow_result_allowed_property(self):
        r = GuardrailResult(verdict=GuardrailVerdict.ALLOW)
        assert r.allowed

    def test_block_result_not_allowed(self):
        r = GuardrailResult(verdict=GuardrailVerdict.BLOCK)
        assert not r.allowed

    def test_override_result_allowed(self):
        r = GuardrailResult(verdict=GuardrailVerdict.OVERRIDE)
        assert r.allowed

    def test_to_dict_serializable(self):
        r = GuardrailResult(
            verdict=GuardrailVerdict.BLOCK,
            reason="test reason",
            matched_rules=[{"target": "/foo"}],
        )
        d = r.to_dict()
        assert d["verdict"] == "block"
        assert d["reason"] == "test reason"
        assert len(d["matched_rules"]) == 1