#!/usr/bin/env node
/**
 * Install helper for the DSH Nexus Memory plugin — the first-start key.
 *
 * The plugin enrols itself on first start, but the MCP tool bridge in
 * `plugins/dsh/cordis.patch.yml` reads `NEXUS_AGENT_KEY` from `process.env`
 * when the harness starts. On a fresh machine that key does not exist yet,
 * so the harness would start without tools. This script does the one-time
 * step before the first start:
 *
 *   1. find the DSH profile,
 *   2. install the plugin's runtime dependencies in the folder the profile
 *      registers (`npm install --omit=dev`),
 *   3. read the installation key,
 *   4. check the running service,
 *   5. enrol this harness (`POST /enroll`),
 *   6. store the agent key and put it into the profile's `.env`.
 *
 * Pure Node, `node:` modules only — macOS, Linux and Windows.
 *
 *   node scripts/install_dsh_plugin.mjs
 *   node scripts/install_dsh_plugin.mjs --profile desktop --harness dsh
 *   node scripts/install_dsh_plugin.mjs --dry-run
 */

import { spawnSync } from 'node:child_process'
import {
  chmodSync,
  copyFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  statSync,
  writeFileSync,
} from 'node:fs'
import os from 'node:os'
import path from 'node:path'

const DEFAULT_PROFILE = 'desktop'
const DEFAULT_HARNESS = 'dsh'
const DEFAULT_BASE_URL = 'http://127.0.0.1:9122'
const HARNESS_PATTERN = /^[a-z0-9._-]{1,64}$/
const PROFILE_PATTERN = /^[A-Za-z0-9._-]{1,64}$/
// The bundle name the DSH profile depends on — its link target is the plugin
// folder the harness loads, so that is where npm install has to run.
const PLUGIN_PACKAGE = '@local/dsh-nexus-memory'
// A first npm install may download the whole tree; give it room.
const NPM_TIMEOUT_MS = 600_000
const NETWORK_TIMEOUT_MS = 10_000
const HEALTH_TIMEOUT_MS = 5_000
// A first /embed may load the model, so the probe gets more room than /enroll.
const PROBE_TIMEOUT_MS = 30_000

const USAGE = `Nexus Memory — Installations-Helfer für das DSH-Plugin

Aufruf:
  node scripts/install_dsh_plugin.mjs [Optionen]

Optionen:
  --profile <name>     DSH-Profil unter <home>/.dsh/profiles (Default: ${DEFAULT_PROFILE})
  --harness <name>     Harness-Name für /enroll und agent_keys/<name>.key (Default: ${DEFAULT_HARNESS})
  --base-url <url>     Adresse des nexus-memory-Dienstes (Default: ${DEFAULT_BASE_URL})
  --force-enroll       Auch dann neu anmelden, wenn der vorhandene Schlüssel gilt
  --dry-run            Alles prüfen, nichts schreiben, kein npm install, nicht enrolen
  --help               Diese Hilfe anzeigen

Plugin-Abhängigkeiten werden per npm install mitinstalliert; --dry-run überspringt das.

Der Installationsschlüssel kommt aus NEXUS_SERVICE_AUTH_KEY oder aus
<data-dir>/service_auth.key (data-dir: NEXUS_DATA_DIR oder ~/.nexus-memory).

Ein vorhandener Agent-Schlüssel wird vorher geprüft. Gilt er noch, wird kein
neuer Agent angelegt und der Schlüssel bleibt unverändert.
`

class ExitError extends Error {
  constructor(code, message) {
    super(message)
    this.code = code
  }
}

/** Parse the command line; every argument error is a usage error (exit 2). */
function parseArgs(argv) {
  const options = {
    profile: DEFAULT_PROFILE,
    harness: DEFAULT_HARNESS,
    baseUrl: DEFAULT_BASE_URL,
    dryRun: false,
    forceEnroll: false,
    help: false,
  }

  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i]
    if (arg === '--help' || arg === '-h') {
      options.help = true
      continue
    }
    if (arg === '--dry-run') {
      options.dryRun = true
      continue
    }
    if (arg === '--force-enroll') {
      options.forceEnroll = true
      continue
    }

    const equals = arg.indexOf('=')
    const name = equals === -1 ? arg : arg.slice(0, equals)
    const inline = equals === -1 ? undefined : arg.slice(equals + 1)

    if (name !== '--profile' && name !== '--harness' && name !== '--base-url') {
      throw new ExitError(2, `Unbekannte Option: ${arg}\n\n${USAGE}`)
    }

    let value = inline
    if (value === undefined) {
      value = argv[i + 1]
      i += 1
    }
    if (value === undefined || value === '' || value.startsWith('--')) {
      throw new ExitError(2, `${name} braucht einen Wert.\n\n${USAGE}`)
    }

    if (name === '--profile') options.profile = value
    else if (name === '--harness') options.harness = value
    else options.baseUrl = value
  }

  return options
}

