/* nexus-memory Web UI — API Client */

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
      return res.json();
    } catch (err) {
      if (err && err.name === 'AbortError') {
        throw new Error(`Request timed out after 15000ms: ${url}`);
      }
      throw err;
    } finally {
      clearTimeout(timeout);
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
    if (filters.limit) params.set('limit', filters.limit);
    if (!params.has('limit')) params.set('limit', '500');
    return this.request(`/api/memories?${params}`);
  },

  async searchMemories(query) {
    return this.request(`/api/memories/search?q=${encodeURIComponent(query)}&limit=20`);
  },

  async getMemory(id) {
    return this.request(`/api/memories/${encodeURIComponent(id)}`);
  },

  async getStats() {
    return this.request('/api/stats');
  },
};
