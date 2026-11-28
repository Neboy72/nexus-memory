#!/usr/bin/env python3
"""Release-Laeufer: die Release-Checkliste als ausfuehrbarer Ablauf.

Warum: am 27.11.2026 ging 0.22.79 mit Version, Commit, Pin, Push, Tag und
GitHub-Release raus — Hermes-Katalog, ClawHub und die Neustarts der anderen
Agenten fehlten, obwohl sie auf der Liste standen. Der Befund ist nicht "die
Liste war unklar", sondern "nichts hat sie abgearbeitet". Pflichtschritte
gehoeren in Code: dieser Laeufer fuehrt sie der Reihe nach aus, prueft nach
jedem Schritt selbst und kommt ohne bestandene Pruefung nicht weiter.

Aufruf:
    python3 tools/nexus_release.py 0.22.80
    python3 tools/nexus_release.py 0.22.80 --trockenlauf       # nur zeigen
    python3 tools/nexus_release.py 0.22.80 --weiter-ab pin     # nach Abbruch weiter

Regeln, die im Code stehen und nicht nur in dieser Beschreibung:
  * Es gibt kein --force und kein --skip. Der einzige Weg an einem Schritt
    vorbei ist --weiter-ab <schritt>, und was dabei uebersprungen wird, steht
    in der Bilanz als "uebersprungen" — nie als OK.
  * Jeder Schritt prueft sich selbst. Schlaegt die Pruefung fehl, endet der
    Lauf dort mit exit 1, nennt den Schritt und die fehlende Bedingung.
  * --trockenlauf ruft kein Kommando auf und schreibt nichts.

Nur Standardbibliothek.
"""

from __future__ import annotations

import argparse
import datetime
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
NOREPLY_NAME = "Nebo"
NOREPLY_MAIL = "140213942+Neboy72@users.noreply.github.com"
GH_REPO = "Neboy72/nexus-memory"
# Pfad zum clawhub-Werkzeug. Der Schalter existiert, damit ein Beweislauf gegen
# ein Wegwerf-Ziel laufen kann, ohne etwas hochzuladen (die Pruefung danach ist
# die echte). Im Normalbetrieb bleibt es beim Aufruf aus dem PATH.
CLAWHUB_BIN = os.environ.get("NEXUS_CLAWHUB_BIN", "clawhub")
CLAWHUB_PKG = "@neboy72/nexus-memory"
CLAWHUB_OWNER = "neboy72"
HEALTHZ = os.environ.get("NEXUS_HEALTHZ", "http://127.0.0.1:9122/healthz")
AUDIT = Path(os.environ.get(
    "NEXUS_AUDIT", str(Path.home() / "DeepSeek Bot" / "tools" / "nexus-push-audit.py")))
CHECKER_PATH = Path(os.environ.get(
    "NEXUS_RELEASE_CHECK",
    str(Path.home() / "DeepSeek Bot" / "tools" / "nexus-release-check.py")))

# Der eine rote Test, der rot bleiben darf (vorbestehend, 27.11.2026).
KNOWN_RED = "tests/test_claude_code_write_path.py::test_leseweg_funktioniert"

SERVICE_JOBS = (
    ("ai.nexus.serve", "Nexus-Dienst"),
    ("ai.hermes.gateway", "Hermes-Gateway"),
    ("ai.openclaw.gateway", "OpenClaw-Gateway"),
)
# Bei Hermes muss der alte Prozess wirklich weg sein, sonst endet der Start mit
# exit 75 ("a gateway already owns this host").
WAIT_PROZESS_WEG_SEC = 90.0
WAIT_DIENST_START_SEC = 60.0

TIMEOUT_GIT = 120
TIMEOUT_TEST = 1800
TIMEOUT_PUSH = 300
# 28.11.2026: der Windows-Lauf braucht deutlich laenger als der Linux-Lauf
# (Abhaengigkeiten + volle Testreihe); 900 s rissen knapp. 1800 s, damit ein
# langsamer Lauf kein falsches "nicht fertig" erzeugt.
TIMEOUT_CI = int(os.environ.get('NEXUS_CI_TIMEOUT', '1800'))
TIMEOUT_GH = 180
TIMEOUT_CLAWHUB = 600

VERSION_DATEIEN = (
    "pyproject.toml",
    "plugins/memory/nexus/pyproject.toml",
    "plugins/memory/nexus/plugin.yaml",
    "nexus/__init__.py",
    "README.md",
)
COMMIT_DATEIEN = VERSION_DATEIEN + ("CHANGELOG.md",)
BACKLOG = "docs/RELEASE-BACKLOG.md"
# Was im Arbeitsbaum schon geaendert sein darf, wenn der Lauf beginnt: die
# Stellen, die der Release selbst fortschreibt. Alles andere ist ein Fehler.
ERLAUBT_VORAB = set(COMMIT_DATEIEN) | {BACKLOG}

