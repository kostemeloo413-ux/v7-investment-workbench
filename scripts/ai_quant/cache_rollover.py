"""Independent, manually invoked AIQ day-end update. No scheduler or economics.

Reuse 24 immutable all-market raw dates. Refresh only new-date factor events
and event-affected/new-member official chains; never join differently anchored
fore-factor scales. Default is staging only; --publish is a separate action.
"""
from __future__ import annotations
import argparse,contextlib,datetime,hashlib,json,math,os,sqlite3,time,uuid,sys
from collections import Counter
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from ai_quant.production_input import QualifiedQFQInput,ProductionInputError,file_sha,ROOT,POINTER
import runtime_config as rc

TZ=ZoneInfo('Asia/Shanghai')
CONTRACT='RAW_OHLC_TIMES_PUBLISHED_FORE_FACTOR_DIVIDED_BY_CUTOFF_FACTOR'

def read(p):return json.loads(Path(p).read_text(encoding='utf-8-sig'))
def write(p,x):
 p=Path(p);p.parent.mkdir(parents=True,exist_ok=True)
 with p.open('w',encoding='utf-8') as f:
  f.write(json.dumps(x,ensure_ascii=False,indent=2)+'\n');f.flush();os.fsync(f.fileno())
def require(ok,reason):
 if not ok:raise ProductionInputError(reason)
def evidence_rows(e):
 p=Path(e.get('path') or e['raw_path']);require(file_sha(p)==e['sha256'],'ROLLOVER_EVIDENCE_HASH_MISMATCH')
 if e.get('wire_path'):
  require(file_sha(e['wire_path'])==e['wire_sha256'] and Path(e['wire_path']).read_bytes().endswith(b'<![CDATA[]]>\n'),'ROLLOVER_PROTOCOL_EVIDENCE_INVALID')
 obj=read(p)
 if isinstance(obj,dict):
  require(str(obj.get('result_code','0'))=='0','ROLLOVER_PROVIDER_RESULT_FAILED')
  return obj['rows']
 return obj
def evidence(path,**kw):return dict(path=str(Path(path).resolve()),sha256=file_sha(path),**kw)
def normalize_date(s):return datetime.datetime.strptime(str(s).replace('-',''),'%Y%m%d').date()
def completed_day(td,now=None):
 d=normalize_date(td);now=now or datetime.datetime.now(TZ)
 require(now.tzinfo is not None,'ROLLOVER_CLOCK_TIMEZONE_REQUIRED')
 now=now.astimezone(TZ)
 require(d<now.date() or (d==now.date() and now.time().replace(tzinfo=None)>=datetime.time(16,30)),
         'ROLLOVER_TARGET_NOT_COMPLETED_NO_PROVIDER_REQUEST')
 state=rc.trading_calendar_status(d)
 require(state.get('status')=='VALID' and state.get('trading_day') is True,'ROLLOVER_NON_TRADING_DATE')
def trading_window(td):
 d=normalize_date(td);dates=[]
 for _ in range(90):
  s=rc.trading_calendar_status(d);require(s['status']=='VALID','ROLLOVER_CALENDAR_INVALID')
  if s['trading_day']:dates.append(d.strftime('%Y%m%d'))
  if len(dates)==25:return list(reversed(dates))
  d-=datetime.timedelta(days=1)
 raise ProductionInputError('ROLLOVER_CALENDAR_WINDOW_UNRESOLVED')
def factor_map(rows,cutoff,code=None):
 result={}
 for r in rows:
  c=r['code'].split('.')[-1]
  if code is not None:require(c==code,'ROLLOVER_FACTOR_CODE_MISMATCH')
  td=normalize_date(r['dividOperateDate']).strftime('%Y%m%d')
  require(td<=cutoff,'ROLLOVER_FUTURE_FACTOR')
  for k in ['foreAdjustFactor','backAdjustFactor','adjustFactor']:
   v=Decimal(r[k]);require(v.is_finite() and v>0,'ROLLOVER_FACTOR_INVALID')
  key=(c,td);require(key not in result,'ROLLOVER_FACTOR_EVENT_DUPLICATE')
  result[key]=r
 return result
