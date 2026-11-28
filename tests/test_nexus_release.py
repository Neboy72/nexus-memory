"""Tests fuer den Release-Laeufer (``tools/nexus_release.py``).

Der Laeufer soll die Checkliste ABARBEITEN, nicht beschreiben. Was hier
festgehalten wird, ist genau das, was ihn von einer Anleitung unterscheidet:

  1. Die Schrittfolge ist die Checkliste, in der Reihenfolge.
  2. Eine fehlgeschlagene Pruefung beendet den Lauf — spaetere Schritte laufen
     nicht mehr.
  3. ``--weiter-ab`` markiert Frueheres als "uebersprungen", nie als OK, und
     das Urteil ist dann nicht "FERTIG".
  4. ``--trockenlauf`` ruft kein Kommando auf.
  5. Der Katalog-Schritt akzeptiert einen offenen UND einen gemergten PR.
  6. Der Versionsschritt schreibt alle sechs Stellen und prueft nach.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = ROOT / "tools" / "nexus_release.py"
CHECKER_PATH = Path.home() / "DeepSeek Bot" / "tools" / "nexus-release-check.py"

SECHSUNDZWANZIG = "0.22.79"
NAECHSTE = "0.22.80"


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load(RUNNER_PATH, "nexus_release")


# ── 1. die Schrittfolge ───────────────────────────────────────────────────

def test_step_order_is_the_checklist():
    assert runner.STEP_NAMEN == (
        "vorpruefung", "version", "changelog", "commit", "pin", "push",
        "tag", "release", "prueflauf", "clawhub", "katalog", "neustart",
        "fassungen", "bilanz")


def test_there_is_no_force_and_no_skip_option():
    """--force und --skip gibt es nicht — auch nicht als versteckte Option."""
    quelltext = RUNNER_PATH.read_text(encoding="utf-8")
    assert '"--force"' not in quelltext
    assert '"--skip"' not in quelltext
    for flag in ("--skip", "--force"):
        with pytest.raises(SystemExit):
            runner.main([NAECHSTE, flag, "push"])


# ── 2./3. Abbruch und Weiterlaufen ────────────────────────────────────────

def _fake_steps(protokoll, verhalten):
    def baue(name):
        def step(_ctx):
            protokoll.append(name)
            art = verhalten.get(name, "ok")
            if art == "fail":
                raise runner.StepFailed("%s fehlt" % name)
            return "getan: %s" % name
        return step
    return [(n, "t", baue(n), "trocken") for n in
            ("vorpruefung", "version", "changelog", "bilanz")]


def test_failed_check_stops_the_run():
    protokoll = []
    steps = _fake_steps(protokoll, {"version": "fail"})
    ergebnisse, urteil = runner.run_steps(steps, runner.Ctx(NAECHSTE))

    assert protokoll == ["vorpruefung", "version"], protokoll
    assert [e[1] for e in ergebnisse] == [runner.OK, runner.FAIL]
    assert "NICHT FERTIG" in urteil and "version" in urteil


def test_weiter_ab_marks_skipped_not_ok():
    protokoll = []
    steps = _fake_steps(protokoll, {})
    ergebnisse, urteil = runner.run_steps(steps, runner.Ctx(NAECHSTE),
                                          weiter_ab="changelog")

    status = {name: status for name, status, _d in ergebnisse}
    assert status["vorpruefung"] == runner.SKIP
    assert status["version"] == runner.SKIP
    assert status["changelog"] == runner.OK
    assert "vorpruefung" not in protokoll, "uebersprungene Schritte laufen nicht"
    assert urteil.startswith("RELEASE UNSICHER"), urteil
    assert not urteil.startswith("RELEASE FERTIG")


def test_unknown_step_for_weiter_ab_is_rejected():
    with pytest.raises(runner.Technical) as fehler:
        runner.run_steps(_fake_steps([], {}), runner.Ctx(NAECHSTE),
                         weiter_ab="gibtesnicht")
    assert "unbekannter Schritt" in str(fehler.value)


def test_clean_run_says_fertig():
    ergebnisse, urteil = runner.run_steps(_fake_steps([], {}), runner.Ctx(NAECHSTE))
    assert urteil == "RELEASE FERTIG %s" % NAECHSTE
    assert all(e[1] == runner.OK for e in ergebnisse)


# ── 4. Trockenlauf ────────────────────────────────────────────────────────

def test_dry_run_calls_nothing_and_writes_nothing(monkeypatch, capsys):
    aufrufe = []
    monkeypatch.setattr(runner, "sh", lambda *a, **k: aufrufe.append(a) or (0, "", ""))
    monkeypatch.setattr(runner, "load_checker", lambda: aufrufe.append("checker"))
    vorher = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert runner.main([NAECHSTE, "--trockenlauf"]) == 0
    ausgabe = capsys.readouterr().out

    assert aufrufe == [], "der Trockenlauf darf nichts aufrufen: %s" % aufrufe
    assert (ROOT / "CHANGELOG.md").read_text(encoding="utf-8") == vorher
    for name in runner.STEP_NAMEN:
        assert name in ausgabe, ausgabe
    assert "nichts ausgefuehrt" in ausgabe


# ── 5. Katalog: offen UND gemergt ─────────────────────────────────────────

@pytest.fixture()
def echter_checker(monkeypatch):
    """Der echte Release-Pruefer, aber mit Attrappen statt GitHub."""
    if not CHECKER_PATH.exists():
        pytest.skip("Release-Pruefer nicht vorhanden: %s" % CHECKER_PATH)
    checker = _load(CHECKER_PATH, "nrc_fuer_runner")
    monkeypatch.setattr(runner, "load_checker", lambda: checker)
    return checker


def _stub_github(checker, monkeypatch, prs, version=NAECHSTE):
    import re as _re

    def fake_json(path):
        if "pulls?" in path:
            return prs
        if "/files" in path:
            return [{"filename": "plugin-catalog/nexus.yaml"}]
        raise AssertionError(path)

    def fake_api(path, raw=False):
        ref = path.split("ref=")[-1]
        if ref.startswith("merge-") or ref.startswith("catalog/nexus-"):
            return 'name: nexus\nversion: "%s"\n' % version
        raise checker.NotFound("nicht vorhanden")

    monkeypatch.setattr(checker, "_gh_json", fake_json)
    monkeypatch.setattr(checker, "gh_api", fake_api)


def _pr(nummer, state, merged_at=None):
    return {"number": nummer, "state": state, "merged_at": merged_at,
            "head": {"ref": "catalog/nexus-0.22.79",
                     "repo": {"full_name": "Neboy72/hermes-agent"}},
            "user": {"login": "Neboy72"}, "merge_commit_sha": "merge-%s" % nummer}


def test_katalog_accepts_an_open_pull_request(echter_checker, monkeypatch):
    _stub_github(echter_checker, monkeypatch,
                 [_pr(160888, "open")])
    detail = runner.step_katalog(runner.Ctx(NAECHSTE))
    assert "offen" in detail, detail


def test_katalog_accepts_a_merged_pull_request(echter_checker, monkeypatch):
    _stub_github(echter_checker, monkeypatch,
                 [_pr(160437, "closed", "2026-11-27T10:00:00Z")])
    detail = runner.step_katalog(runner.Ctx(NAECHSTE))
    assert "gemergt" in detail, detail


def test_katalog_without_a_pr_stops_the_run(echter_checker, monkeypatch):
    _stub_github(echter_checker, monkeypatch, [])
    with pytest.raises(runner.StepFailed) as fehler:
        runner.step_katalog(runner.Ctx(NAECHSTE))
    assert "kein Katalog-PR" in str(fehler.value)


# ── 6. der Versionsschritt ────────────────────────────────────────────────

@pytest.fixture()
def repo(tmp_path, monkeypatch):
    def baue(**abweichung):
        (tmp_path / "plugins/memory/nexus").mkdir(parents=True, exist_ok=True)
        (tmp_path / "nexus").mkdir(exist_ok=True)
        dateien = {
            "pyproject.toml": '[project]\nname = "nexus-memory"\nversion = "%s"\n'
                              % SECHSUNDZWANZIG,
            "plugins/memory/nexus/pyproject.toml":
                '[project]\nversion = "%s"\n' % SECHSUNDZWANZIG,
            "plugins/memory/nexus/plugin.yaml":
                "name: nexus\nversion: %s\n" % SECHSUNDZWANZIG,
            "nexus/__init__.py": '__version__ = "%s"\n' % SECHSUNDZWANZIG,
            "README.md": ("[![Version](https://img.shields.io/badge/version-%s-brightgreen"
                          "?style=flat-square)](x)\n\n<sub>Built by Nebo · v%s · One memory"
                          "</sub>\n" % (SECHSUNDZWANZIG, SECHSUNDZWANZIG)),
        }
        dateien.update(abweichung)
        for rel, inhalt in dateien.items():
            ziel = tmp_path / rel
            ziel.parent.mkdir(parents=True, exist_ok=True)
            ziel.write_text(inhalt, encoding="utf-8")
        monkeypatch.setattr(runner, "REPO", tmp_path)
        ctx = runner.Ctx(NAECHSTE)
        ctx.old_version = SECHSUNDZWANZIG
        return ctx, tmp_path
    return baue


def test_version_step_rewrites_all_six_spots(repo):
    ctx, pfad = repo()
    detail = runner.step_version(ctx)
    assert "6 Stellen" in detail
    for rel in ("pyproject.toml", "plugins/memory/nexus/pyproject.toml",
                "plugins/memory/nexus/plugin.yaml", "nexus/__init__.py",
                "README.md"):
        inhalt = (pfad / rel).read_text(encoding="utf-8")
        assert NAECHSTE in inhalt, "%s nicht gesetzt" % rel
        assert SECHSUNDZWANZIG not in inhalt, "%s nennt noch die alte Fassung" % rel


def test_version_step_fails_when_a_spot_is_missing(repo):
    ctx, pfad = repo(**{"README.md": "<sub>ohne Fusszeile</sub>\n"})
    with pytest.raises(runner.StepFailed) as fehler:
        runner.step_version(ctx)
    assert "README.md" in str(fehler.value)


def test_version_step_refuses_when_versions_are_equal(repo):
    ctx, _ = repo()
    ctx.old_version = NAECHSTE
    with pytest.raises(runner.StepFailed) as fehler:
        runner.step_version(ctx)
    assert "nichts zu tun" in str(fehler.value)


# ── 7. der Changelog-Schritt ──────────────────────────────────────────────

def test_changelog_step_inserts_a_placeholder_and_fails(repo, monkeypatch):
    ctx, pfad = repo()
    (pfad / "CHANGELOG.md").write_text("## [0.22.78] - 2026-11-27\n\n- alt\n",
                                       encoding="utf-8")
    monkeypatch.setattr(runner, "CHECKER_PATH", CHECKER_PATH)
    with pytest.raises(runner.StepFailed) as fehler:
        runner.step_changelog(ctx)
    assert "Platzhalter" in str(fehler.value)
    # die Ueberschrift steht trotzdem schon da — der Schritt hat den Rahmen gebaut
    assert "## [%s] - " % NAECHSTE in (pfad / "CHANGELOG.md").read_text(encoding="utf-8")


def test_changelog_step_accepts_an_english_section(repo, monkeypatch):
    pytest.importorskip("json")
    if not CHECKER_PATH.exists():
        pytest.skip("Release-Pruefer nicht vorhanden")
    ctx, pfad = repo()
    text = ("## [%s] - 2026-11-28\n\n**A short English summary.**\n\n"
            "The runner now walks the whole checklist and checks every step "
            "itself, so a release cannot be published with the catalog pull "
            "request and the package upload still missing. If a check fails the "
            "run stops there and names what is missing. The text is long enough "
            "to count as a real entry and it is written in English.\n\n"
            "## [0.22.78] - 2026-11-27\n\n- older\n" % NAECHSTE)
    (pfad / "CHANGELOG.md").write_text(text, encoding="utf-8")
    monkeypatch.setattr(runner, "CHECKER_PATH", CHECKER_PATH)

    detail = runner.step_changelog(ctx)
    assert "englisch" in detail, detail


def test_changelog_step_rejects_german_text(repo, monkeypatch):
    if not CHECKER_PATH.exists():
        pytest.skip("Release-Pruefer nicht vorhanden")
    ctx, pfad = repo()
    text = ("## [%s] - 2026-11-28\n\n**Eine kurze Zusammenfassung.**\n\n"
            "Der Laeufer geht jetzt die ganze Liste durch und prueft jeden "
            "Schritt selbst, damit ein Release nicht mehr ohne Katalog und "
            "ohne Paket rausgeht. Wenn eine Pruefung nicht besteht, endet der "
            "Lauf dort und nennt genau das, was fehlt, statt weiterzulaufen.\n\n"
            "## [0.22.78] - 2026-11-27\n\n- aelter\n" % NAECHSTE)
    (pfad / "CHANGELOG.md").write_text(text, encoding="utf-8")
    monkeypatch.setattr(runner, "CHECKER_PATH", CHECKER_PATH)

    with pytest.raises(runner.StepFailed) as fehler:
        runner.step_changelog(ctx)
    assert "nicht englisch" in str(fehler.value)


# ── 8. Vorpruefung: was darf schon geaendert sein ────────────────────────

def test_precheck_allows_the_backlog_but_not_other_changes(monkeypatch):
    monkeypatch.setattr(runner, "AUDIT", ROOT / "pyproject.toml")
    monkeypatch.setattr(runner, "git",
                        lambda *a, **k: "M docs/RELEASE-BACKLOG.md\n?? tools/neu.py")
    monkeypatch.setattr(runner, "sh", lambda *a, **k: (
        (0, "", "") if "pytest" not in " ".join(str(x) for x in a)
        else (1, "1 failed, 3500 passed\nFAILED %s\n" % runner.KNOWN_RED, "")))
    detail = runner.step_vorpruefung(runner.Ctx(NAECHSTE))
    assert "Arbeitsbaum sauber" in detail


def test_precheck_stops_on_an_unexpected_change(monkeypatch):
    monkeypatch.setattr(runner, "git",
                        lambda *a, **k: "M src/nexus_memory/mcp_server.py")
    with pytest.raises(runner.StepFailed) as fehler:
        runner.step_vorpruefung(runner.Ctx(NAECHSTE))
    assert "unerwartete Aenderungen" in str(fehler.value)


def test_precheck_stops_on_a_second_failing_test(monkeypatch):
    monkeypatch.setattr(runner, "AUDIT", ROOT / "pyproject.toml")
    monkeypatch.setattr(runner, "git", lambda *a, **k: "")
    monkeypatch.setattr(runner, "sh", lambda *a, **k: (
        (0, "", "") if "pytest" not in " ".join(str(x) for x in a)
        else (1, "2 failed\nFAILED %s\nFAILED tests/test_anderer.py::test_x\n"
              % runner.KNOWN_RED, "")))
    with pytest.raises(runner.StepFailed) as fehler:
        runner.step_vorpruefung(runner.Ctx(NAECHSTE))
    assert "test_anderer" in str(fehler.value)
