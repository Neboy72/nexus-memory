#!/usr/bin/env python3
"""Nexus Memory — Agent Auto-Detection.

Scans the system for installed AI agents and returns which ones
are available for Nexus Memory connection.

Detected agents (Top 15 from OpenRouter coding leaderboard):
- Hermes Agent (config dir, CLI)         #1
- Kilo Code (config dir, CLI)            #2
- OpenClaw (config dir, CLI)             #3
- Claude Code (config dir, CLI)          #4
- pi (CLI)                               #5
- Cline (config dir)                     #6
- Codex CLI (CLI)                        #7
- OpenHands (config dir, CLI)            #8
- Roo Code (config dir)                  #9
- Qwen Code (CLI)                        #10
- Cursor (config dir)                    #11
- Gemini CLI (CLI)                       #12
- OpenCode (CLI)                         #13
- Windsurf (config dir)                  #14
- Crush (CLI)                            #15

Usage:
    python3 agent_detect.py detect
    → JSON list of detected agents
"""

import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def _check_hermes() -> dict:
    """Detect Hermes Agent."""
    info = {"id": "hermes", "name": "Hermes Agent", "icon": "🦊", "plugin_available": True, "mcp_available": True}
    
    # Check config dir
    config_dir = Path.home() / ".hermes"
    info["config_dir"] = str(config_dir) if config_dir.exists() else None
    
    # Check CLI
    cli = shutil.which("hermes")
    info["cli_path"] = cli
    
    # Check if nexus plugin already linked
    plugin_path = config_dir / "hermes-agent" / "plugins" / "memory" / "nexus"
    info["nexus_installed"] = plugin_path.exists()
    
    info["detected"] = bool(config_dir.exists() or cli)
    return info


def _check_openclaw() -> dict:
    """Detect OpenClaw."""
    info = {"id": "openclaw", "name": "OpenClaw", "icon": "🦉", "plugin_available": True, "mcp_available": True}
    
    config_dir = Path.home() / ".openclaw"
    info["config_dir"] = str(config_dir) if config_dir.exists() else None
    
    cli = shutil.which("openclaw")
    info["cli_path"] = cli
    
    # Check if nexus plugin already in openclaw config
    openclaw_json = config_dir / "openclaw.json"
    if openclaw_json.exists():
        try:
            cfg = json.loads(openclaw_json.read_text())
            paths = cfg.get("plugins", {}).get("load", {}).get("paths", [])
            info["nexus_installed"] = any("nexus" in str(p).lower() for p in paths)
        except Exception:
            info["nexus_installed"] = False
    else:
        info["nexus_installed"] = False
    
    info["detected"] = bool(config_dir.exists() or cli)
    return info


def _check_claude_code() -> dict:
    """Detect Claude Code."""
    info = {"id": "claude-code", "name": "Claude Code", "icon": "💻", "plugin_available": True, "mcp_available": True}
    
    config_dir = Path.home() / ".claude"
    info["config_dir"] = str(config_dir) if config_dir.exists() else None
    
    cli = shutil.which("claude")
    info["cli_path"] = cli
    
    # Check if nexus plugin already installed
    plugin_dir = config_dir / "plugins" / "nexus-memory"
    info["nexus_installed"] = plugin_dir.exists()
    
    info["detected"] = bool(config_dir.exists() or cli)
    return info


def _check_codex() -> dict:
    """Detect Codex CLI."""
    info = {"id": "codex", "name": "Codex CLI", "icon": "🔮", "plugin_available": False, "mcp_available": True}
    
    cli = shutil.which("codex")
    info["cli_path"] = cli
    info["config_dir"] = str(Path.home() / ".codex") if (Path.home() / ".codex").exists() else None
    
    info["nexus_installed"] = False
    info["detected"] = bool(cli)
    return info


def _check_cursor() -> dict:
    """Detect Cursor."""
    info = {"id": "cursor", "name": "Cursor", "icon": "🖱️", "plugin_available": False, "mcp_available": True}
    
    config_dir = Path.home() / ".cursor"
    info["config_dir"] = str(config_dir) if config_dir.exists() else None
    
    # Check for mcp.json with nexus
    mcp_json = config_dir / "mcp.json"
    if mcp_json.exists():
        try:
            cfg = json.loads(mcp_json.read_text())
            info["nexus_installed"] = "nexus" in json.dumps(cfg).lower()
        except Exception:
            info["nexus_installed"] = False
    else:
        info["nexus_installed"] = False
    
    info["detected"] = bool(config_dir.exists())
    return info


