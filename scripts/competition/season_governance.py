# -*- coding: utf-8 -*-
"""Explicit, fail-closed V7 paper-season identity and gate writeback.

This module creates only a clean season baseline. It never starts Forward,
places an order, or enables real brokerage execution.
"""

import datetime
import hashlib
import json
import os
from zoneinfo import ZoneInfo

from ai_quant.storage_utils import atomic_write_json, strict_load_json
from competition import season_runtime

NEW_SEASON = 'V7-SEASON-20260930-V1'
NEW_EPOCH = 'AIQ-FWD-V7-20260930-E1'
NEW_SEASON_V2 = 'V7-SEASON-20260930-V2'
NEW_EPOCH_V2 = 'AIQ-FWD-V7-20260930-E2'
SEASON_EPOCHS = {
    NEW_SEASON: NEW_EPOCH,
    NEW_SEASON_V2: NEW_EPOCH_V2,
}
GATE_SCHEMA = 'v7-gate-decision-v1'
LAUNCH_BINDING_SCHEMA = 'v7-season-launch-evidence-binding-v1'


def _now():
    return datetime.datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()


def staging_context(base_dir=season_runtime.BASE, season_id=NEW_SEASON, epoch_id=None):
    epoch_id = epoch_id or SEASON_EPOCHS.get(season_id)
    if season_id not in SEASON_EPOCHS or epoch_id != SEASON_EPOCHS[season_id]:
        raise season_runtime.RuntimeStateError('staging season/epoch identity invalid')
    root = os.path.abspath(os.path.join(base_dir, 'state', 'seasons', season_id))
    manifest_path = os.path.join(root, 'season_manifest.json')
    result = strict_load_json(manifest_path, expected_type=dict)
    if not result.is_valid or result.value.get('season_id') != season_id:
        raise season_runtime.RuntimeStateError('new season manifest unavailable or mismatched')
    if result.value.get('forward_epoch_id') != epoch_id:
        raise season_runtime.RuntimeStateError('new Forward epoch mismatch')
    return {
        'base_dir': os.path.abspath(base_dir),
        'season_id': season_id, 'forward_epoch_id': epoch_id,
        'season_root': root, 'state_root': root,
        'context_path': os.path.join(root, 'active_season_context.json'),
        'manifest_path': manifest_path,
        'competition_ledger_root': os.path.join(root, 'competition_ledgers'),
        'competition_snapshot_path': os.path.join(root, 'competition_accounts.json'),
        'ai_quant_paper_root': os.path.join(root, 'ai_quant_paper'),
        'forward_root': os.path.join(root, 'forward'),
        'governance_safety_path': os.path.join(root, 'governance_halt.json'),
        'manifest': result.value,
    }


