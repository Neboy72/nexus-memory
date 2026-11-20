/**
 * Nexus Memory — native auto-recall and auto-capture for the Harness.
 *
 * Auto-recall: before the first step of a turn, the latest human message is
 * searched in Nexus Memory and the best hits enter the step as one durable,
 * source-attributed user message.
 *
 * Auto-capture: when a turn ends, the human message and the final assistant
 * text are written back as one episodic `session` memory.
 *
 * Two transports, same contract:
 *   - `http`  (default): talks to a running `nexus-memory serve` daemon
 *     (MCP over Streamable HTTP on /mcp plus the gated /write endpoint).
 *     No Python, no local Qdrant — this is the portable path.
 *   - `stdio`: spawns `nexus_bridge.py`, which imports the local
 *     `nexus_memory` package. Falls back to direct store access when no
 *     daemon runs but Python has the package.
 *
 * Every failure is fail-open: a turn never waits longer than `recallTimeoutMs`
 * and never fails because memory is unavailable.
 */

import { spawn } from 'node:child_process'
import { appendFileSync, mkdirSync, readFileSync, unlinkSync, writeFileSync } from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { createUserMessage } from '@deepseek-ai/dsh-llm'
import z from '@deepseek-ai/schemastery'

export const name = 'nexus-memory'

export const inject = ['agents', 'sessions']

/** Same rule the service applies to a harness name (service_auth.py). */
const AGENT_HARNESS_PATTERN = /^[a-z0-9._-]{1,64}$/

/** Opt-in tracing for the capture path: NEXUS_PLUGIN_DEBUG=1. Never logs content. */
function debugTrace(line) {
  if (process.env.NEXUS_PLUGIN_DEBUG !== '1') return
  try {
    const dir = path.join(os.homedir(), '.nexus-memory', 'logs')
    mkdirSync(dir, { recursive: true })
    appendFileSync(path.join(dir, 'plugin-events.log'), `${new Date().toISOString()} ${line}\n`)
  } catch {
    /* tracing must never break a turn */
  }
}

export const Config = z.object({
  transport: z.string().description('http (serve daemon) or stdio (local nexus_memory package).'),
  baseUrl: z.string().description('Serve daemon base URL (default http://127.0.0.1:9122).'),
  serviceKey: z.string().description('Value for x-nexus-service-key; overrides keyFile.'),
  keyFile: z.string().description('File holding the service key (default <dataDir>/service_auth.key).'),
  accessLevel: z.string().description('public | trusted | private; used for reads and writes (default private).'),
  captureCategory: z.string().description('Memory category for captured turns (default session).'),
  recall: z.boolean().description('Enable auto-recall (default true).'),
  recallLimit: z.number().description('How many memories to inject at most (default 5).'),
  recallBudgetChars: z.number().description('Character budget of the injected block (default 1600).'),
  recallTimeoutMs: z.number().description('Give up on recall after this many milliseconds (default 4000).'),
  recallMinChars: z.number().description('Skip auto-recall when the last human message is shorter than this (default 30).'),
  recallMinWords: z.number().description('Skip auto-recall when the last human message has fewer words than this (default 4).'),
  capture: z.boolean().description('Enable auto-capture (default true).'),
  captureMinChars: z.number().description('Skip turns whose human message is shorter than this (default 12).'),
  captureMaxChars: z.number().description('Maximum stored characters per captured turn (default 1600).'),
  captureSubagents: z.boolean().description('Also capture turns of subagent sessions (default false).'),
  python: z.string().description('stdio only: Python executable that can import nexus_memory.'),
  bridge: z.string().description('stdio only: path to nexus_bridge.py (defaults to the copy beside this file).'),
  pythonPath: z.string().description('stdio only: extra PYTHONPATH entries, joined by the platform delimiter.'),
  nexusHome: z.string().description('stdio only: Nexus checkout prepended to PYTHONPATH.'),
  dataDir: z.string().description('stdio only / key lookup: NEXUS_DATA_DIR.'),
  qdrantHost: z.string().description('stdio only: Qdrant host (default 127.0.0.1).'),
  qdrantPort: z.number().description('stdio only: Qdrant port (default 6333).'),
  qdrantExe: z.string().description('stdio only: Qdrant binary the bridge may start.'),
  qdrantStorage: z.string().description('stdio only: storage directory for a bridge-started Qdrant.'),
  ollamaExe: z.string().description('stdio only: Ollama binary the bridge may start.'),
  embeddingProvider: z.string().description('stdio only: NEXUS_EMBEDDING_PROVIDER (default ollama).'),
  agentId: z.string().description('Agent id for the stdio transport (NEXUS_AGENT_ID; default dsh). Over HTTP the key names the agent.'),
  agentHarness: z
    .string()
    .default('dsh')
    .pattern(AGENT_HARNESS_PATTERN)
    .description('Harness name sent to /enroll and used for the agent key file (default dsh).'),
  agentLabel: z
    .string()
    .default('DeepSeek Harness')
    .description('Label stored for this agent at /enroll (default DeepSeek Harness).'),
  logDir: z.string().description('stdio only: directory for bridge logs.'),
  reportIncidents: z
    .boolean()
    .description('Report failed tool calls to the service (/events) so the nightly learner sees them (default true; HTTP transport only).'),
  reportMaxChars: z.number().description('Cap the error text of one report (default 2000).'),
  reportPerSessionMax: z.number().description('How many failures one session reports at most (default 50).'),
})