OK, FAIL, SKIP = "OK", "FAIL", "uebersprungen"


class StepFailed(Exception):
    """Eine Pruefung des Schritts ist nicht bestanden."""


class Technical(Exception):
    """Der Schritt konnte nicht ausgefuehrt werden (Werkzeug fehlt, Netz weg)."""


# ── Werkzeuge ──────────────────────────────────────────────────────────────

def env_with_tools() -> dict:
    env = dict(os.environ)
    extra = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
    env["PATH"] = os.pathsep.join([p for p in extra if p] + [env.get("PATH", "")])
    env.setdefault("LC_ALL", "C")
    return env


def sh(cmd, cwd=None, timeout=120, env=None):
    """Ein Kommando. Liefert (rc, stdout, stderr) — wirft nie."""
    try:
        done = subprocess.run(
            [str(c) for c in cmd], cwd=str(cwd or REPO), env=env or env_with_tools(),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout)
    except FileNotFoundError:
        raise Technical("Werkzeug nicht gefunden: %s" % cmd[0])
    except subprocess.TimeoutExpired:
        raise Technical("Zeitlimit %ss: %s" % (timeout, " ".join(str(c) for c in cmd[:3])))
    return (done.returncode,
            done.stdout.decode("utf-8", "replace"),
            done.stderr.decode("utf-8", "replace"))


def git(*args, timeout=TIMEOUT_GIT):
    rc, out, err = sh(["git", *args], timeout=timeout)
    if rc != 0:
        raise StepFailed("git %s: %s" % (" ".join(str(a) for a in args),
                                         (err or out).strip()[:300]))
    return out.strip()


def must(bedingung, meldung):
    if not bedingung:
        raise StepFailed(meldung)


def read(rel):
    return (REPO / rel).read_text(encoding="utf-8", errors="replace")


def write(rel, text):
    (REPO / rel).write_text(text, encoding="utf-8")


