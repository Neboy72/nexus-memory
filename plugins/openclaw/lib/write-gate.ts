import { fetchWithTimeout } from "./embedder.ts"
import { recordAgentAccess } from "./qdrant-client.ts"
import type { NexusConfig } from "./config.ts"
import { log } from "../logger.ts"

/**
 * What the service reports back after a write. Only used for the success
 * echo (the gate may have tightened the level, e.g. a `paperless` source
 * forces `private`); callers that do not care may ignore it.
 */
export type WriteResult = {
  status?: string
  id?: string
  access_level?: string
  category?: string
  scope?: string
}

/**
 * The one shape the write paths need. It is the same `upsert` contract the
 * tool/hook modules already used against Qdrant, so swapping the transport
 * did not change their call sites — only what happens behind it.
 */
export interface MemoryWriter {
  upsert(
    id: string,
    vector: number[],
    payload: Record<string, unknown>,
  ): Promise<WriteResult | undefined>
}

/** Where the text came from, for the write gate's origin rules. */
export type WriteSourceType = "chat" | "session" | "manual"

/** One gate request embeds and stores server-side; give it room. */
const WRITE_GATE_TIMEOUT_MS = 30_000

/**
 * Sends a write intent to the Nexus service (`POST /write`) instead of
 * writing Qdrant directly.
 *
 * Why (10.11.2026): measured in the store — 632 points without content,
 * 5146 without provenance, 1890 labels without a sentence. The TypeScript
 * plugins were the one path that skipped every check because they spoke HTTP
 * to Qdrant. The service now owns that gate (`nexus/write_gate.py`); this
 * client only carries the intent and the per-installation secret.
 *
 * Fail-loud: without `writeGate.url` + `writeGate.key` in the plugin config
 * the write throws. It NEVER falls back to an unchecked Qdrant write — that
 * would silently restore the hole this closes.
 */
export class WriteGateClient implements MemoryWriter {
  private readonly cfg: NexusConfig
  private readonly sourceType: WriteSourceType

  constructor(cfg: NexusConfig, sourceType: WriteSourceType) {
    this.cfg = cfg
    this.sourceType = sourceType
  }

  private endpoint(): { url: string; key: string } {
    const gate = this.cfg.writeGate
    if (!gate || !gate.url || !gate.key) {
      throw new Error(
        "nexus: write gate is not configured — set writeGate.url and " +
          "writeGate.key in the OpenClaw plugin config. Refusing to write " +
          "unchecked to Qdrant.",
      )
    }
    return gate
  }

  async upsert(
    id: string,
    _vector: number[],
    payload: Record<string, unknown>,
  ): Promise<WriteResult> {
    const { url, key } = this.endpoint()

    // The service embeds and stores the text itself; the vector computed by
    // the plugin is only needed for local scope inference.
    const content = payload.content ?? payload.text
    if (typeof content !== "string") {
      throw new Error(
        "nexus: write rejected before the gate — payload carries no text content",
      )
    }

    const body = {
      id,
      content,
      category: typeof payload.category === "string" ? payload.category : "fact",
      access_level:
        typeof payload.access_level === "string" ? payload.access_level : "",
      source: typeof payload.source === "string" ? payload.source : "",
      source_url:
        typeof payload.source_url === "string" ? payload.source_url : "",
      confidence:
        typeof payload.confidence === "number" ? payload.confidence : undefined,
      scope: typeof payload.scope === "string" ? payload.scope : "default",
      source_type: this.sourceType,
    }

    log.debugRequest("write-gate", {
      url: `${url}/write`,
      category: body.category,
      sourceType: this.sourceType,
      textLen: content.length,
    })

    const resp = await fetchWithTimeout(
      `${url}/write`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          // Same per-installation header the service requires on /embed and /mcp.
          "x-nexus-service-key": key,
        },
        body: JSON.stringify(body),
      },
      WRITE_GATE_TIMEOUT_MS,
    )

    if (!resp.ok) {
      // The gate's reason is the useful part ("leerer Inhalt", "Regel aus
      // einer Dokument- oder Webquelle"); never swallow it.
      let reason = ""
      try {
        const parsed = (await resp.json()) as {
          reason?: unknown
          error?: unknown
        }
        reason =
          typeof parsed.reason === "string"
            ? parsed.reason
            : typeof parsed.error === "string"
              ? parsed.error
              : ""
      } catch {
        try {
          reason = (await resp.text()).slice(0, 200)
        } catch {
          reason = ""
        }
      }
      throw new Error(
        `nexus: write gate rejected (HTTP ${resp.status})` +
          (reason ? `: ${reason}` : ""),
      )
    }

    // Direct Qdrant writes used to count themselves in the agent registry.
    // The transport changed, the telemetry must not: count the write for
    // THIS agent (the service counts the service's own identity).
    recordAgentAccess(false, true)

    // The success body carries the EFFECTIVE values. A non-JSON 200 must not
    // turn a stored memory into a reported failure, so parsing is best-effort.
    let result: WriteResult = {}
    try {
      const parsed = (await resp.json()) as unknown
      if (parsed && typeof parsed === "object") result = parsed as WriteResult
    } catch {
      result = {}
    }

    log.debugResponse("write-gate", { status: resp.status })
    return result
  }
}