const REQUEST_TIMEOUT_MS = 30_000
const MAX_INJECTED_ITEMS = 10
const MCP_PROTOCOL_VERSION = '2025-06-18'
const PLUGIN_VERSION = '0.3.1'

/** Expand a leading `~` so config files can use home-relative paths. */
function expandHome(value) {
  if (typeof value !== 'string' || !value.startsWith('~')) return value
  return path.join(os.homedir(), value.slice(1).replace(/^[\\/]/, ''))
}

/** Extract plain text from one message's content blocks. */
function textOf(content) {
  if (!Array.isArray(content)) return ''
  return content
    .filter((block) => block && block.type === 'text')
    .map((block) => String(block.text ?? ''))
    .join('\n')
    .trim()
}

/** The latest human-authored message among the messages entering the step. */
function latestHumanText(messages) {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index]
    if (!message || message.role !== 'user') continue
    if (message.source?.kind !== 'user') continue
    const text = textOf(message.content)
    if (text) return text
  }
  return ''
}

/** True when a human message carries enough substance to justify a recall.
 *
 * Measured 20.11.2026: a bare status line ("app neu gestartet", 18 characters,
 * 3 words) recalled five unrelated August entries with score 1.0 and 0.801 —
 * a score threshold does not help there, only the query itself does.
 */
export function isSubstantialQuery(text, minChars, minWords) {
  const trimmed = String(text ?? '').trim()
  if (trimmed.length < minChars) return false
  const words = trimmed.split(/\s+/).filter(Boolean)
  return words.length >= minWords
}

/** Render recalled memories into one budgeted block, or '' when nothing fits. */
function renderMemories(memories, budget) {
  const lines = []
  let used = 0
  for (const memory of memories.slice(0, MAX_INJECTED_ITEMS)) {
    const text = String(memory.text ?? '')
      .replace(/\s+/g, ' ')
      .trim()
    if (!text) continue
    const day = String(memory.created_at ?? '').slice(0, 10)
    const meta = [memory.category, day].filter(Boolean).join(' · ')
    const line = `- ${meta ? `[${meta}] ` : ''}${text}`
    if (used + line.length > budget) break
    lines.push(line)
    used += line.length + 1
  }
  if (lines.length === 0) return ''
  return [
    `Nexus Memory — ${lines.length} relevant ${lines.length === 1 ? 'entry' : 'entries'} from earlier sessions:`,
    ...lines,
    'Use these as background context when relevant; prefer newer evidence when entries disagree.',
  ].join('\n')
}

/** Build the stored text for one captured turn. */
export function captureText(user, assistant, session, turn, maxChars) {
  const header = `[DSH session ${String(session).slice(0, 8)} · turn ${turn}]`
  let text = `${header}\nUser: ${user.trim()}\n\nAssistant: ${assistant.trim()}`
  if (text.length > maxChars) text = `${text.slice(0, maxChars - 1).trimEnd()}…`
  return text
}

/** Collapse whitespace so one incident stays one line. */
function collapse(text) {
  return String(text ?? '').replace(/\s+/g, ' ').trim()
}

