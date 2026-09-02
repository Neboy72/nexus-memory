#!/usr/bin/env python3
"""Nexus Memory - Orchestrated Setup Flow.

The missing link that chains all existing building blocks into one
coherent installation experience. Works as CLI and as Bot backend.

Flow:
    1. Welcome + Qdrant check
    2. Embedding auto-detect + user confirms (chat_wizard)
    3. Agent auto-detect (agent_detect - 15 agents)
    4. Pro Agent: Trust-Level choose (chat_wizard + agent_detect)
    5. Pro Agent: Plugin or MCP install
    6. Dashboard start + verify
    7. Done - all agents connected

Two modes:
    - CLI:  Interactive terminal prompts
    - JSON: Returns JSON steps for Bot/Chat display (Telegram, Discord, etc.)

Usage:
    # CLI interactive
    python3 -m nexus_memory.setup

    # JSON mode (for bots)
    python3 -m nexus_memory.setup --json scan_embedding
    python3 -m nexus_memory.setup --json apply_embedding voyage vo-xxx
    python3 -m nexus_memory.setup --json detect_agents
    python3 -m nexus_memory.setup --json set_trust hermes private
    python3 -m nexus_memory.setup --json install_agent hermes
    python3 -m nexus_memory.setup --json status
    python3 -m nexus_memory.setup --json complete
"""

from __future__ import annotations

import json
import os
import sys
import subprocess
from pathlib import Path
from typing import Optional

from nexus_memory.chat_wizard import (
    scan_providers, apply_choice, get_trust_levels, save_trust_level,
    get_status, TRUST_LEVELS, _load_config, _save_config
)
from nexus_memory.agent_detect import (
    detect_all_agents, register_agent, load_agents_registry,
    set_agent_trust_level, _get_agents_registry_path
)


# ── Plugin/MCP Installation ──────────────────────────────────────────────────

INSTALL_SCRIPTS = {
    "hermes": "scripts/install_hermes_plugin.sh",
    "openclaw": "scripts/install_openclaw_plugin.sh",
}

MCP_CONFIG_SNIPPETS = {
    "claude-code": {
        "file": "~/.claude/mcp.json",
        "config": {
            "nexus": {
                "command": "nexus-memory",
                "args": [],
                "env": {}
            }
        }
    },
    "cursor": {
        "file": "~/.cursor/mcp.json",
        "config": {
            "nexus": {
                "command": "nexus-memory",
                "args": [],
                "env": {}
            }
        }
    },
    "kilo-code": {
        "file": "~/.kilo/mcp.json",
        "config": {
            "nexus": {
                "command": "nexus-memory",
                "args": [],
                "env": {}
            }
        }
    },
    "codex": {
        "file": "~/.codex/mcp.json",
        "config": {
            "nexus": {
                "command": "nexus-memory",
                "args": [],
                "env": {}
            }
        }
    },
    "antigravity-cli": {
        "file": "~/.agy/mcp.json",
        "config": {
            "nexus": {
                "command": "nexus-memory",
                "args": [],
                "env": {}
            }
        }
    },
    "cline": {
        "file": "~/.cline/mcp.json",
        "config": {
            "nexus": {
                "command": "nexus-memory",
                "args": [],
                "env": {}
            }
        }
    },
    "roo-code": {
        "file": "~/.roo/mcp.json",
        "config": {
            "nexus": {
                "command": "nexus-memory",
                "args": [],
                "env": {}
            }
        }
    },
    "openhands": {
        "file": "~/.openhands/mcp.json",
        "config": {
            "nexus": {
                "command": "nexus-memory",
                "args": [],
                "env": {}
            }
        }
    },
    "qwen-code": {
        "file": "~/.qwen/mcp.json",
        "config": {
            "nexus": {
                "command": "nexus-memory",
                "args": [],
                "env": {}
            }
        }
    },
    "opencode": {
        "file": "~/.opencode/mcp.json",
        "config": {
            "nexus": {
                "command": "nexus-memory",
                "args": [],
                "env": {}
            }
        }
    },
    "windsurf": {
        "file": "~/.codeium/windsurf/mcp.json",
        "config": {
            "nexus": {
                "command": "nexus-memory",
                "args": [],
                "env": {}
            }
        }
    },
    "crush": {
        "file": "~/.crush/mcp.json",
        "config": {
            "nexus": {
                "command": "nexus-memory",
                "args": [],
                "env": {}
            }
        }
    },
    "pi": {
        "file": "~/.pi/mcp.json",
        "config": {
            "nexus": {
                "command": "nexus-memory",
                "args": [],
                "env": {}
            }
        }
    },
}


