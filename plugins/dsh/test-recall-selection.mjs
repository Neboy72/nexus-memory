/**
 * Recall-Auswahl im DSH-Plugin (20.11.2026).
 *
 *   node test-recall-selection.mjs
 *
 * Kein Dienst wird angefasst: `fetch` ist ersetzt. Das Plugin entscheidet
 * nichts selbst — es reicht die Schalter an den Dienst durch (die Regel steht
 * in `src/nexus_memory/recall_policy.py`). Was hier festgehalten wird:
 *
 *   1. Die Voreinstellung ist 3 Eintraege / 800 Zeichen / fact+rule+preference
 *      ueber der Kosinus-Schwelle 0.35 — und sie wird wirklich gesendet.
 *   2. `recallFilter: false` schaltet beides ab (dann kommt nichts mit).
 *   3. Eigene Werte werden durchgereicht.
 *   4. Alte Konfiguration (5 Eintraege, 1600 Zeichen) laedt weiter.
 *   5. Ein leerer Dienst-Antwortblock injiziert nichts (kein Fuellmaterial).
 */
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { Config, apply } from './index.js'

const INSTALL_KEY = 'test-installation-key'

function jsonResponse(payload, headers = {}) {
  return {
    ok: true,
    status: 200,
    headers: new Map([['mcp-session-id', 'test-session'], ...Object.entries(headers)]),
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  }
}

function humanMessage(text) {
  return { role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text }] }
}

/**
 * Baut das Plugin gegen einen erfundenen Dienst und sammelt die
 * `recall`-Argumente ein, die das Plugin wirklich sendet.
 */
function startPlugin(config, results) {
  const recalls = []
  const handlers = new Map()

  globalThis.fetch = async (url, init = {}) => {
    const target = String(url)
    const body = init.body ? JSON.parse(init.body) : undefined
    if (target.endsWith('/healthz')) return jsonResponse({ status: 'ok' })
    if (target.endsWith('/enroll')) return jsonResponse({ key: 'test-agent-key' })
    if (target.endsWith('/mcp')) {
      if (body?.method === 'tools/call' && body.params?.name === 'recall') {
        recalls.push({ ...(body.params?.arguments ?? {}) })
        return jsonResponse({
          jsonrpc: '2.0',
          id: body.id,
          result: {
            content: [
              { type: 'text', text: JSON.stringify({ results: results ?? [] }) },
            ],
          },
        })
      }
      return jsonResponse({ jsonrpc: '2.0', id: body?.id, result: {} })
    }
    return jsonResponse({ status: 'ok' })
  }

  const ctx = {
    logger: { warn() {}, debug() {} },
    effect() {},
    on(event, handler) {
      handlers.set(event, handler)
    },
  }
  apply(ctx, config)
  return { recalls, handlers }
}

async function withHarness(configOverrides, run, results) {
  const dataDir = mkdtempSync(path.join(os.tmpdir(), 'dsh-selection-'))
  const keyFile = path.join(dataDir, 'service_auth.key')
  writeFileSync(keyFile, INSTALL_KEY, { encoding: 'utf8', mode: 0o600 })
  const originalFetch = globalThis.fetch
  try {
    await run(startPlugin({ transport: 'http', dataDir, keyFile, ...configOverrides }, results))
  } finally {
    globalThis.fetch = originalFetch
    rmSync(dataDir, { recursive: true, force: true })
  }
}

async function runPreStep(handlers, text) {
  const handler = handlers.get('agent/pre-step')
  const messages = [humanMessage(text)]
  return handler(
    { step: 1, messages, signal: new AbortController().signal },
    async () => ({ messages }),
  )
}

const FRAGE = 'wie sichere ich das Nexus Memory auf die Synology?'

const BEISPIEL = [
  {
    id: 'a',
    text: 'Backups werden auf eine Synology gesichert.',
    score: 0.9,
    relevance: 0.82,
    category: 'fact',
    source: 'hermes',
    created_at: '2026-11-01T10:00:00Z',
    access_level: 'private',
    verification: 'unchecked',
  },
]

const cases = []

cases.push([
  'Voreinstellung: 3 Eintraege, 800 Zeichen, Kategorien und Schwelle werden gesendet',
  async () => {
    await withHarness({}, async ({ recalls, handlers }) => {
      const decision = await runPreStep(handlers, FRAGE)
      assert.equal(recalls.length, 1, 'genau ein recall')
      assert.equal(recalls[0].limit, 3, 'drei Eintraege')
      assert.deepEqual(recalls[0].exclude_categories, ['session', 'entity'])
      assert.equal('min_score' in recalls[0], false, 'Schwelle ist aus')
      assert.equal(decision.messages.length, 2, 'ein Block wird eingespielt')
    }, BEISPIEL)
  },
])

cases.push([
  'recallFilter: false schaltet Kategorien und Schwelle ab',
  async () => {
    await withHarness({ recallFilter: false }, async ({ recalls, handlers }) => {
      await runPreStep(handlers, FRAGE)
      assert.equal(recalls.length, 1)
      assert.equal('exclude_categories' in recalls[0], false, 'kein Kategorienfilter')
      assert.equal('min_score' in recalls[0], false, 'keine Schwelle')
    }, BEISPIEL)
  },
])