/**
 * Turn one tool result into an incident for `POST /events`, or null.
 *
 * Only the platform's own failure marks count: `isError` on the message or an
 * `error` object on the event. This function never guesses from the text — the
 * classification (which error class, whether it is a lesson) belongs to Nexus,
 * and a plugin that tries it here would do it four times in four languages.
 *
 * `agent` is deliberately left out: the service names the agent from the key
 * that writes, so a report cannot claim to come from someone else.
 */
export function incidentFromToolResult({ tool, message, error, session, maxChars = 2000 }) {
  if (!tool) return null
  if (!error && message?.isError !== true) return null
  const teile = [textOf(message?.content), error?.reason, error?.code ? `code=${error.code}` : '']
  const text = collapse(teile.filter(Boolean).join(' '))
  if (!text) return null
  return {
    tool: String(tool).slice(0, 64),
    ok: false,
    error: text.slice(0, Math.max(1, maxChars)),
    hint: '',
    session: String(session ?? '').slice(0, 128),
  }
}

/** Race a promise against a timeout, keeping the caller's abort signal live. */
function withTimeout(promise, timeoutMs, signal) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`timed out after ${timeoutMs} ms`)), timeoutMs)
    timer.unref?.()
    const cleanup = () => {
      clearTimeout(timer)
      signal?.removeEventListener('abort', onAbort)
    }
    const onAbort = () => {
      cleanup()
      reject(signal.reason ?? new Error('aborted'))
    }
    signal?.addEventListener('abort', onAbort, { once: true })
    promise.then(
      (value) => {
        cleanup()
        resolve(value)
      },
      (error) => {
        cleanup()
        reject(error)
      },
    )
  })
}

/**
 * HTTP transport: MCP over Streamable HTTP for recall, the gated /write
 * endpoint for capture. Works against any `nexus-memory serve` instance.
 */
class HttpStore {
  constructor(ctx, options) {
    this.ctx = ctx
    this.baseUrl = options.baseUrl.replace(/\/+$/, '')
    this.accessLevel = options.accessLevel
    this.category = options.category
    this.maxChars = options.maxChars
    this.serviceKey = options.serviceKey
    this.keyFile = options.keyFile
    this.keyFromFile = undefined
    this.agentKeyFile = options.agentKeyFile
    this.harness = options.harness
    this.label = options.label
    this.sessionId = undefined
    this.initializing = undefined
  }

  /** The per-installation (or configured) key — used for ONE thing: POST /enroll. */
  installKey() {
    if (this.serviceKey !== undefined) return this.serviceKey
    if (this.keyFromFile !== undefined) return this.keyFromFile
    const candidates = [this.keyFile, path.join(expandHome('~/.nexus-memory'), 'service_auth.key')].filter(Boolean)
    for (const candidate of candidates) {
      try {
        const value = readFileSync(candidate, 'utf8').trim()
        if (value) {
          this.keyFromFile = value
          return value
        }
      } catch {
        /* try the next candidate */
      }
    }
    this.ctx.logger?.warn(
      `nexus-memory: no installation key found (looked in ${candidates.join(', ')}); set serviceKey or keyFile`,
    )
    this.keyFromFile = ''
    return ''
  }

  /** This plugin's OWN key (15.11.2026) — it names the agent, so no header does. */
  readAgentKey() {
    try {
      return readFileSync(this.agentKeyFile, 'utf8').trim()
    } catch {
      return ''
    }
  }

  writeAgentKey(key) {
    mkdirSync(path.dirname(this.agentKeyFile), { recursive: true, mode: 0o700 })
    writeFileSync(this.agentKeyFile, key, { encoding: 'utf8', mode: 0o600 })
  }

  /** Ask the service for a key of our own (the installation key's only door). */
  async enroll(signal) {
    const install = this.installKey()
    if (!install) {
      this.ctx.logger?.warn(
        'nexus-memory: cannot enrol — no installation key (set serviceKey or keyFile)',
      )
      return ''
    }
    try {
      const response = await fetch(`${this.baseUrl}/enroll`, {
        method: 'POST',
        headers: { 'content-type': 'application/json', 'x-nexus-service-key': install },
        body: JSON.stringify({ harness: this.harness, label: this.label }),
        signal: AbortSignal.any([signal, AbortSignal.timeout(10_000)].filter(Boolean)),
      })
      if (!response.ok) {
        this.ctx.logger?.warn(`nexus-memory: /enroll answered ${response.status}`)
        return ''
      }
      const payload = await response.json()
      const key = typeof payload?.key === 'string' ? payload.key.trim() : ''
      if (!key) return ''
      this.writeAgentKey(key)
      return key
    } catch (error) {
      this.ctx.logger?.warn(`nexus-memory: /enroll failed: ${String(error)}`)
      return ''
    }
  }