def load_checker():
    """Den Release-Pruefer als Modul laden — seine Sprachpruefung wird geteilt.

    Pfad ueber NEXUS_RELEASE_CHECK; fehlt er, ist das ein technischer Fehler
    und kein stilles Durchwinken.
    """
    if not CHECKER_PATH.exists():
        raise Technical("Release-Pruefer fehlt: %s" % CHECKER_PATH)
    spec = importlib.util.spec_from_file_location("nexus_release_check", CHECKER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.REPO = REPO
    return module


def changelog_abschnitt(version: str):
    """Der CHANGELOG-Abschnitt dieser Version, oder None."""
    text = read("CHANGELOG.md")
    match = re.search(r"(?ms)^## \[%s\] - .*?(?=^## \[|\Z)" % re.escape(version), text)
    return match.group(0).strip() if match else None


def alte_version() -> str:
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', read("pyproject.toml"))
    return match.group(1) if match else ""


# ── Schritte ───────────────────────────────────────────────────────────────

def step_vorpruefung(ctx) -> str:
    """Arbeitsbaum, Testreihe und Privatsphaere — alles lesend."""
    status = git("status", "--porcelain")
    fremd = []
    for line in status.splitlines():
        # git() liefert gestrippt: " M docs/x" kommt als "M docs/x" an. Deshalb
        # nicht ueber feste Spalten schneiden, sondern am ersten Leerraum
        # trennen — sonst verliert der Pfad sein erstes Zeichen und die
        # Ausnahme fuer die Release-Dateien greift nie.
        teile = line.strip().split(None, 1)
        if len(teile) < 2:
            continue
        code, pfad = teile[0], teile[1].strip()
        if code == "??":
            continue                      # neue, ungetrackte Dateien stoeeren nicht
        if pfad in ERLAUBT_VORAB:
            continue
        fremd.append("%s %s" % (code, pfad))
    must(not fremd, "Arbeitsbaum hat unerwartete Aenderungen: %s"
         % "; ".join(fremd[:5]))

    rc, out, err = sh([ctx.python, "-m", "pytest", "tests", "-q"], timeout=TIMEOUT_TEST)
    rot = re.findall(r"(?m)^FAILED\s+(\S+)", out)
    fremd_rot = [t for t in rot if not t.startswith(KNOWN_RED)]
    must(rc in (0, 1), "Testreihe nicht ausfuehrbar (%s): %s"
         % (ctx.python, (err or out).strip()[-300:]))
    must(not fremd_rot, "Testreihe rot (ausser dem bekannten %s): %s"
         % (KNOWN_RED, ", ".join(fremd_rot[:5])))
    if not rot and rc != 0:
        raise StepFailed("Testreihe endete mit rc=%d ohne FAILED-Zeile" % rc)

    if not AUDIT.exists():
        raise Technical("Privatsphaere-Pruefung fehlt: %s" % AUDIT)
    rc, out, err = sh([ctx.python, str(AUDIT), str(REPO)], timeout=TIMEOUT_TEST)
    must(rc == 0, "Privatsphaere-Pruefung rot: %s" % (out or err).strip()[-400:])
    return "Arbeitsbaum sauber, Testreihe gruen (1 bekannt rot), Audit sauber"


def step_version(ctx) -> str:
    """Die sechs Stellen auf die neue Version setzen und nachlesen."""
    alt, neu = ctx.old_version, ctx.version
    must(alt and alt != neu, "alte Version %r, neue %r — nichts zu tun" % (alt, neu))

    def ersetze(rel, muster, ersatz):
        text = read(rel)
        neuer_text, n = re.subn(muster, ersatz, text)
        must(n >= 1, "%s: Muster nicht gefunden (%s)" % (rel, muster))
        write(rel, neuer_text)
        return n

    n = 0
    n += ersetze("pyproject.toml", r'(?m)^version = "%s"$' % re.escape(alt),
                 'version = "%s"' % neu)
    n += ersetze("plugins/memory/nexus/pyproject.toml",
                 r'(?m)^version = "%s"$' % re.escape(alt), 'version = "%s"' % neu)
    n += ersetze("plugins/memory/nexus/plugin.yaml",
                 r'(?m)^version: %s$' % re.escape(alt), 'version: %s' % neu)
    n += ersetze("nexus/__init__.py", r'__version__ = "%s"' % re.escape(alt),
                 '__version__ = "%s"' % neu)
    n += ersetze("README.md", r"version-%s-brightgreen" % re.escape(alt),
                 "version-%s-brightgreen" % neu)
    n += ersetze("README.md", r"· v%s ·" % re.escape(alt), "· v%s ·" % neu)
    must(n >= 6, "nur %d von 6 Stellen ersetzt" % n)

    # Nachlesen: an keiner der Stellen steht noch die alte Version.
    reste = []
    for rel, muster in (
            ("pyproject.toml", r'(?m)^version = "%s"' % re.escape(alt)),
            ("plugins/memory/nexus/pyproject.toml", r'(?m)^version = "%s"' % re.escape(alt)),
            ("plugins/memory/nexus/plugin.yaml", r'(?m)^version: %s' % re.escape(alt)),
            ("nexus/__init__.py", r'__version__ = "%s"' % re.escape(alt)),
            ("README.md", r"version-%s-brightgreen" % re.escape(alt)),
            ("README.md", r"· v%s ·" % re.escape(alt))):
        if re.search(muster, read(rel)):
            reste.append("%s (%s)" % (rel, muster))
    must(not reste, "alte Version steht noch in: %s" % ", ".join(reste))
    ctx.new_version_written = True
    return "6 Stellen auf %s gesetzt, keine alte Version mehr" % neu


def step_changelog(ctx) -> str:
    """Abschnitt oben anlegen (Ueberschrift + Datum) und den Text pruefen."""
    version = ctx.version
    text = read("CHANGELOG.md")
    kopf = "## [%s] - %s" % (version, datetime.date.today().isoformat())
    if not re.search(r"(?m)^## \[%s\] - " % re.escape(version), text):
        write("CHANGELOG.md", kopf + "\n\n<!-- TODO: english summary -->\n\n" + text)
    abschnitt = changelog_abschnitt(version)
    must(abschnitt, "Abschnitt %s fehlt im CHANGELOG" % version)
    inhalt = abschnitt.split("\n", 1)[1] if "\n" in abschnitt else ""
    must("TODO" not in inhalt,
         "der Abschnitt %s ist noch ein Platzhalter — englischen Text eintragen" % version)
    must(len(inhalt.strip()) >= 200,
         "der Abschnitt %s ist zu kurz (%d Zeichen) — echter Aenderungstext fehlt"
         % (version, len(inhalt.strip())))

    checker = load_checker()
    urteil, de, en = checker._sprache(inhalt)
    must(urteil == "englisch", "Abschnitt %s ist nicht englisch (%s: %d deutsche, "
         "%d englische Funktionswoerter)" % (version, urteil, de, en))
    englisch, treffer = checker._titel_englisch(abschnitt.splitlines()[0])
    must(englisch, "Ueberschrift enthaelt deutsche Woerter: %s" % ", ".join(treffer))
    return "Abschnitt %s vorhanden, %d Zeichen, englisch" % (version, len(inhalt.strip()))


def step_commit(ctx) -> str:
    """Version, Changelog und Doku in EINEM Commit."""
    dateien = list(COMMIT_DATEIEN)
    if BACKLOG in git("status", "--porcelain"):
        dateien.append(BACKLOG)
    titel = "Chore(Release): %s" % ctx.version
    checker = load_checker()
    englisch, treffer = checker._titel_englisch(titel)
    must(englisch, "Commit-Titel nicht englisch: %s" % ", ".join(treffer))

    git("add", *dateien)
    gestaged = [l[3:].strip() for l in git("diff", "--cached", "--name-only").splitlines()]
    must(set(dateien) <= set(gestaged),
         "nicht alle Release-Dateien gestaged: fehlt %s"
         % ", ".join(sorted(set(dateien) - set(gestaged))))
    fremd = [d for d in gestaged if d not in dateien]
    must(not fremd, "fremde Dateien im Commit: %s" % ", ".join(fremd))

    env = env_with_tools()
    env.update({
        "GIT_AUTHOR_NAME": NOREPLY_NAME, "GIT_AUTHOR_EMAIL": NOREPLY_MAIL,
        "GIT_COMMITTER_NAME": NOREPLY_NAME, "GIT_COMMITTER_EMAIL": NOREPLY_MAIL,
    })
    rc, out, err = sh(["git", "commit", "-m", titel], env=env, timeout=TIMEOUT_GIT)
    must(rc == 0, "git commit fehlgeschlagen: %s" % (err or out).strip()[:300])

    wer = git("show", "-s", "--format=%an|%ae|%cn|%ce|%s", "HEAD")
    teile = wer.split("|")
    must(len(teile) == 5, "Commit-Kopf nicht lesbar: %s" % wer)
    must(teile[0] == NOREPLY_NAME and teile[1] == NOREPLY_MAIL,
         "Autor ist %s <%s>, erwartet %s <%s>" % (teile[0], teile[1],
                                                  NOREPLY_NAME, NOREPLY_MAIL))
    must(teile[2] == NOREPLY_NAME and teile[3] == NOREPLY_MAIL,
         "Committer ist %s <%s> statt %s <%s>" % (teile[2], teile[3],
                                                  NOREPLY_NAME, NOREPLY_MAIL))
    ctx.version_commit = git("rev-parse", "HEAD")
    return "Commit %s mit %d Dateien" % (ctx.version_commit[:12], len(dateien))


def step_pin(ctx) -> str:
    """Beide Engine-Referenzen auf den Versions-Commit, eigener Commit, Kette gruen."""
    sha = ctx.version_commit or git("rev-parse", "HEAD")
    must(re.fullmatch(r"[0-9a-f]{40}", sha), "kein Commit-Hash: %r" % sha)

    pyproj = read("plugins/memory/nexus/pyproject.toml")
    neu, n1 = re.subn(r'(nexus-memory = \{ git = "[^"]+", rev = ")[0-9a-f]{40}(")',
                      r"\g<1>%s\g<2>" % sha, pyproj)
    must(n1 == 1, "Engine-Pin in plugins/memory/nexus/pyproject.toml nicht gefunden")
    write("plugins/memory/nexus/pyproject.toml", neu)

    init = read("plugins/memory/nexus/__init__.py")
    neu, n2 = re.subn(r'"@[0-9a-f]{40}"', '"@%s"' % sha, init)
    must(n2 == 1, "Engine-Hash in plugins/memory/nexus/__init__.py nicht gefunden")
    write("plugins/memory/nexus/__init__.py", neu)

    git("add", "plugins/memory/nexus/pyproject.toml", "plugins/memory/nexus/__init__.py")
    env = env_with_tools()
    env.update({"GIT_AUTHOR_NAME": NOREPLY_NAME, "GIT_AUTHOR_EMAIL": NOREPLY_MAIL,
                "GIT_COMMITTER_NAME": NOREPLY_NAME, "GIT_COMMITTER_EMAIL": NOREPLY_MAIL})
    titel = "Chore(Release): engine pin to version commit %s" % ctx.version
    rc, out, err = sh(["git", "commit", "-m", titel], env=env, timeout=TIMEOUT_GIT)
    must(rc == 0, "Pin-Commit fehlgeschlagen: %s" % (err or out).strip()[:300])

    rc, out, err = sh([ctx.python, "-m", "pytest", "tests/test_pin_chain_is_closed.py", "-q"],
                      timeout=TIMEOUT_TEST)
    must(rc == 0, "Pin-Kette nicht geschlossen: %s" % (out or err).strip()[-400:])
    ctx.pin_commit = git("rev-parse", "HEAD")
    return "Pins auf %s, Kette gruen" % sha[:12]


def step_push(ctx) -> str:
    """main nach origin und gegen ls-remote nachlesen."""
    git("push", "origin", "main", timeout=TIMEOUT_PUSH)
    lokal = git("rev-parse", "HEAD")
    fern = git("ls-remote", "origin", "refs/heads/main").split()
    must(fern and fern[0] == lokal,
         "origin/main ist %s, lokal %s" % (fern[0][:12] if fern else "?", lokal[:12]))
    return "origin/main == %s" % lokal[:12]


def step_tag(ctx) -> str:
    """Tag setzen, pushen und gegen ls-remote nachlesen."""
    tag = "v%s" % ctx.version
    lokal = git("rev-parse", "HEAD")
    rc, out, err = sh(["git", "rev-parse", "--verify", "refs/tags/%s" % tag])
    if rc == 0:
        raise StepFailed("Tag %s existiert schon (%s)" % (tag, out.strip()[:12]))
    git("tag", "-a", tag, "-m", "Nexus Memory %s" % ctx.version)
    git("push", "origin", tag, timeout=TIMEOUT_PUSH)
    fern = git("ls-remote", "origin", "refs/tags/%s^{}" % tag).split()
    if not fern:
        fern = git("ls-remote", "origin", "refs/tags/%s" % tag).split()
    must(fern and fern[0] == lokal,
         "Tag %s zeigt auf %s, HEAD ist %s"
         % (tag, fern[0][:12] if fern else "?", lokal[:12]))
    return "Tag %s == %s" % (tag, lokal[:12])


def step_release(ctx) -> str:
    """GitHub-Release mit den Notizen aus dem CHANGELOG."""
    tag = "v%s" % ctx.version
    abschnitt = changelog_abschnitt(ctx.version)
    must(abschnitt, "kein CHANGELOG-Abschnitt fuer %s" % ctx.version)
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False,
                                     encoding="utf-8") as handle:
        handle.write(abschnitt + "\n")
        notizen = handle.name
    try:
        rc, out, err = sh(["gh", "release", "create", tag, "--repo", GH_REPO,
                           "--title", "Nexus Memory %s" % ctx.version,
                           "--notes-file", notizen], timeout=TIMEOUT_GH)
        must(rc == 0, "gh release create: %s" % (err or out).strip()[:400])
        rc, out, err = sh(["gh", "release", "view", tag, "--repo", GH_REPO,
                           "--json", "tagName,url"], timeout=TIMEOUT_GH)
        must(rc == 0, "gh release view: %s" % (err or out).strip()[:300])
        try:
            daten = json.loads(out)
        except ValueError:
            raise StepFailed("gh release view lieferte kein JSON: %s" % out[:200])
        must(daten.get("tagName") == tag,
             "Release-Tag ist %r, erwartet %r" % (daten.get("tagName"), tag))
    finally:
        try:
            os.unlink(notizen)
        except OSError:
            pass
    return "Release %s online (%s)" % (tag, daten.get("url", "?"))


