"""Ein Schluessel, ein Eintrag: die Einschreibung darf sich nicht verdoppeln.

Befund 26.11.2026: 13:53:57 UTC meldet sich die Installation an und bekommt
einen Schluessel, 13:54:22 UTC sieht eine zweite Instanz die Datei nicht und
meldet sich erneut an. Das Register trug danach zwei Eintraege "Hermes Agent" —
der kleine bekam die Datei, der Admin-Eintrag blieb als Leiche stehen.

Diese Datei haelt die zwei Wege fest, auf denen das jetzt nicht mehr passiert:

  1. Zwei Einschreibungen gleichzeitig: GENAU EINE Schluesseldatei, Inhalt des
     ersten Schreibers, und der Dienst wird nur EINMAL gefragt.
  2. Die Datei erscheint zwischen Lesen und Schreiben: die vorhandene gewinnt,
     der eigene Schluessel wird verworfen.

Dazu die Grundregel: ``_write_agent_key`` ueberschreibt nie.
"""
from __future__ import annotations
import os

import json
import threading
import time

import pytest

import plugins.memory.nexus as nx


@pytest.fixture()
def keys(tmp_path, monkeypatch):
    """Eigenes Datenverzeichnis; Dienst und Installationsschluessel gefaelscht."""
    monkeypatch.setattr(nx, "_plugin_data_dir", lambda: tmp_path)
    monkeypatch.setattr(nx, "_install_key", lambda: "installation-key")
    monkeypatch.setattr(nx, "_embed_service_base", lambda: "http://stub.invalid")
    return tmp_path / "agent_keys" / f"{nx._HARNESS}.key"


def _stub_enroll(monkeypatch, key_fn, on_called=None, delay=0.0):
    """Attrappe von /enroll: liefert der Reihe nach Schluessel."""
    calls = []

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return self._payload

    def fake_urlopen(request, timeout=None):
        calls.append(1)
        if on_called:
            on_called(len(calls))
        if delay:
            time.sleep(delay)
        payload = json.dumps({"key": key_fn(len(calls)), "agent_id": "x"})
        return _Resp(payload.encode("utf-8"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return calls


# ── 1. zwei gleichzeitige Einschreibungen ─────────────────────────────────

def test_two_processes_enrolling_at_once_produce_one_key_file(keys, monkeypatch):
    calls = _stub_enroll(monkeypatch, lambda n: "KEY-%d" % n, delay=0.20)

    ergebnis = {}

    def enrol(name):
        ergebnis[name] = nx._service_auth_key()

    threads = [threading.Thread(target=enrol, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert not any(t.is_alive() for t in threads), "eine Einschreibung haengt"
    assert keys.exists(), "keine Schluesseldatei"
    assert keys.read_text(encoding="utf-8") == "KEY-1", (
        "die Datei gehoert dem ersten Schreiber, nicht dem zweiten")
    assert len(calls) == 1, "der Dienst wurde %d-mal gefragt" % len(calls)
    assert set(ergebnis.values()) == {"KEY-1"}, (
        "beide Prozesse muessen denselben Schluessel benutzen: %r" % ergebnis)
    assert not keys.with_name(keys.name + ".lock").exists(), "Sperrdatei blieb liegen"


def test_the_loser_of_the_race_adopts_the_winners_key(keys, monkeypatch):
    """Der zweite Prozess wartet und nimmt den fremden Schluessel."""
    _stub_enroll(monkeypatch, lambda n: "KEY-%d" % n, delay=0.15)
    gesehen = []

    def zweiter():
        time.sleep(0.02)          # startet mitten in der ersten Einschreibung
        gesehen.append(nx._service_auth_key())

    t = threading.Thread(target=zweiter)
    erster = nx._service_auth_key()
    t.start()
    t.join(10)

    assert erster == "KEY-1"
    assert gesehen == ["KEY-1"], "der zweite Prozess hat sich einen eigenen geholt"


# ── 2. die Datei erscheint zwischen Lesen und Schreiben ───────────────────

def test_a_key_that_appears_meanwhile_is_not_overwritten(keys, monkeypatch):
    def fremd_schreibt(_n):
        keys.parent.mkdir(parents=True, exist_ok=True)
        keys.write_text("FREMD", encoding="utf-8")

    calls = _stub_enroll(monkeypatch, lambda n: "EIGEN", on_called=fremd_schreibt)
    assert nx._enroll("http://stub.invalid") == "FREMD", (
        "der vorhandene Schluessel muss uebernommen werden")
    assert keys.read_text(encoding="utf-8") == "FREMD"
    assert len(calls) == 1


def test_write_agent_key_never_overwrites(keys):
    assert nx._write_agent_key("ERSTER") is True
    assert keys.read_text(encoding="utf-8") == "ERSTER"
    assert nx._write_agent_key("ZWEITER") is False
    assert keys.read_text(encoding="utf-8") == "ERSTER", "die Datei wurde ueberschrieben"
    if os.name == "posix":
        # Windows kennt keine POSIX-Rechte; dort ist 0o666 normal und die
        # Aussage dieses Tests (nie ueberschreiben) steht schon oben.
        if os.name == "posix":
            # Windows kennt keine POSIX-Rechte: dort ist 0o666 normal.
            assert oct(keys.stat().st_mode & 0o777) == "0o600"


def test_existing_key_is_used_without_asking_the_service(keys, monkeypatch):
    keys.parent.mkdir(parents=True, exist_ok=True)
    keys.write_text("VORHANDEN", encoding="utf-8")
    calls = _stub_enroll(monkeypatch, lambda n: "NEU")
    assert nx._service_auth_key() == "VORHANDEN"
    assert calls == []


# ── 3. eine liegengebliebene Sperre blockiert nicht ewig ──────────────────

def test_a_stale_lock_is_overridden_after_the_wait(keys, monkeypatch):
    """Ein abgestuerzter Prozess darf die Anmeldung nicht dauerhaft sperren."""
    monkeypatch.setattr(nx, "_AGENT_KEY_LOCK_WAIT_SEC", 0.2)
    monkeypatch.setattr(nx, "_AGENT_KEY_LOCK_STEP_SEC", 0.02)
    lock = keys.with_name(keys.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("", encoding="utf-8")     # Sperre eines toten Prozesses
    _stub_enroll(monkeypatch, lambda n: "KEY-NACH-WARTEN")

    assert nx._service_auth_key() == "KEY-NACH-WARTEN"
    assert keys.read_text(encoding="utf-8") == "KEY-NACH-WARTEN"
    assert lock.exists(), "die fremde Sperre gehoert nicht uns — wir fassen sie nicht an"
