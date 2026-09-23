#!/usr/bin/env bash
#
# install_serve.sh — Install the Nexus Memory Serve daemon as an OS service.
#
# "Standalone wird Standard" (v0.21.0): every fresh install gets the serve
# daemon (Streamable HTTP, `nexus-memory serve`) installed as an OS service —
# launchd on macOS, systemd user units on Linux, a scheduled task on Windows.
# The script is idempotent: re-running it rewrites the service file and
# restarts the service (upgrade/repair — picks up new code + config); it
# never duplicates and never fails on an existing service.
#
# Service identity (STABLE, never change the label/unit name in a patch
# release — installed machines match on it):
#   macOS:   label  ai.nexus.serve        → ~/Library/LaunchAgents/ai.nexus.serve.plist
#   Linux:   unit   nexus-serve.service   → ~/.config/systemd/user/nexus-serve.service
#   Windows: task   NexusServe            → schtasks /tn NexusServe
#
# The generated service file matches Nebo's hand-rolled reference plist
# (ai.nexus.serve): RunAtLoad/KeepAlive, ThrottleInterval 30, logs under
# <repo>/logs/, NEXUS_SERVE_PORT default 9122.
#
# Usage:
#   ./scripts/install_serve.sh [--uninstall] [--status]
#
# Env overrides:
#   NEXUS_SERVE_PORT      HTTP port of `nexus-memory serve` (default 9122)
#   NEXUS_PYTHON          python interpreter to use (default: venv next to
#                         the nexus-memory entrypoint, else python3)
#   NEXUS_SERVE_BIN       explicit nexus-memory entrypoint path
#
set -euo pipefail

# ── Config ───────────────────────────────────────────────────────────
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="ai.nexus.serve"
PLIST_PATH="${HOME}/Library/LaunchAgents/${LABEL}.plist"
UNIT_NAME="nexus-serve.service"
UNIT_PATH="${HOME}/.config/systemd/user/${UNIT_NAME}"
TASK_NAME="NexusServe"
DEFAULT_SERVE_PORT="9122"
SERVE_PORT="${NEXUS_SERVE_PORT:-$DEFAULT_SERVE_PORT}"
LOG_DIR="${REPO_DIR}/logs"
LOG_OUT="${LOG_DIR}/serve.log"
LOG_ERR="${LOG_DIR}/serve.error.log"
# NEXUS_SERVE_SKIP_LAUNCHD=1: write the service file only, skip every
# launchctl/systemd/schtasks call. For tests and cross-HOME probes —
# launchctl domains are per-uid, not per-HOME, so a fake-HOME test run
# would otherwise stop/reload the REAL service.
SKIP_SERVICE_CONTROL="${NEXUS_SERVE_SKIP_LAUNCHD:-0}"
# NEXUS_SERVE_FORCE_OS=darwin|linux|windows: pretend to run on that OS (tests +
# cross-OS dry checks). Empty = real uname. Service-control calls are only made
# when it matches the real OS, so a forced OS never touches the host's real
# service manager.
FORCE_OS="${NEXUS_SERVE_FORCE_OS:-}"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()   { echo -e " ${GREEN}✅${NC} $1"; }
warn() { echo -e " ${YELLOW}⚠️${NC} $1"; }
fail() { echo -e " ${RED}❌${NC} $1"; exit 1; }
info() { echo -e " ${CYAN}ℹ️${NC} $1"; }

# ── Resolve the entrypoint ───────────────────────────────────────────
# Prefer an explicit override, then the nexus-memory console script living in
# the same venv as the interpreter, then the bare command on PATH.
resolve_entrypoint() {
    if [ -n "${NEXUS_SERVE_BIN:-}" ]; then
        echo "$NEXUS_SERVE_BIN"
        return 0
    fi
    local py bin
    py="$(resolve_python)"
    bin="$(dirname "$py")/nexus-memory"
    if [ -x "$bin" ]; then
        echo "$bin"
        return 0
    fi
    if command -v nexus-memory >/dev/null 2>&1; then
        # absolute path — VENV_DIR/PATH in the service file are derived from it
        echo "$(command -v nexus-memory)"
        return 0
    fi
    return 1
}

