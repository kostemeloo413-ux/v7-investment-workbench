/* V7 account-page presentation only. Existing IDs, handlers, records and data admissions stay authoritative. */
(function () {
  'use strict';

  const ids = ['page-arena', 'page-personal', 'page-ai-quant', 'page-forward', 'page-plans', 'page-backtest'];
  const $ = id => document.getElementById(id);
  const observers = [];
  let queued = false;

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (typeof text === 'string') element.textContent = text;
    return element;
  }

  function wrapNodes(parent, before, className, children) {
    if (!parent || !children.length) return null;
    const wrapper = node('div', className);
    parent.insertBefore(wrapper, before);
    children.filter(Boolean).forEach(child => wrapper.append(child));
    return wrapper;
  }

  function sectionHeading(text, id) {
    const heading = node('h2', 'account-section-heading', text);
    if (id) heading.id = id;
    return heading;
  }

  function composePersonal() {
    const page = $('page-personal');
    const form = $('personal-holding-form');
    const holdings = $('personal-holdings');
    if (!page || !form || !holdings || page.querySelector('.personal-workspace')) return;
    const workspace = node('div', 'personal-workspace');
    const editor = node('section', 'account-editor');
    const records = node('section', 'account-personal-records');
    editor.setAttribute('aria-labelledby', 'personal-editor-heading');
    records.setAttribute('aria-labelledby', 'personal-records-heading');
    page.insertBefore(workspace, form);
    editor.append(sectionHeading('记录持仓', 'personal-editor-heading'),
      node('p', 'account-section-note', '手动记录数量与成本，保存在当前浏览器。'), form);
    records.append(sectionHeading('个人持仓记录', 'personal-records-heading'), holdings);
    workspace.append(editor, records);
    const importLabel = page.querySelector('label[for="personal-import-file"]');
    if (importLabel) {
      importLabel.tabIndex = 0;
      importLabel.setAttribute('role', 'button');
      importLabel.addEventListener('keydown', event => {
        if (event.key !== 'Enter' && event.key !== ' ') return;
        event.preventDefault();
        $('personal-import-file')?.click();
      });
    }
  }

  function composeAiQuant() {
    const page = $('page-ai-quant');
    const predictions = $('aiq-page-rows');
    const ledger = $('aiq-paper-summary');
    if (!page || !predictions || page.querySelector('.account-aiq-candidates')) return;
    const candidateSection = node('section', 'account-aiq-candidates');
    candidateSection.setAttribute('aria-labelledby', 'aiq-candidate-heading');
    page.insertBefore(candidateSection, predictions);
    candidateSection.append(sectionHeading('模型候选', 'aiq-candidate-heading'), predictions);
    if (ledger) {
      const ledgerSection = node('section', 'account-aiq-ledger');
      ledgerSection.setAttribute('aria-label', '已保存模拟账户');
      page.insertBefore(ledgerSection, ledger);
      ledgerSection.append(ledger);
    }
    const boundary = page.querySelector('.aiq-lifecycle-note');
    if (boundary && boundary.textContent.includes('≠')) {
      const words = boundary.textContent.split('≠').map(text => text.trim());
      boundary.replaceChildren();
      boundary.setAttribute('aria-label', words.join('不等于'));
      words.forEach((text, index) => {
        if (index) {
          const separator = node('span', 'account-evidence-separator', '≠');
          separator.setAttribute('aria-hidden', 'true');
          boundary.append(separator);
        }
        boundary.append(node('span', 'account-evidence-stage', text));
      });
    }
  }

  function composeForward() {
    const page = $('page-forward');
    const summary = $('forward-summary');
    const runtime = $('forward-runtime-note');
    if (!page || !summary || !runtime || page.querySelector('.account-forward-workspace')) return;
    const workspace = node('div', 'account-forward-workspace');
    const runtimeSection = node('section', 'account-forward-runtime');
    runtimeSection.setAttribute('aria-labelledby', 'forward-runtime-heading');
    page.insertBefore(workspace, summary);
    runtimeSection.append(sectionHeading('运行记录', 'forward-runtime-heading'), runtime);
    workspace.append(summary, runtimeSection);
  }

  function composeResearch() {
    const page = $('page-backtest');
    if (!page || page.querySelector('.account-research-workspace')) return;
    const sections = Array.from(page.children).filter(child => child.classList.contains('research-section'));
    if (sections.length) wrapNodes(page, sections[0], 'account-research-workspace', sections);
  }

  function enhancePersonalFacts() {
    const root = $('personal-holdings');
    if (!root) return;
    root.querySelectorAll('.evidence-fact').forEach(row => {
      const label = row.querySelector('dt')?.textContent;
      if (label === '当前价格/市值' || label === '浮盈亏') row.classList.add('account-fact-unknown');
      if (label === '备注') row.classList.add('account-fact-notes');
    });
    root.querySelectorAll(':scope > p').forEach(paragraph => paragraph.classList.add('account-empty-message'));
  }

  function enhancePredictions() {
    const root = $('aiq-page-rows');
    if (!root) return;
    root.querySelectorAll('.aiq-prediction-card').forEach(card => {
      if (card.querySelector('.account-prediction-identity')) return;
      const items = Array.from(card.children);
      const identity = items.slice(0, 3);
      if (identity.length !== 3 || identity[1].tagName !== 'H2') return;
      const header = node('div', 'account-prediction-identity');
      card.insertBefore(header, card.firstChild);
      identity.forEach(item => header.append(item));
    });
    if (!root.childElementCount) root.append(node('p', 'account-empty-message', '当日完整模型候选待确认，状态与依据见上方说明。'));
    $('aiq-paper-summary')?.querySelectorAll(':scope > p').forEach(paragraph => paragraph.classList.add('account-empty-message'));
  }

  function enhancePlans() {
    for (const id of ['plans-list', 'aiq-plan-records']) {
      $(id)?.querySelectorAll(':scope > p').forEach(paragraph => paragraph.classList.add('account-empty-message'));
    }
    $('plans-list')?.querySelectorAll(':scope > .aiq-plan-record').forEach(card => {
      if (card.querySelector(':scope > .plan-record-details')) return;
      const title=card.querySelector(':scope > h3');if(!title)return;
      const time=[...card.querySelectorAll('.evidence-facts > div')].find(row=>row.querySelector('dt')?.textContent==='记录时间')?.querySelector('dd')?.textContent || '时间待确认';
      const details=node('details','plan-record-details'),summary=node('summary','plan-record-summary');
      summary.append(title,node('span','plan-record-time',time),node('span','plan-record-state','计划记录'));
      const content=node('div','plan-record-content');
      [...card.children].forEach(child=>content.append(child));
      details.append(summary,content);card.append(details);
    });
  }

  function enhanceForward() {
    const day = $('forward-summary')?.querySelector('.aiq-forward-day');
    if (day) day.classList.toggle('account-forward-unknown', !/^Day \d+$/.test(day.textContent.trim()));
    const runtime = $('forward-runtime-note');
    if (runtime && !runtime.childElementCount && !runtime.textContent.trim()) {
      runtime.append(node('p', 'muted', '最近运行记录待确认。'));
    }
  }

  function enhanceResearchFacts() {
    const signedReturns = new Set(['Top5 5日平均收益', '同期候选基准', '毛超额收益', '10bps模拟净收益', '历史模拟净收益率']);
    $('page-backtest')?.querySelectorAll('.evidence-fact').forEach(row => {
      const label = row.querySelector('dt')?.textContent;
      const value = row.querySelector('dd')?.textContent?.trim();
      if (!signedReturns.has(label) || !/^[+-]?\d+(?:\.\d+)?%$/.test(value || '')) return;
      const numeric = Number(value.slice(0, -1));
      row.classList.toggle('account-return-up', numeric > 0);
      row.classList.toggle('account-return-down', numeric < 0);
    });
  }

  function enhanceArenaTables() {
    const numericLabels = new Set(['股数', '成本价', '最近账本估值', '持仓市值', '浮动盈亏', '收益率', '成交股数', '成交价', '交易费用']);
    $('arena-detail')?.querySelectorAll('table').forEach(table => {
      const headers = Array.from(table.querySelectorAll('thead th'));
      headers.forEach((header, index) => {
        if (!numericLabels.has(header.textContent.trim())) return;
        header.classList.add('account-table-number');
        table.querySelectorAll('tbody tr').forEach(row => row.children[index]?.classList.add('account-table-number'));
      });
    });
  }

  function enhance() {
    queued = false;
    enhancePersonalFacts();
    enhancePredictions();
    enhancePlans();
    enhanceForward();
    enhanceResearchFacts();
    enhanceArenaTables();
  }

  function schedule() {
    if (queued) return;
    queued = true;
    requestAnimationFrame(enhance);
  }

  function initialize() {
    ids.forEach(id => $(id)?.classList.add('account-page'));
    composePersonal();
    composeAiQuant();
    composeForward();
    composeResearch();
    for (const id of ['personal-holdings', 'aiq-page-rows', 'aiq-paper-summary', 'aiq-plan-records', 'plans-list',
      'forward-runtime-note', 'forward-summary', 'research-oos-metrics', 'research-replay-metrics', 'arena-detail']) {
      const target = $(id);
      if (!target) continue;
      const observer = new MutationObserver(schedule);
      observer.observe(target, { childList: true, subtree: true });
      observers.push(observer);
    }
    enhance();
  }

  if (document.readyState === 'loading') window.addEventListener('DOMContentLoaded', initialize, { once: true });
  else initialize();
})();
