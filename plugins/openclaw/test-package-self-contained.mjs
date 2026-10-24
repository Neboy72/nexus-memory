// Waechter (Kiosha, 24.10.2026): Das veroeffentlichte Paket muss sich selbst tragen.
//
// Belegter Defekt in 1.21.13: dist/index.js importiert "@sinclair/typebox" zur Laufzeit,
// das tgz enthaelt das Paket aber nicht (es stand nur in peerDependencies und
// devDependencies, und der Build markierte es mit --external). Eine frische
// ClawHub-Installation starb deshalb beim Laden:
//   ERR_MODULE_NOT_FOUND: Cannot find package '@sinclair/typebox'
//
// Regel: dist/index.js darf AUSSCHLIESSLICH "openclaw" und "openclaw/..." als bare
// (nicht relative, nicht node:-) Importe nennen. Alles andere muss im Bundle liegen,
// denn der ClawHub-Installer liefert nur den Tarball und linkt hoechstens den
// openclaw-Peer.
//
// Der Test baut nichts. Er prueft dist/index.js, wie es ausgeliefert wird.
import { readFileSync, existsSync } from "node:fs"
import assert from "node:assert/strict"

const DIST = new URL("./dist/index.js", import.meta.url)

if (!existsSync(DIST)) {
  console.error("FEHLT: plugins/openclaw/dist/index.js — erst `npm run build` laufen lassen.")
  process.exit(2)
}

const source = readFileSync(DIST, "utf8")

const specifiers = new Set()
// ESM-Importdeklarationen stehen bei esbuild je Zeile am Anfang
// ("import { X } from "y";"). Nur diese Form ist ein echter Specifier —
// ein Muster mitten im Text trifft sonst Fehlerstrings wie "... failed".
for (const m of source.matchAll(/^\s*import\s+(?:[^"'\n]*?\bfrom\s*)?["']([^"']+)["']/gm)) {
  specifiers.add(m[1])
}
for (const m of source.matchAll(/\bimport\(\s*["']([^"']+)["']\s*\)/g)) specifiers.add(m[1])

// Minifizierte Bundles enthalten solche Muster auch in String-Literalen (Fehlertexte).
// Ein echter Import-Specifier ist kurz, ohne Leerzeichen/Zeilenumbruch und hat keine
// Klammern — genau diese Form wird geprueft.
const looksLikeSpecifier = (s) => /^[A-Za-z0-9@_./-]+$/.test(s) && s.length < 120

const isRelative = (s) => s.startsWith(".") || s.startsWith("/")
const isNodeBuiltin = (s) => s.startsWith("node:")
const isAllowlistedPeer = (s) => s === "openclaw" || s.startsWith("openclaw/")

const bare = [...specifiers].filter(
  (s) => looksLikeSpecifier(s) && !isRelative(s) && !isNodeBuiltin(s) && !isAllowlistedPeer(s),
)

assert.deepEqual(
  bare,
  [],
  `dist/index.js enthaelt ungedeckte Laufzeit-Importe: ${bare.join(", ")}. ` +
    "Sie liegen nicht im Tarball — ein frisch installiertes Plugin stirbt beim Laden mit " +
    "ERR_MODULE_NOT_FOUND. Diese Pakete in das Bundle aufnehmen (--external entfernen) " +
    "oder aus dem Code entfernen.",
)

console.log(`OK: dist/index.js ist selbsttragend (${specifiers.size} Specifier geprueft, 0 offene bare Importe).`)
