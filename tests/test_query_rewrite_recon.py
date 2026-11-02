#!/usr/bin/env python3
"""VOLLSTAENDIGE Test-Suite fuer die rekonstruierte query_rewrite.py.

Exakte Original-Faelle aus dem 311er-Test-pyc (Konstanten ausgelesen).
Schreibt den think-tag via String-Verkettung (heredoc-sicher).

Aufbau: jede Pruefung ist ein eigener Test, damit sie in der Zaehlung
auftaucht und in CI wirklich laeuft. Frueher war das hier ein Skript, das
pytest nur importierte — die Suite meldete 0 Tests fuer die Datei, und der
einzige zusaetzliche Importpfad zeigte auf einen Checkout *ausserhalb* des
Repositories (Nachbarordner `nexus-memory-test`), den es nur auf einer
Maschine gibt. Existierte er, wurde sogar dieser fremde Stand getestet
statt der Code im Repository.
"""
from nexus_memory import query_rewrite as qr

OPEN = '<' + 'think>'
CLOSE = '<' + '/think>'
THINK = OPEN + 'ok' + CLOSE

Q = 'was war das mit dem ding furs auto'
A = 'wallbox ladekabel rfid ruckgabe'


def boom(prompt):  # noqa: ARG001 - Dispatcher-Attrappe, wirft absichtlich
    raise RuntimeError('station down')


# ── TestEnabled ────────────────────────────────────────────────────
def test_on_by_default(monkeypatch):
    monkeypatch.delenv('NEXUS_REWRITE', raising=False)
    assert qr.enabled() is True


def test_on_with_flag_1(monkeypatch):
    monkeypatch.setenv('NEXUS_REWRITE', '1')
    assert qr.enabled() is True


def test_off_with_flag_0(monkeypatch):
    monkeypatch.setenv('NEXUS_REWRITE', '0')
    assert qr.enabled() is False


def test_on_with_flag_true(monkeypatch):
    monkeypatch.setenv('NEXUS_REWRITE', 'true')
    assert qr.enabled() is True


def test_env_brake_disables(monkeypatch):
    # v0.19.0: Default ist AN (Entscheidung 13.09.); Notbremse ist NEXUS_REWRITE=0.
    monkeypatch.setenv('NEXUS_REWRITE', '0')
    assert qr.enabled() is False


# ── TestCleanOutput (Original-Konstanten) ─────────────────────────
def test_thinking_wrapper_stripped(monkeypatch):
    monkeypatch.setenv('NEXUS_REWRITE', '1')
    cleaned = qr._clean_output('Ich sollte nach Begriffen suchen.' + THINK + ' wallbox ladekabel rfid')
    assert cleaned == 'wallbox ladekabel rfid'


def test_prefix_stripped(monkeypatch):
    monkeypatch.setenv('NEXUS_REWRITE', '1')
    assert qr._clean_output('A: wallbox rfid') == 'wallbox rfid'


def test_quotes_stripped(monkeypatch):
    monkeypatch.setenv('NEXUS_REWRITE', '1')
    assert qr._clean_output('"wallbox rfid"') == 'wallbox rfid'


def test_single_line(monkeypatch):
    monkeypatch.setenv('NEXUS_REWRITE', '1')
    assert qr._clean_output('wallbox rfid\nmore stuff') == 'wallbox rfid'


# ── TestAcceptable (Original-Konstanten) ──────────────────────────
def test_case_identical_rejected():
    assert qr._acceptable('Wallbox RFID', 'wallbox rfid') is False


def test_think_markup_rejected():
    assert qr._acceptable(THINK, 'ding furs auto') is False


def test_normal_accepted():
    assert qr._acceptable('wallbox ladekabel rfid ruckgabe', Q) is True


def test_echo_prefix_rejected():
    assert qr._acceptable('A: wallbox rfid', Q) is False


def test_runaway_rejected():
    assert qr._acceptable('x' * 500, 'x') is False


def test_len_two_accepted_in_acceptable():
    # 'ab' (len 2) ist acceptable im Original (len<2 rejected); zu kurze Queries
    # gehen in rewrite_query ueber _MIN_SAVE_LEN=3 als Original durch.
    assert qr._acceptable('ab', 'x') is True


def test_empty_rejected():
    assert qr._acceptable('', 'x') is False


# ── TestRewriteQuery (Original-Faelle) ────────────────────────────
def test_no_env_still_rewrites(monkeypatch):
    # v0.19.0 Default AN: ohne Env rewritet der Test-Call jetzt zur Antwort A
    # (der fail-open-Vertrag bleibt: None-Dispatcher -> original, siehe unten)
    monkeypatch.delenv('NEXUS_REWRITE', raising=False)
    assert qr.rewrite_query(Q, lambda prompt: A) == A


def test_brake_returns_original(monkeypatch):
    monkeypatch.setenv('NEXUS_REWRITE', '0')
    assert qr.rewrite_query(Q, lambda prompt: A) == Q


def test_plain_rewrite(monkeypatch):
    monkeypatch.setenv('NEXUS_REWRITE', '1')
    assert qr.rewrite_query(Q, lambda prompt: A) == A


def test_meta_leak_output_falls_back_to_original(monkeypatch):
    # GLM-Ausgabe mit Meta-Leak ('hier ist die Antwort') wird BEWUSST zu ''
    # gemacht (Meta-Leak-Schutz im Original) -> fails-open auf Original.
    monkeypatch.setenv('NEXUS_REWRITE', '1')
    leaky = (OPEN + 'denkprozess' + CLOSE
             + 'A: hier ist die Antwort zu deiner Frage\nIch formuliere um. ' + A)
    assert qr.rewrite_query(Q, lambda prompt: leaky) == Q


def test_station_down_falls_back_to_original(monkeypatch):
    monkeypatch.setenv('NEXUS_REWRITE', '1')
    assert qr.rewrite_query(Q, None) == Q


def test_dispatcher_exception_falls_back_to_original(monkeypatch):
    monkeypatch.setenv('NEXUS_REWRITE', '1')
    assert qr.rewrite_query(Q, boom) == Q


def test_port_query_not_rewritten(monkeypatch):
    monkeypatch.setenv('NEXUS_REWRITE', '1')
    assert qr.rewrite_query('port 9220', boom) == 'port 9220'


def test_short_query_not_rewritten(monkeypatch):
    monkeypatch.setenv('NEXUS_REWRITE', '1')
    assert qr.rewrite_query('ab', lambda prompt: 'x') == 'ab'


def test_long_query_not_rewritten(monkeypatch):
    monkeypatch.setenv('NEXUS_REWRITE', '1')
    assert qr.rewrite_query('x' * 601, boom) == 'x' * 601


def test_hund_case(monkeypatch):
    monkeypatch.setenv('NEXUS_REWRITE', '1')
    assert qr.rewrite_query(
        'wies das thema mit dem hund',
        lambda prompt: 'hund spaziergange routine futter',
    ) == 'hund spaziergange routine futter'
