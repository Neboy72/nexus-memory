/**
 * Endpunkt-Beweis: ein echter Treffer aus nexus_hf kommt nicht mehr mit
 * leerem Text an.
 *
 * Viele Punkte der Sammlung (z. B. paperless-Dokumente) tragen ihren Text im
 * Payload-Feld content, nicht in text. Der Plugin-Client mapHit liest jetzt
 * text mit Rueckfall auf content. Dieses Skript belegt am lebenden Bestand:
 *   - ein Suchtreffer ueber den Plugin-Client liefert nicht-leeren text
 *   - die alte Lesestelle (payload.text ohne Rueckfall) waere fuer denselben
 *     Punkt leer.
 *
 * Schreibzugriffe auf die Datenbank: keine.
 */
import { QdrantClient } from "../lib/qdrant-client.ts"
import { Embedder } from "../lib/embedder.ts"

const QDRANT_URL = "http://localhost:6333"
const COLLECTION = "nexus_hf"
const EMBED_BASE = "http://127.0.0.1:9122"

const embedder = new Embedder("nexus", undefined, undefined, EMBED_BASE, 1024)
const client = new QdrantClient(QDRANT_URL, COLLECTION, 1024)

// Suchbegriff, der dokumentenartige Inhalte in nexus_hf trifft.
const query = "Lieferschein energieloesung Regensburg"
const vector = await embedder.embed(query)
const hits = await client.search(vector, 10, "private")

if (hits.length === 0) {
  console.error("Keine Treffer fuer den Beweis-Query erhalten.")
  process.exit(1)
}

// Den ersten content-only-Treffer finden, damit der Vergleich zwingt.
let chosen = null
let rawPayload = null
for (const hit of hits) {
  const resp = await fetch(
    `${QDRANT_URL}/collections/${COLLECTION}/points/${encodeURIComponent(hit.id)}?with_payload=true`,
  )
  if (!resp.ok) continue
  const data = await resp.json()
  const payload = data.result?.payload ?? {}
  if (payload.content && !payload.text) {
    chosen = hit
    rawPayload = payload
    break
  }
}

if (!chosen) {
  console.error("Unter den Top-Treffern kein content-only-Punkt gefunden.")
  process.exit(1)
}

const oldStyleText = (rawPayload.text ?? "") || ""
const newStyleText = chosen.text

console.log("Endpunkt-Beweis nexus_hf (keine Schreibzugriffe)")
console.log("query:", query)
console.log("hit id:", chosen.id)
console.log("score:", chosen.score)
console.log("payload.text (alte Lesestelle):", JSON.stringify(oldStyleText))
console.log("payload.content (Rohwert):", JSON.stringify(rawPayload.content).slice(0, 200))
console.log("Plugin-Client text (text → content Fallback):", JSON.stringify(newStyleText).slice(0, 200))
console.log("")
console.log("Ergebnis:")
console.log(`  alter Mapping haette geliefert: ${oldStyleText === "" ? "LEER" : "nicht leer"}`)
console.log(`  neuer Mapping liefert: ${newStyleText.length > 0 ? `nicht leer (${newStyleText.length} Zeichen)` : "LEER"}`)

if (newStyleText.length === 0) {
  process.exit(1)
}