  /** Our own key, enrolling once when it is missing. */
  async credential(signal) {
    // Der Schluessel aus der Profil-Datei gewinnt: das Dashboard schreibt dort
    // seit 15.11.2026 den EIGENEN Schluessel des Harness hinein. Ist es noch
    // der alte Installationsschluessel, antwortet der Dienst mit 401 — dann
    // meldet sich das Plugin an (derselbe Wert dient als Installationsschluessel).
    return this.readAgentKey() || this.configuredKey() || this.enroll(signal)
  }

  /** The key from the profile (`serviceKey`), when one is configured. */
  configuredKey() {
    return typeof this.serviceKey === 'string' ? this.serviceKey.trim() : ''
  }

  headers(credential) {
    const headers = {
      'content-type': 'application/json',
      accept: 'application/json, text/event-stream',
      'x-nexus-service-key': credential,
    }
    if (this.sessionId !== undefined) headers['mcp-session-id'] = this.sessionId
    return headers
  }

  /** Read either a plain JSON-RPC body or an SSE stream carrying `data:` frames. */
  static parseRpc(text, id) {
    const trimmed = text.trim()
    if (trimmed.startsWith('{')) {
      const message = JSON.parse(trimmed)
      if (message.id === id) return message
    }
    let fallback
    for (const line of trimmed.split(/\r?\n/)) {
      if (!line.startsWith('data:')) continue
      const payload = line.slice(5).trim()
      if (!payload || payload === '[DONE]') continue
      let message
      try {
        message = JSON.parse(payload)
      } catch {
        continue
      }
      if (message.id === id) return message
      if (fallback === undefined) fallback = message
    }
    if (fallback !== undefined) return fallback
    throw new Error(`no JSON-RPC response for id ${id}: ${trimmed.slice(0, 200)}`)
  }

  /** POST with our own key; a 401 means: forget the key, enrol once, retry once. */
  async authenticatedFetch(url, body, { signal, timeoutMs = REQUEST_TIMEOUT_MS } = {}) {
    const send = (credential) =>
      fetch(url, {
        method: 'POST',
        headers: this.headers(credential),
        body: JSON.stringify(body),
        signal: AbortSignal.any([signal, AbortSignal.timeout(timeoutMs)].filter(Boolean)),
      })

    let credential = await this.credential(signal)
    let response = await send(credential)
    if (response.status === 401) {
      try {
        unlinkSync(this.agentKeyFile)
      } catch {
        /* was not there */
      }
      credential = await this.enroll(signal)
      if (credential) response = await send(credential)
    }
    return response
  }

  async post(body, options = {}) {
    const response = await this.authenticatedFetch(
      `${this.baseUrl}/mcp`, body, options)
    if (response.status === 401) {
      throw new Error(
        'Nexus rejected the agent key (401); the plugin must enrol via POST /enroll ' +
          '(check serviceKey/keyFile — the installation key cannot read data)',
      )
    }
    const text = await response.text()
    if (!response.ok) {
      throw new Error(`Nexus /mcp returned ${response.status}: ${text.slice(0, 200)}`)
    }
    const sessionId = response.headers.get('mcp-session-id')
    if (sessionId) this.sessionId = sessionId
    return text
  }

  /** One initialize handshake; concurrent callers share the same promise. */
  ensureSession(signal) {
    if (this.sessionId !== undefined) return Promise.resolve(this.sessionId)
    if (this.initializing === undefined) {
      this.initializing = (async () => {
        const text = await this.post(
          {
            jsonrpc: '2.0',
            id: 1,
            method: 'initialize',
            params: {
              protocolVersion: MCP_PROTOCOL_VERSION,
              capabilities: {},
              clientInfo: { name: 'dsh-nexus-memory', version: PLUGIN_VERSION },
            },
          },
          { signal, timeoutMs: REQUEST_TIMEOUT_MS },
        )
        HttpStore.parseRpc(text, 1)
        await this.post({ jsonrpc: '2.0', method: 'notifications/initialized' }, { signal })
        return this.sessionId
      })()
        .catch((error) => {
          this.sessionId = undefined
          throw error
        })
        .finally(() => {
          this.initializing = undefined
        })
    }
    return this.initializing
  }

