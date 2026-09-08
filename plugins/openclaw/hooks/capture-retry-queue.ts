/**
 * Capture-Retry-Queue (Astra-R6 P1, 08.09.2026)
 *
 * Wenn Qdrant/Embedder kurz weg ist, darf KEINE Erinnerung verloren gehen:
 * fehlgeschlagene Captures landen in einer Datei-Queue (~/.openclaw/workspace/data/
 * capture-retry-queue.jsonl) und werden beim nächsten Capture-Versuch mitgeschickt.
 * Fire-and-forget-Flush: kein Timer, keine Extra-Cron — der nächste Capture-Versuch
 * zieht die Warteschlange nach (Drain).
 */
import { appendFileSync, existsSync, readFileSync, writeFileSync, renameSync } from "node:fs"
import { homedir } from "node:os"
import { join } from "node:path"
import { log } from "../logger.ts"

const QUEUE_FILE = join(
  homedir(), ".openclaw", "workspace", "data", "capture-retry-queue.jsonl"
)
const MAX_QUEUE = 200 // Ring-Größe: älteste Einträge fallen bei Überlauf raus

export interface QueuedCapture {
  id: string
  text: string
  payload: Record<string, unknown>
}

export function enqueueCapture(entry: QueuedCapture): void {
  try {
    appendFileSync(QUEUE_FILE, JSON.stringify(entry) + "\n", "utf8")
    // Ring-Größe erzwingen: bei Überlauf älteste Hälfte verwerfen (Memory-Hygiene,
    // Verlust begrenzt und protokolliert)
    const current = readQueue()
    if (current.length > MAX_QUEUE) {
      const keep = current.slice(Math.floor(current.length / 2))
      writeQueue(keep)
      log.warn(`capture-retry: Queue auf ${keep.length} verkleinert (Überlauf)`)
    }
    log.warn(`capture-retry: queued (${current.length + 1} in Queue)`)
  } catch (err) {
    log.error("capture-retry: enqueue fehlgeschlagen", err)
  }
}

export function readQueue(): QueuedCapture[] {
  try {
    if (!existsSync(QUEUE_FILE)) return []
    const lines = readFileSync(QUEUE_FILE, "utf8").split("\n").filter(Boolean)
    return lines.map((l) => JSON.parse(l) as QueuedCapture)
  } catch {
    return []
  }
}

export function writeQueue(entries: QueuedCapture[]): void {
  const tmp = QUEUE_FILE + ".tmp"
  writeFileSync(tmp, entries.map((e) => JSON.stringify(e)).join("\n") + (entries.length ? "\n" : ""), "utf8")
  renameSync(tmp, QUEUE_FILE)
}

export function queueSize(): number {
  return readQueue().length
}

/**
 * Versucht, alle gequeueten Captures nachzuholen. Aufgerufen nach JEDEM
 * erfolgreichen frischen Capture. Gibt die Anzahl wiederhergestellter
 * Captures zurück. Gelingt ein Eintrag nicht, bleibt er in der Queue
 * (reihenweise, kein Block des frischen Captures).
 */
export async function drainQueue(
  upsert: (id: string, vector: number[], payload: Record<string, unknown>) => Promise<void>,
  embed: (text: string) => Promise<number[]>,
): Promise<number> {
  const entries = readQueue()
  if (entries.length === 0) return 0
  const restoredIds = new Set<string>()
  for (const e of entries) {
    try {
      const vector = await embed(e.text)
      await upsert(e.id, vector, e.payload)
      restoredIds.add(e.id)
      log.info(`capture-retry: restored (id=${e.id})`)
    } catch {
      // Storage noch immer down → Eintrag bleibt in der Queue
      continue
    }
  }
  const kept = entries.filter((e) => !restoredIds.has(e.id))
  writeQueue(kept)
  return entries.length - kept.length
}