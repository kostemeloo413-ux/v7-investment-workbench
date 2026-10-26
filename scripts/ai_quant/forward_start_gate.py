# -*- coding: utf-8 -*-
"""Strict, season-scoped Genuine Forward truth and qualification gate.

This module is deliberately non-economic: it freezes the production model and
research-contract subject, reads persisted gate evidence, and decides whether a
Forward *day* may be qualified. It never enables Forward, starts a runner,
executes a trade, or updates model/label/risk-contract artifacts.
"""

import datetime
import hashlib
import os
import re
import sys
from zoneinfo import ZoneInfo

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _path in (_BASE, os.path.join(_BASE, 'scripts')):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from ai_quant.storage_utils import atomic_write_json, strict_load_json

SEASON_EPOCHS = {
    'V7-SEASON-20260917-V1': 'AIQ-FWD-V7-20260917-E1',
    'V7-SEASON-20260930-V1': 'AIQ-FWD-V7-20260930-E1',
    'V7-SEASON-20260930-V2': 'AIQ-FWD-V7-20260930-E2',
}
ACTIVE_PARTICIPANT_ID = 'P-AI-QUANT'
ACTIVE_ACCOUNT_ID = 'AIQ-PAPER-V2'
TIMEZONE_NAME = 'Asia/Shanghai'
FORWARD_STATUS_INITIALIZED_HOLD = 'INITIALIZED_HOLD'
FORWARD_STATUSES = {'INITIALIZED_HOLD', 'READY_FOR_START_REVIEW', 'ACTIVE', 'HALTED', 'CLOSED'}
EXPECTED_MODEL_SHA256 = 'ae0580b9fb6efdea3d87698c61b7dc91e3fefc4dd270ce21d5117feb50954270'
EXPECTED_CALIBRATOR_SHA256 = '8e6bbc3d27a155947d3b1df7bc1cd918f523dab960c081c9aa535ceba9931b9c'
RISK_CONTRACT_ID = 'AIQ-RISK-CONTRACT-V2'
RISK_FORMULA = 'predicted_mfe / abs(predicted_mae)'
RISK_THRESHOLD = 1.1
LABEL_CONTRACT_ID = 'R2_LABEL_CONTRACT_FINAL_V2'
REQUIRED_GATE_IDS = ('G4_RUNTIME_ACCEPTANCE', 'G5_PAPER_E2E', 'G6_MARKET_COMPLETENESS')
ALLOWED_GATE_STATUSES = {'PENDING', 'OPEN', 'HOLD', 'PASS', 'FAIL'}
REQUIRED_MANIFEST_FIELDS = (
    'schema_version', 'forward_epoch_id', 'season_id', 'participant_id', 'account_id',
    'epoch_type', 'initial_capital', 'baseline_timestamp', 'baseline_timezone',
    'forward_enabled', 'genuine_forward_started', 'forward_day_count',
    'qualified_trading_dates', 'forward_day_counter', 'status', 'real_trading',
    'validation_subject', 'required_gates',
)


class ForwardStateError(RuntimeError):
    pass


def __getattr__(name):
    """Legacy imports resolve through the formal active-season identity."""
    if name in {'ACTIVE_SEASON_ID', 'EPOCH_ID', 'ACTIVE_EPOCH_ID'}:
        from competition.season_runtime import load_active_season_identity
        identity = load_active_season_identity()
        return (identity['active_season_id'] if name == 'ACTIVE_SEASON_ID'
                else identity['forward_epoch_id'])
    raise AttributeError(name)


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact_hash(relative_path):
    full_path = os.path.join(_BASE, relative_path.replace('/', os.sep))
    if not os.path.isfile(full_path):
        raise ForwardStateError(f'validation artifact missing: {relative_path}')
    return _sha256(full_path)


