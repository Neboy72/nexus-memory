/**
 * Tests for scripts/install_dsh_plugin.mjs.
 *
 *   node scripts/test_install_dsh_plugin.mjs
 *
 * Every case uses its own temporary home and data directory; the real
 * ~/.dsh and ~/.nexus-memory are never touched. The service is a local
 * node:http mock on 127.0.0.1 with a free port — no external network.
 */

import assert from 'node:assert/strict'
import { spawn, spawnSync } from 'node:child_process'
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} from 'node:fs'
import http from 'node:http'
import os from 'node:os'
import path from 'node:path'

const SCRIPT = path.join(import.meta.dirname, 'install_dsh_plugin.mjs')
const INSTALL_KEY = 'install-key-for-tests'

const cases = []
function test(name, fn) {
  cases.push({ name, fn })
}

/** Run the installer; the parent event loop stays free for the mock server. */
function runInstaller(args, env) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [SCRIPT, ...args], { env })
    let stdout = ''
    let stderr = ''
    child.stdout.on('data', (chunk) => {
      stdout += chunk
    })
    child.stderr.on('data', (chunk) => {
      stderr += chunk
    })
    child.on('error', reject)
    child.on('close', (status) => resolve({ status, stdout, stderr }))
  })
}

/** Isolated home/data dir with an optional profile and env file. */
function makeSandbox({ profile = 'desktop', envText } = {}) {
  const root = mkdtempSync(path.join(os.tmpdir(), 'nexus-dsh-install-'))
  const home = path.join(root, 'home')
  const dataDir = path.join(root, 'data')
  mkdirSync(home, { recursive: true })
  mkdirSync(dataDir, { recursive: true })

  const profileDir =
    profile === null ? null : path.join(home, '.dsh', 'profiles', profile)
  if (profileDir !== null) {
    mkdirSync(profileDir, { recursive: true })
  }
  if (envText !== undefined && profileDir !== null) {
    writeFileSync(path.join(profileDir, '.env'), envText, 'utf8')
  }

  return {
    root,
    home,
    dataDir,
    profileDir,
    cleanup() {
      rmSync(root, { recursive: true, force: true })
    },
  }
}

function childEnv(sandbox) {
  return {
    ...process.env,
    HOME: sandbox.home,
    USERPROFILE: sandbox.home,
    NEXUS_DATA_DIR: sandbox.dataDir,
    NEXUS_SERVICE_AUTH_KEY: INSTALL_KEY,
  }
}

/**
 * Mock service. `enroll` receives the parsed body and the call number and
 * returns `{status, body}`. `embed` does the same for `POST /embed`; its
 * default is 401 — "no key of this agent works here yet", the state a fresh
 * machine is in, so the installer enrols as it always did.
 */
async function startMock(
  enroll,
  embed = () => ({ status: 401, body: { error: 'unauthorized' } }),
) {
  const requests = []
  const embedRequests = []
  const server = http.createServer((request, response) => {
    if (request.method === 'GET' && request.url === '/healthz') {
      response.writeHead(200, { 'content-type': 'application/json' })
      response.end(JSON.stringify({ status: 'ok', qdrant: true }))
      return
    }
    if (request.method === 'POST' && request.url === '/embed') {
      let raw = ''
      request.on('data', (chunk) => {
        raw += chunk
      })
      request.on('end', () => {
        let body
        try {
          body = JSON.parse(raw || '{}')
        } catch {
          response.writeHead(400, { 'content-type': 'application/json' })
          response.end(JSON.stringify({ error: 'body must be JSON' }))
          return
        }
        embedRequests.push({ headers: request.headers, body })
        const answer = embed(body, embedRequests.length)
        response.writeHead(answer.status ?? 200, {
          'content-type': 'application/json',
        })
        response.end(JSON.stringify(answer.body ?? answer))
      })
      return
    }
    if (request.method === 'POST' && request.url === '/enroll') {
      let raw = ''
      request.on('data', (chunk) => {
        raw += chunk
      })
      request.on('end', () => {
        let body
        try {
          body = JSON.parse(raw || '{}')
        } catch {
          response.writeHead(400, { 'content-type': 'application/json' })
          response.end(JSON.stringify({ error: 'body must be JSON' }))
          return
        }
        requests.push({ headers: request.headers, body })
        const answer = enroll(body, requests.length)
        response.writeHead(answer.status ?? 200, {
          'content-type': 'application/json',
        })
        response.end(JSON.stringify(answer.body ?? answer))
      })
      return
    }
    response.writeHead(404, { 'content-type': 'application/json' })
    response.end(JSON.stringify({ error: 'not found' }))
  })

  await new Promise((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', resolve)
  })
  const url = `http://127.0.0.1:${server.address().port}`

  return {
    url,
    requests,
    embedRequests,
    async close() {
      await new Promise((resolve) => server.close(resolve))
    },
  }
}

