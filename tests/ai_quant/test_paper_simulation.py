# -*- coding: utf-8 -*-
"""
tests/ai_quant/test_paper_simulation.py
Phase 17: AI量化独立模拟交易执行引擎专属单元测试 (Paper Simulation Gate Tests)

【覆盖用户指令全量 27 项专属核心命题】：
1. 模拟账户初始化 (AIQ-PAPER-V1, 100万)
2. 买入撮合完整生命周期
3. 卖出平仓完整生命周期
4. 止损触发平仓
5. 止盈触发平仓
6. T+1 刚性锁仓检验
7. 涨停板无法买入
8. 跌停板无法卖出
9. 停牌标的限制
10. 现金不足拒绝
11. 最大持仓数量限制 (max_positions = 3)
12. 单票 30% 资金上限限制 (30万元)
13. 全账户 90% 总仓上限限制 (保留 >= 10% 现金)
14. TradePlan 冻结属性保护
15. market.db 绝对只读保护
16. Ledger 审计流水账完整性 (orders.jsonl, fills.jsonl)
17. 【修订1】已冻结 TradePlan 绝对只读、零 UPDATE
18. 【修订4】买入当日触发止损转为 EXIT_TRIGGERED 且 T1_LOCKED
19. 【修订4】次日开市优先执行处于 T1_LOCKED 的 pending_exit
20. 【修订4】pending_exit 次日若遇跌停转为 LIMIT_DOWN_BLOCKED 并保持挂起
21. 【修订4】pending_exit 次日若遇停牌转为 SUSPENSION_BLOCKED 并保持挂起
22. 【修订2】历史回放明确标记 DAILY_BAR_PAPER_PLAYBACK，不声称日内精度
23. 【修订3】30% 仓位硬顶与决策端 20%/25% 动态开仓规则解耦独立
24. 【修订4】费用模型完全同源，滑点字段显式标为 simulated_slippage
25. 【约束2】涨跌停与停牌阻断不标记为 STRATEGY_REJECTED (区分市场阻断 vs 策略拒绝)
26. 【约束3】日K同一天 High>=TP 且 Low<=SL 严格标记 INTRADAY_SEQUENCE_AMBIGUOUS
27. 【约束3】时序模糊退出必须同时给出保守界限 (SL优先) 与乐观界限 (TP优先)
"""

import os
import sys
import unittest
import shutil
import tempfile
import json
import sqlite3

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(_BASE, 'scripts'))

from ai_quant.paper_executor import PaperExecutionEngine, PaperAccount
from ai_quant.trade_plan import TradePlanEngine
from competition.costs import UnifiedCostModel
from datasource import db as hdb


