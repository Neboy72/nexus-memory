/**
 * Universalitäts-Vertrag für cron-form-gate (27.09., Nebo-Auftrag):
 * Das Repo ist für ALLE Nutzer — keine installationsspezifischen Titel in
 * aktivem Code oder Tests. Deploy-Titel leben NUR in der lokalen Config
 * (~/.openclaw/openclaw.json). Hintergrund: 08.09. fail-closed-Gate mit 4
 * verdrahteten Titeln hat fremde Cron-Sends stumm geblockt (27.09. gefunden).
 */
import assert from "node:assert"
import fs from "node:fs"
import path from "node:path"

let failed = 0
const t = (name, fn) =>
  Promise.resolve()
    .then(fn)
    .then(() => console.log("PASS ", name))
    .catch((e) => {
      failed++
      console.log("FAIL ", name, "—", e?.stack ?? String(e))
    })

const read = (rel) => fs.readFileSync(path.join(import.meta.dirname, rel), "utf8")

// Die 4 echten Deploy-Titel aus dieser Installation (08.09. + 27.09.) und
// weitere real genutzte Literale. Keiner davon darf im Repo-Aktivcode stehen.
const DEPLOY_TITLES = [
  "🚀 OpenClaw Release",
  "Weekly Skill Check",
  "⚠️ Balance",
  "⚠️ Snapshot",
  "⚠️ Problem",
]

const HOOK = read("./hooks/cron-form-gate.ts")
const INDEX = read("./index.ts")
const CONFIG = read("./lib/config.ts")

await t("Gate-Hook: kein Deploy-Titel im Hook-Code", () => {
  // Kommentare zählen mit: auch als Kommentar wäre der Literal ein Repo-Artefakt
  for (const title of DEPLOY_TITLES) {
    assert.ok(!HOOK.includes(title), `Deploy-Titel "${title}" im Hook!`)
  }
})

await t("index.ts + config.ts: keine Deploy-Titel", () => {
  for (const title of DEPLOY_TITLES) {
    assert.ok(!INDEX.includes(title), `Deploy-Titel "${title}" in index.ts!`)
    assert.ok(!CONFIG.includes(title), `Deploy-Titel "${title}" in config.ts!`)
  }
})

await t("Manifest: kein Deploy-Titel im Plugin-Manifest", () => {
  const manifest = fs.readFileSync(
    path.join(import.meta.dirname, "openclaw.plugin.json"),
    "utf8",
  )
  for (const title of DEPLOY_TITLES) {
    assert.ok(!manifest.includes(title), `Deploy-Titel "${title}" im Manifest!`)
  }
})

await t("Default-Konfiguration enthält keine Titel (leere Liste)", () => {
  // parseConfig-Default: titles muss leer sein — Gate ohne Config = AUS
  const src = CONFIG
  assert.ok(/titles\s*:/.test(src), "config.ts deklariert titles nicht")
  assert.ok(
    !/\btitles\s*:\s*\[\s*"/.test(src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "")),
    "config.ts setzt Titel-Literale als Default — Deploy-Daten im Code!",
  )
})

await t("Test-Suite selbst nutzt Deploy-Titel nur hier (whitelist)", () => {
  // Die übrigen test-*.mjs (außer dieser) dürfen die echten Titel nicht
  // als funktionale Fixtures festnageln — sonst zementiert das Repo sie wieder.
  const files = fs.readdirSync(import.meta.dirname).filter(
    (f) => f.startsWith("test-") && f.endsWith(".mjs") && f !== "test-deploy-literals.mjs",
  )
  for (const f of files) {
    const src = read(`./${f}`)
    assert.ok(
      !src.includes("🚀 OpenClaw Release"),
      `${f} nagelt Deploy-Titel "🚀 OpenClaw Release" fest`,
    )
  }
})

if (failed > 0) {
  console.log(`FAIL-Suite: ${failed} Fehler`)
  process.exit(1)
}
console.log("PASS  Deploy-Literale-Vertrag: Repo bleibt universal (kein install-spezifischer Titel)")