  async callTool(toolName, args, { signal, timeoutMs = REQUEST_TIMEOUT_MS } = {}) {
    await this.ensureSession(signal)
    const id = 2
    const send = () =>
      this.post(
        { jsonrpc: '2.0', id, method: 'tools/call', params: { name: toolName, arguments: args } },
        { signal, timeoutMs },
      )
    let response
    try {
      response = await send()
    } catch (error) {
      // An expired/unknown session is recoverable: re-handshake once.
      this.sessionId = undefined
      await this.ensureSession(signal)
      response = await send().catch(() => {
        throw error
      })
    }
    const message = HttpStore.parseRpc(response, id)
    if (message.error) throw new Error(String(message.error.message ?? JSON.stringify(message.error)))
    const result = message.result ?? {}
    const text = textOf(result.content)
    if (!text) return result
    try {
      return JSON.parse(text)
    } catch {
      return text
    }
  }

  async ready(signal) {
    const response = await fetch(`${this.baseUrl}/healthz`, {
      signal: AbortSignal.any([signal, AbortSignal.timeout(5000)].filter(Boolean)),
    })
    if (!response.ok) throw new Error(`healthz returned ${response.status}`)
    await this.ensureSession(signal)
    return response.json().catch(() => ({}))
  }

  async recall(query, limit, signal) {
    const payload = await this.callTool(
      'recall',
      { query, limit, filter_level: this.accessLevel },
      { signal, timeoutMs: REQUEST_TIMEOUT_MS },
    )
    const results = Array.isArray(payload) ? payload : (payload?.results ?? [])
    return results.map((item) => ({
      id: String(item.id ?? ''),
      text: String(item.text ?? ''),
      score: Number(item.score ?? 0),
      category: String(item.category ?? ''),
      source: String(item.source ?? ''),
      created_at: String(item.created_at ?? ''),
      access_level: String(item.access_level ?? ''),
      verification: String(item.verification ?? ''),
    }))
  }

  async capture({ user, assistant, session, turn }) {
    const content = captureText(user, assistant, session, turn, this.maxChars)
    const response = await this.authenticatedFetch(`${this.baseUrl}/write`, {
      content,
      category: this.category,
      access_level: this.accessLevel,
      source: 'dsh-plugin',
      source_type: 'session',
    })
    const payload = await response.json().catch(() => ({}))
    if (response.status === 422) return { stored: false, reason: String(payload.reason ?? 'rejected') }
    if (!response.ok) throw new Error(`Nexus /write returned ${response.status}`)
    return { stored: payload.status === 'ok', id: String(payload.id ?? '') }
  }

  /**
   * Report one failed tool call (0.22.62). Best effort: a learner that misses
   * an incident loses nothing, a turn that waits on it loses time.
   */
  async reportIncident(incident, signal) {
    const response = await this.authenticatedFetch(`${this.baseUrl}/events`, incident, {
      signal,
      timeoutMs: 4000,
    })
    if (!response.ok) throw new Error(`Nexus /events returned ${response.status}`)
    return true
  }

  dispose() {}
}

/** stdio transport: drives `nexus_bridge.py`, which imports nexus_memory. */
class StdioStore {
  constructor(ctx, options) {
    this.ctx = ctx
    this.options = options
    this.accessLevel = options.accessLevel
    this.category = options.category
    this.maxChars = options.maxChars
    this.child = null
    this.pending = new Map()
    this.nextId = 1
    this.buffer = ''
    this.disposed = false
    this.stderrTail = ''
  }

  spawnBridge() {
    const { python, bridge, env } = this.options
    const child = spawn(python, [bridge], { env, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true })
    this.child = child
    child.stdout.setEncoding('utf8')
    child.stdout.on('data', (chunk) => this.onData(chunk))
    child.stderr.setEncoding('utf8')
    child.stderr.on('data', (chunk) => this.onStderr(chunk))
    child.on('error', (error) => {
      this.ctx.logger?.warn(`nexus-memory: bridge failed to start: ${String(error)}`)
      this.failAll(error)
    })
    child.on('exit', (code, signal) => {
      if (this.child === child) this.child = null
      const error = new Error(`nexus bridge exited (code ${code}, signal ${signal})`)
      this.failAll(error)
      if (!this.disposed && code !== 0) {
        this.ctx.logger?.warn(`nexus-memory: ${error.message}${this.stderrTail ? `\n${this.stderrTail}` : ''}`)
      }
    })
  }

