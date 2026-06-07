#!/usr/bin/env bash
set -euo pipefail

# ──────────────────────────────────────────────────────────────────────
# Hermes Nexus Memory — Setup & Update Script
# ──────────────────────────────────────────────────────────────────────
# One script for fresh install AND upgrade.
# Detects existing installation and handles both paths.
# ──────────────────────────────────────────────────────────────────────

REPO="hermes-nexus-memory"
REPO_URL="https://github.com/Neboy72/${REPO}.git"
HERMES_HOME="${HERMES_HOME:-${HOME}/.hermes}"
INSTALL_DIR="${HERMES_HOME}/${REPO}"
VENV_DIR="${INSTALL_DIR}/.venv"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

info()  { echo -e "${BLUE}ℹ${NC}  $1"; }
ok()    { echo -e "${GREEN}✓${NC}  $1"; }
warn()  { echo -e "${YELLOW}⚠${NC}  $1"; }
err()   { echo -e "${RED}✗${NC}  $1"; }

# ──────────────────────────────────────────────────────────────────────
# STEP 0: Detect existing installation
# ──────────────────────────────────────────────────────────────────────
detect_state() {
    if [ -d "${INSTALL_DIR}" ]; then
        if [ -f "${INSTALL_DIR}/pyproject.toml" ]; then
            echo "upgrade"
        else
            echo "upgrade-broken"
        fi
    else
        echo "fresh"
    fi
}

STATE=$(detect_state)

echo ""
echo -e "${BLUE}══════════════════════════════════════════════${NC}"
echo -e "${BLUE}  Hermes Nexus Memory — Setup${NC}"
echo -e "${BLUE}══════════════════════════════════════════════${NC}"
echo ""

if [ "${STATE}" = "fresh" ]; then
    info "Fresh install detected — setting up from scratch"
elif [ "${STATE}" = "upgrade" ]; then
    info "Existing installation found at ${INSTALL_DIR}"
    warn "Running UPGRADE — your Qdrant collection and memories stay intact"
    echo ""
elif [ "${STATE}" = "upgrade-broken" ]; then
    warn "Incomplete installation found at ${INSTALL_DIR}"
    warn "Will re-clone and reinstall"
fi

# ──────────────────────────────────────────────────────────────────────
# STEP 1: Check prerequisites
# ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}── Step 1: Prerequisites ──────────────────────${NC}"