def _check_antigravity_cli() -> dict:
    """Detect Antigravity CLI (#12 OpenRouter, replaces Gemini CLI as of June 18 2026).
    Google retired Gemini CLI for free/Pro/Ultra users. Antigravity CLI (agy) is the successor.
    """
    info = {"id": "antigravity-cli", "name": "Antigravity CLI", "icon": "🪐", "plugin_available": False, "mcp_available": True}

    cli = shutil.which("agy")
    info["cli_path"] = cli
    config_dir = Path.home() / ".agy"
    info["config_dir"] = str(config_dir) if config_dir.exists() else None

    # Check MCP config
    mcp_json = config_dir / "mcp.json" if config_dir.exists() else None
    if mcp_json and mcp_json.exists():
        try:
            cfg = json.loads(mcp_json.read_text())
            info["nexus_installed"] = "nexus" in json.dumps(cfg).lower()
        except Exception:
            info["nexus_installed"] = False
    else:
        info["nexus_installed"] = False

    info["detected"] = bool(cli or config_dir.exists())
    return info


def _check_opencode() -> dict:
    """Detect OpenCode."""
    info = {"id": "opencode", "name": "OpenCode", "icon": "📂", "plugin_available": False, "mcp_available": True}
    
    cli = shutil.which("opencode")
    info["cli_path"] = cli
    config_dir = Path.home() / ".opencode"
    info["config_dir"] = str(config_dir) if config_dir.exists() else None
    info["detected"] = bool(cli or config_dir.exists())
    info["nexus_installed"] = False
    return info


def _check_kilo_code() -> dict:
    """Detect Kilo Code (#2 OpenRouter). VS Code / JetBrains extension, CLI."""
    info = {"id": "kilo-code", "name": "Kilo Code", "icon": "⚡", "plugin_available": False, "mcp_available": True}

    # VS Code extension config
    vscode_dir = Path.home() / ".vscode" / "extensions"
    info["config_dir"] = str(vscode_dir) if vscode_dir.exists() else None

    cli = shutil.which("kilo")
    info["cli_path"] = cli

    # Check MCP config for nexus
    mcp_json = Path.home() / ".kilo" / "mcp.json"
    if mcp_json.exists():
        try:
            cfg = json.loads(mcp_json.read_text())
            info["nexus_installed"] = "nexus" in json.dumps(cfg).lower()
        except Exception:
            info["nexus_installed"] = False
    else:
        info["nexus_installed"] = False

    info["detected"] = bool(vscode_dir.exists() or cli)
    return info


def _check_pi() -> dict:
    """Detect pi (#5 OpenRouter). CLI agent."""
    info = {"id": "pi", "name": "pi", "icon": "π", "plugin_available": False, "mcp_available": True}

    cli = shutil.which("pi")
    info["cli_path"] = cli
    config_dir = Path.home() / ".pi"
    info["config_dir"] = str(config_dir) if config_dir.exists() else None
    info["detected"] = bool(cli or config_dir.exists())
    info["nexus_installed"] = False
    return info


def _check_cline() -> dict:
    """Detect Cline (#6 OpenRouter). VS Code extension."""
    info = {"id": "cline", "name": "Cline", "icon": "🤖", "plugin_available": False, "mcp_available": True}

    # VS Code extension directory
    vscode_ext = Path.home() / ".vscode" / "extensions"
    info["config_dir"] = str(vscode_ext) if vscode_ext.exists() else None

    # Check for Cline extension folder
    cline_found = False
    if vscode_ext.exists():
        for item in vscode_ext.iterdir():
            if "cline" in item.name.lower():
                cline_found = True
                break
    info["detected"] = cline_found
    info["nexus_installed"] = False
    return info


def _check_openhands() -> dict:
    """Detect OpenHands (#8 OpenRouter). CLI agent."""
    info = {"id": "openhands", "name": "OpenHands", "icon": "🙌", "plugin_available": False, "mcp_available": True}

    cli = shutil.which("openhands")
    info["cli_path"] = cli
    config_dir = Path.home() / ".openhands"
    info["config_dir"] = str(config_dir) if config_dir.exists() else None
    info["detected"] = bool(cli or config_dir.exists())
    info["nexus_installed"] = False
    return info


def _check_roo_code() -> dict:
    """Detect Roo Code (#9 OpenRouter). VS Code extension."""
    info = {"id": "roo-code", "name": "Roo Code", "icon": "🦘", "plugin_available": False, "mcp_available": True}

    vscode_ext = Path.home() / ".vscode" / "extensions"
    info["config_dir"] = str(vscode_ext) if vscode_ext.exists() else None

    roo_found = False
    if vscode_ext.exists():
        for item in vscode_ext.iterdir():
            if "roo" in item.name.lower() and "code" in item.name.lower():
                roo_found = True
                break
    info["detected"] = roo_found
    info["nexus_installed"] = False
    return info


