"""Kein Testlauf legt eine Sicherung im echten Heimatordner ab (22.11.2026).

Befund: ``MemoryStore.__init__`` und ``NexusMemoryProvider.initialize`` starten
einen Hintergrund-Thread, der nach 60 s ein Vollbackup schreibt. Der Pfad war
fest auf ``~/.nexus-memory/backups`` verdrahtet — ``NEXUS_DATA_DIR`` wurde
ignoriert. Ein Testabend legte dort sieben Dateien mit je 55 MB ab, und gegen
Attrappen erschien die Warnung ``Auto-backup failed: not enough values to
unpack (expected 2, got 0)``, ohne dass ein Test rot wurde.

Geprueft wird hier:

- der Sicherungsordner folgt ``NEXUS_BACKUP_DIR``, sonst ``NEXUS_DATA_DIR``,
- ``NEXUS_AUTO_BACKUP=0`` haelt den Thread ganz aus dem Prozess
  (die Kopie im Hermes-Plugin genauso, sie hat ihre eigene Schleife),
- ein Test, der beide Sicherungen wirklich schreibt, laesst den echten Ordner
  unveraendert. Die Sitzungswache
  (``tests/conftest.py::kein_echtes_backup``) prueft dasselbe fuer den ganzen
  Lauf.
"""
import importlib.util
import os
import stat
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import nexus_memory.mcp_server as mcp

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugins" / "memory" / "nexus" / "__init__.py"


# ── Hilfen ───────────────────────────────────────────────────────────────────

def _fester_client(punkte=None):
    """Qdrant-Ersatz: eine Seite, dann Schluss — wie ``scroll`` es liefert."""
    client = MagicMock()
    client.scroll.return_value = (
        [SimpleNamespace(id=str(i), payload={"content": f"fakt {i}"}, vector=None)
         for i in range(len(punkte or [1]))],
        None,
    )
    return client


def _leerer_store():
    store = object.__new__(mcp.MemoryStore)
    store.client = _fester_client()
    return store


def _plugin_modul():
    spec = importlib.util.spec_from_file_location("nexus_plugin_backup_isolation", PLUGIN)
    modul = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = modul
    try:
        spec.loader.exec_module(modul)
    finally:
        sys.modules.pop(spec.name, None)
    return modul


def _freier_provider(modul):
    """Provider ohne ``initialize()`` — nur die Felder, die ``_do_backup`` liest."""
    provider = object.__new__(modul.NexusMemoryProvider)
    provider._qdrant = _fester_client()
    provider._collection = "test-collection-backup"
    provider._write_stop = threading.Event()
    provider._last_backup_time = 0.0
    provider._last_backup_path = ""
    return provider


def _echter_ordner() -> Path:
    return Path(os.path.expanduser("~")) / ".nexus-memory" / "backups"


def _namen(ordner: Path) -> set:
    try:
        return set(os.listdir(ordner))
    except OSError:
        return set()


# ── Der Ordner folgt der Umgebung ────────────────────────────────────────────

class TestBackupDir:
    def test_dienst_sichert_in_das_datenverzeichnis(self, monkeypatch):
        """Ohne ``NEXUS_BACKUP_DIR`` gilt ``NEXUS_DATA_DIR`` (die autouse-Fixture)."""
        monkeypatch.delenv("NEXUS_BACKUP_DIR", raising=False)
        pfad = Path(_leerer_store()._do_backup())

        assert pfad.parent == Path(os.environ["NEXUS_DATA_DIR"]) / "backups"
        assert pfad.name.startswith("nexus-backup-")
        assert pfad.exists()

    def test_backup_dir_gewinnt_gegen_das_datenverzeichnis(self, tmp_path, monkeypatch):
        ziel = tmp_path / "eigener-sicherungsordner"
        monkeypatch.setenv("NEXUS_BACKUP_DIR", str(ziel))
        pfad = Path(_leerer_store()._do_backup())

        assert pfad.parent == ziel
        assert pfad.exists()

    def test_plugin_sichert_in_das_datenverzeichnis(self, monkeypatch):
        """Die Kopie im Hermes-Plugin folgt derselben Regel (eigene Schleife)."""
        monkeypatch.delenv("NEXUS_BACKUP_DIR", raising=False)
        pfad = Path(_freier_provider(_plugin_modul())._do_backup())

        assert pfad.parent == Path(os.environ["NEXUS_DATA_DIR"]) / "backups"
        assert pfad.exists()


# ── Der Schalter haelt den Thread aus dem Prozess ────────────────────────────

class TestAutoBackupSwitch:
    def _starten(self, monkeypatch):
        gestartet = []
        monkeypatch.setattr(threading.Thread, "start",
                            lambda self: gestartet.append(self.name))
        return gestartet

    def test_dienst_startet_keinen_thread_wenn_aus(self, monkeypatch):
        gestartet = self._starten(monkeypatch)
        store = object.__new__(mcp.MemoryStore)

        monkeypatch.setenv("NEXUS_AUTO_BACKUP", "0")
        store._start_auto_backup()
        assert gestartet == []

        monkeypatch.setenv("NEXUS_AUTO_BACKUP", "1")
        store._start_auto_backup()
        assert gestartet == ["nexus-backup"]

    def test_plugin_startet_keinen_thread_wenn_aus(self, monkeypatch):
        gestartet = self._starten(monkeypatch)
        provider = object.__new__(_plugin_modul().NexusMemoryProvider)
        provider._write_stop = threading.Event()

        monkeypatch.setenv("NEXUS_AUTO_BACKUP", "0")
        provider._start_auto_backup()
        assert gestartet == []

        monkeypatch.setenv("NEXUS_AUTO_BACKUP", "1")
        provider._start_auto_backup()
        assert gestartet == ["nexus-backup"]


