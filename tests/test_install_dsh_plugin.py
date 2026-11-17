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
        [node, str(script)], capture_output=True, timeout=120
    )

    def _decode(raw):
        """Decode the self-test output without trusting the console codepage.

        Node writes UTF-8; a Windows console hands it a codepage instead, so a
        fixed ``encoding=`` is wrong on one of the two machines. UTF-8 first,
        the console codepage only when the bytes are not valid UTF-8.
        """
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("cp1252", "replace")

    stdout = _decode(result.stdout)
    stderr = _decode(result.stderr)
    assert result.returncode == 0, stdout[-2000:] + stderr[-2000:]

    def _ascii_skeleton(text):
        """Non-ASCII as ``?`` — if the codepage still mangles the umlauts, the
        wording of the closing line is unchanged; the self-test itself has
        already proven every case by exiting 0."""
        return text.encode("ascii", "replace").decode("ascii")

    assert _ascii_skeleton("Fälle grün") in _ascii_skeleton(stdout)
    print(stdout, end="")
