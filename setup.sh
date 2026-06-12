#!/usr/bin/env bash
#
# Nexus Memory — Universal Memory Layer for AI Agents
# One-time setup script. Idempotent — safe to re-run.
#
set -euo pipefail

# ── Color helpers ─────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e " ${GREEN}✅${NC} $1"; }
warn() { echo -e " ${YELLOW}⚠️${NC} $1"; }
fail() { echo -e " ${RED}❌${NC} $1"; exit 1; }
info() { echo -e " ${CYAN}ℹ️${NC} $1"; }

# ── Config ────────────────────────────────────────────────────────────
REPO="nexus-memory"
REPO_URL="https://github.com/Neboy72/${REPO}.git"
INSTALL_DIR="${HOME}/${REPO}"

# ── Step 1: Check Python ──────────────────────────────────────────────
info "Checking Python..."
PYTHON=""
for cmd in python3.12 python3.11 python3; do
    if command -v "$cmd" &>/dev/null; then
        ver=$("$cmd" --version 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
        major="${ver%.*}"; minor="${ver#*.}"
        if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ] 2>/dev/null; then
            PYTHON="$cmd"
            ok "Python $("$PYTHON" --version 2>&1)"
            break
        fi
    fi
done
[ -n "$PYTHON" ] || fail "Python 3.11+ required"

# ── Step 2: Check / Install Qdrant ────────────────────────────────────
info "Checking Qdrant..."
if curl -sf http://127.0.0.1:6333/healthz >/dev/null 2>&1; then
    ok "Qdrant is running"
else
    warn "Qdrant not running"
    if [[ "$OSTYPE" == "darwin"* ]]; then
        if command -v brew &>/dev/null; then
            info "Installing Qdrant via Homebrew..."
            brew install qdrant 2>/dev/null || true
            brew services start qdrant 2>/dev/null || true
            sleep 2
            if curl -sf http://127.0.0.1:6333/healthz >/dev/null 2>&1; then
                ok "Qdrant installed and running"
            else
                warn "Qdrant installed but not responding — start manually: brew services start qdrant"
            fi
        else
            warn "Install Qdrant manually: https://qdrant.tech/documentation/quick-start/"
        fi
    else
        warn "Install Qdrant manually: https://qdrant.tech/documentation/quick-start/"
    fi
fi

# ── Step 3: Clone / Update Repo ───────────────────────────────────────
info "Setting up ${REPO}..."
if [ -d "$INSTALL_DIR" ]; then
    info "Repository exists — pulling latest..."
    cd "$INSTALL_DIR"
    git pull origin main --ff-only 2>/dev/null || warn "Could not pull (uncommitted changes?)"
else
    git clone "$REPO_URL" "$INSTALL_DIR"
    ok "Cloned ${REPO}"
fi

cd "$INSTALL_DIR"

# ── Step 4: Install Dependencies ──────────────────────────────────────
info "Installing Python dependencies..."
if command -v uv &>/dev/null; then
    if [ -n "${VIRTUAL_ENV:-}" ]; then
        uv pip install -e . 2>/dev/null || $PYTHON -m pip install -e . --quiet
    else
        $PYTHON -m pip install --upgrade pip -q 2>/dev/null || true
        $PYTHON -m pip install -e . --quiet
    fi
else
    $PYTHON -m pip install -e . --quiet
fi
ok "Nexus Memory installed: v$($PYTHON -c "from nexus import __version__; print(__version__)" 2>/dev/null || echo "?")"

# ── Step 5: Embedding Provider ────────────────────────────────────────
info "Detecting available embedding backends..."

EMBED_SUGGEST="sentence-transformers"
EMBED_HINT="🏠 Default (local, no key): pip install sentence-transformers"

# Check Jina
if [ -n "${JINA_API_KEY:-}" ]; then
    EMBED_HINT="$EMBED_HINT | 💜 Jina (API key set)"
    ok "Jina API key detected"
fi

# Check Google
if [ -n "${GOOGLE_API_KEY:-}" ]; then
    EMBED_HINT="$EMBED_HINT | 💚 Google/Vertex (API key set)"
    ok "Google API key detected"
fi

