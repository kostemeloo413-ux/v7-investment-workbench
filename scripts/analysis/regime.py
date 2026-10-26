# -*- coding: utf-8 -*-
"""
scripts/analysis/regime.py
市场状态机（trend/volatility/sentiment/style 四维）

- 计算：每次取最新行情 + LLM事件因子 → 一个 dict
- 消费：诊断/看板/选股阈值/模拟盘配额 全部读这一个对象
- 目的：打破昨天提到的"各板块数据独立、孤岛"问题

状态字段（全部小写、便于跨模块校验）：
  trend     bull / bear / range / crisis
  volatility high / normal / low
  sentiment risk_on / neutral / risk_off
  style     growth / value / defensive / rotation
  as_of     时间戳
  meta      {evidence: {维度: 数值/依据}, version: 1}
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

import runtime_config as rc


def _avg_pct(quotes, syms):
    vals = [quotes.get(s, {}).get('pct') for s in syms]
    vals = [v for v in vals if isinstance(v, (int, float))]
    return sum(vals) / len(vals) if vals else 0.0


def _total_amount_yi(quotes):
    sh = (quotes.get('sh000001', {}).get('amount') or 0) / 10000.0
    sz = (quotes.get('sz399001', {}).get('amount') or 0) / 10000.0
    return sh + sz


def compute_regime(quotes, event_factors=None):
    """
    输入：腾讯实时 quotes（来自 fetch_market_quotes）+ LLM事件因子（可选）
    输出：dict with trend/volatility/sentiment/style/as_of/meta
    """
    now = datetime.datetime.now()
    sh_pct = quotes.get('sh000001', {}).get('pct') or 0.0
    cyb_pct = quotes.get('sz399006', {}).get('pct') or 0.0
    kcb_pct = quotes.get('sh000688', {}).get('pct') or 0.0
    sz_pct = quotes.get('sz399001', {}).get('pct') or 0.0
    avg_pct = _avg_pct(quotes, ['sh000001', 'sz399001', 'sz399006', 'sh000688'])
    amt_yi = _total_amount_yi(quotes)

    # ---- trend（趋势）----
    # 量价共振：涨幅>1% + 成交>1.5万亿 -> bull
    if avg_pct <= -2.0 or (sh_pct < -1.5 and (cyb_pct < -3.0 or kcb_pct < -3.0)):
        trend = 'crisis'
    elif avg_pct >= 1.0 and amt_yi >= 15000:
        trend = 'bull'
    elif avg_pct <= -1.2:
        trend = 'bear'
    else:
        trend = 'range'

    # ---- volatility（量能级别）----
    if amt_yi >= 18000:
        volatility = 'high'
    elif amt_yi >= 8000:
        volatility = 'normal'
    else:
        volatility = 'low'

    # ---- sentiment（情绪/风险偏好）----
    # 融合 LLM 事件因子：近期正负面事件累计得分的符号主导
    event_score = 0.0
    if event_factors:
        for ef in event_factors[:50]:
            s = ef.get('sentiment') or 0
            side = ef.get('impact_side') or 'NEUTRAL'
            sign = {'POSITIVE': 1, 'NEGATIVE': -1, 'NEUTRAL': 0}.get(side, 0)
            event_score += s * sign
    if avg_pct <= -1.0 or event_score < -5.0:
        sentiment = 'risk_off'
    elif avg_pct >= 0.8 and event_score > 5.0:
        sentiment = 'risk_on'
    else:
        sentiment = 'neutral'

    # ---- style（风格）----
    # 成长(kcb+cyb) 强于 价值(sh+sz50) -> growth
    growth = (kcb_pct + cyb_pct) / 2.0
    value = (sh_pct + (quotes.get('sh000016', {}).get('pct') or 0)) / 2.0
    if growth >= 1.5 and growth - value >= 1.0:
        style = 'growth'
    elif value >= 1.0 and value - growth >= 0.5:
        style = 'value'
    elif avg_pct < 0 and (sh_pct > 0 or cyb_pct > -1.0):
        style = 'defensive'
    else:
        style = 'rotation'

    return {
        'trend': trend,
        'volatility': volatility,
        'sentiment': sentiment,
        'style': style,
        'as_of': now.strftime('%Y-%m-%d %H:%M:%S'),
        'meta': {
            'version': 1,
            'avg_pct': round(avg_pct, 3),
            'total_amount_yi': round(amt_yi, 1),
            'event_score': round(event_score, 3),
            'sh_pct': round(sh_pct, 3), 'cyb_pct': round(cyb_pct, 3),
            'kcb_pct': round(kcb_pct, 3), 'sz_pct': round(sz_pct, 3),
        },
    }


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    import auto_update_hotspots as au
    quotes = au.fetch_market_quotes()
    try:
        from analysis.event_factors import factor_snapshot
        factors = factor_snapshot(since_hours=24)
    except Exception:
        factors = []
    r = compute_regime(quotes, factors)
    print(json.dumps(r, ensure_ascii=False, indent=1))
