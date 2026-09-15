#!/usr/bin/env python3
"""Nexus Memory Guardrail Check Hook for Claude Code.

Fires on PreToolExecution. Checks if the tool command is destructive
and if so, queries Qdrant for protection rules. Blocks the action
if a protected target is matched.

Output JSON with "allow: false" blocks the tool call.
"""

import sys
import json
import os
import re
import urllib.request
import urllib.error
from pathlib import Path

# Config
QDRANT_URL = os.getenv("NEXUS_QDRANT_URL", "http://localhost:6333")
COLLECTION = os.getenv("NEXUS_COLLECTION", "nexus")

# Destructive command patterns — (action, [compiled regex, ...]).
#
# H244: matched per FIELD with word-boundary anchors instead of raw substring
# search over an interpolated ``tool_name + command + json.dumps(tool_input)``
# blob. The old version was both over-inclusive ("> " or "truncate" tripped on
# benign file *content*) and under-inclusive (a differently wrapped command
# slipped through).
DESTRUCTIVE_PATTERNS = [
    ("delete", [
        re.compile(r"\brm\s+-[a-z]*r"),          # rm -r / rm -rf / rm -fr
        re.compile(r"\brm\s+-[a-z]*f"),          # rm -f / rm -rf
        re.compile(r"\brmdir\b"),
        re.compile(r"\bdel\s+/"),                # cmd.exe delete
        re.compile(r"\bdrop\s+\w"),              # SQL/Qdrant drop
        re.compile(r"\btruncate\b"),
        re.compile(r"\buninstall\b"),
        re.compile(r"\bremove-item\b"),
        re.compile(r"\bfind\b[^|]*\s-delete\b"),
        re.compile(r"\bgit\s+clean\b"),
        re.compile(r"\bdd\b[^|]*\bof="),
    ]),
    ("kill", [
        re.compile(r"\bkill\s+-9\b"),
        re.compile(r"\bpkill\b"),
        re.compile(r"\bkillall\b"),
        re.compile(r"\btaskkill\b"),
    ]),
    ("overwrite", [
        re.compile(r"\bwrite_file\b"),
        # Shell redirect. Only ever evaluated against the command field, so a
        # literal ">" inside file content (serialized in tool_input) is not a
        # redirect (H244).
        re.compile(r">\s*\S"),
    ]),
    ("recreate", [
        re.compile(r"\brecreate_collection\b"),
        re.compile(r"\bdrop\s+collection\b"),
    ]),
]

# Protection keywords (case-insensitive)
PROTECTION_KEYWORDS = ["never delete", "never remove", "do not delete", "do not remove",
                       "protected", "niemals", "nicht löschen", "nicht entfernen",
                       "verboten", "forbidden", "tabu", "sacred"]

# Path extraction patterns
PATH_PATTERNS = [
    re.compile(r"~[\w./-]+"),
    re.compile(r"/[\w./-]+"),
    re.compile(r"\w:[\\/][\w\\./-]+"),
]


def fail_closed_enabled() -> bool:
    """Whether errors should block (fail-closed) instead of allow (fail-open).

    Default is fail-open for availability: a guardrail outage must never wedge
    the agent. Set NEXUS_GUARDRAIL_FAIL_CLOSED=1 to invert this — on an internal
    error or an unavailable rule store, destructive actions are blocked instead.
    """
    return os.getenv("NEXUS_GUARDRAIL_FAIL_CLOSED", "").strip() in ("1", "true", "yes")


def classify_action(command: str) -> str | None:
    """Return the action type if the command is destructive, None otherwise.

    H244: matches ONE field (the command) with word-boundary regexes — never
    an interpolated ``tool_name + command + json.dumps(tool_input)`` blob. The
    old substring scan was over-inclusive ("> " or "truncate" tripped on
    benign file *content*) and under-inclusive for differently-wrapped
    commands.
    """
    if not command:
        return None
    for action, patterns in DESTRUCTIVE_PATTERNS:
        for pattern in patterns:
            if pattern.search(command):
                return action
    return None


def extract_targets(command: str) -> list[str]:
    """Extract potential protected resource targets from a command."""
    import os
    targets = []
    for pattern in PATH_PATTERNS:
        for match in pattern.finditer(command):
            target = match.group(0).strip().strip("'\"")
            if target and len(target) > 2 and target not in ("~", "/", "."):
                target = os.path.expanduser(target)
                targets.append(target)
    return targets


def normalize_path(path: str) -> str:
    """Normalize a path for comparison."""
    import os
    p = os.path.normpath(path).lower()
    if p.endswith("/") and len(p) > 1:
        p = p[:-1]
    return p


def path_matches(target: str, protected: str) -> bool:
    """Check if a target path matches or is inside a protected path."""
    t = normalize_path(target)
    p = normalize_path(protected)
    if t == p:
        return True
    if p.endswith("*"):
        prefix = p[:-1]
        if t.startswith(prefix):
            return True
    if t.startswith(p + "/"):
        return True
    return False


