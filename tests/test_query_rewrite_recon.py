#!/usr/bin/env python3
"""VOLLSTAENDIGE Test-Suite fuer die rekonstruierte query_rewrite.py.

Exakte Original-Faelle aus dem 311er-Test-pyc (Konstanten ausgelesen).
Schreibt den think-tag via String-Verkettung (heredoc-sicher).
"""
import sys, os
sys.path.insert(0, '/Users/miosha/nexus-memory-test/src')
from nexus_memory import query_rewrite as qr

OPEN = '<' + 'think>'
CLOSE = '<' + '/think>'
THINK = OPEN + 'ok' + CLOSE

fails = []

def check(name, cond):
    print(('PASS' if cond else 'FAIL'), name)
    if not cond:
        fails.append(name)

# ── TestEnabled ────────────────────────────────────────────────────
os.environ.pop('NEXUS_REWRITE', None)
check('on by default', qr.enabled() is True)
os.environ['NEXUS_REWRITE'] = '1'
check('on with =1', qr.enabled() is True)
os.environ['NEXUS_REWRITE'] = '0'
check('off with =0', qr.enabled() is False)
os.environ['NEXUS_REWRITE'] = 'true'
check('on with =true', qr.enabled() is True)

# ── TestCleanOutput (Original-Konstanten) ─────────────────────────
os.environ['NEXUS_REWRITE'] = '1'
c = qr._clean_output('Ich sollte nach Begriffen suchen.' + THINK + ' wallbox ladekabel rfid')
check('thinking_wrapper_stripped', c == 'wallbox ladekabel rfid')
check('prefix_stripped', qr._clean_output('A: wallbox rfid') == 'wallbox rfid')
check('quotes_stripped', qr._clean_output('"wallbox rfid"') == 'wallbox rfid')
check('single_line', qr._clean_output('wallbox rfid\nmore stuff') == 'wallbox rfid')

# ── TestAcceptable (Original-Konstanten) ──────────────────────────
check('case-identical rejected', qr._acceptable('Wallbox RFID', 'wallbox rfid') is False)
check('think-markup rejected', qr._acceptable(THINK, 'ding furs auto') is False)
check('normal accepted', qr._acceptable('wallbox ladekabel rfid ruckgabe', 'was war das mit dem ding furs auto') is True)
check('echo prefix rejected', qr._acceptable('A: wallbox rfid', 'was war das mit dem ding furs auto') is False)
check('runaway rejected', qr._acceptable('x' * 500, 'x') is False)
# 'ab' (len 2) ist acceptable im Original (len<2 rejected); zu kurze Queries
# gehen in rewrite_query ueber _MIN_SAVE_LEN=3 als Original durch.
check('len-2 accepted in acceptable', qr._acceptable('ab', 'x') is True)
check('empty rejected', qr._acceptable('', 'x') is False)

# v0.19.0: Default ist AN (Nebo-Entscheidung 13.09.); Notbremse ist NEXUS_REWRITE=0.
os.environ.pop('NEXUS_REWRITE', None)
check('on by default (unset env)', qr.enabled() is True)
os.environ['NEXUS_REWRITE'] = '0'
check('env brake =0 -> disabled', qr.enabled() is False)
os.environ['NEXUS_REWRITE'] = '1'

# ── TestRewriteQuery (Original-Faelle) ────────────────────────────
Q = 'was war das mit dem ding furs auto'
A = 'wallbox ladekabel rfid ruckgabe'

# v0.19.0 Default AN: ohne Env rewritet der Test-Call jetzt zur Antwort A
# (der fail-open-Vertrag bleibt: None-Dispatcher -> original, siehe unten)
check('no env -> still rewrites (default on)', qr.rewrite_query(Q, lambda p: A) == A)

os.environ['NEXUS_REWRITE'] = '0'
check('brake =0 -> original', qr.rewrite_query(Q, lambda p: A) == Q)
os.environ['NEXUS_REWRITE'] = '1'
check('plain rewrite', qr.rewrite_query(Q, lambda p: A) == A)
# GLM-Ausgabe mit Meta-Leak ('hier ist die Antwort') wird BEWUSST zu '' gemacht
# (Meta-Leak-Schutz im Original) -> fails-open auf Original:
check('meta-leak output -> original',
      qr.rewrite_query(Q, lambda p: OPEN + 'denkprozess' + CLOSE + 'A: hier ist die Antwort zu deiner Frage\nIch formuliere um. ' + A) == Q)
check('station down -> original', qr.rewrite_query(Q, None) == Q)

def boom(p):
    raise RuntimeError('station down')

check('exception -> original', qr.rewrite_query(Q, boom) == Q)
check('port-9220 not rewritten', qr.rewrite_query('port 9220', boom) == 'port 9220')
check('short not rewritten', qr.rewrite_query('ab', lambda p: 'x') == 'ab')
check('long not rewritten', qr.rewrite_query('x' * 601, boom) == 'x' * 601)
check('hund case', qr.rewrite_query('wies das thema mit dem hund',
                                    lambda p: 'hund spaziergange routine futter') == 'hund spaziergange routine futter')

print()
if fails:
    print(f'FAILS ({len(fails)}):', fails)
    sys.exit(1)
print('ALLE TESTS PASS — Rekonstruktion bestaetigt')