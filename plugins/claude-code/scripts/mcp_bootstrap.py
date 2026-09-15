#!/usr/bin/env python3
"""Bootstrap for the Nexus Memory MCP server inside the Claude Code plugin.

Finds a Python interpreter that can import nexus_memory:
1. Explicit override via NEXUS_PYTHON env var
2. Known venv locations (Hermes default venv)
3. Current interpreter (nexus_memory pip-installed)
4. Any Python 3.11+ on PATH that has nexus_memory importable

Then exec the real MCP server with that interpreter.
"""
import os
import sys
import shutil
import subprocess

CANDIDATE_VENVS = [
    os.path.expanduser("~/.hermes/hermes-agent/venv/bin/python3"),
    os.path.expanduser("~/nexus-memory-venv/bin/python3"),
    os.path.expanduser("~/.venv/bin/python3"),
    "/opt/homebrew/bin/python3",
    "/usr/local/bin/python3",
]


# nexus_memory requires Python 3.11+ (see pyproject.toml).
MIN_PYTHON = (3, 11)

# Probe timeout per candidate. A plain ``import nexus_memory`` check needs
# well under a second; the old 10s per probe meant up to 70s of startup stall
# across seven candidates (H246).
_PROBE_TIMEOUT = 3


def interpreter_version_ok(python: str) -> bool:
    """True if the interpreter reports Python >= MIN_PYTHON (H245).

    The module docstring promises a 3.11+ interpreter; an import-probe alone
    would also succeed on a 3.10 that happens to have nexus_memory importable.
    """
    try:
        r = subprocess.run(
            [python, "-c",
             "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"],
            capture_output=True, timeout=_PROBE_TIMEOUT,
        )
        return r.returncode == 0
    except Exception:
        return False


def interpreter_has_nexus(python: str) -> bool:
    """True if ``python`` is Python 3.11+ AND can import nexus_memory."""
    if not interpreter_version_ok(python):
        return False
    try:
        r = subprocess.run(
            [python, "-c", "import nexus_memory"],
            capture_output=True, timeout=_PROBE_TIMEOUT,
            env={**os.environ, "PYTHONPATH": os.environ.get("PYTHONPATH", "")},
        )
        return r.returncode == 0
    except Exception:
        return False


def _path_interpreters() -> list[str]:
    """Any Python 3.11+ interpreter names found on PATH (H245)."""
    found = []
    for name in ("python3.11", "python3", "python"):
        path = shutil.which(name)
        if path:
            found.append(path)
    return found


def main():
    candidates = []

    override = os.getenv("NEXUS_PYTHON")
    if override:
        candidates.append(override)
    candidates += CANDIDATE_VENVS
    candidates.append(sys.executable)
    # 4. Any Python 3.11+ on PATH that has nexus_memory importable.
    candidates += _path_interpreters()

    seen: set[str] = set()
    for py in candidates:
        if not py:
            continue
        # Skip candidates whose interpreter file does not exist BEFORE probing
        # (no subprocess against a non-existent path) and dedupe repeated
        # resolved paths — CANDIDATE_VENVS and PATH often overlap (H246).
        resolved = shutil.which(py) or (py if os.path.isfile(py) else None)
        if not resolved:
            continue
        try:
            resolved = os.path.realpath(resolved)
        except OSError:
            pass
        if resolved in seen:
            continue
        seen.add(resolved)

        if interpreter_has_nexus(resolved):
            # execv does not resolve PATH, so a bare name (e.g. "python3")
            # would fail — ``resolved`` is already an absolute path here.
            os.execv(resolved, [resolved, "-m", "nexus_memory.mcp_server"])

    # Nothing found: print a clear, actionable error and exit.
    msg = (
        "Nexus Memory MCP server could not start: no Python interpreter with "
        "the 'nexus_memory' package was found.\n\n"
        "Fix (one command):\n"
        "  pip install -e ~/nexus-memory   # or wherever you cloned it\n\n"
        "Or set NEXUS_PYTHON to an interpreter that has nexus_memory installed:\n"
        "  export NEXUS_PYTHON=/path/to/python3\n"
        "then restart Claude Code / run /reload-plugins."
    )
    print(msg, file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()