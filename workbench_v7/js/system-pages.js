/* V7 system screens: presentation only. No collection, configuration writes or secret reads. */
(function () {
  'use strict';
  if (typeof V7App === 'undefined') return;
  const app = V7App;
  const byId = id => document.getElementById(id);
  const make = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text != null) element.textContent = String(text);
    if (className) element.className = className;
    return element;
  };
  const unknown = value => value == null || value === '' || ['UNKNOWN', 'UNAVAILABLE', 'NOT_RETURNED'].includes(String(value));
  const visibleValue = value => unknown(value) ? 'UNKNOWN' : String(value);
  const configLabels = {
    configured: '配置存在性',
    provider_host: '服务地址主机',
    model: '当前模型',
    connection_status: '连接状态',
    last_error: '最近错误状态',
    secret_returned: '凭证返回标记',
    write_enabled: '配置写入标记',
    quota_used: '已记录通知用量',
    quota_limit: '每日通知上限',
    last_notification: '最近通知记录'
  };

  function readableJson(text) {
    if (!/^[\[{]/.test(text.trim())) return null;
    try {
      const parsed = JSON.parse(text);
      return parsed && typeof parsed === 'object' ? JSON.stringify(parsed, null, 2) : null;
    } catch (_) {
      return null;
    }
  }

  function enhanceConfig(id) {
    const root = byId(id);
    if (!root) return;
    const rows = Array.from(root.children).filter(node => node.matches('article.card'));
    if (!rows.length) {
      if (!root.querySelector('.system-empty')) root.append(make('p', '配置状态暂未返回，当前值待确认。', 'system-empty'));
      return;
    }
    for (const row of rows) {
      const heading = row.querySelector('h2');
      const value = row.querySelector('p');
      if (!heading || !value || row.dataset.systemPrepared === 'true') continue;
      const key = heading.textContent;
      row.classList.add('system-config-row');
      row.dataset.configKey = key;
      row.dataset.valueKind = unknown(value.textContent) ? 'unknown' : 'recorded';
      if (configLabels[key]) {
        heading.textContent = configLabels[key];
        heading.append(make('small', key, 'system-config-key'));
      }
      value.classList.add('system-config-value');
      const formatted = readableJson(value.textContent);
      if (formatted) value.textContent = formatted;
      row.dataset.systemPrepared = 'true';
    }
  }

  function enhanceSources() {
    const root = byId('sources-grid');
    if (!root) return;
    for (const card of root.querySelectorAll('.source-card')) {
      const panel = card.querySelector('.state-panel');
      if (panel) panel.setAttribute('aria-label', '当前接口返回状态');
    }
  }

  function enhanceDataCenter() {
    const root = byId('data-center-grid');
    if (!root || Array.from(root.children).some(node => node.classList.contains('system-record-group'))) return;
    const cards = Array.from(root.children).filter(node => node.matches('article.card'));
    if (!cards.length) return;
    const groups = [make('section', null, 'system-record-group'), make('section', null, 'system-record-group')];
    groups[0].append(make('h2', '存储元数据'));
    groups[1].append(make('h2', '数据合同与范围'));
    for (const [index, card] of cards.entries()) {
      const key = card.querySelector('h2')?.textContent || '';
      const value = card.querySelector('p');
      card.dataset.recordKey = key;
      if (value) {
        const formatted = readableJson(value.textContent);
        card.dataset.recordKind = formatted ? 'json' : unknown(value.textContent) ? 'unknown' : 'recorded';
        if (formatted) value.textContent = formatted;
      }
      groups[index < 4 ? 0 : 1].append(card);
    }
    root.replaceChildren(...groups);
  }

  function ensureHealthWorkspace() {
    const page = byId('page-health');
    const status = byId('system-health-state');
    if (!page || !status) return null;
    let grid = byId('system-health-facts');
    if (!grid) {
      grid = make('div', null, 'system-health-grid');
      grid.id = 'system-health-facts';
      grid.setAttribute('aria-label', '健康状态分项');
      status.after(grid);
      const boundary = make('section', null, 'system-health-boundary');
      boundary.append(make('h2', '状态解读'), make('p', '业务健康、进程心跳、安全状态与数据完整性分别读取。进程存活不独立证明业务正常；具体限制以当前原始状态为准。'));
      grid.after(boundary);
    }
    return grid;
  }

  function renderHealthFacts(health, safety, g6) {
    const root = ensureHealthWorkspace();
    if (!root) return;
    const rows = [
      ['业务健康', health?.business_status, '业务运行情况以此状态为准。'],
      ['进程心跳', health?.daemon_liveness, '仅代表进程存活，不代表业务健康。'],
      ['安全状态', safety?.status, safety?.new_entry_disabled === true ? '接口记录的新开仓限制已开启。' : '新开仓限制请结合原始摘要核对。'],
      ['G6 数据完整性', g6, '保留当前接口的完整性状态。']
    ];
    root.replaceChildren(...rows.map(([label, value, note]) => {
      const card = make('article', null, 'system-health-fact');
      card.dataset.healthKind = unknown(value) ? 'unknown' : 'recorded';
      card.append(make('h2', label), make('strong', visibleValue(value)), make('p', note));
      return card;
    }));
  }

  let reportFocus = null;
  function ensureReportWorkspace() {
    const page = byId('page-reports');
    const list = byId('reports-list');
    const filters = byId('report-filters');
    const viewer = byId('report-viewer');
    if (!page || !list || !filters || !viewer) return null;
    let workspace = byId('system-report-workspace');
    if (!workspace) {
      workspace = make('div', null, 'system-report-workspace');
      workspace.id = 'system-report-workspace';
      const catalog = make('section', null, 'system-report-catalog');
      catalog.setAttribute('aria-label', '白名单报告目录');
      filters.before(workspace);
      catalog.append(filters, list);
      workspace.append(catalog, viewer);
    }
    return workspace;
  }

  function enhanceReportReader() {
    const workspace = ensureReportWorkspace();
    const viewer = byId('report-viewer');
    if (!workspace || !viewer) return;
    const open = !viewer.hidden;
    workspace.classList.toggle('is-reading', open);
    if (!open || viewer.querySelector('.system-report-close')) return;
    const close = make('button', '关闭正文', 'system-report-close');
    close.type = 'button';
    close.addEventListener('click', () => {
      viewer.hidden = true;
      workspace.classList.remove('is-reading');
      if (reportFocus?.isConnected) reportFocus.focus();
      else workspace.querySelector('button')?.focus();
    });
    viewer.prepend(close);
  }

  function wrapAsync(name, after) {
    if (typeof app[name] !== 'function') return;
    const original = app[name];
    app[name] = async function (...args) {
      const result = await original.apply(this, args);
      after();
      return result;
    };
  }
  wrapAsync('refreshIntegrations', () => {
    enhanceConfig('llm-grid');
    enhanceConfig('wecom-grid');
  });
  wrapAsync('refreshDataCenter', enhanceDataCenter);
  wrapAsync('refreshDataSources', enhanceSources);

  if (typeof app.renderHealth === 'function') {
    const original = app.renderHealth;
    app.renderHealth = function (health, safety, g6) {
      const result = original.call(this, health, safety, g6);
      renderHealthFacts(health, safety, g6);
      return result;
    };
  }
  if (typeof app.renderUnavailable === 'function') {
    const original = app.renderUnavailable;
    app.renderUnavailable = function (...args) {
      const result = original.apply(this, args);
      renderHealthFacts(null, null, null);
      return result;
    };
  }
  if (typeof app.openResearchReport === 'function') {
    const original = app.openResearchReport;
    app.openResearchReport = async function (...args) {
      const viewerId = args[2] || 'report-viewer';
      if (viewerId === 'report-viewer') reportFocus = document.activeElement;
      const result = await original.apply(this, args);
      if (viewerId === 'report-viewer') {
        enhanceReportReader();
        requestAnimationFrame(() => byId('report-viewer')?.scrollIntoView({behavior: 'auto', block: 'start'}));
      }
      return result;
    };
  }

  function initialize() {
    ensureReportWorkspace();
    renderHealthFacts(null, null, null);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize, {once: true});
  else initialize();
})();