def initialize_new_season(*, explicit_initialize=False, base_dir=season_runtime.BASE,
                          season_id=NEW_SEASON, epoch_id=None):
    if explicit_initialize is not True:
        raise season_runtime.RuntimeStateError('explicit_initialize=True required')
    epoch_id = epoch_id or SEASON_EPOCHS.get(season_id)
    if season_id not in SEASON_EPOCHS or epoch_id != SEASON_EPOCHS[season_id]:
        raise season_runtime.RuntimeStateError('new season/epoch identity invalid')
    root = os.path.abspath(os.path.join(base_dir, 'state', 'seasons', season_id))
    if os.path.exists(root):
        raise season_runtime.RuntimeStateError('new season already exists; refusing overwrite')
    old = os.path.join(base_dir, 'state', 'seasons', 'V7-SEASON-20260917-V1', 'season_manifest.json')
    old_result = strict_load_json(old, expected_type=dict)
    if not old_result.is_valid:
        raise season_runtime.RuntimeStateError('approved participant identity roster unavailable')
    roster = old_result.value.get('participants')
    expected = set(season_runtime.STRATEGY_PARTICIPANTS) | {'P-AI-QUANT', 'P-RISK-SENTINEL'}
    if not isinstance(roster, list) or {p.get('participant_id') for p in roster if isinstance(p, dict)} != expected:
        raise season_runtime.RuntimeStateError('approved participant identity roster mismatch')
    participants = []
    for item in roster:
        pid = item['participant_id']
        participants.append({
            'participant_id': pid, 'display_name': item['display_name'],
            'new_account_id': item['new_account_id'], 'season_id': season_id,
            'return_bearing': pid != 'P-RISK-SENTINEL',
            'trading_account': pid != 'P-RISK-SENTINEL',
            'ranking_enabled': pid != 'P-RISK-SENTINEL',
            'initial_capital': 0.0 if pid == 'P-RISK-SENTINEL' else 100000.0,
            'role': 'risk_sentinel' if pid == 'P-RISK-SENTINEL' else 'return_bearing',
        })
    os.makedirs(root, exist_ok=False)
    manifest = {
        'schema_version': 'v7-independent-season-v1', 'season_id': season_id,
        'forward_epoch_id': epoch_id, 'created_at': _now(),
        'status': 'INITIALIZED_HOLD', 'state_root': root,
        'participants': participants, 'mode': 'PAPER/SIMULATION',
        'real_trading': False, 'trading_enabled': False,
        'forward_day_count': 0, 'qualified_trading_dates': [],
        'historical_economic_state_inherited': False,
    }
    atomic_write_json(os.path.join(root, 'season_manifest.json'), manifest)
    atomic_write_json(os.path.join(root, 'active_season_context.json'), {
        'schema_version': 'v7-season-context-v1', 'active_season_id': season_id,
        'state_root': root, 'forward_epoch_id': epoch_id,
        'mode': 'PAPER/SIMULATION', 'real_trading': False,
        'trading_enabled': False, 'forward_epoch_started': False,
    })
    atomic_write_json(os.path.join(root, 'governance_halt.json'), {
        'schema_version': 'v7-paper-safety-v1', 'season_id': season_id,
        'mode': 'PAPER/SIMULATION', 'real_trading': False,
        'trading_enabled': False, 'new_entry_disabled': True,
        'risk_exit_enabled': True, 'execution_state_safe': False,
        'startup_status': 'PENDING_RECONCILIATION',
        'halt_reasons': [], 'total_halt_events': 0,
    })
    atomic_write_json(os.path.join(root, 'gate_state.json'), {
        'schema_version': GATE_SCHEMA, 'season_id': season_id,
        'mode': 'PAPER/SIMULATION', 'real_trading': False,
        'gate_a': 'CLOSED/PASS', 'g4': 'PASS', 'g5': 'PASS',
        'g6': 'OPEN/P0', 'gate_b': 'OPEN/HOLD',
        'decision_at': _now(), 'evidence_refs': ['Gate A approved historical decision'],
    })
    context = staging_context(base_dir, season_id, epoch_id)
    season_runtime.initialize_strategy_ledgers(explicit_initialize=True, context=context)
    from ai_quant.paper_executor import PaperAccount
    aiq = PaperAccount(storage_dir=context['ai_quant_paper_root'], account_id='AIQ-PAPER-V2',
                       initial_capital=100000.0, season_id=season_id)
    if aiq.account_corrupted or aiq.positions_corrupted:
        raise season_runtime.RuntimeStateError('new AIQ account initialization failed')
    for filename in season_runtime.LIFECYCLE_JSONL:
        season_runtime._atomic_empty_jsonl(os.path.join(context['ai_quant_paper_root'], filename))
    season_runtime.build_derived_arena_snapshot(context, persist=True)
    from ai_quant.forward_start_gate import initialize_forward_epoch
    initialize_forward_epoch(explicit_initialize=True, context=context)
    return context


