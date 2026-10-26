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


def _quote_response():
    values = [''] * 47
    values[1] = '芯片科技'
    values[2] = '600000'
    values[3] = '10.00'
    values[6] = '1000000'
    values[32] = '5.0'
    values[37] = '500000'
    values[38] = '8.0'
    values[39] = '20.0'
    values[45] = '500.0'
    return 'v_sh600000="' + '~'.join(values) + '~";\n'


class G25Fix1ProductionIntegrationTests(unittest.TestCase):
    def test_t18_to_t25_scan_output_is_evidence_bounded_and_canonical(self):
        with patch.object(picks, 'load_stock_universe',
                          return_value={'stocks': [{'symbol': 'sh600000'}]}), \
             patch.object(picks, 'fetch_quotes_batch', return_value=_quote_response()):
            result = picks.scan_and_score_all(persist=False)
        self.assertEqual(len(result), 6)
        expected = {
            'P-RULE-QUANT', 'P-HANSHU', 'P-YINWENTAO',
            'P-LIAO-XIAOAI', 'P-COOL', 'P-RISK-SENTINEL',
        }
        self.assertEqual({item['participant_id'] for item in result}, expected)
        self.assertEqual(result[0]['skill_type'], 'RULE_BASED_SKILL')
        self.assertEqual(result[-1]['skill_type'], 'RISK_SENTINEL')
        text = json.dumps(result, ensure_ascii=False)
        forbidden = (
            '20日新高突破', '均线呈多头排列', '主力与机构大资金坚决买入',
            '大资金流入', '早盘下探洗盘', '假跌破确认', '长阳吞没',
            '流动性Sweep', '无限子弹', '主力高位派发',
        )
        for phrase in forbidden:
            self.assertNotIn(phrase, text)
        self.assertIn('MA5/MA20', text)
        self.assertIn('机构资金流', text)

    def test_t26_to_t29_production_buy_safety_blocks_before_mutation(self):
        account = {
            'cash': 100000.0,
            'positions': [],
            'new_entry_disabled': True,
            'execution_state_safe': False,
            'trading_enabled': False,
            'real_trading': False,
        }
        before = json.dumps(account, sort_keys=True)
        result = sim.execute_buy(account, 'hanshu', 'sh600000', '测试', '600000',
                                 10.0, 0.25)
        self.assertIsNone(result)
        self.assertEqual(before, json.dumps(account, sort_keys=True))
        verdict = deterministic_execution_safety(
            action='BUY', account=account, positions=[], shares=100, price=10.0,
            new_entry_disabled=True, execution_state_safe=False,
            trading_enabled=False,
        )
        self.assertEqual(verdict['verdict'], 'BLOCK')
        self.assertIn('NEW_ENTRY_DISABLED', verdict['hard_blocks'])
        self.assertIn('EXECUTION_STATE_UNSAFE', verdict['hard_blocks'])
        self.assertIn('TRADING_DISABLED', verdict['hard_blocks'])

    def test_t30_to_t32_soft_exception_initializes_gate_and_still_blocks(self):
        account = {
            'blogger_id': 'hanshu',
            'blogger_name': '韩叔',
            'cash': 100000.0,
            'positions': [],
            'trade_ids': [],
            'total_pnl': 0.0,
            'new_entry_disabled': True,
            'execution_state_safe': False,
            'trading_enabled': False,
            'real_trading': False,
        }
        with tempfile.TemporaryDirectory() as td:
            data_path = Path(td) / 'data.json'
            data_path.write_text(json.dumps({'blogger_picks': [{
                'blogger_id': 'hanshu', 'triggered': True,
                'stock_code': '600000', 'stock_name': '测试',
                'current_price': 10.0, 'signal': 'BUY',
            }]}), encoding='utf-8')
            patches = [
                patch.object(sim, 'load_accounts_from_ledgers', return_value={'hanshu': account}),
                patch.object(sim, 'fetch_prices', return_value={}),
                patch.object(sim, 'DATA_PATH', str(data_path)),
                patch.object(sim, '_acquire_trade_lock', return_value=object()),
                patch.object(sim, '_load_intraday_executed', return_value={}),
                patch.object(sim, '_load_position_highs', return_value={}),
                patch.object(sim, '_save_intraday_executed'),
                patch.object(sim, '_save_position_highs'),
                patch.object(sim, 'record_trade_feed'),
                patch.object(sim, 'run_simulation', return_value={'accounts': {}}),
                patch('analysis.decision_gate.evaluate_decision',
                      side_effect=RuntimeError('soft context unavailable')),
            ]
            with patches[0]:
                with patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9], patches[10], \
                     patch.object(sim, '_execution_local_timestamp', return_value=datetime.datetime(2026, 9, 21, 10, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=8)) )):
                    with patch.object(sim, 'execute_buy', wraps=sim.execute_buy) as buy:
                        result = sim.execute_intraday(force=True)
            self.assertIn('成交0笔', result)
            self.assertFalse(buy.called)
            self.assertEqual(account['positions'], [])

    def test_t33_skill_ratio_is_not_rewritten_by_soft_context(self):
        account = {
            'cash': 100000.0, 'positions': [],
            'new_entry_disabled': False, 'execution_state_safe': True,
            'trading_enabled': True, 'real_trading': False,
        }
        with patch('analysis.decision_gate._regime', return_value='risk_off'):
            from analysis.decision_gate import evaluate_decision
            context = evaluate_decision('sh600000', account=account)
        self.assertEqual(context['ratio_multiplier'], 1.0)
        self.assertEqual(0.30, sim.BLOGGER_CONFIGS['yintwentao']['buy_ratio'])

    def test_t26_run_simulation_checks_safety_before_execute_buy(self):
        account = {
            'blogger_id': 'hanshu', 'blogger_name': '韩叔',
            'cash': 100000.0, 'positions': [], 'trade_ids': [], 'total_pnl': 0.0,
            'new_entry_disabled': True, 'execution_state_safe': False,
            'trading_enabled': False, 'real_trading': False,
        }
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            data_path = td_path / 'data.json'
            data_path.write_text(json.dumps({'blogger_picks': [{
                'blogger_id': 'hanshu', 'triggered': True, 'stock_code': '600000',
                'stock_name': '测试', 'current_price': 10.0, 'signal': 'BUY',
            }]}), encoding='utf-8')
            with patch.object(sim, 'TRADES_DIR', str(td_path / 'trades')), \
                 patch.object(sim, 'STATE_PATH', str(td_path / 'simulation_state.json')), \
                 patch.object(sim, 'DATA_PATH', str(data_path)), \
                 patch.object(sim, 'load_accounts_from_ledgers', return_value={'hanshu': account}), \
                 patch.object(sim, 'fetch_prices', return_value={'sh600000': {'price': 10.0}}), \
                 patch.object(sim, 'record_trade_feed'), \
                 patch.object(sim, 'execute_buy', wraps=sim.execute_buy) as buy:
                import auto_update_hotspots
                with patch.object(auto_update_hotspots, 'embed_data_into_html'):
                    sim.run_simulation(allow_trading=True)
            self.assertFalse(buy.called)
            self.assertEqual(account['positions'], [])


if __name__ == '__main__':
    unittest.main()
