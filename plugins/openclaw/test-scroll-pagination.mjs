/**
 * Regressionstest: Scroll-Pagination (H10, Welle 12)
 *
 * scrollFiltered/fetchCentroids ignorierten next_page_offset → auf
 * Sammlungen > limit wurden Regeln/Edges/Centroiden aus einem Teilsample
 * berechnet. Jetzt: beide folgen next_page_offset mit hartem Seiten-Cap (5).
 */
import assert from "node:assert"
import { QdrantClient } from "./lib/qdrant-client.ts"
import { fetchCentroids } from "./lib/scope-auto.ts"

let failed = 0
const t = (name, fn) =>
  Promise.resolve()
    .then(fn)
    .then(() => console.log("PASS ", name))
    .catch((e) => {
      failed++
      console.log("FAIL ", name, "—", e.message)
    })

const realFetch = globalThis.fetch
const MAX_SCROLL_PAGES = 5 // mirrors the constant in qdrant-client.ts

/** Stub, der pro Aufruf die nächste Page liefert; protokolliert Bodies. */
function mockPages(pages) {
  const bodies = []
  globalThis.fetch = async (_url, opts = {}) => {
    const body = JSON.parse(opts.body)
    bodies.push(body)
    const page = pages[Math.min(bodies.length - 1, pages.length - 1)]
    return { ok: true, status: 200, json: async () => ({ result: page }), text: async () => "" }
  }
  return bodies
}

await t("scrollFiltered summiert zwei Seiten (next_page_offset)", async () => {
  const bodies = mockPages([
    { points: [{ id: 1, payload: { text: "a" } }], next_page_offset: "off-1" },
    { points: [{ id: 2, payload: { text: "b" } }], next_page_offset: null },
  ])
  const client = new QdrantClient("http://localhost:6333", "nexus", 8)
  const points = await client.scrollFiltered({ must: [] }, 100)
  assert.deepStrictEqual(points.map((p) => p.id), ["1", "2"])
  assert.strictEqual(bodies.length, 2, "beide Seiten müssen gefetcht werden")
  assert.strictEqual(bodies[0].offset, undefined, "Seite 1 ohne offset")
  assert.strictEqual(bodies[1].offset, "off-1", "Seite 2 nutzt next_page_offset")
})

await t("scrollFiltered: Cap greift bei Endlos-offset (max 5 Seiten)", async () => {
  // Der Stub liefert IMMER einen next_page_offset → die Schleife muss beim
  // harten Cap terminieren, nicht weiterfetchen.
  const bodies = mockPages([{ points: [{ id: "x", payload: {} }], next_page_offset: "more" }])
  const client = new QdrantClient("http://localhost:6333", "nexus", 8)
  const points = await client.scrollFiltered({ must: [] }, 10)
  assert.strictEqual(bodies.length, MAX_SCROLL_PAGES, `erwartet ${MAX_SCROLL_PAGES} Seiten`)
  assert.strictEqual(points.length, MAX_SCROLL_PAGES)
})

await t("fetchCentroids summiert beide Seiten", async () => {
  mockPages([
    {
      points: [{ vector: [1, 0], payload: { scope: "proj-a", lifecycle_status: "canonical" } }],
      next_page_offset: "p2",
    },
    {
      points: [{ vector: [0, 1], payload: { scope: "proj-b", lifecycle_status: "canonical" } }],
      next_page_offset: null,
    },
  ])
  const cents = await fetchCentroids("http://localhost:6333", "nexus")
  assert.deepStrictEqual(Object.keys(cents).sort(), ["proj-a", "proj-b"])
})

await t("fetchCentroids: Cap terminiert ebenfalls", async () => {
  const bodies = mockPages([
    {
      points: [{ vector: [1, 0], payload: { scope: "proj-a" } }],
      next_page_offset: "always",
    },
  ])
  await fetchCentroids("http://localhost:6333", "nexus")
  assert.strictEqual(bodies.length, MAX_SCROLL_PAGES)
})

globalThis.fetch = realFetch
process.exit(failed ? 1 : 0)