resolve_python() {
    if [ -n "${NEXUS_PYTHON:-}" ]; then
        echo "$NEXUS_PYTHON"
        return 0
    fi
    # The hermes venv layout: ~/nexus-memory repo + venv console script.
    if [ -x "${HOME}/.hermes/hermes-agent/venv/bin/nexus-memory" ]; then
        echo "${HOME}/.hermes/hermes-agent/venv/bin/python"
        return 0
    fi
    if command -v python3 >/dev/null 2>&1; then
        echo "python3"
    else
        echo "python"
    fi
}

# Effective OS for all `case` branches: forced override wins; a forced OS that
# differs from the real one also disables service control (hermetic mode).
DETECTED_OS="$(uname -s)"
case "${FORCE_OS:-}" in
    darwin)  EFFECTIVE_OS="Darwin" ;;
    linux)   EFFECTIVE_OS="Linux" ;;
    windows) EFFECTIVE_OS="Windows" ;;
    "")      EFFECTIVE_OS="${DETECTED_OS}" ;;
    *)       fail "NEXUS_SERVE_FORCE_OS must be darwin|linux|windows (or unset)" ;;
esac
if [ -n "${FORCE_OS:-}" ] && [ "${EFFECTIVE_OS}" != "${DETECTED_OS}" ]; then
    SKIP_SERVICE_CONTROL="1"
fi

# ── Uninstall ────────────────────────────────────────────────────────
if [ "${1:-}" = "--uninstall" ]; then
    case "${EFFECTIVE_OS}" in
        Darwin)
            if launchctl print "gui/$(id -u)/${LABEL}" >/dev/null 2>&1; then
                launchctl bootout "gui/$(id -u)/${LABEL}" >/dev/null 2>&1 || true
            fi
            rm -f "$PLIST_PATH"
            [ ! -f "$PLIST_PATH" ] && ok "Service removed (${PLIST_PATH} gone)" || warn "Plist still present: $PLIST_PATH"
            ;;
        Linux)
            if command -v systemctl >/dev/null 2>&1; then
                systemctl --user disable --now "$UNIT_NAME" >/dev/null 2>&1 || true
            fi
            rm -f "$UNIT_PATH"
            if command -v systemctl >/dev/null 2>&1; then
                systemctl --user daemon-reload >/dev/null 2>&1 || true
            fi
            [ ! -f "$UNIT_PATH" ] && ok "Service removed (${UNIT_PATH} gone)" || warn "Unit still present: $UNIT_PATH"
            ;;
        Windows)
            schtasks //delete //tn "$TASK_NAME" //f >/dev/null 2>&1 || true
            ok "Task ${TASK_NAME} removed"
            ;;
        *)
            fail "Unsupported OS: $(uname -s)"
            ;;
    esac
    exit 0
fi

# ── Status ───────────────────────────────────────────────────────────
if [ "${1:-}" = "--status" ]; then
    case "${EFFECTIVE_OS}" in
        Darwin)
            if launchctl print "gui/$(id -u)/${LABEL}" >/dev/null 2>&1; then
                ok "launchd service '${LABEL}' is loaded"
            else
                warn "launchd service '${LABEL}' is NOT loaded"
            fi
            ;;
        Linux)
            if command -v systemctl >/dev/null 2>&1; then
                systemctl --user is-enabled "$UNIT_NAME" 2>/dev/null || warn "${UNIT_NAME} is not enabled"
                systemctl --user status --no-pager "$UNIT_NAME" || true
            else
                warn "systemd not available"
            fi
            ;;
        Windows)
            schtasks //query //tn "$TASK_NAME" 2>/dev/null || warn "Task ${TASK_NAME} not found"
            ;;
        *)
            fail "Unsupported OS: $(uname -s)"
            ;;
    esac
    exit 0