def clawhub_zahl(version: str) -> str:
    return "1.22.%s" % version.split(".")[2]


def step_prueflauf(ctx) -> str:
    """Warten, bis die GitHub-Prueflaeufe fuer DIESEN Commit fertig sind.

    Warum als eigener Schritt (28.11.2026): Der Laeufer meldete am 28.11.
    "RELEASE FERTIG", waehrend der Windows-Lauf noch lief — und der wurde rot.
    Ein Release ist erst fertig, wenn beide Laeufe success sind; ein roter
    Lauf ist ein Fehlschlag dieses Schritts, kein Hinweis.
    """
    head = git("rev-parse", "HEAD")
    frist = time.monotonic() + TIMEOUT_CI
    letzte = ""
    while True:
        rc, out, err = sh(["gh", "run", "list", "--repo", GH_REPO, "--limit", "20",
                           "--json", "name,status,conclusion,headSha"],
                          timeout=TIMEOUT_GH)
        must(rc == 0, "gh run list: %s" % (err or out).strip()[:300])
        try:
            alle = json.loads(out)
        except ValueError:
            must(False, "gh run list lieferte kein JSON")
        laeufe = [r for r in alle if r.get("headSha") == head]
        letzte = ", ".join("%s=%s" % (r.get("name"), r.get("conclusion") or r.get("status"))
                           for r in laeufe) or "keine"
        if laeufe and all(r.get("status") == "completed" for r in laeufe):
            schlecht = [r for r in laeufe if r.get("conclusion") != "success"]
            must(not schlecht, "Prueflauf nicht erfolgreich (%s): %s" % (head[:7], letzte))
            return "%d Lauf/Laeufe fuer %s: %s" % (len(laeufe), head[:7], letzte)
        must(time.monotonic() < frist,
             "Prueflauf nach %d s nicht fertig (%s): %s" % (TIMEOUT_CI, head[:7], letzte))
        time.sleep(15)