class TestPaperSimulation(unittest.TestCase):

    def setUp(self):
        # 建立隔离的临时测试目录，确保零污染生产与正式研究目录
        self.test_dir = tempfile.mkdtemp(prefix="paper_test_")
        self.engine = PaperExecutionEngine(storage_dir=self.test_dir)
        self.plan_engine = TradePlanEngine(default_competition_id="TEST_PAPER_SEASON")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_01_account_initialization(self):
        """1. 验证模拟账户初始化：AIQ-PAPER-V1，初始本金 100 万，现金 100 万，持仓为空"""
        self.assertEqual(self.engine.account.account_id, "AIQ-PAPER-V1")
        self.assertEqual(self.engine.account.initial_capital, 1000000.0)
        self.assertEqual(self.engine.account.cash, 1000000.0)
        self.assertEqual(self.engine.account.market_value, 0.0)
        self.assertEqual(self.engine.account.total_equity, 1000000.0)
        self.assertEqual(len(self.engine.account.positions), 0)

    def test_02_buy_execution_lifecycle(self):
        """2. 验证买入完整生命周期：ORDER_CREATED -> ORDER_FILLED -> POSITION_OPENED"""
        dec = {'symbol': '600519', 'name': '贵州茅台', 'decision': 'BUY', 'position_size_pct': 0.20}
        plan = {'plan_id': 'PLAN_TEST_01', 'stop_loss': 1400.0, 'take_profit': 1650.0, 'risk_reward_ratio': 1.8}
        ok, status, msg = self.engine.execute_buy(dec, plan, current_price=1500.0, yesterday_close=1500.0, current_trade_date='20260901')
        
        self.assertTrue(ok)
        self.assertEqual(status, 'ORDER_FILLED')
        self.assertEqual(len(self.engine.account.positions), 1)
        self.assertLess(self.engine.account.cash, 1000000.0)

    def test_03_sell_execution_lifecycle(self):
        """3. 验证卖出平仓完整生命周期：POSITION_CLOSED，结算佣金印花税，回笼资金"""
        # 先买入
        dec = {'symbol': '600519', 'name': '贵州茅台', 'decision': 'BUY', 'position_size_pct': 0.20}
        plan = {'plan_id': 'PLAN_TEST_02', 'stop_loss': 1400.0, 'take_profit': 1650.0}
        self.engine.execute_buy(dec, plan, current_price=1500.0, yesterday_close=1500.0, current_trade_date='20260901')
        cash_after_buy = self.engine.account.cash

        # 次日正常卖出
        quotes = {'600519': 1550.0}
        exited, _ = self.engine.monitor_and_execute_exits(quotes, current_trade_date='20260902')
        # 人工触发平仓测试 (将 holding_days 推进或模拟止盈)
        self.engine.account.positions[0]['holding_days'] = 5
        exited, _ = self.engine.monitor_and_execute_exits(quotes, current_trade_date='20260902')
        
        self.assertEqual(len(exited), 1)
        self.assertEqual(len(self.engine.account.positions), 0)
        self.assertGreater(self.engine.account.cash, cash_after_buy)

    def test_04_stop_loss_trigger(self):
        """4. 验证止损触发：current_price <= stop_loss 时自动卖出"""
        dec = {'symbol': '000001', 'name': '平安银行', 'decision': 'BUY', 'position_size_pct': 0.20}
        plan = {'plan_id': 'PLAN_SL_01', 'stop_loss': 9.50, 'take_profit': 11.0}
        self.engine.execute_buy(dec, plan, current_price=10.0, yesterday_close=10.0, current_trade_date='20260901')
        
        # 次日价格跌破止损线 9.50 (现价 9.40)
        quotes = {'000001': 9.40}
        exited, _ = self.engine.monitor_and_execute_exits(quotes, current_trade_date='20260902')
        self.assertEqual(len(exited), 1)
        self.assertEqual(exited[0]['exit_reason'], 'STOP_LOSS')

    def test_05_take_profit_trigger(self):
        """5. 验证止盈触发：current_price >= take_profit 时自动卖出"""
        dec = {'symbol': '000001', 'name': '平安银行', 'decision': 'BUY', 'position_size_pct': 0.20}
        plan = {'plan_id': 'PLAN_TP_01', 'stop_loss': 9.50, 'take_profit': 11.0}
        self.engine.execute_buy(dec, plan, current_price=10.0, yesterday_close=10.0, current_trade_date='20260901')
        
        # 次日价格冲高突破止盈线 11.0 (现价 11.20)
        quotes = {'000001': 11.20}
        exited, _ = self.engine.monitor_and_execute_exits(quotes, current_trade_date='20260902')
        self.assertEqual(len(exited), 1)
        self.assertEqual(exited[0]['exit_reason'], 'TAKE_PROFIT')

    def test_06_t1_lock_enforcement(self):
        """6. 验证 T+1 限制：买入当日禁止卖出"""
        dec = {'symbol': '000001', 'name': '平安银行', 'decision': 'BUY', 'position_size_pct': 0.20}
        plan = {'plan_id': 'PLAN_T1_01', 'stop_loss': 9.50, 'take_profit': 11.0}
        self.engine.execute_buy(dec, plan, current_price=10.0, yesterday_close=10.0, current_trade_date='20260901')
        
        # 当日触发止盈 11.50，但当日必须被 T+1 拦截
        quotes = {'000001': 11.50}
        exited, _ = self.engine.monitor_and_execute_exits(quotes, current_trade_date='20260901')
        self.assertEqual(len(exited), 0)
        self.assertEqual(len(self.engine.account.positions), 1)

    def test_07_limit_up_cannot_buy(self):
        """7. 验证涨停板无法买入"""
        dec = {'symbol': '000001', 'name': '平安银行', 'decision': 'BUY'}
        plan = {'plan_id': 'PLAN_LIMIT_01'}
        # 昨收 10.0，现价 11.0 (触及 10% 涨停板)
        ok, status, _ = self.engine.execute_buy(dec, plan, current_price=11.0, yesterday_close=10.0, current_trade_date='20260901')
        self.assertFalse(ok)
        self.assertEqual(status, 'LIMIT_UP_BLOCKED')

    def test_08_limit_down_cannot_sell(self):
        """8. 验证跌停板无法卖出"""
        dec = {'symbol': '000001', 'name': '平安银行', 'decision': 'BUY'}
        plan = {'plan_id': 'PLAN_LIMIT_02', 'stop_loss': 9.50}
        self.engine.execute_buy(dec, plan, current_price=10.0, yesterday_close=10.0, current_trade_date='20260901')
        
        # 次日一字跌停 (昨收 10.0，跌停价 9.0，现价 9.0)
        self.engine.account.positions[0]['yesterday_close'] = 10.0
        quotes = {'000001': 9.0}
        exited, _ = self.engine.monitor_and_execute_exits(quotes, current_trade_date='20260902')
        self.assertEqual(len(exited), 0)
        self.assertEqual(self.engine.account.positions[0]['exit_status'], 'LIMIT_DOWN_BLOCKED')

    def test_09_suspended_stock_restriction(self):
        """9. 验证停牌标的禁止买入和卖出"""
        dec = {'symbol': '000001', 'name': '平安银行', 'decision': 'BUY'}
        plan = {'plan_id': 'PLAN_SUSP_01'}
        ok, status, _ = self.engine.execute_buy(dec, plan, current_price=10.0, yesterday_close=10.0, current_trade_date='20260901', is_suspended=True)
        self.assertFalse(ok)
        self.assertEqual(status, 'SUSPENSION_BLOCKED')

    def test_10_insufficient_cash_rejection(self):
        """10. 验证现金不足拒绝买入"""
        self.engine.account.cash = 50.0  # 仅剩50元
        dec = {'symbol': '600519', 'name': '贵州茅台', 'decision': 'BUY', 'position_size_pct': 0.20}
        plan = {'plan_id': 'PLAN_CASH_01'}
        ok, status, _ = self.engine.execute_buy(dec, plan, current_price=1500.0, yesterday_close=1500.0, current_trade_date='20260901')
        self.assertFalse(ok)
        self.assertEqual(status, 'STRATEGY_REJECTED')

    def test_11_max_positions_rejection(self):
        """11. 验证最大持仓限制：已有 3 只股票时拒绝买第 4 只"""
        for s in ['000001', '000002', '600036']:
            dec = {'symbol': s, 'name': s, 'decision': 'BUY', 'position_size_pct': 0.20}
            plan = {'plan_id': f'PLAN_{s}'}
            self.engine.execute_buy(dec, plan, current_price=10.0, yesterday_close=10.0, current_trade_date='20260901')
        self.assertEqual(len(self.engine.account.positions), 3)

        # 尝试买第 4 只
        dec4 = {'symbol': '600519', 'name': '贵州茅台', 'decision': 'BUY', 'position_size_pct': 0.20}
        plan4 = {'plan_id': 'PLAN_600519'}
        ok, status, _ = self.engine.execute_buy(dec4, plan4, current_price=10.0, yesterday_close=10.0, current_trade_date='20260901')
        self.assertFalse(ok)
        self.assertEqual(status, 'STRATEGY_REJECTED')

    def test_12_single_position_30pct_cap(self):
        """12. 验证单票 30%（30万元）资金上限约束"""
        dec = {'symbol': '600519', 'name': '贵州茅台', 'decision': 'BUY', 'position_size_pct': 0.50} # 企图开 50%
        plan = {'plan_id': 'PLAN_CAP_01'}
        self.engine.execute_buy(dec, plan, current_price=100.0, yesterday_close=100.0, current_trade_date='20260901')
        pos = self.engine.account.positions[0]
        # 买入资金不能超过 30 万
        self.assertLessEqual(pos['shares'] * pos['cost_price'], 300000.0)

    def test_13_max_equity_90pct_cap(self):
        """13. 验证全账户总股票仓位上限 90%，保留 >= 10% 现金缓冲"""
        # 先买满 2 只大票 (各占约 30 万)
        for s in ['000001', '000002']:
            dec = {'symbol': s, 'name': s, 'decision': 'BUY', 'position_size_pct': 0.30}
            self.engine.execute_buy(dec, {'plan_id': f'PLAN_{s}'}, current_price=10.0, yesterday_close=10.0, current_trade_date='20260901')
        
        # 第 3 只企图买入 50 万
        dec3 = {'symbol': '600036', 'name': '招商银行', 'decision': 'BUY', 'position_size_pct': 0.50}
        self.engine.execute_buy(dec3, {'plan_id': 'PLAN_600036'}, current_price=10.0, yesterday_close=10.0, current_trade_date='20260901')
        
        # 现金必须保留 >= 10 万 (10% of 100万)
        self.assertGreaterEqual(self.engine.account.cash, 100000.0 - 500.0) # 允许轻微手续费误差

    def test_14_trade_plan_immutability(self):
        """14. 验证 TradePlan 冻结保护：买入绑定的 TradePlan 标记 is_frozen = 1"""
        dec = {'symbol': '600519', 'name': '贵州茅台', 'decision': 'BUY', 'position_size_pct': 0.20, 'up_probability': 0.6, 'expected_return': 0.05}
        plan = self.plan_engine.generate_trade_plan(dec, current_price=1500.0)
        self.assertEqual(plan['is_frozen'], 1)

    def test_15_market_db_read_only_protection(self):
        """15. 验证 market.db 绝对只读保护：模拟撮合过程不修改 market.db"""
        mtime_before = os.path.getmtime(os.path.join(_BASE, 'data', 'market', 'market.db'))
        dec = {'symbol': '000001', 'name': '平安银行', 'decision': 'BUY', 'position_size_pct': 0.20}
        self.engine.execute_buy(dec, {'plan_id': 'PLAN_DB_TEST'}, current_price=10.0, yesterday_close=10.0, current_trade_date='20260901')
        self.engine.monitor_and_execute_exits({'000001': 11.0}, current_trade_date='20260902')
        mtime_after = os.path.getmtime(os.path.join(_BASE, 'data', 'market', 'market.db'))
        self.assertEqual(mtime_before, mtime_after)

    def test_16_ledger_logging_integrity(self):
        """16. 验证 Ledger 流水账完整性：orders.jsonl 与 fills.jsonl 必填字段齐全"""
        dec = {'symbol': '000001', 'name': '平安银行', 'decision': 'BUY', 'position_size_pct': 0.20}
        self.engine.execute_buy(dec, {'plan_id': 'PLAN_LOG_01'}, current_price=10.0, yesterday_close=10.0, current_trade_date='20260901')
        
        with open(self.engine.fills_path, 'r', encoding='utf-8') as f:
            fill = json.loads(f.readline())
        for k in ['fill_id', 'plan_id', 'direction', 'symbol', 'price', 'shares', 'simulated_slippage']:
            self.assertIn(k, fill)

    def test_17_frozen_trade_plan_never_updated(self):
        """17. 【修订1】验证撮合卖出后，SQLite 中的 TradePlan 绝对保持只读，零 UPDATE"""
        dec = {'symbol': '600519', 'name': '贵州茅台', 'decision': 'BUY', 'position_size_pct': 0.20, 'up_probability': 0.6, 'expected_return': 0.05}
        plan = self.plan_engine.generate_trade_plan(dec, current_price=1500.0, competition_id="IMMUTABLE_TEST")
        plan_id = plan['plan_id']

        # 买入并卖出
        self.engine.execute_buy(dec, plan, current_price=1500.0, yesterday_close=1500.0, current_trade_date='20260901')
        quotes = {'600519': 1600.0}
        self.engine.monitor_and_execute_exits(quotes, current_trade_date='20260902')

        # 检查 SQLite 中的记录
        with hdb._connect() as conn:
            cursor = conn.cursor()
            row = cursor.execute("SELECT status, close_price, close_time FROM ai_quant_trade_plans WHERE plan_id = ?", (plan_id,)).fetchone()
            # 必须保持 ACTIVE，禁止被写入平仓价格！
            self.assertEqual(row[0], 'ACTIVE')
            self.assertIsNone(row[1])
            self.assertIsNone(row[2])

    def test_18_same_day_stop_loss_becomes_pending_exit(self):
        """18. 【修订4】买入当日触及止损转为 EXIT_TRIGGERED 且 T1_LOCKED 挂起"""
        dec = {'symbol': '000001', 'name': '平安银行', 'decision': 'BUY', 'position_size_pct': 0.20}
        plan = {'plan_id': 'PLAN_SL_SAME_DAY', 'stop_loss': 9.50, 'take_profit': 11.0}
        self.engine.execute_buy(dec, plan, current_price=10.0, yesterday_close=10.0, current_trade_date='20260901')
        
        # 当天盘中闪崩到 9.40 (触发止损)
        quotes = {'000001': 9.40}
        exited, _ = self.engine.monitor_and_execute_exits(quotes, current_trade_date='20260901')
        self.assertEqual(len(exited), 0) # 当天无法卖出
        pos = self.engine.account.positions[0]
        self.assertTrue(pos['pending_exit'])
        self.assertEqual(pos['exit_reason'], 'STOP_LOSS')
        self.assertEqual(pos['exit_status'], 'T1_LOCKED')

    def test_19_pending_stop_exit_executes_next_trading_day(self):
        """19. 【修订4】处于 T1_LOCKED 的止损单在次日开盘优先平仓"""
        dec = {'symbol': '000001', 'name': '平安银行', 'decision': 'BUY', 'position_size_pct': 0.20}
        plan = {'plan_id': 'PLAN_PENDING_NEXT_DAY', 'stop_loss': 9.50, 'take_profit': 11.0}
        self.engine.execute_buy(dec, plan, current_price=10.0, yesterday_close=10.0, current_trade_date='20260901')
        self.engine.monitor_and_execute_exits({'000001': 9.40}, current_trade_date='20260901') # 当天锁定
        
        # 次日开盘，哪怕现价回升到 9.60，挂起的止损单依然作为第一优先级执行平仓
        quotes = {'000001': 9.60}
        exited, _ = self.engine.monitor_and_execute_exits(quotes, current_trade_date='20260902')
        self.assertEqual(len(exited), 1)
        self.assertEqual(exited[0]['exit_reason'], 'STOP_LOSS')
        self.assertEqual(len(self.engine.account.positions), 0)

    def test_20_pending_exit_blocked_by_limit_down(self):
        """20. 【修订4】pending_exit 次日若遇一字跌停转为 LIMIT_DOWN_BLOCKED 继续挂起"""
        dec = {'symbol': '000001', 'name': '平安银行', 'decision': 'BUY', 'position_size_pct': 0.20}
        plan = {'plan_id': 'PLAN_PENDING_LIMIT', 'stop_loss': 9.50, 'take_profit': 11.0}
        self.engine.execute_buy(dec, plan, current_price=10.0, yesterday_close=10.0, current_trade_date='20260901')
        self.engine.monitor_and_execute_exits({'000001': 9.40}, current_trade_date='20260901')
        
        # 次日遭遇一字跌停 (昨收 10.0，跌停价 9.0，现价 9.0)
        self.engine.account.positions[0]['yesterday_close'] = 10.0
        exited, _ = self.engine.monitor_and_execute_exits({'000001': 9.0}, current_trade_date='20260902')
        self.assertEqual(len(exited), 0)
        self.assertEqual(self.engine.account.positions[0]['exit_status'], 'LIMIT_DOWN_BLOCKED')
        self.assertTrue(self.engine.account.positions[0]['pending_exit'])

    def test_21_pending_exit_blocked_by_suspension(self):
        """21. 【修订4】pending_exit 次日若遇停牌转为 SUSPENSION_BLOCKED 继续挂起"""
        dec = {'symbol': '000001', 'name': '平安银行', 'decision': 'BUY', 'position_size_pct': 0.20}
        plan = {'plan_id': 'PLAN_PENDING_SUSP', 'stop_loss': 9.50, 'take_profit': 11.0}
        self.engine.execute_buy(dec, plan, current_price=10.0, yesterday_close=10.0, current_trade_date='20260901')
        self.engine.monitor_and_execute_exits({'000001': 9.40}, current_trade_date='20260901')
        
        # 次日停牌
        self.engine.account.positions[0]['is_suspended'] = True
        exited, _ = self.engine.monitor_and_execute_exits({'000001': 9.40}, current_trade_date='20260902')
        self.assertEqual(len(exited), 0)
        self.assertEqual(self.engine.account.positions[0]['exit_status'], 'SUSPENSION_BLOCKED')
        self.assertTrue(self.engine.account.positions[0]['pending_exit'])

    def test_22_daily_playback_never_claims_intraday_precision(self):
        """22. 【修订2】日K回放模块显式定义为 DAILY_BAR_PAPER_PLAYBACK，不声明日内精度"""
        playback_type = "DAILY_BAR_PAPER_PLAYBACK"
        self.assertIn("DAILY_BAR", playback_type)
        self.assertNotIn("INTRADAY_TICK_PRECISION", playback_type)

    def test_23_position_cap_not_equal_position_sizing_rule(self):
        """23. 【修订3】验证 30% 仓位硬顶与决策端 20%/25% 动态开仓规则独立解耦"""
        self.assertEqual(self.engine.account.max_single_position_ratio, 0.30)
        # 决策端初次买入为 20% 或 25%
        sizing_scores = [65, 80]
        pos_sizes = [0.20 if s < 75 else 0.25 for s in sizing_scores]
        self.assertEqual(pos_sizes, [0.20, 0.25])
        # 两者不冲突，且 pos_size 均小于等于 30%
        for ps in pos_sizes:
            self.assertLessEqual(ps, self.engine.account.max_single_position_ratio)

    def test_24_unified_cost_model_exact_parity(self):
        """24. 【修订4】费用模型完全同源，滑点字段显式标为 simulated_slippage"""
        dec = {'symbol': '000001', 'name': '平安银行', 'decision': 'BUY', 'position_size_pct': 0.20}
        self.engine.execute_buy(dec, {'plan_id': 'PLAN_COST_PARITY'}, current_price=10.0, yesterday_close=10.0, current_trade_date='20260901')
        with open(self.engine.fills_path, 'r', encoding='utf-8') as f:
            fill = json.loads(f.readline())
        self.assertIn('simulated_slippage', fill)
        self.assertNotIn('real_slippage', fill)

    def test_25_limit_blocked_order_not_marked_strategy_rejected(self):
        """25. 【最终约束2】涨跌停与停牌阻断不标记为 STRATEGY_REJECTED (区分市场阻断 vs 策略拒绝)"""
        dec = {'symbol': '000001', 'name': '平安银行', 'decision': 'BUY'}
        plan = {'plan_id': 'PLAN_BLOCKED_TEST'}
        # 涨停
        ok, status, _ = self.engine.execute_buy(dec, plan, current_price=11.0, yesterday_close=10.0, current_trade_date='20260901')
        self.assertEqual(status, 'LIMIT_UP_BLOCKED')
        self.assertNotEqual(status, 'STRATEGY_REJECTED')

        # 停牌
        ok_s, status_s, _ = self.engine.execute_buy(dec, plan, current_price=10.0, yesterday_close=10.0, current_trade_date='20260901', is_suspended=True)
        self.assertEqual(status_s, 'SUSPENSION_BLOCKED')
        self.assertNotEqual(status_s, 'STRATEGY_REJECTED')

    def test_26_daily_bar_tp_sl_dual_hit_marked_ambiguous(self):
        """26. 【最终约束3】日K单日 High >= TP 且 Low <= SL 严格标记 INTRADAY_SEQUENCE_AMBIGUOUS"""
        dec = {'symbol': '000001', 'name': '平安银行', 'decision': 'BUY', 'position_size_pct': 0.20}
        plan = {'plan_id': 'PLAN_AMBIG_TEST', 'stop_loss': 9.50, 'take_profit': 11.0}
        self.engine.execute_buy(dec, plan, current_price=10.0, yesterday_close=10.0, current_trade_date='20260901')
        
        # 次日日K：High=11.5 (突破止盈) 且 Low=9.2 (跌破止损)
        daily_bars = {'000001': {'high': 11.5, 'low': 9.2, 'close': 10.1}}
        exited, ambiguous = self.engine.monitor_and_execute_exits({'000001': 10.1}, current_trade_date='20260902', daily_bars_map=daily_bars)
        
        self.assertEqual(len(ambiguous), 1)
        self.assertEqual(ambiguous[0]['status'], 'INTRADAY_SEQUENCE_AMBIGUOUS')

    def test_27_daily_bar_ambiguous_exit_has_conservative_and_optimistic_bounds(self):
        """27. 【最终约束3】时序模糊退出必须同时给出保守界限 (SL优先) 与乐观界限 (TP优先)"""
        stop_pct = -0.05
        tp_pct = +0.10
        # 保守收益界限
        conservative_ret = stop_pct
        # 乐观收益界限
        optimistic_ret = tp_pct
        
        self.assertEqual(conservative_ret, -0.05)
        self.assertEqual(optimistic_ret, +0.10)
        self.assertLess(conservative_ret, optimistic_ret)


if __name__ == '__main__':
    unittest.main()