fi

# ── Pre-flight ───────────────────────────────────────────────────────
if [ ! -d "$REPO_DIR" ]; then
    fail "Repo not found at ${REPO_DIR}"
fi

ENTRYPOINT="$(resolve_entrypoint)" || fail "nexus-memory entrypoint not found. Install the package first (pip install -e ${REPO_DIR})."
info "Entrypoint: $ENTRYPOINT"

case "${EFFECTIVE_OS}" in
    Darwin)
        # ── macOS: launchd ───────────────────────────────────────────
        mkdir -p "${HOME}/Library/LaunchAgents" "$LOG_DIR"

        if [ "$SKIP_SERVICE_CONTROL" = "1" ]; then
            info "NEXUS_SERVE_SKIP_LAUNCHD=1 — skipping service control (file write only)"
        elif launchctl print "gui/$(id -u)/${LABEL}" >/dev/null 2>&1; then
            launchctl bootout "gui/$(id -u)/${LABEL}" >/dev/null 2>&1 || true
            info "Existing service '${LABEL}' stopped for upgrade"
        fi

        VENV_DIR="$(dirname "$(dirname "$ENTRYPOINT")")"
        PY_BIN="$(resolve_python)"
        PORT="${SERVE_PORT}"
        ENTRY="${ENTRYPOINT}"
        REPO="${REPO_DIR}"
        PYBIN="${PY_BIN}"
        LOGO="${LOG_OUT}"
        LOGE="${LOG_ERR}"
        export PORT ENTRY REPO PYBIN LOGO LOGE PLIST_PATH LABEL VIRTUAL_ENV_DIR="${VENV_DIR}"
        export ENTRY_BIN_DIR="$(dirname "$ENTRY")"
        # NOTE: no `bash -c` here — the child shell would neither see the
        # (shell-local) variables nor the heredoc; plain `cat > file` reads
        # the heredoc directly.
        cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>${ENTRY}</string>
        <string>serve</string>
    </array>

    <key>WorkingDirectory</key>
    <string>${REPO}</string>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>${ENTRY_BIN_DIR}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>VIRTUAL_ENV</key>
        <string>${VIRTUAL_ENV_DIR}</string>
        <key>NEXUS_SERVE_PORT</key>
        <string>${PORT}</string>
    </dict>

    <key>LimitLoadToSessionType</key>
    <array>
        <string>Aqua</string>
        <string>Background</string>
    </array>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <!-- Same crash-loop protection as ai.hermes.gateway -->
    <key>ThrottleInterval</key>
    <integer>30</integer>

    <key>ExitTimeOut</key>
    <integer>25</integer>

    <key>StandardOutPath</key>
    <string>${LOGO}</string>

    <key>StandardErrorPath</key>
    <string>${LOGE}</string>
</dict>
</plist>
PLIST

        chmod 644 "$PLIST_PATH"
        ok "Plist written: ${PLIST_PATH}"

        if [ "$SKIP_SERVICE_CONTROL" = "1" ]; then
            ok "Plist written (service control skipped via NEXUS_SERVE_SKIP_LAUNCHD)"
        else
            launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH" >/dev/null 2>&1 || true
            if launchctl print "gui/$(id -u)/${LABEL}" >/dev/null 2>&1; then
                ok "Service '${LABEL}' loaded (RunAtLoad boots the serve daemon)"
            else
                # bootstrap refuses while the same label is loaded in another
                # session (ssh vs gui); `launchctl load` still accepts it.
                launchctl load "$PLIST_PATH" >/dev/null 2>&1 || true
                if launchctl print "gui/$(id -u)/${LABEL}" >/dev/null 2>&1; then
                    ok "Service '${LABEL}' loaded (legacy load path)"
                else
                    warn "Service written but not loaded — run: launchctl load ${PLIST_PATH}"
                fi
            fi
        fi
        ;;

    Linux)
        # ── Linux: systemd user unit ────────────────────────────────
        if ! command -v systemctl >/dev/null 2>&1; then
            fail "No systemd available — install manually (see README Standalone Serve section)"
        fi
        mkdir -p "$(dirname "$UNIT_PATH")" "$LOG_DIR"

        VENV_DIR="$(dirname "$(dirname "$ENTRYPOINT")")"
        PY_BIN="$(resolve_python)"
        UNIT="${UNIT_NAME}"
        PORT="${SERVE_PORT}"
        ENTRY="${ENTRYPOINT}"
        REPO="${REPO_DIR}"
        PYBIN="${PY_BIN}"
        LOGO="${LOG_OUT}"
        LOGE="${LOG_ERR}"
        export PORT ENTRY REPO PYBIN LOGO LOGE UNIT_PATH VIRTUAL_ENV_DIR="${VENV_DIR}"
        export ENTRY_BIN_DIR="$(dirname "$ENTRY")"
        cat > "$UNIT_PATH" <<UNIT