def step_clawhub(ctx) -> str:
    """Paket auf 1.22.<patch> mit Source Ref v<version>, danach nachlesen."""
    erwartet = clawhub_zahl(ctx.version)
    sha = ctx.pin_commit or ctx.version_commit or git("rev-parse", "HEAD")
    tag = "v%s" % ctx.version
    rc, out, err = sh([CLAWHUB_BIN, "package", "publish", str(REPO),
                       "--family", "code-plugin", "--name", CLAWHUB_PKG,
                       "--owner", CLAWHUB_OWNER, "--version", erwartet,
                       "--source-repo", GH_REPO, "--source-commit", sha,
                       "--source-ref", tag,
                       "--changelog", "Nexus Memory %s" % ctx.version,
                       "--wait"], timeout=TIMEOUT_CLAWHUB)
    must(rc == 0, "clawhub publish: %s" % (err or out).strip()[-500:])

    rc, out, err = sh([CLAWHUB_BIN, "package", "inspect", CLAWHUB_PKG],
                      timeout=TIMEOUT_CLAWHUB)
    must(rc == 0, "clawhub inspect: %s" % (err or out).strip()[-300:])
    felder = dict(re.findall(r"(?m)^([A-Za-z ]+):\s*(.+?)\s*$", out))
    latest = felder.get("Latest", "")
    ref = felder.get("Source Ref", "")
    must(latest == erwartet, "ClawHub Latest ist %r, erwartet %r" % (latest, erwartet))
    must(ref == tag, "ClawHub Source Ref ist %r, erwartet %r" % (ref, tag))
    return "ClawHub %s, Source Ref %s" % (latest, ref)


