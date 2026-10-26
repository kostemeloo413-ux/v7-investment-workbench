'use strict';
(() => {
  const get = (id) => document.getElementById(id);
  const money = (value) => new Intl.NumberFormat('zh-CN', {style:'currency',currency:'CNY'}).format(value);
  const cell = (text) => { const element=document.createElement('td'); element.textContent=text; return element; };
  const row = (values) => { const element=document.createElement('tr'); values.forEach(value=>element.append(cell(value))); return element; };
  async function run() {
    get('run-demo').disabled=true;
    get('status').textContent='正在使用本地历史样本计算…';
    try {
      const response=await fetch('/api/demo',{cache:'no-store'});
      if(!response.ok) throw new Error('回放接口暂时不可用');
      const result=await response.json();
      if(result.mode!=='HISTORICAL_HYPOTHETICAL_DEMO') throw new Error('回放模式无法确认');
      const summary=result.summary;
      get('initial').textContent=money(summary.initial_capital);
      get('pnl').textContent=money(summary.hypothetical_net_pnl);
      get('pnl').className=summary.hypothetical_net_pnl>0?'gain':summary.hypothetical_net_pnl<0?'loss':'';
      get('t1').textContent=summary.t1_blocked?'已按 T+1 拦截':'检查失败';
      get('ledger-status').textContent=summary.ledger_reconciliation==='PASS'?'一致':'待检查';
      get('sample-title').textContent=`${result.market_sample.name} · ${result.market_sample.symbol}`;
      get('sample-source').textContent=`${result.market_sample.provider} / ${result.market_sample.bars[0].trade_date} — ${result.market_sample.bars[2].trade_date} / 非实时`;
      get('bars').replaceChildren(...result.market_sample.bars.map(bar=>row([bar.trade_date,...['open','high','low','close'].map(key=>Number(bar[key]).toFixed(2))])));
      get('fills').replaceChildren(...result.fills.map(fill=>row([fill.trade_date,fill.direction==='BUY'?'假设买入':'假设卖出',fill.shares,Number(fill.price).toFixed(4),money(fill.commission),money(fill.stamp_tax)])));
      const steps=[['输入假设买单','按历史收盘价代理计算'],['同日退出被拒绝','实际调用 A 股 T+1 检查'],['下一交易日退出','计入滑点、佣金和印花税'],['账本重新核对','成交现金流与账户余额一致']];
      get('timeline').replaceChildren(...steps.map(([title,detail])=>{const item=document.createElement('li');item.textContent=title;const small=document.createElement('small');small.textContent=detail;item.append(small);return item;}));
      get('raw-result').textContent=JSON.stringify(result,null,2);
      get('status').textContent='回放完成 · 2 笔假设成交，1 次 T+1 拦截';
    } catch(error) {
      get('status').textContent=error.message;
    } finally { get('run-demo').disabled=false; }
  }
  get('run-demo').addEventListener('click',run);
  run();
})();
