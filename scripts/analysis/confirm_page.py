# -*- coding: utf-8 -*-
"""
scripts/analysis/confirm_page.py
「今日操作确认单」页 · 辅助决策的最终呈现（阶段一新增）

设计立场：这是【辅助决策】系统——
- 一切数据带时间戳与新鲜度标记，过期就是过期，不装实时；
- AI观点(LLM) / 六策略共识(规则) / 模拟盘验证(账本) 三栏并列，互相校验；
- 不生成任何自动下单动作，最后落到"人工确认清单"。

由 daemon/app.py 的 /confirm 路由每次请求时现渲（无缓存陈旧问题）。
"""

import os
import sys
import json
import datetime

_BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_BASE, os.path.join(_BASE, 'scripts')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import runtime_config as rc

DATA_PATH = os.path.join(_BASE, 'workbench', 'data.json')
BLOGGER_ORDER = ['hanshu', 'yintwentao', 'liao-xiaoai', 'cool', 'ai-alpha', 'anti-demo']

STANCE_COLOR = {'BUY': '#dc2626', 'REDUCE': '#d97706',
                'WAIT': '#64748b', 'RISK': '#b91c1c'}


def _load():
    try:
        with open(DATA_PATH, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _age_minutes(ts):
    try:
        t = datetime.datetime.strptime(str(ts)[:19], '%Y-%m-%d %H:%M:%S')
        return round((datetime.datetime.now() - t).total_seconds() / 60)
    except Exception:
        return None


def _freshness_badge(ts, warn_min=45):
    age = _age_minutes(ts)
    if age is None:
        return '<span class="badge red">时间戳缺失</span>'
    if age <= warn_min:
        return f'<span class="badge green">新鲜 {ts[11:16]} ({age}分钟前)</span>'
    return f'<span class="badge yellow">已过时 {age}分钟</span>'


def render_html():
    d = _load()
    now = rc.now_str()
    phase = rc.market_phase()
    engine = d.get('research_engine', '?')

    # ---- 大盘行 ----
    ov = d.get('market_overview', {})
    idx_cells = []
    for sym in ['sh000001', 'sz399001', 'sz399006', 'sh000688', 'usIXIC', 'usINX']:
        q = ov.get(sym, {})
        stale = q.get('status') == 'stale'
        color_cls = 'muted' if stale else ('up' if str(q.get('pct', '')).startswith('+')
                                           else 'down')
        idx_cells.append(
            f'<div class="idx"><div class="idx-name">{q.get("name", sym)}</div>'
            f'<div class="idx-price {color_cls}">{q.get("price", "--")}'
            f'<span>{q.get("pct", "--")}</span></div></div>')
    overview_html = ''.join(idx_cells)

    # ---- 六博主研判汇总：AI观点(热点) + 策略选股 + 反思战绩 ----
    try:
        from analysis.reflection_memory import snapshot_stats
        stats = snapshot_stats()
    except Exception:
        stats = {}

    rows = []
    for bid in BLOGGER_ORDER:
        s = stats.get(bid, {})
        lessons = '<br>'.join(s.get('recent_lessons', [])[:2]) or '—'
        pick = next((p for p in (d.get('blogger_picks') or [])
                     if p.get('blogger_id') == bid), None)
        if pick:
            trig = bool(pick.get('triggered'))
            sel = (f'<b>{"🟢" if trig else "⚪"} {pick.get("stock_name","-")}</b> '
                   f'{pick.get("signal","")} @ {pick.get("current_price") or "-"}<br>'
                   f'<small>{str(pick.get("trigger_reason",""))[:80]}</small>')
        else:
            sel = '—'

        # 从热点里聚合该博主的LLM观点(取最近2条)
        views_html = []
        for h in (d.get('hotspots') or [])[:4]:
            v = (h.get('blogger_views') or {}).get(bid)
            meta_v = ((h.get('blogger_views_meta') or {}).get('views') or {}).get(bid)
            if not v:
                continue
            stance = (meta_v or {}).get('stance', '')
            conf = (meta_v or {}).get('confidence', '')
            color = STANCE_COLOR.get(stance, '#334155')
            views_html.append(
                f'<div class="view" style="border-left-color:{color}">'
                f'<span class="stance">{v[:46]}…</span>'
                f'<small>{h.get("time","")[:16]} {h.get("title","")[:26]}'
                f'</small></div>')
        views_cell = ''.join(views_html) or '<span class="muted">本周期无该博主观点</span>'

        rows.append(f'''
      <tr>
        <td><b>{bid}</b><br><small class="muted">平仓{s.get("closed_trades",0)}笔
          盈{s.get("wins",0)}/亏{s.get("losses",0)}<br>净额{s.get("net_pnl",0)}元</small></td>
        <td class="views-cell">{views_cell}</td>
        <td>{sel}</td>
        <td><small>{lessons}</small></td>
      </tr>''')

    hot_fresh = _freshness_badge(d.get('updated_at'))
    picks_fresh = _freshness_badge(d.get('picks_updated_at'))
    sim_fresh = _freshness_badge((d.get('simulation') or {}).get('updated_at'))

    offline_banner = ''
    if engine != 'llm':
        offline_banner = ('<div class="banner">⚠ LLM 研判引擎未启用'
                          '（.env 未配置 DEEPSEEK_API_KEY）——观点栏显示为诚实空缺，'
                          '不会再出现模板伪装观点。</div>')

    html = f'''<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="60">
<title>今日操作确认单 · {now[:10]}</title>
<style>
 body{{font-family:"Microsoft YaHei",sans-serif;background:#0f172a;color:#e2e8f0;
       margin:24px;}}
 h1{{font-size:22px}} h2{{font-size:16px;margin:18px 0 8px;color:#94a3b8}}
 .idx-row{{display:flex;gap:18px;flex-wrap:wrap;margin:10px 0 20px}}
 .idx{{background:#1e293b;border-radius:10px;padding:10px 16px;min-width:130px}}
 .idx-name{{color:#94a3b8;font-size:12px}}
 .idx-price{{font-size:18px;font-weight:700}}
 .idx-price span{{font-size:12px;margin-left:6px}}
 .up{{color:#ef4444}} .down{{color:#22c55e}} .muted{{color:#64748b}}
 table{{width:100%;border-collapse:collapse;background:#1e293b;border-radius:10px}}
 th,td{{padding:10px 12px;text-align:left;vertical-align:top;
        border-bottom:1px solid #334155;font-size:13px}}
 th{{color:#94a3b8;font-weight:normal}}
 .views-cell{{max-width:380px}}
 .view{{margin:4px 0;padding:4px 8px;background:#0f172a;border-left:3px solid #555;
       border-radius:4px}}
 .view small{{display:block;color:#94a3b8;margin-top:2px}}
 .stance{{font-size:13px}}
 .badge{{padding:2px 10px;border-radius:99px;font-size:12px;margin-right:8px}}
 .green{{background:#064e3b;color:#34d399}} .yellow{{background:#713f12;color:#fbbf24}}
 .red{{background:#7f1d1d;color:#fca5a5}}
 .banner{{background:#78350f;color:#fde68a;padding:10px 14px;border-radius:8px;
         margin:14px 0;font-size:13px}}
 .foot{{margin-top:16px;color:#475569;font-size:12px;line-height:1.8}}
</style></head><body>

<h1>📋 今日操作确认单 <span style="font-size:13px;color:#94a3b8">
[{phase == "open" and "盘中" or ("午间休市" if phase=="break" else ("盘前" if phase=="pre" else "已收盘"))} · 刷新于 {now}]</span></h1>

<div class="idx-row">{''.join(idx_cells)}</div>
{offline_banner}

<h2>① 数据新鲜度（决策前先看这个）</h2>
<div>研判快讯 {hot_fresh} 选股信号 {picks_fresh} 模拟盘 {sim_fresh}
<span class="badge {'green' if engine=='llm' else 'red'}">研判引擎: {engine}</span></div>

<h2>② 六博主 AI 观点 × 策略选股 × 真实战绩</h2>
<table>
<tr><th>博主(AI分身)</th><th>LLM研判·今日快讯观点</th><th>契约选股信号</th><th>近期真实盈亏教训</th></tr>
{''.join(rows)}
</table>

<h2>③ 使用纪律（确认单只到这一步，下单由你手动执行）</h2>
<div class="foot">
 · 单票仓位 ≤20% · 弱市配额减半 · 推送级告警到达微信即处理，不追高不硬扛<br>
 · LLM观点=语义解读层；选股信号=规则契约层；模拟盘=历史验证层——三者冲突时以模拟盘账本为准<br>
 · 本页每60秒自动刷新。任何一格数据过时,请先点托盘"手动跑一轮流水线"再读结论。
</div>
</body></html>'''
    return html


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    out = render_html()
    path = os.path.join(_BASE, 'outputs', 'confirm_preview.html')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(out)
    print('预览已输出:', path, f'({len(out)} chars)')
