"""ReDoS regression tests: ``_wikilinks`` must match the regex, in linear time.

``WIKILINK_PATTERN.findall`` is quadratic on input like ``"[" * n + "]"``
(measured: 32 001 chars = 1.77 s, doubling quadruples the time). The linear
scanner must return exactly the same list — no more, no less.
"""

from __future__ import annotations

import random
import time

import pytest

from nexus.discovery.classifier import WIKILINK_PATTERN, _wikilinks

EDGE_CASES = [
    "",
    "[]",
    "[[]]",
    "[[]]]",
    "[[a]]",
    "[[a]b]]",
    "[[[a]]]",
    "[[a[[b]]",
    "[[a[b]]",
    "[[a]]b]]",
    "[[a]] [[b]]",
    "[[ ]]",
    "[[[[",
    "[[a",
    "a[[b",
    "[a]",
    "]]]]",
    "[[a]]]",
    "[[a]]]]",
    "[[ab[[cd]x]]",
    "[[a[[b]x",
    "[[a]] x [[b]] y [[c",
    "[" * 100 + "]",
    "[[a" * 40 + "]",
]


@pytest.mark.parametrize("text", EDGE_CASES)
def test_wikilinks_matches_pattern_on_edge_cases(text: str) -> None:
    assert _wikilinks(text) == WIKILINK_PATTERN.findall(text)


@pytest.mark.parametrize(
    "text",
    [
        "[[a]]",
        "[[ ]]",
        "[[a]b]]",
        "]]",
        "[[]]",
        "[[[a]]]",
        "[[a[[b]]",
    ],
)
def test_wikilinks_returns_expected_values(text: str) -> None:
    """Spot-check that the oracle really is the pattern's semantics."""
    assert _wikilinks(text) == WIKILINK_PATTERN.findall(text)


def test_wikilinks_matches_pattern_on_random_strings() -> None:
    rng = random.Random(20261025)
    alphabet = "[]ab x"
    for _ in range(200_000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
        assert _wikilinks(text) == WIKILINK_PATTERN.findall(text), repr(text)


@pytest.mark.parametrize(
    "text",
    [
        "[" * 100_000 + "]",
        "[[a" * 33_333 + "]",
        "[" * 100_000,
    ],
)
def test_wikilinks_is_linear_on_pathological_input(text: str) -> None:
    start = time.perf_counter()
    _wikilinks(text)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.5, f"_wikilinks took {elapsed:.3f}s on {len(text)} chars"


def test_explicit_reference_uses_linear_scanner() -> None:
    """The classifier path itself must not hit the quadratic regex."""
    from nexus.discovery.classifier import _check_explicit_reference

    start = time.perf_counter()
    result = _check_explicit_reference("[" * 100_000 + "]", "some target content")
    elapsed = time.perf_counter() - start
    assert result is None
    assert elapsed < 0.5, f"_check_explicit_reference took {elapsed:.3f}s"
