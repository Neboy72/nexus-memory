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
  Lauf,
- die Sicherung ist fuer andere Konten nicht lesbar — auf POSIX ueber die
  Modusbits 0600/0700, auf Windows ueber die ACL (``icacls``). Windows meldet
  ``os.stat().st_mode`` immer als 0o666/0o777, deshalb prueft dort die ACL und
  nicht das Bit.
"""
import importlib.util
import getpass
import os
import stat
import subprocess
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


# ── Der Waechter unterscheidet Test und Dienst (26.11.2026) ──────────────────

def test_waechter_zaehlt_nur_die_eigenen_sicherungen(tmp_path):
    """Beleg fuer die Regel: fremde PID = Dienst, eigene/fehlende = Fund.

    Der Sicherungsordner ist geteilt: der Dienst sichert beim Start hinein. Am
    26.11.2026 schrieb ein neustartender Dienst zwei 124-MB-Dateien waehrend
    der Suite, und der Waechter machte daraus einen Fehler im Testlauf. Seit
    die Datei ihren Schreiber im Feld ``pid`` nennt, zaehlt nur noch die eigene
    Prozess-Kennung — und eine Datei ohne Angabe bleibt ein Fund.
    """
    import json
    import os
    from conftest import _aus_diesem_prozess

    eigene = tmp_path / "nexus-backup-eigene.json"
    eigene.write_text(json.dumps({"pid": os.getpid(), "points": []}), encoding="utf-8")
    fremde = tmp_path / "nexus-backup-fremde.json"
    fremde.write_text(json.dumps({"pid": 999999, "points": []}), encoding="utf-8")
    ohne = tmp_path / "nexus-backup-ohne.json"
    ohne.write_text(json.dumps({"points": []}), encoding="utf-8")

    namen = [eigene.name, fremde.name, ohne.name]
    assert _aus_diesem_prozess(namen, tmp_path) == [eigene.name, ohne.name]


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


# ── Rechte der Sicherungen: Modusbits auf POSIX, ACLs auf Windows ────────────

# Windows kennt keine POSIX-Modusbits: ``os.chmod`` schaltet dort nur das
# Read-only-Attribut, und ``os.stat().st_mode`` meldet immer 0o666 (Datei) bzw.
# 0o777 (Ordner) — die Zusicherung "fuer andere Konten nicht lesbar" haengt dort
# an der ACL, die ``_restrict_windows_acl`` per ``icacls`` setzt (Konto + SYSTEM,
# keine breiten Prinzipale). Geprueft wird deshalb plattformgerecht: auf POSIX
# die Bits, auf Windows die ACL. Ein pauschaler ``skipif`` waere falsch — die
# Zusicherung gilt auf beiden Seiten, nur die Messstelle ist eine andere.
_POSIX_MODUSBITS = os.name != "nt"

# Muessen auf Windows aus der ACL verschwunden sein (GitHub-Runner ist en-US);
# zusaetzlich ueber die SIDs, weil die Namen lokalisiert sein koennen.
_WEITE_TRUSTEES = ("everyone", "builtin\\users", "authenticated users")
_WEITE_TRUSTEE_SIDS = ("s-1-1-0", "s-1-5-32-545", "s-1-5-11")


def _icacls(pfad: Path) -> str:
    """Die ACL-Zeilen von ``icacls`` (nur Windows)."""
    proc = subprocess.run(
        ["icacls", str(pfad)], capture_output=True, text=True,
        encoding="oem", errors="replace", timeout=30,
    )
    assert proc.returncode == 0, f"icacls {pfad} schlug fehl: {proc.stderr}"
    # Der Pfad steht selbst in der Ausgabe — sonst wuerde ein Ordner namens
    # "Users" als Prinzipal durchgehen.
    return proc.stdout.replace(str(pfad), "").lower()


def _assert_privat(pfad: Path, *, ist_ordner: bool = False) -> None:
    """Nur das aktuelle Konto (und SYSTEM) kommt an *pfad* heran."""
    if _POSIX_MODUSBITS:
        modus = stat.S_IMODE(os.stat(pfad).st_mode)
        erwartet = 0o700 if ist_ordner else 0o600
        assert modus & 0o077 == 0, f"{pfad} ist {oct(modus)} statt {oct(erwartet)}"
        return

    acl = _icacls(pfad)
    for trustee in _WEITE_TRUSTEES:
        assert trustee not in acl, f"{trustee} hat weiter Zugriff auf {pfad}:\n{acl}"
    for sid in _WEITE_TRUSTEE_SIDS:
        assert sid not in acl, f"{sid} hat weiter Zugriff auf {pfad}:\n{acl}"
    assert getpass.getuser().lower() in acl, f"Eigentuemer fehlt in der ACL von {pfad}:\n{acl}"
    assert "system" in acl, f"SYSTEM fehlt in der ACL von {pfad}:\n{acl}"


def _ohne_umask():
    """POSIX: umask 0 ist der schlimmste Fall — der umask nimmt nichts weg."""
    return os.umask(0) if _POSIX_MODUSBITS else None


def test_plugin_sicherung_ist_fuer_andere_unlesbar(monkeypatch):
    """Private Erinnerungen: Datei 0600 bzw. nur Eigentuemer+SYSTEM — auch bei umask 0."""
    monkeypatch.delenv("NEXUS_BACKUP_DIR", raising=False)
    alt = _ohne_umask()
    try:
        pfad = Path(_freier_provider(_plugin_modul())._do_backup())
    finally:
        if alt is not None:
            os.umask(alt)

    _assert_privat(pfad)
    if _POSIX_MODUSBITS:
        # Den Ordner schraenkt das Plugin nur auf POSIX selbst ein. Auf Windows
        # erbt er die ACL des Datenverzeichnisses: ``fix_dir_permissions`` setzt
        # sie mit (OI)(CI), also fuer jedes Kind mit — deshalb prueft der Test
        # dort die Datei, nicht eine Ordner-ACL, die das Plugin nie setzt.
        _assert_privat(pfad.parent, ist_ordner=True)


def test_dienst_sicherung_ist_fuer_andere_unlesbar(monkeypatch):
    """Der Dienst schraenkt Ordner UND Datei ein (auf Windows beide per icacls)."""
    monkeypatch.delenv("NEXUS_BACKUP_DIR", raising=False)
    alt = _ohne_umask()
    try:
        pfad = Path(_leerer_store()._do_backup())
    finally:
        if alt is not None:
            os.umask(alt)

    _assert_privat(pfad.parent, ist_ordner=True)
    _assert_privat(pfad)


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


# ── Die Start-Sicherung fragt, ob heute schon gesichert wurde (26.11.2026) ───
#
# Nebos Befund: an einem Tag mit vier Neustarts lagen vier Dateien zu je 124 MB
# im Ordner (13:53:28, 13:54:09, 13:56:50, 13:56:52) — jeder Start schrieb seine
# eigene "Start-Sicherung". Seit 0.22.76 fragt die Start-Sicherung, ob fuer
# heute schon eine Datei liegt; der Takt danach (6 h im Dienst, 24 h im Plugin)
# laeuft weiter und schreibt, wenn der Tag wechselt.
#
# Geprueft wird die echte Schleife, nicht nur der Helfer: das Ziel des
# Backup-Fadens wird abgefangen und einmal ausgefuehrt, ``time.sleep`` bricht
# die Endlosschleife mit ``_Fertig`` ab. Dass der Abbruch ueberhaupt kommt,
# beweist, dass die Schleife nach dem (uebersprungenen) Start weitergelaufen
# ist und nicht zurueckgekehrt ist.

class _Fertig(BaseException):
    """Bricht die Endlosschleife ab, ohne als Fehler gefangen zu werden."""


class _NieGestoppt:
    """``_write_stop``-Ersatz des Plugins: nicht gesetzt, kein echtes Warten."""

    def wait(self, *args, **kwargs):
        return False

    def is_set(self):
        return False


def _fange_schleife(monkeypatch, starten):
    """Startet den Backup-Thread nicht, sondern gibt seine Schleife zurueck."""
    import threading as _threading

    gefangen = []

    def _fake_thread(**kwargs):
        gefangen.append(kwargs.get("target"))
        return SimpleNamespace(start=lambda: None)

    monkeypatch.setattr(_threading, "Thread", _fake_thread)
    starten()
    assert len(gefangen) == 1, gefangen
    return gefangen[0]


def _schlafe_bis_abbruch(monkeypatch, modul, erlaubte_schlaefer):
    """``time.sleep`` laesst die ersten Aufrufe durch, dann bricht es ab."""
    zaehler = {"n": 0}

    def _schlaf(sekunden):
        zaehler["n"] += 1
        if zaehler["n"] > erlaubte_schlaefer:
            raise _Fertig

    monkeypatch.setattr(modul.time, "sleep", _schlaf)
    return zaehler


def _heutiger_name() -> str:
    from datetime import datetime
    return f"nexus-backup-{datetime.now().strftime('%Y-%m-%d')}_08-00-00.json"


def test_dienst_helfer_erkennt_nur_heute(tmp_path):
    """Kriterium: das Datum im Dateinamen — heute ja, gestern nein."""
    (tmp_path / _heutiger_name()).write_text("{}", encoding="utf-8")
    (tmp_path / "nexus-backup-2020-01-01_00-00-00.json").write_text("{}", encoding="utf-8")
    (tmp_path / "irgendwas.json").write_text("{}", encoding="utf-8")

    assert mcp._backup_for_today_exists(str(tmp_path)) is True

    leer = tmp_path / "leer"
    leer.mkdir()
    assert mcp._backup_for_today_exists(str(leer)) is False
    assert mcp._backup_for_today_exists(str(tmp_path / "gibt-es-nicht")) is False


def test_plugin_helfer_erkennt_nur_heute(tmp_path):
    modul = _plugin_modul()
    (tmp_path / _heutiger_name()).write_text("{}", encoding="utf-8")
    (tmp_path / "nexus-backup-2020-01-01_00-00-00.json").write_text("{}", encoding="utf-8")

    assert modul._backup_for_today_exists(tmp_path) is True
    (tmp_path / _heutiger_name()).unlink()
    assert modul._backup_for_today_exists(tmp_path) is False


def test_dienst_start_sicherung_legt_pro_tag_nur_eine_an(monkeypatch, tmp_path):
    """Heute liegt schon eine Sicherung: der Start schreibt keine zweite."""
    monkeypatch.setenv("NEXUS_AUTO_BACKUP", "1")
    monkeypatch.setattr(mcp, "_backup_dir", lambda: str(tmp_path))
    (tmp_path / _heutiger_name()).write_text("{}", encoding="utf-8")

    store = object.__new__(mcp.MemoryStore)
    aufrufe = []
    monkeypatch.setattr(store, "_do_backup", lambda: aufrufe.append(1))
    ziel = _fange_schleife(monkeypatch, store._start_auto_backup)
    _schlafe_bis_abbruch(monkeypatch, mcp, 1)

    with pytest.raises(_Fertig):  # die Schleife lief weiter (kein Rueckweg)
        ziel()

    assert aufrufe == [], "Start hat trotz vorhandener Sicherung geschrieben"


def test_dienst_start_sicherung_schreibt_nach_tageswechsel(monkeypatch, tmp_path):
    """Nur gestern gesichert: der Start sichert heute (der Tag ist gewechselt)."""
    monkeypatch.setenv("NEXUS_AUTO_BACKUP", "1")
    monkeypatch.setattr(mcp, "_backup_dir", lambda: str(tmp_path))
    (tmp_path / "nexus-backup-2020-01-01_00-00-00.json").write_text("{}", encoding="utf-8")

    store = object.__new__(mcp.MemoryStore)
    aufrufe = []
    monkeypatch.setattr(store, "_do_backup", lambda: aufrufe.append(1))
    ziel = _fange_schleife(monkeypatch, store._start_auto_backup)
    _schlafe_bis_abbruch(monkeypatch, mcp, 1)

    with pytest.raises(_Fertig):
        ziel()

    assert aufrufe == [1]


def test_dienst_sechsstunden_takt_laeuft_nach_uebersprungenem_start(monkeypatch, tmp_path):
    """Ein uebersprungener Start haelt den 6-Stunden-Takt nicht an."""
    monkeypatch.setenv("NEXUS_AUTO_BACKUP", "1")
    monkeypatch.setattr(mcp, "_backup_dir", lambda: str(tmp_path))
    (tmp_path / _heutiger_name()).write_text("{}", encoding="utf-8")

    store = object.__new__(mcp.MemoryStore)
    aufrufe = []
    monkeypatch.setattr(store, "_do_backup", lambda: aufrufe.append(1))
    ziel = _fange_schleife(monkeypatch, store._start_auto_backup)
    # 1 Schlaf vor dem Start + 360 des ersten 6h-Blocks; danach kommt der
    # zweite Durchlauf, der wieder sichern darf.
    _schlafe_bis_abbruch(monkeypatch, mcp, 1 + 360)

    with pytest.raises(_Fertig):
        ziel()

    assert aufrufe == [1], (
        "erwartet: Start uebersprungen und der 6-Stunden-Takt hat einmal "
        f"geschrieben — bekommen: {aufrufe}")


def test_plugin_start_sicherung_legt_pro_tag_nur_eine_an(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXUS_AUTO_BACKUP", "1")
    monkeypatch.setenv("NEXUS_BACKUP_DIR", str(tmp_path))
    (tmp_path / _heutiger_name()).write_text("{}", encoding="utf-8")

    modul = _plugin_modul()
    provider = object.__new__(modul.NexusMemoryProvider)
    provider._write_stop = _NieGestoppt()
    aufrufe = []
    monkeypatch.setattr(provider, "_do_backup", lambda: aufrufe.append(1))
    ziel = _fange_schleife(monkeypatch, provider._start_auto_backup)
    _schlafe_bis_abbruch(monkeypatch, modul, 1)

    with pytest.raises(_Fertig):
        ziel()

    assert aufrufe == [], "Start hat trotz vorhandener Sicherung geschrieben"


def test_plugin_start_sicherung_schreibt_nach_tageswechsel(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXUS_AUTO_BACKUP", "1")
    monkeypatch.setenv("NEXUS_BACKUP_DIR", str(tmp_path))
    (tmp_path / "nexus-backup-2020-01-01_00-00-00.json").write_text("{}", encoding="utf-8")

    modul = _plugin_modul()
    provider = object.__new__(modul.NexusMemoryProvider)
    provider._write_stop = _NieGestoppt()
    aufrufe = []
    monkeypatch.setattr(provider, "_do_backup", lambda: aufrufe.append(1))
    ziel = _fange_schleife(monkeypatch, provider._start_auto_backup)
    _schlafe_bis_abbruch(monkeypatch, modul, 1)

    with pytest.raises(_Fertig):
        ziel()

    assert aufrufe == [1]


def test_plugin_tages_takt_laeuft_nach_uebersprungenem_start(monkeypatch, tmp_path):
    """Die taegliche Sicherung laeuft weiter, auch wenn der Start uebersprungen wurde."""
    monkeypatch.setenv("NEXUS_AUTO_BACKUP", "1")
    monkeypatch.setenv("NEXUS_BACKUP_DIR", str(tmp_path))
    (tmp_path / _heutiger_name()).write_text("{}", encoding="utf-8")

    modul = _plugin_modul()
    provider = object.__new__(modul.NexusMemoryProvider)
    provider._write_stop = _NieGestoppt()
    aufrufe = []
    monkeypatch.setattr(provider, "_do_backup", lambda: aufrufe.append(1))
    ziel = _fange_schleife(monkeypatch, provider._start_auto_backup)
    _schlafe_bis_abbruch(monkeypatch, modul, modul.BACKUP_INTERVAL_ITERATIONS)

    with pytest.raises(_Fertig):
        ziel()

    assert aufrufe == [1], (
        "erwartet: Start uebersprungen und die taegliche Sicherung hat einmal "
        f"geschrieben — bekommen: {aufrufe}")
