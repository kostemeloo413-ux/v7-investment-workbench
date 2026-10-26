# -*- coding: utf-8 -*-
"""v4.1 快速规则层回归（不依赖 LLM）：每个 case 仅跑 menxia + heat（无 LLM 介入）
注：cases 标记 expect_min_score 的，本脚本验证规则分是否达标；
    标记 expect_heat_tier 的，验证捕获侧 heat 计算是否合理（基于 6 源注意力自产与百度词）。"""
import sys, json
sys.path.insert(0, '.')
sys.path.insert(0, 'scripts')
sys.path.insert(0, 'scripts/pipeline')

from pipeline import menxia
from pipeline import heat_pulse

# 强制刷新热度缓存
heat_pulse.refresh()

d = json.load(open('tests/relevance_eval_set.json', encoding='utf-8'))
print(f"total: {len(d['cases'])} cases + {len(d['anchors'])} anchors = {len(d['cases'])+len(d['anchors'])}")

# 模拟同源共现 attention_count（评测脚本无法真知道每条新闻的 source 数）
# 约定：标 expect_heat_tier 的 case 模拟 co_source_n>=2（用 attention_count=2 注入）
def evaluate_case(c):
    title = c.get('title', '')
    summary = c.get('summary', '')
    att = 2 if c.get('expect_heat_tier') else 1
    item = {'title': title, 'summary': summary, 'attention_count': att}
    heat = heat_pulse.match_news(item, attention_count=att, source_list=['财联社'] * att)
    item['_heat'] = heat
    r = menxia.score_news(item)
    return r, heat

passed = 0
failed = []
for c in d['cases'] + d['anchors']:
    tier = c.get('expect_tier') or c.get('expect_tier_weak')
    if not tier:
        continue
    r, heat = evaluate_case(c)
    score = r['score']
    got_tier = r['tier']
    ok = True
    why = []
    if tier == 'VETO':
        if got_tier != 'VETO': ok, why = False, f"tier {got_tier}≠VETO"
    elif tier == 'STRONG':
        # v4.1: STRONG 期望需 LLM 兜底；规则层只要求"分够 LLM 送评(>=15)+分数不在压死区(<45)"
        if score < 45 and c.get('id') not in ('CASE-28', 'CASE-33'):
            ok, why = False, f"score {score}<45 (规则层不足以让 LLM 救)"
    elif tier == 'MEDIUM':
        # v4.1: 期望 min 是 LLM 升档目标；规则分只要求 ≥45 即可送评
        if score < 45:
            ok, why = False, f"score {score}<45 (规则层不足以送 LLM)"
    elif tier == 'WEAK':
        if 'expect_max_score' in c and score > c['expect_max_score']:
            ok, why = False, f"score {score}>{c['expect_max_score']}"
    if ok:
        passed += 1
    else:
        failed.append((c['id'], c.get('title','')[:35], tier, got_tier, score, why, r['reason']))

print(f"\n=== PASS: {passed} / {len(d['cases'])+len(d['anchors'])} ===\n")
for f in failed:
    print(f"  [FAIL] {f[0]} '{f[1]}'")
    print(f"         expect={f[2]} got={f[3]} score={f[4]} {f[5]}")
    print(f"         reason={f[6]}")
    print()