  onStderr(chunk) {
    this.stderrTail = (this.stderrTail + chunk).slice(-4000)
    for (const line of chunk.split(/\r?\n/)) {
      if (line.trim()) this.ctx.logger?.debug?.(`nexus-bridge: ${line}`)
    }
  }

  onData(chunk) {
    this.buffer += chunk
    let index = this.buffer.indexOf('\n')
    while (index !== -1) {
      const line = this.buffer.slice(0, index).trim()
      this.buffer = this.buffer.slice(index + 1)
      if (line) this.onLine(line)
      index = this.buffer.indexOf('\n')
    }
  }

  onLine(line) {
    let message
    try {
      message = JSON.parse(line)
    } catch {
      this.ctx.logger?.warn(`nexus-memory: bridge wrote non-JSON: ${line.slice(0, 200)}`)
      return
    }
    const entry = this.pending.get(message.id)
    if (!entry) return
    this.pending.delete(message.id)
    clearTimeout(entry.timer)
    if (message.ok) entry.resolve(message.result)
    else entry.reject(new Error(String(message.error ?? 'bridge error')))
  }

  failAll(error) {
    for (const [id, entry] of this.pending) {
      this.pending.delete(id)
      clearTimeout(entry.timer)
      entry.reject(error)
    }
  }

  request(op, payload = {}, { timeoutMs = REQUEST_TIMEOUT_MS, signal } = {}) {
    if (this.disposed) return Promise.reject(new Error('plugin disposed'))
    if (!this.child) this.spawnBridge()
    const id = this.nextId++
    const line = `${JSON.stringify({ id, op, ...payload })}\n`
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id)
        reject(new Error(`nexus bridge timed out after ${timeoutMs} ms`))
      }, timeoutMs)
      timer.unref?.()
      this.pending.set(id, { resolve, reject, timer })
      const onAbort = () => {
        this.pending.delete(id)
        clearTimeout(timer)
        reject(signal.reason ?? new Error('aborted'))
      }
      signal?.addEventListener('abort', onAbort, { once: true })
      try {
        this.child.stdin.write(line)
      } catch (error) {
        clearTimeout(timer)
        this.pending.delete(id)
        reject(error)
      }
    })
  }

  async ready(signal) {
    return this.request('warmup', {}, { timeoutMs: 120_000, signal })
  }

  async recall(query, limit, signal) {
    const results = await this.request('recall', { query, limit }, { timeoutMs: REQUEST_TIMEOUT_MS, signal })
    return Array.isArray(results) ? results : []
  }

  async capture({ user, assistant, session, turn }) {
    return this.request(
      'capture',
      { session, turn, user, assistant },
      { timeoutMs: REQUEST_TIMEOUT_MS },
    )
  }

  /** stdio has no /events endpoint: the bridge talks JSON-RPC, not HTTP. */
  async reportIncident() {
    return false
  }

  dispose() {
    this.disposed = true
    this.failAll(new Error('plugin disposed'))
    const child = this.child
    this.child = null
    if (!child) return
    try {
      child.stdin.end()
    } catch {
      /* already closed */
    }
    const killTimer = setTimeout(() => child.kill(), 2000)
    killTimer.unref?.()
    child.once('exit', () => clearTimeout(killTimer))
  }
}

