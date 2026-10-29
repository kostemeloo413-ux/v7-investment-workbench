/* Pure display projection. No quote collection, recommendations or permissions. */
(function(root) {
  'use strict';
  const roles = [['P-LIAO-XIAOAI','廖小爱'],['P-COOL','酷儿'],['P-YINWENTAO','殷文涛'],['P-HANSHU','韩叔'],['P-RULE-QUANT','规则量化']];
  const finite = x => typeof x === 'number' && Number.isFinite(x);
  const code = x => typeof x === 'string' ? x.replace(/^(sh|sz|bj)\.?/i,'').replace(/\.(sh|sz|bj)$/i,'') : '';
  const day = x => typeof x === 'string' && Number.isFinite(Date.parse(x)) ? new Intl.DateTimeFormat('en-CA',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(x)).replaceAll('-','') : null;
  const bad = x => x?.stale === true || x?.error || x?.error_message || ['STALE','ERROR','INVALID','UNAVAILABLE','EMPTY'].includes(x?.status);
  function project(envelope, today, now, isCurrent) {
    const data = envelope?.data;
    if (!data || bad(envelope) || data.scan_date_status !== 'CURRENT_TRADING_DATE' || data.trade_date?.replaceAll?.('-','') !== today
      || day(data.as_of) !== today || Date.parse(data.as_of) > now || data.execution_authorized !== false
      || ['STALE','ERROR','INVALID','UNAVAILABLE','EMPTY'].includes(data.data_status)) return {items:[],status:'UNAVAILABLE',asOf:null};
    const map = new Map();
    function add(symbol,name,source,record) {
      const normalized = code(symbol);
      if (!/^\d{6}$/.test(normalized) || !/^(00|60)/.test(normalized) || typeof name !== 'string' || !name.trim()) return;
      if (!map.has(normalized)) map.set(normalized,{code:normalized,name,sources:[],samples:[]});
      const item = map.get(normalized);
      if (item.name !== name) item.conflict = true;
      item.sources.push(source); if(record) item.samples.push(record);
    }
    for (const [id,label] of roles) {
      const matches = (data.participants || []).filter(p => p?.participant_id === id);
      if (matches.length !== 1) continue;
      const p = matches[0];
      if (!isCurrent(data,p,today,now)) continue;
      const ev = p.scan_evidence || {}, sr = ev.source_record || {}, refs = ev.evidence_refs || [];
      const symbol = code(p.symbol), value = p.candidate_evidence?.change_pct;
      const time = ev.scan_at;
      const verifiedRecord = ev.status === 'RECORDED_CURRENT_SCAN' && ev.identity_status === 'MATCH'
        && !bad(ev) && !bad(sr) && day(time) === today && Date.parse(time) <= now
        && sr.scan_timestamp === time && typeof sr.source === 'string' && sr.source.trim()
        && typeof ev.source_name === 'string' && ev.source_name.trim()
        && Array.isArray(refs) && refs.includes('tencent_quote:'+symbol+':'+time)
        && finite(value);
      add(p.symbol,p.stock_name,{label,id,action:p.action,reason:p.decision_reason,scanAt:p.scan_timestamp,
        paused:['P-COOL','P-HANSHU'].includes(id),kind:'STRATEGY'},verifiedRecord ? {value,time,source:ev.source_name} : null);
    }
    const aiq = data.ai_quant_top5, predictions = aiq?.predictions;
    if (!bad(aiq) && aiq?.status === 'NORMAL' && aiq.classification === 'CURRENT_DAY_TOP5' && Array.isArray(predictions)
      && predictions.length === 5 && aiq.pulse?.row_count === 5 && aiq.pulse?.trade_date?.replaceAll?.('-','') === today
      && new Set(predictions.map(p=>code(p.symbol))).size === 5 && new Set(predictions.map(p=>p.rank)).size === 5
      && predictions.every(p=>p.trade_date?.replaceAll?.('-','') === today && /^(00|60)\d{4}$/.test(code(p.symbol))
        && typeof p.name === 'string' && p.name.trim() && !/^(UNKNOWN|UNAVAILABLE|EMPTY)$/.test(p.name.trim())
        && Number.isInteger(p.rank) && p.rank>=1 && p.rank<=5
        && p.predict_time === aiq.pulse.predict_time && p.model_version === aiq.pulse.model_version
        && typeof p.predict_time === 'string' && /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(p.predict_time)
        && day(p.predict_time.replace(' ','T')+'+08:00') === today && Date.parse(p.predict_time.replace(' ','T')+'+08:00') <= now)) {
      [...predictions].sort((a,b)=>a.rank-b.rank).forEach(p=>add(p.symbol,p.name,{label:'AIQ #'+p.rank,id:'P-AI-QUANT',kind:'PREDICTION',scanAt:p.predict_time,action:'MODEL_CANDIDATE',reason:'模型候选，不代表已成交。'},null));
    }
    const items = [...map.values()].map(item=>{
      const values = new Set(item.samples.map(s=>s.value));
      const known = !item.conflict && values.size === 1;
      return {...item,change:known ? item.samples[0].value : null,quoteStatus:known?'RECORDED_SCAN':item.conflict || values.size>1?'CONFLICT':'UNKNOWN'};
    });
    return {items,status:items.length?'AVAILABLE':'EMPTY',asOf:data.as_of};
  }
  root.V7CandidateHeatgrid = {project};
  if(typeof module !== 'undefined') module.exports = {project};
})(typeof window !== 'undefined' ? window : globalThis);