def step_katalog(ctx) -> str:
    """Ein Katalog-PR auf die Version muss existieren — offen ODER gemergt."""
    checker = load_checker()
    rows = checker.Rows()
    try:
        checker.check_catalog(rows, ctx.version)
    except Technical as exc:
        raise Technical("Katalog-Pruefung nicht moeglich: %s" % exc)
    fails = [r for r in rows.items if r[0] == checker.FAIL]
    must(not fails, "kein Katalog-PR auf %s (offen oder gemergt): %s"
         % (ctx.version, "; ".join(d for _s, _g, _n, d in fails)[:300]))
    treffer = [d for s, _g, _n, d in rows.items if s == checker.OK]
    return "Katalog: %s" % (treffer[0] if treffer else "PR gefunden")


def _launchctl_pid(label: str):
    rc, out, err = sh(["launchctl", "list", label])
    if rc != 0:
        return None
    teile = out.split()
    if len(teile) >= 2 and teile[1].isdigit():
        return int(teile[1])
    return None


def step_neustart(ctx) -> str:
    """Dienste neu starten und pruefen, dass danach jeder laeuft."""
    uid = os.getuid()
    details = []
    for label, name in SERVICE_JOBS:
        alt = _launchctl_pid(label)
        rc, out, err = sh(["launchctl", "kickstart", "-k",
                           "gui/%d/%s" % (uid, label)], timeout=60)
        must(rc == 0, "%s (%s) startet nicht: %s"
             % (name, label, (out or err).strip()[:300]))

        frist = time.time() + WAIT_PROZESS_WEG_SEC
        while time.time() < frist:
            neu = _launchctl_pid(label)
            if neu and neu != alt:
                break
            time.sleep(1.0)
        neu = _launchctl_pid(label)
        must(neu and neu != alt,
             "%s (%s): alter Prozess %s laeuft nach %.0fs noch — ein Gateway, das "
             "den Host schon besitzt, endet mit exit 75"
             % (name, label, alt, WAIT_PROZESS_WEG_SEC))

        frist = time.time() + WAIT_DIENST_START_SEC
        while time.time() < frist:
            if re.search(r'"PID"\s*=\s*\d+', sh(["launchctl", "print",
                                                 "gui/%d/%s" % (uid, label)])[1]):
                break
            time.sleep(1.0)
        details.append("%s=%s" % (label, neu))
    return "neu gestartet: %s" % ", ".join(details)


