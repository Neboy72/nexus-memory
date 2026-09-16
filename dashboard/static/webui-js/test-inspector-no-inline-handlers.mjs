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

// W39/F4+F7: Source-Laden mit fail-loud Diagnose statt unhandled module-scope throw
// (Umbenennung/Layout-Änderung → klare Meldung, nicht kryptischer Crash).
let src;
try {
  src = readFileSync(join(__dirname, 'inspector.js'), 'utf8');
} catch (e) {
  console.error(`SMOKE-FAIL: inspector.js nicht lesbar unter ${join(__dirname, 'inspector.js')} — ${e?.stack ?? String(e)}`);
  process.exitCode = 1;
}

if (src !== undefined) {
  test('no inline handlers remain (onclick=" anywhere in markup strings)', () => {
    // W39/F1: Der erste Check (/onclick="Inspector/) war von /onclick="/ subsumiert
    // (dead assertion). Ein einzelner, vollständiger Check genügt — zusätzlich
    // ohne Inspector-Namensraum, damit ein Umbau der Tabellen keine Lücke lässt.
    assert.doesNotMatch(src, /onclick="/);
  });

  test('data-insp-action attributes cover ALL dispatchable actions', () => {
    // W39/F5: Aus der Dispatch-Tabelle abgeleitet (nicht dupliziert): jede
    // action === '...' -Zeile in der Delegation muss ein data-Attribut-Template
    // haben. Drift in einer Richtung schlägt an.
    const dispatchActions = [...src.matchAll(/action === '([a-z]+)'/g)].map((m) => m[1]);
    assert.ok(dispatchActions.length >= 5, `Dispatch-Tabelle muss ≥5 Actions haben, fand ${dispatchActions.length}: ${dispatchActions}`);
    for (const action of dispatchActions) {
      assert.match(src, new RegExp(`data-insp-action="${action}"`),
        `Action '${action}' ist dispatcht, aber kein Template erzeugt sie (gedroppter Button?)`);
    }
    // Und die Kern-Actions existieren (positiver Kern, auch wenn die Tabelle driftet):
    for (const required of ['back', 'edit', 'save', 'restore', 'deprecate']) {
      assert.match(src, new RegExp(`data-insp-action="${required}"`), `required action ${required} fehlt`);
    }
  });

  test('data-mem-id attributes route through the escape helper (attribute-safe)', () => {
    // W39/F6: Beweis tiefer als '_esc wird aufgerufen': die Template-Ausdrücke müssen
    // ALLE data-mem-id-Werte durch _esc leiten — jede data-mem-id= ohne _esc ist
    // ein XSS-Verdacht und schlägt an.
    const attrTemplates = [...src.matchAll(/data-mem-id="\$\{([^}]*)\}"/g)].map((m) => m[1]);
    assert.ok(attrTemplates.length >= 1, 'mindestens ein data-mem-id-Template erwartet');
    for (const expr of attrTemplates) {
      assert.match(expr, /_esc\(/, `data-mem-id-Wert muss durch _esc: \${${expr}}`);
    }
  });

  test('delegated click listener is registered', () => {
    assert.match(src, /document\.addEventListener\('click',/);
    assert.match(src, /closest\('\[data-insp-action\]'\)/);
    assert.match(src, /closest\('\.insp-row'\)/);
  });

  test('_detail delegation stays intact (formatting-tolerant)', () => {
    // W39/F3+F8: Nicht mehr an exakte Whitespace/Formatierung gekoppelt — der
    // _detail-Body darf Kommentare/Guards enthalten; gepinnt bleibt nur, dass
    // _detail(id) an this.why(id) delegiert (der Kontrakt des Breakout-Fixes).
    const m = src.match(/_detail\(id\)\s*\{([\s\S]*?)\n  \}/);
    assert.ok(m, '_detail(id)-Methode gefunden');
    assert.match(m[1], /this\.why\(id\)/, '_detail muss an this.why(id) delegieren');
  });
}