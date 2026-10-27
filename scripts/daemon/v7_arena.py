"""Read-only active-season Arena view. No engine calls, writers or price substitution."""
import datetime as dt
import hashlib
import json
import math
import re
from pathlib import Path

VERSION = 'arena-ledger-view-v1'
TZ = dt.timezone(dt.timedelta(hours=8))
MONEY = ('initial_capital', 'cash', 'market_value', 'total_equity', 'net_pnl',
         'net_pnl_pct', 'max_drawdown_pct')


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def code(value):
    match = re.fullmatch(r'(?:sh|sz|bj)?(?:\.)?(\d{6})', str(value or '').lower())
    return match.group(1) if match else None


def timestamp(value):
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.replace(tzinfo=TZ) if parsed.tzinfo is None else parsed
    except (ValueError, TypeError):
        return None


class Reader:
    def __init__(self, now):
        self.now = now
        self.sources = {}
        self.warnings = []

    def read(self, path, kind=dict, jsonl=False, required=True):
        path = Path(path)
        try:
            raw = path.read_bytes()
            value = [json.loads(line) for line in raw.decode('utf-8-sig').splitlines() if line.strip()] if jsonl else json.loads(raw)
            if not isinstance(value, kind) or (jsonl and any(not isinstance(row, dict) for row in value)):
                raise ValueError('invalid structure')
            self.sources[str(path)] = {'sha256': hashlib.sha256(raw).hexdigest(),
                'file_modified_at': dt.datetime.fromtimestamp(path.stat().st_mtime, TZ).isoformat()}
            return value
        except (OSError, ValueError, UnicodeError) as exc:
            if required:
                self.warnings.append(f'{path.name}: {type(exc).__name__}')
            return None

    def stable(self):
        for path, evidence in self.sources.items():
            try:
                if hashlib.sha256(Path(path).read_bytes()).hexdigest() != evidence['sha256']:
                    self.warnings.append('CONCURRENT_SOURCE_CHANGE: ' + Path(path).name)
                    return False
            except OSError:
                return False
        return True


def current_status(safety, forward=None, aiq=False):
    fields = ('execution_state_safe', 'new_entry_disabled', 'trading_enabled', 'risk_exit_enabled', 'real_trading')
    if not isinstance(safety, dict) or not all(isinstance(safety.get(k), bool) for k in fields):
        return 'UNKNOWN'
    if safety['real_trading'] or not safety['execution_state_safe'] or safety['new_entry_disabled'] or not safety['trading_enabled']:
        return 'HOLD'
    if aiq:
        if not isinstance(forward, dict) or not isinstance(forward.get('forward_enabled'), bool):
            return 'UNKNOWN'
        if not forward['forward_enabled'] or forward.get('status') != 'ACTIVE':
            return 'HOLD'
    return 'ACTIVE'


def position_view(row, names, quote_evidence):
    sym = code(row.get('code') or row.get('symbol'))
    shares = number(row.get('shares')); price = number(row.get('current_price')); cost = number(row.get('cost_price'))
    valid = shares is not None and shares > 0 and price is not None and price > 0 and cost is not None and cost > 0
    # Arithmetic is a display derivation from persisted marks, never an economic writer.
    value = round(shares * price, 2) if valid else None
    recorded = number(row.get('position_value'))
    if recorded is not None and value is not None and abs(recorded - value) > .05:
        valid = False; value = None
    quote = quote_evidence.get(sym, {})
    if not isinstance(quote, dict): quote = {}
    quote_price = number(quote.get('market_price', quote.get('price')))
    source_time = (quote.get('source_time') or quote.get('observed_at')) if valid and quote_price == price else row.get('price_source_time')
    return {'symbol': sym, 'name': row.get('name') or names.get(sym), 'shares': shares,
        'buy_date': row.get('buy_date'), 'cost_price': cost, 'current_price': price if valid else None,
        'price_source_time': source_time, 'price_time_status': 'RECORDED' if timestamp(source_time) else 'UNKNOWN',
        'price_basis': 'PERSISTED_LEDGER_MARK', 'price_provider': quote.get('provider') if source_time else None,
        'position_value': value, 'unrealized_pnl': round(shares * (price-cost), 2) if valid else None,
        'unrealized_pnl_pct': round((price/cost-1)*100, 4) if valid else None,
        'data_status': 'VERIFIED' if valid and sym else 'UNKNOWN'}


