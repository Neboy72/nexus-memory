/* nexus-memory Web UI — shared color maps (single source of truth).
 *
 * Previously app.js (category + drift colors) and graph.js (category colors)
 * each carried their own copy, which drifted apart silently. Loaded before
 * graph.js/app.js in graph.html so both bundles read the same values.
 *
 * Jede Kategorie im Bestand hat hier eine EIGENE Farbe. Fehlte eine, fiel sie
 * auf den Ersatzton — und der war exakt die Temp-Farbe, sodass 6.024 Eintraege
 * aus neun Kategorien in der Anzeige wie "Temp" aussahen. Wer eine neue
 * Kategorie einfuehrt, traegt sie hier ein. */
const NEXUS_CATEGORY_COLORS = {
  fact: '#3b82f6',           // blau
  entity: '#06b6d4',         // cyan
  rule: '#10b981',           // gruen
  uncategorized: '#d946ef',  // fuchsia
  session: '#f59e0b',        // amber
  preference: '#ec4899',     // pink
  belief: '#8b5cf6',         // violett
  pattern: '#84cc16',        // limette
  procedure: '#14b8a6',      // teal
  lesson: '#eab308',         // gelb
  decision: '#f97316',       // orange-rot
  test: '#a78bfa',           // lavendel
  idea: '#fde047',           // hellgelb
  sica_session: '#7dd3fc',   // hellblau
  temp: '#6b7280',           // grau — fluechtig, bleibt bewusst unauffaellig
};

// Nur fuer Kategorien, die es hier noch nicht gibt. Bewusst HELL und damit
// klar unterscheidbar vom Temp-Grau: ein unbekannter Eintrag darf nicht wie
// ein temporaerer aussehen.
const NEXUS_CATEGORY_FALLBACK_COLOR = '#e5e7eb';

const NEXUS_DRIFT_COLORS = {
  fresh: '#22c55e', drifting: '#eab308', drifted: '#ef4444', not_tracked: '#6b7280',
};

// Frozen: a consumer must not be able to mutate the shared source of truth for
// every other bundle (they are plain globals in a classic script).
Object.freeze(NEXUS_CATEGORY_COLORS);
Object.freeze(NEXUS_DRIFT_COLORS);
