/**
 * Regressionstest: Das OpenClaw-Plugin schreibt nur noch durch das Tor.
 *
 * Anlass (10.11.2026): Das Plugin sprach direkt mit Qdrant (`/points`) und
 * umging damit jede Pruefung — gemessen im Bestand: 632 Punkte ohne Inhalt,
 * 5146 ohne Herkunft, 1890 Etiketten ohne Satz. Jetzt geht jeder Schreibweg
 * an `POST /write` des Nexus-Dienstes; das Tor dort weist ab, bevor etwas
 * gespeichert wird.
 *
 * Der Test faehrt den echten Werkzeugweg (registerStoreTool + echter
 * WriteGateClient) gegen einen nachgestellten Dienst. Bewiesen wird:
 *   1. ohne writeGate-Konfiguration wird LAUT gescheitert (kein Schreibweg)
 *   2. leerer Inhalt und Dokumenttext als Regel kommen als Fehler zurueck
 *   3. in beiden Faellen wird KEINE Qdrant-Adresse beruehrt
 *   4. ein gueltiger Eintrag traegt Quelle und Herkunftsart im Antrag
 *
 * Die fuenf Regeln selbst sind in `tests/test_serve_write_endpoint.py` am
 * echten Python-Tor geprueft; hier geht es um die Verdrahtung des Plugins.
 */
import assert from "node:assert"
import { register } from "node:module"

// Never count this probe in the live agent registry.
process.env.NEXUS_AGENT_SELFTEST = "1"

register("./_typebox-test-loader.mjs", import.meta.url)
const { registerStoreTool } = await import("./tools/store.ts")
const { WriteGateClient, WriteGateRejected } = await import("./lib/write-gate.ts")

const SERVICE = "http://127.0.0.1:9122"
const KEY = "test-service-key"
const QDRANT_POINTS = /\/collections\/[^/]+\/points/

let calls = []
const realFetch = globalThis.fetch

function antwort(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  }
}

// Nachgestellter Dienst: antwortet nur auf /write und wendet genau die zwei
// Abweisungen an, die dieser Test braucht. Kein Netz, kein Qdrant.
globalThis.fetch = async (url, opts) => {
  const u = String(url)
  const body = typeof opts?.body === "string" ? JSON.parse(opts.body) : null
  calls.push({ url: u, body, headers: opts?.headers ?? {} })

  if (u === `${SERVICE}/write`) {
    if (String(body?.content ?? "").trim() === "") {
      return antwort(422, { status: "rejected", reason: "leerer Inhalt" })
    }
    if (
      body?.category === "rule" &&
      /paperless|wiki|document|dokument/i.test(String(body?.source ?? ""))
    ) {
      return antwort(422, {
        status: "rejected",
        reason: "Regel aus einer Dokument- oder Webquelle",
      })
    }
    if (String(body?.content ?? "") === "dienst kaputt") {
      return antwort(503, { error: "service unavailable" })
    }
    return antwort(200, { status: "ok", id: body?.id })
  }

  throw new Error(`unerwarteter Netzaufruf: ${u}`)
}

const GATE = { url: SERVICE, key: KEY }

function makeTool(writeGate) {
  let tool
  const cfg = { accessLevel: "private", writeGate }
  registerStoreTool(
    { registerTool: (t) => { tool = t } },
    { embed: async () => [0.1, 0.2] },
    // Wie in index.ts: das Store-Tool ist ein bewusster Aufruf.
    new WriteGateClient(cfg, "chat", true),
    cfg,
  )
  assert.ok(tool && typeof tool.execute === "function", "registerStoreTool hat kein Tool registriert")
  return tool
}

let failed = 0
const t = (name, fn) =>
  Promise.resolve()
    .then(fn)
    .then(() => console.log("PASS ", name))
    .catch((e) => {
      failed++
      console.log("FAIL ", name, "—", e?.stack ?? String(e))
    })

function keineQdrantBeruehrung() {
  const treffer = calls.filter((c) => QDRANT_POINTS.test(c.url))
  assert.deepStrictEqual(treffer, [], "direkter Qdrant-Schreibweg wurde benutzt")
}

