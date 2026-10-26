# -*- coding: utf-8 -*-
"""
tests/ai_quant/test_decision_plan.py
Phase 6-8 & Phase 16A: Top 5 排名、AI 独立决策与 Trade Plan 交易合同 Gate 单元测试
覆盖《指南》第 1.3、1.9、1.10、12、13、14 节及用户 Phase 16A 指令：
1. Top 5 动态排名：已持仓股票允许继续出现在 Top 5
2. Top 5 与 BUY 严格解耦：Top 5 存在不等于必须买入，支持 BUY = 0 绝对空仓
3. AI 独立决策：BUY / WAIT / NO_TRADE 逻辑边界与风控熔断
4. Trade Plan 交易计划：买入前一次性确定 Entry/SL/TP/仓位/周期，落库后标记 is_frozen = 1 永久冻结
5. 版本指纹：model/feature/label/decision 版本严格锁死
6. 【Phase 16A 专项测试】：
   A. 旧公式回归测试：证明旧公式 (expected_return / |predicted_mae|) 不再被调用阻断；
   B. 新公式数学测试：输入 mfe=0.10, mae=-0.05 -> ratio=2.0，BUY 允许通过；
   C. 边界测试：输入 mfe=0.055, mae=-0.05 -> ratio=1.1，临界允许通过；
   D. 失败测试：输入 mfe=0.05, mae=-0.06 -> ratio=0.833，必须 NO_TRADE；
   E. NaN / zero division 异常保护测试。
"""
import os
import sys
import unittest
import math

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_BASE, 'scripts'))

from ai_quant.ranking import Top5RankingEngine
from ai_quant.decision import AIQuantDecisionEngine
from ai_quant.trade_plan import TradePlanEngine


