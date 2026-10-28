"""ReDoS regression tests for the destructive-command patterns in guardrails.

The "word ... word" patterns used to be written as ``A.*B``. ``re.search``
retries such a pattern from every occurrence of A and backtracks through every
occurrence of B, so it is quadratic, and ``\\bgit\\b.*clean.*-[fd]`` is cubic:
doubling the command multiplies the time by about 8. classify_action() runs
before every terminal command, and nothing caps the command length.

The rewrites ``(?m)^(?>.*?A).*?B`` commit to the first A on each line, which is
always the best choice for fixed-length tokens, so every verdict is unchanged.
These tests pin both properties: same verdicts as the old patterns, linear time.
"""

from __future__ import annotations

import math
import random
import re
import time

import pytest

from nexus_memory.guardrails import (
    GuardrailAction,
    _DESTRUCTIVE_PATTERNS,
    classify_action,
)

# The patterns as they were before the fix: the independent oracle.
OLD_PATTERNS = {
    r"(?m)^(?>.*?\bfind\b).*?-delete": r"\bfind\b.*-delete",
    r"(?m)^(?>.*?\bgit\b)(?>.*?clean).*?-[fd]": r"\bgit\b.*clean.*-[fd]",
    r"(?m)^(?>.*?\bdd\b).*?\bof\b": r"\bdd\b.*\bof\b",
    r"(?m)^(?>.*?DELETE).*?collection": r"DELETE.*collection",
    r"(?m)^(?>.*?\bdrop\b).*?collection": r"\bdrop\b.*collection",
    r"(?m)^(?>.*?\bpip\b).*?install": r"\bpip\b.*install",
    r"(?m)^(?>.*?\bnpm\b).*?install": r"\bnpm\b.*install",
    r"(?m)^(?>.*?\bbrew\b).*?install": r"\bbrew\b.*install",
}

TOKENS = [
    "git", "GIT", "clean", "cleanx", "-f", "-d", "-x", "find", "-delete", "dd",
    "of", "off", "DELETE", "delete", "collection", "drop", "pip", "npm", "brew",
    "install", "a", "x", " ", "  ", "\n", "-", "_", "\t",
]


def _rewritten():
    found = {p.pattern: p for _, p in _DESTRUCTIVE_PATTERNS if p.pattern in OLD_PATTERNS}
    assert set(found) == set(OLD_PATTERNS), "a rewritten pattern is missing"
    return found


def test_old_regex_shape_is_gone():
    # No destructive pattern may use an unbounded ".*" outside an atomic group again.
    for _, pattern in _DESTRUCTIVE_PATTERNS:
        assert not re.search(r"(?<!\?)\.\*(?!\?)", pattern.pattern), pattern.pattern


@pytest.mark.parametrize("new", sorted(OLD_PATTERNS))
def test_same_verdicts_as_the_old_pattern(new):
    compiled = _rewritten()[new]
    old = re.compile(OLD_PATTERNS[new], re.IGNORECASE)
    rng = random.Random(20261028)
    for _ in range(20_000):
        text = "".join(rng.choice(TOKENS) for _ in range(rng.randint(0, 14)))
        assert bool(compiled.search(text)) == bool(old.search(text)), repr(text)


@pytest.mark.parametrize(
    "command, expected",
    [
        ("git clean -fd", GuardrailAction.DELETE),
        ("git -C repo clean -x -f", GuardrailAction.DELETE),
        ("git clean\n-f", None),  # ".*" never crossed a newline
        ("find . -name '*.tmp' -delete", GuardrailAction.DELETE),
        ("dd if=/dev/zero of=/dev/sda", GuardrailAction.DELETE),
        ("pip install requests", GuardrailAction.INSTALL),
        ("git status", None),
    ],
)
def test_known_commands_keep_their_verdict(command, expected):
    assert classify_action(command) == expected


SLOW_SHAPES = {
    "git_clean_repeated": "git clean " * 6_400,   # old pattern: minutes
    "pip_repeated": "pip " * 16_000,
    "find_repeated": "find " * 12_800,
    "dd_repeated": "dd " * 21_000,
    "delete_repeated": "DELETE " * 9_000,
}


@pytest.mark.parametrize("shape", sorted(SLOW_SHAPES))
def test_classify_action_is_linear(shape):
    # Parametrised by name: a 64 000-character id overflows the Windows environment block.
    command = SLOW_SHAPES[shape]
    started = time.perf_counter()
    classify_action(command)
    elapsed = time.perf_counter() - started
    assert elapsed < 0.5, f"{elapsed:.3f}s for {len(command)} characters"


# Non-matching input per pattern: the repeated prefix with no terminator, so
# the old A.*B shape had to try and fail at every occurrence. 100 000 characters.
NON_MATCHING_PREFIX = {
    r"(?m)^(?>.*?\bfind\b).*?-delete": "find ",
    r"(?m)^(?>.*?\bgit\b)(?>.*?clean).*?-[fd]": "git clean ",
    r"(?m)^(?>.*?\bdd\b).*?\bof\b": "dd ",
    r"(?m)^(?>.*?DELETE).*?collection": "DELETE ",
    r"(?m)^(?>.*?\bdrop\b).*?collection": "drop ",
    r"(?m)^(?>.*?\bpip\b).*?install": "pip ",
    r"(?m)^(?>.*?\bnpm\b).*?install": "npm ",
    r"(?m)^(?>.*?\bbrew\b).*?install": "brew ",
}
NON_MATCHING_LENGTH = 100_000


def test_every_rewritten_pattern_has_a_non_matching_case():
    assert set(NON_MATCHING_PREFIX) == set(OLD_PATTERNS)


@pytest.mark.parametrize("new", sorted(NON_MATCHING_PREFIX))
def test_non_matching_input_is_linear(new):
    # Tests the pattern itself: classify_action would stop early on 'drop'.
    compiled = _rewritten()[new]
    prefix = NON_MATCHING_PREFIX[new]
    text = (prefix * math.ceil(NON_MATCHING_LENGTH / len(prefix)))[:NON_MATCHING_LENGTH]
    started = time.perf_counter()
    found = compiled.search(text)
    elapsed = time.perf_counter() - started
    assert found is None, "the input must not contain the terminator"
    assert elapsed < 0.5, f"{elapsed:.3f}s for {len(text)} characters"
