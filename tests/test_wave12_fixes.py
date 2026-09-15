"""Tests for OCR review Wave 12 — HIGH findings H2–H12.

Covers the Python/shell/packaging half of the wave (H2 setup.sh version
compare, H3 setup.sh venv/PEP 668, H4 plugin package markers, H5 dead
data-files entry). The TypeScript findings (H6–H12) live in
``plugins/openclaw/test-*.mjs`` and are run with ``node``.

Each finding gets a behaviour test where a real invocation is feasible
(H2: the extracted shell clause is executed with bash) and structural
contracts otherwise.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from setuptools import find_packages

REPO_ROOT = Path(__file__).resolve().parents[1]
SETUP_SH = REPO_ROOT / "setup.sh"
PYPROJECT = REPO_ROOT / "pyproject.toml"
PLUGINS_INIT = REPO_ROOT / "plugins" / "__init__.py"
PLUGINS_MEMORY_INIT = REPO_ROOT / "plugins" / "memory" / "__init__.py"
PLUGIN_PKG = REPO_ROOT / "plugins" / "memory" / "nexus"


# ── H2: setup.sh Python version compare ──────────────────────────────────────


def _version_clause(setup_text: str) -> str:
    """Extract the version-compare condition from the `if ...; then` line."""
    line = next(l for l in setup_text.splitlines() if "-gt 3" in l)
    cond = line.strip()
    assert cond.startswith("if "), cond
    assert cond.endswith("; then"), cond
    return cond[len("if "):-len("; then")]


def _run_clause(cond: str, ver: str) -> str:
    """Run the real bash clause with `ver` preset → 'OK' or 'REJECT'."""
    script = (
        "set -euo pipefail\n"
        f'ver="{ver}"\n'
        f'major="${{ver%.*}}"; minor="${{ver#*.}}"\n'
        f"if {cond}; then echo OK; else echo REJECT; fi\n"
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def _reference_ok(major: int, minor: int) -> bool:
    """major > 3 OR (major == 3 AND minor >= 11)."""
    return major > 3 or (major == 3 and minor >= 11)


class TestH2VersionCompare:
    def test_ver_assignment_survives_empty_grep(self):
        # Under `set -o pipefail`, a grep without a match must not abort the
        # script → the assignment needs `|| true`.
        lines = [
            l for l in SETUP_SH.read_text().splitlines()
            if l.strip().startswith("ver=$(")
        ]
        assert len(lines) == 1, lines
        assert "|| true" in lines[0], lines[0]

    def test_new_clause_shape(self):
        src = SETUP_SH.read_text()
        assert '[ "$major" -gt 3 ]' in src
        assert '[ "$major" -eq 3 ]' in src
        assert '[ "$minor" -ge 11 ]' in src
        assert '-n "$ver"' in src
        # the old independent comparison is gone
        assert '[ "$major" -ge 3 ] && [ "$minor" -ge 11 ]' not in src

    def test_clause_matches_reference_semantics(self):
        cond = _version_clause(SETUP_SH.read_text())
        cases = {
            "4.0": (4, 0),
            "3.11": (3, 11),
            "3.12": (3, 12),
            "3.10": (3, 10),
            "3.9": (3, 9),
            "2.7": (2, 7),
        }
        for ver, (major, minor) in cases.items():
            expected = "OK" if _reference_ok(major, minor) else "REJECT"
            assert _run_clause(cond, ver) == expected, f"ver={ver}"

    def test_empty_version_is_rejected(self):
        cond = _version_clause(SETUP_SH.read_text())
        assert _run_clause(cond, "") == "REJECT"

    def test_syntax_ok(self):
        assert subprocess.run(["bash", "-n", str(SETUP_SH)]).returncode == 0


# ── H3: setup.sh venv / PEP 668 ──────────────────────────────────────────────


class TestH3Venv:
    def test_venv_dir_is_defined(self):
        assert 'VENV_DIR=' in SETUP_SH.read_text()

    def test_venv_is_created(self):
        src = SETUP_SH.read_text()
        assert "-m venv" in src
        assert '"$VENV_DIR"' in src

    def test_interpreter_is_rebound_to_venv(self):
        src = SETUP_SH.read_text()
        assert 'PYTHON="$VENV_DIR/bin/python"' in src

    def test_pip_install_does_not_suppress_stderr(self):
        bad = [
            l for l in SETUP_SH.read_text().splitlines()
            if "pip install -e ." in l and "2>/dev/null" in l
        ]
        assert bad == [], bad

    def test_pip_self_upgrade_is_visible(self):
        lines = [
            l for l in SETUP_SH.read_text().splitlines()
            if "pip install --upgrade pip" in l
        ]
        assert len(lines) == 1, lines
        assert "2>/dev/null" not in lines[0]
        assert 'info "pip self-upgrade skipped"' in lines[0]

    def test_syntax_ok(self):
        assert subprocess.run(["bash", "-n", str(SETUP_SH)]).returncode == 0


# ── H4: plugins* package markers (wheel content) ─────────────────────────────


class TestH4PluginPackages:
    def test_markers_exist(self):
        assert PLUGINS_INIT.is_file()
        assert PLUGINS_MEMORY_INIT.is_file()

    def test_find_packages_includes_plugin_package(self):
        packages = find_packages(where=str(REPO_ROOT), exclude=[], include=["plugins*"])
        assert "plugins.memory.nexus" in packages, packages

    def test_package_data_files_exist(self):
        # The package-data key (pyproject) references these two files.
        assert (PLUGIN_PKG / "plugin.yaml").is_file()
        assert (PLUGIN_PKG / "README.md").is_file()


# ── H5: pyproject dead data-files entry removed ──────────────────────────────


class TestH5NoDeadDataFiles:
    def test_data_files_table_gone(self):
        src = PYPROJECT.read_text()
        assert "[tool.setuptools.data-files]" not in src
        assert "share/nexus-memory/handbook" not in src

    def test_comment_points_at_dashboard_handbook(self):
        src = PYPROJECT.read_text()
        assert "dashboard/handbook" in src