# Python 3.11+
PYTHON=""
for cmd in python3.12 python3.11 python3; do
    if command -v "$cmd" &>/dev/null; then
        VER=$("$cmd" --version 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
        MAJOR=$(echo "$VER" | cut -d. -f1)
        MINOR=$(echo "$VER" | cut -d. -f2)
        if [ "$MAJOR" -ge 3 ] && [ "$MINOR" -ge 11 ]; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    err "Python 3.11+ required but not found."
    err "Install it: brew install python@3.12  (macOS)"
    err "             apt install python3.12  (Linux)"
    exit 1
fi
ok "Python: $($PYTHON --version 2>&1)"

# Git
if ! command -v git &>/dev/null; then
    err "git is required but not installed."
    exit 1
fi
ok "Git: $(git --version 2>&1)"

# pip
if ! $PYTHON -m pip --version &>/dev/null; then
    err "pip is required but not available for $PYTHON"
    exit 1
fi
ok "pip: $($PYTHON -m pip --version 2>&1 | head -1)"

# Qdrant
QDRANT_OK=false
if curl -sf http://127.0.0.1:6333/healthz &>/dev/null; then
    QDRANT_OK=true
    ok "Qdrant: running at http://127.0.0.1:6333"
fi

if [ "${QDRANT_OK}" = false ]; then
    warn "Qdrant is NOT running."
    echo ""
    echo "  Qdrant is required for Nexus Memory. Start it with:"
    echo "    macOS: brew install qdrant && brew services start qdrant"
    echo "    Linux: docker run -d -p 6333:6333 qdrant/qdrant"
    echo ""
    read -rp "  Start Qdrant now? (Y/n): " START_QDRANT
    START_QDRANT="${START_QDRANT:-Y}"
    if [[ "$START_QDRANT" =~ ^[Yy] ]]; then
        if [[ "$(uname)" == "Darwin" ]]; then
            if ! command -v brew &>/dev/null; then
                err "Homebrew not found. Cannot install Qdrant automatically."
                err "Please start Qdrant manually, then re-run this script."
                exit 1
            fi
            if ! brew list qdrant &>/dev/null 2>&1; then
                info "Installing Qdrant via Homebrew..."
                brew install qdrant
            fi
            info "Starting Qdrant service..."
            brew services start qdrant 2>/dev/null || true
            sleep 3
            if curl -sf http://127.0.0.1:6333/healthz &>/dev/null; then
                ok "Qdrant started successfully"
                QDRANT_OK=true
            else
                err "Qdrant failed to start. Run 'brew services start qdrant' manually."
                exit 1
            fi
        else
            # Linux — try docker
            if command -v docker &>/dev/null; then
                info "Starting Qdrant via Docker..."
                docker run -d --name qdrant -p 6333:6333 qdrant/qdrant 2>/dev/null || true
                sleep 2
                if curl -sf http://127.0.0.1:6333/healthz &>/dev/null; then
                    ok "Qdrant started via Docker"
                    QDRANT_OK=true
                else
                    err "Docker Qdrant failed to start."
                    exit 1
                fi
            else
                err "No Docker found. Please start Qdrant manually."
                exit 1
            fi
        fi
    else
        warn "Skipping Qdrant setup. Script will proceed but Nexus Memory won't work until Qdrant is running."
    fi
fi

# ──────────────────────────────────────────────────────────────────────
# STEP 2: Clone / Pull repository
# ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}── Step 2: Repository ─────────────────────────${NC}"

mkdir -p "${HERMES_HOME}"

if [ "${STATE}" = "fresh" ] || [ "${STATE}" = "upgrade-broken" ]; then
    if [ "${STATE}" = "upgrade-broken" ]; then
        info "Removing incomplete installation..."
        rm -rf "${INSTALL_DIR}"
    fi
    info "Cloning ${REPO}..."
    git clone "${REPO_URL}" "${INSTALL_DIR}"
    ok "Repository cloned to ${INSTALL_DIR}"
else
    info "Pulling latest version..."
    cd "${INSTALL_DIR}"
    git fetch origin
    LOCAL=$(git rev-parse HEAD)
    REMOTE=$(git rev-parse origin/main 2>/dev/null || echo "")
    if [ -n "$REMOTE" ] && [ "$LOCAL" != "$REMOTE" ]; then
        git pull origin main
        ok "Updated to latest commit ($(git rev-parse --short HEAD))"
    else
        ok "Already up to date ($(git rev-parse --short HEAD))"
    fi
    cd "$OLDPWD"
fi

# ──────────────────────────────────────────────────────────────────────
# STEP 3: Python virtual environment + install package
# ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}── Step 3: Python Package ──────────────────────${NC}"

if [ ! -d "${VENV_DIR}" ]; then
    info "Creating virtual environment..."
    $PYTHON -m venv "${VENV_DIR}"
    ok "Virtual environment created"
fi

source "${VENV_DIR}/bin/activate" || { err "Failed to activate venv"; exit 1; }
ok "Virtual environment activated ($($PYTHON --version 2>&1))"

info "Installing hermes-nexus-memory with ALL extras..."
pip install -e "${INSTALL_DIR}[all]" --quiet
ok "Package installed: hermes-nexus-memory $(pip show hermes-nexus-memory 2>/dev/null | grep Version | cut -d' ' -f2)"

# ──────────────────────────────────────────────────────────────────────
# STEP 4: Embedding Provider Detection
# ──────────────────────────────────────────────────────────────────────
# Order: sentence-transformers (free/local) → ollama → voyage → openai → jina
# ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}── Step 4: Embedding Provider ──────────────────${NC}"

select_embed_provider() {
    # Try sentence-transformers (always available after [all] install)
    ST_OK=false
    if "$PYTHON" -c "from sentence_transformers import SentenceTransformer; m = SentenceTransformer('all-MiniLM-L6-v2')" &>/dev/null; then
        ST_OK=true
    fi

    # Try Ollama
    OLLAMA_OK=false
    if curl -sf http://127.0.0.1:11434/api/tags &>/dev/null; then
        if curl -sf http://127.0.0.1:11434/api/tags 2>/dev/null | "$PYTHON" -c "import json,sys; d=json.load(sys.stdin); exit(0 if any('nomic-embed' in m['name'] for m in d.get('models',[])) else 1)" 2>/dev/null; then
            OLLAMA_OK=true
        fi
    fi

    # Try Voyage
    VOYAGE_OK=false
    if [ -n "${VOYAGE_API_KEY:-}" ]; then
        VOYAGE_OK=true
    elif grep -q "VOYAGE_API_KEY" "${HERMES_HOME}/.env" 2>/dev/null; then
        VOYAGE_OK=true
    fi

    # Try OpenAI
    OPENAI_OK=false
    if [ -n "${OPENAI_API_KEY:-}" ]; then
        OPENAI_OK=true
    elif grep -q "OPENAI_API_KEY" "${HERMES_HOME}/.env" 2>/dev/null; then
        OPENAI_OK=true
    fi

    # Jina
    JINA_OK=false
    if [ -n "${JINA_API_KEY:-}" ]; then
        JINA_OK=true
    elif grep -q "JINA_API_KEY" "${HERMES_HOME}/.env" 2>/dev/null; then
        JINA_OK=true
    fi

    echo ""
    info "Detected embedding providers:"
    $ST_OK     && ok "  sentence-transformers (offline, free, 384d)"     || warn "  sentence-transformers (not available)"
    $OLLAMA_OK && ok "  Ollama nomic-embed-text (local, 768d)"          || warn "  Ollama nomic-embed-text (not found)"
    $VOYAGE_OK && ok "  Voyage (API Key found, 1024d, ⭐ recommended)"  || warn "  Voyage (no API Key)"
    $OPENAI_OK && ok "  OpenAI (API Key found, 1536d)"                  || warn "  OpenAI (no API Key)"
    $JINA_OK   && ok "  Jina (API Key found, 1024d, affordable)"        || warn "  Jina (no API Key)"
    echo ""

    # Auto-select best available
    PROVIDER="sentence-transformers"  # fallback
    if $VOYAGE_OK; then
        PROVIDER="voyage"
    elif $OPENAI_OK; then
        PROVIDER="openai"
    elif $OLLAMA_OK; then
        PROVIDER="ollama"
    elif $JINA_OK; then
        PROVIDER="jina"
    fi

    echo -e "  Recommended: ${GREEN}${PROVIDER}${NC}"
    read -rp "  Use ${PROVIDER}? (Enter=yes, or type: voyage/openai/ollama/jina/sentence-transformers): " PROVIDER_INPUT
    PROVIDER="${PROVIDER_INPUT:-$PROVIDER}"

    # Validate choice
    case "$PROVIDER" in
        voyage)
            if ! $VOYAGE_OK; then
                warn "Voyage selected but no API key found."
                read -rp "  Enter your Voyage API Key (or press Enter to cancel → use sentence-transformers): " VOYAGE_KEY
                if [ -n "$VOYAGE_KEY" ]; then
                    echo "VOYAGE_API_KEY=$VOYAGE_KEY" >> "${HERMES_HOME}/.env"
                    ok "Voyage API key saved to ${HERMES_HOME}/.env"
                else
                    warn "Defaulting to sentence-transformers"
                    PROVIDER="sentence-transformers"
                fi
            fi
            ;;
        openai)
            if ! $OPENAI_OK; then
                warn "OpenAI selected but no API key found."
                read -rp "  Enter your OpenAI API Key (or press Enter to cancel → use sentence-transformers): " OPENAI_KEY
                if [ -n "$OPENAI_KEY" ]; then
                    echo "OPENAI_API_KEY=$OPENAI_KEY" >> "${HERMES_HOME}/.env"
                    ok "OpenAI API key saved to ${HERMES_HOME}/.env"
                else
                    warn "Defaulting to sentence-transformers"
                    PROVIDER="sentence-transformers"
                fi
            fi
            ;;
        ollama)
            if ! $OLLAMA_OK; then
                warn "Ollama selected but nomic-embed-text not found."
                info "Pulling nomic-embed-text..."
                ollama pull nomic-embed-text 2>/dev/null || {
                    warn "Failed to pull model. Defaulting to sentence-transformers"
                    PROVIDER="sentence-transformers"
                }
            fi
            ;;
        jina)
            if ! $JINA_OK; then
                warn "Jina selected but no API key found."
                read -rp "  Enter your Jina API Key (or press Enter to cancel → use sentence-transformers): " JINA_KEY
                if [ -n "$JINA_KEY" ]; then
                    echo "JINA_API_KEY=$JINA_KEY" >> "${HERMES_HOME}/.env"
                    ok "Jina API key saved to ${HERMES_HOME}/.env"
                else
                    warn "Defaulting to sentence-transformers"
                    PROVIDER="sentence-transformers"
                fi
            fi
            ;;
    esac

    # Final fallback
    if [ "$PROVIDER" = "sentence-transformers" ] && ! $ST_OK; then
        info "Installing sentence-transformers..."
        pip install sentence-transformers --quiet
        ok "sentence-transformers installed"
    fi
}

