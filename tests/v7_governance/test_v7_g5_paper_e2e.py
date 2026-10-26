"""G5 isolated Paper E2E and active-season execution-integrity tests.

Every economic mutation in this module is confined to TemporaryDirectory.
The live season is inspected only for immutability/safety assertions.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ai_quant.paper_executor import PaperExecutionEngine
from ai_quant.reconciliation import LedgerReconciliationEngine
from competition.transaction_writer import commit_strategy_transaction, reconcile_transaction_ledger

SEASON = 'V7-SEASON-20260917-V1'
LIVE_SEASON = ROOT / 'state' / 'seasons' / SEASON


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def _sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


class G5Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='v7_g5_e2e_')
        self.root = Path(self.tmp.name)
        self.halt = self.root / 'governance_halt.json'
        _write(self.halt, {
            'schema_version': 'v7.2', 'startup_status': 'TEST_PASS',
            'new_entry_disabled': False, 'risk_exit_enabled': True,
            'execution_state_safe': True, 'trading_enabled': True,
            'real_trading': False, 'total_halt_events': 0, 'halt_reasons': [],
        })
        self.live_hashes = self._live_hashes()
        self.engine = None

    def tearDown(self):
        self.assertEqual(self.live_hashes, self._live_hashes())
        self.tmp.cleanup()

    def _live_hashes(self):
        paths = [
            LIVE_SEASON / 'governance_halt.json',
            LIVE_SEASON / 'forward' / 'forward_epoch_manifest.json',
            LIVE_SEASON / 'ai_quant_paper' / 'account.json',
            LIVE_SEASON / 'ai_quant_paper' / 'positions.json',
        ]
        return {str(p): _sha(p) for p in paths if p.exists()}

    def aiq(self, path=None, account_id='AIQ-PAPER-V2', season_id=SEASON, capital=100000.0):
        path = Path(path or self.root)
        halt = path / 'governance_halt.json'
        if not halt.exists():
            _write(halt, {
                'schema_version': 'v7.2', 'startup_status': 'TEST_PASS',
                'new_entry_disabled': False, 'risk_exit_enabled': True,
                'execution_state_safe': True, 'trading_enabled': True,
                'real_trading': False, 'total_halt_events': 0, 'halt_reasons': [],
            })
        self.engine = PaperExecutionEngine(
            storage_dir=str(path), account_id=account_id,
            initial_capital=capital, season_id=season_id,
            governance_safety_path=str(halt),
        )
        return self.engine

    def buy(self, engine=None, plan='PLAN-G5-BUY', symbol='600000', date='20260918'):
        e = engine or self.engine or self.aiq()
        return e.execute_buy(
            {'symbol': symbol, 'name': 'G5-TEST', 'decision_id': f'DEC-{plan}', 'position_size_pct': 0.2},
            {'plan_id': plan, 'expected_holding_days': 1, 'stop_loss': 9.0, 'take_profit': 10.5},
            10.0, 9.9, date,
        )

    def sell_next_day(self, engine=None, date='20260919'):
        e = engine or self.engine
        e.account.positions[0]['pending_exit'] = True
        e.account.save()
        return e.monitor_and_execute_exits({'600000': 11.0}, date)[0]

    def next_valid_trading_date(self, buy_date='20260918'):
        from runtime_config import is_trading_day
        date = dt.datetime.strptime(buy_date, '%Y%m%d').date() + dt.timedelta(days=1)
        while not is_trading_day(date):
            date += dt.timedelta(days=1)
        self.assertNotEqual(date.strftime('%Y%m%d'), '20260919')
        return date.strftime('%Y%m%d')

    # AI Quant identity/governance contract (T1-T10)
    def test_t01_fresh_active_identity(self):
        e = self.aiq()
        self.assertEqual((e.account.account_id, e.account.season_id, e.account.initial_capital), ('AIQ-PAPER-V2', SEASON, 100000.0))

    def test_t02_active_governance_path(self):
        e = self.aiq()
        self.assertEqual(Path(e.policy.halt_path), self.halt)

    def test_t03_legacy_identity_not_adopted(self):
        e = self.aiq(account_id='AIQ-PAPER-V2', capital=100000)
        self.assertNotEqual(e.account.account_id, 'AIQ-PAPER-V1')
        self.assertNotEqual(e.account.initial_capital, 1000000.0)

    def test_t04_account_identity_mismatch_fail_closed(self):
        e = self.aiq()
        payload = json.loads((self.root / 'account.json').read_text(encoding='utf-8'))
        payload['account_id'] = 'AIQ-PAPER-V1'
        _write(self.root / 'account.json', payload)
        bad = PaperExecutionEngine(storage_dir=str(self.root), account_id='AIQ-PAPER-V2', initial_capital=100000, season_id=SEASON, governance_safety_path=str(self.halt))
        self.assertTrue(bad.account.account_corrupted)
        self.assertFalse(bad.ensure_pre_cycle_safety()[0])

    def test_t05_season_identity_mismatch_fail_closed(self):
        self.aiq()
        payload = json.loads((self.root / 'account.json').read_text(encoding='utf-8'))
        payload['season_id'] = 'WRONG-SEASON'
        _write(self.root / 'account.json', payload)
        bad = PaperExecutionEngine(storage_dir=str(self.root), account_id='AIQ-PAPER-V2', initial_capital=100000, season_id=SEASON, governance_safety_path=str(self.halt))
        self.assertTrue(bad.account.account_corrupted)

    def test_t06_capital_identity_mismatch_fail_closed(self):
        self.aiq()
        payload = json.loads((self.root / 'account.json').read_text(encoding='utf-8'))
        payload['initial_capital'] = 1000000
        _write(self.root / 'account.json', payload)
        bad = PaperExecutionEngine(storage_dir=str(self.root), account_id='AIQ-PAPER-V2', initial_capital=100000, season_id=SEASON, governance_safety_path=str(self.halt))
        self.assertTrue(bad.account.account_corrupted)

    def test_t07_forward_engine_uses_context_identity(self):
        from ai_quant.forward_runner import ForwardPaperRunner
        context = {'season_id': SEASON, 'season_root': str(self.root), 'ai_quant_paper_root': str(self.root), 'governance_safety_path': str(self.halt), 'manifest': {}}
        runner = ForwardPaperRunner(storage_dir=str(self.root), context=context)
        self.assertEqual(runner.engine.account.account_id, 'AIQ-PAPER-V2')

    def test_t08_governance_schema_preserved_on_halt(self):
        e = self.aiq()
        e.policy.set_execution_state_unsafe('G5_TEST')
        payload = json.loads(self.halt.read_text(encoding='utf-8'))
        for key in ('startup_status', 'trading_enabled', 'real_trading', 'risk_exit_enabled'):
            self.assertIn(key, payload)
        self.assertFalse(payload['execution_state_safe'])

    def test_t09_empty_registry_identity(self):
        e = self.aiq()
        registry = json.loads((self.root / 'processed_idempotency_registry.json').read_text(encoding='utf-8'))
        self.assertEqual((registry['season_id'], registry['account_id'], registry['total_keys']), (SEASON, 'AIQ-PAPER-V2', 0))

    def test_t10_initial_reconciliation_pass(self):
        e = self.aiq()
        self.assertEqual(e.ensure_pre_cycle_safety()[0], True)

    # AIQ BUY / SELL / idempotency / reconciliation (T11-T25)
    def test_t11_buy_order_created(self):
        e = self.aiq(); self.assertTrue(self.buy(e)[0])
        self.assertEqual(len((self.root / 'orders.jsonl').read_text(encoding='utf-8').splitlines()), 1)

    def test_t12_buy_fill_created(self):
        e = self.aiq(); self.buy(e)
        self.assertEqual(len((self.root / 'fills.jsonl').read_text(encoding='utf-8').splitlines()), 1)

    def test_t13_buy_registry_committed(self):
        e = self.aiq(); self.buy(e)
        reg = json.loads((self.root / 'processed_idempotency_registry.json').read_text(encoding='utf-8'))
        self.assertEqual(list(reg['records'].values())[0]['status'], 'COMMITTED')

    def test_t14_buy_position_and_cash(self):
        e = self.aiq(); self.buy(e)
        self.assertGreater(e.account.positions[0]['shares'], 0)
        self.assertLess(e.account.cash, 100000)

    def test_t15_buy_entry_trace_persisted(self):
        e = self.aiq(); self.buy(e)
        p = json.loads((self.root / 'positions.json').read_text(encoding='utf-8'))[0]
        r = list(json.loads((self.root / 'processed_idempotency_registry.json').read_text(encoding='utf-8'))['records'].values())[0]
        self.assertEqual(p['entry_transaction_id'], r['transaction_id'])

    def test_t16_buy_equity_snapshot(self):
        e = self.aiq(); self.buy(e)
        self.assertTrue((self.root / 'equity_curve.jsonl').exists())
        self.assertGreaterEqual(len((self.root / 'equity_curve.jsonl').read_text(encoding='utf-8').splitlines()), 1)

    def test_t17_buy_reload_preserves_trace(self):
        e = self.aiq(); self.buy(e); tx = e.account.positions[0]['entry_transaction_id']
        fresh = self.aiq()
        self.assertEqual(fresh.account.positions[0]['entry_transaction_id'], tx)

    def test_t18_buy_reload_reconciliation_pass(self):
        e = self.aiq(); self.buy(e); self.aiq()
        self.assertEqual(LedgerReconciliationEngine(str(self.root)).run_startup_reconciliation()['status'], 'PASS')

    def test_t19_buy_idempotent_replay_no_second_fill(self):
        e = self.aiq(); self.buy(e); result = self.buy(e)
        self.assertFalse(result[0])
        self.assertEqual(len((self.root / 'fills.jsonl').read_text(encoding='utf-8').splitlines()), 1)

    def test_t20_t1_same_day_blocked(self):
        e = self.aiq(); self.buy(e); e.monitor_and_execute_exits({'600000': 11}, '20260918')
        self.assertEqual(len((self.root / 'fills.jsonl').read_text(encoding='utf-8').splitlines()), 1)

    def test_t21_next_day_sell_fill(self):
        e = self.aiq(); self.buy(e); self.sell_next_day(e, self.next_valid_trading_date())
        self.assertEqual(len((self.root / 'fills.jsonl').read_text(encoding='utf-8').splitlines()), 2)

    def test_t22_sell_removes_position(self):
        e = self.aiq(); self.buy(e); self.sell_next_day(e, self.next_valid_trading_date())
        self.assertEqual(e.account.positions, [])

    def test_t23_sell_result_links_buy(self):
        e = self.aiq(); self.buy(e); tx = e.account.positions[0]['entry_transaction_id']; self.sell_next_day(e, self.next_valid_trading_date())
        result = json.loads((self.root / 'paper_trade_results.jsonl').read_text(encoding='utf-8').splitlines()[0])
        self.assertEqual(result['open_transaction_id'], tx)

    def test_t24_sell_idempotent_replay(self):
        e = self.aiq(); self.buy(e); sell_date = self.next_valid_trading_date(); self.sell_next_day(e, sell_date); counts = {n: len((self.root / n).read_text(encoding='utf-8').splitlines()) for n in ('orders.jsonl','fills.jsonl','paper_trade_results.jsonl')}
        fresh = self.aiq(); fresh.monitor_and_execute_exits({'600000': 11}, sell_date)
        self.assertEqual(counts, {n: len((self.root / n).read_text(encoding='utf-8').splitlines()) for n in counts})

    def test_t25_sell_reload_reconciliation_pass(self):
        e = self.aiq(); self.buy(e); self.sell_next_day(e); self.aiq()
        self.assertEqual(LedgerReconciliationEngine(str(self.root)).run_daily_ledger_reconciliation()['status'], 'PASS')

    # Strategy participant E2E and isolation (T26-T35)
    def strategy_root(self, participant='P-HANSHU'):
        root = self.root / 'competition_ledgers' / participant
        account = {'participant_id': participant, 'account_id': 'HANSHU-V2' if participant == 'P-HANSHU' else participant + '-V2', 'season_id': SEASON, 'initial_capital': 100000.0, 'cash': 100000.0, 'market_value': 0.0, 'total_equity': 100000.0, 'net_pnl': 0.0, 'net_pnl_pct': 0.0, 'peak_equity': 100000.0, 'max_drawdown_pct': 0.0, 'max_positions': 3, 'max_single_position_ratio': 0.3, 'max_equity_ratio': 0.9}
        _write(root / 'account.json', account); _write(root / 'positions.json', [])
        _write(root / 'processed_idempotency_registry.json', {'schema_version':'v7.2','season_id':SEASON,'participant_id':participant,'account_id':account['account_id'],'records':{},'total_keys':0})
        for name in ('orders.jsonl','fills.jsonl','paper_trade_results.jsonl','equity_curve.jsonl'):
            (root / name).write_text('', encoding='utf-8')
        return root, account

    def commit_strategy_buy(self, root, account, positions, plan='SP1', symbol='600001', decision='SD1'):
        price = 10.0; shares = 100
        account = dict(account); account['cash'] -= 1000; account['market_value'] = (len(positions) + 1) * 1000; account['total_equity'] = 100000; account['net_pnl'] = 0; account['net_pnl_pct'] = 0
        positions = [dict(p) for p in positions] + [{'symbol':symbol,'shares':shares,'cost_price':price,'current_price':price,'buy_date':'20260918','plan_id':plan,'participant_id':account['participant_id'],'account_id':account['account_id'],'season_id':SEASON}]
        trade = {'direction':'BUY','symbol':symbol,'shares':shares,'price':price,'date':'20260918','plan_id':plan,'decision_id':decision,'net_cash_effect':-1000,'commission':0,'stamp_tax':0}
        return commit_strategy_transaction(str(root), account, positions, trade, decision={'decision_id':decision,'participant_id':account['participant_id']}), account, positions

    def test_t26_strategy_buy_commits(self):
        root, account = self.strategy_root(); result, _, _ = self.commit_strategy_buy(root, account, []); self.assertEqual(result['status'], 'COMMITTED')

    def test_t27_strategy_order_fill_separate(self):
        root, account = self.strategy_root(); self.commit_strategy_buy(root, account, []); self.assertNotEqual((root/'orders.jsonl').read_text(), (root/'fills.jsonl').read_text())

    def test_t28_strategy_reload_entry_trace(self):
        root, account = self.strategy_root(); result, _, _ = self.commit_strategy_buy(root, account, []); pos = json.loads((root/'positions.json').read_text())[0]; self.assertEqual(pos['entry_transaction_id'], result['transaction_id'])

    def test_t29_strategy_reconcile_after_buy(self):
        root, account = self.strategy_root(); self.commit_strategy_buy(root, account, []); self.assertEqual(reconcile_transaction_ledger(str(root))['status'], 'PASS')

    def test_t30_strategy_other_ledgers_untouched(self):
        participants = ('P-HANSHU', 'P-YINWENTAO', 'P-LIAO-XIAOAI', 'P-COOL', 'P-RULE-QUANT')
        roots = {}
        for participant in participants:
            roots[participant], _ = self.strategy_root(participant)
        before = {
            participant: {
                name: _sha(roots[participant] / name)
                for name in ('account.json', 'positions.json', 'orders.jsonl', 'fills.jsonl',
                             'paper_trade_results.jsonl', 'equity_curve.jsonl',
                             'processed_idempotency_registry.json')
            }
            for participant in participants if participant != 'P-HANSHU'
        }
        root, account = roots['P-HANSHU'], json.loads((roots['P-HANSHU'] / 'account.json').read_text(encoding='utf-8'))
        self.commit_strategy_buy(root, account, [])
        after = {
            participant: {
                name: _sha(roots[participant] / name)
                for name in before[participant]
            }
            for participant in before
        }
        self.assertEqual(before, after)

    def test_t31_strategy_participant_identity(self):
        root, account = self.strategy_root('P-YINWENTAO'); result, _, _ = self.commit_strategy_buy(root, account, [], decision='SAME'); self.assertEqual(result['decision_id'], 'SAME')

    def test_t32_strategy_sell_trace(self):
        root, account = self.strategy_root(); buy, a, pos = self.commit_strategy_buy(root, account, []); sell_account = dict(a); sell_account['cash'] = 100005; sell_account['market_value'] = 0; sell_account['total_equity'] = 100005; sell_account['net_pnl'] = 5; pos[0]['entry_transaction_id'] = buy['transaction_id']; pos[0]['entry_order_id'] = buy['order_id']; pos[0]['entry_fill_id'] = buy['fill_id']; sell = {'direction':'SELL','symbol':'600001','shares':100,'price':10.05,'date':'20260919','plan_id':'SP1','decision_id':'SD1','entry_transaction_id':buy['transaction_id'],'entry_price':10.0,'entry_date':'20260918','net_cash_effect':1005,'reason':'TEST'}; res = commit_strategy_transaction(str(root), sell_account, [], sell, decision={'decision_id':'SD1','participant_id':'P-HANSHU'}); self.assertEqual(res['status'], 'COMMITTED')

    def test_t33_strategy_sell_result_open_ref(self):
        self.test_t32_strategy_sell_trace(); root = self.root/'competition_ledgers'/'P-HANSHU'; result = json.loads((root/'paper_trade_results.jsonl').read_text().splitlines()[0]); self.assertTrue(result['open_transaction_id'])

    def test_t34_strategy_sell_reconcile(self):
        self.test_t32_strategy_sell_trace(); self.assertEqual(reconcile_transaction_ledger(str(self.root/'competition_ledgers'/'P-HANSHU'))['status'], 'PASS')

    def test_t35_strategy_risk_sentinel_no_ledger(self):
        self.assertFalse((self.root/'competition_ledgers'/'P-RISK-SENTINEL').exists())

    # Multi-trade intermediate snapshots / idempotency isolation (T36-T45)
    def test_t36_two_buys_create_two_transactions(self):
        root, a = self.strategy_root(); r1, a1, p1 = self.commit_strategy_buy(root, a, [], 'A','600001','D'); r2, a2, p2 = self.commit_strategy_buy(root, a1, p1, 'B','600002','D2'); self.assertEqual((r1['status'],r2['status']),('COMMITTED','COMMITTED'))

    def test_t37_two_buys_have_two_orders(self):
        self.test_t36_two_buys_create_two_transactions(); root=self.root/'competition_ledgers'/'P-HANSHU'; self.assertEqual(len((root/'orders.jsonl').read_text().splitlines()),2)

    def test_t38_two_buys_have_two_fills(self):
        self.test_t36_two_buys_create_two_transactions(); root=self.root/'competition_ledgers'/'P-HANSHU'; self.assertEqual(len((root/'fills.jsonl').read_text().splitlines()),2)

    def test_t39_intermediate_equity_snapshots_differ(self):
        self.test_t36_two_buys_create_two_transactions(); rows=[json.loads(x) for x in (self.root/'competition_ledgers'/'P-HANSHU'/'equity_curve.jsonl').read_text().splitlines()]; self.assertEqual([r['position_count'] for r in rows], [1,2])

    def test_t40_multi_trade_reconcile(self):
        self.test_t36_two_buys_create_two_transactions(); self.assertEqual(reconcile_transaction_ledger(str(self.root/'competition_ledgers'/'P-HANSHU'))['status'],'PASS')

    def test_t41_different_participants_same_decision_isolated(self):
        r1,a1=self.strategy_root('P-HANSHU'); r2,a2=self.strategy_root('P-YINWENTAO'); x,_,_=self.commit_strategy_buy(r1,a1,[], 'P1','600001','SAME'); y,_,_=self.commit_strategy_buy(r2,a2,[], 'P2','600001','SAME'); self.assertNotEqual(x['idempotency_key'], y['idempotency_key'])

    def test_t42_same_participant_same_decision_duplicate(self):
        root,a=self.strategy_root()
        self.commit_strategy_buy(root,a,[],'P1','600001','SAME')
        duplicate = self.commit_strategy_buy(root,a,[],'P1','600001','SAME')[0]
        self.assertEqual(duplicate['status'], 'DUPLICATE')

    def test_t43_snapshot_hashes_differ(self):
        self.test_t36_two_buys_create_two_transactions()
        root = self.root / 'competition_ledgers' / 'P-HANSHU'
        rows = [json.loads(x) for x in (root / 'equity_curve.jsonl').read_text(encoding='utf-8').splitlines()]
        registry = json.loads((root / 'processed_idempotency_registry.json').read_text(encoding='utf-8'))
        records = {record['transaction_id']: record for record in registry['records'].values()}
        self.assertEqual(rows[0]['post_account_hash'], records[rows[0]['transaction_id']]['post_account_hash'])
        self.assertEqual(rows[1]['post_account_hash'], records[rows[1]['transaction_id']]['post_account_hash'])
        self.assertNotEqual(rows[0]['post_account_hash'], rows[1]['post_account_hash'])

    def test_t44_no_live_strategy_mutation(self):
        self.assertEqual(self._live_hashes(), self.live_hashes)

    def test_t45_strategy_transaction_refs_complete(self):
        self.test_t36_two_buys_create_two_transactions(); root=self.root/'competition_ledgers'/'P-HANSHU'; rows=[json.loads(x) for x in (root/'fills.jsonl').read_text().splitlines()]; self.assertTrue(all(f.get('order_id') and f.get('transaction_id') for f in rows))

    # Process reload and fail-closed evidence (T46-T53)
    def test_t46_true_subprocess_reload(self):
        e=self.aiq(); self.buy(e)
        script=self.root/'reload.py'; script.write_text("import sys,json; sys.path.insert(0, sys.argv[2]); from ai_quant.paper_executor import PaperExecutionEngine; e=PaperExecutionEngine(storage_dir=sys.argv[1],account_id='AIQ-PAPER-V2',initial_capital=100000,season_id='V7-SEASON-20260917-V1',governance_safety_path=sys.argv[3]); r=e.ensure_pre_cycle_safety(); print(r[0],r[1])", encoding='utf-8')
        out=subprocess.run([sys.executable,str(script),str(self.root),str(SCRIPTS),str(self.halt)],capture_output=True,text=True,encoding='utf-8',check=True); self.assertIn('True',out.stdout, out.stdout)

    def test_t47_prepared_detected(self):
        e=self.aiq(); self.buy(e); reg=json.loads((self.root/'processed_idempotency_registry.json').read_text()); next(iter(reg['records'].values()))['status']='PREPARED'; _write(self.root/'processed_idempotency_registry.json',reg); self.assertEqual(LedgerReconciliationEngine(str(self.root)).run_startup_reconciliation()['status'],'FAIL')

    def test_t48_order_without_fill_detected(self):
        e=self.aiq(); self.buy(e); fills=(self.root/'fills.jsonl').read_text(); (self.root/'fills.jsonl').write_text('',encoding='utf-8'); self.assertEqual(LedgerReconciliationEngine(str(self.root)).run_startup_reconciliation()['status'],'FAIL')

    def test_t49_fill_without_effect_detected(self):
        e=self.aiq(); self.buy(e); _write(self.root/'positions.json',[]); self.assertEqual(LedgerReconciliationEngine(str(self.root)).run_startup_reconciliation()['status'],'FAIL')

    def test_t50_committed_without_fill_detected(self):
        e=self.aiq(); self.buy(e); (self.root/'fills.jsonl').write_text('',encoding='utf-8'); self.assertEqual(LedgerReconciliationEngine(str(self.root)).run_startup_reconciliation()['status'],'FAIL')

    def test_t51_duplicate_fill_detected(self):
        e=self.aiq(); self.buy(e); line=(self.root/'fills.jsonl').read_text(); (self.root/'fills.jsonl').write_text(line+line,encoding='utf-8'); self.assertEqual(LedgerReconciliationEngine(str(self.root)).run_startup_reconciliation()['status'],'FAIL')

    def test_t52_duplicate_order_detected(self):
        e=self.aiq(); self.buy(e); line=(self.root/'orders.jsonl').read_text(); (self.root/'orders.jsonl').write_text(line+line,encoding='utf-8'); self.assertEqual(LedgerReconciliationEngine(str(self.root)).run_startup_reconciliation()['status'],'FAIL')

    def test_t53_equity_mismatch_detected(self):
        e=self.aiq(); self.buy(e); p=json.loads((self.root/'account.json').read_text()); p['total_equity']+=1; _write(self.root/'account.json',p); self.assertEqual(LedgerReconciliationEngine(str(self.root)).run_startup_reconciliation()['status'],'FAIL')

    # Live isolation and final governance invariants (T54-T60)
    def test_t54_live_forward_day_zero(self):
        m=json.loads((LIVE_SEASON/'forward'/'forward_epoch_manifest.json').read_text(encoding='utf-8')); self.assertEqual(m['forward_day_count'],0)

    def test_t55_live_forward_disabled(self):
        m=json.loads((LIVE_SEASON/'forward'/'forward_epoch_manifest.json').read_text(encoding='utf-8')); self.assertFalse(m['forward_enabled'])

    def test_t56_live_entry_disabled(self):
        h=json.loads((LIVE_SEASON/'governance_halt.json').read_text()); self.assertTrue(h['new_entry_disabled'])

    def test_t57_live_trading_disabled(self):
        h=json.loads((LIVE_SEASON/'governance_halt.json').read_text()); self.assertFalse(h['trading_enabled'])

    def test_t58_live_real_trading_false(self):
        h=json.loads((LIVE_SEASON/'governance_halt.json').read_text()); self.assertFalse(h['real_trading'])

    def test_t59_live_aiq_empty(self):
        p=json.loads((LIVE_SEASON/'ai_quant_paper'/'positions.json').read_text()); self.assertEqual(p,[])

    def test_t60_live_hashes_unchanged(self):
        self.assertEqual(self.live_hashes, self._live_hashes())


if __name__ == '__main__':
    unittest.main()
