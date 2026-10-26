# -*- coding: utf-8 -*-
"""
scripts/competition/costs.py
Phase 10: 七选手统一真实费用模型（Unified Transaction Costs）

核心职责（《指南》第 16 节）：
七位选手统一执行真实 A 股交易费率，参数统一从配置读取：
1. 佣金 (Commission): 万分之三，单笔最低 5 元
2. 印花税 (Stamp Duty): 卖出单边收取万分之五 (0.05%)
3. 双边滑点 (Slippage): 买入 +0.1%，卖出 -0.1%
4. 净收益统一公式：
   Net PnL = Gross Revenue - Commission - Stamp Duty - Slippage
"""

import os
import sys
import json

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_BASE, os.path.join(_BASE, 'scripts')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import runtime_config as rc

CONFIG_PATH = os.path.join(_BASE, 'config', 'system_params.json')


class UnifiedCostModel:
    def __init__(self):
        # 默认标准费率
        self.commission_rate = 0.0003
        self.min_commission = 5.0
        self.stamp_duty_rate = 0.0005
        self.slippage_rate = 0.001
        self._load_from_config()

    def _load_from_config(self):
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                    d = json.load(f)
                c_params = d.get('categories', {}).get('simulation_and_risk', {}).get('params', {})
                if c_params:
                    self.commission_rate = float(c_params.get('commission_rate', self.commission_rate))
                    self.min_commission = float(c_params.get('min_commission', self.min_commission))
                    self.stamp_duty_rate = float(c_params.get('stamp_duty_rate', self.stamp_duty_rate))
                    self.slippage_rate = float(c_params.get('slippage_rate', self.slippage_rate))
            except Exception:
                pass

    def calculate_buy_cost(self, raw_price, shares):
        """
        计算买入成交的滑点后价格、成交金额与佣金。
        返回 (exec_price, gross_amount, commission, total_cash_required)
        """
        exec_price = round(float(raw_price) * (1.0 + self.slippage_rate), 4)
        gross_amount = round(exec_price * int(shares), 2)
        commission = round(max(self.min_commission, gross_amount * self.commission_rate), 2)
        total_cash = round(gross_amount + commission, 2)
        return exec_price, gross_amount, commission, total_cash

    def calculate_sell_proceeds(self, raw_price, shares, buy_cost_total):
        """
        计算卖出成交的滑点后价格、净回款、佣金、印花税与最终净收益。
        返回 (exec_price, gross_revenue, commission, stamp_duty, net_proceeds, net_pnl, pnl_pct)
        """
        exec_price = round(float(raw_price) * (1.0 - self.slippage_rate), 4)
        gross_revenue = round(exec_price * int(shares), 2)
        commission = round(max(self.min_commission, gross_revenue * self.commission_rate), 2)
        stamp_duty = round(gross_revenue * self.stamp_duty_rate, 2)
        
        net_proceeds = round(gross_revenue - commission - stamp_duty, 2)
        net_pnl = round(net_proceeds - float(buy_cost_total), 2)
        pnl_pct = round((net_pnl / float(buy_cost_total)) * 100.0, 2) if buy_cost_total > 0 else 0.0
        
        return exec_price, gross_revenue, commission, stamp_duty, net_proceeds, net_pnl, pnl_pct


if __name__ == '__main__':
    cost_m = UnifiedCostModel()
    ep, ga, comm, tc = cost_m.calculate_buy_cost(10.0, 1000)
    print(f"买入10元1000股: 成交价={ep}, 佣金={comm}, 扣现金={tc}")
    
    ep_s, gr_s, comm_s, tax_s, np_s, pnl, pct = cost_m.calculate_sell_proceeds(10.8, 1000, tc)
    print(f"10.8元卖出1000股: 成交价={ep_s}, 佣金={comm_s}, 印花税={tax_s}, 净回款={np_s}, 净盈亏={pnl}元 ({pct}%)")
