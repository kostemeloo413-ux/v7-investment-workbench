/* V7 personal portfolio only. This store never reads or writes simulation accounts. */
(function attachPersonalPortfolioStore(root, factory) {
  const store = factory();
  if (typeof module === 'object' && module.exports) module.exports = store;
  if (root) root.V7PersonalPortfolioStore = store;
})(typeof globalThis !== 'undefined' ? globalThis : this, function createStore() {
  const STORAGE_KEY = 'v7_personal_portfolio_v1';
  const PREVIOUS_V7_KEY = 'v7_personal_portfolio';
  const LEGACY_V6_KEY = 'user_portfolio';
  const STORAGE_FORMAT = 'V7_PERSONAL_PORTFOLIO';
  const BACKUP_FORMAT = 'V7_PERSONAL_PORTFOLIO_BACKUP';
  const SCHEMA_VERSION = 1;

  function fail(code) {
    const error = new Error(code);
    error.code = code;
    throw error;
  }

  function expectedExchange(code) {
    if (code.startsWith('6')) return 'sh';
    if (code.startsWith('4') || code.startsWith('8')) return 'bj';
    return 'sz';
  }

  function normalizeSymbol(value) {
    if (typeof value !== 'string') fail('PERSONAL_SYMBOL_INVALID');
    const match = value.trim().match(/^(?:(SH|SZ|BJ))?(\d{6})(?:\.(SH|SZ|BJ))?$/i);
    if (!match) fail('PERSONAL_SYMBOL_INVALID');
    const code = match[2];
    const prefix = match[1]?.toLowerCase();
    const suffix = match[3]?.toLowerCase();
    if (prefix && suffix && prefix !== suffix) fail('PERSONAL_EXCHANGE_CONFLICT');
    const exchange = prefix || suffix || expectedExchange(code);
    if (exchange !== expectedExchange(code)) fail('PERSONAL_EXCHANGE_CONFLICT');
    return `${exchange}${code}`;
  }

  function normalizeHolding(item) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) fail('PERSONAL_HOLDING_INVALID');
    const symbol = normalizeSymbol(item.symbol ?? item.code);
    const nameValue = item.name ?? item.symbol_name;
    if (typeof nameValue !== 'string') fail('PERSONAL_NAME_INVALID');
    const name = nameValue.trim();
    if (!name || name.length > 80) fail('PERSONAL_NAME_INVALID');
    const shares = Number(item.shares ?? item.quantity);
    if (!Number.isSafeInteger(shares) || shares < 1) fail('PERSONAL_SHARES_INVALID');
    const costPrice = Number(item.cost_price ?? item.costPrice);
    if (!Number.isFinite(costPrice) || costPrice <= 0) fail('PERSONAL_COST_INVALID');
    const notesValue = item.notes ?? '';
    if (typeof notesValue !== 'string' || notesValue.length > 300) fail('PERSONAL_NOTES_INVALID');
    return { symbol, name, shares, cost_price: costPrice, notes: notesValue.trim() };
  }

  function validateHoldings(holdings) {
    if (!Array.isArray(holdings)) fail('PERSONAL_HOLDINGS_NOT_ARRAY');
    const normalized = holdings.map(normalizeHolding);
    const seen = new Set();
    for (const item of normalized) {
      if (seen.has(item.symbol)) fail('PERSONAL_DUPLICATE_SYMBOL');
      seen.add(item.symbol);
    }
    return normalized;
  }

  function decodeJson(raw, failureCode) {
    try { return JSON.parse(raw); }
    catch (_) { return fail(failureCode); }
  }

  function readLegacyArray(raw, source) {
    const parsed = decodeJson(raw, 'PERSONAL_STORAGE_JSON_INVALID');
    if (!Array.isArray(parsed)) fail('PERSONAL_STORAGE_SCHEMA_INVALID');
    return { holdings: validateHoldings(parsed), source };
  }

  function readStorage(storage) {
    const versionedRaw = storage.getItem(STORAGE_KEY);
    if (versionedRaw !== null) {
      const parsed = decodeJson(versionedRaw, 'PERSONAL_STORAGE_JSON_INVALID');
      if (!parsed || parsed.format !== STORAGE_FORMAT || parsed.schema_version !== SCHEMA_VERSION) {
        fail('PERSONAL_STORAGE_SCHEMA_INVALID');
      }
      return { holdings: validateHoldings(parsed.holdings), source: 'V7_LOCAL_V1' };
    }
    const previousV7Raw = storage.getItem(PREVIOUS_V7_KEY);
    if (previousV7Raw !== null) return readLegacyArray(previousV7Raw, 'V7_LOCAL_PREVIOUS_SCHEMA_READ_ONLY');
    const legacyV6Raw = storage.getItem(LEGACY_V6_KEY);
    if (legacyV6Raw !== null) return readLegacyArray(legacyV6Raw, 'V6_USER_PORTFOLIO_READ_ONLY_COMPATIBILITY_VIEW');
    return { holdings: [], source: 'V7_LOCAL_V1_EMPTY' };
  }

  function serializeStorage(holdings) {
    return JSON.stringify({
      format: STORAGE_FORMAT,
      schema_version: SCHEMA_VERSION,
      holdings: validateHoldings(holdings),
    });
  }

  function upsert(holdings, item, index = null) {
    const current = validateHoldings(holdings);
    const normalized = normalizeHolding(item);
    const next = current.slice();
    if (index === null || index === '') next.push(normalized);
    else {
      const parsedIndex = Number(index);
      if (!Number.isInteger(parsedIndex) || parsedIndex < 0 || parsedIndex >= next.length) fail('PERSONAL_EDIT_INDEX_INVALID');
      next[parsedIndex] = normalized;
    }
    return validateHoldings(next);
  }

  function remove(holdings, index) {
    const next = validateHoldings(holdings);
    if (!Number.isInteger(index) || index < 0 || index >= next.length) fail('PERSONAL_DELETE_INDEX_INVALID');
    next.splice(index, 1);
    return next;
  }

  function createBackup(holdings, exportedAt = new Date().toISOString()) {
    return {
      format: BACKUP_FORMAT,
      schema_version: SCHEMA_VERSION,
      exported_at: exportedAt,
      holdings: validateHoldings(holdings),
    };
  }

  function parseBackup(input) {
    const parsed = typeof input === 'string' ? decodeJson(input, 'PERSONAL_BACKUP_JSON_INVALID') : input;
    if (!parsed || parsed.format !== BACKUP_FORMAT || parsed.schema_version !== SCHEMA_VERSION ||
        typeof parsed.exported_at !== 'string' || !Number.isFinite(Date.parse(parsed.exported_at))) {
      fail('PERSONAL_BACKUP_SCHEMA_INVALID');
    }
    return validateHoldings(parsed.holdings);
  }

  return {
    STORAGE_KEY,
    PREVIOUS_V7_KEY,
    LEGACY_V6_KEY,
    STORAGE_FORMAT,
    BACKUP_FORMAT,
    SCHEMA_VERSION,
    normalizeSymbol,
    normalizeHolding,
    validateHoldings,
    readStorage,
    serializeStorage,
    upsert,
    remove,
    createBackup,
    parseBackup,
  };
});
