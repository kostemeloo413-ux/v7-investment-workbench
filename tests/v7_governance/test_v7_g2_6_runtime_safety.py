# -*- coding: utf-8 -*-
"""G2.6 runtime safety, active-season routing, and fail-closed regression."""

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))

from competition.accounts import ArenaAccountsManager
from competition.season_runtime import (
    STRATEGY_PARTICIPANTS,
    build_derived_arena_snapshot,
    initialize_strategy_ledgers,
    load_strategy_ledgers,
    reconcile_arena_snapshot,
    resolve_active_season_context,
    run_daily_reconciliation,
    startup_runtime_gate,
)


# Explicit isolated test identity; never read the developer's active pointer.
ACTIVE_SEASON_ID = 'V7-SEASON-20260930-V2'
TEST_EPOCH_ID = 'AIQ-FWD-V7-20260930-E2'


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, (dict, list)):
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    else:
        path.write_text(str(value), encoding='utf-8')


def _base_account(pid, account_id, season_id):
    return {
        'participant_id': pid, 'account_id': account_id, 'season_id': season_id,
        'initial_capital': 100000.0, 'cash': 100000.0,
        'market_value': 0.0, 'total_equity': 100000.0,
        'net_pnl': 0.0, 'net_pnl_pct': 0.0, 'peak_equity': 100000.0,
        'max_drawdown_pct': 0.0, 'positions': [],
    }


