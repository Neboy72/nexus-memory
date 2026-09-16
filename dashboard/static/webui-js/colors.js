/* nexus-memory Web UI — shared color maps (single source of truth).
 *
 * Previously app.js (category + drift colors) and graph.js (category colors)
 * each carried their own copy, which drifted apart silently. Loaded before
 * graph.js/app.js in graph.html so both bundles read the same values. */
const NEXUS_CATEGORY_COLORS = {
  fact: '#3b82f6', belief: '#8b5cf6', session: '#f59e0b',
  rule: '#10b981', preference: '#ec4899', temp: '#6b7280',
};

const NEXUS_DRIFT_COLORS = {
  fresh: '#22c55e', drifting: '#eab308', drifted: '#ef4444', not_tracked: '#6b7280',
};

// Frozen: a consumer must not be able to mutate the shared source of truth for
// every other bundle (they are plain globals in a classic script).
Object.freeze(NEXUS_CATEGORY_COLORS);
Object.freeze(NEXUS_DRIFT_COLORS);
