/* V7 UI A1: render only fields backed by the read-only View Model. */
const V7ResearchReportMeta = {
  phase12_research_report: { title: 'AI Quant Phase12 样本外研究', category: '研究', description: '冻结模型在历史样本外区间的研究结果。' },
  phase12_entry_gate: { title: 'Phase12 研究准入检查', category: '准入', description: 'Phase12 研究开始前的资格与数据检查记录。' },
  phase12_human_audit: { title: 'Phase12 人工审计', category: '研究', description: 'Phase12 样本口径、统计与假设的审计记录。' },
  phase13_production_gap_audit: { title: '生产差距审计', category: '系统', description: '研究制品与生产运行之间的差异审计。' },
  phase13a_artifact_promotion: { title: '研究制品晋级记录', category: '系统', description: '研究制品晋级相关的原始记录。' },
  phase14_shadow_operational_report: { title: 'Shadow 运行验证', category: '系统', description: 'Shadow 阶段运行观察报告。' },
  phase14_5_risk_gate_human_audit: { title: '风险准入人工审计', category: '风险', description: '风险准入条件的人工审计记录。' },
  phase15_risk_calibration_research: { title: '风险校准研究', category: '风险', description: '风险校准相关研究证据。' },
  phase15_5_risk_contract_decision: { title: '风险合同决议', category: '风险', description: '风险合同审查与决议原文。' },
  phase16a_risk_contract_patch_report: { title: '风险合同变更报告', category: '风险', description: '风险合同实施阶段的原始变更报告。' },
  phase16b_shadow_risk_validation: { title: 'Shadow 风险验证', category: '风险', description: 'Shadow 阶段风险验证记录。' },
  phase17_daily_report: { title: 'Phase17 日报', category: '系统', description: 'Phase17 阶段的日度运行报告。' },
  phase17_paper_simulation_gate: { title: '模拟运行准入检查', category: '准入', description: '模拟运行阶段的准入检查记录。' },
  phase18_baseline_freeze: { title: 'Phase18 基线冻结记录', category: '回放', description: '历史模拟验证所用基线与参数封存记录。' },
  phase18_forward_20d_interim_report: { title: 'Phase18 历史模拟验证', category: '回放', description: '20日中期模拟结果；完整性审计将该批结果定性为历史回放。' },
  phase18_5_forward_integrity: { title: 'Phase18 历史回放完整性审计', category: '回放', description: '回放时序、收益归因与风险分布审计。' },
  phase18f_genuine_forward_start_gate: { title: '真实前向启动准入检查', category: 'Forward', description: '真实前向观察的启动条件与状态记录。' },
  phase_ui_0_frontend_architecture_audit: { title: '前端架构审计', category: '系统', description: '工作台前端架构的审计记录。' },
  phase_ui_1_v7_frontend_redesign_blueprint: { title: 'V7 前端改版蓝图', category: '系统', description: 'V7 工作台的前端改版设计记录。' },
  v7_ui_a1_overview: { title: 'V7 工作台总览实现报告', category: '系统', description: 'V7 总览界面实施报告。' },
  v7_ui_a2_market_news: { title: 'V7 市场与新闻实现报告', category: '系统', description: '市场与新闻界面的实施报告。' },
  v7_g6_p1c1_scanner: { title: '扫描链路集成报告', category: '系统', description: '扫描链路集成阶段的报告。' },
  v7_g6_p1c2_parity: { title: '扫描数据一致性报告', category: '系统', description: '扫描数据一致性核验报告。' },
  new_season_architecture: { title: '新赛季架构决策', category: '系统', description: '新赛季架构决策原文。' },
  new_season_launch_admission: { title: '新赛季数据准入报告', category: '准入', description: '新赛季数据准入记录。' }
};

/* Phase 1 display-only vocabulary: raw values remain in system details. */
const V7Display = {
  status(value, context = 'status') {
    if (value === true || value === false) {
      const pairs = { permission: ['关闭', '开启'], real: ['实盘交易关闭', '实盘交易开启'], entryDisabled: ['允许新开仓', '禁止新开仓'] };
      return pairs[context] ? pairs[context][value ? 1 : 0] : (value ? '是' : '否');
    }
    const unknown = { time: '时间待确认', permission: '权限待确认', health: '健康状态待确认', status: '状态待确认' };
    if (value == null || value === '' || value === 'UNKNOWN') return unknown[context] || '待确认';
    const labels = { STALE: context === 'quote' ? '报价已过期' : '数据较旧', UNAVAILABLE: '暂不可用', EMPTY: '暂无数据', DEGRADED: '部分数据受限', ACTIVE: '进行中', PAPER_ACTIVE: '模拟赛进行中', ERROR: '数据需检查', INVALID: '数据需检查' };
    return labels[value] || unknown[context] || '待确认';
  },
  season(season) {
    return season?.season_id === 'V7-SEASON-20260930-V2' && season?.status === 'PAPER_ACTIVE' && season?.data_status === 'VALID'
      ? '赛季二进行中' : '赛季状态待确认';
  },
  real(value) { return typeof value === 'boolean' ? this.status(value, 'real') : '实盘交易状态待确认'; },
  today() { return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date()).replaceAll('-', ''); },
  currentTop5(data, today = this.today()) {
    const rows = data?.recent_predictions;
    return data?.prediction_classification === 'CURRENT_DAY_TOP5' && data?.prediction_status === 'NORMAL'
      && typeof data?.prediction_pulse?.trade_date === 'string' && data.prediction_pulse.trade_date.replaceAll('-', '') === today
      && Array.isArray(rows) && rows.length === 5 && new Set(rows.map(row => row.symbol)).size === 5
      && rows.every(row => typeof row.symbol === 'string' && /^[0-9]{6}$/.test(row.symbol))
      && rows.map(row => row.rank).sort((a, b) => a - b).join(',') === '1,2,3,4,5';
  },
  opinions(data, today = this.today()) {
    if (['INVALID', 'STALE', 'UNAVAILABLE', 'EMPTY'].includes(data?.data_status) || data?.scan_date_status !== 'CURRENT_TRADING_DATE' || typeof data?.trade_date !== 'string' || data.trade_date.replaceAll('-', '') !== today || !Array.isArray(data?.participants)) return '今日策略观点待确认';
    const ids = ['P-LIAO-XIAOAI', 'P-COOL', 'P-YINWENTAO', 'P-HANSHU', 'P-RULE-QUANT'];
    const roles = ids.map(id => data.participants.find(item => item.participant_id === id));
    if (roles.some(item => !item || !['BUY', 'NO_SIGNAL', 'HOLD', 'WAIT', 'OBSERVE', 'SELL', 'NO_TRADE'].includes(item.action))) return '今日策略观点待确认';
    const buy = roles.filter(item => item.action === 'BUY').length;
    const observe = roles.filter(item => ['NO_SIGNAL', 'HOLD', 'WAIT', 'OBSERVE'].includes(item.action)).length;
    const other = roles.length - buy - observe;
    return [buy ? buy + ' 个策略考虑买入' : '', observe ? observe + ' 个策略继续观察' : '', other ? other + ' 个策略暂无买入观点' : ''].filter(Boolean).join(' · ');
  }
};

/* Owner decision A1: display existing trend only; never classify from quotes. */
const V7MarketEnvironment = {
  labels: { bull: '偏强', range: '震荡', bear: '偏弱', crisis: '偏弱' },
  descriptions: { bull: '主要指数整体表现偏强。', range: '主要指数整体处于区间震荡。', bear: '主要指数整体表现偏弱。', crisis: '主要指数整体表现偏弱。' },
  eligible(environment, today = V7Display.today()) {
    if (!environment || Array.isArray(environment) || environment.status !== 'VERIFIED'
      || environment.stale === true || environment.error || environment.error_message
      || !Object.hasOwn(this.labels, environment.classification)
      || environment.rule_version !== 'existing_market_regime_v1') return null;
    const inputs = environment.inputs;
    if (!inputs || Array.isArray(inputs) || !['avg_pct', 'sh_pct', 'sz_pct', 'cyb_pct', 'kcb_pct']
      .every(key => typeof inputs[key] === 'number' && Number.isFinite(inputs[key]))) return null;
    const observed = environment.observed_at;
    if (typeof observed !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00$/.test(observed)
      || !Number.isFinite(Date.parse(observed)) || Date.parse(observed) > Date.now()
      || observed.slice(0, 10).replaceAll('-', '') !== today) return null;
    const parsedDay = new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' })
      .format(new Date(observed)).replaceAll('-', '');
    if (parsedDay !== today) return null;
    return { label: this.labels[environment.classification], description: this.descriptions[environment.classification], inputs, time: observed.slice(11, 16) };
  }
};

/* Phase 3: opinion display only. No signal, plan or permission is changed. */
const V7PicksPresentation = {
  roles: [['P-LIAO-XIAOAI', '廖小爱'], ['P-COOL', '酷儿'], ['P-YINWENTAO', '殷文涛'], ['P-HANSHU', '韩叔'], ['P-RULE-QUANT', '规则量化派']],
  actions: { BUY: '考虑买入', NO_SIGNAL: '继续观察' },
  action(value) { return Object.hasOwn(this.actions, value) ? this.actions[value] : null; },
  finite(value) { return typeof value === 'number' && Number.isFinite(value); },
  text(value) { return typeof value === 'string' && value.trim() && !/^(UNKNOWN|UNAVAILABLE|EMPTY|DEGRADED)$/.test(value.trim()); },
  code(value) { return typeof value === 'string' ? value.replace(/^(sh|sz|bj)\.?/i, '').replace(/\.(sh|sz|bj)$/i, '') : null; },
  day(timestamp) {
    if (typeof timestamp !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(timestamp) || !Number.isFinite(Date.parse(timestamp))) return null;
    const [year, month, day] = timestamp.slice(0, 10).split('-').map(Number);
    if (new Date(Date.UTC(year, month - 1, day)).toISOString().slice(0, 10) !== timestamp.slice(0, 10)
      || Number(timestamp.slice(11, 13)) > 23) return null;
    return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date(timestamp)).replaceAll('-', '');
  },
  identity(role) {
    if (!role || typeof role.symbol !== 'string' || !/^\d{6}$/.test(role.symbol) || !this.text(role.stock_name)) return false;
    const evidence = role.candidate_evidence || {};
    return role.scan_evidence?.identity_status !== 'MISMATCH' && ['symbol', 'code'].every(key => evidence[key] == null || this.code(evidence[key]) === role.symbol);
  },
  current(data, role, today = V7Display.today(), now = Date.now()) {
    return data?.scan_date_status === 'CURRENT_TRADING_DATE' && data?.trade_date?.replaceAll?.('-', '') === today
      && this.day(data.as_of) === today && Date.parse(data.as_of) <= now
      && !['STALE', 'INVALID', 'UNAVAILABLE', 'EMPTY'].includes(data.data_status)
      && this.roles.some(([id]) => id === role?.participant_id) && this.identity(role)
      && role.scan_date_qualification === 'CURRENT_TRADING_DATE' && this.day(role.scan_timestamp) === today
      && Date.parse(role.scan_timestamp) <= now && this.action(role.action) !== null && role.execution_authorized === false
      && role.scan_evidence?.source_record?.stale !== true && !role.scan_evidence?.source_record?.error && !role.scan_evidence?.source_record?.error_message
      && ((typeof role.decision_reason === 'string' && role.decision_reason.trim())
        || Object.keys(role.candidate_evidence || {}).some(key => this.finite(role.candidate_evidence[key])));
  },
  firstReason(reason) {
    if (!this.text(reason)) return '';
    const boundary = reason.search(/[。；;]/);
    return boundary < 0 ? reason : reason.slice(0, boundary + 1);
  },
  risk(role) {
    if (Array.isArray(role.skill_rules_failed) && role.skill_rules_failed.length) return '尚缺确认条件';
    return this.finite(role.exit_plan?.stop_loss) ? `策略止损参考 ${(role.exit_plan.stop_loss * 100).toFixed(1)}%` : null;
  },
  horizon(role) { return this.text(role.holding_horizon) ? role.holding_horizon : null; },
  scanFacts(role) {
    const evidence = role.candidate_evidence || {};
    return [['记录价格', 'price', ''], ['涨跌幅', 'change_pct', '%'], ['PE', 'pe', ''], ['记录成交额（亿元）', 'amount_yi', ''], ['换手率', 'turnover_ratio', '%'], ['记录市值（亿元）', 'market_cap_yi', '']]
      .filter(([, key]) => this.finite(evidence[key])).map(([label, key, unit]) => [label, `${evidence[key]}${unit}`]);
  }
};

/* Phase 4 Option A: combine persisted evidence; never calculate a stock score. */
const V7StockEvidence = {
  validCode(value) { return typeof value === 'string' && /^\d{6}$/.test(value.trim()); },
  currentFive(envelope, today = V7Display.today(), now = Date.now()) {
    const data = envelope?.data;
    if (!data || ['STALE', 'ERROR', 'INVALID', 'UNAVAILABLE', 'EMPTY'].includes(envelope.status)
      || data.scan_date_status !== 'CURRENT_TRADING_DATE' || data.trade_date?.replaceAll?.('-', '') !== today
      || V7PicksPresentation.day(data.as_of) !== today || Date.parse(data.as_of) > now
      || ['STALE', 'ERROR', 'INVALID', 'UNAVAILABLE', 'EMPTY'].includes(data.data_status)
      || data.execution_authorized !== false || !Array.isArray(data.participants)) return null;
    const roles = [];
    for (const [id] of V7PicksPresentation.roles) {
      const rows = data.participants.filter(row => row?.participant_id === id);
      if (rows.length !== 1) return null;
      const role = rows[0];
      if (!V7PicksPresentation.action(role.action) || role.execution_authorized !== false
        || role.scan_date_qualification !== 'CURRENT_TRADING_DATE'
        || V7PicksPresentation.day(role.scan_timestamp) !== today || Date.parse(role.scan_timestamp) > now
        || role.scan_evidence?.identity_status === 'MISMATCH' || role.scan_evidence?.source_record?.stale === true
        || role.scan_evidence?.source_record?.error || role.scan_evidence?.source_record?.error_message
        || (role.symbol != null && !V7PicksPresentation.identity(role))) return null;
      roles.push(role);
    }
    return roles;
  },
  personaContext(stock, picks, code, today = V7Display.today(), now = Date.now()) {
    const roles = this.currentFive(picks, today, now);
    if (!roles || !Array.isArray(stock?.persona_views)) return { complete: false, matches: [] };
    const matches = roles.filter(role => role.symbol === code);
    // Two GETs may straddle a natural scan. Conflicting projections cannot support a conclusion.
    const projected = stock.persona_views.filter(role => role?.symbol === code && V7PicksPresentation.action(role.action));
    if (matches.length !== projected.length || matches.some(role => !projected.some(view =>
      view.participant_id === role.participant_id && view.action === role.action && view.execution_authorized === false
      && view.decision_id === role.decision_id && view.scan_timestamp === role.scan_timestamp))) return { complete: false, matches: [] };
    return { complete: true, matches };
  },
  currentTop5(picks, today = V7Display.today()) {
    const top = picks?.data?.ai_quant_top5;
    if (!top || ['STALE', 'ERROR', 'INVALID', 'UNAVAILABLE', 'EMPTY'].includes(picks.status) || top.status !== 'NORMAL' || top.classification !== 'CURRENT_DAY_TOP5'
      || top.pulse?.trade_date?.replaceAll?.('-', '') !== today || top.pulse?.row_count !== 5 || !Array.isArray(top.predictions) || top.predictions.length !== 5) return null;
    const rows = top.predictions;
    if (rows.some(row => !row || !this.validCode(row.symbol) || row.symbol !== row.symbol.trim()
      || row.trade_date?.replaceAll?.('-', '') !== today || !Number.isInteger(row.rank) || row.rank < 1 || row.rank > 5)
      || new Set(rows.map(row => row.symbol)).size !== 5 || new Set(rows.map(row => row.rank)).size !== 5) return null;
    return rows;
  },
  quote(quote) {
    const value = quote?.value_fields || {};
    if (quote?.source_type === 'LEGACY_PRODUCTION_SCANNER') {
      return { title: '策略扫描记录', note: '为策略扫描保存值，不代表实时行情。', facts: V7PicksPresentation.scanFacts({ candidate_evidence: value }) };
    }
    if (quote?.source_type === 'LOCAL_SQLITE_RESEARCH_BAR' && quote.status === 'STALE') {
      const facts = [];
      if (typeof quote.trade_date === 'string' && /^\d{8}$/.test(quote.trade_date)) facts.push(['交易日期', quote.trade_date.replace(/^(\d{4})(\d{2})(\d{2})$/, '$1-$2-$3')]);
      if (V7PicksPresentation.finite(value.close)) facts.push(['收盘价', String(value.close)]);
      if (V7PicksPresentation.finite(value.pct_chg)) facts.push(['涨跌幅', String(value.pct_chg) + '%']);
      return { title: '最近历史行情', note: '历史交易日保存值，不代表实时行情。', facts };
    }
    return { title: '行情证据', note: '当前没有可确认的行情证据。', facts: [] };
  },
  sentinel(stock, picks, code, complete) {
    const risk = picks?.data?.risk_sentinel;
    return complete && risk?.symbol === code && risk.alert_triggered === true
      && risk.trading_enabled === false && risk.ranking_enabled === false
      && V7PicksPresentation.day(risk.decision_at) === V7Display.today() && Date.parse(risk.decision_at) <= Date.now()
      && stock?.risk_sentinel?.alert_triggered === true && V7PicksPresentation.text(risk.reason) ? risk : null;
  },
  technical(stockEnvelope, picksEnvelope, context) {
    const stock = stockEnvelope.data || {};
    return { stock_endpoint_status: stockEnvelope.status, identity: stock.identity, quote_evidence: stock.quote_evidence,
      persona_views: stock.persona_views, ai_quant: stock.ai_quant, risk_sentinel: stock.risk_sentinel,
      news: { status: stock.news?.status, items: stock.news?.items }, execution_authorized: stock.execution_authorized,
      picks: { endpoint_status: picksEnvelope?.status, scan_date_status: picksEnvelope?.data?.scan_date_status,
        as_of: picksEnvelope?.data?.as_of, execution_authorized: picksEnvelope?.data?.execution_authorized,
        participants: picksEnvelope?.data?.participants, ai_quant_top5: picksEnvelope?.data?.ai_quant_top5,
        risk_sentinel: picksEnvelope?.data?.risk_sentinel },
      current_five_persona_scan_complete: context.complete };
  }
};


