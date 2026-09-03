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


def interpreter_has_nexus(python: str) -> bool:
    try:
        import subprocess
        r = subprocess.run(
            [python, "-c", "import nexus_memory"],
            capture_output=True, timeout=10,
            env={**os.environ, "PYTHONPATH": os.environ.get("PYTHONPATH", "")},
        )
        return r.returncode == 0
    except Exception:
        return False


def main():
    candidates = []

    override = os.getenv("NEXUS_PYTHON")
    if override:
        candidates.append(override)
    candidates += CANDIDATE_VENVS
    candidates.append(sys.executable)

    for py in candidates:
        if not py or not (shutil.which(py) or os.path.isfile(py)):
            continue
        if interpreter_has_nexus(py):
            os.execv(py, [py, "-m", "nexus_memory.mcp_server"])

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