/** Resolve the stdio child environment from the row config. */
function resolveStdioOptions(config) {
  const here = path.dirname(fileURLToPath(import.meta.url))
  const qdrantHost = config.qdrantHost ?? '127.0.0.1'
  const qdrantPort = config.qdrantPort ?? 6333
  const pythonPath = [
    config.pythonPath,
    config.nexusHome ? path.join(config.nexusHome, 'src') : '',
    config.nexusHome,
  ]
    .filter((entry) => typeof entry === 'string' && entry.length > 0)
    .join(path.delimiter)
  const env = {
    ...process.env,
    PYTHONIOENCODING: 'utf-8',
    PYTHONUNBUFFERED: '1',
    PYTHONUTF8: '1',
    NEXUS_QDRANT_HOST: qdrantHost,
    NEXUS_QDRANT_PORT: String(qdrantPort),
    NEXUS_EMBEDDING_PROVIDER: config.embeddingProvider ?? 'ollama',
    NEXUS_AGENT_ID: config.agentId ?? 'dsh',
    NEXUS_PLUGIN_ACCESS_LEVEL: config.accessLevel ?? 'private',
    NEXUS_PLUGIN_CAPTURE_CATEGORY: config.captureCategory ?? 'session',
    NEXUS_PLUGIN_CAPTURE_MAX_CHARS: String(config.captureMaxChars ?? 1600),
    NEXUS_PLUGIN_CAPTURE_MIN_CHARS: String(config.captureMinChars ?? 12),
  }
  if (pythonPath) env.PYTHONPATH = pythonPath
  if (config.dataDir) env.NEXUS_DATA_DIR = config.dataDir
  if (config.logDir) env.NEXUS_PLUGIN_LOG_DIR = config.logDir
  if (config.qdrantExe) env.NEXUS_PLUGIN_QDRANT_EXE = config.qdrantExe
  if (config.qdrantStorage) env.NEXUS_PLUGIN_QDRANT_STORAGE = config.qdrantStorage
  if (config.ollamaExe) env.NEXUS_PLUGIN_OLLAMA_EXE = config.ollamaExe
  return {
    python: config.python ?? 'python',
    bridge: config.bridge ?? path.join(here, 'nexus_bridge.py'),
    env,
  }
}

/** Default and validate the harness name; an invalid one fails at load time. */
function resolveAgentHarness(config) {
  const harness = config.agentHarness ?? 'dsh'
  if (!AGENT_HARNESS_PATTERN.test(harness)) {
    throw new Error(
      `nexus-memory: invalid agentHarness ${JSON.stringify(harness)} — ` +
        'expected 1-64 characters from [a-z0-9._-]',
    )
  }
  return harness
}

/** Build the transport selected by `transport`. */
function createStore(ctx, config) {
  const agentHarness = resolveAgentHarness(config)
  const accessLevel = config.accessLevel ?? 'private'
  const category = config.captureCategory ?? 'session'
  const maxChars = config.captureMaxChars ?? 1600
  const transport = (config.transport ?? 'http').toLowerCase()
  if (transport === 'stdio') {
    return new StdioStore(ctx, { ...resolveStdioOptions(config), accessLevel, category, maxChars })
  }
  const keyFile = config.keyFile ?? (config.dataDir ? path.join(config.dataDir, 'service_auth.key') : undefined)
  // This plugin's OWN key, written by the self-enrolment (15.11.2026).
  const dataDir = config.dataDir ? expandHome(config.dataDir) : expandHome('~/.nexus-memory')
  return new HttpStore(ctx, {
    baseUrl: config.baseUrl ?? 'http://127.0.0.1:9122',
    serviceKey: config.serviceKey,
    keyFile: keyFile === undefined ? undefined : expandHome(keyFile),
    agentKeyFile: path.join(dataDir, 'agent_keys', `${agentHarness}.key`),
    harness: agentHarness,
    label: config.agentLabel ?? 'DeepSeek Harness',
    accessLevel,
    category,
    maxChars,
  })
}

