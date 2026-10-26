# -*- coding: utf-8 -*-
"""Minimal crash-detectable transaction writer for strategy ledgers.

The writer is intentionally smaller than the AI Quant execution framework.
It gives each participant ledger an append-only order/fill trail, an atomic
idempotency state file, atomic account/positions effects, and an equity
snapshot.  The final registry transition to COMMITTED is the last step; any
interruption before it is therefore observable by reconciliation.
"""

import datetime
import hashlib
import json
import os
import uuid

from ai_quant.storage_utils import (
    ACCOUNT_FIELD_TYPES,
    ACCOUNT_REQUIRED_FIELDS,
    CriticalStateStatus,
    atomic_write_json,
    strict_load_json,
    strict_read_jsonl,
    validate_positions_schema,
)


TRANSACTION_STATES = ('PREPARED', 'EFFECTS_WRITTEN', 'COMMITTED', 'RECOVERY_REQUIRED')
ORDER_STATUSES = ('CREATED', 'FILLED', 'REJECTED')


class TransactionIntegrityError(RuntimeError):
    """A transaction cannot be safely committed or reconciled."""


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _json_hash(value):
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()


def _append_jsonl(path, record):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n')
        f.flush()
        os.fsync(f.fileno())


def _read_jsonl(path):
    result = strict_read_jsonl(path)
    if result.status != CriticalStateStatus.VALID:
        raise TransactionIntegrityError(f'{os.path.basename(path)} {result.status}: {result.error}')
    return list(result.value)


def _registry(path):
    result = strict_load_json(path, expected_type=dict, required_fields=['records'], field_types={'records': dict})
    if not result.is_valid:
        raise TransactionIntegrityError(f'{os.path.basename(path)} {result.status}: {result.error}')
    return result.value


def _account_effect(account, positions):
    effect = dict(account)
    effect['positions'] = positions
    return effect


def _validate_account_effect(account, positions):
    if not isinstance(account, dict):
        raise TransactionIntegrityError('account effect must be an object')
    for field in ACCOUNT_REQUIRED_FIELDS:
        if field not in account:
            raise TransactionIntegrityError(f'account effect missing {field}')
    for field, typ in ACCOUNT_FIELD_TYPES.items():
        value = account.get(field)
        if typ is str:
            if not isinstance(value, str):
                raise TransactionIntegrityError(f'account effect {field} must be string')
        elif isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TransactionIntegrityError(f'account effect {field} must be numeric')
    if not isinstance(positions, list):
        raise TransactionIntegrityError('positions effect must be a list')
    valid, error = validate_positions_schema(positions)
    if not valid:
        raise TransactionIntegrityError(error)
    market_value = sum(float(p.get('shares', 0)) * float(p.get('current_price', p.get('cost_price', 0))) for p in positions)
    if abs(float(account['market_value']) - market_value) > 0.05:
        raise TransactionIntegrityError('account market_value does not match positions')
    if abs(float(account['total_equity']) - (float(account['cash']) + market_value)) > 0.05:
        raise TransactionIntegrityError('account total_equity does not equal cash + market_value')
    if abs(float(account['net_pnl']) - (float(account['total_equity']) - float(account['initial_capital']))) > 0.05:
        raise TransactionIntegrityError('account net_pnl does not equal total_equity - initial_capital')


def _identity(trade, account, decision=None):
    decision = decision or {}
    participant_id = trade.get('participant_id') or decision.get('participant_id') or account.get('participant_id')
    season_id = trade.get('season_id') or decision.get('season_id') or account.get('season_id')
    account_id = trade.get('account_id') or account.get('account_id')
    direction = str(trade.get('direction') or trade.get('action') or '').upper()
    symbol = trade.get('symbol')
    decision_id = trade.get('decision_id') or decision.get('decision_id')
    if not decision_id:
        decision_id = f'LEGACY-{participant_id}-{direction}-{symbol}-{trade.get("date", "")}-{trade.get("ts", "")}'
    key = trade.get('idempotency_key') or f'{season_id}:{participant_id}:{decision_id}:{direction}'
    return participant_id, season_id, account_id, direction, symbol, str(decision_id), str(key)