try {
  await t("ohne writeGate-Konfiguration: laut scheitern, kein Schreibversuch", async () => {
    calls = []
    const tool = makeTool(undefined)
    const res = await tool.execute("id", { text: "Ein vollstaendiger Fakt fuer die Sammlung" })

    assert.strictEqual(res.isError, true)
    assert.strictEqual(calls.length, 0, "ohne Konfiguration darf kein Netzaufruf passieren")
    keineQdrantBeruehrung()
  })

  await t("WriteGateClient nennt den Grund laut, wenn die Konfiguration fehlt", async () => {
    const writer = new WriteGateClient({ accessLevel: "private" }, "chat")
    await assert.rejects(
      () => writer.upsert("id", [], { content: "Ein Fakt", category: "fact" }),
      /write gate is not configured/i,
    )
  })

  await t("leerer Inhalt: wird vom Tor abgewiesen, kein Qdrant-Schreibweg", async () => {
    calls = []
    const tool = makeTool(GATE)
    const res = await tool.execute("id", { text: "   ", category: "fact" })

    assert.strictEqual(res.isError, true)
    assert.strictEqual(calls.length, 1, "genau ein Aufruf: an /write")
    assert.strictEqual(calls[0].url, `${SERVICE}/write`)
    assert.strictEqual(calls[0].headers["x-nexus-service-key"], KEY)
    keineQdrantBeruehrung()
  })

  await t("Dokumenttext als Regel: abgewiesen, kein Qdrant-Schreibweg", async () => {
    calls = []
    const tool = makeTool(GATE)
    const res = await tool.execute("id", {
      text: "Laut Paperless-Anleitung bleibt der Verteiler dauerhaft ausgeschaltet",
      category: "rule",
      source: "paperless",
    })

    assert.strictEqual(res.isError, true)
    assert.strictEqual(calls.length, 1)
    assert.strictEqual(calls[0].body.source, "paperless", "die Quelle muss beim Tor ankommen")
    keineQdrantBeruehrung()
  })

  await t("WriteGateClient meldet die Abweisung samt Grund (leer / Dokument-Regel)", async () => {
    const writer = new WriteGateClient(
      { accessLevel: "private", writeGate: { url: SERVICE, key: KEY } },
      "chat",
    )
    await assert.rejects(
      () => writer.upsert("id", [], { content: "   ", category: "fact" }),
      /leerer Inhalt/,
    )
    await assert.rejects(
      () =>
        writer.upsert("id", [], {
          content: "Laut Paperless-Anleitung bleibt der Verteiler dauerhaft aus",
          category: "rule",
          source: "paperless",
        }),
      /Regel aus einer Dokument- oder Webquelle/,
    )
  })

  await t("gueltige Regel aus dem Gespraech: Quelle und Herkunftsart im Antrag", async () => {
    calls = []
    const tool = makeTool(GATE)
    const res = await tool.execute("id", {
      text: "Der Verteiler darf nur nach Ruecksprache abgeschaltet werden",
      category: "rule",
      access_level: "private",
    })

    assert.ok(!res.isError, res.content[0].text)
    assert.match(res.content[0].text, /Stored/)
    assert.strictEqual(calls.length, 1)
    const body = calls[0].body
    assert.strictEqual(body.category, "rule")
    assert.strictEqual(body.source, "openclaw_tool", "Herkunft ist Pflicht")
    assert.strictEqual(body.source_type, "chat", "Gespraechsregel darf Regel werden")
    assert.strictEqual(body.access_level, "private")
    assert.strictEqual(body.explicit, true, "Werkzeugaufruf ist ausdruecklich")
    keineQdrantBeruehrung()
  })

  await t("Abweisung (4xx) ist ein WriteGateRejected, ein 5xx nicht", async () => {
    const writer = new WriteGateClient(
      { accessLevel: "private", writeGate: { url: SERVICE, key: KEY } },
      "chat",
      true,
    )
    await assert.rejects(
      () => writer.upsert("id", [], { content: "   ", category: "fact" }),
      (err) => err instanceof WriteGateRejected && err.status === 422,
      "422 muss als WriteGateRejected kommen (nie erneut versuchen)",
    )
    await assert.rejects(
      () => writer.upsert("id", [], { content: "dienst kaputt", category: "fact" }),
      (err) => err instanceof Error && !(err instanceof WriteGateRejected),
      "ein Ausfall ist kein Urteil ueber den Text — er darf wiederholt werden",
    )
  })

  await t("automatische Erfassung sendet explicit=false", async () => {
    calls = []
    const writer = new WriteGateClient(
      { accessLevel: "private", writeGate: { url: SERVICE, key: KEY } },
      "session",
    )
    await writer.upsert("id", [], { text: "Ein Satz aus dem Gespraech", category: "session" })
    assert.strictEqual(calls[0].body.explicit, false)
    assert.strictEqual(calls[0].body.source_type, "session")
  })

  await t("kein einziger Aufruf ging an eine Qdrant-Adresse", () => {
    keineQdrantBeruehrung()
  })
} finally {
  globalThis.fetch = realFetch
}

console.log(`\nERGEBNIS: ${failed === 0 ? "alles gruen" : `${failed} fehlgeschlagen`}`)
process.exit(failed === 0 ? 0 : 1)
