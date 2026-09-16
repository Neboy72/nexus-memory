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
const src = readFileSync(join(__dirname, 'app.js'), 'utf8');

// escapeHtml is verified by SOURCE CONTRACT (no Function()/eval — banned):
// the body must chain all 5 entity replacements (& < > " ') in this order.
// Order matters: & must be replaced FIRST or double-escaping breaks round-trips.
const bodyMatch = src.match(/function escapeHtml\s*\(s\)\s*\{([\s\S]*?)\n\}/);
assert.ok(bodyMatch, 'escapeHtml() helper must exist in app.js');
const body = bodyMatch[1];
assert.match(body, /String\(s \?\? ''\)/, 'null/undefined must coerce to empty string');
const expectedOrder = ['&/g, \'&amp;\'', '</g, \'&lt;\'', '>/g, \'&gt;\'', '/"/g, \'&quot;\'', "/'/g, \'&#39;\'"];
let last = -1;
for (const token of expectedOrder) {
  const idx = body.indexOf(token);
  assert.ok(idx > last, `escapeHtml must replace entities in order, missing: ${token}`);
  last = idx;
}

test('escapeHtml escapes angle brackets (body contract)', () => {
  assert.ok(body.includes("&lt;"), "escapes <");
  assert.ok(body.includes("&gt;"), "escapes >");
  assert.ok(body.includes("&quot;"), "escapes double quote (attribute breakout)");
  assert.ok(body.includes("&#39;"), "escapes single quote (attribute breakout)");
  assert.ok(body.includes("&amp;"), "escapes & first (round-trip safety)");
});

test('app.js escapes untrusted values at all 3 tainted sites', () => {
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

test('app.js does not interpolate raw err.message/author/quote', () => {
  assert.doesNotMatch(src, /\$\{err\.message\}/);
  assert.doesNotMatch(src, /\$\{author\}/);
  assert.doesNotMatch(src, /\$\{quote\}/);
});
