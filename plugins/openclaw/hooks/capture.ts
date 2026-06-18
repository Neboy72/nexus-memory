import { randomUUID } from "node:crypto"
import { Embedder } from "../lib/embedder.ts"
import type { QdrantClient } from "../lib/qdrant-client.ts"
import type { NexusConfig } from "../lib/config.ts"
import { log } from "../logger.ts"
import { isInteractiveTrigger } from "./trigger.ts"

const SKIPPED_PROVIDERS = ["exec-event", "cron-event", "heartbeat"]

/** Remove injected context tags from captured text. */
function cleanContextTags(text: string): string {
  return text
    .replace(/<nexus-context>[\s\S]*?<\/nexus-context>\s*/g, "")
    .trim()
}

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
) {
  return async (
    event: Record<string, unknown>,
    ctx: Record<string, unknown>,
  ) => {
    const trigger = ctx.trigger as string | undefined
    if (!isInteractiveTrigger(trigger)) {
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
        const cleaned = cleanContextTags(joined)
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

    try {
      // Embed the captured content
      const vector = await embedder.embed(content)

      // Generate a UUID for this memory
      const id = randomUUID()

      const payload = {
        text: content,
        access_level: cfg.accessLevel,
        category: "session",
        source: "openclaw",
        source_url: "",
        confidence: 0.7,
        created_at: new Date().toISOString(),
      }

      await qdrantClient.upsert(id, vector, payload)

      log.debug(`capture stored (id=${id})`)
    } catch (err) {
      log.error("capture failed", err)
    }
  }
}