/** `~` at the start of a configured path means the home directory. */
function expandHome(value) {
  if (value === '~') return os.homedir()
  if (value.startsWith('~/') || value.startsWith('~\\')) {
    return path.join(os.homedir(), value.slice(2))
  }
  return value
}

/** `<data-dir>` = NEXUS_DATA_DIR or ~/.nexus-memory. */
function resolveDataDir() {
  const configured = (process.env.NEXUS_DATA_DIR || '').trim()
  if (!configured) return path.join(os.homedir(), '.nexus-memory')
  return path.resolve(expandHome(configured))
}

function resolveProfileDir(profile) {
  if (!PROFILE_PATTERN.test(profile) || profile === '.' || profile === '..') {
    throw new ExitError(
      2,
      `Ungültiger Profilname: ${JSON.stringify(profile)} ` +
        '(erlaubt: Buchstaben, Ziffern, Punkt, Unterstrich, Bindestrich).',
    )
  }
  return path.join(os.homedir(), '.dsh', 'profiles', profile)
}

/** The installation key: environment first, then the data directory. */
function resolveInstallKey(dataDir) {
  const fromEnv = (process.env.NEXUS_SERVICE_AUTH_KEY || '').trim()
  if (fromEnv) return fromEnv

  const file = path.join(dataDir, 'service_auth.key')
  try {
    const value = readFileSync(file, 'utf8').trim()
    if (value) return value
  } catch {
    /* missing or unreadable: reported below */
  }
  return null
}

function normalizeBaseUrl(value) {
  let url
  try {
    url = new URL(value)
  } catch {
    throw new ExitError(2, `Ungültige --base-url: ${JSON.stringify(value)}`)
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') {
    throw new ExitError(2, `--base-url muss http:// oder https:// sein: ${value}`)
  }
  return url.href.replace(/\/+$/, '')
}

async function checkHealth(baseUrl) {
  let response
  try {
    response = await fetch(`${baseUrl}/healthz`, {
      signal: AbortSignal.timeout(HEALTH_TIMEOUT_MS),
    })
  } catch (error) {
    throw new ExitError(
      3,
      `Dienst nicht erreichbar unter ${baseUrl}/healthz (${describeError(error)}).`,
    )
  }

  const body = await response.text()
  let payload
  try {
    payload = JSON.parse(body)
  } catch {
    payload = undefined
  }
  if (!response.ok || payload?.status !== 'ok') {
    throw new ExitError(
      3,
      `Dienst antwortet nicht mit {"status":"ok"} (HTTP ${response.status}): ${body.trim()}`,
    )
  }
}

async function enroll(baseUrl, installKey, harness, label) {
  let response
  try {
    response = await fetch(`${baseUrl}/enroll`, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'x-nexus-service-key': installKey,
      },
      body: JSON.stringify({ harness, label }),
      signal: AbortSignal.timeout(NETWORK_TIMEOUT_MS),
    })
  } catch (error) {
    throw new ExitError(
      3,
      `Anmeldung am Dienst fehlgeschlagen (${describeError(error)}).`,
    )
  }

  const body = await response.text()
  if (response.status === 401) {
    throw new ExitError(
      3,
      'Der Installationsschlüssel ist nicht (mehr) gültig (HTTP 401). ' +
        'Prüfe NEXUS_SERVICE_AUTH_KEY bzw. <data-dir>/service_auth.key.',
    )
  }
  if (!response.ok) {
    throw new ExitError(
      3,
      `Anmeldung fehlgeschlagen (HTTP ${response.status}): ${body.trim()}`,
    )
  }

  let payload
  try {
    payload = JSON.parse(body)
  } catch {
    throw new ExitError(3, `Antwort von /enroll ist kein JSON: ${body.trim()}`)
  }
  if (
    typeof payload.agent_id !== 'string' ||
    typeof payload.key !== 'string' ||
    !payload.key.trim()
  ) {
    throw new ExitError(3, `Antwort von /enroll unvollständig: ${body.trim()}`)
  }
  return {
    agentId: payload.agent_id,
    key: payload.key.trim(),
    created: payload.created === true,
  }
}

