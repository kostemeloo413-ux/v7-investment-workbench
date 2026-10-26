# -*- coding: utf-8 -*-
"""G2.6 active-season runtime and economic-ledger boundary.

This module is deliberately small: it resolves one active season namespace,
strictly validates the five strategy ledgers, and builds a derived Arena view.
It never falls back to ``data/trades`` for an active season and never resets a
corrupt or missing economic state.
"""

import datetime
import json
import os
import tempfile
import hashlib

from ai_quant.storage_utils import (
    ACCOUNT_FIELD_TYPES,
    ACCOUNT_REQUIRED_FIELDS,
    CriticalStateStatus,
    atomic_write_json,
    strict_load_json,
    strict_read_jsonl,
    validate_positions_schema,
)


BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ACTIVE_IDENTITY_SCHEMA = 'v7-active-season-identity-v1'
SEASON_EPOCHS = {
    'V7-SEASON-20260917-V1': 'AIQ-FWD-V7-20260917-E1',
    'V7-SEASON-20260930-V1': 'AIQ-FWD-V7-20260930-E1',
    'V7-SEASON-20260930-V2': 'AIQ-FWD-V7-20260930-E2',
}
STRATEGY_PARTICIPANTS = (
    'P-HANSHU',
    'P-YINWENTAO',
    'P-LIAO-XIAOAI',
    'P-COOL',
    'P-RULE-QUANT',
)
LIFECYCLE_JSONL = ('orders.jsonl', 'fills.jsonl', 'paper_trade_results.jsonl', 'equity_curve.jsonl')


class RuntimeStateError(RuntimeError):
    """A critical active-season state error; callers must fail closed."""


def __getattr__(name):
    """Compatibility exports resolve from the governed pointer, never constants."""
    if name in {'ACTIVE_SEASON_ID', 'ACTIVE_EPOCH_ID', 'EPOCH_ID'}:
        identity = load_active_season_identity()
        return (identity['active_season_id'] if name == 'ACTIVE_SEASON_ID'
                else identity['forward_epoch_id'])
    raise AttributeError(name)


def _canonical_root(base_dir, season_id):
    return os.path.abspath(os.path.join(base_dir, 'state', 'seasons', season_id))


def load_active_season_identity(base_dir=BASE):
    """Read the sole governed active-season pointer; missing means HOLD."""
    path = os.path.join(base_dir, 'state', 'active_season.json')
    result = strict_load_json(path, expected_type=dict)
    if not result.is_valid:
        raise RuntimeStateError(f'active_season pointer {result.status}: {result.error}')
    value = result.value
    season_id = value.get('active_season_id')
    expected_root = _canonical_root(base_dir, season_id) if isinstance(season_id, str) else None
    if value.get('schema_version') != ACTIVE_IDENTITY_SCHEMA or season_id not in SEASON_EPOCHS:
        raise RuntimeStateError('active_season pointer schema/identity invalid')
    if os.path.abspath(str(value.get('season_root', ''))) != expected_root:
        raise RuntimeStateError('active_season pointer root mismatch')
    if value.get('forward_epoch_id') != SEASON_EPOCHS[season_id]:
        raise RuntimeStateError('active_season pointer epoch mismatch')
    if value.get('mode') != 'PAPER/SIMULATION' or value.get('real_trading') is not False:
        raise RuntimeStateError('active_season pointer mode is not paper-only')
    for key in ('activated_at', 'evidence_ref'):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise RuntimeStateError(f'active_season pointer {key} missing')
    try:
        timestamp = datetime.datetime.fromisoformat(value['activated_at'].replace('Z', '+00:00'))
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError('timezone offset missing')
    except (TypeError, ValueError) as exc:
        raise RuntimeStateError('active_season pointer activation time invalid') from exc
    return value