def _bind_buy_entry_reference(positions, trade, transaction_id, order_id, fill_id, decision_id):
    """Bind a BUY identity to exactly one post-effect position before writing it."""
    plan_id = trade.get('plan_id')
    candidates = []
    for position in positions:
        if position.get('entry_transaction_id'):
            continue
        if plan_id and position.get('plan_id') == plan_id:
            candidates.append(position)
        elif (position.get('symbol') == trade.get('symbol')
              and int(position.get('shares', 0)) == int(trade.get('shares', 0))
              and str(position.get('buy_date', '')) == str(trade.get('date', trade.get('buy_date', '')))):
            candidates.append(position)
    if len(candidates) != 1:
        raise TransactionIntegrityError(
            f'BUY position binding is ambiguous or missing: candidates={len(candidates)}'
        )
    position = candidates[0]
    position['entry_transaction_id'] = transaction_id
    position['entry_order_id'] = order_id
    position['entry_fill_id'] = fill_id
    position['entry_decision_id'] = decision_id


def _require_sell_entry_reference(trade):
    if not trade.get('entry_transaction_id'):
        raise TransactionIntegrityError('SELL requires entry_transaction_id under the active-season contract')


def commit_strategy_transaction(ledger_root, account, positions, trade, decision=None):
    """Commit one already safety-approved economic mutation.

    ``account`` and ``positions`` are the post-effect values computed by the
    existing fill logic.  This function owns all active-ledger IO and never
    rewrites append-only history files.
    """
    participant_id, season_id, account_id, direction, symbol, decision_id, idem_key = _identity(trade, account, decision)
    if direction not in ('BUY', 'SELL'):
        raise TransactionIntegrityError(f'unsupported direction: {direction}')
    if direction == 'BUY':
        # Final authorization boundary directly before transaction/economic IO.
        # Do not create even a rejected-order row for a stale or invalid decision.
        from competition.activation_boundary import check_new_entry_boundary, decision_timestamp_from
        decision_at = decision_timestamp_from(
            {'decision_timestamp': trade.get('decision_timestamp')},
            decision,
            trade.get('skill_decision'),
        )
        decision_season_ids = [
            trade.get('decision_season_id'), trade.get('season_id'),
            decision.get('season_id') if isinstance(decision, dict) else None,
            trade.get('skill_decision', {}).get('season_id')
            if isinstance(trade.get('skill_decision'), dict) else None,
        ]
        boundary = check_new_entry_boundary(
            decision_at, account=account, decision_season_ids=decision_season_ids,
            ledger_root=ledger_root,
        )
        if not boundary.allowed:
            raise TransactionIntegrityError(f'BUY blocked by activation/session boundary: {boundary.reason}')
    try:
        shares = int(trade['shares'])
        price = float(trade['price'])
    except (KeyError, TypeError, ValueError) as exc:
        raise TransactionIntegrityError(f'invalid fill quantity/price: {exc}') from exc
    if shares <= 0 or price <= 0:
        raise TransactionIntegrityError('shares and price must be positive')
    _validate_account_effect(account, positions)

    registry_path = os.path.join(ledger_root, 'processed_idempotency_registry.json')
    orders_path = os.path.join(ledger_root, 'orders.jsonl')
    fills_path = os.path.join(ledger_root, 'fills.jsonl')
    results_path = os.path.join(ledger_root, 'paper_trade_results.jsonl')
    equity_path = os.path.join(ledger_root, 'equity_curve.jsonl')
    registry = _registry(registry_path)
    if registry.get('season_id') != season_id or registry.get('participant_id') != participant_id or registry.get('account_id') != account_id:
        raise TransactionIntegrityError('registry identity mismatch')
    existing = registry.get('records', {}).get(idem_key)
    if existing:
        if existing.get('status') == 'COMMITTED':
            return {'status': 'DUPLICATE', **{k: existing.get(k) for k in ('transaction_id', 'order_id', 'fill_id', 'decision_id')}}
        raise TransactionIntegrityError(f'idempotency key has non-COMMITTED state: {existing.get("status")}')

    orders = _read_jsonl(orders_path)
    fills = _read_jsonl(fills_path)
    order_ids = {x.get('order_id') for x in orders}
    fill_ids = {x.get('fill_id') for x in fills}
    transaction_id = trade.get('transaction_id') or f'TX-{uuid.uuid4().hex}'
    order_id = trade.get('order_id') or f'ORD-{uuid.uuid4().hex}'
    fill_id = trade.get('fill_id') or f'FILL-{uuid.uuid4().hex}'
    if order_id in order_ids or fill_id in fill_ids:
        raise TransactionIntegrityError('duplicate order_id or fill_id')
    if direction == 'SELL':
        _require_sell_entry_reference(trade)
    timestamp = trade.get('ts') or _now()
    gross = round(shares * price, 4)
    commission = float(trade.get('commission', trade.get('fee', 0.0)))
    stamp_tax = float(trade.get('stamp_tax', 0.0))
    fees = round(commission + stamp_tax, 4)
    net_cash_effect = round(-(gross + fees) if direction == 'BUY' else gross - fees, 4)
    order = {
        'order_id': order_id, 'transaction_id': transaction_id, 'decision_id': decision_id,
        'season_id': season_id, 'participant_id': participant_id, 'account_id': account_id,
        'symbol': symbol, 'direction': direction, 'requested_shares': shares,
        'requested_price': price, 'created_at': timestamp, 'status': 'FILLED',
    }
    fill = {
        'fill_id': fill_id, 'order_id': order_id, 'transaction_id': transaction_id,
        'decision_id': decision_id, 'season_id': season_id, 'participant_id': participant_id,
        'account_id': account_id, 'symbol': symbol, 'direction': direction,
        'shares': shares, 'price': price, 'commission': round(commission, 4),
        'stamp_tax': round(stamp_tax, 4), 'gross_amount': gross,
        'net_cash_effect': net_cash_effect, 'trade_date': trade.get('date'),
        'executed_at': timestamp,
    }
    transaction = {
        'status': 'PREPARED', 'transaction_id': transaction_id, 'order_id': order_id,
        'fill_id': fill_id, 'decision_id': decision_id, 'participant_id': participant_id,
        'season_id': season_id, 'account_id': account_id, 'direction': direction,
        'symbol': symbol, 'idempotency_key': idem_key, 'created_at': _now(),
    }
    if direction == 'BUY':
        _bind_buy_entry_reference(positions, trade, transaction_id, order_id, fill_id, decision_id)
    registry['records'][idem_key] = transaction
    registry['total_keys'] = len(registry['records'])
    atomic_write_json(registry_path, registry)

    # Append-only history is written in the order contract: order then fill.
    _append_jsonl(orders_path, order)
    _append_jsonl(fills_path, fill)

    result = None
    if direction == 'SELL' and trade.get('closed_trade', True):
        entry_price = float(trade.get('entry_price', trade.get('cost_price', price)))
        entry_date = trade.get('entry_date', trade.get('buy_date'))
        net_pnl = float(trade.get('net_pnl', (price - entry_price) * shares - fees))
        result = {
            'transaction_id': transaction_id, 'open_transaction_id': trade.get('entry_transaction_id'),
            'season_id': season_id, 'participant_id': participant_id, 'account_id': account_id,
            'symbol': symbol, 'buy_date': entry_date, 'exit_date': trade.get('date'),
            'shares': shares, 'entry_price': entry_price, 'exit_price': price,
            'net_pnl': round(net_pnl, 2),
            'pnl_pct': round((net_pnl / (entry_price * shares)) * 100, 4) if entry_price and shares else 0.0,
            'exit_reason': trade.get('reason', ''),
        }
        _append_jsonl(results_path, result)

    post_account_hash = _json_hash(_account_effect(account, positions))
    atomic_write_json(os.path.join(ledger_root, 'account.json'), account)
    atomic_write_json(os.path.join(ledger_root, 'positions.json'), positions)
    _append_jsonl(equity_path, {
        'timestamp': timestamp, 'season_id': season_id, 'participant_id': participant_id,
        'account_id': account_id, 'cash': round(float(account['cash']), 2),
        'market_value': round(float(account['market_value']), 2),
        'total_equity': round(float(account['total_equity']), 2),
        'net_pnl': round(float(account['net_pnl']), 2), 'position_count': len(positions),
        'transaction_id': transaction_id, 'post_account_hash': post_account_hash,
    })

    transaction['status'] = 'EFFECTS_WRITTEN'
    transaction['post_account_hash'] = post_account_hash
    transaction['updated_at'] = _now()
    registry['records'][idem_key] = transaction
    atomic_write_json(registry_path, registry)

    transaction['status'] = 'COMMITTED'
    transaction['updated_at'] = _now()
    registry['records'][idem_key] = transaction
    atomic_write_json(registry_path, registry)
    return {'status': 'COMMITTED', 'transaction_id': transaction_id, 'order_id': order_id, 'fill_id': fill_id, 'decision_id': decision_id, 'idempotency_key': idem_key, 'result': result}


