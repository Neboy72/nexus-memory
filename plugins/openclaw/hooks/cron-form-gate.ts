/**
 * Cron-Form-Gate (Astra-R6 P0, 08.09.2026)
 *
 * Unbeaufsichtigte Nachrichten (Cron-/Heartbeat-Sessions) dürfen nur als
 * festes Formular gesendet werden. Fail-closed: Kaputtes Formular → Senden
 * wird abgebrochen (cancel) + 1 Zeile ins Daily Log. Interaktive Sessions
 * (DM/Gruppe mit User) bleiben unangetastet.
 *
 * sessionKey-Formate (Log-Beweise 08.09.):
 * - Cron:   agent:main:cron:<jobId>:run:<runId>
 * - Heartbeat: agent:main:main:heartbeat
 * - DM:     agent:main:telegram:default:direct:<chatId>
 */
import { log } from "../logger.ts"
import { isPureReasoningBlock } from "./thought-filter.ts"
import { appendFileSync } from "node:fs"
import { homedir } from "node:os"
import { join } from "node:path"

const ALLOWED_TITLES = [
  "🚀 OpenClaw Release",
  "Weekly Skill Check",
  "⚠️ Balance",
  "⚠️ Snapshot",
]
const MAX_LINES = 6
const MAX_CHARS = 900

export function isUnattendedSession(sessionKey: string | undefined | null): boolean {
  if (typeof sessionKey !== "string" || !sessionKey) return false
  return sessionKey.includes(":cron:") || sessionKey.endsWith(":heartbeat")
}

export function isCompliantForm(text: string): boolean {
  if (typeof text !== "string") return false
  const trimmed = text.trim()
  if (trimmed.length < 12 || trimmed.length > MAX_CHARS) return false
  const lines = trimmed.split("\n")
  if (lines.length > MAX_LINES) return false
  const title = lines[0]
  if (!ALLOWED_TITLES.some((t) => title.startsWith(t))) return false
  // Kein Reasoning-Leak irgendwo im Text (reuse der bewährten Marker)
  for (const line of lines) {
    if (isPureReasoningBlock(line, false)) return false
  }
  return true
}

function appendDailyNote(line: string): void {
  try {
    const d = new Date()
    const ymd = d.toISOString().slice(0, 10)
    const file = join(homedir(), ".openclaw", "workspace", "memory", `${ymd}.md`)
    appendFileSync(file, `${line}\n`, "utf8")
  } catch {
    // Daily-Log nicht kritisch — Senden bleibt trotzdem geblockt
  }
}

/**
 * message_sending-Handler: blockt nicht-formalisierte Cron-Sends.
 */
export function buildCronFormGateHandler() {
  return async (
    event: { to?: string; content?: string },
    ctx?: { sessionKey?: string },
  ) => {
    try {
      const sessionKey = ctx?.sessionKey
      if (!isUnattendedSession(sessionKey)) return // interaktiv → nichts tun
      const raw = event?.content
      if (typeof raw !== "string" || raw.trim().length < 24) return // NO_REPLY etc.
      if (isCompliantForm(raw)) return // Formular ok → durchlassen
      log.warn(
        `cron-form-gate: BLOCKED (kein gültiges Formular, ${raw.length} Zeichen, session=${sessionKey})`
      )
      appendDailyNote(
        `⛔ cron-form-gate: ungeformte Cron-Nachricht blockiert (${raw.length} Zeichen) — bitte Cron-Prompt prüfen`
      )
      return { cancel: true, cancelReason: "cron-form-gate: kein gültiges Formular" }
    } catch {
      return // Fail-open bei Gate-eigenem Fehler: nie Senden kaputt machen
    }
  }
}