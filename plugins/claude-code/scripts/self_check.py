#!/usr/bin/env python3
"""Nexus Memory self-check for the Claude Code plugin.

Runs at SessionStart, writes a per-agent health file for the server-side
watchdog, and surfaces a warning in the agent's prompt when memory is broken.
Fail-open everywhere: a read-only home or a missing dependency must never
prevent the hook from returning.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

DEFAULT_QDRANT_URL = "http://localhost:6333"
DEFAULT_PROVIDER = "ollama"
DEFAULT_AGENT_ID = "claude-code"
PLUGIN_MANIFEST = (
    Path(__file__).resolve().parent.parent / ".claude-plugin" / "plugin.json"
)


def _plugin_version() -> str:
    """Read the plugin version from its manifest; fall back to the literal."""
    try:
        data = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))
        version = data.get("version")
        if version:
            return str(version)
    except Exception:
        pass
    return "1.2.2"


def _sanitize_agent_id(raw: str) -> str:
    """Filesystem-safe agent id; a raw id must never escape the data dir."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", (raw or "").strip())
    return safe.strip("-") or "unknown"


def _data_dir() -> Path:
    """Nexus data dir: ``$NEXUS_DATA_DIR`` when set, else ``~/.nexus-memory``."""
    env = os.environ.get("NEXUS_DATA_DIR", "").strip()
    if env:
        return Path(os.path.expanduser(env))
    return Path.home() / ".nexus-memory"


def _selfcheck_path() -> Path:
    """Per-agent self-check file consumed by the server-side watchdog."""
    raw_id = os.environ.get("NEXUS_AGENT_ID", "").strip() or DEFAULT_AGENT_ID
    agent_id = _sanitize_agent_id(raw_id)
    return _data_dir() / f"agent-selfcheck-{agent_id}.json"


def _required_env_var(provider: str) -> str:
    """Return the API-key env var name for a provider, or "" for keyless ones."""
    mapping = {
        "voyage": "VOYAGE_API_KEY",
        "openai": "OPENAI_API_KEY",
        "google": "GOOGLE_API_KEY",
        "jina": "JINA_API_KEY",
    }
    return mapping.get(provider, "")


def _repo_root() -> Path:
    """Best-effort repo root derived from this script's location."""
    try:
        root = Path(__file__).resolve().parent.parent.parent.parent
        if (root / "pyproject.toml").exists():
            return root
    except Exception:
        pass
    return Path.home() / "nexus-memory"


def _build_fix(provider: str, missing_key_var: str) -> str:
    """One copy-pasteable fix line, quoted for safety."""
    if missing_key_var:
        return f"export {missing_key_var}=... (then restart Claude Code)"
    root = _repo_root()
    fix = f'pip install -e "{root}"'
    var = _required_env_var(provider)
    if var:
        fix += f" and ensure {var} is exported"
    return fix


def _probe() -> tuple[bool, str, str]:
    """Check Qdrant reachability and embedding configuration.

    Returns ``(ok, reason, missing_key_var)``. Does not call embedding APIs.
    """
    host = os.environ.get("NEXUS_QDRANT_URL", "").strip() or DEFAULT_QDRANT_URL
    try:
        req = urllib.request.Request(f"{host}/collections")
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
    except Exception as exc:
        return (
            False,
            f"Qdrant at {host} is unreachable or unhealthy "
            f"({type(exc).__name__}: {exc})",
            "",
        )

    provider = os.environ.get("NEXUS_EMBEDDING_PROVIDER", "").strip() or DEFAULT_PROVIDER
    var = _required_env_var(provider)
    if var and not os.environ.get(var, "").strip():
        return (
            False,
            f"the embedding provider '{provider}' has no API key ({var} is empty)",
            var,
        )

    return True, "", ""


def _warning_text(reason: str, fix: str) -> str:
    """English warning shown to the agent when memory is broken."""
    text = (
        "⚠️ Nexus Memory self-check: NOT WORKING\n"
        f"Cause: {reason}\n"
        f"Fix: {fix}\n"
        "Your stored memories are safe and not lost — they remain in the "
        "memory database; only this agent's access is offline.\n"
        "Tell your user about this and offer to run the fix."
    )
    return text[:1000]


def _system_message(reason: str, fix: str) -> str:
    """Short single-line warning shown to the user via systemMessage."""
    cause = " ".join((reason or "memory backend unhealthy").split())
    remedy = " ".join((fix or "check the Nexus install").split())
    if len(cause) > 160:
        cause = cause[:157].rstrip() + "…"
    if len(remedy) > 120:
        remedy = remedy[:117].rstrip() + "…"
    return (
        f"Nexus Memory is not working: {cause}. Fix: {remedy}. "
        "Your memories are safe."
    )


def write_selfcheck() -> tuple[bool, str, str]:
    """Probe health and atomically write the self-check file.

    Returns ``(ok, reason, fix)``. Never raises.
    """
    ok = True
    reason = ""
    fix = ""
    tmp: Optional[Path] = None
    try:
        ok, reason, missing_key_var = _probe()
        provider = (
            os.environ.get("NEXUS_EMBEDDING_PROVIDER", "").strip() or DEFAULT_PROVIDER
        )
        fix = _build_fix(provider, missing_key_var)

        path = _selfcheck_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "agent_id": os.environ.get("NEXUS_AGENT_ID", "").strip() or DEFAULT_AGENT_ID,
            "ok": bool(ok),
            "reason": reason,
            "fix": fix,
            "interpreter": sys.executable,
            "plugin_version": _plugin_version(),
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as exc:
        print(f"[nexus self-check] write skipped (non-fatal): {exc}", file=sys.stderr)
        ok = True
        reason = ""
        fix = ""
    finally:
        if tmp is not None:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass

    return ok, reason, fix


def main() -> None:
    ok, reason, fix = write_selfcheck()
    if not ok:
        warning = _warning_text(reason, fix)
        output = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": warning,
            },
            "systemMessage": _system_message(reason, fix),
        }
        print(json.dumps(output))


if __name__ == "__main__":
    main()
