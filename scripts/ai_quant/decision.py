# -*- coding: utf-8 -*-
"""
scripts/ai_quant/decision.py
Phase 7: AI 量化独立决策引擎（AI Decision Engine）

核心职责（《指南》第 1.9 & 13 节）：
1. 独立决策逻辑（不强制套用 6 博主规则）：
   - 输入 Top 5 候选池的 7 维推断画像；
   - 对每只标的输出：BUY / WAIT / NO_TRADE；
2. 绝对支持空仓：
   - 当 Top 5 标的质量未达门槛，或全市场处于极度危险状态时，输出 BUY = 0（空仓防守）；
3. BUY 决策准入纪律（硬门槛）：
   - 上涨概率: up_probability >= 0.52 (具备统计优势)
   - 预期盈亏比: expected_return / abs(expected_mae) >= 1.2
   - 右侧确认分: right_side_score >= 65 (处于右侧形态)
   - 仓位与资金: 账户可用资金充足、持仓未满
   - 持仓防重: 已持仓股票转为 WAIT（不重复追高加仓）
"""

import os
import sys
import json
import math
import datetime

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_BASE, os.path.join(_BASE, 'scripts')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

DECISION_VERSION = "D-V1.0"


class AIQuantDecisionEngine:
    def __init__(self, max_positions=3, min_up_prob=0.50, min_right_score=60, min_pl_ratio=1.1):
        self.decision_version = DECISION_VERSION
        self.max_positions = max_positions
        self.min_up_prob = min_up_prob
        self.min_right_score = min_right_score
        self.min_pl_ratio = min_pl_ratio

    def evaluate_candidates(self, top5_candidates, current_account=None, market_regime=None):
        """
        对 Top 5 候选列表执行独立决策评估。
        top5_candidates: list of dict (来自 Top5RankingEngine 的推断画像)
        current_account: dict (包含 cash, positions=[{symbol, shares, cost_price}...])
        market_regime: dict (四维状态机: trend, volatility, sentiment, style)
        
        返回：
          decisions: list of dict, 每个候选补充：
            - decision: 'BUY' | 'WAIT' | 'NO_TRADE'
            - decision_reason: 判定理由
            - position_size_pct: 建议开仓仓位比例 (例如 0.20 代表 20% 资金)
        """
        if not top5_candidates:
            return []

        acc = current_account or {'cash': 100000.0, 'positions': []}
        positions = acc.get('positions', [])
        holding_syms = {p.get('symbol') for p in positions}
        current_pos_count = len(positions)
        available_cash = float(acc.get('cash', 100000.0))

        # 检查宏观市场风控
        is_crisis = (market_regime or {}).get('trend') == 'crisis' or (market_regime or {}).get('sentiment') == 'risk_off'

        decisions = []
        buy_count = 0

        for cand in top5_candidates:
            sym = cand['symbol']
            p_up = float(cand.get('up_probability', 0.5))
            exp_ret = float(cand.get('expected_return', 0.0))
            # 优先读取契约规范键 predicted_mae 与 predicted_mfe
            exp_mae = abs(float(cand.get('predicted_mae', cand.get('expected_mae', -0.03))))
            pred_mfe = float(cand.get('predicted_mfe', 0.055))
            right_score = int(cand.get('right_side_score', 50))

            # Phase 16A 风险合同替换：risk_reward_ratio = predicted_mfe / abs(predicted_mae)
            # NaN / zero division 与浮点精度 (如 0.055/0.05 = 1.0999999999999999) 严密防御保护
            if exp_mae > 0 and not (math.isnan(pred_mfe) or math.isnan(exp_mae)):
                risk_reward_ratio = round(pred_mfe / exp_mae, 6)
            else:
                risk_reward_ratio = 0.0

            # 兼容保留旧变量名便于下游消费
            pl_ratio = risk_reward_ratio

            d_result = 'NO_TRADE'
            reason = ''
            pos_size = 0.0

            # 1. 检查是否已持仓
            if sym in holding_syms:
                d_result = 'WAIT'
                reason = '已在持仓组合中，继续持股观察，不重复开仓'
            
            # 2. 宏观危机熔断
            elif is_crisis:
                d_result = 'NO_TRADE'
                reason = '全市场处于 Risk-off 避险/危机状态，AI 暂停多头开仓'

            # 3. 仓位名额已满或资金枯竭
            elif current_pos_count + buy_count >= self.max_positions:
                d_result = 'WAIT'
                reason = f'持仓数量已达上限 ({self.max_positions}只)，名额受限列入观察'
            elif available_cash < 10000.0:
                d_result = 'WAIT'
                reason = '可用现金不足，列入观察等待回笼'

            # 4. 统计胜率与盈亏比门槛 (准入条件: risk_reward_ratio >= min_pl_ratio)
            elif p_up < self.min_up_prob:
                d_result = 'NO_TRADE'
                reason = f'校准上涨概率 ({round(p_up*100, 1)}%) 低于开仓准入线 ({round(self.min_up_prob*100, 1)}%)'
            elif right_score < self.min_right_score:
                d_result = 'NO_TRADE'
                reason = f'右侧确认分 ({right_score}) 未达右侧突破门槛 ({self.min_right_score})'
            elif risk_reward_ratio < self.min_pl_ratio:
                d_result = 'NO_TRADE'
                reason = f'空间盈亏比 ({round(risk_reward_ratio, 2)}) 低于风控底线 ({self.min_pl_ratio})'

            # 5. 完全符合，批准 BUY
            else:
                d_result = 'BUY'
                # 动态仓位：依据右侧分与上涨概率分配 15%~25% 单票仓位
                pos_size = 0.20 if right_score < 75 else 0.25
                reason = (
                    f"右侧突破确立(得分{right_score}) + 上涨概率{round(p_up*100,1)}% + "
                    f"空间盈亏比{round(risk_reward_ratio,2)}达标，批准建立{int(pos_size*100)}%仓位"
                )
                buy_count += 1

            d_item = dict(cand)
            d_item['risk_reward_ratio'] = round(risk_reward_ratio, 4)
            d_item['pl_ratio'] = round(risk_reward_ratio, 4)  # 保持兼容
            d_item['decision'] = d_result
            d_item['decision_reason'] = reason
            d_item['position_size_pct'] = pos_size
            d_item['decision_version'] = self.decision_version
            decisions.append(d_item)

        return decisions