/** Lines of a .env file, split on the platform's line ending. */
function envLines(file) {
  return readFileSync(file, 'utf8').split(/\r?\n/)
}

function agentKeyLines(file) {
  return envLines(file).filter((line) => /^\s*(?:export\s+)?NEXUS_AGENT_KEY\s*=/.test(line))
}

function modeOf(file) {
  return statSync(file).mode & 0o777
}

// --- Case 0: syntax check ------------------------------------------------

test('installer passes node --check', () => {
  const result = spawnSync(process.execPath, ['--check', SCRIPT], {
    encoding: 'utf8',
  })
  assert.equal(result.status, 0, result.stderr)
})

// --- Case 1: dry run without a profile ----------------------------------

test('dry run without profile: exit 2, nothing created', async () => {
  const sandbox = makeSandbox({ profile: null })
  try {
    const result = await runInstaller(['--dry-run'], childEnv(sandbox))
    assert.equal(result.status, 2, result.stderr)
    assert.match(result.stderr, /Profil nicht gefunden/)
    assert.equal(existsSync(path.join(sandbox.home, '.dsh')), false)
    assert.equal(existsSync(path.join(sandbox.dataDir, 'agent_keys')), false)
  } finally {
    sandbox.cleanup()
  }
})

// --- Case 2: enrol against the mock -------------------------------------

test('enrol stores the key and sets exactly one NEXUS_AGENT_KEY line', async () => {
  const sandbox = makeSandbox({ envText: 'OLLAMA_API_KEY=keep-me\nOTHER=1\n' })
  const mock = await startMock(() => ({
    body: { agent_id: 'abc123', key: 'K-TEST', created: true },
  }))
  try {
    const result = await runInstaller(
      ['--base-url', mock.url],
      childEnv(sandbox),
    )
    assert.equal(result.status, 0, result.stderr)

    assert.equal(mock.requests.length, 1)
    assert.equal(mock.requests[0].body.harness, 'dsh')
    assert.equal(mock.requests[0].headers['x-nexus-service-key'], INSTALL_KEY)

    const keyPath = path.join(sandbox.dataDir, 'agent_keys', 'dsh.key')
    assert.equal(readFileSync(keyPath, 'utf8'), 'K-TEST')
    if (process.platform !== 'win32') {
      assert.equal(modeOf(keyPath), 0o600)
      assert.equal(modeOf(path.dirname(keyPath)), 0o700)
    }

    const envPath = path.join(sandbox.profileDir, '.env')
    const keyLines = agentKeyLines(envPath)
    assert.equal(keyLines.length, 1)
    assert.equal(keyLines[0], 'NEXUS_AGENT_KEY=K-TEST')
    assert.ok(envLines(envPath).includes('OLLAMA_API_KEY=keep-me'))
    assert.ok(envLines(envPath).includes('OTHER=1'))
    assert.notEqual(readFileSync(envPath, 'utf8').charCodeAt(0), 0xfeff)

    assert.match(result.stdout, /abc123/)
    assert.match(result.stdout, /neu angelegt/)
    assert.match(result.stdout, /Harness neu starten/)
  } finally {
    await mock.close()
    sandbox.cleanup()
  }
})

// --- Case 3: second run replaces the line and backs up the key ----------

