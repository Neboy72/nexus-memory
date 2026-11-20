/**
 * Die Auswahlregel des Auto-Recall in OpenClaw (20.11.2026).
 *
 * OpenClaw liest Qdrant direkt und kann die verbindliche Python-Fassung
 * (`src/nexus_memory/recall_policy.py`) nicht importieren. Die Regel steht
 * deshalb hier ein zweites Mal — diese Datei haelt beide gegen dieselbe
 * Falltabelle (identisch mit `tests/test_claude_code_recall_policy.py`).
 *
 * Anlass: Nebo bekam fuenf Eintraege je Nachricht, davon oft zwei bis drei
 * ohne Bezug — Entities wie "spotify", halbe Sprachsaetze als "rule".
 */
import assert from 'node:assert/strict'

import { passesRecallRule, effectiveCategory } from './hooks/recall.ts'
import {
  DEFAULT_RECALL_CATEGORIES,
  DEFAULT_RECALL_MIN_SCORE,
  parseConfig,
} from './lib/config.ts'

const FAELLE = [
  [{ category: 'fact' }, 'fact'],
  [{ category: 'rule' }, 'rule'],
  [{ category: 'preference' }, 'preference'],
  [{ category: 'entity' }, 'entity'],
  [{ category: 'session' }, 'session'],
  [{ category: 'FACT ' }, 'fact'],
  [{}, 'fact'],
  [{ category: null }, 'fact'],
  [{ type: 'turn' }, 'session'],
  [{ type: 'turn', category: 'fact' }, 'session'],
  [{ type: 'memory', category: 'rule' }, 'rule'],
  [{ type: 'TURN', category: 'rule' }, 'session'],
]

let bestanden = 0
const ok = (name) => {
  bestanden += 1
  console.log(`PASS  ${name}`)
}

// 1. Kategorie-Aufloesung
for (const [payload, erwartet] of FAELLE) {
  assert.equal(
    effectiveCategory(payload),
    erwartet,
    `Kategorie fuer ${JSON.stringify(payload)}`,
  )
}
ok('Kategorie: Turn = session, ohne Kategorie = fact')

// 2. Auswahl (Kategorien + Schwelle)
for (const [payload, _] of FAELLE) {
  for (const score of [0, 0.2, 0.3499, 0.35, 0.8, 1]) {
    const item = { ...payload, score, id: 'x', text: 'x' }
    const erwartet = DEFAULT_RECALL_CATEGORIES.includes(effectiveCategory(payload))
      && score >= DEFAULT_RECALL_MIN_SCORE
    assert.equal(
      passesRecallRule(item, DEFAULT_RECALL_CATEGORIES, DEFAULT_RECALL_MIN_SCORE),
      erwartet,
      `${JSON.stringify(payload)} @ ${score}`,
    )
  }
}
ok('Auswahl: Kategorien und Mindestpunktzahl wirken wie im Dienst')

// 3. Nichts ueber der Schwelle -> leerer Block (kein Fuellmaterial)
const alles_drunter = [
  { category: 'fact', score: 0.1, id: 'a', text: 'a' },
  { category: 'rule', score: 0.2, id: 'b', text: 'b' },
]
assert.deepEqual(
  alles_drunter.filter((r) =>
    passesRecallRule(r, DEFAULT_RECALL_CATEGORIES, DEFAULT_RECALL_MIN_SCORE)),
  [],
)
ok('Grenzfall: kein Treffer ueber der Schwelle ergibt nichts')

// 4. Voreinstellungen des Plugins
const cfg = parseConfig({})
assert.equal(cfg.maxRecallResults, 3, 'drei Eintraege')
assert.equal(cfg.recallBudgetChars, 800, '800 Zeichen')
assert.deepEqual(cfg.recallCategories, DEFAULT_RECALL_CATEGORIES)
assert.equal(cfg.recallMinScore, DEFAULT_RECALL_MIN_SCORE)
ok('Voreinstellung: 3 Eintraege, 800 Zeichen, fact/rule/preference, 0.35')

// 5. Abschalten und eigene Werte
const aus = parseConfig({ recallCategories: [], recallMinScore: 0 })
assert.deepEqual(aus.recallCategories, [], 'leere Liste = kein Kategorienfilter')
assert.equal(aus.recallMinScore, 0, '0 = keine Schwelle')
const eigen = parseConfig({
  maxRecallResults: 7,
  recallBudgetChars: 2400,
  recallCategories: ['fact'],
  recallMinScore: 0.6,
})
assert.equal(eigen.maxRecallResults, 7)
assert.equal(eigen.recallBudgetChars, 2400)
assert.deepEqual(eigen.recallCategories, ['fact'])
assert.equal(eigen.recallMinScore, 0.6)
ok('Schalter: abschaltbar und frei einstellbar')

const alt = parseConfig({ maxRecallResults: 10 })
assert.equal(alt.maxRecallResults, 10, 'alte Konfiguration laedt weiter')
ok('Alte Konfiguration laedt weiter')

console.log(`\n${bestanden}/${bestanden} bestanden`)
