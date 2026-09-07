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

import fcntl
import json
import os
import re
import shutil
import socket
import sys
import threading
from contextlib import contextmanager
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
    """Detect Cursor.

    Strict install signals only: the Cursor app bundle or the ``cursor``
    CLI. A bare ``~/.cursor`` directory does NOT count — tools (e.g. our
    own skill deployments) create it without Cursor ever being installed.
    """
    info = {"id": "cursor", "name": "Cursor", "icon": "🖱️", "plugin_available": False, "mcp_available": True}

    cli = shutil.which("cursor")
    info["cli_path"] = cli

    app_dir = Path("/Applications/Cursor.app")
    home_cursor = Path.home() / ".cursor"
    mcp_json = home_cursor / "mcp.json"
    if mcp_json.exists():
        try:
            cfg = json.loads(mcp_json.read_text())
            info["nexus_installed"] = "nexus" in json.dumps(cfg).lower()
        except Exception:
            info["nexus_installed"] = False
    else:
        info["nexus_installed"] = False

    info["config_dir"] = str(home_cursor) if home_cursor.exists() else None
    info["detected"] = bool(cli or app_dir.exists())
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
    """Detect Kilo Code (#2 OpenRouter). VS Code / JetBrains extension, CLI.

    Strict install signals only: a ``kilo`` binary or a ``~/.kilo`` config
    dir counts. The mere presence of ``~/.vscode/extensions`` does NOT
    (any VS Code install would otherwise register as Kilo Code).
    """
    info = {"id": "kilo-code", "name": "Kilo Code", "icon": "⚡", "plugin_available": False, "mcp_available": True}

    cli = shutil.which("kilo")
    info["cli_path"] = cli

    kilo_dir = Path.home() / ".kilo"
    mcp_json = kilo_dir / "mcp.json"
    if mcp_json.exists():
        try:
            cfg = json.loads(mcp_json.read_text())
            info["nexus_installed"] = "nexus" in json.dumps(cfg).lower()
        except Exception:
            info["nexus_installed"] = False
    else:
        info["nexus_installed"] = False

    # Extension-style install inside ~/.kilo (e.g. mcp.json or other config)
    kilo_cfg = kilo_dir.exists() and any(kilo_dir.iterdir())

    info["config_dir"] = str(kilo_dir) if kilo_dir.exists() else None
    info["detected"] = bool(cli or kilo_cfg)
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

    # Check MCP config — Windsurf uses mcp_config.json (NOT mcp.json);
    # "nexus" anywhere in the config means Nexus Memory is connected.
    nexus_installed = False
    for mcp_name in ("mcp_config.json", "mcp.json"):
        mcp_json = config_dir / mcp_name if config_dir.exists() else None
        if mcp_json and mcp_json.exists():
            try:
                cfg = json.loads(mcp_json.read_text())
                if "nexus" in json.dumps(cfg).lower():
                    nexus_installed = True
                    break
            except Exception:
                pass
    info["nexus_installed"] = nexus_installed

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

# Locally detectable agent ids (must stay in sync with the ``_check_*``
# detectors below). register_remote_agent() refuses these so a remote
# pre-registration can never shadow a real local agent.
LOCAL_AGENT_IDS = frozenset({
    "hermes", "kilo-code", "openclaw", "claude-code", "pi", "cline",
    "codex", "openhands", "roo-code", "qwen-code", "cursor",
    "antigravity-cli", "opencode", "windsurf", "crush", "gemini-cli",
})

# Review fix (MEDIUM :587): ghost horizon for explicitly registered REMOTE
# agents. Their regular cleanup conditions are always true (never detected,
# no config_dir), so without this horizon the sweep would delete every
# remote seat after grace_days. 30 days gives a seat whose agent never
# called back a fair window before it is reaped.
REMOTE_GHOST_DAYS = 30


