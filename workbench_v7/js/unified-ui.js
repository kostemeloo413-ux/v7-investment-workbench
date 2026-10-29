/* Unified V7 presentation. Existing view models and business rules remain authoritative. */
(function() {
  'use strict';
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const finite = x => typeof x === 'number' && Number.isFinite(x);
  const number = (n,d=2) => finite(n)?n.toLocaleString('zh-CN',{minimumFractionDigits:d,maximumFractionDigits:d}):'待确认';
  const pct = n => finite(n)?(n>0?'+':'')+n.toFixed(2)+'%':'涨跌待确认';
  const tone = n => finite(n)&&n>0?'up':finite(n)&&n<0?'down':'neutral';
  const clock = s => typeof s==='string' && /^\d{2}:\d{2}$/.test(s) ? s : typeof s==='string' && s.length>=16 ? s.slice(11,16) : '时间待确认';
  let chartMode = 'recorded', pendingAccount = null, selectedIndex = 'sh000001', view = {items:[],status:'UNAVAILABLE'}, chartSeries = null, queued = false, focusFrom = null;
  const app = V7App;

  function drawChart() {
    const host=$('unified-chart'); if(!host)return;
    const hadFocus=host.contains(document.activeElement),series=chartSeries;
    if(!series||series.segments[0]?.[0]?.minute!==570){
      host.innerHTML='<div class="chart-empty"><span>分时记录待确认</span><small>有完整来源与日期后显示走势</small></div>';
      $('chart-session-note').textContent='记录时段待确认';return;
    }
    const width=Math.max(260,host.clientWidth),height=host.clientHeight||238,top=14,bottom=30;
    const timeline=V7ChartMath.timeline(series,width,chartMode,48,12,10);
    if(!timeline)return;
    const {x,ticks}=timeline,points=series.segments.flat(),first=points[0].value;
    const values=points.map(p=>(p.value/first-1)*100);
    let lo=Math.min(0,...values),hi=Math.max(0,...values),pad=Math.max((hi-lo)*.12,.04);lo-=pad;hi+=pad;
    const y=value=>top+(hi-value)/(hi-lo)*(height-top-bottom);
    const grid=[0,1,2,3].map(i=>{const v=hi-(hi-lo)*i/3,yy=y(v);return '<line class="chart-grid" x1="48" x2="'+(width-12)+'" y1="'+yy+'" y2="'+yy+'"/><text class="chart-label" text-anchor="end" x="40" y="'+(yy+4)+'">'+V7ChartMath.axisLabel(v,hi-lo)+'</text>';}).join('');
    const paths=series.segments.map(segment=>'<path class="chart-path" d="'+segment.map((p,i)=>(i?'L':'M')+x(p.minute).toFixed(2)+' '+y((p.value/first-1)*100).toFixed(2)).join(' ')+'"/>').join('');
    const labels=ticks.map(m=>'<text class="chart-label" text-anchor="'+(m===570?'start':m===ticks.at(-1)?'end':'middle')+'" x="'+x(m)+'" y="'+(height-8)+'">'+String(Math.floor(m/60)).padStart(2,'0')+':'+String(m%60).padStart(2,'0')+'</text>').join('');
    const last=points.at(-1),change=(last.value/first-1)*100,cx=x(last.minute),cy=y(change),polarity=tone(change);
    const discontinuity=timeline.split?'<path class="chart-break" d="M'+(timeline.breakX-4)+' '+(height-bottom+8)+'l4 -8m0 8l4 -8"/>':'';
    const name=V7DashboardData.indexes.find(r=>r[0]===selectedIndex)?.[2]||selectedIndex;
    host.innerHTML='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 '+width+' '+height+'" role="img" tabindex="0" aria-label="'+esc(name)+'，'+(chartMode==='full'?'完整交易日':'已记录时段')+'，相对09:30首条记录，截至'+clock(series.last)+'。左右方向键逐点读取。">'+grid+paths+labels+discontinuity+
      '<circle class="chart-end '+polarity+'" cx="'+cx+'" cy="'+cy+'" r="3"/><text class="chart-end-label '+polarity+'" x="'+Math.min(width-12,cx+3)+'" y="'+Math.max(14,cy-9)+'" text-anchor="end">'+pct(change)+'</text><line class="chart-cursor" visibility="hidden" y1="'+top+'" y2="'+(height-bottom)+'"/><circle class="chart-end chart-hover" visibility="hidden" r="3"/></svg>';
    const now=new Intl.DateTimeFormat('en-GB',{timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).format(new Date()).split(':').map(Number),minute=now[0]*60+now[1];
    const lunch=minute>=690&&minute<780;
    $('chart-session-note').textContent=(timeline.split?'午休 11:30 // 13:00 已折叠':lunch?'午休中，13:00复市':'显示09:30—'+clock(series.last))+(chartMode==='recorded'?'，仅已记录时段':'，全日窗口');
    const svg=host.querySelector('svg'),cursor=svg.querySelector('.chart-cursor'),dot=svg.querySelector('.chart-hover');let at=points.length-1;
    function read(i){at=Math.max(0,Math.min(points.length-1,i));const p=points[at],xx=x(p.minute),yy=y((p.value/first-1)*100);cursor.setAttribute('visibility','visible');cursor.setAttribute('x1',xx);cursor.setAttribute('x2',xx);dot.setAttribute('visibility','visible');dot.setAttribute('cx',xx);dot.setAttribute('cy',yy);$('chart-readout').textContent=clock(p.bar_at)+'  '+number(p.value)+'  较09:30 '+pct((p.value/first-1)*100);}
    svg.addEventListener('pointermove',e=>{const rect=svg.getBoundingClientRect(),pos=(e.clientX-rect.left)/rect.width*width;let best=0;points.forEach((p,i)=>{if(Math.abs(x(p.minute)-pos)<Math.abs(x(points[best].minute)-pos))best=i;});read(best);});
    svg.addEventListener('pointerleave',()=>{cursor.setAttribute('visibility','hidden');dot.setAttribute('visibility','hidden');$('chart-readout').textContent='分时记录末点 '+clock(series.last)+'，非实时行情';});
    svg.addEventListener('keydown',e=>{if(['ArrowLeft','ArrowRight','Home','End'].includes(e.key)){e.preventDefault();read(e.key==='Home'?0:e.key==='End'?points.length-1:at+(e.key==='ArrowRight'?1:-1));}});
    if(hadFocus)svg.focus({preventScroll:true});
  }

  function renderMarket() {
    const data=app.overviewEvidence.market?.data,environment=V7MarketEnvironment.eligible(data?.environment);
    $('insight-environment').textContent=environment?.label || '待确认';
    document.querySelector('.insight-signal').dataset.tone=V7ChartMath.environmentTone(environment?data.environment:null);
    $('insight-environment-note').textContent=environment?'规则记录 '+clock(environment.time || data.environment.observed_at)+'，'+environment.description:'市场判断等待合格证据';
    const rows=V7DashboardData.rows(data);
    $('unified-index-tabs').innerHTML=rows.map(item=>{const s=item.series,first=s?.segments[0]?.[0],relative=first?.minute===570?(s.lastValue/first.value-1)*100:null;return '<button class="index-tab" role="tab" aria-selected="'+(item.symbol===selectedIndex)+'" data-index="'+item.symbol+'"><span>'+esc(item.name)+'</span><strong>'+number(s?.lastValue)+'</strong><small class="delta-'+tone(relative)+'">'+(finite(relative)?pct(relative)+'<span class="index-baseline">较09:30</span>':'分时待确认')+'</small></button>';}).join('');
    const selected=rows.find(r=>r.symbol===selectedIndex);chartSeries=selected?.series;
    $('market-record-time').textContent=chartSeries?chartSeries.date.replace(/^(\d{4})(\d{2})(\d{2})$/,'$1-$2-$3')+' 保存分时':'保存分时';
    $('chart-readout').textContent=chartSeries?'分时记录末点 '+clock(chartSeries.last)+'，非实时行情':'等待可靠记录';drawChart();
  }

  function renderCandidates() {
    view=V7CandidateHeatgrid.project(app.overviewEvidence.picks,V7Display.today(),Date.now(),(...args)=>V7PicksPresentation.current(...args));
    const opinions=V7DashboardData.opinions(app.overviewEvidence.picks);
    $('insight-opinions').textContent=opinions?opinions.buy+' 考虑买入 / '+opinions.observe+' 观察':'观点待确认';
    $('insight-opinions-note').textContent='观点记录，非交易授权';
    $('opinion-cells').innerHTML=V7PicksPresentation.roles.map(([id,label])=>{const role=opinions?.roles.find(p=>p.participant_id===id),paused=['P-COOL','P-HANSHU'].includes(id);return '<button type="button" class="opinion-cell" data-persona="'+id+'" data-action="'+esc(role?.action||'UNKNOWN')+'" aria-label="'+esc(label+'，'+(role?V7PicksPresentation.action(role.action):'观点待确认')+(paused?'，账户暂停':''))+'"><strong>'+esc(label==='规则量化派'?'规则量化':label)+'</strong><span>'+(role?role.action==='BUY'?'考虑买入':'观察':'待确认')+'</span>'+(paused?'<small>暂停</small>':'')+'</button>';}).join('');
    $('heat-batch').textContent=view.asOf?clock(view.asOf)+' 批次':'当日批次待确认';
    $('heat-count').textContent=view.items.length?'去重 '+view.items.length+' 只':'暂无合格候选';
    $('unified-heat-grid').innerHTML=view.items.length?view.items.slice(0,10).map(item=>{
      const first=item.sources[0],sourceText=item.sources.map(x=>x.label+(x.paused?'（暂停观察）':'')).join(' / ');
      const foot='<span class="heat-source" title="'+esc(sourceText)+'"><span class="heat-source-label">'+esc(first.label)+'</span>'+(first.paused?'<span class="heat-source-paused">暂停</span>':'')+(item.sources.length>1?'<span class="heat-source-more">+'+(item.sources.length-1)+'</span>':'')+(!finite(item.change)?'<span>待确认</span>':'')+'</span>';
      return '<button type="button" class="heat-cell" data-stock="'+item.code+'" data-tone="'+tone(item.change)+'" data-strength="'+(Math.abs(item.change)>=5?'high':Math.abs(item.change)>=2?'mid':'low')+'" aria-label="'+esc(item.name+' '+item.code+'，'+(finite(item.change)?'扫描记录涨跌 '+pct(item.change):'涨跌待确认')+'，'+sourceText+'，查看候选来源')+'"><span class="heat-name">'+esc(item.name)+'<span class="heat-code">'+item.code+'</span></span><strong class="heat-value '+(finite(item.change)?'delta-'+tone(item.change):'unknown')+'">'+(finite(item.change)?pct(item.change):'—')+'</strong>'+foot+'</button>';
    }).join(''):'<div class="chart-empty" style="grid-column:1/-1;min-height:240px"><span>当日候选待确认</span><small>不会以历史候选补齐</small></div>';
    $('heat-quality').textContent='扫描记录涨跌，非实时行情';
    if(app.pickFocusReturn?.dataset?.persona){const id=app.pickFocusReturn.dataset.persona;app.pickFocusReturn=document.querySelector('[data-persona="'+id+'"]')||app.pickFocusReturn;}
    if(focusFrom?.dataset?.stock){const code=focusFrom.dataset.stock;focusFrom=document.querySelector('[data-stock="'+code+'"]')||focusFrom;}
  }

  function renderLower() {
    const envelope=app.overviewEvidence.arena,data=envelope?.data,season=app.overviewEvidence.overview?.data?.active_season?.season_id;
    const players=season&&data?.season_id===season&&Array.isArray(data.leaderboard)?data.leaderboard:[];
    const valid=players.length>0&&data.players_count===players.length,max=Math.max(1,...players.filter(p=>finite(p.net_pnl_pct)).map(p=>Math.abs(p.net_pnl_pct)));
    const old=players.filter(p=>p.stale===true||p.data_status==='STALE').length;
    $('unified-account-list').innerHTML=valid?players.map(p=>{const value=p.net_pnl_pct,size=finite(value)?Math.abs(value)/max*48:0;return '<button type="button" class="home-account-row" data-home-account="'+esc(p.player_id)+'" aria-label="'+esc(p.name+'，账本累计收益率'+(finite(value)?pct(value):'待确认')+'，'+(p.stale?'估值过期':'数据状态'+p.data_status)+'，查看账户')+'"><span>'+esc(p.name==='规则量化派'?'规则量化':p.name)+'</span><span class="home-account-bar" aria-hidden="true">'+(finite(value)?'<span class="home-account-fill '+tone(value)+'" style="width:'+size+'%;left:'+(value<0?50-size:50)+'%"></span>':'')+'</span><span class="home-account-value delta-'+tone(value)+'">'+(finite(value)?pct(value):'待确认')+'</span></button>';}).join(''):'<p class="muted">模拟账户数据待确认</p>';
    $('home-account-caption').textContent=valid?'账本记录'+(old?'，'+old+'个账户估值待更新':'，估值状态以账户详情为准'):'等待当前赛季账户证据';
    const news=V7DashboardData.news(app.overviewEvidence.news);
    $('unified-news').innerHTML=news.length?news.map(n=>'<a class="home-news-row" href="'+esc(n.url)+'" target="_blank" rel="noopener noreferrer"><time>'+clock(n.published_at)+'</time><span><strong>'+esc(n.title)+'</strong><small>'+esc(n.source)+'，来源记录时间</small></span></a>').join(''):'<p class="muted">暂无合格新闻记录</p>';
    const health=app.overviewEvidence.overview?.data?.system_health,business=health?.business_status;
    const unknown=view.items.filter(item=>!finite(item.change)).length,hasCandidates=view.items.length>0;
    const riskRows=[
      {label:'账户估值',short:old?'估值待更新':valid?'账本状态':'估值待确认',value:valid?old?old+'个账户待更新':'以保存账本为准':'账户数据待确认',detail:'价格与收益以保存的估值时点为准。',attention:old>0},
      {label:'候选报价',short:unknown?'报价待确认':hasCandidates?'扫描记录':'报价待确认',value:hasCandidates?unknown?unknown+'只涨跌待确认':'扫描记录可追溯':'当日候选待确认',detail:'扫描记录非实时行情，模型预测不作为实际涨跌。',attention:hasCandidates&&unknown>0},
      {label:'业务状态',short:business&&!['OK','NORMAL'].includes(business)?'业务待检查':business?'业务状态':'业务待确认',value:business?['OK','NORMAL'].includes(business)?'按原始业务状态查看':'仍有待检查项':'业务状态待确认',detail:'详细任务、数据准入与安全状态见系统健康。',attention:Boolean(business&&!['OK','NORMAL'].includes(business))}
    ];
    $('insight-risk').textContent='风险关注';
    $('risk-chips').innerHTML=riskRows.map(r=>'<div class="risk-chip" data-level="'+(r.attention?'attention':'neutral')+'"><strong>'+r.short+'</strong><span>'+r.value+'</span></div>').join('');
    $('unified-risks').innerHTML=riskRows.map(r=>'<div class="home-risk-row"><span class="home-risk-label">'+r.label+'</span><span><strong>'+r.value+'</strong><small>'+r.detail+'</small></span></div>').join('');
  }

  function render() {
    const focused=document.activeElement,stock=focused?.dataset?.stock,index=focused?.dataset?.index,persona=focused?.dataset?.persona,account=focused?.dataset?.homeAccount;
    renderMarket();renderCandidates();renderLower();
    if(stock)document.querySelector('[data-stock="'+stock+'"]')?.focus({preventScroll:true});
    if(index)document.querySelector('[data-index="'+index+'"]')?.focus({preventScroll:true});
    if(persona)document.querySelector('[data-persona="'+persona+'"]')?.focus({preventScroll:true});
    if(account)document.querySelector('[data-home-account="'+account+'"]')?.focus({preventScroll:true});
    $('preview-today').textContent=new Intl.DateTimeFormat('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',timeZone:'Asia/Shanghai'}).format(new Date());
  }
  const originalRecord=app.recordOverviewEvidence;
  app.recordOverviewEvidence=function(...args){originalRecord.apply(this,args);if(!queued){queued=true;requestAnimationFrame(()=>{queued=false;render();});}};
  const originalSwitch=app.switchPage;
  app.switchPage=function(id,updateHash=true){if(id!=='page-arena')pendingAccount=null;originalSwitch.call(this,id,updateHash);window.scrollTo({top:0,behavior:'instant'});document.querySelectorAll('.nav-item').forEach(b=>{if(b.dataset.page===id)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');});};
  window.addEventListener('hashchange',()=>{const id=location.hash.slice(1);if(document.getElementById(id)?.classList.contains('page-view')){if(!document.getElementById(id).classList.contains('active'))app.switchPage(id,false);requestAnimationFrame(()=>window.scrollTo({top:0,behavior:'instant'}));}});
  window.addEventListener('load',()=>requestAnimationFrame(()=>window.scrollTo({top:0,behavior:'instant'})));

  const originalArena=app.renderArena;
  app.renderArena=function(envelope){
    originalArena.call(this,envelope);
    const players=envelope?.data?.leaderboard || [],max=Math.max(1,...players.filter(p=>finite(p.net_pnl_pct)).map(p=>Math.abs(p.net_pnl_pct)));
    document.querySelectorAll('#arena-grid .arena-account').forEach(button=>{
      const p=players.find(p=>p.player_id===button.dataset.playerId);if(!p||!finite(p.net_pnl_pct))return;
      button.querySelector('strong').textContent=p.rank+'  '+p.name;
      button.querySelector('.arena-return').textContent=pct(p.net_pnl_pct);
      button.querySelector('span:nth-child(4)').textContent=finite(p.total_equity)?'¥'+number(p.total_equity):'资产待确认';
      const track=document.createElement('span');track.className='arena-bar';track.setAttribute('aria-hidden','true');
      const fill=document.createElement('span');fill.className='arena-bar-fill'+(p.net_pnl_pct<0?' negative':'');
      const size=Math.abs(p.net_pnl_pct)/max*48;fill.style.width=size+'%';fill.style.left=(p.net_pnl_pct<0?50-size:50)+'%';track.append(fill);button.append(track);
    });
    const stale=players.filter(p=>p.stale===true||p.data_status==='STALE').length;
    $('arena-visual-summary').innerHTML='<div><strong>'+players.length+'</strong><span>收益账户</span></div><div><strong>'+players.filter(p=>['PAUSED','STRATEGY_DISABLED'].includes(p.status)).length+'</strong><span>暂停账户</span></div><div><strong>'+stale+'</strong><span>估值过期账户</span></div><div><strong>¥100,000</strong><span>每账户初始本金合同</span></div>';
    if(pendingAccount&&$('page-arena').classList.contains('active')){const id=pendingAccount,button=[...document.querySelectorAll('#arena-grid .arena-account')].find(b=>b.dataset.playerId===id);if(button){pendingAccount=null;button.click();}}
  };

  app.saveDisplaySettings=function(event){
    event.preventDefault();const density=$('setting-density').value,home=$('setting-home').value;
    if(!['comfortable','compact'].includes(density)||!['page-overview','page-market','page-picks'].includes(home))return;
    try{localStorage.setItem('v7_display_settings',JSON.stringify({density,home}));document.body.dataset.density=density;$('settings-feedback').textContent='已保存，此浏览器立即生效。';}
    catch(_){$('settings-feedback').textContent='保存失败：浏览器本地存储不可用。';}
  };
  const originalLoad=app.loadDisplaySettings;
  app.loadDisplaySettings=function(){originalLoad.call(this);document.body.dataset.density=$('setting-density')?.value==='compact'?'compact':'comfortable';};

  function openCandidate(code,button) {
    const item=view.items.find(i=>i.code===code);if(!item)return;focusFrom=button;
    $('heat-dialog-title').textContent=item.name+' '+item.code;
    $('heat-dialog-content').innerHTML='<p class="heat-detail-value delta-'+tone(item.change)+'">'+(finite(item.change)?pct(item.change):'涨跌待确认')+'</p><p class="heat-detail-caption">'+(finite(item.change)?'扫描记录涨跌，非实时行情。记录来源 '+esc(item.samples[0].source)+'，记录时间 '+esc(item.samples[0].time):item.quoteStatus==='CONFLICT'?'来源数值或名称存在冲突，暂不着色。':'当前候选缺少合格的实际涨跌记录。')+'</p>'+item.sources.map(s=>'<section class="source-entry"><strong>'+esc(s.label)+(s.paused?'　暂停账户的观察记录':'')+'</strong><p>'+esc(s.kind==='PREDICTION'?'模型候选':s.action==='BUY'?'考虑买入':'继续观察')+'</p><p>'+esc(s.reason)+'</p><small>候选记录时间 '+esc(s.scanAt)+'</small></section>').join('');
    $('heat-analysis-link').dataset.code=code;$('heat-detail-dialog').showModal();
  }
  window.addEventListener('DOMContentLoaded',()=>{
    const iconPaths={
      'page-overview':'M3 10 12 3l9 7v10H3z M9 20v-7h6v7',
      'page-market':'M4 20V9h4v11 M10 20V4h4v16 M16 20v-9h4v9',
      'page-news':'M5 3h11l3 3v15H5z M8 8h5 M8 12h8 M8 16h8',
      'page-picks':'M10 18a8 8 0 1 1 0-16 8 8 0 0 1 0 16 M16 16l5 5',
      'page-scoring':'M4 5h16v12H9l-5 4z M8 9h8 M8 13h5',
      'page-arena':'M7 3h10v6a5 5 0 0 1-10 0z M7 5H3v3c0 3 2 4 5 4 M17 5h4v3c0 3-2 4-5 4 M12 14v6 M8 21h8',
      'page-personal':'M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8 M4 21v-3a8 6 0 0 1 16 0v3z',
      'page-ai-quant':'M8 3h8v4H8z M3 16h6v5H3z M15 16h6v5h-6z M12 7v5 M6 16v-4h12v4',
      'page-forward':'M4 5h16v15H4z M4 9h16 M8 3v4 M16 3v4 M8 13h3v3H8z',
      'page-backtest':'M4 3v18h17 M7 16l4-5 4 2 5-7',
      'page-sources':'M3 5c0-3 18-3 18 0v14c0 3-18 3-18 0z M3 5c0 3 18 3 18 0 M3 12c0 3 18 3 18 0',
      'page-llm':'M7 3h10v18H7z M3 8h4 M17 8h4 M3 16h4 M17 16h4 M10 7h4 M10 12h4 M10 17h4',
      'page-wecom':'M3 4h18v13H9l-6 4z M7 9h2 M11 9h2 M15 9h2',
      'page-health':'M12 2l9 4v6c0 5-6 9-9 10-3-1-9-5-9-10V6z M6 12h3l2-4 2 8 2-4h3',
      'page-settings':'M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8 M9 3h6l1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1z',
      'page-plans':'M5 3h14v18H5z M8 8h8 M8 12h8 M8 16h5',
      'page-data':'M3 4h18v16H3z M3 9h18 M9 4v16 M15 4v16',
      'page-reports':'M5 3h11l4 4v14H5z M16 3v5h4 M8 12h8 M8 16h6'
    };
    document.querySelectorAll('.nav-item').forEach(button=>{const path=iconPaths[button.dataset.page];if(path&&!button.querySelector('svg'))button.insertAdjacentHTML('afterbegin','<svg class="nav-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="'+path+'"/></svg>');});
    document.querySelector('.brand')?.addEventListener('click',event=>{event.preventDefault();app.switchPage('page-overview');});
    document.querySelectorAll('.nav-group h2').forEach(h=>{h.textContent=h.textContent.replace(/^\d+\s*/,'');});
    document.addEventListener('click',event=>{
      const index=event.target.closest('[data-index]');if(index){selectedIndex=index.dataset.index;renderMarket();document.querySelector('[data-index="'+selectedIndex+'"]')?.focus({preventScroll:true});}
      const cell=event.target.closest('[data-stock]');if(cell)openCandidate(cell.dataset.stock,cell);
      const mode=event.target.closest('[data-chart-mode]');if(mode){chartMode=mode.dataset.chartMode==='full'?'full':'recorded';document.querySelectorAll('[data-chart-mode]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.chartMode===chartMode)));drawChart();}
      const persona=event.target.closest('[data-persona]');if(persona){const opinions=V7DashboardData.opinions(app.overviewEvidence.picks),role=opinions?.roles.find(r=>r.participant_id===persona.dataset.persona);if(role&&V7PicksPresentation.identity(role))app.openPickDrawer(role,persona);else app.switchPage('page-picks');}
      const account=event.target.closest('[data-home-account]');if(account){const id=account.dataset.homeAccount;app.switchPage('page-arena');const target=[...document.querySelectorAll('#arena-grid .arena-account')].find(b=>b.dataset.playerId===id);if(target)target.click();else pendingAccount=id;}

      const jump=event.target.closest('[data-ui-page]');if(jump)app.switchPage(jump.dataset.uiPage);
    });
    $('unified-index-tabs').addEventListener('keydown',event=>{
      if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
      event.preventDefault();const ids=V7DashboardData.indexes.map(row=>row[0]),at=ids.indexOf(selectedIndex);
      selectedIndex=ids[event.key==='Home'?0:event.key==='End'?ids.length-1:(at+(event.key==='ArrowRight'?1:-1)+ids.length)%ids.length];
      renderMarket();document.querySelector('[data-index="'+selectedIndex+'"]')?.focus({preventScroll:true});
    });
    $('heat-dialog-close').addEventListener('click',()=>$('heat-detail-dialog').close());
    $('heat-detail-dialog').addEventListener('close',()=>{if($('page-overview').classList.contains('active')){const target=focusFrom?.isConnected?focusFrom:document.querySelector('[data-ui-page="page-picks"]');target?.focus({preventScroll:true});}});
    $('heat-detail-dialog').addEventListener('click',event=>{if(event.target===$('heat-detail-dialog')){const r=event.target.getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)event.target.close();}});
    $('heat-analysis-link').addEventListener('click',()=>{const code=$('heat-analysis-link').dataset.code;$('heat-detail-dialog').close();app.switchPage('page-scoring');$('stock-code-input').value=code;app.loadStock(code);});
    const observer=new ResizeObserver(()=>requestAnimationFrame(drawChart));observer.observe($('unified-chart'));render();
  });
})();