def _check_qwen_code() -> dict:
    """Detect Qwen Code (#10 OpenRouter). CLI tool."""
    info = {"id": "qwen-code", "name": "Qwen Code", "icon": "🔤", "plugin_available": False, "mcp_available": True}

    cli = shutil.which("qwen")
    info["cli_path"] = cli
    config_dir = Path.home() / ".qwen"
    info["config_dir"] = str(config_dir) if config_dir.exists() else None
    info["detected"] = bool(cli or config_dir.exists())
    info["nexus_installed"] = False
    return info


def _check_windsurf() -> dict:
    """Detect Windsurf (#14 OpenRouter). IDE."""
    info = {"id": "windsurf", "name": "Windsurf", "icon": "🏄", "plugin_available": False, "mcp_available": True}

    config_dir = Path.home() / ".codeium" / "windsurf"
    info["config_dir"] = str(config_dir) if config_dir.exists() else None

    cli = shutil.which("windsurf")
    info["cli_path"] = cli

    # Check MCP config
    mcp_json = config_dir / "mcp.json" if config_dir.exists() else None
    if mcp_json and mcp_json.exists():
        try:
            cfg = json.loads(mcp_json.read_text())
            info["nexus_installed"] = "nexus" in json.dumps(cfg).lower()
        except Exception:
            info["nexus_installed"] = False
    else:
        info["nexus_installed"] = False

    info["detected"] = bool(config_dir.exists() or cli)
    return info


def _check_crush() -> dict:
    """Detect Crush (#15 OpenRouter). CLI by charm.land."""
    info = {"id": "crush", "name": "Crush", "icon": "⭐", "plugin_available": False, "mcp_available": True}

    cli = shutil.which("crush")
    info["cli_path"] = cli
    config_dir = Path.home() / ".crush"
    info["config_dir"] = str(config_dir) if config_dir.exists() else None
    info["detected"] = bool(cli or config_dir.exists())
    info["nexus_installed"] = False
    return info


def detect_all_agents() -> dict:
    """Scan system for all known AI agents. Returns JSON for chat display."""
    detectors = [
        _check_hermes,
        _check_kilo_code,
        _check_openclaw,
        _check_claude_code,
        _check_pi,
        _check_cline,
        _check_codex,
        _check_openhands,
        _check_roo_code,
        _check_qwen_code,
        _check_cursor,
        _check_antigravity_cli,
        _check_opencode,
        _check_windsurf,
        _check_crush,
    ]
    
    results = []
    for detector in detectors:
        try:
            info = detector()
            results.append(info)
        except Exception as e:
            results.append({
                "id": "unknown",
                "name": "Unknown",
                "icon": "❓",
                "detected": False,
                "error": str(e)
            })
    
    detected = [r for r in results if r.get("detected")]
    not_detected = [r for r in results if not r.get("detected")]
    
    return {
        "step": "agent_detection",
        "title": "Nexus Memory - Agent Detection",
        "detected_agents": detected,
        "not_detected": not_detected,
        "instructions": "For each detected agent, choose a trust level (1=Public, 2=Trusted, 3=Private). Reply with: agent_id=trust_level (e.g. hermes=2,claude-code=1)"
    }


# ── Agent Registry (agents.json) ──────────────────────────────────────────

def _get_agents_registry_path() -> Path:
    """Get the path to the agents registry file."""
    return Path.home() / ".nexus-memory" / "agents.json"


def load_agents_registry() -> dict:
    """Load the agents registry."""
    path = _get_agents_registry_path()
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {"agents": []}


def save_agents_registry(registry: dict) -> None:
    """Save the agents registry."""
    path = _get_agents_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(registry, indent=2) + "\n")


def register_agent(agent_id: str, name: str, icon: str, trust_level: str,
                   install_type: str, config_dir: str = None) -> dict:
    """Register or update an agent in the registry."""
    registry = load_agents_registry()
    
    # Find existing or create new
    agent = None
    for a in registry.get("agents", []):
        if a["id"] == agent_id:
            agent = a
            break
    
    if agent is None:
        agent = {"id": agent_id}
        registry.setdefault("agents", []).append(agent)
    
    agent.update({
        "name": name,
        "icon": icon,
        "trust_level": trust_level,
        "install_type": install_type,  # "plugin+mcp", "mcp_only"
        "config_dir": config_dir,
        "connected_at": _now_iso(),
        "last_seen": _now_iso(),
        "reads": 0,
        "writes": 0,
    })
    
    save_agents_registry(registry)
    return agent


def update_agent_stats(agent_id: str, read: bool = False, write: bool = False) -> None:
    """Update last_seen, reads, writes for an agent."""
    registry = load_agents_registry()
    for a in registry.get("agents", []):
        if a["id"] == agent_id:
            a["last_seen"] = _now_iso()
            if read:
                a["reads"] = a.get("reads", 0) + 1
            if write:
                a["writes"] = a.get("writes", 0) + 1
            break
    save_agents_registry(registry)