// Phase6: display gates only; no inference, execution or performance calculation.
const V7AiQuantPresentation = {
  currentTop5(data, today = V7Display.today()) {
    const rows = data?.recent_predictions;
    const pulse = data?.prediction_pulse;
    if (data?.prediction_classification !== 'CURRENT_DAY_TOP5' || data?.prediction_status !== 'NORMAL' ||
        pulse?.trade_date !== today || !Array.isArray(rows) || rows.length !== 5) return false;
    return rows.every(row => /^\d{6}$/.test(row.symbol || '') && row.trade_date === today &&
      Number.isInteger(row.rank) && row.rank >= 1 && row.rank <= 5 &&
      row.predict_time === pulse.predict_time && row.model_version === pulse.model_version) &&
      new Set(rows.map(row => row.rank)).size === 5 && new Set(rows.map(row => row.symbol)).size === 5;
  },
  latestTelemetry(data) {
    const rows = Array.isArray(data?.recent_telemetry) ? data.recent_telemetry : [];
    return rows.filter(row => Number.isFinite(Date.parse(String(row.timestamp || '').replace(' ', 'T') +
      (/Z$|[+-]\d\d:\d\d$/.test(row.timestamp || '') ? '' : '+08:00'))))
      .reduce((latest, row) => !latest || row.timestamp > latest.timestamp ? row : latest, null);
  },
  inputError(data) { return this.latestTelemetry(data)?.error_message === 'AIQ_INPUT_STALE_COMPLETED_DATE'; },
  forward(data) {
    const genuine = data?.is_genuine_forward === true && data?.genuine_forward_started === true &&
      data?.forward_enabled === true && data?.observation_status === 'ACTIVE' &&
      (data?.epoch_type === undefined || data.epoch_type === 'GENUINE_FORWARD') &&
      typeof data?.forward_epoch_id === 'string' && Boolean(data.forward_epoch_id);
    const day = genuine && Number.isInteger(data?.forward_day_count) && data.forward_day_count >= 0 ? data.forward_day_count : null;
    return { genuine, day, latest: this.latestTelemetry(data) };
  },
  finite(value) { return typeof value === 'number' && Number.isFinite(value); },
  number(value) { return this.finite(value) ? value.toLocaleString('zh-CN', {maximumFractionDigits: 2}) : '—'; },
  money(value) { return this.finite(value) ? '¥' + value.toLocaleString('zh-CN', {minimumFractionDigits: 2, maximumFractionDigits: 2}) : '—'; },
  pct(value) { return this.finite(value) ? (value * 100).toFixed(2) + '%' : '—'; }
};

/* Stage2 display validation. Never infer quote dates from a capture or another series. */
const V7DashboardData = {
  indexes: [['sh000001', '000001', '上证指数', 'sh_pct'], ['sz399001', '399001', '深证成指', 'sz_pct'], ['sz399006', '399006', '创业板指', 'cyb_pct']],
  finite(value) { return typeof value === 'number' && Number.isFinite(value); },
  day(value) {
    if (typeof value !== 'string' || !/^(\d{8}|\d{4}-\d{2}-\d{2})$/.test(value)) return null;
    const day = value.replaceAll('-', '');
    const iso = `${day.slice(0, 4)}-${day.slice(4, 6)}-${day.slice(6, 8)}`;
    const date = new Date(iso + 'T00:00:00+08:00');
    return Number.isFinite(date.getTime()) && V7PicksPresentation.day(iso + 'T00:00:00+08:00') === day ? day : null;
  },
  timestamp(value, today, now) {
    return V7PicksPresentation.day(value) === today && Date.parse(value) <= now;
  },
  rejected(value) {
    return !value || typeof value !== 'object' || Array.isArray(value) || ('stale' in value && value.stale !== false) || value.error || value.error_message
      || !['UNKNOWN', 'VALID', 'NORMAL', 'AVAILABLE'].includes(value.status);
  },
  snapshot(row, today = V7Display.today(), now = Date.now()) {
    const saved = row?.snapshot;
    if (this.rejected(saved) || !this.finite(saved.last_price) || saved.last_price <= 0) return null;
    const date = this.day(saved.market_date);
    const observedDay = V7PicksPresentation.day(saved.market_observed_at);
    if (saved.market_date != null && date !== today) return null;
    if (saved.market_observed_at != null && !this.timestamp(saved.market_observed_at, today, now)) return null;
    if (date !== today && observedDay !== today) return null;
    if (!this.timestamp(saved.capture_started_at, today, now)) return null;
    return { value: saved.last_price, capture: saved.capture_started_at, marketDate: date || observedDay };
  },
  series(row, today = V7Display.today(), now = Date.now()) {
    const saved = row?.intraday;
    if (this.rejected(saved) || this.day(saved.market_date) !== today || typeof saved.source !== 'string' || !saved.source.trim()
      || !Array.isArray(saved.points) || !saved.points.length || saved.points.length > 390) return null;
    const latestAllowed = Math.floor(now / 60000) * 60000 + 59999;
    const segments = []; let prior = -1; let current = null; let session = null;
    for (const point of saved.points) {
      if (!point || !this.finite(point.value) || point.value <= 0 || typeof point.time !== 'string' || !/^\d{4}$/.test(point.time)
        || typeof point.bar_at !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:00\+08:00$/.test(point.bar_at)
        || !this.timestamp(point.bar_at, today, latestAllowed) || point.bar_at.slice(11, 16).replace(':', '') !== point.time) return null;
      const minute = Number(point.time.slice(0, 2)) * 60 + Number(point.time.slice(2));
      const nextSession = minute >= 570 && minute <= 690 ? 'morning' : (minute >= 780 && minute <= 900 ? 'afternoon' : null);
      if (!nextSession || minute <= prior) return null;
      if (!current || nextSession !== session || minute - prior > 1) { current = []; segments.push(current); }
      current.push({ minute, value: point.value, bar_at: point.bar_at }); prior = minute; session = nextSession;
    }
    const last = saved.points.at(-1);
    if (saved.market_time !== last.bar_at || !this.timestamp(saved.market_time, today, latestAllowed)) return null;
    const values = saved.points.map(point => point.value);
    return { date: saved.market_date, last: last.bar_at, lastValue: last.value, count: values.length, min: Math.min(...values), max: Math.max(...values), segments };
  },
  rows(data) {
    const records = Array.isArray(data?.indexes) ? data.indexes : [];
    return this.indexes.map(([symbol, code, name, input]) => {
      const matches = records.filter(row => row?.symbol === symbol);
      const row = matches.length === 1 && matches[0].code === code ? matches[0] : null;
      return { symbol, name, input, row, snapshot: this.snapshot(row), series: this.series(row) };
    });
  },
  commonScale(rows) {
    const eligible = rows.filter(item => item.series?.segments[0]?.[0]?.minute === 570);
    if (!eligible.length) return null;
    const values = eligible.flatMap(item => {
      const first = item.series.segments[0][0].value;
      return item.series.segments.flat().map(point => (point.value / first - 1) * 100);
    });
    return { min: Math.min(...values), max: Math.max(...values), symbols: eligible.map(item => item.symbol), mode: 'RELATIVE_0930_COMMON_SCALE' };
  },
  coordinates(series, width, height, scale = null) {
    const inset = 6, gap = 10, sessionWidth = (width - inset * 2 - gap) / 2;
    const first = series.segments[0][0].value;
    const min = scale ? scale.min : series.min, max = scale ? scale.max : series.max;
    return series.segments.map(segment => segment.map(point => [
      point.minute <= 690 ? inset + (point.minute - 570) / 120 * sessionWidth
        : inset + sessionWidth + gap + (point.minute - 780) / 120 * sessionWidth,
      max === min ? height / 2 : height - inset - ((scale ? (point.value / first - 1) * 100 : point.value) - min) / (max - min) * (height - inset * 2)
    ]));
  },
  news(envelope) {
    if (!['OK', 'DEGRADED', 'NORMAL', 'VALID'].includes(envelope?.status) || !Array.isArray(envelope?.data?.all_news)) return [];
    const seen = new Set();
    return envelope.data.all_news.filter(row => {
      if (!V7PicksPresentation.text(row?.title) || /(?:\.\.\.|…)\s*$/.test(row.title) || !V7PicksPresentation.text(row.source)
        || !V7PicksPresentation.text(row.source_record) || !V7PicksPresentation.day(row.published_at)
        || Date.parse(row.published_at) > Date.now() || row.stale === true || row.error) return false;
      try { const url = new URL(row.url); if (!['http:', 'https:'].includes(url.protocol) || !url.hostname || url.username || url.password || seen.has(url.href)) return false; seen.add(url.href); return true; } catch (_) { return false; }
    }).slice(0, 2);
  },
  opinions(envelope, today = V7Display.today(), now = Date.now()) {
    if (!['OK', 'DEGRADED', 'NORMAL', 'VALID'].includes(envelope?.status)
      || !['OK', 'DEGRADED', 'NORMAL', 'VALID'].includes(envelope?.data?.data_status)) return null;
    const roles = V7StockEvidence.currentFive(envelope, today, now);
    if (!roles || roles.some(role => role.scan_evidence?.status !== 'RECORDED_CURRENT_SCAN'
      || (role.symbol != null && role.scan_evidence?.identity_status !== 'MATCH')
      || !role.scan_evidence?.source_record || typeof role.scan_evidence.source_record !== 'object' || Array.isArray(role.scan_evidence.source_record)
      || Date.parse(role.scan_timestamp) !== Date.parse(envelope.data.as_of)
      || !(V7PicksPresentation.text(role.decision_reason) || Object.values(role.candidate_evidence || {}).some(value => this.finite(value))))) return null;
    const buy = roles.filter(role => role.action === 'BUY').length;
    const observe = roles.filter(role => role.action === 'NO_SIGNAL').length;
    if (buy + observe !== 5) return null;
    return { roles, buy, observe, asOf: envelope.data.as_of };
  }
};

