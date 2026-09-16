// XSS regression test for dashboard/static/webui-js/app.js (OCR finding #1).
//
// app.js is a browser bundle: importing it requires a DOM (DOMContentLoaded,
// document, window), so it is NOT imported here. Two things are verified:
//   1. the escapeHtml() helper extracted from the source behaves correctly;
//   2. app.js calls escapeHtml() at every untrusted interpolation site.
// (2) is a STRUCTURE CONTRACT: it guards against a regression that reopens the
// XSS by removing/losing an escapeHtml() wrapper on a tainted value.
//
// Run: node --test dashboard/static/webui-js/test-app-escape.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));

// Lazy source load (W38/F1: a missing app.js must fail as a named test, not as
// an uncaught top-level ENOENT that skips the whole file).
let src = null;
let body = null;
function loadSource() {
  if (src !== null) return src;
  src = readFileSync(join(__dirname, 'app.js'), 'utf8');
  // escapeHtml is verified by SOURCE CONTRACT (no Function()/eval — banned):
  // the body must chain all 5 entity replacements (& < > " ') in this order.
  // Order matters: & must be replaced FIRST or double-escaping breaks round-trips.
  // W38/F2+F7: brace-counted extraction instead of a non-greedy first-} regex —
  // robust against nested blocks and reformatted closing braces.
  const fnIdx = src.indexOf('function escapeHtml');
  assert.ok(fnIdx >= 0, 'escapeHtml() helper must exist in app.js');
  const braceStart = src.indexOf('{', fnIdx);
  let depth = 0, end = -1;
  for (let i = braceStart; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}') { depth--; if (depth === 0) { end = i; break; } }
  }
  assert.ok(end > braceStart, 'escapeHtml body braces unbalanced');
  body = src.slice(braceStart + 1, end);
  assert.match(body, /String\(s \?\? ''\)/, 'null/undefined must coerce to empty string');
  const expectedOrder = ['\u0026/g, \u0027\u0026amp;\u0027', '</g, \u0027\u0026lt;\u0027', '>/g, \u0027\u0026gt;\u0027', '/\u0022/g, \u0027\u0026quot;\u0027', "/\u0027/g, \u0027\u0026#39;\u0027"];
  let last = -1;
  for (const token of expectedOrder) {
    const idx = body.indexOf(token);
    assert.ok(idx > last, `escapeHtml must replace entities in order, missing: ${token}`);
    last = idx;
  }
  return src;
}

test('escapeHtml: full body contract (order + entities + coercion)', () => {
  const s = loadSource();
  assert.ok(body.includes("&lt;"), "escapes <");
  assert.ok(body.includes("&gt;"), "escapes >");
  assert.ok(body.includes("&quot;"), "escapes double quote (attribute breakout)");
  assert.ok(body.includes("&#39;"), "escapes single quote (attribute breakout)");
  assert.ok(body.includes("&amp;"), "escapes & first (round-trip safety)");
  assert.ok(body.length > 0, "body contract extracted a real function body");
});

test('app.js escapes untrusted values at all tainted interpolation sites (a/b/c paths)', () => {
  // (a) graph-error path: err.message
  assert.match(src, /\$\{escapeHtml\(err\.message\)\}/);
  // (b) showDetail detailBody
  assert.match(src, /\$\{escapeHtml\(d\.category \|\| d\._category \|\| 'fact'\)\}/);
  assert.match(src, /\$\{escapeHtml\(d\.access_level \|\| d\.access \|\| 'unknown'\)\}/);
  assert.match(src, /\$\{escapeHtml\(d\.drift \|\| 'unknown'\)\}/);
  assert.match(src, /\$\{escapeHtml\(paperCat\)\}/);
  assert.match(src, /\$\{escapeHtml\(cleanLabel\(/);
  assert.match(src, /\$\{escapeHtml\(d\.source\)\}/);
  assert.match(src, /\$\{escapeHtml\(d\.id\)\}/);
  assert.match(src, /\$\{escapeHtml\(created\)\}/);
  // (c) badge tooltip: author + quote from data-* attributes
  assert.match(src, /\$\{escapeHtml\(author\)\}/);
  assert.match(src, /\$\{escapeHtml\(quote\)\}/);
});

test('app.js does not interpolate raw untrusted values (full negative guard)', () => {
  const s = loadSource();
  for (const expr of ['err.message', 'd.category', 'd._category', 'd.access_level', 'd.access',
                      'd.drift', 'paperCat', 'd.source', 'd.id', 'created', 'author', 'quote',
                      'd.title', 'd.content', 'd.name']) {
    assert.doesNotMatch(s, new RegExp(`\\$\\{${expr.replaceAll('.', '\\.')}\\}`),
                        `raw ${expr} darf nicht interpoliert werden`);
  }
  // F9 (low): auch Concat-Injektion + Bracket-Form schliessen
  assert.doesNotMatch(s, /['"]\s*\+\s*(author|quote|err\.message)/, 'String-Concat-Injektion verboten');
  assert.doesNotMatch(s, /\$\{\s*err\[.message.\]\s*\}/, 'Bracket-Access-Rohinterpolation verboten');
});
