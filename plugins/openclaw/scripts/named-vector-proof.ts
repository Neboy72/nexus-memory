/**
 * Sharp proof for the named-vector fix (09.10.2026).
 *
 * Runs the real built client against two throwaway Qdrant collections:
 *   A) an ANONYMOUS collection  — must keep working exactly as before
 *   B) a NAMED collection       — was broken (400 "Not existing vector name"),
 *                                 must now read and write
 *
 * Nothing here touches the live collection; both are created and dropped.
 */
import { randomUUID } from "node:crypto"

import { QdrantClient } from "../lib/qdrant-client.ts"

const URL = "http://127.0.0.1:6333"
const ANON = "zz_named_proof_anon"
const NAMED = "zz_named_proof_named"
const VN = "ollama__qwen3-embedding_0_6b__1024"
const DIM = 8
const v = Array.from({ length: DIM }, (_, i) => (i + 1) / 10)

async function drop(name: string) {
  await fetch(`${URL}/collections/${name}`, { method: "DELETE" })
}

async function makeAnon(name: string) {
  await drop(name)
  const r = await fetch(`${URL}/collections/${name}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ vectors: { size: DIM, distance: "Cosine" } }),
  })
  if (!r.ok) throw new Error(`anon create ${r.status}: ${await r.text()}`)
}

async function makeNamed(name: string) {
  await drop(name)
  const r = await fetch(`${URL}/collections/${name}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ vectors: { [VN]: { size: DIM, distance: "Cosine" } } }),
  })
  if (!r.ok) throw new Error(`named create ${r.status}: ${await r.text()}`)
}

async function main() {
  let failures = 0
  const check = (label: string, ok: boolean, detail = "") => {
    console.log(`  ${ok ? "ok  " : "ROT "} ${label}${detail ? " — " + detail : ""}`)
    if (!ok) failures++
  }

  // ── A) anonymous collection: behaviour must be unchanged ─────────────
  console.log("A) anonyme Sammlung (so sieht unsere Live-Sammlung aus)")
  await makeAnon(ANON)
  const anon = new QdrantClient(URL, ANON, DIM)
  await anon.ensureCollection(DIM)
  await anon.upsert(randomUUID(), v, { access_level: "private", content: "anon probe" })
  const anonHits = await anon.search(v, 5, "private")
  check("schreiben + suchen", anonHits.length === 1, `${anonHits.length} Treffer`)
  await drop(ANON)

  // ── B) named collection: the previously broken case ─────────────────
  console.log("B) benannte Sammlung (Regel B, was eine frische Installation anlegt)")
  await makeNamed(NAMED)
  const named = new QdrantClient(URL, NAMED, DIM)
  await named.ensureCollection(DIM)

  // Write: used to 400 on a named space.
  let wrote = true
  let writeErr = ""
  try {
    await named.upsert(randomUUID(), v, { access_level: "private", content: "named probe" })
  } catch (e) {
    wrote = false
    writeErr = e instanceof Error ? e.message : String(e)
  }
  check("schreiben in benannte Sammlung", wrote, writeErr)

  // Read: used to 400 as well.
  let hits: unknown[] = []
  let readErr = ""
  try {
    hits = await named.search(v, 5, "private")
  } catch (e) {
    readErr = e instanceof Error ? e.message : String(e)
  }
  check("suchen in benannter Sammlung", hits.length === 1, readErr || `${hits.length} Treffer`)

  // The body sent must carry the name — verified against Qdrant directly.
  let direct = false
  try {
    const r = await fetch(`${URL}/collections/${NAMED}/points/search`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ vector: { name: VN, vector: v }, limit: 5, with_payload: true }),
    })
    const d = await r.json()
    direct = Array.isArray(d?.result) && d.result.length === 1
  } catch { /* direct stays false */ }
  check("direkt mit Vektor-Namen abrufbar", direct)

  await drop(NAMED)
  console.log(failures === 0 ? "\nALLES GRUEN" : `\n${failures} FEHLER`)
  process.exit(failures === 0 ? 0 : 1)
}

main().catch((e) => {
  console.error("Abbruch:", e)
  process.exit(2)
})
