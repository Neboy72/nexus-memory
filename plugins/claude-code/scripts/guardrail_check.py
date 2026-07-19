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
import urllib.request
import urllib.error
from pathlib import Path

# Config
QDRANT_URL = os.getenv("NEXUS_QDRANT_URL", "http://localhost:6333")
COLLECTION = os.getenv("NEXUS_COLLECTION", "nexus")

# Destructive command patterns
DESTRUCTIVE_PATTERNS = [
    ("delete", ["rm -r", "rm -f", "rmdir", "del /", "drop ", "truncate", "uninstall", "remove-item", "find -delete", "git clean", "dd of="]),
    ("kill", ["kill -9", "pkill", "killall", "taskkill"]),
    ("overwrite", ["write_file", "> "]),
    ("recreate", ["recreate_collection", "drop collection"]),
]

# Protection keywords (case-insensitive)
PROTECTION_KEYWORDS = ["never delete", "never remove", "do not delete", "do not remove",
                       "protected", "niemals", "nicht löschen", "nicht entfernen",
                       "verboten", "forbidden", "tabu", "sacred"]

# Path extraction patterns
import re
PATH_PATTERNS = [
    re.compile(r"~[\w./-]+"),
    re.compile(r"/[\w./-]+"),
    re.compile(r"\w:[\\/][\w\\./-]+"),
]


def classify_action(command: str) -> str | None:
    """Return the action type if destructive, None otherwise."""
    cmd_lower = command.lower()
    for action, patterns in DESTRUCTIVE_PATTERNS:
        for pattern in patterns:
            if pattern in cmd_lower:
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


def load_protection_rules() -> list[dict]:
    """Load protection rules from Qdrant."""
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
        # Fail-open: no rules = allow everything
        print(f"Guardrail: Failed to load rules (fail-open): {exc}", file=sys.stderr)
        return []


def check_action(command: str, tool_name: str = "", tool_input: dict = None) -> dict:
    """Check if an action is safe."""
    tool_input = tool_input or {}

    # Build full command string
    full_input = f"{tool_name} {command} {json.dumps(tool_input) if tool_input else ''}"

    action = classify_action(full_input)
    if not action:
        return {"verdict": "allow", "reason": "Non-destructive action"}

    targets = extract_targets(full_input)
    # Also check tool_input paths
    if tool_input and isinstance(tool_input, dict):
        for v in tool_input.values():
            if isinstance(v, str) and ("/" in v or "~" in v):
                targets.extend(extract_targets(v))

    if not targets:
        return {"verdict": "allow", "reason": f"Destructive action ({action}) but no protected target"}

    rules = load_protection_rules()
    if not rules:
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
        destructive_tools = ["Bash", "Terminal", "Write", "Edit", "Delete"]
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
        # Fail-open on any error
        print(json.dumps({"allow": True}))


if __name__ == "__main__":
    main()