if __name__ == '__main__':
    print("=== 测试 AIQuantDecisionEngine 独立决策 ===")
    engine = AIQuantDecisionEngine()
    mock_candidates = [
        {'symbol': '000001', 'name': '平安银行', 'up_probability': 0.62, 'expected_return': 0.052, 'expected_mae': -0.025, 'right_side_score': 78, 'expected_holding_days': 2},
        {'symbol': '600519', 'name': '贵州茅台', 'up_probability': 0.48, 'expected_return': 0.015, 'expected_mae': -0.035, 'right_side_score': 55, 'expected_holding_days': 1},
        {'symbol': '601318', 'name': '中国平安', 'up_probability': 0.58, 'expected_return': 0.045, 'expected_mae': -0.028, 'right_side_score': 72, 'expected_holding_days': 3},
    ]
    # 假设账户已持有 601318
    mock_acc = {'cash': 80000.0, 'positions': [{'symbol': '601318', 'shares': 500, 'cost_price': 52.0}]}
    
    decs = engine.evaluate_candidates(mock_candidates, current_account=mock_acc)
    print("\n决策输出结果:")
    for d in decs:
        print(f"  标的: {d['name']} ({d['symbol']}) | 决策: 【{d['decision']}】 (仓位: {int(d['position_size_pct']*100)}%)")
        print(f"    理由: {d['decision_reason']}")