# ── Der direkte Beweis: der echte Ordner bleibt unberuehrt ───────────────────

def test_beide_sicherungen_lassen_den_echten_ordner_unberuehrt(monkeypatch):
    """Beide Wege schreiben wirklich — und keine Datei landet im echten Ordner."""
    monkeypatch.delenv("NEXUS_BACKUP_DIR", raising=False)
    echt = _echter_ordner()
    vorher = _namen(echt)

    dienst = Path(_leerer_store()._do_backup())
    plugin = Path(_freier_provider(_plugin_modul())._do_backup())

    assert dienst.parent == plugin.parent == Path(os.environ["NEXUS_DATA_DIR"]) / "backups"
    assert not _namen(echt) - vorher, (
        f"im echten Ordner {echt} ist eine Datei dazugekommen: "
        f"{sorted(_namen(echt) - vorher)}"
    )


# ── Ein echter Fehlschlag bleibt laut ────────────────────────────────────────

def test_ein_gescheiterter_backup_wird_weiter_gemeldet():
    """Der Schalter beruhigt Tests, nicht die Produktion.

    In Tests laeuft der Thread nicht — im Betrieb muss eine gescheiterte
    Sicherung weiterhin eine Warnung schreiben, sonst verschwindet der einzige
    Hinweis darauf, dass die private Sammlung seit Tagen nicht gesichert ist.
    """
    dienst = (REPO / "src" / "nexus_memory" / "mcp_server.py").read_text(encoding="utf-8")
    schleife = dienst[dienst.index("def _start_auto_backup"):]
    schleife = schleife[:schleife.index("\n    def ", 1)]
    assert "logging.warning" in schleife and "Auto-backup failed" in schleife

    plugin = PLUGIN.read_text(encoding="utf-8")
    schleife = plugin[plugin.index("def _start_auto_backup"):]
    schleife = schleife[:schleife.index("\n    def ", 1)]
    assert "logger.warning" in schleife and "Auto-backup failed" in schleife


# ── Aufbewahrung: vier Dateien, in beiden Kopien ────────────────────────────

def _alte_sicherungen(anzahl: int) -> Path:
    ordner = Path(os.environ["NEXUS_DATA_DIR"]) / "backups"
    ordner.mkdir(parents=True, exist_ok=True)
    for i in range(anzahl):
        (ordner / f"nexus-backup-2020-01-{i + 1:02d}_00-00-00.json").write_text("{}", encoding="utf-8")
    return ordner


def test_aufbewahrung_behaelt_vier(monkeypatch):
    """Vier = ein Tag beim 6-Stunden-Takt des Dienstes (vorher sieben)."""
    monkeypatch.delenv("NEXUS_BACKUP_DIR", raising=False)
    ordner = _alte_sicherungen(6)

    neu = Path(_leerer_store()._do_backup())

    rest = sorted(p.name for p in ordner.glob("nexus-backup-*"))
    assert len(rest) == 4, rest
    assert neu.name in rest
    assert mcp.BACKUP_KEEP == 4


def test_aufbewahrung_behaelt_vier_im_plugin(monkeypatch):
    monkeypatch.delenv("NEXUS_BACKUP_DIR", raising=False)
    modul = _plugin_modul()
    ordner = _alte_sicherungen(6)

    neu = Path(_freier_provider(modul)._do_backup())

    rest = sorted(p.name for p in ordner.glob("nexus-backup-*"))
    assert len(rest) == 4, rest
    assert neu.name in rest
    assert modul.BACKUP_KEEP == 4


# ── Rechte der Plugin-Sicherung ─────────────────────────────────────────────

def test_plugin_sicherung_ist_fuer_andere_unlesbar(monkeypatch):
    """Private Erinnerungen: die Datei ist 0o600, der Ordner 0o700 — auch bei umask 0."""
    monkeypatch.delenv("NEXUS_BACKUP_DIR", raising=False)
    alt = os.umask(0)  # schlimmster Fall: der umask nimmt nichts weg
    try:
        pfad = Path(_freier_provider(_plugin_modul())._do_backup())
    finally:
        os.umask(alt)

    datei = stat.S_IMODE(os.stat(pfad).st_mode)
    ordner = stat.S_IMODE(os.stat(pfad.parent).st_mode)
    assert datei & 0o077 == 0, f"Datei ist {oct(datei)} statt 0o600"
    assert ordner & 0o077 == 0, f"Ordner ist {oct(ordner)} statt 0o700"


# ── Die Werkzeugbeschreibung nennt das echte Verhalten ──────────────────────

def test_werkzeugbeschreibung_nennt_das_echte_verhalten():
    """Keine Vektoren (benannte Vektoren), und der Dienst sichert alle sechs Stunden."""
    quelle = (REPO / "src" / "nexus_memory" / "mcp_server.py").read_text(encoding="utf-8")
    beschreibung = quelle[quelle.index('name="backup"'):]
    beschreibung = beschreibung[:beschreibung.index('name="restore"')]

    assert "Payloads only" in beschreibung
    assert "every 6 hours" in beschreibung
    assert "every 24h" not in beschreibung
    assert "Includes payloads + vectors" not in beschreibung