# Run provider selection
select_embed_provider

# Write the MCP server script
# ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}── Step 5: MCP Server Setup ────────────────────${NC}"

SCRIPTS_DIR="${HERMES_HOME}/scripts"
mkdir -p "${SCRIPTS_DIR}"

# Generiere MCP Server Script
cat > "${SCRIPTS_DIR}/nexus_mcp_server.py" << 'MCPEOF'
#!/usr/bin/env python3
"""Hermes Nexus Memory MCP Server — BM25 + Vector Hybrid Search.

Launched by Hermes Gateway as an MCP subprocess (stdio transport).
Registers tool `nexus_search(query, top_k=5)`.
"""
import json
import sys
import os
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
sys.path.insert(0, str(HERMES_HOME / "hermes-nexus-memory"))

try:
    from nexus.retrieval import HybridRetriever
except ImportError:
    # Fallback: bm25s-only without vector search
    HybridRetriever = None

qdrant_host = os.environ.get("QDRANT_HOST", "127.0.0.1")
qdrant_port = int(os.environ.get("QDRANT_PORT", "6333"))
collection_name = os.environ.get("NEXUS_COLLECTION", "hermes-memory")


def handle_request(req: dict) -> dict:
    """Handle a single MCP request."""
    method = req.get("method", "")
    req_id = req.get("id", 0)

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "0.1.0",
                "capabilities": {
                    "tools": {
                        "nexus_search": {
                            "description": "Search Nexus Memory (BM25 + Vector + RRF hybrid)",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "query": {"type": "string", "description": "Search query"},
                                    "top_k": {"type": "integer", "default": 5, "description": "Number of results"}
                                },
                                "required": ["query"]
                            }
                        }
                    }
                },
                "serverInfo": {"name": "nexus-memory", "version": "2.1.0"}
            }
        }

    if method == "tools/call":
        tool = req.get("params", {}).get("name", "")
        args = req.get("params", {}).get("arguments", {})
        query = args.get("query", "")
        top_k = args.get("top_k", 5)

        if tool == "nexus_search":
            try:
                if HybridRetriever is None:
                    # BM25-only mode
                    from nexus.retrieval import BM25Retriever
                    retriever = BM25Retriever(qdrant_host=qdrant_host, qdrant_port=qdrant_port, collection_name=collection_name)
                    retriever.index_memories()
                    results = retriever.search_bm25(query, top_k=top_k)
                else:
                    retriever = HybridRetriever(qdrant_host=qdrant_host, qdrant_port=qdrant_port, collection_name=collection_name)
                    retriever.index_memories()
                    results = retriever.search_hybrid(query, top_k=top_k)

                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(results, indent=2)}]
                    }
                }
            except Exception as e:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps({"error": str(e)})}]
                    }
                }

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Tool not found: {tool}"}
        }

    if method == "notifications/initialized":
        return None  # No response for notifications

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"}
    }


