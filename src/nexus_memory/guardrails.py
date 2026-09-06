"""Active Guardrails - Memory-driven prevention of destructive actions.

Guards against destructive operations by checking Nexus Memory for
stored protection rules before an action is executed.

This is the active layer that turns passive memory retrieval into
policy enforcement. Instead of hoping the agent searches for relevant
"NIEMALS" rules, the guardrail checks proactively before every
destructive operation.

Design:
- Protected paths/collections stored in Qdrant (memory-driven, not hardcoded)
- Pre-action check returns allow/block/override
- Override requires explicit reasoning + creates audit trail
- Zero LLM cost - pure pattern matching + Qdrant lookup

Based on Issue #6 (Active Guardrails) and two real incidents:
- Qdrant-Wipe (18.06.2026): 8644 points deleted by ensureCollection()
- Sandbox Deletion (23.06.2026): ~/nexus-memory-test/ deleted despite
  memory explicitly marking it as protected
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class GuardrailAction(str, Enum):
    """Action categories the guardrail evaluates."""
    DELETE = "delete"          # rm, del, drop, remove
    OVERWRITE = "overwrite"    # write_file (full replace), > redirect
    KILL = "kill"              # kill, pkill, terminate
    RECREATE = "recreate"      # collection recreate, truncate
    INSTALL = "install"        # pip install, npm install (uninstall = delete)


class GuardrailVerdict(str, Enum):
    """Result of a guardrail check."""
    ALLOW = "allow"
    BLOCK = "block"
    OVERRIDE = "override"


@dataclass
class GuardrailResult:
    """Outcome of a check_action call."""
    verdict: GuardrailVerdict
    reason: str = ""
    matched_rules: list[dict] = field(default_factory=list)
    override_id: Optional[str] = None

    @property
    def allowed(self) -> bool:
        return self.verdict in (GuardrailVerdict.ALLOW, GuardrailVerdict.OVERRIDE)

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "reason": self.reason,
            "matched_rules": self.matched_rules,
            "override_id": self.override_id,
        }


# ---------------------------------------------------------------------------
# Pattern matching - identifies destructive actions in commands
# ---------------------------------------------------------------------------

# Regex patterns for destructive commands
_DESTRUCTIVE_PATTERNS: list[tuple[GuardrailAction, re.Pattern]] = [
    # Delete operations
    # NOTE: rm/rmdir/unlink/shred are destructive regardless of which options
    # are passed ('rm /protected/file' is just as dangerous as 'rm -rf ...'),
    # so they are matched on the command word itself, not on flags.
    (GuardrailAction.DELETE, re.compile(r"\brm\s+\S", re.IGNORECASE)),
    (GuardrailAction.DELETE, re.compile(r"\brmdir\s+\S", re.IGNORECASE)),
    (GuardrailAction.DELETE, re.compile(r"\bunlink\s+\S", re.IGNORECASE)),
    (GuardrailAction.DELETE, re.compile(r"\bshred\s+\S", re.IGNORECASE)),
    (GuardrailAction.DELETE, re.compile(r"\bdel\b\s+/[fsq]", re.IGNORECASE)),
    (GuardrailAction.DELETE, re.compile(r"\bdrop\b", re.IGNORECASE)),
    (GuardrailAction.DELETE, re.compile(r"\btruncate\b", re.IGNORECASE)),
    (GuardrailAction.DELETE, re.compile(r"\buninstall\b", re.IGNORECASE)),
    (GuardrailAction.DELETE, re.compile(r"\bremove-item\b", re.IGNORECASE)),
    (GuardrailAction.DELETE, re.compile(r"\bfind\b.*-delete", re.IGNORECASE)),
    (GuardrailAction.DELETE, re.compile(r"\bgit\b.*clean.*-[fd]", re.IGNORECASE)),
    (GuardrailAction.DELETE, re.compile(r"\bdd\b.*\bof\b", re.IGNORECASE)),

    # Overwrite operations
    (GuardrailAction.OVERWRITE, re.compile(r"\bwrite_file\b", re.IGNORECASE)),
    (GuardrailAction.OVERWRITE, re.compile(r">\s*[^|&]")),  # redirect to file

    # Kill operations
    # NOTE: kill is destructive with any argument, not only with -9.
    (GuardrailAction.KILL, re.compile(r"\bkill\s+\S", re.IGNORECASE)),
    (GuardrailAction.KILL, re.compile(r"\bpkill\b", re.IGNORECASE)),
    (GuardrailAction.KILL, re.compile(r"\bkillall\b", re.IGNORECASE)),
    (GuardrailAction.KILL, re.compile(r"\btaskkill\b", re.IGNORECASE)),

    # Recreate operations (Qdrant collection recreation wipes all points)
    (GuardrailAction.RECREATE, re.compile(r"\brecreate_collection\b", re.IGNORECASE)),
    (GuardrailAction.RECREATE, re.compile(r"DELETE.*collection", re.IGNORECASE)),
    (GuardrailAction.RECREATE, re.compile(r"\bdrop\b.*collection", re.IGNORECASE)),

    # Install operations
    (GuardrailAction.INSTALL, re.compile(r"\bpip\b.*install", re.IGNORECASE)),
    (GuardrailAction.INSTALL, re.compile(r"\bnpm\b.*install", re.IGNORECASE)),
    (GuardrailAction.INSTALL, re.compile(r"\bbrew\b.*install", re.IGNORECASE)),
]


def classify_action(command: str) -> Optional[GuardrailAction]:
    """Classify a command string into a destructive action type.

    Returns None if the command is not destructive.
    """
    for action, pattern in _DESTRUCTIVE_PATTERNS:
        if pattern.search(command):
            return action
    return None


# ---------------------------------------------------------------------------
# Protected resource extraction - finds paths/collections in commands
# ---------------------------------------------------------------------------

_PATH_PATTERNS = [
    re.compile(r"~[\w./-]+"),                    # Home paths: ~/foo/bar
    re.compile(r"/[\w./-]+"),                    # Absolute paths: /foo/bar
    re.compile(r"\w:[\\/][\w\\./-]+"),           # Windows paths: C:\foo\bar
]
_COLLECTION_PATTERN = re.compile(r"collection[_\s=]+(\w+)", re.IGNORECASE)

# Environment variables used as targets ($BACKUP_DIR, ${BACKUP_DIR}, %BACKUP_DIR%)
_VARIABLE_PATTERN = re.compile(r"\$\{?\w+\}?|%[\w.-]+%")

# First operand of a destructive command word ('rm -rf protected',
# 'kill -9 12345', 'pip uninstall requests') - captured even when it is a
# bare relative name, a PID or a package name instead of an absolute path.
_BARE_OPERAND_PATTERN = re.compile(
    r"\b(?:rm|rmdir|shred|unlink|erase|truncate|kill|pkill|killall|taskkill"
    r"|drop|uninstall|remove-item)\b"
    r"(?:\s+(?:-{1,2}[\w-]+|/[A-Za-z]))*\s+([^\s;&|'\"`]+)",
    re.IGNORECASE,
)

# Operations that destroy a whole subtree (rm -r, del /s, ...)
_RECURSIVE_DELETE_PATTERN = re.compile(
    r"(?:^|\s)-{1,2}[a-zA-Z]*r[a-zA-Z]*(?=$|\s)|(?:^|\s)/s(?=$|\s)",
    re.IGNORECASE,
)


def extract_targets(command: str) -> list[str]:
    """Extract potential protected resource targets from a command.

    Returns a list of paths, collection names, variable references and bare
    destructive-command operands found in the command.
    Paths with ~ are expanded to the home directory.
    """
    import os
    targets: list[str] = []
    for pattern in _PATH_PATTERNS:
        for match in pattern.finditer(command):
            target = match.group(0).strip().strip("'\"")
            if target and len(target) > 2 and target not in ("~", "/", "."):
                # Expand ~ to home directory for consistent matching
                target = os.path.expanduser(target)
                targets.append(target)
    # Extract collection names (capture group 1 = the name, not the full match)
    for match in _COLLECTION_PATTERN.finditer(command):
        targets.append(match.group(1))
    # Extract variable references (cannot be resolved -> must not pass checks)
    for match in _VARIABLE_PATTERN.finditer(command):
        targets.append(match.group(0))
    # Extract the first operand of destructive command words, so relative
    # paths, PIDs and package names are captured as targets as well
    for match in _BARE_OPERAND_PATTERN.finditer(command):
        operand = match.group(1).strip().strip("'\"")
        if operand and not operand.startswith("-") and operand not in ("~", "/", "."):
            targets.append(os.path.expanduser(operand))
    # De-duplicate while preserving order
    deduped: list[str] = []
    seen: set[str] = set()
    for target in targets:
        if target not in seen:
            seen.add(target)
            deduped.append(target)
    return deduped


# ---------------------------------------------------------------------------
# Guardrail engine
# ---------------------------------------------------------------------------

class GuardrailEngine:
    """Core guardrail engine - checks actions against protected resources.

    The engine does NOT store protected paths itself. It queries Qdrant
    (via the MemoryStore) for rules stored with category="rule" that
    contain protection directives. This makes the guardrail memory-driven:
    storing a new "NIEMALS löschen" rule in Nexus Memory automatically
    registers it as a protected resource.
    """

    # Keywords in memory text that signal a protection rule
    PROTECTION_KEYWORDS = [
        "niemals", "never", "do not delete", "don't delete",
        "nicht löschen", "schützen", "protect", "protected",
        "testgelände", "sandbox", "production", "live",
        "notfall", "emergency", "critical",
    ]

    # Keywords that signal a path in the memory text
    PATH_KEYWORD = re.compile(r"[~]?/[\w./-]+|\w:[\\/][\w\\./-]+")

    # Keywords that signal a collection name in the memory text
    COLLECTION_KEYWORD = re.compile(r"collection[\s_=:\"']+([\w-]+)", re.IGNORECASE)

    def __init__(self, qdrant_client, collection_name: str = "nexus", vector_dim: int = 384):
        self.client = qdrant_client
        self.collection = collection_name
        self.vector_dim = vector_dim
        self._cache: list[dict] = []
        self._cache_time: float = 0
        self._cache_ttl: float = 60.0  # 1 minute cache
        # State of the last rule load: 'fresh' (complete load), 'cached'
        # (served from a previously complete load), 'degraded' (load failed,
        # cache returned as additional layer only) or 'unknown' (never loaded).
        self._last_load_state: str = "unknown"

    def _load_protected_rules(self, force_refresh: bool = False) -> list[dict]:
        """Load protection rules from Qdrant (category='rule' with protection keywords).

        Queries the Nexus Memory collection for entries that:
        1. Have category='rule'
        2. Contain protection keywords in the text

        Paginates with the scroll offset until the end of the result set so
        rules beyond the first page are never skipped. The cached rule list
        is only replaced after a complete, successful load; on failure the
        stale cache is returned as an additional layer and the load state is
        marked 'degraded'.

        Returns a list of {text, path, collection, source_memory_id} dicts.
        """
        import time
        now = time.time()
        if self._cache and not force_refresh and (now - self._cache_time) < self._cache_ttl:
            self._last_load_state = "cached"
            return self._cache

        rules: list[dict] = []
        try:
            from qdrant_client import models as qmodels

            # Scroll all rule-category entries, following the returned offset
            # until the end so every rule page is loaded.
            offset = None
            pages = 0
            while True:
                results = self.client.scroll(
                    collection_name=self.collection,
                    scroll_filter=qmodels.Filter(
                        must=[
                            qmodels.FieldCondition(
                                key="category",
                                match=qmodels.MatchValue(value="rule"),
                            )
                        ]
                    ),
                    limit=200,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
                if not results:
                    break
                points, next_offset = results
                if not points and not next_offset:
                    break

                for point in points:
                    payload = point.payload or {}
                    text = payload.get("content", "")
                    text_lower = text.lower()

                    # Check if this rule contains protection keywords
                    if not any(kw in text_lower for kw in self.PROTECTION_KEYWORDS):
                        continue

                    # Extract paths from the rule text
                    paths = self.PATH_KEYWORD.findall(text)
                    if paths:
                        for path in paths:
                            # Normalize: expand ~ and normalize path
                            import os
                            path_norm = os.path.expanduser(path)
                            rules.append({
                                "path": path_norm,
                                "rule_text": text[:200],
                                "source_memory_id": str(point.id),
                                "access_level": payload.get("access_level", "public"),
                            })
                    else:
                        # Path-less protection rules: extract collection names
                        collections = self.COLLECTION_KEYWORD.findall(text)
                        for coll in collections:
                            rules.append({
                                "path": "",
                                "collection": coll,
                                "rule_text": text[:200],
                                "source_memory_id": str(point.id),
                                "access_level": payload.get("access_level", "public"),
                            })
                        # Protection rule with no path AND no collection: treat
                        # the bare target it names as unknown-but-protected via
                        # text matching (see check_action).
                        if not collections:
                            rules.append({
                                "path": "",
                                "collection": "",
                                "text": text[:200].lower(),
                                "rule_text": text[:200],
                                "source_memory_id": str(point.id),
                                "access_level": payload.get("access_level", "public"),
                            })

                pages += 1
                if not next_offset:
                    break
                offset = next_offset
                if pages > 100:  # Hard safety bound against endless loops
                    logger.warning(
                        "Guardrail: rule scroll exceeded 100 pages, stopping"
                    )
                    break

            # Replace the cache only after a fully successful load
            self._cache = rules
            self._cache_time = now
            self._last_load_state = "fresh"
            logger.debug(f"Guardrail: loaded {len(rules)} protected rules from Qdrant")
        except Exception as e:
            logger.warning(f"Guardrail: failed to load rules from Qdrant: {e}")
            self._last_load_state = "degraded"
            # Serve the stale cache as an additional layer only; the load
            # state stays 'degraded' so check_action can block destructive
            # actions on incomplete information.
            return list(self._cache)

        return rules

    def check_action(
        self,
        command: str,
        tool_name: str = "",
        tool_input: Optional[dict] = None,
    ) -> GuardrailResult:
        """Check if an action is allowed, blocked, or needs override.

        Args:
            command: The command string to check (for terminal tools)
            tool_name: The tool being called (write_file, patch, terminal)
            tool_input: Full tool input dict (for write_file path checks)

        Returns:
            GuardrailResult with verdict, reason, and matched rules
        """
        # Step 1: Classify the action
        full_input = f"{tool_name} {command} {tool_input or ''}"
        action = classify_action(full_input)
        if action is None:
            return GuardrailResult(
                verdict=GuardrailVerdict.ALLOW,
                reason="Non-destructive action",
            )

        # Step 2: Extract targets (paths, collections) from the command
        targets = extract_targets(command)
        if tool_input:
            path = tool_input.get("path", "")
            if path:
                targets.append(path)

        if not targets:
            # Destructive action with no identifiable target: block when rule
            # loading is incomplete (unknown targets must not pass silently),
            # allow with a caution reason when rules were fully loaded.
            self._load_protected_rules()
            if self._last_load_state != "fresh":
                return GuardrailResult(
                    verdict=GuardrailVerdict.BLOCK,
                    reason=(
                        f"Destructive action ({action.value}) but no target "
                        f"identified and protection rules not fully loaded "
                        f"(state: {self._last_load_state})"
                    ),
                )
            return GuardrailResult(
                verdict=GuardrailVerdict.ALLOW,
                reason=f"Destructive action ({action.value}) but no protected target identified",
            )

        # Step 3: Load protected rules from Qdrant
        protected_rules = self._load_protected_rules()
        # 'degraded'/'unknown' = rule coverage incomplete; 'fresh' and
        # 'cached' (served from a previously complete load) are complete.
        incomplete_check = self._last_load_state in ("degraded", "unknown")
        recursive = bool(_RECURSIVE_DELETE_PATTERN.search(command))

        # Step 4: Check if any target matches a protected rule
        matched: list[dict] = []
        for target in targets:
            target_norm = self._normalize_path(target)
            for rule in protected_rules:
                if rule.get("collection"):
                    # Collection rule: match the target against the name
                    if target_norm == self._normalize_path(rule["collection"]):
                        matched.append({
                            "target": target,
                            "protected_collection": rule["collection"],
                            "rule_text": rule["rule_text"],
                            "source_memory_id": rule["source_memory_id"],
                            "action": action.value,
                        })
                        continue
                    if rule.get("path"):
                        # Both path and collection in one rule
                        rule_path_norm = self._normalize_path(rule["path"])
                        if self._path_matches(target_norm, rule_path_norm, recursive):
                            matched.append({
                                "target": target,
                                "protected_path": rule["path"],
                                "rule_text": rule["rule_text"],
                                "source_memory_id": rule["source_memory_id"],
                                "action": action.value,
                            })
                    continue
                if rule.get("path"):
                    rule_path_norm = self._normalize_path(rule["path"])
                    if self._path_matches(target_norm, rule_path_norm, recursive):
                        matched.append({
                            "target": target,
                            "protected_path": rule["path"],
                            "rule_text": rule["rule_text"],
                            "source_memory_id": rule["source_memory_id"],
                            "action": action.value,
                        })
                        continue
                # Path-less rule with raw text: block if the target string
                # appears inside the protection rule text
                rule_text = rule.get("text", "")
                if rule_text and target.strip().strip("'\"").lower() in rule_text:
                    matched.append({
                        "target": target,
                        "protected_rule_text": rule_text[:100],
                        "rule_text": rule["rule_text"],
                        "source_memory_id": rule["source_memory_id"],
                        "action": action.value,
                    })

        # Fail closed: with incomplete rule information, unknown destructive
        # targets must not be released.
        if not matched and incomplete_check:
            return GuardrailResult(
                verdict=GuardrailVerdict.BLOCK,
                reason=(
                    f"Destructive action ({action.value}) on target(s) "
                    f"{', '.join(targets[:3])} could not be fully verified "
                    f"because protection rules are not fully loaded "
                    f"(state: {self._last_load_state}). "
                    "Retry when the rule store is reachable."
                ),
                matched_rules=[],
            )

        if not matched:
            return GuardrailResult(
                verdict=GuardrailVerdict.ALLOW,
                reason=f"Destructive action ({action.value}) on unprotected target",
            )

        # Block!
        reasons = []
        for m in matched:
            reasons.append(
                f"Target '{m['target']}' matches protected path '{m['protected_path']}'"
                f" (rule: {m['rule_text'][:80]}...)"
            )

        return GuardrailResult(
            verdict=GuardrailVerdict.BLOCK,
            reason=" | ".join(reasons),
            matched_rules=matched,
        )

    def record_override(
        self,
        command: str,
        matched_rules: list[dict],
        reasoning: str,
        agent_id: str = "unknown",
    ) -> str:
        """Record a guardrail override with full audit trail.

        Returns the override_id. The override is stored as a Nexus Memory
        entry with category='session' for audit purposes.

        Raises:
            ValueError: if reasoning is empty/too short.
            RuntimeError: if the audit entry could not be persisted
                (upsert failed). No override_id is issued in that case.
        """
        if not reasoning or len(reasoning.strip()) < 10:
            raise ValueError(
                "Override requires explicit reasoning (min 10 characters, "
                "no whitespace-only)."
            )

        override_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).isoformat()

        audit_text = (
            f"GUARDRAIL OVERRIDE by {agent_id} at {created_at}\n"
            f"Command: {command[:200]}\n"
            f"Reasoning: {reasoning}\n"
            f"Overridden rules: {len(matched_rules)}\n"
        )
        for m in matched_rules:
            audit_text += f"  - {m['protected_path']}: {m['rule_text'][:100]}\n"

        try:
            from qdrant_client import models as qmodels
            vector = [0.0] * self.vector_dim  # Zero vector of correct dimension

            self.client.upsert(
                collection_name=self.collection,
                points=[qmodels.PointStruct(
                    id=override_id,
                    vector=vector,
                    payload={
                        "id": override_id,
                        "content": audit_text,
                        "category": "session",
                        "access_level": "private",
                        "created_at": created_at,
                        "lifecycle_status": "canonical",
                        "guardrail_override": True,
                        "overridden_rules": [m["source_memory_id"] for m in matched_rules],
                        "agent_id": agent_id,
                    },
                )],
            )
            logger.info(f"Guardrail override recorded: {override_id[:8]} by {agent_id}")
        except Exception as e:
            logger.warning(f"Guardrail: failed to record override: {e}")
            # Persisting the audit entry failed: do NOT hand out an override
            # id - raise so callers know the override was not recorded.
            raise RuntimeError(
                f"Override audit entry could not be persisted: {e}"
            ) from e

        return override_id

    @staticmethod
    def _normalize_path(path: str) -> str:
        """Normalize a path for comparison.

        Resolves symlinks against the real filesystem where the path exists
        so symlink aliases of protected resources compare equal to the real
        path. Non-existent paths fall back to lexical normalization.
        """
        import os
        path = os.path.expanduser(path)
        path = os.path.normpath(path)
        try:
            path = os.path.realpath(path)
        except OSError:
            pass
        return path.rstrip("/").lower()

    @staticmethod
    def _path_matches(target: str, protected: str, recursive: bool = False) -> bool:
        """Check if a target path matches or is inside a protected path.

        Matches:
        - Exact match: /foo/bar == /foo/bar
        - Subpath: /foo/bar/baz is inside /foo/bar
        - Wildcard: /foo/* matches /foo/anything
        - Recursive delete: protected /foo/bar is inside target /foo
          (deleting a parent subtree also destroys the protected child)
        """
        target_norm = GuardrailEngine._normalize_path(target)
        protected_norm = GuardrailEngine._normalize_path(protected)
        if target_norm == protected_norm:
            return True
        if protected_norm.endswith("*"):
            prefix = protected_norm[:-1]
            if target_norm.startswith(prefix):
                return True
        if target_norm.startswith(protected_norm + "/"):
            return True
        if recursive and protected_norm.startswith(target_norm + "/"):
            # Parent-dir bypass: deleting the target subtree would also
            # destroy the protected child inside it (e.g. 'rm -rf /data'
            # with a protected /data/protected).
            return True
        return False

    def clear_cache(self) -> None:
        """Clear the in-memory rule cache (force reload on next check)."""
        self._cache = []
        self._cache_time = 0


# ---------------------------------------------------------------------------
# Convenience function for quick checks
# ---------------------------------------------------------------------------

def quick_check(command: str, qdrant_client=None) -> GuardrailResult:
    """One-shot guardrail check without a persistent engine instance.

    Useful for pre_tool_call hooks that need to check a single command.
    Creates a temporary GuardrailEngine if qdrant_client is provided,
    otherwise only does pattern-based classification (no memory lookup).
    """
    if qdrant_client is None:
        # Pattern-only mode - no memory lookup
        action = classify_action(command)
        if action is None:
            return GuardrailResult(verdict=GuardrailVerdict.ALLOW, reason="Non-destructive")
        targets = extract_targets(command)
        if not targets:
            return GuardrailResult(
                verdict=GuardrailVerdict.ALLOW,
                reason=f"{action.value} but no target identified",
            )
        return GuardrailResult(
            verdict=GuardrailVerdict.ALLOW,
            reason=f"{action.value} on {targets[0]} (no memory lookup - pattern-only mode)",
        )

    engine = GuardrailEngine(qdrant_client)
    return engine.check_action(command)