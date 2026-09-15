import { randomUUID } from "node:crypto"
import { Embedder } from "../lib/embedder.ts"
import type { QdrantClient } from "../lib/qdrant-client.ts"
import type { NexusConfig } from "../lib/config.ts"
import { ScopeCentroidCache, inferScope } from "../lib/scope-auto.ts"
import { neutralizeContextClose, stripNexusContextBlock } from "../lib/prompt-safety.ts"
import { log } from "../logger.ts"
import { isInteractiveTrigger } from "./trigger.ts"
import { enqueueCapture, drainQueue } from "./capture-retry-queue.ts"

/**
 * Resolve the scope for an auto-captured memory (self-organizing memory):
 * explicit cfg.scope wins; otherwise infer from the vector against scoped
 * centroids on a CLEAR match; else 'default'. Fail-open everywhere.
 */
async function inferCaptureScope(
  embedder: Embedder,
  vector: number[],
  centroidCache: ScopeCentroidCache | undefined,
  manualScope: string,
): Promise<string> {
  if (manualScope) return manualScope
  if (!centroidCache) return "default"
  try {
    const cents = await centroidCache.get()
    return inferScope(vector, cents)
  } catch (err) {
    log.debug("scope_auto: capture inference failed — fail-open to default", err)
    return "default"
  }
}

const SKIPPED_PROVIDERS = ["exec-event", "cron-event", "heartbeat"]

function getLastTurn(messages: unknown[]): unknown[] {
  let lastUserIdx = -1
  for (let i = messages.length - 1; i >= 0; i--) {
    const msg = messages[i]
    if (
      msg &&
      typeof msg === "object" &&
      (msg as Record<string, unknown>).role === "user"
    ) {
      lastUserIdx = i
      break
    }
  }
  return lastUserIdx >= 0 ? messages.slice(lastUserIdx) : messages
}

export function buildCaptureHandler(
  embedder: Embedder,
  qdrantClient: QdrantClient,
  cfg: NexusConfig,
  centroidCache?: ScopeCentroidCache,
) {
  return async (
    event: Record<string, unknown>,
    ctx: Record<string, unknown>,
  ) => {
    const trigger = ctx.trigger as string | undefined
    if (!isInteractiveTrigger(trigger)) {
      return
    }

    // Group-context privacy gate (Astra-R2 critical finding, 08.09.2026):
    // group/channel turns MUST NOT flow into the private memory store.
    // groupId is set for group chats, null/undefined for DMs.
    // Fail-closed: ANY present groupId — including "" and whitespace-only —
    // skips capture entirely. Only a truly ABSENT group (null/undefined)
    // leaves private capture allowed; a blank groupId is a group turn whose
    // id failed to resolve, never a DM.
    const rawGroupId = ctx.groupId
    if (rawGroupId !== null && rawGroupId !== undefined) {
      if (String(rawGroupId).trim() === "") {
        log.warn(
          "nexus: capture skipped — group context with empty groupId (fail-closed)",
        )
      } else {
        log.info("nexus: capture skipped — group context (privacy gate)")
      }
      return
    }

    log.info(
      `agent_end fired: provider="${ctx.messageProvider}" success=${event.success}`,
    )
    const provider = ctx.messageProvider as string

    if (SKIPPED_PROVIDERS.includes(provider)) {
      return
    }

    if (
      !event.success ||
      !Array.isArray(event.messages) ||
      event.messages.length === 0
    )
      return

    const lastTurn = getLastTurn(event.messages)

    const texts: string[] = []
    for (const msg of lastTurn) {
      if (!msg || typeof msg !== "object") continue
      const msgObj = msg as Record<string, unknown>
      const role = msgObj.role
      if (role !== "user" && role !== "assistant") continue

      const content = msgObj.content

      const parts: string[] = []

      if (typeof content === "string") {
        parts.push(content)
      } else if (Array.isArray(content)) {
        for (const block of content) {
          if (!block || typeof block !== "object") continue
          const b = block as Record<string, unknown>
          if (b.type === "text" && typeof b.text === "string") {
            parts.push(b.text)
          }
        }
      }

      if (parts.length > 0) {
        const joined = parts.join("\n")
        // Strip any injected wrapper, then neutralize stray closing tags so
        // the STORED text can never break a future <nexus-context> block.
        const cleaned = neutralizeContextClose(stripNexusContextBlock(joined))
        if (cleaned.length > 0) {
          texts.push(`[role: ${role}]\n${cleaned}\n[${role}:end]`)
        }
      }
    }

    // Filter out very short captures
    const captured = texts.filter((t) => t.length >= 10)

    if (captured.length === 0) return

    const content = captured.join("\n\n")

    log.debug(`capturing ${captured.length} texts (${content.length} chars)`)

    // ID und Payload VOR dem try: der Retry-Requeue (catch) muss exakt dieselbe
    // ID und denselben (ggf. inferierten) Scope wiederverwenden — sonst kann
    // derselbe Text unter 2 IDs doppelt im Store landen.
    const id = randomUUID()
    let payload: Record<string, unknown> = {
      text: content,
      access_level: cfg.accessLevel,
      category: "session",
      source: "openclaw",
      source_url: "",
      confidence: 0.7,
      created_at: new Date().toISOString(),
      scope: cfg.scope || "default",
    }

    try {
      // Embed the captured content
      const vector = await embedder.embed(content)

      // Scope (self-organizing memory, Nebo law 07.09): infer the area from
      // existing scoped centroids on a CLEAR match; explicit cfg.scope wins;
      // else 'default'. Fail-open, zero config, zero LLM cost.
      payload = {
        ...payload,
        scope: await inferCaptureScope(embedder, vector, centroidCache, cfg.scope),
      }

      await qdrantClient.upsert(id, vector, payload)

      log.debug(`capture stored (id=${id})`)

      // Drain: nachholen, was bei früherem Storage-Ausfall angestanden hat
      try {
        const restored = await drainQueue(
          (rid, rvec, rpayload) => qdrantClient.upsert(rid, rvec, rpayload),
          (rtext) => embedder.embed(rtext),
        )
        if (restored > 0) log.info(`capture-retry: ${restored} nachgeholt`)
      } catch {
        // Drain-Fehler darf frisches Capture nicht torpedieren
      }
    } catch (err) {
      log.error("capture failed", err)
      // Retry-Queue (Astra-R6 P1): nichts geht verloren — dieselbe ID + Payload
      // (mit inferiertem Scope) in die Warteschlange, Drain beim nächsten Versuch.
      try {
        enqueueCapture({ id, text: content, payload })
      } catch {
        // Queue selbst kaputt → alter Zustand (nur Log)
      }
    }
  }
}