def resolve_active_season_context(base_dir=BASE, season_id=None):
    """Resolve the sole active season through its strict persisted identity."""
    pointer = load_active_season_identity(base_dir)
    active_id = pointer['active_season_id']
    if season_id is not None and season_id != active_id:
        raise RuntimeStateError('explicit season differs from active pointer')
    season_id = active_id
    root = _canonical_root(base_dir, season_id)
    context_path = os.path.join(root, 'active_season_context.json')
    context_res = strict_load_json(context_path, expected_type=dict)
    if not context_res.is_valid:
        raise RuntimeStateError(f'active_season_context {context_res.status}: {context_res.error}')
    context = context_res.value
    if context.get('active_season_id') != season_id:
        raise RuntimeStateError('active_season_context season mismatch')
    if context.get('real_trading') is not False:
        raise RuntimeStateError('active_season_context real trading invalid')
    if context.get('forward_epoch_id') != pointer['forward_epoch_id']:
        raise RuntimeStateError('active_season_context epoch mismatch')
    if context.get('mode') != 'PAPER/SIMULATION':
        raise RuntimeStateError('active_season_context mode invalid')
    configured_root = os.path.abspath(str(context.get('state_root', '')))
    if configured_root != root:
        raise RuntimeStateError('active_season_context state_root mismatch')

    manifest_path = os.path.join(root, 'season_manifest.json')
    manifest_res = strict_load_json(manifest_path, expected_type=dict)
    if not manifest_res.is_valid or manifest_res.value.get('season_id') != season_id:
        raise RuntimeStateError(f'season_manifest invalid: {manifest_res.status}')
    if manifest_res.value.get('forward_epoch_id') != pointer['forward_epoch_id']:
        raise RuntimeStateError('season_manifest epoch mismatch')
    if os.path.abspath(str(manifest_res.value.get('state_root', ''))) != root:
        raise RuntimeStateError('season_manifest root mismatch')
    if manifest_res.value.get('mode') != 'PAPER/SIMULATION' or manifest_res.value.get('real_trading') is not False:
        raise RuntimeStateError('season_manifest mode invalid')

    return {
        'season_id': season_id,
        'forward_epoch_id': pointer['forward_epoch_id'],
        'active_identity': pointer,
        'season_root': root,
        'context_path': context_path,
        'manifest_path': manifest_path,
        'competition_ledger_root': os.path.join(root, 'competition_ledgers'),
        'competition_snapshot_path': os.path.join(root, 'competition_accounts.json'),
        'ai_quant_paper_root': os.path.join(root, 'ai_quant_paper'),
        'forward_root': os.path.join(root, 'forward'),
        'governance_safety_path': os.path.join(root, 'governance_halt.json'),
        'legacy_trades_root': os.path.join(base_dir, 'data', 'trades'),
        'manifest': manifest_res.value,
    }


def resolve_daily_reconciliation_path(context=None):
    """Return the one season-scoped daily reconciliation evidence path."""
    context = context or resolve_active_season_context()
    return os.path.join(context['season_root'], 'daily_reconciliation_report.json')


def _participant_map(context):
    result = {}
    for item in context['manifest'].get('participants', []):
        if not isinstance(item, dict):
            continue
        pid = item.get('participant_id')
        if item.get('return_bearing') is True and pid in STRATEGY_PARTICIPANTS:
            result[pid] = item
    if set(result) != set(STRATEGY_PARTICIPANTS):
        raise RuntimeStateError('season_manifest strategy participant mapping incomplete')
    return result


def _jsonl(path):
    result = strict_read_jsonl(path)
    if result.status != CriticalStateStatus.VALID:
        raise RuntimeStateError(f'{os.path.basename(path)} {result.status}: {result.error}')
    return result.value


def _validate_account(account, expected):
    if not isinstance(account, dict):
        raise RuntimeStateError('account root must be object')
    for key in ACCOUNT_REQUIRED_FIELDS:
        if key not in account:
            raise RuntimeStateError(f'account missing field: {key}')
    if account.get('season_id') != expected['season_id']:
        raise RuntimeStateError('account season mismatch')
    if account.get('participant_id') != expected['participant_id']:
        raise RuntimeStateError('account participant mismatch')
    if account.get('account_id') != expected['new_account_id']:
        raise RuntimeStateError('account identity mismatch')
    for key, typ in ACCOUNT_FIELD_TYPES.items():
        value = account.get(key)
        if typ is str:
            if not isinstance(value, str):
                raise RuntimeStateError(f'account field {key} must be string')
        elif isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RuntimeStateError(f'account field {key} must be numeric')
    if float(account.get('cash', -1)) < 0:
        raise RuntimeStateError('account cash is negative')


