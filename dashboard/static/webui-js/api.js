/* nexus-memory Web UI — API Client */

const API = {
  async fetch(url) {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${res.statusText}`);
    return res.json();
  },

  async getHealth() {
    return this.fetch('/api/health');
  },

  async getMemories(filters = {}) {
    const params = new URLSearchParams();
    if (filters.category && filters.category !== 'all') params.set('category', filters.category);
    if (filters.access_level && filters.access_level !== 'all') params.set('access_level', filters.access_level);
    if (filters.drift && filters.drift !== 'all') params.set('drift', filters.drift);
    if (filters.source) params.set('source', filters.source);
    if (filters.limit) params.set('limit', filters.limit);
    if (!params.has('limit')) params.set('limit', '500');
    return this.fetch(`/api/memories?${params}`);
  },

  async searchMemories(query) {
    return this.fetch(`/api/memories/search?q=${encodeURIComponent(query)}&limit=20`);
  },

  async getMemory(id) {
    return this.fetch(`/api/memories/${encodeURIComponent(id)}`);
  },

  async getStats() {
    return this.fetch('/api/stats');
  },
};
