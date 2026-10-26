# -*- coding: utf-8 -*-
"""G3 Forward truth/qualification tests. All state mutation uses temp namespaces."""

import datetime
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
for item in (ROOT, ROOT / 'scripts'):
    if str(item) not in sys.path:
        sys.path.insert(0, str(item))

from ai_quant.forward_start_gate import (
    ACTIVE_ACCOUNT_ID, ACTIVE_SEASON_ID, EPOCH_ID, build_validation_subject,
    evaluate_forward_day_qualification, evaluate_forward_start_gate,
    initialize_forward_epoch, load_forward_manifest, migrate_forward_manifest_contract,
    update_forward_required_gate, validate_forward_manifest,
)
from ai_quant.forward_epoch_manager import ForwardEpochManager
from ai_quant.storage_utils import atomic_write_json
from competition.season_runtime import resolve_daily_reconciliation_path
from datasource.completeness import build_completeness_evidence, build_universe_snapshot, persist_universe_snapshot


class G3ForwardTruthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'state' / 'seasons' / ACTIVE_SEASON_ID
        self.root.mkdir(parents=True)
        self.aiq_root = self.root / 'ai_quant_paper'
        self.aiq_root.mkdir()
        self.context = {
            'season_id': ACTIVE_SEASON_ID, 'season_root': str(self.root),
            'manifest_path': str(self.root / 'season_manifest.json'),
            'forward_root': str(self.root / 'forward'), 'ai_quant_paper_root': str(self.aiq_root),
            'competition_snapshot_path': str(self.root / 'competition_accounts.json'),
        }
        atomic_write_json(self.context['manifest_path'], {'season_id': ACTIVE_SEASON_ID})
        (self.root / 'forward').mkdir()
        self.path = self.root / 'forward' / 'forward_epoch_manifest.json'
        self.manifest = self._manifest()
        atomic_write_json(self.path, self.manifest)
        self.universe_source = self.root / 'stock_universe.json'
        self.universe_source.write_text('{"updated_at":"2026-09-18 09:00:00","total_count":1,"stocks":[{"code":"000001"}]}', encoding='utf-8')

    def tearDown(self):
        self.tmp.cleanup()

    def _manifest(self, **overrides):
        value = {
            'schema_version': 'v7.3-forward-truth-integrity', 'forward_epoch_id': EPOCH_ID,
            'season_id': ACTIVE_SEASON_ID, 'participant_id': 'P-AI-QUANT', 'account_id': ACTIVE_ACCOUNT_ID,
            'epoch_type': 'GENUINE_FORWARD', 'initial_capital': 100000.0,
            'baseline_timestamp': '2026-09-17T12:00:00+08:00', 'baseline_timezone': 'Asia/Shanghai',
            'forward_enabled': False, 'genuine_forward_started': False, 'forward_day_count': 0,
            'qualified_trading_dates': [], 'forward_day_counter': {'current_forward_day_count': 0, 'qualified_trading_dates': []},
            'status': 'INITIALIZED_HOLD', 'real_trading': False, 'validation_subject': build_validation_subject(),
            'required_gates': {'G4_RUNTIME_ACCEPTANCE': {'status': 'PENDING'},
                               'G5_PAPER_E2E': {'status': 'PENDING'},
                               'G6_MARKET_COMPLETENESS': {'status': 'OPEN'}}, 'required_gate_audit': [],
        }
        value.update(overrides)
        return value

    def _write_reconciliation_pass(self, trade_date='20260918', **overrides):
        report = {
            'status': 'PASS', 'overall': {'status': 'PASS'}, 'season_id': ACTIVE_SEASON_ID,
            'trade_date': trade_date, 'completed_at': f'{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}T16:00:00+08:00',
            'strategy': {'status': 'PASS', 'findings': []},
            'ai_quant': {'status': 'PASS', 'findings': []}, 'arena': {'status': 'PASS', 'findings': []},
            'findings': [],
        }
        report.update(overrides)
        atomic_write_json(Path(resolve_daily_reconciliation_path(self.context)), report)

    def _write_market_evidence(self, trade_date='20260918', path_trade_date=None, **overrides):
        completeness_root = self.root / 'market_data_completeness'
        snapshot_date = path_trade_date or trade_date
        snapshot = build_universe_snapshot(
            snapshot_date, ['000001'], 'MAINBOARD_TRADABLE_CANDIDATE_UNIVERSE',
            'g3-test-v1', str(self.universe_source),
        )
        snapshot['season_id'] = ACTIVE_SEASON_ID
        persist_universe_snapshot(str(completeness_root / 'universe_snapshots' / f'{snapshot_date}.json'), snapshot)
        now_iso = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat()
        evidence = build_completeness_evidence(
            snapshot, str(completeness_root), ['000001'], [], [], True, True,
            {'test-provider': 1}, [], now_iso,
            source_by_symbol={'000001': 'test-provider'},
        )
        evidence['trade_date'] = trade_date
        evidence.update(overrides)
        path = completeness_root / f'{path_trade_date or trade_date}.json'
        atomic_write_json(path, evidence)

    def _active_manifest(self, g6_status='PASS'):
        value = self._manifest(forward_enabled=True, genuine_forward_started=True, status='ACTIVE',
            required_gates={'G4_RUNTIME_ACCEPTANCE': {'status': 'PASS'},
                            'G5_PAPER_E2E': {'status': 'PASS'}, 'G6_MARKET_COMPLETENESS': {'status': g6_status}})
        atomic_write_json(self.path, value)
        return value

    # T1-T31 preserve the foundation invariants.
    def test_t01_manifest_is_valid_day0(self):
        self.assertTrue(load_forward_manifest(str(self.path)).is_valid)

    def test_t02_epoch_identity(self):
        self.assertEqual(self.manifest['forward_epoch_id'], EPOCH_ID)

    def test_t03_aiq_v2_identity(self):
        self.assertEqual(self.manifest['account_id'], 'AIQ-PAPER-V2')

    def test_t04_hold_is_not_started(self):
        self.assertFalse(self.manifest['forward_enabled'])

    def test_t05_start_gate_holds_day0(self):
        self.assertFalse(evaluate_forward_start_gate(context=self.context, manifest=self.manifest)['pass'])

    def test_t06_real_trading_invalid(self):
        bad = self._manifest(real_trading=True)
        self.assertFalse(validate_forward_manifest(bad)[0])

    def test_t07_missing_manifest_fail_closed(self):
        self.assertEqual(load_forward_manifest(str(self.root / 'missing.json')).status, 'MISSING')

    def test_t08_replay_blocks(self):
        self.assertIn('REPLAY_NOT_ALLOWED', evaluate_forward_start_gate(context=self.context, manifest=self.manifest, replay=True)['reason_codes'])

    def test_t09_backfill_blocks(self):
        self.assertIn('BACKFILL_NOT_ALLOWED', evaluate_forward_start_gate(context=self.context, manifest=self.manifest, backfill=True)['reason_codes'])

    def test_t10_timestamp_before_baseline_blocks(self):
        result = evaluate_forward_start_gate(context=self.context, manifest=self.manifest, current_timestamp='2026-09-17T11:59:59+08:00')
        self.assertIn('TIMESTAMP_NOT_AFTER_BASELINE', result['reason_codes'])

    def test_t11_initialize_requires_explicit_flag(self):
        with self.assertRaises(Exception):
            initialize_forward_epoch(False, self.context)

    def test_t12_legacy_epoch_not_inherited(self):
        self.assertNotEqual(EPOCH_ID, 'AIQ-FWD-20260911-V1')

    def test_t13_manifest_season_mismatch_invalid(self):
        self.assertFalse(validate_forward_manifest(self._manifest(season_id='OTHER'))[0])

    def test_t14_manifest_account_mismatch_invalid(self):
        self.assertFalse(validate_forward_manifest(self._manifest(account_id='AIQ-PAPER-V1'))[0])

    def test_t15_counter_count_mismatch_invalid(self):
        self.assertFalse(validate_forward_manifest(self._manifest(forward_day_count=1))[0])

    def test_t16_counter_nested_mismatch_invalid(self):
        self.assertFalse(validate_forward_manifest(self._manifest(forward_day_counter={'current_forward_day_count': 1, 'qualified_trading_dates': []}))[0])

    def test_t17_duplicate_counter_dates_invalid(self):
        self.assertFalse(validate_forward_manifest(self._manifest(forward_day_count=2, qualified_trading_dates=['20260918', '20260918'], forward_day_counter={'current_forward_day_count': 2, 'qualified_trading_dates': ['20260918', '20260918']}))[0])

    def test_t18_status_active_requires_started_flags(self):
        self.assertFalse(validate_forward_manifest(self._manifest(status='ACTIVE'))[0])

    def test_t19_hold_cannot_enable_forward(self):
        self.assertFalse(validate_forward_manifest(self._manifest(forward_enabled=True))[0])

    def test_t20_required_gates_are_persisted(self):
        self.assertIn('G6_MARKET_COMPLETENESS', self.manifest['required_gates'])

    def test_t21_update_gate_requires_audit_note(self):
        with self.assertRaises(Exception):
            update_forward_required_gate('G4_RUNTIME_ACCEPTANCE', 'PASS', '', self.context)

    def test_t22_manager_date_alone_cannot_increment(self):
        manager = ForwardEpochManager(str(self.path), self.context)
        self.assertFalse(manager.record_forward_day_end('20260918', {}, None)['accepted'])

    def test_t23_manager_uses_authoritative_qualification(self):
        manager = ForwardEpochManager(str(self.path), self.context)
        result = manager.record_forward_day_end('20260918', {}, '2026-09-18T15:00:00+08:00')
        self.assertFalse(result['accepted'])
        self.assertEqual(result['forward_day_count'], 0)

    def test_t24_no_legacy_trade_root_in_epoch_manager(self):
        source = (ROOT / 'scripts' / 'ai_quant' / 'forward_epoch_manager.py').read_text(encoding='utf-8')
        self.assertNotIn('data/trades', source)

    def test_t25_no_caller_acceptance_booleans(self):
        self.assertNotIn('runtime_accepted', evaluate_forward_start_gate.__code__.co_varnames)

    def test_t26_no_caller_data_complete_boolean(self):
        self.assertNotIn('data_complete', evaluate_forward_start_gate.__code__.co_varnames)

    def test_t27_no_caller_paper_e2e_boolean(self):
        self.assertNotIn('paper_e2e_accepted', evaluate_forward_start_gate.__code__.co_varnames)

    def test_t28_initialization_has_subject(self):
        self.assertIn('model', self.manifest['validation_subject'])

    def test_t29_initialization_has_no_qualified_dates(self):
        self.assertEqual(self.manifest['qualified_trading_dates'], [])

    def test_t30_runtime_hold_has_no_mutation(self):
        before = self.path.read_bytes()
        evaluate_forward_start_gate(context=self.context, manifest=self.manifest)
        self.assertEqual(before, self.path.read_bytes())

    def test_t31_active_context_identity_is_required(self):
        bad_context = dict(self.context, season_id='OTHER')
        self.assertIn('SEASON_MISMATCH', evaluate_forward_start_gate(context=bad_context, manifest=self.manifest)['reason_codes'])

    # T32-T60: FIX1 subject, qualification, API truth, and no-runtime behaviors.
    def test_t32_model_hash_drift_blocks(self):
        bad = self._manifest()
        bad['validation_subject']['model']['sha256'] = '0' * 64
        self.assertFalse(validate_forward_manifest(bad)[0])

    def test_t33_calibrator_hash_drift_blocks(self):
        bad = self._manifest()
        bad['validation_subject']['calibrator']['sha256'] = '0' * 64
        self.assertFalse(validate_forward_manifest(bad)[0])

    def test_t34_risk_contract_drift_blocks(self):
        bad = self._manifest()
        bad['validation_subject']['risk_contract']['threshold'] = 9.9
        self.assertFalse(validate_forward_manifest(bad)[0])

    def test_t35_label_contract_drift_blocks(self):
        bad = self._manifest()
        bad['validation_subject']['label_contract']['contract_id'] = 'OTHER'
        self.assertFalse(validate_forward_manifest(bad)[0])

    def test_t36_subject_is_frozen_by_normal_gate(self):
        before = self.path.read_bytes()
        evaluate_forward_start_gate(context=self.context, manifest=self.manifest)
        self.assertEqual(before, self.path.read_bytes())

    def test_t37_explicit_migration_is_day0_only(self):
        legacy = dict(self.manifest)
        legacy.pop('validation_subject')
        legacy.pop('required_gates')
        atomic_write_json(self.path, legacy)
        migrated = migrate_forward_manifest_contract(True, self.context)
        self.assertIn('validation_subject', migrated)

    def test_t61_second_migration_is_blocked(self):
        legacy = dict(self.manifest)
        legacy.pop('validation_subject')
        legacy.pop('required_gates')
        atomic_write_json(self.path, legacy)
        migrate_forward_manifest_contract(True, self.context)
        with self.assertRaisesRegex(Exception, 'VALIDATION_SUBJECT_ALREADY_FROZEN'):
            migrate_forward_manifest_contract(True, self.context)

    def test_t62_frozen_subject_cannot_refresh(self):
        before = self.path.read_bytes()
        with self.assertRaisesRegex(Exception, 'VALIDATION_SUBJECT_ALREADY_FROZEN'):
            migrate_forward_manifest_contract(True, self.context)
        self.assertEqual(before, self.path.read_bytes())

    def test_t38_explicit_migration_rejects_non_day0(self):
        bad = self._manifest(forward_day_count=1, qualified_trading_dates=['20260918'], forward_day_counter={'current_forward_day_count': 1, 'qualified_trading_dates': ['20260918']})
        atomic_write_json(self.path, bad)
        with self.assertRaises(Exception):
            migrate_forward_manifest_contract(True, self.context)

    def test_t39_g4_persisted_status_blocks(self):
        result = evaluate_forward_start_gate(context=self.context, manifest=self.manifest)
        self.assertIn('RUNTIME_NOT_ACCEPTED', result['reason_codes'])

    def test_t40_g5_persisted_status_blocks(self):
        result = evaluate_forward_start_gate(context=self.context, manifest=self.manifest)
        self.assertIn('PAPER_E2E_NOT_ACCEPTED', result['reason_codes'])

    def test_t41_g6_persisted_status_blocks(self):
        result = evaluate_forward_start_gate(context=self.context, manifest=self.manifest)
        self.assertIn('G6_MARKET_COMPLETENESS_OPEN', result['reason_codes'])

    def test_t42_weekend_never_qualifies(self):
        self._active_manifest(); self._write_reconciliation_pass()
        result = evaluate_forward_day_qualification('20260919', '2026-09-19T15:00:00+08:00', self.context)
        self.assertIn('NON_TRADING_DATE', result['reason_codes'])

    def test_t43_holiday_never_qualifies(self):
        self._active_manifest(); self._write_reconciliation_pass()
        result = evaluate_forward_day_qualification('20261001', '2026-10-01T15:00:00+08:00', self.context)
        self.assertIn('NON_TRADING_DATE', result['reason_codes'])

    def test_t44_naive_timestamp_never_qualifies(self):
        result = evaluate_forward_day_qualification('20260918', '2026-09-18T15:00:00', self.context)
        self.assertIn('INVALID_TRADE_DATE_OR_TIMESTAMP', result['reason_codes'])

    def test_t45_trade_date_timestamp_mismatch_blocks(self):
        result = evaluate_forward_day_qualification('20260918', '2026-09-19T15:00:00+08:00', self.context)
        self.assertIn('TRADE_DATE_TIMESTAMP_MISMATCH', result['reason_codes'])

    def test_t46_missing_daily_reconciliation_blocks(self):
        self._active_manifest()
        result = evaluate_forward_day_qualification('20260918', '2026-09-18T15:00:00+08:00', self.context)
        self.assertIn('RECONCILIATION_FAILED', result['reason_codes'])

    def test_t47_valid_trading_day_can_qualify(self):
        active = self._active_manifest(); self._write_reconciliation_pass(); self._write_market_evidence()
        result = evaluate_forward_day_qualification('20260918', '2026-09-18T15:00:00+08:00', self.context, active)
        self.assertTrue(result['pass'])

    def test_t48_duplicate_date_cannot_qualify_twice(self):
        self._active_manifest(); self._write_reconciliation_pass(); self._write_market_evidence()
        manager = ForwardEpochManager(str(self.path), self.context)
        self.assertTrue(manager.record_forward_day_end('20260918', {}, '2026-09-18T15:00:00+08:00')['accepted'])
        manager = ForwardEpochManager(str(self.path), self.context)
        self.assertEqual(manager.record_forward_day_end('20260918', {}, '2026-09-18T15:00:00+08:00')['reason_code'], 'DUPLICATE_FORWARD_DATE')

    def test_t49_replay_never_qualifies(self):
        active = self._active_manifest(); self._write_reconciliation_pass(); self._write_market_evidence()
        self.assertIn('REPLAY_NOT_ALLOWED', evaluate_forward_day_qualification('20260918', '2026-09-18T15:00:00+08:00', self.context, active, replay=True)['reason_codes'])

    def test_t50_backfill_never_qualifies(self):
        active = self._active_manifest(); self._write_reconciliation_pass(); self._write_market_evidence()
        self.assertIn('BACKFILL_NOT_ALLOWED', evaluate_forward_day_qualification('20260918', '2026-09-18T15:00:00+08:00', self.context, active, backfill=True)['reason_codes'])

    def test_t51_recording_syncs_both_counters(self):
        self._active_manifest(); self._write_reconciliation_pass(); self._write_market_evidence()
        result = ForwardEpochManager(str(self.path), self.context).record_forward_day_end('20260918', {}, '2026-09-18T15:00:00+08:00')
        self.assertEqual(result['forward_day_count'], 1)
        saved = load_forward_manifest(str(self.path)).value
        self.assertEqual(saved['forward_day_counter']['qualified_trading_dates'], saved['qualified_trading_dates'])

    def test_t52_invalid_open_counter_reference_fails(self):
        bad = self._active_manifest()
        bad.update(forward_day_count=1, qualified_trading_dates=['20260918'], forward_day_counter={'current_forward_day_count': 0, 'qualified_trading_dates': []})
        self.assertFalse(validate_forward_manifest(bad)[0])

    def test_t53_api_overview_uses_active_context(self):
        import daemon.v7_api as api
        active = self._active_manifest()
        atomic_write_json(self.aiq_root / 'account.json', {'account_id': ACTIVE_ACCOUNT_ID, 'initial_capital': 100000.0, 'cash': 100000.0, 'market_value': 0.0, 'total_equity': 100000.0})
        atomic_write_json(self.aiq_root / 'positions.json', [])
        with patch('competition.season_runtime.resolve_active_season_context', return_value=self.context):
            data = api.get_v7_overview()['data']
        self.assertEqual(data['forward_epoch']['epoch_id'], active['forward_epoch_id'])
        self.assertEqual(data['account_matrix']['account_id'], ACTIVE_ACCOUNT_ID)

    def test_t54_api_paper_uses_active_aiq_v2(self):
        import daemon.v7_api as api
        atomic_write_json(self.aiq_root / 'account.json', {'account_id': ACTIVE_ACCOUNT_ID, 'initial_capital': 100000.0, 'cash': 100000.0, 'market_value': 0.0, 'total_equity': 100000.0})
        atomic_write_json(self.aiq_root / 'positions.json', [])
        with patch('competition.season_runtime.resolve_active_season_context', return_value=self.context):
            data = api.get_v7_paper()['data']
        self.assertEqual(data['account_id'], ACTIVE_ACCOUNT_ID)

    def test_t55_api_forward_day0_is_not_genuine(self):
        import daemon.v7_api as api
        with patch('competition.season_runtime.resolve_active_season_context', return_value=self.context):
            data = api.get_v7_forward()['data']
        self.assertFalse(data['is_genuine_forward'])
        self.assertEqual(data['observation_status'], 'INITIALIZED_HOLD')

    def test_t56_api_arena_uses_active_snapshot(self):
        import daemon.v7_api as api
        atomic_write_json(self.root / 'competition_accounts.json', {'season_id': ACTIVE_SEASON_ID, 'accounts': {
            'P-HANSHU': {'participant_id': 'P-HANSHU', 'name': '韩叔', 'return_bearing': True, 'ranking_enabled': True,
                         'role': 'return_bearing', 'initial_capital': 100000.0, 'total_equity': 100000.0, 'net_pnl': 0.0, 'net_pnl_pct': 0.0, 'max_drawdown_pct': 0.0, 'positions': []},
            'P-RISK-SENTINEL': {'participant_id': 'P-RISK-SENTINEL', 'return_bearing': False, 'ranking_enabled': False},
        }})
        with patch('competition.season_runtime.resolve_active_season_context', return_value=self.context):
            data = api.get_v7_arena()['data']
        self.assertEqual(data['season_id'], ACTIVE_SEASON_ID)
        self.assertEqual(data['players_count'], 1)

    def test_t57_api_has_no_mutating_routes(self):
        import daemon.v7_api as api
        self.assertTrue(all('GET' in route.methods for route in api.v7_router.routes))

    def test_t58_legacy_api_constants_not_used_by_forward_endpoint(self):
        source = (ROOT / 'scripts' / 'daemon' / 'v7_api.py').read_text(encoding='utf-8')
        region = source[source.index('def get_v7_forward'):source.index('def get_v7_trade_plans')]
        self.assertNotIn('FORWARD_EPOCH_DIR', region)
        self.assertNotIn('PAPER_DIR', region)

    def test_t59_forward_day0_does_not_start_runner(self):
        import daemon.trading_daemon as daemon
        with patch('ai_quant.forward_start_gate.evaluate_forward_start_gate', return_value={'pass': False, 'reason_codes': ['FORWARD_DISABLED']}), \
             patch('ai_quant.forward_runner.execute_forward_paper_cycle') as runner:
            daemon.forward_runtime_branch(context=self.context)
        runner.assert_not_called()

    def test_t60_live_runtime_is_not_started_by_tests(self):
        self.assertFalse(self.manifest['forward_enabled'])
        self.assertFalse(self.manifest['genuine_forward_started'])

    def test_t63_writer_and_reader_share_canonical_season_path(self):
        self.assertEqual(resolve_daily_reconciliation_path(self.context), str(self.root / 'daily_reconciliation_report.json'))
        source = (ROOT / 'scripts' / 'daemon' / 'trading_daemon.py').read_text(encoding='utf-8')
        self.assertIn('resolve_daily_reconciliation_path', source)

    def test_t64_reconciliation_requires_exact_season_and_date(self):
        self._active_manifest(); self._write_reconciliation_pass('20260917'); self._write_market_evidence()
        result = evaluate_forward_day_qualification('20260918', '2026-09-18T15:00:00+08:00', self.context)
        self.assertIn('RECONCILIATION_DATE_MISMATCH', result['reason_codes'])

    def test_t65_market_evidence_missing_blocks(self):
        self._active_manifest(); self._write_reconciliation_pass()
        result = evaluate_forward_day_qualification('20260918', '2026-09-18T15:00:00+08:00', self.context)
        self.assertIn('DAILY_MARKET_COMPLETENESS_MISSING', result['reason_codes'])

    def test_t66_market_evidence_malformed_blocks(self):
        self._active_manifest(); self._write_reconciliation_pass()
        path = self.root / 'market_data_completeness' / '20260918.json'; path.parent.mkdir(); path.write_text('{', encoding='utf-8')
        result = evaluate_forward_day_qualification('20260918', '2026-09-18T15:00:00+08:00', self.context)
        self.assertIn('DAILY_MARKET_COMPLETENESS_INVALID', result['reason_codes'])

    def test_t67_market_evidence_fail_blocks(self):
        self._active_manifest(); self._write_reconciliation_pass(); self._write_market_evidence(status='FAIL', failure_reasons=['PROVIDER_FAILURE'])
        result = evaluate_forward_day_qualification('20260918', '2026-09-18T15:00:00+08:00', self.context)
        self.assertIn('DAILY_MARKET_COMPLETENESS_FAILED', result['reason_codes'])

    def test_t68_market_evidence_unknown_blocks(self):
        self._active_manifest(); self._write_reconciliation_pass(); self._write_market_evidence(status='UNKNOWN')
        result = evaluate_forward_day_qualification('20260918', '2026-09-18T15:00:00+08:00', self.context)
        self.assertIn('DAILY_MARKET_COMPLETENESS_FAILED', result['reason_codes'])

    def test_t69_market_evidence_wrong_season_blocks(self):
        self._active_manifest(); self._write_reconciliation_pass(); self._write_market_evidence(season_id='OTHER')
        result = evaluate_forward_day_qualification('20260918', '2026-09-18T15:00:00+08:00', self.context)
        self.assertIn('DAILY_MARKET_COMPLETENESS_SEASON_MISMATCH', result['reason_codes'])

    def test_t70_market_evidence_wrong_date_blocks(self):
        self._active_manifest(); self._write_reconciliation_pass(); self._write_market_evidence(trade_date='20260917', path_trade_date='20260918')
        result = evaluate_forward_day_qualification('20260918', '2026-09-18T15:00:00+08:00', self.context)
        self.assertIn('DAILY_MARKET_COMPLETENESS_DATE_MISMATCH', result['reason_codes'])

    def test_t71_g6_open_cannot_be_overridden_by_daily_pass(self):
        active = self._active_manifest(g6_status='OPEN'); self._write_reconciliation_pass(); self._write_market_evidence()
        result = evaluate_forward_day_qualification('20260918', '2026-09-18T15:00:00+08:00', self.context, active)
        self.assertIn('G6_MARKET_COMPLETENESS_OPEN', result['reason_codes'])
        self.assertFalse(result['pass'])

    def test_t72_g6_pass_cannot_override_missing_daily_evidence(self):
        active = self._active_manifest(g6_status='PASS'); self._write_reconciliation_pass()
        result = evaluate_forward_day_qualification('20260918', '2026-09-18T15:00:00+08:00', self.context, active)
        self.assertIn('DAILY_MARKET_COMPLETENESS_MISSING', result['reason_codes'])

    def test_t73_live_manifest_is_day0_and_has_no_third_migration(self):
        live = ROOT / 'state' / 'seasons' / ACTIVE_SEASON_ID / 'forward' / 'forward_epoch_manifest.json'
        self.assertTrue(live.exists())
        value = load_forward_manifest(str(live)).value
        self.assertEqual(value['status'], 'INITIALIZED_HOLD')
        self.assertEqual(value['forward_day_count'], 0)
        self.assertIn('validation_subject', value)
        self.assertIn('required_gates', value)
        audits = [item for item in value.get('required_gate_audit', []) if item.get('event') == 'EXPLICIT_G3_FIX1_CONTRACT_MIGRATION']
        self.assertEqual(len(audits), 2)


if __name__ == '__main__':
    unittest.main()