def load_chains(m):
 chains={}
 for e in m['factor_seed_evidence']:
  for (code,td),row in factor_map(evidence_rows(e),m['cutoff']).items():chains.setdefault(code,{})[td]=row
 for e in m['date_factor_evidence'].values():
  for (code,td),row in factor_map(evidence_rows(e),m['cutoff']).items():
   old=chains.setdefault(code,{}).get(td)
   require(old is None or all(old[k]==row[k] for k in ['backAdjustFactor','adjustFactor']),
           'ROLLOVER_OLD_FACTOR_EVIDENCE_CONFLICT')
   # A refreshed official per-symbol chain owns the current fore scale;
   # immutable older date responses retain their own original anchor.
   if old is None:chains[code][td]=row
 return chains
def compare_official_refresh(old,new):
 """BaoStock can renormalize past fore factors after a new event.
 Ratios must have one scale and immutable back/adjust factors must agree.
 Neither new event fore=1 nor cached prior fore=1 implies equal price scale.
 """
 common=sorted(set(old)&set(new));require(bool(common),'ROLLOVER_CHAIN_COMMON_ANCHOR_MISSING')
 require(set(old)<=set(new),'ROLLOVER_KNOWN_OFFICIAL_EVENT_MISSING_AFTER_REFRESH')
 ratios=[];lower=Decimal(0);upper=Decimal('Infinity')
 for td in common:
  a,b=old[td],new[td]
  for k in ['backAdjustFactor','adjustFactor']:
   require(Decimal(a[k])==Decimal(b[k]),'ROLLOVER_OFFICIAL_FACTOR_HISTORY_REVISED')
  av,bv=Decimal(a['foreAdjustFactor']),Decimal(b['foreAdjustFactor'])
  # Intersect intervals implied by the provider's published decimal precision;
  # there is no arbitrary small-price-difference equivalence threshold.
  da=Decimal(10)**av.as_tuple().exponent/2;db=Decimal(10)**bv.as_tuple().exponent/2
  require(av>da and bv>db,'ROLLOVER_FACTOR_PRECISION_UNUSABLE')
  lower=max(lower,(bv-db)/(av+da));upper=min(upper,(bv+db)/(av-da))
  ratios.append(bv/av)
 scale=ratios[-1]
 require(lower<=upper,'ROLLOVER_FORE_FACTOR_SCALE_INCONSISTENT')
 return str(scale)

def validate_g6(context,td):
 from datasource.active_g6_v2_route import validate_v2_snapshot
 from datasource.completeness import validate_completeness_evidence,validate_target_rows
 base=Path(context['season_root'])/'market_data_completeness'
 paths={'snapshot':base/'universe_snapshots'/f'{td}.json','v2':base/'v2_universe_snapshots'/f'{td}.json',
        'evidence':base/f'{td}.json','health':base/'health'/f'{td}.json'}
 snap,v2,g6=(read(paths[k]) for k in ['snapshot','v2','evidence'])
 # The natural PASS route persists the frozen G6 evidence, not a HOLD-health
 # file. An absent optional diagnostic is not an absent G6 acceptance.
 health=read(paths['health']) if paths['health'].exists() else None
 valid,reasons=validate_v2_snapshot(v2,td)
 require(valid and v2.get('snapshot_readiness')=='READY_CANDIDATE','ROLLOVER_V2_INVALID:'+','.join(reasons))
 require(snap['universe_scope']=='DATE_EFFECTIVE_UNIVERSE_V2' and set(snap['expected_codes'])=={c.split('.')[0] for c in v2['expected_codes']},'ROLLOVER_MEMBER_IDENTITY_MISMATCH')
 require(snap['universe_version']=='DATE_EFFECTIVE_UNIVERSE_V2:'+v2['snapshot_identity_sha256'],'ROLLOVER_V2_IDENTITY_MISMATCH')
 valid,reasons=validate_completeness_evidence(g6,snap,context['season_id'],td,now=datetime.datetime.now(TZ))
 require(valid,'ROLLOVER_G6_NOT_PASS:'+','.join(reasons))
 # A preserved historical HOLD event is not the latest accepted G6 version.
 # Only a hash-bound formal recovery acceptance can supersede that event.
 if health is not None and health['status']!='PASS':
  accepted=[]
  for p in sorted((base/'recovery_acceptances').glob(td+'-*.json')):
   a=read(p)
   if (a.get('status')=='PASS' and a.get('season_id')==context['season_id'] and a.get('trade_date')==td
       and Path(a['formal_g6_path']).resolve()==paths['evidence'].resolve()
       and a.get('formal_g6_sha256')==file_sha(paths['evidence'])
       and Path(a['supersedes_event_path']).resolve()==paths['health'].resolve()
       and a.get('original_event_retained') is True
       and file_sha(a['recovery_capture_manifest'])==a['capture_manifest_sha256']
       and file_sha(a['trusted_validator_path'])==a['trusted_validator_sha256']):
    accepted.append(p)
  require(bool(accepted),'ROLLOVER_HEALTH_HOLD_WITHOUT_HASH_BOUND_RECOVERY')
  paths['recovery_acceptance']=accepted[-1]
 with sqlite3.connect(Path(rc.DB_PATH).resolve().as_uri()+'?mode=ro',uri=True) as db:
  db.row_factory=sqlite3.Row;rows=db.execute('SELECT * FROM daily_kline WHERE trade_date=?',(td,)).fetchall()
 grouped={}
 for r in rows:grouped.setdefault(r['code'],[]).append(dict(r))
 for code in snap['expected_codes']:
  bars=grouped.get(code,[]);valid,reason=validate_target_rows(code,td,bars)
  require(valid and bars[0]['source']==g6['source_by_symbol'].get(code),'ROLLOVER_G6_DATABASE_INVALID:'+code)
 identity={'count':len(snap['expected_codes']),'universe_snapshot_path':str(paths['snapshot']),
           'universe_snapshot_file_sha256':file_sha(paths['snapshot']),
           'formal_G6_health_sha256':file_sha(paths['health']) if health is not None else None,
           'formal_expected_codes_sha256':snap['expected_codes_sha256'],
           'formal_universe_identity_sha256':snap['universe_sha256']}
 return snap,v2,identity,{k:evidence(p) for k,p in paths.items() if p.exists()}

