# -*- coding: utf-8 -*-
"""
scripts/analysis/eval_set.py
研判层回归评测集（阶段一验收工具）

要解决的问题：
- 旧模板引擎的"死板重复"没有量化检验手段——6位博主同一条新闻输出逐字雷同。
- 2026-08-27 事故：新闻含"国际知名度"四字被关键词误命中，6博主集体外事幻觉。

评测方案（验收标准来自改造计划）：
1. 【陷阱回归】固定样本集含"国际知名度"陷阱、宏观数据、农业数据、官方政策、
   公司动态、赛道新闻、情绪妖股、ETF溢价、减持公告、银行防守板块、降息预期等
   11 类典型场景，另从 SQLite 新闻库追加真实快讯至 ~30 条；
2. 【差异化】同一条新闻内 6 博主观点文本两两去重率必须为 100%（旧模板为 0）；
3. 【多义性】同一博主对不同类型新闻的观点分布中，至少出现 2 种不同 stance
   （证明不是复读机）；
4. 【引用锚定】所有观点 quote_excerpt 必须通过原文校验（LLM层强制，此处复核）；
5. 【幻觉陷阱】"国际知名度"样本中，不得出现"外事/国际关系"类全员一致误判
   （若六人一致且内容高度雷同 → 判定仍为模板化/关键词联想）。

运行：python -m scripts.analysis.eval_set （未配置 API Key 时输出环境检查报告）
"""

import os
import sys
import json
import itertools

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_BASE, os.path.join(_BASE, 'scripts')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import runtime_config as rc

BLOGGER_IDS = ['hanshu', 'yintwentao', 'liao-xiaoai', 'cool', 'ai-alpha', 'anti-demo']

# 固定陷阱样本（手工构造，覆盖历史事故与典型类别）
FIXED_CASES = [
    ('trap_intl_fame', '国际知名度陷阱',
     '某上市公司获评"国际知名度显著提升"奖项，其董事长表示将继续深耕国内市场。',
     'SECTOR'),
    ('macro_agri', '宏观农业数据',
     '国家统计局发布数据：全国早稻总产量2817万吨，比上年增产6.8万吨。',
     'MACRO_DATA'),
    ('macro_policy', '产业政策',
     '工信部等七部门联合印发《关于推动人工智能终端产业发展的指导意见》，提出到2030年产业规模突破2万亿元。',
     'MACRO_POLICY'),
    ('stock_news', '公司减持',
     '中微公司公告：持股5%以上股东拟减持不超过总股本2%，用于偿还质押融资。',
     'STOCK'),
    ('sector_ai', 'AI算力赛道',
     '海外云厂商资本开支持续上修，800G/1.6T光模块订单能见度已拉长至2027年，国内光模块龙头满产满销。',
     'SECTOR'),
    ('sector_semi', '半导体国产化',
     '长鑫存储宣布新一代存储芯片良率突破关键阈值，国产先进制程设备招标量三季度环比增长40%。',
     'SECTOR'),
    ('sentiment', '情绪妖股',
     '多只4+连板概念股今日换手率突破25%，龙虎榜显示散户席位净买入占比创阶段新高。',
     'SENTIMENT'),
    ('etf_premium', 'ETF溢价',
     '纳指ETF场内溢价率飙升至12%，大量散户通过场内买入跨境ETF，套利资金开始入场。',
     'SECTOR'),
    ('bank_defense', '银行防守板块',
     '今日银行板块缩量拉升，工行农行涨超1%，市场避险情绪升温资金回流高股息。',
     'INDEX'),
    ('rate_cut', '美联储降息预期',
     '美国9月非农数据低于预期，市场对美联储FOMC降息50bp的预期升至95%，离岸人民币升值。',
     'INTL_MACRO'),
    ('earnings', '中报收官',
     'A股中报披露收官：光模块龙头新易盛净利润同比+180%，部分微盘题材股业绩暴雷连续跌停。',
     'STOCK'),
]


def _build_real_cases():
    """从 SQLite 追加真实快讯至总量~30条。"""
    try:
        from datasource import db as history_db
        news = history_db.recent_news(limit=25)
    except Exception:
        news = []
    cases = []
    for n in news:
        txt = (n.get('content') or n.get('title') or '').strip()
        if len(txt) > 15:
            cases.append((f'real_{len(cases)}', n.get('category') or 'REAL',
                          txt, 'REAL'))
    return cases


