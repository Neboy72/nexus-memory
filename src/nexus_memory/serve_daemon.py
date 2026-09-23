#!/usr/bin/env python3
"""Standalone wird Standard (v0.21.0) — Serve-Daemon-Engine.

Baustein 2 — Setup-Wizard-Anbindung: ``step_serve_daemon()`` installs the
serve daemon as an OS service via ``scripts/install_serve.sh`` — with NO user
question (the daemon is always wanted; without it the MCP transport works
stdio-only and other machines can't reach memory). Idempotent: an already
healthy daemon surfaces as ``status: "already_installed"`` and costs one
local GET.

Baustein 3 — do_update-Nachzieh-Anbindung: ``ensure_serve_daemon()`` is
imported by ``mcp_server._do_update`` so an update can pull machines that
predate v0.21.0 (no service installed yet) and re-assert the service after a
package change. Called fail-open there: any failure logs a warning and never
fails the update.

The install itself shells out to ``scripts/install_serve.sh`` (single source
of truth for the launchd/systemd/Windows logic). This module never raises:
every failure comes back as ``{"status": "failed", "error": ...}``.

Own imports: stdlib only — setup.py and mcp_server.py import this module, so
a circular import is ruled out by construction.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# Single source of truth, kept in sync with mcp_server.DEFAULT_SERVE_PORT.
DEFAULT_SERVE_PORT = 9122

# Stable service identity (must match scripts/install_serve.sh):
LAUNCHD_LABEL = "ai.nexus.serve"
SYSTEMD_UNIT = "nexus-serve.service"
WINDOWS_TASK = "NexusServe"

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
INSTALL_SERVE_SCRIPT = _REPO_ROOT / "scripts" / "install_serve.sh"

# How long the post-install healthz poll inside ensure_serve_daemon may take
# (20 attempts x ~1s, sized to cover the launchd ThrottleInterval=30 restart
# gap on top of the script's own 15s poll).
SERVE_INSTALL_TIMEOUT_S = 60


def _serve_port() -> int:
    """HTTP port the service should listen on (NEXUS_SERVE_PORT wins)."""
    raw = os.environ.get("NEXUS_SERVE_PORT")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return DEFAULT_SERVE_PORT


def _healthz_ok(port: int, timeout: float = 2.0) -> bool:
    """GET /healthz and require HTTP 200."""
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/healthz", headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def serve_daemon_status() -> dict:
    """Read-only snapshot for the wizard UI: is a serve daemon service
    installed AND is /healthz answering on the port?"""
    port = _serve_port()
    service_installed = False
    detail = ""
    try:
        if sys.platform == "darwin":
            plist = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
            r = subprocess.run(
                ["launchctl", "print", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"],
                capture_output=True, timeout=10,
            )
            service_installed = r.returncode == 0
            detail = str(plist)
        elif sys.platform.startswith("linux"):
            r = subprocess.run(
                ["systemctl", "--user", "is-enabled", SYSTEMD_UNIT],
                capture_output=True, text=True, timeout=10,
            )
            service_installed = r.returncode == 0
            detail = SYSTEMD_UNIT
        elif os.name == "nt":
            r = subprocess.run(
                ["schtasks", "/query", "/tn", WINDOWS_TASK],
                capture_output=True, timeout=10,
            )
            service_installed = r.returncode == 0
            detail = WINDOWS_TASK
    except Exception as exc:
        detail = f"detection error: {exc}"

    return {
        "port": port,
        "service_installed": service_installed,
        "healthz_ok": _healthz_ok(port),
        "detail": detail,
    }


def ensure_serve_daemon() -> dict:
    """Make sure the serve daemon runs as an OS service — no user question.

    Idempotent: when /healthz already answers, this is a cheap no-op
    returning ``already_installed``. Otherwise it runs
    ``scripts/install_serve.sh`` (launchd / systemd / schtasks per OS), then
    verifies /healthz. All failures are returned as
    ``{"status": "failed", "error": ...}`` — the do_update caller is
    fail-open and must never see a raised exception from here.
    """
    port = _serve_port()
    if _healthz_ok(port):
        return {
            "status": "already_installed",
            "message": f"Serve daemon already healthy on port {port}",
            "port": port,
        }

    if not INSTALL_SERVE_SCRIPT or not Path(INSTALL_SERVE_SCRIPT).exists():
        return {
            "status": "failed",
            "error": f"install script not found: {INSTALL_SERVE_SCRIPT}",
            "port": port,
        }

    try:
        proc = subprocess.run(
            ["bash", str(INSTALL_SERVE_SCRIPT)],
            capture_output=True, text=True,
            timeout=SERVE_INSTALL_TIMEOUT_S,  # must exceed script poll + throttle gap
            cwd=str(_REPO_ROOT),
            env={**os.environ, "NEXUS_SERVE_PORT": str(port)},
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "failed",
            "error": f"install_serve.sh timed out after {SERVE_INSTALL_TIMEOUT_S}s",
            "port": port,
        }
    except Exception as exc:
        return {"status": "failed", "error": str(exc), "port": port}

    # Idempotent upgrade path: launchd bootstrap refuses while the same label
    # is loaded in another session → the script exits non-zero but DID
    # rewrite + reload the service. Verify by healthz, not by exit code.
    if proc.returncode != 0 and not _healthz_ok(port, timeout=2.0):
        return {
            "status": "failed",
            "error": (
                f"install_serve.sh failed (rc={proc.returncode}): "
                f"{(proc.stderr or proc.stdout).strip()[-400:]}"
            ),
            "port": port,
            "stdout_tail": (proc.stdout or "")[-400:],
            "stderr_tail": (proc.stderr or "")[-400:],
        }

    # Post-install verification (the script already polls up to ~15s; this
    # second poll covers slow warmup and the launchd ThrottleInterval=30
    # restart gap after a bootout/bootstrap cycle — 20s here puts the total
    # poll window past the 30s throttle worst case).
    healthz_ok = False
    for _ in range(20):
        if _healthz_ok(port, timeout=2.0):
            healthz_ok = True
            break
        time.sleep(1)

    if healthz_ok:
        stdout_lines = (proc.stdout or "").strip().splitlines()
        # strip ANSI color codes — the detail line goes into JSON results
        _ansi = re.compile(r"\x1b\[[0-9;]*m")
        detail = _ansi.sub("", stdout_lines[-1]).strip() if stdout_lines else None
        return {
            "status": "installed",
            "message": f"Serve daemon installed as OS service, /healthz on port {port}",
            "port": port,
            "detail": detail,
        }
    return {
        "status": "failed",
        "error": f"Service installed but /healthz not answering on port {port} (check logs/serve.error.log)",
        "port": port,
        "stdout_tail": (proc.stdout or "")[-400:],
        "stderr_tail": (proc.stderr or "")[-400:],
    }


def step_serve_daemon() -> dict:
    """Wizard step 5b (called automatically from setup.step_complete — no
    user question). The daemon is optional at RUNTIME (stdio MCP still
    works), so a failed install degrades to a note instead of breaking the
    setup flow."""
    result = ensure_serve_daemon()
    if result.get("status") == "failed":
        result.setdefault(
            "message",
            "Serve daemon not installed; stdio MCP keeps working without it.",
        )
    return result