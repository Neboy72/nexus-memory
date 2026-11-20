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
  DEFAULT_RECALL_EXCLUDE_CATEGORIES,
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

// 2. Auswahl (Ausschluss + Schwelle)
for (const [payload, _] of FAELLE) {
  for (const score of [0, 0.2, 0.4499, 0.45, 0.8, 1]) {
    for (const schwelle of [0, 0.45]) {
      const item = { ...payload, score, id: 'x', text: 'x' }
      const erwartet = !DEFAULT_RECALL_EXCLUDE_CATEGORIES.includes(effectiveCategory(payload))
        && (schwelle === 0 || score >= schwelle)
      assert.equal(
        passesRecallRule(item, DEFAULT_RECALL_EXCLUDE_CATEGORIES, schwelle),
        erwartet,
        `${JSON.stringify(payload)} @ ${score}, Schwelle ${schwelle}`,
      )
    }
  }
}
ok('Auswahl: Ausschluss und Mindestpunktzahl wirken wie im Dienst')

// 2b. Wissen bleibt drin, Etiketten und Protokolle nicht
for (const kategorie of ['lesson', 'pattern', 'procedure', 'belief', 'decision', 'idea']) {
  assert.equal(
    passesRecallRule({ category: kategorie, score: 0.9, id: 'x', text: 'x' },
      DEFAULT_RECALL_EXCLUDE_CATEGORIES, 0),
    true,
    `${kategorie} ist Wissen und muss durch`,
  )
}
for (const kategorie of ['session', 'entity']) {
  assert.equal(
    passesRecallRule({ category: kategorie, score: 0.9, id: 'x', text: 'x' },
      DEFAULT_RECALL_EXCLUDE_CATEGORIES, 0),
    false,
    `${kategorie} bleibt draussen`,
  )
}
ok('Ausschluss: lesson/pattern/procedure/belief bleiben drin')

// 3. Eine hohe Schwelle ergibt einen leeren Block (kein Fuellmaterial)
const alles_drunter = [
  { category: 'fact', score: 0.1, id: 'a', text: 'a' },
  { category: 'rule', score: 0.2, id: 'b', text: 'b' },
]
assert.deepEqual(
  alles_drunter.filter((r) => passesRecallRule(r, DEFAULT_RECALL_EXCLUDE_CATEGORIES, 0.45)),
  [],
)
ok('Grenzfall: kein Treffer ueber der Schwelle ergibt nichts')

// 4. Voreinstellungen des Plugins
const cfg = parseConfig({})
assert.equal(cfg.maxRecallResults, 3, 'drei Eintraege')
assert.equal(cfg.recallBudgetChars, 800, '800 Zeichen')
assert.deepEqual(cfg.recallExcludeCategories, DEFAULT_RECALL_EXCLUDE_CATEGORIES)
assert.equal(cfg.recallMinScore, DEFAULT_RECALL_MIN_SCORE)
ok('Voreinstellung: 3 Eintraege, 800 Zeichen, session/entity raus, Schwelle aus')

// 5. Abschalten und eigene Werte
const aus = parseConfig({ recallExcludeCategories: [], recallMinScore: 0 })
assert.deepEqual(aus.recallExcludeCategories, [], 'leere Liste = kein Filter')
assert.equal(aus.recallMinScore, 0, '0 = keine Schwelle')
const eigen = parseConfig({
  maxRecallResults: 7,
  recallBudgetChars: 2400,
  recallExcludeCategories: ['session'],
  recallMinScore: 0.6,
})
assert.equal(eigen.maxRecallResults, 7)
assert.equal(eigen.recallBudgetChars, 2400)
assert.deepEqual(eigen.recallExcludeCategories, ['session'])
assert.equal(eigen.recallMinScore, 0.6)
ok('Schalter: abschaltbar und frei einstellbar')

const alt = parseConfig({ maxRecallResults: 10 })
assert.equal(alt.maxRecallResults, 10, 'alte Konfiguration laedt weiter')
ok('Alte Konfiguration laedt weiter')

console.log(`\n${bestanden}/${bestanden} bestanden`)
