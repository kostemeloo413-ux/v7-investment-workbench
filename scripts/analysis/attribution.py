# -*- coding: utf-8 -*-
"""
scripts/analysis/attribution.py
阶段五·任务E/G：平仓归因 + 买入理由消失检查 + 因子教训库

三件事：
1. attribute_close()   平仓时关联开仓快照，判定"因子方向 vs 盈亏"并回写 trace；
2. reason_gone_check() L6 检查：开仓时的利好因子是否已被反向事件证伪；
3. get_factor_lessons() 从已归因的平仓记录生成带标签教训（喂 reflection_memory）。

诚实协议：无快照的持仓（decision_trace 诞生前的历史仓位）如实标注，绝不伪归因。
"""

import os
import sys
import json
import datetime

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for p in (_BASE, os.path.join(_BASE, 'scripts')):
    if p not in sys.path:
        sys.path.insert(0, p)


def _parse_snapshot(raw):
    try:
        d = json.loads(raw or '{}')
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def attribute_close(blogger_id, symbol, pnl_pct):
    """
    平仓归因：pnl_pct 用费前口径 (卖价-成本)/成本*100。
    返回 {'label': str, 'trace_id': int|None}。label 也会被回写进 trace.attribution。
    """
    from datasource import db as hdb
    tr = hdb.get_open_trace(blogger_id, symbol)
    if not tr:
        return {'label': '无快照(历史交易)', 'trace_id': None}
    snap = _parse_snapshot(tr.get('factor_snapshot'))
    fnet = snap.get('factor_net')
    if fnet is None:
        label = '无因子(纯契约交易)'
    elif fnet >= 0.5 and pnl_pct > 0:
        label = f'因子命中(开仓净情绪{fnet:+.2f})'
    elif fnet >= 0.5 and pnl_pct <= 0:
        label = f'因子失效(开仓净情绪{fnet:+.2f}但未兑现)'
    elif fnet <= -0.5 and pnl_pct < 0:
        label = '利空验证(风险规避正确)'
    else:
        label = '中性/方向不明'
    now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    hdb.link_close_trace(blogger_id, symbol, pnl_pct, label, now)
    return {'label': label, 'trace_id': tr['id']}


def reason_gone_check(blogger_id, symbol):
    """
    L6 检查：开仓时依赖利好因子（净情绪≥+0.5），当前净情绪已转 ≤-0.5 → 事件证伪。
    返回证伪描述文本或 None（无快照/未证伪均如实返回 None）。
    """
    from datasource import db as hdb
    from analysis.decision_gate import _net_factor_sentiment
    tr = hdb.get_open_trace(blogger_id, symbol)
    if not tr:
        return None                       # 历史持仓无快照，诚实跳过
    snap = _parse_snapshot(tr.get('factor_snapshot'))
    fnet = snap.get('factor_net')
    if fnet is None or fnet < 0.5:
        return None                       # 当初不是靠利好因子开的仓
    cur, _ = _net_factor_sentiment(symbol)
    if cur is None:
        return None
    if cur <= -0.5:
        return f'开仓时利好因子(净{fnet:+.2f})，当前净情绪{cur:+.2f}——事件已证伪'
    return None


def get_factor_lessons(blogger_id, k=5):
    """从已平仓归因记录生成带标签教训（新→旧）。type: factor_win/factor_loss/no_factor。"""
    from datasource import db as hdb
    rows = hdb.recent_traces(limit=60)
    out = []
    for r in rows:
        if r.get('blogger_id') != blogger_id or r.get('action') != 'BUY':
            continue
        if not r.get('attribution') or r.get('pnl_pct') is None:
            continue
        pnl = float(r['pnl_pct'])
        attr = r['attribution']
        date = str(r.get('ts') or '')[:10]
        if '因子命中' in attr and pnl > 0:
            out.append({'type': 'factor_win', 'date': date,
                        'text': f'{date} 依事件因子买入{r.get("name")}赚+{pnl:.1f}%（{attr}）——因子驱动的机会可继续捕捉'})
        elif '因子失效' in attr or ('因子命中' in attr and pnl <= 0):
            out.append({'type': 'factor_loss', 'date': date,
                        'text': f'{date} 依事件因子买入{r.get("name")}亏{pnl:.1f}%（{attr}）——利好因子不保证兑现，须等确认'})
        if len([x for x in out if x]) >= k:
            break
    return out[:k]