def write_gate_state(context, *, gate_a, g4, g5, g6, gate_b, evidence_refs, explicit_writeback=False):
    if explicit_writeback is not True:
        raise season_runtime.RuntimeStateError('explicit governed gate writeback required')
    if (context['season_id'] not in SEASON_EPOCHS
            or context['forward_epoch_id'] != SEASON_EPOCHS.get(context['season_id'])):
        raise season_runtime.RuntimeStateError('gate writeback season mismatch')
    if (gate_a != 'CLOSED/PASS' or g4 not in ('PASS', 'OPEN/HOLD')
            or g5 not in ('PASS', 'OPEN/HOLD') or g6 not in ('OPEN/P0', 'CLOSED/PASS')
            or gate_b not in ('OPEN/HOLD', 'CLOSED/PASS')):
        raise season_runtime.RuntimeStateError('gate state invalid')
    if gate_b == 'CLOSED/PASS' and (g6 != 'CLOSED/PASS' or g4 != 'PASS' or g5 != 'PASS'):
        raise season_runtime.RuntimeStateError('Gate B cannot close before G4/G5/G6')
    if not isinstance(evidence_refs, list) or not evidence_refs or not all(isinstance(x, str) and x for x in evidence_refs):
        raise season_runtime.RuntimeStateError('gate evidence refs missing')
    if gate_b == 'CLOSED/PASS' and not verify_evidence_refs(
            evidence_refs, base_dir=context.get('base_dir', season_runtime.BASE)):
        raise season_runtime.RuntimeStateError('gate evidence reference/hash verification failed')
    path = os.path.join(context['season_root'], 'gate_state.json')
    prior = strict_load_json(path, expected_type=dict)
    if (not prior.is_valid or prior.value.get('season_id') != context['season_id']
            or prior.value.get('real_trading') is not False):
        raise season_runtime.RuntimeStateError('prior gate state invalid')
    result = {**prior.value, 'gate_a': gate_a, 'g4': g4, 'g5': g5,
              'g6': g6, 'gate_b': gate_b,
              'decision_at': _now(), 'evidence_refs': evidence_refs,
              'mode': 'PAPER/SIMULATION', 'real_trading': False}
    atomic_write_json(path, result)
    return result


def verify_evidence_refs(evidence_refs, base_dir=season_runtime.BASE):
    """Verify governed evidence references encoded as `relative#sha256=<hex>`."""
    import hashlib
    if not isinstance(evidence_refs, list) or not evidence_refs:
        return False
    base = os.path.abspath(base_dir)
    for ref in evidence_refs:
        if not isinstance(ref, str) or '#sha256=' not in ref:
            return False
        relative, expected = ref.rsplit('#sha256=', 1)
        if len(expected) != 64 or any(ch not in '0123456789abcdef' for ch in expected.lower()):
            return False
        path = os.path.abspath(os.path.join(base, relative.replace('/', os.sep)))
        try:
            if os.path.commonpath((base, path)) != base or not os.path.isfile(path):
                return False
            digest = hashlib.sha256()
            with open(path, 'rb') as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                    digest.update(chunk)
            if digest.hexdigest() != expected.lower():
                return False
        except (OSError, ValueError):
            return False
    return True


def _evidence_document(reference, base_dir):
    if not verify_evidence_refs([reference], base_dir=base_dir):
        raise season_runtime.RuntimeStateError(f'evidence reference invalid: {reference}')
    relative = reference.rsplit('#sha256=', 1)[0]
    path = os.path.abspath(os.path.join(base_dir, relative.replace('/', os.sep)))
    result = strict_load_json(path, expected_type=dict)
    if not result.is_valid:
        raise season_runtime.RuntimeStateError(f'evidence JSON invalid: {relative}: {result.status}')
    return result.value


