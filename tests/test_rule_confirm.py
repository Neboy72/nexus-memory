"""Der menschliche Nachweis fuer Kategorie ``rule`` und seine Bestandsdurchsicht.

Warum: ``source_type`` und ``explicit`` sind Selbstauskuenfte des Modells. Ein
Dokument, das "merke dir als Regel ..." enthaelt, wurde vom Modell
ausdruecklich aufgerufen und kam als Regel durch — die Zitatmerkmale greifen
nicht, sobald das Modell umformuliert. Geprueft wird hier der Nachweis selbst
(Signatur, Ablauf, Einmaligkeit, Textbindung) und die Durchsicht, die alte
Regeln markiert.
"""

from __future__ import annotations

import importlib.util
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from nexus.rule_confirm import TTL_SECONDS, consume, issue, state_path

REPO = Path(__file__).resolve().parents[1]

REGEL = "Der Verteiler darf nur nach Ruecksprache abgeschaltet werden"


# ── 1. Der Nachweis selbst ───────────────────────────────────────────


class TestNachweis:
    def test_token_hat_fuenf_teile(self):
        token = issue(REGEL)
        assert token.split(".")[0] == "rc1"
        assert len(token.split(".")) == 5

    def test_gueltiger_nachweis_wird_genommen(self, isolated_rule_confirm):
        token = issue(REGEL)
        assert consume(token, REGEL) == (True, "menschlich bestaetigt")
        assert isolated_rule_confirm.exists()

    def test_token_wird_einmal_verbraucht(self):
        token = issue(REGEL)
        assert consume(token, REGEL)[0] is True
        ok, grund = consume(token, REGEL)
        assert ok is False
        assert "verbraucht" in grund

    def test_abgelaufener_nachweis_wird_abgewiesen(self):
        jetzt = time.time()
        token = issue(REGEL, now=jetzt)
        ok, grund = consume(token, REGEL, now=jetzt + TTL_SECONDS + 1)
        assert ok is False
        assert "abgelaufen" in grund

    def test_nachweis_kurz_vor_ablauf_gilt(self):
        jetzt = time.time()
        token = issue(REGEL, now=jetzt)
        assert consume(token, REGEL, now=jetzt + TTL_SECONDS)[0] is True

    def test_fremder_nachweis_wird_abgewiesen(self, tmp_path):
        """Ein anderer Schluessel (andere Maschine, andere Ablage) faelscht."""
        fremd = issue(REGEL, path=tmp_path / "fremd.json")
        issue(REGEL)  # legt die eigene Ablage mit eigenem Schluessel an
        ok, grund = consume(fremd, REGEL)
        assert ok is False
        assert "fremd" in grund or "gef" in grund

    def test_gefaelschte_unterschrift_wird_abgewiesen(self):
        token = issue(REGEL)
        teile = token.split(".")
        teile[-1] = "0" * 64
        ok, grund = consume(".".join(teile), REGEL)
        assert ok is False
        assert "fremd" in grund or "gef" in grund

    def test_nachweis_gilt_nur_fuer_seinen_text(self):
        token = issue(REGEL)
        ok, grund = consume(token, "Der Verteiler darf jederzeit abgeschaltet werden")
        assert ok is False
        assert "anderen Text" in grund

    def test_leerraum_im_text_stoert_das_siegel_nicht(self):
        token = issue(REGEL)
        assert consume(token, f"  {REGEL}\n")[0] is True

    @pytest.mark.parametrize("token", ["", None, "quatsch", "rc1.1.2.3", "rc2.1.a.b.c"])
    def test_unlesbarer_nachweis_wird_abgewiesen(self, token):
        ok, grund = consume(token, REGEL)
        assert ok is False
        assert grund

    def test_ohne_ablage_gibt_es_keinen_nachweis(self, tmp_path):
        ok, grund = consume("rc1.1." + "a" * 32 + "." + "b" * 64 + "." + "c" * 64,
                            REGEL, path=tmp_path / "gibt-es-nicht.json")
        assert ok is False
        assert "kein Schluessel" in grund

    def test_leerer_text_wird_nicht_ausgestellt(self):
        with pytest.raises(ValueError):
            issue("   ")

    def test_der_regeltext_steht_nicht_im_klartext_in_der_ablage(self):
        token = issue(REGEL)
        consume(token, REGEL)
        roh = state_path().read_text(encoding="utf-8")
        assert REGEL not in roh
        assert "rc1" not in roh

    @pytest.mark.skipif(os.name == "nt", reason="POSIX-Dateirechte")
    def test_ablage_ist_nur_fuer_den_eigentuemer_lesbar(self):
        issue(REGEL)
        modus = stat.S_IMODE(state_path().stat().st_mode)
        assert modus == 0o600