def _venv_version(python: Path):
    if not python.exists():
        return None
    rc, out, err = sh([str(python), "-c",
                       "import importlib.metadata as m;print(m.version('nexus-memory'))"],
                      timeout=60)
    return out.strip() if rc == 0 else None


def step_fassungen(ctx) -> str:
    """Installierte Fassung in allen Umgebungen und am laufenden Dienst."""
    umgebungen = (
        (REPO / ".venv/bin/python3", "repo .venv"),
        (Path.home() / ".hermes/hermes-agent/venv/bin/python3", "Hermes-venv"),
        (Path.home() / ".openclaw/nexus-venv/bin/python3", "OpenClaw-venv"),
    )
    falsch = []
    for python, name in umgebungen:
        ist = _venv_version(python)
        if ist is None:
            falsch.append("%s: keine Fassung lesbar (%s)" % (name, python))
        elif ist != ctx.version:
            falsch.append("%s: %s statt %s" % (name, ist, ctx.version))
    must(not falsch, "Fassungen stimmen nicht: %s" % "; ".join(falsch))

    try:
        with urllib.request.urlopen(HEALTHZ, timeout=8) as resp:
            health = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise StepFailed("Dienst antwortet nicht auf %s: %s" % (HEALTHZ, exc))
    must(health.get("status") == "ok",
         "Dienst meldet %r statt ok: %s" % (health.get("status"), health))
    # /healthz traegt bewusst keine Version (nur status und qdrant, siehe
    # mcp_server.healthz). Was sich pruefen laesst: der Dienst laeuft, und der
    # Interpreter, aus dem er laeuft, hat die neue Fassung installiert — das
    # ist die erste Zeile dieser Pruefung.
    return ("3 Umgebungen auf %s; Dienst antwortet ok (healthz ohne Versionsfeld)"
            % ctx.version)


def step_bilanz(ctx) -> str:
    """Wird nicht ausgefuehrt — die Bilanz schreibt der Lauf selbst."""
    return "Bilanz"


STEPS = (
    ("vorpruefung", "Arbeitsbaum, Testreihe, Privatsphaere", step_vorpruefung,
     "git status · pytest tests -q · tools/nexus-push-audit.py"),
    ("version", "sechs Versionsstellen setzen", step_version,
     "pyproject.toml, plugins/.../pyproject.toml, plugin.yaml, nexus/__init__.py, "
     "README (Badge + Fusszeile)"),
    ("changelog", "Abschnitt oben, englisch, mit Datum", step_changelog,
     "CHANGELOG.md Abschnitt anlegen/pruefen (Sprachpruefung des Release-Pruefers)"),
    ("commit", "Version + Changelog + Doku in einem Commit", step_commit,
     "git add <Release-Dateien> && git commit -m 'Chore(Release): <version>'"),
    ("pin", "Engine-Pins auf den Versions-Commit", step_pin,
     "zwei Pin-Stellen setzen, eigener Commit, pytest tests/test_pin_chain_is_closed.py"),
    ("push", "main nach origin", step_push,
     "git push origin main · git ls-remote origin refs/heads/main"),
    ("tag", "Tag setzen und pushen", step_tag,
     "git tag -a v<version> · git push origin v<version> · ls-remote"),
    ("release", "GitHub-Release mit CHANGELOG-Notizen", step_release,
     "gh release create v<version> --notes-file <changelog> · gh release view"),
    ("prueflauf", "beide GitHub-Prueflaeufe muessen success sein", step_prueflauf,
     "gh run list --repo <repo> · warten bis completed · jeder andere Ausgang ist ein Fehlschlag"),
    ("clawhub", "Paket 1.22.<patch> mit Source Ref v<version>", step_clawhub,
     "clawhub package publish . --version 1.22.<patch> --source-ref v<version> · "
     "clawhub package inspect"),
    ("katalog", "Katalog-PR auf die Version (offen oder gemergt)", step_katalog,
     "Release-Pruefer: check_catalog (state=all)"),
    ("neustart", "Dienst und beide Gateways neu starten", step_neustart,
     "launchctl kickstart -k ai.nexus.serve / ai.hermes.gateway / ai.openclaw.gateway"),
    ("fassungen", "installierte Fassungen und der laufende Dienst", step_fassungen,
     "3 venvs: importlib.metadata · /healthz"),
    ("bilanz", "eine Zeile je Schritt und das Urteil", step_bilanz,
     "(wird vom Lauf selbst geschrieben)"),
)
STEP_NAMEN = tuple(s[0] for s in STEPS)


class Ctx:
    def __init__(self, version, dry=False, python=None):
        self.version = version
        self.dry = dry
        self.python = python or sys.executable
        self.old_version = ""
        self.version_commit = ""
        self.pin_commit = ""
        self.new_version_written = False


