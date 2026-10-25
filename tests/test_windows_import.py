"""Windows has no fcntl; the engine modules must import without it.

Regression guard for the import-time failure that stopped the whole suite on
Windows: `agent_detect.py` and `fuel_chain.py` imported `fcntl` at module
level, and the autouse fixture in `conftest.py` imports the registry for every
test, so collection failed before a single test ran.

`sys.modules["fcntl"] = None` makes the import fail the way it does on
Windows, which keeps this test meaningful on a POSIX runner.
"""

from __future__ import annotations

import subprocess
import sys

PROBE = """
import sys
sys.modules["fcntl"] = None
import nexus_memory.agent_detect as agent_detect
import nexus_memory.fuel_chain as fuel_chain
assert agent_detect.fcntl is None
assert fuel_chain.fcntl is None
print("imported")
"""


def test_engine_modules_import_without_fcntl() -> None:
    result = subprocess.run(
        [sys.executable, "-c", PROBE],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("imported")