/**
 * Ask the service whether `agentKey` still works: a tiny `POST /embed`.
 * `200` means the key is good, `401`/`403` means it is not. Any other answer
 * aborts — guessing here either creates a duplicate agent or skips an
 * enrolment that is actually needed.
 */
async function probeAgentKey(baseUrl, agentKey) {
  let response
  try {
    response = await fetch(`${baseUrl}/embed`, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'x-nexus-service-key': agentKey,
      },
      body: JSON.stringify({ text: 'installer probe' }),
      signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
    })
  } catch (error) {
    throw new ExitError(
      3,
      `Vorhandener Schlüssel nicht prüfbar: ${baseUrl} antwortet nicht ` +
        `(${describeError(error)}). Es wurde nichts geändert.`,
    )
  }

  if (response.status === 200) return 'valid'
  if (response.status === 401 || response.status === 403) return 'invalid'
  const body = await response.text()
  throw new ExitError(
    3,
    `Vorhandener Schlüssel nicht prüfbar (HTTP ${response.status}): ` +
      `${body.trim()}. Es wurde nichts geändert.`,
  )
}

/** Read `<data-dir>/agent_keys/<harness>.key`; null when missing or empty. */
function readAgentKey(keyPath) {
  try {
    const value = readFileSync(keyPath, 'utf8').trim()
    return value || null
  } catch {
    return null
  }
}

/** Local timestamp for the backup name: YYYYMMDD-HHMMSS. */
function backupStamp(date = new Date()) {
  const pad = (value) => String(value).padStart(2, '0')
  return (
    `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}` +
    `-${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`
  )
}

/** Write `<data-dir>/agent_keys/<harness>.key`, backing up an existing file. */
function writeAgentKey(keyPath, key) {
  const dir = path.dirname(keyPath)
  mkdirSync(dir, { recursive: true, mode: 0o700 })
  try {
    chmodSync(dir, 0o700)
  } catch {
    /* Windows: mode is advisory, not an error */
  }

  let backupPath = null
  if (existsSync(keyPath)) {
    backupPath = `${keyPath}.bak-${backupStamp()}`
    copyFileSync(keyPath, backupPath)
    try {
      chmodSync(backupPath, 0o600)
    } catch {
      /* Windows: mode is advisory, not an error */
    }
  }

  writeFileSync(keyPath, key, { encoding: 'utf8', mode: 0o600 })
  try {
    chmodSync(keyPath, 0o600)
  } catch {
    /* Windows: mode is advisory, not an error */
  }
  return backupPath
}

const AGENT_KEY_LINE = /^\s*(?:export\s+)?NEXUS_AGENT_KEY\s*=(.*)$/

/**
 * Make sure `.env` carries exactly one `NEXUS_AGENT_KEY=<key>` line.
 * Returns false when it already says exactly that — the file is then not
 * touched at all (no rewrite, no newline normalisation).
 */
function ensureAgentKeyLine(envPath, key) {
  if (existsSync(envPath)) {
    let text = readFileSync(envPath, 'utf8')
    if (text.charCodeAt(0) === 0xfeff) text = text.slice(1)
    const values = []
    for (const line of text.split(/\r?\n/)) {
      const match = AGENT_KEY_LINE.exec(line)
      if (match) values.push(match[1].trim())
    }
    if (values.length === 1 && values[0] === key) return false
  }
  writeEnvFile(envPath, key)
  return true
}

/**
 * Set exactly one `NEXUS_AGENT_KEY=` line in the profile's `.env`.
 * Every other line stays as it was; the file is written without a BOM and
 * with the platform's line ending.
 */
function writeEnvFile(envPath, key) {
  let lines = []
  if (existsSync(envPath)) {
    let text = readFileSync(envPath, 'utf8')
    if (text.charCodeAt(0) === 0xfeff) text = text.slice(1)
    lines = text.split(/\r?\n/)
    // Only the empty entry created by a final newline is dropped; blank
    // lines inside the file are content and stay.
    if (lines.length > 0 && lines[lines.length - 1] === '') lines.pop()
  }

  const wanted = `NEXUS_AGENT_KEY=${key}`
  const output = []
  let replaced = false
  for (const line of lines) {
    if (AGENT_KEY_LINE.test(line)) {
      if (!replaced) {
        output.push(wanted)
        replaced = true
      }
      continue
    }
    output.push(line)
  }
  if (!replaced) output.push(wanted)

  writeFileSync(envPath, output.join(os.EOL) + os.EOL, 'utf8')
}