def load_protection_rules() -> list[dict] | None:
    """Load protection rules from Qdrant.

    Returns the list of matched rules on success (possibly empty — genuinely
    no protection rules exist, callers treat that as allow-fast). On a store
    outage returns [] in fail-open mode (default) and None in fail-closed mode
    (NEXUS_GUARDRAIL_FAIL_CLOSED=1) so callers can fail closed.
    """
    try:
        url = f"{QDRANT_URL}/collections/{COLLECTION}/points/scroll"
        payload = json.dumps({
            "filter": {
                "must": [{"key": "category", "match": {"value": "rule"}}]
            },
            "limit": 200,
            "with_payload": True,
            "with_vector": False,
        }).encode()

        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())

        rules = []
        for point in data.get("result", {}).get("points", []):
            payload = point.get("payload", {})
            text = payload.get("content", "")
            # Check if this rule contains protection keywords
            text_lower = text.lower()
            if not any(kw in text_lower for kw in PROTECTION_KEYWORDS):
                continue
            # Extract paths from the rule text
            for pattern in PATH_PATTERNS:
                for match in pattern.finditer(text):
                    path = match.group(0).strip()
                    if path and len(path) > 2:
                        rules.append({
                            "path": os.path.expanduser(path),
                            "rule_text": text[:200],
                            "source_id": point.get("id"),
                        })
        return rules
    except Exception as exc:
        if fail_closed_enabled():
            print(f"Guardrail: Failed to load rules (fail-closed): {exc}", file=sys.stderr)
            return None
        # Fail-open: no rules = allow everything
        print(f"Guardrail: Failed to load rules (fail-open): {exc}", file=sys.stderr)
        return []


def check_action(command: str, tool_name: str = "", tool_input: dict = None) -> dict:
    """Check if an action is safe.

    H244: only the COMMAND field is classified. Serialized ``tool_input`` (and
    the tool name) are never scanned as shell text — a literal ">" or
    "truncate" inside file content must not look like a destructive command.
    ``tool_input`` is used solely to collect path-like targets.
    """
    tool_input = tool_input or {}

    action = classify_action(command)
    if not action:
        return {"verdict": "allow", "reason": "Non-destructive action"}

    # Targets come from the command string and from path-like tool_input
    # values (e.g. Write's file_path) — never from serialized JSON as a whole.
    targets = extract_targets(command)
    if tool_input and isinstance(tool_input, dict):
        for v in tool_input.values():
            if isinstance(v, str) and ("/" in v or "~" in v):
                targets.extend(extract_targets(v))

    if not targets:
        return {"verdict": "allow", "reason": f"Destructive action ({action}) but no protected target"}

    rules = load_protection_rules()
    if rules is None:
        # Fail-closed: rule store unavailable, cannot prove this is safe.
        return {
            "verdict": "block",
            "reason": f"Destructive action ({action}) but protection rules are unavailable (fail-closed)",
            "matched_rules": [],
        }
    if not rules:
        # Genuinely no rules configured — fast allow (not an outage).
        return {"verdict": "allow", "reason": f"Destructive action ({action}) but no protection rules"}

    matched = []
    for target in targets:
        for rule in rules:
            if path_matches(target, rule["path"]):
                matched.append({
                    "target": target,
                    "protected_path": rule["path"],
                    "rule_text": rule["rule_text"],
                    "source_memory_id": rule["source_id"],
                    "action": action,
                })

    if matched:
        return {
            "verdict": "block",
            "reason": f"Destructive action ({action}) on protected target",
            "matched_rules": matched,
        }

    return {"verdict": "allow", "reason": f"Destructive action ({action}) on unprotected target"}


def main():
    """Read tool call from stdin, check guardrails, output decision."""
    try:
        raw = sys.stdin.read()
        if not raw:
            print(json.dumps({"allow": True}))
            return

        data = json.loads(raw)
        tool_name = data.get("tool_name", "")
        tool_input = data.get("tool_input", {})

        # Extract command from tool_input
        command = ""
        if isinstance(tool_input, dict):
            command = tool_input.get("command", tool_input.get("path", ""))

        # Only check destructive tools
        destructive_tools = ["Bash", "Write", "Edit", "MultiEdit", "NotebookEdit"]
        if tool_name not in destructive_tools:
            print(json.dumps({"allow": True}))
            return

        result = check_action(command, tool_name, tool_input)

        if result["verdict"] == "block":
            # Block the action
            print(json.dumps({
                "allow": False,
                "message": f"🛡️ Nexus Guardrail BLOCKED: {result['reason']}\n\n"
                           f"Matched rules: {json.dumps(result.get('matched_rules', []), indent=2)}\n\n"
                           f"If this action is explicitly authorized, call nexus_guardrail_override "
                           f"with explicit reasoning (min 10 chars) to proceed with audit trail.",
            }, indent=2))
        else:
            print(json.dumps({"allow": True}))

    except Exception as exc:
        print(f"guardrail_check: inner error (fail-open): {exc}", file=sys.stderr)
        if fail_closed_enabled():
            print(json.dumps({"allow": False, "message": f"🛡️ Nexus Guardrail: internal error (fail-closed): {exc}"}))
        else:
            # Fail-open on any error (availability default)
            print(json.dumps({"allow": True}))


if __name__ == "__main__":
    main()