# -*- coding: utf-8 -*-
"""
tests/competition/test_arena.py
Phase 9-11: 统一 A 股规则、模拟撮合与七选手竞技场赛季系统 Gate 单元测试
覆盖《指南》第 15, 16, 17 节与 Gate 6 & Gate 7 验收要求：
1. 7 位选手物理隔离与初始公平性 (统一10万虚拟现金)
2. 统一裁判规则与 A 股约束：强制 T+1，一字板不可买卖，买入整手截断
3. 真实费率扣除：买卖佣金 (最低5元)、印花税 (0.05%) 与双边滑点 (0.1%)
4. 不限制交易次数
5. 比赛第一指标验证：严格以账户净收益率 Net PnL % 排序确定总冠军
6. 赛季开账与结算报告归档
"""
import os
import sys
import unittest

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_BASE, 'scripts'))

from competition.rules import AShareRules
from competition.costs import UnifiedCostModel
from competition.fills import SimulationFillEngine
from competition.accounts import ArenaAccountsManager, INITIAL_CAPITAL, SEVEN_PLAYERS
from competition.ranking import ArenaRankingEngine
from competition.settlement import SeasonSettlementEngine


class TestCompetitionArena(unittest.TestCase):

    def setUp(self):
        self.mgr = ArenaAccountsManager(season_id="UNITTEST_S1")
        self.mgr.reset_season("UNITTEST_S1")
        self.fill_engine = SimulationFillEngine()
        self.ranking_engine = ArenaRankingEngine(self.mgr)
        self.settle_engine = SeasonSettlementEngine(self.mgr)

    def test_seven_players_isolation_and_fairness(self):
        """Gate 7: 测试 7 个选手独立隔离且初始资金完全公平"""
        accs = self.mgr.accounts
        self.assertEqual(len(accs), 7)
        self.assertIn('ai-quant', accs)
        self.assertIn('hanshu', accs)
        self.assertIn('cool', accs)
        
        # 验证初始资金完全一致 (10万元现金，0持仓)
        for pid, a in accs.items():
            self.assertEqual(a['cash'], INITIAL_CAPITAL)
            self.assertEqual(a['total_equity'], INITIAL_CAPITAL)
            self.assertEqual(a['net_pnl'], 0.0)
            self.assertEqual(len(a['positions']), 0)

    def test_ashare_rules_and_t1(self):
        """Gate 6: 测试 A 股统一裁判规则引擎 (T+1与涨停)"""
        # 1. 涨停无法买入
        can_b, reason_b = AShareRules.can_buy(11.0, 10.0, open_price=11.0, low_price=11.0)
        self.assertFalse(can_b)
        self.assertIn("涨停", reason_b)

        # 2. 当天买入不可卖 (T+1 刚性)
        pos_t0 = {'symbol': '000001', 'shares': 500, 'buy_date': '20260905'}
        can_s, _, reason_s = AShareRules.validate_t1_sell(pos_t0, current_trade_date='20260905')
        self.assertFalse(can_s)
        self.assertIn("T+1", reason_s)

        # 3. 次日允许卖出
        can_s_next, sh, _ = AShareRules.validate_t1_sell(pos_t0, current_trade_date='20260906')
        self.assertTrue(can_s_next)
        self.assertEqual(sh, 500)

    def test_costs_and_simulation_fills(self):
        """Gate 6: 测试真实费用模型与模拟撮合成交"""
        # 买入 10元 1000股：需考虑 0.1% 滑点与万三佣金 (最低5元)
        ok, fill, msg = self.fill_engine.try_fill_buy('000001', '平安银行', 10.0, budget=20000.0, yesterday_close=9.9)
        self.assertTrue(ok)
        self.assertEqual(fill['shares'], 1900) # 预算内整手截断
        # 卖出滑点与印花税验证
        pos = {'symbol': '000001', 'name': '平安银行', 'shares': 1000, 'cost_price': 10.01, 'fee': 5.0, 'buy_date': '20260904'}
        ok_sell, fill_sell, msg_s = self.fill_engine.try_fill_sell(pos, raw_price=11.0, yesterday_close=10.5, current_date='20260905')
        self.assertTrue(ok_sell)
        self.assertGreater(fill_sell['net_proceeds'], 0)
        self.assertGreater(fill_sell['stamp_tax'], 0) # 必须产生印花税
        self.assertEqual(fill_sell['commission'], max(5.0, round(fill_sell['gross_amount'] * 0.0003, 2)))

    def test_ranking_by_net_pnl_pct(self):
        """Gate 7: 验证比赛唯一第一排名指标为账户净收益率 Net PnL %"""
        # 人为模拟三位选手的交易结果
        # AI_QUANT 赚 5000 元
        self.mgr.accounts['ai-quant']['cash'] += 5000.0
        self.mgr.accounts['ai-quant']['total_equity'] += 5000.0
        self.mgr.accounts['ai-quant']['net_pnl'] = 5000.0
        self.mgr.accounts['ai-quant']['net_pnl_pct'] = 5.0
        self.mgr.accounts['ai-quant']['trade_count'] = 2

        # 酷儿赚 8000 元
        self.mgr.accounts['cool']['cash'] += 8000.0
        self.mgr.accounts['cool']['total_equity'] += 8000.0
        self.mgr.accounts['cool']['net_pnl'] = 8000.0
        self.mgr.accounts['cool']['net_pnl_pct'] = 8.0
        self.mgr.accounts['cool']['trade_count'] = 15 # 交易次数多不影响排名

        # 殷文涛亏 2000 元
        self.mgr.accounts['yintwentao']['cash'] -= 2000.0
        self.mgr.accounts['yintwentao']['total_equity'] -= 2000.0
        self.mgr.accounts['yintwentao']['net_pnl'] = -2000.0
        self.mgr.accounts['yintwentao']['net_pnl_pct'] = -2.0
        self.mgr.accounts['yintwentao']['trade_count'] = 1

        self.mgr.save()

        # 生成排行榜
        board = self.ranking_engine.get_current_leaderboard()
        lb = board['leaderboard']

        # 冠军必须是酷儿 (+8.0%)
        self.assertEqual(lb[0]['player_id'], 'cool')
        self.assertEqual(lb[0]['rank'], 1)
        self.assertEqual(lb[0]['net_pnl_pct'], 8.0)

        # 亚军必须是 AI_QUANT (+5.0%)
        self.assertEqual(lb[1]['player_id'], 'ai-quant')
        self.assertEqual(lb[1]['rank'], 2)
        self.assertEqual(lb[1]['net_pnl_pct'], 5.0)

        # 垫底必须是殷文涛 (-2.0%)
        self.assertEqual(lb[-1]['player_id'], 'yintwentao')
        self.assertEqual(lb[-1]['rank'], 7)

    def test_season_settlement_report(self):
        """Gate 7: 测试赛季结算与正式战报 Markdown 生成"""
        res = self.settle_engine.settle_season(season_id="UNITTEST_SETTLE")
        self.assertTrue(os.path.exists(res['report_path']))
        content = open(res['report_path'], encoding='utf-8').read()
        self.assertIn("七选手量化竞技场 · 赛季官方战报", content)
        self.assertIn("最终完整排行榜", content)
        self.assertIn("AI量化", content)


if __name__ == '__main__':
    unittest.main()
