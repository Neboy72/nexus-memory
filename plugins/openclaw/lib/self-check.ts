/**
 * Self-report for the OpenClaw Nexus Memory plugin (parity with the Hermes
 * plugin's `write_agent_selfcheck` and the server-side watchdog
 * `src/nexus_memory/self_report.py`).
 *
 * On process start the plugin publishes a per-agent health file that the
 * watchdog reads. `ok:false` means this plugin is loaded but NOT working —
 * exactly the silent death (e.g. a broken embedder) the feature exists for:
 * the watchdog alerts the operator AND this module feeds a warning line into
 * the agent's system prompt so a broken memory never goes unnoticed.
 *
 * File contract (an external watchdog parses this — do not change casually):
 *   <data-dir>/agent-selfcheck-<agent-id>.json
 *   {agent_id, ok, reason, fix, interpreter, plugin_version, ts}
 *
 * Fail-open in every direction: a read-only home directory must never stop the
 * agent from running, so every error is logged and swallowed.
 */

import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import { fileURLToPath } from "node:url"
import { log } from "../logger.ts"

/** Verdict published by the last probe. `ok:true` is the healthy default. */
export type SelfCheckResult = { ok: boolean; reason: string; fix: string }

const HEALTHY: SelfCheckResult = { ok: true, reason: "", fix: "" }

// Last observed verdict. `buildPromptSection` reads this (via
// buildSelfCheckWarning) the same way `updateInfo` is read in runtime.ts —
// but UNLIKE the once-per-process update nudge, a broken self-check must keep
// warning on every prompt build until a healthy write clears it.
let lastResult: SelfCheckResult = { ...HEALTHY }

/**
 * Data dir: `$NEXUS_DATA_DIR` when set, else `~/.nexus-memory` — the SAME rule
 * as the Python plugin and `self_report.data_dir()`, so plugin and watchdog
 * never read/write in different places.
 */
function resolveDataDir(): string {
  const env = (process.env.NEXUS_DATA_DIR || "").trim()
  if (env) return env
  return path.join(os.homedir(), ".nexus-memory")
}

/**
 * Filesystem-safe agent id. A raw id with a path separator would escape the
 * data dir, so everything outside `[A-Za-z0-9._-]` becomes `-` and leading/
 * trailing dashes are trimmed. Empty result → "unknown".
 */
function sanitizeAgentId(raw: string): string {
  return raw.replace(/[^A-Za-z0-9._-]/g, "-").replace(/^-+|-+$/g, "") || "unknown"
}

/** Raw agent id used INSIDE the payload (filename uses the sanitized form). */
function rawAgentId(): string {
  return process.env.NEXUS_AGENT_ID || "openclaw"
}

function selfCheckPath(): string {
  return path.join(resolveDataDir(), `agent-selfcheck-${sanitizeAgentId(rawAgentId())}.json`)
}

/** Best-effort installed plugin version (never raises); "unknown" on failure. */
function readPluginVersion(): string {
  try {
    const here = path.dirname(fileURLToPath(import.meta.url))
    const candidates = [
      path.join(here, "..", "package.json"),
      path.join(here, "..", "..", "package.json"),
    ]
    for (const p of candidates) {
      if (fs.existsSync(p)) {
        const pkg = JSON.parse(fs.readFileSync(p, "utf8")) as { version?: unknown }
        if (typeof pkg.version === "string") return pkg.version
      }
    }
  } catch {
    /* fail-open */
  }
  return "unknown"
}

/** Seconds-precision UTC timestamp — same format the Python side writes. */
function isoSeconds(): string {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "Z")
}

/**
 * Publish provider health for the watchdog. Fail-open: never throws.
 *
 * The verdict is recorded for `buildSelfCheckWarning` FIRST, so a broken
 * memory still surfaces in the prompt even when the file write itself fails.
 */
export function writeSelfCheck(ok: boolean, reason: string, fix: string): void {
  lastResult = { ok: !!ok, reason, fix }
  try {
    const target = selfCheckPath()
    fs.mkdirSync(path.dirname(target), { recursive: true })
    const payload = {
      agent_id: rawAgentId(),
      ok: !!ok,
      reason,
      fix,
      interpreter: process.execPath,
      plugin_version: readPluginVersion(),
      ts: isoSeconds(),
    }
    // Atomic: write a pid-suffixed temp file, then rename over the target.
    const tmp = `${target}.tmp-${process.pid}`
    try {
      fs.writeFileSync(tmp, JSON.stringify(payload))
      fs.renameSync(tmp, target)
    } catch (err) {
      // Never leak the temp file when the write/rename fails.
      try {
        fs.unlinkSync(tmp)
      } catch {
        /* best-effort cleanup */
      }
      throw err
    }
  } catch (err) {
    log.warn(
      `self-check write skipped (non-fatal): ${err instanceof Error ? err.message : String(err)}`,
    )
  }
}

/** Record a verdict without writing a file (mirrors runtime's setUpdateCheckResult). */
export function setSelfCheckResult(result: { ok: boolean; reason: string; fix: string }): void {
  lastResult = { ok: !!result.ok, reason: result.reason, fix: result.fix }
}

/**
 * Prompt warning lines. `[]` while healthy; the full warning while broken —
 * and it KEEPS returning it on every call until a healthy write clears the
 * state (not once-per-process), so the operator cannot miss it.
 */
export function buildSelfCheckWarning(): string[] {
  if (lastResult.ok) return []
  return [
    "## Nexus Memory self-check: NOT WORKING",
    `Cause: ${lastResult.reason}`,
    `Fix: ${lastResult.fix}`,
    "Your stored memories are safe and not lost — they remain in the memory database; only this agent's access is offline.",
    "Tell your user about this and offer to run the fix.",
  ]
}

/** Test hook: restore the healthy default between cases. */
export function resetSelfCheckForTest(): void {
  lastResult = { ...HEALTHY }
}
