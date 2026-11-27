#!/usr/bin/env python3
"""Heilt doppelte Agenten-Eintraege: ein Keeper, ein Schluessel, keine Leiche.

Befund 26.11.2026: derselbe Harness meldete sich zweimal an, 25 Sekunden
auseinander. Das Register trug danach zwei Eintraege "Hermes Agent":

    hx7m2p  admin  private  3610 Reads / 16.888 Writes   (der berechtigte)
    674zw5  know   trusted     0 Reads /     11 Writes   (die Leiche)

Die Schluesseldatei trug den Schluessel des KLEINEREN Eintrags, die
Installation arbeitete also mit ``know`` statt ``admin``.

Dieses Werkzeug dreht das um, ohne Historie zu loeschen:

  1. Gruppen mit mehreren, nicht stillgelegten Eintraegen zum selben
     Harness (sonst zum selben normalisierten Namen) bilden.
  2. Keeper = der Eintrag mit der groesseren Historie (Reads + Writes),
     Gleichstand: aelteres ``connected_at``, dann die Kennung.
  3. Dem Keeper ueber ``service_auth.issue_key`` einen NEUEN Schluessel
     ausstellen. Der Klartext existiert genau einmal — er wird direkt in
     ``agent_keys/<harness>.key`` geschrieben (mode 0600, atomar) und NIE
     ausgegeben, auch nicht gekuerzt.
  4. Die uebrigen Eintraege stilllegen: ``retired_at`` + ``retired_into``,
     Schluesselhash entfernt. Der Dienst kennt beide Felder — ein
     stillgelegter Eintrag bekommt keinen Zugang und wird nie wieder
     uebernommen (``service_auth._is_retired``).

Aufruf:
    python3 scripts/fix_duplicate_agent.py            # Trockenlauf (Standard)
    python3 scripts/fix_duplicate_agent.py --apply    # wirklich heilen

``--registry`` und ``--key-dir`` zeigen auf eine Kopie des Registers bzw. ein
anderes Datenverzeichnis — fuer Trockenlaeufe und Tests.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class _Pfade:
    """Register-/Datenpfad umbiegen — und beim Verlassen WIEDERHERSTELLEN.

    26.11.2026: das Umbiegen ohne Rueckweg hat in der Testreihe die Pfade fuer
    den ganzen Prozess verstellt; spaetere Pruefungen liefen danach gegen ein
    geloeschtes Verzeichnis und wurden rot. Ein Kontextmanager, kein
    Seiten-Effekt auf Modulebene.
    """

    def __init__(self, registry: "str | None", key_dir: "str | None") -> None:
        self.registry = registry
        self.key_dir = key_dir
        self._saved = []

    def __enter__(self):
        from nexus_memory import agent_detect, service_auth

        if self.registry:
            self._saved.append((agent_detect, "_get_agents_registry_path",
                                agent_detect._get_agents_registry_path))
            agent_detect._get_agents_registry_path = lambda: Path(self.registry)
        if self.key_dir:
            self._saved.append((service_auth, "_data_dir", service_auth._data_dir))
            service_auth._data_dir = lambda: Path(self.key_dir)
        return self

    def __exit__(self, *exc):
        for module, name, original in reversed(self._saved):
            setattr(module, name, original)
        self._saved = []
        return False


def _history(entry: dict) -> int:
    def zahl(name):
        try:
            return int(entry.get(name) or 0)
        except (TypeError, ValueError):
            return 0
    return zahl("reads") + zahl("writes")


def _host_of(entry: dict) -> str:
    """Der Rechner, auf dem der Eintrag laeuft — Teil seiner Identitaet.

    Zwei Installationen desselben Harness auf ZWEI Rechnern sind gewollt
    (DSH auf Mac mini und Windows-PC, ``host_label`` "Mac Mini" / "Windows PC").
    Nur zwei Eintraege desselben Harness AUF DEMSELBEN Rechner sind eine
    Dublette. Ohne diese Unterscheidung haette das Werkzeug die Windows-
    Installation stillgelegt.
    """
    for feld in ("host_label", "host_type"):
        wert = entry.get(feld)
        if isinstance(wert, str) and wert.strip():
            return wert.strip().lower()
    return "?"


def _group_key(entry: dict) -> str:
    """Zusammengehoerig heisst: gleicher Harness (sonst Name) UND gleicher Host."""
    from nexus_memory import service_auth

    host = _host_of(entry)
    harness = entry.get(service_auth.HARNESS_FIELD)
    if isinstance(harness, str) and harness.strip():
        return "harness:%s | host:%s" % (harness.strip().lower(), host)
    return "name:%s | host:%s" % (service_auth._normalized_agent_name(entry.get("name")), host)


def find_groups(entries: list) -> list:
    """``[(group_key, keeper, [entries to retire])]`` — nur echte Dubletten."""
    from nexus_memory import service_auth

    groups: dict[str, list] = {}
    for entry in entries:
        if not isinstance(entry, dict) or service_auth._is_retired(entry):
            continue
        groups.setdefault(_group_key(entry), []).append(entry)

    out = []
    for key, members in sorted(groups.items()):
        if len(members) < 2:
            continue
        keeper = sorted(members, key=lambda e: (
            -_history(e), str(e.get("connected_at") or "~"), str(e.get("id") or "")))[0]
        retire = [e for e in members if e is not keeper]
        out.append((key, keeper, retire))
    return out


def _zeile(entry: dict) -> str:
    from nexus_memory import service_auth

    return "%-8s role=%-6s reads=%-6s writes=%-7s name=%r" % (
        entry.get("id"), entry.get("role") or "?",
        entry.get("reads", 0), entry.get("writes", 0), entry.get("name"))


def _write_key_atomic(path: Path, key: str) -> None:
    """0600, atomar — der Klartext existiert nur hier, nicht im Bericht."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(key)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Doppelte Agenten-Eintraege heilen (Trockenlauf ist der Standard).")
    ap.add_argument("--apply", action="store_true",
                    help="wirklich heilen: neuen Schluessel fuer den Keeper, "
                         "die Dubletten stilllegen")
    ap.add_argument("--registry", default="", help="Pfad zu einer agents.json (Kopie)")
    ap.add_argument("--key-dir", default="", help="Datenverzeichnis mit agent_keys/")
    args = ap.parse_args(argv)

    from nexus_memory import agent_detect, service_auth

    with _Pfade(args.registry or None, args.key_dir or None):
        return _run(args, agent_detect, service_auth)