def bind_reused_launch_evidence(context, evidence_refs, *, explicit_binding=False):
    """Persist a new-season evidence binding without copying or relabeling source evidence."""
    if explicit_binding is not True:
        raise season_runtime.RuntimeStateError('explicit evidence binding required')
    if (context.get('season_id') != NEW_SEASON_V2
            or context.get('forward_epoch_id') != NEW_EPOCH_V2):
        raise season_runtime.RuntimeStateError('launch evidence binding is restricted to V2/E2')
    base_dir = os.path.abspath(context.get('base_dir', season_runtime.BASE))
    required = {
        'v2_result', 'v2_snapshot', 'g6_replay', 'formal_g6', 'trusted_validator',
        'active_route_smoke', 'r4_runtime', 'r4_six_account', 'r4_economic_audit',
        'prior_gate_state', 'g4_review_package', 'g5_review_package',
    }
    if not isinstance(evidence_refs, dict) or set(evidence_refs) != required:
        raise season_runtime.RuntimeStateError('launch evidence reference set incomplete or unexpected')
    refs = [evidence_refs[key] for key in sorted(required)]
    if not all(isinstance(ref, str) and ref for ref in refs) or not verify_evidence_refs(refs, base_dir=base_dir):
        raise season_runtime.RuntimeStateError('launch evidence reference SHA verification failed')

    docs = {key: _evidence_document(ref, base_dir) for key, ref in evidence_refs.items()
            if key not in ('g4_review_package', 'g5_review_package')}
    v2 = docs['v2_result']
    snapshot = docs['v2_snapshot']
    g6 = docs['g6_replay']
    formal_g6 = docs['formal_g6']
    trusted = docs['trusted_validator']
    route = docs['active_route_smoke']
    r4_manifest = docs['r4_runtime']
    r4_runtime = docs['r4_six_account']
    r4_economics = docs['r4_economic_audit']
    prior_gate = docs['prior_gate_state']
    r4_orders = r4_economics.get('orders_created', r4_economics.get('order_created'))
    r4_fills = r4_economics.get('fills_created', r4_economics.get('fill_created'))

    if not (v2.get('trade_date') == '20260929'
            and v2.get('snapshot_readiness') == 'READY_CANDIDATE'
            and v2.get('expected_count') == v2.get('valid_daily_count') == 3046
            and v2.get('missing_count') == v2.get('failed_count') == v2.get('unresolved_count') == 0
            and v2.get('members_unique') is True and v2.get('canonical_members') is True):
        raise season_runtime.RuntimeStateError('reviewed V2 evidence is not a complete 20260929 snapshot')
    members = snapshot.get('members')
    if not (snapshot.get('trade_date') == '20260929'
            and snapshot.get('snapshot_readiness') == 'READY_CANDIDATE'
            and snapshot.get('snapshot_identity_sha256') == v2.get('snapshot_identity_sha256')
            and snapshot.get('members_hash') == v2.get('members_hash')
            and isinstance(members, list) and len(members) == 3046
            and len(set(members)) == 3046 and set(members) == set(snapshot.get('expected_codes', []))):
        raise season_runtime.RuntimeStateError('reviewed V2 snapshot content/identity mismatch')
    if not (g6.get('trade_date') == '20260929' and g6.get('status') == 'PASS'
            and g6.get('expected_count') == g6.get('valid_count') == 3046
            and g6.get('missing_count') == g6.get('failed_count') == 0
            and g6.get('source_valid') is True and g6.get('retry_complete') is True
            and g6.get('fallback_complete') is True and g6.get('completeness_evidence_valid') is True
            and g6.get('formal_g6_sha256') == '7912c7a69bf9b187874b48be2c8d631b2b4db8c3720a080ee18750d0aa31ab97'
            and g6.get('v2_snapshot_identity_sha256') == v2.get('snapshot_identity_sha256')):
        raise season_runtime.RuntimeStateError('reviewed G6 replay evidence is not a complete frozen-consumer PASS')
    if not (formal_g6.get('trade_date') == '20260929' and formal_g6.get('status') == 'PASS'
            and formal_g6.get('universe_scope') == 'DATE_EFFECTIVE_UNIVERSE_V2'
            and formal_g6.get('expected_universe_count') == formal_g6.get('successful_symbol_count') == 3046
            and formal_g6.get('failed_symbol_count') == formal_g6.get('missing_symbol_count') == 0
            and formal_g6.get('retry_complete') is True and formal_g6.get('fallback_complete') is True
            and len(formal_g6.get('valid_symbols', [])) == 3046
            and not formal_g6.get('failed_symbols') and not formal_g6.get('missing_symbols')):
        raise season_runtime.RuntimeStateError('persisted G6 source evidence is invalid')
    checks = trusted.get('checks')
    if not (trusted.get('status') == 'VERIFIED' and trusted.get('called') is True
            and trusted.get('v2_snapshot_readiness') == 'READY_CANDIDATE'
            and trusted.get('v2_unresolved_count') == 0
            and isinstance(checks, dict) and all(value is True for value in checks.values())
            and trusted.get('subject_identity', {}).get('evidence_id') == v2.get('snapshot_identity_sha256')):
        raise season_runtime.RuntimeStateError('Trusted Validator evidence is not VERIFIED for reviewed snapshot')
    if not (route.get('status') == 'PASS' and route.get('trade_date') == '20260929'
            and route.get('active_route_source') == 'DATE_EFFECTIVE_UNIVERSE_V2'
            and route.get('legacy_route_used_for_admission') is False
            and route.get('g6_expected_count') == route.get('v2_expected_count') == 3046):
        raise season_runtime.RuntimeStateError('active V2 route smoke evidence invalid')
    if not (r4_manifest.get('r4_runtime_reuse_valid') is True
            and r4_manifest.get('accounts_expected') == r4_manifest.get('accounts_entered_runtime') == 6
            and r4_manifest.get('aiq_top5_consumed') is True
            and r4_manifest.get('aiq_feature_rows_consumed') == 5
            and r4_manifest.get('cool_runtime_entered') is True
            and r4_manifest.get('economic_write') is False
            and r4_manifest.get('orders_created') == r4_manifest.get('fills_created') == 0
            and r4_runtime.get('accounts_expected') == r4_runtime.get('accounts_entered_runtime') == 6
            and r4_economics.get('economic_write') is False
            and r4_orders == r4_fills == 0
            and r4_economics.get('account_balance_delta') == 0
            and r4_economics.get('position_delta') == 0
            and r4_economics.get('forward_delta') == 'NONE'
            and r4_economics.get('real_trading') is False):
        raise season_runtime.RuntimeStateError('R4 runtime/economic-safety evidence invalid')
    if not (prior_gate.get('season_id') == NEW_SEASON
            and prior_gate.get('gate_a') == 'CLOSED/PASS' and prior_gate.get('g4') == 'PASS'
            and prior_gate.get('g5') == 'PASS' and prior_gate.get('g6') == 'CLOSED/PASS'
            and prior_gate.get('gate_b') == 'CLOSED/PASS' and prior_gate.get('real_trading') is False):
        raise season_runtime.RuntimeStateError('prior governed gate evidence does not establish prerequisite state')

    now = _now()
    binding = {
        'schema_version': LAUNCH_BINDING_SCHEMA,
        'binding_id': f'{NEW_SEASON_V2}:20260929:V2-G6-R4',
        'consumer_season_id': NEW_SEASON_V2,
        'forward_epoch_id': NEW_EPOCH_V2,
        'source_g6_season_id': formal_g6.get('season_id'),
        'trade_date': '20260929',
        'binding_type': 'GOVERNED_REFERENCE_TO_HASH_VERIFIED_ACCEPTED_EVIDENCE',
        'created_at': now,
        'v2': {
            'status': 'READY_CANDIDATE', 'snapshot_identity_sha256': v2['snapshot_identity_sha256'],
            'members_hash': v2['members_hash'], 'expected_count': 3046,
            'valid_daily_count': 3046, 'missing_count': 0, 'failed_count': 0,
            'unresolved_count': 0,
        },
        'g6': {
            'status': 'PASS', 'expected_count': 3046, 'valid_count': 3046,
            'missing_count': 0, 'failed_count': 0, 'source_valid': True,
            'retry_complete': True, 'fallback_complete': True,
            'trusted_validator_status': 'VERIFIED',
        },
        'r4_runtime': {
            'accounts_expected': 6, 'accounts_entered_runtime': 6,
            'aiq_top5_consumed': True, 'aiq_feature_rows_consumed': 5,
            'cool_runtime_entered': True, 'runtime_rerun': False,
            'economic_write': False, 'orders_created': 0, 'fills_created': 0,
        },
        'evidence_refs': {key: {'reference': ref, 'sha256': ref.rsplit('#sha256=', 1)[1]}
                          for key, ref in evidence_refs.items()},
        'source_evidence_copied': False,
        'historical_trade_replayed': False,
        'real_trading': False,
    }
    path = os.path.join(context['season_root'], 'market_data_completeness',
                        'gate_bindings', '20260929_launch_binding.json')
    if os.path.exists(path):
        raise season_runtime.RuntimeStateError('launch evidence binding already exists; refusing overwrite')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    atomic_write_json(path, binding)
    digest = hashlib.sha256(open(path, 'rb').read()).hexdigest()
    return {'path': path, 'sha256': digest, 'binding': binding}