const V7App = {
  overviewEvidence: {},
  pollIntervalMs: 30000,
  pollTimer: null,
  personalStorageKey: window.V7PersonalPortfolioStore?.STORAGE_KEY || 'v7_personal_portfolio_v1',
  personalItems: null,
  personalSource: 'V7_LOCAL',
  personalBackupUrl: null,
  researchReportCache: {},
  reportCategoryFilter: '全部',

  init() {
    this.initPickDrawer();
    document.querySelectorAll('[data-page]').forEach((item) => {
      item.addEventListener('click', () => this.switchPage(item.dataset.page));
    });
    const menu = document.getElementById('mobile-menu');
    const sidebar = document.getElementById('app-sidebar');
    if (menu && sidebar) menu.addEventListener('click', () => sidebar.classList.toggle('open'));
    document.getElementById('overview-details-link')?.addEventListener('click', () => {
      const details = document.getElementById('overview-system-details');
      if (details) { details.open = true; details.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
    });
    document.getElementById('overview-technical-source')?.addEventListener('change', () => this.renderOverviewTechnicalEvidence());
    this.loadDisplaySettings();
    const configuredHome = document.getElementById('setting-home')?.value;
    const initialPage = window.location.hash.slice(1) || configuredHome || 'page-overview';
    if (initialPage && document.getElementById(initialPage)) this.switchPage(initialPage, false);
    this.startPolling();
    this.refreshOverview();
    this.refreshMarket();
    this.refreshNews();
    this.refreshPicks();
    const lookup = document.getElementById('stock-lookup-form');
    if (lookup) lookup.addEventListener('submit', (event) => {
      event.preventDefault();
      this.loadStock(document.getElementById('stock-code-input')?.value || '');
    });
    const portfolioForm = document.getElementById('personal-holding-form');
    if (portfolioForm) portfolioForm.addEventListener('submit', (event) => this.savePersonalHolding(event));
    document.getElementById('personal-cancel-edit')?.addEventListener('click', () => this.resetPersonalForm());
    document.getElementById('personal-export')?.addEventListener('click', () => this.exportPersonalPortfolio());
    document.getElementById('personal-import-file')?.addEventListener('change', (event) => this.importPersonalPortfolio(event));
    document.getElementById('personal-holdings')?.addEventListener('click', (event) => {
      const button = event.target.closest('button[data-personal-action]');
      if (!button || !Array.isArray(this.personalItems)) return;
      const index = Number(button.dataset.index);
      if (!Number.isInteger(index) || !this.personalItems[index]) return;
      if (button.dataset.personalAction === 'edit') this.editPersonalHolding(index);
      if (button.dataset.personalAction === 'delete') this.deletePersonalHolding(index);
    });
    this.loadPersonalPortfolio();
    this.refreshArena();
    this.refreshHomeAccounts();
    this.refreshAiQuant();
    this.refreshForward();
    this.refreshPlans();
    this.refreshDataCenter();
    this.refreshDataSources();
    this.refreshIntegrations();
    document.getElementById('display-settings')?.addEventListener('submit', (event) => this.saveDisplaySettings(event));
  },

  switchPage(pageId, updateHash = true) {
    if (!document.getElementById(pageId)) return;
    document.querySelectorAll('.nav-item').forEach((item) => {
      item.classList.toggle('active', item.dataset.page === pageId);
    });
    document.querySelectorAll('.page-view').forEach((page) => {
      page.classList.toggle('active', page.id === pageId);
    });
    if (['page-data', 'page-reports'].includes(pageId)) {
      const advanced = document.getElementById('nav-advanced'); if (advanced) advanced.open = true;
    }
    document.getElementById('app-sidebar')?.classList.remove('open');
    if (updateHash && window.location.hash !== `#${pageId}`) window.location.hash = pageId;
    // Refresh on demand instead of polling every research/config endpoint.
    const refreshByPage = {
      'page-arena': () => this.refreshArena(),
      'page-ai-quant': () => { this.refreshAiQuant(); this.refreshPlans(); },
      'page-forward': () => this.refreshForward(),
      'page-plans': () => this.refreshPlans(),
      'page-backtest': () => this.refreshResearch(),
      'page-data': () => this.refreshDataCenter(),
      'page-sources': () => this.refreshDataSources(),
      'page-llm': () => this.refreshIntegrations(),
      'page-wecom': () => this.refreshIntegrations(),
      'page-reports': () => this.refreshReports(),
      'page-health': () => this.refreshOverview(),
    };
    refreshByPage[pageId]?.();
  },

  startPolling() {
    if (this.pollTimer) window.clearInterval(this.pollTimer);
    this.pollTimer = window.setInterval(() => {
      this.refreshOverview();
      this.refreshMarket();
      this.refreshNews();
      this.refreshPicks();
      this.refreshAiQuant();
      this.refreshForward();
      this.refreshHomeAccounts();
    }, this.pollIntervalMs);
  },

  async refreshOverview() {
    const response = await V7Api.getOverview();
    this.recordOverviewEvidence('overview', response);
    if (!response || !response.data || !['OK', 'DEGRADED'].includes(response.status)) {
      this.renderUnavailable();
      return;
    }
    this.renderOverview(response.data, response);
  },

  async refreshMarket() {
    try {
      const response = await V7Api.getMarket();
      if (!response || !response.data) throw new Error('市场 View Model 不可用');
      this.renderMarket(response);
    } catch (_) {
      this.renderMarketUnavailable();
    }
  },

  async refreshNews() {
    try {
      const response = await V7Api.getNews();
      if (!response || !response.data) throw new Error('新闻 View Model 不可用');
      this.renderNews(response);
    } catch (_) {
      this.renderNewsUnavailable();
    }
  },

  async refreshPicks() {
    const response = await V7Api.getPicks();
    if (!response || !response.data) {
      this.renderPicksUnavailable();
      return;
    }
    this.renderPicks(response);
  },

  async loadStock(symbol) {
    const request = this.stockQuerySequence = (this.stockQuerySequence || 0) + 1;
    const code = String(symbol ?? '').trim();
    const root = document.getElementById('stock-detail');
    const warning = document.getElementById('stock-detail-warning');
    root?.replaceChildren();
    root?.setAttribute('aria-busy', 'false');
    if (warning) warning.hidden = false;
    if (!V7StockEvidence.validCode(code)) {
      this.setPanel('stock-detail-warning', 'error', '请输入6位股票代码。', '');
      return;
    }
    this.switchPage('page-scoring');
    const input = document.getElementById('stock-code-input');
    if (input) input.value = code;
    this.setPanel('stock-detail-warning', 'unavailable', '正在读取个股证据…', '');
    root?.setAttribute('aria-busy', 'true');
    const [stockResult, picksResult] = await Promise.allSettled([V7Api.getStock(code), V7Api.getPicks()]);
    if (request !== this.stockQuerySequence) return;
    root?.setAttribute('aria-busy', 'false');
    const stock = stockResult.status === 'fulfilled' ? stockResult.value : null;
    const picks = picksResult.status === 'fulfilled' ? picksResult.value : null;
    if (!stock?.data || ['ERROR', 'INVALID', 'UNAVAILABLE'].includes(stock.status) || stock.data.identity?.symbol !== code) {
      this.setPanel('stock-detail-warning', 'error', '个股证据暂时读取失败。', '');
      return;
    }
    if (warning) warning.hidden = true;
    this.renderStock(stock, picks, code);
  },

  setText(id, value) {
    const node = document.getElementById(id);
    if (node) node.textContent = value === null || value === undefined || value === '' ? 'UNKNOWN' : String(value);
  },

  setPanel(id, state, titleText, detailText) {
    const node = document.getElementById(id);
    if (!node) return;
    const allowed = ['normal', 'empty', 'stale', 'error', 'hold', 'unavailable'];
    node.className = `state-panel ${allowed.includes(state) ? state : 'unavailable'}`;
    const title = node.querySelector('strong');
    const detail = node.querySelector('p');
    if (title) title.textContent = titleText;
    if (detail) detail.textContent = detailText;
  },

  translateStatus(value) {
    if (value === true) return '是';
    if (value === false) return '否';
    const status = typeof value === 'string' ? value.trim().toUpperCase() : '';
    const labels = {
      OK: '数据读取完成', VALID: '数据已核验', NORMAL: '状态正常',
      DEGRADED: '部分数据尚未完成验证', PARTIAL: '部分可用',
      UNAVAILABLE: '暂不可用', EMPTY: '暂无已保存数据', MISSING: '未提供',
      INVALID: '数据格式需要确认', UNKNOWN: '需要确认', UNVERIFIED: '需要确认',
      STALE: '数据时间较早，需确认', PASS: '已通过', OPEN: '尚未通过',
      PENDING: '等待完成', FAILED: '未通过', ERROR: '暂不可用',
      IMPLEMENTED: '能力已实现', NOT_SWITCHED: '尚未切换到当前生产路由',
      LEGACY_SCANNER_ONLY: '现有扫描链路使用', LEGACY_DIRECT_TENCENT: '现有扫描路由记录为直连腾讯',
      ISOLATED_PARTIAL_PARITY: '隔离比对尚未完全一致', EMPIRICAL_PARTIAL: '部分字段口径尚待核验',
      NOT_STARTED: '尚未开始', READ_ONLY_PULSE_ONLY: '仅观察已保存的模型记录',
      AVAILABLE: '可读取已保存内容', UNVERIFIED_PROVIDER_TIME: '行情发生时间尚未核验',
      UNVERIFIED_THRESHOLD: '数据时效阈值尚未确认', NOT_PERSISTED_PER_SOURCE: '未逐源保存状态'
    };
    return labels[status] || '当前生产状态需要进一步确认';
  },

  statusClass(status) {
    return ({ VALID: 'normal', OK: 'normal', PASS: 'normal', ACTIVE: 'normal', DEGRADED: 'hold', OPEN: 'hold', PENDING: 'hold', STALE: 'stale', EMPTY: 'empty', MISSING: 'empty', INVALID: 'error', FAILED: 'error', ERROR: 'error', UNAVAILABLE: 'unavailable', UNKNOWN: 'unavailable' })[status] || 'unavailable';
  },

  textNode(tag, value, className = '') {
    const node = document.createElement(tag);
    if (className) node.className = className;
    node.textContent = value === null || value === undefined || value === '' ? '—' : String(value);
    return node;
  },

  appendFactList(parent, facts) {
    const dl = document.createElement('dl'); dl.className = 'evidence-facts';
    for (const [label, value] of facts) {
      const row = document.createElement('div'); row.className = 'evidence-fact';
      row.append(this.textNode('dt', label), this.textNode('dd', value)); dl.append(row);
    }
    parent.append(dl);
  },

  renderPicks(envelope) {
    const data = envelope.data || {};
    const status = data.data_status || envelope.status || 'UNKNOWN';
    const rows = Array.isArray(data.participants) ? data.participants.filter(row => row && typeof row === 'object' && !Array.isArray(row)) : [];
    const roles = V7PicksPresentation.roles.map(([id, name]) => {
      const matches = rows.filter(row => row.participant_id === id);
      return { id, name, role: matches.length === 1 && V7PicksPresentation.current(data, matches[0]) ? matches[0] : null };
    });
    const hasCurrent = roles.some(item => item.role);
    this.setText('picks-page-status', hasCurrent ? '本次扫描观点' : '策略观点待确认');
    const scanLabel = document.getElementById('picks-as-of');
    if (scanLabel) scanLabel.textContent = V7PicksPresentation.day(data.as_of) ? `扫描记录：${data.as_of.slice(0, 16).replace('T', ' ')}` : '';
    const warning = document.getElementById('picks-warning');
    if (warning) warning.hidden = hasCurrent;
    this.setPanel('picks-warning', 'empty', '今日暂无可确认的策略观点。', '');
    const technical = document.getElementById('picks-technical-json');
    if (technical) technical.textContent = JSON.stringify(envelope, null, 2);
    const grid = document.getElementById('picks-role-grid');
    if (grid) {
      const aiqCard = document.getElementById('picks-aiq-card');
      grid.replaceChildren();
      for (const { id, name, role } of hasCurrent ? roles : []) {
        if (!role) {
          const empty = document.createElement('article'); empty.className = 'card pick-empty';
          empty.append(this.textNode('h2', name), this.textNode('p', '本轮暂无有效观点', 'muted')); grid.append(empty); continue;
        }
        const card = document.createElement('article'); card.className = 'card pick-card';
        card.tabIndex = 0; card.setAttribute('role', 'button'); card.dataset.pickParticipant = id;
        card.setAttribute('aria-haspopup', 'dialog'); card.setAttribute('aria-controls', 'pick-detail-drawer');
        card.setAttribute('aria-label', `${name} · ${role.stock_name} · ${V7PicksPresentation.action(role.action)}，查看详细分析`);
        const heading = document.createElement('div'); heading.className = 'card-heading';
        heading.append(this.textNode('h2', name)); card.append(heading);
        card.append(this.textNode('strong', role.stock_name, 'pick-stock'), this.textNode('span', role.symbol, 'pick-symbol'));
        card.append(this.textNode('p', V7PicksPresentation.action(role.action), `pick-opinion ${role.action === 'BUY' ? 'consider' : 'observe'}`));
        const reason = V7PicksPresentation.firstReason(role.decision_reason);
        if (reason) card.append(this.textNode('p', reason, 'pick-reason'));
        const horizon = V7PicksPresentation.horizon(role); const risk = V7PicksPresentation.risk(role);
        if (horizon) card.append(this.textNode('p', `计划周期：${horizon}`, 'pick-secondary'));
        if (risk) card.append(this.textNode('p', risk, 'pick-secondary'));
        card.append(this.textNode('span', '查看详细分析 →', 'text-link pick-detail-link'));
        card.addEventListener('click', () => this.openPickDrawer(role, card));
        card.addEventListener('keydown', event => {
          if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); this.openPickDrawer(role, card); }
        });
        grid.append(card);
      }
      if (aiqCard) grid.append(aiqCard);
    }
    const history = document.getElementById('picks-history'); const historyList = document.getElementById('picks-history-list');
    const historical = rows.filter(role => V7PicksPresentation.roles.some(([id]) => id === role.participant_id)
      && V7PicksPresentation.identity(role) && V7PicksPresentation.day(role.scan_timestamp)
      && Date.parse(role.scan_timestamp) < Date.now()
      && (data.scan_date_status === 'HISTORICAL' || data.data_status === 'STALE' || role.scan_date_qualification === 'HISTORICAL_OR_NON_TRADING_DATE'));
    if (history) history.hidden = historical.length === 0;
    if (historyList) historyList.replaceChildren(...historical.map(role => this.textNode('p', `${role.display_name} · ${role.stock_name} ${role.symbol} · ${role.scan_timestamp}${V7PicksPresentation.action(role.action) ? ' · 历史观点：' + V7PicksPresentation.action(role.action) : ''}`)));
    const sentinel = document.getElementById('picks-sentinel');
    const riskSection = document.getElementById('picks-risk-section');
    const item = data.risk_sentinel || {};
    const currentRisk = data.scan_date_status === 'CURRENT_TRADING_DATE' && V7PicksPresentation.day(data.as_of) === V7Display.today()
      && Date.parse(data.as_of) <= Date.now() && !['STALE', 'INVALID', 'UNAVAILABLE', 'EMPTY'].includes(status)
      && V7PicksPresentation.day(item.decision_at) === V7Display.today() && Date.parse(item.decision_at) <= Date.now()
      && item.alert_triggered === true && item.trading_enabled === false && item.ranking_enabled === false
      && typeof item.reason === 'string' && item.reason.trim();
    if (riskSection) riskSection.hidden = !currentRisk;
    if (sentinel) {
      sentinel.replaceChildren();
      if (currentRisk) {
        sentinel.append(this.textNode('p', '反指哨兵 · 当前发现高位风险信号'));
        const details = document.createElement('details'); details.append(this.textNode('summary', '查看观察原因'), this.textNode('p', item.reason)); sentinel.append(details);
      }
    }
    const aiq = data.ai_quant_top5 || {};
    this.setText('aiq-pulse-status', aiq.classification || aiq.status || 'UNKNOWN');
    const pulse = aiq.pulse || {};
    this.setText('aiq-pulse-meta', `日期：${pulse.trade_date || 'UNKNOWN'} · 预测时间：${pulse.predict_time || 'UNKNOWN'} · 模型：${pulse.model_version || 'UNKNOWN'} · 同一截面 ${pulse.row_count || 0} 条`);
    const list = document.getElementById('aiq-top5-list');
    if (list) {
      list.replaceChildren();
      if (aiq.classification === 'CURRENT_DAY_TOP5' && aiq.status === 'NORMAL') {
        for (const row of aiq.predictions || []) {
          const line = this.textNode('button', `#${row.rank} ${row.symbol || ''} ${row.name || ''} · score ${row.raw_score ?? '—'} · up ${row.up_probability ?? '—'}`, 'aiq-row');
          line.type = 'button'; if (row.symbol) line.addEventListener('click', () => this.loadStock(row.symbol)); list.append(line);
        }
      } else {
        list.append(this.textNode('p', aiq.classification === 'LATEST_HISTORICAL_PREDICTION'
          ? `LATEST_HISTORICAL_PREDICTION · ${pulse.trade_date || 'UNKNOWN'}；不是今日 Top5。`
          : 'NO_CURRENT_AI_QUANT_EVIDENCE', 'muted'));
      }
    }
    this.recordOverviewEvidence('picks', envelope);
    this.renderHomeOpportunities(envelope);
  },

  renderPicksUnavailable() {
    this.renderPicks({ status: 'UNAVAILABLE', data: {} });
    this.closePickDrawer();
    this.recordOverviewEvidence('picks', { status: 'UNAVAILABLE', data: null });
    this.setText('overview-picks-summary', '今日策略观点暂不可用');
  },

  initPickDrawer() {
    const drawer = document.getElementById('pick-detail-drawer');
    if (!drawer || this.pickDrawerInitialized) return;
    this.pickDrawerInitialized = true;
    document.getElementById('pick-drawer-close')?.addEventListener('click', () => this.closePickDrawer());
    drawer.addEventListener('cancel', event => { event.preventDefault(); this.closePickDrawer(); });
    drawer.addEventListener('keydown', event => {
      if (event.key === 'Escape') { event.preventDefault(); this.closePickDrawer(); }
    });
    drawer.addEventListener('close', () => {
      document.body.style.overflow = this.pickPreviousOverflow || '';
      const target = this.pickFocusReturn?.isConnected ? this.pickFocusReturn
        : document.querySelector(`[data-pick-participant="${this.pickFocusParticipant}"]`);
      target?.focus();
    });
  },

  openPickDrawer(role, card) {
    const drawer = document.getElementById('pick-detail-drawer'); const content = document.getElementById('pick-drawer-content');
    if (!drawer || !content || !V7PicksPresentation.action(role.action)) return;
    this.initPickDrawer();
    this.pickFocusReturn = card; this.pickFocusParticipant = role.participant_id;
    this.setText('pick-drawer-title', card.querySelector('h2')?.textContent || role.display_name);
    content.replaceChildren(this.textNode('h3', `${role.stock_name} · ${role.symbol}`, 'pick-drawer-stock'),
      this.textNode('p', V7PicksPresentation.action(role.action), 'pick-opinion'));
    const boundary = this.textNode('p', '这是策略观点，不代表订单或成交。', 'muted'); boundary.id = 'pick-drawer-boundary'; content.append(boundary);
    const section = (title, nodes) => {
      const block = document.createElement('section'); block.className = 'pick-drawer-section';
      block.append(this.textNode('h3', title), ...nodes); content.append(block); return block;
    };
    if (typeof role.decision_reason === 'string' && role.decision_reason.trim()) section('为什么关注', [this.textNode('p', role.decision_reason, 'pick-full-reason')]);
    const facts = V7PicksPresentation.scanFacts(role);
    if (facts.length) {
      const block = section('本次扫描记录', [this.textNode('p', '原样展示扫描保存值；行情发生时间与字段口径尚未独立核验，不作为执行行情。', 'muted')]);
      this.appendFactList(block, facts);
    }
    for (const [title, key] of [['已满足条件', 'skill_rules_triggered'], ['尚未满足条件', 'skill_rules_failed']]) {
      const rules = Array.isArray(role[key]) ? role[key].filter(value => typeof value === 'string' && value.trim()) : [];
      if (rules.length) { const list = document.createElement('ul'); list.append(...rules.map(value => this.textNode('li', value))); section(title, [list]); }
    }
    const plans = [
      ['仓位计划', role.position_plan, '这是策略计划，不是订单。', [['目标仓位', 'target_ratio', 'ratio'], ['单票仓位上限', 'max_position_ratio', 'ratio'], ['持仓数量上限', 'max_positions', 'number'], ['分批条件', 'scale_in_rule', 'text']]],
      ['退出计划', role.exit_plan, '这是策略计划，不是已挂单的止损止盈。', [['止损参考', 'stop_loss', 'ratio'], ['止盈参考', 'take_profit', 'ratio'], ['追踪参考', 'trailing', 'ratio'], ['时间退出条件', 'time_exit', 'text']]]
    ];
    for (const [title, plan, note, fields] of plans) {
      const values = fields.filter(([, key, type]) => type === 'text' ? typeof plan?.[key] === 'string' && plan[key].trim() : V7PicksPresentation.finite(plan?.[key]))
        .map(([label, key, type]) => [label, type === 'ratio' ? `${(plan[key] * 100).toFixed(1)}%` : plan[key]]);
      if (values.length) { const block = section(title, [this.textNode('p', note, 'muted')]); this.appendFactList(block, values); }
    }
    const horizon = V7PicksPresentation.horizon(role);
    if (horizon) section('计划周期', [this.textNode('p', horizon)]);
    const scan = role.scan_evidence;
    if (scan?.status === 'RECORDED_CURRENT_SCAN' && scan.source_name && scan.scan_at) {
      const block = section('数据与证据', [this.textNode('p', '仅为扫描来源记录，不代表行情发生时间已核验或行情已获执行准入。', 'muted')]);
      this.appendFactList(block, [['扫描来源', scan.source_name], ['扫描时间', scan.scan_at]]);
      if (Array.isArray(scan.evidence_refs) && scan.evidence_refs.length) { const refs = document.createElement('ul'); refs.className = 'pick-evidence-refs'; refs.append(...scan.evidence_refs.map(ref => this.textNode('li', typeof ref === 'string' ? ref : JSON.stringify(ref)))); block.append(refs); }
    }
    const technical = document.createElement('details'); technical.className = 'technical-details';
    technical.append(this.textNode('summary', '技术详情'), this.textNode('pre', JSON.stringify(role, null, 2), 'technical-json')); content.append(technical);
    if (!drawer.open) { this.pickPreviousOverflow = document.body.style.overflow; document.body.style.overflow = 'hidden'; drawer.showModal(); }
    document.getElementById('pick-drawer-close')?.focus();
  },

  closePickDrawer() {
    const drawer = document.getElementById('pick-detail-drawer'); if (drawer?.open) drawer.close();
  },

  async refreshArena() {
    const response = await V7Api.getArena();
    if (!response || !response.data) {
      this.setPanel('arena-warning', 'error', '竞技账户数据不可用', '未取得 active-season 只读账户快照。');
      return;
    }
    this.renderArena(response);
  },

  renderArena(envelope) {
    const data = envelope.data || {};
    this.setText('arena-status', `状态：${envelope.status || 'UNKNOWN'}`);
    this.setText('arena-season', `赛季：${data.season_id || 'UNKNOWN'}`);
    this.setPanel('arena-warning', this.statusClass(envelope.status),
      `收益账户快照：${data.players_count ?? 'UNKNOWN'} 个 · 主指标：${data.primary_metric || 'UNKNOWN'}`,
      data.comparability_warning || (envelope.warnings || []).join('；') || '来自 active-season 派生账户快照。');
    const grid = document.getElementById('arena-grid');
    if (!grid) return;
    grid.replaceChildren();
    for (const player of data.leaderboard || []) {
      const card = document.createElement('article'); card.className = 'card arena-card';
      card.append(this.textNode('h2', `${player.rank ?? '—'} · ${player.name || player.player_id || '收益账户'}`));
      card.append(this.textNode('p', `${player.player_id || 'UNKNOWN'} · ${player.status || 'UNKNOWN'} · ${player.account_type || 'UNKNOWN'}`, 'muted'));
      this.appendFactList(card, [['净值', this.moneyOrUnknown(player.total_equity)], ['净收益', this.moneyOrUnknown(player.net_pnl)],
        ['收益率', this.percentOrUnknown(player.net_pnl_pct)], ['持仓数', player.positions_count],
        ['最大回撤', this.percentOrUnknown(player.max_drawdown_pct)], ['初始本金', this.moneyOrUnknown(player.initial_capital)]]);
      grid.append(card);
    }
    this.setText('overview-arena-summary', `${data.players_count ?? 'UNKNOWN'} 个收益账户快照 · ¥100,000 是每账户初始本金合同，不是当前收益；Sentinel 排除。`);
    if (!grid.childElementCount) grid.append(this.textNode('p', '当前没有可审计的账户条目；不以空快照构造收益。', 'muted'));
  },

  moneyOrUnknown(value) { return typeof value === 'number' && Number.isFinite(value) ? `¥${this.formatValue(value, 2)}` : 'UNKNOWN'; },
  percentOrUnknown(value) { return typeof value === 'number' && Number.isFinite(value) ? `${this.formatValue(value, 2)}%` : 'UNKNOWN'; },

  async refreshAiQuant() {
    const [envelope, paper, forward] = await Promise.all([V7Api.getAiQuant(), V7Api.getPaper(), V7Api.getForward()]);
    this.recordOverviewEvidence('aiQuant', envelope);
    this.setText('overview-aiq-summary', V7AiQuantPresentation.currentTop5(envelope?.data) ? 'AI Quant 今日候选已生成' : 'AI Quant 今日候选暂不可用');
    this.renderAiQuantToday(envelope, paper, forward);
  },

  phase6Details(title, value) {
    const details = document.createElement('details'); details.className = 'technical-details aiq-evidence-details';
    details.append(this.textNode('summary', title), this.textNode('pre', JSON.stringify(value, null, 2), 'technical-json'));
    return details;
  },

  renderAiQuantToday(envelope, paper, forward) {
    const data = envelope?.data;
    const complete = V7AiQuantPresentation.currentTop5(data);
    this.setPanel('aiq-page-warning', complete ? 'normal' : 'hold', complete ? '今日 AI Quant Top5' : '今日AI策略暂不可确认。',
      complete ? '模型预测证据，不代表交易计划、交易指令、成交或持仓。' :
      (V7AiQuantPresentation.inputError(forward?.data) ? '今日 AI Quant 输入数据日期未满足要求，当前策略结果不可用。' : '当前保存的预测证据未满足今日完整结果要求。'));
    const root = document.getElementById('aiq-page-rows'); if (root) {
      root.replaceChildren();
      if (complete) for (const row of data.recent_predictions) {
        const card = document.createElement('article'); card.className = 'card aiq-prediction-card';
        card.append(this.textNode('p', '排名 ' + row.rank, 'card-kicker'), this.textNode('h2', row.name || row.symbol), this.textNode('p', row.symbol, 'muted'));
        const fields = [['上涨概率', row.up_probability, 'pct'], ['预期收益', row.expected_return, 'pct'],
          ['预测 MAE', row.predicted_mae, 'pct'], ['AI Quant右侧确认分', row.right_side_score, 'number'], ['预计持有天数', row.expected_holding_days, 'number']];
        this.appendFactList(card, fields.filter(([,value]) => V7AiQuantPresentation.finite(value))
          .map(([label,value,format]) => [label, V7AiQuantPresentation[format](value)]));
        card.append(this.textNode('p', '以上为已保存模型关键指标，不补写因果解释。', 'muted')); root.append(card);
      }
    }
    const model = document.getElementById('aiq-model-information');
    if (model) model.textContent = JSON.stringify({ status: envelope?.status, warnings: envelope?.warnings,
      data_freshness: envelope?.data_freshness, model_metadata: data?.model_metadata,
      feature_importance: data?.feature_importance_ranking, risk_contract: data?.risk_contract,
      prediction_classification: data?.prediction_classification, prediction_status: data?.prediction_status,
      prediction_pulse: data?.prediction_pulse, saved_predictions: data?.recent_predictions,
      latest_forward_telemetry: V7AiQuantPresentation.latestTelemetry(forward?.data) }, null, 2);
    this.renderAiQuantPaper(paper);
  },

  renderAiQuantPaper(envelope) {
    const root = document.getElementById('aiq-paper-summary'); if (!root) return;
    root.replaceChildren(); const data = envelope?.data;
    if (!data?.capital_summary) { root.append(this.textNode('p', '模拟账户账本暂不可读取。', 'muted')); return; }
    const card = document.createElement('article'); card.className = 'card aiq-paper-card';
    card.append(this.textNode('h2', '模拟账户实际状态'), this.textNode('p', '这是已保存账户账本，与今日预测分开；不作为 Genuine Forward Day0 绩效。', 'muted'));
    this.appendFactList(card, [['当前现金', V7AiQuantPresentation.money(data.capital_summary.cash)],
      ['当前总资产', V7AiQuantPresentation.money(data.capital_summary.total_equity)],
      ['持仓股票', Array.isArray(data.positions) ? data.positions.length + '只' : '持仓情况待确认'],
      ['账本保存时间', envelope.data_freshness?.source_timestamp || '时间待确认']]);
    const positions = document.createElement('div'); positions.className = 'aiq-saved-positions';
    for (const row of data.positions || []) positions.append(this.textNode('p', (row.name || row.symbol) + ' · ' + row.symbol + ' · ' + V7AiQuantPresentation.number(row.shares) + '股'));
    card.append(positions);
    const history = this.phase6Details('完整持仓、交易指令、成交与账户记录', envelope);
    card.append(history); root.append(card);
  },

  async refreshForward() {
    const envelope = await V7Api.getForward(); const data = envelope?.data || {};
    this.recordOverviewEvidence('forward', envelope); this.renderHomeForward(data);
    this.renderForward(data, data.season_id, envelope);
  },

  async refreshPlans() {
    const envelope = await V7Api.getTradePlans({ limit: 50 });
    this.renderPlanRecords(envelope, 'aiq-plan-records', 3);
    this.renderPlanRecords(envelope, 'plans-list');
    this.setText('plans-status', '已保存 ' + (envelope?.data?.total_records ?? '—') + ' 条记录');
    this.setPanel('plans-warning', 'hold', '计划记录 · 赛季归属暂未确认', '这是计划，不代表已经下单或成交；预测 ≠ 计划 ≠ 交易指令 ≠ 已成交 ≠ 持仓。');
  },

  renderPlanRecords(envelope, rootId, limit = null) {
    const root = document.getElementById(rootId); if (!root) return; root.replaceChildren();
    const records = Array.isArray(envelope?.data?.plans) ? envelope.data.plans : [];
    for (const plan of limit === null ? records : records.slice(0, limit)) {
      const card = document.createElement('article'); card.className = 'card aiq-plan-record';
      card.append(this.textNode('h3', (plan.name || plan.symbol || '计划记录') + ' · ' + (plan.symbol || '')),
        this.textNode('p', '赛季归属暂未确认 · 这是计划，不代表交易指令、成交或持仓。', 'muted'));
      this.appendFactList(card, [['记录时间', plan.signal_time || '时间待确认'],
        ...[['入场计划价',plan.entry_price,'money'],['计划仓位',plan.position_size,'pct'],
          ['止损参考价',plan.stop_loss,'money'],['止盈参考价',plan.take_profit,'money'],
          ['预计持有天数',plan.expected_holding_days,'number']].filter(([,value])=>V7AiQuantPresentation.finite(value))
          .map(([label,value,format])=>[label,V7AiQuantPresentation[format](value)])]);
      card.append(this.phase6Details('计划状态与技术证据', plan)); root.append(card);
    }
    if (!records.length) root.append(this.textNode('p', '暂无可读取的计划记录。', 'muted'));
  },

  async refreshDataCenter() {
    const envelope = await V7Api.getDataCenter(); const data = envelope?.data || {};
    this.setText('data-center-status', envelope?.status || 'UNKNOWN');
    this.setPanel('data-center-warning', this.statusClass(envelope?.status), `数据库概况：${data.database?.statistics_status || 'UNKNOWN'}`,
      data.database?.source_note || '只读元数据；不构成 G6 完整性证明。');
    const root = document.getElementById('data-center-grid'); if (!root) return;
    root.replaceChildren();
    for (const [title, value] of [['正式数据库', data.database?.path], ['文件大小', data.database?.size_bytes],
      ['主文件 mtime（非行情时间）', data.database?.main_file_mtime], ['表结构', JSON.stringify(data.database?.tables_present || {})],
      ['股票池范围', data.scope], ['G6', data.g6_status], ['P1C2', data.p1c2_parity], ['P1C3', data.p1c3]]) {
      const card = document.createElement('article'); card.className = 'card'; card.append(this.textNode('h2', title), this.textNode('p', value ?? 'UNKNOWN')); root.append(card);
    }
  },

  async refreshDataSources() {
    const envelope = await V7Api.getDataSources();
    const data = envelope?.data || {};
    const root = document.getElementById('sources-grid');
    this.setText('sources-page-status', this.translateStatus(envelope?.status));
    if (!root) return;
    root.replaceChildren();
    const sources = Array.isArray(data.sources) ? data.sources : [];
    const descriptionById = {
      hithink: '行情数据源适配能力与当前生产路由状态。',
      tencent: '行情数据来源与字段口径状态。',
      news: '已保存的新闻内容。',
      market_db: '行情与研究数据的只读存储状态。',
      scanner: '读取行情并形成候选结果的现有链路。',
      ai_quant: '模型已保存预测记录的观察状态。'
    };
    for (const source of sources) {
      const card = document.createElement('article'); card.className = 'card source-card';
      const name = source.name || '数据源';
      card.append(this.textNode('h2', name));
      card.append(this.textNode('p', descriptionById[source.id] || '数据源状态来自当前只读接口。', 'source-purpose'));
      let state;
      let note = '';
      if (typeof source.production_route === 'string') {
        state = this.translateStatus(source.production_route);
        if (source.adapter) note = `适配能力：${this.translateStatus(source.adapter)}。`;
        if (source.credential_rotation) note += `凭证状态：${this.translateStatus(source.credential_rotation)}。`;
        if (source.field_contract) note += `字段口径：${this.translateStatus(source.field_contract)}。`;
        if (source.p1c2) note += `数据一致性：${this.translateStatus(source.p1c2)}。`;
        if (source.p1c3) note += `迁移状态：${this.translateStatus(source.p1c3)}。`;
      } else if (source.id === 'ai_quant') {
        state = this.translateStatus(source.prediction_status);
        note = `推理触发：${this.translateStatus(source.inference_triggered)}；这里只读取已保存预测记录。`;
      } else if (source.id === 'news') {
        state = source.artifact === 'AVAILABLE' ? '可查看已保存新闻' : this.translateStatus(source.artifact);
        note = `本页读取已保存内容；采集触发：${this.translateStatus(source.collection_triggered)}。`;
      } else if (source.id === 'market_db') {
        state = source.metadata === 'AVAILABLE' && source.g6 === 'PASS'
          ? '元数据可读，完整性已通过接口确认'
          : (source.metadata === 'AVAILABLE' && source.g6 ? '数据可查询，完整性尚未确认' : this.translateStatus(source.metadata));
        note = `完整性状态：${this.translateStatus(source.g6)}；元数据可读不代表行情完整。`;
      } else {
        state = this.translateStatus(source.production_route || source.status || source.adapter || source.artifact || source.metadata);
        note = '当前生产状态需要进一步确认。';
      }
      const panel = document.createElement('div'); panel.className = `state-panel ${this.statusClass(source.production_route || source.prediction_status || source.artifact || source.metadata || 'UNKNOWN')}`;
      panel.append(this.textNode('strong', state), this.textNode('p', note || '当前生产状态需要进一步确认。'));
      card.append(panel);
      const details = document.createElement('details'); details.className = 'technical-details source-details';
      details.append(this.textNode('summary', '技术详情'), this.textNode('pre', JSON.stringify(source, null, 2), 'technical-json'));
      card.append(details);
      root.append(card);
    }
    if (!sources.length) {
      this.setPanel('sources-warning', 'unavailable', '数据源状态暂不可用', '接口未返回数据源清单；当前生产状态需要进一步确认。');
      root.append(this.textNode('p', '没有可显示的数据源记录。', 'muted'));
    } else {
      this.setPanel('sources-warning', this.statusClass(envelope?.status), '状态按当前接口动态展示', '状态说明不代表切换生产数据源；本页不会触发采集、推理或路由变更。');
    }
  },

  async refreshIntegrations() {
    const [llm, wecom] = await Promise.all([V7Api.getLlmStatus(), V7Api.getWecomStatus()]);
    const llmRoot = document.getElementById('llm-grid'); if (llmRoot) {
      llmRoot.replaceChildren();
      for (const [key, value] of Object.entries(llm?.data || {})) {
        const card = document.createElement('article'); card.className = 'card'; card.append(this.textNode('h2', key), this.textNode('p', value ?? 'UNKNOWN')); llmRoot.append(card);
      }
    }
    const wecomRoot = document.getElementById('wecom-grid'); if (wecomRoot) {
      wecomRoot.replaceChildren();
      for (const [key, value] of Object.entries(wecom?.data || {})) {
        const card = document.createElement('article'); card.className = 'card'; card.append(this.textNode('h2', key), this.textNode('p', typeof value === 'object' ? JSON.stringify(value) : (value ?? 'UNKNOWN'))); wecomRoot.append(card);
      }
    }
  },

  async getResearchReport(reportId) {
    if (Object.prototype.hasOwnProperty.call(this.researchReportCache, reportId)) return this.researchReportCache[reportId];
    const envelope = await V7Api.getResearch(reportId);
    const data = envelope?.data;
    const result = data?.report_id === reportId && typeof data.markdown_content === 'string'
      ? { envelope, data, content: data.markdown_content }
      : null;
    if (result) this.researchReportCache[reportId] = result;
    return result;
  },

  cleanMarkdownCell(value) {
    return String(value || '').replace(/\*\*/g, '').replace(/`/g, '').trim();
  },

  reportTableCell(markdown, rowLabel, columnIndex) {
    if (typeof markdown !== 'string') return null;
    for (const line of markdown.split(/\r?\n/)) {
      if (!line.includes('|')) continue;
      const cells = line.trim().split('|').slice(1, -1).map((cell) => this.cleanMarkdownCell(cell));
      if (cells[0] === rowLabel && typeof cells[columnIndex] === 'string') return cells[columnIndex];
    }
    return null;
  },

  parsePhase12Metrics(markdown) {
    const pct = (value) => typeof value === 'string' && /^[+-]?\d+(?:\.\d+)?%$/.test(value) ? value : null;
    const top5 = pct(this.reportTableCell(markdown, 'LightGBM Top 5 收益', 2));
    const alpha = pct(this.reportTableCell(markdown, 'LightGBM 5D 毛超额 Alpha', 2));
    const winRate = pct(this.reportTableCell(markdown, 'Top 5 样本胜率', 2));
    const extremes = this.reportTableCell(markdown, 'Top 5 极值表现', 2) || '';
    const maeMfe = extremes.match(/MAE:\s*([+-]?\d+(?:\.\d+)?%)\s*\/\s*MFE:\s*([+-]?\d+(?:\.\d+)?%)/);
    const netMatch = typeof markdown === 'string' ? markdown.match(/Next Open OOS Net \(10bps\)\s*=\s*([+-]?\d+(?:\.\d+)?%)/) : null;
    return {
      top5: top5 && { value: top5, label: '样本外 Top5 5日收益', semantic: 'OOS / Next Open / 5D 毛收益' },
      alpha: alpha && { value: alpha, label: '相对基准 Alpha', semantic: 'OOS / Next Open / 5D 毛超额' },
      net: pct(netMatch?.[1]) && { value: netMatch[1], label: '成本后收益', semantic: 'OOS / Next Open / 5D · 10bps 模拟成本' },
      winRate: winRate && { value: winRate, label: '样本胜率', semantic: 'OOS / Next Open / Top5 信号' },
      mae: pct(maeMfe?.[1]) && { value: maeMfe[1], label: '历史 MAE', semantic: 'OOS / Next Open / 5D 日K极值' },
      mfe: pct(maeMfe?.[2]) && { value: maeMfe[2], label: '历史 MFE', semantic: 'OOS / Next Open / 5D 日K极值' }
    };
  },

  extractReportPercent(markdown, pattern) {
    if (typeof markdown !== 'string') return null;
    const match = markdown.match(pattern);
    return match && /^[+-]?\d+(?:\.\d+)?%$/.test(match[1]) ? match[1] : null;
  },

  renderResearchMetricGrid(rootId, metrics, keys) {
    const root = document.getElementById(rootId); if (!root) return;
    root.replaceChildren();
    const labels = { top5: '样本外 Top5 5日收益', alpha: '相对基准 Alpha', net: '成本后收益', winRate: '样本胜率', mae: '历史 MAE', mfe: '历史 MFE', accountReturn: '账户模拟收益', maxDrawdown: '最大动态回撤', closedTrades: '已平仓交易' };
    for (const key of keys) {
      const metric = metrics?.[key];
      const card = document.createElement('article'); card.className = 'card research-metric-card';
      card.append(this.textNode('p', metric?.label || labels[key], 'card-kicker'));
      card.append(this.textNode('strong', metric?.value ?? '暂未解析', 'research-metric-value'));
      card.append(this.textNode('p', metric?.semantic || '请查看完整报告确认此项。', 'research-metric-note'));
      root.append(card);
    }
  },

  phase6HistoryMetrics(research, audit, interim, integrity) {
    const pct = value => typeof value === 'string' && /^[+-]?\d+(?:\.\d+)?%$/.test(value) ? value : null;
    const match = (text, regex) => typeof text === 'string' ? text.match(regex)?.[1] ?? null : null;
    const cell = (text, label, column) => this.reportTableCell(text, label, column);
    const extremes = cell(research, 'Top 5 极值表现', 2)?.match(/MAE:\s*([+-]?\d+(?:\.\d+)?%)\s*\/\s*MFE:\s*([+-]?\d+(?:\.\d+)?%)/);
    const oos = [
      ['Top5 5日平均收益', pct(cell(research,'LightGBM Top 5 收益',2))],
      ['同期候选基准', pct(cell(research,'OOS 全市场基准',2))],
      ['毛超额收益', pct(cell(research,'LightGBM 5D 毛超额 Alpha',2))],
      ['10bps模拟净收益', pct(match(research,/Next Open OOS Net \(10bps\)\s*=\s*([+-]?\d+(?:\.\d+)?%)/))],
      ['信号胜率', pct(cell(research,'Top 5 样本胜率',2))], ['日K MAE',pct(extremes?.[1])],['日K MFE',pct(extremes?.[2])],
      ['研究交易日', match(cell(audit,'OOS 实际交易日天数',1),/(\d+) 天/)],
      ['Top5信号数', match(cell(audit,'OOS 实际入榜信号数',1),/(\d+) 笔/)]];
    const historicalReplay = typeof integrity === 'string' && /FORWARD_DATA_GENUINE = FALSE/.test(integrity) && /HISTORICAL_PAPER_REPLAY/.test(integrity);
    const replay = historicalReplay ? [
      ['历史模拟净收益率', pct(match(integrity,/总账净盈亏[^\n]*?\(\x60?([+-]?\d+(?:\.\d+)?%)\x60?\)/))],
      ['最大动态回撤', pct(match(interim,/全周期最大动态回撤为\s*\x60?(\d+(?:\.\d+)?%)/))],
      ['已平仓交易', match(cell(integrity,'样本总量 N',1),/(\d+) 笔/)],
      ['已平仓胜率',pct(match(integrity,/中位数 \+1\.11%，胜率\s*(\d+(?:\.\d+)?%)/))],
      ['历史模拟交易日', match(integrity,/该\s*(\d+)\s*交易日测试成果/)]] : [];
    const cost = historicalReplay ? [
      ['手续费',match(cell(integrity,'已支付佣金 (Commission)',1),/([\d,]+\.\d+) 元/)],
      ['印花税',match(cell(integrity,'已缴纳印花税 (Stamp Tax)',1),/([\d,]+\.\d+) 元/)],
      ['模拟滑点',match(integrity,/隐式模拟滑点[^\n]*?\x60([\d,]+\.\d+) 元/)],
      ['总交易摩擦',match(integrity,/Cost\\ Drag = [^\n]*?=\s*([\d,]+\.\d+)\\text/)]] : [];
    const exit = historicalReplay ? ['TAKE_PROFIT (止盈出场)','STOP_LOSS (止损出场)','TIME_EXIT (到期出场)'].map((label,i)=>[
      ['止盈出场','止损出场','到期出场'][i], match(cell(integrity,label,1),/(\d+) 笔/), pct(cell(integrity,label,2))]) : [];
    return { oos, replay, cost, exit, historicalReplay,
      tail1: pct(match(audit,/Next Open Top-5 均值从[^\n]*?下降到\s*\*\*\x60([+-]?\d+(?:\.\d+)?%)/)),
      tail5: pct(match(audit,/Next Open Top-5 均值下降至\s*\*\*\x60([+-]?\d+(?:\.\d+)?%)/)) };
  },

  async refreshResearch() {
    const envelope = await V7Api.getResearch();
    const reports = envelope?.data?.reports || []; this.researchReports = reports; this.renderResearchArchive(reports);
    const [research, audit, interim, integrity, start] = await Promise.all([
      this.getResearchReport('phase12_research_report'), this.getResearchReport('phase12_human_audit'),
      this.getResearchReport('phase18_forward_20d_interim_report'), this.getResearchReport('phase18_5_forward_integrity'),
      this.getResearchReport('phase18f_genuine_forward_start_gate')]);
    const metrics = this.phase6HistoryMetrics(research?.content, audit?.content, interim?.content, integrity?.content);
    const renderFacts = (id, fields) => { const root = document.getElementById(id); if (!root) return;
      root.replaceChildren(); const valid = fields.filter(([,value])=>value !== null);
      if (valid.length) this.appendFactList(root,valid); else root.append(this.textNode('p','历史指标证据暂不可读取。','muted')); };
    renderFacts('research-oos-metrics', metrics.oos);
    renderFacts('research-replay-metrics', metrics.replay);
    renderFacts('research-replay-costs', metrics.cost.map(([label,value])=>[label,value === null ? null : value+'元']));
    const exits = document.getElementById('research-replay-exits'); if (exits) { exits.replaceChildren();
      for (const [label,count,avg] of metrics.exit) if (count !== null && avg !== null)
        exits.append(this.textNode('p',label+'：'+count+'笔，平均 '+avg)); }
    const limits = document.getElementById('research-limitations'); if (limits) {
      limits.replaceChildren();
      if (metrics.tail1 && metrics.tail5) limits.append(this.textNode('p','收益对少数大赢家存在明显依赖。剔除表现最好前1%，5日平均收益为 '+metrics.tail1+'；剔除前5%，下降至 '+metrics.tail5+'。'));
      if (metrics.historicalReplay) limits.append(this.textNode('p','历史模拟的主要亏损来自少数大幅止损交易；T+1、跳空及实际退出价可能扩大损失。'));
      limits.append(this.textNode('p','样本外测试与历史模拟均不代表未来收益。日K MAE/MFE排除了入场当日波动；开盘价代理并不保证涨跌停时可以成交。'));
      limits.append(this.textNode('p','滑点压力研究仅动态改变卖出端，买入端使用固定基准；不是完整双边重建。'));
    }
    this.setText('research-classification-note', metrics.historicalReplay ? '本组结果经完整性审计重定性为历史模拟回放，不代表真实前向绩效。' : '历史回放归属证据暂不可确认，不展示模拟绩效。');
    const raw = document.getElementById('history-technical-json'); if (raw) raw.textContent = JSON.stringify({
      classification: metrics.historicalReplay ? 'HISTORICAL_PAPER_REPLAY' : 'UNVERIFIED',
      metric_sources: ['PHASE_12_RESEARCH_REPORT.md','PHASE_12_HUMAN_AUDIT.md','PHASE_18_FORWARD_20D_INTERIM_REPORT.md','PHASE_18_5_FORWARD_INTEGRITY_AND_PNL_ATTRIBUTION.md'],
      supersession: 'Phase18.5重新分类旧Forward并修正MAE误差语义；动态回撤取原中期报告，其他模拟指标优先完整性审计。',
      owner_research_decision: /RESEARCH_VALIDATED = TRUE/.test(start?.content || '') ? '后续Owner已批准科研状态，非绩效或实盘批准' : '后续Owner裁决证据不可读取',
      historical_epoch: '旧Epoch仅历史审计，当前以active Forward API为准', parsed_metrics: metrics },null,2);
    for (const [id,reportId] of [['research-phase12-link','phase12_research_report'],['research-phase18-link','phase18_5_forward_integrity']]) {
      const button=document.getElementById(id); if(button) button.onclick=async()=>{this.switchPage('page-reports');await this.openResearchReport(reportId);};
    }
  },

  renderResearchNotes(state) {
    const conclusion = document.getElementById('research-conclusion');
    if (conclusion) {
      conclusion.replaceChildren();
      conclusion.append(this.textNode('p', state.phase12Available ? 'Phase12 已完成样本外研究；研究结论和人工裁决状态请以报告原文为准。' : 'Phase12 正文暂不可用，请查看白名单报告。', 'research-note-line'));
      conclusion.append(this.textNode('p', state.phase18Available && state.integrityAvailable ? 'Phase18 为历史模拟验证；完整性审计将该批结果重新定性为历史模拟回放。' : 'Phase18 正文暂不可用，请查看白名单报告。', 'research-note-line'));
      if (state.replayConflict) conclusion.append(this.textNode('p', '研究证据存在版本差异，请查看完整报告。', 'research-conflict'));
    }
    const limits = document.getElementById('research-limitations');
    if (limits) {
      limits.replaceChildren();
      for (const note of ['研究样本来自历史区间；历史表现不代表未来结果。', 'Phase18 是历史模拟回放，不代表 Genuine Forward 或实盘收益。', 'Phase18 中期报告指出结果存在右尾赢家依赖；请结合完整风险审计阅读。']) limits.append(this.textNode('p', note, 'research-note-line'));
    }
  },

  renderResearchArchive(reports) {
    const root = document.getElementById('backtest-reports'); if (!root) return;
    root.replaceChildren();
    const archiveIds = ['phase12_research_report', 'phase12_entry_gate', 'phase12_human_audit', 'phase18_baseline_freeze', 'phase18_forward_20d_interim_report', 'phase18_5_forward_integrity', 'phase18f_genuine_forward_start_gate'];
    const reportById = Object.fromEntries(reports.map((report) => [report.report_id, report]));
    for (const id of archiveIds) if (reportById[id]) root.append(this.createReportCard(reportById[id], 'research'));
    if (!root.childElementCount) root.append(this.textNode('p', '当前 API 未返回可显示的历史研究报告。', 'muted'));
  },

  reportMeta(reportId) {
    return V7ResearchReportMeta[reportId] || { title: '研究与系统报告', category: '系统', description: '只读白名单报告；请查看正文了解具体内容。' };
  },

  createReportCard(report, mode = 'audit') {
    const meta = this.reportMeta(report.report_id);
    const card = document.createElement('article'); card.className = 'card report-card';
    const heading = document.createElement('div'); heading.className = 'card-heading';
    heading.append(this.textNode('h2', meta.title), this.textNode('span', meta.category, 'status-chip neutral'));
    card.append(heading, this.textNode('p', meta.description, 'report-description'), this.textNode('p', `更新时间：${report.updated_at || '未提供'}`, 'report-updated'));
    const button = this.textNode('button', '查看报告 →', 'text-link'); button.type = 'button';
    button.addEventListener('click', () => this.openResearchReport(report.report_id, report, mode === 'research' ? 'research-report-viewer' : 'report-viewer'));
    card.append(button);
    const details = document.createElement('details'); details.className = 'technical-details report-technical';
    details.append(this.textNode('summary', '技术详情'), this.textNode('pre', JSON.stringify({ report_id: report.report_id, file_name: report.file_name, file_size: report.file_size }, null, 2), 'technical-json'));
    card.append(details);
    return card;
  },

  renderReportFilters() {
    const root = document.getElementById('report-filters'); if (!root) return;
    root.replaceChildren();
    for (const category of ['全部', '研究', '回放', '风险', '准入', 'Forward', '系统']) {
      const button = this.textNode('button', category, `report-filter${this.reportCategoryFilter === category ? ' active' : ''}`);
      button.type = 'button'; button.setAttribute('aria-pressed', String(this.reportCategoryFilter === category));
      button.addEventListener('click', () => { this.reportCategoryFilter = category; this.renderReportFilters(); this.renderReportList(this.researchReports || []); });
      root.append(button);
    }
  },

  renderReportList(reports) {
    const root = document.getElementById('reports-list'); if (!root) return;
    root.replaceChildren();
    const filtered = reports.filter((report) => this.reportCategoryFilter === '全部' || this.reportMeta(report.report_id).category === this.reportCategoryFilter);
    for (const report of filtered) root.append(this.createReportCard(report, 'audit'));
    if (!filtered.length) root.append(this.textNode('p', '此类别当前没有白名单报告。', 'muted'));
    this.setText('reports-count', `${reports.length} 份白名单报告`);
  },

  async openResearchReport(reportId, reportMeta = null, viewerId = 'report-viewer') {
    const viewer = document.getElementById(viewerId); if (!viewer) return;
    viewer.hidden = false; viewer.replaceChildren(this.textNode('h2', this.reportMeta(reportId).title, 'report-viewer-title'));
    const loaded = await this.getResearchReport(reportId);
    if (!loaded) { viewer.append(this.textNode('p', '报告正文暂不可用，请稍后重试。', 'state-panel unavailable')); return; }
    const data = loaded.data;
    const facts = reportMeta || this.researchReports?.find((item) => item.report_id === reportId) || {};
    const technical = document.createElement('details'); technical.className = 'technical-details';
    technical.append(this.textNode('summary', '报告来源与技术详情'), this.textNode('pre', JSON.stringify({ report_id: data.report_id, file_name: data.file_name, file_size: facts.file_size, updated_at: facts.updated_at }, null, 2), 'technical-json'));
    viewer.append(technical, this.textNode('pre', loaded.content, 'report-content'));
    viewer.scrollIntoView({ behavior: 'smooth', block: 'start' });
  },

  async refreshReports() {
    const envelope = await V7Api.getResearch();
    this.researchReports = Array.isArray(envelope?.data?.reports) ? envelope.data.reports : [];
    this.renderReportFilters();
    this.renderReportList(this.researchReports);
    this.renderResearchArchive(this.researchReports);
    this.setPanel('reports-warning', this.researchReports.length ? 'normal' : 'unavailable', '白名单报告目录', this.researchReports.length ? '报告目录仅提供白名单元数据；列表未声明 PASS、HOLD 或研究状态。' : '当前 API 未返回可用报告清单。');
  },

  loadDisplaySettings() {
    try {
      const values = JSON.parse(window.localStorage.getItem('v7_display_settings') || '{}');
      if (values.density) document.getElementById('setting-density').value = values.density;
      if (values.home) document.getElementById('setting-home').value = values.home;
    } catch (_) { /* Corrupt local display preference is ignored, not a system-state failure. */ }
  },

  saveDisplaySettings(event) {
    event.preventDefault();
    const values = { density: document.getElementById('setting-density').value,
      home: document.getElementById('setting-home').value };
    try { window.localStorage.setItem('v7_display_settings', JSON.stringify(values)); }
    catch (_) { this.setPanel('data-center-warning', 'error', '本地偏好未保存', '浏览器存储不可用。'); }
  },

  loadPersonalPortfolio() {
    const container = document.getElementById('personal-holdings');
    try {
      const result = window.V7PersonalPortfolioStore.readStorage(window.localStorage);
      this.personalItems = result.holdings;
      this.personalSource = result.source;
      this.setText('personal-storage-status', `本地持仓：${this.personalItems.length} 条 · ${this.personalSource}`);
      this.setPanel('personal-warning', 'hold', '个人本地数据 · 非券商资产', '记录仅保存在此浏览器；清理浏览器数据可能丢失，请定期导出备份。没有合格个人行情时，当前价、市值和浮盈亏保持 UNKNOWN。');
      this.renderPersonalPortfolio();
      this.updatePersonalOverview();
    } catch (_) {
      this.personalItems = null;
      this.setText('personal-storage-status', '本地数据校验失败 · 未覆盖原数据');
      this.setPanel('personal-warning', 'error', '个人本地数据损坏或结构不兼容', '为避免静默丢失，已停止读写；可以选择有效备份导入恢复。原存储未被改写。');
      this.setPanel('personal-state', 'error', '本地持仓校验失败', '未能读取个人记录；请使用有效备份恢复。不会回退到模拟账户数据。');
      if (container) container.replaceChildren();
      this.setText('personal-risk', 'UNKNOWN · 本地数据校验失败');
      this.setText('personal-overview-status', '本地数据错误');
    }
  },

  savePersonalHolding(event) {
    event.preventDefault();
    if (!Array.isArray(this.personalItems)) return;
    const code = document.getElementById('personal-symbol').value.trim();
    const name = document.getElementById('personal-name').value.trim();
    const shares = Number(document.getElementById('personal-shares').value);
    const cost = Number(document.getElementById('personal-cost').value);
    const notes = document.getElementById('personal-notes').value.trim();
    const value = { symbol: code, name, shares, cost_price: cost, notes };
    const rawIndex = document.getElementById('personal-edit-index').value;
    try {
      const next = window.V7PersonalPortfolioStore.upsert(this.personalItems, value, rawIndex);
      if (this.commitPersonalItems(next, '个人记录已保存')) this.resetPersonalForm();
    } catch (error) {
      this.setPanel('personal-warning', 'error', '记录未保存', error.code === 'PERSONAL_DUPLICATE_SYMBOL'
        ? '同一证券已有记录；请编辑已有持仓，不重复新增。'
        : '请检查证券代码与交易所、名称、正整数数量、正成本价及备注长度。');
    }
  },

  editPersonalHolding(index) {
    const item = this.personalItems?.[index]; if (!item) return;
    const code = String(item.symbol || '').replace(/^(SH|SZ|BJ)/i, '').replace(/\.(SH|SZ|BJ)$/i, '');
    document.getElementById('personal-edit-index').value = String(index);
    document.getElementById('personal-symbol').value = code;
    document.getElementById('personal-name').value = item.name || '';
    document.getElementById('personal-shares').value = item.shares ?? '';
    document.getElementById('personal-cost').value = item.cost_price ?? '';
    document.getElementById('personal-notes').value = item.notes || '';
  },

  deletePersonalHolding(index) {
    if (!Array.isArray(this.personalItems)) return;
    try {
      const next = window.V7PersonalPortfolioStore.remove(this.personalItems, index);
      this.commitPersonalItems(next, '记录已删除');
    } catch (_) { this.setPanel('personal-warning', 'error', '删除未能完成', '持仓列表或本地数据校验失败；原记录未主动覆盖。'); }
  },

  commitPersonalItems(items, message) {
    try {
      const validated = window.V7PersonalPortfolioStore.validateHoldings(items);
      window.localStorage.setItem(this.personalStorageKey, window.V7PersonalPortfolioStore.serializeStorage(validated));
      this.personalItems = validated;
      this.personalSource = 'V7_LOCAL_V1';
      this.setText('personal-storage-status', `本地持仓：${validated.length} 条 · ${this.personalSource}`);
      this.renderPersonalPortfolio();
      this.updatePersonalOverview();
      this.setPanel('personal-warning', 'normal', message, '仅写入独立浏览器本地个人持仓；不触及 Arena、AI Quant Paper、模拟账户或交易账本。');
      return true;
    } catch (_) {
      this.setPanel('personal-warning', 'error', '浏览器本地保存失败', '原记录保持不变；未写入服务器、模拟账户或经济账本。请检查浏览器存储空间。');
      return false;
    }
  },

  updatePersonalOverview() {
    const count = Array.isArray(this.personalItems) ? this.personalItems.length : null;
    this.setText('personal-overview-status', count === null ? '个人记录待确认' : (count === 0 ? '暂无持仓' : '已记录 ' + count + ' 只'));
    this.recordOverviewEvidence('personal', { status: count === null ? 'UNAVAILABLE' : 'LOCAL_ONLY', count, source: this.personalSource });
  },

  exportPersonalPortfolio() {
    if (!Array.isArray(this.personalItems)) {
      this.setPanel('personal-warning', 'error', '无法导出未通过校验的数据', '先用有效备份恢复；原损坏数据不会被静默丢弃。');
      return;
    }
    try {
      const payload = window.V7PersonalPortfolioStore.createBackup(this.personalItems);
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      if (this.personalBackupUrl) URL.revokeObjectURL(this.personalBackupUrl);
      this.personalBackupUrl = url;
      const link = document.getElementById('personal-download-link');
      if (!link) throw new Error('PERSONAL_DOWNLOAD_LINK_MISSING');
      link.href = url;
      link.download = `v7-personal-portfolio-backup-${new Date().toISOString().slice(0, 10)}.json`;
      link.hidden = false;
      link.click();
      this.setPanel('personal-warning', 'normal', '本地备份已生成', '请将导出文件保存在安全位置；备份不含行情、券商资产或模拟账户数据。若浏览器未自动下载，可点击旁边的备用下载链接。');
    } catch (_) { this.setPanel('personal-warning', 'error', '备份导出失败', '当前浏览器未能生成备份；原本地持仓未修改。'); }
  },

  async importPersonalPortfolio(event) {
    const input = event.currentTarget;
    const file = input?.files?.[0];
    if (!file) return;
    try {
      const imported = window.V7PersonalPortfolioStore.parseBackup(await file.text());
      if (Array.isArray(this.personalItems) && this.personalItems.length &&
          !window.confirm(`将以备份中的 ${imported.length} 条记录替换当前个人持仓，是否继续？`)) return;
      this.commitPersonalItems(imported, '个人持仓备份已导入');
    } catch (error) {
      this.setPanel('personal-warning', 'error', '备份未导入', `文件结构或记录校验失败（${error.code || 'IMPORT_ERROR'}）；当前数据未修改。`);
    } finally { input.value = ''; }
  },

  resetPersonalForm() {
    document.getElementById('personal-holding-form')?.reset();
    const index = document.getElementById('personal-edit-index'); if (index) index.value = '';
  },

  renderPersonalPortfolio() {
    const root = document.getElementById('personal-holdings'); if (!root) return;
    root.replaceChildren();
    for (const [index, item] of (this.personalItems || []).entries()) {
      const card = document.createElement('article'); card.className = 'card personal-card';
      const basis = Number(item.shares) * Number(item.cost_price);
      card.append(this.textNode('h2', `${item.name || '未命名'} · ${String(item.symbol || '').toUpperCase()}`));
      this.appendFactList(card, [['持有数量', Number.isFinite(Number(item.shares)) ? item.shares : 'UNKNOWN'],
        ['成本价', this.moneyOrUnknown(Number(item.cost_price))], ['成本金额', Number.isFinite(basis) ? this.moneyOrUnknown(basis) : 'UNKNOWN'],
        ['当前价格/市值', 'UNKNOWN · 无经验证的个人行情'], ['浮盈亏', 'UNKNOWN · 未计算'], ['备注', item.notes || '—']]);
      const actions = document.createElement('div'); actions.className = 'personal-actions';
      for (const [action, label] of [['edit', '编辑'], ['delete', '删除']]) {
        const button = this.textNode('button', label, 'text-link'); button.type = 'button';
        button.dataset.personalAction = action; button.dataset.index = String(index); actions.append(button);
      }
      card.append(actions); root.append(card);
    }
    if (!root.childElementCount) root.append(this.textNode('p', '暂无个人持仓记录；不填充演示股票。', 'muted'));
  },

  renderStock(envelope, picksEnvelope = null, queryCode = '') {
    const root = document.getElementById('stock-detail');
    if (!root) return;
    root.replaceChildren();
    const data = envelope.data || {};
    const code = queryCode || data.identity?.symbol || '';
    const context = V7StockEvidence.personaContext(data, picksEnvelope, code);
    const top5 = V7StockEvidence.currentTop5(picksEnvelope);
    const prediction = top5?.find(row => row.symbol === code);
    const quote = V7StockEvidence.quote(data.quote_evidence);
    const newsItems = Array.isArray(data.news?.items) ? data.news.items : [];
    const hasIdentity = V7PicksPresentation.text(data.identity?.name);
    const hasEvidence = hasIdentity || quote.facts.length || context.matches.length || prediction || newsItems.length || data.risk_sentinel?.alert_triggered === true;
    const section = (title, className = '') => {
      const node = document.createElement('article'); node.className = 'card stock-evidence-section ' + className;
      node.append(this.textNode('h2', title)); root.append(node); return node;
    };
    if (!hasEvidence) {
      section('暂未找到可验证的股票信息。', 'stock-empty').append(this.textNode('p', code, 'muted'));
    } else {
      const overview = section(hasIdentity ? data.identity.name : code, 'stock-identity');
      if (hasIdentity) overview.append(this.textNode('p', code, 'stock-code'));
      const exchanges = { SH: '上海证券交易所', SZ: '深圳证券交易所', BJ: '北京证券交易所' };
      const boards = { MAIN: '主板', GEM: '创业板', STAR: '科创板' };
      const identityNote = [exchanges[data.identity?.exchange], boards[data.identity?.board]].filter(Boolean).join(' · ');
      if (identityNote) overview.append(this.textNode('p', identityNote, 'muted'));
      const quoteSection = section(quote.title);
      quoteSection.append(this.textNode('p', quote.note, 'stock-evidence-note'));
      if (quote.facts.length) {
        this.appendFactList(quoteSection, quote.facts);
        const quoteValues = data.quote_evidence?.value_fields || {};
        const change = data.quote_evidence?.source_type === 'LOCAL_SQLITE_RESEARCH_BAR' ? quoteValues.pct_chg : quoteValues.change_pct;
        const changeIndex = quote.facts.findIndex(([label]) => label === '涨跌幅');
        if (changeIndex >= 0 && V7PicksPresentation.finite(change) && change !== 0) {
          quoteSection.querySelector('dl').children[changeIndex].querySelector('dd').className = change > 0 ? 'metric-up' : 'metric-down';
        }
      }
      const personas = section('今日策略关注');
      if (!context.complete) personas.append(this.textNode('p', '今日策略关注情况暂无法完整确认。'));
      else if (!context.matches.length) personas.append(this.textNode('p', '今日五个策略均未主动选择该股。'));
      else {
        for (const [id, name] of V7PicksPresentation.roles) {
          const role = context.matches.find(row => row.participant_id === id);
          if (!role) continue;
          const row = document.createElement('section'); row.className = 'stock-persona-view';
          row.append(this.textNode('h3', name), this.textNode('strong', V7PicksPresentation.action(role.action), 'stock-opinion'));
          const reason = V7PicksPresentation.firstReason(role.decision_reason);
          if (reason) row.append(this.textNode('p', reason));
          if (V7PicksPresentation.horizon(role)) row.append(this.textNode('p', '计划周期：' + role.holding_horizon, 'muted'));
          if (role.decision_reason && role.decision_reason !== reason) {
            const full = document.createElement('details'); full.className = 'stock-reason-details';
            full.append(this.textNode('summary', '查看完整策略理由'), this.textNode('p', role.decision_reason)); row.append(full);
          }
          personas.append(row);
        }
        personas.append(this.textNode('p', '这是策略观点，不代表订单或成交。', 'stock-evidence-note'));
      }
      const aiq = section('AI Quant');
      if (!top5) aiq.append(this.textNode('p', '今日 AI Quant Top5 暂不可确认。'));
      else if (!prediction) aiq.append(this.textNode('p', '今日未进入 AI Quant Top5。'));
      else {
        aiq.append(this.textNode('h3', '今日 AI Quant Top5'), this.textNode('p', '排名 ' + prediction.rank));
        const fields = [['上涨概率', 'up_probability', true, '%'], ['预期收益', 'expected_return', true, '%'], ['预测MAE', 'predicted_mae', true, '%'],
          ['AI Quant 右侧确认分', 'right_side_score', false, ''], ['预计持有天数', 'expected_holding_days', false, '天']];
        this.appendFactList(aiq, fields.filter(([, key]) => V7PicksPresentation.finite(prediction[key])).map(([label, key, percent, unit]) =>
          [label, (percent ? (prediction[key] * 100).toFixed(2) : String(prediction[key])) + unit]));
        aiq.append(this.textNode('p', '模型预测证据，不代表订单或成交。', 'stock-evidence-note'));
      }
      const news = section('相关新闻');
      if (!newsItems.length) news.append(this.textNode('p', data.news?.status === 'EMPTY' ? '暂无直接关联的新闻记录。' : '相关新闻暂无法确认。', 'muted'));
      for (const item of newsItems) {
        const article = document.createElement('section'); article.className = 'stock-news-item';
        if (V7PicksPresentation.text(item.title)) article.append(this.textNode('h3', item.title));
        const metadata = [];
        if (V7PicksPresentation.text(item.source)) metadata.push(item.source);
        if (V7PicksPresentation.text(item.published_at || item.published_at_raw)) metadata.push('来源记录时间：' + (item.published_at || item.published_at_raw));
        if (metadata.length) article.append(this.textNode('p', metadata.join(' · '), 'muted'));
        if (V7PicksPresentation.text(item.content || item.summary)) {
          const content = document.createElement('details'); content.append(this.textNode('summary', '查看保存的新闻内容'), this.textNode('p', item.content || item.summary)); article.append(content);
        }
        news.append(article);
      }
      const sentinel = V7StockEvidence.sentinel(data, picksEnvelope, code, context.complete);
      if (sentinel) {
        const risk = section('风险观察');
        risk.append(this.textNode('h3', '反指哨兵'), this.textNode('p', sentinel.reason),
          this.textNode('p', '仅提供风险观察，不参与交易和排名。', 'stock-evidence-note'));
      }
    }
    const technical = document.createElement('details'); technical.className = 'technical-details stock-technical-evidence'; technical.id = 'stock-technical-evidence';
    technical.append(this.textNode('summary', '查看技术证据'), this.textNode('pre', JSON.stringify(V7StockEvidence.technical(envelope, picksEnvelope, context), null, 2), 'technical-json'));
    root.append(technical);
  },

  formatValue(value, digits = 2) {
    return typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits }) : '—';
  },

  renderMarket(envelope) {
    const data = envelope.data || {};
    const view = V7MarketEnvironment.eligible(data.environment);
    const root = document.getElementById('market-environment-content');
    if (root) {
      root.replaceChildren(); root.hidden = !view;
      if (view) {
        const summary = this.dashboardStateCard(view, data.environment.classification);
        const indexes = document.createElement('div'); indexes.id = 'market-index-grid'; indexes.className = 'card-grid three-col market-environment-indexes';
        indexes.classList.add('dashboard-index-grid');
        for (const item of V7DashboardData.rows(data)) indexes.append(this.dashboardIndexCard(item, { ...view, observedAt: data.environment.observed_at }, false));
        const reason = document.createElement('article'); reason.className = 'card market-environment-reason';
        reason.append(this.textNode('h2', '为什么是“' + view.label + '”'),
          this.textNode('p', '四个主要指数平均涨跌约 ' + this.formatValue(view.inputs.avg_pct, 2) + '%。'));
        const dates = [...new Set(V7DashboardData.rows(data).filter(item => item.series).map(item => item.series.last.slice(0, 10)))];
        root.append(summary, this.textNode('h2', '指数表现', 'market-section-title'), indexes, this.textNode('p', (dates.length ? dates.join(' / ') + ' · ' : '') + '展示已保存行情记录，非实时行情；曲线独立缩放。', 'dashboard-shared-note'), this.dashboardMarketEvidence(data, view), reason);
      }
    }
    const basis = document.getElementById('market-environment-basis');
    if (basis) {
      basis.replaceChildren();
      if (view) {
        basis.append(this.textNode('p', '本次判断使用上证、深证、创业板、科创50四个指数。'));
        for (const [key, name] of [['sh_pct', '上证'], ['sz_pct', '深证'], ['cyb_pct', '创业板'], ['kcb_pct', '科创50']])
          basis.append(this.textNode('p', name + '：' + this.formatValue(view.inputs[key], 3) + '%'));
        basis.append(this.textNode('p', '四指数平均输入：' + this.formatValue(view.inputs.avg_pct, 3) + '%'),
          this.textNode('p', '原始规则结果：' + data.environment.classification + ' → ' + view.label),
          this.textNode('p', '复用 market_regime v1：crisis 为均幅≤-2，或上证<-1.5且创业板/科创50<-3；bull 为均幅≥1且原成交输入≥15000；bear 为均幅≤-1.2；其余为 range。按原规则顺序判断。'),
          this.textNode('p', 'as_of 是规则结果的生成时间，不是 Provider 报价时间。'));
      }
    }
    const raw = document.getElementById('market-technical-json');
    if (raw) raw.textContent = JSON.stringify({ status: envelope.status, warnings: envelope.warnings || [], data_freshness: envelope.data_freshness || {}, data }, null, 2);
    this.recordOverviewEvidence('market', envelope);
    this.renderHomeMarket(data);
  },

  marketPercentNode(value) {
    return this.textNode('strong', (value > 0 ? '+' : '') + this.formatValue(value, 2) + '%',
      'market-environment-percent ' + (value > 0 ? 'metric-up' : (value < 0 ? 'metric-down' : '')));
  },

  dashboardIndexCard(item, view, compact, scale = null) {
    const card = document.createElement('article'); card.className = 'dashboard-index-card'; card.dataset.indexSymbol = item.symbol;
    const percent = view.inputs[item.input];
    const quote = document.createElement('div'); quote.className = 'dashboard-index-quote'; quote.append(this.textNode('h3', item.name));
    if (item.series) {
      const point = this.textNode('strong', this.formatValue(item.series.lastValue), 'dashboard-index-value');
      point.dataset.source = 'intraday.points'; point.dataset.barAt = item.series.last;
      quote.append(point, this.textNode('span', '分时记录末点 · ' + item.series.last.slice(11, 16), 'dashboard-value-label'));
    }
    card.append(quote);
    if (item.series && (!compact || (scale && item.series.segments[0][0].minute === 570))) {
      const figure = document.createElement('figure'); figure.className = 'dashboard-series';
      const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', `${item.name}保存分时，${item.series.last.slice(0, 10)}截至${item.series.last.slice(11, 16)}；${compact ? '相对09:30首条记录，统一比例' : '独立缩放'}，非实时行情`);
      svg.classList.add('dashboard-sparkline', percent > 0 ? 'metric-up' : (percent < 0 ? 'metric-down' : 'metric-flat'));
      svg.dataset.recordCount = String(item.series.count); svg.dataset.noonGap = '10';
      svg.dataset.indexSymbol = item.symbol;
      svg.dataset.scaleMode = compact ? scale.mode : 'INDEPENDENT_PRICE_RANGE';
      svg.dataset.scaleMin = String(compact ? scale.min : item.series.min);
      svg.dataset.scaleMax = String(compact ? scale.max : item.series.max);
      if (compact) svg.dataset.originBar = item.series.segments[0][0].bar_at;
      const draw = width => {
        const height = compact ? 48 : 80;
        svg.setAttribute('viewBox', `0 0 ${width} ${height}`); svg.setAttribute('preserveAspectRatio', 'none'); svg.replaceChildren();
        const segments = V7DashboardData.coordinates(item.series, width, height, compact ? scale : null);
        item.series.segments.forEach((segment, index) => {
          const coordinates = segments[index];
          const path = document.createElementNS('http://www.w3.org/2000/svg', coordinates.length === 1 ? 'circle' : 'polyline');
          path.dataset.firstBar = segment[0].bar_at; path.dataset.lastBar = segment.at(-1).bar_at; path.dataset.recordCount = String(segment.length);
          if (coordinates.length === 1) { path.setAttribute('cx', String(coordinates[0][0])); path.setAttribute('cy', String(coordinates[0][1])); path.setAttribute('r', '2'); }
          else { path.setAttribute('points', coordinates.map(point => point.join(',')).join(' ')); path.setAttribute('fill', 'none'); }
          svg.append(path);
        });
      };
      draw(200); figure.append(svg);
      if (!compact) {
        const axis = document.createElement('figcaption'); axis.className = 'dashboard-time-axis';
        for (const times of [['09:30', '11:30'], ['13:00', '15:00']]) {
          const session = document.createElement('span'); session.className = 'dashboard-axis-session';
          for (const time of times) session.append(this.textNode('span', time)); axis.append(session);
        }
        figure.append(axis);
      }
      const observer = new ResizeObserver(entries => { if (!svg.isConnected) { observer.disconnect(); return; } const width = entries[0].contentRect.width; if (width > 22) draw(width); });
      observer.observe(svg); card.append(figure);
    } else card.append(this.textNode('span', '', 'dashboard-series-absent'));
    card.append(this.marketPercentNode(percent)); return card;
  },

  dashboardMarketEvidence(data, view, scale = null) {
    const details = document.createElement('details'); details.className = 'dashboard-index-evidence';
    details.append(this.textNode('summary', '数据来源与时间'));
    details.append(this.textNode('p', '分时记录末点只取合格分时的最后一个真实值；不是快照、实时价或已验证收盘价。' + (scale ? '首页走势相对各自09:30记录，统一比例；绘图相对变化不是正式每日涨跌幅。' : '本页每条曲线独立缩放，只比较本指数走势形态。'), 'muted'));
    details.append(this.textNode('pre', JSON.stringify(V7DashboardData.rows(data).map(item => ({
      symbol: item.symbol, percent: { value: view.inputs[item.input], source: 'market.environment.inputs.' + item.input, observed_at: data.environment.observed_at },
      snapshot: item.row?.snapshot ?? null, snapshot_qualified: Boolean(item.snapshot), snapshot_displayed: false,
      snapshot_note: item.snapshot ? '快照证据保留；第一层统一显示分时记录末点' : '快照报价同日证据不足或记录失效，未展示',
      intraday: item.row?.intraday ? { ...item.row.intraday, points: `${item.row.intraday.points?.length ?? 0}条；完整points见市场原始证据` } : null,
      point_source: 'indexes[].intraday.points[last].value', point_displayed: Boolean(item.series), series_displayed: Boolean(item.series),
      last_record: item.series ? { value: item.series.lastValue, bar_at: item.series.last } : null,
      chart_scale: scale, chart_origin: scale && item.series?.segments[0]?.[0]?.minute === 570 ? item.series.segments[0][0] : null,
      note: '采集批次、报价时间、分时bar时间分别记录；没有新增新鲜度SLA或执行授权。'
    })), null, 2), 'technical-json'));
    return details;
  },

  dashboardStateCard(view, classification) {
    const card = document.createElement('article'); card.className = 'dashboard-state-card ' + (classification === 'bull' ? 'state-bull' : (classification === 'range' ? 'state-range' : 'state-bear'));
    card.append(this.textNode('span', '市场环境', 'dashboard-state-title'), this.textNode('strong', view.label, 'market-environment-label'), this.textNode('p', view.description), this.textNode('span', '规则记录 · ' + view.time, 'dashboard-record-time'));
    return card;
  },

  renderHomeOpportunities(envelope) {
    const root = document.getElementById('overview-opinion-distribution');
    const view = V7DashboardData.opinions(envelope);
    const summary = document.getElementById('overview-picks-summary');
    if (summary) summary.textContent = view ? '' : '今日策略观点待确认';
    if (!root) return;
    root.replaceChildren(); root.hidden = !view;
    if (!view) return;
    const groups = document.createElement('div'); groups.className = 'dashboard-opinion-groups';
    for (const [action, count, label, tone] of [['BUY', view.buy, '考虑买入', 'consider'], ['NO_SIGNAL', view.observe, '继续观察', 'observe']]) {
      const group = document.createElement('section'); group.className = 'dashboard-opinion-group ' + tone; group.dataset.action = action;
      const heading = document.createElement('div'); heading.className = 'dashboard-group-heading';
      heading.append(this.textNode('strong', String(count), 'dashboard-opinion-count'), this.textNode('span', label));
      group.setAttribute('aria-label', label + count + '个策略');
      const cards = document.createElement('div'); cards.className = 'dashboard-persona-cards';
      for (const role of view.roles.filter(role => role.action === action)) {
        const button = document.createElement('button'); button.type = 'button'; button.className = 'dashboard-persona-card ' + tone; button.dataset.participantId = role.participant_id; button.dataset.action = role.action;
        const name = V7PicksPresentation.roles.find(([id]) => id === role.participant_id)[1];
        button.append(this.textNode('span', name, 'dashboard-persona-name'));
        if (V7PicksPresentation.identity(role)) button.append(this.textNode('strong', role.stock_name, 'dashboard-stock-name'));
        button.setAttribute('aria-label', name + ' · ' + (V7PicksPresentation.identity(role) ? role.stock_name + ' · ' : '') + label + '，查看今日选股');
        button.addEventListener('click', () => this.switchPage('page-picks')); cards.append(button);
      }
      group.append(heading, cards); groups.append(group);
    }
    root.append(groups, this.textNode('p', '观点记录 · ' + view.asOf.slice(0, 16).replace('T', ' ') + ' · 策略观点，不代表交易授权。', 'dashboard-record-time'));
  },

  renderHomeNews(envelope) {
    const root = document.getElementById('overview-news-summary'); const area = document.getElementById('overview-news-auxiliary'); const container = document.getElementById('overview-opportunities-card');
    const rows = V7DashboardData.news(envelope).slice(0, 1);
    if (area) area.hidden = !rows.length;
    if (container) container.classList.toggle('without-news', !rows.length);
    if (!root) return; root.replaceChildren();
    for (const row of rows) {
      const article = document.createElement('article'); const link = this.textNode('a', row.title, 'dashboard-news-title'); link.title = row.title; link.href = row.url; link.target = '_blank'; link.rel = 'noopener noreferrer';
      article.append(link, this.textNode('p', row.source + ' · 记录 ' + row.published_at.slice(5, 16).replace('T', ' '), 'dashboard-record-time')); root.append(article);
    }
  },

  safeArticleLink(value) {
    if (typeof value !== 'string') return null;
    try {
      const parsed = new URL(value, window.location.href);
      return ['http:', 'https:'].includes(parsed.protocol) && !parsed.username && !parsed.password ? parsed.href : null;
    } catch (_) { return null; }
  },

  renderNewsItem(item) {
    const article = document.createElement('article'); article.className = 'news-item';
    const heading = document.createElement('h3');
    const href = this.safeArticleLink(item.url);
    if (href) {
      const link = document.createElement('a'); link.href = href; link.target = '_blank'; link.rel = 'noopener noreferrer'; link.textContent = item.title || '无标题'; heading.append(link);
    } else heading.textContent = item.title || '无标题';
    const meta = document.createElement('p'); meta.className = 'news-meta';
    meta.textContent = `${item.source || '来源未知'} · ${item.category || '未分类'} · 来源记录时间：${item.published_at_raw || 'UNKNOWN'}（${item.published_at_status || 'UNAVAILABLE'}）`;
    const summary = document.createElement('p'); summary.className = 'news-summary'; summary.textContent = item.summary || item.content || '无保存摘要';
    article.append(heading, meta, summary);
    if (item.ai_analysis?.status === 'AVAILABLE') {
      const ai = document.createElement('details'); ai.className = 'news-ai';
      const label = document.createElement('summary'); label.textContent = `独立 AI 研判 · ${item.ai_analysis.generated_by}`;
      const body = document.createElement('div');
      for (const [name, perspective] of Object.entries(item.ai_analysis.perspectives || {})) {
        const p = document.createElement('p'); p.textContent = `${name}：${perspective}`; body.append(p);
      }
      ai.append(label, body); article.append(ai);
    }
    return article;
  },

  renderNews(envelope) {
    const data = envelope.data || {};
    const status = envelope.status || data.status || 'UNKNOWN';
    this.setText('news-page-status', `状态：${status}`);
    this.setText('news-capture-time', `批次开始：${data.capture_started_at || 'UNKNOWN'}`);
    this.setText('news-hot-count', String((data.hot_news || []).length));
    this.setText('news-ordinary-count', String((data.ordinary_news || []).length));
    const hot = document.getElementById('news-hot-list');
    const ordinary = document.getElementById('news-ordinary-list');
    if (hot) hot.replaceChildren(...(data.hot_news || []).map((item) => this.renderNewsItem(item)));
    if (ordinary) ordinary.replaceChildren(...(data.ordinary_news || []).map((item) => this.renderNewsItem(item)));
    for (const node of [hot, ordinary]) {
      if (node && !node.childElementCount) {
        const empty = document.createElement('p'); empty.className = 'muted'; empty.textContent = status === 'EMPTY' ? '暂无新闻。' : '当前没有可用新闻记录。'; node.append(empty);
      }
    }
    this.setPanel('news-page-warning', this.statusClass(status), `新闻数据：${status}`, (envelope.warnings || []).join('；') || `来源：${data.source || 'UNKNOWN'}；更新时间为采集批次开始时间。`);
    this.recordOverviewEvidence('news', envelope);
    const count = Array.isArray(data.all_news) ? data.all_news.length : null;
    const current = typeof data.capture_started_at === 'string' && data.capture_started_at.slice(0, 10).replaceAll('-', '') === V7Display.today() && !['STALE', 'UNAVAILABLE', 'INVALID'].includes(status);
    this.renderHomeNews(envelope);
  },

  renderMarketUnavailable() {
    this.renderMarket({ status: 'UNAVAILABLE', warnings: ['MARKET_API_READ_FAILED'], data: {} });
  },

  renderNewsUnavailable() {
    this.setPanel('news-page-warning', 'error', '新闻数据不可用', '读取失败；不生成示例新闻。');
    this.recordOverviewEvidence('news', { status: 'UNAVAILABLE', data: null });
    this.renderHomeNews({ status: 'UNAVAILABLE', data: null });
  },

  renderCompactUnavailable(id, titleText, detailText) {
    const node = document.getElementById(id);
    if (!node) return;
    node.replaceChildren();
    const panel = document.createElement('div'); panel.className = 'state-panel error';
    const title = document.createElement('strong'); title.textContent = titleText;
    const detail = document.createElement('p'); detail.textContent = detailText;
    panel.append(title, detail); node.append(panel);
  },

  recordOverviewEvidence(key, envelope) {
    this.overviewEvidence[key] = envelope || { status: 'UNAVAILABLE', data: null };
    this.renderOverviewTechnicalEvidence();
    const limited = Object.values(this.overviewEvidence).some(item => ['DEGRADED', 'STALE', 'UNAVAILABLE', 'INVALID', 'ERROR'].includes(item?.status)
      || (Array.isArray(item?.warnings) && item.warnings.length > 0));
    const health = this.overviewEvidence.overview?.data?.system_health?.business_status;
    const forward = this.overviewEvidence.forward?.data;
    const last = forward?.recent_telemetry?.at(-1);
    const currentError = forward?.season_id === this.overviewEvidence.overview?.data?.active_season?.season_id
      && typeof last?.trade_date === 'string' && last.trade_date.replaceAll('-', '') === V7Display.today() && last.execution_status === 'ERROR';
    this.setText('overview-user-status', limited || ['DEGRADED', 'STALE', 'UNAVAILABLE'].includes(health)
      ? '部分数据需检查' : '数据状态待确认');
    if (currentError) this.setText('overview-user-status', '部分功能暂时受限');
  },

  renderOverviewTechnicalEvidence() {
    const key = document.getElementById('overview-technical-source')?.value || 'overview';
    const raw = document.getElementById('overview-technical-json');
    if (raw) raw.textContent = JSON.stringify(this.overviewEvidence[key] ?? { status: 'NOT_LOADED', data: null }, null, 2);
  },

  renderHomeMarket(data) {
    const root = document.getElementById('overview-market-summary');
    const layer = document.getElementById('overview-market-layer');
    const view = V7MarketEnvironment.eligible(data.environment);
    if (layer) layer.hidden = !view;
    if (!root) return; root.replaceChildren();
    const time = document.getElementById('overview-market-time'); if (time) time.textContent = '';
    if (!view) return;
    const grid = document.createElement('div'); grid.className = 'dashboard-market-layout';
    const indexes = document.createElement('div'); indexes.className = 'dashboard-index-grid dashboard-home-indexes';
    const rows = V7DashboardData.rows(data); const scale = V7DashboardData.commonScale(rows);
    for (const item of rows) indexes.append(this.dashboardIndexCard(item, view, true, scale));
    grid.append(this.dashboardStateCard(view, data.environment.classification), indexes);
    const dates = [...new Set(V7DashboardData.rows(data).filter(item => item.series).map(item => item.series.last.slice(0, 10)))];
    root.append(grid, this.textNode('p', (dates.length ? dates.join(' / ') + ' · ' : '') + '展示已保存行情记录，非实时行情。', 'dashboard-shared-note'), this.textNode('p', '走势相对09:30首条记录，统一比例', 'dashboard-shared-note'), this.dashboardMarketEvidence(data, view, scale));
  },

  async refreshHomeAccounts() {
    const envelope = await V7Api.getArena();
    this.recordOverviewEvidence('arena', envelope);
    this.renderHomeAccounts(envelope);
  },

  renderHomeAccounts(envelope) {
    const data = envelope?.data || {};
    const rows = Array.isArray(data.leaderboard) ? data.leaderboard : [];
    const seasonId = this.overviewEvidence.overview?.data?.active_season?.season_id;
    const current = Boolean(seasonId) && data.season_id === seasonId;
    const count = Number.isInteger(data.players_count) && data.players_count === rows.length ? data.players_count : null;
    this.setText('overview-account-count', current && count !== null ? count + ' 个模拟账户' : '账户数据待确认');
    const leaders = rows.filter(item => item.rank === 1);
    const validRanking = current && envelope?.status === 'OK' && envelope?.data_freshness?.freshness_status === 'FRESH'
      && rows.length === 6 && rows.every(item => item.data_status === 'VERIFIED' && typeof item.net_pnl_pct === 'number' && Number.isFinite(item.net_pnl_pct));
    this.setText('overview-account-leader', validRanking && leaders.length === 1 && leaders[0].name ? '当前领先：' + leaders[0].name : '当前排名待确认');
    const aiq = rows.find(item => item.player_id === 'P-AI-QUANT' || item.participant_id === 'P-AI-QUANT');
    const positions = aiq?.positions_count;
    this.setText('overview-aiq-positions', current && aiq?.data_status === 'VERIFIED' && Number.isInteger(positions) && positions >= 0 ? '当前持仓：' + positions + ' 只' : '持仓数据待确认');
  },

  renderHomeForward(data) {
    const season = this.overviewEvidence.overview?.data?.active_season?.season_id;
    const current = Boolean(season) && data.season_id === season;
    const day = data.forward_day_count;
    this.setText('overview-forward-day', current && Number.isInteger(day) && day >= 0 ? 'Day ' + day : '天数待确认');
    const records = Array.isArray(data.recent_telemetry) ? data.recent_telemetry : [];
    const latest = records[records.length - 1];
    const sameDay = typeof latest?.trade_date === 'string' && latest.trade_date.replaceAll('-', '') === V7Display.today();
    this.setText('overview-aiq-runtime', current && sameDay && latest.execution_status === 'ERROR'
      ? '运行数据需检查' : '当前运行状态待确认');
    if (current && sameDay && latest.execution_status === 'ERROR') this.setText('overview-user-status', '部分功能暂时受限');
  },

  renderOverview(data, envelope) {
    const control = data.system_control || {};
    const safety = control.safety || {};
    const forward = data.forward_epoch || {};
    const health = data.system_health || {};
    const season = data.active_season || {};
    const realLabel = V7Display.real(control.real_trading);
    this.setText('global-season', V7Display.season(season));
    this.setText('global-mode', control.execution_mode === 'PAPER_SIMULATION' ? '模拟交易' : '交易模式待确认');
    this.setText('global-real', realLabel);
    this.setText('sidebar-real-status', realLabel);
    const realNode = document.getElementById('global-real');
    if (realNode) realNode.className = 'state-pill ' + (control.real_trading === false ? 'neutral' : 'critical');
    this.setText('overview-season', (season.season_id ?? 'UNKNOWN') + ' / ' + (season.status ?? 'UNKNOWN'));
    this.setText('overview-generated', envelope.generated_at ?? 'UNKNOWN');
    this.setText('overview-forward', (forward.epoch_id ?? 'UNKNOWN') + ' / ' + (forward.status ?? 'UNKNOWN') + ' / Day ' + (forward.forward_day_count ?? 'UNKNOWN'));
    this.setText('overview-g6', control.g6_status ?? 'UNKNOWN');
    this.setText('overview-halt', String(safety.halt_status ?? control.halt_status ?? 'UNKNOWN'));
    this.setText('overview-permissions', JSON.stringify({ new_entry_disabled: safety.new_entry_disabled ?? null, trading_enabled: safety.trading_enabled ?? null, real_trading: control.real_trading ?? null, execution_state_safe: safety.execution_state_safe ?? null, execution_authorization: data.today_picks?.execution_authorized ?? null }));
    this.setText('overview-health', (health.business_status ?? 'UNKNOWN') + ' / ' + (health.daemon_liveness ?? 'UNKNOWN'));
    this.setText('global-forward', JSON.stringify(forward));
    this.setText('global-g6', control.g6_status ?? 'UNKNOWN');
    this.setText('footer-source', '状态与来源见系统详情');
    this.updatePersonalOverview();
    if (this.overviewEvidence.arena) this.renderHomeAccounts(this.overviewEvidence.arena);
    if (this.overviewEvidence.forward?.data) this.renderHomeForward(this.overviewEvidence.forward.data);
    // Forward product page is rendered only from the complete active /forward View Model.
    this.renderHealth(health, safety, control.g6_status);
  },

  renderForward(forward, seasonId, envelope = null) {
    const state = V7AiQuantPresentation.forward(forward);
    this.setPanel('forward-state', state.latest?.execution_status === 'ERROR' ? 'hold' : 'unavailable',
      state.genuine ? '真实前向观察已启动' : '真实前向观察状态待确认',
      state.genuine ? '只呈现当前真实前向记录；启动不代表已形成有效绩效。' : '以当前赛季的前向观察记录为准。');
    const summary = document.getElementById('forward-summary'); if (summary) {
      summary.replaceChildren();
      summary.append(this.textNode('strong', state.day === null ? '观察天数待确认' : 'Day ' + state.day, 'aiq-forward-day'));
      if (state.genuine && state.day === 0) summary.append(this.textNode('p', '尚无合格 Forward 交易日。'));
      summary.append(this.textNode('p', '暂无可计算的 Forward 绩效。', 'muted'));
    }
    const runtime = document.getElementById('forward-runtime-note'); if (runtime) {
      runtime.replaceChildren();
      if (state.latest?.execution_status === 'ERROR') runtime.append(this.textNode('p',
        V7AiQuantPresentation.inputError(forward) ? '当前前向执行存在数据输入问题，尚未形成有效 Forward 结果。' : '最近前向检查存在异常，请查看技术详情。'));
      if (state.latest?.timestamp) runtime.append(this.textNode('p', '最近检查：' + state.latest.timestamp, 'muted'));
    }
    const raw = document.getElementById('forward-technical-json'); if (raw) raw.textContent = JSON.stringify({
      status: envelope?.status ?? 'NOT_RETURNED', warnings: envelope?.warnings || [],
      data_freshness: envelope?.data_freshness || {}, data: forward || {}, season_id: seasonId ?? null,
      qualification_note: 'API未提供qualified_trading_dates明细；不推导样本或绩效。' }, null, 2);
  },

  renderHealth(health, safety, g6Status) {
    const node = document.getElementById('system-health-state');
    if (!node) return;
    const title = node.querySelector('strong');
    const detail = node.querySelector('p');
    const degraded = health.business_status === 'DEGRADED' || safety.status !== 'VERIFIED' || g6Status !== 'PASS';
    node.className = `state-panel ${degraded ? 'hold' : 'unavailable'}`;
    if (title) title.textContent = `业务健康：${health.business_status || 'UNKNOWN'} · 安全状态：${safety.status || 'UNAVAILABLE'}`;
    if (detail) detail.textContent = `Daemon liveness=${health.daemon_liveness || 'UNKNOWN'}；G6=${g6Status || 'UNKNOWN'}；new_entry_disabled=${safety.new_entry_disabled === true ? 'TRUE' : 'UNKNOWN'}。心跳不等同业务健康。`;
  },

  renderUnavailable() {
    this.setText('global-season', '赛季状态待确认');
    this.setText('global-mode', '交易模式待确认');
    this.setText('global-real', V7Display.real(null));
    this.setText('sidebar-real-status', V7Display.real(null));
    this.setText('overview-season', 'UNKNOWN');
    this.setText('overview-generated', this.overviewEvidence.overview?.generated_at ?? 'UNKNOWN');
    this.setText('overview-forward', 'UNKNOWN');
    this.setText('overview-g6', 'UNKNOWN');
    this.setText('overview-halt', 'UNKNOWN');
    this.setText('overview-permissions', 'UNKNOWN');
    this.setText('overview-health', 'UNAVAILABLE');
    this.setText('overview-account-count', '账户数据待确认');
    this.setText('overview-account-leader', '当前排名待确认');
    this.setText('overview-aiq-positions', '持仓数据待确认');
    this.setText('overview-aiq-runtime', '当前运行状态待确认');
    this.setText('overview-forward-day', '天数待确认');
    this.setText('overview-user-status', '部分功能暂时受限');
    const top = document.getElementById('global-real'); if (top) top.className = 'state-pill critical';
    this.setPanel('forward-state', 'error', '观察数据暂不可用', '观察状态与天数待确认。');
    this.setPanel('system-health-state', 'error', '系统状态暂不可用', '不将未知状态显示为正常。');
  },
};

window.addEventListener('DOMContentLoaded', () => V7App.init());