def _run(args, agent_detect, service_auth) -> int:
    registry_path = agent_detect._get_agents_registry_path()
    if not registry_path.exists():
        print("FEHLER: Register nicht gefunden: %s" % registry_path, file=sys.stderr)
        return 1
    registry = agent_detect.load_agents_registry()
    entries = [e for e in registry.get("agents") or [] if isinstance(e, dict)]

    print("Register           : %s" % registry_path)
    print("Eintraege          : %d" % len(entries))

    groups = find_groups(entries)
    print("Doppelte Gruppen   : %d" % len(groups))
    if not groups:
        print("Nichts zu tun.")
        return 0

    plan = []
    for key, keeper, retire in groups:
        print("")
        print("Gruppe %s — %d Eintraege" % (key, 1 + len(retire)))
        print("  Keeper     : %s" % _zeile(keeper))
        for entry in retire:
            print("  stilllegen : %s" % _zeile(entry))
        harness = keeper.get(service_auth.HARNESS_FIELD) or ""
        if harness:
            ziel = service_auth.agent_key_path(harness)
            print("  Schluessel : %s (wird neu geschrieben, 0600)" % ziel)
        else:
            print("  Schluessel : (kein Harness am Keeper — keine Datei)")
        plan.append((keeper, retire, harness))

    if not args.apply:
        print("")
        print("Trockenlauf: nichts geschrieben. Mit --apply heilen.")
        return 0

    for keeper, retire, harness in plan:
        keeper_id = str(keeper.get("id") or "")
        key = service_auth.issue_key(keeper_id)
        if harness:
            _write_key_atomic(service_auth.agent_key_path(harness), key)
        for entry in retire:
            service_auth.retire_agent(str(entry.get("id") or ""), into=keeper_id)
        print("")
        print("Geheilt: Keeper %s hat einen neuen Schluessel, %d Eintrag/Eintraege "
              "stillgelegt (%s)"
              % (keeper_id, len(retire), ", ".join(str(e.get("id")) for e in retire)))

    # Nachpruefung am gespeicherten Register, nicht am Plan.
    registry = agent_detect.load_agents_registry()
    nach = [e for e in registry.get("agents") or [] if isinstance(e, dict)]
    offen = find_groups(nach)
    still = [e.get("id") for e in nach if service_auth._is_retired(e)]
    print("")
    print("Nachpruefung       : %d doppelte Gruppe(n) verblieben, stillgelegt: %s"
          % (len(offen), ", ".join(str(s) for s in still) or "keine"))
    for _keeper, _retire, harness in plan:
        if not harness:
            continue
        path = service_auth.agent_key_path(harness)
        mode = oct(path.stat().st_mode & 0o777) if path.exists() else "fehlt"
        print("Schluesseldatei    : %s (%s)" % (path, mode))
    return 0 if not offen else 1


if __name__ == "__main__":
    sys.exit(main())