def load_strategy_ledgers(context=None):
    """Strictly load all five active-season economic ledgers."""
    context = context or resolve_active_season_context()
    mapping = _participant_map(context)
    loaded = {}
    for pid in STRATEGY_PARTICIPANTS:
        expected = mapping[pid]
        ledger_root = os.path.join(context['competition_ledger_root'], pid)
        paths = {name: os.path.join(ledger_root, name) for name in ('account.json', 'positions.json', 'processed_idempotency_registry.json')}
        account_res = strict_load_json(
            paths['account.json'], expected_type=dict,
            required_fields=ACCOUNT_REQUIRED_FIELDS,
            field_types=ACCOUNT_FIELD_TYPES,
        )
        if not account_res.is_valid:
            raise RuntimeStateError(f'{pid}/account.json {account_res.status}: {account_res.error}')
        _validate_account(account_res.value, expected)

        positions_res = strict_load_json(
            paths['positions.json'], expected_type=list, validator=validate_positions_schema
        )
        if not positions_res.is_valid:
            raise RuntimeStateError(f'{pid}/positions.json {positions_res.status}: {positions_res.error}')
        positions = positions_res.value
        seen = set()
        for pos in positions:
            if int(pos.get('shares', 0)) <= 0:
                raise RuntimeStateError(f'{pid} has non-positive position')
            if pos.get('symbol') in seen:
                raise RuntimeStateError(f'{pid} has duplicate position symbol')
            seen.add(pos.get('symbol'))

        registry_res = strict_load_json(
            paths['processed_idempotency_registry.json'], expected_type=dict,
            required_fields=['records'], field_types={'records': dict},
        )
        if not registry_res.is_valid:
            raise RuntimeStateError(f'{pid}/processed_idempotency_registry.json {registry_res.status}: {registry_res.error}')
        registry = registry_res.value
        if registry.get('season_id') != context['season_id'] or registry.get('participant_id') != pid:
            raise RuntimeStateError(f'{pid} registry identity mismatch')
        records = registry.get('records', {})
        if not isinstance(records, dict):
            raise RuntimeStateError(f'{pid} registry records type invalid')

        events = {name: _jsonl(os.path.join(ledger_root, name)) for name in LIFECYCLE_JSONL}
        loaded[pid] = {
            'participant': expected,
            'root': ledger_root,
            'account': account_res.value,
            'positions': positions,
            'registry': registry,
            **events,
        }
    return loaded


def _empty_account(mapping, context):
    return {
        'player_id': mapping['participant_id'],
        'participant_id': mapping['participant_id'],
        'account_id': mapping['new_account_id'],
        'season_id': context['season_id'],
        'name': mapping['display_name'],
        'role': 'return_bearing',
        'return_bearing': True,
        'ranking_enabled': True,
        'trading_account': True,
        'initial_capital': 100000.0,
        'cash': 100000.0,
        'market_value': 0.0,
        'total_equity': 100000.0,
        'net_pnl': 0.0,
        'net_pnl_pct': 0.0,
        'peak_equity': 100000.0,
        'max_drawdown_pct': 0.0,
        'positions': [],
        'trade_count': 0,
        'win_count': 0,
        'loss_count': 0,
    }


def build_derived_arena_snapshot(context=None, persist=False):
    """Build the Arena snapshot from ledgers; never use snapshot as SSOT."""
    context = context or resolve_active_season_context()
    loaded = load_strategy_ledgers(context)
    accounts = {}
    for pid, state in loaded.items():
        account = dict(state['account'])
        account['positions'] = state['positions']
        accounts[pid] = account

    aiq_root = context['ai_quant_paper_root']
    aiq_acc = strict_load_json(
        os.path.join(aiq_root, 'account.json'), expected_type=dict,
        required_fields=ACCOUNT_REQUIRED_FIELDS, field_types=ACCOUNT_FIELD_TYPES,
    )
    if not aiq_acc.is_valid:
        raise RuntimeStateError(f'AI Quant account {aiq_acc.status}: {aiq_acc.error}')
    if aiq_acc.value.get('season_id') != context['season_id'] or aiq_acc.value.get('account_id') != 'AIQ-PAPER-V2':
        raise RuntimeStateError('AI Quant account identity mismatch')
    aiq_pos = strict_load_json(os.path.join(aiq_root, 'positions.json'), expected_type=list, validator=validate_positions_schema)
    if not aiq_pos.is_valid:
        raise RuntimeStateError(f'AI Quant positions {aiq_pos.status}: {aiq_pos.error}')
    aiq_view = dict(aiq_acc.value)
    aiq_view['player_id'] = 'P-AI-QUANT'
    aiq_view['participant_id'] = 'P-AI-QUANT'
    aiq_view['name'] = 'AI Quant'
    aiq_view['role'] = 'return_bearing'
    aiq_view['return_bearing'] = True
    aiq_view['trading_account'] = True
    aiq_view['positions'] = aiq_pos.value
    accounts['P-AI-QUANT'] = aiq_view

    sentinel = next((p for p in context['manifest'].get('participants', [])
                     if isinstance(p, dict) and p.get('participant_id') == 'P-RISK-SENTINEL'), None)
    if sentinel is None:
        raise RuntimeStateError('Risk Sentinel mapping missing')
    accounts['P-RISK-SENTINEL'] = {
        'player_id': 'P-RISK-SENTINEL',
        'participant_id': 'P-RISK-SENTINEL',
        'account_id': sentinel.get('new_account_id'),
        'season_id': context['season_id'],
        'name': sentinel.get('display_name'),
        'role': 'risk_sentinel',
        'return_bearing': False,
        'ranking_enabled': False,
        'trading_account': False,
        'initial_capital': 0.0,
        'cash': 0.0,
        'market_value': 0.0,
        'total_equity': 0.0,
        'net_pnl': 0.0,
        'net_pnl_pct': 0.0,
        'positions': [],
        'trade_count': 0,
    }
    snapshot = {
        'season_id': context['season_id'],
        'snapshot_type': 'DERIVED_ARENA_SNAPSHOT',
        'updated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'accounts': accounts,
        'source': 'season_scoped_competition_ledgers_plus_ai_quant_paper',
    }
    if persist:
        atomic_write_json(context['competition_snapshot_path'], snapshot)
    return snapshot


