/**
 * Capture-Retry-Queue (Astra-R6 P1, 08.09.2026)
 *
 * Wenn Qdrant/Embedder kurz weg ist, darf KEINE Erinnerung verloren gehen:
 * fehlgeschlagene Captures landen in einer Datei-Queue (~/.openclaw/workspace/data/
 * capture-retry-queue.jsonl) und werden beim nächsten Capture-Versuch mitgeschickt.
 * Fire-and-forget-Flush: kein Timer, keine Extra-Cron — der nächste Capture-Versuch
 * zieht die Warteschlange nach (Drain).
 */
import {
  appendFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  statSync,
  unlinkSync,
  writeFileSync,
} from "node:fs"
import { homedir } from "node:os"
import { dirname, join } from "node:path"
import { log } from "../logger.ts"

const QUEUE_FILE = join(
  homedir(), ".openclaw", "workspace", "data", "capture-retry-queue.jsonl"
)

/**
 * W27: test/CI override — resolved at call time so tests can point
 * the queue at a temp dir via env BEFORE any enqueue/read call.
 */
export function queueFile(): string {
  return process.env.NEXUS_CAPTURE_QUEUE_FILE || QUEUE_FILE
}

const MAX_QUEUE = 200 // Ring-Größe: älteste Einträge fallen bei Überlauf raus

export interface QueuedCapture {
  id: string
  text: string
  payload: Record<string, unknown>
}

/**
 * Single-writer-Prinzip: enqueueCapture ist APPEND-ONLY (appendFileSync).
 * Rewrites der Queue (Drain + Überlauf-Trim) macht ausschließlich drainQueue
 * bzw. trimQueue — so können zwei Hook-Prozesse sich nicht mehr gegenseitig
 * durch read-modify-write clobberen (verlorene Einträge).
 */
export function enqueueCapture(entry: QueuedCapture): void {
  try {
    // Queue-Dir bei Neuanlage mit restriktiven Rechten (0o700).
    mkdirSync(dirname(queueFile()), { recursive: true, mode: 0o700 })
    // mode wirkt nur bei Neuanlage — bestehende Dateien bleiben unangetastet.
    appendFileSync(queueFile(), JSON.stringify(entry) + "\n", {
      encoding: "utf8",
      mode: 0o600,
    })
    // Kein read-modify-write hier: Trim läuft single-writer in drainQueue.
    log.warn(`capture-retry: queued (id=${entry.id})`)
  } catch (err) {
    log.error("capture-retry: enqueue fehlgeschlagen", err)
  }
}

export function readQueue(): QueuedCapture[] {
  try {
    if (!existsSync(queueFile())) return []
    const lines = readFileSync(queueFile(), "utf8").split("\n").filter(Boolean)
    // Zeilenweise parsen: EINE korrupte Zeile (abgeschnittener Crash-Write) darf
    // nicht die ganze Queue als "leer" erscheinen lassen → Datenverlust.
    // Die korrupte Zeile wird beim nächsten writeQueue automatisch weggeschrieben
    // (sie war nie in entries) — Verlust bleibt auf maximal 1 Zeile begrenzt.
    const out: QueuedCapture[] = []
    for (let i = 0; i < lines.length; i++) {
      try {
        out.push(JSON.parse(lines[i]) as QueuedCapture)
      } catch {
        log.warn(`capture-retry: korrupte Queue-Zeile ${i + 1} übersprungen`)
      }
    }
    return out
  } catch {
    // NUR wenn die DATEI selbst unlesbar ist (kein Zugriff) → leer.
    return []
  }
}

/**
 * Überlauf-Trimmung (Ring-Größe). SINGLE-WRITER: wird nur von drainQueue
 * aufgerufen — niemals aus enqueueCapture (sonst Rewrite-Race zwischen Prozessen).
 */
export function trimQueue(): void {
  try {
    const current = readQueue()
    if (current.length > MAX_QUEUE) {
      const keep = current.slice(Math.floor(current.length / 2))
      writeQueue(keep)
      log.warn(`capture-retry: Queue auf ${keep.length} verkleinert (Überlauf)`)
    }
  } catch (err) {
    log.error("capture-retry: trim fehlgeschlagen", err)
  }
}

export function writeQueue(entries: QueuedCapture[]): void {
  // Guard: the queue dir may not exist yet (fresh install) — create it with
  // restrictive perms before the temp file is written.
  mkdirSync(dirname(queueFile()), { recursive: true, mode: 0o700 })
  const tmp = queueFile() + ".tmp"
  try {
    writeFileSync(
      tmp,
      entries.map((e) => JSON.stringify(e)).join("\n") + (entries.length ? "\n" : ""),
      { encoding: "utf8", mode: 0o600 },
    )
    renameSync(tmp, queueFile())
  } catch (err) {
    log.warn("capture-retry: writeQueue fehlgeschlagen", err)
    throw err
  } finally {
    // Rename removes the temp file on success; a crash/failure can leave it.
    if (existsSync(tmp)) {
      try {
        unlinkSync(tmp)
      } catch {
        // best effort — never mask the original error
      }
    }
  }
}

export function queueSize(): number {
  return readQueue().length
}