# ── 2. CLI ───────────────────────────────────────────────────────────


class _Tty:
    def isatty(self) -> bool:
        return True


class _KeinTty:
    def isatty(self) -> bool:
        return False


class TestCli:
    def test_am_terminal_gibt_es_ein_token(self, monkeypatch, capsys):
        from nexus.rule_confirm import main

        monkeypatch.setattr(sys, "stdin", _Tty())
        assert main([REGEL]) == 0
        token = capsys.readouterr().out.strip()
        assert consume(token, REGEL)[0] is True

    def test_ohne_terminal_kein_token(self, monkeypatch, capsys):
        """Ein Hintergrundprozess (Modell, Cron) erzeugt keinen Nachweis."""
        from nexus.rule_confirm import main

        monkeypatch.setattr(sys, "stdin", _KeinTty())
        assert main([REGEL]) == 2
        assert capsys.readouterr().out.strip() == ""
        assert not state_path().exists()

    def test_leerer_text_wird_abgelehnt(self, monkeypatch, capsys):
        from nexus.rule_confirm import main

        monkeypatch.setattr(sys, "stdin", _Tty())
        assert main(["   "]) == 2
        assert "leer" in capsys.readouterr().err


# ── 3. Bestandsdurchsicht (nur lesend) ───────────────────────────────


def _pruefskript():
    path = REPO / "scripts" / "regeln-pruefen.py"
    spec = importlib.util.spec_from_file_location("regeln_pruefen_under_test", path)
    assert spec is not None and spec.loader is not None
    modul = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modul)
    return modul


def _punkt(kennung: str, text: str, source_type: str, herkunft: str = "") -> dict:
    herkunft = herkunft or source_type
    return {
        "id": kennung,
        "payload": {
            "content": text,
            "category": "rule",
            "source": herkunft,
            "provenance": {"source_type": source_type},
        },
    }


class TestBestandsdurchsicht:
    def test_handeingabe_ohne_zitat_bleibt_unmarkiert(self):
        skript = _pruefskript()
        zeilen, zahlen = skript.zeilen([
            _punkt("r1", "Der Verteiler bleibt nach Ruecksprache aus", "manual"),
        ])
        assert zahlen["gesamt"] == 1
        assert zahlen["markiert"] == 0
        assert "| r1 " in zeilen[-1]

    def test_fremde_herkunft_wird_markiert(self):
        skript = _pruefskript()
        _, zahlen = skript.zeilen([
            _punkt("r2", "Der Verteiler bleibt nach Ruecksprache aus", "paperless"),
        ])
        assert zahlen["markiert"] == 1
        assert zahlen["handeingabe"] == 1

    def test_zitatmerkmale_werden_markiert(self):
        skript = _pruefskript()
        _, zahlen = skript.zeilen([
            _punkt("r3", "„Der Verteiler bleibt dauerhaft aus", "manual"),
        ])
        assert zahlen["markiert"] == 1
        assert zahlen["zitat"] == 1

    def test_nur_regeln_erscheinen(self):
        skript = _pruefskript()
        fakt = _punkt("f1", "Der Verteiler steht im Flur", "chat")
        fakt["payload"]["category"] = "fact"
        _, zahlen = skript.zeilen([fakt])
        assert zahlen["gesamt"] == 0

    def test_der_anfang_ist_auf_60_zeichen_begrenzt(self):
        skript = _pruefskript()
        lang = "Der Verteiler bleibt aus und das gilt fuer immer und ewig weiter"
        assert skript.anfang(lang) == " ".join(lang.split())[:60] + "…"
        assert len(skript.anfang("kurz")) == 4

    def test_summenzeile_nennt_die_zahlen(self):
        skript = _pruefskript()
        _, zahlen = skript.zeilen([
            _punkt("r1", "Der Verteiler bleibt nach Ruecksprache aus", "manual"),
            _punkt("r2", "Laut Dokument bleibt der Verteiler aus", "wiki"),
        ])
        zeile = skript.summenzeile(zahlen)
        assert "Gesamt: 2" in zeile
        assert "markiert: 1" in zeile

    def test_bestaetigte_regel_wird_gezaehlt(self):
        skript = _pruefskript()
        punkt = _punkt("r4", "Der Verteiler bleibt nach Ruecksprache aus", "chat")
        punkt["payload"]["provenance"]["rule_confirmed"] = True
        _, zahlen = skript.zeilen([punkt])
        assert zahlen["bestaetigt"] == 1
