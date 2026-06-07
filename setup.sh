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
        ver=$("$cmd" --version 2>&1 | grep -oP '\d+\.\d+')
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
    uv pip install --system -e . 2>/dev/null || uv pip install -e .
else
    $PYTHON -m pip install -e . --quiet
fi
ok "Nexus Memory installed: v$($PYTHON -c "from nexus import __version__; print(__version__)" 2>/dev/null || echo "?")"

# ── Step 5: API Key Setup ─────────────────────────────────────────────
ENV_FILE="${HOME}/.hermes/.env"
if [ ! -f "$ENV_FILE" ]; then
    mkdir -p "${HOME}/.hermes"
    touch "$ENV_FILE"
fi

if grep -q "VOYAGE_API_KEY" "$ENV_FILE" 2>/dev/null; then
    ok "VOYAGE_API_KEY found in ${ENV_FILE}"
else
    info "Add your Voyage AI API key to ${ENV_FILE}:"
    echo '  echo "VOYAGE_API_KEY=vo-...put your key here..." >> '"${ENV_FILE}"
    echo ""
fi

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

# ── Step 7: Optional — MCP Server Start ───────────────────────────────
echo ""
info "To start the MCP server:"
echo "  cd ${INSTALL_DIR}"
echo "  ${PYTHON} -m src.nexus_memory.mcp_server"
echo ""
info "To connect Hermes Agent, add to ~/.hermes/config.yaml:"
echo '  mcp_servers:'
echo '    nexus:'
echo "      command: ${PYTHON}"
echo '      args: ["-m", "nexus_memory.mcp_server"]'
echo '      env:'
echo "        PYTHONPATH: ${INSTALL_DIR}"
echo ""

ok "${REPO} setup complete! 🦊"
echo ""
echo "  Next steps:"
echo "  1. Set your VOYAGE_API_KEY (if not done)"
echo "  2. Start the MCP server: nexus-memory"
echo "  3. Connect your agent (see AGENTS.md)"
echo "  4. Run tests: cd ${INSTALL_DIR} && python3 -m pytest tests/ -q"
