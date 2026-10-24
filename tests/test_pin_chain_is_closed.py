"""The engine revision is ONE immutable value, named the same everywhere, and it resolves.

Fassung 2 (11.10.2026). Der Reviewer hat den Tag-Namen abgelehnt: ein Tag laesst sich
verschieben, und ein verschobener Tag wuerde die Engine fuer jede Neuinstallation und
jeden Reparaturlauf still veraendern, ohne dass es jemand erneut prueft. Verlangt ist
stattdessen ein voller 40-stelliger Commit-Hash, mit dem Zwei-Commit-Rezept:

  Commit A: die Engine fertig.
  Commit B: NUR die zwei Revisionsstellen auf A's vollen Hash (plus Versionsnummern).
  Dieser Test beweist, dass zwischen A und B nichts an der Engine passiert ist.

Warum Fassung 1 falsch war: sie VERLANGTE einen Tag-Namen ("ein Commit kann seinen
eigenen Hash nicht enthalten"). Das war bequem, aber es liess genau die Hintertuer
offen, die der Reviewer schliesst. Ein Commit kann seinen eigenen Hash nicht nennen —
deshalb nennt der Pin den letzten ENGINE-Commit, und der Ketten-Beweis unten zeigt,
dass HEAD seither keine Engine-Aenderung mehr enthaelt.

Was diese Datei bewacht, in der Reihenfolge, in der es kaputtgeht:

1. `plugins/memory/nexus/pyproject.toml` (`[tool.uv.sources]`) und
   `plugins/memory/nexus/__init__.py` (`_repair_command`) nennen DIESELBE Revision.
   Sie sind zweimal auseinandergelaufen.
2. Diese Revision ist ein VOLLER Commit-Hash (40 Hexzeichen), kein Tag-Name.
3. Der Hash existiert (lokal; und ist von einem entfernten Ref erreichbar, sobald
   das Repository einen kennt). Solange der Pin in der noch unveroeffentlichten
   Kette von HEAD liegt, ist Erreichbarkeit nachweislich unmoeglich — dafuer gilt
   genau eine Ausnahme, ein sichtbarer SKIP mit Grund (siehe Test 3).
4. **Ketten-Beweis:** `git diff <pin> HEAD -- src nexus pyproject.toml` ist LEER.
   Das ist der Satz des Reviewers in Pruefform: B aendert nur die Revisionsstellen
   und die Versionsnummern, nicht die Engine. Beim Pruefen ist HEAD der Pin-Commit
   B, also ist `git diff <pin> HEAD` genau das geforderte `git diff A B`. Jeder
   spaetere Engine-Commit macht ihn rot, solange kein neues Paar A'/B' gebaut wird
   — das ist der Zweck dieses Waechters.
5. Die Versionsangaben (Manifest, Plugin-pyproject, Root-pyproject, README-Badge)
   nennen alle dieselbe Version; haengt am gepinnten Hash ein vX.Y.Z-Tag, muss es
   genau diese Version sein.
6. Der gepinnte Hash ist ein Vorfahr von HEAD — erreichbar, kein Waisencommit.

Die Lehre vom 10.10.2026 bleibt eingebaut: alle inhaltlichen Tests waren gruen, weil
sie den ARBEITSSTAND gelesen haben, nicht den Stand, auf den der Pin zeigt. Deshalb
liest Punkt 4 ausdruecklich gegen den gepinnten Commit und nicht gegen die Datei im
Verzeichnis.
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

# Der Ketten-Beweis laeuft ueber genau diese Pfade (Reviewer-Wortlaut). Sie sind
# WURZEL-bezogen: `nexus` trifft `nexus/…` an der Wurzel, nicht
# `plugins/memory/nexus/…`; `pyproject.toml` die Wurzeldatei, nicht die des
# Plugins; `src` den Wurzelbaum. Die zwei Revisionsstellen sind bewusst NICHT in
# dieser Liste — B muss sie aendern duerfen, sonst waere der Beweis unmoeglich
# ("aendere nur diese zwei Stellen, aber beruehre die Engine nicht"). Als
# unabhaengige Probe, gemessen am 11.10.2026 an einem veroeffentlichten Commit,
# der BEIDE Arten von Datei angefasst hat:
#   git diff --name-only bc00e65^ bc00e65 -- src nexus pyproject.toml
#   -> pyproject.toml            (nur die Wurzeldatei)
#   git diff --name-only bc00e65^ bc00e65 -- plugins/memory/nexus
#   -> plugins/memory/nexus/pyproject.toml, __init__.py, plugin.yaml
# Die Plugin-Dateien liegen also ausserhalb der drei Pfade, obwohl sie
# `pyproject.toml` heissen.
ENGINE_PATHS = ["src", "nexus", "pyproject.toml"]

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")


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


def pinned_ref() -> str:
    uv_rev, _ = refs_in_working_tree()
    return uv_rev


# ─── 1 + 2: dieselbe Revision, und zwar ein voller Hash ─────────────────────

def test_both_engine_refs_name_the_same_revision() -> None:
    uv_rev, init_rev = refs_in_working_tree()
    assert uv_rev == init_rev, (
        f"the two engine refs disagree: pyproject says {uv_rev!r}, "
        f"_repair_command says {init_rev!r}. One of them installs the other one's promise."
    )


def test_engine_ref_is_a_full_commit_sha_not_a_tag() -> None:
    rev = pinned_ref()
    assert not TAG_RE.match(rev), (
        f"the engine ref is the tag name {rev!r}. A tag can be moved, and a moved tag "
        "would change the engine for every new install and every repair run without "
        "another review. Pin the full 40-character commit sha instead."
    )
    assert SHA_RE.match(rev), (
        f"the engine ref {rev!r} is neither a vX.Y.Z tag nor a full 40-character commit "
        "sha. Short hashes are not accepted: they are ambiguous by construction."
    )


# ─── 3: der Hash existiert und ist erreichbar ───────────────────────────────

def test_engine_ref_exists_as_a_commit() -> None:
    rev = pinned_ref()
    probe = subprocess.run(["git", "cat-file", "-e", f"{rev}^{{commit}}"],
                           cwd=REPO, capture_output=True, text=True)
    assert probe.returncode == 0, (
        f"the pinned revision {rev!r} is not a commit in this repository. A pin that "
        "does not resolve installs nothing for everybody."
    )


def remote_refs_containing(rev: str) -> list[str]:
    """Every remote-tracking ref whose history contains `rev`."""
    probe = subprocess.run(
        ["git", "for-each-ref", "--contains", rev, "--format=%(refname)", "refs/remotes"],
        cwd=REPO, capture_output=True, text=True)
    if probe.returncode != 0:
        pytest.fail(f"git for-each-ref --contains {rev} failed: {probe.stderr.strip()[:300]}")
    return [ref for ref in probe.stdout.splitlines() if ref.strip()]


def test_engine_ref_is_reachable_from_a_remote_ref() -> None:
    """The pin must survive outside this checkout — otherwise a fresh clone fails.

    Genau eine Ausnahme, eng gefasst: ein Pin, der gerade erst entstanden ist, steht
    auf keinem entfernten Ref — ein Push kann nicht enthalten, was er noch nicht
    geschickt hat. Der SKIP ist nur erlaubt, solange der Pin in der noch
    unveroeffentlichten Kette von HEAD liegt (HEAD selbst oder ein Vorfahr davon) und
    KEIN entferntes Ref ihn enthaelt: wer HEAD pusht, veroeffentlicht den Pin mit, und
    der CI-Lauf auf dem veroeffentlichten Commit prueft die Erreichbarkeit erneut.
    Ein Pin, den kein entferntes Ref enthaelt und der KEIN Vorfahr von HEAD ist, ist
    eine Waise (umgeschriebene, verlorene oder erfundene Historie) und bleibt rot.
    """
    rev = pinned_ref()
    if not remote_refs_containing(rev):
        unpublished = subprocess.run(["git", "merge-base", "--is-ancestor", rev, "HEAD"],
                                      cwd=REPO, capture_output=True, text=True)
        if unpublished.returncode != 0:
            pytest.fail(
                f"the pinned revision {rev!r} is contained in no remote ref and is not an "
                "ancestor of HEAD: it is an orphan, so nobody else can install it."
            )
        pytest.skip(
            f"pin commit {rev[:12]} is part of the unpublished history at HEAD and not yet "
            "on any remote ref — reachability is checked by CI after publication"
        )
    remote_ref = None
    for candidate in ("origin/main", "origin/HEAD", "origin/master"):
        probe = subprocess.run(["git", "rev-parse", "--verify", "--quiet", candidate],
                               cwd=REPO, capture_output=True, text=True)
        if probe.returncode == 0:
            remote_ref = candidate
            break
    if remote_ref is None:
        pytest.skip("no remote-tracking ref in this checkout — cannot prove reachability here")
    probe = subprocess.run(["git", "merge-base", "--is-ancestor", rev, remote_ref],
                           cwd=REPO, capture_output=True, text=True)
    assert probe.returncode == 0, (
        f"the pinned revision {rev!r} is not reachable from {remote_ref}. It exists only "
        "locally, so nobody else can install it."
    )


# ─── 4: der Ketten-Beweis (der Satz des Reviewers in Pruefform) ─────────────

def test_chain_is_closed_no_engine_change_after_the_pin() -> None:
    rev = pinned_ref()
    changed = git("diff", "--name-only", rev, "HEAD", "--", *ENGINE_PATHS)
    assert changed == "", (
        f"the engine changed after the pin: {changed.splitlines()}. The pin names "
        f"{rev!r}, but HEAD carries engine edits on top of it, so an install from the "
        "catalog would receive different code than the reviewed one. Put engine changes "
        "into the pinned commit (Commit A), and let the ref commit (B) change nothing "
        "but the two revision lines and the version numbers."
    )


def test_pin_is_an_ancestor_of_head() -> None:
    rev = pinned_ref()
    subprocess.run(["git", "merge-base", "--is-ancestor", rev, "HEAD"],
                   cwd=REPO, capture_output=True, check=True)


# ─── 5: die Versionen sagen dasselbe ───────────────────────────────────────

def test_advertised_versions_agree() -> None:
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
    values = set(advertised.values())
    assert len(values) == 1, (
        f"the advertised version strings disagree: {advertised}. A pin whose version "
        "strings disagree is the same defect one layer up."
    )

    # Wenn am gepinnten Commit ein Versions-Tag haengt, muss es diese Version sein.
    rev = pinned_ref()
    tags = [t for t in git("tag", "--points-at", rev).splitlines() if TAG_RE.match(t)]
    if tags:
        expected = next(iter(values))
        assert any(t.lstrip("v") == expected for t in tags), (
            f"the pinned commit {rev[:10]} carries the tag(s) {tags}, but the advertised "
            f"version is {expected!r}. Whoever checks out the pin sees a different version "
            "than the catalog promises."
        )
