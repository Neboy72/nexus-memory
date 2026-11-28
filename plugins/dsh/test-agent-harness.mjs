/**
 * Agent harness name — offline checks for the configurable `/enroll` identity.
 *
 *   node test-agent-harness.mjs
 *
 * `fetch` is replaced, so no service is contacted and no network is needed.
 * Every case uses its own temporary data dir; the real ~/.nexus-memory is
 * never touched.
 */

import assert from 'node:assert/strict'
import { existsSync, mkdtempSync, rmSync, statSync, writeFileSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { normalizeConfig as Config, apply, captureText } from './index.js'

const INSTALL_KEY = 'test-installation-key'

/** Minimal HTTP response shape the plugin reads (ok/status/headers/json/text). */
function jsonResponse(payload) {
  return {
    ok: true,
    status: 200,
    headers: new Map([['mcp-session-id', 'test-session']]),
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  }
}

/** Run `apply` against a fake service and capture the /enroll bodies. */
function startPlugin(config) {
  const enrolls = []
  let resolveEnroll
  const enrolled = new Promise((resolve) => {
    resolveEnroll = resolve
  })

  globalThis.fetch = async (url, init = {}) => {
    const target = String(url)
    if (target.endsWith('/enroll')) {
      const body = JSON.parse(init.body)
      enrolls.push(body)
      resolveEnroll(body)
      return jsonResponse({ key: `key-for-${body.harness}` })
    }
    if (target.endsWith('/healthz')) return jsonResponse({ status: 'ok' })
    if (target.endsWith('/mcp')) {
      const body = JSON.parse(init.body)
      const message =
        body.id === undefined
          ? { jsonrpc: '2.0', result: {} }
          : { jsonrpc: '2.0', id: body.id, result: {} }
      return jsonResponse(message)
    }
    return jsonResponse({ status: 'ok' })
  }

  const ctx = {
    logger: { warn() {}, debug() {} },
    effect() {},
    on() {},
  }
  apply(ctx, config)
  return { enrolls, enrolled }
}

/** Wait until `predicate` holds, or fail after `timeoutMs`. */
async function waitFor(predicate, timeoutMs = 2000) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (predicate()) return
    await new Promise((resolve) => setTimeout(resolve, 10))
  }
  throw new Error('condition not met within timeout')
}

/** One isolated run: temp data dir, temp install-key file, fetch restored after. */
async function withHarness(configOverrides, run) {
  const dataDir = mkdtempSync(path.join(os.tmpdir(), 'dsh-harness-'))
  const keyFile = path.join(dataDir, 'service_auth.key')
  writeFileSync(keyFile, INSTALL_KEY, { encoding: 'utf8', mode: 0o600 })
  const originalFetch = globalThis.fetch
  try {
    const config = { transport: 'http', dataDir, keyFile, ...configOverrides }
    const plugin = startPlugin(config)
    await run({ ...plugin, dataDir })
  } finally {
    globalThis.fetch = originalFetch
    rmSync(dataDir, { recursive: true, force: true })
  }
}

const cases = []

cases.push([
  'default: no agentHarness sends harness "dsh"',
  async () => {
    assert.equal(Config({}).agentHarness, 'dsh', 'schema default is dsh')
    await withHarness({}, async ({ enrolls, enrolled }) => {
      const body = await enrolled
      assert.equal(body.harness, 'dsh')
      assert.equal(body.label, 'DeepSeek Harness')
      assert.equal(enrolls.length, 1)
    })
  },
])

cases.push([
  'agentHarness "dsh-win" sends harness "dsh-win"',
  async () => {
    await withHarness({ agentHarness: 'dsh-win' }, async ({ enrolled }) => {
      const body = await enrolled
      assert.equal(body.harness, 'dsh-win')
    })
  },
])

cases.push([
  'agent key path and permissions follow agentHarness',
  async () => {
    await withHarness({ agentHarness: 'dsh-win' }, async ({ enrolled, dataDir }) => {
      await enrolled
      const keyDir = path.join(dataDir, 'agent_keys')
      const keyPath = path.join(keyDir, 'dsh-win.key')
      await waitFor(() => existsSync(keyPath))
      assert.ok(keyPath.endsWith(path.join('agent_keys', 'dsh-win.key')))
      assert.equal(statSync(keyDir).mode & 0o777, 0o700, 'agent_keys is 0700')
      assert.equal(statSync(keyPath).mode & 0o777, 0o600, 'key file is 0600')
    })
  },
])

cases.push([
  'invalid agentHarness fails at load instead of falling back',
  async () => {
    assert.throws(() => Config({ agentHarness: 'Dsh Win' }), /regexp/)
    const dataDir = mkdtempSync(path.join(os.tmpdir(), 'dsh-harness-'))
    try {
      const ctx = { logger: { warn() {}, debug() {} }, effect() {}, on() {} }
      assert.throws(
        () => apply(ctx, { transport: 'http', dataDir, agentHarness: 'Dsh Win' }),
        /invalid agentHarness/,
      )
    } finally {
      rmSync(dataDir, { recursive: true, force: true })
    }
  },
])

cases.push([
  'captureText truncates a 2000-char turn to maxChars 1600 with an ellipsis',
  async () => {
    const text = captureText('u'.repeat(2000), 'a'.repeat(2000), 'session-1', 1, 1600)
    assert.ok(text.length <= 1600, `length ${text.length} stays within 1600`)
    assert.ok(text.endsWith('…'), 'result ends with the ellipsis character')
  },
])

let failed = 0
for (const [name, run] of cases) {
  try {
    await run()
    console.log(`ok - ${name}`)
  } catch (error) {
    failed += 1
    console.error(`not ok - ${name}`)
    console.error(error)
  }
}

if (failed > 0) {
  console.error(`\n${cases.length - failed}/${cases.length} passed, ${failed} failed`)
  process.exit(1)
}
console.log(`\n${cases.length}/${cases.length} passed`)
