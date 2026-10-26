# -*- coding: utf-8 -*-
"""
scripts/analysis/reflection_memory.py
博主反思教训库 · 把"自我反思"第一次接入决策闭环（阶段一改造）

旧系统的问题：generate_reflections 生成的反思只躺在 data.json 里展示，
从未被检索回灌进任何研判 prompt——自改进闭环事实上不存在。

本模块从唯一真相（data/trades/*.json 账本）提取每位博主的真实战绩：
- 亏损交易 → 教训（何种信号买入、何种原因止损）
- 大额盈利 → 有效经验
供 llm_analyst 在研判 prompt 中注入该博主最近 N 条真实教训，
实现「反思 → 下次研判行为改变」的闭环。
"""

import os
import sys
import json

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

TRADES_DIR = os.path.join(_BASE, 'data', 'trades')
BLOGGER_IDS = ['hanshu', 'yintwentao', 'liao-xiaoai', 'cool', 'ai-alpha', 'anti-demo']


def _load_ledger(blogger_id):
    path = os.path.join(TRADES_DIR, blogger_id + '.json')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f).get('trades', [])
    except Exception:
        return []


def build_lessons(blogger_id):
    """从账本构建结构化教训列表（新→旧）。"""
    trades = _load_ledger(blogger_id)
    sells = [t for t in trades if t.get('direction') == 'SELL']
    lessons = []
    for t in reversed(sells):          # 新→旧
        pnl_pct = t.get('pnl_pct')
        name = t.get('name', t.get('symbol', '?'))
        reason = str(t.get('reason', ''))[:40]
        date = t.get('date', '')
        if pnl_pct is None:
            continue
        if pnl_pct < -0.5:
            lessons.append({'type': 'loss', 'date': date, 'text':
                            f'{date} 卖出{name}亏{pnl_pct}%（{reason}）——'
                            f'同类信号再次出现时应更警惕'})
        elif pnl_pct >= 3.0:
            lessons.append({'type': 'win', 'date': date, 'text':
                            f'{date} 卖出{name}赚+{pnl_pct}%（{reason}）——'
                            f'该类型机会值得优先捕捉'})
    # 阶段五·任务E：因子归因教训（带标签：因子命中/因子失效），与账本教训合并按日期排序
    try:
        from analysis.attribution import get_factor_lessons
        for it in get_factor_lessons(blogger_id, k=5):
            lessons.append({'type': it['type'], 'date': it['date'], 'text': it['text']})
    except Exception:
        pass  # 表未建/旧库时诚实跳过
    lessons.sort(key=lambda x: x.get('date', ''), reverse=True)
    return lessons


def get_lessons_for_prompt(blogger_id, k=5):
    """给 LLM 的紧凑教训文本，最多 k 条；无账本时返回空列表。"""
    ls = build_lessons(blogger_id)[:k]
    out = []
    for it in ls:
        prefix = '✅经验' if it['type'] == 'win' else '⚠️教训'
        out.append(f'{prefix}: {it["text"]}')
    return out


def snapshot_stats():
    """各博主简单战绩统计（用于健康页/确认单）。"""
    stats = {}
    for bid in BLOGGER_IDS:
        trades = _load_ledger(bid)
        sells = [t for t in trades if t.get('direction') == 'SELL']
        wins = sum(1 for t in sells if (t.get('pnl_pct') or 0) > 0)
        losses = sum(1 for t in sells if (t.get('pnl_pct') or 0) <= 0)
        total_pnl = sum(t.get('net_pnl', 0) or 0 for t in sells)
        stats[bid] = {
            'closed_trades': len(sells), 'wins': wins, 'losses': losses,
            'net_pnl': round(total_pnl, 2),
            'recent_lessons': get_lessons_for_prompt(bid, k=2),
        }
    return stats


if __name__ == '__main__':
    import sys as _s
    if hasattr(_s.stdout, 'reconfigure'):
        _s.stdout.reconfigure(encoding='utf-8')
    st = snapshot_stats()
    for bid, s in st.items():
        print(f"{bid}: 平仓{s['closed_trades']}笔 盈{s['wins']}/亏{s['losses']} "
              f"净额{s['net_pnl']}元")
        for l in s['recent_lessons']:
            print('   ', l)
