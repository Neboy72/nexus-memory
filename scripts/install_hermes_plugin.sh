#!/usr/bin/env bash
# install_hermes_plugin.sh — Symlink the Nexus Memory Hermes native plugin
#
# One-command setup for Hermes Agent users.
# Links ~/nexus-memory/plugins/memory/nexus → ~/.hermes/hermes-agent/plugins/memory/nexus
# and sets memory.provider to "nexus".
#
# Usage: ./scripts/install_hermes_plugin.sh
set -euo pipefail

HERMES_PLUGIN_DIR="${HOME}/.hermes/hermes-agent/plugins/memory"
NEXUS_REPO="${HOME}/nexus-memory"
PLUGIN_SRC="${NEXUS_REPO}/plugins/memory/nexus"
PLUGIN_DST="${HERMES_PLUGIN_DIR}/nexus"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "=== Nexus Memory — Hermes Native Plugin Installer ==="
echo ""

# --- Pre-flight checks ---

if [ ! -d "${NEXUS_REPO}" ]; then
    echo -e "${RED}✗${NC} Nexus Memory repo not found at ${NEXUS_REPO}"
    echo "  Clone it first: git clone https://github.com/Neboy72/nexus-memory.git ~/nexus-memory"
    exit 1
fi

if [ ! -d "${PLUGIN_SRC}" ]; then
    echo -e "${RED}✗${NC} Plugin source not found at ${PLUGIN_SRC}"
    exit 1
fi

# --- Check Hermes installation ---

if [ ! -d "${HERMES_PLUGIN_DIR}" ]; then
    echo -e "${YELLOW}⚠${NC} Hermes Agent not found (${HERMES_PLUGIN_DIR} missing)."
    echo "  Use the MCP server instead: run 'nexus-memory' and configure your agent's mcpServers."
    echo "  See AGENTS.md for MCP setup instructions."
    exit 0
fi

# --- Link the plugin ---

if [ -L "${PLUGIN_DST}" ]; then
    current_target="$(readlink "${PLUGIN_DST}")"
    if [ "${current_target}" = "${PLUGIN_SRC}" ]; then
        echo -e "${GREEN}✓${NC} Plugin already linked: ${PLUGIN_DST} → ${PLUGIN_SRC}"
    else
        echo -e "${YELLOW}⚠${NC} Existing symlink points elsewhere (${current_target}). Replacing..."
        rm "${PLUGIN_DST}"
        ln -s "${PLUGIN_SRC}" "${PLUGIN_DST}"
        echo -e "${GREEN}✓${NC} Plugin linked: ${PLUGIN_DST} → ${PLUGIN_SRC}"
    fi
elif [ -d "${PLUGIN_DST}" ]; then
    echo -e "${YELLOW}⚠${NC} ${PLUGIN_DST} exists as a directory (not a symlink)."
    echo "  Backing up to ${PLUGIN_DST}.bak and replacing with symlink."
    mv "${PLUGIN_DST}" "${PLUGIN_DST}.bak"
    ln -s "${PLUGIN_SRC}" "${PLUGIN_DST}"
    echo -e "${GREEN}✓${NC} Plugin linked (backup at ${PLUGIN_DST}.bak)"
else
    ln -s "${PLUGIN_SRC}" "${PLUGIN_DST}"
    echo -e "${GREEN}✓${NC} Plugin linked: ${PLUGIN_DST} → ${PLUGIN_SRC}"
fi

# --- Set memory.provider ---

if command -v hermes &> /dev/null; then
    hermes config set memory.provider nexus
    echo -e "${GREEN}✓${NC} Hermes config: memory.provider = nexus"
else
    echo -e "${YELLOW}⚠${NC} 'hermes' CLI not found on PATH. Set manually: hermes config set memory.provider nexus"
fi

echo ""
echo "=== Done! Restart Hermes Gateway to activate Nexus Memory. ==="
