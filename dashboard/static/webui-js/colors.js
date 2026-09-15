/* nexus-memory Web UI — shared color maps (single source of truth).
 *
 * Previously app.js (category + drift colors) and graph.js (category colors)
 * each carried their own copy, which drift apart silently. Loaded before
 * graph.js/app.js in graph.html so both bundles read the same values. */
const NEXUS_CATEGORY_COLORS = {
  fact: '#3b82f6', belief: '#8b5cf6', session: '#f59e0b',
  rule: '#10b981', preference: '#ec4899', temp: '#6b7280',
};

const NEXUS_DRIFT_COLORS = {
  fresh: '#22c55e', drifting: '#eab308', drifted: '#ef4444', not_tracked: '#6b7280',
};
