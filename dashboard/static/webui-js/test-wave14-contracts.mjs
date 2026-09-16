// Wave 14 structure contracts for the dashboard bundles (H145-H153, H403-H406).
//
// app.js / api.js / inspector.js / graph.js are browser bundles: importing them
// needs a DOM (DOMContentLoaded, document, d3), so they are NOT imported here —
// except api.js, whose object literal is materialized with new Function() and
// exercised against a stubbed global fetch.
//
// Run: node --test dashboard/static/webui-js/test-wave14-contracts.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const read = (f) => readFileSync(join(__dirname, f), 'utf8');
const appSrc = read('app.js');
const apiSrc = read('api.js');
const inspSrc = read('inspector.js');
const graphSrc = read('graph.js');

// ── H145: applyFilters is fire-and-forget with a catch ──────────────────────

test('H145: no bare applyFilters() call remains', () => {
  assert.doesNotMatch(appSrc, /^\s*applyFilters\(\);\s*$/m);
});

test('H145: handler sites use the guarded wrapper (no bare calls, sites >= 4)', () => {
  assert.match(appSrc, /void applyFilters\(\)\.catch\(/);
  const calls = appSrc.match(/queueApplyFilters\(\);/g) || [];
  // W38 (medium): exakte Zählung bricht bei legitimen Refactors. Unter-Grenze:
  // die 3 change-Handler + debounced input müssen da sein; MEHR Sites sind kein Fehler.
  assert.ok(calls.length >= 4, `erwartet >= 4 queueApplyFilters-Sites, gefunden: ${calls.length}`);
});

// ── H146: dead ternary removed ──────────────────────────────────────────────

test('H146: statGrownWeek uses a plain nullish default', () => {
  assert.match(appSrc, /el\('statGrownWeek'\)\.textContent = stats\.grown_this_week \?\? '-';/);
  assert.doesNotMatch(appSrc, /\(stats\.grown_this_week \?\? '-'\) \+/);
});

// ── H147: NaN-safe confidence formatting at all 3 sites ─────────────────────

test('H147: formatConfidence helper exists and is NaN-safe', () => {
  // Source contract (no Function()/eval): body must guard with Number.isFinite
  // (NaN/undefined → '-'), scale by 100 and append '%'.
  const m = appSrc.match(/function formatConfidence\s*\(v\)\s*\{([\s\S]*?)\n\s*\}/);
  assert.ok(m, 'formatConfidence() must exist');
  const body = m[1];
  assert.match(body, /Number\.isFinite\(v \* 100\)/, 'NaN/undefined guard required');
  assert.match(body, /toFixed\(0\)/, 'integer-percent rendering required');
  assert.match(body, /'-'|"-"/, 'non-finite fallback required');
});

test('H147: helper/guard used at all 3 sites, raw multiplication gone', () => {
  assert.match(appSrc, /formatConfidence\(stats\.avg_confidence\)/);
  assert.match(appSrc, /formatConfidence\(d\.confidence\)/);
  assert.match(appSrc, /Number\.isFinite\(d\.confidence \* 100\)/);
  assert.doesNotMatch(appSrc, /\(stats\.avg_confidence \* 100\)\.toFixed/);
  assert.doesNotMatch(appSrc, /style="width:\$\{d\.confidence \* 100\}%"/);
});

// ── H403: dead lastScroll removed ───────────────────────────────────────────

test('H403: lastScroll is gone', () => {
  assert.doesNotMatch(appSrc, /lastScroll/);
  // the header-class behaviour itself must survive
  assert.match(appSrc, /header\.classList\.add\('header--scrolled'\)/);
});

// ── H405: href="#" cannot reach querySelector ───────────────────────────────

test('H405: anchor handler guards against a bare "#"', () => {
  assert.match(appSrc, /const href = anchor\.getAttribute\('href'\);/);
  assert.match(appSrc, /if \(href && href\.length > 1\)/);
});

// ── H404: shared color maps ─────────────────────────────────────────────────

test('H404: colors.js is the single source for category/drift colors', () => {
  const colors = read('colors.js');
  assert.match(colors, /const NEXUS_CATEGORY_COLORS = \{/);
  assert.match(colors, /const NEXUS_DRIFT_COLORS = \{/);
  // neither bundle carries its own copy any more
  assert.doesNotMatch(graphSrc, /fact:\s*'#3b82f6'/);
  assert.doesNotMatch(appSrc, /fact: '#3b82f6'/);
  assert.doesNotMatch(appSrc, /fresh:'#22c55e'/);
  assert.match(graphSrc, /NEXUS_CATEGORY_COLORS/);
  assert.match(appSrc, /NEXUS_CATEGORY_COLORS\[d\.category\]/);
  assert.match(appSrc, /Object\.entries\(NEXUS_DRIFT_COLORS\)/);
});

test('H404: graph.html loads colors.js before graph.js', () => {
  const html = readFileSync(join(__dirname, '..', 'graph.html'), 'utf8');
  const colorsAt = html.indexOf('webui-js/colors.js');
  const graphAt = html.indexOf('webui-js/graph.js');
  const appAt = html.indexOf('webui-js/app.js');
  // W38 (low): fehlende Scripts liefern sonst eine irreführende "load order"-Meldung.
  assert.ok(colorsAt !== -1, 'colors.js fehlt in graph.html (script-tag umbenannt?)');
  assert.ok(graphAt !== -1, 'graph.js fehlt in graph.html (script-tag umbenannt?)');
  assert.ok(appAt !== -1, 'app.js fehlt in graph.html (script-tag umbenannt?)');
  assert.ok(colorsAt < graphAt && graphAt < appAt, 'load order: colors → graph → app');
});

// ── H148/H149/H150: api.js ──────────────────────────────────────────────────

test('H148: method renamed to request, no this.fetch left', () => {
  assert.doesNotMatch(apiSrc, /async fetch\(/);
  assert.match(apiSrc, /async request\(url\)/);
  assert.doesNotMatch(apiSrc, /this\.fetch\(/);
  assert.ok((apiSrc.match(/this\.request\(/g) || []).length >= 5);
});

test('H149: AbortController + 15s timeout, cleared in finally', () => {
  assert.match(apiSrc, /new AbortController\(\)/);
  assert.match(apiSrc, /controller\.abort\(\), 15000\)/);
  assert.match(apiSrc, /clearTimeout\(timeout\)/);
  assert.match(apiSrc, /finally \{/);
});

test('H150: error body is read, statusText only a fallback', () => {
  assert.match(apiSrc, /await res\.text\(\)/);
  assert.doesNotMatch(apiSrc, /throw new Error\(`HTTP \$\{res\.status\}: \$\{res\.statusText\}`\)/);
});

test('H148-H150: request() behaviour with a stubbed fetch', async () => {
  // api.js is materialized via dynamic import of a data: URL (no Function()/eval —
  // banned): the file is DOM-free, so the object literal evaluates cleanly. We
  // append an export line because the bundle itself never exports (browser tag).
  const apiDataUrl = `data:text/javascript,${encodeURIComponent(apiSrc + "\nexport default API;")}`;
  const { default: API } = await import(apiDataUrl);
  const originalFetch = globalThis.fetch;
  try {
    // success path (parseJson now reads text(), per H148 empty-body contract)
    globalThis.fetch = async () => ({
      ok: true, status: 200,
      url: '/api/x',
      headers: { get: () => 'application/json' },
      text: async () => JSON.stringify({ ok: 1 }),
    });
    assert.deepEqual(await API.request('/api/x'), { ok: 1 });

    // error path: detail from the JSON body beats the empty statusText
    globalThis.fetch = async () => ({
      ok: false, status: 503, statusText: '',
      text: async () => JSON.stringify({ detail: 'qdrant unreachable' }),
    });
    await assert.rejects(() => API.request('/api/x'), /HTTP 503: qdrant unreachable/);

    // abort path (W38, medium): ECHTER AbortController — der Stub prüft, dass das
    // Signal der Produktion überhaupt mitgegeben wurde, und lehnt dann real ab.
    // Ein Regression, die controller.signal nicht an fetch durchreicht, wird so sichtbar.
    const seenSignals = [];
    globalThis.fetch = (_url, opts = {}) => {
      seenSignals.push(opts.signal ?? null);
      const abortErr = new Error('aborted');
      abortErr.name = 'AbortError';
      return Promise.reject(abortErr);
    };
    await assert.rejects(() => API.request('/api/x'), /Request timed out after 15000ms: \/api\/x/);
    assert.ok(seenSignals.length === 1, `request() muss genau 1 fetch rufen, waren ${seenSignals.length}`);
    assert.ok(seenSignals[0] instanceof AbortSignal, 'request() muss controller.signal an fetch übergeben (H149)');
  } finally {
    globalThis.fetch = originalFetch;
  }
});

// ── H151: inspector staleness token ─────────────────────────────────────────

test('H151: load() and why() both use the request token', () => {
  assert.match(inspSrc, /_reqSeq: 0/);
  // W38 (medium): strukturelle Proxy-Counts (starts===2, checks>=4) brechen bei
  // harmlosen Refactors. Semantik bleibt: JEDER load/why-Einstieg erzeugt einen
  // Token, jede Rückkehr prüft ihn — mindestens je 2/4, aber keine Obergrenze.
  const starts = inspSrc.match(/const token = \+\+this\._reqSeq;/g) || [];
  assert.ok(starts.length >= 2, `erwartet >= 2 Token-Starts (load + why), gefunden: ${starts.length}`);
  const checks = inspSrc.match(/if \(token !== this\._reqSeq\) return;/g) || [];
  assert.ok(checks.length >= 4, 'success + catch in each method');
  // Jeder Start hat mindestens eine zugehörige Prüfung (Paarungs-Beweis):
  assert.ok(checks.length >= starts.length, 'jeder Token-Start braucht mindestens eine Token-Prüfung');
});

// ── H152/H153/H406: graph.js ────────────────────────────────────────────────

test('H152: bound this._zoom reused by resetZoom', () => {
  assert.match(graphSrc, /this\._zoom = d3\.zoom\(\)/);
  assert.match(graphSrc, /this\.svg\.call\(this\._zoom\)/);
  assert.match(graphSrc, /this\._zoom\.transform, d3\.zoomIdentity/);
  assert.doesNotMatch(graphSrc, /d3\.zoom\(\)\.transform/);
});

test('H153: nullish defaults keep a real 0', () => {
  assert.match(graphSrc, /confidence: m\.confidence \?\? 0\.7/);
  assert.match(graphSrc, /drift: m\.drift \?\? 'not_tracked'/);
  assert.match(graphSrc, /category: m\.category \?\? 'fact'/);
  assert.match(graphSrc, /access_level: m\.access_level \?\? 'unknown'/);
  assert.doesNotMatch(graphSrc, /m\.confidence \|\| 0\.7/);
});

test('H406: logo-strip hack removed', () => {
  assert.doesNotMatch(graphSrc, /logo\|logos/);
  assert.doesNotMatch(graphSrc, /replace\(\/\\b\(logo/);
  // whitespace collapse + 4-word cut stays
  assert.match(graphSrc, /d\.text\.replace\(\/\\s\+\/g, ' '\)\.trim\(\)/);
});