export function apply(ctx, config) {
  const store = createStore(ctx, config)
  ctx.effect(() => () => store.dispose(), 'nexus-memory transport')

  // Warm the path while the user is still typing.
  store.ready().catch((error) => {
    ctx.logger?.warn(`nexus-memory: store not ready: ${String(error)}`)
  })

  const recallEnabled = config.recall !== false
  const captureEnabled = config.capture !== false
  const recallLimit = Math.max(1, Math.min(config.recallLimit ?? 5, 20))
  const recallBudget = Math.max(200, config.recallBudgetChars ?? 1600)
  const recallTimeout = Math.max(500, config.recallTimeoutMs ?? 4000)
  const recallMinChars = Math.max(4, config.recallMinChars ?? 30)
  const recallMinWords = Math.max(1, config.recallMinWords ?? 4)
  const captureSubagents = config.captureSubagents === true
  const captureMinChars = config.captureMinChars ?? 12
  const reportEnabled = config.reportIncidents !== false
  const reportMaxChars = Math.max(200, config.reportMaxChars ?? 2000)
  const reportPerSessionMax = Math.max(1, config.reportPerSessionMax ?? 50)

  /** Per-session turn buffer, flushed on turn/end. */
  const turns = new Map()

  ctx.on(
    'agent/pre-step',
    async (payload, next) => {
      const decision = await next()
      if (!recallEnabled || payload.step !== 1) return decision
      if (decision.kind === 'reject' || payload.signal.aborted) return decision
      const query = latestHumanText(decision.messages)
      if (!isSubstantialQuery(query, recallMinChars, recallMinWords)) return decision
      let memories
      try {
        memories = await withTimeout(
          store.recall(query, recallLimit, payload.signal),
          recallTimeout,
          payload.signal,
        )
      } catch (error) {
        ctx.logger?.warn(`nexus-memory: auto-recall skipped: ${String(error)}`)
        return decision
      }
      const text = renderMemories(Array.isArray(memories) ? memories : [], recallBudget)
      if (!text) return decision
      return {
        ...decision,
        messages: [
          ...decision.messages,
          createUserMessage({
            content: [{ type: 'text', text }],
            source: {
              kind: name,
              form: 'snapshot',
              sections: [{ name, text }],
            },
          }),
        ],
      }
    },
    { prepend: true },
  )

  ctx.on('session/event', (session, event) => {
    if (!captureEnabled) return
    debugTrace(
      `event=${String(event?.type)} session=${String(session?.id)} `
      + `parent=${String(session?.header?.parentSession !== undefined)} `
      + `dataKeys=${Object.keys(event?.data ?? {}).join(',')}`,
    )
    const state = turns.get(session)
    switch (event.type) {
      case 'turn/start':
        turns.set(session, { turn: event.data.turn, user: '', assistant: '' })
        return
      case 'user/message': {
        if (!state) return
        // Gemessen 20.11.2026 im Trace: role=user, source.kind=user. Die
        // ursprüngliche Bedingung war also richtig; sie bleibt, damit keine
        // Bot-Nachricht als Mensch im Speicher landet.
        debugTrace(`user/message role=${String(event.data.role)} sourceKind=${String(event.data.source?.kind)}`)
        if (event.data.source?.kind !== 'user') return
        const text = textOf(event.data.content)
        if (text) state.user = text
        return
      }
      case 'assistant/message': {
        if (!state) return
        const text = textOf(event.data.message?.content)
        if (text) state.assistant = text
        return
      }
      case 'turn/end': {
        if (!state) return
        turns.delete(session)
        debugTrace(`turn/end user=${state.user.length} assistant=${state.assistant.length}`)
        if (!captureSubagents && session.header?.parentSession !== undefined) return
        if (!state.user || !state.assistant) return
        if (state.user.trim().length < captureMinChars) return
        store
          .capture({
            session: String(session.id ?? ''),
            turn: state.turn ?? 0,
            user: state.user,
            assistant: state.assistant,
          })
          .catch((error) => {
            debugTrace(`capture failed: ${String(error)}`)
            ctx.logger?.warn(`nexus-memory: auto-capture failed: ${String(error)}`)
          })
        return
      }
      default:
        return
    }
  })

  // Failed tool calls, reported to the service so the nightly learner sees
  // them. Deliberately separate from capture: one can be wanted without the
  // other, and a broken report must never cost a turn.
  const toolNames = new Map()
  const reportedPerSession = new Map()
  ctx.on('session/event', (session, event) => {
    if (!reportEnabled) return
    if (event?.type === 'tool/call') {
      const callId = String(event.data?.callId ?? '')
      if (callId) toolNames.set(callId, String(event.data?.name ?? ''))
      return
    }
    if (event?.type !== 'tool/result') return
    const callId = String(event.data?.message?.toolCallId ?? '')
    const tool =
      (callId ? toolNames.get(callId) : '') ||
      String(event.data?.name ?? event.data?.message?.source?.name ?? '')
    if (callId) toolNames.delete(callId)
    const incident = incidentFromToolResult({
      tool,
      message: event.data?.message,
      error: event.data?.error,
      session: session?.id,
      maxChars: reportMaxChars,
    })
    if (!incident) return
    const count = (reportedPerSession.get(session) ?? 0) + 1
    if (count > reportPerSessionMax) return
    reportedPerSession.set(session, count)
    Promise.resolve()
      .then(() => store.reportIncident(incident))
      .catch((error) => debugTrace(`incident not reported: ${String(error)}`))
  })

  ctx.on('session/disposed', (session) => {
    turns.delete(session)
    reportedPerSession.delete(session)
  })
}
