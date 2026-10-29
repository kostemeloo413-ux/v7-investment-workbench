/* Market and decision views: presentation only, existing renderers own qualification. */
(function () {
  'use strict';
  if (typeof V7App === 'undefined') return;
  const app = V7App;
  const $ = id => document.getElementById(id);
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text != null) element.textContent = String(text);
    if (className) element.className = className;
    return element;
  };
  const finite = value => typeof value === 'number' && Number.isFinite(value);
  function after(method, decorate) {
    const original = app[method];
    if (typeof original !== 'function') return;
    app[method] = function (...args) {
      const result = original.apply(this, args);
      decorate(...args);
      return result;
    };
  }

  function market() {
    const root = $('market-environment-content');
    const page = $('page-market');
    if (!root || !page) return;
    let empty = $('market-evidence-empty');
    if (!empty) {
      empty = node('div', null, 'state-panel unavailable market-evidence-empty');
      empty.id = 'market-evidence-empty';
      empty.append(node('strong', '市场环境暂待确认'), node('p', '当前没有可用于环境判断的合格记录。已有来源与状态可在判断依据中查看。'));
      root.before(empty);
    }
    empty.hidden = !root.hidden;
    for (const card of root.querySelectorAll('.dashboard-index-card')) {
      const missing = card.querySelector('.dashboard-series-absent');
      if (missing) missing.textContent = '分时记录待确认';
      const percent = card.querySelector('.market-environment-percent');
      if (!percent || percent.parentElement.classList.contains('market-index-footer')) continue;
      const footer = node('div', null, 'market-index-footer');
      footer.append(node('span', '规则记录涨跌', 'market-measure-label'));
      percent.before(footer);
      footer.append(percent);
    }
  }

  function decorateNewsList(root) {
    if (!root) return;
    [...root.querySelectorAll('.news-item')].forEach(article => {
      if (article.dataset.marketStyled === 'true') return;
      article.dataset.marketStyled = 'true';
      // Read this article's already-rendered source field. Never pair rows by array position.
      const meta = article.querySelector(':scope > .news-meta');
      const metadata = meta?.textContent || '';
      const field = metadata.match(/来源记录时间：([^（）]*)（[^（）]*）\s*$/u);
      const raw = field?.[1].trim();
      const match = typeof raw === 'string' ? raw.match(/(?:^|[T\s])([01]\d|2[0-3]):([0-5]\d)(?::[0-5]\d(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?$/) : null;
      const stamp = node('span', match ? match[1] + ':' + match[2] : '—', 'news-record-stamp');
      if (match) stamp.setAttribute('aria-label', '来源记录时间 ' + raw);
      article.prepend(stamp);
      if (meta) {
        const source = metadata.split(' · 来源记录时间：')[0];
        const recorded = raw && raw !== 'UNKNOWN' ? raw.replace('T', ' ') : '';
        meta.textContent = source + (recorded ? ' · 记录时间：' + recorded : '');
      }
      const analysis = article.querySelector('.news-ai > summary');
      if (analysis) analysis.textContent = 'AI 研判';
      const paragraph = article.querySelector(':scope > .news-summary');
      if (paragraph && paragraph.textContent.trim()) {
        const reading = node('details', null, 'news-saved-content');
        reading.append(node('summary', '阅读保存内容'));
        paragraph.before(reading);
        reading.append(paragraph);
      }
    });
  }
  function news() {
    const batch = $('news-capture-time');
    if (batch) {
      const match = batch.textContent.match(/\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/);
      batch.textContent = match ? '记录批次：' + match[0].replace('T', ' ') : '';
    }
    decorateNewsList($('news-hot-list'));
    decorateNewsList($('news-ordinary-list'));
  }

  function picks(envelope) {
    const page = $('page-picks');
    const grid = $('picks-role-grid');
    const aiq = $('picks-aiq-card');
    if (!page || !grid) return;
    let heading = $('market-picks-heading');
    if (!heading) {
      heading = node('div', null, 'market-picks-heading');
      heading.id = 'market-picks-heading';
      heading.append(node('h2', '五策略观点'), node('span', '分别查看原始观点与确认条件', 'muted'));
      grid.before(heading);
    }
    if (aiq) {
      let lane = $('market-model-lane');
      if (!lane) {
        lane = node('section', null, 'market-model-lane');
        lane.id = 'market-model-lane';
        lane.setAttribute('aria-label', 'AI Quant 模型候选');
        grid.after(lane);
      }
      lane.append(aiq);
      if (!aiq.querySelector('.market-model-note')) {
        const note = node('p', '模型得分与上涨概率是预测证据，不是实际涨跌或交易授权。', 'market-model-note');
        $('aiq-pulse-meta')?.after(note);
      }
    }
    grid.hidden = grid.children.length === 0;
    heading.hidden = grid.hidden;
    for (const card of grid.querySelectorAll('.pick-card')) {
      const h2 = card.querySelector('h2');
      const cardHeading = card.querySelector('.card-heading');
      if (cardHeading && h2) cardHeading.dataset.personaInitial = [...h2.textContent.trim()][0] || '';
      let statusSlot = card.querySelector('.persona-status-slot');
      if (!statusSlot) {
        statusSlot = node('div', null, 'persona-status-slot');
        const opinion = card.querySelector('.pick-opinion');
        if (opinion) opinion.after(statusSlot);
      }
      if (['P-COOL','P-HANSHU'].includes(card.dataset.pickParticipant)) {
        const note = card.querySelector('.persona-paused-note') || node('p','账户暂停，仅展示观察记录','persona-paused-note');
        statusSlot.append(note);
      }
    }
    const data = envelope?.data?.ai_quant_top5;
    const predictions = data?.predictions;
    const buttons = $('aiq-top5-list')?.querySelectorAll('button.aiq-row');
    if (data?.classification !== 'CURRENT_DAY_TOP5' || data.status !== 'NORMAL' || !Array.isArray(predictions) || !buttons || buttons.length !== predictions.length) return;
    [...buttons].forEach((button, index) => {
      const prediction = predictions[index];
      if (!prediction) return;
      button.classList.add('market-model-row');
      button.replaceChildren();
      button.append(node('span', '#' + (prediction.rank ?? '—'), 'market-model-rank'));
      const identity = node('span', null, 'market-model-identity');
      identity.append(node('strong', prediction.name || '名称待确认'), node('small', prediction.symbol || '代码待确认'));
      const score = node('span', null, 'market-model-measure');
      score.append(node('small', '模型分'), node('strong', finite(prediction.raw_score) ? prediction.raw_score.toFixed(4) : '—'));
      const probability = node('span', null, 'market-model-measure');
      probability.append(node('small', '预测上涨概率'), node('strong', finite(prediction.up_probability) ? (prediction.up_probability * 100).toFixed(2) + '%' : '—'));
      button.append(identity, score, probability);
    });
  }

  function stock() {
    const root = $('stock-detail');
    if (!root || root.querySelector('.stock-evidence-nav')) return;
    const cards = [...root.querySelectorAll(':scope > article.stock-evidence-section')];
    if (!cards.length || cards[0].classList.contains('stock-empty')) return;
    cards.forEach((card, index) => {
      const title = card.querySelector('h2')?.textContent.trim();
      card.dataset.readerSection = card.classList.contains('stock-identity') ? 'identity'
        : title === '今日策略关注' ? 'personas' : title === 'AI Quant' ? 'model'
          : title === '相关新闻' ? 'news' : title === '风险观察' ? 'risk' : 'quote';
      card.dataset.readerIndex = String(index);
      card.tabIndex = -1;
      for (const opinion of card.querySelectorAll('.stock-opinion')) {
        opinion.classList.toggle('market-opinion-consider', opinion.textContent.trim() === '考虑买入');
      }
    });
    if (cards.length < 3) return;
    const navigation = node('nav', null, 'stock-evidence-nav');
    navigation.setAttribute('aria-label', '个股证据阅读');
    for (const card of cards.slice(1)) {
      const title = card.querySelector('h2')?.textContent.trim();
      if (!title) continue;
      const button = node('button', title, 'stock-reader-link');
      button.type = 'button';
      button.addEventListener('click', () => {
        const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
        card.scrollIntoView({ block: 'start', behavior: reduced ? 'instant' : 'smooth' });
        card.focus({ preventScroll: true });
      });
      navigation.append(button);
    }
    cards[0].after(navigation);
  }

  after('renderMarket', market);
  after('renderNews', news);
  after('renderPicks', picks);
  after('renderStock', stock);
  function ready() {
    for (const id of ['page-market', 'page-news', 'page-picks', 'page-scoring']) $(id)?.classList.add('market-unified-page');
    market();
    picks(app.overviewEvidence?.picks);
    news(app.overviewEvidence?.news);
    stock();
  }
  if (document.readyState === 'loading') window.addEventListener('DOMContentLoaded', ready);
  else ready();
})();