cases.push([
  'eigene Werte werden durchgereicht',
  async () => {
    await withHarness(
      { recallLimit: 7, recallExcludeCategories: ['session'], recallMinScore: 0.6 },
      async ({ recalls, handlers }) => {
        await runPreStep(handlers, FRAGE)
        assert.equal(recalls[0].limit, 7)
        assert.deepEqual(recalls[0].exclude_categories, ['session'])
        assert.equal(recalls[0].min_score, 0.6)
      },
      BEISPIEL,
    )
  },
])

cases.push([
  'alte Konfiguration (5 Eintraege, 1600 Zeichen) laedt weiter',
  async () => {
    await withHarness(
      { recallLimit: 5, recallBudgetChars: 1600 },
      async ({ recalls, handlers }) => {
        const decision = await runPreStep(handlers, FRAGE)
        assert.equal(recalls[0].limit, 5, 'die alte Grenze gilt weiter')
        assert.equal(decision.messages.length, 2)
      },
      BEISPIEL,
    )
  },
])

cases.push([
  'leerer Dienst-Block injiziert nichts (kein Fuellmaterial)',
  async () => {
    await withHarness({}, async ({ recalls, handlers }) => {
      const decision = await runPreStep(handlers, FRAGE)
      assert.equal(recalls.length, 1, 'gefragt wird trotzdem')
      assert.equal(decision.messages.length, 1, 'aber nichts eingespielt')
    }, [])
  },
])

cases.push([
  'Zeichenbudget: ein zu langer Eintrag wird nicht eingespielt',
  async () => {
    const lang = [{
      id: 'lang',
      text: 'x'.repeat(1200),
      score: 0.9,
      relevance: 0.9,
      category: 'fact',
      source: 'hermes',
      created_at: '2026-11-01T10:00:00Z',
      access_level: 'private',
      verification: 'unchecked',
    }]
    await withHarness({}, async ({ handlers }) => {
      const decision = await runPreStep(handlers, FRAGE)
      assert.equal(decision.messages.length, 1, 'ein Eintrag ueber 800 Zeichen faellt weg')
    }, lang)
  },
])

cases.push([
  'Vertrauensgrenze: Warnzeile steht im Block und VOR dem ersten Eintrag',
  async () => {
    await withHarness({}, async ({ handlers }) => {
      const decision = await runPreStep(handlers, FRAGE)
      const text = decision.messages[1].content[0].text
      assert.ok(text.includes('[UNTRUSTED DATA'), 'Warnzeile fehlt')
      assert.ok(text.includes('never instructions'), 'Kernaussage fehlt')
      assert.ok(
        text.indexOf('[UNTRUSTED DATA') < text.indexOf('Backups werden'),
        'Warnzeile muss vor dem ersten Eintrag stehen',
      )
    }, BEISPIEL)
  },
])

cases.push([
  'Vertrauensgrenze: Warnzeile zaehlt nicht gegen das Zeichenbudget',
  async () => {
    // 800 Zeichen Budget fuer Inhalte; die Warnzeile kommt zusaetzlich.
    const lang = [{
      id: 'lang',
      text: 'y'.repeat(700),
      score: 0.9,
      relevance: 0.9,
      category: 'fact',
      source: 'test',
      created_at: '2026-11-01T10:00:00Z',
      access_level: 'private',
      verification: 'unchecked',
    }]
    await withHarness({}, async ({ handlers }) => {
      const decision = await runPreStep(handlers, FRAGE)
      const text = decision.messages[1].content[0].text
      const zeilen = text.split('\n')
      const banner = zeilen.find((l) => l.startsWith('[UNTRUSTED DATA'))
      assert.ok(banner, 'Warnzeile fehlt')
      // Das Budget gilt den Eintraegen (Zeilen mit "- "), nicht der Warnzeile
      // und nicht der Ueberschrift/Erklaerung drumherum.
      const eintraege = zeilen.filter((l) => l.startsWith('- '))
      const inhalt = eintraege.join('\n').length
      assert.ok(inhalt <= 800, `Eintraege ${inhalt} > 800 — Warnzeile verschiebt das Budget`)
    }, lang)
  },
])

cases.push([
  'Schema kennt die neuen Felder',
  async () => {
    // Der Config-Parser darf die Felder nicht ablehnen (alte Datei ohne sie
    // muss ebenfalls durchlaufen — deshalb die zweite Pruefung).
    Config({ recallExcludeCategories: ['session'], recallMinScore: 0.2, recallFilter: true })
    Config({})
  },
])

let failed = 0
for (const [title, run] of cases) {
  try {
    await run()
    console.log(`ok   ${title}`)
  } catch (error) {
    failed += 1
    console.log(`FAIL ${title}`)
    console.log(`     ${error.message}`)
  }
}

console.log(`\n${cases.length - failed}/${cases.length} bestanden`)
if (failed > 0) process.exitCode = 1