class TestDecisionAndPlan(unittest.TestCase):

    def setUp(self):
        self.ranking_engine = Top5RankingEngine()
        self.decision_engine = AIQuantDecisionEngine(max_positions=3, min_up_prob=0.50, min_right_score=60, min_pl_ratio=1.1)
        self.plan_engine = TradePlanEngine(default_competition_id="TEST_SEASON_001")

    def test_top5_allows_holding_stocks(self):
        """测试 Top 5 排名引擎：已持仓股票允许继续留在 Top 5 状态榜"""
        test_stocks = ['000001', '000002', '600519', '601318', '600036', '601899']
        top5 = self.ranking_engine.rank_market(as_of_date='20260904', candidate_symbols=test_stocks, top_k=5)
        self.assertEqual(len(top5), 5)
        top1_sym = top5[0]['symbol']

        mock_acc = {'cash': 50000.0, 'positions': [{'symbol': top1_sym, 'shares': 500, 'cost_price': 10.0}]}
        decs = self.decision_engine.evaluate_candidates(top5, current_account=mock_acc)
        self.assertEqual(len(decs), 5)
        top1_dec = decs[0]
        self.assertEqual(top1_dec['symbol'], top1_sym)
        self.assertEqual(top1_dec['decision'], 'WAIT')
        self.assertIn('已在持仓', top1_dec['decision_reason'])

    def test_decision_supports_absolute_empty_position(self):
        """测试 AI 决策引擎支持绝对空仓 (BUY = 0)"""
        bad_candidates = [
            {'symbol': '000001', 'name': '标的A', 'up_probability': 0.35, 'expected_return': -0.02, 'predicted_mae': -0.05, 'predicted_mfe': 0.02, 'right_side_score': 30},
            {'symbol': '000002', 'name': '标的B', 'up_probability': 0.42, 'expected_return': 0.01, 'predicted_mae': -0.04, 'predicted_mfe': 0.03, 'right_side_score': 45},
        ]
        decs = self.decision_engine.evaluate_candidates(bad_candidates)
        buy_count = sum(1 for d in decs if d['decision'] == 'BUY')
        self.assertEqual(buy_count, 0)
        for d in decs:
            self.assertEqual(d['decision'], 'NO_TRADE')

    def test_trade_plan_generation_and_freeze(self):
        """测试买入前生成完整 Trade Plan 且标记 is_frozen = 1 冻结"""
        buy_cand = {
            'decision': 'BUY',
            'symbol': '600519',
            'name': '贵州茅台',
            'up_probability': 0.68,
            'expected_return': 0.065,
            'predicted_mae': -0.028,
            'predicted_mfe': 0.085,
            'risk_reward_ratio': 3.0357,
            'right_side_score': 85,
            'position_size_pct': 0.25,
            'expected_holding_days': 4
        }
        current_p = 1350.0
        plan = self.plan_engine.generate_trade_plan(buy_cand, current_price=current_p)
        self.assertIsNotNone(plan)

        self.assertEqual(plan['symbol'], '600519')
        self.assertEqual(plan['entry_price'], 1350.0)
        self.assertEqual(plan['position_size'], 0.25)
        self.assertLess(plan['stop_loss'], 1350.0)
        self.assertGreater(plan['take_profit'], 1350.0)
        self.assertEqual(plan['expected_holding_days'], 4)
        self.assertEqual(plan['is_frozen'], 1)
        self.assertIn('risk_reward_ratio', plan)
        for ver_key in ['model_version', 'feature_version', 'label_version', 'decision_version']:
            self.assertIn(ver_key, plan)
            self.assertTrue(len(plan[ver_key]) > 0)

    # ==================== Phase 16A 专属数学与回归测试 ====================

    def test_16a_old_formula_regression(self):
        """A. 旧公式回归测试：证明旧公式 expected_return / |mae| 不再阻断高 MFE 标的"""
        # 标的特征：expected_return = 0.008 (仅0.8%), mae = -0.07, 但 mfe = 0.14
        # 若按旧公式：0.008 / 0.07 = 0.114 < 1.1 -> 必然被 NO_TRADE 误杀
        # 若按新公式：0.14 / 0.07 = 2.0 >= 1.1 -> 顺利放行 BUY
        cand = [{
            'symbol': '600150', 'name': '中国船舶',
            'up_probability': 0.55, 'expected_return': 0.008,
            'predicted_mae': -0.07, 'predicted_mfe': 0.14,
            'right_side_score': 80
        }]
        decs = self.decision_engine.evaluate_candidates(cand)
        self.assertEqual(decs[0]['decision'], 'BUY')
        self.assertEqual(decs[0]['risk_reward_ratio'], 2.0)
        self.assertIn('空间盈亏比2.0达标', decs[0]['decision_reason'])

    def test_16a_new_formula_math_pass(self):
        """B. 新公式数学测试：输入 mfe=0.10, mae=-0.05 -> ratio=2.0，BUY 允许"""
        cand = [{
            'symbol': '000001', 'name': '平安银行',
            'up_probability': 0.55, 'expected_return': 0.02,
            'predicted_mae': -0.05, 'predicted_mfe': 0.10,
            'right_side_score': 75
        }]
        decs = self.decision_engine.evaluate_candidates(cand)
        self.assertEqual(decs[0]['decision'], 'BUY')
        self.assertAlmostEqual(decs[0]['risk_reward_ratio'], 2.0, places=3)

    def test_16a_boundary_condition_pass(self):
        """C. 边界测试：mfe=0.055, mae=-0.05 -> ratio=1.1，临界允许通过"""
        cand = [{
            'symbol': '000002', 'name': '万科A',
            'up_probability': 0.52, 'expected_return': 0.015,
            'predicted_mae': -0.05, 'predicted_mfe': 0.055,
            'right_side_score': 65
        }]
        decs = self.decision_engine.evaluate_candidates(cand)
        self.assertEqual(decs[0]['decision'], 'BUY')
        self.assertAlmostEqual(decs[0]['risk_reward_ratio'], 1.1, places=3)

    def test_16a_failure_condition_no_trade(self):
        """D. 失败测试：mfe=0.05, mae=-0.06 -> ratio=0.833，必须 NO_TRADE"""
        cand = [{
            'symbol': '601318', 'name': '中国平安',
            'up_probability': 0.55, 'expected_return': 0.02,
            'predicted_mae': -0.06, 'predicted_mfe': 0.05,
            'right_side_score': 70
        }]
        decs = self.decision_engine.evaluate_candidates(cand)
        self.assertEqual(decs[0]['decision'], 'NO_TRADE')
        self.assertAlmostEqual(decs[0]['risk_reward_ratio'], 0.8333, places=3)
        self.assertIn('低于风控底线 (1.1)', decs[0]['decision_reason'])

    def test_16a_nan_and_zero_division_protection(self):
        """E. NaN / zero division 防御测试：遇到 0 或 NaN 安全返回 0.0 并 NO_TRADE，绝不抛崩"""
        cands = [
            {'symbol': 'T1', 'name': 'ZeroMAE', 'up_probability': 0.6, 'predicted_mae': 0.0, 'predicted_mfe': 0.1, 'right_side_score': 80},
            {'symbol': 'T2', 'name': 'NaNMAE', 'up_probability': 0.6, 'predicted_mae': float('nan'), 'predicted_mfe': 0.1, 'right_side_score': 80},
            {'symbol': 'T3', 'name': 'NaNMFE', 'up_probability': 0.6, 'predicted_mae': -0.05, 'predicted_mfe': float('nan'), 'right_side_score': 80},
        ]
        decs = self.decision_engine.evaluate_candidates(cands)
        for d in decs:
            self.assertEqual(d['decision'], 'NO_TRADE')
            self.assertEqual(d['risk_reward_ratio'], 0.0)


if __name__ == '__main__':
    unittest.main()
