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
    # Exit 2 (distinct from a hard error 1): "Hermes not installed" is a
    # valid no-op, so callers/CI can tell it apart from a real failure.
    exit 2
fi

# --- Link the plugin ---

# Collision-free backup path: an existing .bak is never overwritten.
# The timestamp alone has 1-second resolution, so two installs in the same
# second (interactive retry, CI matrix, parallel runs) would collide; PID plus
# an existence-checked counter keeps the "never clobbers an earlier backup"
# guarantee.
backup_path() {
    local base="${PLUGIN_DST}.bak"
    local candidate="${base}"
    if [ -e "${candidate}" ]; then
        candidate="${base}.$(date +%Y%m%d%H%M%S).$$"
        local n=1
        while [ -e "${candidate}" ]; do
            candidate="${base}.$(date +%Y%m%d%H%M%S).$$.${n}"
            n=$((n + 1))
        done
    fi
    echo "${candidate}"
}

# Atomically point ${2} at ${1} and verify the result.
# `ln -s` into a temp name plus `mv -T` swaps in a single rename(2): there is
# no window where the target path is missing, and a failed `ln` leaves the old
# link untouched. `-T` (GNU) makes mv replace the destination instead of
# moving INTO a symlinked directory; BSD/macOS mv lacks it, so fall back to an
# unlink+recreate there — still verified below.
atomic_symlink() {
    local target="$1" dst="$2" dir tmp got
    dir="$(dirname "${dst}")"
    tmp="$(mktemp -u "${dir}/.nexus-link.XXXXXX")" || return 1
    if ! ln -s "${target}" "${tmp}"; then
        echo -e "${RED}✗${NC} ln -s ${tmp} failed — old link left untouched." >&2
        return 1
    fi
    if mv -T "${tmp}" "${dst}" 2>/dev/null; then
        : # GNU mv: single atomic rename
    else
        rm -f "${tmp}"
        ln -sfn "${target}" "${dst}" || return 1
    fi
    got="$(readlink "${dst}" 2>/dev/null || true)"
    if [ "${got}" != "${target}" ]; then
        echo -e "${RED}✗${NC} Symlink verification failed: ${dst} → ${got:-<none>}" >&2
        return 1
    fi
    return 0
}

# Move an existing target to a collision-free backup path and confirm the
# move. Never clobbers an earlier backup (timestamp suffix above) and aborts
# instead of continuing when the source is gone or the backup cannot be
# verified — a second run must not lose the first run's backup.
backup_and_remove() {
    local src="$1"
    if [ ! -e "${src}" ]; then
        echo -e "${RED}✗${NC} ${src} vanished before backup — aborting." >&2
        return 1
    fi
    local dst
    dst="$(backup_path)"
    mv "${src}" "${dst}"
    if [ ! -e "${dst}" ]; then
        echo -e "${RED}✗${NC} Backup ${dst} missing after move — aborting." >&2
        return 1
    fi
    printf '%s' "${dst}"
}

if [ -L "${PLUGIN_DST}" ]; then
    current_target="$(readlink "${PLUGIN_DST}")"
    if [ "${current_target}" = "${PLUGIN_SRC}" ]; then
        echo -e "${GREEN}✓${NC} Plugin already linked: ${PLUGIN_DST} → ${PLUGIN_SRC}"
    else
        echo -e "${YELLOW}⚠${NC} Existing symlink points elsewhere (${current_target}). Replacing..."
        atomic_symlink "${PLUGIN_SRC}" "${PLUGIN_DST}"
        echo -e "${GREEN}✓${NC} Plugin linked: ${PLUGIN_DST} → ${PLUGIN_SRC}"
    fi
elif [ -d "${PLUGIN_DST}" ]; then
    echo -e "${YELLOW}⚠${NC} ${PLUGIN_DST} exists as a directory (not a symlink)."
    echo "  Backing up and replacing with symlink."
    BACKUP_DST="$(backup_and_remove "${PLUGIN_DST}")"
    atomic_symlink "${PLUGIN_SRC}" "${PLUGIN_DST}"
    echo -e "${GREEN}✓${NC} Plugin linked (backup at ${BACKUP_DST})"
elif [ -e "${PLUGIN_DST}" ]; then
    # Regular file (or other non-dir) at the target path: back it up rather
    # than clobbering it with ln -s.
    echo -e "${YELLOW}⚠${NC} ${PLUGIN_DST} exists as a regular file (not a symlink)."
    echo "  Backing up and replacing with symlink."
    BACKUP_DST="$(backup_and_remove "${PLUGIN_DST}")"
    atomic_symlink "${PLUGIN_SRC}" "${PLUGIN_DST}"
    echo -e "${GREEN}✓${NC} Plugin linked (backup at ${BACKUP_DST})"
else
    atomic_symlink "${PLUGIN_SRC}" "${PLUGIN_DST}"
    echo -e "${GREEN}✓${NC} Plugin linked: ${PLUGIN_DST} → ${PLUGIN_SRC}"
fi

# --- Set memory.provider ---

if command -v hermes &> /dev/null; then
    # Do not let set -e kill the script on a CLI failure: the symlink above is
    # already valid, so a failed config write only needs a recovery hint.
    if hermes config set memory.provider nexus; then
        echo -e "${GREEN}✓${NC} Hermes config: memory.provider = nexus"
    else
        echo -e "${YELLOW}⚠${NC} 'hermes config set memory.provider nexus' failed."
        echo "  The plugin symlink is still in place and valid."
        echo "  Recovery: run manually once the CLI works: hermes config set memory.provider nexus"
    fi
else
    echo -e "${YELLOW}⚠${NC} 'hermes' CLI not found on PATH. Set manually: hermes config set memory.provider nexus"
fi

echo ""
echo "=== Done! Restart Hermes Gateway to activate Nexus Memory. ==="
echo ""
# Success moment: the user must SEE their dashboard, not hunt for a URL.
# Web-UI is optional; if installed, the address + bookmark hint goes here.
echo "🧠  Want to SEE your memory? Start the dashboard:"
echo ""
echo "    nexus-memory webui   # dashboard on http://127.0.0.1:9121"
echo ""
echo "    → opens at http://127.0.0.1:9121 (browser opens automatically"
echo "      on first start). Bookmark it — one click to your dashboard."
echo ""
