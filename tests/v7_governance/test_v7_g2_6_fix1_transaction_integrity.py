# -*- coding: utf-8 -*-
"""G2.6 FIX1 strategy transaction, relationship, and crash-detection tests."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))

from competition.transaction_writer import (
    TransactionIntegrityError,
    commit_strategy_transaction,
    reconcile_transaction_ledger,
)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, (dict, list)):
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    else:
        path.write_text(str(value), encoding='utf-8')


def append(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(value, ensure_ascii=False) + '\n')


def account(cash=100000.0, market_value=0.0, equity=None, net_pnl=None, positions=None):
    equity = cash + market_value if equity is None else equity
    net_pnl = equity - 100000.0 if net_pnl is None else net_pnl
    return {
        'participant_id': 'P-HANSHU', 'account_id': 'HANSHU-V2',
        'season_id': 'V7-SEASON-20260917-V1', 'initial_capital': 100000.0,
        'cash': cash, 'market_value': market_value, 'total_equity': equity,
        'net_pnl': net_pnl, 'net_pnl_pct': net_pnl / 1000.0,
        'peak_equity': max(100000.0, equity), 'max_drawdown_pct': 0.0,
    }


class TransactionFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'P-HANSHU'
        self.root.mkdir(parents=True)
        write(self.root / 'account.json', account())
        write(self.root / 'positions.json', [])
        write(self.root / 'processed_idempotency_registry.json', {
            'schema_version': 'v7.2', 'season_id': 'V7-SEASON-20260917-V1',
            'participant_id': 'P-HANSHU', 'account_id': 'HANSHU-V2',
            'records': {}, 'total_keys': 0,
        })
        for name in ('orders.jsonl', 'fills.jsonl', 'paper_trade_results.jsonl', 'equity_curve.jsonl'):
            write(self.root / name, '')

    def tearDown(self):
        self.tmp.cleanup()

    def buy_trade(self, decision_id='DEC-BUY-1'):
        return {
            'direction': 'BUY', 'symbol': 'sh600000', 'shares': 1000,
            'price': 10.0, 'commission': 5.0, 'stamp_tax': 0.0,
            'date': '2026-09-17', 'ts': '2026-09-17T01:00:00+00:00',
            'decision_id': decision_id, 'idempotency_key':
                f'V7-SEASON-20260917-V1:P-HANSHU:{decision_id}:BUY',
        }

    def buy_effect(self):
        pos = [{'symbol': 'sh600000', 'code': '600000', 'name': 'test', 'shares': 1000,
                'cost_price': 10.0, 'current_price': 10.0, 'buy_date': '2026-09-17'}]
        return account(cash=89995.0, market_value=10000.0, equity=99995.0, net_pnl=-5.0), pos

    def commit_buy(self, decision_id='DEC-BUY-1'):
        acc, pos = self.buy_effect()
        self.last_buy_result = commit_strategy_transaction(str(self.root), acc, pos, self.buy_trade(decision_id))
        return self.last_buy_result

    def sell_trade(self, decision_id='DEC-SELL-1'):
        return {'direction':'SELL','symbol':'sh600000','shares':1000,'price':11.0,
                'commission':5.0,'stamp_tax':5.5,'date':'2026-09-18',
                'decision_id':decision_id,'entry_price':10.0,
                'entry_date':'2026-09-17','entry_transaction_id':
                self.last_buy_result['transaction_id'],'reason':'test close'}

    def test_t40_buy_intent_creates_distinct_order(self):
        result = self.commit_buy()
        orders = [json.loads(x) for x in (self.root / 'orders.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(result['status'], 'COMMITTED')
        self.assertEqual(orders[0]['status'], 'FILLED')
        self.assertIn('order_id', orders[0]); self.assertNotIn('fill_id', orders[0])

    def test_t41_buy_fill_created(self):
        self.commit_buy()
        fills = [json.loads(x) for x in (self.root / 'fills.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(fills), 1); self.assertIn('fill_id', fills[0]); self.assertEqual(fills[0]['direction'], 'BUY')

    def test_t42_buy_registry_committed(self):
        result = self.commit_buy()
        reg = json.loads((self.root / 'processed_idempotency_registry.json').read_text(encoding='utf-8'))
        self.assertEqual(reg['records'][result['idempotency_key']]['status'], 'COMMITTED')

    def test_t43_buy_cash_effect(self):
        self.commit_buy()
        self.assertEqual(json.loads((self.root / 'account.json').read_text(encoding='utf-8'))['cash'], 89995.0)

    def test_t44_buy_position_effect(self):
        self.commit_buy()
        self.assertEqual(json.loads((self.root / 'positions.json').read_text(encoding='utf-8'))[0]['shares'], 1000)

    def test_t45_buy_equity_snapshot_appended(self):
        self.commit_buy()
        row = json.loads((self.root / 'equity_curve.jsonl').read_text(encoding='utf-8').splitlines()[0])
        fill = json.loads((self.root / 'fills.jsonl').read_text(encoding='utf-8').splitlines()[0])
        self.assertEqual(row['transaction_id'], fill['transaction_id'])
        self.assertEqual(row['total_equity'], 99995.0)

    def test_t46_buy_reload_startup_reconciliation_passes(self):
        self.commit_buy()
        self.assertEqual(reconcile_transaction_ledger(str(self.root))['status'], 'PASS')

    def test_t47_buy_reload_daily_reconciliation_passes(self):
        from unittest.mock import patch
        from test_v7_g2_6_runtime_safety import G26Fixture
        from competition.season_runtime import run_daily_reconciliation
        import simulated_trading as sim
        fixture = G26Fixture('test_t01_valid_arena_snapshot_loads')
        fixture.setUp()
        try:
            acc = sim.new_account('hanshu')
            acc.update({'participant_id':'P-HANSHU','account_id':'HANSHU-V2','season_id':fixture.sid,'cash':89995.0})
            acc['positions'] = [{'symbol':'sh600000','code':'600000','name':'test','shares':1000,'cost_price':10.0,'current_price':10.0,'buy_date':'2026-09-17'}]
            acc['trade_ids'] = [self.buy_trade('DEC-DAILY-RECON')]
            with patch.object(sim, '_active_runtime_context', return_value=fixture.ctx):
                sim._persist_active_strategy_ledgers({'hanshu': acc})
            self.assertEqual(run_daily_reconciliation(fixture.ctx)['status'], 'PASS')
        finally:
            fixture.tearDown()

    def test_t48_same_idempotency_key_has_no_second_fill(self):
        first = self.commit_buy()
        second = self.commit_buy()
        self.assertEqual(first['status'], 'COMMITTED'); self.assertEqual(second['status'], 'DUPLICATE')
        self.assertEqual(len((self.root / 'fills.jsonl').read_text(encoding='utf-8').splitlines()), 1)

    def test_t49_same_decision_other_participant_is_independent(self):
        other = Path(self.tmp.name) / 'P-COOL'; other.mkdir()
        for name in ('account.json', 'positions.json', 'processed_idempotency_registry.json', 'orders.jsonl', 'fills.jsonl', 'paper_trade_results.jsonl', 'equity_curve.jsonl'):
            src = self.root / name
            dst = other / name
            if name == 'account.json': write(dst, {**account(), 'participant_id': 'P-COOL', 'account_id': 'COOL-V2'})
            elif name == 'processed_idempotency_registry.json': write(dst, {'schema_version':'v7.2','season_id':'V7-SEASON-20260917-V1','participant_id':'P-COOL','account_id':'COOL-V2','records':{}})
            elif name == 'positions.json': write(dst, [])
            else: write(dst, '')
        other_account = {**account(cash=89995.0, market_value=10000.0, equity=99995.0, net_pnl=-5.0), 'participant_id':'P-COOL','account_id':'COOL-V2'}
        other_positions = [{'symbol':'sh600000','code':'600000','name':'test','shares':1000,
                            'cost_price':10.0,'current_price':10.0,'buy_date':'2026-09-17'}]
        result = commit_strategy_transaction(str(other), other_account, other_positions, {**self.buy_trade(), 'participant_id':'P-COOL','account_id':'COOL-V2'})
        self.assertEqual(result['status'], 'COMMITTED')

    def test_t50_sell_creates_order_fill(self):
        self.commit_buy()
        sell_acc = account(cash=100984.5, market_value=0.0, equity=100984.5, net_pnl=984.5)
        sell = self.sell_trade()
        result = commit_strategy_transaction(str(self.root), sell_acc, [], sell)
        self.assertEqual(result['status'], 'COMMITTED')
        self.assertEqual(len((self.root / 'fills.jsonl').read_text(encoding='utf-8').splitlines()), 2)

    def test_t51_sell_registry_committed(self):
        self.commit_buy()
        result = commit_strategy_transaction(str(self.root), account(cash=100984.5, equity=100984.5, net_pnl=984.5), [], self.sell_trade())
        reg = json.loads((self.root / 'processed_idempotency_registry.json').read_text(encoding='utf-8'))
        self.assertEqual(reg['records'][result['idempotency_key']]['status'], 'COMMITTED')

    def test_t52_sell_cash_increases(self):
        self.commit_buy(); self.test_t51_sell_registry_committed()
        self.assertEqual(json.loads((self.root / 'account.json').read_text(encoding='utf-8'))['cash'], 100984.5)

    def test_t53_sell_removes_position(self):
        self.commit_buy(); commit_strategy_transaction(str(self.root), account(cash=100984.5, equity=100984.5, net_pnl=984.5), [], self.sell_trade())
        self.assertEqual(json.loads((self.root / 'positions.json').read_text(encoding='utf-8')), [])

    def test_t54_sell_closed_trade_result(self):
        self.commit_buy(); commit_strategy_transaction(str(self.root), account(cash=100984.5, equity=100984.5, net_pnl=984.5), [], self.sell_trade())
        result = json.loads((self.root / 'paper_trade_results.jsonl').read_text(encoding='utf-8').splitlines()[0])
        self.assertEqual(result['exit_price'], 11.0); self.assertIn('net_pnl', result)

    def test_t55_sell_reload_reconciliation_passes(self):
        self.commit_buy(); commit_strategy_transaction(str(self.root), account(cash=100984.5, equity=100984.5, net_pnl=984.5), [], self.sell_trade())
        self.assertEqual(reconcile_transaction_ledger(str(self.root))['status'], 'PASS')

    def partial_registry(self, status, record=None):
        record = record or {'status':status,'transaction_id':'TX-1','order_id':'ORD-1','fill_id':'FILL-1','decision_id':'DEC-1','participant_id':'P-HANSHU','season_id':'V7-SEASON-20260917-V1','account_id':'HANSHU-V2','direction':'BUY','symbol':'sh600000'}
        write(self.root / 'processed_idempotency_registry.json', {'season_id':'V7-SEASON-20260917-V1','participant_id':'P-HANSHU','account_id':'HANSHU-V2','records':{'key':record}})

    def test_t56_prepared_only_fails(self):
        self.partial_registry('PREPARED'); self.assertEqual(reconcile_transaction_ledger(str(self.root))['status'], 'FAIL')

    def test_t57_order_without_fill_fails(self):
        self.partial_registry('PREPARED'); append(self.root/'orders.jsonl', {'order_id':'ORD-1','transaction_id':'TX-1','decision_id':'DEC-1','season_id':'V7-SEASON-20260917-V1','participant_id':'P-HANSHU','account_id':'HANSHU-V2','symbol':'sh600000','direction':'BUY','requested_shares':100,'requested_price':10,'created_at':'x','status':'FILLED'})
        self.assertEqual(reconcile_transaction_ledger(str(self.root))['status'], 'FAIL')

    def test_t58_fill_without_account_effect_fails(self):
        self.partial_registry('PREPARED'); append(self.root/'orders.jsonl', {'order_id':'ORD-1','transaction_id':'TX-1','decision_id':'DEC-1','season_id':'V7-SEASON-20260917-V1','participant_id':'P-HANSHU','account_id':'HANSHU-V2','symbol':'sh600000','direction':'BUY','requested_shares':100,'requested_price':10,'created_at':'x','status':'FILLED'}); append(self.root/'fills.jsonl', {'fill_id':'FILL-1','order_id':'ORD-1','transaction_id':'TX-1','decision_id':'DEC-1','season_id':'V7-SEASON-20260917-V1','participant_id':'P-HANSHU','account_id':'HANSHU-V2','symbol':'sh600000','direction':'BUY','shares':100,'price':10,'net_cash_effect':-1005})
        self.assertEqual(reconcile_transaction_ledger(str(self.root))['status'], 'FAIL')

    def test_t59_uncommitted_effect_fails(self):
        self.partial_registry('EFFECTS_WRITTEN'); acc, pos = self.buy_effect(); write(self.root/'account.json', acc); write(self.root/'positions.json', pos); self.assertEqual(reconcile_transaction_ledger(str(self.root))['status'], 'FAIL')

    def test_t60_committed_without_fill_fails(self):
        self.partial_registry('COMMITTED'); self.assertEqual(reconcile_transaction_ledger(str(self.root))['status'], 'FAIL')

    def test_t61_duplicate_fill_fails(self):
        self.partial_registry('PREPARED'); append(self.root/'fills.jsonl', {'fill_id':'FILL-1','order_id':'ORD-1','transaction_id':'TX-1','direction':'BUY'}); append(self.root/'fills.jsonl', {'fill_id':'FILL-1','order_id':'ORD-1','transaction_id':'TX-1','direction':'BUY'}); self.assertEqual(reconcile_transaction_ledger(str(self.root))['status'], 'FAIL')

    def test_t62_duplicate_order_fails(self):
        self.partial_registry('PREPARED'); append(self.root/'orders.jsonl', {'order_id':'ORD-1','transaction_id':'TX-1','status':'FILLED'}); append(self.root/'orders.jsonl', {'order_id':'ORD-1','transaction_id':'TX-1','status':'FILLED'}); self.assertEqual(reconcile_transaction_ledger(str(self.root))['status'], 'FAIL')

    def test_t63_cash_equity_mismatch_fails(self):
        write(self.root/'account.json', account(cash=90000.0, equity=100000.0, net_pnl=0.0)); self.assertEqual(reconcile_transaction_ledger(str(self.root))['status'], 'FAIL')

    def test_t64_order_and_fill_schemas_are_separate(self):
        self.commit_buy(); order = json.loads((self.root/'orders.jsonl').read_text(encoding='utf-8')); fill = json.loads((self.root/'fills.jsonl').read_text(encoding='utf-8')); self.assertNotIn('fill_id', order); self.assertIn('fill_id', fill); self.assertEqual(fill['order_id'], order['order_id'])

    def test_t65_committed_registry_relations_exist(self):
        result = self.commit_buy(); check = reconcile_transaction_ledger(str(self.root)); self.assertEqual(check['status'], 'PASS'); self.assertEqual(check['registry']['records'][result['idempotency_key']]['fill_id'], result['fill_id'])

    def test_t66_noncommitted_registry_fails(self):
        self.commit_buy(); reg = json.loads((self.root/'processed_idempotency_registry.json').read_text(encoding='utf-8')); key = next(iter(reg['records'])); reg['records'][key]['status'] = 'EFFECTS_WRITTEN'; write(self.root/'processed_idempotency_registry.json', reg); self.assertEqual(reconcile_transaction_ledger(str(self.root))['status'], 'FAIL')

    def test_t67_account_equity_identity(self):
        self.commit_buy(); check = reconcile_transaction_ledger(str(self.root)); self.assertEqual(check['account']['cash'] + check['account']['market_value'], check['account']['total_equity'])

    def test_t68_net_pnl_identity(self):
        self.commit_buy(); check = reconcile_transaction_ledger(str(self.root)); self.assertAlmostEqual(check['account']['net_pnl'], check['account']['total_equity'] - check['account']['initial_capital'], places=2)

    def test_t69_simulated_persist_routes_through_transaction_writer(self):
        from unittest.mock import patch
        from test_v7_g2_6_runtime_safety import G26Fixture
        import simulated_trading as sim
        fixture = G26Fixture('test_t01_valid_arena_snapshot_loads')
        fixture.setUp()
        try:
            acc = sim.new_account('hanshu')
            acc.update({'participant_id': 'P-HANSHU', 'account_id': 'HANSHU-V2', 'season_id': fixture.sid, 'cash': 89995.0})
            acc['positions'] = [{'symbol':'sh600000','code':'600000','name':'test','shares':1000,'cost_price':10.0,'current_price':10.0,'buy_date':'2026-09-17'}]
            acc['trade_ids'] = [self.buy_trade('DEC-PRODUCTION-PERSIST')]
            with patch.object(sim, '_active_runtime_context', return_value=fixture.ctx):
                sim._persist_active_strategy_ledgers({'hanshu': acc})
            check = reconcile_transaction_ledger(str(Path(fixture.ctx['competition_ledger_root']) / 'P-HANSHU'))
            self.assertEqual(check['status'], 'PASS')
            self.assertEqual(check['orders'][0]['status'], 'FILLED')
        finally:
            fixture.tearDown()

    def test_t70_buy_position_persists_committed_entry_transaction_id(self):
        result = self.commit_buy()
        position = json.loads((self.root / 'positions.json').read_text(encoding='utf-8'))[0]
        self.assertEqual(position['entry_transaction_id'], result['transaction_id'])

    def test_t71_buy_reload_preserves_entry_trace(self):
        result = self.commit_buy()
        reloaded = json.loads((self.root / 'positions.json').read_text(encoding='utf-8'))
        self.assertEqual(reloaded[0]['entry_transaction_id'], result['transaction_id'])

    def test_t72_sell_result_references_original_buy_transaction(self):
        buy = self.commit_buy()
        commit_strategy_transaction(str(self.root), account(cash=100984.5, equity=100984.5, net_pnl=984.5), [], self.sell_trade())
        result = json.loads((self.root / 'paper_trade_results.jsonl').read_text(encoding='utf-8').splitlines()[0])
        self.assertEqual(result['open_transaction_id'], buy['transaction_id'])

    def test_t73_valid_buy_sell_chain_reconciles_open_reference(self):
        self.commit_buy()
        commit_strategy_transaction(str(self.root), account(cash=100984.5, equity=100984.5, net_pnl=984.5), [], self.sell_trade())
        self.assertEqual(reconcile_transaction_ledger(str(self.root))['status'], 'PASS')

    def test_t74_broken_trade_result_open_reference_fails_reconciliation(self):
        self.commit_buy()
        commit_strategy_transaction(str(self.root), account(cash=100984.5, equity=100984.5, net_pnl=984.5), [], self.sell_trade())
        result_path = self.root / 'paper_trade_results.jsonl'
        result = json.loads(result_path.read_text(encoding='utf-8').splitlines()[0])
        result['open_transaction_id'] = 'UNKNOWN'
        write(result_path, result)
        self.assertEqual(reconcile_transaction_ledger(str(self.root))['status'], 'FAIL')

    def test_t75_broken_position_entry_reference_fails_reconciliation(self):
        self.commit_buy()
        positions_path = self.root / 'positions.json'
        positions = json.loads(positions_path.read_text(encoding='utf-8'))
        positions[0]['entry_transaction_id'] = 'UNKNOWN'
        write(positions_path, positions)
        self.assertEqual(reconcile_transaction_ledger(str(self.root))['status'], 'FAIL')

    def test_t76_sell_without_entry_reference_is_rejected_before_fill(self):
        self.commit_buy()
        positions_path = self.root / 'positions.json'
        positions = json.loads(positions_path.read_text(encoding='utf-8'))
        positions[0].pop('entry_transaction_id', None)
        positions[0].pop('entry_order_id', None)
        positions[0].pop('entry_fill_id', None)
        positions[0].pop('entry_decision_id', None)
        write(positions_path, positions)
        sell = self.sell_trade()
        sell.pop('entry_transaction_id')
        with self.assertRaises(TransactionIntegrityError):
            commit_strategy_transaction(str(self.root), account(cash=100984.5, equity=100984.5, net_pnl=984.5), [], sell)
        self.assertEqual(len((self.root / 'fills.jsonl').read_text(encoding='utf-8').splitlines()), 1)


if __name__ == '__main__':
    unittest.main()