def activate_pointer(context, *, evidence_ref, explicit_activation=False,
                     expected_current_season=None, base_dir=season_runtime.BASE):
    if explicit_activation is not True or not isinstance(evidence_ref, str) or not evidence_ref:
        raise season_runtime.RuntimeStateError('explicit activation and evidence required')
    gate = strict_load_json(os.path.join(context['season_root'], 'gate_state.json'), expected_type=dict)
    if (not gate.is_valid or gate.value.get('gate_a') != 'CLOSED/PASS'
            or gate.value.get('g4') != 'PASS' or gate.value.get('g5') != 'PASS'
            or gate.value.get('g6') != 'CLOSED/PASS'
            or gate.value.get('gate_b') != 'CLOSED/PASS'):
        raise season_runtime.RuntimeStateError('formal gates not closed')
    if (context['season_id'] not in SEASON_EPOCHS
            or context['forward_epoch_id'] != SEASON_EPOCHS.get(context['season_id'])):
        raise season_runtime.RuntimeStateError('activation context identity invalid')
    path = os.path.join(base_dir, 'state', 'active_season.json')
    if os.path.exists(path):
        try:
            current = season_runtime.load_active_season_identity(base_dir=base_dir)
        except season_runtime.RuntimeStateError as exc:
            raise season_runtime.RuntimeStateError('current active pointer is invalid') from exc
        if (expected_current_season is None
                or current.get('active_season_id') != expected_current_season
                or context['season_id'] != NEW_SEASON_V2
                or expected_current_season != NEW_SEASON):
            raise season_runtime.RuntimeStateError('active pointer replacement is not explicitly authorized for V1-to-V2')
        if not verify_evidence_refs([evidence_ref], base_dir=base_dir):
            raise season_runtime.RuntimeStateError('activation evidence reference/hash invalid')
        target_safety = strict_load_json(context['governance_safety_path'], expected_type=dict)
        if (not target_safety.is_valid or target_safety.value.get('season_id') != NEW_SEASON_V2
                or target_safety.value.get('new_entry_disabled') is not True
                or target_safety.value.get('risk_exit_enabled') is not True
                or target_safety.value.get('real_trading') is not False):
            raise season_runtime.RuntimeStateError('V2 safety must remain fail-closed before pointer activation')
    elif expected_current_season is not None:
        raise season_runtime.RuntimeStateError('expected current active pointer is absent')
    pointer = {
        'schema_version': season_runtime.ACTIVE_IDENTITY_SCHEMA,
        'active_season_id': context['season_id'], 'season_root': context['season_root'],
        'forward_epoch_id': context['forward_epoch_id'], 'mode': 'PAPER/SIMULATION',
        'real_trading': False, 'activated_at': _now(),
        'evidence_ref': evidence_ref,
    }
    atomic_write_json(path, pointer)
    season_runtime.resolve_active_season_context(base_dir=base_dir, season_id=context['season_id'])
    return pointer


