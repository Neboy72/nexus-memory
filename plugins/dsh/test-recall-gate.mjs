/**
 * Recall gate — offline checks for the "is this human message worth a recall?" brake.
 *
 *   node test-recall-gate.mjs
 *
 * No service is contacted: `fetch` is replaced. The real ~/.nexus-memory and the
 * running app are never touched.
 *
 * Measured case this gate exists for (20.11.2026, live service):
 *   "app neu gestartet" (18 chars, 3 words) recalled five August/September
 *   entries with score 1.0 and 0.801 — irrelevant, but scored top. A score
 *   threshold cannot filter that; the query has to be worth asking.
 */

import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { Config, apply, isSubstantialQuery } from './index.js'

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

/** Run the plugin against a fake service and expose the captured pre-step handler. */
function startPlugin(config) {
  const calls = []
  const handlers = new Map()

  globalThis.fetch = async (url, init = {}) => {
    const target = String(url)
    const body = init.body ? JSON.parse(init.body) : undefined
    if (target.endsWith('/healthz')) return jsonResponse({ status: 'ok' })
    if (target.endsWith('/enroll')) {
      calls.push({ kind: 'enroll' })
      return jsonResponse({ key: 'test-agent-key' })
    }
    if (target.endsWith('/mcp')) {
      const method = body?.method
      if (method === 'tools/call') {
        calls.push({ kind: 'tools/call', name: body.params?.name, query: body.params?.arguments?.query })
        return jsonResponse({
          jsonrpc: '2.0',
          id: body.id,
          result: {
            content: [
              {
                type: 'text',
                text: JSON.stringify({
                  results: [
                    {
                      id: 'one',
                      text: 'software: browser-use daemon (status=neu gestartet)',
                      score: 1.0,
                      category: 'entity',
                      source: 'hermes-plugin-session-end',
                      created_at: '2026-08-29T23:30:57Z',
                      access_level: 'private',
                      verification: 'unchecked',
                    },
                  ],
                }),
              },
            ],
          },
        })
      }
      return jsonResponse({ jsonrpc: '2.0', id: body?.id, result: {} }, {})
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
  return { calls, handlers }
}

async function withHarness(configOverrides, run) {
  const dataDir = mkdtempSync(path.join(os.tmpdir(), 'dsh-gate-'))
  const keyFile = path.join(dataDir, 'service_auth.key')
  writeFileSync(keyFile, INSTALL_KEY, { encoding: 'utf8', mode: 0o600 })
  const originalFetch = globalThis.fetch
  try {
    const config = { transport: 'http', dataDir, keyFile, ...configOverrides }
    await run(startPlugin(config))
  } finally {
    globalThis.fetch = originalFetch
    rmSync(dataDir, { recursive: true, force: true })
  }
}

/** Drive the pre-step hook once with one human message. */
async function runPreStep(handlers, text) {
  const handler = handlers.get('agent/pre-step')
  assert.equal(typeof handler, 'function', 'plugin registered agent/pre-step')
  const messages = [humanMessage(text)]
  const decision = await handler(
    { step: 1, messages, signal: new AbortController().signal },
    async () => ({ messages }),
  )
  return decision
}

const cases = []

cases.push([
  'default gate: a bare status line is not worth a recall',
  async () => {
    assert.equal(Config({}).recallMinChars, undefined, 'no hard-coded schema default, the code defaults apply')
    assert.equal(isSubstantialQuery('app neu gestartet', 30, 4), false, '18 chars, 3 words -> skip')
    assert.equal(isSubstantialQuery('ok', 30, 4), false)
    assert.equal(isSubstantialQuery('   ', 30, 4), false)
  },
])

cases.push([
  'default gate: a real question passes',
  async () => {
    assert.equal(
      isSubstantialQuery('wie sichere ich das Nexus Memory auf die Synology?', 30, 4),
      true,
    )
    assert.equal(
      isSubstantialQuery('was ist der Standardwert für das Bot-Gedächtnis', 30, 4),
      true,
    )
  },
])

cases.push([
  'end to end: the short message never reaches the service',
  async () => {
    await withHarness({}, async ({ calls, handlers }) => {
      const decision = await runPreStep(handlers, 'app neu gestartet')
      const recalls = calls.filter((c) => c.kind === 'tools/call' && c.name === 'recall')
      assert.equal(recalls.length, 0, 'no recall for a status line')
      assert.equal(decision.messages.length, 1, 'nothing injected')
    })
  },
])

cases.push([
  'end to end: a real question is recalled and injected',
  async () => {
    await withHarness({}, async ({ calls, handlers }) => {
      const decision = await runPreStep(
        handlers,
        'wie sichere ich das Nexus Memory auf die Synology?',
      )
      const recalls = calls.filter((c) => c.kind === 'tools/call' && c.name === 'recall')
      assert.equal(recalls.length, 1, 'one recall')
      assert.equal(recalls[0].query, 'wie sichere ich das Nexus Memory auf die Synology?')
      assert.equal(decision.messages.length, 2, 'one block injected')
      const injected = decision.messages[1]
      assert.equal(injected.source?.kind, 'nexus-memory', 'attributed, not silent')
    })
  },
])

cases.push([
  'thresholds are configurable',
  async () => {
    await withHarness({ recallMinChars: 5, recallMinWords: 1 }, async ({ calls, handlers }) => {
      await runPreStep(handlers, 'status?')
      const recalls = calls.filter((c) => c.kind === 'tools/call' && c.name === 'recall')
      assert.equal(recalls.length, 1, 'a lowered gate lets it through')
    })
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