/**
 * The folder the profile's `dependencies[PLUGIN_PACKAGE]` points at — `link:`
 * or `file:` prefix, `~` allowed. That target is the bundle folder the
 * harness loads, so its own dependencies have to live there too.
 * Returns null while the profile does not register the plugin yet.
 */
function resolvePluginDir(profileDir) {
  let manifest
  try {
    manifest = JSON.parse(readFileSync(path.join(profileDir, 'package.json'), 'utf8'))
  } catch {
    return null
  }
  const spec = manifest?.dependencies?.[PLUGIN_PACKAGE]
  const link = typeof spec === 'string' ? /^(?:link|file):(.+)$/.exec(spec.trim()) : null
  // A registry spec needs no separate install — npm fetched its dependencies
  // when the profile was built.
  if (!link) return null
  return path.resolve(profileDir, expandHome(link[1]))
}

/**
 * Install the plugin's runtime dependencies where the harness loads them.
 * `dsh plugin add` links the bundle instead of copying it, so npm never
 * fetches its `dependencies` — without them the harness starts without tools.
 */
function installRuntimeDependencies(pluginDir, { dryRun = false, log = console.log } = {}) {
  if (dryRun) {
    log(`würde npm install in ${pluginDir} ausführen`)
    return
  }

  const result = spawnSync('npm', ['install', '--omit=dev', '--no-audit', '--no-fund'], {
    cwd: pluginDir,
    // Windows only finds npm through npm.cmd, which needs a shell.
    shell: process.platform === 'win32',
    timeout: NPM_TIMEOUT_MS,
    encoding: 'utf8',
    // npm's progress goes straight to the user; only the error tail is kept.
    stdio: ['ignore', 'inherit', 'pipe'],
  })

  if (!result.error && result.status === 0) {
    log(`Abhängigkeiten installiert in ${pluginDir}`)
    return
  }

  let reason
  if (result.error) {
    reason =
      result.error.code === 'ENOENT'
        ? existsSync(pluginDir)
          ? 'npm nicht gefunden'
          : `Plugin-Ordner fehlt: ${pluginDir}`
        : result.error.code === 'ETIMEDOUT'
          ? `Zeitüberschreitung (${NPM_TIMEOUT_MS / 1000} s)`
          : result.error.message || String(result.error)
  } else if (result.signal) {
    reason = `abgebrochen (${result.signal})`
  } else {
    reason = `npm endete mit Status ${result.status}`
  }

  const tail = (result.stderr || '')
    .trim()
    .split(/\r?\n/)
    .slice(-8)
    .join('\n')
  throw new ExitError(
    1,
    `npm install in ${pluginDir} fehlgeschlagen: ${reason}.` +
      (tail ? `\nLetzte Ausgabe:\n${tail}` : '') +
      '\nOhne diese Abhängigkeiten fehlen dem Plugin @deepseek-ai/dsh-llm und ' +
      'schemastery — der Harness startet dann ohne Werkzeuge.',
  )
}

function describeError(error) {
  if (error?.name === 'TimeoutError') return 'Zeitüberschreitung'
  if (error?.cause?.code) return error.cause.code
  return error?.message || String(error)
}

function printField(label, value) {
  console.log(`${label.padEnd(11)}${value}`)
}