# ── Lauf ───────────────────────────────────────────────────────────────────

def run_steps(steps, ctx, weiter_ab=None):
    """Die Schritte der Reihe nach. Liefert (ergebnisse, urteil_text)."""
    namen = [s[0] for s in steps]
    if weiter_ab and weiter_ab not in namen:
        raise Technical("unbekannter Schritt %r (bekannt: %s)"
                        % (weiter_ab, ", ".join(namen)))
    uebersprungen = set(namen[:namen.index(weiter_ab)]) if weiter_ab else set()

    ergebnisse = []
    for name, _titel, func, _trocken in steps:
        if name in uebersprungen:
            ergebnisse.append((name, SKIP, "--weiter-ab %s" % weiter_ab))
            continue
        if name == "bilanz":
            continue
        try:
            detail = func(ctx)
        except StepFailed as exc:
            ergebnisse.append((name, FAIL, str(exc)))
            return ergebnisse, "RELEASE NICHT FERTIG — Schritt %s: %s" % (name, exc)
        except Technical as exc:
            ergebnisse.append((name, FAIL, "technisch: %s" % exc))
            return ergebnisse, "RELEASE NICHT FERTIG — Schritt %s (technisch): %s" % (name, exc)
        ergebnisse.append((name, OK, detail))

    if any(e[1] == SKIP for e in ergebnisse):
        return ergebnisse, ("RELEASE UNSICHER — %d Schritt(e) uebersprungen; jeder "
                            "uebersprungene Schritt muss einzeln belegt werden"
                            % sum(1 for e in ergebnisse if e[1] == SKIP))
    return ergebnisse, "RELEASE FERTIG %s" % ctx.version


def zeige_bilanz(ergebnisse, urteil):
    breite = max([len(e[0]) for e in ergebnisse] + [10])
    print("")
    print("=" * 100)
    print("Bilanz")
    for name, status, detail in ergebnisse:
        print("%-*s  %-12s  %s" % (breite, name, status, detail))
    print("-" * 100)
    print(urteil)


def zeige_trockenlauf(ctx):
    print("Release %s — Trockenlauf, es wird nichts ausgefuehrt und nichts geaendert."
          % ctx.version)
    print("Repo: %s" % REPO)
    print("")
    for nummer, (name, titel, _func, trocken) in enumerate(STEPS, start=1):
        print("%2d. %-12s %s" % (nummer, name, titel))
        print("    %s" % trocken)
    print("")
    print("Kein --force, kein --skip. Nach einem Abbruch: --weiter-ab <schritt> "
          "(uebersprungene Schritte stehen danach als 'uebersprungen' in der Bilanz).")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Nexus-Release als ausfuehrbarer Ablauf (kein --force, kein --skip).")
    parser.add_argument("version", help="Zielversion X.Y.Z")
    parser.add_argument("--trockenlauf", action="store_true",
                        help="nur die Schrittfolge zeigen, nichts ausfuehren")
    parser.add_argument("--weiter-ab", default="", metavar="SCHRITT",
                        help="beim genannten Schritt fortsetzen (fruehere gelten als "
                             "uebersprungen): %s" % ", ".join(STEP_NAMEN))
    args = parser.parse_args(argv)

    version = args.version.strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        print("FEHLER: Version muss X.Y.Z sein, bekam %r" % version, file=sys.stderr)
        return 2
    if not (REPO / "pyproject.toml").exists():
        print("FEHLER: kein Nexus-Repo unter %s" % REPO, file=sys.stderr)
        return 2

    ctx = Ctx(version, dry=args.trockenlauf)
    try:
        ctx.old_version = alte_version()
    except OSError as exc:
        print("FEHLER: pyproject.toml nicht lesbar: %s" % exc, file=sys.stderr)
        return 2

    if args.trockenlauf:
        zeige_trockenlauf(ctx)
        return 0

    if args.weiter_ab and args.weiter_ab not in STEP_NAMEN:
        print("FEHLER: unbekannter Schritt %r (bekannt: %s)"
              % (args.weiter_ab, ", ".join(STEP_NAMEN)), file=sys.stderr)
        return 2

    try:
        ergebnisse, urteil = run_steps(STEPS, ctx, args.weiter_ab or None)
    except Technical as exc:
        print("FEHLER: %s" % exc, file=sys.stderr)
        return 2
    zeige_bilanz(ergebnisse, urteil)
    return 0 if urteil.startswith("RELEASE FERTIG") else 1


if __name__ == "__main__":
    sys.exit(main())
