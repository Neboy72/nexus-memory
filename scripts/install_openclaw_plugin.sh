#!/usr/bin/env bash
# install_openclaw_plugin.sh — One-command install for the Nexus Memory OpenClaw plugin
#
# Detects OpenClaw installation, patches ~/.openclaw/openclaw.json to register
# the nexus-memory plugin, auto-detects your embedding provider, and restarts
# the OpenClaw gateway.
#
# Idempotent: running it twice won't duplicate entries or break existing config.
#
# Usage: ./scripts/install_openclaw_plugin.sh
set -euo pipefail

# --- Paths ---

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NEXUS_REPO="$(cd "${SCRIPT_DIR}/.." && pwd)"
PLUGIN_SRC="${NEXUS_REPO}/plugins/openclaw"

OPENCLAW_STATE_DIR="${OPENCLAW_STATE_DIR:-${HOME}/.openclaw}"
OPENCLAW_CONFIG="${OPENCLAW_STATE_DIR}/openclaw.json"

# --- Colors ---

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo "=== Nexus Memory — OpenClaw Plugin Installer ==="
echo ""

# --- Pre-flight checks ---

if [ ! -d "${NEXUS_REPO}" ]; then
    echo -e "${RED}✗${NC} Nexus Memory repo not found at ${NEXUS_REPO}"
    echo "  Clone it first: git clone https://github.com/Neboy72/nexus-memory.git ~/nexus-memory"
    exit 1
fi

if [ ! -d "${PLUGIN_SRC}" ]; then
    echo -e "${RED}✗${NC} OpenClaw plugin source not found at ${PLUGIN_SRC}"
    echo "  Make sure the repo is complete (plugins/openclaw/ directory)."
    exit 1
fi

# --- Check OpenClaw installation ---

OPENCLAW_FOUND=false

if command -v openclaw &> /dev/null; then
    OPENCLAW_FOUND=true
    echo -e "${GREEN}✓${NC} 'openclaw' command found: $(which openclaw)"
else
    echo -e "${YELLOW}⚠${NC} 'openclaw' command not found on PATH."
fi

if [ -d "${OPENCLAW_STATE_DIR}" ]; then
    OPENCLAW_FOUND=true
    echo -e "${GREEN}✓${NC} OpenClaw state directory found: ${OPENCLAW_STATE_DIR}"
else
    echo -e "${YELLOW}⚠${NC} OpenClaw state directory not found at ${OPENCLAW_STATE_DIR}"
fi

if [ "${OPENCLAW_FOUND}" = false ]; then
    echo ""
    echo -e "${RED}✗${NC} OpenClaw installation not detected."
    echo "  Install OpenClaw first, or use the MCP server instead:"
    echo "    nexus-memory  (then configure mcpServers in your agent)"
    echo "  See AGENTS.md for MCP setup instructions."
    exit 0
fi

echo ""

# --- Ensure state directory exists ---

mkdir -p "${OPENCLAW_STATE_DIR}"

# --- Auto-detect embedding provider ---

detect_embedding() {
    if [ -n "${VOYAGE_API_KEY:-}" ]; then
        EMBEDDING_PROVIDER="voyage"
        EMBEDDING_MODEL="voyage-3-large"
        EMBEDDING_APIKEY='${VOYAGE_API_KEY}'
        echo -e "${GREEN}✓${NC} Embedding: Voyage (voyage-3-large, 1024d)"
    elif [ -n "${OPENAI_API_KEY:-}" ]; then
        EMBEDDING_PROVIDER="openai"
        EMBEDDING_MODEL="text-embedding-3-small"
        EMBEDDING_APIKEY='${OPENAI_API_KEY}'
        echo -e "${GREEN}✓${NC} Embedding: OpenAI (text-embedding-3-small, 1536d)"
    elif [ -n "${GOOGLE_API_KEY:-}" ]; then
        EMBEDDING_PROVIDER="google"
        EMBEDDING_MODEL="text-embedding-004"
        EMBEDDING_APIKEY='${GOOGLE_API_KEY}'
        echo -e "${GREEN}✓${NC} Embedding: Google (text-embedding-004, 768d)"
    elif [ -n "${JINA_API_KEY:-}" ]; then
        EMBEDDING_PROVIDER="jina"
        EMBEDDING_MODEL="jina-embeddings-v3"
        EMBEDDING_APIKEY='${JINA_API_KEY}'
        echo -e "${GREEN}✓${NC} Embedding: Jina (jina-embeddings-v3, 1024d)"
    elif command -v ollama &> /dev/null; then
        EMBEDDING_PROVIDER="ollama"
        EMBEDDING_MODEL="nomic-embed-text"
        EMBEDDING_APIKEY=""
        echo -e "${GREEN}✓${NC} Embedding: Ollama (nomic-embed-text, 768d) — local, no API key needed"
    else
        EMBEDDING_PROVIDER="voyage"
        EMBEDDING_MODEL="voyage-3-large"
        EMBEDDING_APIKEY='${VOYAGE_API_KEY}'
        echo -e "${YELLOW}⚠${NC} No embedding provider detected. Defaulting to Voyage."
        echo "  Set one of: VOYAGE_API_KEY, OPENAI_API_KEY, GOOGLE_API_KEY, JINA_API_KEY"
        echo "  Or install Ollama with an embed model for local zero-setup."
    fi
}

detect_embedding
echo ""

