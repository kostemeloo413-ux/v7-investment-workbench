import json
import datetime
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if os.path.join(ROOT, 'scripts') not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, 'scripts'))

import scan_mainboard_picks as picks
import simulated_trading as sim
from analysis.decision_gate import deterministic_execution_safety


SCHEMA = {
    'participant_id', 'skill_version', 'decision_id', 'timestamp', 'symbol',
    'action', 'candidate_evidence', 'skill_rules_triggered',
    'skill_rules_failed', 'decision_reason', 'position_plan', 'exit_plan',
    'holding_horizon', 'soft_risk_context', 'confidence', 'data_freshness',
    'evidence_refs',
}


def _quote_response():
    values = [''] * 47
    values[1] = '芯片科技'
    values[2] = '600000'
    values[3] = '10.00'
    values[6] = '1000000'
    values[32] = '5.0'
    values[37] = '600000'
    values[38] = '8.0'
    values[39] = '20.0'
    values[45] = '500.0'
    return 'v_sh600000="' + '~'.join(values) + '~";\n'


def _scan_result():
    with patch.object(picks, 'load_stock_universe',
                      return_value={'stocks': [{'symbol': 'sh600000'}]}), \
         patch.object(picks, 'fetch_quotes_batch', return_value=_quote_response()):
        return picks.scan_and_score_all(persist=False)


def _account():
    return {
        'blogger_id': 'hanshu', 'blogger_name': '韩叔',
        'cash': 100000.0, 'positions': [], 'trade_ids': [], 'total_pnl': 0.0,
        'new_entry_disabled': False, 'execution_state_safe': True,
        'trading_enabled': True, 'real_trading': False,
    }


class G25Fix2SkillDecisionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = _scan_result()
        cls.by_pid = {p['participant_id']: p for p in cls.result}

    def test_t34_t35_all_picks_have_valid_canonical_decision(self):
        self.assertEqual(len(self.result), 6)
        for item in self.result:
            self.assertIn('skill_decision', item)
            self.assertEqual(set(item['skill_decision']), SCHEMA)
            self.assertIn(item['skill_decision']['action'],
                          {'BUY', 'SELL', 'HOLD', 'NO_SIGNAL'})

    def test_t36_legacy_fields_are_derived_consistently(self):
        for item in self.result:
            action = item['skill_decision']['action']
            self.assertEqual(item['triggered'], action == 'BUY')
            if action != 'BUY':
                self.assertNotIn('(BUY)', str(item.get('signal')))

    def test_t37_t38_hanshu_missing_ma_is_no_signal_and_not_consumed(self):
        item = self.by_pid['P-HANSHU']
        self.assertEqual(item['skill_decision']['action'], 'NO_SIGNAL')
        self.assertIn('MA20_CONFIRMATION_UNAVAILABLE',
                      item['skill_decision']['skill_rules_failed'])
        self.assertEqual(item['skill_decision']['position_plan']['target_ratio'], 0.0)

    def test_t39_t40_cool_missing_structure_is_no_signal(self):
        item = self.by_pid['P-COOL']
        self.assertEqual(item['skill_decision']['action'], 'NO_SIGNAL')
        self.assertIn('INTRADAY_STRUCTURE_UNAVAILABLE',
                      item['skill_decision']['skill_rules_failed'])

    def test_t41_t42_valid_ywt_and_rule_evidence_can_buy(self):
        self.assertEqual(self.by_pid['P-YINWENTAO']['skill_decision']['action'], 'BUY')
        self.assertEqual(self.by_pid['P-RULE-QUANT']['skill_decision']['action'], 'BUY')

    def test_t43_risk_sentinel_has_no_trade_action(self):
        action = self.by_pid['P-RISK-SENTINEL']['skill_decision']['action']
        self.assertIn(action, {'HOLD', 'NO_SIGNAL'})
        self.assertFalse(self.by_pid['P-RISK-SENTINEL']['triggered'])

    def test_t44_t46_run_simulation_uses_canonical_no_signal(self):
        account = _account()
        pick = dict(self.by_pid['P-HANSHU'])
        pick['triggered'] = True
        pick['signal'] = 'BUY'
        pick['skill_decision'] = dict(pick['skill_decision'])
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data_path = root / 'data.json'
            data_path.write_text(json.dumps({'blogger_picks': [pick]}), encoding='utf-8')
            with patch.object(sim, 'TRADES_DIR', str(root / 'trades')), \
                 patch.object(sim, 'STATE_PATH', str(root / 'state.json')), \
                 patch.object(sim, 'DATA_PATH', str(data_path)), \
                 patch.object(sim, 'load_accounts_from_ledgers',
                              return_value={'hanshu': account}), \
                 patch.object(sim, 'fetch_prices',
                              return_value={'sh600000': {'price': 10.0}}), \
                 patch.object(sim, 'execute_buy', wraps=sim.execute_buy) as buy:
                import auto_update_hotspots
                with patch.object(auto_update_hotspots, 'embed_data_into_html'):
                    sim.run_simulation(allow_trading=True)
            self.assertFalse(buy.called)
            self.assertEqual(account['positions'], [])

    def test_t45_t46_execute_intraday_uses_canonical_no_signal(self):
        account = _account()
        pick = dict(self.by_pid['P-HANSHU'])
        pick['triggered'] = True
        pick['signal'] = 'BUY'
        with tempfile.TemporaryDirectory() as td:
            data_path = Path(td) / 'data.json'
            data_path.write_text(json.dumps({'blogger_picks': [pick]}), encoding='utf-8')
            with patch.object(sim, 'load_accounts_from_ledgers',
                              return_value={'hanshu': account}), \
                 patch.object(sim, 'fetch_prices', return_value={}), \
                 patch.object(sim, 'DATA_PATH', str(data_path)), \
                 patch.object(sim, '_acquire_trade_lock', return_value=object()), \
                 patch.object(sim, '_load_intraday_executed', return_value={}), \
                 patch.object(sim, '_load_position_highs', return_value={}), \
                 patch.object(sim, '_save_intraday_executed'), \
                 patch.object(sim, '_save_position_highs'), \
                 patch.object(sim, 'run_simulation'), \
                 patch.object(sim, '_execution_local_timestamp', return_value=datetime.datetime(2026, 9, 21, 10, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=8)))), \
                 patch.object(sim, 'execute_buy', wraps=sim.execute_buy) as buy:
                result = sim.execute_intraday(force=True)
            self.assertIn('成交0笔', result)
            self.assertFalse(buy.called)

    def test_t47_t50_buy_contract_has_ratio_plans_and_structured_evidence(self):
        for pid in ('P-YINWENTAO', 'P-RULE-QUANT'):
            decision = self.by_pid[pid]['skill_decision']
            self.assertIsInstance(decision['candidate_evidence'], dict)
            self.assertIsNotNone(decision['position_plan'])
            self.assertIsNotNone(decision['exit_plan'])
            self.assertGreater(decision['position_plan']['target_ratio'], 0)
            self.assertIn('source', decision['data_freshness'])
            self.assertTrue(decision['evidence_refs'])

    def test_t48_current_hold_season_safety_still_blocks_canonical_buy(self):
        result = deterministic_execution_safety(
            action='BUY', account={
                'cash': 100000, 'new_entry_disabled': True,
                'execution_state_safe': False, 'trading_enabled': False,
            }, shares=100, price=10, new_entry_disabled=True,
            execution_state_safe=False, trading_enabled=False,
        )
        self.assertEqual(result['verdict'], 'BLOCK')


if __name__ == '__main__':
    unittest.main()