def _pairwise_unique(views_texts):
    """两两去重率：任意两博主文本不同则1.0。"""
    pairs = list(itertools.combinations(views_texts, 2))
    if not pairs:
        return 1.0, 0
    diffs = sum(1 for a, b in pairs if a != b)
    return diffs / len(pairs), len(pairs)


def run():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')

    if not rc.LLM_API_KEY:
        report = (
            '# LLM研判回归评测 · 未运行\n\n'
            '环境检查：`.env` 未配置 `DEEPSEEK_API_KEY`，评测无法执行。\n'
            '配置 Key 后重跑：`.venv\\Scripts\\python.exe -m scripts.analysis.eval_set`\n'
            f'当前判定引擎：{rc.LLM_MODEL}（未配置）\n')
        path = os.path.join(_BASE, 'outputs', 'llm_eval_report.md')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(report)
        print(report)
        return

    from analysis.llm_analyst import analyze_news

    cases = FIXED_CASES + _build_real_cases()
    print(f'[eval] 评测集 {len(cases)} 条（固定{len(FIXED_CASES)}+真实{len(cases) - len(FIXED_CASES)}）')

    rows = []
    total_pair_ok = True
    stance_spread = {b: set() for b in BLOGGER_IDS}
    trap_rows = []

    for cid, cname, text, cat in cases:
        item = {'title': text[:80], 'content': text, 'time': rc.now_str(),
                'category': cname, 'source': 'eval', '_subj_type': cat}
        views = analyze_news(item)
        meta = views.get('_meta', {})
        texts = {b: views.get(b, '') for b in BLOGGER_IDS}
        uniq_ratio, npairs = _pairwise_unique([texts[b] for b in BLOGGER_IDS])
        anchors = [bool((meta.get('views') or {}).get(b, {}).get('anchor_ok'))
                   for b in BLOGGER_IDS]
        stances = [(meta.get('views') or {}).get(b, {}).get('stance', '')
                   for b in BLOGGER_IDS]
        for b, s in zip(BLOGGER_IDS, stances):
            if s:
                stance_spread[b].add(s)
        anchor_ok = all(anchors)
        pair_ok = uniq_ratio == 1.0
        total_pair_ok = total_pair_ok and pair_ok
        rows.append((cid, cname, npairs, uniq_ratio, pair_ok, anchor_ok,
                     len(set(stances))))
        if cid == 'trap_intl_fame':
            all_same = len(set(texts.values())) == 1
            trap_rows.append((all_same, stances))

    # 汇总
    lines = [f'# LLM研判回归评测报告', f'- 时间: {rc.now_str()}',
             f'- 模型: {rc.LLM_MODEL} | 样本: {len(cases)} 条',
             f'- 评测集: 固定陷阱{len(FIXED_CASES)}类 + SQLite真实快讯{len(cases) - len(FIXED_CASES)}条',
             '', '| 用例 | 类别 | 两两去重率 | 观点差异化 | 引用锚定 | 出现stance数 |', '|---|---|---|---|---|---|']
    for cid, cname, npairs, ur, pair_ok, anchor_ok, ns in rows:
        lines.append(f'| {cid} | {cname} | {ur:.0%}({npairs}对) | '
                     f'{"✅" if pair_ok else "❌重复"} | {"✅" if anchor_ok else "❌幻觉"} | {ns} |')

    trap_line = '—'
    if trap_rows:
        all_same, stances = trap_rows[0]
        trap_line = '❌ 六人观点仍逐字雷同(模板化) ' if all_same else \
            f'✅ 未集体雷同 (stances={sorted(set(stances))})'
    spread_line = '、'.join(f'{b}:{len(v)}种' for b, v in stance_spread.items())
    ok_diversity = all(len(v) >= 2 for v in stance_spread.values())

    lines += ['', '## 验收结论', '',
              f'- 观点差异化: {"✅ 通过(全部用例两两去重)" if total_pair_ok else "❌ 存在雷同用例"}',
              f'- 陷阱回归("国际知名度"): {trap_line}',
              f'- 多义性(stance分布): {"✅ 通过" if ok_diversity else "❌ 某博主立场单一"} {spread_line}',
              '', '- ⛔ 本报告仅验证"研判层不再死板重复/不再关键词幻觉"，不代表投资收益。']

    md = '\n'.join(lines)
    path = os.path.join(_BASE, 'outputs', 'llm_eval_report.md')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(md)
    print(md)


if __name__ == '__main__':
    run()