def main():
    """Read JSON-RPC requests from stdin, write responses to stdout."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            resp = handle_request(req)
            if resp is not None:
                sys.stdout.write(json.dumps(resp) + "\n")
                sys.stdout.flush()
        except json.JSONDecodeError:
            pass
        except Exception as e:
            err_resp = {"jsonrpc": "2.0", "error": {"code": -32603, "message": str(e)}}
            if "id" in locals() or "id" in dir():
                pass
            sys.stdout.write(json.dumps(err_resp) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
MCPEOF
chmod +x "${SCRIPTS_DIR}/nexus_mcp_server.py"
ok "MCP server script created at ${SCRIPTS_DIR}/nexus_mcp_server.py"

# ──────────────────────────────────────────────────────────────────────
# STEP 5: Register in Hermes Config
# ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}── Step 6: Hermes Configuration ────────────────${NC}"

if command -v hermes &>/dev/null; then
    # Check if nexus-memory MCP already configured
    CURRENT_MCP=$(hermes config get mcp_servers.nexus-memory.command 2>/dev/null || echo "")
    if [ -n "$CURRENT_MCP" ]; then
        info "Nexus Memory MCP already configured"
    else
        info "Registering Nexus Memory MCP server..."
        PROVIDER="${PROVIDER:-sentence-transformers}"
        hermes config set mcp_servers.nexus-memory.command "python3 ${SCRIPTS_DIR}/nexus_mcp_server.py"
        hermes config set mcp_servers.nexus-memory.type "stdio"
        hermes config set mcp_servers.nexus-memory.description "Nexus Memory — persistent vector memory with BM25+Vector hybrid search"
        hermes config set memory.provider "nexus"
        hermes config set nexus-memory.embed_provider "${PROVIDER}"
        ok "Nexus Memory MCP registered in Hermes config"
    fi
else
    warn "Hermes CLI not found in PATH."
    warn "Add this to your config.yaml manually:"
    echo ""
    echo "  mcp_servers:"
    echo "    nexus-memory:"
    echo "      command: python3 ${SCRIPTS_DIR}/nexus_mcp_server.py"
    echo "      type: stdio"
    echo "      description: Nexus Memory — persistent vector memory"
    echo ""
    echo "  memory:"
    echo "    provider: nexus"
    echo ""
    echo "  nexus-memory:"
    echo "    embed_provider: ${PROVIDER:-sentence-transformers}"
fi

# ──────────────────────────────────────────────────────────────────────
# STEP 7: Gateway Restart
# ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}── Step 7: Gateway Restart ─────────────────────${NC}"

read -rp "  Restart Hermes Gateway now? (Y/n): " RESTART_GW
RESTART_GW="${RESTART_GW:-Y}"
if [[ "$RESTART_GW" =~ ^[Yy] ]]; then
    if command -v hermes &>/dev/null; then
        info "Restarting Hermes Gateway..."
        hermes gateway restart 2>/dev/null || {
            warn "'hermes gateway restart' not available."
            info "Try: launchctl kickstart gui/$(id -u)/ai.hermes.gateway 2>/dev/null"
            info "Or manually restart your Hermes Gateway process."
        }
        sleep 2
        ok "Gateway restart initiated"
    else
        warn "Hermes CLI not found. Restart your Gateway manually."
    fi
else
    warn "Skipping Gateway restart. Changes will apply after next restart."
fi

# ──────────────────────────────────────────────────────────────────────
# STEP 8: Verify Installation
# ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}── Step 8: Verification ────────────────────────${NC}"

verify_ok=true

# Check Python package
if $PYTHON -c "import nexus; print('ok')" 2>/dev/null; then
    ok "Python package nexus importable"
else
    warn "Python package nexus not importable — check installation"
    verify_ok=false
fi

# Check Qdrant
if curl -sf http://127.0.0.1:6333/healthz &>/dev/null; then
    ok "Qdrant reachable at localhost:6333"
else
    warn "Qdrant not reachable — start it manually"
    verify_ok=false
fi

# Check BM25
if $PYTHON -c "import bm25s; print('ok')" 2>/dev/null; then
    ok "BM25 hybrid search available"
else
    warn "BM25 not installed — hybrid search degraded to vector-only"
    warn "  Run: pip install bm25s"
fi

# Check Hermes config
if command -v hermes &>/dev/null; then
    MCP_OK=$(hermes config get mcp_servers.nexus-memory.command 2>/dev/null || echo "")
    if [ -n "$MCP_OK" ]; then
        ok "Hermes config: nexus-memory MCP registered"
    else
        warn "Hermes config: nexus-memory MCP not registered"
        verify_ok=false
    fi
fi

# ──────────────────────────────────────────────────────────────────────
# DONE
# ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}══════════════════════════════════════════════${NC}"
echo -e "${GREEN}  Hermes Nexus Memory Setup Complete!${NC}"
echo -e "${GREEN}══════════════════════════════════════════════${NC}"
echo ""
echo "  Installation:  ${INSTALL_DIR}"
echo "  Venv:          ${VENV_DIR}"
echo "  Provider:      ${PROVIDER}"
echo "  Qdrant:        http://127.0.0.1:6333"
echo "  Collection:    hermes-memory"
echo ""

if $verify_ok; then
    ok "All checks passed. Nexus Memory is ready."
    echo ""
    echo "  Next steps:"
    echo "  1. Tell your Hermes Agent: \"use nexus memory\""
    echo "  2. Memories are stored automatically"
    echo "  3. Search via: nexus_search(query, top_k=5)"
    echo "  4. Run drift detection: cd ${INSTALL_DIR} && python3 -c \"from nexus.health import DriftDetector; print(DriftDetector().run())\""
    echo "  5. Enable Self-Improvement (SICA): scripts/hermes-cron-setup.sh"
    echo "  6. Enable Session Export: scripts/hermes-cron-setup.sh"
else
    warn "Some checks failed. Review warnings above."
fi

echo ""
