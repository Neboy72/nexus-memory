#!/usr/bin/env python3
"""Staffel-Fixtures fuer den LitM-Bench: 45 Sessions x N Items (16/24/32).

Chirurgisch: gleiche Struktur wie fixtures.jsonl (haus-betrieb), nur mehr
Distraktoren. Fake-Daten, Namensschema N-XXXX / DIST-<n>. Rotation
Anfang/Mitte/Ende 15/15/15, Themen-Rotation Homeinfra/Voice/Projekte.
"""
import json
import random

random.seed(42)

THEMEN = {
    'Homeinfra': [
        'Heizkreis {k} laeuft mit Vorlauf-Solltemperatur {t},{x}5 °C',
        'AP-{k}: Firmware {a}.{b}.{c} im Update-Log vermerkt',
        'Volume VOL-{k}: {x} TB Kapazitaet im Storage-Pool',
        'Backup {k} lief um {h}:30 Uhr, {y} GB gesichert',
        'VLAN {n} fuer {k}-Geraete, Range {m}0-{m}9',
    ],
    'Voice': [
        'Routine {k} startet mit Lautstaerke 0,{x}5',
        'Box {k}: Standardpegel auf 0,{x}4 gesetzt',
        'Wake Word {k}: Empfindlichkeit {x} von 10',
        'PTT-Dauer {k}: Durchschnitt {x},{y} Sekunden',
        'Sprachmodell {k}: Antwortzeit p50 {x}00 ms',
    ],
    'Projekte': [
        'Projekt {k}: Meilenstein {x} im Sprint geplant',
        'Repo {k}: Branch feature-{x} mit {y} offenen PRs',
        'Release {k}: Tag v0.{x}.{y} vorbereitet',
        'Ticket {k}-{x}: priorisiert fuer Woche {y}',
        'Budget {k}: {x}00 EUR im Quartal verbraucht',
    ],
}

NAMES = ['OST', 'WEST', 'NORD', 'SUED', 'GARAGE', 'KELLER', 'DACH', 'HUETTE',
         'WERKSTATT', 'BUERO', 'FLUR', 'KUECHE', 'BAD', 'WOHN', 'TERRASSE']


def fmt(tpl: str, i: int, d: int) -> str:
    name = NAMES[(i + d) % len(NAMES)]
    return tpl.format(k=name, t=i % 9, a=1 + i % 3, b=2 + i % 5, c=i % 9,
                      x=1 + (i + d) % 8, y=(i + d) % 9, h=(i + d) % 23,
                      n=(i + d) % 40, m=(i + d) % 9)


def mk_session(i: int, n_items: int) -> dict:
    themen_keys = list(THEMEN)
    topic = themen_keys[i % 3]
    needle_pos = ['Anfang', 'Mitte', 'Ende'][i % 3]
    needle_id = f'N-{1000 + i:04d}'
    needle_text = f'{needle_id}: ' + fmt(THEMEN[topic][i % 5], i, 0)
    mems = [{'category': 'fact', 'score': 0.92, 'text': needle_text}]
    for d in range(n_items - 1):
        dt = themen_keys[(i + d + 1) % 3]
        did = f'DIST-{(i * 100 + d):04d}'
        txt = f'{did}: ' + fmt(THEMEN[dt][(i + d) % 5], i, d + 1)
        mems.append({'category': 'graph' if d >= n_items - 3 else 'fact',
                     'score': round(0.88 - d * 0.02, 2), 'text': txt})
    random.shuffle(mems)
    idx = {'Anfang': 0, 'Mitte': len(mems) // 2, 'Ende': len(mems) - 1}[needle_pos]
    mems.remove(next(m for m in mems if m['text'].startswith(needle_id)))
    mems.insert(idx, {'category': 'fact', 'score': 0.92, 'text': needle_text})
    return {
        'session_id': f'litm-{1000 + i}',
        'needle': {'id': needle_id, 'text': needle_text, 'topic': topic,
                   'position': needle_pos},
        'memories': mems,
        'question': (f'Welcher Eintrag nennt die Details zu {NAMES[i % len(NAMES)]} '
                     'im Kontext? Nenne die ID des Eintrags im Format N-XXXX.'),
    }


if __name__ == '__main__':
    for n_items in (16, 24, 32):
        path = f'benches/litm/fixtures_{n_items}.jsonl'
        with open(path, 'w') as f:
            for i in range(45):
                f.write(json.dumps(mk_session(i, n_items), ensure_ascii=False) + '\n')
        print(f'{path}: 45 Sessions x {n_items} Items')