def approved_capture(capture_root,v2,td):
 cap=read(Path(capture_root)/'capture_manifest.json')
 # Official0.9.4 distribution reports __version__='00.9.40' in its SDK.
 require(cap['capture_complete'] and cap['trade_date']==td and cap.get('package_version') in {'0.9.4','00.9.40'},'ROLLOVER_CAPTURE_INCOMPLETE')
 result={}
 for name in ['daily','stock_basic','all_stock']:
  item=cap['sources'][name];p=Path(item['normalized_path'])
  require(item['query_status']=='SUCCESS' and str(item['result_code'])=='0' and item['row_count']>0 and item['row_count']==item['unique_count'], 'ROLLOVER_CAPTURE_SOURCE_INVALID')
  require(file_sha(p)==item['sha256']==v2['source_evidence'][name]['sha256'],'ROLLOVER_CAPTURE_V2_PROVENANCE_MISMATCH')
  parts=[]
  for frame in item['raw_protocol_responses']:
   raw=Path(capture_root)/'raw'/frame['raw_file']
   require(file_sha(raw)==frame['response_sha256'],'ROLLOVER_CAPTURE_PROTOCOL_HASH_MISMATCH')
   parts.append(raw.read_bytes())
  bundle=b''.join(len(part).to_bytes(8,'big')+part for part in parts)
  require(bool(parts) and hashlib.sha256(bundle).hexdigest()==item['response_sha256']==v2['source_evidence'][name]['response_sha256'],
          'ROLLOVER_CAPTURE_RAW_RESPONSE_IDENTITY_MISMATCH')
  result[name]=evidence(p,date=td,received_at=item['observed_at'])
 return result