def parse_aware_timestamp(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('timestamp must be a non-empty ISO string')
    parsed = datetime.datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('timestamp must include timezone offset')
    return parsed


def normalize_asia_shanghai_timestamp(value):
    return parse_aware_timestamp(value).astimezone(ZoneInfo(TIMEZONE_NAME))


def _load_production_manifest():
    result = strict_load_json(os.path.join(_BASE, 'models', 'ai_quant', 'production_manifest.json'), expected_type=dict)
    if not result.is_valid:
        raise ForwardStateError(f'production manifest unavailable: {result.status}')
    return result.value


def build_validation_subject():
    """Build frozen subject from approved artifacts; only explicit init/migration calls this."""
    production = _load_production_manifest()
    model, calibrator = production.get('model', {}), production.get('calibrator', {})
    model_path, calibrator_path = model.get('production_path'), calibrator.get('production_path')
    if not isinstance(model_path, str) or not isinstance(calibrator_path, str):
        raise ForwardStateError('production manifest model/calibrator paths are invalid')
    model_hash, calibrator_hash = _artifact_hash(model_path), _artifact_hash(calibrator_path)
    if model_hash != EXPECTED_MODEL_SHA256 or model.get('sha256') != EXPECTED_MODEL_SHA256:
        raise ForwardStateError('MODEL_HASH_DRIFT')
    if calibrator_hash != EXPECTED_CALIBRATOR_SHA256 or calibrator.get('sha256') != EXPECTED_CALIBRATOR_SHA256:
        raise ForwardStateError('CALIBRATOR_HASH_DRIFT')
    risk_path = 'docs/v7_review/PHASE_15_5_RISK_CONTRACT_DECISION.md'
    label_path = 'docs/v7_review/R2_LABEL_CONTRACT_FINAL_V2.md'
    return {
        'model': {'path': model_path.replace('\\', '/'), 'identity': model.get('model_version'), 'sha256': model_hash},
        'calibrator': {'path': calibrator_path.replace('\\', '/'), 'identity': calibrator.get('calibrator_name'), 'sha256': calibrator_hash},
        'risk_contract': {'contract_id': RISK_CONTRACT_ID, 'formula': RISK_FORMULA, 'threshold': RISK_THRESHOLD,
                          'evidence_path': risk_path, 'evidence_sha256': _artifact_hash(risk_path)},
        'label_contract': {'contract_id': LABEL_CONTRACT_ID, 'evidence_path': label_path,
                           'evidence_sha256': _artifact_hash(label_path)},
    }


def validation_subject_drift_codes(subject):
    """Return deterministic drift codes without mutating the frozen manifest subject."""
    if not isinstance(subject, dict):
        return ['VALIDATION_SUBJECT_MISSING']
    try:
        production, expected = _load_production_manifest(), build_validation_subject()
    except ForwardStateError as exc:
        return [str(exc)]
    codes = []
    model = subject.get('model')
    if not isinstance(model, dict) or model.get('path') != expected['model']['path'] or \
            model.get('sha256') != expected['model']['sha256'] or production.get('model', {}).get('sha256') != model.get('sha256'):
        codes.append('MODEL_HASH_DRIFT')
    calibrator = subject.get('calibrator')
    if not isinstance(calibrator, dict) or calibrator.get('path') != expected['calibrator']['path'] or \
            calibrator.get('sha256') != expected['calibrator']['sha256'] or production.get('calibrator', {}).get('sha256') != calibrator.get('sha256'):
        codes.append('CALIBRATOR_HASH_DRIFT')
    risk = subject.get('risk_contract')
    if not isinstance(risk, dict) or any(risk.get(k) != expected['risk_contract'][k] for k in expected['risk_contract']):
        codes.append('RISK_CONTRACT_DRIFT')
    label = subject.get('label_contract')
    if not isinstance(label, dict) or any(label.get(k) != expected['label_contract'][k] for k in expected['label_contract']):
        codes.append('LABEL_CONTRACT_DRIFT')
    return codes


def _validate_required_gates(gates):
    if not isinstance(gates, dict):
        return False, 'required_gates must be an object'
    for gate_id in REQUIRED_GATE_IDS:
        if not isinstance(gates.get(gate_id), dict) or gates[gate_id].get('status') not in ALLOWED_GATE_STATUSES:
            return False, f'required gate invalid: {gate_id}'
    return True, None


def _validate_counter(manifest):
    dates, counter = manifest.get('qualified_trading_dates'), manifest.get('forward_day_counter')
    if not isinstance(dates, list) or not isinstance(counter, dict):
        return False, 'qualified dates/counter invalid'
    nested_dates, nested_count = counter.get('qualified_trading_dates'), counter.get('current_forward_day_count')
    if not isinstance(nested_dates, list) or not isinstance(nested_count, int):
        return False, 'nested counter invalid'
    if any(not isinstance(item, str) or not re.fullmatch(r'\d{8}', item) for item in dates):
        return False, 'qualified date format invalid'
    if len(set(dates)) != len(dates) or dates != sorted(dates):
        return False, 'qualified dates must be unique and sorted'
    if manifest.get('forward_day_count') != len(dates) or nested_count != len(dates) or nested_dates != dates:
        return False, 'forward counters disagree'
    return True, None


def validate_forward_manifest(manifest):
    if not isinstance(manifest, dict):
        return False, 'manifest root must be object'
    missing = [key for key in REQUIRED_MANIFEST_FIELDS if key not in manifest]
    if missing:
        return False, 'missing fields: ' + ', '.join(missing)
    season_id = manifest.get('season_id')
    if season_id not in SEASON_EPOCHS:
        return False, 'season_id is not governed'
    for key, expected in (
        ('forward_epoch_id', SEASON_EPOCHS[season_id]), ('season_id', season_id),
        ('participant_id', ACTIVE_PARTICIPANT_ID), ('account_id', ACTIVE_ACCOUNT_ID),
        ('epoch_type', 'GENUINE_FORWARD'), ('initial_capital', 100000.0), ('baseline_timezone', TIMEZONE_NAME),
    ):
        if manifest.get(key) != expected:
            return False, f'{key} mismatch'
    try:
        normalize_asia_shanghai_timestamp(manifest['baseline_timestamp'])
    except (TypeError, ValueError) as exc:
        return False, f'baseline timestamp invalid: {exc}'
    if not isinstance(manifest['forward_enabled'], bool) or not isinstance(manifest['genuine_forward_started'], bool):
        return False, 'forward flags must be boolean'
    if manifest['status'] not in FORWARD_STATUSES:
        return False, 'forward status invalid'
    if manifest['status'] in {'INITIALIZED_HOLD', 'READY_FOR_START_REVIEW'} and (manifest['forward_enabled'] or manifest['genuine_forward_started']):
        return False, 'hold/review status cannot start Forward'
    if manifest['status'] == 'ACTIVE' and not (manifest['forward_enabled'] and manifest['genuine_forward_started']):
        return False, 'active status requires enabled genuine Forward'
    if manifest['status'] == 'CLOSED' and manifest['forward_enabled']:
        return False, 'closed status cannot enable Forward'
    if manifest['real_trading'] is not False:
        return False, 'real trading must remain false'
    if not isinstance(manifest['forward_day_count'], int) or manifest['forward_day_count'] < 0:
        return False, 'forward day count must be a non-negative integer'
    valid, error = _validate_counter(manifest)
    if not valid:
        return False, error
    valid, error = _validate_required_gates(manifest['required_gates'])
    if not valid:
        return False, error
    drift = validation_subject_drift_codes(manifest['validation_subject'])
    return (False, 'validation subject drift: ' + ', '.join(drift)) if drift else (True, None)


def resolve_forward_root(context=None):
    if context is None:
        from competition.season_runtime import resolve_active_season_context
        context = resolve_active_season_context()
    return os.path.abspath(context.get('forward_root') or os.path.join(context['season_root'], 'forward'))


def resolve_forward_manifest_path(context=None):
    return os.path.join(resolve_forward_root(context), 'forward_epoch_manifest.json')


def load_forward_manifest(path=None):
    return strict_load_json(path or resolve_forward_manifest_path(), expected_type=dict, validator=validate_forward_manifest)


def _resolve_context():
    from competition.season_runtime import resolve_active_season_context
    return resolve_active_season_context()


def _new_required_gates():
    return {'G4_RUNTIME_ACCEPTANCE': {'status': 'PENDING'}, 'G5_PAPER_E2E': {'status': 'PENDING'},
            'G6_MARKET_COMPLETENESS': {'status': 'OPEN'}}


def initialize_forward_epoch(explicit_initialize=False, context=None, now=None):
    """Explicitly create clean Day-0 only; it never starts Forward."""
    if explicit_initialize is not True:
        raise ForwardStateError('forward initialization requires explicit_initialize=True')
    context = context or _resolve_context()
    season_id = context.get('season_id')
    epoch_id = context.get('forward_epoch_id') or SEASON_EPOCHS.get(season_id)
    if season_id not in SEASON_EPOCHS or epoch_id != SEASON_EPOCHS[season_id]:
        raise ForwardStateError('active season mismatch; refusing Forward initialization')
    path = resolve_forward_manifest_path(context)
    if os.path.exists(path):
        raise ForwardStateError('forward epoch manifest already exists; do not overwrite')
    if now is None:
        now = datetime.datetime.now(ZoneInfo(TIMEZONE_NAME))
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('initialization timestamp must be timezone-aware')
    created = now.astimezone(ZoneInfo(TIMEZONE_NAME))
    manifest = {
        'schema_version': 'v7.3-forward-truth-integrity', 'forward_epoch_id': epoch_id,
        'season_id': season_id, 'participant_id': ACTIVE_PARTICIPANT_ID, 'account_id': ACTIVE_ACCOUNT_ID,
        'epoch_type': 'GENUINE_FORWARD', 'initial_capital': 100000.0, 'baseline_timestamp': created.isoformat(),
        'baseline_timezone': TIMEZONE_NAME, 'forward_enabled': False, 'genuine_forward_started': False,
        'forward_day_count': 0, 'qualified_trading_dates': [],
        'forward_day_counter': {'current_forward_day_count': 0, 'qualified_trading_dates': []},
        'status': FORWARD_STATUS_INITIALIZED_HOLD, 'real_trading': False,
        'validation_subject': build_validation_subject(), 'required_gates': _new_required_gates(),
        'required_gate_audit': [], 'legacy_epoch_imported': False, 'created_at': created.isoformat(),
        'foundation_note': 'Manifest only; no Forward cycle, trade, telemetry, or economic mutation started.',
    }
    valid, error = validate_forward_manifest(manifest)
    if not valid:
        raise ForwardStateError(error)
    os.makedirs(resolve_forward_root(context), exist_ok=True)
    atomic_write_json(path, manifest)
    return manifest


def migrate_forward_manifest_contract(explicit_migration=False, context=None):
    """One explicit Day-0 metadata migration; normal runtime cannot refresh frozen subject."""
    if explicit_migration is not True:
        raise ForwardStateError('forward contract migration requires explicit_migration=True')
    context = context or _resolve_context()
    path = resolve_forward_manifest_path(context)
    raw = strict_load_json(path, expected_type=dict)
    if not raw.is_valid:
        raise ForwardStateError(f'forward manifest unavailable: {raw.status}')
    manifest = dict(raw.value)
    if 'validation_subject' in manifest:
        raise ForwardStateError('VALIDATION_SUBJECT_ALREADY_FROZEN')
    if manifest.get('forward_day_count') != 0 or manifest.get('qualified_trading_dates') not in ([], None) or \
            manifest.get('status') != FORWARD_STATUS_INITIALIZED_HOLD or manifest.get('forward_enabled') or manifest.get('genuine_forward_started'):
        raise ForwardStateError('only clean INITIALIZED_HOLD Day-0 manifest may be migrated')
    manifest.update({
        'schema_version': 'v7.3-forward-truth-integrity', 'forward_day_count': 0, 'qualified_trading_dates': [],
        'forward_day_counter': {'current_forward_day_count': 0, 'qualified_trading_dates': []},
        'validation_subject': build_validation_subject(), 'required_gates': _new_required_gates(), 'real_trading': False,
        'required_gate_audit': list(manifest.get('required_gate_audit', [])) + [{
            'event': 'EXPLICIT_G3_FIX1_CONTRACT_MIGRATION',
            'at': datetime.datetime.now(ZoneInfo(TIMEZONE_NAME)).isoformat(),
        }],
    })
    # These G3-foundation flat fields were superseded by required_gates.  Do not
    # preserve a second active-looking truth source in the live manifest.
    for legacy_key in ('g4_runtime_acceptance_status', 'g5_paper_e2e_status', 'g6_market_completeness_status'):
        manifest.pop(legacy_key, None)
    valid, error = validate_forward_manifest(manifest)
    if not valid:
        raise ForwardStateError(error)
    atomic_write_json(path, manifest)
    return manifest


def update_forward_required_gate(gate_id, status, audit_note, context=None):
    """Explicit atomic metadata update. It cannot enable, start, or qualify Forward."""
    if gate_id not in REQUIRED_GATE_IDS or status not in ALLOWED_GATE_STATUSES:
        raise ForwardStateError('invalid required gate update')
    if not isinstance(audit_note, str) or not audit_note.strip():
        raise ForwardStateError('required gate update needs an audit note')
    context = context or _resolve_context()
    result = load_forward_manifest(resolve_forward_manifest_path(context))
    if not result.is_valid:
        raise ForwardStateError(f'forward manifest unavailable: {result.status}')
    manifest = dict(result.value)
    if manifest.get('season_id') != context.get('season_id') or \
            manifest.get('forward_epoch_id') != context.get('forward_epoch_id'):
        raise ForwardStateError('required gate context identity mismatch')
    if manifest.get('forward_enabled') or manifest.get('genuine_forward_started'):
        raise ForwardStateError('required gate update cannot alter a started Forward epoch')
    manifest['required_gates'] = dict(manifest['required_gates'])
    manifest['required_gates'][gate_id] = {'status': status}
    manifest['required_gate_audit'] = list(manifest.get('required_gate_audit', [])) + [{
        'gate_id': gate_id, 'status': status, 'audit_note': audit_note.strip(),
        'at': datetime.datetime.now(ZoneInfo(TIMEZONE_NAME)).isoformat(),
    }]
    valid, error = validate_forward_manifest(manifest)
    if not valid:
        raise ForwardStateError(error)
    atomic_write_json(resolve_forward_manifest_path(context), manifest)
    return manifest


def append_forward_gate_audit_erratum(
        gate_id, package, incorrect_sha256, correct_sha256,
        verified_original_zip_sha256, verified_backup_zip_sha256,
        reason='HISTORICAL_EVIDENCE_HASH_TRANSCRIPTION_ERROR',
        context=None, at=None):
    """Append one idempotent, non-gate-changing evidence erratum.

    This helper is deliberately separate from ``update_forward_required_gate``:
    it may only append an audit event for an already-PASS gate and cannot alter
    required gate status, Forward flags, counters, or economic state.
    """
    if gate_id != 'G5_PAPER_E2E':
        raise ForwardStateError('erratum is restricted to G5_PAPER_E2E')
    if not isinstance(package, str) or not package.strip():
        raise ForwardStateError('erratum package is required')
    if reason != 'HISTORICAL_EVIDENCE_HASH_TRANSCRIPTION_ERROR':
        raise ForwardStateError('unsupported erratum reason')
    hash_values = (incorrect_sha256, correct_sha256,
                   verified_original_zip_sha256, verified_backup_zip_sha256)
    if any(not isinstance(value, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', value)
           for value in hash_values):
        raise ForwardStateError('erratum SHA256 values are invalid')
    if incorrect_sha256.lower() == correct_sha256.lower():
        raise ForwardStateError('erratum must identify a changed transcription')
    if correct_sha256.lower() != verified_original_zip_sha256.lower() or \
            correct_sha256.lower() != verified_backup_zip_sha256.lower():
        raise ForwardStateError('verified ZIP hashes do not match correct SHA256')

    context = context or _resolve_context()
    result = load_forward_manifest(resolve_forward_manifest_path(context))
    if not result.is_valid:
        raise ForwardStateError(f'forward manifest unavailable: {result.status}')
    manifest = result.value
    if manifest.get('season_id') != context.get('season_id') or \
            manifest.get('forward_epoch_id') != context.get('forward_epoch_id'):
        raise ForwardStateError('active season or Forward epoch mismatch')
    if manifest.get('required_gates', {}).get(gate_id, {}).get('status') != 'PASS':
        raise ForwardStateError('G5 must already be PASS')
    if manifest.get('status') != FORWARD_STATUS_INITIALIZED_HOLD or \
            manifest.get('forward_enabled') or manifest.get('genuine_forward_started') or \
            manifest.get('forward_day_count') != 0 or manifest.get('qualified_trading_dates') != []:
        raise ForwardStateError('erratum requires disabled Forward Day 0 hold')
    if manifest.get('real_trading') is not False:
        raise ForwardStateError('real trading must remain false')

    audits = manifest.get('required_gate_audit')
    if not isinstance(audits, list):
        raise ForwardStateError('required_gate_audit must be a list')
    pass_events = [
        (index, event) for index, event in enumerate(audits)
        if isinstance(event, dict) and event.get('gate_id') == gate_id and event.get('status') == 'PASS'
    ]
    if len(pass_events) != 1:
        raise ForwardStateError('G5 PASS audit event must be unique')
    pass_index, pass_event = pass_events[0]
    target_event_ref = {
        'audit_index': pass_index,
        'gate_id': gate_id,
        'status': 'PASS',
        'at': pass_event.get('at'),
    }
    if not isinstance(pass_event.get('at'), str):
        raise ForwardStateError('original G5 PASS audit event timestamp is missing')

    identity = {
        'event': 'G5_EVIDENCE_SHA256_ERRATUM',
        'gate_id': gate_id,
        'target_event_ref': target_event_ref,
        'reason': reason,
        'package': package,
        'incorrect_sha256': incorrect_sha256.lower(),
        'correct_sha256': correct_sha256.lower(),
        'verified_original_zip_sha256': verified_original_zip_sha256.lower(),
        'verified_backup_zip_sha256': verified_backup_zip_sha256.lower(),
    }
    existing = [event for event in audits
                if isinstance(event, dict) and event.get('event') == identity['event']]
    if len(existing) > 1:
        raise ForwardStateError('duplicate G5 evidence erratum events exist')
    if existing:
        if any(existing[0].get(key) != value for key, value in identity.items()):
            raise ForwardStateError('conflicting G5 evidence erratum exists')
        return manifest

    event = dict(identity)
    event['at'] = at or datetime.datetime.now(ZoneInfo(TIMEZONE_NAME)).isoformat()
    try:
        normalize_asia_shanghai_timestamp(event['at'])
    except (TypeError, ValueError) as exc:
        raise ForwardStateError(f'erratum timestamp invalid: {exc}') from exc
    updated = dict(manifest)
    updated['required_gate_audit'] = list(audits) + [event]
    valid, error = validate_forward_manifest(updated)
    if not valid:
        raise ForwardStateError(error)
    atomic_write_json(resolve_forward_manifest_path(context), updated)
    return updated


def _gate_reason_codes(manifest):
    mapping = {'G4_RUNTIME_ACCEPTANCE': 'RUNTIME_NOT_ACCEPTED', 'G5_PAPER_E2E': 'PAPER_E2E_NOT_ACCEPTED',
               'G6_MARKET_COMPLETENESS': 'G6_MARKET_COMPLETENESS_OPEN'}
    return [code for gate_id, code in mapping.items() if manifest['required_gates'][gate_id]['status'] != 'PASS']


def evaluate_forward_start_gate(current_timestamp=None, context=None, manifest=None, replay=False, backfill=False):
    """Read persisted truth only; caller-supplied acceptance booleans are intentionally absent."""
    reasons, context = [], context or _resolve_context()
    result = manifest if hasattr(manifest, 'is_valid') else None
    if result is None:
        result = load_forward_manifest(resolve_forward_manifest_path(context)) if manifest is None else manifest
    if hasattr(result, 'is_valid') and not result.is_valid:
        return _verdict(False, reasons + [f'FORWARD_MANIFEST_{result.status}'], context, None)
    current = result.value if hasattr(result, 'value') else result
    if not isinstance(current, dict):
        return _verdict(False, reasons + ['FORWARD_MANIFEST_SCHEMA_ERROR'], context, None)
    if current.get('season_id') != context.get('season_id') or \
            current.get('forward_epoch_id') != context.get('forward_epoch_id'):
        reasons.append('SEASON_MISMATCH')
    reasons.extend(validation_subject_drift_codes(current.get('validation_subject')))
    valid, error = validate_forward_manifest(current)
    if not valid:
        return _verdict(False, reasons + ['FORWARD_MANIFEST_SCHEMA_ERROR', error], context, current)
    if current.get('forward_enabled') is not True:
        reasons.append('FORWARD_DISABLED')
    if current.get('genuine_forward_started') is not True:
        reasons.append('GENUINE_FORWARD_NOT_STARTED')
    if current.get('status') != 'ACTIVE':
        reasons.append('FORWARD_STATUS_NOT_ACTIVE')
    reasons.extend(_gate_reason_codes(current))
    if replay:
        reasons.append('REPLAY_NOT_ALLOWED')
    if backfill:
        reasons.append('BACKFILL_NOT_ALLOWED')
    if current_timestamp is not None:
        try:
            if normalize_asia_shanghai_timestamp(current_timestamp) <= normalize_asia_shanghai_timestamp(current['baseline_timestamp']):
                reasons.append('TIMESTAMP_NOT_AFTER_BASELINE')
        except (TypeError, ValueError):
            reasons.append('TIMESTAMP_NOT_AFTER_BASELINE')
    return _verdict(not reasons, reasons, context, current)


def _daily_reconciliation_status(context):
    from competition.season_runtime import resolve_daily_reconciliation_path
    path = resolve_daily_reconciliation_path(context)
    result = strict_load_json(path, expected_type=dict)
    if not result.is_valid:
        return False, {'path': path, 'status': result.status, 'reason_code': 'RECONCILIATION_FAILED'}
    report = result.value
    if report.get('season_id') != context.get('season_id'):
        return False, {'path': path, 'status': 'FAIL', 'reason_code': 'RECONCILIATION_FAILED',
                       'finding': 'RECONCILIATION_SEASON_MISMATCH'}
    if not isinstance(report.get('trade_date'), str) or not re.fullmatch(r'\d{8}', report['trade_date']):
        return False, {'path': path, 'status': 'FAIL', 'reason_code': 'RECONCILIATION_FAILED',
                       'finding': 'RECONCILIATION_DATE_MISSING'}
    components = ('strategy', 'ai_quant', 'arena')
    overall = report.get('overall')
    if report.get('status') != 'PASS' or not isinstance(overall, dict) or overall.get('status') != 'PASS' or \
            any(not isinstance(report.get(name), dict) or report[name].get('status') != 'PASS' for name in components):
        return False, {'path': path, 'status': report.get('status'), 'trade_date': report.get('trade_date'),
                       'reason_code': 'RECONCILIATION_FAILED'}
    if not isinstance(report.get('completed_at'), str):
        return False, {'path': path, 'status': 'FAIL', 'trade_date': report.get('trade_date'),
                       'reason_code': 'RECONCILIATION_FAILED', 'finding': 'RECONCILIATION_COMPLETED_AT_MISSING'}
    try:
        normalize_asia_shanghai_timestamp(report['completed_at'])
    except (TypeError, ValueError):
        return False, {'path': path, 'status': 'FAIL', 'trade_date': report.get('trade_date'),
                       'reason_code': 'RECONCILIATION_FAILED', 'finding': 'RECONCILIATION_COMPLETED_AT_INVALID'}
    return True, {'path': path, 'status': 'PASS', 'season_id': report['season_id'],
                  'trade_date': report['trade_date'], 'completed_at': report['completed_at']}


def _daily_market_completeness_status(context, trade_date):
    date_text = trade_date.strftime('%Y%m%d')
    completeness_root = os.path.join(context['season_root'], 'market_data_completeness')
    path = os.path.join(completeness_root, f'{date_text}.json')
    result = strict_load_json(path, expected_type=dict)
    if not result.is_valid:
        code = 'DAILY_MARKET_COMPLETENESS_MISSING' if result.status == 'MISSING' else 'DAILY_MARKET_COMPLETENESS_INVALID'
        return False, {'path': path, 'status': result.status, 'reason_code': code}
    from datasource.completeness import load_universe_snapshot, universe_snapshot_path, validate_completeness_evidence

    snapshot_path = universe_snapshot_path(completeness_root, date_text)
    try:
        snapshot = load_universe_snapshot(snapshot_path, date_text)
    except Exception:
        return False, {'path': path, 'universe_snapshot_path': snapshot_path,
                       'status': 'INVALID', 'reason_code': 'DAILY_MARKET_COMPLETENESS_UNIVERSE_INVALID'}
    valid, reasons = validate_completeness_evidence(
        result.value, snapshot, context.get('season_id'), date_text,
        now=datetime.datetime.now(ZoneInfo(TIMEZONE_NAME)),
    )
    if not valid:
        evidence = result.value
        if evidence.get('status') != 'PASS':
            reason_code = 'DAILY_MARKET_COMPLETENESS_FAILED'
        elif any(reason in reasons for reason in ('EVIDENCE_SEASON_MISMATCH',)):
            reason_code = 'DAILY_MARKET_COMPLETENESS_SEASON_MISMATCH'
        elif any(reason in reasons for reason in ('EVIDENCE_DATE_MISMATCH',)):
            reason_code = 'DAILY_MARKET_COMPLETENESS_DATE_MISMATCH'
        elif any(reason in reasons for reason in ('EVIDENCE_SCHEMA_MISMATCH',)):
            reason_code = 'DAILY_MARKET_COMPLETENESS_INVALID'
        else:
            reason_code = 'DAILY_MARKET_COMPLETENESS_INVALID'
        return False, {'path': path, 'universe_snapshot_path': snapshot_path,
                       'status': evidence.get('status'), 'reason_code': reason_code,
                       'reasons': reasons}
    evidence = result.value
    return True, {'path': path, 'universe_snapshot_path': snapshot_path,
                  'status': 'PASS', 'season_id': evidence['season_id'],
                  'trade_date': evidence['trade_date'], 'generated_at': evidence['generated_at']}


def evaluate_forward_day_qualification(trade_date_str, market_timestamp, context=None, manifest=None, replay=False, backfill=False):
    """Single authoritative Day-qualification function; no caller PASS booleans are trusted."""
    context = context or _resolve_context()
    try:
        trade_date = datetime.datetime.strptime(str(trade_date_str).replace('-', '')[:8], '%Y%m%d').date()
        market_time = normalize_asia_shanghai_timestamp(market_timestamp)
    except (TypeError, ValueError):
        return {'pass': False, 'accepted': False, 'reason_codes': ['INVALID_TRADE_DATE_OR_TIMESTAMP']}
    if market_time.date() != trade_date:
        return {'pass': False, 'accepted': False, 'reason_codes': ['TRADE_DATE_TIMESTAMP_MISMATCH']}
    from runtime_config import is_trading_day
    if not is_trading_day(trade_date):
        return {'pass': False, 'accepted': False, 'reason_codes': ['NON_TRADING_DATE']}
    gate = evaluate_forward_start_gate(current_timestamp=market_time.isoformat(), context=context,
                                       manifest=manifest, replay=replay, backfill=backfill)
    reasons = list(gate['reason_codes'])
    current = manifest.value if hasattr(manifest, 'value') else manifest
    if current is None:
        raw = load_forward_manifest(resolve_forward_manifest_path(context))
        current = raw.value if raw.is_valid else None
    if isinstance(current, dict) and trade_date.strftime('%Y%m%d') in current.get('qualified_trading_dates', []):
        reasons.append('DUPLICATE_FORWARD_DATE')
    recon_ok, recon_evidence = _daily_reconciliation_status(context)
    if not recon_ok:
        reasons.append(recon_evidence.get('finding') or recon_evidence.get('reason_code') or 'RECONCILIATION_FAILED')
    if isinstance(recon_evidence, dict) and recon_evidence.get('trade_date') not in (None, trade_date.strftime('%Y%m%d')):
        reasons.append('RECONCILIATION_DATE_MISMATCH')
    market_ok, market_evidence = _daily_market_completeness_status(context, trade_date)
    if not market_ok:
        reasons.append(market_evidence.get('reason_code', 'DAILY_MARKET_COMPLETENESS_INVALID'))
    return {'pass': not reasons, 'accepted': not reasons, 'reason_codes': list(dict.fromkeys(reasons)),
            'trade_date': trade_date.strftime('%Y%m%d'), 'market_timestamp': market_time.isoformat(),
            'daily_reconciliation': recon_evidence, 'daily_market_completeness': market_evidence, 'gate': gate}


def _verdict(passed, reasons, context, manifest):
    unique = list(dict.fromkeys(reasons))
    return {'status': 'PASS' if passed else 'HOLD', 'pass': bool(passed), 'verdict': 'ALLOW' if passed else 'HOLD',
            'reason_codes': unique, 'findings': unique, 'season_id': context.get('season_id'),
            'forward_epoch_id': manifest.get('forward_epoch_id') if manifest else None,
            'account_id': manifest.get('account_id') if manifest else None,
            'execution_state_safe': bool(passed), 'mutated': False}


class ForwardStartGate:
    def __init__(self, context=None):
        self.context = context

    def evaluate(self, **kwargs):
        return evaluate_forward_start_gate(context=self.context, **kwargs)