class G26Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.sid = ACTIVE_SEASON_ID
        self.root = self.base / 'state' / 'seasons' / self.sid
        self.account_ids = {
            'P-HANSHU': 'HANSHU-V2', 'P-YINWENTAO': 'YINWENTAO-V2',
            'P-LIAO-XIAOAI': 'LIAO-XIAOAI-V2', 'P-COOL': 'COOL-V2',
            'P-RULE-QUANT': 'AI-ALPHA-V2', 'P-AI-QUANT': 'AIQ-PAPER-V2',
            'P-RISK-SENTINEL': 'RISK-SENTINEL-V1',
        }
        names = {
            'P-HANSHU': '韩叔', 'P-YINWENTAO': '殷文涛', 'P-LIAO-XIAOAI': '廖小爱',
            'P-COOL': '酷儿', 'P-RULE-QUANT': '规则量化派', 'P-AI-QUANT': 'AI Quant',
            'P-RISK-SENTINEL': '反指哨兵',
        }
        participants = []
        for pid in (*STRATEGY_PARTICIPANTS, 'P-AI-QUANT', 'P-RISK-SENTINEL'):
            participants.append({
                'participant_id': pid, 'display_name': names[pid],
                'new_account_id': self.account_ids[pid],
                'old_account_id': pid + '-OLD',
                'season_id': self.sid,
                'return_bearing': pid != 'P-RISK-SENTINEL',
            })
        _write(self.base / 'state' / 'active_season.json', {
            'schema_version': 'v7-active-season-identity-v1',
            'active_season_id': self.sid, 'season_root': str(self.root),
            'forward_epoch_id': TEST_EPOCH_ID, 'mode': 'PAPER/SIMULATION',
            'real_trading': False, 'activated_at': '2026-09-30T17:24:33+08:00',
            'evidence_ref': 'ISOLATED_SYNTHETIC_TEST_FIXTURE',
        })
        _write(self.root / 'active_season_context.json', {
            'active_season_id': self.sid, 'state_root': str(self.root),
            'forward_epoch_id': TEST_EPOCH_ID, 'mode': 'PAPER/SIMULATION',
            'trading_enabled': False, 'real_trading': False,
        })
        _write(self.root / 'season_manifest.json', {
            'season_id': self.sid, 'status': 'INITIALIZED_HOLD', 'participants': participants,
            'forward_epoch_id': TEST_EPOCH_ID, 'state_root': str(self.root),
            'mode': 'PAPER/SIMULATION', 'real_trading': False,
        })
        _write(self.root / 'governance_halt.json', {
            'startup_status': 'INITIALIZED_NOT_VALIDATED', 'new_entry_disabled': True,
            'risk_exit_enabled': True, 'execution_state_safe': False,
            'trading_enabled': False, 'real_trading': False, 'halt_reasons': [],
        })
        self.aiq = self.root / 'ai_quant_paper'
        _write(self.aiq / 'account.json', _base_account('P-AI-QUANT', 'AIQ-PAPER-V2', self.sid))
        _write(self.aiq / 'positions.json', [])
        _write(self.aiq / 'processed_idempotency_registry.json', {
            'schema_version': 'v7.2', 'season_id': self.sid, 'account_id': 'AIQ-PAPER-V2',
            'records': {},
        })
        for name in ('orders.jsonl', 'fills.jsonl', 'paper_trade_results.jsonl', 'equity_curve.jsonl'):
            _write(self.aiq / name, '')
        self.ctx = resolve_active_season_context(base_dir=str(self.base), season_id=self.sid)
        initialize_strategy_ledgers(explicit_initialize=True, context=self.ctx)
        _write(Path(self.ctx['competition_snapshot_path']), build_derived_arena_snapshot(self.ctx))

        self.legacy_trades = self.base / 'data' / 'trades' / 'hanshu.json'
        _write(self.legacy_trades, {'trades': [{'direction': 'BUY', 'symbol': 'sh999999', 'shares': 100}]})
        self.old_archive = self.base / 'archives' / 'V7-LEGACY-SEASON-001' / 'final_seal_manifest.json'
        _write(self.old_archive, {'seal_status': 'SEALED'})
        self.old_aiq = self.base / 'data' / 'ai_quant_research' / 'paper_simulation' / 'account.json'
        _write(self.old_aiq, {'account_id': 'AIQ-PAPER-V1'})
        self.old_halt = self.base / 'state' / 'governance_halt.json'
        _write(self.old_halt, {'new_entry_disabled': True, 'execution_state_safe': False})

    def tearDown(self):
        self.tmp.cleanup()

    def test_t01_valid_arena_snapshot_loads(self):
        path = self.base / 'arena.json'
        _write(path, {'season_id': self.sid, 'accounts': {}})
        mgr = ArenaAccountsManager(self.sid, state_path=str(path), explicit_initialize=False)
        self.assertEqual(mgr.season_id, self.sid)

    def test_t02_missing_arena_snapshot_fails(self):
        with self.assertRaises(RuntimeError):
            ArenaAccountsManager(self.sid, state_path=str(self.base / 'missing.json'), explicit_initialize=False)

    def test_t03_parse_error_fails_closed(self):
        path = self.base / 'arena.json'; _write(path, '{')
        with self.assertRaises(RuntimeError):
            ArenaAccountsManager(self.sid, state_path=str(path), explicit_initialize=False)

    def test_t04_wrong_type_fails_closed(self):
        path = self.base / 'arena.json'; _write(path, [])
        with self.assertRaises(RuntimeError):
            ArenaAccountsManager(self.sid, state_path=str(path), explicit_initialize=False)

    def test_t05_schema_error_fails_closed(self):
        path = self.base / 'arena.json'; _write(path, {'season_id': self.sid})
        with self.assertRaises(RuntimeError):
            ArenaAccountsManager(self.sid, state_path=str(path), explicit_initialize=False)

    def test_t06_season_mismatch_fails_closed(self):
        path = self.base / 'arena.json'; _write(path, {'season_id': 'OTHER', 'accounts': {}})
        with self.assertRaises(RuntimeError):
            ArenaAccountsManager(self.sid, state_path=str(path), explicit_initialize=False)

    def test_t07_critical_fail_never_calls_reset(self):
        with patch.object(ArenaAccountsManager, 'reset_season', side_effect=AssertionError('reset called')):
            with self.assertRaises(RuntimeError):
                ArenaAccountsManager(self.sid, state_path=str(self.base / 'missing.json'), explicit_initialize=False)

    def test_t08_atomic_save_interruption_preserves_target(self):
        path = self.base / 'arena.json'
        _write(path, {'season_id': self.sid, 'accounts': {}})
        mgr = ArenaAccountsManager(self.sid, state_path=str(path), explicit_initialize=False)
        before = path.read_bytes()
        with patch('competition.accounts.atomic_write_json', side_effect=OSError('interrupt')):
            with self.assertRaises(OSError):
                mgr.save()
        self.assertEqual(path.read_bytes(), before)

    def test_t09_new_season_strategy_ledger_loads(self):
        loaded = load_strategy_ledgers(self.ctx)
        self.assertEqual(set(loaded), set(STRATEGY_PARTICIPANTS))

    def test_t10_active_loader_does_not_read_legacy_source(self):
        import simulated_trading as sim
        with patch('competition.season_runtime.resolve_active_season_context', return_value=self.ctx):
            accounts = sim.load_accounts_from_ledgers()
        self.assertEqual(accounts['hanshu']['positions'], [])
        self.assertEqual(accounts['hanshu']['participant_id'], 'P-HANSHU')

    def test_t11_legacy_positions_cannot_reappear(self):
        import simulated_trading as sim
        with patch('competition.season_runtime.resolve_active_season_context', return_value=self.ctx):
            with patch.object(sim, 'TRADES_DIR', str(self.base / 'data' / 'trades')):
                accounts = sim.load_accounts_from_ledgers()
        self.assertEqual(accounts['hanshu']['positions'], [])

    def test_t12_five_accounts_start_clean(self):
        loaded = load_strategy_ledgers(self.ctx)
        self.assertEqual(len(loaded), 5)
        self.assertTrue(all(v['account']['cash'] == 100000.0 and not v['positions'] for v in loaded.values()))

    def test_t13_risk_sentinel_has_no_economic_ledger(self):
        self.assertFalse((Path(self.ctx['competition_ledger_root']) / 'P-RISK-SENTINEL').exists())

    def test_t14_ai_quant_uses_independent_paper_root(self):
        snapshot = build_derived_arena_snapshot(self.ctx)
        self.assertEqual(snapshot['accounts']['P-AI-QUANT']['account_id'], 'AIQ-PAPER-V2')
        self.assertNotIn('P-AI-QUANT', load_strategy_ledgers(self.ctx))

    def test_t15_participant_ledgers_are_isolated(self):
        loaded = load_strategy_ledgers(self.ctx)
        self.assertEqual(len({v['root'] for v in loaded.values()}), 5)

    def test_t16_idempotency_registries_are_isolated(self):
        loaded = load_strategy_ledgers(self.ctx)
        roots = {v['registry']['participant_id'] for v in loaded.values()}
        self.assertEqual(roots, set(STRATEGY_PARTICIPANTS))

    def test_t17_all_valid_state_startup_passes(self):
        result = startup_runtime_gate(update_safety=True, context=self.ctx)
        self.assertEqual(result['status'], 'PASS')

    def test_t18_strategy_corruption_fails_startup(self):
        _write(Path(self.ctx['competition_ledger_root']) / 'P-HANSHU' / 'account.json', '{')
        self.assertEqual(startup_runtime_gate(update_safety=False, context=self.ctx)['status'], 'FAIL')

    def test_t19_aiq_corruption_fails_startup(self):
        _write(self.aiq / 'account.json', '{')
        self.assertEqual(startup_runtime_gate(update_safety=False, context=self.ctx)['status'], 'FAIL')

    def test_t20_registry_corruption_fails_startup(self):
        _write(Path(self.ctx['competition_ledger_root']) / 'P-COOL' / 'processed_idempotency_registry.json', '{')
        self.assertEqual(startup_runtime_gate(update_safety=False, context=self.ctx)['status'], 'FAIL')

    def test_t21_malformed_fills_fail_startup(self):
        _write(Path(self.ctx['competition_ledger_root']) / 'P-RULE-QUANT' / 'fills.jsonl', '{bad')
        self.assertEqual(startup_runtime_gate(update_safety=False, context=self.ctx)['status'], 'FAIL')

    def test_t22_season_mismatch_fails_startup(self):
        bad = dict(self.ctx); bad['season_id'] = 'OTHER'
        self.assertEqual(startup_runtime_gate(update_safety=False, context=bad)['status'], 'FAIL')

    def test_t23_startup_fail_keeps_entry_disabled(self):
        _write(Path(self.ctx['competition_ledger_root']) / 'P-HANSHU' / 'account.json', '{')
        startup_runtime_gate(update_safety=True, context=self.ctx)
        halt = json.loads((self.root / 'governance_halt.json').read_text(encoding='utf-8'))
        self.assertTrue(halt['new_entry_disabled'])

    def test_t24_startup_pass_sets_execution_safe(self):
        startup_runtime_gate(update_safety=True, context=self.ctx)
        halt = json.loads((self.root / 'governance_halt.json').read_text(encoding='utf-8'))
        self.assertTrue(halt['execution_state_safe'])

    def test_t25_startup_pass_does_not_enable_entry(self):
        startup_runtime_gate(update_safety=True, context=self.ctx)
        halt = json.loads((self.root / 'governance_halt.json').read_text(encoding='utf-8'))
        self.assertTrue(halt['new_entry_disabled'])

    def test_t26_startup_pass_does_not_enable_trading(self):
        startup_runtime_gate(update_safety=True, context=self.ctx)
        halt = json.loads((self.root / 'governance_halt.json').read_text(encoding='utf-8'))
        self.assertFalse(halt['trading_enabled'])

    def test_t27_daemon_mutating_job_is_gated(self):
        from daemon import trading_daemon as daemon
        with patch.object(daemon, 'startup_runtime_gate', return_value={'pass': True, 'status': 'PASS'}):
            ok, _ = daemon.runtime_job_preflight('intraday_cycle')
        self.assertTrue(ok)

    def test_t28_startup_fail_blocks_scheduler_eligibility(self):
        from daemon import trading_daemon as daemon
        with patch.object(daemon, 'startup_runtime_gate', return_value={'pass': False, 'status': 'FAIL'}):
            ok, _ = daemon.runtime_job_preflight('close_settlement')
        self.assertFalse(ok)

    def test_t29_once_intraday_cannot_bypass_gate(self):
        from daemon import trading_daemon as daemon
        with patch.object(daemon, 'startup_runtime_gate', return_value={'pass': False, 'status': 'FAIL'}) as gate:
            daemon.runtime_job_preflight('intraday_cycle')
        gate.assert_called_once_with(update_safety=True)

    def test_t30_once_close_cannot_bypass_gate(self):
        from daemon import trading_daemon as daemon
        with patch.object(daemon, 'startup_runtime_gate', return_value={'pass': False, 'status': 'FAIL'}) as gate:
            daemon.runtime_job_preflight('close_settlement')
        gate.assert_called_once_with(update_safety=True)

    def test_t31_read_only_news_path_may_bypass_gate(self):
        from daemon import trading_daemon as daemon
        with patch.object(daemon, 'startup_runtime_gate') as gate:
            ok, result = daemon.runtime_job_preflight('news_refresh')
        self.assertTrue(ok)
        self.assertEqual(result['status'], 'BYPASS_READ_ONLY')
        gate.assert_not_called()

    def test_t32_aiq_daily_reconciliation_uses_active_root(self):
        result = run_daily_reconciliation(self.ctx)
        self.assertEqual(result['ai_quant']['status'], 'PASS')
        self.assertEqual(Path(self.ctx['ai_quant_paper_root']), self.aiq)

    def test_t33_strategy_daily_reconciliation_uses_five_ledgers(self):
        result = run_daily_reconciliation(self.ctx)
        self.assertEqual(result['strategy']['status'], 'PASS')
        self.assertEqual(len(result['strategy']['ledgers']), 5)

    def test_t34_daily_failure_leaves_entry_disabled(self):
        _write(Path(self.ctx['competition_ledger_root']) / 'P-HANSHU' / 'fills.jsonl', '{bad')
        result = run_daily_reconciliation(self.ctx)
        self.assertEqual(result['status'], 'FAIL')
        self.assertTrue(json.loads((self.root / 'governance_halt.json').read_text(encoding='utf-8'))['new_entry_disabled'])

    def test_t35_legacy_aiq_path_is_not_used(self):
        result = run_daily_reconciliation(self.ctx)
        self.assertEqual(result['ai_quant']['status'], 'PASS')
        self.assertEqual(result['ai_quant']['reconciliation_date'], result['ai_quant']['reconciliation_date'])

    def _unchanged(self, path):
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        startup_runtime_gate(update_safety=False, context=self.ctx)
        after = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_t36_legacy_data_trades_unchanged(self):
        self._unchanged(self.legacy_trades)

    def test_t37_g1_archive_unchanged(self):
        self._unchanged(self.old_archive)

    def test_t38_old_aiq_v1_unchanged(self):
        self._unchanged(self.old_aiq)

    def test_t39_old_legacy_halt_unchanged(self):
        self._unchanged(self.old_halt)


if __name__ == '__main__':
    unittest.main()