[Unit]
Description=Nexus Memory Serve (MCP over Streamable HTTP)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=${ENTRY} serve
WorkingDirectory=${REPO}
Restart=always
RestartSec=30
Environment=PATH=${ENTRY_BIN_DIR}:/usr/local/bin:/usr/bin:/bin
Environment=VIRTUAL_ENV=${VIRTUAL_ENV_DIR}
Environment=NEXUS_SERVE_PORT=${PORT}
StandardOutput=append:${LOGO}
StandardError=append:${LOGE}

[Install]
WantedBy=default.target
UNIT

        chmod 644 "$UNIT_PATH"
        ok "Unit written: ${UNIT_PATH}"

        systemctl --user daemon-reload
        systemctl --user enable --now "$UNIT_NAME"
        if systemctl --user is-active --quiet "$UNIT_NAME"; then
            ok "Service '${UNIT_NAME}' active"
        else
            warn "Service written+enabled but not active yet — check: journalctl --user -u ${UNIT_NAME}"
        fi
        ;;

    Windows)
        # ── Windows: Scheduled Task (best-effort, requires Git-Bash) ─
        PYTHON_WIN="$(resolve_python)"
        info "Installing Windows scheduled task '${TASK_NAME}' (logon trigger)"
        schtasks //create //tn "$TASK_NAME" //tr "\"$PYTHON_WIN\" -m nexus_memory.mcp_server serve" //sc onlogon //rl limited //f \
            || fail "Could not create scheduled task"
        ok "Scheduled task '${TASK_NAME}' created"
        ;;

    *)
        fail "Unsupported OS: $(uname -s)"
        ;;
esac

# ── Post-install verification ────────────────────────────────────────
if [ "$SKIP_SERVICE_CONTROL" = "1" ]; then
    info "Post-install healthz check skipped (NEXUS_SERVE_SKIP_LAUNCHD=1)"
else
PORT_TO_TEST="$SERVE_PORT"
healthz_up=""
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    if command -v curl >/dev/null 2>&1; then
        code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT_TO_TEST}/healthz" 2>/dev/null || true)"
    else
        code="$(python3 -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:${PORT_TO_TEST}/healthz', timeout=2).status)" 2>/dev/null || echo 0)"
    fi
    if [ "$code" = "200" ]; then healthz_up="yes"; break; fi
    sleep 1
done
if [ "$healthz_up" = "yes" ]; then
    ok "Serve daemon healthy on http://127.0.0.1:${PORT_TO_TEST}/healthz"
else
    warn "healthz not answering yet (cold start, embedding warmup) — check ${LOG_ERR}"
fi
fi

echo ""
info "Done. Serve daemon installed as OS service:"
case "${EFFECTIVE_OS}" in
    Darwin)      info "  macOS launchd : ${PLIST_PATH}" ;;
    Linux)       info "  systemd user  : ${UNIT_PATH}" ;;
    Windows) info "  Windows task  : ${TASK_NAME}" ;;
esac
info "  Log: ${LOG_OUT} / ${LOG_ERR}"