test('second run replaces the env line, adds no duplicate, backs up the key', async () => {
  const sandbox = makeSandbox({ envText: 'OLLAMA_API_KEY=keep-me\n' })
  const first = await startMock(() => ({
    body: { agent_id: 'abc123', key: 'K-TEST', created: true },
  }))
  const second = await startMock(() => ({
    body: { agent_id: 'abc123', key: 'K-TEST-2', created: false },
  }))
  try {
    const firstRun = await runInstaller(['--base-url', first.url], childEnv(sandbox))
    assert.equal(firstRun.status, 0, firstRun.stderr)

    const secondRun = await runInstaller(['--base-url', second.url], childEnv(sandbox))
    assert.equal(secondRun.status, 0, secondRun.stderr)

    const keyPath = path.join(sandbox.dataDir, 'agent_keys', 'dsh.key')
    assert.equal(readFileSync(keyPath, 'utf8'), 'K-TEST-2')

    const envPath = path.join(sandbox.profileDir, '.env')
    const keyLines = agentKeyLines(envPath)
    assert.equal(keyLines.length, 1)
    assert.equal(keyLines[0], 'NEXUS_AGENT_KEY=K-TEST-2')
    assert.ok(envLines(envPath).includes('OLLAMA_API_KEY=keep-me'))

    const backups = readdirSync(path.dirname(keyPath)).filter((name) =>
      /^dsh\.key\.bak-\d{8}-\d{6}$/.test(name),
    )
    assert.equal(backups.length, 1, `backups: ${backups.join(', ')}`)
    assert.equal(
      readFileSync(path.join(path.dirname(keyPath), backups[0]), 'utf8'),
      'K-TEST',
    )

    assert.match(secondRun.stdout, /übernommen/)
  } finally {
    await first.close()
    await second.close()
    sandbox.cleanup()
  }
})

// --- Case 4: 401 leaves the env untouched -------------------------------

test('401 from the service: exit 3, no env change, no key file', async () => {
  const envText = 'OLLAMA_API_KEY=keep-me\nOTHER=1\n'
  const sandbox = makeSandbox({ envText })
  const mock = await startMock(() => ({
    status: 401,
    body: { error: 'invalid service key' },
  }))
  try {
    const result = await runInstaller(['--base-url', mock.url], childEnv(sandbox))
    assert.equal(result.status, 3, result.stderr)
    assert.match(result.stderr, /401/)
    assert.match(result.stderr, /nicht \(mehr\) gültig/)

    const envPath = path.join(sandbox.profileDir, '.env')
    assert.equal(readFileSync(envPath, 'utf8'), envText)
    assert.equal(
      existsSync(path.join(sandbox.dataDir, 'agent_keys', 'dsh.key')),
      false,
    )
  } finally {
    await mock.close()
    sandbox.cleanup()
  }
})

// --- Case 5: a key that still works is never replaced -------------------

test('valid key: no /enroll, env line added, key file byte-identical', async () => {
  const sandbox = makeSandbox({ envText: 'OLLAMA_API_KEY=keep-me\n' })
  const keyPath = path.join(sandbox.dataDir, 'agent_keys', 'dsh.key')
  mkdirSync(path.dirname(keyPath), { recursive: true })
  writeFileSync(keyPath, 'K-EXISTING', 'utf8')
  const keyBefore = readFileSync(keyPath)

  const mock = await startMock(
    () => ({ body: { agent_id: 'unexpected', key: 'K-NEW', created: true } }),
    () => ({ body: { embedding: [0.1, 0.2], model: 'mock', dim: 2 } }),
  )
  try {
    const result = await runInstaller(['--base-url', mock.url], childEnv(sandbox))
    assert.equal(result.status, 0, result.stderr)

    assert.equal(mock.requests.length, 0, '/enroll must not be called')
    assert.equal(mock.embedRequests.length, 1)
    assert.equal(
      mock.embedRequests[0].headers['x-nexus-service-key'],
      'K-EXISTING',
    )

    assert.deepEqual(readFileSync(keyPath), keyBefore)
    assert.deepEqual(readdirSync(path.dirname(keyPath)), ['dsh.key'])

    const envPath = path.join(sandbox.profileDir, '.env')
    const keyLines = agentKeyLines(envPath)
    assert.equal(keyLines.length, 1)
    assert.equal(keyLines[0], 'NEXUS_AGENT_KEY=K-EXISTING')
    assert.ok(envLines(envPath).includes('OLLAMA_API_KEY=keep-me'))

    assert.match(result.stdout, /kein neuer Agent angelegt/)
  } finally {
    await mock.close()
    sandbox.cleanup()
  }
})