def reconcile_transaction_ledger(ledger_root):
    """Return PASS/FAIL for all transaction relationships in one ledger."""
    findings = []
    account_res = strict_load_json(os.path.join(ledger_root, 'account.json'), expected_type=dict)
    positions_res = strict_load_json(os.path.join(ledger_root, 'positions.json'), expected_type=list, validator=validate_positions_schema)
    registry_res = strict_load_json(os.path.join(ledger_root, 'processed_idempotency_registry.json'), expected_type=dict, required_fields=['records'], field_types={'records': dict})
    if not account_res.is_valid: findings.append(f'account {account_res.status}')
    if not positions_res.is_valid: findings.append(f'positions {positions_res.status}')
    if not registry_res.is_valid: findings.append(f'registry {registry_res.status}')
    if findings: return {'status': 'FAIL', 'findings': findings}
    account = account_res.value
    positions = positions_res.value
    try:
        _validate_account_effect(account, positions)
    except TransactionIntegrityError as exc:
        findings.append(str(exc))
    events = {}
    for name in ('orders.jsonl', 'fills.jsonl', 'paper_trade_results.jsonl', 'equity_curve.jsonl'):
        result = strict_read_jsonl(os.path.join(ledger_root, name))
        if result.status != CriticalStateStatus.VALID:
            findings.append(f'{name} {result.status}: {result.error}')
            events[name] = []
        else:
            events[name] = result.value
    orders, fills = events['orders.jsonl'], events['fills.jsonl']
    order_by_id, fill_by_id = {}, {}
    order_tx_ids, fill_tx_ids = set(), set()
    for order in orders:
        oid = order.get('order_id')
        if not oid or oid in order_by_id: findings.append(f'duplicate/missing order_id: {oid}')
        else: order_by_id[oid] = order
        if order.get('transaction_id') in order_tx_ids: findings.append(f'duplicate order transaction_id: {order.get("transaction_id")}')
        order_tx_ids.add(order.get('transaction_id'))
        if order.get('status') not in ORDER_STATUSES: findings.append(f'invalid order status: {oid}')
    for fill in fills:
        fid = fill.get('fill_id')
        if not fid or fid in fill_by_id: findings.append(f'duplicate/missing fill_id: {fid}')
        else: fill_by_id[fid] = fill
        if fill.get('transaction_id') in fill_tx_ids: findings.append(f'duplicate fill transaction_id: {fill.get("transaction_id")}')
        fill_tx_ids.add(fill.get('transaction_id'))
        order = order_by_id.get(fill.get('order_id'))
        if order is None: findings.append(f'fill without order: {fid}')
        elif any(fill.get(k) != order.get(k) for k in ('order_id', 'transaction_id', 'decision_id', 'season_id', 'participant_id', 'account_id', 'symbol', 'direction')):
            findings.append(f'order/fill identity mismatch: {fid}')
    for oid, order in order_by_id.items():
        if order.get('status') == 'FILLED' and not any(f.get('order_id') == oid for f in fills): findings.append(f'order without fill: {oid}')
    registry = registry_res.value
    records = registry.get('records', {})
    committed = []
    committed_by_tx = {}
    for key, record in records.items():
        status = record.get('status')
        if status not in TRANSACTION_STATES: findings.append(f'invalid transaction status: {key}')
        if status != 'COMMITTED':
            findings.append(f'non-COMMITTED transaction: {key}={status}')
        if status == 'COMMITTED':
            committed.append(record)
            tx_id = record.get('transaction_id')
            if tx_id in committed_by_tx:
                findings.append(f'duplicate COMMITTED transaction_id: {tx_id}')
            committed_by_tx[tx_id] = record
            if record.get('order_id') not in order_by_id: findings.append(f'COMMITTED without order: {key}')
            if record.get('fill_id') not in fill_by_id: findings.append(f'COMMITTED without fill: {key}')
    committed_tx = {r.get('transaction_id') for r in committed}
    current_effect_hash = _json_hash(_account_effect(account, positions))
    if committed and committed[-1].get('post_account_hash') and committed[-1].get('post_account_hash') != current_effect_hash:
        findings.append('latest COMMITTED transaction effect hash does not match account/positions')
    for fill in fills:
        if fill.get('transaction_id') not in committed_tx: findings.append(f'fill not backed by COMMITTED registry: {fill.get("fill_id")}')
    results = events['paper_trade_results.jsonl']
    result_by_sell_tx = {}
    for result in results:
        sell_tx = result.get('transaction_id')
        if sell_tx in result_by_sell_tx:
            findings.append(f'duplicate trade result transaction_id: {sell_tx}')
        result_by_sell_tx[sell_tx] = result
        sell_record = committed_by_tx.get(sell_tx)
        if sell_record is None or sell_record.get('direction') != 'SELL':
            findings.append(f'trade result without committed SELL: {sell_tx}')
        open_tx = result.get('open_transaction_id')
        open_record = committed_by_tx.get(open_tx)
        if not open_tx or open_record is None or open_record.get('status') != 'COMMITTED':
            findings.append(f'closed trade result has invalid open_transaction_id: {sell_tx}')
        elif (open_record.get('direction') != 'BUY'
              or any(result.get(field) != open_record.get(field)
                     for field in ('season_id', 'participant_id', 'account_id', 'symbol'))):
            findings.append(f'closed trade open reference identity mismatch: {sell_tx}')
    for fill in fills:
        if fill.get('direction') == 'SELL' and fill.get('transaction_id') not in result_by_sell_tx:
            findings.append(f'SELL fill missing exactly one closed trade result: {fill.get("fill_id")}')
    for position in positions:
        entry_tx = position.get('entry_transaction_id')
        if not entry_tx:
            findings.append(f'open position missing entry_transaction_id: {position.get("symbol")}')
            continue
        entry_record = committed_by_tx.get(entry_tx)
        if entry_record is None or entry_record.get('direction') != 'BUY':
            findings.append(f'open position has invalid BUY reference: {entry_tx}')
        elif any(position.get(field) != entry_record.get(field)
                 for field in ('season_id', 'participant_id', 'account_id', 'symbol')
                 if field in position):
            findings.append(f'open position entry identity mismatch: {entry_tx}')
    buy_shares, sell_shares = {}, {}
    for fill in fills:
        bucket = buy_shares if fill.get('direction') == 'BUY' else sell_shares
        bucket[fill.get('symbol')] = bucket.get(fill.get('symbol'), 0) + int(fill.get('shares', 0))
    position_shares = {p.get('symbol'): int(p.get('shares', 0)) for p in positions}
    for symbol in set(buy_shares) | set(sell_shares) | set(position_shares):
        expected = buy_shares.get(symbol, 0) - sell_shares.get(symbol, 0)
        if expected != position_shares.get(symbol, 0): findings.append(f'fill/position quantity mismatch: {symbol}')
    expected_cash = float(account.get('initial_capital', 0)) + sum(float(f.get('net_cash_effect', 0)) for f in fills)
    if abs(float(account.get('cash', 0)) - expected_cash) > 0.05: findings.append('cash does not reconcile to fill net_cash_effect')
    if not fills and (abs(float(account.get('cash', 0)) - float(account.get('initial_capital', 0))) > 0.05 or positions):
        findings.append('economic effect exists without fill evidence')
    equity_by_tx = {}
    for row in events['equity_curve.jsonl']:
        tx_id = row.get('transaction_id')
        if tx_id in equity_by_tx:
            findings.append(f'duplicate equity snapshot transaction_id: {tx_id}')
        equity_by_tx[tx_id] = row
    if len(events['equity_curve.jsonl']) < len(committed): findings.append('equity curve is missing successful mutation snapshots')
    for record in committed:
        tx_id = record.get('transaction_id')
        snapshot = equity_by_tx.get(tx_id)
        if snapshot is None:
            findings.append(f'equity snapshot missing transaction: {tx_id}')
            continue
        registry_hash = record.get('post_account_hash')
        snapshot_hash = snapshot.get('post_account_hash')
        if not registry_hash or not snapshot_hash:
            findings.append(f'equity post_account_hash missing: {tx_id}')
        elif registry_hash != snapshot_hash:
            findings.append(f'equity post_account_hash mismatch: {tx_id}')
    sell_count = sum(1 for f in fills if f.get('direction') == 'SELL')
    if len(results) != sell_count: findings.append('closed SELL trade result count does not equal SELL fill count')
    return {'status': 'PASS' if not findings else 'FAIL', 'findings': findings, 'orders': orders, 'fills': fills, 'registry': registry, 'positions': positions, 'account': account, 'committed_transactions': len(committed)}