# Check Ollama
if curl -sf http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    OLLAMA_EMBED=$($PYTHON -c "
import json, urllib.request
r = urllib.request.urlopen('http://localhost:11434/api/tags', timeout=2)
models = [m['name'] for m in json.loads(r.read()).get('models', [])]
emb = [m for m in models if 'embed' in m.lower()]
print(emb[0] if emb else '')
" 2>/dev/null)
    if [ -n "$OLLAMA_EMBED" ]; then
        EMBED_SUGGEST="ollama:$OLLAMA_EMBED"
        EMBED_HINT="🦙 Ollama ($OLLAMA_EMBED detected) — recommended"
        ok "Ollama embedding detected: $OLLAMA_EMBED"
    fi
fi

# Check OpenAI
if [ -n "${OPENAI_API_KEY:-}" ]; then
    EMBED_HINT="$EMBED_HINT | ☁️ OpenAI (API key set)"
    ok "OpenAI API key detected"
fi

# Check Voyage
if [ -n "${VOYAGE_API_KEY:-}" ]; then
    EMBED_HINT="$EMBED_HINT | ☁️ Voyage (API key set)"
    ok "Voyage AI API key detected"
fi

echo ""
info "Recommended: $EMBED_HINT"
info "The MCP server auto-detects the best available backend at startup."
echo ""

# ── Step 6: Quick Test ────────────────────────────────────────────────
info "Running quick smoke test..."
if $PYTHON -c "
from nexus import MemoryCategory
from nexus.health import DriftDetector
from nexus.retrieval import HybridRetriever
print('OK')
" 2>/dev/null; then
    ok "Core imports OK"
else
    warn "Import test failed — check Python environment"
fi

# ── Step 7: Platform Configuration Guide ──────────────────────────
echo ""
info "═══════════════════════════════════════════════"
info "  Platform Configuration Guide"
info "═══════════════════════════════════════════════"
echo ""

info "Universal MCP Config (works with any MCP client):"
echo '  Create ~/.nexus-mcp.json or add to your MCP config:'
echo '  {'
echo '    "mcpServers": {'
echo '      "nexus": {'
echo '        "command": "'${PYTHON}'",'
echo '        "args": ["-m", "nexus_memory.mcp_server"]'
echo '      }'
echo '    }'
echo '  }'
echo ""

info "┌────────────────────────────────────────────────────────────┐"
info "│ Platform-Specific Configurations                           │"
info "└────────────────────────────────────────────────────────────┘"
echo ""

info "🔷 Hermes Agent — ~/.hermes/config.yaml:"
echo '  mcp_servers:'
echo '    nexus:'
echo "      command: ${PYTHON}"
echo '      args: ["-m", "nexus_memory.mcp_server"]'
echo '      env:'
echo "        PYTHONPATH: ${INSTALL_DIR}"
echo ""

info "🔷 Claude Code — ~/.claude/settings.json:"
echo '  {'
echo '    "mcpServers": {'
echo '      "nexus": {'
echo '        "command": "'${PYTHON}'",'
echo '        "args": ["-m", "nexus_memory.mcp_server"]'
echo '      }'
echo '    }'
echo '  }'
echo ""

info "🔷 Claude Code (project-level) — .mcp.json in your project:"
echo '  {'
echo '    "mcpServers": {'
echo '      "nexus": {'
echo '        "command": "python3",'
echo '        "args": ["-m", "nexus_memory.mcp_server"]'
echo '      }'
echo '    }'
echo '  }'
echo ""

info "🔷 OpenClaw — ~/.openclaw/config.yaml:"
echo '  mcp_servers:'
echo '    nexus:'
echo "      command: ${PYTHON}"
echo '      args: ["-m", "nexus_memory.mcp_server"]'
echo ""

info "🔷 Codex CLI — ~/.codex/config.toml:"
echo '  [mcp_servers.nexus]'
echo "  command = \"${PYTHON}\""
echo '  args = ["-m", "nexus_memory.mcp_server"]'
echo ""

info "🔷 GitHub Copilot (VS Code) — .vscode/mcp.json:"
echo '  {'
echo '    "mcpServers": {'
echo '      "nexus": {'
echo '        "command": "'${PYTHON}'",'
echo '        "args": ["-m", "nexus_memory.mcp_server"]'
echo '      }'
echo '    }'
echo '  }'
echo ""

info "🔷 Pi Coding Agent — ~/.pi/config.json:"
echo '  {'
echo '    "mcpServers": {'
echo '      "nexus": {'
echo '        "command": "'${PYTHON}'",'
echo '        "args": ["-m", "nexus_memory.mcp_server"]'
echo '      }'
echo '    }'
echo '  }'
echo ""

info "🔷 Continue.dev — .mcp.json or ~/.continue/config.json:"
echo '  {'
echo '    "mcpServers": {'
echo '      "nexus": {'
echo '        "command": "'${PYTHON}'",'
echo '        "args": ["-m", "nexus_memory.mcp_server"]'
echo '      }'
echo '    }'
echo '  }'
echo ""

info "🔷 Odysseus (PewDiePie) — Settings → MCP Management → Add Server:"
echo '  Name: nexus'
echo "  Command: ${PYTHON}"
echo '  Arguments: -m nexus_memory.mcp_server'
echo ""

info "🔷 Cursor — Settings → Features → MCP Servers:"
echo '  Name: nexus'
echo "  Command: ${PYTHON}"
echo '  Arguments: -m nexus_memory.mcp_server'
echo ""

info "🔷 Cline — MCP Server Config:"
echo '  {'
echo '    "mcpServers": {'
echo '      "nexus": {'
echo '        "command": "'${PYTHON}'",'
echo '        "args": ["-m", "nexus_memory.mcp_server"]'
echo '      }'
echo '    }'
echo '  }'
echo ""

info "🔷 Other MCP-compatible agents — Standard MCP stdio:"
echo '  Command: python3 -m nexus_memory.mcp_server'
echo '  Protocol: stdio (JSON-RPC 2.0)'
echo ""

info "═══════════════════════════════════════════════"
echo ""

ok "${REPO} setup complete! 🦊"
echo ""
echo "  Next steps:"
echo "  1. Set your VOYAGE_API_KEY (if not done)"
echo "  2. Start the MCP server: nexus-memory"
echo "  3. Connect your agent (see AGENTS.md)"
echo "  4. (Optional) Web UI: pip install nexus-memory[webui] && nexus-memory webui"
echo "  5. Run tests: cd ${INSTALL_DIR} && python3 -m pytest tests/ -q"
