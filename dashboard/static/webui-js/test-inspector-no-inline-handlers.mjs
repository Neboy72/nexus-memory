// Inline-handler breakout regression test for inspector.js (OCR finding #2).
//
// `&#39;` is decoded back to `'` by the HTML parser BEFORE the inline handler
// is compiled as JS, so `onclick="Inspector._detail('...')"` could be broken
// out of with an id like `');alert(document.cookie)//`. The fix replaces all
// inline handlers with delegated listeners + data-* attributes.
//
// inspector.js is a browser bundle (touches `document` at load time), so it is
// NOT imported here. These are STRUCTURE-CONTRACT assertions on the source:
// they fail if a regression reintroduces an inline handler or drops the
// delegation wiring.
//
// Run: node --test dashboard/static/webui-js/test-inspector-no-inline-handlers.mjs

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(__dirname, 'inspector.js'), 'utf8');

test('no inline Inspector.* handlers remain', () => {
  assert.doesNotMatch(src, /onclick="Inspector/);
  assert.doesNotMatch(src, /onclick="/);
});

test('data-insp-action and data-mem-id attributes are used', () => {
  assert.match(src, /data-insp-action="back"/);
  assert.match(src, /data-insp-action="edit"/);
  assert.match(src, /data-insp-action="save"/);
  assert.match(src, /data-insp-action="restore"/);
  assert.match(src, /data-insp-action="deprecate"/);
  assert.match(src, /data-mem-id="\$\{this\._esc\(/);
});

test('delegated click listener is registered', () => {
  assert.match(src, /document\.addEventListener\('click',/);
  assert.match(src, /closest\('\[data-insp-action\]'\)/);
  assert.match(src, /closest\('\.insp-row'\)/);
});

test('_detail delegation stays intact', () => {
  assert.match(src, /_detail\(id\)\s*\{\s*this\.why\(id\);\s*\}/);
});