def _get_agents_registry_path() -> Path:
    """Get the path to the agents registry file."""
    return Path.home() / ".nexus-memory" / "agents.json"


def _registry_lock_path() -> Path:
    """Advisory-lock file for the agents registry.

    Lives in the SAME directory as the registry, so redirected registry
    paths (tests, custom installs) automatically get their own lock.
    """
    path = _get_agents_registry_path()
    return path.with_name(path.name + ".lock")


@contextmanager
def _registry_lock(exclusive: bool = True):
    """Cross-process lock around a WHOLE registry read-modify-write cycle.

    The lock must span read AND write: locking only the write would leave
    the read-modify-write window open and parallel writers would silently
    lose each other's registrations/trust changes. flock() conflicts across
    file descriptors, so it serializes other processes AND other threads
    (every caller opens its own fd on the lock file).
    """
    lock_path = _registry_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as lock_file:
        fcntl.flock(
            lock_file.fileno(),
            fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH,
        )
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _load_registry_unlocked() -> dict:
    """Read the registry file without taking the lock.

    Callers either hold the registry lock already or accept a snapshot
    read. A parse error is NOT treated as an empty registry: swallowing it
    made the next save overwrite every existing entry with an empty list.
    Fail closed instead — raise so the caller aborts without writing.
    """
    path = _get_agents_registry_path()
    if not path.exists():
        return {"agents": []}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("agents registry must be a JSON object")
    return data


def load_agents_registry() -> dict:
    """Load the agents registry (shared lock, parse errors fail closed)."""
    with _registry_lock(exclusive=False):
        return _load_registry_unlocked()


def _save_registry_unlocked(registry: dict) -> None:
    """Atomically replace the registry file. Caller MUST hold the lock.

    Writes a unique temp file next to the registry (flushed + fsynced) and
    os.replace()s it into place, so readers see either the old or the new
    complete document — never a half-written registry.
    """
    path = _get_agents_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(registry, indent=2) + "\n"
    tmp_path = path.with_name(
        f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}"
    )
    try:
        with open(tmp_path, "w") as tmp_file:
            tmp_file.write(payload)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def save_agents_registry(registry: dict) -> None:
    """Save the agents registry (exclusive lock + atomic replace).

    Kept for external callers; the in-module read-modify-write cycles use
    the lock plus the unlocked load/save helpers directly so the ENTIRE
    cycle stays inside one critical section.
    """
    with _registry_lock():
        _save_registry_unlocked(registry)


def unregister_agent(agent_id: str) -> bool:
    """Remove an agent from the registry (dashboard Disconnect semantics).

    Returns True when an entry was removed, False when the id was not
    registered. The whole read-modify-write cycle runs under the registry
    lock, so a parallel connect/trust write cannot be lost.
    """
    with _registry_lock():
        registry = _load_registry_unlocked()
        agents = registry.get("agents", [])
        remaining = [a for a in agents if a.get("id") != agent_id]
        if len(remaining) == len(agents):
            return False
        registry["agents"] = remaining
        _save_registry_unlocked(registry)
        return True


