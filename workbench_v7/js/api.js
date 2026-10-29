/**
 * workbench_v7/js/api.js
 * Phase UI-3: V7 只读聚合 API 统一客户端 (只读、带超时、防并发、带缓存)
 */
const V7Api = {
  baseUrl: document.documentElement.dataset.uiDemo === 'synthetic-v1' ? '/api/ui-demo' : '/api/v7',

  async getOverview() {
    return this._fetchJson(`${this.baseUrl}/overview`);
  },

  async getMarket() {
    return this._fetchJson(`${this.baseUrl}/market`);
  },

  async getNews() {
    return this._fetchJson(`${this.baseUrl}/news`);
  },

  async getPicks() {
    return this._fetchJson(`${this.baseUrl}/picks`);
  },

  async getStock(symbol) {
    return this._fetchJson(`${this.baseUrl}/stock/${encodeURIComponent(symbol)}`);
  },

  async getArena() {
    return this._fetchJson(`${this.baseUrl}/arena`);
  },

  async getDataCenter() { return this._fetchJson(`${this.baseUrl}/data-center`); },
  async getDataSources() { return this._fetchJson(`${this.baseUrl}/data-sources`); },
  async getLlmStatus() { return this._fetchJson(`${this.baseUrl}/integrations/llm`); },
  async getWecomStatus() { return this._fetchJson(`${this.baseUrl}/integrations/wecom`); },

  async getAiQuant() {
    return this._fetchJson(`${this.baseUrl}/ai-quant`);
  },

  async getPaper() {
    return this._fetchJson(`${this.baseUrl}/paper`);
  },

  async getForward() {
    return this._fetchJson(`${this.baseUrl}/forward`);
  },

  async getTradePlans(params = {}) {
    const q = new URLSearchParams(params).toString();
    return this._fetchJson(`${this.baseUrl}/trade-plans${q ? '?' + q : ''}`);
  },

  async getArena() {
    return this._fetchJson(`${this.baseUrl}/arena`);
  },

  async getResearch(reportId = null) {
    const q = reportId ? `?report_id=${encodeURIComponent(reportId)}` : '';
    return this._fetchJson(`${this.baseUrl}/research${q}`);
  },

  async getSystem() {
    return this._fetchJson(`${this.baseUrl}/system`);
  },

  async _fetchJson(url) {
    try {
      const resp = await fetch(url, { method: 'GET', headers: { 'Accept': 'application/json' } });
      if (!resp.ok) {
        throw new Error(`HTTP Error ${resp.status} on ${url}`);
      }
      return await resp.json();
    } catch (err) {
      console.warn(`[V7Api] Request failed for ${url}:`, err);
      return {
        schema_version: 'v7.1',
        status: 'UNAVAILABLE',
        warnings: [err.message],
        data_freshness: { freshness_status: 'UNAVAILABLE' },
        data: null
      };
    }
  }
};
