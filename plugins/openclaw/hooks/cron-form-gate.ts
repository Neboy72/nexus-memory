/**
 * Cron-Form-Gate (Astra-R6 P0, 08.09.2026)
 *
 * Unbeaufsichtigte Nachrichten (Cron-/Heartbeat-Sessions) dürfen nur als
 * festes Formular gesendet werden. Fail-closed für Formular-Verletzungen:
 * kaputtes Formular → Senden wird abgebrochen (cancel) + 1 Zeile ins Daily Log.
 * Fail-open NUR bei Gate-eigenem Crash (nie den Sendepfad kaputtmachen).
 * Interaktive Sessions (DM/Gruppe mit User) bleiben unangetastet.
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

// Erlaubte Kurz-Nachrichten (Cron-/Heartbeat-Steuersignale). Alles andere —
// auch Kurz-Texte < 24 Zeichen — muss das Formular erfüllen, sonst Gate-Umgehung.
const SHORT_TOKENS = new Set(["NO_REPLY", "[SILENT]", "-", "ok", "OK"])

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
  // EXACT match (after trim): a startsWith check let titles like
  // "Weekly Skill CheckXYZ" through as a "fixed form".
  const firstLine = trimmed.split("\n")[0].trim()
  if (!ALLOWED_TITLES.includes(firstLine)) return false
  // Kein Reasoning-Leak irgendwo im Text (reuse der bewährten Marker)
  for (const line of lines) {
    if (isPureReasoningBlock(line, false)) return false
  }
  return true
}

function appendDailyNote(line: string): void {
  try {
    // Local date parts: toISOString() is UTC, so between local midnight and
    // the UTC offset the note landed in the wrong day's file.
    const now = new Date()
    const pad = (n: number) => String(n).padStart(2, "0")
    const localDate = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`
    const file = join(homedir(), ".openclaw", "workspace", "memory", `${localDate}.md`)
    appendFileSync(file, `${line}\n`, "utf8")
  } catch (err) {
    // Daily-Log nicht kritisch — Senden bleibt trotzdem geblockt
    log.warn("cron-form-gate: memory-file note failed", err)
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
      // Gleiche Payload-Kette wie thought-filter: der Sender kann den Text in
      // content ODER message ODER text legen — sonst wäre das Gate umgehbar.
      const raw = event?.content ?? (event as any)?.message ?? (event as any)?.text
      if (typeof raw !== "string") return
      const trimmed = raw.trim()
      if (trimmed.length === 0) return // leer/whitespace
      if (SHORT_TOKENS.has(trimmed)) return // explizite Steuersignale (NO_REPLY etc.)
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