def enable_paper_simulation(context, *, explicit_start=False):
    if explicit_start is not True:
        raise season_runtime.RuntimeStateError('explicit paper start required')
    base_dir = context.get('base_dir', season_runtime.BASE)
    active = season_runtime.resolve_active_season_context(base_dir=base_dir)
    if active['season_id'] != context['season_id']:
        raise season_runtime.RuntimeStateError('paper start pointer mismatch')
    gate = strict_load_json(os.path.join(context['season_root'], 'gate_state.json'), expected_type=dict)
    safety = strict_load_json(context['governance_safety_path'], expected_type=dict)
    forward_path = os.path.join(context['forward_root'], 'forward_epoch_manifest.json')
    forward = strict_load_json(forward_path, expected_type=dict)
    if (not gate.is_valid or not safety.is_valid or not forward.is_valid
            or gate.value.get('schema_version') != GATE_SCHEMA
            or gate.value.get('season_id') != context['season_id']
            or gate.value.get('gate_a') != 'CLOSED/PASS'
            or gate.value.get('g4') != 'PASS' or gate.value.get('g5') != 'PASS'
            or gate.value.get('g6') != 'CLOSED/PASS'
            or gate.value.get('gate_b') != 'CLOSED/PASS'
            or safety.value.get('season_id') != context['season_id']
            or safety.value.get('mode') != 'PAPER/SIMULATION'
            or safety.value.get('trading_enabled') is not False
            or safety.value.get('new_entry_disabled') is not True
            or safety.value.get('risk_exit_enabled') is not True
            or safety.value.get('startup_status') != 'STARTUP_RECONCILIATION_PASS'
            or safety.value.get('execution_state_safe') is not True
            or safety.value.get('real_trading') is not False
            or forward.value.get('season_id') != context['season_id']
            or forward.value.get('forward_epoch_id') != context['forward_epoch_id']
            or forward.value.get('forward_enabled') is not False
            or forward.value.get('genuine_forward_started') is not False
            or forward.value.get('forward_day_count') != 0
            or forward.value.get('qualified_trading_dates') != []
            or forward.value.get('real_trading') is not False
            or any(forward.value.get('required_gates', {}).get(key, {}).get('status') != 'PASS'
                   for key in ('G4_RUNTIME_ACCEPTANCE', 'G5_PAPER_E2E', 'G6_MARKET_COMPLETENESS'))
            or not verify_evidence_refs(gate.value.get('evidence_refs', []), base_dir=base_dir)):
        raise season_runtime.RuntimeStateError('paper start safety/gate prerequisites failed')
    activation_at = active['active_identity']['activated_at']
    manifest_result = strict_load_json(context['manifest_path'], expected_type=dict)
    context_result = strict_load_json(context['context_path'], expected_type=dict)
    if (not manifest_result.is_valid or not context_result.is_valid
            or manifest_result.value.get('season_id') != context['season_id']
            or context_result.value.get('active_season_id') != context['season_id']):
        raise season_runtime.RuntimeStateError('season activation metadata identity invalid')
    updated_manifest = {**manifest_result.value, 'status': 'PAPER_ACTIVE',
                        'trading_enabled': True, 'mode': 'PAPER/SIMULATION',
                        'real_trading': False, 'activated_at': activation_at}
    updated_context = {**context_result.value, 'trading_enabled': True,
                       'mode': 'PAPER/SIMULATION', 'real_trading': False,
                       'activated_at': activation_at}
    atomic_write_json(context['manifest_path'], updated_manifest)
    atomic_write_json(context['context_path'], updated_context)
    updated = {**safety.value, 'mode': 'PAPER/SIMULATION', 'trading_enabled': True,
               'new_entry_disabled': False, 'risk_exit_enabled': True,
               'real_trading': False, 'paper_activated_at': activation_at}
    atomic_write_json(context['governance_safety_path'], updated)
    return updated
