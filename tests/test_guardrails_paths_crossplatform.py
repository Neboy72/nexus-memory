"""Protected-path matching must hold under both path flavours.

The engine compares paths with the host's os.path. These tests swap in
ntpath and posixpath explicitly, so a separator assumption that only
breaks on Windows also fails on a macOS or Linux CI run.
"""

import ntpath
import posixpath
from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from nexus_memory import guardrails
from nexus_memory.guardrails import GuardrailEngine, GuardrailVerdict


@dataclass
class _Point:
    id: str
    payload: dict


FLAVOURS = {
    "posix": (posixpath, "/data", "/data/protected", "/data/protected/file.txt", "/data2", "/"),
    "windows": (ntpath, r"C:\data", r"C:\data\protected", r"C:\data\protected\file.txt", r"C:\data2", "C:\\"),
}


@pytest.fixture(params=sorted(FLAVOURS))
def flavour(request, monkeypatch):
    mod, *paths = FLAVOURS[request.param]
    monkeypatch.setattr(guardrails, "_osp", mod)
    return paths


def test_child_is_inside_protected(flavour):
    _, protected, child, _, _ = flavour
    assert GuardrailEngine._path_matches(child, protected)


def test_recursive_parent_hits_protected_child(flavour):
    parent, protected, _, _, _ = flavour
    assert GuardrailEngine._path_matches(parent, protected, recursive=True)
    assert not GuardrailEngine._path_matches(parent, protected)


def test_sibling_with_shared_prefix_is_not_inside(flavour):
    parent, _, _, sibling, _ = flavour
    assert not GuardrailEngine._path_matches(sibling, parent)
    assert not GuardrailEngine._path_matches(parent, sibling, recursive=True)


def test_everything_is_inside_a_protected_root(flavour):
    parent, _, child, _, root = flavour
    assert GuardrailEngine._path_matches(parent, root)
    assert GuardrailEngine._path_matches(child, root)


def test_other_drive_is_not_inside(monkeypatch):
    monkeypatch.setattr(guardrails, "_osp", ntpath)
    assert not GuardrailEngine._path_matches(r"D:\data\protected", r"C:\data")
    assert not GuardrailEngine._path_matches(r"D:\data", r"C:\data\protected", recursive=True)


def test_rm_child_and_parent_blocked_end_to_end(flavour):
    parent, protected, child, _, _ = flavour
    client = MagicMock()
    client.scroll.return_value = ([_Point(
        id="rule1",
        payload={"content": f"NIEMALS {protected} loeschen - kritisch", "category": "rule"},
    )], None)
    engine = GuardrailEngine(client)
    for target in (protected, child, parent):
        result = engine.check_action(f"rm -rf {target}")
        assert result.verdict == GuardrailVerdict.BLOCK, target
