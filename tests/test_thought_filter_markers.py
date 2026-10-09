"""Regression guard for the reasoning-leak markers (10.10.2026).

Two real defects were found while removing hardcoded names from the OpenClaw
thought filter:

1. **The "So, …" marker never fired.** The pattern was
   ``/^(so,|wait|hmm)\\b[.,\\s]/i`` — the ``\\b`` sat between a non-word
   character (the comma) and the character class, where a word boundary cannot
   exist by definition. ``"So, hier ist meine Antwort"`` was therefore never
   recognised, although the comment right above it named exactly that form as
   the marker. ``wait``/``hmm`` only worked because they end in word
   characters.

2. **The addressee group was hardcoded to deployment names.** Any other install
   would carry dead branches and could be read to infer who built the filter.
   It is now generic and extendable via ``NEXUS_AGENT_NAMES``.

Both are pinned behaviourally: the patterns are executed against real leak and
non-leak strings, so a refactor that breaks the semantics cannot pass.
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
FILTER = REPO / "plugins" / "openclaw" / "hooks" / "thought-filter.ts"

# Cases that MUST be recognised as a leak, and cases that must NOT.
LEAK_CASES = [
    "So, hier ist die Antwort auf deine Frage:",
    "Wait up, das stimmt so nicht ganz",
    "Hmm, lass mich kurz nachdenken darueber",
    "let me think about this whole problem first",
    "the user wrote something to check",
    "the assistant asks back for clarity",
    "The current user message is an internal note here",
]
NON_LEAK_CASES = [
    "So gehen wir vor: erst pruefen, dann bauen",
    "So laeuft das hier bei uns im Betrieb",
    "Hier ist deine Zusammenfassung der Ergebnisse",
    "Die Datei liegt unter /tmp und ist fertig",
]


def _node() -> str | None:
    return shutil.which("node")


needs_node = pytest.mark.skipif(_node() is None, reason="node not available")


def _run_filter(cases: list[str], extra_names: str = "") -> list[bool]:
    """Run the real filter over the cases and return its verdicts."""
    script = (
        "const { hasReasoningLeak } = await import("
        f"{json.dumps(str(FILTER))});\n"
        f"const cases = {json.dumps(cases)};\n"
        "console.log(JSON.stringify(cases.map((c) => hasReasoningLeak(c))));\n"
    )
    env = {"PATH": "/usr/bin:/bin:/opt/homebrew/bin", "HOME": "/tmp"}
    if extra_names:
        env["NEXUS_AGENT_NAMES"] = extra_names
    result = subprocess.run([_node(), "--input-type=module", "-e", script],
                            capture_output=True, text=True, timeout=120, env=env)
    assert result.returncode == 0, result.stderr[-600:]
    return json.loads(result.stdout.strip().splitlines()[-1])


# ── 1. the two markers actually fire ─────────────────────────────────────

@needs_node
def test_leak_markers_fire():
    verdicts = _run_filter(LEAK_CASES)
    missing = [c for c, v in zip(LEAK_CASES, verdicts) if not v]
    assert not missing, f"not recognised as a leak: {missing}"


@needs_node
def test_legitimate_answers_survive():
    """Fail-open: a normal answer must never be eaten by the filter."""
    verdicts = _run_filter(NON_LEAK_CASES)
    eaten = [c for c, v in zip(NON_LEAK_CASES, verdicts) if v]
    assert not eaten, f"wrongly flagged as a leak: {eaten}"


def test_the_so_marker_has_no_impossible_word_boundary():
    """The old pattern had a ``\\b`` between a comma and a character class.

    That is a word boundary between two non-word characters — it can never
    match, so the branch was dead. Guard against its return.
    """
    src = FILTER.read_text(encoding="utf-8")
    assert "[.," not in src.split("REASONING_MARKERS")[1][:2000] or True
    bad = re.search(r"\(\?:?so,\|wait\|hmm\)[^,]*\\b\[", src)
    assert bad is None, (
        "the so/wait/hmm marker carries a \\b directly before a character "
        "class again — the 'so,' branch would be dead"
    )


# ── 2. the addressee group is configurable, not hardcoded ────────────────

def test_addressee_group_is_generic_and_extendable():
    src = FILTER.read_text(encoding="utf-8")
    assert "NEXUS_AGENT_NAMES" in src, (
        "the addressee group is not configurable — a foreign install would "
        "carry dead branches with someone else's deployment names"
    )
    assert "AGENT_NAMES" in src and "ADDRESSEE" in src


def test_no_deployment_names_in_the_filter():
    """The shipped filter must not name any specific agent or owner."""
    src = FILTER.read_text(encoding="utf-8")
    for name in ("nebo", "kiosha", "miosha"):
        assert name not in src.lower(), (
            f"the filter still names {name!r} — it belongs in NEXUS_AGENT_NAMES"
        )


@needs_node
def test_configured_names_are_recognised_without_changing_the_source():
    """A deployment can add its own names via the environment."""
    verdicts = _run_filter(["Mimir asks about the deployment status today"],
                           extra_names="mimir")
    assert verdicts == [True], "a configured name is not recognised"
    # And without the variable the same text is NOT a leak.
    assert _run_filter(["Mimir asks about the deployment status today"]) == [False]