def register_agent(agent_id: str, name: str, icon: str, trust_level: str,
                   install_type: str, config_dir: str = None) -> dict:
    """Register or update an agent in the registry.

    Re-registration refreshes install metadata but PRESERVES the original
    connected_at and usage stats (reads/writes) — otherwise every re-run
    of the setup wizard would reset the dashboard counters.

    Review fixes:
    - MEDIUM :430: trust_level is validated (public/trusted/private) —
      a direct registration can no longer inject an unknown level.
    - MEDIUM :660: local detector registration refuses ids that belong to
      an explicitly registered REMOTE host (host_type="remote") so a
      coincidental local match cannot clobber a remote seat.
    """
    valid_trust = ("public", "trusted", "private")
    if trust_level not in valid_trust:
        return {"error": f"Invalid trust level: {trust_level}. Must be one of: {list(valid_trust)}"}
    now = _now_iso()
    with _registry_lock():
        registry = _load_registry_unlocked()
        agents = registry.setdefault("agents", [])

        # Find existing or create new
        agent = None
        for a in agents:
            if a.get("id") == agent_id:
                agent = a
                break

        # Review fix (MEDIUM :660): an id explicitly registered as REMOTE
        # must not be overwritten by local detection — different host,
        # different seat. Local registration of such an id is refused.
        if agent is not None and agent.get("host_type") == "remote":
            return {"error": (
                f"Agent id '{agent_id}' is registered as a REMOTE host "
                f"(host_type=remote). Local detection must not overwrite it; "
                f"use a different agent id or update the remote entry explicitly.")}

        if agent is None:
            agent = {
                "id": agent_id,
                "name": name,
                "icon": icon,
                "trust_level": trust_level,
                "install_type": install_type,  # "plugin+mcp", "mcp_only"
                "config_dir": config_dir,
                "connected_at": now,
                "last_seen": now,
                "reads": 0,
                "writes": 0,
            }
            agents.append(agent)
        else:
            agent.update({
                "name": name,
                "icon": icon,
                "trust_level": trust_level,
                "install_type": install_type,
                "config_dir": config_dir,
                "last_seen": now,
            })
            agent.setdefault("connected_at", now)
            agent.setdefault("reads", 0)
            agent.setdefault("writes", 0)

        # Registration via local detection = this machine (never clobbers an
        # explicitly-registered remote host's fields, since those entries are
        # not re-registered by detectors).
        annotate_host(agent, host_type="local")

        _save_registry_unlocked(registry)
    return agent


def update_agent_stats(agent_id: str, read: bool = False, write: bool = False) -> None:
    """Update last_seen, reads, writes for an agent."""
    with _registry_lock():
        registry = _load_registry_unlocked()
        for a in registry.get("agents", []):
            if a.get("id") == agent_id:
                a["last_seen"] = _now_iso()
                if read:
                    a["reads"] = a.get("reads", 0) + 1
                if write:
                    a["writes"] = a.get("writes", 0) + 1
                _save_registry_unlocked(registry)
                return
        # Unknown agent id: nothing to update, don't rewrite the file.


def update_agent_seen(agent_id: str) -> None:
    """Update last_seen timestamp for an agent (backward compat)."""
    update_agent_stats(agent_id)


# An agent must be undetectable AND have lost its config dir for this many
# days before the registry treats it as removed (protects against transient
# detection hiccups and short uninstall/reinstall windows).
AGENT_REMOVAL_GRACE_DAYS = 14


def _local_host_label() -> str:
    """Human-readable name of the machine this code runs on.

    ``NEXUS_HOST_LABEL`` wins (explicit config), else the macOS hostname.
    Used to auto-annotate locally-detected agents in the registry.

    Universal fallback (Nebo, 07.09.): a raw technical hostname like
    "Mac-mini-von-Nebojsa.local" is prettified for the badge — suffixes
    (.local/.lan) stripped, dashes/underscores become spaces, first
    letters capitalised. A user who never set NEXUS_HOST_LABEL still
    gets a readable "Mac Mini Von Nebojsa" instead of the raw mDNS name.
    """
    env = os.environ.get("NEXUS_HOST_LABEL", "").strip()
    if env:
        return env
    try:
        name = socket.gethostname().strip()
        if not name:
            return "this machine"
        # Prettify: strip mDNS/DNS suffixes, split camel-case + separators.
        # Device-core rule (Nebo, 07.09.): collect LEADING device words only —
        # "Mac-mini-von-Nebojsa.local" → "Mac Mini" (owner suffix dropped, the
        # badge shows the machine TYPE like every other card in the fleet).
        name = name.split(".")[0]
        name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)          # camelCase
        name = re.sub(r"[-_]+", " ", name)                         # dash/underscore
        DEVICE_WORDS = {"mac": "Mac", "mini": "Mini", "pc": "PC", "windows": "Windows",
                        "pro": "Pro", "air": "Air", "studio": "Studio", "ultra": "Ultra",
                        "server": "Server", "laptop": "Laptop", "book": "Book",
                        "desktop": "Desktop", "workstation": "Workstation",
                        "gaming": "Gaming", "nas": "NAS", "homelab": "Homelab",
                        "office": "Office", "arbeits": "Arbeits", "buero": "Büro",
                        "home": "Home"}
        words = []
        for word in name.split():
            low = word.lower()
            if low in DEVICE_WORDS:
                words.append(DEVICE_WORDS[low])
            elif not words:
                continue                     # owner prefix before device words
            else:
                break                        # owner suffix after device words
        if not words:
            # No recognised device word: fall back to prettified full name.
            for word in name.split():
                low = word.lower()
                if word.isupper() and len(word) >= 4:
                    words.append(word)       # DESKTOP / AB12CD3 — serial-like
                elif word.islower():
                    words.append(word[0].upper() + word[1:])
                else:
                    words.append(word)       # Mixed case = real name, keep
        return " ".join(words) or "this machine"
    except Exception:
        return "this machine"


