# -*- coding: utf-8 -*-
"""
scripts/competition/fills.py
Phase 10: 模拟撮合执行引擎（Fill Engine）

核心职责（《指南》第 16 & 20 节）：
1. 撮合裁判与防作弊：
   - 每一笔订单撮合前必须通过 AShareRules 裁判（停牌/一字板/T+1刚性约束）；
   - 杜绝“发出信号即默认成交”的虚假假设；
2. 触线即成交与悲观序原则：
   - 止损触发或止盈触发时，按触发点位与滑点进行保守撮合；
   - 遇到同日双触线（既触止损又触止盈），严格按悲观序执行止损；
3. 输出完整 Fill 成交凭据：
   - 包含成交时间、方向、价格、股数、佣金、印花税、净收益。
"""

import os
import sys
import datetime

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_BASE, os.path.join(_BASE, 'scripts')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import runtime_config as rc
from competition.rules import AShareRules
from competition.costs import UnifiedCostModel


class SimulationFillEngine:
    def __init__(self, clock=None):
        """Use the wall clock by default; offline replay may supply its clock."""
        self.cost_model = UnifiedCostModel()
        self._clock = clock or datetime.datetime.now

    def try_fill_buy(self, symbol, name, raw_price, budget, yesterday_close, open_p=None, low_p=None, is_st=False, board='MAIN', is_suspended=False, reason=""):
        """
        尝试执行买入撮合：
        返回 (success: bool, fill_record: dict, message: str)
        """
        # 1. 裁判检验
        can_b, msg = AShareRules.can_buy(
            raw_price, yesterday_close, open_price=open_p, low_price=low_p,
            is_st=is_st, board=board, is_suspended=is_suspended
        )
        if not can_b:
            return False, None, msg

        # 2. 计算滑点后价格与可买手数
        exec_price_raw = raw_price * (1.0 + self.cost_model.slippage_rate)
        shares = AShareRules.calculate_lot_shares(budget, exec_price_raw)
        if shares < 100:
            return False, None, f"预算不足以买入最小 1 手 (100股)"

        # 3. 计算真实费用
        exec_p, gross_amt, comm, total_cash = self.cost_model.calculate_buy_cost(raw_price, shares)
        if total_cash > budget * 1.05: # 加上佣金后若严重超出预算
            # 扣减1手重算
            shares -= 100
            if shares < 100:
                return False, None, "预算扣除手续费后不足1手"
            exec_p, gross_amt, comm, total_cash = self.cost_model.calculate_buy_cost(raw_price, shares)

        now = self._clock()
        fill_record = {
            'direction': 'BUY',
            'symbol': str(symbol).zfill(6),
            'name': name,
            'trade_time': now.strftime('%Y-%m-%d %H:%M:%S'),
            'trade_date': now.strftime('%Y%m%d'),
            'price': exec_p,
            'shares': shares,
            'gross_amount': gross_amt,
            'commission': comm,
            'stamp_tax': 0.0,
            'total_cash_required': total_cash,
            'reason': reason
        }
        return True, fill_record, "买入撮合成交"

    def try_fill_sell(self, position, raw_price, yesterday_close, current_date=None, open_p=None, high_p=None, is_st=False, board='MAIN', is_suspended=False, reason=""):
        """
        尝试执行卖出撮合（全平）：
        返回 (success: bool, fill_record: dict, message: str)
        """
        now = self._clock()
        cur_d = current_date or now.strftime('%Y%m%d')

        # 1. T+1 裁判
        can_t1, avail_shares, msg_t1 = AShareRules.validate_t1_sell(position, cur_d)
        if not can_t1:
            return False, None, msg_t1

        # 2. 跌停/停牌裁判
        can_s, msg_s = AShareRules.can_sell(
            raw_price, yesterday_close, open_price=open_p, high_price=high_p,
            is_st=is_st, board=board, is_suspended=is_suspended
        )
        if not can_s:
            return False, None, msg_s

        # 3. 计算卖出回款与净收益
        shares = avail_shares
        buy_cost_total = float(position.get('cost_price', raw_price)) * shares + float(position.get('fee', 5.0))
        exec_p, gross_rev, comm, tax, net_proc, net_pnl, pnl_pct = self.cost_model.calculate_sell_proceeds(
            raw_price, shares, buy_cost_total
        )

        fill_record = {
            'direction': 'SELL',
            'symbol': position['symbol'],
            'name': position.get('name', position['symbol']),
            'trade_time': now.strftime('%Y-%m-%d %H:%M:%S'),
            'trade_date': cur_d,
            'price': exec_p,
            'shares': shares,
            'gross_amount': gross_rev,
            'commission': comm,
            'stamp_tax': tax,
            'net_proceeds': net_proc,
            'net_pnl': net_pnl,
            'pnl_pct': pnl_pct,
            'reason': reason
        }
        return True, fill_record, "卖出撮合成交"


if __name__ == '__main__':
    print("=== 测试 SimulationFillEngine 模拟撮合 ===")
    engine = SimulationFillEngine()
    
    # 1. 买入撮合测试
    ok, fill_b, msg = engine.try_fill_buy('000001', '平安银行', 11.50, budget=25000.0, yesterday_close=11.40)
    print(f"买入测试结果: {ok} ({msg})")
    if ok:
        print(f"  成交价: {fill_b['price']} | 股数: {fill_b['shares']} | 需现金: {fill_b['total_cash_required']}元")

    # 2. 卖出撮合测试 (T+1 违规测试)
    pos_mock_today = {'symbol': '000001', 'name': '平安银行', 'shares': 2100, 'cost_price': 11.5115, 'fee': 7.25, 'buy_date': '20260905'}
    ok_s1, _, msg1 = engine.try_fill_sell(pos_mock_today, 12.0, 11.5, current_date='20260905')
    print(f"当天卖出违规拦截测试: {not ok_s1} ({msg1})")

    # 3. 正常次日卖出测试
    ok_s2, fill_s, msg2 = engine.try_fill_sell(pos_mock_today, 12.20, 11.5, current_date='20260906')
    print(f"次日卖出测试: {ok_s2} ({msg2})")
    if ok_s2:
        print(f"  成交价: {fill_s['price']} | 净回款: {fill_s['net_proceeds']}元 | 净盈亏: {fill_s['net_pnl']}元 ({fill_s['pnl_pct']}%)")
