#!/usr/bin/env bash
# install_claude_plugin.sh — One-command install for the Nexus Memory Claude Code plugin
#
# Copies plugins/claude-code into ~/.claude/plugins/nexus-memory (the location
# Claude Code loads skills-dir plugins from), then verifies the hook scripts and
# the MCP bootstrap interpreter.
#
# Idempotent: running it twice just refreshes the copy (a timestamped backup of
# the previous install is kept).
#
# Usage: ./scripts/install_claude_plugin.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NEXUS_REPO="$(cd "${SCRIPT_DIR}/.." && pwd)"
PLUGIN_SRC="${NEXUS_REPO}/plugins/claude-code"

CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-${HOME}/.claude}"
PLUGIN_DST="${CLAUDE_DIR}/plugins/nexus-memory"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo "=== Nexus Memory — Claude Code Plugin Installer ==="
echo

if [ ! -d "${PLUGIN_SRC}" ]; then
    echo -e "${RED}✗${NC} Plugin source not found at ${PLUGIN_SRC}"
    echo "  Clone the repo first: git clone https://github.com/Neboy72/nexus-memory.git ~/nexus-memory"
    exit 1
fi

if [ ! -d "${CLAUDE_DIR}" ]; then
    echo -e "${RED}✗${NC} Claude Code config dir not found: ${CLAUDE_DIR}"
    echo "  Install Claude Code first, or set CLAUDE_CONFIG_DIR."
    exit 1
fi

# --- Backup an existing install (house rule: never overwrite without one) ---

if [ -d "${PLUGIN_DST}" ]; then
    BACKUP="${PLUGIN_DST}.bak-$(date +%Y%m%d-%H%M%S)"
    cp -R "${PLUGIN_DST}" "${BACKUP}"
    echo -e "${GREEN}✓${NC} Backup of previous install: ${BACKUP}"
fi

# --- Copy (exclude caches) ---

mkdir -p "${PLUGIN_DST}"
if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete --exclude='__pycache__' "${PLUGIN_SRC}/" "${PLUGIN_DST}/"
else
    rm -rf "${PLUGIN_DST:?}/"*
    cp -R "${PLUGIN_SRC}/." "${PLUGIN_DST}/"
    find "${PLUGIN_DST}" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
fi
echo -e "${GREEN}✓${NC} Plugin installed: ${PLUGIN_DST}"

# --- Verify the pieces that make it work ---

VERSION=$(python3 -c "import json,sys; print(json.load(open('${PLUGIN_DST}/.claude-plugin/plugin.json'))['version'])" 2>/dev/null || echo "?")
echo -e "${GREEN}✓${NC} Version: ${VERSION}"

for f in scripts/auto_recall.py scripts/auto_capture.py scripts/session_start.py \
         scripts/guardrail_check.py scripts/self_check.py scripts/mcp_bootstrap.py \
         hooks/nexus-hooks.json; do
    if [ ! -f "${PLUGIN_DST}/${f}" ]; then
        echo -e "${RED}✗${NC} Missing after install: ${f}"
        exit 1
    fi
done
echo -e "${GREEN}✓${NC} All hook scripts present (incl. self_check)"

# --- MCP server: is there an interpreter that can import nexus_memory? ---

if python3 -c "import nexus_memory" 2>/dev/null; then
    echo -e "${GREEN}✓${NC} 'nexus_memory' importable with python3 (MCP server will start)"
else
    echo -e "${YELLOW}!${NC} 'nexus_memory' is not importable by python3 — the MCP server needs it."
    echo "    Fix (one command):"
    echo "      pip install -e \"${NEXUS_REPO}\""
    echo "    The plugin will still run its hooks; only the MCP tools are unavailable."
fi

# --- Qdrant reachability (informational; the plugin fails open) ---

QDRANT_URL="${NEXUS_QDRANT_URL:-http://localhost:6333}"
if curl -s -m 3 "${QDRANT_URL}/collections" >/dev/null 2>&1; then
    echo -e "${GREEN}✓${NC} Qdrant reachable at ${QDRANT_URL}"
else
    echo -e "${YELLOW}!${NC} Qdrant not reachable at ${QDRANT_URL} — start it before using the plugin."
fi

echo
echo "Next steps:"
echo "  1. Restart Claude Code (or run /reload-plugins)."
echo "  2. The SessionStart hook now writes ${NEXUS_DATA_DIR:-$HOME/.nexus-memory}/agent-selfcheck-claude-code.json"
echo "     so a broken memory is reported instead of going silent."
echo
echo -e "${GREEN}Done.${NC}"