def update_agent_seen(agent_id: str) -> None:
    """Update last_seen timestamp for an agent (backward compat)."""
    update_agent_stats(agent_id)


# An agent must be undetectable AND have lost its config dir for this many
# days before the registry treats it as removed (protects against transient
# detection hiccups and short uninstall/reinstall windows).
AGENT_REMOVAL_GRACE_DAYS = 14


def cleanup_removed_agents(grace_days: int = AGENT_REMOVAL_GRACE_DAYS) -> dict:
    """Drop registry entries for agents that are no longer on this machine.

    An entry is only removed when ALL of the following hold:
      - detect_all_agents() no longer reports it (its detector says
        not "detected", or the id no longer has a detector at all),
      - its stored config_dir no longer exists on disk (a re-install
        or config-dir-only remnant keeps the entry alive),
      - its last_seen is older than ``grace_days`` (fresh entries always
        survive; active agents never hit the timeout anyway).

    Returns a report dict; never raises for individual entries.
    """
    try:
        detected = detect_all_agents()
    except Exception as e:
        return {"status": "error", "error": f"detect failed: {e}"}

    # id -> detected flag. detect_all_agents() returns a wizard-shaped dict
    # with the list under "detected_agents" (older callers/tests may build a
    # bare {"agents": [...]}). Detectors report "detected" per entry; ids
    # without any detection info are treated as undetectable too.
    raw_list = (
        detected.get("detected_agents")
        if isinstance(detected, dict) else None
    ) or detected.get("agents", [])
    detection_map = {
        d.get("id"): bool(d.get("detected", False))
        for d in raw_list
        if isinstance(d, dict) and d.get("id")
    }

    registry = load_agents_registry()
    kept, removed = [], []
    now = datetime.now(timezone.utc)

    for agent in registry.get("agents", []):
        aid = agent.get("id", "")
        undetected = not detection_map.get(aid, False)
        config_dir = agent.get("config_dir")
        dir_gone = (not config_dir) or (not Path(config_dir).exists())

        # last_seen may be missing on hand-crafted entries -> treat as stale.
        last_seen_raw = agent.get("lastSeen") or agent.get("last_seen") or ""
        try:
            last_seen = datetime.fromisoformat(last_seen_raw) if last_seen_raw else None
        except Exception:
            last_seen = None
        stale_days = (now - last_seen).days if last_seen else grace_days + 1

        if undetected and dir_gone and stale_days > grace_days:
            removed.append({"id": aid, "last_seen": last_seen_raw})
        else:
            kept.append(aid)

    if removed:
        registry["agents"] = [a for a in registry.get("agents", []) if a.get("id") in set(kept)]
        save_agents_registry(registry)

    return {
        "status": "ok",
        "removed": removed,
        "kept": kept,
        "grace_days": grace_days,
        "time": _now_iso(),
    }


def set_agent_trust_level(agent_id: str, trust_level: str) -> dict:
    """Change the trust level for an agent (used by dashboard)."""
    valid = ["public", "trusted", "private"]
    if trust_level not in valid:
        return {"error": f"Invalid trust level: {trust_level}"}
    
    registry = load_agents_registry()
    for a in registry.get("agents", []):
        if a["id"] == agent_id:
            a["trust_level"] = trust_level
            save_agents_registry(registry)
            return {"agent_id": agent_id, "trust_level": trust_level, "status": "updated"}
    
    return {"error": f"Agent not found: {agent_id}"}


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ── CLI ────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: agent_detect.py [detect|register|list|trust]"}))
        sys.exit(1)
    
    command = sys.argv[1]
    
    if command == "detect":
        result = detect_all_agents()
    elif command == "register":
        # register <agent_id> <name> <icon> <trust_level> <install_type> [config_dir]
        if len(sys.argv) < 7:
            print(json.dumps({"error": "Usage: agent_detect.py register <id> <name> <icon> <trust> <install_type> [config_dir]"}))
            sys.exit(1)
        result = register_agent(
            sys.argv[2], sys.argv[3], sys.argv[4],
            sys.argv[5], sys.argv[6],
            sys.argv[7] if len(sys.argv) > 7 else None
        )
    elif command == "list":
        result = load_agents_registry()
    elif command == "trust":
        # trust <agent_id> <level>
        if len(sys.argv) < 4:
            print(json.dumps({"error": "Usage: agent_detect.py trust <agent_id> <level>"}))
            sys.exit(1)
        result = set_agent_trust_level(sys.argv[2], sys.argv[3])
    elif command == "cleanup":
        # Remove registry entries for agents no longer on this machine.
        grace = int(sys.argv[2]) if len(sys.argv) > 2 else AGENT_REMOVAL_GRACE_DAYS
        result = cleanup_removed_agents(grace)
    else:
        result = {"error": f"Unknown command: {command}"}
    
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()