test('valid key and matching env line: .env is left untouched', async () => {
  const envText = 'OLLAMA_API_KEY=keep-me\nNEXUS_AGENT_KEY=K-EXISTING\n'
  const sandbox = makeSandbox({ envText })
  const keyPath = path.join(sandbox.dataDir, 'agent_keys', 'dsh.key')
  mkdirSync(path.dirname(keyPath), { recursive: true })
  writeFileSync(keyPath, 'K-EXISTING', 'utf8')

  const mock = await startMock(
    () => ({ body: { agent_id: 'unexpected', key: 'K-NEW', created: true } }),
    () => ({ body: { embedding: [0.1], model: 'mock', dim: 1 } }),
  )
  try {
    const result = await runInstaller(['--base-url', mock.url], childEnv(sandbox))
    assert.equal(result.status, 0, result.stderr)
    assert.equal(mock.requests.length, 0)
    assert.equal(
      readFileSync(path.join(sandbox.profileDir, '.env'), 'utf8'),
      envText,
    )
    assert.match(result.stdout, /Keine Änderung nötig/)
  } finally {
    await mock.close()
    sandbox.cleanup()
  }
})

// --- Case 6: a rejected key enrols and backs up the old one -------------

test('401 from /embed: enrol runs and the old key is backed up', async () => {
  const sandbox = makeSandbox({ envText: 'OLLAMA_API_KEY=keep-me\n' })
  const keyPath = path.join(sandbox.dataDir, 'agent_keys', 'dsh.key')
  mkdirSync(path.dirname(keyPath), { recursive: true })
  writeFileSync(keyPath, 'K-OLD', 'utf8')

  const mock = await startMock(() => ({
    body: { agent_id: 'abc123', key: 'K-NEW', created: true },
  }))
  try {
    const result = await runInstaller(['--base-url', mock.url], childEnv(sandbox))
    assert.equal(result.status, 0, result.stderr)

    assert.equal(mock.embedRequests.length, 1)
    assert.equal(mock.embedRequests[0].headers['x-nexus-service-key'], 'K-OLD')
    assert.equal(mock.requests.length, 1)

    assert.equal(readFileSync(keyPath, 'utf8'), 'K-NEW')
    const backups = readdirSync(path.dirname(keyPath)).filter((name) =>
      /^dsh\.key\.bak-\d{8}-\d{6}$/.test(name),
    )
    assert.equal(backups.length, 1, `backups: ${backups.join(', ')}`)
    assert.equal(
      readFileSync(path.join(path.dirname(keyPath), backups[0]), 'utf8'),
      'K-OLD',
    )
    assert.equal(
      agentKeyLines(path.join(sandbox.profileDir, '.env'))[0],
      'NEXUS_AGENT_KEY=K-NEW',
    )
  } finally {
    await mock.close()
    sandbox.cleanup()
  }
})

// --- Case 7: --force-enroll skips the probe -----------------------------

test('--force-enroll replaces a working key without probing', async () => {
  const sandbox = makeSandbox()
  const keyPath = path.join(sandbox.dataDir, 'agent_keys', 'dsh.key')
  mkdirSync(path.dirname(keyPath), { recursive: true })
  writeFileSync(keyPath, 'K-OLD', 'utf8')

  const mock = await startMock(
    () => ({ body: { agent_id: 'abc123', key: 'K-NEW', created: true } }),
    () => ({ body: { embedding: [0.1], model: 'mock', dim: 1 } }),
  )
  try {
    const result = await runInstaller(
      ['--base-url', mock.url, '--force-enroll'],
      childEnv(sandbox),
    )
    assert.equal(result.status, 0, result.stderr)
    assert.equal(mock.embedRequests.length, 0)
    assert.equal(mock.requests.length, 1)
    assert.equal(readFileSync(keyPath, 'utf8'), 'K-NEW')
  } finally {
    await mock.close()
    sandbox.cleanup()
  }
})

// --- Runner -------------------------------------------------------------

let failed = 0
for (const entry of cases) {
  try {
    await entry.fn()
    console.log(`ok - ${entry.name}`)
  } catch (error) {
    failed += 1
    console.error(`not ok - ${entry.name}`)
    console.error(error?.stack || error)
  }
}

console.log(`\n${cases.length - failed}/${cases.length} Fälle grün`)
process.exitCode = failed === 0 ? 0 : 1