def transactions(orders, fills, closed, names):
    """Project successful buy/sell fills; unfilled orders stay in the audit ledger."""
    fill_map = {}
    for fill in fills:
        fill_map.setdefault(fill.get('order_id'), []).append(fill)
    reasons = {row.get('transaction_id'): row.get('exit_reason') for row in closed if row.get('exit_reason')}
    result = []
    seen = set()
    def view(order, fill=None):
        actual = fill or order; sym = code(actual.get('symbol') or order.get('symbol'))
        commission = number(fill.get('commission')) if fill else None
        tax = number(fill.get('stamp_tax')) if fill else None
        reason = actual.get('reason') or actual.get('exit_reason') or order.get('reason') or reasons.get(actual.get('transaction_id'))
        at = actual.get('executed_at') or actual.get('timestamp') or order.get('created_at')
        return {'record_id': actual.get('fill_id') or order.get('order_id'), 'order_id': order.get('order_id'),
            'fill_id': fill.get('fill_id') if fill else None, 'symbol': sym,
            'name': actual.get('name') or order.get('name') or names.get(sym),
            'direction': actual.get('direction') or order.get('direction'), 'timestamp': at,
            'trade_date': actual.get('trade_date'), 'shares': number(fill.get('shares')) if fill else None,
            'price': number(fill.get('price')) if fill else None,
            'requested_shares': number(order.get('requested_shares')), 'requested_price': number(order.get('requested_price')),
            'commission': commission, 'stamp_tax': tax,
            'fees': round(commission+tax, 4) if commission is not None and tax is not None else None,
            'simulated_slippage': number(fill.get('simulated_slippage')) if fill else None,
            'execution_status': 'FILLED' if fill else order.get('status', 'UNKNOWN'),
            'reason': reason, 'decision_id': actual.get('decision_id') or order.get('decision_id'),
            'evidence_kind': 'FILL' if fill else 'ORDER_WITHOUT_FILL'}
    for order in orders:
        linked = fill_map.get(order.get('order_id'), [])
        if linked:
            for fill in linked:
                result.append(view(order, fill)); seen.add(fill.get('fill_id'))
    for fill in fills:
        if fill.get('fill_id') not in seen:
            result.append(view({'order_id': fill.get('order_id')}, fill))
    result = [row for row in result if row['direction'] in ('BUY', 'SELL')]
    result.sort(key=lambda row: timestamp(row['timestamp']) or dt.datetime.min.replace(tzinfo=TZ), reverse=True)
    return result


