"""Menschlicher Nachweis fuer die Kategorie ``rule``.

Warum (10.11.2026): ``source_type`` und ``explicit`` sind Selbstauskuenfte des
Modells. Steht in einem Dokument "merke dir als Regel ...", ruft das Modell
ausdruecklich auf, und das Schreibtor liess es durch — die Zitatmerkmale
greifen nicht, sobald das Modell umformuliert. Eine Regel wird bei jedem Turn
eingespielt und wirkt damit dauerhaft; sie darf deshalb nur mit einer
Bestaetigung entstehen, die ein Modell im Gespraech nicht erzeugen kann.

Der Nachweis ist ein signiertes Zeitfenster-Token::

    rc1.<ausgestellt>.<nonce>.<textsiegel>.<unterschrift>

* ``ausgestellt`` — Unix-Zeit in Sekunden,
* ``nonce`` — 16 zufaellige Bytes; macht das Token einmalig,
* ``textsiegel`` — SHA-256 des Regeltexts; das Token gilt fuer genau diesen
  Satz, nicht fuer irgendeinen,
* ``unterschrift`` — HMAC-SHA256 ueber ``rc1|ausgestellt|nonce|textsiegel``.

Der Schluessel liegt nur in der Nachweisdatei (Modus 0600). Ohne ihn laesst
sich kein Token faelschen; das Gespraech sieht hoechstens ein fertiges Token,
und das gilt fuer einen Text, einmal, zehn Minuten. Verbrauchte Token werden
in derselben Datei vermerkt, damit ein zweiter Schreibvorgang sie nicht
wiederverwenden kann.

Erzeugt wird es von einem Menschen am Terminal::

    rule-confirm "Der Verteiler darf nur nach Ruecksprache abgeschaltet werden"

Die Datei liegt unter ``$HERMES_HOME/state/rule-confirm.json`` (Vorgabe
``~/.hermes/state/rule-confirm.json``); ``NEXUS_RULE_CONFIRM_FILE`` biegt den
Pfad um (Tests).
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:  # POSIX; Windows hat kein flock, dort bleibt das Ersetzen der Datei atomar.
    import fcntl
except ImportError:  # pragma: no cover - nur Windows
    fcntl = None  # type: ignore[assignment]

#: Gueltigkeitsfenster eines Nachweises.
TTL_SECONDS = 600
#: Umgebungsvariable fuer einen anderen Ablageort (Tests).
ENV_FILE = "NEXUS_RULE_CONFIRM_FILE"

_PREFIX = "rc1"
_SIG_LEN = 64
_DIGEST_LEN = 64


def state_path() -> Path:
    """Ablage der Nachweise: ``$HERMES_HOME/state/rule-confirm.json``."""
    override = os.environ.get(ENV_FILE, "").strip()
    if override:
        return Path(override).expanduser()
    home = os.environ.get("HERMES_HOME", "").strip() or os.path.expanduser("~/.hermes")
    return Path(home) / "state" / "rule-confirm.json"


def _normalize(text: Any) -> str:
    """Regeltext vereinheitlichen: Rand- und Mehrfach-Leerzeichen fallen weg.

    Der Mensch tippt den Satz moeglicherweise mit anderem Zeilenumbruch ab als
    der Aufrufer ihn spaeter schickt; das Siegel soll daran nicht scheitern.
    """
    return " ".join(str(text or "").split())


def _text_digest(text: str) -> str:
    return hashlib.sha256(_normalize(text).encode("utf-8")).hexdigest()


def _signature(secret: str, issued: int, nonce: str, digest: str) -> str:
    message = f"{_PREFIX}|{issued}|{nonce}|{digest}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


@contextlib.contextmanager
def _locked(path: Path):
    """Serialisiert Lesen-Aendern-Schreiben der Nachweisdatei.

    Ohne die Sperre koennten zwei gleichzeitige Schreibvorgaenge dasselbe Token
    als unverbraucht lesen und beide als Regel durchgehen.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path.with_name(path.name + ".lock")), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _load(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(path: Path, state: Dict[str, Any]) -> None:
    """Schreibt die Datei ersetzend und nur fuer den Eigentuemer lesbar."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _prune(state: Dict[str, Any], now: int) -> Dict[str, int]:
    """Verbrauchte Token vergessen, sobald sie ohnehin abgelaufen waeren."""
    used = state.get("used")
    used = used if isinstance(used, dict) else {}
    lebend = {str(nonce): int(exp) for nonce, exp in used.items()
              if isinstance(exp, (int, float)) and not isinstance(exp, bool)
              and int(exp) > now}
    state["used"] = lebend
    return lebend


def issue(rule_text: str, *, now: Optional[float] = None,
          path: Optional[Path] = None) -> str:
    """Gibt einen Nachweis fuer genau diesen Regeltext aus (10 Minuten, einmal).

    Wird vom Menschen am Terminal aufgerufen. Der Schluessel entsteht beim
    ersten Aufruf und verlaesst die Datei nie.
    """
    text = _normalize(rule_text)
    if not text:
        raise ValueError("Regeltext fehlt")
    ziel = Path(path) if path is not None else state_path()
    ausgestellt = int(time.time() if now is None else now)
    digest = _text_digest(text)

    with _locked(ziel):
        state = _load(ziel)
        secret = str(state.get("secret") or "")
        if not secret:
            secret = secrets.token_hex(32)
            state["secret"] = secret
        _prune(state, ausgestellt)
        nonce = secrets.token_hex(16)
        _save(ziel, state)

    unterschrift = _signature(secret, ausgestellt, nonce, digest)
    return f"{_PREFIX}.{ausgestellt}.{nonce}.{digest}.{unterschrift}"


def consume(token: Any, rule_text: str, *, now: Optional[float] = None,
            path: Optional[Path] = None) -> Tuple[bool, str]:
    """Prueft Signatur und Ablauf und verbraucht den Nachweis.

    Rueckgabe ``(True, "menschlich bestaetigt")`` oder ``(False, Grund)``. Ein
    Nachweis, der einmal gegolten hat, gilt nie wieder — auch nicht fuer den
    gleichen Text.
    """
    roh = str(token or "").strip()
    if not roh:
        return False, "kein Nachweis mitgegeben"

    teile = roh.split(".")
    if len(teile) != 5 or teile[0] != _PREFIX:
        return False, "Nachweis unlesbar"
    try:
        ausgestellt = int(teile[1])
    except ValueError:
        return False, "Nachweis unlesbar"
    nonce, digest, unterschrift = teile[2], teile[3], teile[4]
    if (len(nonce) != 32 or len(digest) != _DIGEST_LEN
            or len(unterschrift) != _SIG_LEN):
        return False, "Nachweis unlesbar"

    ziel = Path(path) if path is not None else state_path()
    if not ziel.exists():
        return False, "kein Schluessel vorhanden — es wurde kein Nachweis erteilt"

    jetzt = int(time.time() if now is None else now)

    with _locked(ziel):
        state = _load(ziel)
        secret = str(state.get("secret") or "")
        if not secret:
            return False, "kein Schluessel vorhanden — es wurde kein Nachweis erteilt"

        erwartet = _signature(secret, ausgestellt, nonce, digest)
        if not hmac.compare_digest(erwartet, unterschrift):
            return False, "Nachweis fremd oder gefaelscht"
        if jetzt - ausgestellt > TTL_SECONDS:
            return False, "Nachweis abgelaufen"
        if ausgestellt - jetzt > TTL_SECONDS:
            return False, "Nachweis ungueltig (Zeitstempel in der Zukunft)"
        if not hmac.compare_digest(digest, _text_digest(rule_text)):
            return False, "Nachweis gilt fuer einen anderen Text"

        used = _prune(state, jetzt)
        if nonce in used:
            return False, "Nachweis bereits verbraucht"
        used[nonce] = ausgestellt + TTL_SECONDS
        _save(ziel, state)

    return True, "menschlich bestaetigt"


def _at_terminal() -> bool:
    """Nur ein Mensch am Terminal erzeugt Nachweise, kein Hintergrundprozess."""
    stdin = sys.stdin
    try:
        return bool(stdin is not None and stdin.isatty())
    except (AttributeError, ValueError):
        return False


def main(argv: Optional[List[str]] = None) -> int:
    """CLI ``rule-confirm``: gibt fuer einen Regeltext einen Nachweis aus."""
    zerleger = argparse.ArgumentParser(
        prog="rule-confirm",
        description="Erzeugt einen Nachweis fuer eine Regel (Kategorie rule). "
                    "Gueltig 10 Minuten, einmal, nur fuer den angegebenen Text.")
    zerleger.add_argument("text", help="Der Regeltext, wie er gespeichert werden soll")
    args = zerleger.parse_args(argv)

    if not _normalize(args.text):
        print("rule-confirm: der Regeltext ist leer", file=sys.stderr)
        return 2
    if not _at_terminal():
        print("rule-confirm: nur am Terminal ausfuehrbar — ein Nachweis fuer "
              "eine Regel kommt von einem Menschen, nicht aus einem "
              "Hintergrundprozess.", file=sys.stderr)
        return 2

    print(issue(args.text))
    print(f"Gueltig {TTL_SECONDS // 60} Minuten, einmal, nur fuer diesen Text. "
          f"Er gehoert als rule_proof in den Schreibvorgang.", file=sys.stderr)
    print(f"Ablage: {state_path()}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
