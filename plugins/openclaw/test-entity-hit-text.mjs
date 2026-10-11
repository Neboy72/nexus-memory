/**
 * Regressionstest: entity-Treffer mit Payload-Feld content statt text
 *
 * Wissens-Knoten in der Sammlung nexus_hf tragen ihren Text im Feld
 * payload.content, nicht in payload.text. Die mapHit-Mapping in
 * lib/qdrant-client.ts muss deshalb von text auf content zurueckfallen,
 * sonst landen leere Texte in Erinnerungs-Einblendung und Tool-Hinweisen.
 */
import assert from "node:assert"
import { QdrantClient } from "./lib/qdrant-client.ts"

const realFetch = globalThis.fetch

let failed = 0
const t = async (name, fn) => {
  let timer
  let timedOut = false
  try {
    await Promise.race([
      Promise.resolve().then(fn),
      new Promise((_, rej) => {
        timer = setTimeout(() => {
          timedOut = true
          rej(new Error("timeout: test hing 10s"))
        }, 10000)
      }),
    ])
    console.log("PASS ", name)
  } catch (e) {
    failed++
    console.log("FAIL ", name, "—", (e && e.stack) || String(e))
  } finally {
    clearTimeout(timer)
    globalThis.fetch = realFetch
  }
  if (timedOut) throw new Error("timeout abort")
}

/** fetch-Mock fuer eine named vector space (nexus_hf) mit beliebigen Treffern. */
function mockFetch(hits) {
  globalThis.fetch = async (url, o = {}) => {
    const method = o.method ?? "GET"
    const u = String(url)
    if (method === "GET" && u.endsWith("/collections/nexus_hf")) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          result: {
            config: {
              params: {
                vectors: {
                  "sentence-transformers__qwen_qwen3-embedding-0_6b__1024": {
                    size: 1024,
                    distance: "Cosine",
                  },
                },
              },
            },
          },
        }),
        text: async () => "",
      }
    }
    if (method === "POST" && u.endsWith("/collections/nexus_hf/points/search")) {
      return {
        ok: true,
        status: 200,
        json: async () => ({ result: hits }),
        text: async () => "",
      }
    }
    return { ok: true, status: 200, json: async () => ({}), text: async () => "" }
  }
}

await t("entity-Treffer mit nur content liefert nicht-leeren text", async () => {
  mockFetch([
    {
      id: "entity-1",
      score: 0.91,
      payload: {
        category: "entity",
        access_level: "public",
        source: "ingestion",
        created_at: "2026-10-11T12:00:00Z",
        content: "Hermes Agent auf Apple Silicon",
      },
    },
  ])
  const client = new QdrantClient("http://localhost:6333", "nexus_hf", 1024)
  const results = await client.searchByVector(new Array(1024).fill(0.001), 5, "public")
  assert.strictEqual(results.length, 1)
  assert.strictEqual(results[0].text, "Hermes Agent auf Apple Silicon")
})

await t("Treffer mit text UND content bevorzugt text", async () => {
  mockFetch([
    {
      id: "entity-2",
      score: 0.88,
      payload: {
        category: "entity",
        access_level: "public",
        source: "ingestion",
        created_at: "2026-10-11T12:00:00Z",
        text: "das text-Feld",
        content: "das content-Feld",
      },
    },
  ])
  const client = new QdrantClient("http://localhost:6333", "nexus_hf", 1024)
  const results = await client.searchByVector(new Array(1024).fill(0.001), 5, "public")
  assert.strictEqual(results[0].text, "das text-Feld")
})

await t("Treffer ohne text und content bleibt leer und wirft nicht", async () => {
  mockFetch([
    {
      id: "entity-3",
      score: 0.85,
      payload: {
        category: "entity",
        access_level: "public",
        source: "ingestion",
        created_at: "2026-10-11T12:00:00Z",
      },
    },
  ])
  const client = new QdrantClient("http://localhost:6333", "nexus_hf", 1024)
  const results = await client.searchByVector(new Array(1024).fill(0.001), 5, "public")
  assert.strictEqual(results.length, 1)
  assert.strictEqual(results[0].text, "")
})

globalThis.fetch = realFetch
process.exitCode = failed ? 1 : 0