def build_arena_view(context, now=None, participant_id=None, offset=0, limit=20):
    now = now or dt.datetime.now(TZ)
    reader = Reader(now); sid = context['season_id']
    snapshot = reader.read(context['competition_snapshot_path'])
    if snapshot is not None and (snapshot.get('season_id') != sid or not isinstance(snapshot.get('accounts'), dict)):
        reader.warnings.append('INVALID_ARENA_SNAPSHOT_IDENTITY_OR_STRUCTURE'); snapshot = None
    safety = reader.read(context['governance_safety_path'])
    forward = reader.read(Path(context['forward_root'])/'forward_epoch_manifest.json')
    if isinstance(safety, dict) and safety.get('season_id') != sid:
        safety = None; reader.warnings.append('GOVERNANCE_IDENTITY_MISMATCH')
    if isinstance(forward, dict) and (forward.get('season_id') != sid or forward.get('forward_epoch_id') != context['forward_epoch_id']):
        forward = None; reader.warnings.append('FORWARD_IDENTITY_MISMATCH')
    roster = context['manifest'].get('participants', [])
    quote_evidence = {}; names = {}
    metadata = reader.read(Path(context['season_root']).parents[2]/'data/stock_universe_mainboard.json', required=False)
    if isinstance(metadata, dict):
        for row in metadata.get('stocks', []):
            if isinstance(row, dict) and row.get('name'):
                names[code(row.get('code') or row.get('symbol'))] = row['name']
    # Only already saved provenance is used; no network quote retrieval in this view.
    telemetry = Path(context['forward_root'])/'telemetry'/f'forward_telemetry_{now:%Y%m%d}.jsonl'
    records = reader.read(telemetry, list, jsonl=True, required=False)
    latest_cycle = records[-1] if records else reader.read(telemetry.parent/'latest.json', required=False)
    if latest_cycle:
        quote_evidence = latest_cycle.get('quote_evidence', {})
        if not isinstance(quote_evidence, dict): quote_evidence = {}
    execution_health = arena_execution_health(context, now, latest_cycle)
    if execution_health['new_entry_data_status'] != 'ADMITTED':
        reader.warnings.append('ENTRY_DATA_HOLD: '+execution_health['new_entry_reason'])
    if (latest_cycle or {}).get('execution_status') not in (None, 'SUCCESS'):
        reader.warnings.append('AIQ_CYCLE: '+str(latest_cycle.get('execution_status')))
    players = []
    for participant in roster:
        if not participant.get('return_bearing') or not participant.get('ranking_enabled'):
            continue
        pid = participant['participant_id']; aiq = pid == 'P-AI-QUANT'
        root = Path(context['ai_quant_paper_root']) if aiq else Path(context['competition_ledger_root'])/pid
        account = reader.read(root/'account.json'); positions = reader.read(root/'positions.json', list)
        orders = reader.read(root/'orders.jsonl', list, jsonl=True)
        fills = reader.read(root/'fills.jsonl', list, jsonl=True)
        closed = reader.read(root/'paper_trade_results.jsonl', list, jsonl=True)
        expected_account = participant['new_account_id']
        def identity(row):
            return isinstance(row, dict) and row.get('season_id') == sid and row.get('participant_id') == pid and row.get('account_id') == expected_account
        valid = identity(account) and positions is not None and orders is not None and fills is not None and closed is not None
        if valid:
            valid = all(identity(row) for rows in (positions, orders, fills, closed) for row in rows)
        valid = valid and all(number(account.get(k)) is not None for k in MONEY)
        if not valid: reader.warnings.append(pid+': MISSING_CORRUPT_OR_IDENTITY_INVALID_LEDGER')
        for row in (positions or [])+(fills or []):
            if isinstance(row, dict) and row.get('name'): names[code(row.get('code') or row.get('symbol'))] = row['name']
        pos = [position_view(row, names, quote_evidence if aiq else {}) for row in positions] if valid else None
        for row in pos or []:
            quote_at = timestamp(row['price_source_time'])
            quote_age = (now-quote_at).total_seconds() if quote_at else None
            row['price_age_seconds'] = round(quote_age,1) if quote_age is not None else None
            row['quote_freshness'] = 'UNKNOWN' if quote_age is None else ('FRESH' if 0 <= quote_age <= 90 else 'STALE')
        valid = valid and all(row['data_status'] == 'VERIFIED' for row in pos)
        if valid and (abs(account['total_equity']-account['cash']-account['market_value']) > .05 or account['cash'] < 0):
            valid = False; reader.warnings.append(pid+': BOOK_IDENTITY_INVALID')
        snap_account = (snapshot or {}).get('accounts', {}).get(pid)
        parity = 'UNKNOWN'
        if valid and isinstance(snap_account, dict) and snapshot.get('season_id') == sid:
            parity = 'MATCH' if all(snap_account.get(k) == account.get(k) for k in MONEY) and snap_account.get('positions') == positions else 'LAGGING'
        source = reader.sources.get(str(root/'account.json'), {})
        ledger_at = account.get('updated_at') if valid else None
        file_at = source.get('file_modified_at')
        ledger_at = ledger_at if timestamp(ledger_at) else file_at
        history = transactions(orders, fills, closed, names) if valid else None
        item = {'player_id': pid, 'name': participant.get('display_name'), 'role': participant.get('role'),
            'season_id': sid, 'account_id': expected_account, 'account_type': 'ACTIVE_SEASON_FORMAL_LEDGER',
            **{k: number(account.get(k)) if valid else None for k in MONEY},
            'positions_count': len(positions) if valid else None, 'positions': pos if valid else None,
            'orders_count': len(orders) if valid else None, 'fills_count': len(fills) if valid else None,
            'status': current_status(safety, forward, aiq) if valid else 'UNKNOWN',
            'data_status': 'VERIFIED' if valid else 'UNKNOWN', 'projection_status': parity,
            'ledger_updated_at': ledger_at, 'ledger_time_kind': 'RECORDED_UPDATED_AT' if valid and timestamp(account.get('updated_at')) else 'FILE_MODIFIED_AT',
            'transactions': history[:20] if history is not None else None,
            'transactions_total': len(history) if history is not None else None}
        item['valuation_status'] = ('CURRENT_VERIFIED' if not pos or all(p['quote_freshness']=='FRESH' for p in pos)
                                    else 'STALE_OR_UNKNOWN_MARK') if valid else 'UNKNOWN'
        if item['valuation_status'] == 'STALE_OR_UNKNOWN_MARK':
            reader.warnings.append(pid+': POSITION_MARK_STALE_OR_UNKNOWN')
        if aiq:
            item['execution_health'] = execution_health['aiq']
        if pid in ('P-HANSHU','P-COOL'):
            item['strategy_status'] = 'DISABLED_PENDING_RULE_DEFINITION'
            item['strategy_reason'] = '规则口径待统一，证据接入未完成，当前不启用新开仓。'
            if item['status'] == 'ACTIVE':
                item['status'] = 'STRATEGY_DISABLED'
        if pid == participant_id and history is not None:
            item['transactions'] = history[offset:offset+limit]; item['offset'] = offset
            item['has_more'] = offset+limit < len(history)
        players.append(item)
    players.sort(key=lambda row: (row['net_pnl_pct'] is None, -(row['net_pnl_pct'] or 0)))
    rank = 0
    for row in players:
        if row['net_pnl_pct'] is not None: rank += 1
        row['rank'] = rank if row['net_pnl_pct'] is not None else None
    stable = reader.stable()
    if not stable:
        for row in players:
            for k in MONEY: row[k] = None
            row.update(data_status='UNKNOWN', status='UNKNOWN', positions=None, positions_count=None, transactions=None, transactions_total=None)
    # Snapshot publish/file time is data evidence, not request time or a quote timestamp.
    snap_source = reader.sources.get(str(Path(context['competition_snapshot_path'])), {})
    source_at = (snapshot or {}).get('updated_at') or snap_source.get('file_modified_at')
    parsed = timestamp(source_at)
    if parsed and parsed > now+dt.timedelta(seconds=5):
        reader.warnings.append('FUTURE_SOURCE_TIMESTAMP'); parsed = None
    age = max(0, (now-parsed).total_seconds()) if parsed else None
    in_session = now.weekday()<5 and (dt.time(9,30)<=now.time().replace(tzinfo=None)<=dt.time(11,30) or dt.time(13)<=now.time().replace(tzinfo=None)<=dt.time(15))
    stale = age is None or age > (600 if in_session else 86400)
    freshness = {'source_updated_at': source_at, 'source_time_kind': 'SNAPSHOT_UPDATED_AT' if (snapshot or {}).get('updated_at') else 'FILE_MODIFIED_AT',
        'source_age_seconds': round(age,1) if age is not None else None,
        'freshness_status': 'UNKNOWN' if age is None else ('STALE' if stale else 'FRESH'), 'stale': stale}
    for row in players:
        row['stale'] = stale or row.get('valuation_status') == 'STALE_OR_UNKNOWN_MARK'
        if row['stale'] and row['data_status'] == 'VERIFIED': row['data_status'] = 'STALE'
    data = {'view_version': VERSION, 'season_id': sid, 'primary_metric': 'NET_PNL_PERCENT',
        'players_count': len(players), 'leaderboard': players, 'governance': {k:(safety or {}).get(k) for k in
            ('trading_enabled','execution_state_safe','new_entry_disabled','risk_exit_enabled','real_trading')},
        'comparability_warning': '按正式账本收益率排序；竞技场投影单独核对。反指哨兵不参与排名。',
        'execution_health': execution_health,
        'source_evidence': reader.sources, 'sentinel': {'ranking_enabled': False, 'trading_account': False}}
    if participant_id:
        data = next((row for row in players if row['player_id'] == participant_id), None)
    return {'schema_version': 'v7.1', 'generated_at': now.isoformat(), 'status': 'OK' if stable and not reader.warnings and not stale else 'DEGRADED',
        'warnings': reader.warnings, 'data_freshness': freshness, 'data': data}


def arena_execution_health(context, now, latest_cycle):
    """Observe entry admission separately from process and snapshot liveness."""
    try:
        from competition.data_admission import latest_completed_data_admission
        allowed, reason = latest_completed_data_admission(context, now)
    except Exception as exc:
        allowed, reason = False, 'DATA_ADMISSION_UNAVAILABLE:'+type(exc).__name__
    cycle = latest_cycle or {}
    at = timestamp(cycle.get('timestamp'))
    age = (now-at).total_seconds() if at else None
    return {'new_entry_data_status': 'ADMITTED' if allowed else 'HOLD',
        'new_entry_reason': reason,
        'aiq': {**{k:cycle.get(k) for k in ('timestamp','execution_status','entry_status',
            'entry_reason','exit_check_status','history_cutoff','market_value_status')},
            'cycle_age_seconds': round(age,1) if age is not None else None,
            'cycle_freshness': 'UNKNOWN' if age is None else ('FRESH' if 0<=age<=600 else 'STALE')}}