def _get_repo_root() -> Path:
    """Get the Nexus Memory repo root."""
    return Path(__file__).resolve().parent.parent.parent


def _install_plugin(agent_id: str) -> dict:
    """Install Nexus Memory as a native plugin for an agent."""
    script_rel = INSTALL_SCRIPTS.get(agent_id)
    if not script_rel:
        return {"agent_id": agent_id, "install_type": "plugin", "status": "no_script", "message": f"No plugin script for {agent_id}"}

    script_path = _get_repo_root() / script_rel
    if not script_path.exists():
        return {"agent_id": agent_id, "install_type": "plugin", "status": "script_missing", "message": f"Script not found: {script_path}"}

    try:
        result = subprocess.run(
            ["bash", str(script_path)],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0:
            return {"agent_id": agent_id, "install_type": "plugin", "status": "installed", "message": f"Plugin installed for {agent_id}"}
        else:
            return {"agent_id": agent_id, "install_type": "plugin", "status": "failed", "message": result.stderr.strip()[:200]}
    except Exception as e:
        return {"agent_id": agent_id, "install_type": "plugin", "status": "error", "message": str(e)}


def _install_mcp(agent_id: str) -> dict:
    """Install Nexus Memory as an MCP server for an agent."""
    snippet = MCP_CONFIG_SNIPPETS.get(agent_id)
    if not snippet:
        return {"agent_id": agent_id, "install_type": "mcp", "status": "no_config", "message": f"No MCP config template for {agent_id}"}

    mcp_file = Path(snippet["file"]).expanduser()
    mcp_file.parent.mkdir(parents=True, exist_ok=True)

    # Load existing config or create new
    existing = {}
    if mcp_file.exists():
        try:
            existing = json.loads(mcp_file.read_text())
        except Exception:
            existing = {}

    # Merge nexus into mcpServers
    servers_key = "mcpServers" if "mcpServers" not in existing else "mcpServers"
    if servers_key not in existing:
        existing[servers_key] = {}

    if "nexus" in existing[servers_key]:
        return {"agent_id": agent_id, "install_type": "mcp", "status": "already_installed", "message": f"Nexus MCP already configured for {agent_id}"}

    existing[servers_key]["nexus"] = snippet["config"]["nexus"]
    mcp_file.write_text(json.dumps(existing, indent=2) + "\n")

    return {"agent_id": agent_id, "install_type": "mcp", "status": "installed", "message": f"MCP server configured for {agent_id} at {mcp_file}"}


def install_agent(agent_id: str, trust_level: str = "public") -> dict:
    """Install Nexus Memory for an agent (plugin if available, else MCP) and register it."""
    # Get agent info from detection
    detection = detect_all_agents()
    agent_info = None
    for a in detection["detected_agents"] + detection["not_detected"]:
        if a["id"] == agent_id:
            agent_info = a
            break

    if not agent_info:
        return {"error": f"Unknown agent: {agent_id}"}

    # Try plugin first, then MCP
    if agent_info.get("plugin_available"):
        result = _install_plugin(agent_id)
        install_type = "plugin+mcp" if agent_info.get("mcp_available") else "plugin"
    elif agent_info.get("mcp_available"):
        result = _install_mcp(agent_id)
        install_type = "mcp"
    else:
        return {"error": f"Agent {agent_id} supports neither plugin nor MCP"}

    # If plugin succeeded and MCP is also available, add MCP too
    if result["status"] == "installed" and agent_info.get("plugin_available") and agent_info.get("mcp_available"):
        mcp_result = _install_mcp(agent_id)
        if mcp_result["status"] in ("installed", "already_installed"):
            install_type = "plugin+mcp"

    # Register agent in registry
    register_agent(
        agent_id=agent_id,
        name=agent_info["name"],
        icon=agent_info.get("icon", "❓"),
        trust_level=trust_level,
        install_type=install_type,
        config_dir=agent_info.get("config_dir"),
    )

    return {
        "agent_id": agent_id,
        "name": agent_info["name"],
        "trust_level": trust_level,
        "install_type": install_type,
        "status": result["status"],
        "message": result["message"],
    }


# ── Step Functions (for JSON mode) ───────────────────────────────────────────

def step_welcome() -> dict:
    """Step 1: Welcome + Qdrant check."""
    qdrant_running = False
    try:
        import urllib.request
        req = urllib.request.Request("http://localhost:6333/health", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=2) as resp:
            qdrant_running = resp.status == 200
    except Exception:
        pass

    return {
        "step": "welcome",
        "title": "Welcome to Nexus Memory Setup",
        "message": "Nexus Memory is a universal memory layer for AI agents. This setup will configure embedding, detect your agents, and connect them.",
        "qdrant_running": qdrant_running,
        "qdrant_instructions": "Make sure Qdrant is running on localhost:6333. Start it with: docker run -p 6333:6333 qdrant/qdrant" if not qdrant_running else None,
        "next_step": "scan_embedding",
    }


def step_scan_embedding() -> dict:
    """Step 2: Scan for embedding providers."""
    return scan_providers()


def step_apply_embedding(provider_id: str, api_key: str = None) -> dict:
    """Step 2b: Apply embedding provider choice."""
    return apply_choice(provider_id, api_key)


def step_detect_agents() -> dict:
    """Step 3: Detect installed agents."""
    return detect_all_agents()


def step_set_trust(agent_id: str, trust_level: str) -> dict:
    """Step 4: Set trust level for an agent and install it."""
    # Validate trust level
    valid = [l["id"] for l in TRUST_LEVELS]
    if trust_level not in valid:
        return {"error": f"Invalid trust level: {trust_level}. Must be one of: {valid}"}

    # Install + register
    return install_agent(agent_id, trust_level)


def step_status() -> dict:
    """Return full setup status."""
    base_status = get_status()
    registry = load_agents_registry()

    return {
        "embedding": {
            "provider": base_status.get("embedding_provider", "not set"),
            "qdrant_running": base_status.get("qdrant_running", False),
        },
        "agents": {
            "registered": registry.get("agents", []),
            "count": len(registry.get("agents", [])),
        },
        "config_path": base_status.get("config_path"),
        "env_path": base_status.get("env_path"),
    }


def step_complete() -> dict:
    """Final step: Summary of what was configured."""
    status = step_status()
    agents = status["agents"]["registered"]

    return {
        "step": "complete",
        "title": "Nexus Memory Setup Complete!",
        "summary": {
            "embedding_provider": status["embedding"]["provider"],
            "qdrant_running": status["embedding"]["qdrant_running"],
            "agents_connected": len(agents),
            "agents": [
                {
                    "name": a.get("name", a["id"]),
                    "icon": a.get("icon", ""),
                    "trust_level": a.get("trust_level", "public"),
                    "install_type": a.get("install_type", "mcp"),
                }
                for a in agents
            ],
        },
        "dashboard_url": "http://localhost:9121",
        "message": "Nexus Memory is ready. Start the dashboard with: nexus-memory dashboard",
    }


# ── CLI Interactive Mode ────────────────────────────────────────────────────

def _cli_print(json_data: dict, indent: int = 0):
    """Pretty-print JSON data for CLI."""
    prefix = "  " * indent

    if "title" in json_data:
        print(f"\n{prefix}📋 {json_data['title']}")
    if "message" in json_data:
        print(f"{prefix}{json_data['message']}")

    if "providers" in json_data:
        for i, p in enumerate(json_data["providers"]):
            marker = " ⭐" if i == json_data.get("recommended_index", -1) else ""
            status = "✅" if p["available"] else "❌"
            key_info = f" (needs API key: {p['key_url']})" if p.get("needs_api_key") else ""
            print(f"{prefix}  [{i+1}] {status} {p['icon']} {p['name']} ({p['dims']}d, {p['quality']}){key_info}{marker}")

    if "detected_agents" in json_data:
        print(f"\n{prefix}Detected agents:")
        for a in json_data["detected_agents"]:
            nexus = " 🔗" if a.get("nexus_installed") else ""
            plugin = " [Plugin+MCP]" if a.get("plugin_available") else " [MCP]"
            print(f"{prefix}  {a['icon']} {a['name']} ({a['id']}){plugin}{nexus}")

        not_detected = json_data.get("not_detected", [])
        if not_detected:
            print(f"\n{prefix}Not installed:")
            for a in not_detected:
                print(f"{prefix}  ⬜ {a['name']} ({a['id']})")

    if "levels" in json_data:
        for i, l in enumerate(json_data["levels"]):
            current = " ← current" if l["id"] == json_data.get("current") else ""
            print(f"{prefix}  [{i+1}] {l['icon']} {l['name']}: {l['description']}{current}")

    if "agents_connected" in json_data:
        print(f"\n{prefix}✅ {json_data['summary']['agents_connected']} agent(s) connected")
        for a in json_data["summary"]["agents"]:
            print(f"{prefix}  {a['icon']} {a['name']} - {a['trust_level']} ({a['install_type']})")
        print(f"\n{prefix}Dashboard: {json_data.get('dashboard_url', 'http://localhost:9121')}")


def cli_interactive():
    """Run the full setup in interactive CLI mode."""
    # Step 1: Welcome
    welcome = step_welcome()
    _cli_print(welcome)

    if not welcome["qdrant_running"]:
        print(f"\n  ⚠️  Qdrant is not running. {welcome.get('qdrant_instructions', '')}")
        resp = input("\n  Continue anyway? (y/n): ").strip().lower()
        if resp != "y":
            print("  Setup aborted. Start Qdrant first.")
            return

    # Step 2: Embedding
    scan = step_scan_embedding()
    _cli_print(scan)

    available = [p for p in scan["providers"] if p["available"]]
    if not available:
        print("\n  ⚠️  No embedding providers available.")
        print("  Install Ollama (https://ollama.com) with an embed model, or set an API key.")
        return

    recommended = scan["providers"][scan["recommended_index"]]
    choice = input(f"\n  Choose provider (1-{len(scan['providers'])}) [Enter for {recommended['name']}]: ").strip()

    if not choice:
        provider_id = recommended["id"]
        api_key = None
    else:
        parts = choice.split(maxsplit=1)
        try:
            idx = int(parts[0]) - 1
            provider_id = scan["providers"][idx]["id"]
            api_key = parts[1] if len(parts) > 1 else None
        except (ValueError, IndexError):
            provider_id = choice
            api_key = parts[1] if len(parts) > 1 else None

    result = step_apply_embedding(provider_id, api_key)
    _cli_print(result)

    # Step 3: Agent Detection
    detection = step_detect_agents()
    _cli_print(detection)

    detected = detection["detected_agents"]
    if not detected:
        print("\n  No agents detected. Install an agent (Hermes, Claude Code, etc.) and re-run setup.")
        return

    # Step 4: Trust Level per Agent
    print(f"\n  Now choose a trust level for each detected agent.")
    print(f"  🟢 Public = general facts only")
    print(f"  🟡 Trusted = facts + setup details")
    print(f"  🔴 Private = everything (primary agent only)")
    print()

    for agent in detected:
        agent_id = agent["id"]
        name = agent["name"]
        icon = agent.get("icon", "")

        # Suggest default trust level
        if agent_id in ("hermes", "openclaw"):
            suggested = "private"
        elif agent.get("plugin_available"):
            suggested = "trusted"
        else:
            suggested = "public"

        print(f"  {icon} {name} ({agent_id})")
        print(f"    Plugin: {'✅' if agent.get('plugin_available') else '❌'}  MCP: {'✅' if agent.get('mcp_available') else '❌'}")
        choice = input(f"    Trust level (public/trusted/private) [{suggested}]: ").strip().lower()

        if not choice:
            trust_level = suggested
        elif choice in ("public", "trusted", "private", "1", "2", "3"):
            level_map = {"1": "public", "2": "trusted", "3": "private"}
            trust_level = level_map.get(choice, choice)
        else:
            print(f"    Invalid choice, using {suggested}")
            trust_level = suggested

        result = step_set_trust(agent_id, trust_level)
        _cli_print(result, indent=1)
        print()

    # Step 5: Complete
    complete = step_complete()
    _cli_print(complete)


# ── JSON Mode (for Bots) ────────────────────────────────────────────────────

def json_mode(args: list[str]):
    """Handle JSON mode commands for bot/chat integration."""
    if not args:
        print(json.dumps({"error": "Usage: setup --json <command> [args...]"}))
        sys.exit(1)

    command = args[0]

    if command == "welcome":
        result = step_welcome()
    elif command == "scan_embedding":
        result = step_scan_embedding()
    elif command == "apply_embedding":
        if len(args) < 2:
            result = {"error": "Usage: setup --json apply_embedding <provider_id> [api_key]"}
        else:
            provider_id = args[1]
            api_key = args[2] if len(args) > 2 else None
            result = step_apply_embedding(provider_id, api_key)
    elif command == "detect_agents":
        result = step_detect_agents()
    elif command == "set_trust":
        if len(args) < 3:
            result = {"error": "Usage: setup --json set_trust <agent_id> <trust_level>"}
        else:
            result = step_set_trust(args[1], args[2])
    elif command == "status":
        result = step_status()
    elif command == "complete":
        result = step_complete()
    elif command == "install_agent":
        if len(args) < 2:
            result = {"error": "Usage: setup --json install_agent <agent_id> [trust_level]"}
        else:
            trust_level = args[2] if len(args) > 2 else "public"
            result = install_agent(args[1], trust_level)
    else:
        result = {"error": f"Unknown command: {command}"}

    print(json.dumps(result, indent=2))


# ── Entry Point ─────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]

    if args and args[0] == "--json":
        json_mode(args[1:])
    else:
        cli_interactive()


if __name__ == "__main__":
    main()