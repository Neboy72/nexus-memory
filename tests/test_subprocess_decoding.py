"""Child output that the parent locale cannot decode must never become ``None``.

``subprocess.run(..., capture_output=True, text=True)`` decodes the child's
output with the parent's preferred encoding. When the child writes bytes that
encoding cannot represent — a Windows console program writing the OEM code
page while the parent runs in UTF-8 mode, or ``git``/``bash`` writing UTF-8
while the parent runs on a legacy code page — the decode raises inside the
reader thread. CPython then leaves ``stdout``/``stderr`` as ``None`` and does
not raise in the caller, so the next ``.strip()`` on that value raises
``AttributeError`` far away from the cause.

Each test below drives a real function with a child that writes bytes no
single encoding can be assumed to decode (``b"\\xff\\xfe"``), so the guard
fails on every platform before the fix and passes after it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

# 0xFF 0xFE is not valid UTF-8 and is not a valid GBK sequence either.
_PY_WRITES_BAD_BYTES = (
    "import sys;"
    "sys.stdout.buffer.write(b'\\xff\\xfe');"
    "sys.stdout.flush();"
    "sys.stderr.buffer.write(b'\\xff\\xfe');"
    "sys.stderr.flush()"
)


def _bash_script(path: Path, *, exit_code: int) -> Path:
    """A script that prints a readable marker *and* bytes no single codec decodes.

    The marker is what a user needs to see in the failure report: it stands for
    the child's own diagnostics. The undecodable bytes are what breaks the
    decode today.
    """
    path.write_text(
        "#!/usr/bin/env bash\n"
        "printf 'install-marker\\n'\n"
        "printf '\\xff\\xfe'\n"
        "printf 'install-marker\\n' >&2\n"
        "printf '\\xff\\xfe' >&2\n"
        f"exit {exit_code}\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def _require_bash() -> None:
    if subprocess.run(["bash", "-c", "true"], capture_output=True).returncode != 0:
        pytest.skip("bash is not available on this machine")


def test_run_cmd_returns_text_for_undecodable_output():
    """wizard._run_cmd must hand callers strings, never None."""
    from nexus_memory import wizard

    rc, out, err = wizard._run_cmd([sys.executable, "-c", _PY_WRITES_BAD_BYTES])

    assert rc == 0
    assert isinstance(out, str), "undecodable stdout came back as None"
    assert isinstance(err, str), "undecodable stderr came back as None"


def test_plugin_install_reports_script_failure_instead_of_losing_it(tmp_path, monkeypatch):
    """A failing install script must be reported as ``failed`` with its own text."""
    from nexus_memory import setup as setup_mod

    _require_bash()
    _bash_script(tmp_path / "install_plugin.sh", exit_code=1)
    monkeypatch.setattr(setup_mod, "INSTALL_SCRIPTS", {"demo": "install_plugin.sh"})
    monkeypatch.setattr(setup_mod, "_get_repo_root", lambda: tmp_path)

    result = setup_mod._install_plugin("demo")

    assert result["status"] == "failed", result
    assert "NoneType" not in result["message"], result
    assert isinstance(result["message"], str)


def test_ensure_serve_daemon_returns_failure_instead_of_raising(tmp_path, monkeypatch):
    """The install path promises ``{"status": "failed"}``, never an exception."""
    from nexus_memory import serve_daemon

    _require_bash()
    script = _bash_script(tmp_path / "install_serve.sh", exit_code=1)
    monkeypatch.setattr(serve_daemon, "INSTALL_SERVE_SCRIPT", script)
    monkeypatch.setattr(serve_daemon, "_healthz_ok", lambda *a, **k: False)
    monkeypatch.setattr(serve_daemon, "_launched_by_launchd", lambda: False)

    result = serve_daemon.ensure_serve_daemon()

    assert result["status"] == "failed", result
    error = result.get("error")
    assert isinstance(error, str), result
    # The report must be about the script, not about the decoder: on Windows the
    # old code crashed here, and on Linux it returned the codec error text.
    assert "install_serve.sh failed (rc=1)" in error, result
    assert "install-marker" in error, result