def annotate_host(agent: dict, host_type: Optional[str] = None,
                  host_label: Optional[str] = None) -> dict:
    """Fill host fields on a registry entry without clobbering existing ones.

    host_type: "local" (this machine) or "remote" (VPS/other machine).
    host_label: human-readable seat, e.g. "Mac Mini" or "Hetzner CX22".
    host_provider: optional vendor, e.g. "Hetzner".
    """
    agent.setdefault("host_type", host_type or "local")
    agent.setdefault("host_label", host_label or _local_host_label())
    # Universal self-heal (Nebo, 07.09.): registry entries that carry a raw
    # technical hostname (with .local/.lan suffix) get prettified too — a
    # user who connected BEFORE the prettify-fix existed still sees a
    # readable badge.
    current = (agent.get("host_label") or "").strip()
    try:
        raw_hostname = socket.gethostname().strip()
    except Exception:
        raw_hostname = ""
    if current and raw_hostname and (current == raw_hostname or current == raw_hostname.split(".")[0]
                                     or current.endswith(".local") or current.endswith(".lan")):
        agent["host_label"] = _local_host_label()
    return agent


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

    # Detection scans the filesystem — keep it OUTSIDE the lock. The whole
    # registry read-modify-write below runs under the exclusive lock with
    # an atomic replace.
    with _registry_lock():
        registry = _load_registry_unlocked()
        kept, removed = [], []
        changed = False
        now = datetime.now(timezone.utc)

        for agent in registry.get("agents", []):
            aid = agent.get("id", "")
            # Review fix (MEDIUM :587): explicitly registered REMOTE agents
            # are never locally detectable and have no config_dir by design
            # (config_dir=None) — both regular cleanup conditions are ALWAYS
            # true for them, so the sweep would delete every registered
            # remote agent after grace_days. Remote seats therefore use a
            # much longer ghost horizon (30 days stale last_seen instead of
            # grace_days): user intent is protected, but a seat whose agent
            # NEVER called back still gets reaped eventually.
            if agent.get("host_type") == "remote":
                last_seen_raw_r = agent.get("lastSeen") or agent.get("last_seen") or ""
                try:
                    ls_r = datetime.fromisoformat(last_seen_raw_r) if last_seen_raw_r else None
                except Exception:
                    ls_r = None
                if ls_r is not None and ls_r.tzinfo is None:
                    ls_r = ls_r.replace(tzinfo=timezone.utc)
                remote_stale_days = (now - ls_r).days if ls_r else REMOTE_GHOST_DAYS + 1
                if remote_stale_days > REMOTE_GHOST_DAYS:
                    removed.append({"id": aid, "last_seen": last_seen_raw_r})
                else:
                    kept.append(aid)
                continue
            undetected = not detection_map.get(aid, False)
            config_dir = agent.get("config_dir")
            dir_gone = (not config_dir) or (not Path(config_dir).exists())

            # last_seen may be missing on hand-crafted entries -> treat as stale.
            last_seen_raw = agent.get("lastSeen") or agent.get("last_seen") or ""
            try:
                last_seen = datetime.fromisoformat(last_seen_raw) if last_seen_raw else None
            except Exception:
                last_seen = None
            if last_seen is not None and last_seen.tzinfo is None:
                # This module always writes tz-aware ISO timestamps; treat
                # hand-crafted timezone-less values as UTC so the naive/
                # aware subtraction below cannot crash the whole cleanup.
                last_seen = last_seen.replace(tzinfo=timezone.utc)
            stale_days = (now - last_seen).days if last_seen else grace_days + 1

            if undetected and dir_gone and stale_days > grace_days:
                removed.append({"id": aid, "last_seen": last_seen_raw})
            else:
                kept.append(aid)

        if removed:
            registry["agents"] = [a for a in registry.get("agents", []) if a.get("id") in set(kept)]
            changed = True

        # Backfill host annotations for legacy entries (pre-host-fields).
        for agent in registry.get("agents", []):
            if not agent.get("host_type"):
                annotate_host(agent, host_type="local")
                changed = True

        if changed:
            _save_registry_unlocked(registry)

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
    
    with _registry_lock():
        registry = _load_registry_unlocked()
        for a in registry.get("agents", []):
            if a.get("id") == agent_id:
                a["trust_level"] = trust_level
                _save_registry_unlocked(registry)
                return {"agent_id": agent_id, "trust_level": trust_level, "status": "updated"}

    return {"error": f"Agent not found: {agent_id}"}


