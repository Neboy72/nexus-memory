"""Run the Node self-test for ``scripts/install_dsh_plugin.mjs``.

The installer is plain Node with no dependencies, so its own test file is the
measurement. This wrapper invokes it the way the repo already runs Node tests
(see ``tests/test_formerly_kein_geistereintrag.py``).
"""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_install_dsh_plugin_self_test():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")

    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "test_install_dsh_plugin.mjs"
    )
    result = subprocess.run(
        [node, str(script)], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert "Fälle grün" in result.stdout
    print(result.stdout, end="")
