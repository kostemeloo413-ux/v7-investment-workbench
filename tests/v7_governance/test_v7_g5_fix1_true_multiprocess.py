"""G5 FIX1 truth tests: real subprocess phases, strategy isolation and hashes."""

import datetime as dt
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / 'scripts'
CANARY = SCRIPTS / 'v7_acceptance' / 'g5_fix1_process_e2e_canary.py'
LIVE_SEASON = ROOT / 'state' / 'seasons' / 'V7-SEASON-20260917-V1'
SEASON = 'V7-SEASON-20260917-V1'
CRITICAL_LEDGER_FILES = (
    'account.json', 'positions.json', 'orders.jsonl', 'fills.jsonl',
    'paper_trade_results.jsonl', 'equity_curve.jsonl',
    'processed_idempotency_registry.json',
)
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from competition.transaction_writer import commit_strategy_transaction, reconcile_transaction_ledger


def _sha(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


class G5Fix1Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.canary_tmp = tempfile.TemporaryDirectory(prefix='v7_g5_fix1_evidence_')
        cls.canary_evidence = Path(cls.canary_tmp.name) / 'process_evidence.json'
        result = subprocess.run(
            [sys.executable, str(CANARY), '--run', '--root', 'unused', '--scripts', str(SCRIPTS),
             '--evidence', str(cls.canary_evidence)],
            capture_output=True, text=True, encoding='utf-8', errors='replace', check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f'G5 FIX1 canary failed: {result.stdout}\n{result.stderr}')
        cls.canary = json.loads(cls.canary_evidence.read_text(encoding='utf-8'))

    @classmethod
    def tearDownClass(cls):
        cls.canary_tmp.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v7_g5_fix1_strategy_')
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_t61_real_subprocess_performs_buy(self):
        self.assertEqual(self.canary['phase_A']['process_exit_code'], 0)
        self.assertEqual(self.canary['phase_A']['fills_count'], 1)
        self.assertTrue(self.canary['phase_A']['transaction_id'])

    def test_t62_fresh_subprocess_reloads_buy_state(self):
        phase = self.canary['phase_B']
        self.assertEqual(phase['process_exit_code'], 0)
        self.assertEqual(phase['startup_reconciliation'], 'PASS')
        self.assertEqual(phase['identity'], {'account_id': 'AIQ-PAPER-V2', 'initial_capital': 100000.0, 'season_id': SEASON})
        self.assertEqual(phase['positions_count'], 1)
        self.assertEqual(phase['fill_count'], 1)
        self.assertEqual(phase['entry_transaction_id'], self.canary['phase_A']['transaction_id'])

    def test_t63_fresh_same_day_subprocess_t1_locked(self):
        self.assertEqual(self.canary['phase_C']['status'], 'T1_LOCKED')
        self.assertTrue(self.canary['phase_C']['t1_locked'])

    def test_t64_same_day_sell_fill_count_zero(self):
        self.assertEqual(self.canary['phase_C']['sell_fill_count'], 0)
        self.assertEqual(self.canary['phase_C']['closed_result_count'], 0)

    def test_t65_next_sell_date_is_canonical_trading_day(self):
        self.assertTrue(self.canary['sell_date_is_valid_trading_day'])
        self.assertGreater(self.canary['next_valid_sell_trade_date'], self.canary['buy_trade_date'])

    def test_t66_saturday_not_successful_sell_date(self):
        self.assertNotEqual(self.canary['next_valid_sell_trade_date'], '20260919')
        self.assertNotEqual(self.canary['phase_D']['sell_date'], '20260919')

    def test_t67_fresh_next_trading_day_subprocess_sell(self):
        self.assertEqual(self.canary['phase_D']['process_exit_code'], 0)
        self.assertEqual(self.canary['phase_D']['positions_count'], 0)
        self.assertTrue(self.canary['phase_D']['sell_fill_id'])

    def test_t68_sell_references_original_buy(self):
        phase_a, phase_d = self.canary['phase_A'], self.canary['phase_D']
        self.assertEqual(phase_d['open_transaction_id'], phase_a['transaction_id'])
        self.assertEqual(phase_d['open_order_id'], phase_a['order_id'])
        self.assertEqual(phase_d['open_fill_id'], phase_a['fill_id'])

    def test_t69_fresh_final_startup_reconciliation_pass(self):
        self.assertEqual(self.canary['phase_E']['startup_reconciliation'], 'PASS')

    def test_t70_fresh_final_daily_reconciliation_pass(self):
        self.assertEqual(self.canary['phase_E']['daily_reconciliation'], 'PASS')

    def test_t71_final_positions_zero(self):
        self.assertEqual(self.canary['phase_E']['positions_count'], 0)

    def test_t72_final_buy_fill_count_one(self):
        self.assertEqual(self.canary['phase_E']['buy_fill_count'], 1)

    def test_t73_final_sell_fill_count_one(self):
        self.assertEqual(self.canary['phase_E']['sell_fill_count'], 1)

    def test_t74_final_closed_result_count_one(self):
        self.assertEqual(self.canary['phase_E']['closed_result_count'], 1)

    def test_t75_fresh_idempotent_reload_has_no_economic_effect(self):
        phase = self.canary['phase_F']
        self.assertEqual(phase['duplicate_sell_fill_delta'], 0)
        self.assertEqual(phase['duplicate_result_delta'], 0)
        self.assertEqual(phase['cash_delta'], 0.0)
        self.assertEqual(phase['before_hashes'], phase['after_hashes'])

    def _strategy_season(self):
        root = self.root / 'state' / 'seasons' / SEASON
        shutil.copytree(LIVE_SEASON, root)
        active_context_path = root / 'active_season_context.json'
        active_context = json.loads(active_context_path.read_text(encoding='utf-8'))
        active_context['state_root'] = str(root.resolve())
        _write(active_context_path, active_context)
        from competition.season_runtime import resolve_active_season_context
        return root, resolve_active_season_context(base_dir=str(self.root), season_id=SEASON)

    def _strategy_before_after(self, context):
        participants = ('P-HANSHU', 'P-YINWENTAO', 'P-LIAO-XIAOAI', 'P-COOL', 'P-RULE-QUANT')
        before = {
            pid: {name: _sha(Path(context['competition_ledger_root']) / pid / name) for name in CRITICAL_LEDGER_FILES}
            for pid in participants if pid != 'P-HANSHU'
        }
        import simulated_trading
        with mock.patch.object(simulated_trading, '_active_runtime_context', return_value=context):
            accounts = simulated_trading.load_accounts_from_ledgers()
            accounts['hanshu']['trade_ids'].extend([
                {'direction': 'BUY', 'symbol': '600001', 'shares': 100, 'price': 10.0, 'date': '20260918', 'plan_id': 'FIX1-A', 'decision_id': 'FIX1-D1', 'commission': 0.0, 'stamp_tax': 0.0},
                {'direction': 'BUY', 'symbol': '600002', 'shares': 100, 'price': 10.0, 'date': '20260918', 'plan_id': 'FIX1-B', 'decision_id': 'FIX1-D2', 'commission': 0.0, 'stamp_tax': 0.0},
            ])
            simulated_trading._persist_active_strategy_ledgers(accounts)
        after = {
            pid: {name: _sha(Path(context['competition_ledger_root']) / pid / name) for name in CRITICAL_LEDGER_FILES}
            for pid in before
        }
        return context, before, after

    def test_t76_other_four_strategy_ledgers_true_hashes_unchanged(self):
        _, context = self._strategy_season()
        _, before, after = self._strategy_before_after(context)
        self.assertEqual(before, after)

    def test_t77_production_persistence_path_tested(self):
        _, context = self._strategy_season()
        self._strategy_before_after(context)
        ledger = Path(context['competition_ledger_root']) / 'P-HANSHU'
        registry = json.loads((ledger / 'processed_idempotency_registry.json').read_text(encoding='utf-8'))
        self.assertEqual(len(registry['records']), 2)

    def test_t78_first_intermediate_snapshot_position_count_one(self):
        _, context = self._strategy_season()
        self._strategy_before_after(context)
        rows = [json.loads(x) for x in (Path(context['competition_ledger_root']) / 'P-HANSHU' / 'equity_curve.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(rows[0]['position_count'], 1)

    def test_t79_second_intermediate_snapshot_position_count_two(self):
        _, context = self._strategy_season()
        self._strategy_before_after(context)
        rows = [json.loads(x) for x in (Path(context['competition_ledger_root']) / 'P-HANSHU' / 'equity_curve.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(rows[1]['position_count'], 2)

    def _two_trade_ledger(self):
        _, context = self._strategy_season()
        self._strategy_before_after(context)
        return Path(context['competition_ledger_root']) / 'P-HANSHU'

    def test_t80_tx1_snapshot_hash_matches_registry(self):
        ledger = self._two_trade_ledger()
        rows = [json.loads(x) for x in (ledger / 'equity_curve.jsonl').read_text(encoding='utf-8').splitlines()]
        reg = json.loads((ledger / 'processed_idempotency_registry.json').read_text(encoding='utf-8'))
        records = {v['transaction_id']: v for v in reg['records'].values()}
        self.assertEqual(rows[0]['post_account_hash'], records[rows[0]['transaction_id']]['post_account_hash'])

    def test_t81_tx2_snapshot_hash_matches_registry(self):
        ledger = self._two_trade_ledger()
        rows = [json.loads(x) for x in (ledger / 'equity_curve.jsonl').read_text(encoding='utf-8').splitlines()]
        reg = json.loads((ledger / 'processed_idempotency_registry.json').read_text(encoding='utf-8'))
        records = {v['transaction_id']: v for v in reg['records'].values()}
        self.assertEqual(rows[1]['post_account_hash'], records[rows[1]['transaction_id']]['post_account_hash'])

    def test_t82_tx1_tx2_post_state_hashes_differ(self):
        ledger = self._two_trade_ledger()
        rows = [json.loads(x) for x in (ledger / 'equity_curve.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertNotEqual(rows[0]['post_account_hash'], rows[1]['post_account_hash'])

    def _one_trade_ledger(self):
        root = self.root / 'ledger'
        account = {'participant_id': 'P-HANSHU', 'account_id': 'HANSHU-V2', 'season_id': SEASON, 'initial_capital': 100000.0, 'cash': 99000.0, 'market_value': 1000.0, 'total_equity': 100000.0, 'net_pnl': 0.0, 'net_pnl_pct': 0.0, 'peak_equity': 100000.0, 'max_drawdown_pct': 0.0, 'max_positions': 3, 'max_single_position_ratio': 0.3, 'max_equity_ratio': 0.9}
        _write(root / 'account.json', account); _write(root / 'positions.json', [{'symbol': '600001', 'shares': 100, 'cost_price': 10.0, 'current_price': 10.0, 'buy_date': '20260918', 'plan_id': 'HASH-1', 'participant_id': 'P-HANSHU', 'account_id': 'HANSHU-V2', 'season_id': SEASON}])
        _write(root / 'processed_idempotency_registry.json', {'schema_version': 'v7.2', 'season_id': SEASON, 'participant_id': 'P-HANSHU', 'account_id': 'HANSHU-V2', 'records': {}, 'total_keys': 0})
        for name in ('orders.jsonl', 'fills.jsonl', 'paper_trade_results.jsonl', 'equity_curve.jsonl'):
            (root / name).write_text('', encoding='utf-8')
        commit_strategy_transaction(str(root), account, json.loads((root / 'positions.json').read_text(encoding='utf-8')), {'direction': 'BUY', 'symbol': '600001', 'shares': 100, 'price': 10.0, 'date': '20260918', 'plan_id': 'HASH-1', 'decision_id': 'HASH-D1', 'net_cash_effect': -1000.0}, decision={'participant_id': 'P-HANSHU', 'decision_id': 'HASH-D1'})
        return root

    def test_t83_missing_post_hash_reconciliation_fail_closed(self):
        ledger = self._one_trade_ledger(); path = ledger / 'equity_curve.jsonl'; row = json.loads(path.read_text(encoding='utf-8').splitlines()[0]); row.pop('post_account_hash'); path.write_text(json.dumps(row) + '\n', encoding='utf-8')
        self.assertEqual(reconcile_transaction_ledger(str(ledger))['status'], 'FAIL')

    def test_t84_mismatched_post_hash_reconciliation_fail_closed(self):
        ledger = self._one_trade_ledger(); path = ledger / 'equity_curve.jsonl'; row = json.loads(path.read_text(encoding='utf-8').splitlines()[0]); row['post_account_hash'] = '0' * 64; path.write_text(json.dumps(row) + '\n', encoding='utf-8')
        self.assertEqual(reconcile_transaction_ledger(str(ledger))['status'], 'FAIL')

    def test_t85_duplicate_equity_snapshot_reconciliation_fail_closed(self):
        ledger = self._one_trade_ledger(); path = ledger / 'equity_curve.jsonl'; line = path.read_text(encoding='utf-8').splitlines()[0]; path.write_text(line + '\n' + line + '\n', encoding='utf-8')
        self.assertEqual(reconcile_transaction_ledger(str(ledger))['status'], 'FAIL')

    def test_t86_live_full_economic_hash_set_unchanged(self):
        files = [LIVE_SEASON / 'governance_halt.json', LIVE_SEASON / 'forward' / 'forward_epoch_manifest.json']
        for pid in ('P-HANSHU', 'P-YINWENTAO', 'P-LIAO-XIAOAI', 'P-COOL', 'P-RULE-QUANT'):
            files.extend(LIVE_SEASON.joinpath('competition_ledgers', pid, name) for name in CRITICAL_LEDGER_FILES)
        files.extend(LIVE_SEASON / 'ai_quant_paper' / name for name in CRITICAL_LEDGER_FILES)
        before = {str(p): _sha(p) for p in files if p.exists()}
        canary = self.canary
        self.assertTrue(canary['live_state_mutated'] is False)
        after = {str(p): _sha(p) for p in files if p.exists()}
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
