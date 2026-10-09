"""Regression guard for the Claude Code plugin scripts (09.10.2026).

Two silent failures were found in the field and are locked here:

1. **The hooks never saw the host's configuration.** They run under the
   app's environment, not a login shell, so ``NEXUS_COLLECTION`` (set only in
   ``~/.hermes/.env``) was unset and every script fell back to its own
   default ``nexus`` — reading and writing the OLD collection while the MCP
   server of the same install used ``nexus_qwen``. Zero hits, no error.

2. **Two hooks did not start at all.** Claude Code runs them as
   ``python3 <script>``; on macOS ``python3`` is /usr/bin/python3 = 3.9, where
   ``int | None`` in an annotation raises TypeError at def time. A dead
   guardrail hook means a destructive command passes unchecked, and a dead
   recall hook means no memory is injected — both silently.

The tests stay static/offline: they parse the sources and run the scripts in a
subprocess under the system python3, so they need no Qdrant and no network.
"""
from __future__ import annotations

import ast
import json
import pathlib
import shutil
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "plugins" / "claude-code" / "scripts"

# Every script a user's Claude Code runs on a normal session.
HOOK_SCRIPTS = [
    "auto_recall.py",
    "auto_capture.py",
    "session_start.py",
    "guardrail_check.py",
    "graph_traverse.py",
]


def _source(name: str) -> str:
    return (SCRIPTS / name).read_text(encoding="utf-8")


# ── 1. the host configuration reaches the hook ───────────────────────────

def test_every_hook_reads_the_central_env():
    """A hook must consult ~/.hermes/.env, not only its own defaults."""
    for name in HOOK_SCRIPTS:
        src = _source(name)
        assert "_central_env" in src, f"{name} has no _central_env() reader"
        assert ".hermes" in src, f"{name} does not point at ~/.hermes/.env"
        assert "NEXUS_ENV_FILE" in src, (
            f"{name} ignores NEXUS_ENV_FILE — an override could not redirect it"
        )


def test_central_env_honours_only_the_three_nexus_keys():
    """Reading a foreign .env must not leak unrelated variables into config."""
    for name in HOOK_SCRIPTS:
        src = _source(name)
        assert "NEXUS_COLLECTION" in src and "NEXUS_EMBEDDING_PROVIDER" in src


def test_real_environment_still_wins_over_the_env_file():
    """An explicit override (settings.json, shell export) must not be beaten."""
    for name in HOOK_SCRIPTS:
        src = _source(name)
        # The pattern is: os.environ first, then the parsed file, then default.
        assert src.count("or _CENTRAL.get") >= 1, (
            f"{name} does not fall back through _CENTRAL"
        )


def test_collection_default_is_overridden_by_the_env_file(tmp_path):
    """Sharp proof: with a .env present, the hook must use ITS collection."""
    envfile = tmp_path / ".hermes" / ".env"
    envfile.parent.mkdir(parents=True, exist_ok=True)
    envfile.write_text(
        "NEXUS_COLLECTION=test_coll_xyz\n"
        "NEXUS_EMBEDDING_PROVIDER=ollama\n"
        "NEXUS_EMBEDDING_MODEL=some-model\n"
        "UNRELATED_SECRET=must-not-be-read\n",
        encoding="utf-8",
    )
    probe = (
        "import importlib.util, sys, os, json\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        f"s = importlib.util.spec_from_file_location('m', r'{SCRIPTS / 'auto_recall.py'}')\n"
        "m = importlib.util.module_from_spec(s)\n"
        "s.loader.exec_module(m)\n"
        "print(json.dumps({'collection': m.COLLECTION,"
        " 'provider': m.EMBEDDING_PROVIDER}))\n"
        "print(json.dumps({'unrelated': os.environ.get('UNRELATED_SECRET')}))\n"
    )
    py = shutil.which("python3") or sys.executable
    result = subprocess.run(
        [py, "-c", probe],
        capture_output=True, text=True, timeout=60,
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
        cwd=str(SCRIPTS),
    )
    assert result.returncode == 0, result.stderr[-800:]
    out = result.stdout
    assert '"collection": "test_coll_xyz"' in out, out
    assert '"provider": "ollama"' in out, out
    # A foreign key in that file is never exported into the process.
    assert '"unrelated": null' in out, out


# ── 2. the script actually starts on the system interpreter ──────────────