# --- Create or patch openclaw.json ---

# We use Python for reliable JSON manipulation (jq may not be installed)
patch_config() {
    python3 - "$OPENCLAW_CONFIG" "$PLUGIN_SRC" "$EMBEDDING_PROVIDER" "$EMBEDDING_MODEL" "$EMBEDDING_APIKEY" <<'PYEOF'
import json
import os
import sys
import time
from pathlib import Path

config_path = Path(sys.argv[1])
plugin_src  = sys.argv[2]
provider    = sys.argv[3]
model       = sys.argv[4]
api_key     = sys.argv[5]

# Load existing config or start fresh
if config_path.exists():
    with open(config_path) as f:
        try:
            config = json.load(f)
        except json.JSONDecodeError:
            # Backup the broken file and start fresh
            backup = config_path.with_suffix(f".json.broken.{int(time.time())}")
            config_path.rename(backup)
            print(f"  ⚠ Existing config was invalid JSON. Backed up to {backup}")
            config = {}
else:
    config = {}

# Ensure top-level "plugins" key exists
plugins = config.setdefault("plugins", {})

# --- 1. plugins.load.paths (idempotent) ---
load = plugins.setdefault("load", {})
paths = load.setdefault("paths", [])
if plugin_src not in paths:
    paths.append(plugin_src)
    print(f"  ✓ Added to plugins.load.paths: {plugin_src}")
else:
    print(f"  ✓ Already in plugins.load.paths: {plugin_src}")

# --- 2. plugins.slots.memory (idempotent) ---
slots = plugins.setdefault("slots", {})
if slots.get("memory") != "nexus-memory":
    slots["memory"] = "nexus-memory"
    print('  ✓ Set plugins.slots.memory = "nexus-memory"')
else:
    print('  ✓ plugins.slots.memory already set to "nexus-memory"')

# --- 3. plugins.entries.nexus-memory (idempotent) ---
entries = plugins.setdefault("entries", {})
entry = entries.get("nexus-memory", {})

# Preserve existing enabled / hooks / config, but ensure all required fields
entry["enabled"] = entry.get("enabled", True)

hooks = entry.setdefault("hooks", {})
hooks.setdefault("allowPromptInjection", True)
hooks.setdefault("allowConversationAccess", True)

cfg = entry.setdefault("config", {})
cfg.setdefault("qdrantUrl", "http://localhost:6333")
cfg.setdefault("collection", "nexus")

# Embedding: only set if not already configured
embedding = cfg.setdefault("embedding", {})
if "provider" not in embedding:
    embedding["provider"] = provider
    embedding["model"] = model
    if api_key:
        embedding["apiKey"] = api_key

cfg.setdefault("autoRecall", True)
cfg.setdefault("autoCapture", True)
cfg.setdefault("maxRecallResults", 10)
cfg.setdefault("accessLevel", "private")

entries["nexus-memory"] = entry
plugins["entries"] = entries

# --- 4. plugins.allow (idempotent) ---
allow = plugins.get("allow")
if allow is None:
    # Don't create "allow" if it doesn't exist — some configs use a deny list instead.
    # Only add if there's already an allow list.
    pass
elif isinstance(allow, list):
    if "nexus-memory" not in allow:
        allow.append("nexus-memory")
        print('  ✓ Added "nexus-memory" to plugins.allow')
    else:
        print('  ✓ "nexus-memory" already in plugins.allow')

# Write back
config_path.parent.mkdir(parents=True, exist_ok=True)
with open(config_path, "w") as f:
    json.dump(config, f, indent=2, ensure_ascii=False)
    f.write("\n")

print(f"  ✓ Config written to {config_path}")
PYEOF
}

# Backup existing config before patching
if [ -f "${OPENCLAW_CONFIG}" ]; then
    BACKUP="${OPENCLAW_CONFIG}.bak.$(date +%s)"
    cp "${OPENCLAW_CONFIG}" "${BACKUP}"
    echo -e "${BLUE}ℹ${NC} Backed up existing config to ${BACKUP}"
    echo ""
fi

echo "Patching ${OPENCLAW_CONFIG}..."
patch_config
echo ""

# --- Restart OpenClaw gateway ---

if command -v openclaw &> /dev/null; then
    echo -e "${BLUE}ℹ${NC} Restarting OpenClaw gateway..."
    if openclaw gateway restart 2>/dev/null; then
        echo -e "${GREEN}✓${NC} OpenClaw gateway restarted."
    else
        echo -e "${YELLOW}⚠${NC} 'openclaw gateway restart' failed. Restart manually: openclaw gateway restart"
    fi
else
    echo -e "${YELLOW}⚠${NC} 'openclaw' CLI not found. Restart gateway manually: openclaw gateway restart"
fi

echo ""
echo "=== Done! Nexus Memory OpenClaw plugin installed. ==="
echo ""
echo "Verify with:"
echo "  openclaw plugins list        # → nexus-memory should appear"
echo "  openclaw gateway status      # → should show nexus-memory loaded"
echo ""
echo "Nexus tools available to your agent:"
echo "  nexus_search   — hybrid search memories"
echo "  nexus_store    — store a new memory"
echo "  nexus_forget   — delete a memory"
echo ""
echo "Auto-Recall: memories injected before every turn"
echo "Auto-Capture: conversation stored after every turn"
echo ""
echo "Shared store: same Qdrant 'nexus' collection as Hermes plugin and MCP server."