def build_cache(m,window,target,stock_evidence,raw_evidence,date_factors,seed_evidence,
                identity,output_dir,refresh_codes=(),parent_manifest_sha=None):
 """Pure builder, explicit evidence only. No socket, scheduler or pointer access."""
 require(len(window)==25 and window==trading_window(window[-1]),'ROLLOVER_WINDOW_NOT_25_CONTIGUOUS')
 cutoff=window[-1];target=set(target)
 require(len(target)==identity['count'] and all(len(c)==6 and c.isdigit() for c in target),'ROLLOVER_TARGET_INVALID')
 require(set(date_factors)==set(window) and set(raw_evidence)==set(window),'ROLLOVER_REQUIRED_DATE_MISSING')
 master={}
 for row in evidence_rows(stock_evidence):
  if row['type']!='1':continue
  code=row['code'].split('.')[-1]
  require(code not in master,'ROLLOVER_MASTER_DUPLICATE');master[code]=row
 require(target<=set(master),'ROLLOVER_MEMBER_METADATA_MISSING')
 old_chains=load_chains(m);chains={c:dict(x) for c,x in old_chains.items()}
 refreshed={}
 for e in seed_evidence:
  rows=evidence_rows(e);require(bool(rows),'ROLLOVER_SEED_EMPTY')
  codes={r['code'].split('.')[-1] for r in rows};require(len(codes)==1,'ROLLOVER_SEED_IDENTITY_INVALID')
  c=next(iter(codes));refreshed[c]={d:r for (code,d),r in factor_map(rows,cutoff,c).items()}
 scales={}
 for c in refresh_codes:
  require(c in refreshed,'ROLLOVER_REFRESH_CHAIN_REQUIRED:'+c)
  if old_chains.get(c):scales[c]=compare_official_refresh(old_chains[c],refreshed[c])
  chains[c]=refreshed[c]
 # Preserve original seed chains when unchanged; accept official chains for new members.
 for c,chain in refreshed.items():
  if c not in chains:chains[c]=chain
 events={}
 for td in window:
  for (c,day),r in factor_map(evidence_rows(date_factors[td]),cutoff).items():
   require(day==td,'ROLLOVER_DATED_FACTOR_TARGET_MISMATCH');events.setdefault(c,{})[day]=r
 for c,chain in events.items():
  if c not in target:continue
  if c in refresh_codes:
   require(all(chains[c].get(d)==r for d,r in chain.items() if d==cutoff),'ROLLOVER_REFRESH_TARGET_EVENT_MISMATCH')
   # Older dated responses can use the preceding anchor; compare stable official fields.
   for d,r in chain.items():
    require(d in chains[c] and all(chains[c][d][k]==r[k] for k in ['backAdjustFactor','adjustFactor']), 'ROLLOVER_EVENT_CHAIN_DISAGREES')
  else:
   for d,r in chain.items():
    old=chains.setdefault(c,{}).get(d)
    require(old is None or all(old[k]==r[k] for k in ['backAdjustFactor','adjustFactor']),
            'ROLLOVER_UNREFRESHED_FACTOR_SCALE_CONFLICT')
    if old is None:chains[c][d]=r
 output_dir=Path(output_dir);output_dir.mkdir(parents=True,exist_ok=False)
 dbpath=output_dir/'qualified_history.db';by_symbol={c:[] for c in target};preipo=[];susp=[]
 # Reuse schema and industry labels from immutable admitted cache, read-only.
 with sqlite3.connect(Path(m['db_path']).as_uri()+'?mode=ro',uri=True) as prev:
  schemas=[r[0] for r in prev.execute("SELECT sql FROM sqlite_master WHERE type='table' ORDER BY name")]
  industries={r[0]:r[1] for r in prev.execute('SELECT symbol,industry FROM instrument_master')}
 db=sqlite3.connect(dbpath)
 try:
  for s in schemas:db.execute(s)
  for td in window:
   rows=evidence_rows(raw_evidence[td]);bycode={}
   for row in rows:
    c=row['code'].split('.')[-1];require(c not in bycode,'ROLLOVER_RAW_DUPLICATE');bycode[c]=row
    require(row['date'].replace('-','')==td and row['adjustflag']=='3','ROLLOVER_RAW_DATE_OR_BASIS_INVALID')
   for c in sorted(target):
    md=master[c];ipo=md['ipoDate'].replace('-','');out=md['outDate'].replace('-','')
    require(not out or out>cutoff,'ROLLOVER_DELISTED_IN_V2_TARGET')
    bar=bycode.get(c)
    if bar is None:
     require(ipo>td,'ROLLOVER_UNEXPLAINED_RAW_MISSING:'+c+':'+td);preipo.append({'symbol':c,'date':td,'ipo_date':ipo});continue
    require(ipo<=td and bar['tradestatus'] in ['0','1'] and bar['isST'] in ['0','1'],'ROLLOVER_STATUS_INVALID')
    raw={k:Decimal(bar[k]) for k in ['open','high','low','close']}
    require(all(x.is_finite() and x>0 for x in raw.values()) and raw['high']>=max(raw.values()) and raw['low']<=min(raw.values()),'ROLLOVER_OHLC_INVALID')
    chain=chains.get(c,{})
    if chain:
     earlier=[d for d in chain if d<=td];anchor=[d for d in chain if d<=cutoff]
     require(bool(earlier) and bool(anchor),'ROLLOVER_OFFICIAL_PRE_FACTOR_MISSING:'+c)
     factor=Decimal(chain[max(earlier)]['foreAdjustFactor'])/Decimal(chain[max(anchor)]['foreAdjustFactor'])
    else:
     # Complete official event responses for all25dates prove constant scale
     # cancels within the window. This does not invent an absolute seed.
     require(c not in events,'ROLLOVER_SEED_REQUIRED:'+c);factor=Decimal(1)
    numeric=lambda v:None if v in ['',None] else float(v)
    volume,amount,pct=(numeric(bar[k]) for k in ['volume','amount','pctChg'])
    if bar['tradestatus']=='1':require(volume is not None and volume>0 and amount is not None and amount>=0 and math.isfinite(volume) and math.isfinite(amount),'ROLLOVER_ACTIVE_UNITS_INVALID')
    else:susp.append({'symbol':c,'date':td,'raw_volume':bar['volume'],'raw_amount':bar['amount']})
    require(pct is None or math.isfinite(pct),'ROLLOVER_PCT_INVALID')
    q=[float(raw[k]*factor) for k in ['open','high','low','close']]
    require(all(math.isfinite(v) and v>0 for v in q),'ROLLOVER_QFQ_INVALID')
    rowsha=hashlib.sha256(json.dumps(bar,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    db.execute('INSERT INTO daily_kline VALUES('+','.join('?'*19)+')',(c,td,*q,volume,amount,pct,'BaoStock0.9.4:RAW3_OFFICIAL_FACTOR_QFQ',raw_evidence[td].get('received_at',datetime.datetime.now(TZ).isoformat()),bar['tradestatus'],bar['isST'],*(bar[k] for k in ['open','high','low','close']),str(factor),rowsha))
    by_symbol[c].append(bar)
  qualification={}
  for c in sorted(target):
   md=master[c];bars=by_symbol[c];last=next((r for r in bars if r['date'].replace('-','')==cutoff),None)
   require(last is not None,'ROLLOVER_TARGET_CROSS_SECTION_MISSING:'+c)
   age=(normalize_date(cutoff)-normalize_date(md['ipoDate'])).days;active=sum(r['tradestatus']=='1' for r in bars)
   reason=('LISTED_UNDER_60_DAYS' if age<60 else 'ST_AT_CUTOFF' if last['isST']=='1' else 'SUSPENDED_AT_CUTOFF' if last['tradestatus']=='0' else 'LESS_THAN_25_VALID_BARS_LEGAL_PROVIDER_SUSPENSION' if active<25 else 'QUALIFIED')
   qualification[c]={'status':reason,'valid_active_bars':active,'available_dates':len(bars),'IPO_age_days':age,'suspension_dates':[r['date'] for r in bars if r['tradestatus']=='0']}
   exchange='SH' if c.startswith('6') else 'SZ'
   db.execute('INSERT INTO instrument_master VALUES('+','.join('?'*10)+')',(c,md['code_name'],c+'.'+exchange,exchange,'MAIN',industries.get(c,'主板'),md['ipoDate'].replace('-',''),md['outDate'].replace('-','') or None,int(last['isST']),int(last['tradestatus']=='0')))
   db.execute('INSERT INTO input_qualification VALUES(?,?,?,?,?)',(c,'QUALIFIED' if reason=='QUALIFIED' else 'LEGAL_INELIGIBLE',reason,active,len(bars)))
  db.commit();require(db.execute('PRAGMA integrity_check').fetchone()[0]=='ok','ROLLOVER_DB_INTEGRITY_FAILED')
  count=db.execute('SELECT COUNT(*) FROM daily_kline').fetchone()[0]
 finally:db.close()
 new=dict(m,cutoff=cutoff,QFQ_anchor=cutoff,window=window,universe_identity=identity,db_path=str(dbpath.resolve()),db_sha256=file_sha(dbpath),
          stock_basic_path=stock_evidence['path'],stock_basic_sha256=stock_evidence['sha256'],raw_date_evidence=[dict(raw_evidence[d],date=d) for d in window],
          date_factor_evidence=date_factors,factor_seed_evidence=seed_evidence,qualification=qualification,qualification_reason_counts=dict(Counter(q['status'] for q in qualification.values())),
          legal_pre_IPO=preipo,provider_suspensions=susp,unexplained_missing=[],future_bar_count=0,row_count=count,created_at=datetime.datetime.now(TZ).isoformat(),
          rollover={'parent_manifest_sha256':parent_manifest_sha,'reused_raw_dates':window[:-1],'refreshed_factor_codes':sorted(refresh_codes),'official_common_fore_scale':scales,
                    'added_members':sorted(target-set(m['qualification'])),'removed_members':sorted(set(m['qualification'])-target)})
 mp=output_dir/'manifest.json';write(mp,new);return mp

@contextlib.contextmanager
def update_lock(root):
 import msvcrt
 root=Path(root);root.mkdir(parents=True,exist_ok=True)
 with (root/'rollover.lock').open('a+b') as f:
  f.seek(0);f.write(b'0');f.flush();f.seek(0)
  try:msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
  except OSError as exc:raise ProductionInputError('ROLLOVER_ALREADY_RUNNING') from exc
  try:yield
  finally:f.seek(0);msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)

def qualify_candidate(mp):
 from ai_quant.ranking import Top5RankingEngine
 adapter=QualifiedQFQInput(mp,file_sha(mp))
 ranker=Top5RankingEngine(input_provider=adapter,persist_predictions=False)
 top5=ranker.rank_market(as_of_date=adapter.cutoff)
 require(len(top5)==5,'ROLLOVER_MODEL_INPUT_INCOMPLETE')
 return {'status':'PASS','cutoff':adapter.cutoff,'members':len(adapter.manifest['qualification']),
         'feature_22D_count':len(ranker.input_audit['feature_rows']),'Top5':top5,'model_and_calibrator_unchanged':True,'formal_SQL_write':False,
         'manifest_path':str(Path(mp).resolve()),'manifest_sha256':file_sha(mp)}

def publish_candidate(mp,pointer,expected_pointer_sha,qualification,g6_hashes):
 require(qualification['status']=='PASS','ROLLOVER_NOT_QUALIFIED')
 require(qualification.get('manifest_sha256')==file_sha(mp) and qualification.get('manifest_path')==str(Path(mp).resolve()),'ROLLOVER_QUALIFICATION_IDENTITY_CHANGED')
 for e in g6_hashes.values():require(file_sha(e['path'])==e['sha256'],'ROLLOVER_G6_CHANGED_BEFORE_PUBLISH')
 require(file_sha(pointer)==expected_pointer_sha,'ROLLOVER_POINTER_CONCURRENT_CHANGE')
 # Fully reverify cache, model, evidence and active-season identity at publication.
 QualifiedQFQInput(mp,file_sha(mp))
 temp=Path(pointer).with_name('CURRENT.'+uuid.uuid4().hex+'.tmp')
 write(temp,{'manifest_path':str(Path(mp).resolve()),'manifest_sha256':file_sha(mp),'status':'QUALIFIED_ENGINEERING_PENDING_INDEPENDENT_REVIEW','owner_Forward_activation_authorized':False})
 try:
  require(file_sha(pointer)==expected_pointer_sha,'ROLLOVER_POINTER_CONCURRENT_CHANGE')
  os.replace(temp,pointer)
 finally:
  if temp.exists():temp.unlink()

def protocol_allowance(ledger,cutoff,max_calls,cumulative_limit,daily_operations):
 base=len(ledger['requests'])
 require(1<=max_calls<=20 if daily_operations else 1<=max_calls<=44,'ROLLOVER_RUN_PROTOCOL_BUDGET_INVALID')
 if daily_operations:
  used=sum(c.get('trade_date')==cutoff for c in ledger['requests'])
  remaining=min(max_calls,20-used)
  require(remaining>=2,'ROLLOVER_DAILY_PROTOCOL_BUDGET_EXHAUSTED')
  return base,remaining
 if cumulative_limit:require(356+base+max_calls<=cumulative_limit,'ROLLOVER_OWNER_TASK_PROTOCOL_BUDGET')
 return base,max_calls

def collect_factors(work,cutoff,old,target,window,master,max_calls=20,cumulative_limit=400,daily_operations=False):
 """Future completed-day explicit opt-in only. No retries. Count every frame."""
 import baostock as bs
 from datasource.baostock_transport import bounded_baostock_session
 from datasource.active_g6_v2_route import _ProtocolCapture,_consume
 completed_day(cutoff);work=Path(work)
 event_path=work/'new_date_factors.json';ledger_path=work.parent/'protocol_ledger.json'
 ledger=read(ledger_path) if ledger_path.exists() else {'requests':[]}
 checkpoint_path=work/'capture_checkpoint.json'
 checkpoint=read(checkpoint_path) if checkpoint_path.exists() else {}
 def reusable(path):
  key=Path(path).name
  if not Path(path).exists():return None
  require(key in checkpoint,'ROLLOVER_UNVERIFIED_EXISTING_STAGING')
  e=checkpoint[key];evidence_rows(e);return e
 def planned(events):
  dated=factor_map(evidence_rows(events),cutoff)
  require(all(d==cutoff for c,d in dated),'ROLLOVER_NEW_FACTOR_DATE_MISMATCH')
  new_codes={c for c,d in dated}&set(target)
  chains=load_chains(old)
  refresh=new_codes|((set(target)-set(old['qualification']))&set(chains))
  seeds=[e for e in old['factor_seed_evidence'] if not {r['code'].split('.')[-1] for r in evidence_rows(e)}&refresh]
  needed=[]
  for c in sorted(refresh):
   e=reusable(work/('chain_'+c+'.json'))
   if e is None:needed.append(c)
   else:seeds.append(e)
  return seeds,refresh,needed
 existing=reusable(event_path)
 if existing:
  seeds,refresh,needed=planned(existing)
  if not needed:return existing,seeds,refresh
 base,remaining=protocol_allowance(ledger,cutoff,max_calls,cumulative_limit,daily_operations)
 session=work/'sessions'/uuid.uuid4().hex;session.mkdir(parents=True,exist_ok=False)
 tap=None
 def flush():
  if tap is None:return
  ledger['requests']=ledger['requests'][:base]+[dict(c,ordinal=base+c['ordinal'],inherited_task_ordinal=356+base+c['ordinal'],trade_date=cutoff,run=str(work),session=str(session),budget_mode='DAILY_OPERATIONS' if daily_operations else 'OWNER_IMPLEMENTATION') for c in tap.calls]
  ledger['inherited_requests']=356;ledger['total_with_inherited']=356+len(ledger['requests'])
  ledger['daily_protocol_limit']=20;ledger['daily_counts']=dict(Counter(c.get('trade_date','LEGACY_UNDATED') for c in ledger['requests']))
  temp=ledger_path.with_suffix('.tmp');write(temp,ledger);os.replace(temp,ledger_path)
 def query(label,fn,path):
  with tap.operation(label):fields,rows,code,message=_consume(fn())
  require(code=='0','ROLLOVER_PROVIDER_ERROR:'+code)
  wire=session/'wire'/('wire_%03d.bin'%len(tap.calls));require(wire.read_bytes().endswith(b'<![CDATA[]]>\n'),'ROLLOVER_PROTOCOL_PARTIAL_FRAME')
  write(path,{'fields':fields,'rows':rows,'result_code':code,'result_message':message})
  e=evidence(path,received_at=datetime.datetime.now(TZ).isoformat(),wire_path=str(wire),wire_sha256=file_sha(wire),session=str(session))
  checkpoint[Path(path).name]=e;write(checkpoint_path,checkpoint);return e
 try:
  with bounded_baostock_session(wire_root=session/'wire',query_timeout=90,total_timeout=600):
   # Install after the bounded transport: installing before it loses the tap
   # when the transport replaces send_msg, bypassing the protocol budget.
   tap=_ProtocolCapture(session/'protocol',remaining)
   bounded_send=tap.original
   def observed(msg):
    flush()  # The tap reserved this ordinal before any actual send (crash safe).
    time.sleep(.25)
    return bounded_send(msg)
   tap.original=observed;tap.install()
   try:
    with tap.operation('login'):login=bs.login()
    require(login.error_code=='0','ROLLOVER_LOGIN_FAILED')
    if existing:events=existing
    else:events=query('new_date_factors',lambda:bs.query_daily_adjust_factor(date=normalize_date(cutoff).isoformat()),event_path)
    seed_files,refresh,needed=planned(events)
    require(len(tap.calls)+len(needed)+1<=remaining,'ROLLOVER_FACTOR_REQUEST_BUDGET_INSUFFICIENT')
    for c in needed:
     md=master[c];p=work/('chain_'+c+'.json')
     e=query('official_chain:'+c,lambda md=md:bs.query_adjust_factor(md['code'],start_date=md['ipoDate'],end_date=normalize_date(cutoff).isoformat()),p)
     require(bool(evidence_rows(e)),'ROLLOVER_OFFICIAL_CHAIN_EMPTY');seed_files.append(e)
    with tap.operation('logout'):logout=bs.logout()
    require(logout.error_code=='0','ROLLOVER_LOGOUT_FAILED')
    return events,seed_files,refresh
   finally:
    flush();tap.socketutil.send_msg=bounded_send
 finally:flush()

def run(args):
 from competition.season_runtime import resolve_active_season_context
 completed_day(args.trade_date)
 output=Path(args.output_root).resolve()
 require(output.is_relative_to(ROOT/'data/ai_quant/input_cache') or output.is_relative_to(ROOT/'docs/v7_review/codex'),
         'ROLLOVER_OUTPUT_MUST_BE_INDEPENDENT_CACHE_OR_REVIEW')
 with update_lock(args.output_root):
  parent=QualifiedQFQInput();m=parent.manifest;td=normalize_date(args.trade_date).strftime('%Y%m%d')
  context=resolve_active_season_context();snap,v2,identity,g6=validate_g6(context,td)
  safety=read(context['governance_safety_path'])
  require(safety.get('real_trading') is False and safety.get('execution_state_safe') is True,
          'ROLLOVER_PRODUCTION_EXECUTION_STATE_UNSAFE')
  if m['cutoff']==td:return {'status':'ALREADY_QUALIFIED_NO_WRITE','cutoff':td,'manifest_sha256':parent.manifest_sha256}
  window=trading_window(td)
  require(window[:-1]==m['window'][1:],'ROLLOVER_REQUIRES_SEQUENTIAL_MISSING_TRADING_DAYS')
  raw={e['date']:e for e in m['raw_date_evidence'] if e['date'] in window}
  cap=approved_capture(args.capture_root or Path(context['season_root'])/'market_data_completeness/provider_capture'/td,v2,td)
  raw[td]=cap['daily'];factors={d:e for d,e in m['date_factor_evidence'].items() if d in window}
  target=set(snap['expected_codes']);master={r['code'].split('.')[-1]:r for r in evidence_rows(cap['stock_basic']) if r['type']=='1'}
  version='BAOSTOCK_QFQ_'+td+'_'+datetime.datetime.now(TZ).strftime('%Y%m%dT%H%M%S%f')
  work=Path(args.output_root)/('evidence_'+td+'_'+parent.manifest_sha256[:12]);work.mkdir(exist_ok=True)
  expected=file_sha(POINTER)
  if args.collect_factors:
   events,seeds,refresh=collect_factors(work,td,m,target,window,master,args.max_protocol_calls,args.cumulative_protocol_limit,getattr(args,'daily_operations',False))
  else:
   require(args.factor_bundle is not None,'ROLLOVER_OFFICIAL_FACTOR_BUNDLE_REQUIRED')
   bundle=read(args.factor_bundle);events=bundle['date_event_evidence'];seeds=bundle['seed_evidence'];refresh=set(bundle['refresh_codes'])
   require(({c for c,d in factor_map(evidence_rows(events),td)}&target)<=refresh,'ROLLOVER_NEW_EVENT_REQUIRES_FULL_CHAIN')
  factors[td]=events
  mp=build_cache(m,window,target,cap['stock_basic'],raw,factors,seeds,identity,Path(args.output_root)/version,refresh,parent.manifest_sha256)
  result=qualify_candidate(mp);result.update(manifest_path=str(mp),manifest_sha256=file_sha(mp),published=False,production_economic_writes=0)
  write(mp.parent/'admission_result.json',result)
  if args.publish:
   # Recheck G6/database and execution identity immediately before pointer CAS.
   validate_g6(context,td)
   publish_candidate(mp,POINTER,expected,result,g6);result['published']=True
  try:write(work/'result.json',result)
  except OSError as exc:
   if not result['published']:raise
   result['audit_receipt_write_warning']=str(exc)
  return result

def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--trade-date',required=True);p.add_argument('--capture-root',type=Path)
 p.add_argument('--output-root',type=Path,default=ROOT/'data/ai_quant/input_cache')
 p.add_argument('--factor-bundle',type=Path);p.add_argument('--collect-factors',action='store_true')
 p.add_argument('--max-protocol-calls',type=int,default=20);p.add_argument('--publish',action='store_true')
 p.add_argument('--daily-operations',action='store_true',help='Approved day-end quota:20 cumulative protocol requests per target date, across all runs; historical356 ledger retained.')
 p.add_argument('--cumulative-protocol-limit',type=int,default=400,
                help='Current owner task cap400; future approved operational quota may specify another cap or0 (per-run cap still applies). Never resets request ledger.')
 a=p.parse_args()
 try:result=run(a)
 except Exception as exc:result={'status':'HOLD','reason':str(exc),'pointer_published':False,'economic_writes':0};print(json.dumps(result,ensure_ascii=False));return 2
 print(json.dumps(result,ensure_ascii=False,indent=2));return 0
if __name__=='__main__':raise SystemExit(main())
