/* Arena only: read-only formal ledger details. No economic commands. */
(() => {
  const app = V7App;
  const state = { selected: null, tab: 'positions', envelope: null, busy: false, lastSuccess: null, generation: 0 };
  // Context-specific display mappings; raw evidence remains in technical details.
  const display = {
    accountStatus: value => ({ACTIVE:'运行中', HOLD:'当前受限', STRATEGY_DISABLED:'策略暂未启用', UNKNOWN:'状态待确认'}[value] || '状态待确认'),
    accountData: value => ({VERIFIED:'账户数据已核对', STALE:'账户数据已过期', UNKNOWN:'数据状态待确认'}[value] || '数据状态待确认'),
    projection: value => ({MATCH:'竞技场汇总一致', LAGGING:'竞技场汇总稍有延迟', UNKNOWN:'汇总核对待确认'}[value] || '汇总核对待确认'),
    quote: p => !p.price_source_time || p.price_time_status !== 'RECORDED' ? '报价时间待确认'
      : ({FRESH:'报价较新', STALE:'报价已过期', UNKNOWN:'报价时间待确认'}[p.quote_freshness] || '报价时间待确认'),
    ledgerLabel: value => ({RECORDED_UPDATED_AT:'账本更新时间', FILE_MODIFIED_AT:'账本文件更新时间'}[value] || '账本时间口径待确认'),
    snapshotLabel: value => ({SNAPSHOT_UPDATED_AT:'账户数据截至', FILE_MODIFIED_AT:'数据文件更新时间'}[value] || '账户数据时间口径待确认')
  };
  const node = (tag, text, cls) => app.textNode(tag, text == null ? '—' : String(text), cls);
  const finite = value => typeof value === 'number' && Number.isFinite(value);
  const format = value => finite(value) ? value.toLocaleString('zh-CN', {maximumFractionDigits:4}) : '—';
  const money = value => finite(value) ? '¥' + value.toLocaleString('zh-CN', {minimumFractionDigits:2,maximumFractionDigits:2}) : '—';
  const percent = value => finite(value) ? value.toLocaleString('zh-CN', {maximumFractionDigits:4}) + '%' : '—';
  const known = (value, fallback='—') => value == null ? fallback : value;
  const time = value => value && !['UNKNOWN','UNAVAILABLE'].includes(value) ? String(value).replace(/^(\d{4})(\d{2})(\d{2})$/, '$1-$2-$3').replace('T',' ').replace(/\.\d+(?=\+|$)/,'') : '时间待确认';
  const signed = (element, value) => { if (typeof value === 'number' && Number.isFinite(value) && value!==0) element.classList.add(value>0?'arena-gain':'arena-loss'); };
  const button = (text, action, cls='secondary-button') => { const b=node('button',text,cls); if(text==='')b.textContent=''; b.type='button'; b.addEventListener('click',action); return b; };

  let dialogInitialized = false;
  let previousBodyOverflow = '';
  function closeDetailDialog() {
    const dialog = document.getElementById('arena-detail-dialog');
    if (dialog?.open) dialog.close();
  }
  function openDetailDialog() {
    const dialog = document.getElementById('arena-detail-dialog');
    if (!dialog || dialog.open) return;
    if (!dialogInitialized) {
      dialogInitialized = true;
      document.getElementById('arena-dialog-close')?.addEventListener('click', closeDetailDialog);
      dialog.addEventListener('cancel', event => { event.preventDefault(); closeDetailDialog(); });
      dialog.addEventListener('close', () => {
        document.body.style.overflow = previousBodyOverflow;
        Array.from(document.querySelectorAll('#arena-grid .arena-account')).find(card => card.dataset.playerId === state.selected)?.focus();
      });
      window.addEventListener('hashchange', () => { if (location.hash !== '#page-arena') closeDetailDialog(); });
    }
    previousBodyOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    dialog.showModal();
    dialog.querySelector('.arena-dialog-content').scrollTop = 0;
  }

  async function read(url) {
    try {
      const endpoint = V7Api.baseUrl === '/api/ui-demo' ? url.replace(/^\/api\/v7(?=\/)/, '/api/ui-demo') : url;
      const res = await fetch(endpoint, {method:'GET', cache:'no-store', headers:{Accept:'application/json'}, signal:AbortSignal.timeout(12000)});
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return await res.json();
    } catch (err) { return {status:'UNAVAILABLE', data:null, warnings:[err.message], data_freshness:{stale:true}}; }
  }
  function table(headers, rows, empty) {
    if (!rows.length) return node('p',empty,'arena-empty');
    const wrap=document.createElement('div'); wrap.className='arena-table-scroll'; wrap.tabIndex=0;
    const t=document.createElement('table'); t.className='arena-table';
    const head=document.createElement('thead'), hr=document.createElement('tr');
    headers.forEach(v=>hr.append(node('th',v))); head.append(hr); t.append(head);
    const body=document.createElement('tbody');
    rows.forEach(cells=>{const tr=document.createElement('tr'); cells.forEach(v=>{const td=document.createElement('td'); td.append(v instanceof Node ? v : document.createTextNode(v == null ? '—' : String(v))); tr.append(td);}); body.append(tr);});
    t.append(body); wrap.append(t); return wrap;
  }
  function positions(player, root) {
    if (!Array.isArray(player.positions) || !finite(player.positions_count)) {
      root.append(node('p','持仓情况待确认。','arena-empty')); return;
    }
    const rows=player.positions.map(p=>{
      const stock=document.createElement('div'); stock.append(node('strong',p.name || '名称未记录'),node('small',p.symbol,'muted'));
      const price=document.createElement('div'); price.append(node('span',format(p.current_price)));
      const hasQuoteTime = !!p.price_source_time && p.price_time_status === 'RECORDED';
      price.append(node('small',hasQuoteTime ? '报价时间：' + time(p.price_source_time) : '报价时间待确认','muted'));
      if(hasQuoteTime) price.append(node('small',display.quote(p),'muted'));
      const pnl=node('span',money(p.unrealized_pnl)); signed(pnl,p.unrealized_pnl);
      const pct=node('span',percent(p.unrealized_pnl_pct)); signed(pct,p.unrealized_pnl_pct);
      return [stock,format(p.shares),time(p.buy_date),format(p.cost_price),price,money(p.position_value),pnl,pct];
    });
    if (!rows.length) {
      const verifiedEmpty = player.positions_count === 0 && player.data_status === 'VERIFIED' && player.stale !== true;
      root.append(node('p',verifiedEmpty ? '当前空仓' : player.positions_count === 0 && player.data_status === 'STALE'
        ? '已保存账本为空仓，账户数据已过期。' : '持仓情况待确认。','arena-empty')); return;
    }
    root.append(node('p','价格为账本保存的估值，可能不同于最新行情。','muted'));
    root.append(table(['证券','股数','建仓日期','成本价','最近账本估值','持仓市值','浮动盈亏','收益率'],rows,'持仓情况待确认。'));
  }
  function history(player, root) {
    if (!Array.isArray(player.transactions) || !finite(player.transactions_total)) { root.append(node('p','交易记录待确认。','arena-empty')); return; }
    const records=player.transactions.filter(r=>r.evidence_kind==='FILL' && r.execution_status==='FILLED' && ['BUY','SELL'].includes(r.direction));
    if (!records.length && player.transactions_total === 0) {
      root.append(node('p','暂无成交记录','arena-empty')); return;
    }
    root.append(node('p','已显示 '+records.length+' / '+known(player.transactions_total)+' 条成交记录 · 最新在前。','muted'));
    const rows=records.map(r=>{
      const stock=document.createElement('div'); stock.append(node('strong',r.name || '名称未记录'),node('small',r.symbol,'muted'));
      const direction=node('span',({BUY:'买入',SELL:'卖出'}[r.direction] || '方向待确认')); direction.className=r.direction==='BUY'?'arena-gain':r.direction==='SELL'?'arena-loss':'';
      const reason=document.createElement('div'); reason.className='arena-reason'; reason.append(node('span',r.reason || '未记录交易原因'));
      const detail=document.createElement('details'); detail.className='arena-transaction-details';
      detail.append(node('summary','查看交易技术信息'),node('pre',JSON.stringify(r,null,2),'technical-json')); reason.append(detail);
      return [direction,stock,time(r.timestamp),format(r.shares),format(r.price),money(r.fees),'已成交',reason];
    });
    root.append(table(['方向','证券','时间','成交股数','成交价','交易费用','状态','交易原因'],rows,'交易记录待确认。'));
    if (records.length < player.transactions_total) {
      const more=button('查看更多成交记录',async()=>{
        more.disabled=true; const id=player.player_id; const gen=state.generation;
        const response=await read('/api/v7/arena/'+encodeURIComponent(id)+'?offset='+records.length+'&limit=20');
        if(gen!==state.generation || state.selected!==id) return;
        if(response.data && Array.isArray(response.data.transactions)) {
          player.transactions=records.concat(response.data.transactions); player.transactions_total=response.data.transactions_total;
          renderDetail(player);
        } else { more.disabled=false; root.append(node('p','历史加载失败，保留已读取记录。','arena-empty')); }
      }); root.append(more);
    }
  }
  function renderDetail(player) {
    const root=document.getElementById('arena-detail'); if(!root) return;
    const tabHadFocus = root.contains(document.activeElement) && document.activeElement.getAttribute('role') === 'tab';
    root.replaceChildren();
    root.dataset.playerId=player.player_id;
    root.append(node('h2',player.name),node('p',(player.player_id==='P-AI-QUANT'?'AI Quant 模拟账户':'模拟账户')+' · '+display.accountStatus(player.status)+' · '+display.accountData(player.data_status),'muted'));
    const facts=document.createElement('div');
    const fields=['initial_capital','cash','market_value','total_equity','net_pnl','net_pnl_pct','max_drawdown_pct','orders_count','fills_count'];
    app.appendFactList(facts,[['初始资金',money(player.initial_capital)],['当前现金',money(player.cash)],['持仓市值',money(player.market_value)],
      ['当前总资产',money(player.total_equity)],['累计赚/亏',money(player.net_pnl)],['累计收益率',percent(player.net_pnl_pct)],
      ['最大回撤',percent(player.max_drawdown_pct)],['交易指令',known(player.orders_count,'交易指令数据待确认')],['已成交',known(player.fills_count,'已成交数据待确认')]]);
    facts.className='arena-summary';
    Array.from(facts.querySelector('dl').children).forEach((row,i)=>{row.dataset.arenaField=fields[i];if(['net_pnl','net_pnl_pct'].includes(fields[i]))signed(row.querySelector('dd'),player[fields[i]]);});
    root.append(facts);
    root.append(node('p',display.ledgerLabel(player.ledger_time_kind)+'：'+time(player.ledger_updated_at)+' · '+display.projection(player.projection_status),'muted'));
    const tabs=document.createElement('div'); tabs.className='arena-tabs'; tabs.setAttribute('role','tablist');
    ['positions','transactions'].forEach(key=>{const b=button(key==='positions'?'当前持仓':'交易记录',()=>{state.tab=key;renderDetail(player);},'arena-tab');
      b.setAttribute('role','tab'); b.setAttribute('aria-selected',String(state.tab===key));tabs.append(b);}); root.append(tabs);
    const content=document.createElement('div'); content.setAttribute('role','tabpanel'); root.append(content);
    if(state.tab==='positions') positions(player,content); else history(player,content);
    const technical=document.createElement('details'); technical.className='technical-details arena-technical';
    const envelope=state.envelope || {};
    const raw={account:player,arena:{season_id:envelope.data?.season_id,primary_metric:envelope.data?.primary_metric,
      status:envelope.status,generated_at:envelope.generated_at,data_freshness:envelope.data_freshness,warnings:envelope.warnings,
      governance:envelope.data?.governance,comparability_warning:envelope.data?.comparability_warning,
      source_evidence:envelope.data?.source_evidence,sentinel:envelope.data?.sentinel}};
    technical.append(node('summary','查看账户技术信息'),node('p','模拟滑点已计入成交价，不重复计入交易费用。','muted'),
      node('pre',JSON.stringify(raw,null,2),'technical-json')); root.append(technical);
    if (tabHadFocus) tabs.querySelector('[aria-selected="true"]')?.focus();
  }
  app.refreshArena = async function() {
    if(state.busy) return; state.busy=true;
    const response=await read('/api/v7/arena'); state.busy=false;
    if(!response.data || response.data.view_version!=='arena-ledger-view-v1') {
      state.generation++; state.envelope=null;
      this.setText('arena-status','数据状态待确认');
      this.setPanel('arena-warning','error','账户数据暂时读取失败。','已同步时间保留，当前资金和持仓情况不能确认。');
      const warning=document.getElementById('arena-warning');if(warning)warning.hidden=false;
      const grid=document.getElementById('arena-grid'), detail=document.getElementById('arena-detail');
      if(grid)grid.replaceChildren();
      if(detail) {
        detail.replaceChildren(node('p','账户详情暂无法确认。'));
        const technical=document.createElement('details'); technical.className='technical-details arena-technical';
        technical.append(node('summary','查看读取技术信息'),node('pre',JSON.stringify(response,null,2),'technical-json'));
        detail.append(technical);
      }
      this.setText('arena-sync',`页面同步：${time(state.lastSuccess)} · 当前读取失败`); return;
    }
    state.generation++; state.envelope=response; state.lastSuccess=new Date().toLocaleTimeString('zh-CN',{hour12:false}); this.renderArena(response);
  };
  app.renderArena = function(envelope) {
    const data=envelope.data; const freshness=envelope.data_freshness || {};
    this.setText('overview-arena-summary',`${known(data.players_count)} 个收益账户快照 · ¥100,000 是每账户初始本金合同，不是当前收益；Sentinel 排除。`);
    this.setText('arena-status',({FRESH:'账户数据已同步',STALE:'账户数据已过期',UNKNOWN:'账户数据时间待确认'}[freshness.freshness_status] || '数据状态待确认'));
    this.setText('arena-season',finite(data.players_count)?data.players_count+' 个模拟账户':'账户数量待确认');
    this.setText('arena-sync','页面同步：'+time(state.lastSuccess)+' · '+display.snapshotLabel(freshness.source_time_kind)+'：'+time(freshness.source_updated_at));
    const needsAttention = freshness.stale === true || envelope.status !== 'OK' || (envelope.warnings || []).length > 0;
    const health=data.execution_health || {}, aiq=health.aiq || {};
    const entryMessage=health.new_entry_data_status==='HOLD'?'新开仓等待合格日线数据。':'';
    const exitMessage=aiq.exit_check_status==='CHECKED'?'AIQ 最近已检查持仓退出。':aiq.exit_check_status==='PARTIAL_MISSING_QUOTES'?'AIQ 部分持仓报价缺失，等待有效报价。':'AIQ 退出检查状态待确认。';
    this.setPanel('arena-warning',freshness.stale?'hold':this.statusClass(envelope.status),freshness.freshness_status==='STALE'?'账户数据已过期':'部分账户数据需检查',
      entryMessage+exitMessage+' 持仓估值请结合报价时间查看。');
    const warning=document.getElementById('arena-warning');if(warning)warning.hidden=!needsAttention;
    const grid=document.getElementById('arena-grid'); grid.replaceChildren();
    const players=data.leaderboard || []; if(!players.some(p=>p.player_id===state.selected)) state.selected=players.length ? players[0].player_id : null;
    for(const p of players) {
      const b=button('',()=>{state.selected=p.player_id;state.tab='positions';this.renderArena(state.envelope);openDetailDialog();},'card arena-account');
      b.dataset.playerId=p.player_id;
      b.setAttribute('aria-haspopup','dialog'); b.setAttribute('aria-controls','arena-detail-dialog');
      b.setAttribute('aria-label','查看'+p.name+'账户'); b.setAttribute('aria-pressed',String(state.selected===p.player_id));
      b.append(node('strong',known(p.rank,'—')+' · '+p.name));
      const ret=node('span',percent(p.net_pnl_pct),'arena-return'); signed(ret,p.net_pnl_pct); b.append(ret);
      b.append(node('small','累计收益率','muted'),node('span','当前总资产 '+money(p.total_equity)),
        node('span',p.positions_count===0 && p.data_status==='VERIFIED' && p.stale!==true?'当前空仓':finite(p.positions_count)?'持仓 '+format(p.positions_count)+' 只':'持仓情况待确认'),
        node('small',display.accountStatus(p.status)+' · '+display.accountData(p.data_status),'muted')); grid.append(b);
    }
    const selected=players.find(p=>p.player_id===state.selected); if(selected) renderDetail(selected);
  };
  const refresh=document.getElementById('arena-refresh'); if(refresh)refresh.addEventListener('click',()=>app.refreshArena());
  window.setInterval(()=>{const page=document.getElementById('page-arena');if(page && page.classList.contains('active'))app.refreshArena();},30000);
})();