async function main(argv) {
  const options = parseArgs(argv)
  if (options.help) {
    console.log(USAGE)
    return 0
  }

  if (!HARNESS_PATTERN.test(options.harness)) {
    throw new ExitError(
      2,
      `Ungültiger Harness-Name: ${JSON.stringify(options.harness)} ` +
        '(erlaubt: a-z, 0-9, Punkt, Unterstrich, Bindestrich; max. 64 Zeichen).',
    )
  }

  const baseUrl = normalizeBaseUrl(options.baseUrl)
  const profileDir = resolveProfileDir(options.profile)
  if (!existsSync(profileDir) || !statSync(profileDir).isDirectory()) {
    throw new ExitError(
      2,
      `Profil nicht gefunden: ${profileDir}\n` +
        'Lege das Profil zuerst an oder wähle mit --profile ein vorhandenes.',
    )
  }

  // Before the service: missing dependencies break the next harness start
  // whether or not the daemon is up.
  const pluginDir = resolvePluginDir(profileDir)
  if (pluginDir === null) {
    console.warn(
      `Hinweis: ${path.join(profileDir, 'package.json')} registriert ${PLUGIN_PACKAGE} ` +
        'nicht — die Plugin-Abhängigkeiten werden nicht installiert. ' +
        'Zuerst `dsh plugin --profile <name> add <pfad>` ausführen.',
    )
  } else {
    installRuntimeDependencies(pluginDir, { dryRun: options.dryRun })
  }

  const dataDir = resolveDataDir()
  const installKey = resolveInstallKey(dataDir)
  if (!installKey) {
    const keyFile = path.join(dataDir, 'service_auth.key')
    throw new ExitError(
      3,
      'Dienst läuft nicht oder Schlüssel fehlt: weder NEXUS_SERVICE_AUTH_KEY ' +
        `noch ${keyFile} enthält einen Installationsschlüssel.`,
    )
  }

  await checkHealth(baseUrl)

  const keyPath = path.join(dataDir, 'agent_keys', `${options.harness}.key`)
  const envPath = path.join(profileDir, '.env')

  // A second run on an enrolled machine must not create a second agent: the
  // service never adopts an entry that already owns a key, so only a key that
  // still fails to work (or an explicit --force-enroll) may enrol again.
  const existingKey = options.forceEnroll ? null : readAgentKey(keyPath)
  const keyIsValid = existingKey
    ? (await probeAgentKey(baseUrl, existingKey)) === 'valid'
    : false

  if (options.dryRun) {
    const backupPath =
      existingKey && !keyIsValid ? `${keyPath}.bak-${backupStamp()}` : null
    console.log('Trockenlauf — es wird nichts geschrieben und nicht enrolen.')
    printField('Profil:', profileDir)
    printField('Dienst:', `${baseUrl} (erreichbar, status ok)`)
    if (keyIsValid) {
      printField('Agent:', 'vorhandener Schlüssel gilt, kein neuer Agent angelegt')
      printField('Schlüssel:', `${keyPath} bleibt unverändert`)
      printField(
        'Umgebung:',
        `${envPath} erhält NEXUS_AGENT_KEY nur, falls die Zeile fehlt`,
      )
    } else {
      printField('Agent:', 'würde per POST /enroll angemeldet')
      printField(
        'Schlüssel:',
        backupPath
          ? `${keyPath} würde geschrieben (vorher gesichert als ${backupPath})`
          : `${keyPath} würde geschrieben`,
      )
      printField('Umgebung:', `${envPath} würde NEXUS_AGENT_KEY erhalten`)
    }
    console.log('Harness danach neu starten, dann stehen die Werkzeuge bereit.')
    return 0
  }

  if (keyIsValid) {
    const envChanged = ensureAgentKeyLine(envPath, existingKey)
    console.log('Nexus Memory — DSH-Plugin ist bereits eingerichtet.')
    printField('Agent:', 'vorhandener Schlüssel gilt, kein neuer Agent angelegt')
    printField('Schlüssel:', keyPath)
    printField(
      'Umgebung:',
      envChanged ? `${envPath} ergänzt` : `${envPath} unverändert`,
    )
    console.log(
      envChanged
        ? 'Harness neu starten, dann stehen die Werkzeuge bereit.'
        : 'Keine Änderung nötig.',
    )
    return 0
  }

  const enrolled = await enroll(
    baseUrl,
    installKey,
    options.harness,
    `${options.harness} plugin`,
  )
  const backupPath = writeAgentKey(keyPath, enrolled.key)
  writeEnvFile(envPath, enrolled.key)

  console.log('Nexus Memory — DSH-Plugin installiert.')
  printField('Agent:', `${enrolled.agentId} (${enrolled.created ? 'neu angelegt' : 'übernommen'})`)
  printField('Schlüssel:', keyPath)
  printField('Umgebung:', envPath)
  if (backupPath) printField('Sicherung:', backupPath)
  console.log('Harness neu starten, dann stehen die Werkzeuge bereit.')
  return 0
}

main(process.argv.slice(2))
  .then((code) => {
    process.exitCode = code
  })
  .catch((error) => {
    if (error instanceof ExitError) {
      console.error(error.message)
      process.exitCode = error.code
    } else {
      console.error(`Unerwarteter Fehler: ${error?.stack || error}`)
      process.exitCode = 1
    }
  })
