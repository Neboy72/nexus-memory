/**
 * Incident reporting — offline checks for `POST /events` (0.22.62).
 *
 *   node test-report-incidents.mjs
 *
 * `fetch` is replaced; no service is contacted and nothing under
 * ~/.nexus-memory is touched.
 *
 * Why this exists: the nightly learner only learns from what agents report.
 * A plugin that silently reports nothing looks exactly like a plugin that
 * works, so the reporting has to be pinned by a test that counts the calls.
 */

import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { Config, apply, incidentFromToolResult } from './index.js'

const INSTALL_KEY = 'test-installation-key'

function jsonResponse(payload) {
  return {
    ok: true,
    status: 200,
    headers: new Map([['mcp-session-id', 'test-session']]),
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  }
}

/** Run the plugin against a fake service; handlers are collected as lists. */
function startPlugin(config) {
  const calls = []
  const handlers = new Map()

  globalThis.fetch = async (url, init = {}) => {
    const target = String(url)
    const body = init.body ? JSON.parse(init.body) : undefined
    if (target.endsWith('/enroll')) {
      calls.push({ kind: 'enroll' })
      return jsonResponse({ key: 'test-agent-key' })
    }
    if (target.endsWith('/events')) {
      calls.push({ kind: 'events', body })
      return jsonResponse({ stored: 1, received: 1 })
    }
    if (target.endsWith('/healthz')) return jsonResponse({ status: 'ok' })
    return jsonResponse({})
  }

  const ctx = {
    logger: { warn() {}, debug() {} },
    effect() {},
    on(event, handler) {
      const list = handlers.get(event) ?? []
      list.push(handler)
      handlers.set(event, list)
    },
  }
  apply(ctx, config)
  return { calls, handlers }
}

async function withHarness(configOverrides, run) {
  const dataDir = mkdtempSync(path.join(os.tmpdir(), 'dsh-report-'))
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

/** Deliver one session event to every registered session/event handler. */
async function emit(handlers, session, event) {
  for (const handler of handlers.get('session/event') ?? []) {
    await handler(session, event)
  }
  await new Promise((resolve) => setTimeout(resolve, 20))
}

const session = { id: 'session-1' }

function toolCall(callId, name) {
  return { type: 'tool/call', data: { turn: 1, step: 1, callId, name, arguments: '{}' } }
}

function toolResult(callId, content, extra = {}) {
  return {
    type: 'tool/result',
    data: {
      turn: 1,
      step: 1,
      message: { role: 'tool', toolCallId: callId, content, ...extra },
    },
  }
}

const cases = []

cases.push([
  'a successful tool result is not reported',
  async () => {
    assert.equal(
      incidentFromToolResult({
        tool: 'terminal',
        message: { isError: false, content: [{ type: 'text', text: 'ok' }] },
        session: 's',
      }),
      null,
    )
  },
])

cases.push([
  'a failed result becomes one incident, whitespace collapsed',
  async () => {
    const incident = incidentFromToolResult({
      tool: 'terminal',
      message: {
        isError: true,
        content: [{ type: 'text', text: 'Traceback\n\n  ModuleNotFoundError: No module named \'requests\'' }],
      },
      session: 's1',
    })
    assert.equal(incident.tool, 'terminal')
    assert.equal(incident.ok, false)
    assert.equal(incident.session, 's1')
    assert.ok(!incident.error.includes('\n'), 'one line')
    assert.ok(incident.error.includes("No module named 'requests'"))
    assert.equal(incident.agent, undefined, 'the service names the agent from the key')
  },
])

cases.push([
  'a platform error object counts even without isError',
  async () => {
    const incident = incidentFromToolResult({
      tool: 'read_file',
      message: { content: [] },
      error: { name: 'ToolError', code: 'NOT_FOUND', reason: 'file is not there' },
      session: 's2',
      maxChars: 20,
    })
    assert.ok(incident, 'reported')
    assert.equal(incident.error.length, 20, 'capped')
    assert.ok(incident.error.startsWith('file is not there'), 'reason comes first')
    // Ohne Kappung steht der Code mit drin.
    const full = incidentFromToolResult({
      tool: 'read_file',
      message: { content: [] },
      error: { name: 'ToolError', code: 'NOT_FOUND', reason: 'file is not there' },
      session: 's2',
    })
    assert.ok(full.error.includes('code=NOT_FOUND'))
  },
])

cases.push([
  'no tool name means no report',
  async () => {
    assert.equal(
      incidentFromToolResult({
        tool: '',
        message: { isError: true, content: [{ type: 'text', text: 'boom' }] },
        session: 's',
      }),
      null,
    )
  },
])

cases.push([
  'end to end: one failed call reaches POST /events',
  async () => {
    await withHarness({}, async ({ calls, handlers }) => {
      await emit(handlers, session, toolCall('call-1', 'terminal'))
      await emit(
        handlers,
        session,
        toolResult('call-1', [{ type: 'text', text: 'exit code 1: command not found' }], {
          isError: true,
        }),
      )
      const posted = calls.filter((call) => call.kind === 'events')
      assert.equal(posted.length, 1, 'exactly one report')
      assert.equal(posted[0].body.tool, 'terminal', 'name from the tool/call event')
      assert.equal(posted[0].body.ok, false)
      assert.equal(posted[0].body.session, 'session-1')
    })
  },
])

cases.push([
  'end to end: a successful call sends nothing',
  async () => {
    await withHarness({}, async ({ calls, handlers }) => {
      await emit(handlers, session, toolCall('call-2', 'terminal'))
      await emit(handlers, session, toolResult('call-2', [{ type: 'text', text: 'ok' }]))
      assert.equal(calls.filter((call) => call.kind === 'events').length, 0)
    })
  },
])

cases.push([
  'the switch turns reporting off',
  async () => {
    await withHarness({ reportIncidents: false }, async ({ calls, handlers }) => {
      await emit(handlers, session, toolCall('call-3', 'terminal'))
      await emit(handlers, session, toolResult('call-3', [{ type: 'text', text: 'boom' }], {
        isError: true,
      }))
      assert.equal(calls.filter((call) => call.kind === 'events').length, 0)
    })
  },
])

cases.push([
  'one session cannot flood the feed',
  async () => {
    await withHarness({ reportPerSessionMax: 2 }, async ({ calls, handlers }) => {
      for (const id of ['a', 'b', 'c']) {
        await emit(handlers, session, toolCall(id, 'terminal'))
        await emit(handlers, session, toolResult(id, [{ type: 'text', text: `boom ${id}` }], {
          isError: true,
        }))
      }
      assert.equal(calls.filter((call) => call.kind === 'events').length, 2, 'capped at two')
    })
  },
])

cases.push([
  'schema: the defaults are documented, not hidden',
  async () => {
    const shape = Config({})
    assert.equal(shape.reportIncidents, undefined, 'no hard-coded schema default; the code applies true')
    assert.equal(shape.reportMaxChars, undefined)
    assert.equal(shape.reportPerSessionMax, undefined)
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

if (failed > 0) {
  console.log(`\n${failed} case(s) failed`)
  process.exitCode = 1
}
