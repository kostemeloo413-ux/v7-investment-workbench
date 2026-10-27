"""Bounded, resumable daily data recovery. Never writes economic ledgers.

V2/G6 and QFQ qualification retain their frozen contracts. Each capture attempt
has its own evidence directory; historical HOLD events are retained verbatim.
The vendored trust validator is the unchanged, previously accepted validator.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import datetime as dt
import hashlib
import json
import math
import sqlite3
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT/'scripts') not in sys.path:
    sys.path.insert(0, str(ROOT/'scripts'))

import runtime_config as rc
from datasource import completeness as g6
from datasource.active_g6_v2_route import (
    ActiveG6RouteHold, archive_legacy_artifact, build_v2_snapshot,
    capture_target_date, prepare_g6_snapshot, validate_v2_snapshot,
)
from datasource.baostock_transport import bounded_baostock_session, daily_g6_lock

TZ = ZoneInfo('Asia/Shanghai')
VALIDATOR_SHA = 'f70519373bcd298739e6a143b535323ee31f11d7c571646d4c243b7727289ed6'
MAX_CAPTURE_ATTEMPTS_PER_DATE_PER_DAY = 3
BAR_FIELDS = ('open','high','low','close','volume','amount','pct_chg')
INSERT_BAR = ('INSERT INTO daily_kline(code,trade_date,open,high,low,close,volume,amount,pct_chg,source,updated_at) '
              'VALUES(:code,:trade_date,:open,:high,:low,:close,:volume,:amount,:pct_chg,:source,:updated_at)')


def now():
    return dt.datetime.now(TZ)


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def run_id():
    return now().strftime('%Y%m%dT%H%M%S%f')+'_'+uuid.uuid4().hex[:8]


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def require(condition, reason):
    if not condition:
        raise ActiveG6RouteHold(reason)


def completed_trade_date(timestamp=None):
    stamp = timestamp or now()
    date = stamp.date()
    if stamp.time().replace(tzinfo=None) < dt.time(16,30):
        date -= dt.timedelta(days=1)
    for _ in range(370):
        calendar = rc.trading_calendar_status(date)
        require(calendar.get('status') == 'VALID', 'RECOVERY_CALENDAR_INVALID')
        if calendar.get('trading_day') is True:
            return date.strftime('%Y%m%d')
        date -= dt.timedelta(days=1)
    raise ActiveG6RouteHold('RECOVERY_NO_COMPLETED_TRADING_DATE')


def missing_dates(cutoff, target):
    date = dt.datetime.strptime(cutoff, '%Y%m%d').date() + dt.timedelta(days=1)
    end = dt.datetime.strptime(target, '%Y%m%d').date()
    require((end-date).days < 370, 'RECOVERY_GAP_EXCEEDS_ONE_YEAR')
    dates = []
    while date <= end:
        calendar = rc.trading_calendar_status(date)
        require(calendar.get('status') == 'VALID', 'RECOVERY_CALENDAR_INVALID')
        if calendar.get('trading_day') is True:
            dates.append(date.strftime('%Y%m%d'))
        date += dt.timedelta(days=1)
    return dates


def verify_capture(capture_root, target):
    """Read only evidence-bound files inside the individual attempt directory."""
    root = Path(capture_root).resolve()
    capture = read(root/'capture_manifest.json')
    require(capture.get('capture_complete') is True and capture.get('trade_date') == target,
            'RECOVERY_CAPTURE_INCOMPLETE')
    rows = {}
    for name in ('stock_basic','all_stock','daily'):
        e = capture['sources'][name]
        path = Path(e['normalized_path']).resolve()
        require(path.is_relative_to(root) and sha(path) == e['sha256'], 'RECOVERY_NORMALIZED_HASH_MISMATCH')
        require(e['query_status'] == 'SUCCESS' and str(e['result_code']) == '0', 'RECOVERY_PROVIDER_ERROR')
        rows[name] = read(path)
        require(len(rows[name]) == e['row_count'] == e['unique_count'] > 0, 'RECOVERY_CAPTURE_ROW_COUNT_MISMATCH')
        parts = []
        for frame in e['raw_protocol_responses']:
            raw = (root/'raw'/frame['raw_file']).resolve()
            require(raw.is_relative_to(root/'raw') and sha(raw) == frame['response_sha256'], 'RECOVERY_PROTOCOL_HASH_MISMATCH')
            parts.append(raw.read_bytes())
        bundle = b''.join(len(part).to_bytes(8,'big')+part for part in parts)
        require(parts and hashlib.sha256(bundle).hexdigest() == e['response_sha256'], 'RECOVERY_PROTOCOL_BUNDLE_MISMATCH')
    for call in capture['protocol_calls']:
        if call.get('raw_file'):
            require(sha(root/'raw'/call['raw_file']) == call['response_sha256'], 'RECOVERY_PROTOCOL_CALL_HASH_MISMATCH')
    capture['rows'] = rows
    return capture


def capture_for_date(base, target):
    """Resume valid captures; reserve one bounded attempt before network use."""
    runs = base/'recovery_runs'/target
    for manifest in sorted(runs.glob('*/capture/capture_manifest.json'), reverse=True):
        if read(manifest).get('capture_complete'):
            capture = verify_capture(manifest.parent, target)
            try:
                build_v2_snapshot(capture,trade_date=target)
            except ActiveG6RouteHold:
                continue  # Retain source evidence, but allow a bounded new capture.
            return manifest.parent, capture
    ledger_path = runs/'capture_budget.json'
    ledger = read(ledger_path) if ledger_path.exists() else {'attempts':[]}
    today = now().strftime('%Y%m%d')
    attempts = [a for a in ledger['attempts'] if a['execution_date'] == today]
    require(len(attempts) < MAX_CAPTURE_ATTEMPTS_PER_DATE_PER_DAY, 'RECOVERY_CAPTURE_DAILY_ATTEMPT_BUDGET_EXHAUSTED')
    identity = run_id()
    root = runs/identity/'capture'
    record = dict(execution_date=today, run_id=identity, status='RESERVED',
                  max_protocol_calls=100, root=str(root), reserved_at=now().isoformat())
    ledger['attempts'].append(record)
    g6.atomic_json_write(ledger_path, ledger)
    try:
        with bounded_baostock_session(wire_root=root/'transport_raw'):
            capture = capture_target_date(target, root)
        record.update(status='COMPLETE' if capture['capture_complete'] else 'HOLD',
                      protocol_calls=capture['protocol_call_count'], reason=capture.get('stop_reason'))
        require(capture['capture_complete'], 'RECOVERY_CAPTURE_INCOMPLETE:'+str(capture.get('stop_reason')))
        return root, verify_capture(root, target)
    finally:
        record['finished_at'] = now().isoformat()
        g6.atomic_json_write(ledger_path, ledger)


def captured_bars(capture, codes, target):
    daily = capture['rows']['daily']
    mapped = {r['code'].split('.')[-1]:r for r in daily}
    require(len(mapped) == len(daily), 'RECOVERY_DUPLICATE_PROVIDER_BAR')
    bars = []
    for code in codes:
        raw = mapped[code]
        require(raw['date'].replace('-','') == target, 'RECOVERY_BAR_DATE_MISMATCH')
        bar = {k:float(raw[k]) for k in BAR_FIELDS if k != 'pct_chg'}
        bar.update(code=code, trade_date=target, pct_chg=float(raw['pctChg']),
                   source=capture['sources']['daily']['source_id'], updated_at=now().isoformat())
        require(g6.validate_target_bar(code,target,bar)[0], 'RECOVERY_INVALID_BAR:'+code)
        require(all(math.isfinite(bar[k]) for k in BAR_FIELDS), 'RECOVERY_NONFINITE_BAR:'+code)
        bars.append(bar)
    return bars


def validate_trusted(v2, capture, capture_root, run, target):
    from datasource import universe_trust_boundary as trust
    require(sha(trust.__file__) == VALIDATOR_SHA, 'RECOVERY_TRUST_VALIDATOR_SOURCE_DRIFT')
    members = []
    for m in v2['member_records']:
        require(m['qualification_status']=='ELIGIBLE' and m['is_st'] is False
                and m['all_stock_trade_status']=='1' and m['daily_trade_status']=='1', 'RECOVERY_MEMBER_NOT_ELIGIBLE')
        members.append(dict(canonical_symbol=m['canonical_symbol'], exchange=m['exchange'],
            board='MAINBOARD', listing_status='LISTED', st_status='NOT_ST', suspension_status='NOT_SUSPENDED',
            list_date=m['identity_record']['ipoDate'], delist_date=m['identity_record'].get('outDate') or None,
            source_ids=dict(identity=m['identity_source_id'],same_date_status=m['status_source_id'],daily_bar=m['daily_source_id']),
            source_sha256=dict(identity=m['identity_evidence_sha256'],same_date_status=m['status_evidence_sha256'],daily_bar=m['daily_evidence_sha256']),
            target_daily_record_sha256=trust.sha256_json(m['daily_record'])))
    members.sort(key=lambda m:(m['canonical_symbol'],m['exchange']))
    identity = sha(run/'v2_snapshot.json')
    trusted = dict(trade_date=dt.datetime.strptime(target,'%Y%m%d').date().isoformat(),
        scope=v2['scope'], version='DATE_EFFECTIVE_TRADABLE_UNIVERSE_V2',
        source_identity='|'.join(k+':'+e['source_id'] for k,e in sorted(capture['sources'].items())),
        evidence_id=identity, effective_at=capture['completed_at'], members=members,
        members_count=len(members),members_hash=trust.sha256_json(members),evidence_class='REAL_SOURCE_EVIDENCE')
    def source_verifier(candidate):
        verified = verify_capture(capture_root,target)
        rebuilt = build_v2_snapshot(verified,trade_date=target)
        return (candidate==trusted and sha(run/'v2_snapshot.json')==identity
                and rebuilt==v2 and validate_v2_snapshot(v2,target)[0])
    result = trust.DateEffectiveUniverseValidator(source_verifier=source_verifier).validate_snapshot(trusted,trusted['trade_date'])
    ok, reasons = trust.verify_validation_result(result,result_type=trust.UniverseValidationResult,
        validator_id=trust.UNIVERSE_VALIDATOR_ID,expected_subject=result.subject_identity,allow_synthetic=False)
    g6.atomic_json_write(run/'trusted_validator_input.json',trusted)
    g6.atomic_json_write(run/'trusted_validator_result.json',dict(status=result.status,
        result=asdict(result),verify_result=ok,verify_reasons=reasons,validator_source_sha256=VALIDATOR_SHA))
    require(ok, 'RECOVERY_TRUST_VALIDATOR_REJECTED:'+','.join(reasons))


def insert_verified_bars(database, bars, *, authorize=None):
    """One authorized INSERT transaction. Conflicts fail before any row commits."""
    if authorize is None:
        from datasource import db as hdb
        authorize = hdb._GOVERNANCE_POLICY.validate_db_write
    ok, reason = authorize('kline_writer','daily_kline','INSERT')
    require(ok, 'RECOVERY_DB_WRITER_DENIED:'+str(reason))
    with closing(sqlite3.connect(database,timeout=15)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN IMMEDIATE')
        missing = []
        for bar in bars:
            previous = db.execute('SELECT * FROM daily_kline WHERE code=? AND trade_date=?',
                                  (bar['code'],bar['trade_date'])).fetchone()
            if previous is None:
                missing.append(bar)
            else:
                require(all(previous[k] == bar[k] for k in (*BAR_FIELDS,'source')),
                        'RECOVERY_EXISTING_BAR_CONFLICT:'+bar['code'])
        db.executemany(INSERT_BAR,missing)
    return len(missing)


def validate_database(database, snapshot, evidence_root, source):
    """Use the unchanged G6 collector to independently reread stored rows."""
    with closing(sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory = sqlite3.Row
        def reader(code,target):
            return [dict(r) for r in db.execute('SELECT * FROM daily_kline WHERE code=? AND trade_date=?',(code,target))]
        result = g6.collect_target_date(snapshot,evidence_root,
            lambda code,target:dict(rows=reader(code,target),source=source,provider_attempts=['BaoStock.verified_batch_capture']),
            reader,trading_day_fn=rc.is_trading_day,max_attempts=1,retry_delay_sec=0)
    ok,reasons = g6.validate_completeness_evidence(result,snapshot,snapshot['season_id'],snapshot['trade_date'],now=now())
    require(ok,'RECOVERY_G6_REJECTED:'+','.join(reasons))
    return result


def publish_date(context, target, capture_root, capture):
    base = Path(context['season_root'])/'market_data_completeness'
    run = base/'recovery_runs'/target/run_id()
    run.mkdir(parents=True,exist_ok=False)
    v2 = build_v2_snapshot(capture,trade_date=target)
    g6.atomic_json_write(run/'v2_snapshot.json',v2)
    snapshot,codes = prepare_g6_snapshot(v2,trade_date=target,season_id=context['season_id'],completeness_root=run/'formal_g6')
    bars = captured_bars(capture,codes,target)
    validate_trusted(v2,capture,capture_root,run,target)
    from datasource import db as hdb
    stage_db = run/'staged_market.db'
    with closing(sqlite3.connect(stage_db)) as db, db:
        db.executescript(hdb.SCHEMA)
        db.executemany(INSERT_BAR,bars)
    g6.persist_universe_snapshot(g6.universe_snapshot_path(run/'stage_g6',target),snapshot)
    validate_database(stage_db,snapshot,run/'stage_g6',capture['sources']['daily']['source_id'])
    # A crash between database commit and evidence publication is resumable:
    # equal bars are verified and retained; conflicting bars are never replaced.
    inserted = insert_verified_bars(rc.DB_PATH,bars)
    g6.persist_universe_snapshot(g6.universe_snapshot_path(run/'formal_g6',target),snapshot)
    formal = validate_database(rc.DB_PATH,snapshot,run/'formal_g6',capture['sources']['daily']['source_id'])
    paths = dict(snapshot=Path(g6.universe_snapshot_path(base,target)),v2=base/'v2_universe_snapshots'/f'{target}.json',
                 evidence=base/f'{target}.json')
    for name,path in paths.items():
        archive_legacy_artifact(path,run/'previous_canonical'/f'{name}.json')
    formal['universe_snapshot_path'] = str(paths['snapshot'])
    g6.atomic_json_write(paths['snapshot'],snapshot)
    g6.atomic_json_write(paths['v2'],v2)
    g6.atomic_json_write(paths['evidence'],formal)
    acceptance = dict(schema_version='v7-dated-g6-recovery-acceptance-v1',season_id=context['season_id'],
        trade_date=target,run_id=run.name,at=now().isoformat(),status='PASS',
        supersedes_event_path=str(base/'health'/f'{target}.json'),original_event_retained=True,
        recovery_capture_manifest=str(capture_root/'capture_manifest.json'),capture_manifest_sha256=sha(capture_root/'capture_manifest.json'),
        formal_g6_path=str(paths['evidence']),formal_g6_sha256=sha(paths['evidence']),
        trusted_validator_path=str(run/'trusted_validator_result.json'),trusted_validator_sha256=sha(run/'trusted_validator_result.json'),
        expected_count=len(codes),real_trading=False,economic_writes=0,
        authorization='Owner-approved trading stall repair; daily data maintenance')
    g6.atomic_json_write(base/'recovery_acceptances'/f'{target}-{run.name}.json',acceptance)
    result = dict(status='PASS',trade_date=target,inserted_rows=inserted,expected_count=len(codes),
                  capture_root=str(capture_root),run_root=str(run),economic_writes=0)
    g6.atomic_json_write(run/'publish_result.json',result)
    return result


def recover_backlog(*, target_date=None, max_dates=20, max_seconds=1800):
    from competition.season_runtime import resolve_active_season_context
    from ai_quant.cache_rollover import run as rollover, validate_g6, approved_daily_protocol_limit
    from ai_quant.production_input import QualifiedQFQInput
    context = resolve_active_season_context()
    safety = read(context['governance_safety_path'])
    require(safety.get('real_trading') is False,'RECOVERY_REAL_TRADING_INVARIANT')
    target = target_date or completed_trade_date()
    target = dt.datetime.strptime(target,'%Y%m%d').strftime('%Y%m%d')
    require(target <= completed_trade_date(),'RECOVERY_UNCOMPLETED_DATE')
    require(rc.trading_calendar_status(dt.datetime.strptime(target,'%Y%m%d').date()).get('trading_day') is True,
            'RECOVERY_TARGET_NOT_TRADING_DATE')
    require(1 <= max_dates <= 30 and 1 <= max_seconds <= 3600,'RECOVERY_BOUNDS_INVALID')
    base = Path(context['season_root'])/'market_data_completeness'
    results = []
    started = time.monotonic()
    latest = dict(status='HOLD',target_date=target,season_id=context['season_id'],economic_writes=0)
    try:
        with daily_g6_lock(Path(rc.STATE_DIR)/'daily_g6.lock'):
            parent = QualifiedQFQInput()
            dates = missing_dates(parent.cutoff,target)
            latest['initial_cutoff'] = parent.cutoff
            for date in dates[:max_dates]:
                require(time.monotonic()-started < max_seconds,'RECOVERY_BATCH_TIME_BUDGET_EXHAUSTED')
                try:
                    validate_g6(context,date)
                    receipts = sorted((base/'recovery_acceptances').glob(date+'-*.json'),reverse=True)
                    require(bool(receipts),'RECOVERY_CAPTURE_RECEIPT_REQUIRED')
                    capture_root = Path(read(receipts[0])['recovery_capture_manifest']).parent
                    verify_capture(capture_root,date)
                    data_result = dict(status='ALREADY_VALID',trade_date=date,capture_root=str(capture_root))
                except (ActiveG6RouteHold,RuntimeError,ValueError,KeyError,OSError):
                    capture_root,capture = capture_for_date(base,date)
                    data_result = publish_date(context,date,capture_root,capture)
                factor_limit = approved_daily_protocol_limit(date)
                cache = rollover(SimpleNamespace(trade_date=date,capture_root=capture_root,
                    output_root=ROOT/'data/ai_quant/input_cache',collect_factors=True,factor_bundle=None,
                    max_protocol_calls=factor_limit,daily_protocol_limit=factor_limit,
                    cumulative_protocol_limit=0,daily_operations=True,publish=True))
                results.append(dict(trade_date=date,data=data_result,aiq_cache=cache))
                latest.update(cutoff=QualifiedQFQInput().cutoff,dates=results)
                g6.atomic_json_write(base/'recovery_latest.json',latest)
            cutoff = QualifiedQFQInput().cutoff
            require(cutoff == target,'RECOVERY_BACKLOG_REMAINS')
            # Also verify the final admission when the pointer already matched.
            validate_g6(context,target)
            latest.update(status='PASS',cutoff=cutoff,reason_codes=[])
    except Exception as exc:
        latest.update(status='HOLD',reason_codes=[str(exc)],error_type=type(exc).__name__,
                      details=getattr(exc,'details',{}))
    latest.update(dates=results,completed_at=now().isoformat(),elapsed_seconds=round(time.monotonic()-started,2))
    g6.atomic_json_write(base/'recovery_latest.json',latest)
    return latest


def inspect_backlog(*, target_date=None):
    """Read-only dry-run: no provider calls, locks, files or database writes."""
    from competition.season_runtime import resolve_active_season_context
    from ai_quant.production_input import QualifiedQFQInput
    context = resolve_active_season_context()
    parent = QualifiedQFQInput()
    target = target_date or completed_trade_date()
    target = dt.datetime.strptime(target,'%Y%m%d').strftime('%Y%m%d')
    require(target <= completed_trade_date(),'RECOVERY_UNCOMPLETED_DATE')
    dates = missing_dates(parent.cutoff,target)
    base = Path(context['season_root'])/'market_data_completeness'
    return dict(status='DRY_RUN',season_id=context['season_id'],cutoff=parent.cutoff,target_date=target,
        dates=[dict(trade_date=day,
            saved_capture_attempts=read(base/'recovery_runs'/day/'capture_budget.json')['attempts']
                if (base/'recovery_runs'/day/'capture_budget.json').exists() else [],
            has_formal_g6=(base/f'{day}.json').is_file()) for day in dates],
        network_requests=0,database_writes=0,economic_writes=0,pointer_published=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target-date',help='Explicit completed trading date YYYYMMDD')
    parser.add_argument('--max-dates',type=int,default=20)
    parser.add_argument('--dry-run',action='store_true',help='Inspect missing dates without network or writes')
    args = parser.parse_args()
    from daemon.trading_daemon import data_runtime_preflight
    ok,preflight = data_runtime_preflight('daily_review')
    if not ok:
        result = dict(status='HOLD',preflight=preflight)
    elif args.dry_run:
        result = inspect_backlog(target_date=args.target_date)
    else:
        result = recover_backlog(target_date=args.target_date,max_dates=args.max_dates)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['status'] in ('PASS','DRY_RUN') else 2


if __name__ == '__main__':
    raise SystemExit(main())