def register_remote_agent(agent_id: str, name: str, trust_level: str = "trusted",
                          host_label: str = "", host_provider: str = "",
                          icon: str = "🌐", install_type: str = "mcp",
                          notes: str = "") -> dict:
    """Pre-register a REMOTE agent (VPS / other machine) in the registry.

    The remote harness is not on this machine, so detection will never
    confirm it: the entry is created host_type="remote" and stats start
    counting with its first real call (NEXUS_AGENT_ID set on the remote
    side). Trust defaults to "trusted" because remote agents are expected
    to talk to this Qdrant over the network by explicit user intent.
    """
    valid = ["public", "trusted", "private"]
    if trust_level not in valid:
        return {"error": f"Invalid trust level: {trust_level}"}
    if agent_id in LOCAL_AGENT_IDS:
        return {"error": f"Refusing to shadow a local agent id: {agent_id}"}

    entry = {
        "id": agent_id,
        "name": name or agent_id,
        "icon": icon if len(icon) <= 4 else "🌐",
        "trust_level": trust_level,
        "install_type": install_type,
        "config_dir": None,
        "connected_at": _now_iso(),
        "last_seen": _now_iso(),
        "reads": 0,
        "writes": 0,
        "host_type": "remote",
        "host_label": host_label or "unknown host",
        "host_provider": host_provider or "",
    }
    if notes:
        entry["notes"] = notes

    # Duplicate check + append + save inside ONE critical section: two
    # concurrent registrations of the same id must not both pass the check.
    with _registry_lock():
        registry = _load_registry_unlocked()
        if any(a.get("id") == agent_id for a in registry.get("agents", [])):
            return {"error": f"Agent id already registered: {agent_id}"}
        registry.setdefault("agents", []).append(entry)
        _save_registry_unlocked(registry)
    return {"status": "registered", "agent": entry, "next_step": (
        "On the remote machine: install nexus-memory, set "
        f"NEXUS_AGENT_ID={agent_id} and NEXUS_QDRANT_HOST to this "
        "machine's Tailscale IP. Dashboard shows stats on first call.")}


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