def reconcile_arena_snapshot(context=None):
    """Compare derived economic fields with the existing Arena view."""
    context = context or resolve_active_season_context()
    derived = build_derived_arena_snapshot(context, persist=False)
    existing_res = strict_load_json(context['competition_snapshot_path'], expected_type=dict)
    if not existing_res.is_valid:
        return {'status': 'FAIL', 'findings': [f'competition snapshot {existing_res.status}'], 'derived': derived}
    existing = existing_res.value
    findings = []
    if existing.get('season_id') != context['season_id']:
        findings.append('competition snapshot season mismatch')
    current_accounts = existing.get('accounts')
    if not isinstance(current_accounts, dict):
        findings.append('competition snapshot accounts type invalid')
    else:
        for pid, expected in derived['accounts'].items():
            actual = current_accounts.get(pid)
            if not isinstance(actual, dict):
                findings.append(f'missing derived account {pid}')
                continue
            for key in ('account_id', 'season_id', 'cash', 'total_equity', 'positions'):
                if actual.get(key) != expected.get(key):
                    findings.append(f'snapshot mismatch {pid}.{key}')
    return {'status': 'PASS' if not findings else 'FAIL', 'findings': findings, 'derived': derived}


def economic_ledger_hashes(context):
    """Stable inputs for validated projection publication; excludes lock/temp files."""
    from pathlib import Path
    hashes = {}
    for root in (context['competition_ledger_root'], context['ai_quant_paper_root']):
        for path in sorted(Path(root).rglob('*')):
            if path.is_file() and '.tmp_' not in path.name and not path.name.endswith('.lock'):
                hashes[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def validate_projection_ledgers(context):
    """Economic truth only; a stale derived Arena view cannot veto its own rebuild."""
    strategy = reconcile_strategy_ledgers(context)
    from ai_quant.reconciliation import LedgerReconciliationEngine
    engine = LedgerReconciliationEngine(context['ai_quant_paper_root'])
    engine.cleanup_safe_temp_files = lambda: []
    startup = engine.run_startup_reconciliation()
    daily = engine.run_daily_ledger_reconciliation()
    findings = list(strategy.get('findings', [])) + startup['findings'] + daily['findings']
    registry = strict_load_json(engine.registry_path, expected_type=dict,
                                required_fields=['records'], field_types={'records': dict})
    if not registry.is_valid:
        findings.append('AIQ registry unavailable for projection')
    else:
        for record in registry.value['records'].values():
            if record.get('status') not in {'COMMITTED', 'ABORTED', 'REJECTED'}:
                findings.append('AIQ incomplete transaction prevents projection')
    account = strict_load_json(engine.account_path, expected_type=dict)
    positions = strict_load_json(engine.positions_path, expected_type=list)
    fills = strict_read_jsonl(engine.fills_path)
    if account.is_valid and positions.is_valid and fills.is_valid:
        expected_cash = account.value['initial_capital'] + sum(f.get('net_cash_effect', 0) for f in fills.value)
        if abs(account.value['cash'] - expected_cash) > 0.05:
            findings.append('AIQ cash does not reconcile to committed fill effects')
        quantities = {}
        for fill in fills.value:
            symbol = fill['symbol']
            quantities[symbol] = quantities.get(symbol, 0) + fill['shares'] * (1 if fill['direction']=='BUY' else -1)
        actual = {p['symbol']:p['shares'] for p in positions.value}
        if any(quantities.get(s, 0) != actual.get(s, 0) for s in set(quantities) | set(actual)):
            findings.append('AIQ position quantity does not reconcile to committed fills')
    return {'status':'PASS' if not findings else 'FAIL', 'findings':findings,
            'strategy':strategy['status'], 'ai_quant_startup':startup['status'],
            'ai_quant_daily':daily['status']}


def publish_reconciled_arena_snapshot(context=None):
    """Validate terminal ledgers, reuse the existing projection, atomically publish."""
    context = context or resolve_active_season_context()
    from datasource.baostock_transport import daily_g6_lock
    lock_path = os.path.join(context['season_root'], 'arena_projection.lock')
    with daily_g6_lock(lock_path):
        before = economic_ledger_hashes(context)
        validation = validate_projection_ledgers(context)
        if validation['status'] != 'PASS':
            raise RuntimeStateError('Arena projection refused: ' + '; '.join(validation['findings']))
        # The existing builder is the only source of projection semantics.
        snapshot = build_derived_arena_snapshot(context, persist=False)
        if before != economic_ledger_hashes(context):
            raise RuntimeStateError('Economic ledger changed during Arena publication')
        atomic_write_json(context['competition_snapshot_path'], snapshot)
        readback = reconcile_arena_snapshot(context)
        if readback['status'] != 'PASS' or before != economic_ledger_hashes(context):
            raise RuntimeStateError('Arena publication readback or ledger stability failed')
    return {'status':'PASS', 'source':'VERIFIED_TERMINAL_ECONOMIC_LEDGERS',
            'economic_sha256':before, 'snapshot_path':context['competition_snapshot_path']}


def reconcile_strategy_ledgers(context=None):
    try:
        loaded = load_strategy_ledgers(context)
        findings = []
        transactions = {}
        from competition.transaction_writer import reconcile_transaction_ledger
        for pid, state in loaded.items():
            tx = reconcile_transaction_ledger(state['root'])
            transactions[pid] = tx
            findings.extend(f'{pid}: {item}' for item in tx.get('findings', []))
        return {'status': 'PASS' if not findings else 'FAIL', 'findings': findings,
                'ledgers': loaded, 'transactions': transactions}
    except RuntimeStateError as exc:
        return {'status': 'FAIL', 'findings': [str(exc)], 'ledgers': {}, 'transactions': {}}


def _atomic_empty_jsonl(path):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + os.path.basename(path) + '.tmp_', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def initialize_strategy_ledgers(explicit_initialize=False, context=None):
    """Create the five clean season ledgers only through explicit init."""
    if not explicit_initialize:
        raise RuntimeStateError('strategy ledger initialization requires explicit_initialize=True')
    context = context or resolve_active_season_context()
    mapping = _participant_map(context)
    root = context['competition_ledger_root']
    for pid in STRATEGY_PARTICIPANTS:
        path = os.path.join(root, pid)
        if os.path.exists(path):
            raise RuntimeStateError(f'ledger namespace already exists: {pid}')

    for pid in STRATEGY_PARTICIPANTS:
        expected = mapping[pid]
        path = os.path.join(root, pid)
        os.makedirs(path, exist_ok=False)
        account = _empty_account(expected, context)
        account.update({
            'participant_id': pid,
            'account_id': expected['new_account_id'],
            'season_id': context['season_id'],
        })
        atomic_write_json(os.path.join(path, 'account.json'), account)
        atomic_write_json(os.path.join(path, 'positions.json'), [])
        atomic_write_json(os.path.join(path, 'processed_idempotency_registry.json'), {
            'schema_version': 'v7.2',
            'season_id': context['season_id'],
            'participant_id': pid,
            'account_id': expected['new_account_id'],
            'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'total_keys': 0,
            'records': {},
        })
        for filename in LIFECYCLE_JSONL:
            _atomic_empty_jsonl(os.path.join(path, filename))
    return load_strategy_ledgers(context)


def run_daily_reconciliation(context=None):
    """Run read/derive-only daily checks for active strategy and AIQ ledgers."""
    context = context or resolve_active_season_context()
    strategy = reconcile_strategy_ledgers(context)
    from ai_quant.reconciliation import LedgerReconciliationEngine
    aiq = LedgerReconciliationEngine(context['ai_quant_paper_root']).run_daily_ledger_reconciliation()
    arena = reconcile_arena_snapshot(context) if strategy['status'] == 'PASS' else {
        'status': 'FAIL', 'findings': ['strategy ledger reconciliation failed']
    }
    findings = list(strategy.get('findings', []))
    findings.extend(aiq.get('findings', []))
    findings.extend(arena.get('findings', []))
    return {
        'status': 'PASS' if not findings and aiq.get('status') == 'PASS' and arena.get('status') == 'PASS' else 'FAIL',
        'strategy': strategy,
        'ai_quant': aiq,
        'arena': arena,
        'findings': findings,
    }


def _update_active_safety(context, passed, findings):
    safety_res = strict_load_json(context['governance_safety_path'], expected_type=dict)
    if not safety_res.is_valid:
        return False, [f'governance safety {safety_res.status}: {safety_res.error}']
    safety = dict(safety_res.value)
    if safety.get('season_id') != context['season_id'] or safety.get('real_trading') is not False:
        return False, ['governance safety season/mode mismatch']
    reasons = list(safety.get('halt_reasons', [])) if isinstance(safety.get('halt_reasons', []), list) else []
    gate_res = strict_load_json(os.path.join(context['season_root'], 'gate_state.json'), expected_type=dict)
    gates_closed = (gate_res.is_valid
                    and gate_res.value.get('schema_version') == 'v7-gate-decision-v1'
                    and gate_res.value.get('season_id') == context['season_id']
                    and gate_res.value.get('real_trading') is False
                    and gate_res.value.get('gate_a') == 'CLOSED/PASS'
                    and gate_res.value.get('g4') == 'PASS'
                    and gate_res.value.get('g5') == 'PASS'
                    and gate_res.value.get('g6') == 'CLOSED/PASS'
                    and gate_res.value.get('gate_b') == 'CLOSED/PASS')
    paper_active = (passed and gates_closed and safety.get('mode') == 'PAPER/SIMULATION'
                    and safety.get('trading_enabled') is True
                    and safety.get('new_entry_disabled') is False
                    and isinstance(safety.get('paper_activated_at'), str))
    safety['new_entry_disabled'] = not paper_active
    safety['risk_exit_enabled'] = True
    safety['trading_enabled'] = bool(paper_active)
    safety['real_trading'] = False
    safety['execution_state_safe'] = bool(passed)
    safety['startup_status'] = 'STARTUP_RECONCILIATION_PASS' if passed else 'STARTUP_RECONCILIATION_FAIL'
    if not passed:
        reasons.append({
            'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'condition': 'G2_6_STARTUP_RECONCILIATION_FAIL',
            'reason': '; '.join(str(item) for item in findings)[:500],
        })
    safety['halt_reasons'] = reasons
    safety['total_halt_events'] = len(reasons)
    atomic_write_json(context['governance_safety_path'], safety)
    return True, []


def startup_runtime_gate(update_safety=True, context=None):
    """Validate all active economic state before any mutating runtime job."""
    findings = []
    try:
        context = context or resolve_active_season_context()
        strategy = reconcile_strategy_ledgers(context)
        findings.extend(strategy.get('findings', []))

        from ai_quant.reconciliation import LedgerReconciliationEngine
        aiq = LedgerReconciliationEngine(context['ai_quant_paper_root']).run_startup_reconciliation()
        if aiq.get('status') != 'PASS':
            findings.extend(aiq.get('findings', []))

        if strategy.get('status') == 'PASS':
            arena = reconcile_arena_snapshot(context)
            findings.extend(arena.get('findings', []))
        else:
            arena = {'status': 'FAIL', 'findings': ['strategy ledgers unavailable']}

        passed = not findings and aiq.get('status') == 'PASS' and arena.get('status') == 'PASS'
        if update_safety:
            ok, safety_findings = _update_active_safety(context, passed, findings)
            if not ok:
                findings.extend(safety_findings)
                passed = False
        return {
            'status': 'PASS' if passed else 'FAIL',
            'pass': passed,
            'season_id': context['season_id'],
            'execution_state_safe': passed,
            'findings': findings,
            'strategy': strategy,
            'ai_quant': aiq,
            'arena': arena,
        }
    except Exception as exc:
        findings.append(f'{type(exc).__name__}: {exc}')
        return {
            'status': 'FAIL', 'pass': False, 'season_id': None,
            'execution_state_safe': False, 'findings': findings,
        }