/**
 * Does one queued entry belong to the memory that was just deleted?
 *
 * ID first: `enqueueCapture` always writes the memory id, so an id match is
 * proof. The TEXT fallback exists only for entries that carry no id of their
 * own (older/hand-written lines) — an entry with a DIFFERENT id belongs to
 * another memory even when the text repeats, so it is left alone.
 */
function matchesDeleted(entry: unknown, id: string, text: string): boolean {
  if (!entry || typeof entry !== "object") return false
  const e = entry as Record<string, unknown>
  const entryId = typeof e.id === "string" ? e.id.trim() : ""
  if (entryId !== "") return entryId === id
  if (text === "") return false
  const payload =
    e.payload && typeof e.payload === "object"
      ? (e.payload as Record<string, unknown>)
      : {}
  return e.text === text || payload.text === text
}

/**
 * Remove the queued captures of a DELETED memory (ClawHub audit 1.22.0).
 *
 * `nexus_forget` only deleted in Qdrant, so a text that failed to store
 * earlier kept sitting in capture-retry-queue.jsonl and came BACK on the next
 * drain — a forgotten memory reappearing. Called after a successful delete.
 *
 * Returns the number of removed entries. NEVER throws: an unreadable queue is
 * a warning, and the Qdrant delete still counts as successful.
 *
 * Single-writer discipline: `enqueueCapture` is append-only, so the rewrite
 * happens only when an entry actually matches, and the file is re-read right
 * before the rename so entries appended meanwhile survive. An unparseable
 * line never matches (it cannot be proven to belong to the memory) and is
 * carried over verbatim.
 */
export function removeQueuedCaptures(memoryId: string, text?: string): number {
  const id = typeof memoryId === "string" ? memoryId.trim() : ""
  const body = typeof text === "string" ? text : ""
  if (id === "" && body === "") return 0

  const file = queueFile()

  // Read RAW here — readQueue() cannot be used: it reports an unreadable file
  // as "empty", and rewriting that would wipe every pending capture.
  let raw: string
  try {
    if (!existsSync(file)) return 0
    raw = readFileSync(file, "utf8")
  } catch (err) {
    log.warn(
      "capture-retry: Queue nicht lesbar — Eintrag bleibt stehen (Loeschen im Store bleibt erfolgreich)",
      err,
    )
    return 0
  }

  const partition = (content: string): { kept: string[]; removed: number } => {
    const kept: string[] = []
    let removed = 0
    for (const line of content.split("\n")) {
      if (line === "") continue
      let entry: unknown
      try {
        entry = JSON.parse(line)
      } catch {
        kept.push(line) // corrupt/partial line: keep it, we cannot judge it
        continue
      }
      if (matchesDeleted(entry, id, body)) {
        removed++
        continue
      }
      kept.push(line)
    }
    return { kept, removed }
  }

  const first = partition(raw)
  // Nothing matched with certainty -> leave the file untouched (no rewrite, no
  // chance to race a concurrent append).
  if (first.removed === 0) return 0

  // Re-read immediately before the write: enqueueCapture may have appended
  // while we were parsing. Unknown means "keep the first snapshot".
  let latest = raw
  try {
    latest = readFileSync(file, "utf8")
  } catch {
    // best effort — the first snapshot is still a valid basis
  }
  const { kept, removed } = latest === raw ? first : partition(latest)
  // A concurrent drain may already have restored/removed everything we were
  // after — then there is nothing left to write.
  if (removed === 0) return 0

  // Process-unique temp name: drainQueue may rewrite the same file at the same
  // time, and a SHARED `.tmp` name would let one process rename the other's
  // half-written file into place.
  const tmp = `${file}.${process.pid}.${Date.now()}.tmp`
  try {
    mkdirSync(dirname(file), { recursive: true, mode: 0o700 })
    // Preserve the file's permissions: a tmp file created with a default mode
    // would silently loosen (or tighten) what an operator set up.
    let mode = 0o600
    try {
      mode = statSync(file).mode & 0o777
    } catch {
      // file vanished between read and stat — fall back to the queue default
    }
    writeFileSync(tmp, kept.join("\n") + (kept.length ? "\n" : ""), {
      encoding: "utf8",
      mode,
    })
    renameSync(tmp, file)
  } catch (err) {
    log.warn(
      "capture-retry: Queue konnte nach dem Loeschen nicht neu geschrieben werden — Eintrag bleibt stehen",
      err,
    )
    try {
      if (existsSync(tmp)) unlinkSync(tmp)
    } catch {
      // best effort — never mask the original error
    }
    return 0
  }

  log.info(
    `capture-retry: ${removed} Eintrag/Eintraege der geloeschten Erinnerung entfernt (id=${id || "-"})`,
  )
  return removed
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
  // W27: re-read before rewrite — entries appended while the slow
  // embed/upsert loop ran are not in the stale `entries` snapshot;
  // rebuilding `kept` from a fresh read keeps them (restoredIds only
  // contains ids from the snapshot, so appends survive).
  const fresh = readQueue()
  const kept = fresh.filter((e) => !restoredIds.has(e.id))
  const restored = restoredIds.size
  try {
    writeQueue(kept)
  } catch (err) {
    log.warn("capture-retry: drain konnte Queue nicht zurückschreiben", err)
    return restored
  }
  // Single-writer: NUR drainQueue trimmt die Queue (Append-Only-enqueue).
  trimQueue()
  return restored
}