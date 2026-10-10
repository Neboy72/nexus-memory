"""The engine revision is ONE value, named the same everywhere, and it resolves.

Round 4 (10.10.2026). The catalog entry pinned `v0.22.21`, while two places inside the
repository still fetched an older engine revision. A user installing through the catalog
therefore received an engine that did not carry the access-level promise the entry made.
Every content test was green: they read the WORKING TREE, which had long moved on, not
the tree the pin describes.

What this file guards, in order of who breaks it:

1. `plugins/memory/nexus/pyproject.toml` (`[tool.uv.sources]`) and
   `plugins/memory/nexus/__init__.py` (`_repair_command`) must name the SAME revision.
   They drifted apart twice.
2. That revision is a TAG NAME, never a bare sha. A commit cannot contain its own sha,
   so a sha in the tree is stale the moment it is written; a tag name can be resolved.
3. The tag exists, is annotated, and its version matches the version the manifest,
   `pyproject.toml` and the README badge advertise.
4. **Self-consistency at the pin**: read the same two files AT the tag and they must
   name the same tag again. This is the check that would have caught the 10.10. case —
   the pinned tree was not asked what it installed.
5. The tag is an ancestor of HEAD, so the pin is reachable and cannot be a dangling
   orphan commit.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PLUGIN_PYPROJECT = "plugins/memory/nexus/pyproject.toml"
PLUGIN_INIT = "plugins/memory/nexus/__init__.py"
PLUGIN_YAML = "plugins/memory/nexus/plugin.yaml"
ROOT_PYPROJECT = "pyproject.toml"
README = "README.md"

TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


def git(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True)
    if result.returncode != 0:
        pytest.fail(f"git {' '.join(args)} failed: {result.stderr.strip()[:300]}")
    return result.stdout.strip()


def ref_from_pyproject(text: str) -> str:
    """The rev of `nexus-memory` in [tool.uv.sources]."""
    match = re.search(
        r'nexus-memory\s*=\s*\{[^}]*?rev\s*=\s*"([^"]+)"', text, re.S)
    assert match, f"no [tool.uv.sources] nexus-memory rev in {PLUGIN_PYPROJECT}"
    return match.group(1)


def ref_from_init(text: str) -> str:
    """The rev in `_repair_command`'s _SRC string."""
    match = re.search(
        r'nexus-memory\s*@\s*git\+https://github\.com/Neboy72/nexus-memory\.git"\s*\n?\s*"@([^"]+)"',
        text)
    assert match, f"no pinned git+ rev in {PLUGIN_INIT}"
    return match.group(1)


def refs_in_working_tree() -> tuple[str, str]:
    return (ref_from_pyproject((REPO / PLUGIN_PYPROJECT).read_text()),
            ref_from_init((REPO / PLUGIN_INIT).read_text()))


def test_both_engine_refs_name_the_same_revision() -> None:
    uv_rev, init_rev = refs_in_working_tree()
    assert uv_rev == init_rev, (
        f"the two engine refs disagree: pyproject says {uv_rev!r}, "
        f"_repair_command says {init_rev!r}. One of them installs the other one's promise."
    )


def test_engine_ref_is_a_tag_name_not_a_sha() -> None:
    uv_rev, _ = refs_in_working_tree()
    assert not SHA_RE.match(uv_rev), (
        f"the engine ref is the bare sha {uv_rev!r}. A commit cannot name itself, so a "
        "sha written into the tree is stale the moment it lands; pin the tag name."
    )
    assert TAG_RE.match(uv_rev), (
        f"the engine ref {uv_rev!r} is not a vX.Y.Z tag name."
    )


def test_engine_ref_tag_exists_and_matches_the_advertised_version() -> None:
    tag, _ = refs_in_working_tree()
    assert git("cat-file", "-t", tag) == "tag", (
        f"{tag} is not an annotated tag — a lightweight tag can silently move."
    )

    manifest = (REPO / PLUGIN_YAML).read_text()
    plugin_toml = (REPO / PLUGIN_PYPROJECT).read_text()
    root_toml = (REPO / ROOT_PYPROJECT).read_text()
    readme = (REPO / README).read_text()

    advertised = {
        PLUGIN_YAML: re.search(r"^version:\s*(\S+)", manifest, re.M).group(1),
        PLUGIN_PYPROJECT: re.search(r'^version\s*=\s*"([^"]+)"', plugin_toml, re.M).group(1),
        ROOT_PYPROJECT: re.search(r'^version\s*=\s*"([^"]+)"', root_toml, re.M).group(1),
        README: re.search(r"badge/version-([0-9.]+)-", readme).group(1),
    }
    expected = tag.lstrip("v")
    for where, value in advertised.items():
        assert value == expected, (
            f"{where} advertises {value!r} while the engine ref names tag {tag!r} "
            f"({expected!r}). A pin whose version strings disagree is the same defect "
            "one layer up."
        )


def test_engine_ref_is_self_consistent_at_the_pin() -> None:
    """Read the two ref files AT the tag: they must name the same tag again.

    This is the guard for the failure class found on 10.10.2026 — the tree that a
    catalog install actually receives was never asked what it installs.
    """
    tag, _ = refs_in_working_tree()
    at_pin_pyproject = git("show", f"{tag}:{PLUGIN_PYPROJECT}")
    at_pin_init = git("show", f"{tag}:{PLUGIN_INIT}")

    pinned_uv = ref_from_pyproject(at_pin_pyproject)
    pinned_init = ref_from_init(at_pin_init)
    assert pinned_uv == pinned_init == tag, (
        f"at {tag} the refs say pyproject={pinned_uv!r} and _repair_command="
        f"{pinned_init!r}, but the tag itself is {tag!r}. Whoever installs this tag "
        "gets a different engine than the tag promises."
    )


def test_engine_ref_tag_is_an_ancestor_of_head() -> None:
    tag, _ = refs_in_working_tree()
    subprocess.run(["git", "merge-base", "--is-ancestor", tag, "HEAD"],
                   cwd=REPO, capture_output=True, check=True)