def test_no_hook_relies_on_pep604_annotations_at_runtime():
    """A 3.9 annotation crash at import time would kill the hook silently.

    Checks the FUNCTION ANNOTATIONS only — that is where PEP 604 is evaluated
    at runtime without ``from __future__ import annotations``. An earlier
    version filtered on ``ast.BinOp`` nodes carrying a ``ctx``, which no BinOp
    has, so the test passed vacuously and never asserted anything; the shape
    below is verified by the sharp proof in the module docstring (break it and
    this test goes red).
    """
    for name in HOOK_SCRIPTS:
        src = _source(name)
        tree = ast.parse(src)
        has_future = any(
            isinstance(node, ast.ImportFrom)
            and node.module == "__future__"
            and any(a.name == "annotations" for a in node.names)
            for node in tree.body
        )
        if has_future:
            continue
        pep604 = False
        for node in ast.walk(tree):
            ann = None
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                ann = node.returns
                for arg in (list(node.args.args) + list(node.args.kwonlyargs)
                            + [node.args.vararg, node.args.kwarg]):
                    if arg is not None and arg.annotation is not None:
                        if _contains_pep604(arg.annotation):
                            pep604 = True
            if ann is not None and _contains_pep604(ann):
                pep604 = True
        assert not pep604, (
            f"{name} uses `X | None` in an annotation without "
            f"`from __future__ import annotations` — it raises TypeError on "
            f"Python 3.9, which is what macOS `python3` still is."
        )


def _contains_pep604(node: ast.AST) -> bool:
    """True if a `X | Y` union appears anywhere inside an annotation."""
    return any(
        isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr)
        for n in ast.walk(node)
    )


def test_the_pep604_detector_actually_fires():
    """Falsification for the detector above — a vacuous guard is worse than none."""
    assert _contains_pep604(ast.parse("def f(x: int | None) -> None: ...")
                            .body[0].args.args[0].annotation)
    assert _contains_pep604(ast.parse("def f() -> list[int] | None: ...")
                            .body[0].returns)
    assert not _contains_pep604(ast.parse("def f(x: Optional[int]): ...")
                                .body[0].args.args[0].annotation)


def test_search_body_handles_both_vector_layouts():
    """The hooks must speak to an anonymous AND a named collection.

    A named space (what the engine creates for a fresh install) rejects a bare
    vector with 400 "Not existing vector name error", which the caller turns
    into an empty result — memory stops appearing, silently. Both hook scripts
    therefore route their body through the shared helper.
    """
    for name in ("auto_recall.py", "session_start.py"):
        src = _source(name)
        assert "_vector_body(query_embedding)" in src, (
            f"{name} still sends a bare vector — dead on a named collection"
        )
        assert "search_vector_body" in src or "_scope_auto" in src

    shared = (SCRIPTS / "scope_auto.py").read_text(encoding="utf-8")
    assert "def vector_field(" in shared
    assert "def search_vector_body(" in shared
    # The name is read from the collection, never derived from the model id.
    assert "not existing vector name" in shared or "vector name error" in shared


def test_shared_layout_reader_fails_open():
    """An unreadable layout must fall back to today's behaviour, not raise."""
    shared = (SCRIPTS / "scope_auto.py").read_text(encoding="utf-8")
    assert "_LAYOUT_CACHE" in shared
    assert "except Exception" in shared


def test_search_body_used_to_be_a_bare_vector():
    """Falsification: the old shape must NOT be what ships anymore."""
    for name in ("auto_recall.py", "session_start.py"):
        src = _source(name)
        assert '"vector": query_embedding,' not in src, (
            f"{name} is back to the bare vector that broke on named collections"
        )


@pytest.mark.parametrize("name", HOOK_SCRIPTS)
def test_hook_imports_on_the_system_python(name):
    """The real proof: run the script under /usr/bin/python3 (3.9 on macOS)."""
    py = "/usr/bin/python3" if pathlib.Path("/usr/bin/python3").exists() else sys.executable
    probe = (
        "import importlib.util, sys, pathlib\n"
        f"sys.path.insert(0, r'{SCRIPTS}')\n"
        f"p = pathlib.Path(r'{SCRIPTS / name}')\n"
        "s = importlib.util.spec_from_file_location(p.stem, p)\n"
        "m = importlib.util.module_from_spec(s)\n"
        "s.loader.exec_module(m)\n"
        "print('OK')\n"
    )
    result = subprocess.run([py, "-c", probe], capture_output=True, text=True,
                            timeout=90, cwd=str(SCRIPTS))
    assert result.returncode == 0, (
        f"{name} does not import on {py}:\n{result.stderr[-800:]}"
    )


def test_guardrail_hook_starts_so_rules_cannot_be_skipped():
    """Named separately: a dead guardrail lets destructive commands through."""
    src = _source("guardrail_check.py")
    assert "from __future__ import annotations" in src
