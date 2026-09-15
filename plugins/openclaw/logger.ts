export type LoggerBackend = {
  info(msg: string, ...args: unknown[]): void
  warn(msg: string, ...args: unknown[]): void
  error(msg: string, ...args: unknown[]): void
  debug?(msg: string, ...args: unknown[]): void
}

const NOOP_LOGGER: LoggerBackend = {
  info() {},
  warn() {},
  error() {},
  debug() {},
}

let _backend: LoggerBackend = NOOP_LOGGER
let _debug = false

export function initLogger(backend: LoggerBackend, debug: boolean): void {
  _backend = backend
  _debug = debug
}

/** Key names whose VALUES are redacted in debug dumps (matched case-insensitively). */
const SECRET_KEYS = new Set([
  "apikey",
  "api_key",
  "token",
  "authorization",
  "password",
  "key",
])

/**
 * JSON.stringify that never throws and redacts obvious secrets.
 *
 * - Circular references → "[circular]" (tracked via a WeakSet).
 * - BigInt → its decimal string form.
 * - Secret-looking key names → "[redacted]".
 * - Any remaining failure → "[unserializable: <typeof v>]".
 *
 * Limits (documented): the redactor matches key NAMES at every depth the
 * replacer reaches, but it is best-effort — it does not scrub secrets that
 * appear inside string VALUES (e.g. a bearer token embedded in a message
 * body). The payloads logged here are shallow (≈depth 2). A value that is
 * referenced twice without a cycle is also reported as "[circular]".
 */
export function safeStringify(v: unknown): string {
  const seen = new WeakSet<object>()
  try {
    const out = JSON.stringify(
      v,
      (key, value) => {
        if (key && SECRET_KEYS.has(key.toLowerCase())) return "[redacted]"
        if (typeof value === "bigint") return value.toString()
        if (typeof value === "object" && value !== null) {
          if (seen.has(value as object)) return "[circular]"
          seen.add(value as object)
        }
        return value
      },
      2,
    )
    return out ?? String(v)
  } catch {
    return `[unserializable: ${typeof v}]`
  }
}

/**
 * Emit a debug line. Bound to the backend (an unbound method call loses
 * `this`). When the backend has no debug sink, the promoted info line is
 * TRUNCATED: the level distinction stays visible and info receives a
 * summary, never the full debug payload.
 */
function emitDebug(msg: string, ...args: unknown[]): void {
  const dbg = _backend.debug
  if (dbg) {
    dbg.bind(_backend)(msg, ...args)
  } else {
    _backend.info.bind(_backend)(`[debug-suppressed] ${String(msg).slice(0, 120)}`)
  }
}

export const log = {
  info(msg: string, ...args: unknown[]): void {
    _backend.info(`nexus: ${msg}`, ...args)
  },

  warn(msg: string, ...args: unknown[]): void {
    _backend.warn(`nexus: ${msg}`, ...args)
  },

  error(msg: string, err?: unknown, ...args: unknown[]): void {
    // Pass the WHOLE error through (stack included) — `err.message` alone
    // threw away the stack. Only absent errors are synthesized; other falsy
    // values (0, "", false) are forwarded as-is.
    if (err === undefined || err === null) err = new Error("unknown error")
    _backend.error(`nexus: ${msg}`, err, ...args)
  },

  debug(msg: string, ...args: unknown[]): void {
    if (!_debug) return
    emitDebug(`nexus [debug]: ${msg}`, ...args)
  },

  debugRequest(method: string, params: Record<string, unknown>): void {
    if (!_debug) return
    emitDebug(`nexus [debug] → ${method}`, safeStringify(params))
  },

  debugResponse(method: string, data: unknown): void {
    if (!_debug) return
    emitDebug(`nexus [debug] ← ${method}`, safeStringify(data))
  },
}