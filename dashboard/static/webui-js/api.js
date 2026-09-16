/* nexus-memory Web UI — API Client */

// Tuning knobs kept in one place so the client and the backend contract cannot
// drift apart as scattered literals.
const DEFAULT_PAGE_SIZE = 500;
const SEARCH_LIMIT = 20;

const API = {
  // Named `request`, not `fetch`: a method called `fetch` shadows the global
  // within the object and reads as if it were the platform fetch. All internal
  // callers use this.request().
  async request(url) {
    // Bound every call: a hung request would otherwise leave the UI in its
    // loading state forever. 15s is generous for the local dashboard.
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const res = await fetch(url, { signal: controller.signal });
      if (!res.ok) {
        // statusText is empty under HTTP/2 and the real reason (FastAPI
        // `detail`) lives in the body — read it before throwing.
        let detail = '';
        try {
          const body = await res.text();
          if (body) {
            try {
              const parsed = JSON.parse(body);
              const d = parsed.detail ?? parsed.error;
              if (typeof d === 'string') detail = d;
              else if (d !== undefined) detail = JSON.stringify(d);
            } catch {
              detail = body;
            }
          }
        } catch {
          /* body unreadable — fall back to statusText below */
        }
        throw new Error(`HTTP ${res.status}: ${detail || res.statusText || 'request failed'}`);
      }
      return await this.parseJson(res);
    } catch (err) {
      if (err && err.name === 'AbortError') {
        throw new Error(`Request timed out after 15000ms: ${url}`);
      }
      throw err;
    } finally {
      clearTimeout(timeout);
    }
  },

  // A 2xx does not guarantee a JSON body: 204/205 have none, and a proxy or
  // error page can be served with a 2xx status and a non-JSON content type.
  // Parsing those unconditionally would surface as a raw "Unexpected end of
  // JSON input" instead of a usable result.
  async parseJson(res) {
    if (res.status === 204 || res.status === 205) return {};
    const text = await res.text();
    if (!text) return {};
    try {
      return JSON.parse(text);
    } catch {
      const contentType = res.headers.get('content-type') || '';
      // Advertised as JSON but malformed — surface a concrete error rather
      // than silently returning an empty object.
      if (contentType.includes('json')) {
        throw new Error(`Invalid JSON in response from ${res.url || 'request'} (HTTP ${res.status})`);
      }
      // Non-JSON success body: treat as no data. Callers guard their fields.
      return {};
    }
  },

  async getHealth() {
    return this.request('/api/health');
  },

  async getMemories(filters = {}) {
    const params = new URLSearchParams();
    if (filters.category && filters.category !== 'all') params.set('category', filters.category);
    if (filters.access_level && filters.access_level !== 'all') params.set('access_level', filters.access_level);
    if (filters.drift && filters.drift !== 'all') params.set('drift', filters.drift);
    if (filters.source) params.set('source', filters.source);
    // Explicit != null: a legitimate limit is never silently dropped by
    // truthiness. Non-numeric / non-positive values are ignored rather than
    // forwarded verbatim to the backend.
    const limit = Number(filters.limit);
    if (Number.isInteger(limit) && limit > 0) params.set('limit', limit);
    if (!params.has('limit')) params.set('limit', DEFAULT_PAGE_SIZE);
    return this.request(`/api/memories?${params}`);
  },

  async searchMemories(query) {
    return this.request(`/api/memories/search?q=${encodeURIComponent(query)}&limit=${SEARCH_LIMIT}`);
  },

  async getMemory(id) {
    return this.request(`/api/memories/${encodeURIComponent(id)}`);
  },

  async getStats() {
    return this.request('/api/stats');
  },
};
